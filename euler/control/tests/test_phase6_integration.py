"""Codex-owned scheduler → lease → compute/CAS → settlement integration gate."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from euler.control.artifacts import ArtifactStore
from euler.control.budget import BudgetLedger
from euler.control.compute import (
    ComputeEvidenceKind,
    ComputeLimits,
    ComputeRequest,
    ComputeRunner,
    ComputeTermination,
)
from euler.control.compute_plugins.polynomial_counterexample import (
    PLUGIN_ID,
    SEARCHER_VERSION,
    IntegerInterval,
    PolynomialChecker,
    PolynomialPredicate,
    PolynomialRelation,
    PolynomialSearcher,
    PolynomialTerm,
)
from euler.control.context import ContextLimits
from euler.control.contracts import (
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.event_store import EventStore
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    DimensionLimit,
    ResourceSnapshot,
    ResourceVector,
)
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.runtime import LeaseStatus, RuntimeCoordinator
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    ScheduleDisposition,
    TransparentScheduler,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import (
    BudgetLimit,
    BudgetLimitKind,
    EventEnvelope,
    MathematicalStatus,
)
from euler.control.views import ResearchView

_TS = "2026-09-17T00:00:00Z"


def _event(key: str, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="phase6",
        actor="codex-main",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _policy() -> BudgetPolicy:
    unlimited = DimensionLimit.unlimited()
    return BudgetPolicy(
        cost_microusd=DimensionLimit.limited(1_000),
        input_tokens=unlimited,
        output_tokens=unlimited,
        reasoning_tokens=unlimited,
        cached_tokens=unlimited,
        wall_ms=DimensionLimit.limited(10_000),
        tool_cpu_ms=DimensionLimit.limited(10_000),
        tool_gpu_ms=DimensionLimit.limited(1),
        max_reasoning_slots=1,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def test_checked_polynomial_candidate_stays_outside_truth_until_alignment():
    scope = MathematicalScope.build(
        assumptions=("x is in the recorded finite interval",),
        quantifiers=(
            QuantifierBinding(
                variable="x",
                kind=QuantifierKind.FORALL,
                domain="integers from 0 through 3",
            ),
        ),
        objects=(),
        parameter_domain="0 <= x <= 3",
        definitions_hash=sha256_hex("phase6-definitions"),
    )
    tested_claim = "For every integer x from 0 through 3, x equals zero."
    contract = ProblemContract.build(
        problem_id="phase6",
        version=1,
        exact_statement=tested_claim,
        scope=scope,
        permitted_tools=("bounded integer polynomial search",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("independent alignment", "verifier"),
        budget_policy_ref="phase6-policy",
    )
    obligation = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=tested_claim,
        scope=scope,
        desired_output="checked counterexample candidate",
        created_by="codex-main",
        version=1,
    )
    view = ResearchView()
    view.apply(1, _event("contract", "problem.contract.created", {"contract": contract.to_dict()}))
    view.apply(2, _event("obligation", "obligation.created", {"obligation": obligation.to_dict()}))
    before_view = (view.contracts(), view.obligations(), view.evidence(), view.frontier())

    spec, context_request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap="Search the exact finite box for a violating integer assignment.",
        output_contract="Return persisted candidate and independent checker record.",
        tool_environment_hash=sha256_hex("phase6-tool-env"),
        acceptance_policy_hash=sha256_hex("phase6-acceptance"),
        reference_versions_hash=sha256_hex("phase6-references"),
        limits=ContextLimits.unlimited(),
        action_type="counterexample",
        mechanism="exact-polynomial-box",
        model_id="builtin-compute",
        effort="exact",
        task_mode="compute",
        parameters_json=canonical_json({"plugin": PLUGIN_ID}),
        dependency_hash=sha256_hex("phase6-dependencies"),
        definitions_hash=scope.definitions_hash,
    )
    candidate = CandidateAction(
        candidate_id="phase6-polynomial",
        action_spec=spec,
        context_request=context_request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        mechanism_family="exact-polynomial-box",
        action_kind=ActionKind.COUNTEREXAMPLE,
        slot_kind=slot_kind_for_action(ActionKind.COUNTEREXAMPLE),
        budget_class=BudgetClass.NORMAL,
        requested=ResourceVector(
            cost_microusd=0,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=1_000,
            tool_cpu_ms=1_000,
            tool_gpu_ms=0,
        ),
        priority_class=PriorityClass.DISCRIMINATING_CHEAP,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )
    plan = TransparentScheduler().select(
        (candidate,),
        policy=_policy(),
        snapshot=ResourceSnapshot.empty(),
        now_utc=_TS,
        view=view,
    )
    assert plan.decision_for(candidate.candidate_id).disposition is ScheduleDisposition.SELECTED

    predicate = PolynomialPredicate.build(
        variables=("x",),
        intervals=(IntegerInterval(lower=0, upper=3),),
        terms=(PolynomialTerm(coefficient=1, exponents=(1,)),),
        relation=PolynomialRelation.EQ,
    )
    searcher = PolynomialSearcher()
    checker = PolynomialChecker()
    compute_request = ComputeRequest.build(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=spec.action_id,
        tested_claim=tested_claim,
        scope=scope,
        plugin_id=PLUGIN_ID,
        plugin_version=SEARCHER_VERSION,
        plugin_payload=predicate.to_dict(),
        domain_hash=predicate.domain_hash(),
        declared_total_points=predicate.total_points(),
        limits=ComputeLimits(
            max_points=DimensionLimit.limited(predicate.total_points()),
            max_wall_ms=DimensionLimit.limited(1_000),
        ),
        searcher=searcher.identity,
        checker=checker.identity,
        tool_environment_hash=spec.tool_environment_hash,
        created_at=_TS,
    )
    assert compute_request.action_id == candidate.action_spec.action_id

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        artifacts = ArtifactStore(root / "cas")
        with EventStore.open(root / "control.sqlite3") as store:
            budget = BudgetLedger(store)
            budget.ensure_account(
                "phase6",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=1_000),
                timestamp_utc=_TS,
            )
            runtime = RuntimeCoordinator(_policy(), store, budget, artifacts)
            lease = runtime.issue(
                "phase6", candidate, idempotency_key="compute", timestamp_utc=_TS
            )
            assert lease.action_id == compute_request.action_id
            assert runtime.replay("phase6").resources.compute_slots_in_use == 1

            result = ComputeRunner().run(
                compute_request, searcher, checker, artifacts
            )
            assert result.termination is ComputeTermination.CHECKED_COUNTEREXAMPLE
            assert result.coverage.examined_points == 2
            assert result.coverage.enumeration_complete is False
            assert result.candidate_ref is not None
            assert json.loads(artifacts.get_bytes(result.candidate_ref.content_hash)) == {"x": 1}
            assert result.evidence is not None
            assert (
                result.evidence.evidence_kind
                is ComputeEvidenceKind.CHECKED_COUNTEREXAMPLE_CANDIDATE
            )
            assert result.evidence.action_id == lease.action_id
            assert not isinstance(result.evidence, EvidenceArtifact)
            assert view.evidence() == ()

            done = runtime.complete(
                "phase6",
                lease.lease_id,
                usage=result.usage,
                artifact_refs=(
                    result.candidate_ref.content_hash,
                    result.report_ref.content_hash,
                ),
                mathematical_status=MathematicalStatus.CANDIDATE,
                timestamp_utc="2026-09-17T00:00:01Z",
            )
            assert done.status is LeaseStatus.COMPLETED
            assert done.mathematical_status is MathematicalStatus.CANDIDATE
            replayed = runtime.replay("phase6")
            assert replayed.resources.compute_slots_in_use == 0
            assert replayed.active_lease_ids == ()

    after_view = (view.contracts(), view.obligations(), view.evidence(), view.frontier())
    assert after_view == before_view


def main() -> None:
    test_checked_polynomial_candidate_stays_outside_truth_until_alignment()
    print("  [ok] test_checked_polynomial_candidate_stays_outside_truth_until_alignment")
    print("ALL PHASE 6 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
