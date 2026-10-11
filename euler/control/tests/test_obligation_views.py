"""Offline tests for obligation/route/relation types and ResearchView replay.

Covers AND readiness, OR alternative survival, local blocked routes, stale
propagation, replay, reopen, unknown events, and no auto-discharge.
No Fact Graph / gateway / verify / Global Memory imports.

Runs standalone (``python -m euler.control.tests.test_obligation_views``)
and under pytest.
"""

from __future__ import annotations

import ast
from pathlib import Path

from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
    ValidityStatus,
)
from euler.control.failures import (
    ConfidenceClass,
    FailureKind,
    FailureRecord,
)
from euler.control.ids import sha256_hex
from euler.control.obligations import (
    Obligation,
    ObligationGraphError,
    ObligationStatus,
    Relation,
    RelationType,
    Route,
    RouteStatus,
    transition_obligation,
    transition_route,
)
from euler.control.schema import EventEnvelope
from euler.control.views import (
    InvalidResearchTransition,
    ResearchView,
    RouteReadiness,
    UnknownResearchEvent,
)

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(
        f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}"
    )


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope(*, domain: str = "Z", variable: str = "n") -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(
                variable=variable,
                kind=QuantifierKind.FORALL,
                domain=domain,
            ),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
    )


def _contract(*, problem_id: str = "goldbach", version: int = 1) -> ProblemContract:
    return ProblemContract.build(
        problem_id=problem_id,
        version=version,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _evidence(
    contract: ProblemContract,
    *,
    claim: str = "lemma holds",
    kind: EvidenceKind = EvidenceKind.LLM_CHECKED_PROOF,
    content: str = "blob-a",
    scope: MathematicalScope | None = None,
    result: CheckerResult | None = CheckerResult.PASS,
) -> EvidenceArtifact:
    checked = kind is not EvidenceKind.HEURISTIC_OBSERVATION
    return EvidenceArtifact.build(
        content_hash=_hex("blob", content),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=kind,
        exact_claim=claim,
        scope=contract.scope if scope is None else scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker" if checked else None,
        checker_version="1" if checked else None,
        checker_result=result if checked else None,
    )


def _obligation(
    contract: ProblemContract,
    *,
    statement: str,
    desired: str = "proof",
    version: int = 1,
    status: ObligationStatus = ObligationStatus.OPEN,
    evidence_refs: tuple[str, ...] = (),
    scope: MathematicalScope | None = None,
) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=statement,
        scope=contract.scope if scope is None else scope,
        desired_output=desired,
        created_by="researcher",
        version=version,
        status=status,
        evidence_refs=evidence_refs,
    )


def _route(
    target: Obligation,
    required: tuple[Obligation, ...],
    *,
    mechanism: str,
    group: str | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> Route:
    return Route.build(
        target_obligation_id=target.obligation_id,
        mechanism=mechanism,
        required_obligation_ids=tuple(item.obligation_id for item in required),
        alternative_group=group,
        evidence_refs=evidence_refs,
    )


def _event(event_type: str, payload: dict, key: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="tester",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _seed_contract(view: ResearchView, contract: ProblemContract, seq: int) -> int:
    view.apply(seq, _event("problem.contract.created", {"contract": contract.to_dict()}, f"c-{seq}"))
    return seq + 1


def _seed_evidence(view: ResearchView, evidence: EvidenceArtifact, seq: int) -> int:
    view.apply(seq, _event("evidence.registered", {"evidence": evidence.to_dict()}, f"e-{seq}"))
    return seq + 1


def _seed_obligation(view: ResearchView, obligation: Obligation, seq: int) -> int:
    view.apply(
        seq,
        _event("obligation.created", {"obligation": obligation.to_dict()}, f"o-{seq}"),
    )
    return seq + 1


def _seed_route(view: ResearchView, route: Route, seq: int) -> int:
    view.apply(seq, _event("route.created", {"route": route.to_dict()}, f"r-{seq}"))
    return seq + 1


def _seed_relation(view: ResearchView, relation: Relation, seq: int) -> int:
    view.apply(
        seq, _event("relation.created", {"relation": relation.to_dict()}, f"rel-{seq}")
    )
    return seq + 1


def _discharge(view: ResearchView, obligation: Obligation, evidence: EvidenceArtifact, seq: int) -> int:
    view.apply(
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": obligation.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [evidence.artifact_id],
            },
            f"d-{seq}",
        ),
    )
    return seq + 1


# --------------------------------------------------------------------------- #
# identity and pure transitions                                               #
# --------------------------------------------------------------------------- #


def test_status_does_not_change_obligation_identity():
    contract = _contract()
    open_ob = _obligation(contract, statement="lemma A")
    discharged = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="lemma A",
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
        status=ObligationStatus.DISCHARGED,
        evidence_refs=(_hex("ev"),),
    )
    assert open_ob.obligation_id == discharged.obligation_id
    assert open_ob.status is ObligationStatus.OPEN
    moved = transition_obligation(
        open_ob,
        ObligationStatus.DISCHARGED,
        evidence_refs=(_hex("ev"),),
    )
    assert moved.obligation_id == open_ob.obligation_id
    assert moved.status is ObligationStatus.DISCHARGED
    _raises(
        ObligationGraphError,
        transition_obligation,
        moved,
        ObligationStatus.REFUTED,
        evidence_refs=(_hex("ev"),),
    )
    _raises(
        ObligationGraphError,
        transition_obligation,
        moved,
        ObligationStatus.OPEN,
    )
    _raises(
        ObligationGraphError,
        transition_obligation,
        moved,
        ObligationStatus.OPEN,
        reason="new checked lemma",
    )
    reopened = transition_obligation(
        moved,
        ObligationStatus.OPEN,
        reason="new checked lemma",
        new_premises="stronger lemma",
    )
    assert reopened.status is ObligationStatus.OPEN
    assert reopened.evidence_refs == ()
    assert reopened.validity is ValidityStatus.VALID
    _raises(
        ObligationGraphError,
        transition_obligation,
        open_ob,
        ObligationStatus.DISCHARGED,
    )


def test_relation_evidence_and_well_founded_constraints():
    contract = _contract()
    left = _obligation(contract, statement="left")
    right = _obligation(contract, statement="right")
    _raises(
        ObligationGraphError,
        Relation.build,
        relation_type=RelationType.REDUCES_TO,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left reduces to right",
        scope=contract.scope,
    )
    _raises(
        ObligationGraphError,
        Relation.build,
        relation_type=RelationType.EQUIVALENT_UNDER,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left equivalent to right",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
    )
    _raises(
        ObligationGraphError,
        Relation.build,
        relation_type=RelationType.DESCENDS_BY,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left descends to right",
        scope=contract.scope,
    )
    suspected = Relation.build(
        relation_type=RelationType.SUSPECTED_SAME,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left looks like right",
        scope=contract.scope,
        assumptions=("same shape",),
    )
    assert suspected.relation_id == suspected.relation_id
    assert left.obligation_id != right.obligation_id
    _raises(
        ObligationGraphError,
        Relation.build,
        relation_type=RelationType.DESCENDS_BY,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left descends to right",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(_hex("ev"),),
        well_founded_measure="degree of polynomial",
        side_conditions=("degree strictly decreases",),
    )
    descends = Relation.build(
        relation_type=RelationType.DESCENDS_BY,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left descends to right",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(_hex("ev"),),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
        well_founded_measure="degree of polynomial",
        side_conditions=("degree strictly decreases",),
    )
    assert descends.well_founded_measure is not None


def test_illegal_route_terminal_transition():
    contract = _contract()
    target = _obligation(contract, statement="target")
    lemma = _obligation(contract, statement="lemma")
    route = _route(target, (lemma,), mechanism="direct")
    _raises(ObligationGraphError, transition_route, route, RouteStatus.COMPLETED)
    completed = transition_route(
        route, RouteStatus.COMPLETED, evidence_refs=(_hex("route-ev"),)
    )
    _raises(ObligationGraphError, transition_route, completed, RouteStatus.ACTIVE)
    reopened = transition_route(
        completed, RouteStatus.PROPOSED, reason="new tool representation"
    )
    assert reopened.status is RouteStatus.PROPOSED
    assert reopened.route_id == route.route_id


# --------------------------------------------------------------------------- #
# ResearchView                                                                #
# --------------------------------------------------------------------------- #


def test_and_readiness_does_not_auto_discharge_target():
    contract = _contract()
    ev_a = _evidence(contract, claim="lemma A", content="a")
    ev_b = _evidence(contract, claim="lemma B", content="b")
    target = _obligation(contract, statement="main theorem")
    lemma_a = _obligation(contract, statement="lemma A")
    lemma_b = _obligation(contract, statement="lemma B")
    route = _route(target, (lemma_a, lemma_b), mechanism="and-split")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, ev_a, seq)
    seq = _seed_evidence(view, ev_b, seq)
    seq = _seed_obligation(view, target, seq)
    seq = _seed_obligation(view, lemma_a, seq)
    seq = _seed_obligation(view, lemma_b, seq)
    seq = _seed_route(view, route, seq)
    assert view.route_readiness(route.route_id) is RouteReadiness.WAITING
    seq = _discharge(view, lemma_a, ev_a, seq)
    assert view.route_readiness(route.route_id) is RouteReadiness.WAITING
    seq = _discharge(view, lemma_b, ev_b, seq)
    assert view.route_readiness(route.route_id) is RouteReadiness.READY_FOR_REVIEW
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN
    assert target.obligation_id in view.frontier()
    assert view.get_obligation(lemma_a.obligation_id).status is ObligationStatus.DISCHARGED


def test_or_alternatives_survive_local_blocked_route():
    contract = _contract()
    evidence = _evidence(contract, claim="exit lemma", content="exit")
    target = _obligation(contract, statement="main")
    blocked_req = _obligation(contract, statement="cyclic lemma")
    exit_req = _obligation(contract, statement="exit lemma")
    blocked_route = _route(
        target, (blocked_req,), mechanism="cycle-like", group="or-1"
    )
    exit_route = _route(target, (exit_req,), mechanism="direct-exit", group="or-1")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, evidence, seq)
    seq = _seed_obligation(view, target, seq)
    seq = _seed_obligation(view, blocked_req, seq)
    seq = _seed_obligation(view, exit_req, seq)
    seq = _seed_route(view, blocked_route, seq)
    seq = _seed_route(view, exit_route, seq)
    view.apply(
        seq,
        _event(
            "route.status_changed",
            {"route_id": blocked_route.route_id, "status": "BLOCKED"},
            "block",
        ),
    )
    seq += 1
    seq = _discharge(view, exit_req, evidence, seq)
    alts = view.alternatives(target.obligation_id)
    assert {item.route_id for item in alts} == {
        blocked_route.route_id,
        exit_route.route_id,
    }
    assert view.route_readiness(blocked_route.route_id) is RouteReadiness.BLOCKED
    assert view.route_readiness(exit_route.route_id) is RouteReadiness.READY_FOR_REVIEW
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN
    assert view.get_obligation(target.obligation_id).status is not ObligationStatus.PARKED


def test_invalidation_marks_dependents_stale_and_keeps_history():
    contract = _contract()
    evidence = _evidence(contract, claim="source", content="reduce")
    source = _obligation(
        contract, statement="source", evidence_refs=(evidence.artifact_id,)
    )
    target = _obligation(contract, statement="target")
    route = _route(
        target, (source,), mechanism="reduce", evidence_refs=(evidence.artifact_id,)
    )
    relation = Relation.build(
        relation_type=RelationType.REDUCES_TO,
        source=source.obligation_id,
        target=target.obligation_id,
        exact_claim="source",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, evidence, seq)
    seq = _seed_obligation(view, source, seq)
    seq = _seed_obligation(view, target, seq)
    seq = _seed_route(view, route, seq)
    seq = _seed_relation(view, relation, seq)
    assert view.get_relation(relation.relation_id).validity is ValidityStatus.VALID
    view.apply(
        seq,
        _event(
            "evidence.invalidated",
            {"artifact_id": evidence.artifact_id},
            "inv",
        ),
    )
    assert view.get_evidence(evidence.artifact_id) is not None
    assert view.get_evidence(evidence.artifact_id).validity is ValidityStatus.STALE
    assert view.get_route(route.route_id).validity is ValidityStatus.STALE
    assert view.get_relation(relation.relation_id).validity is ValidityStatus.STALE
    stale = view.stale_dependencies()
    assert source.obligation_id in stale.obligation_ids
    assert route.route_id in stale.route_ids
    assert relation.relation_id in stale.relation_ids
    assert view.get_obligation(source.obligation_id).validity is ValidityStatus.STALE
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN
    assert view.route_readiness(route.route_id) is RouteReadiness.STALE
    assert view.get_relation(relation.relation_id).relation_type is RelationType.REDUCES_TO


def test_replay_is_deterministic_and_reopen_requires_reason():
    contract = _contract()
    evidence = _evidence(contract, claim="park then reopen")
    target = _obligation(contract, statement="main")
    lemma = _obligation(contract, statement="lemma")
    route = _route(target, (lemma,), mechanism="direct")
    events = [
        (1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c")),
        (2, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e")),
        (3, _event("obligation.created", {"obligation": target.to_dict()}, "t")),
        (4, _event("obligation.created", {"obligation": lemma.to_dict()}, "l")),
        (5, _event("route.created", {"route": route.to_dict()}, "r")),
        (
            6,
            _event(
                "obligation.status_changed",
                {"obligation_id": lemma.obligation_id, "status": "PARKED"},
                "park",
            ),
        ),
    ]
    first = ResearchView()
    first.replay(events)
    second = ResearchView()
    second.replay(list(reversed(events)))
    assert first.frontier() == second.frontier()
    assert first.get_obligation(lemma.obligation_id).status is ObligationStatus.PARKED
    _raises(
        InvalidResearchTransition,
        first.apply,
        7,
        _event(
            "obligation.reopened",
            {"obligation_id": lemma.obligation_id, "reason": ""},
            "bad-reopen",
        ),
    )
    first.apply(
        7,
        _event(
            "obligation.reopened",
            {
                "obligation_id": lemma.obligation_id,
                "reason": "new premise: stronger lemma",
                "new_premises": "stronger lemma",
            },
            "reopen",
        ),
    )
    assert first.get_obligation(lemma.obligation_id).status is ObligationStatus.OPEN
    first.apply(7, _event(
            "obligation.reopened",
            {
                "obligation_id": lemma.obligation_id,
                "reason": "new premise: stronger lemma",
                "new_premises": "stronger lemma",
            },
            "reopen",
        ))
    other = _event(
        "obligation.reopened",
        {
            "obligation_id": lemma.obligation_id,
            "reason": "different reason",
            "new_premises": "other premise",
        },
        "reopen-other",
    )
    _raises(InvalidResearchTransition, first.apply, 7, other)


def test_unknown_events_and_missing_refs_fail_closed():
    view = ResearchView()
    _raises(
        UnknownResearchEvent,
        view.apply,
        1,
        _event("budget.reserved", {"reservation_id": "x"}, "budget"),
    )
    _raises(
        UnknownResearchEvent,
        view.apply,
        1,
        _event("obligation.exploded", {"obligation_id": "x"}, "boom"),
    )
    contract = _contract()
    orphan = _obligation(contract, statement="orphan")
    _raises(
        InvalidResearchTransition,
        view.apply,
        1,
        _event("obligation.created", {"obligation": orphan.to_dict()}, "orphan"),
    )
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    _raises(
        InvalidResearchTransition,
        view.apply,
        2,
        _event(
            "obligation.status_changed",
            {"obligation_id": orphan.obligation_id, "status": "OPEN"},
            "missing-ob",
        ),
    )


def test_failure_record_does_not_refute_or_discharge():
    contract = _contract()
    target = _obligation(contract, statement="main")
    failure = FailureRecord.build(
        kind=FailureKind.INVALID_PROOF,
        exact_failed_claim="attempted proof of main",
        scope=contract.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="attempt-1",
        created_at=_TS,
        target_obligation_id=target.obligation_id,
        contract_id=contract.contract_id,
        rejected_step="gap in lemma 2",
        reopen_conditions=("repair the gap",),
    )
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_obligation(view, target, seq)
    view.apply(seq, _event("failure.recorded", {"failure": failure.to_dict()}, "fail"))
    assert view.get_failure(failure.failure_id) is not None
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN


def test_heuristic_and_checker_fail_cannot_discharge():
    contract = _contract()
    lemma = _obligation(contract, statement="lemma A")
    heuristic = _evidence(
        contract,
        claim="lemma A",
        kind=EvidenceKind.HEURISTIC_OBSERVATION,
        content="heur",
        result=None,
    )
    failed = _evidence(
        contract,
        claim="lemma A",
        content="fail",
        result=CheckerResult.FAIL,
    )
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, heuristic, seq)
    seq = _seed_evidence(view, failed, seq)
    seq = _seed_obligation(view, lemma, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": lemma.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [heuristic.artifact_id],
            },
            "heur-d",
        ),
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": lemma.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [failed.artifact_id],
            },
            "fail-d",
        ),
    )
    assert view.get_obligation(lemma.obligation_id).status is ObligationStatus.OPEN


def test_unrelated_same_scope_counterexample_cannot_refute_target():
    contract = _contract()
    target = _obligation(contract, statement="main theorem")
    unrelated = _evidence(
        contract,
        claim="a different lemma over the same scope",
        kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        content="cex-unrelated",
        result=CheckerResult.PASS,
    )
    matching = _evidence(
        contract,
        claim="main theorem",
        kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        content="cex-target",
        result=CheckerResult.PASS,
    )
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, unrelated, seq)
    seq = _seed_evidence(view, matching, seq)
    seq = _seed_obligation(view, target, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": target.obligation_id,
                "status": "REFUTED",
                "evidence_refs": [unrelated.artifact_id],
            },
            "unrelated-cex",
        ),
    )
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN
    view.apply(
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": target.obligation_id,
                "status": "REFUTED",
                "evidence_refs": [matching.artifact_id],
            },
            "matching-cex",
        ),
    )
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.REFUTED


def test_arbitrary_evidence_cannot_refute():
    contract = _contract()
    target = _obligation(contract, statement="main theorem")
    proof = _evidence(contract, claim="main theorem", content="proof")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, proof, seq)
    seq = _seed_obligation(view, target, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": target.obligation_id,
                "status": "REFUTED",
                "evidence_refs": [proof.artifact_id],
            },
            "bad-refute",
        ),
    )
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN


def test_mislabeled_relation_and_failure_are_rejected():
    contract = _contract()
    left = _obligation(contract, statement="left")
    right = _obligation(contract, statement="right")
    heuristic = _evidence(
        contract,
        claim="left",
        kind=EvidenceKind.HEURISTIC_OBSERVATION,
        content="rel-heur",
        result=None,
    )
    proof = _evidence(contract, claim="left", content="rel-proof")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, heuristic, seq)
    seq = _seed_evidence(view, proof, seq)
    seq = _seed_obligation(view, left, seq)
    seq = _seed_obligation(view, right, seq)
    bad_relation = Relation.build(
        relation_type=RelationType.REDUCES_TO,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(heuristic.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("relation.created", {"relation": bad_relation.to_dict()}, "bad-rel"),
    )
    labeled = FailureRecord.build(
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        exact_failed_claim=right.exact_statement,
        scope=contract.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="mislabeled",
        created_at=_TS,
        target_obligation_id=right.obligation_id,
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        witness_refs=(proof.artifact_id,),
        exclusion_scope=contract.scope,
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("failure.recorded", {"failure": labeled.to_dict()}, "bad-fail"),
    )


def test_invalidated_obligation_blocks_route_readiness():
    contract = _contract()
    evidence = _evidence(contract, claim="lemma A", content="block-stale")
    target = _obligation(contract, statement="main")
    lemma = _obligation(contract, statement="lemma A")
    route = _route(target, (lemma,), mechanism="direct")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, evidence, seq)
    seq = _seed_obligation(view, target, seq)
    seq = _seed_obligation(view, lemma, seq)
    seq = _seed_route(view, route, seq)
    seq = _discharge(view, lemma, evidence, seq)
    assert view.route_readiness(route.route_id) is RouteReadiness.READY_FOR_REVIEW
    view.apply(
        seq,
        _event("evidence.invalidated", {"artifact_id": evidence.artifact_id}, "stale-ob"),
    )
    assert view.get_obligation(lemma.obligation_id).status is ObligationStatus.DISCHARGED
    assert view.get_obligation(lemma.obligation_id).validity is ValidityStatus.STALE
    assert view.route_readiness(route.route_id) is RouteReadiness.STALE
    assert lemma.obligation_id in view.stale_dependencies().obligation_ids
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN


def test_payload_extras_and_missing_keys_are_rejected():
    contract = _contract()
    view = ResearchView()
    _raises(
        InvalidResearchTransition,
        view.apply,
        1,
        _event(
            "problem.contract.created",
            {"contract": contract.to_dict(), "extra": True},
            "extra-c",
        ),
    )
    view.apply(
        1,
        _event("problem.contract.created", {"contract": contract.to_dict()}, "c"),
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        2,
        _event("evidence.invalidated", {}, "missing-id"),
    )
    lemma = _obligation(contract, statement="lemma A")
    seq = _seed_obligation(view, lemma, 2)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": lemma.obligation_id,
                "status": "PARKED",
                "unexpected": 1,
            },
            "extra-status",
        ),
    )


def test_relation_exact_claim_mismatch_is_rejected():
    contract = _contract()
    evidence = _evidence(contract, claim="source reduces to target", content="rel-claim")
    source = _obligation(contract, statement="source")
    target = _obligation(contract, statement="target")
    relation = Relation.build(
        relation_type=RelationType.REDUCES_TO,
        source=source.obligation_id,
        target=target.obligation_id,
        exact_claim="a different reduction claim",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, evidence, seq)
    seq = _seed_obligation(view, source, seq)
    seq = _seed_obligation(view, target, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("relation.created", {"relation": relation.to_dict()}, "claim-mismatch"),
    )


def test_content_hash_is_not_an_alias_and_shared_bytes_are_distinct():
    contract = _contract()
    first = _evidence(contract, claim="lemma A", content="shared-bytes")
    second = EvidenceArtifact.build(
        content_hash=first.content_hash,
        producer="worker-2",
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim="lemma B",
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker",
        checker_version="1",
        checker_result=CheckerResult.PASS,
    )
    assert first.content_hash == second.content_hash
    assert first.artifact_id != second.artifact_id
    lemma = _obligation(contract, statement="lemma A")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, first, seq)
    seq = _seed_evidence(view, second, seq)
    seq = _seed_obligation(view, lemma, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": lemma.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [first.content_hash],
            },
            "hash-alias",
        ),
    )
    seq = _discharge(view, lemma, first, seq)
    assert view.get_obligation(lemma.obligation_id).status is ObligationStatus.DISCHARGED
    stale = EvidenceArtifact.build(
        content_hash=_hex("blob", "stale-init"),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim="stale at register",
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker",
        checker_version="1",
        checker_result=CheckerResult.PASS,
        validity=ValidityStatus.STALE,
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("evidence.registered", {"evidence": stale.to_dict()}, "stale-reg"),
    )


def test_stale_reopen_clears_old_evidence_refs():
    contract = _contract()
    old = _evidence(contract, claim="lemma A", content="old-ev")
    fresh = _evidence(contract, claim="lemma A", content="new-ev")
    lemma = _obligation(contract, statement="lemma A")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, old, seq)
    seq = _seed_evidence(view, fresh, seq)
    seq = _seed_obligation(view, lemma, seq)
    seq = _discharge(view, lemma, old, seq)
    view.apply(
        seq,
        _event("evidence.invalidated", {"artifact_id": old.artifact_id}, "inv-old"),
    )
    seq += 1
    assert view.get_obligation(lemma.obligation_id).validity is ValidityStatus.STALE
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "obligation.reopened",
            {
                "obligation_id": lemma.obligation_id,
                "reason": "need a new proof",
            },
            "reopen-reason-only",
        ),
    )
    view.apply(
        seq,
        _event(
            "obligation.reopened",
            {
                "obligation_id": lemma.obligation_id,
                "reason": "need a new proof",
                "evidence_refs": [fresh.artifact_id],
            },
            "reopen-new",
        ),
    )
    reopened = view.get_obligation(lemma.obligation_id)
    assert reopened.status is ObligationStatus.OPEN
    assert reopened.validity is ValidityStatus.VALID
    assert reopened.evidence_refs == (fresh.artifact_id,)
    assert old.artifact_id not in reopened.evidence_refs


def test_relation_endpoint_scope_mismatch_is_rejected():
    contract = _contract()
    other_scope = _scope(domain="Q")
    left = _obligation(contract, statement="left")
    right = _obligation(contract, statement="right", scope=other_scope)
    evidence = _evidence(contract, claim="left reduces to right", content="scope-mis")
    relation = Relation.build(
        relation_type=RelationType.REDUCES_TO,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left reduces to right",
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_evidence(view, evidence, seq)
    seq = _seed_obligation(view, left, seq)
    seq = _seed_obligation(view, right, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("relation.created", {"relation": relation.to_dict()}, "scope-mis"),
    )


def test_route_completed_rejects_cross_contract_evidence():
    contract = _contract()
    other = _contract(problem_id="other-problem")
    target = _obligation(contract, statement="main")
    lemma = _obligation(contract, statement="lemma A")
    route = _route(target, (lemma,), mechanism="direct")
    foreign = _evidence(other, claim="main", content="foreign")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_contract(view, other, seq)
    seq = _seed_evidence(view, foreign, seq)
    seq = _seed_obligation(view, target, seq)
    seq = _seed_obligation(view, lemma, seq)
    seq = _seed_route(view, route, seq)
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event(
            "route.status_changed",
            {
                "route_id": route.route_id,
                "status": "COMPLETED",
                "evidence_refs": [foreign.artifact_id],
            },
            "cross-contract",
        ),
    )
    assert view.get_route(route.route_id).status is RouteStatus.PROPOSED
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN


def test_failure_target_mismatch_is_rejected():
    contract = _contract()
    target = _obligation(contract, statement="main")
    lemma = _obligation(contract, statement="lemma A")
    route = _route(target, (lemma,), mechanism="direct")
    view = ResearchView()
    seq = 1
    seq = _seed_contract(view, contract, seq)
    seq = _seed_obligation(view, target, seq)
    seq = _seed_obligation(view, lemma, seq)
    seq = _seed_route(view, route, seq)
    other_scope = _scope(domain="Q")
    scoped = _obligation(contract, statement="over Q", scope=other_scope)
    seq = _seed_obligation(view, scoped, seq)
    mismatched = FailureRecord.build(
        kind=FailureKind.INVALID_PROOF,
        exact_failed_claim="attempted proof over Q",
        scope=contract.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="attempt",
        created_at=_TS,
        target_obligation_id=scoped.obligation_id,
        contract_id=contract.contract_id,
        rejected_step="gap",
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("failure.recorded", {"failure": mismatched.to_dict()}, "fail-scope"),
    )
    premise_failure = FailureRecord.build(
        kind=FailureKind.INVALID_PROOF,
        exact_failed_claim="attempted proof of lemma A",
        scope=lemma.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="attempt",
        created_at=_TS,
        target_obligation_id=lemma.obligation_id,
        route_id=route.route_id,
        contract_id=contract.contract_id,
        rejected_step="gap",
    )
    # A blocker on an actual AND premise is relevant to its parent route.
    view.apply(seq, _event("failure.recorded", {"failure": premise_failure.to_dict()}, "premise-failure"))
    seq += 1
    assert view.get_failure(premise_failure.failure_id) == premise_failure
    assert view.get_obligation(target.obligation_id).status is ObligationStatus.OPEN
    unrelated = _obligation(contract, statement="unrelated lemma B")
    seq = _seed_obligation(view, unrelated, seq)
    route_mismatch = FailureRecord.build(
        kind=FailureKind.INVALID_PROOF,
        exact_failed_claim="attempted unrelated lemma B",
        scope=unrelated.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="attempt",
        created_at=_TS,
        target_obligation_id=unrelated.obligation_id,
        route_id=route.route_id,
        contract_id=contract.contract_id,
        rejected_step="gap",
    )
    _raises(
        InvalidResearchTransition,
        view.apply,
        seq,
        _event("failure.recorded", {"failure": route_mismatch.to_dict()}, "fail-route"),
    )


def test_production_modules_do_not_import_truth_stores():
    forbidden_modules = {
        "euler.core.factgraph",
        "euler.core.FactGraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("obligations.py", "views.py"):
        path = _ROOT / filename
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported: set[str] = set()
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
                    named.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    named.add(alias.name)
        assert imported.isdisjoint(forbidden_modules), filename
        assert named.isdisjoint(forbidden_names), filename
        assert "FactGraph.add" not in source
        assert "fact_submit" not in source
        assert "fact_graph/" not in source
        assert "global_memory/" not in source


def main() -> None:
    tests = [
        test_status_does_not_change_obligation_identity,
        test_relation_evidence_and_well_founded_constraints,
        test_illegal_route_terminal_transition,
        test_and_readiness_does_not_auto_discharge_target,
        test_or_alternatives_survive_local_blocked_route,
        test_invalidation_marks_dependents_stale_and_keeps_history,
        test_replay_is_deterministic_and_reopen_requires_reason,
        test_unknown_events_and_missing_refs_fail_closed,
        test_failure_record_does_not_refute_or_discharge,
        test_heuristic_and_checker_fail_cannot_discharge,
        test_unrelated_same_scope_counterexample_cannot_refute_target,
        test_arbitrary_evidence_cannot_refute,
        test_mislabeled_relation_and_failure_are_rejected,
        test_invalidated_obligation_blocks_route_readiness,
        test_payload_extras_and_missing_keys_are_rejected,
        test_relation_exact_claim_mismatch_is_rejected,
        test_content_hash_is_not_an_alias_and_shared_bytes_are_distinct,
        test_stale_reopen_clears_old_evidence_refs,
        test_relation_endpoint_scope_mismatch_is_rejected,
        test_route_completed_rejects_cross_contract_evidence,
        test_failure_target_mismatch_is_rejected,
        test_production_modules_do_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL OBLIGATION/VIEW TESTS PASSED")


if __name__ == "__main__":
    main()
