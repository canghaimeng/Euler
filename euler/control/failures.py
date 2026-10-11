"""Scope-aware FailureRecord.

Stdlib only. Infrastructure, extraction, and budget failures never carry a
mathematical exclusion. A certified counterexample can only exclude an exactly
matching scope and must cite checked evidence. An invalid proof never refutes a
proposition. Semantic recurrence is a warning, not a confirmed dead end.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .contracts import (
    EvidenceKind,
    MathematicalScope,
    _CHECKED_COUNTEREXAMPLE_KINDS,
    _mapping,
    _optional_text,
    _reject_extra,
    _require_enum,
    _require_hex64,
    _require_hex64_tuple,
    _require_keys,
    _require_original_text,
    _require_original_tuple,
    _require_positive_int,
    _require_utc,
    _tuple_or_empty,
)
from .ids import canonical_json, stable_id
from .schema import SchemaValidationError

_FAILURE_DOMAIN = "failure"
_IDENTITY_VERSION = 1

__all__ = [
    "FailureKind",
    "ConfidenceClass",
    "FailureRecord",
]


class FailureKind(str, Enum):
    INVALID_PROOF = "INVALID_PROOF"
    CERTIFIED_COUNTEREXAMPLE = "CERTIFIED_COUNTEREXAMPLE"
    MISSING_PREMISE = "MISSING_PREMISE"
    SEMANTIC_RECURRENCE = "SEMANTIC_RECURRENCE"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    COMPUTE_INCONCLUSIVE = "COMPUTE_INCONCLUSIVE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    EXTRACTION_FAILURE = "EXTRACTION_FAILURE"
    BUDGET_EXHAUSTION = "BUDGET_EXHAUSTION"


class ConfidenceClass(str, Enum):
    CERTAIN = "CERTAIN"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


_NO_MATH_EXCLUSION = frozenset(
    {
        FailureKind.INFRASTRUCTURE_FAILURE,
        FailureKind.EXTRACTION_FAILURE,
        FailureKind.BUDGET_EXHAUSTION,
        FailureKind.INVALID_PROOF,
        FailureKind.SEMANTIC_RECURRENCE,
        FailureKind.COMPUTE_INCONCLUSIVE,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureRecord:
    """A recorded failure. Reopen conditions are stored verbatim."""

    failure_id: str
    kind: FailureKind
    exact_failed_claim: str
    scope: MathematicalScope
    confidence: ConfidenceClass
    context_version: int
    provenance: str
    created_at: str
    reopen_conditions: tuple[str, ...] = ()
    repair_conditions: tuple[str, ...] = ()
    missing_conditions: tuple[str, ...] = ()
    witness_refs: tuple[str, ...] = ()
    evidence_kind: Optional[EvidenceKind] = None
    target_obligation_id: Optional[str] = None
    route_id: Optional[str] = None
    contract_id: Optional[str] = None
    rejected_step: Optional[str] = None
    exclusion_scope: Optional[MathematicalScope] = None

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        kind: FailureKind,
        exact_failed_claim: str,
        scope: MathematicalScope,
        confidence: ConfidenceClass,
        context_version: int,
        provenance: str,
        created_at: str,
        reopen_conditions: tuple[str, ...] | list[str] = (),
        repair_conditions: tuple[str, ...] | list[str] = (),
        missing_conditions: tuple[str, ...] | list[str] = (),
        witness_refs: tuple[str, ...] | list[str] = (),
        evidence_kind: Optional[EvidenceKind] = None,
        target_obligation_id: Optional[str] = None,
        route_id: Optional[str] = None,
        contract_id: Optional[str] = None,
        rejected_step: Optional[str] = None,
        exclusion_scope: Optional[MathematicalScope] = None,
    ) -> "FailureRecord":
        kind_e = _require_enum(FailureKind, kind, "kind")
        conf_e = _require_enum(ConfidenceClass, confidence, "confidence")
        ev_kind = (
            None
            if evidence_kind is None
            else _require_enum(EvidenceKind, evidence_kind, "evidence_kind")
        )
        return cls(
            failure_id=_failure_id(
                kind=kind_e,
                exact_failed_claim=exact_failed_claim,
                scope=scope,
                confidence=conf_e,
                context_version=context_version,
                provenance=provenance,
                reopen_conditions=tuple(reopen_conditions),
                repair_conditions=tuple(repair_conditions),
                missing_conditions=tuple(missing_conditions),
                witness_refs=tuple(witness_refs),
                evidence_kind=ev_kind,
                target_obligation_id=target_obligation_id,
                route_id=route_id,
                contract_id=contract_id,
                rejected_step=rejected_step,
                exclusion_scope=exclusion_scope,
            ),
            kind=kind_e,
            exact_failed_claim=exact_failed_claim,
            scope=scope,
            confidence=conf_e,
            context_version=context_version,
            provenance=provenance,
            created_at=created_at,
            reopen_conditions=tuple(reopen_conditions),
            repair_conditions=tuple(repair_conditions),
            missing_conditions=tuple(missing_conditions),
            witness_refs=tuple(witness_refs),
            evidence_kind=ev_kind,
            target_obligation_id=target_obligation_id,
            route_id=route_id,
            contract_id=contract_id,
            rejected_step=rejected_step,
            exclusion_scope=exclusion_scope,
        )

    def validate(self) -> None:
        object.__setattr__(
            self, "kind", _require_enum(FailureKind, self.kind, "kind")
        )
        object.__setattr__(
            self,
            "exact_failed_claim",
            _require_original_text("exact_failed_claim", self.exact_failed_claim),
        )
        if not isinstance(self.scope, MathematicalScope):
            raise SchemaValidationError("scope must be a MathematicalScope")
        self.scope.validate()
        object.__setattr__(
            self,
            "confidence",
            _require_enum(ConfidenceClass, self.confidence, "confidence"),
        )
        object.__setattr__(
            self,
            "context_version",
            _require_positive_int("context_version", self.context_version),
        )
        object.__setattr__(
            self,
            "provenance",
            _require_original_text("provenance", self.provenance),
        )
        _require_utc("created_at", self.created_at)
        object.__setattr__(
            self,
            "reopen_conditions",
            _require_original_tuple("reopen_conditions", self.reopen_conditions),
        )
        object.__setattr__(
            self,
            "repair_conditions",
            _require_original_tuple("repair_conditions", self.repair_conditions),
        )
        object.__setattr__(
            self,
            "missing_conditions",
            _require_original_tuple("missing_conditions", self.missing_conditions),
        )
        object.__setattr__(
            self,
            "witness_refs",
            _require_hex64_tuple("witness_refs", self.witness_refs),
        )
        evidence_kind = (
            None
            if self.evidence_kind is None
            else _require_enum(EvidenceKind, self.evidence_kind, "evidence_kind")
        )
        object.__setattr__(self, "evidence_kind", evidence_kind)
        object.__setattr__(
            self,
            "target_obligation_id",
            _optional_hex64("target_obligation_id", self.target_obligation_id),
        )
        object.__setattr__(
            self, "route_id", _optional_hex64("route_id", self.route_id)
        )
        object.__setattr__(
            self, "contract_id", _optional_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "rejected_step", _optional_text("rejected_step", self.rejected_step)
        )
        exclusion = self.exclusion_scope
        if exclusion is not None:
            if not isinstance(exclusion, MathematicalScope):
                raise SchemaValidationError(
                    "exclusion_scope must be a MathematicalScope"
                )
            exclusion.validate()
        if self.kind in _NO_MATH_EXCLUSION and exclusion is not None:
            raise SchemaValidationError(
                f"{self.kind.value} must not carry a mathematical exclusion scope"
            )
        if (
            self.kind is FailureKind.SEMANTIC_RECURRENCE
            and self.confidence is ConfidenceClass.CERTAIN
        ):
            raise SchemaValidationError(
                "SEMANTIC_RECURRENCE is a warning and cannot be CERTAIN"
            )
        if self.kind is FailureKind.CERTIFIED_COUNTEREXAMPLE:
            if evidence_kind not in _CHECKED_COUNTEREXAMPLE_KINDS:
                raise SchemaValidationError(
                    "CERTIFIED_COUNTEREXAMPLE requires checked counterexample evidence"
                )
            if not self.witness_refs:
                raise SchemaValidationError(
                    "CERTIFIED_COUNTEREXAMPLE requires nonempty witness_refs"
                )
            if exclusion is None:
                raise SchemaValidationError(
                    "CERTIFIED_COUNTEREXAMPLE requires exclusion_scope"
                )
            if exclusion != self.scope:
                raise SchemaValidationError(
                    "CERTIFIED_COUNTEREXAMPLE exclusion_scope must exactly match claim scope"
                )
        expected = _failure_id(
            kind=self.kind,
            exact_failed_claim=self.exact_failed_claim,
            scope=self.scope,
            confidence=self.confidence,
            context_version=self.context_version,
            provenance=self.provenance,
            reopen_conditions=self.reopen_conditions,
            repair_conditions=self.repair_conditions,
            missing_conditions=self.missing_conditions,
            witness_refs=self.witness_refs,
            evidence_kind=self.evidence_kind,
            target_obligation_id=self.target_obligation_id,
            route_id=self.route_id,
            contract_id=self.contract_id,
            rejected_step=self.rejected_step,
            exclusion_scope=self.exclusion_scope,
        )
        object.__setattr__(
            self, "failure_id", _require_hex64("failure_id", self.failure_id)
        )
        if self.failure_id != expected:
            raise SchemaValidationError("failure_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "kind": self.kind.value,
            "exact_failed_claim": self.exact_failed_claim,
            "scope": self.scope.to_dict(),
            "confidence": self.confidence.value,
            "context_version": self.context_version,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "reopen_conditions": list(self.reopen_conditions),
            "repair_conditions": list(self.repair_conditions),
            "missing_conditions": list(self.missing_conditions),
            "witness_refs": list(self.witness_refs),
            "evidence_kind": None if self.evidence_kind is None else self.evidence_kind.value,
            "target_obligation_id": self.target_obligation_id,
            "route_id": self.route_id,
            "contract_id": self.contract_id,
            "rejected_step": self.rejected_step,
            "exclusion_scope": None
            if self.exclusion_scope is None
            else self.exclusion_scope.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "FailureRecord":
        payload = _mapping("FailureRecord", data)
        allowed = {
            "failure_id",
            "kind",
            "exact_failed_claim",
            "scope",
            "confidence",
            "context_version",
            "provenance",
            "created_at",
            "reopen_conditions",
            "repair_conditions",
            "missing_conditions",
            "witness_refs",
            "evidence_kind",
            "target_obligation_id",
            "route_id",
            "contract_id",
            "rejected_step",
            "exclusion_scope",
        }
        _reject_extra(payload, allowed, "FailureRecord")
        required = {
            "failure_id",
            "kind",
            "exact_failed_claim",
            "scope",
            "confidence",
            "context_version",
            "provenance",
            "created_at",
        }
        _require_keys(payload, required, "FailureRecord")
        exclusion = payload.get("exclusion_scope")
        return cls(
            failure_id=payload["failure_id"],
            kind=payload["kind"],
            exact_failed_claim=payload["exact_failed_claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            confidence=payload["confidence"],
            context_version=payload["context_version"],
            provenance=payload["provenance"],
            created_at=payload["created_at"],
            reopen_conditions=_tuple_or_empty(payload.get("reopen_conditions", ())),
            repair_conditions=_tuple_or_empty(payload.get("repair_conditions", ())),
            missing_conditions=_tuple_or_empty(payload.get("missing_conditions", ())),
            witness_refs=_tuple_or_empty(payload.get("witness_refs", ())),
            evidence_kind=payload.get("evidence_kind"),
            target_obligation_id=payload.get("target_obligation_id"),
            route_id=payload.get("route_id"),
            contract_id=payload.get("contract_id"),
            rejected_step=payload.get("rejected_step"),
            exclusion_scope=None
            if exclusion is None
            else MathematicalScope.from_dict(exclusion),
        )


def _failure_id(
    *,
    kind: FailureKind,
    exact_failed_claim: str,
    scope: MathematicalScope,
    confidence: ConfidenceClass,
    context_version: int,
    provenance: str,
    reopen_conditions: tuple[str, ...],
    repair_conditions: tuple[str, ...],
    missing_conditions: tuple[str, ...],
    witness_refs: tuple[str, ...],
    evidence_kind: Optional[EvidenceKind],
    target_obligation_id: Optional[str],
    route_id: Optional[str],
    contract_id: Optional[str],
    rejected_step: Optional[str],
    exclusion_scope: Optional[MathematicalScope],
) -> str:
    payload = {
        "kind": kind.value,
        "exact_failed_claim": exact_failed_claim,
        "scope": scope.identity_payload(),
        "confidence": confidence.value,
        "context_version": context_version,
        "provenance": provenance,
        "reopen_conditions": list(reopen_conditions),
        "repair_conditions": list(repair_conditions),
        "missing_conditions": list(missing_conditions),
        "witness_refs": list(witness_refs),
        "evidence_kind": None if evidence_kind is None else evidence_kind.value,
        "target_obligation_id": target_obligation_id,
        "route_id": route_id,
        "contract_id": contract_id,
        "rejected_step": rejected_step,
        "exclusion_scope": None
        if exclusion_scope is None
        else exclusion_scope.identity_payload(),
    }
    return stable_id(_FAILURE_DOMAIN, _IDENTITY_VERSION, canonical_json(payload))


def _optional_hex64(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_hex64(name, value)
