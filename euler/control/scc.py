"""Deterministic research-graph projection and Tarjan SCC.

Stdlib only. Projection follows the reviewed edge whitelist and
never mutates ResearchView. Route AND edges stay distinct by route ID; OR
alternatives are not collapsed into one AND closure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

from euler.control.contracts import (
    CheckerResult,
    MathematicalScope,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.ids import canonical_json, stable_id
from euler.control.obligations import (
    Obligation,
    ObligationStatus,
    Relation,
    RelationType,
    Route,
    RouteStatus,
)
from euler.control.views import ResearchView, RouteReadiness

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_INVALID_OBLIGATION = frozenset(
    {ObligationStatus.REFUTED, ObligationStatus.PARKED}
)
_UNRESOLVED_OBLIGATION = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)
_NONVIABLE_ROUTE = frozenset(
    {RouteStatus.BLOCKED, RouteStatus.PARKED, RouteStatus.REFUTED}
)

_LIVE_ROUTE_STATUSES = frozenset({RouteStatus.PROPOSED, RouteStatus.ACTIVE})
_BLOCKED_READINESS = frozenset(
    {
        RouteReadiness.BLOCKED,
        RouteReadiness.PARKED,
        RouteReadiness.REFUTED,
        RouteReadiness.STALE,
    }
)

__all__ = [
    "ProjectedEdgeKind",
    "SCCClassification",
    "ProjectedEdge",
    "SCCReport",
    "project_research_graph",
    "tarjan_scc",
    "classify_scc",
]


class ProjectedEdgeKind(str, Enum):
    ROUTE_AND = "ROUTE_AND"
    ROUTE_DEPENDENCY = "ROUTE_DEPENDENCY"
    REQUIRES = "REQUIRES"
    REDUCES_TO = "REDUCES_TO"
    EQUIVALENT_UNDER = "EQUIVALENT_UNDER"
    BLOCKED_BY = "BLOCKED_BY"
    DESCENDS_BY = "DESCENDS_BY"


class SCCClassification(str, Enum):
    ACYCLIC = "ACYCLIC"
    LEGAL_DESCENT = "LEGAL_DESCENT"
    HAS_EXTERNAL_EXIT = "HAS_EXTERNAL_EXIT"
    CHANGED_FRONTIER = "CHANGED_FRONTIER"
    SUSPECTED_RECURRENCE = "SUSPECTED_RECURRENCE"
    RECURRENT_NO_EXIT = "RECURRENT_NO_EXIT"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectedEdge:
    kind: ProjectedEdgeKind
    source: str
    target: str
    route_id: str | None = None
    relation_id: str | None = None
    legal_descent: bool = False
    well_founded_measure: str | None = None
    side_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.kind, ProjectedEdgeKind):
            raise ValueError("kind must be a ProjectedEdgeKind")
        _require_hex64("source", self.source)
        _require_hex64("target", self.target)
        _optional_hex64("route_id", self.route_id)
        _optional_hex64("relation_id", self.relation_id)
        if not isinstance(self.legal_descent, bool):
            raise ValueError("legal_descent must be a bool")
        if self.well_founded_measure is not None:
            if (
                not isinstance(self.well_founded_measure, str)
                or self.well_founded_measure == ""
            ):
                raise ValueError("well_founded_measure must be None or a nonempty str")
        if isinstance(self.side_conditions, str) or not isinstance(
            self.side_conditions, tuple
        ):
            raise ValueError("side_conditions must be a tuple of str")
        for item in self.side_conditions:
            if not isinstance(item, str) or item == "":
                raise ValueError("side_conditions must be a tuple of nonempty str")

    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.kind.value,
            self.source,
            self.target,
            self.route_id or "",
            self.relation_id or "",
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "legal_descent": self.legal_descent,
            "relation_id": self.relation_id,
            "route_id": self.route_id,
            "side_conditions": list(self.side_conditions),
            "source": self.source,
            "target": self.target,
            "well_founded_measure": self.well_founded_measure,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SCCReport:
    nodes: tuple[str, ...]
    internal_edges: tuple[ProjectedEdge, ...]
    external_exits: tuple[str, ...]
    descent_edges: tuple[ProjectedEdge, ...]
    classification: SCCClassification
    evidence_frontier_hash: str
    premise_hash: str
    tool_hash: str
    nontrivial: bool
    scopes_compatible: bool

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.classification, SCCClassification):
            raise ValueError("classification must be an SCCClassification")
        _require_hex64_tuple("nodes", self.nodes)
        _require_edge_tuple("internal_edges", self.internal_edges)
        _require_hex64_tuple("external_exits", self.external_exits)
        _require_edge_tuple("descent_edges", self.descent_edges)
        _require_hex64("evidence_frontier_hash", self.evidence_frontier_hash)
        _require_hex64("premise_hash", self.premise_hash)
        _require_hex64("tool_hash", self.tool_hash)
        if not isinstance(self.nontrivial, bool):
            raise ValueError("nontrivial must be a bool")
        if not isinstance(self.scopes_compatible, bool):
            raise ValueError("scopes_compatible must be a bool")


def project_research_graph(
    view: ResearchView,
) -> tuple[tuple[str, ...], tuple[ProjectedEdge, ...]]:
    """Return sorted obligation nodes and reviewed directed edges."""
    nodes = tuple(item.obligation_id for item in view.obligations())
    node_set = set(nodes)
    edges: list[ProjectedEdge] = []
    for route in view.routes():
        edges.extend(_route_edges(view, route, node_set))
    for relation in view.relations():
        edges.extend(_relation_edges(view, relation, node_set))
    return nodes, _sorted_edges(edges)


def tarjan_scc(
    nodes: Sequence[str], edges: Sequence[ProjectedEdge]
) -> tuple[tuple[str, ...], ...]:
    """Deterministic Tarjan SCC. Node/edge insertion order does not matter."""
    ordered_nodes = tuple(sorted(set(nodes)))
    adjacency: dict[str, list[str]] = {node: [] for node in ordered_nodes}
    for edge in edges:
        if edge.source in adjacency and edge.target in adjacency:
            adjacency[edge.source].append(edge.target)
    for node in ordered_nodes:
        adjacency[node] = sorted(set(adjacency[node]))

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def strongconnect(vertex: str) -> None:
        nonlocal index
        indices[vertex] = index
        lowlink[vertex] = index
        index += 1
        stack.append(vertex)
        on_stack.add(vertex)
        for successor in adjacency[vertex]:
            if successor not in indices:
                strongconnect(successor)
                lowlink[vertex] = min(lowlink[vertex], lowlink[successor])
            elif successor in on_stack:
                lowlink[vertex] = min(lowlink[vertex], indices[successor])
        if lowlink[vertex] == indices[vertex]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == vertex:
                    break
            components.append(tuple(sorted(component)))

    for vertex in ordered_nodes:
        if vertex not in indices:
            strongconnect(vertex)
    components.sort(key=lambda item: item[0] if item else "")
    return tuple(components)


def classify_scc(
    view: ResearchView,
    nodes: Sequence[str],
    edges: Sequence[ProjectedEdge],
    *,
    evidence_frontier_hash: str,
    premise_hash: str,
    tool_hash: str,
    previous_evidence_hash: str | None = None,
    previous_tool_hash: str | None = None,
    strike_count: int = 0,
    threshold: int = 3,
    exhausted_route_ids: Iterable[str] = (),
) -> SCCReport:
    """Classify one SCC from already-projected edges. Does not mutate ``view``."""
    node_set = set(nodes)
    ordered_nodes = tuple(sorted(node_set))
    internal = _sorted_edges(
        edge
        for edge in edges
        if edge.source in node_set and edge.target in node_set
    )
    descent = tuple(edge for edge in internal if edge.legal_descent)
    exhausted = frozenset(exhausted_route_ids)
    exits = credible_or_exits(view, ordered_nodes, exhausted)
    nontrivial = _nontrivial(ordered_nodes, internal)
    compatible = _scopes_compatible(view, ordered_nodes)
    evidence_changed = (
        previous_evidence_hash is not None
        and previous_evidence_hash != evidence_frontier_hash
    )
    tool_changed = previous_tool_hash is not None and previous_tool_hash != tool_hash
    if not nontrivial:
        classification = SCCClassification.ACYCLIC
    elif descent and _acyclic_without_legal_descent(ordered_nodes, internal):
        classification = SCCClassification.LEGAL_DESCENT
    elif exits:
        classification = SCCClassification.HAS_EXTERNAL_EXIT
    elif evidence_changed or tool_changed:
        classification = SCCClassification.CHANGED_FRONTIER
    elif not compatible:
        classification = SCCClassification.SUSPECTED_RECURRENCE
    elif strike_count >= threshold:
        classification = SCCClassification.RECURRENT_NO_EXIT
    else:
        classification = SCCClassification.SUSPECTED_RECURRENCE
    return SCCReport(
        nodes=ordered_nodes,
        internal_edges=internal,
        external_exits=exits,
        descent_edges=descent,
        classification=classification,
        evidence_frontier_hash=evidence_frontier_hash,
        premise_hash=premise_hash,
        tool_hash=tool_hash,
        nontrivial=nontrivial,
        scopes_compatible=compatible,
    )


def credible_or_exits(
    view: ResearchView, scc_nodes: Sequence[str], exhausted_route_ids: Iterable[str]
) -> tuple[str, ...]:
    """VALID PROPOSED/ACTIVE same-target routes with a real edge leaving the SCC."""
    scc = set(scc_nodes)
    exhausted = frozenset(exhausted_route_ids)
    exits: list[str] = []
    for route in view.routes():
        if route.target_obligation_id not in scc:
            continue
        if not _live_route(route):
            continue
        readiness = view.route_readiness(route.route_id)
        if readiness in _BLOCKED_READINESS:
            continue
        if route.route_id in exhausted:
            continue
        if not route.required_obligation_ids and not route.dependencies:
            continue
        if _route_has_viable_external_exit(view, route, scc):
            exits.append(route.route_id)
    return tuple(sorted(exits))


def _route_edges(
    view: ResearchView, route: Route, node_set: set[str]
) -> list[ProjectedEdge]:
    if not _live_route(route):
        return []
    edges: list[ProjectedEdge] = []
    source = route.target_obligation_id
    if source not in node_set:
        return []
    for required in route.required_obligation_ids:
        if required in node_set:
            edges.append(
                ProjectedEdge(
                    kind=ProjectedEdgeKind.ROUTE_AND,
                    source=source,
                    target=required,
                    route_id=route.route_id,
                )
            )
    for dep in route.dependencies:
        target = _dependency_node(view, dep)
        if target in node_set:
            edges.append(
                ProjectedEdge(
                    kind=ProjectedEdgeKind.ROUTE_DEPENDENCY,
                    source=source,
                    target=target,
                    route_id=route.route_id,
                )
            )
    return edges


def _relation_edges(
    view: ResearchView, relation: Relation, node_set: set[str]
) -> list[ProjectedEdge]:
    if relation.validity is not ValidityStatus.VALID:
        return []
    source = _endpoint_obligation_id(view, relation.source)
    target = _endpoint_obligation_id(view, relation.target)
    if source is None or target is None:
        return []
    if source not in node_set or target not in node_set:
        return []
    kind = relation.relation_type
    if kind is RelationType.SUSPECTED_SAME or kind is RelationType.REFUTED_BY:
        return []
    if kind is RelationType.REQUIRES:
        if not _verified_relation(view, relation):
            return []
        return [
            ProjectedEdge(
                kind=ProjectedEdgeKind.REQUIRES,
                source=source,
                target=target,
                relation_id=relation.relation_id,
            )
        ]
    if kind is RelationType.BLOCKED_BY:
        return [
            ProjectedEdge(
                kind=ProjectedEdgeKind.BLOCKED_BY,
                source=source,
                target=target,
                relation_id=relation.relation_id,
            )
        ]
    if kind is RelationType.REDUCES_TO:
        if not _verified_relation(view, relation):
            return []
        return [
            ProjectedEdge(
                kind=ProjectedEdgeKind.REDUCES_TO,
                source=source,
                target=target,
                relation_id=relation.relation_id,
            )
        ]
    if kind is RelationType.EQUIVALENT_UNDER:
        if not _verified_relation(view, relation):
            return []
        return [
            ProjectedEdge(
                kind=ProjectedEdgeKind.EQUIVALENT_UNDER,
                source=source,
                target=target,
                relation_id=relation.relation_id,
            ),
            ProjectedEdge(
                kind=ProjectedEdgeKind.EQUIVALENT_UNDER,
                source=target,
                target=source,
                relation_id=relation.relation_id,
            ),
        ]
    if kind is RelationType.DESCENDS_BY:
        legal = _legal_descent(view, relation)
        if not legal:
            return []
        return [
            ProjectedEdge(
                kind=ProjectedEdgeKind.DESCENDS_BY,
                source=source,
                target=target,
                relation_id=relation.relation_id,
                legal_descent=True,
                well_founded_measure=relation.well_founded_measure,
                side_conditions=relation.side_conditions,
            )
        ]
    return []


def _live_route(route: Route) -> bool:
    return (
        route.validity is ValidityStatus.VALID
        and route.status in _LIVE_ROUTE_STATUSES
    )


def _endpoint_obligation_id(view: ResearchView, endpoint: str) -> str | None:
    """Resolve a relation endpoint to an obligation ID (routes via target)."""
    obligation = view.get_obligation(endpoint)
    if obligation is not None:
        return obligation.obligation_id
    route = view.get_route(endpoint)
    if route is not None:
        return route.target_obligation_id
    return None


def _dependency_node(view: ResearchView, dep: str) -> str:
    other = view.get_route(dep)
    if other is not None:
        return other.target_obligation_id
    return dep


def _verified_relation(view: ResearchView, relation: Relation) -> bool:
    if relation.evidence_kind is None or relation.contract_id is None:
        return False
    if not relation.evidence_refs:
        return False
    for ref in relation.evidence_refs:
        artifact = view.get_evidence(ref)
        if artifact is None:
            continue
        if evidence_satisfies(
            artifact,
            relation.evidence_kind,
            relation.scope,
            relation.contract_id,
            exact_claim=relation.exact_claim,
        ):
            return True
    return False


def _legal_descent(view: ResearchView, relation: Relation) -> bool:
    if not isinstance(relation.well_founded_measure, str) or relation.well_founded_measure == "":
        return False
    if not relation.side_conditions:
        return False
    return _verified_relation(view, relation)


def _route_has_viable_external_exit(
    view: ResearchView, route: Route, scc: set[str]
) -> bool:
    """Credible exit: all outside AND refs viable; at least one unresolved."""
    unresolved = False
    for required in route.required_obligation_ids:
        if required in scc:
            continue
        status = _outside_obligation_class(view, required)
        if status == "invalid":
            return False
        if status == "unresolved":
            unresolved = True
    for dep in route.dependencies:
        target = _dependency_node(view, dep)
        if target in scc:
            continue
        status = _outside_dependency_class(view, dep)
        if status == "invalid":
            return False
        if status == "unresolved":
            unresolved = True
    return unresolved


def _outside_obligation_class(view: ResearchView, obligation_id: str) -> str:
    obligation = view.get_obligation(obligation_id)
    if obligation is None:
        return "invalid"
    return _obligation_outside_class(obligation)


def _obligation_outside_class(obligation: Obligation) -> str:
    if obligation.validity is not ValidityStatus.VALID:
        return "invalid"
    if obligation.status in _INVALID_OBLIGATION:
        return "invalid"
    if obligation.status is ObligationStatus.DISCHARGED:
        return "neutral"
    if obligation.status in _UNRESOLVED_OBLIGATION:
        return "unresolved"
    return "invalid"


def _outside_dependency_class(view: ResearchView, dep: str) -> str:
    obligation = view.get_obligation(dep)
    if obligation is not None:
        return _obligation_outside_class(obligation)
    route = view.get_route(dep)
    if route is None:
        return "invalid"
    if route.validity is not ValidityStatus.VALID:
        return "invalid"
    if route.status in _NONVIABLE_ROUTE:
        return "invalid"
    if view.route_readiness(route.route_id) in _BLOCKED_READINESS:
        return "invalid"
    target = view.get_obligation(route.target_obligation_id)
    if target is None:
        return "invalid"
    return _obligation_outside_class(target)


def _acyclic_without_legal_descent(
    nodes: Sequence[str], internal: Sequence[ProjectedEdge]
) -> bool:
    remaining = tuple(edge for edge in internal if not edge.legal_descent)
    for component in tarjan_scc(nodes, remaining):
        comp = set(component)
        comp_edges = [
            edge
            for edge in remaining
            if edge.source in comp and edge.target in comp
        ]
        if _nontrivial(component, comp_edges):
            return False
    return True


def _nontrivial(nodes: Sequence[str], internal: Sequence[ProjectedEdge]) -> bool:
    if len(nodes) > 1:
        return True
    if len(nodes) == 1:
        node = nodes[0]
        return any(edge.source == node and edge.target == node for edge in internal)
    return False


def _scopes_compatible(view: ResearchView, nodes: Sequence[str]) -> bool:
    expected: tuple[str, MathematicalScope] | None = None
    for oid in nodes:
        obligation = view.get_obligation(oid)
        if obligation is None:
            return False
        signature = (obligation.contract_id, obligation.scope)
        if expected is None:
            expected = signature
        elif signature != expected:
            return False
    return True


def _sorted_edges(edges: Iterable[ProjectedEdge]) -> tuple[ProjectedEdge, ...]:
    unique: dict[tuple[str, str, str, str, str], ProjectedEdge] = {}
    for edge in edges:
        unique[edge.sort_key()] = edge
    return tuple(sorted(unique.values(), key=lambda item: item.sort_key()))


def scc_pass_artifact_ids(view: ResearchView, nodes: Sequence[str]) -> tuple[str, ...]:
    """VALID+PASS artifact IDs attached to the SCC closure."""
    node_set = set(nodes)
    refs: set[str] = set()
    for oid in node_set:
        obligation = view.get_obligation(oid)
        if obligation is not None:
            refs.update(obligation.evidence_refs)
    for route in view.routes():
        if route.target_obligation_id in node_set:
            refs.update(route.evidence_refs)
    for relation in view.relations():
        source = _endpoint_obligation_id(view, relation.source)
        target = _endpoint_obligation_id(view, relation.target)
        if source in node_set or target in node_set:
            refs.update(relation.evidence_refs)
    passing: list[str] = []
    for artifact_id in refs:
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            continue
        if (
            artifact.validity is ValidityStatus.VALID
            and artifact.checker_result is CheckerResult.PASS
        ):
            passing.append(artifact_id)
    return tuple(sorted(passing))


def scc_premise_hash(view: ResearchView, nodes: Sequence[str]) -> str:
    parts: list[str] = []
    for oid in sorted(nodes):
        obligation = view.get_obligation(oid)
        if obligation is None:
            continue
        contract = view.get_contract(obligation.contract_id)
        version = contract.version if contract is not None else 0
        parts.append(
            canonical_json(
                {
                    "contract_id": obligation.contract_id,
                    "contract_version": version,
                    "definitions_hash": obligation.scope.definitions_hash,
                    "obligation_id": oid,
                    "obligation_version": obligation.version,
                    "scope": obligation.scope.to_dict(),
                }
            )
        )
    if not parts:
        return stable_id("premise", 1, "")
    return stable_id("premise", 1, *parts)


def _require_hex64(name: str, value: object) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 64-char lowercase hex hash")
    return value


def _optional_hex64(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _require_hex64(name, value)


def _require_hex64_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple of 64-hex hashes")
    for index, item in enumerate(value):
        _require_hex64(f"{name}[{index}]", item)
    return value


def _require_edge_tuple(name: str, value: object) -> tuple[ProjectedEdge, ...]:
    if isinstance(value, str) or not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple of ProjectedEdge")
    for index, item in enumerate(value):
        if not isinstance(item, ProjectedEdge):
            raise ValueError(f"{name}[{index}] must be a ProjectedEdge")
        item.validate()
    return value
