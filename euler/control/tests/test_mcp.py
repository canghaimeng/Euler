"""Role and serialization tests for the euler-control MCP."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from euler.control.mcp import (
    CONTROL_TOOLS,
    build_app,
    euler_analyze_recurrence,
    euler_checkpoint,
    euler_project_status,
    euler_record_failure,
    euler_register_contract,
    euler_schedule,
    tools_for,
)
from euler.control.cli import do_propose
from euler.control.failures import ConfidenceClass, FailureKind, FailureRecord
from euler.control.context import ContextLimits
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.execution.scaffold import do_new
from euler.gateway.roles import tools_for as gateway_tools_for

_TS = "2026-01-01T00:00:00Z"


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain="Z"),
        ),
        objects=(),
        parameter_domain="Z",
        definitions_hash=_hex("defs"),
    )


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="goldbach",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _policy() -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    return BudgetPolicy(
        cost_microusd=DimensionLimit.limited(10_000),
        input_tokens=unbound,
        output_tokens=unbound,
        reasoning_tokens=unbound,
        cached_tokens=unbound,
        wall_ms=unbound,
        tool_cpu_ms=unbound,
        tool_gpu_ms=unbound,
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _candidate(contract: ProblemContract, obligation: Obligation, tag: str) -> CandidateAction:
    spec, request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap="gap",
        output_contract="proof",
        tool_environment_hash=_hex("tool"),
        acceptance_policy_hash=_hex("accept"),
        reference_versions_hash=_hex("refs"),
        limits=ContextLimits.unlimited(),
        action_type="prove",
        mechanism=tag,
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"k": tag}),
        dependency_hash=_hex("dep", tag),
        definitions_hash=_hex("defs"),
    )
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        mechanism_family=tag,
        action_kind=ActionKind.PROVE,
        slot_kind=slot_kind_for_action(ActionKind.PROVE),
        budget_class=BudgetClass.NORMAL,
        requested=ResourceVector(
            cost_microusd=1,
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=1,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )


def test_role_table_worker_has_no_control_tools():
    assert tools_for("worker") == []
    assert tools_for("verifier") == []
    assert tools_for("nope") == []
    assert list(tools_for("main")) == list(CONTROL_TOOLS)
    assert "fact_submit" not in tools_for("main")
    for name in CONTROL_TOOLS:
        assert name not in gateway_tools_for("worker")
        assert name not in gateway_tools_for("main")
    for role in ("worker", "main", "verifier", "all"):
        assert build_app(role) is not None


def test_non_main_tools_refuse_to_run():
    with _env(EULER_ROLE="worker"):
        _raises(RuntimeError, euler_project_status, "demo")
    with _env(EULER_ROLE=None):
        _raises(RuntimeError, euler_project_status, "demo")


def test_register_status_schedule_serialization(tmp_path: Path):
    del tmp_path
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        contract_file = tmp / "worker.md"
        contract_file.write_text("# worker\n", encoding="utf-8")
        skills = tmp / "skills"
        skills.mkdir()
        with _env(
            EULER_AGENTS_ROOT=str(tmp / "agents"),
            EULER_WORKER_CONTRACT=str(contract_file),
            EULER_WORKER_SKILLS=str(skills),
            EULER_ROLE="main",
        ):
            do_new("demo", roles="high:1")
            contract = _contract()
            policy = _policy()
            out = euler_register_contract(
                "demo", contract.to_dict(), policy.to_dict()
            )
            assert out["control_plane"] == "euler"
            again = euler_register_contract(
                "demo", contract.to_dict(), policy.to_dict()
            )
            assert again["manifest_hash"] == out["manifest_hash"]
            status = euler_project_status("demo")
            assert status["project_id"] == "demo"
            assert status["contract_id"] == out["contract_id"]
            from euler.control.cli import target_obligation_from_contract

            ob = target_obligation_from_contract(contract, created_by="euler-control")
            cand = _candidate(contract, ob, "alpha")
            proposed = do_propose("demo/high", cand.to_dict(), timestamp_utc=_TS)
            assert proposed["candidate_id"] == "alpha"
            scheduled = euler_schedule("demo")
            assert scheduled["launch"] is False
            assert scheduled["dispatched"] == []
            assert "alpha" in scheduled["selected_candidate_ids"]
            decisions = scheduled["plan"]["decisions"]
            assert decisions == sorted(decisions, key=lambda item: item["candidate_id"])
            other = ProblemContract.build(
                problem_id="other",
                version=1,
                exact_statement="a different statement",
                scope=_scope(),
                permitted_tools=("lean",),
                external_help_policy="none",
                target_kind=TargetKind.PROOF,
                required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
                completion_checks=("proof checked",),
                budget_policy_ref="default-budget",
            )
            _raises(
                RuntimeError,
                euler_register_contract,
                "demo",
                other.to_dict(),
                policy.to_dict(),
            )
            failure = FailureRecord.build(
                kind=FailureKind.INFRASTRUCTURE_FAILURE,
                exact_failed_claim="worker crashed",
                scope=contract.scope,
                confidence=ConfidenceClass.HIGH,
                context_version=1,
                provenance="test",
                created_at=_TS,
            )
            recorded = euler_record_failure("demo", failure.to_dict())
            assert recorded["failure_id"] == failure.failure_id
            rec = euler_analyze_recurrence("demo")
            assert rec["auto_park"] is False
            assert rec["recommendation"]
            class _Fake:
                def to_dict(self):
                    return {"status": "DEFERRED", "replayed": False}

            from unittest.mock import patch
            with patch("euler.control.cli.run_final_audit", return_value=_Fake()):
                ck = euler_checkpoint("demo")
            assert ck["run"]["status"] == "DEFERRED"


def main() -> None:
    tests = [
        test_role_table_worker_has_no_control_tools,
        test_non_main_tools_refuse_to_run,
        test_register_status_schedule_serialization,
    ]
    for test in tests:
        if test.__name__ == "test_register_status_schedule_serialization":
            test(Path("."))
        else:
            test()
        print(f"  [ok] {test.__name__}")
    print("ALL CONTROL MCP TESTS PASSED")


if __name__ == "__main__":
    main()
