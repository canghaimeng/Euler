"""Codex-owned Phase 5 scheduler → context → lease integration gate."""

from __future__ import annotations

import tempfile
from pathlib import Path

from euler.control.adapters.project import ProjectSnapshot
from euler.control.artifacts import ArtifactStore
from euler.control.budget import BudgetLedger
from euler.control.context import ContextCompiler, ContextLimits
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    TargetKind,
)
from euler.control.event_store import EventStore
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    DimensionLimit,
    OnDeadlinePolicy,
    ResourceSnapshot,
    ResourceUsage,
    ResourceVector,
)
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.runtime import LeaseStatus, RuntimeCoordinator, RuntimeError
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    ScheduleDisposition,
    ScheduleReason,
    TransparentScheduler,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import (
    BudgetLimit,
    BudgetLimitKind,
    EventEnvelope,
    UsageRecord,
)
from euler.control.views import ResearchView

_TS = "2026-09-17T00:00:00Z"
_AFTER_DEADLINE = "2026-09-18T00:00:00Z"


def _event(key: str, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="phase5",
        actor="codex-main",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _vector(*, cost: int = 100) -> ResourceVector:
    return ResourceVector(
        cost_microusd=cost,
        input_tokens=10,
        output_tokens=20,
        reasoning_tokens=30,
        cached_tokens=5,
        wall_ms=1_000,
        tool_cpu_ms=10,
        tool_gpu_ms=0,
    )


def _policy() -> BudgetPolicy:
    limit = DimensionLimit.limited(10_000)
    return BudgetPolicy(
        cost_microusd=limit,
        input_tokens=limit,
        output_tokens=limit,
        reasoning_tokens=limit,
        cached_tokens=limit,
        wall_ms=limit,
        tool_cpu_ms=limit,
        tool_gpu_ms=limit,
        max_reasoning_slots=1,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
        final_audit_reserve_bp=2_000,
        exploration_reserve_bp=1_000,
        deadline_utc="2026-09-17T12:00:00Z",
        on_deadline=OnDeadlinePolicy.REJECT_NORMAL,
    )


def _candidate(
    contract: ProblemContract,
    obligation: Obligation,
    tag: str,
    *,
    action_kind: ActionKind = ActionKind.PROVE,
    budget_class: BudgetClass = BudgetClass.NORMAL,
    priority: PriorityClass = PriorityClass.NORMAL,
    sequence: int = 1,
) -> CandidateAction:
    spec, request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap="Close the remaining scoped obligation.",
        output_contract="Return evidence for verifier review.",
        tool_environment_hash=sha256_hex("phase5-tools"),
        acceptance_policy_hash=sha256_hex("phase5-acceptance"),
        reference_versions_hash=sha256_hex("phase5-references"),
        limits=ContextLimits.unlimited(),
        action_type=action_kind.value.lower(),
        mechanism=tag,
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"tag": tag}),
        dependency_hash=sha256_hex("phase5-dependencies"),
        definitions_hash=contract.scope.definitions_hash,
    )
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        mechanism_family=tag,
        action_kind=action_kind,
        slot_kind=slot_kind_for_action(action_kind),
        budget_class=budget_class,
        requested=_vector(),
        priority_class=priority,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=sequence,
    )


def _unknown_usage() -> UsageRecord:
    return UsageRecord.from_dict(ResourceUsage.unknown().to_dict())


def test_scheduler_context_runtime_replay_and_final_audit_reserve():
    scope = MathematicalScope.build(
        assumptions=("fixed assumptions",),
        quantifiers=(),
        objects=(),
        parameter_domain="fixed domain",
        definitions_hash=sha256_hex("phase5-definitions"),
    )
    contract = ProblemContract.build(
        problem_id="phase5",
        version=1,
        exact_statement="Prove the fixed target.",
        scope=scope,
        permitted_tools=("reasoning", "verify"),
        external_help_policy="configured workers only",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("verify",),
        budget_policy_ref="phase5-policy",
    )
    obligation = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=scope,
        desired_output="checked proof candidate",
        created_by="codex-main",
        version=1,
    )
    view = ResearchView()
    research_events = (
        _event("contract", "problem.contract.created", {"contract": contract.to_dict()}),
        _event("obligation", "obligation.created", {"obligation": obligation.to_dict()}),
    )
    for seq, event in enumerate(research_events, 1):
        view.apply(seq, event)
    before_view = (
        view.contracts(),
        view.obligations(),
        view.routes(),
        view.relations(),
        view.failures(),
        view.frontier(),
    )

    primary = _candidate(contract, obligation, "direct", sequence=1)
    competing = _candidate(contract, obligation, "alternate", sequence=2)
    policy = _policy()
    scheduler = TransparentScheduler()
    plan = scheduler.select(
        (competing, primary),
        policy=policy,
        snapshot=ResourceSnapshot.empty(),
        now_utc=_TS,
        view=view,
    )
    assert plan.selected_candidate_ids == ("direct",)
    assert plan.decision_for("alternate").reason is ScheduleReason.SLOT_EXHAUSTED

    empty_snapshot = ProjectSnapshot(
        active_facts=(), revoked_fact_ids=(), memory_records=(), receipts=()
    )
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        artifacts = ArtifactStore(root / "cas")
        packet = ContextCompiler().compile(
            primary.context_request, view, empty_snapshot, artifacts
        )
        with EventStore.open(root / "control.sqlite3") as store:
            for event in research_events:
                store.append(event)
            budget = BudgetLedger(store)
            budget.ensure_account(
                "phase5",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=10_000),
                timestamp_utc=_TS,
            )
            runtime = RuntimeCoordinator(policy, store, budget, artifacts)
            lease = runtime.issue(
                "phase5",
                primary,
                idempotency_key="primary",
                timestamp_utc=_TS,
                context_cache_key=packet.cache_key.key_id,
            )
            assert lease.context_seed_hash == primary.context_seed.seed_hash
            assert lease.context_cache_key == packet.cache_key.key_id
            try:
                runtime.issue(
                    "phase5",
                    competing,
                    idempotency_key="competing",
                    timestamp_utc=_TS,
                )
            except RuntimeError as exc:
                assert "slot" in str(exc)
            else:
                raise AssertionError("runtime admitted a second reasoning lease")

            running = runtime.mark_running(
                "phase5", lease.lease_id, timestamp_utc="2026-09-17T00:00:01Z"
            )
            assert running.status is LeaseStatus.RUNNING
            completed = runtime.complete(
                "phase5",
                lease.lease_id,
                usage=_unknown_usage(),
                timestamp_utc="2026-09-17T00:00:02Z",
            )
            assert completed.status is LeaseStatus.COMPLETED
            replayed = runtime.replay("phase5")
            assert replayed.active_lease_ids == ()
            assert replayed.resources.reasoning_slots_in_use == 0
            assert replayed.resources.consumed == primary.requested

            final = _candidate(
                contract,
                obligation,
                "final-verifier",
                action_kind=ActionKind.VERIFY,
                budget_class=BudgetClass.FINAL_AUDIT,
                priority=PriorityClass.FINAL_ACCEPTANCE,
                sequence=3,
            )
            after_deadline = scheduler.select(
                (primary, final),
                policy=policy,
                snapshot=replayed.resources,
                now_utc=_AFTER_DEADLINE,
                view=view,
            )
            assert after_deadline.decision_for("direct").disposition is ScheduleDisposition.DEFERRED
            assert after_deadline.decision_for("direct").reason is ScheduleReason.DEADLINE
            assert after_deadline.decision_for("final-verifier").disposition is ScheduleDisposition.SELECTED
            final_lease = runtime.issue(
                "phase5", final, idempotency_key="final", timestamp_utc=_AFTER_DEADLINE
            )
            assert runtime.replay("phase5").resources.verifier_slots_in_use == 1
            runtime.cancel(
                "phase5",
                final_lease.lease_id,
                timestamp_utc="2026-09-18T00:00:01Z",
            )
            assert runtime.replay("phase5").resources.verifier_slots_in_use == 0

    after_view = (
        view.contracts(),
        view.obligations(),
        view.routes(),
        view.relations(),
        view.failures(),
        view.frontier(),
    )
    assert after_view == before_view


def main() -> None:
    test_scheduler_context_runtime_replay_and_final_audit_reserve()
    print("  [ok] test_scheduler_context_runtime_replay_and_final_audit_reserve")
    print("ALL PHASE 5 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
