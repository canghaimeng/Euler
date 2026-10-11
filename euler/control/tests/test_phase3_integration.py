"""Codex-owned integration tests for conservative recurrence control."""

from __future__ import annotations

import tempfile
from pathlib import Path

from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    TargetKind,
)
from euler.control.dedupe import ActionAttemptIndex, DedupeDecision
from euler.control.event_store import EventStore
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, Route
from euler.control.progress import ActionSpec, ProgressKind, SamplingMode
from euler.control.recurrence import (
    RecurrenceHistory,
    RecurrenceRecommendation,
)
from euler.control.schema import EventEnvelope, InfrastructureStatus
from euler.control.scc import SCCClassification
from euler.control.views import ResearchView


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("fixed assumptions",),
        quantifiers=(),
        objects=(),
        parameter_domain="fixed domain",
        definitions_hash=sha256_hex("phase3-definitions"),
    )


def _event(key: str, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="phase3",
        actor="codex-main",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc="2026-09-17T00:00:00Z",
    )


def _build_cycle_with_exit(store: EventStore) -> tuple[ResearchView, Route]:
    scope = _scope()
    contract = ProblemContract.build(
        problem_id="phase3",
        version=1,
        exact_statement="Target A",
        scope=scope,
        permitted_tools=("reasoning",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("verify",),
        budget_policy_ref="budget-v1",
    )
    a = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="A",
        scope=scope,
        desired_output="proof",
        created_by="codex-main",
        version=1,
    )
    b = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="B",
        scope=scope,
        desired_output="proof",
        created_by="codex-main",
        version=1,
    )
    c = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="C outside cycle",
        scope=scope,
        desired_output="proof",
        created_by="codex-main",
        version=1,
    )
    ab = Route.build(
        target_obligation_id=a.obligation_id,
        mechanism="A through B",
        required_obligation_ids=(b.obligation_id,),
        alternative_group="cycle",
    )
    ba = Route.build(
        target_obligation_id=b.obligation_id,
        mechanism="B through A",
        required_obligation_ids=(a.obligation_id,),
        alternative_group="cycle",
    )
    exit_route = Route.build(
        target_obligation_id=a.obligation_id,
        mechanism="independent exit through C",
        required_obligation_ids=(c.obligation_id,),
        alternative_group="exit",
    )
    payloads = [
        ("1", "problem.contract.created", {"contract": contract.to_dict()}),
        ("2", "obligation.created", {"obligation": a.to_dict()}),
        ("3", "obligation.created", {"obligation": b.to_dict()}),
        ("4", "obligation.created", {"obligation": c.to_dict()}),
        ("5", "route.created", {"route": ab.to_dict()}),
        ("6", "route.created", {"route": ba.to_dict()}),
        ("7", "route.created", {"route": exit_route.to_dict()}),
    ]
    for key, event_type, payload in payloads:
        store.append(_event(key, event_type, payload))
    view = ResearchView()
    view.replay(store.iter_project("phase3"))
    return view, exit_route


def test_exact_dedupe_and_explicit_sampling():
    spec = ActionSpec.build(
        action_type="prove",
        mechanism="bounded proof attempt",
        model_id="grok-4.6",
        effort="high",
        task_mode="direct",
        context_hash=sha256_hex("context"),
        dependency_hash=sha256_hex("deps"),
        definitions_hash=sha256_hex("defs"),
        tool_environment_hash=sha256_hex("tools"),
        parameters_json=canonical_json({"cap": 1}),
    )
    index = ActionAttemptIndex()
    assert index.decide(spec) is DedupeDecision.ISSUE
    first = index.start(spec, sha256_hex("attempt-1"))
    assert index.decide(spec) is DedupeDecision.WAIT_FOR_EXISTING
    index.finish(
        first.attempt_id,
        infrastructure_status=InfrastructureStatus.FAILED,
    )
    assert index.decide(spec) is DedupeDecision.RETRY_TRANSPORT
    retry = index.start(spec, sha256_hex("attempt-2"))
    index.finish(
        retry.attempt_id,
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        result="durable result",
    )
    assert index.decide(spec) is DedupeDecision.RECOVER_OR_REUSE

    new_sample = ActionSpec.build(
        action_type=spec.action_type,
        mechanism=spec.mechanism,
        model_id=spec.model_id,
        effort=spec.effort,
        task_mode=spec.task_mode,
        context_hash=spec.context_hash,
        dependency_hash=spec.dependency_hash,
        definitions_hash=spec.definitions_hash,
        tool_environment_hash=spec.tool_environment_hash,
        parameters_json=spec.parameters_json,
        sampling_mode=SamplingMode.EXPLICIT_NEW,
        sampling_nonce="sample-2",
    )
    assert new_sample.action_id == spec.action_id
    assert index.decide(new_sample) is DedupeDecision.ALLOW_NEW_SAMPLE


def test_or_exit_exhaustion_to_bounded_park_candidate_without_view_mutation():
    with tempfile.TemporaryDirectory() as raw:
        with EventStore(Path(raw) / "control.sqlite3") as store:
            view, exit_route = _build_cycle_with_exit(store)
            before = tuple(item.to_dict() for item in view.routes())
            history = RecurrenceHistory(threshold=3)
            tool_hash = sha256_hex("tool-v1")

            first = history.evaluate(view, tool_hash=tool_hash)
            assert first.classification is SCCClassification.HAS_EXTERNAL_EXIT
            assert first.recommendation is RecurrenceRecommendation.CHANGE_ROUTE
            assert first.audit_required is False

            strike1 = history.evaluate(
                view,
                tool_hash=tool_hash,
                progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                no_verifiable_progress_route_id=exit_route.route_id,
            )
            assert strike1.classification is SCCClassification.SUSPECTED_RECURRENCE
            assert strike1.strike_count == 1
            assert strike1.audit_required is False
            strike2 = history.evaluate(view, tool_hash=tool_hash)
            assert strike2.strike_count == 2
            final = history.evaluate(view, tool_hash=tool_hash)
            assert final.classification is SCCClassification.RECURRENT_NO_EXIT
            assert final.strike_count == 3
            assert final.recommendation is RecurrenceRecommendation.PARK_ROUTE_CANDIDATE
            assert final.audit_required is True
            assert final.exhausted_route_ids == (exit_route.route_id,)
            assert tuple(item.to_dict() for item in view.routes()) == before


def main() -> None:
    tests = [
        test_exact_dedupe_and_explicit_sampling,
        test_or_exit_exhaustion_to_bounded_park_candidate_without_view_mutation,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL PHASE 3 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
