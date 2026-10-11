"""Codex-owned V1 final acceptance → checkpoint → evaluation integration gate."""

from __future__ import annotations

import tempfile
from pathlib import Path

from euler.control.acceptance import (
    AcceptanceVerdict,
    FinalAuditKind,
    FinalAuditReceipt,
    ProjectOutcome,
    assess_final_acceptance,
    build_checkpoint_report,
    derive_project_outcome,
)
from euler.control.adapters.project import (
    FactSnapshot,
    MemoryRecord,
    ProjectSnapshot,
)
from euler.control.artifacts import ArtifactStore
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    TargetKind,
)
from euler.control.evaluation import (
    ComparisonContract,
    DatasetSplit,
    EvaluationRun,
    MetricStatus,
    RunMode,
    RunOutcome,
    SystemVariant,
    TaskSpec,
    UsageBreakdown,
    export_evaluation_json,
    summarize_runs,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation
from euler.control.policy import BudgetPolicy, DimensionLimit, ResourceSnapshot
from euler.control.runtime import RuntimeSnapshot
from euler.control.schema import (
    EventEnvelope,
    ObservationStatus,
    ObservedQuantity,
    UsageRecord,
)
from euler.control.views import ResearchView

_TS = "2026-09-18T00:00:00Z"
_TS2 = "2026-09-18T00:01:00Z"


def _event(key: str, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="phase7",
        actor="codex-main",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _quantity(unit: str, value: int) -> ObservedQuantity:
    return ObservedQuantity(
        unit=unit, status=ObservationStatus.OBSERVED, value=value
    )


def _usage(cost: int) -> UsageRecord:
    return UsageRecord(
        input_tokens=_quantity("tokens", 0),
        output_tokens=_quantity("tokens", 0),
        reasoning_tokens=_quantity("tokens", 0),
        cached_tokens=_quantity("tokens", 0),
        cost_microusd=_quantity("microusd", cost),
        wall_ms=_quantity("ms", 1),
        tool_cpu_ms=_quantity("ms", 0),
        tool_gpu_ms=_quantity("ms", 0),
    )


def _breakdown(cost: int) -> UsageBreakdown:
    zero = _usage(0)
    return UsageBreakdown(
        coordinator=zero,
        worker=_usage(cost),
        verifier=zero,
        context=zero,
        retry=zero,
        compute=zero,
    )


def _policy() -> BudgetPolicy:
    unlimited = DimensionLimit.unlimited()
    return BudgetPolicy(
        cost_microusd=unlimited,
        input_tokens=unlimited,
        output_tokens=unlimited,
        reasoning_tokens=unlimited,
        cached_tokens=unlimited,
        wall_ms=unlimited,
        tool_cpu_ms=unlimited,
        tool_gpu_ms=unlimited,
    )


def test_clean_toy_proof_acceptance_checkpoint_and_evaluation():
    scope = MathematicalScope.build(
        assumptions=("a is fixed",),
        quantifiers=(),
        objects=(),
        parameter_domain="singleton",
        definitions_hash=sha256_hex("phase7-definitions"),
    )
    claim = "The fixed object a equals itself."
    contract = ProblemContract.build(
        problem_id="phase7",
        version=1,
        exact_statement=claim,
        scope=scope,
        permitted_tools=("reasoning", "final audit"),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("fact admission", "independent final audit"),
        budget_policy_ref="phase7-policy",
    )
    obligation = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=claim,
        scope=scope,
        desired_output="complete checked proof",
        created_by="codex-main",
        version=1,
    )
    policy_hash = sha256_hex("phase7-acceptance-policy")

    with tempfile.TemporaryDirectory() as raw:
        artifacts = ArtifactStore(Path(raw) / "cas")
        proof_ref = artifacts.put_text("Independent proof evidence.")
        evidence = EvidenceArtifact.build(
            content_hash=proof_ref.content_hash,
            producer="worker",
            contract_id=contract.contract_id,
            evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
            exact_claim=claim,
            scope=scope,
            environment_hash=sha256_hex("proof-env"),
            checker_id="euler-verifier",
            checker_version="1",
            checker_result=CheckerResult.PASS,
            created_at=_TS,
        )
        view = ResearchView()
        view.apply(1, _event("contract", "problem.contract.created", {"contract": contract.to_dict()}))
        view.apply(2, _event("evidence", "evidence.registered", {"evidence": evidence.to_dict()}))
        view.apply(3, _event("obligation", "obligation.created", {"obligation": obligation.to_dict()}))
        view.apply(
            4,
            _event(
                "discharge",
                "obligation.status_changed",
                {
                    "obligation_id": obligation.obligation_id,
                    "status": "DISCHARGED",
                    "evidence_refs": [evidence.artifact_id],
                },
            ),
        )
        live = view.get_obligation(obligation.obligation_id)
        assert live is not None

        fact_id = "1234567890abcdef"
        raw_fact = f"## statement\n{claim}\n\n## proof\nReflexivity.\n"
        fact = FactSnapshot(
            fact_id=fact_id,
            relative_path=f"fact_graph/facts/{fact_id}.md",
            raw_markdown=raw_fact,
            content_sha256=sha256_hex(raw_fact),
            statement=claim,
            predecessors=(),
            external_refs=(),
        )
        admission_payload = {
            "id": "admission-1",
            "kind": "verification",
            "claim": claim,
            "provenance": "fact_submit",
            "transport_status": "ok",
            "verdict": "correct",
            "fact_id": fact_id,
        }
        snapshot = ProjectSnapshot(
            active_facts=(fact,),
            revoked_fact_ids=(),
            memory_records=(
                MemoryRecord(
                    kind="verification",
                    record_id="admission-1",
                    relative_path="global_memory/verification.jsonl",
                    canonical_json=canonical_json(admission_payload),
                ),
            ),
            receipts=(),
        )
        audit_ref = artifacts.put_text("Independent final audit: PASS")
        audit = FinalAuditReceipt.build(
            audit_kind=FinalAuditKind.PROOF,
            checker_result=CheckerResult.PASS,
            contract_id=contract.contract_id,
            obligation_id=obligation.obligation_id,
            action_id=sha256_hex("phase7-final-action"),
            exact_claim=claim,
            scope=scope,
            acceptance_policy_hash=policy_hash,
            auditor_id="independent-final-auditor",
            auditor_version="1",
            environment_hash=sha256_hex("audit-env"),
            audit_artifact_hash=audit_ref.content_hash,
            created_at=_TS,
            translation_confirmed=False,
            original_object_realized=False,
            fact_id=fact_id,
            evidence_ids=(evidence.artifact_id,),
        )
        accepted = assess_final_acceptance(
            contract,
            live,
            view,
            snapshot,
            audit,
            acceptance_policy_hash=policy_hash,
            artifacts=artifacts,
        )
        assert accepted.verdict is AcceptanceVerdict.PASS
        assert accepted.project_outcome is ProjectOutcome.SOLVED
        assert accepted.fact_id == fact_id

        resources = ResourceSnapshot.empty()
        runtime = RuntimeSnapshot(
            project_id="phase7",
            leases=(),
            active_lease_ids=(),
            resources=resources,
            verifier_slots_in_use=0,
        )
        outcome = derive_project_outcome(
            contract, accepted, _policy(), resources, view, runtime
        )
        assert outcome.outcome is ProjectOutcome.SOLVED
        checkpoint = build_checkpoint_report(
            contract,
            outcome,
            view,
            runtime,
            4,
            (accepted.record_id,),
            created_at=_TS2,
        )
        assert checkpoint.accepted_result_ids == (accepted.record_id,)
        assert checkpoint.created_at == _TS2

        common = {
            "model_config_hash": sha256_hex("same-model"),
            "tool_config_hash": sha256_hex("same-tools"),
            "context_config_hash": sha256_hex("same-context"),
            "verifier_config_hash": sha256_hex("same-final-verifier"),
        }
        task = TaskSpec.build(
            family="toy_reflexivity",
            split=DatasetSplit.TEST,
            initial_state_hash=sha256_hex("initial-state"),
            cutoff_hash=sha256_hex("cutoff"),
            budget_config_hash=sha256_hex("same-budget"),
            no_future_info_audit_hash=sha256_hex("no-future-info-audit"),
            future_derived=False,
            **common,
        )
        comparison = ComparisonContract.build(
            variants=(SystemVariant.BASELINE_PINNED, SystemVariant.EULER_V1),
            baseline_commit="0f3f7415518fd515faee79d333ec1dd72834886f",
            baseline_config_hash=sha256_hex("baseline-config"),
            compute_enabled=False,
            **common,
        )
        euler_run = EvaluationRun.build(
            task_id=task.task_id,
            variant=SystemVariant.EULER_V1,
            seed=0,
            outcome=RunOutcome.SOLVED,
            run_mode=RunMode.LIVE_BRANCH,
            usage=_breakdown(7),
            verified_error=False,
            started_at=_TS,
            ended_at=_TS2,
            acceptance_id=accepted.record_id,
        )
        baseline_run = EvaluationRun.build(
            task_id=task.task_id,
            variant=SystemVariant.BASELINE_PINNED,
            seed=0,
            outcome=RunOutcome.BUDGET_EXHAUSTED,
            run_mode=RunMode.LIVE_BRANCH,
            usage=_breakdown(11),
            verified_error=False,
            started_at=_TS,
            ended_at=_TS2,
        )
        summary = summarize_runs(
            (task,), (baseline_run, euler_run), comparison
        )
        solve = dict(summary.solve_at_budget)
        assert solve[SystemVariant.EULER_V1.value].numerator == 1
        assert solve[SystemVariant.EULER_V1.value].denominator == 1
        assert solve[SystemVariant.BASELINE_PINNED.value].numerator == 0
        assert dict(summary.cost_per_verified_solve)[
            SystemVariant.BASELINE_PINNED.value
        ].status is MetricStatus.UNDEFINED
        exported = export_evaluation_json(summary, (euler_run, baseline_run))
        assert '"metrics_label":"causal"' in exported
        assert str(11) in exported


def main() -> None:
    test_clean_toy_proof_acceptance_checkpoint_and_evaluation()
    print("  [ok] test_clean_toy_proof_acceptance_checkpoint_and_evaluation")
    print("ALL PHASE 7 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
