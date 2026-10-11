"""Offline tests for research-graph SCC projection and recurrence history.

Covers acyclic graphs, true no-exit cycles, verified descent, OR exits and
exhaustion, frontier reset, suspected-same exclusion, stale/noncredible exits,
threshold/audit, determinism, zero ResearchView mutation, route endpoints,
full scope mismatch, mixed descent cycles, nonviable external requirements,
invalid hashes, unrelated exhausted routes, progress-kind misuse,
speculative REQUIRES, discharged-neutral exits, internal exhaustion, and
malformed public records.

Runs standalone (``python -m euler.control.tests.test_recurrence_scc``).
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
)
from euler.control.ids import sha256_hex
from euler.control.obligations import (
    Obligation,
    Relation,
    RelationType,
    Route,
    RouteStatus,
)
from euler.control.progress import ProgressKind
from euler.control.recurrence import (
    FrontierFingerprint,
    RecurrenceAssessment,
    RecurrenceHistory,
    RecurrenceRecommendation,
    StrikeKey,
)
from euler.control.scc import (
    ProjectedEdge,
    ProjectedEdgeKind,
    SCCClassification,
    SCCReport,
    project_research_graph,
    tarjan_scc,
)
from euler.control.schema import EventEnvelope
from euler.control.views import ResearchView

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


def _is_hex64(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _scope(
    *,
    domain: str = "Z",
    assumptions: tuple[str, ...] = ("n is an integer",),
) -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=assumptions,
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain=domain),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
    )


def _contract(*, problem_id: str = "goldbach") -> ProblemContract:
    return ProblemContract.build(
        problem_id=problem_id,
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


def _evidence(
    contract: ProblemContract,
    *,
    claim: str,
    content: str,
    kind: EvidenceKind = EvidenceKind.LLM_CHECKED_PROOF,
    result: CheckerResult | None = CheckerResult.PASS,
) -> EvidenceArtifact:
    checked = kind is not EvidenceKind.HEURISTIC_OBSERVATION
    return EvidenceArtifact.build(
        content_hash=_hex("blob", content),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=kind,
        exact_claim=claim,
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker" if checked else None,
        checker_version="1" if checked else None,
        checker_result=result if checked else None,
    )


def _obligation(
    contract: ProblemContract,
    statement: str,
    *,
    scope: MathematicalScope | None = None,
) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=statement,
        scope=contract.scope if scope is None else scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )


def _route(
    target: Obligation,
    required: tuple[Obligation, ...],
    *,
    mechanism: str,
    group: str | None = None,
) -> Route:
    return Route.build(
        target_obligation_id=target.obligation_id,
        mechanism=mechanism,
        required_obligation_ids=tuple(item.obligation_id for item in required),
        alternative_group=group,
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


def _seed(view: ResearchView, event_type: str, payload: dict, seq: int) -> int:
    view.apply(seq, _event(event_type, payload, f"{event_type}-{seq}"))
    return seq + 1


def _build_view(
    contract: ProblemContract,
    *,
    evidence: tuple[EvidenceArtifact, ...] = (),
    obligations: tuple[Obligation, ...] = (),
    routes: tuple[Route, ...] = (),
    relations: tuple[Relation, ...] = (),
) -> ResearchView:
    view = ResearchView()
    seq = 1
    seq = _seed(view, "problem.contract.created", {"contract": contract.to_dict()}, seq)
    for item in evidence:
        seq = _seed(view, "evidence.registered", {"evidence": item.to_dict()}, seq)
    for item in obligations:
        seq = _seed(view, "obligation.created", {"obligation": item.to_dict()}, seq)
    for item in routes:
        seq = _seed(view, "route.created", {"route": item.to_dict()}, seq)
    for item in relations:
        seq = _seed(view, "relation.created", {"relation": item.to_dict()}, seq)
    return view


def _cycle(contract: ProblemContract) -> tuple[ResearchView, Obligation, Obligation, Route, Route]:
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    r1 = _route(left, (right,), mechanism="to-right")
    r2 = _route(right, (left,), mechanism="to-left")
    view = _build_view(contract, obligations=(left, right), routes=(r1, r2))
    return view, left, right, r1, r2


def test_acyclic_graph():
    contract = _contract()
    root = _obligation(contract, "root")
    leaf = _obligation(contract, "leaf")
    route = _route(root, (leaf,), mechanism="direct")
    view = _build_view(contract, obligations=(root, leaf), routes=(route,))
    nodes, edges = project_research_graph(view)
    assert any(
        edge.kind is ProjectedEdgeKind.ROUTE_AND
        and edge.source == root.obligation_id
        and edge.target == leaf.obligation_id
        and edge.route_id == route.route_id
        for edge in edges
    )
    components = tarjan_scc(nodes, edges)
    assert all(len(component) == 1 for component in components)
    history = RecurrenceHistory()
    before = view.obligations()
    assessment = history.evaluate(view)
    assert assessment.classification is SCCClassification.ACYCLIC
    assert assessment.recommendation is RecurrenceRecommendation.CONTINUE
    assert assessment.audit_required is False
    assert view.obligations() == before


def test_true_no_exit_cycle_reaches_audit_threshold():
    contract = _contract()
    view, left, right, _r1, _r2 = _cycle(contract)
    history = RecurrenceHistory()
    first = history.evaluate(view)
    assert first.classification is SCCClassification.SUSPECTED_RECURRENCE
    assert first.audit_required is False
    assert first.strike_count == 1
    second = history.evaluate(view)
    assert second.classification is SCCClassification.SUSPECTED_RECURRENCE
    assert second.strike_count == 2
    third = history.evaluate(view)
    assert third.classification is SCCClassification.RECURRENT_NO_EXIT
    assert third.strike_count == 3
    assert third.audit_required is True
    assert third.recommendation is RecurrenceRecommendation.BOUNDED_AUDIT
    assert {left.obligation_id, right.obligation_id} <= set(third.report.nodes)


def test_verified_legal_descent_exempts_cycle():
    contract = _contract()
    parent = _obligation(contract, "parent")
    child = _obligation(contract, "child")
    claim = "parent descends to child"
    evidence = _evidence(contract, claim=claim, content="descent")
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
    back = _route(child, (parent,), mechanism="back")
    view = _build_view(
        contract,
        evidence=(evidence,),
        obligations=(parent, child),
        routes=(back,),
        relations=(down,),
    )
    _nodes, edges = project_research_graph(view)
    descent = [edge for edge in edges if edge.kind is ProjectedEdgeKind.DESCENDS_BY]
    assert len(descent) == 1
    assert descent[0].legal_descent is True
    assert descent[0].well_founded_measure == "n -> n-1"
    assessment = RecurrenceHistory().evaluate(view)
    assert assessment.classification is SCCClassification.LEGAL_DESCENT
    assert assessment.audit_required is False
    assert "LEGAL_DESCENT" in assessment.exemptions


def test_or_exit_and_exhaustion():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    outside = _obligation(contract, "outside")
    cycle_left = _route(left, (right,), mechanism="cycle-l", group="or-1")
    cycle_right = _route(right, (left,), mechanism="cycle-r")
    exit_route = _route(left, (outside,), mechanism="exit", group="or-1")
    view = _build_view(
        contract,
        obligations=(left, right, outside),
        routes=(cycle_left, cycle_right, exit_route),
    )
    history = RecurrenceHistory()
    first = history.evaluate(view)
    assert first.classification is SCCClassification.HAS_EXTERNAL_EXIT
    assert exit_route.route_id in first.report.external_exits
    assert first.recommendation is RecurrenceRecommendation.CHANGE_ROUTE
    assert first.audit_required is False
    exhausted = history.evaluate(
        view,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=exit_route.route_id,
    )
    assert exit_route.route_id not in exhausted.report.external_exits
    assert exhausted.classification in {
        SCCClassification.SUSPECTED_RECURRENCE,
        SCCClassification.RECURRENT_NO_EXIT,
    }
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
    assert parked.classification is SCCClassification.RECURRENT_NO_EXIT
    assert exit_route.route_id in parked.exhausted_route_ids
    assert parked.recommendation is RecurrenceRecommendation.PARK_ROUTE_CANDIDATE
    assert parked.audit_required is True


def test_empty_shell_route_is_not_an_exit():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    cycle_left = _route(left, (right,), mechanism="cycle-l", group="or-1")
    cycle_right = _route(right, (left,), mechanism="cycle-r")
    shell = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="shell",
        required_obligation_ids=(),
        alternative_group="or-1",
    )
    view = _build_view(
        contract,
        obligations=(left, right),
        routes=(cycle_left, cycle_right, shell),
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert shell.route_id not in assessment.report.external_exits
    assert assessment.classification is SCCClassification.SUSPECTED_RECURRENCE


def test_frontier_reset_on_new_evidence_and_tool():
    contract = _contract()
    view, left, _right, _r1, _r2 = _cycle(contract)
    history = RecurrenceHistory()
    history.evaluate(view)
    history.evaluate(view)
    assert history.evaluate(view).strike_count == 3
    fresh = _evidence(contract, claim=left.exact_statement, content="new-pass")
    view.apply(
        20,
        _event("evidence.registered", {"evidence": fresh.to_dict()}, "new-ev"),
    )
    view.apply(
        21,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": left.obligation_id,
                "status": "IN_PROGRESS",
                "evidence_refs": [fresh.artifact_id],
            },
            "attach-ev",
        ),
    )
    reset = history.evaluate(view)
    assert reset.classification is SCCClassification.CHANGED_FRONTIER
    assert reset.strike_count == 0
    assert reset.audit_required is False

    cycle_view, _, _, _, _ = _cycle(contract)
    tools = RecurrenceHistory()
    tools.evaluate(cycle_view, tool_hash="tool-a")
    tools.evaluate(cycle_view, tool_hash="tool-a")
    gain_only = tools.evaluate(
        cycle_view,
        tool_hash="tool-b",
        progress_kind="REPRESENTATION_GAIN",
    )
    assert gain_only.classification is SCCClassification.RECURRENT_NO_EXIT
    assert gain_only.strike_count == 3
    assert gain_only.audit_required is True
    tool_cycle = RecurrenceHistory()
    tool_cycle.evaluate(cycle_view, tool_hash="tool-a")
    tool_cycle.evaluate(cycle_view, tool_hash="tool-a")
    changed = tool_cycle.evaluate(
        cycle_view,
        tool_hash="tool-b",
        executable_next_action="run lean checker",
        progress_kind="REPRESENTATION_GAIN",
    )
    assert changed.classification is SCCClassification.CHANGED_FRONTIER
    assert changed.strike_count == 0


def test_suspected_same_is_excluded_from_scc_edges():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    suspected = Relation.build(
        relation_type=RelationType.SUSPECTED_SAME,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left looks like right",
        scope=contract.scope,
        assumptions=("shape",),
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    view = _build_view(
        contract,
        obligations=(left, right),
        relations=(suspected,),
    )
    _nodes, edges = project_research_graph(view)
    assert edges == ()
    assessment = RecurrenceHistory().evaluate(view)
    assert assessment.classification is SCCClassification.ACYCLIC


def test_stale_and_parked_routes_are_not_exits():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    outside = _obligation(contract, "outside")
    cycle_left = _route(left, (right,), mechanism="cycle-l", group="or-1")
    cycle_right = _route(right, (left,), mechanism="cycle-r")
    parked = _route(left, (outside,), mechanism="parked-exit", group="or-1")
    view = _build_view(
        contract,
        obligations=(left, right, outside),
        routes=(cycle_left, cycle_right, parked),
    )
    seq = 10
    view.apply(
        seq,
        _event(
            "route.status_changed",
            {"route_id": parked.route_id, "status": "PARKED"},
            "park",
        ),
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert parked.route_id not in assessment.report.external_exits
    assert assessment.classification is SCCClassification.SUSPECTED_RECURRENCE


def test_and_edges_remain_distinct_by_route_id():
    contract = _contract()
    target = _obligation(contract, "target")
    req = _obligation(contract, "required")
    r1 = _route(target, (req,), mechanism="and-1")
    r2 = _route(target, (req,), mechanism="and-2")
    view = _build_view(contract, obligations=(target, req), routes=(r1, r2))
    _nodes, edges = project_research_graph(view)
    and_edges = [edge for edge in edges if edge.kind is ProjectedEdgeKind.ROUTE_AND]
    assert len(and_edges) == 2
    assert {edge.route_id for edge in and_edges} == {r1.route_id, r2.route_id}


def test_determinism_independent_of_insertion_order():
    node_a = _hex("a")
    node_b = _hex("b")
    node_c = _hex("c")
    nodes = (node_b, node_a, node_c)
    edges = (
        ProjectedEdge(kind=ProjectedEdgeKind.REQUIRES, source=node_c, target=node_a),
        ProjectedEdge(kind=ProjectedEdgeKind.REQUIRES, source=node_a, target=node_b),
        ProjectedEdge(kind=ProjectedEdgeKind.REQUIRES, source=node_b, target=node_a),
    )
    first = tarjan_scc(nodes, edges)
    second = tarjan_scc(tuple(reversed(nodes)), tuple(reversed(edges)))
    assert first == second
    premise = _hex("premise")
    keys = [
        StrikeKey.build(comp, edges, premise)
        for comp in first
    ]
    assert keys == [
        StrikeKey.build(comp, tuple(reversed(edges)), premise)
        for comp in second
    ]


def test_engine_does_not_mutate_view():
    contract = _contract()
    view, _left, _right, _r1, _r2 = _cycle(contract)
    snapshot = (
        view.obligations(),
        view.routes(),
        view.relations(),
        view.evidence(),
        view.frontier(),
    )
    history = RecurrenceHistory()
    history.evaluate(view)
    history.evaluate(view)
    history.evaluate(view)
    assert (
        view.obligations(),
        view.routes(),
        view.relations(),
        view.evidence(),
        view.frontier(),
    ) == snapshot


def test_route_endpoints_project_through_target_obligation():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    target_route = Route.build(
        target_obligation_id=right.obligation_id,
        mechanism="carrier-target",
        required_obligation_ids=(),
    )
    source_route = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="carrier-source",
        required_obligation_ids=(),
    )
    back = _route(right, (left,), mechanism="back")
    claim = "left depends on right"
    evidence = _evidence(contract, claim=claim, content="requires-cycle")
    via_target = Relation.build(
        relation_type=RelationType.REQUIRES,
        source=left.obligation_id,
        target=target_route.route_id,
        exact_claim=claim,
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    via_source = Relation.build(
        relation_type=RelationType.REQUIRES,
        source=source_route.route_id,
        target=right.obligation_id,
        exact_claim=claim,
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    view = _build_view(
        contract,
        evidence=(evidence,),
        obligations=(left, right),
        routes=(target_route, source_route, back),
        relations=(via_target, via_source),
    )
    _nodes, edges = project_research_graph(view)
    requires = [
        edge
        for edge in edges
        if edge.kind is ProjectedEdgeKind.REQUIRES
        and edge.source == left.obligation_id
        and edge.target == right.obligation_id
    ]
    assert {edge.relation_id for edge in requires} == {
        via_target.relation_id,
        via_source.relation_id,
    }
    components = tarjan_scc(_nodes, edges)
    assert any(
        {left.obligation_id, right.obligation_id} <= set(component)
        for component in components
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert assessment.classification is SCCClassification.SUSPECTED_RECURRENCE


def test_full_scope_mismatch_prevents_recurrent_no_exit():
    contract = _contract()
    other_scope = _scope(assumptions=("n is even",))
    assert other_scope.definitions_hash == contract.scope.definitions_hash
    assert other_scope != contract.scope
    left = _obligation(contract, "left")
    right = _obligation(contract, "right", scope=other_scope)
    r1 = _route(left, (right,), mechanism="to-right")
    r2 = _route(right, (left,), mechanism="to-left")
    view = _build_view(contract, obligations=(left, right), routes=(r1, r2))
    history = RecurrenceHistory()
    first = history.evaluate(view)
    assert first.report.scopes_compatible is False
    assert first.classification is SCCClassification.SUSPECTED_RECURRENCE
    history.evaluate(view)
    third = history.evaluate(view)
    assert third.classification is SCCClassification.SUSPECTED_RECURRENCE
    assert third.classification is not SCCClassification.RECURRENT_NO_EXIT
    assert third.strike_count == 3
    assert third.report.scopes_compatible is False

    other_contract = _contract(problem_id="other-problem")
    a = _obligation(contract, "a")
    b = _obligation(other_contract, "b")
    mixed = ResearchView()
    seq = 1
    seq = _seed(mixed, "problem.contract.created", {"contract": contract.to_dict()}, seq)
    seq = _seed(
        mixed,
        "problem.contract.created",
        {"contract": other_contract.to_dict()},
        seq,
    )
    for item in (a, b):
        seq = _seed(mixed, "obligation.created", {"obligation": item.to_dict()}, seq)
    ra = _route(a, (b,), mechanism="a-to-b")
    rb = _route(b, (a,), mechanism="b-to-a")
    for item in (ra, rb):
        seq = _seed(mixed, "route.created", {"route": item.to_dict()}, seq)
    mixed_history = RecurrenceHistory()
    mixed_history.evaluate(mixed)
    mixed_history.evaluate(mixed)
    mixed_third = mixed_history.evaluate(mixed)
    assert mixed_third.report.scopes_compatible is False
    assert mixed_third.classification is SCCClassification.SUSPECTED_RECURRENCE
    assert mixed_third.classification is not SCCClassification.RECURRENT_NO_EXIT


def test_mixed_descent_and_non_descent_cycle_is_not_exempt():
    contract = _contract()
    parent = _obligation(contract, "parent")
    child = _obligation(contract, "child")
    claim = "parent descends to child"
    evidence = _evidence(contract, claim=claim, content="descent")
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
    forward = _route(parent, (child,), mechanism="non-descent-forward")
    back = _route(child, (parent,), mechanism="non-descent-back")
    view = _build_view(
        contract,
        evidence=(evidence,),
        obligations=(parent, child),
        routes=(forward, back),
        relations=(down,),
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert assessment.report.descent_edges
    assert assessment.classification is not SCCClassification.LEGAL_DESCENT
    assert assessment.classification is SCCClassification.SUSPECTED_RECURRENCE
    assert "LEGAL_DESCENT" not in assessment.exemptions


def test_nonviable_external_requirement_is_not_an_exit():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    parked_outside = _obligation(contract, "parked-outside")
    open_outside = _obligation(contract, "open-outside")
    cycle_left = _route(left, (right,), mechanism="cycle-l", group="or-1")
    cycle_right = _route(right, (left,), mechanism="cycle-r")
    parked_exit = _route(left, (parked_outside,), mechanism="parked-req", group="or-1")
    open_exit = _route(left, (open_outside,), mechanism="open-req", group="or-1")
    view = _build_view(
        contract,
        obligations=(left, right, parked_outside, open_outside),
        routes=(cycle_left, cycle_right, parked_exit, open_exit),
    )
    view.apply(
        20,
        _event(
            "obligation.status_changed",
            {"obligation_id": parked_outside.obligation_id, "status": "PARKED"},
            "park-out",
        ),
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert parked_exit.route_id not in assessment.report.external_exits
    assert open_exit.route_id in assessment.report.external_exits
    assert assessment.classification is SCCClassification.HAS_EXTERNAL_EXIT

    view.apply(
        21,
        _event(
            "obligation.status_changed",
            {"obligation_id": open_outside.obligation_id, "status": "PARKED"},
            "park-open",
        ),
    )
    closed = RecurrenceHistory().evaluate(view)
    assert closed.report.external_exits == ()
    assert closed.classification is SCCClassification.SUSPECTED_RECURRENCE


def test_nonhex_tool_hash_is_hashed_not_accepted():
    contract = _contract()
    view, left, right, _r1, _r2 = _cycle(contract)
    nodes = (left.obligation_id, right.obligation_id)
    bogus = "z" * 64
    assert len(bogus) == 64
    assert not _is_hex64(bogus)
    hashed = FrontierFingerprint.compute(view, nodes, tool_hash=bogus)
    assert hashed.tool_hash != bogus
    assert _is_hex64(hashed.tool_hash)
    assert _is_hex64(hashed.evidence_hash)
    assert _is_hex64(hashed.premise_hash)
    valid = _hex("tool")
    kept = FrontierFingerprint.compute(view, nodes, tool_hash=valid)
    assert kept.tool_hash == valid
    upper = valid.upper()
    folded = FrontierFingerprint.compute(view, nodes, tool_hash=upper)
    assert folded.tool_hash != upper
    assert _is_hex64(folded.tool_hash)
    _raises(
        ValueError,
        FrontierFingerprint,
        evidence_hash=bogus,
        premise_hash=valid,
        tool_hash=valid,
    )
    assessment = RecurrenceHistory().evaluate(view, tool_hash=bogus)
    assert assessment.fingerprint.tool_hash != bogus
    assert _is_hex64(assessment.fingerprint.tool_hash)


def test_unrelated_exhausted_route_does_not_park_or_exhaust():
    contract = _contract()
    view, left, right, r1, r2 = _cycle(contract)
    other = _obligation(contract, "other")
    leaf = _obligation(contract, "leaf")
    unrelated = _route(other, (leaf,), mechanism="unrelated")
    view.apply(10, _event("obligation.created", {"obligation": other.to_dict()}, "other"))
    view.apply(11, _event("obligation.created", {"obligation": leaf.to_dict()}, "leaf"))
    view.apply(12, _event("route.created", {"route": unrelated.to_dict()}, "unrelated"))
    history = RecurrenceHistory()
    kwargs = {
        "progress_kind": "NO_VERIFIABLE_PROGRESS",
        "no_verifiable_progress_route_id": unrelated.route_id,
    }
    first = history.evaluate(view, **kwargs)
    assert unrelated.route_id not in first.exhausted_route_ids
    assert first.classification is SCCClassification.SUSPECTED_RECURRENCE
    history.evaluate(view, **kwargs)
    third = history.evaluate(view, **kwargs)
    assert third.classification is SCCClassification.RECURRENT_NO_EXIT
    assert unrelated.route_id not in third.exhausted_route_ids
    assert third.recommendation is RecurrenceRecommendation.BOUNDED_AUDIT
    targeting = history.evaluate(
        view,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=r1.route_id,
    )
    assert r1.route_id not in targeting.exhausted_route_ids
    assert targeting.recommendation is RecurrenceRecommendation.BOUNDED_AUDIT


def test_progress_kind_misuse_with_exhausted_route_fails_closed():
    contract = _contract()
    view, _left, _right, r1, _r2 = _cycle(contract)
    history = RecurrenceHistory()
    _raises(
        ValueError,
        history.evaluate,
        view,
        progress_kind="REPRESENTATION_GAIN",
        no_verifiable_progress_route_id=r1.route_id,
    )
    _raises(
        ValueError,
        history.evaluate,
        view,
        progress_kind="OBLIGATION_DISCHARGED",
        no_verifiable_progress_route_id=r1.route_id,
    )
    _raises(
        ValueError,
        history.evaluate,
        view,
        no_verifiable_progress_route_id=r1.route_id,
    )
    _raises(ValueError, history.evaluate, view, progress_kind="NOT_A_KIND")
    first = history.evaluate(view)
    assert first.classification is SCCClassification.SUSPECTED_RECURRENCE
    assert first.strike_count == 1
    assert first.exhausted_route_ids == ()


def test_speculative_requires_is_excluded_from_scc():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    speculative = Relation.build(
        relation_type=RelationType.REQUIRES,
        source=left.obligation_id,
        target=right.obligation_id,
        exact_claim="left might require right",
        scope=contract.scope,
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    back = _route(right, (left,), mechanism="back")
    view = _build_view(
        contract,
        obligations=(left, right),
        routes=(back,),
        relations=(speculative,),
    )
    _nodes, edges = project_research_graph(view)
    assert all(edge.kind is not ProjectedEdgeKind.REQUIRES for edge in edges)
    assessment = RecurrenceHistory().evaluate(view)
    assert assessment.classification is SCCClassification.ACYCLIC


def test_discharged_outside_is_neutral_with_open_exit():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    discharged = _obligation(contract, "discharged-outside")
    open_outside = _obligation(contract, "open-outside")
    evidence = _evidence(
        contract, claim=discharged.exact_statement, content="discharged-out"
    )
    cycle_left = _route(left, (right,), mechanism="cycle-l", group="or-1")
    cycle_right = _route(right, (left,), mechanism="cycle-r")
    mixed_exit = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="mixed-exit",
        required_obligation_ids=(
            discharged.obligation_id,
            open_outside.obligation_id,
        ),
        alternative_group="or-1",
    )
    view = _build_view(
        contract,
        evidence=(evidence,),
        obligations=(left, right, discharged, open_outside),
        routes=(cycle_left, cycle_right, mixed_exit),
    )
    view.apply(
        20,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": discharged.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [evidence.artifact_id],
            },
            "discharge-mixed",
        ),
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert mixed_exit.route_id in assessment.report.external_exits
    assert assessment.classification is SCCClassification.HAS_EXTERNAL_EXIT


def test_discharged_only_outside_is_not_an_exit():
    contract = _contract()
    left = _obligation(contract, "left")
    right = _obligation(contract, "right")
    discharged = _obligation(contract, "discharged-only")
    evidence = _evidence(
        contract, claim=discharged.exact_statement, content="discharged-only"
    )
    cycle_left = _route(left, (right,), mechanism="cycle-l", group="or-1")
    cycle_right = _route(right, (left,), mechanism="cycle-r")
    done_exit = _route(left, (discharged,), mechanism="done-exit", group="or-1")
    view = _build_view(
        contract,
        evidence=(evidence,),
        obligations=(left, right, discharged),
        routes=(cycle_left, cycle_right, done_exit),
    )
    view.apply(
        20,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": discharged.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [evidence.artifact_id],
            },
            "discharge-only",
        ),
    )
    assessment = RecurrenceHistory().evaluate(view)
    assert done_exit.route_id not in assessment.report.external_exits
    assert assessment.classification is SCCClassification.SUSPECTED_RECURRENCE


def test_internal_cycle_route_cannot_be_exhausted_or_park():
    contract = _contract()
    view, _left, _right, r1, r2 = _cycle(contract)
    history = RecurrenceHistory()
    history.evaluate(view)
    history.evaluate(view)
    third = history.evaluate(
        view,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=r1.route_id,
    )
    assert third.classification is SCCClassification.RECURRENT_NO_EXIT
    assert r1.route_id not in third.exhausted_route_ids
    assert r2.route_id not in third.exhausted_route_ids
    assert third.recommendation is RecurrenceRecommendation.BOUNDED_AUDIT
    assert third.audit_required is True


def test_malformed_public_records_fail_closed():
    valid = _hex("ok")
    _raises(
        ValueError,
        ProjectedEdge,
        kind=ProjectedEdgeKind.REQUIRES,
        source="not-hex",
        target=valid,
    )
    _raises(ValueError, StrikeKey, key_id="not-hex", nodes=(), premise_hash=valid)
    _raises(
        ValueError,
        SCCReport,
        nodes=("nope",),
        internal_edges=(),
        external_exits=(),
        descent_edges=(),
        classification=SCCClassification.ACYCLIC,
        evidence_frontier_hash=valid,
        premise_hash=valid,
        tool_hash=valid,
        nontrivial=False,
        scopes_compatible=True,
    )
    report = SCCReport(
        nodes=(),
        internal_edges=(),
        external_exits=(),
        descent_edges=(),
        classification=SCCClassification.ACYCLIC,
        evidence_frontier_hash=valid,
        premise_hash=valid,
        tool_hash=valid,
        nontrivial=False,
        scopes_compatible=True,
    )
    key = StrikeKey.build((), (), valid)
    fingerprint = FrontierFingerprint(
        evidence_hash=valid, premise_hash=valid, tool_hash=valid
    )
    _raises(
        ValueError,
        RecurrenceAssessment,
        classification=SCCClassification.ACYCLIC,
        recommendation=RecurrenceRecommendation.CONTINUE,
        audit_required=False,
        strike_count=-1,
        strike_key=key,
        fingerprint=fingerprint,
        report=report,
        exemptions=(),
        exhausted_route_ids=(),
    )
    recurrent = SCCReport(
        nodes=(),
        internal_edges=(),
        external_exits=(),
        descent_edges=(),
        classification=SCCClassification.RECURRENT_NO_EXIT,
        evidence_frontier_hash=valid,
        premise_hash=valid,
        tool_hash=valid,
        nontrivial=True,
        scopes_compatible=True,
    )
    _raises(
        ValueError,
        RecurrenceAssessment,
        classification=SCCClassification.RECURRENT_NO_EXIT,
        recommendation=RecurrenceRecommendation.PARK_ROUTE_CANDIDATE,
        audit_required=True,
        strike_count=3,
        strike_key=key,
        fingerprint=fingerprint,
        report=recurrent,
        exemptions=(),
        exhausted_route_ids=(),
    )
    _raises(
        ValueError,
        RecurrenceAssessment,
        classification=SCCClassification.RECURRENT_NO_EXIT,
        recommendation=RecurrenceRecommendation.BOUNDED_AUDIT,
        audit_required=False,
        strike_count=3,
        strike_key=key,
        fingerprint=fingerprint,
        report=recurrent,
        exemptions=(),
        exhausted_route_ids=(),
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
    for filename in ("scc.py", "recurrence.py", "persistent_recurrence.py"):
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


def main() -> None:
    tests = [
        test_acyclic_graph,
        test_true_no_exit_cycle_reaches_audit_threshold,
        test_verified_legal_descent_exempts_cycle,
        test_or_exit_and_exhaustion,
        test_empty_shell_route_is_not_an_exit,
        test_frontier_reset_on_new_evidence_and_tool,
        test_suspected_same_is_excluded_from_scc_edges,
        test_stale_and_parked_routes_are_not_exits,
        test_and_edges_remain_distinct_by_route_id,
        test_determinism_independent_of_insertion_order,
        test_engine_does_not_mutate_view,
        test_route_endpoints_project_through_target_obligation,
        test_full_scope_mismatch_prevents_recurrent_no_exit,
        test_mixed_descent_and_non_descent_cycle_is_not_exempt,
        test_nonviable_external_requirement_is_not_an_exit,
        test_nonhex_tool_hash_is_hashed_not_accepted,
        test_unrelated_exhausted_route_does_not_park_or_exhaust,
        test_progress_kind_misuse_with_exhausted_route_fails_closed,
        test_speculative_requires_is_excluded_from_scc,
        test_discharged_outside_is_neutral_with_open_exit,
        test_discharged_only_outside_is_not_an_exit,
        test_internal_cycle_route_cannot_be_exhausted_or_park,
        test_malformed_public_records_fail_closed,
        test_production_modules_do_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL RECURRENCE/SCC TESTS PASSED")


if __name__ == "__main__":
    main()
