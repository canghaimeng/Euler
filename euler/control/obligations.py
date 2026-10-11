"""Obligation, Route, and Relation types plus pure status transitions.

Stdlib only. Content IDs are stable SHA-256 over semantic fields; status and
validity are projected and never participate in mathematical identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ValidityStatus,
)
from euler.control.ids import canonical_json, stable_id

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_OBLIGATION_DOMAIN = "obligation"
_ROUTE_DOMAIN = "route"
_RELATION_DOMAIN = "relation"
_ID_VERSION = 1

__all__ = [
    "ObligationStatus",
    "RouteStatus",
    "RelationType",
    "Obligation",
    "Route",
    "Relation",
    "transition_obligation",
    "transition_route",
]


class ObligationStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    DISCHARGED = "DISCHARGED"
    REFUTED = "REFUTED"
    PARKED = "PARKED"


class RouteStatus(str, Enum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    PARKED = "PARKED"
    COMPLETED = "COMPLETED"
    REFUTED = "REFUTED"


class RelationType(str, Enum):
    REQUIRES = "REQUIRES"
    REDUCES_TO = "REDUCES_TO"
    EQUIVALENT_UNDER = "EQUIVALENT_UNDER"
    SUSPECTED_SAME = "SUSPECTED_SAME"
    BLOCKED_BY = "BLOCKED_BY"
    REFUTED_BY = "REFUTED_BY"
    DESCENDS_BY = "DESCENDS_BY"


_TERMINAL_OBLIGATION = frozenset(
    {ObligationStatus.DISCHARGED, ObligationStatus.REFUTED}
)
_REOPENABLE_OBLIGATION = frozenset(
    {
        ObligationStatus.PARKED,
        ObligationStatus.DISCHARGED,
        ObligationStatus.REFUTED,
    }
)
_ALLOWED_OBLIGATION_TRANSITIONS = frozenset(
    {
        (ObligationStatus.OPEN, ObligationStatus.IN_PROGRESS),
        (ObligationStatus.OPEN, ObligationStatus.PARKED),
        (ObligationStatus.OPEN, ObligationStatus.DISCHARGED),
        (ObligationStatus.OPEN, ObligationStatus.REFUTED),
        (ObligationStatus.IN_PROGRESS, ObligationStatus.OPEN),
        (ObligationStatus.IN_PROGRESS, ObligationStatus.PARKED),
        (ObligationStatus.IN_PROGRESS, ObligationStatus.DISCHARGED),
        (ObligationStatus.IN_PROGRESS, ObligationStatus.REFUTED),
        (ObligationStatus.PARKED, ObligationStatus.OPEN),
        (ObligationStatus.DISCHARGED, ObligationStatus.OPEN),
        (ObligationStatus.REFUTED, ObligationStatus.OPEN),
    }
)
_TERMINAL_ROUTE = frozenset({RouteStatus.COMPLETED, RouteStatus.REFUTED})
_REOPENABLE_ROUTE = frozenset(
    {RouteStatus.PARKED, RouteStatus.COMPLETED, RouteStatus.REFUTED}
)
_ALLOWED_ROUTE_TRANSITIONS = frozenset(
    {
        (RouteStatus.PROPOSED, RouteStatus.ACTIVE),
        (RouteStatus.PROPOSED, RouteStatus.BLOCKED),
        (RouteStatus.PROPOSED, RouteStatus.PARKED),
        (RouteStatus.PROPOSED, RouteStatus.COMPLETED),
        (RouteStatus.PROPOSED, RouteStatus.REFUTED),
        (RouteStatus.ACTIVE, RouteStatus.PROPOSED),
        (RouteStatus.ACTIVE, RouteStatus.BLOCKED),
        (RouteStatus.ACTIVE, RouteStatus.PARKED),
        (RouteStatus.ACTIVE, RouteStatus.COMPLETED),
        (RouteStatus.ACTIVE, RouteStatus.REFUTED),
        (RouteStatus.BLOCKED, RouteStatus.PROPOSED),
        (RouteStatus.BLOCKED, RouteStatus.ACTIVE),
        (RouteStatus.BLOCKED, RouteStatus.PARKED),
        (RouteStatus.PARKED, RouteStatus.PROPOSED),
        (RouteStatus.PARKED, RouteStatus.ACTIVE),
        (RouteStatus.COMPLETED, RouteStatus.PROPOSED),
        (RouteStatus.REFUTED, RouteStatus.PROPOSED),
    }
)
_EVIDENCE_REQUIRED_RELATIONS = frozenset(
    {
        RelationType.REDUCES_TO,
        RelationType.EQUIVALENT_UNDER,
        RelationType.REFUTED_BY,
        RelationType.DESCENDS_BY,
    }
)


class ObligationGraphError(ValueError):
    """Malformed obligation-graph object or illegal pure transition."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Obligation:
    """A scoped research obligation. Status is projected, not identity."""

    obligation_id: str
    contract_id: str
    exact_statement: str
    scope: MathematicalScope
    desired_output: str
    created_by: str
    version: int
    status: ObligationStatus = ObligationStatus.OPEN
    evidence_refs: tuple[str, ...] = ()
    validity: ValidityStatus = ValidityStatus.VALID

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_refs", _require_hex64_tuple("evidence_refs", self.evidence_refs)
        )
        object.__setattr__(
            self, "status", _require_enum(ObligationStatus, self.status, "status")
        )
        object.__setattr__(
            self, "validity", _require_enum(ValidityStatus, self.validity, "validity")
        )
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        contract_id: str,
        exact_statement: str,
        scope: MathematicalScope,
        desired_output: str,
        created_by: str,
        version: int,
        status: ObligationStatus = ObligationStatus.OPEN,
        evidence_refs: Sequence[str] = (),
        validity: ValidityStatus = ValidityStatus.VALID,
    ) -> "Obligation":
        identity = _obligation_identity(
            contract_id=contract_id,
            exact_statement=exact_statement,
            scope=scope,
            desired_output=desired_output,
            version=version,
        )
        return cls(
            obligation_id=_content_id(_OBLIGATION_DOMAIN, identity),
            contract_id=contract_id,
            exact_statement=exact_statement,
            scope=scope,
            desired_output=desired_output,
            created_by=created_by,
            version=version,
            status=status,
            evidence_refs=tuple(evidence_refs),
            validity=validity,
        )

    def validate(self) -> None:
        _require_hex64("obligation_id", self.obligation_id)
        _require_hex64("contract_id", self.contract_id)
        _require_nonempty_str("exact_statement", self.exact_statement)
        if not isinstance(self.scope, MathematicalScope):
            raise ObligationGraphError("scope must be a MathematicalScope")
        _require_nonempty_str("desired_output", self.desired_output)
        _require_nonempty_str("created_by", self.created_by)
        _require_positive_int("version", self.version)
        expected = _content_id(
            _OBLIGATION_DOMAIN,
            _obligation_identity(
                contract_id=self.contract_id,
                exact_statement=self.exact_statement,
                scope=self.scope,
                desired_output=self.desired_output,
                version=self.version,
            ),
        )
        if self.obligation_id != expected:
            raise ObligationGraphError("obligation_id does not match identity fields")
        if self.status in _TERMINAL_OBLIGATION and not self.evidence_refs:
            raise ObligationGraphError(
                f"{self.status.value} obligation requires evidence refs"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "exact_statement": self.exact_statement,
            "scope": self.scope.to_dict(),
            "desired_output": self.desired_output,
            "created_by": self.created_by,
            "version": self.version,
            "status": self.status.value,
            "evidence_refs": list(self.evidence_refs),
            "validity": self.validity.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "Obligation":
        payload = _mapping("Obligation", data)
        allowed = {
            "obligation_id",
            "contract_id",
            "exact_statement",
            "scope",
            "desired_output",
            "created_by",
            "version",
            "status",
            "evidence_refs",
            "validity",
        }
        _reject_extra(payload, allowed, "Obligation")
        required = allowed - {"status", "evidence_refs", "validity"}
        _require_keys(payload, required, "Obligation")
        return cls(
            obligation_id=payload["obligation_id"],
            contract_id=payload["contract_id"],
            exact_statement=payload["exact_statement"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            desired_output=payload["desired_output"],
            created_by=payload["created_by"],
            version=payload["version"],
            status=payload.get("status", ObligationStatus.OPEN),
            evidence_refs=payload.get("evidence_refs", ()),
            validity=payload.get("validity", ValidityStatus.VALID),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Route:
    """A research route. Required obligations are AND; alternative_group is OR."""

    route_id: str
    target_obligation_id: str
    mechanism: str
    required_obligation_ids: tuple[str, ...]
    alternative_group: Optional[str] = None
    dependencies: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    progress_claim: str = ""
    reopen_conditions: str = ""
    status: RouteStatus = RouteStatus.PROPOSED
    validity: ValidityStatus = ValidityStatus.VALID

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "required_obligation_ids",
            _require_hex64_tuple("required_obligation_ids", self.required_obligation_ids),
        )
        object.__setattr__(
            self, "dependencies", _require_hex64_tuple("dependencies", self.dependencies)
        )
        object.__setattr__(
            self, "evidence_refs", _require_hex64_tuple("evidence_refs", self.evidence_refs)
        )
        object.__setattr__(
            self, "status", _require_enum(RouteStatus, self.status, "status")
        )
        object.__setattr__(
            self, "validity", _require_enum(ValidityStatus, self.validity, "validity")
        )
        if self.alternative_group is not None:
            object.__setattr__(
                self,
                "alternative_group",
                _require_nonempty_str("alternative_group", self.alternative_group),
            )
        if not isinstance(self.progress_claim, str):
            raise ObligationGraphError("progress_claim must be a str")
        if not isinstance(self.reopen_conditions, str):
            raise ObligationGraphError("reopen_conditions must be a str")
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        target_obligation_id: str,
        mechanism: str,
        required_obligation_ids: Sequence[str],
        alternative_group: str | None = None,
        dependencies: Sequence[str] = (),
        evidence_refs: Sequence[str] = (),
        progress_claim: str = "",
        reopen_conditions: str = "",
        status: RouteStatus = RouteStatus.PROPOSED,
        validity: ValidityStatus = ValidityStatus.VALID,
    ) -> "Route":
        required = tuple(required_obligation_ids)
        deps = tuple(dependencies)
        identity = _route_identity(
            target_obligation_id=target_obligation_id,
            mechanism=mechanism,
            required_obligation_ids=required,
            alternative_group=alternative_group,
            dependencies=deps,
            reopen_conditions=reopen_conditions,
        )
        return cls(
            route_id=_content_id(_ROUTE_DOMAIN, identity),
            target_obligation_id=target_obligation_id,
            mechanism=mechanism,
            required_obligation_ids=required,
            alternative_group=alternative_group,
            dependencies=deps,
            evidence_refs=tuple(evidence_refs),
            progress_claim=progress_claim,
            reopen_conditions=reopen_conditions,
            status=status,
            validity=validity,
        )

    def validate(self) -> None:
        _require_hex64("route_id", self.route_id)
        _require_hex64("target_obligation_id", self.target_obligation_id)
        _require_nonempty_str("mechanism", self.mechanism)
        expected = _content_id(
            _ROUTE_DOMAIN,
            _route_identity(
                target_obligation_id=self.target_obligation_id,
                mechanism=self.mechanism,
                required_obligation_ids=self.required_obligation_ids,
                alternative_group=self.alternative_group,
                dependencies=self.dependencies,
                reopen_conditions=self.reopen_conditions,
            ),
        )
        if self.route_id != expected:
            raise ObligationGraphError("route_id does not match identity fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "target_obligation_id": self.target_obligation_id,
            "mechanism": self.mechanism,
            "required_obligation_ids": list(self.required_obligation_ids),
            "alternative_group": self.alternative_group,
            "dependencies": list(self.dependencies),
            "evidence_refs": list(self.evidence_refs),
            "progress_claim": self.progress_claim,
            "reopen_conditions": self.reopen_conditions,
            "status": self.status.value,
            "validity": self.validity.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "Route":
        payload = _mapping("Route", data)
        allowed = {
            "route_id",
            "target_obligation_id",
            "mechanism",
            "required_obligation_ids",
            "alternative_group",
            "dependencies",
            "evidence_refs",
            "progress_claim",
            "reopen_conditions",
            "status",
            "validity",
        }
        _reject_extra(payload, allowed, "Route")
        required = {
            "route_id",
            "target_obligation_id",
            "mechanism",
            "required_obligation_ids",
        }
        _require_keys(payload, required, "Route")
        return cls(
            route_id=payload["route_id"],
            target_obligation_id=payload["target_obligation_id"],
            mechanism=payload["mechanism"],
            required_obligation_ids=payload["required_obligation_ids"],
            alternative_group=payload.get("alternative_group"),
            dependencies=payload.get("dependencies", ()),
            evidence_refs=payload.get("evidence_refs", ()),
            progress_claim=payload.get("progress_claim", ""),
            reopen_conditions=payload.get("reopen_conditions", ""),
            status=payload.get("status", RouteStatus.PROPOSED),
            validity=payload.get("validity", ValidityStatus.VALID),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Relation:
    """A scoped graph edge. SUSPECTED_SAME cannot merge or discharge."""

    relation_id: str
    relation_type: RelationType
    source: str
    target: str
    exact_claim: str
    scope: MathematicalScope
    assumptions: tuple[str, ...]
    evidence_kind: Optional[EvidenceKind] = None
    evidence_refs: tuple[str, ...] = ()
    reviewer: str = ""
    contract_id: Optional[str] = None
    definitions_hash: Optional[str] = None
    validity: ValidityStatus = ValidityStatus.VALID
    well_founded_measure: Optional[str] = None
    side_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relation_type",
            _require_enum(RelationType, self.relation_type, "relation_type"),
        )
        object.__setattr__(
            self, "assumptions", _require_str_tuple("assumptions", self.assumptions)
        )
        object.__setattr__(
            self, "evidence_refs", _require_hex64_tuple("evidence_refs", self.evidence_refs)
        )
        object.__setattr__(
            self,
            "side_conditions",
            _require_str_tuple("side_conditions", self.side_conditions),
        )
        object.__setattr__(
            self, "validity", _require_enum(ValidityStatus, self.validity, "validity")
        )
        if self.evidence_kind is not None:
            object.__setattr__(
                self,
                "evidence_kind",
                _require_enum(EvidenceKind, self.evidence_kind, "evidence_kind"),
            )
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        relation_type: RelationType,
        source: str,
        target: str,
        exact_claim: str,
        scope: MathematicalScope,
        assumptions: Sequence[str] = (),
        evidence_kind: EvidenceKind | None = None,
        evidence_refs: Sequence[str] = (),
        reviewer: str = "",
        contract_id: str | None = None,
        definitions_hash: str | None = None,
        validity: ValidityStatus = ValidityStatus.VALID,
        well_founded_measure: str | None = None,
        side_conditions: Sequence[str] = (),
    ) -> "Relation":
        assumptions_t = tuple(assumptions)
        refs = tuple(evidence_refs)
        sides = tuple(side_conditions)
        identity = _relation_identity(
            relation_type=relation_type,
            source=source,
            target=target,
            exact_claim=exact_claim,
            scope=scope,
            assumptions=assumptions_t,
            evidence_kind=evidence_kind,
            evidence_refs=refs,
            reviewer=reviewer,
            contract_id=contract_id,
            definitions_hash=definitions_hash,
            well_founded_measure=well_founded_measure,
            side_conditions=sides,
        )
        return cls(
            relation_id=_content_id(_RELATION_DOMAIN, identity),
            relation_type=relation_type,
            source=source,
            target=target,
            exact_claim=exact_claim,
            scope=scope,
            assumptions=assumptions_t,
            evidence_kind=evidence_kind,
            evidence_refs=refs,
            reviewer=reviewer,
            contract_id=contract_id,
            definitions_hash=definitions_hash,
            validity=validity,
            well_founded_measure=well_founded_measure,
            side_conditions=sides,
        )

    def validate(self) -> None:
        _require_hex64("relation_id", self.relation_id)
        _require_hex64("source", self.source)
        _require_hex64("target", self.target)
        _require_nonempty_str("exact_claim", self.exact_claim)
        if not isinstance(self.scope, MathematicalScope):
            raise ObligationGraphError("scope must be a MathematicalScope")
        if self.reviewer != "":
            _require_nonempty_str("reviewer", self.reviewer)
        if self.contract_id is not None:
            _require_hex64("contract_id", self.contract_id)
        if self.definitions_hash is not None:
            _require_hex64("definitions_hash", self.definitions_hash)
        if self.relation_type in _EVIDENCE_REQUIRED_RELATIONS:
            if not self.evidence_refs:
                raise ObligationGraphError(
                    f"{self.relation_type.value} requires evidence refs"
                )
            if self.evidence_kind is None:
                raise ObligationGraphError(
                    f"{self.relation_type.value} requires evidence kind"
                )
            _require_nonempty_str("reviewer", self.reviewer)
            if self.contract_id is None:
                raise ObligationGraphError(
                    f"{self.relation_type.value} requires contract_id"
                )
            if self.definitions_hash is None:
                raise ObligationGraphError(
                    f"{self.relation_type.value} requires definitions_hash"
                )
        if self.relation_type is RelationType.DESCENDS_BY:
            if not isinstance(self.well_founded_measure, str) or self.well_founded_measure == "":
                raise ObligationGraphError(
                    "DESCENDS_BY requires a nonempty well-founded measure"
                )
            if not self.side_conditions:
                raise ObligationGraphError(
                    "DESCENDS_BY requires nonempty side conditions"
                )
        elif self.well_founded_measure is not None:
            raise ObligationGraphError(
                f"{self.relation_type.value} forbids well_founded_measure"
            )
        expected = _content_id(
            _RELATION_DOMAIN,
            _relation_identity(
                relation_type=self.relation_type,
                source=self.source,
                target=self.target,
                exact_claim=self.exact_claim,
                scope=self.scope,
                assumptions=self.assumptions,
                evidence_kind=self.evidence_kind,
                evidence_refs=self.evidence_refs,
                reviewer=self.reviewer,
                contract_id=self.contract_id,
                definitions_hash=self.definitions_hash,
                well_founded_measure=self.well_founded_measure,
                side_conditions=self.side_conditions,
            ),
        )
        if self.relation_id != expected:
            raise ObligationGraphError("relation_id does not match identity fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "relation_type": self.relation_type.value,
            "source": self.source,
            "target": self.target,
            "exact_claim": self.exact_claim,
            "scope": self.scope.to_dict(),
            "assumptions": list(self.assumptions),
            "evidence_kind": None if self.evidence_kind is None else self.evidence_kind.value,
            "evidence_refs": list(self.evidence_refs),
            "reviewer": self.reviewer,
            "contract_id": self.contract_id,
            "definitions_hash": self.definitions_hash,
            "validity": self.validity.value,
            "well_founded_measure": self.well_founded_measure,
            "side_conditions": list(self.side_conditions),
        }

    @classmethod
    def from_dict(cls, data: object) -> "Relation":
        payload = _mapping("Relation", data)
        allowed = {
            "relation_id",
            "relation_type",
            "source",
            "target",
            "exact_claim",
            "scope",
            "assumptions",
            "evidence_kind",
            "evidence_refs",
            "reviewer",
            "contract_id",
            "definitions_hash",
            "validity",
            "well_founded_measure",
            "side_conditions",
        }
        _reject_extra(payload, allowed, "Relation")
        required = {
            "relation_id",
            "relation_type",
            "source",
            "target",
            "exact_claim",
            "scope",
            "assumptions",
        }
        _require_keys(payload, required, "Relation")
        return cls(
            relation_id=payload["relation_id"],
            relation_type=payload["relation_type"],
            source=payload["source"],
            target=payload["target"],
            exact_claim=payload["exact_claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            assumptions=payload["assumptions"],
            evidence_kind=payload.get("evidence_kind"),
            evidence_refs=payload.get("evidence_refs", ()),
            reviewer=payload.get("reviewer", ""),
            contract_id=payload.get("contract_id"),
            definitions_hash=payload.get("definitions_hash"),
            validity=payload.get("validity", ValidityStatus.VALID),
            well_founded_measure=payload.get("well_founded_measure"),
            side_conditions=payload.get("side_conditions", ()),
        )


def transition_obligation(
    obligation: Obligation,
    new_status: ObligationStatus,
    *,
    evidence_refs: Sequence[str] | None = None,
    reason: str | None = None,
    new_premises: object = None,
    new_tool: object = None,
) -> Obligation:
    """Return a copy with projected status updated. Identity is unchanged."""
    if not isinstance(obligation, Obligation):
        raise ObligationGraphError("transition_obligation requires an Obligation")
    new_status = _require_enum(ObligationStatus, new_status, "new_status")
    if obligation.status is new_status:
        if evidence_refs is None:
            return obligation
        refs = _require_hex64_tuple("evidence_refs", evidence_refs)
        if refs == obligation.evidence_refs:
            return obligation
        raise ObligationGraphError(
            "identical obligation status cannot carry different evidence refs"
        )
    pair = (obligation.status, new_status)
    if pair not in _ALLOWED_OBLIGATION_TRANSITIONS:
        raise ObligationGraphError(
            f"illegal obligation transition {obligation.status.value} -> "
            f"{new_status.value}"
        )
    refs = (
        obligation.evidence_refs
        if evidence_refs is None
        else _require_hex64_tuple("evidence_refs", evidence_refs)
    )
    if pair[0] in _REOPENABLE_OBLIGATION and new_status is ObligationStatus.OPEN:
        _require_reopen_reason(reason)
        if not _has_reopen_input(evidence_refs, new_premises, new_tool):
            raise ObligationGraphError(
                "reopen requires nonempty evidence_refs, new_premises, or new_tool"
            )
        new_refs = (
            ()
            if evidence_refs is None
            else _require_hex64_tuple("evidence_refs", evidence_refs)
        )
        return replace(
            obligation,
            status=ObligationStatus.OPEN,
            evidence_refs=new_refs,
            validity=ValidityStatus.VALID,
        )
    if new_status in _TERMINAL_OBLIGATION and not refs:
        raise ObligationGraphError(
            f"{new_status.value} obligation requires evidence refs"
        )
    return replace(obligation, status=new_status, evidence_refs=refs)


def transition_route(
    route: Route,
    new_status: RouteStatus,
    *,
    evidence_refs: Sequence[str] | None = None,
    reason: str | None = None,
) -> Route:
    """Return a copy with projected route status updated. Identity is unchanged."""
    if not isinstance(route, Route):
        raise ObligationGraphError("transition_route requires a Route")
    new_status = _require_enum(RouteStatus, new_status, "new_status")
    if new_status is route.status:
        return route
    pair = (route.status, new_status)
    if pair not in _ALLOWED_ROUTE_TRANSITIONS:
        raise ObligationGraphError(
            f"illegal route transition {route.status.value} -> {new_status.value}"
        )
    refs = (
        route.evidence_refs
        if evidence_refs is None
        else _require_hex64_tuple("evidence_refs", evidence_refs)
    )
    if pair[0] in _REOPENABLE_ROUTE and new_status in {
        RouteStatus.PROPOSED,
        RouteStatus.ACTIVE,
    }:
        _require_reopen_reason(reason)
    if new_status in _TERMINAL_ROUTE and not refs:
        raise ObligationGraphError(
            f"{new_status.value} route requires nonempty evidence refs"
        )
    return replace(route, status=new_status, evidence_refs=refs)


def _obligation_identity(
    *,
    contract_id: str,
    exact_statement: str,
    scope: MathematicalScope,
    desired_output: str,
    version: int,
) -> dict[str, Any]:
    return {
        "contract_id": contract_id,
        "desired_output": desired_output,
        "exact_statement": exact_statement,
        "scope": scope.to_dict(),
        "version": version,
    }


def _route_identity(
    *,
    target_obligation_id: str,
    mechanism: str,
    required_obligation_ids: tuple[str, ...],
    alternative_group: str | None,
    dependencies: tuple[str, ...],
    reopen_conditions: str,
) -> dict[str, Any]:
    return {
        "alternative_group": alternative_group,
        "dependencies": list(dependencies),
        "mechanism": mechanism,
        "reopen_conditions": reopen_conditions,
        "required_obligation_ids": list(required_obligation_ids),
        "target_obligation_id": target_obligation_id,
    }


def _relation_identity(
    *,
    relation_type: RelationType,
    source: str,
    target: str,
    exact_claim: str,
    scope: MathematicalScope,
    assumptions: tuple[str, ...],
    evidence_kind: EvidenceKind | None,
    evidence_refs: tuple[str, ...],
    reviewer: str,
    contract_id: str | None,
    definitions_hash: str | None,
    well_founded_measure: str | None,
    side_conditions: tuple[str, ...],
) -> dict[str, Any]:
    kind = relation_type.value if isinstance(relation_type, RelationType) else str(relation_type)
    evidence_kind_value = (
        None if evidence_kind is None else getattr(evidence_kind, "value", evidence_kind)
    )
    return {
        "assumptions": list(assumptions),
        "contract_id": contract_id,
        "definitions_hash": definitions_hash,
        "evidence_kind": evidence_kind_value,
        "evidence_refs": list(evidence_refs),
        "exact_claim": exact_claim,
        "relation_type": kind,
        "reviewer": reviewer,
        "scope": scope.to_dict(),
        "side_conditions": list(side_conditions),
        "source": source,
        "target": target,
        "well_founded_measure": well_founded_measure,
    }


def _content_id(domain: str, identity: Mapping[str, Any]) -> str:
    return stable_id(domain, _ID_VERSION, canonical_json(identity))


def _require_reopen_reason(reason: object) -> str:
    if not isinstance(reason, str) or reason.strip() == "":
        raise ObligationGraphError(
            "reopen requires a nonempty new evidence/premise/tool reason"
        )
    return reason


def _has_reopen_input(
    evidence_refs: object, new_premises: object, new_tool: object
) -> bool:
    if evidence_refs is not None:
        if isinstance(evidence_refs, str) or not isinstance(evidence_refs, (list, tuple)):
            raise ObligationGraphError("evidence_refs must be a list of 64-hex ids")
        if len(evidence_refs) > 0:
            return True
    if _nonempty_reopen_text(new_premises) or _nonempty_reopen_text(new_tool):
        return True
    return False


def _nonempty_reopen_text(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple)):
        return any(isinstance(item, str) and item.strip() != "" for item in value)
    return False


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise ObligationGraphError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise ObligationGraphError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise ObligationGraphError(f"{label} missing fields: {sorted(missing)}")


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ObligationGraphError(f"{name} must be a nonempty str")
    return value


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty_str(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise ObligationGraphError(f"{name} must be 64-char lowercase hex")
    return text


def _require_hex64_tuple(name: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ObligationGraphError(f"{name} must be a list of 64-hex ids")
    return tuple(_require_hex64(f"{name}[{i}]", item) for i, item in enumerate(value))


def _require_str_tuple(name: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ObligationGraphError(f"{name} must be a list of str")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ObligationGraphError(f"{name}[{i}] must be a str")
        out.append(item)
    return tuple(out)


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ObligationGraphError(f"{name} must be a positive int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise ObligationGraphError(f"{name} has unknown value {value!r}") from None
    raise ObligationGraphError(f"{name} must be {enum_cls.__name__}")
