"""v0.2 recall, persistent recurrence, and action-reuse tests.

Covers report-aligned behavior: interface-only default proofs, automatic
scoped blocker recall, revocation/caps, observe/replay across recreated
objects, poll isolation, unrelated progress, legal descent, and persistent
action reuse.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from euler.control.adapters.project import FactSnapshot, ProjectSnapshot
from euler.control.artifacts import ArtifactStore
from euler.control.context import (
    ContextCompiler,
    ContextLimits,
    ContextRequest,
    ContextTooLarge,
    MissingRequiredContext,
)
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.dedupe import (
    ActionAttemptIndex,
    DedupeDecision,
    build_action_attempt_index,
    ingest_runtime_lease,
)
from euler.control.failures import ConfidenceClass, FailureKind, FailureRecord
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, Relation, RelationType, Route, RouteStatus
from euler.control.persistent_recurrence import (
    EVENT_RECURRENCE_OBSERVED,
    RecurrenceObservation,
    analyze_recurrence,
    history_from_state,
    observe_recurrence,
    replay_recurrence_history,
)
from euler.control.progress import ActionSpec, ProgressKind, SamplingMode
from euler.control.recall import LOOP_INTEGRATION, recall_for_action
from euler.control.recurrence import RecurrenceHistory, RecurrenceRecommendation
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
from euler.control.schema import EventEnvelope, InfrastructureStatus
from euler.control.scc import SCCClassification
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceSnapshot, ResourceVector
from euler.control.views import ResearchView

_TS = "2026-01-01T00:00:00Z"
_PROOF = "By a very long ancestral argument that must not inflate the default prompt.\n" * 40


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope(*, domain: str = "Z") -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain=domain),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
    )


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="odd_sum",
        version=1,
        exact_statement="∀n∈ℕ, n is a sum of odd numbers.",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract, **kwargs) -> Obligation:
    fields = dict(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )
    fields.update(kwargs)
    return Obligation.build(**fields)


def _evidence(contract: ProblemContract, *, content: str = "ev-bytes", **kwargs) -> EvidenceArtifact:
    fields = dict(
        content_hash=sha256_hex(content.encode("utf-8")),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim=contract.exact_statement,
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker",
        checker_version="1",
        checker_result=CheckerResult.PASS,
    )
    fields.update(kwargs)
    return EvidenceArtifact.build(**fields)


def _event(event_type: str, payload: dict, key: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="tester",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _fact(fact_id: str, statement: str, proof: str, *, predecessors=()) -> FactSnapshot:
    raw = (
        "---\n"
        f"fact_id: {fact_id}\n"
        "---\n\n"
        "## statement\n"
        f"{statement}\n\n"
        "## proof\n"
        f"{proof}\n"
    )
    return FactSnapshot(
        fact_id=fact_id,
        relative_path=f"fact_graph/facts/{fact_id}.md",
        raw_markdown=raw,
        content_sha256=sha256_hex(raw.encode("utf-8")),
        statement=statement,
        predecessors=tuple(predecessors),
        external_refs=(),
    )


def _seed_view(contract, obligation, evidence, **extra):
    view = ResearchView()
    seq = 1
    view.apply(seq, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    seq += 1
    view.apply(seq, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e"))
    seq += 1
    view.apply(seq, _event("obligation.created", {"obligation": obligation.to_dict()}, "o"))
    seq += 1
    route = extra.get("route")
    if route is not None:
        view.apply(seq, _event("route.created", {"route": route.to_dict()}, "r"))
        seq += 1
    for index, failure in enumerate(extra.get("failures", ())):
        view.apply(
            seq,
            _event("failure.recorded", {"failure": failure.to_dict()}, f"f{index}"),
        )
        seq += 1
    for index, relation in enumerate(extra.get("relations", ())):
        view.apply(
            seq,
            _event("relation.created", {"relation": relation.to_dict()}, f"rel{index}"),
        )
        seq += 1
    return view


def _request(contract, obligation, **overrides) -> ContextRequest:
    kwargs = dict(
        request_id="req-1",
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=_hex("action"),
        concrete_gap="close the remaining even case",
        output_contract="return a KERNEL_CHECKED_PROOF",
        tool_environment_hash=_hex("tool-v1"),
        acceptance_policy_hash=_hex("accept-v1"),
        reference_versions_hash=_hex("refs-v1"),
        expand_evidence_content=False,
        limits=ContextLimits.unlimited(),
    )
    kwargs.update(overrides)
    return ContextRequest(**kwargs)


def test_full_proof_prompt_blowup_avoided_without_losing_statement():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        parent = _fact("aaaaaaaaaaaaaaaa", "parent lemma statement", _PROOF)
        child = _fact(
            "bbbbbbbbbbbbbbbb",
            "child lemma statement",
            "uses parent",
            predecessors=("aaaaaaaaaaaaaaaa",),
        )
        snapshot = ProjectSnapshot(
            active_facts=(parent, child),
            revoked_fact_ids=(),
            memory_records=(),
            receipts=(),
        )
        packet = ContextCompiler().compile(
            _request(contract, obligation, required_fact_ids=("bbbbbbbbbbbbbbbb",)),
            view,
            snapshot,
            store,
        )
        assert "parent lemma statement" in packet.rendered_text
        assert "child lemma statement" in packet.rendered_text
        assert "n is an integer" in packet.rendered_text
        assert "proof_handle:" in packet.rendered_text
        assert _PROOF not in packet.rendered_text
        assert packet.fact_markdown[0] == parent.raw_markdown
        assert LOOP_INTEGRATION in ContextCompiler.loop_integration
        expanded = ContextCompiler().compile(
            _request(
                contract,
                obligation,
                required_fact_ids=("bbbbbbbbbbbbbbbb",),
                expand_proof_content=True,
            ),
            view,
            snapshot,
            store,
        )
        assert _PROOF in expanded.rendered_text


def test_automatic_blocker_recall_excludes_different_scope():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        route = Route.build(
            target_obligation_id=obligation.obligation_id,
            mechanism="direct induction",
            required_obligation_ids=(),
        )
        matching = FailureRecord.build(
            kind=FailureKind.MISSING_PREMISE,
            exact_failed_claim="induction step stalled",
            scope=contract.scope,
            confidence=ConfidenceClass.HIGH,
            context_version=1,
            provenance="direct induction worker",
            created_at="2026-01-02T00:00:00Z",
            target_obligation_id=obligation.obligation_id,
            route_id=route.route_id,
            contract_id=contract.contract_id,
        )
        other_scope = FailureRecord.build(
            kind=FailureKind.MISSING_PREMISE,
            exact_failed_claim="induction step stalled over Q",
            scope=_scope(domain="Q"),
            confidence=ConfidenceClass.HIGH,
            context_version=1,
            provenance="other",
            created_at="2026-01-03T00:00:00Z",
        )
        view = _seed_view(
            contract, obligation, evidence, route=route, failures=(matching, other_scope)
        )
        snapshot = ProjectSnapshot(
            active_facts=(), revoked_fact_ids=(), memory_records=(), receipts=()
        )
        packet = ContextCompiler().compile(
            _request(contract, obligation, route_id=route.route_id),
            view,
            snapshot,
            store,
        )
        assert matching.failure_id in packet.manifest.included_failure_ids
        assert other_scope.failure_id not in packet.manifest.included_failure_ids
        assert "induction step stalled over Q" not in packet.rendered_text


def test_evidence_revocation_and_required_caps():
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        contract = _contract()
        evidence = _evidence(contract)
        obligation = _obligation(contract)
        view = _seed_view(contract, obligation, evidence)
        snapshot = ProjectSnapshot(
            active_facts=(_fact("abcdef0123456789", "leaf", "short"),),
            revoked_fact_ids=("deaddeaddeaddead",),
            memory_records=(),
            receipts=(),
        )
        _raises(
            MissingRequiredContext,
            ContextCompiler().compile,
            _request(contract, obligation, required_fact_ids=("deaddeaddeaddead",)),
            view,
            snapshot,
            store,
        )
        stale = _evidence(contract, content="stale-ev")
        view.apply(9, _event("evidence.registered", {"evidence": stale.to_dict()}, "stale"))
        view.apply(10, _event("evidence.invalidated", {"artifact_id": stale.artifact_id}, "inv"))
        _raises(
            MissingRequiredContext,
            ContextCompiler().compile,
            _request(
                contract,
                obligation,
                required_evidence_ids=(stale.artifact_id,),
            ),
            view,
            snapshot,
            store,
        )
        _raises(
            ContextTooLarge,
            ContextCompiler().compile,
            _request(
                contract,
                obligation,
                required_fact_ids=("abcdef0123456789",),
                limits=ContextLimits.build(max_chars=8, max_bytes=8),
            ),
            view,
            snapshot,
            store,
        )


def test_recurrence_persists_across_recreated_objects_and_polling():
    contract = _contract()
    left = _obligation(contract, exact_statement="left")
    right = _obligation(contract, exact_statement="right", version=2)
    r1 = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="to-right",
        required_obligation_ids=(right.obligation_id,),
    )
    r2 = Route.build(
        target_obligation_id=right.obligation_id,
        mechanism="to-left",
        required_obligation_ids=(left.obligation_id,),
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("obligation.created", {"obligation": left.to_dict()}, "o1"))
    view.apply(3, _event("obligation.created", {"obligation": right.to_dict()}, "o2"))
    view.apply(4, _event("route.created", {"route": r1.to_dict()}, "r1"))
    view.apply(5, _event("route.created", {"route": r2.to_dict()}, "r2"))
    history = RecurrenceHistory(threshold=3)
    first_id = _hex("obs-1")
    second_id = _hex("obs-2")
    third_id = _hex("obs-3")
    observe_recurrence(
        history,
        view,
        RecurrenceObservation(observation_id=first_id, source="completed_action"),
    )
    poll = analyze_recurrence(view, history)
    assert poll.strike_count == 1
    again = history.observe(first_id, view)
    assert again.strike_count == 1
    history.observe(second_id, view)
    restored = history_from_state(history.snapshot_state(), threshold=3)
    replayed, last = replay_recurrence_history(
        [
            _event(
                EVENT_RECURRENCE_OBSERVED,
                {
                    "observation": RecurrenceObservation(
                        observation_id=first_id, source="completed_action"
                    ).to_dict()
                },
                "obs-a",
            ),
            _event("runtime.lease_health", {"lease_id": _hex("lease")}, "poll"),
            _event(
                EVENT_RECURRENCE_OBSERVED,
                {
                    "observation": RecurrenceObservation(
                        observation_id=second_id, source="completed_action"
                    ).to_dict()
                },
                "obs-b",
            ),
            _event(
                EVENT_RECURRENCE_OBSERVED,
                {
                    "observation": RecurrenceObservation(
                        observation_id=third_id, source="completed_action"
                    ).to_dict()
                },
                "obs-c",
            ),
        ],
        view,
        threshold=3,
    )
    assert restored.analyze(view).strike_count == 2
    assert last.strike_count == 3
    assert last.classification is SCCClassification.RECURRENT_NO_EXIT
    assert replayed.analyze(view).strike_count == 3
    assert history.evaluate(view, poll=True).strike_count == 2


def test_unrelated_progress_does_not_reset_a_route():
    contract = _contract()
    left = _obligation(contract, exact_statement="left")
    right = _obligation(contract, exact_statement="right", version=2)
    outsider = _obligation(contract, exact_statement="outsider", version=3)
    r1 = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="to-right",
        required_obligation_ids=(right.obligation_id,),
    )
    r2 = Route.build(
        target_obligation_id=right.obligation_id,
        mechanism="to-left",
        required_obligation_ids=(left.obligation_id,),
    )
    r3 = Route.build(
        target_obligation_id=outsider.obligation_id,
        mechanism="independent",
        required_obligation_ids=(),
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("obligation.created", {"obligation": left.to_dict()}, "o1"))
    view.apply(3, _event("obligation.created", {"obligation": right.to_dict()}, "o2"))
    view.apply(4, _event("obligation.created", {"obligation": outsider.to_dict()}, "o3"))
    view.apply(5, _event("route.created", {"route": r1.to_dict()}, "r1"))
    view.apply(6, _event("route.created", {"route": r2.to_dict()}, "r2"))
    view.apply(7, _event("route.created", {"route": r3.to_dict()}, "r3"))
    history = RecurrenceHistory(threshold=3)
    history.observe(_hex("a1"), view, affected_route_id=r1.route_id)
    history.observe(_hex("a2"), view, affected_route_id=r1.route_id)
    before = history.analyze(view).strike_count
    assert before == 2
    fresh = _evidence(contract, exact_claim=outsider.exact_statement, content="other-pass")
    view.apply(20, _event("evidence.registered", {"evidence": fresh.to_dict()}, "ev-out"))
    view.apply(
        21,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": outsider.obligation_id,
                "status": "IN_PROGRESS",
                "evidence_refs": [fresh.artifact_id],
            },
            "attach-out",
        ),
    )
    after = history.observe(
        _hex("a-out"),
        view,
        affected_route_id=r3.route_id,
        progress_kind=ProgressKind.REPRESENTATION_GAIN,
    )
    assert after.strike_count == before
    assert after.classification is not SCCClassification.CHANGED_FRONTIER


def test_legal_induction_exit_and_acyclic_blocked_mechanism_audit():
    contract = _contract()
    parent = _obligation(contract, exact_statement="P(n)")
    child = _obligation(contract, exact_statement="P(n-1)", version=2)
    claim = "parent descends to child"
    evidence = _evidence(contract, exact_claim=claim, content="descent")
    down = Relation.build(
        relation_type=RelationType.DESCENDS_BY,
        source=parent.obligation_id,
        target=child.obligation_id,
        exact_claim=claim,
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
        well_founded_measure="n -> n-1",
        side_conditions=("n is a positive integer",),
    )
    back = Route.build(
        target_obligation_id=child.obligation_id,
        mechanism="back",
        required_obligation_ids=(parent.obligation_id,),
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e"))
    view.apply(3, _event("obligation.created", {"obligation": parent.to_dict()}, "o1"))
    view.apply(4, _event("obligation.created", {"obligation": child.to_dict()}, "o2"))
    view.apply(5, _event("relation.created", {"relation": down.to_dict()}, "rel"))
    view.apply(6, _event("route.created", {"route": back.to_dict()}, "r"))
    legal = RecurrenceHistory().analyze(view)
    assert legal.classification is SCCClassification.LEGAL_DESCENT
    assert "LEGAL_DESCENT" in legal.exemptions
    assert legal.audit_required is False

    blocked_target = _obligation(contract, exact_statement="blocked target", version=4)
    blocked_route = Route.build(
        target_obligation_id=blocked_target.obligation_id,
        mechanism="unchanged-search",
        required_obligation_ids=(),
    )
    blocked_view = ResearchView()
    blocked_view.apply(
        1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c")
    )
    blocked_view.apply(
        2, _event("obligation.created", {"obligation": blocked_target.to_dict()}, "o")
    )
    blocked_view.apply(
        3, _event("route.created", {"route": blocked_route.to_dict()}, "r")
    )
    blocked_view.apply(
        4,
        _event(
            "route.status_changed",
            {"route_id": blocked_route.route_id, "status": "BLOCKED"},
            "block",
        ),
    )
    history = RecurrenceHistory(threshold=3)
    history.observe(_hex("b1"), blocked_view, affected_route_id=blocked_route.route_id)
    history.observe(_hex("b2"), blocked_view, affected_route_id=blocked_route.route_id)
    third = history.observe(
        _hex("b3"), blocked_view, affected_route_id=blocked_route.route_id
    )
    assert third.blocked_mechanism is True
    assert third.audit_required is True
    assert third.recommendation is RecurrenceRecommendation.BOUNDED_AUDIT
    assert third.park_is_falsity is False


def test_persistent_action_reuse_and_transport_retry():
    spec = ActionSpec.build(
        action_type="prove",
        mechanism="direct",
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        context_hash=_hex("ctx"),
        dependency_hash=_hex("dep"),
        definitions_hash=_hex("defs"),
        tool_environment_hash=_hex("tool"),
        parameters_json=canonical_json({"k": 1}),
    )
    attempt_ok = _hex("attempt-ok")
    attempt_fail = _hex("attempt-fail")
    events = [
        _event(
            "runtime.lease_issued",
            {
                "lease": {
                    "action_id": spec.action_id,
                    "attempt_id": attempt_ok,
                    "infrastructure_status": InfrastructureStatus.PENDING.value,
                    "artifact_refs": [],
                }
            },
            "issued-ok",
        ),
        _event(
            "runtime.lease_completed",
            {
                "lease": {
                    "action_id": spec.action_id,
                    "attempt_id": attempt_ok,
                    "infrastructure_status": InfrastructureStatus.SUCCEEDED.value,
                    "artifact_refs": [_hex("art")],
                }
            },
            "done-ok",
        ),
    ]
    index = build_action_attempt_index(events=events)
    assert index.decide(spec) is DedupeDecision.RECOVER_OR_REUSE
    fail_spec = ActionSpec.build(
        action_type="prove",
        mechanism="direct",
        model_id="grok-4.6",
        effort="high",
        task_mode="retry",
        context_hash=_hex("ctx-2"),
        dependency_hash=_hex("dep"),
        definitions_hash=_hex("defs"),
        tool_environment_hash=_hex("tool"),
        parameters_json=canonical_json({"k": 1}),
    )
    fail_index = ActionAttemptIndex()
    ingest_runtime_lease(
        fail_index,
        {
            "action_id": fail_spec.action_id,
            "attempt_id": attempt_fail,
            "infrastructure_status": InfrastructureStatus.FAILED.value,
            "artifact_refs": [],
        },
    )
    assert fail_index.decide(fail_spec) is DedupeDecision.RETRY_TRANSPORT
    resample = ActionSpec.build(
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
    assert resample.action_id == spec.action_id
    assert index.decide(resample) is DedupeDecision.ALLOW_NEW_SAMPLE

    spec2, request, seed = build_action_context_pair(
        contract_id=_hex("contract"),
        obligation_id=_hex("obligation"),
        concrete_gap="gap",
        output_contract="proof",
        tool_environment_hash=_hex("tool"),
        acceptance_policy_hash=_hex("accept"),
        reference_versions_hash=_hex("refs"),
        limits=ContextLimits.unlimited(),
        action_type="prove",
        mechanism="direct",
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"k": 1}),
        dependency_hash=_hex("dep"),
        definitions_hash=_hex("defs"),
    )
    candidate = CandidateAction(
        candidate_id="reuse",
        action_spec=spec2,
        context_request=request,
        context_seed=seed,
        obligation_id=request.obligation_id,
        mechanism_family=spec2.mechanism,
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
    reuse_index = build_action_attempt_index(
        events=[
            _event(
                "runtime.lease_completed",
                {
                    "lease": {
                        "action_id": spec2.action_id,
                        "attempt_id": _hex("hist"),
                        "infrastructure_status": InfrastructureStatus.SUCCEEDED.value,
                        "artifact_refs": [_hex("blob")],
                    }
                },
                "hist",
            )
        ]
    )
    policy = BudgetPolicy(
        **{name: DimensionLimit.unlimited() for name in (
            "cost_microusd",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "wall_ms",
            "tool_cpu_ms",
            "tool_gpu_ms",
        )},
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )
    plan = TransparentScheduler().select(
        [candidate],
        policy=policy,
        snapshot=ResourceSnapshot.empty(),
        now_utc=_TS,
        dedupe_index=reuse_index,
    )
    decision = plan.decision_for("reuse")
    assert decision.disposition is ScheduleDisposition.DEFERRED
    assert decision.reason is ScheduleReason.DEDUPE_RECOVER


def test_park_is_policy_with_reopen_conditions():
    contract = _contract()
    left = _obligation(contract, exact_statement="left")
    right = _obligation(contract, exact_statement="right", version=2)
    outside = _obligation(contract, exact_statement="outside", version=3)
    cycle_left = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="cycle-l",
        required_obligation_ids=(right.obligation_id,),
        alternative_group="or-1",
    )
    cycle_right = Route.build(
        target_obligation_id=right.obligation_id,
        mechanism="cycle-r",
        required_obligation_ids=(left.obligation_id,),
    )
    exit_route = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="exit",
        required_obligation_ids=(outside.obligation_id,),
        alternative_group="or-1",
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("obligation.created", {"obligation": left.to_dict()}, "o1"))
    view.apply(3, _event("obligation.created", {"obligation": right.to_dict()}, "o2"))
    view.apply(4, _event("obligation.created", {"obligation": outside.to_dict()}, "o3"))
    view.apply(5, _event("route.created", {"route": cycle_left.to_dict()}, "r1"))
    view.apply(6, _event("route.created", {"route": cycle_right.to_dict()}, "r2"))
    view.apply(7, _event("route.created", {"route": exit_route.to_dict()}, "r3"))
    history = RecurrenceHistory(threshold=3)
    history.evaluate(
        view,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=exit_route.route_id,
    )
    history.evaluate(
        view,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=exit_route.route_id,
    )
    parked = history.evaluate(
        view,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=exit_route.route_id,
    )
    assert parked.recommendation is RecurrenceRecommendation.PARK_ROUTE_CANDIDATE
    assert parked.park_is_falsity is False
    assert parked.reopen_conditions
    assert view.get_route(exit_route.route_id).status is RouteStatus.PROPOSED


def test_recall_for_action_infers_linked_facts():
    contract = _contract()
    obligation = _obligation(contract)
    parent = _fact("aaaaaaaaaaaaaaaa", contract.exact_statement, "parent-proof")
    child = _fact(
        "bbbbbbbbbbbbbbbb",
        "child uses parent",
        "child-proof",
        predecessors=("aaaaaaaaaaaaaaaa",),
    )
    evidence = _evidence(
        contract,
        content="ev",
        exact_claim=contract.exact_statement,
        input_hashes=(parent.content_sha256,),
    )
    view = _seed_view(contract, obligation, evidence)
    snapshot = ProjectSnapshot(
        active_facts=(parent, child),
        revoked_fact_ids=(),
        memory_records=(),
        receipts=(),
    )
    recalled = recall_for_action(
        view=view,
        snapshot=snapshot,
        obligation=obligation,
        explicit_evidence_ids=(evidence.artifact_id,),
    )
    assert parent.fact_id in recalled.required_fact_ids
    assert recalled.interfaces[0].statement == contract.exact_statement
    assert recalled.interfaces[0].full_proof_available is True


def main() -> None:
    tests = [
        test_full_proof_prompt_blowup_avoided_without_losing_statement,
        test_automatic_blocker_recall_excludes_different_scope,
        test_evidence_revocation_and_required_caps,
        test_recurrence_persists_across_recreated_objects_and_polling,
        test_unrelated_progress_does_not_reset_a_route,
        test_legal_induction_exit_and_acyclic_blocked_mechanism_audit,
        test_persistent_action_reuse_and_transport_retry,
        test_park_is_policy_with_reopen_conditions,
        test_recall_for_action_infers_linked_facts,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL V0.2 RECALL/RECURRENCE TESTS PASSED")


if __name__ == "__main__":
    main()
