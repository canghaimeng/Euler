"""Event-replayed research view over Phase 2 obligation-graph events.

Stdlib only. The view is a deterministic projection: unknown events, duplicate
seq with a different payload, missing refs and illegal transitions fail closed.
Ready routes are reported as READY_FOR_REVIEW and never auto-discharge a target.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping

from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.failures import FailureKind, FailureRecord
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
from euler.control.schema import EventEnvelope, SchemaValidationError

_EVENT_CONTRACT_CREATED = "problem.contract.created"
_EVENT_EVIDENCE_REGISTERED = "evidence.registered"
_EVENT_EVIDENCE_INVALIDATED = "evidence.invalidated"
_EVENT_OBLIGATION_CREATED = "obligation.created"
_EVENT_OBLIGATION_STATUS = "obligation.status_changed"
_EVENT_OBLIGATION_REOPENED = "obligation.reopened"
_EVENT_ROUTE_CREATED = "route.created"
_EVENT_ROUTE_STATUS = "route.status_changed"
_EVENT_RELATION_CREATED = "relation.created"
_EVENT_FAILURE_RECORDED = "failure.recorded"

_ALLOWED_EVENT_TYPES = frozenset(
    {
        _EVENT_CONTRACT_CREATED,
        _EVENT_EVIDENCE_REGISTERED,
        _EVENT_EVIDENCE_INVALIDATED,
        _EVENT_OBLIGATION_CREATED,
        _EVENT_OBLIGATION_STATUS,
        _EVENT_OBLIGATION_REOPENED,
        _EVENT_ROUTE_CREATED,
        _EVENT_ROUTE_STATUS,
        _EVENT_RELATION_CREATED,
        _EVENT_FAILURE_RECORDED,
    }
)
_OPEN_OBLIGATION = frozenset(
    {ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS}
)
_READY_ROUTE_STATUSES = frozenset({RouteStatus.PROPOSED, RouteStatus.ACTIVE})
_EVIDENCE_REQUIRED_RELATIONS = frozenset(
    {
        RelationType.REDUCES_TO,
        RelationType.EQUIVALENT_UNDER,
        RelationType.REFUTED_BY,
        RelationType.DESCENDS_BY,
    }
)
_PAYLOAD_SPEC: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    _EVENT_CONTRACT_CREATED: (frozenset({"contract"}), frozenset()),
    _EVENT_EVIDENCE_REGISTERED: (frozenset({"evidence"}), frozenset()),
    _EVENT_EVIDENCE_INVALIDATED: (frozenset({"artifact_id"}), frozenset()),
    _EVENT_OBLIGATION_CREATED: (frozenset({"obligation"}), frozenset()),
    _EVENT_OBLIGATION_STATUS: (
        frozenset({"obligation_id", "status"}),
        frozenset({"evidence_refs"}),
    ),
    _EVENT_OBLIGATION_REOPENED: (
        frozenset({"obligation_id", "reason"}),
        frozenset({"evidence_refs", "new_premises", "new_tool"}),
    ),
    _EVENT_ROUTE_CREATED: (frozenset({"route"}), frozenset()),
    _EVENT_ROUTE_STATUS: (
        frozenset({"route_id", "status"}),
        frozenset({"evidence_refs", "reason"}),
    ),
    _EVENT_RELATION_CREATED: (frozenset({"relation"}), frozenset()),
    _EVENT_FAILURE_RECORDED: (frozenset({"failure"}), frozenset()),
}

__all__ = [
    "ResearchViewError",
    "UnknownResearchEvent",
    "InvalidResearchTransition",
    "RouteReadiness",
    "StaleDependencies",
    "ResearchView",
]


class ResearchViewError(Exception):
    """Malformed research-view input or non-deterministic replay."""


class UnknownResearchEvent(ResearchViewError):
    """Event type is not in the Phase 2 projection set."""


class InvalidResearchTransition(ResearchViewError):
    """Illegal status change, missing reference, or conflicting replay."""


class RouteReadiness(str, Enum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    PARKED = "PARKED"
    COMPLETED = "COMPLETED"
    REFUTED = "REFUTED"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True, kw_only=True)
class StaleDependencies:
    obligation_ids: tuple[str, ...]
    route_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]


class ResearchView:
    """Deterministic projection of Phase 2 research-graph events."""

    def __init__(self) -> None:
        self._by_seq: dict[int, str] = {}
        self._max_seq = 0
        self._contracts: dict[str, ProblemContract] = {}
        self._evidence: dict[str, EvidenceArtifact] = {}
        self._evidence_validity: dict[str, ValidityStatus] = {}
        self._obligations: dict[str, Obligation] = {}
        self._routes: dict[str, Route] = {}
        self._relations: dict[str, Relation] = {}
        self._failures: dict[str, FailureRecord] = {}

    def replay(self, events: Iterable[tuple[int, EventEnvelope]]) -> None:
        ordered = list(events)
        try:
            ordered.sort(key=lambda item: item[0])
        except TypeError as exc:
            raise ResearchViewError("replay events must be (seq, envelope) pairs") from exc
        for seq, envelope in ordered:
            self.apply(seq, envelope)

    def apply(self, seq: int, envelope: EventEnvelope) -> None:
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ResearchViewError("seq must be a positive int")
        if not isinstance(envelope, EventEnvelope):
            raise ResearchViewError("apply requires an EventEnvelope")
        previous = self._by_seq.get(seq)
        if previous is not None:
            if previous == envelope.event_id:
                return
            raise InvalidResearchTransition(
                f"duplicate seq {seq} with a different event"
            )
        if self._max_seq and seq <= self._max_seq:
            raise ResearchViewError(f"event seq {seq} is out of order")
        if envelope.event_type not in _ALLOWED_EVENT_TYPES:
            raise UnknownResearchEvent(
                f"unsupported research event type {envelope.event_type!r}"
            )
        payload = dict(envelope.payload)
        _require_payload_shape(envelope.event_type, payload)
        try:
            self._dispatch(envelope.event_type, payload)
        except (ObligationGraphError, SchemaValidationError) as exc:
            raise InvalidResearchTransition(str(exc)) from exc
        self._by_seq[seq] = envelope.event_id
        self._max_seq = seq

    def frontier(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                oid
                for oid, obligation in self._obligations.items()
                if obligation.status in _OPEN_OBLIGATION
            )
        )

    def route_readiness(self, route_id: str) -> RouteReadiness:
        route = self._routes.get(route_id)
        if route is None:
            return RouteReadiness.MISSING
        if route.validity is not ValidityStatus.VALID:
            return RouteReadiness.STALE
        if self._has_stale_requirement(route):
            return RouteReadiness.STALE
        if route.status is RouteStatus.BLOCKED:
            return RouteReadiness.BLOCKED
        if route.status is RouteStatus.PARKED:
            return RouteReadiness.PARKED
        if route.status is RouteStatus.COMPLETED:
            return RouteReadiness.COMPLETED
        if route.status is RouteStatus.REFUTED:
            return RouteReadiness.REFUTED
        if route.status not in _READY_ROUTE_STATUSES:
            return RouteReadiness.WAITING
        if self._and_requirements_met(route):
            return RouteReadiness.READY_FOR_REVIEW
        return RouteReadiness.WAITING

    def alternatives(self, obligation_id: str) -> tuple[Route, ...]:
        routes = [
            route
            for route in self._routes.values()
            if route.target_obligation_id == obligation_id
        ]
        return tuple(sorted(routes, key=lambda item: item.route_id))

    def stale_dependencies(self) -> StaleDependencies:
        obligation_ids = tuple(
            sorted(
                oid
                for oid, obligation in self._obligations.items()
                if obligation.validity is not ValidityStatus.VALID
            )
        )
        route_ids = tuple(
            sorted(
                rid
                for rid, route in self._routes.items()
                if route.validity is not ValidityStatus.VALID
            )
        )
        relation_ids = tuple(
            sorted(
                rid
                for rid, relation in self._relations.items()
                if relation.validity is not ValidityStatus.VALID
            )
        )
        return StaleDependencies(
            obligation_ids=obligation_ids,
            route_ids=route_ids,
            relation_ids=relation_ids,
        )

    def get_contract(self, contract_id: str) -> ProblemContract | None:
        return self._contracts.get(contract_id)

    def get_evidence(self, artifact_id: str) -> EvidenceArtifact | None:
        return self._evidence.get(artifact_id)

    def get_obligation(self, obligation_id: str) -> Obligation | None:
        return self._obligations.get(obligation_id)

    def get_route(self, route_id: str) -> Route | None:
        return self._routes.get(route_id)

    def get_relation(self, relation_id: str) -> Relation | None:
        return self._relations.get(relation_id)

    def get_failure(self, failure_id: str) -> FailureRecord | None:
        return self._failures.get(failure_id)

    def evidence_validity(self, artifact_id: str) -> ValidityStatus | None:
        return self._evidence_validity.get(artifact_id)

    def contracts(self) -> tuple[ProblemContract, ...]:
        return tuple(self._contracts[key] for key in sorted(self._contracts))

    def evidence(self) -> tuple[EvidenceArtifact, ...]:
        return tuple(self._evidence[key] for key in sorted(self._evidence))

    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(self._obligations[key] for key in sorted(self._obligations))

    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes[key] for key in sorted(self._routes))

    def relations(self) -> tuple[Relation, ...]:
        return tuple(self._relations[key] for key in sorted(self._relations))

    def failures(self) -> tuple[FailureRecord, ...]:
        return tuple(self._failures[key] for key in sorted(self._failures))

    def _dispatch(self, event_type: str, payload: Mapping[str, object]) -> None:
        if event_type == _EVENT_CONTRACT_CREATED:
            self._apply_contract_created(payload)
        elif event_type == _EVENT_EVIDENCE_REGISTERED:
            self._apply_evidence_registered(payload)
        elif event_type == _EVENT_EVIDENCE_INVALIDATED:
            self._apply_evidence_invalidated(payload)
        elif event_type == _EVENT_OBLIGATION_CREATED:
            self._apply_obligation_created(payload)
        elif event_type == _EVENT_OBLIGATION_STATUS:
            self._apply_obligation_status(payload)
        elif event_type == _EVENT_OBLIGATION_REOPENED:
            self._apply_obligation_reopened(payload)
        elif event_type == _EVENT_ROUTE_CREATED:
            self._apply_route_created(payload)
        elif event_type == _EVENT_ROUTE_STATUS:
            self._apply_route_status(payload)
        elif event_type == _EVENT_RELATION_CREATED:
            self._apply_relation_created(payload)
        elif event_type == _EVENT_FAILURE_RECORDED:
            self._apply_failure_recorded(payload)
        else:  # pragma: no cover - guarded by allow-list
            raise UnknownResearchEvent(event_type)

    def _apply_contract_created(self, payload: Mapping[str, object]) -> None:
        record = _nested_record(payload, "contract")
        contract = ProblemContract.from_dict(record)
        self._put_unique("contract", self._contracts, contract.contract_id, contract)

    def _apply_evidence_registered(self, payload: Mapping[str, object]) -> None:
        record = _nested_record(payload, "evidence")
        evidence = EvidenceArtifact.from_dict(record)
        contract_id = getattr(evidence, "contract_id", None)
        if contract_id is not None and contract_id not in self._contracts:
            raise InvalidResearchTransition(
                f"evidence references missing contract {contract_id!r}"
            )
        if evidence.validity is not ValidityStatus.VALID:
            raise InvalidResearchTransition(
                "registered EvidenceArtifact must have initial validity VALID"
            )
        self._put_unique("evidence", self._evidence, evidence.artifact_id, evidence)
        self._evidence_validity[evidence.artifact_id] = ValidityStatus.VALID

    def _apply_evidence_invalidated(self, payload: Mapping[str, object]) -> None:
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or artifact_id == "":
            raise InvalidResearchTransition("evidence.invalidated requires artifact_id")
        if artifact_id not in self._evidence:
            raise InvalidResearchTransition(
                f"cannot invalidate missing evidence {artifact_id!r}"
            )
        evidence = self._evidence[artifact_id]
        self._evidence[artifact_id] = replace(evidence, validity=ValidityStatus.STALE)
        self._evidence_validity[artifact_id] = ValidityStatus.STALE
        for obligation_id, obligation in list(self._obligations.items()):
            if artifact_id in obligation.evidence_refs:
                self._obligations[obligation_id] = replace(
                    obligation, validity=ValidityStatus.STALE
                )
        for route_id, route in list(self._routes.items()):
            if artifact_id in route.evidence_refs:
                self._routes[route_id] = replace(route, validity=ValidityStatus.STALE)
        for relation_id, relation in list(self._relations.items()):
            if artifact_id in relation.evidence_refs:
                self._relations[relation_id] = replace(
                    relation, validity=ValidityStatus.STALE
                )

    def _apply_obligation_created(self, payload: Mapping[str, object]) -> None:
        record = _nested_record(payload, "obligation")
        obligation = Obligation.from_dict(record)
        if obligation.contract_id not in self._contracts:
            raise InvalidResearchTransition(
                f"obligation references missing contract {obligation.contract_id!r}"
            )
        self._require_known_evidence(obligation.evidence_refs)
        if obligation.status is ObligationStatus.DISCHARGED:
            self._require_discharge_evidence(obligation, obligation.evidence_refs)
        elif obligation.status is ObligationStatus.REFUTED:
            self._require_refutation_evidence(obligation, obligation.evidence_refs)
        self._put_unique(
            "obligation", self._obligations, obligation.obligation_id, obligation
        )

    def _apply_obligation_status(self, payload: Mapping[str, object]) -> None:
        obligation_id = _require_str(payload, "obligation_id")
        status = payload.get("status")
        obligation = self._require_obligation(obligation_id)
        new_status = status
        refs = payload.get("evidence_refs")
        if new_status in {"DISCHARGED", "REFUTED", ObligationStatus.DISCHARGED, ObligationStatus.REFUTED}:
            if refs is None:
                raise InvalidResearchTransition(
                    f"{new_status} requires evidence_refs"
                )
        if refs is not None:
            self._require_known_evidence(refs)
        updated = transition_obligation(
            obligation,
            status,  # type: ignore[arg-type]
            evidence_refs=None if refs is None else refs,
        )
        if updated.status is ObligationStatus.DISCHARGED:
            self._require_discharge_evidence(updated, updated.evidence_refs)
        if updated.status is ObligationStatus.REFUTED:
            self._require_refutation_evidence(updated, updated.evidence_refs)
        self._obligations[obligation_id] = updated

    def _apply_obligation_reopened(self, payload: Mapping[str, object]) -> None:
        obligation_id = _require_str(payload, "obligation_id")
        reason = payload.get("reason")
        refs = payload.get("evidence_refs")
        new_premises = payload.get("new_premises")
        new_tool = payload.get("new_tool")
        obligation = self._require_obligation(obligation_id)
        if refs is not None:
            self._require_known_evidence(refs)
        try:
            updated = transition_obligation(
                obligation,
                ObligationStatus.OPEN,
                evidence_refs=None if refs is None else refs,
                reason=reason if isinstance(reason, str) else None,
                new_premises=new_premises,
                new_tool=new_tool,
            )
        except ObligationGraphError as exc:
            raise InvalidResearchTransition(str(exc)) from exc
        self._obligations[obligation_id] = updated

    def _apply_route_created(self, payload: Mapping[str, object]) -> None:
        record = _nested_record(payload, "route")
        route = Route.from_dict(record)
        self._require_obligation(route.target_obligation_id)
        for required_id in route.required_obligation_ids:
            self._require_obligation(required_id)
        for dep in route.dependencies:
            if dep not in self._obligations and dep not in self._routes:
                raise InvalidResearchTransition(f"route dependency {dep!r} is missing")
        self._require_known_evidence(route.evidence_refs)
        self._put_unique("route", self._routes, route.route_id, route)

    def _apply_route_status(self, payload: Mapping[str, object]) -> None:
        route_id = _require_str(payload, "route_id")
        status = payload.get("status")
        route = self._routes.get(route_id)
        if route is None:
            raise InvalidResearchTransition(f"unknown route {route_id!r}")
        refs = payload.get("evidence_refs")
        if status in {"COMPLETED", "REFUTED", RouteStatus.COMPLETED, RouteStatus.REFUTED}:
            if refs is None:
                raise InvalidResearchTransition(f"{status} route requires evidence_refs")
        if refs is not None:
            self._require_known_evidence(refs)
        reason = payload.get("reason")
        updated = transition_route(
            route,
            status,  # type: ignore[arg-type]
            evidence_refs=None if refs is None else refs,
            reason=reason if isinstance(reason, str) else None,
        )
        if updated.status in {RouteStatus.COMPLETED, RouteStatus.REFUTED}:
            self._require_route_terminal_evidence(updated)
        self._routes[route_id] = updated

    def _apply_relation_created(self, payload: Mapping[str, object]) -> None:
        record = _nested_record(payload, "relation")
        relation = Relation.from_dict(record)
        if not self._known_node(relation.source):
            raise InvalidResearchTransition(
                f"relation source {relation.source!r} is missing"
            )
        if not self._known_node(relation.target):
            raise InvalidResearchTransition(
                f"relation target {relation.target!r} is missing"
            )
        self._require_endpoint_alignment(relation)
        self._require_known_evidence(relation.evidence_refs)
        if relation.contract_id is not None and relation.contract_id not in self._contracts:
            raise InvalidResearchTransition(
                f"relation references missing contract {relation.contract_id!r}"
            )
        if relation.relation_type in _EVIDENCE_REQUIRED_RELATIONS:
            self._require_relation_evidence(relation)
        self._put_unique("relation", self._relations, relation.relation_id, relation)

    def _apply_failure_recorded(self, payload: Mapping[str, object]) -> None:
        record = _nested_record(payload, "failure")
        failure = FailureRecord.from_dict(record)
        failure_id = getattr(failure, "failure_id", None)
        if not isinstance(failure_id, str) or failure_id == "":
            raise InvalidResearchTransition("FailureRecord is missing failure_id")
        if failure.contract_id is not None and failure.contract_id not in self._contracts:
            raise InvalidResearchTransition(
                f"failure references missing contract {failure.contract_id!r}"
            )
        if failure.target_obligation_id is not None:
            target = self._require_obligation(failure.target_obligation_id)
            if failure.scope != target.scope:
                raise InvalidResearchTransition(
                    "failure scope does not match the referenced obligation"
                )
            if (
                failure.contract_id is not None
                and failure.contract_id != target.contract_id
            ):
                raise InvalidResearchTransition(
                    "failure contract_id does not match the referenced obligation"
                )
        if failure.route_id is not None:
            route = self._routes.get(failure.route_id)
            if route is None:
                raise InvalidResearchTransition(
                    f"failure references missing route {failure.route_id!r}"
                )
            if (
                failure.target_obligation_id is not None
                and failure.target_obligation_id not in {
                    route.target_obligation_id, *route.required_obligation_ids,
                }
            ):
                raise InvalidResearchTransition(
                    "failure route target does not match the referenced obligation"
                )
        self._require_known_evidence(failure.witness_refs)
        if failure.kind is FailureKind.CERTIFIED_COUNTEREXAMPLE:
            self._require_certified_counterexample_witnesses(failure)
        self._put_unique("failure", self._failures, failure_id, failure)

    def _has_stale_requirement(self, route: Route) -> bool:
        for required_id in route.required_obligation_ids:
            required = self._obligations.get(required_id)
            if required is not None and required.validity is not ValidityStatus.VALID:
                return True
        for dep in route.dependencies:
            obligation = self._obligations.get(dep)
            if obligation is not None and obligation.validity is not ValidityStatus.VALID:
                return True
        return False

    def _and_requirements_met(self, route: Route) -> bool:
        if not route.required_obligation_ids:
            return False
        for required_id in route.required_obligation_ids:
            required = self._obligations.get(required_id)
            if required is None or required.status is not ObligationStatus.DISCHARGED:
                return False
        for dep in route.dependencies:
            obligation = self._obligations.get(dep)
            if obligation is not None:
                if obligation.status is not ObligationStatus.DISCHARGED:
                    return False
                continue
            other = self._routes.get(dep)
            if other is None or self.route_readiness(other.route_id) not in {
                RouteReadiness.READY_FOR_REVIEW,
                RouteReadiness.COMPLETED,
            }:
                return False
        return True

    def _require_discharge_evidence(
        self, obligation: Obligation, refs: object
    ) -> None:
        contract = self._contracts.get(obligation.contract_id)
        if contract is None:
            raise InvalidResearchTransition(
                f"missing contract {obligation.contract_id!r} for discharge"
            )
        artifacts = self._artifacts_for_refs(refs, required=True)
        if not any(
            evidence_satisfies(
                artifact,
                contract.required_evidence_level,
                obligation.scope,
                obligation.contract_id,
                exact_claim=obligation.exact_statement,
            )
            for artifact in artifacts
        ):
            raise InvalidResearchTransition(
                "DISCHARGED requires evidence that satisfies the contract "
                "requirement with exact obligation scope, contract, and claim"
            )

    def _require_refutation_evidence(
        self, obligation: Obligation, refs: object
    ) -> None:
        artifacts = self._artifacts_for_refs(refs, required=True)
        if not any(
            evidence_satisfies(
                artifact,
                EvidenceKind.CHECKED_COUNTEREXAMPLE,
                obligation.scope,
                obligation.contract_id,
                exact_claim=obligation.exact_statement,
            )
            for artifact in artifacts
        ):
            raise InvalidResearchTransition(
                "REFUTED requires PASS checked counterexample/certificate "
                "evidence with exact obligation scope, contract, and claim"
            )
        for relation in self._relations.values():
            if relation.relation_type is not RelationType.REFUTED_BY:
                continue
            if relation.target != obligation.obligation_id:
                continue
            if relation.validity is not ValidityStatus.VALID:
                continue
            if relation.scope != obligation.scope:
                raise InvalidResearchTransition(
                    "REFUTED_BY scope does not match the obligation scope"
                )

    def _require_relation_evidence(self, relation: Relation) -> None:
        if relation.evidence_kind is None or relation.contract_id is None:
            raise InvalidResearchTransition(
                f"{relation.relation_type.value} is missing evidence kind or contract"
            )
        contract = self._contracts.get(relation.contract_id)
        if contract is None:
            raise InvalidResearchTransition(
                f"relation references missing contract {relation.contract_id!r}"
            )
        if relation.definitions_hash != contract.scope.definitions_hash:
            raise InvalidResearchTransition(
                "relation definitions_hash does not match the contract scope"
            )
        artifacts = self._artifacts_for_refs(relation.evidence_refs, required=True)
        required_kind = (
            EvidenceKind.CHECKED_COUNTEREXAMPLE
            if relation.relation_type is RelationType.REFUTED_BY
            else relation.evidence_kind
        )
        for artifact in artifacts:
            if artifact.evidence_kind is EvidenceKind.HEURISTIC_OBSERVATION:
                raise InvalidResearchTransition(
                    f"{relation.relation_type.value} cannot use heuristic evidence"
                )
            if not evidence_satisfies(
                artifact,
                required_kind,
                relation.scope,
                relation.contract_id,
                exact_claim=relation.exact_claim,
            ):
                raise InvalidResearchTransition(
                    f"{relation.relation_type.value} evidence does not match "
                    "the relation kind, scope, contract, or checker PASS"
                )

    def _require_route_terminal_evidence(self, route: Route) -> None:
        target = self._require_obligation(route.target_obligation_id)
        artifacts = self._artifacts_for_refs(route.evidence_refs, required=True)
        if route.status is RouteStatus.REFUTED:
            claims = [(target.exact_statement, target.scope)]
            claims.extend((self._require_obligation(oid).exact_statement,
                           self._require_obligation(oid).scope)
                          for oid in route.required_obligation_ids)
            if route.progress_claim:
                claims.append((route.progress_claim, target.scope))
            if not any(
                evidence_satisfies(
                    artifact,
                    EvidenceKind.CHECKED_COUNTEREXAMPLE,
                    claim_scope,
                    target.contract_id,
                    exact_claim=claim,
                )
                for artifact in artifacts
                for claim, claim_scope in claims
            ):
                raise InvalidResearchTransition(
                    "REFUTED route requires a checked counterexample to its exact scoped target, premise or closing implication"
                )
            return
        if not any(
            artifact.validity is ValidityStatus.VALID
            and artifact.checker_result is CheckerResult.PASS
            and artifact.evidence_kind is not EvidenceKind.HEURISTIC_OBSERVATION
            and artifact.contract_id == target.contract_id
            and artifact.scope == target.scope
            for artifact in artifacts
        ):
            raise InvalidResearchTransition(
                "COMPLETED route requires PASS non-heuristic evidence with the "
                "target obligation contract and scope"
            )

    def _require_certified_counterexample_witnesses(self, failure: FailureRecord) -> None:
        if not failure.witness_refs:
            raise InvalidResearchTransition(
                "CERTIFIED_COUNTEREXAMPLE requires nonempty witness_refs"
            )
        contract_id = failure.contract_id
        if contract_id is None:
            raise InvalidResearchTransition(
                "CERTIFIED_COUNTEREXAMPLE requires contract_id"
            )
        scope = failure.exclusion_scope if failure.exclusion_scope is not None else failure.scope
        for artifact in self._artifacts_for_refs(failure.witness_refs, required=True):
            if not evidence_satisfies(
                artifact,
                EvidenceKind.CHECKED_COUNTEREXAMPLE,
                scope,
                contract_id,
            ):
                raise InvalidResearchTransition(
                    "CERTIFIED_COUNTEREXAMPLE witness is not PASS checked "
                    "counterexample/certificate evidence for the exact scope"
                )
            if artifact.scope != failure.scope:
                raise InvalidResearchTransition(
                    "CERTIFIED_COUNTEREXAMPLE witness scope does not match the claim"
                )

    def _artifacts_for_refs(
        self, refs: object, *, required: bool
    ) -> list[EvidenceArtifact]:
        if refs is None:
            if required:
                raise InvalidResearchTransition("evidence refs are required")
            return []
        if isinstance(refs, str) or not isinstance(refs, (list, tuple)):
            raise InvalidResearchTransition("evidence_refs must be a list of ids")
        if required and len(refs) == 0:
            raise InvalidResearchTransition("evidence refs must be nonempty")
        artifacts: list[EvidenceArtifact] = []
        for item in refs:
            artifacts.append(self._resolve_artifact(item))
        return artifacts

    def _resolve_artifact(self, ref: object) -> EvidenceArtifact:
        if not isinstance(ref, str) or ref == "":
            raise InvalidResearchTransition("evidence ref must be a nonempty str")
        if ref not in self._evidence:
            raise InvalidResearchTransition(
                f"unknown evidence artifact_id {ref!r}"
            )
        if self._evidence_validity.get(ref) is ValidityStatus.STALE:
            raise InvalidResearchTransition(f"evidence {ref!r} is stale")
        return self._evidence[ref]

    def _require_obligation(self, obligation_id: str) -> Obligation:
        obligation = self._obligations.get(obligation_id)
        if obligation is None:
            raise InvalidResearchTransition(f"unknown obligation {obligation_id!r}")
        return obligation

    def _known_node(self, node_id: str) -> bool:
        return node_id in self._obligations or node_id in self._routes

    def _endpoint_scope_and_contract(
        self, node_id: str
    ) -> tuple[MathematicalScope, str]:
        obligation = self._obligations.get(node_id)
        if obligation is not None:
            return obligation.scope, obligation.contract_id
        route = self._routes.get(node_id)
        if route is None:
            raise InvalidResearchTransition(f"unknown relation endpoint {node_id!r}")
        target = self._require_obligation(route.target_obligation_id)
        return target.scope, target.contract_id

    def _require_endpoint_alignment(self, relation: Relation) -> None:
        source_scope, source_contract = self._endpoint_scope_and_contract(relation.source)
        target_scope, target_contract = self._endpoint_scope_and_contract(relation.target)
        if relation.scope != source_scope or relation.scope != target_scope:
            raise InvalidResearchTransition(
                "relation scope must equal both endpoint scopes"
            )
        if relation.contract_id is None:
            raise InvalidResearchTransition("relation requires contract_id")
        if relation.contract_id != source_contract or relation.contract_id != target_contract:
            raise InvalidResearchTransition(
                "relation contract_id must equal both endpoint contracts"
            )
        if relation.definitions_hash is None:
            raise InvalidResearchTransition("relation requires definitions_hash")
        if (
            relation.definitions_hash != source_scope.definitions_hash
            or relation.definitions_hash != target_scope.definitions_hash
        ):
            raise InvalidResearchTransition(
                "relation definitions_hash must equal both endpoint definition hashes"
            )

    def _require_known_evidence(self, refs: object) -> None:
        if refs is None:
            return
        if isinstance(refs, str) or not isinstance(refs, (list, tuple)):
            raise InvalidResearchTransition("evidence_refs must be a list of ids")
        for item in refs:
            self._resolve_artifact(item)

    def _put_unique(self, label: str, table: dict[str, object], key: str, value: object) -> None:
        existing = table.get(key)
        if existing is None:
            table[key] = value
            return
        if existing == value:
            return
        raise InvalidResearchTransition(
            f"{label} {key!r} already exists with a different payload"
        )


def _require_payload_shape(event_type: str, payload: Mapping[str, object]) -> None:
    required, optional = _PAYLOAD_SPEC[event_type]
    keys = set(payload)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise InvalidResearchTransition(
            f"{event_type} missing fields: {sorted(missing)}"
        )
    if extra:
        raise InvalidResearchTransition(
            f"{event_type} has unknown fields: {sorted(extra)}"
        )


def _nested_record(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    if key not in payload:
        raise InvalidResearchTransition(f"missing {key} record")
    record = payload[key]
    if not isinstance(record, Mapping) or isinstance(record, (str, bytes)):
        raise InvalidResearchTransition(f"{key} must be a JSON object")
    return record


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise InvalidResearchTransition(f"{key} must be a nonempty str")
    return value
