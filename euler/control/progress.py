"""Action identity and progress receipts for conservative recurrence.

Stdlib only. Sampling nonce is excluded from base action identity. Mathematical
progress requires registered VALID+PASS artifacts; representation gain is never
verified mathematical progress. Validation never mutates ResearchView.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from euler.control.contracts import (
    CheckerResult,
    EvidenceKind,
    MathematicalScope,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.ids import canonical_json, stable_id
from euler.control.obligations import ObligationStatus, RelationType
from euler.control.schema import SchemaValidationError
from euler.control.views import ResearchView

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UTC = timezone.utc
_ZERO = timedelta(0)
_ACTION_DOMAIN = "action"
_PROGRESS_DOMAIN = "progress"
_ID_VERSION = 1

__all__ = [
    "ProgressKind",
    "SamplingMode",
    "ActionSpec",
    "ProgressReceipt",
    "validate_progress_against_view",
]


class ProgressKind(str, Enum):
    OBLIGATION_DISCHARGED = "OBLIGATION_DISCHARGED"
    REDUCTION_VERIFIED = "REDUCTION_VERIFIED"
    COUNTEREXAMPLE_CHECKED = "COUNTEREXAMPLE_CHECKED"
    REFERENCE_VALIDATED = "REFERENCE_VALIDATED"
    GAP_REPAIRED = "GAP_REPAIRED"
    REPRESENTATION_GAIN = "REPRESENTATION_GAIN"
    NO_VERIFIABLE_PROGRESS = "NO_VERIFIABLE_PROGRESS"


class SamplingMode(str, Enum):
    DEFAULT = "DEFAULT"
    EXPLICIT_NEW = "EXPLICIT_NEW"


_MATHEMATICAL_KINDS = frozenset(
    {
        ProgressKind.OBLIGATION_DISCHARGED,
        ProgressKind.REDUCTION_VERIFIED,
        ProgressKind.COUNTEREXAMPLE_CHECKED,
        ProgressKind.REFERENCE_VALIDATED,
        ProgressKind.GAP_REPAIRED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionSpec:
    """Exact action identity. ``sampling_nonce`` is not part of ``action_id``."""

    action_id: str
    action_type: str
    mechanism: str
    model_id: str
    effort: str
    task_mode: str
    context_hash: str
    dependency_hash: str
    definitions_hash: str
    tool_environment_hash: str
    parameters_json: str
    obligation_id: Optional[str] = None
    route_id: Optional[str] = None
    sampling_nonce: Optional[str] = None
    sampling_mode: SamplingMode = SamplingMode.DEFAULT

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        action_type: str,
        mechanism: str,
        model_id: str,
        effort: str,
        task_mode: str,
        context_hash: str,
        dependency_hash: str,
        definitions_hash: str,
        tool_environment_hash: str,
        parameters_json: str,
        obligation_id: Optional[str] = None,
        route_id: Optional[str] = None,
        sampling_nonce: Optional[str] = None,
        sampling_mode: SamplingMode = SamplingMode.DEFAULT,
    ) -> "ActionSpec":
        mode = _require_enum(SamplingMode, sampling_mode, "sampling_mode")
        return cls(
            action_id=_action_id(
                action_type=action_type,
                mechanism=mechanism,
                model_id=model_id,
                effort=effort,
                task_mode=task_mode,
                context_hash=context_hash,
                dependency_hash=dependency_hash,
                definitions_hash=definitions_hash,
                tool_environment_hash=tool_environment_hash,
                parameters_json=parameters_json,
                obligation_id=obligation_id,
                route_id=route_id,
            ),
            action_type=action_type,
            mechanism=mechanism,
            model_id=model_id,
            effort=effort,
            task_mode=task_mode,
            context_hash=context_hash,
            dependency_hash=dependency_hash,
            definitions_hash=definitions_hash,
            tool_environment_hash=tool_environment_hash,
            parameters_json=parameters_json,
            obligation_id=obligation_id,
            route_id=route_id,
            sampling_nonce=sampling_nonce,
            sampling_mode=mode,
        )

    def validate(self) -> None:
        object.__setattr__(
            self, "action_type", _require_safe("action_type", self.action_type)
        )
        object.__setattr__(
            self, "mechanism", _require_nonempty("mechanism", self.mechanism)
        )
        object.__setattr__(
            self, "model_id", _require_nonempty("model_id", self.model_id)
        )
        object.__setattr__(self, "effort", _require_nonempty("effort", self.effort))
        object.__setattr__(
            self, "task_mode", _require_safe("task_mode", self.task_mode)
        )
        object.__setattr__(
            self, "context_hash", _require_hex64("context_hash", self.context_hash)
        )
        object.__setattr__(
            self,
            "dependency_hash",
            _require_hex64("dependency_hash", self.dependency_hash),
        )
        object.__setattr__(
            self,
            "definitions_hash",
            _require_hex64("definitions_hash", self.definitions_hash),
        )
        object.__setattr__(
            self,
            "tool_environment_hash",
            _require_hex64("tool_environment_hash", self.tool_environment_hash),
        )
        object.__setattr__(
            self,
            "parameters_json",
            _require_canonical_object_json("parameters_json", self.parameters_json),
        )
        object.__setattr__(
            self, "obligation_id", _optional_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(self, "route_id", _optional_hex64("route_id", self.route_id))
        object.__setattr__(
            self,
            "sampling_mode",
            _require_enum(SamplingMode, self.sampling_mode, "sampling_mode"),
        )
        nonce = _optional_text("sampling_nonce", self.sampling_nonce)
        if self.sampling_mode is SamplingMode.DEFAULT and nonce is not None:
            raise SchemaValidationError("DEFAULT sampling forbids sampling_nonce")
        if self.sampling_mode is SamplingMode.EXPLICIT_NEW and nonce is None:
            raise SchemaValidationError(
                "EXPLICIT_NEW sampling requires a nonempty sampling_nonce"
            )
        object.__setattr__(self, "sampling_nonce", nonce)
        expected = _action_id(
            action_type=self.action_type,
            mechanism=self.mechanism,
            model_id=self.model_id,
            effort=self.effort,
            task_mode=self.task_mode,
            context_hash=self.context_hash,
            dependency_hash=self.dependency_hash,
            definitions_hash=self.definitions_hash,
            tool_environment_hash=self.tool_environment_hash,
            parameters_json=self.parameters_json,
            obligation_id=self.obligation_id,
            route_id=self.route_id,
        )
        object.__setattr__(self, "action_id", _require_hex64("action_id", self.action_id))
        if self.action_id != expected:
            raise SchemaValidationError("action_id does not match semantic fields")

    def sample_key(self) -> tuple[str, str]:
        return (self.action_id, self.sampling_nonce or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "mechanism": self.mechanism,
            "model_id": self.model_id,
            "effort": self.effort,
            "task_mode": self.task_mode,
            "context_hash": self.context_hash,
            "dependency_hash": self.dependency_hash,
            "definitions_hash": self.definitions_hash,
            "tool_environment_hash": self.tool_environment_hash,
            "parameters_json": self.parameters_json,
            "obligation_id": self.obligation_id,
            "route_id": self.route_id,
            "sampling_nonce": self.sampling_nonce,
            "sampling_mode": self.sampling_mode.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ActionSpec":
        payload = _mapping("ActionSpec", data)
        allowed = {
            "action_id",
            "action_type",
            "mechanism",
            "model_id",
            "effort",
            "task_mode",
            "context_hash",
            "dependency_hash",
            "definitions_hash",
            "tool_environment_hash",
            "parameters_json",
            "obligation_id",
            "route_id",
            "sampling_nonce",
            "sampling_mode",
        }
        _reject_extra(payload, allowed, "ActionSpec")
        required = allowed - {
            "obligation_id",
            "route_id",
            "sampling_nonce",
            "sampling_mode",
        }
        _require_keys(payload, required, "ActionSpec")
        return cls(
            action_id=payload["action_id"],
            action_type=payload["action_type"],
            mechanism=payload["mechanism"],
            model_id=payload["model_id"],
            effort=payload["effort"],
            task_mode=payload["task_mode"],
            context_hash=payload["context_hash"],
            dependency_hash=payload["dependency_hash"],
            definitions_hash=payload["definitions_hash"],
            tool_environment_hash=payload["tool_environment_hash"],
            parameters_json=payload["parameters_json"],
            obligation_id=payload.get("obligation_id"),
            route_id=payload.get("route_id"),
            sampling_nonce=payload.get("sampling_nonce"),
            sampling_mode=payload.get("sampling_mode", SamplingMode.DEFAULT),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProgressReceipt:
    receipt_id: str
    action_id: str
    attempt_id: str
    contract_id: str
    kind: ProgressKind
    claim: str
    scope: MathematicalScope
    old_frontier_hash: str
    new_frontier_hash: str
    created_at: str
    new_evidence_refs: tuple[str, ...] = ()
    affected_obligation_ids: tuple[str, ...] = ()
    affected_route_ids: tuple[str, ...] = ()
    next_action: Optional[str] = None
    old_tool_environment_hash: Optional[str] = None
    new_tool_environment_hash: Optional[str] = None

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        action_id: str,
        attempt_id: str,
        contract_id: str,
        kind: ProgressKind,
        claim: str,
        scope: MathematicalScope,
        old_frontier_hash: str,
        new_frontier_hash: str,
        created_at: str,
        new_evidence_refs: tuple[str, ...] | list[str] = (),
        affected_obligation_ids: tuple[str, ...] | list[str] = (),
        affected_route_ids: tuple[str, ...] | list[str] = (),
        next_action: Optional[str] = None,
        old_tool_environment_hash: Optional[str] = None,
        new_tool_environment_hash: Optional[str] = None,
    ) -> "ProgressReceipt":
        kind_e = _require_enum(ProgressKind, kind, "kind")
        return cls(
            receipt_id=_progress_id(
                action_id=action_id,
                attempt_id=attempt_id,
                contract_id=contract_id,
                kind=kind_e,
                claim=claim,
                scope=scope,
                old_frontier_hash=old_frontier_hash,
                new_frontier_hash=new_frontier_hash,
                new_evidence_refs=tuple(new_evidence_refs),
                affected_obligation_ids=tuple(affected_obligation_ids),
                affected_route_ids=tuple(affected_route_ids),
                next_action=next_action,
                old_tool_environment_hash=old_tool_environment_hash,
                new_tool_environment_hash=new_tool_environment_hash,
            ),
            action_id=action_id,
            attempt_id=attempt_id,
            contract_id=contract_id,
            kind=kind_e,
            claim=claim,
            scope=scope,
            old_frontier_hash=old_frontier_hash,
            new_frontier_hash=new_frontier_hash,
            created_at=created_at,
            new_evidence_refs=tuple(new_evidence_refs),
            affected_obligation_ids=tuple(affected_obligation_ids),
            affected_route_ids=tuple(affected_route_ids),
            next_action=next_action,
            old_tool_environment_hash=old_tool_environment_hash,
            new_tool_environment_hash=new_tool_environment_hash,
        )

    def validate(self) -> None:
        object.__setattr__(self, "action_id", _require_hex64("action_id", self.action_id))
        object.__setattr__(
            self, "attempt_id", _require_hex64("attempt_id", self.attempt_id)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(self, "kind", _require_enum(ProgressKind, self.kind, "kind"))
        object.__setattr__(self, "claim", _require_nonempty("claim", self.claim))
        if not isinstance(self.scope, MathematicalScope):
            raise SchemaValidationError("scope must be a MathematicalScope")
        object.__setattr__(
            self,
            "old_frontier_hash",
            _require_hex64("old_frontier_hash", self.old_frontier_hash),
        )
        object.__setattr__(
            self,
            "new_frontier_hash",
            _require_hex64("new_frontier_hash", self.new_frontier_hash),
        )
        _require_utc("created_at", self.created_at)
        object.__setattr__(
            self,
            "new_evidence_refs",
            _require_hex64_tuple("new_evidence_refs", self.new_evidence_refs),
        )
        object.__setattr__(
            self,
            "affected_obligation_ids",
            _require_hex64_tuple("affected_obligation_ids", self.affected_obligation_ids),
        )
        object.__setattr__(
            self,
            "affected_route_ids",
            _require_hex64_tuple("affected_route_ids", self.affected_route_ids),
        )
        object.__setattr__(
            self, "next_action", _optional_text("next_action", self.next_action)
        )
        object.__setattr__(
            self,
            "old_tool_environment_hash",
            _optional_hex64("old_tool_environment_hash", self.old_tool_environment_hash),
        )
        object.__setattr__(
            self,
            "new_tool_environment_hash",
            _optional_hex64("new_tool_environment_hash", self.new_tool_environment_hash),
        )
        if self.kind in _MATHEMATICAL_KINDS:
            if not self.new_evidence_refs:
                raise SchemaValidationError(
                    f"{self.kind.value} requires nonempty artifact-ID evidence refs"
                )
            if self.old_frontier_hash == self.new_frontier_hash:
                raise SchemaValidationError(
                    f"{self.kind.value} requires a changed frontier hash"
                )
        if self.kind is ProgressKind.NO_VERIFIABLE_PROGRESS and self.new_evidence_refs:
            raise SchemaValidationError(
                "NO_VERIFIABLE_PROGRESS cannot claim evidence"
            )
        if self.kind is ProgressKind.REPRESENTATION_GAIN:
            if not self.next_action:
                raise SchemaValidationError(
                    "REPRESENTATION_GAIN requires a nonempty executable next_action"
                )
            if (
                self.old_tool_environment_hash is None
                or self.new_tool_environment_hash is None
                or self.old_tool_environment_hash == self.new_tool_environment_hash
            ):
                raise SchemaValidationError(
                    "REPRESENTATION_GAIN requires changed old/new tool hashes"
                )
        if self.kind is ProgressKind.OBLIGATION_DISCHARGED and not self.affected_obligation_ids:
            raise SchemaValidationError(
                "OBLIGATION_DISCHARGED requires affected obligation IDs"
            )
        if self.kind is ProgressKind.COUNTEREXAMPLE_CHECKED and not self.affected_obligation_ids:
            raise SchemaValidationError(
                "COUNTEREXAMPLE_CHECKED requires affected obligation IDs"
            )
        if self.kind is ProgressKind.GAP_REPAIRED and not (
            self.affected_obligation_ids or self.affected_route_ids
        ):
            raise SchemaValidationError(
                "GAP_REPAIRED requires at least one affected obligation or route"
            )
        expected = _progress_id(
            action_id=self.action_id,
            attempt_id=self.attempt_id,
            contract_id=self.contract_id,
            kind=self.kind,
            claim=self.claim,
            scope=self.scope,
            old_frontier_hash=self.old_frontier_hash,
            new_frontier_hash=self.new_frontier_hash,
            new_evidence_refs=self.new_evidence_refs,
            affected_obligation_ids=self.affected_obligation_ids,
            affected_route_ids=self.affected_route_ids,
            next_action=self.next_action,
            old_tool_environment_hash=self.old_tool_environment_hash,
            new_tool_environment_hash=self.new_tool_environment_hash,
        )
        object.__setattr__(
            self, "receipt_id", _require_hex64("receipt_id", self.receipt_id)
        )
        if self.receipt_id != expected:
            raise SchemaValidationError("receipt_id does not match semantic fields")

    def is_mathematical(self) -> bool:
        return self.kind in _MATHEMATICAL_KINDS

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "contract_id": self.contract_id,
            "kind": self.kind.value,
            "claim": self.claim,
            "scope": self.scope.to_dict(),
            "old_frontier_hash": self.old_frontier_hash,
            "new_frontier_hash": self.new_frontier_hash,
            "created_at": self.created_at,
            "new_evidence_refs": list(self.new_evidence_refs),
            "affected_obligation_ids": list(self.affected_obligation_ids),
            "affected_route_ids": list(self.affected_route_ids),
            "next_action": self.next_action,
            "old_tool_environment_hash": self.old_tool_environment_hash,
            "new_tool_environment_hash": self.new_tool_environment_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ProgressReceipt":
        payload = _mapping("ProgressReceipt", data)
        allowed = {
            "receipt_id",
            "action_id",
            "attempt_id",
            "contract_id",
            "kind",
            "claim",
            "scope",
            "old_frontier_hash",
            "new_frontier_hash",
            "created_at",
            "new_evidence_refs",
            "affected_obligation_ids",
            "affected_route_ids",
            "next_action",
            "old_tool_environment_hash",
            "new_tool_environment_hash",
        }
        _reject_extra(payload, allowed, "ProgressReceipt")
        required = allowed - {
            "new_evidence_refs",
            "affected_obligation_ids",
            "affected_route_ids",
            "next_action",
            "old_tool_environment_hash",
            "new_tool_environment_hash",
        }
        _require_keys(payload, required, "ProgressReceipt")
        return cls(
            receipt_id=payload["receipt_id"],
            action_id=payload["action_id"],
            attempt_id=payload["attempt_id"],
            contract_id=payload["contract_id"],
            kind=payload["kind"],
            claim=payload["claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            old_frontier_hash=payload["old_frontier_hash"],
            new_frontier_hash=payload["new_frontier_hash"],
            created_at=payload["created_at"],
            new_evidence_refs=tuple(payload.get("new_evidence_refs") or ()),
            affected_obligation_ids=tuple(payload.get("affected_obligation_ids") or ()),
            affected_route_ids=tuple(payload.get("affected_route_ids") or ()),
            next_action=payload.get("next_action"),
            old_tool_environment_hash=payload.get("old_tool_environment_hash"),
            new_tool_environment_hash=payload.get("new_tool_environment_hash"),
        )


def validate_progress_against_view(receipt: ProgressReceipt, view: ResearchView) -> None:
    """Confirm a receipt against current view state. Never mutates ``view``."""
    if not isinstance(receipt, ProgressReceipt):
        raise SchemaValidationError("receipt must be a ProgressReceipt")
    if not isinstance(view, ResearchView):
        raise SchemaValidationError("view must be a ResearchView")
    receipt.validate()
    if receipt.kind is ProgressKind.REPRESENTATION_GAIN:
        return
    if receipt.kind is ProgressKind.NO_VERIFIABLE_PROGRESS:
        if receipt.new_evidence_refs:
            raise SchemaValidationError(
                "NO_VERIFIABLE_PROGRESS cannot claim evidence"
            )
        return
    for artifact_id in receipt.new_evidence_refs:
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            raise SchemaValidationError(
                f"progress references unregistered artifact {artifact_id}"
            )
        validity = view.evidence_validity(artifact_id)
        if validity is not ValidityStatus.VALID or artifact.validity is not ValidityStatus.VALID:
            raise SchemaValidationError(
                f"progress artifact {artifact_id} is not VALID"
            )
        if artifact.evidence_kind is EvidenceKind.HEURISTIC_OBSERVATION:
            raise SchemaValidationError(
                "mathematical progress cannot use heuristic evidence"
            )
        if artifact.contract_id != receipt.contract_id:
            raise SchemaValidationError(
                f"progress artifact {artifact_id} contract must match receipt"
            )
        if artifact.checker_result is not CheckerResult.PASS:
            raise SchemaValidationError(
                f"mathematical progress requires checker PASS, got {artifact.checker_result}"
            )
        required_kind = (
            EvidenceKind.CHECKED_COUNTEREXAMPLE
            if receipt.kind is ProgressKind.COUNTEREXAMPLE_CHECKED
            else artifact.evidence_kind
        )
        if not evidence_satisfies(
            artifact,
            required_kind,
            receipt.scope,
            receipt.contract_id,
            exact_claim=receipt.claim,
        ):
            raise SchemaValidationError(
                "progress evidence does not satisfy scope, contract, claim, or kind"
            )
    if receipt.kind is ProgressKind.REDUCTION_VERIFIED:
        _require_reduction_relation(receipt, view)
    expected_status = None
    if receipt.kind is ProgressKind.OBLIGATION_DISCHARGED:
        expected_status = ObligationStatus.DISCHARGED
    elif receipt.kind is ProgressKind.COUNTEREXAMPLE_CHECKED:
        expected_status = ObligationStatus.REFUTED
    receipt_refs = set(receipt.new_evidence_refs)
    for obligation_id in receipt.affected_obligation_ids:
        obligation = view.get_obligation(obligation_id)
        if obligation is None:
            raise SchemaValidationError(
                f"progress references missing obligation {obligation_id}"
            )
        if obligation.scope != receipt.scope:
            raise SchemaValidationError(
                f"affected obligation {obligation_id} scope must equal receipt scope"
            )
        if obligation.contract_id != receipt.contract_id:
            raise SchemaValidationError(
                f"affected obligation {obligation_id} contract must match receipt"
            )
        if expected_status is not None and obligation.status is not expected_status:
            raise SchemaValidationError(
                f"{receipt.kind.value} requires affected obligation "
                f"{obligation_id} to be {expected_status.value}"
            )
        if receipt.kind in {
            ProgressKind.OBLIGATION_DISCHARGED,
            ProgressKind.COUNTEREXAMPLE_CHECKED,
        } and not receipt_refs.issubset(set(obligation.evidence_refs)):
            raise SchemaValidationError(
                f"{receipt.kind.value} evidence refs must be a subset of "
                f"obligation {obligation_id} evidence refs"
            )
    for route_id in receipt.affected_route_ids:
        route = view.get_route(route_id)
        if route is None:
            raise SchemaValidationError(f"progress references missing route {route_id}")
        target = view.get_obligation(route.target_obligation_id)
        if target is None:
            raise SchemaValidationError(f"affected route {route_id} target is missing")
        if target.scope != receipt.scope:
            raise SchemaValidationError(
                f"affected route {route_id} target scope must equal receipt scope"
            )
        if target.contract_id != receipt.contract_id:
            raise SchemaValidationError(
                f"affected route {route_id} target contract must match receipt"
            )


_REDUCTION_TYPES = frozenset(
    {RelationType.REDUCES_TO, RelationType.EQUIVALENT_UNDER}
)


def _require_reduction_relation(receipt: ProgressReceipt, view: ResearchView) -> None:
    refs = set(receipt.new_evidence_refs)
    for relation in view.relations():
        if relation.relation_type not in _REDUCTION_TYPES:
            continue
        if relation.validity is not ValidityStatus.VALID:
            continue
        if relation.exact_claim != receipt.claim:
            continue
        if relation.scope != receipt.scope:
            continue
        if relation.contract_id != receipt.contract_id:
            continue
        if refs.issubset(set(relation.evidence_refs)):
            return
    raise SchemaValidationError(
        "REDUCTION_VERIFIED requires a VALID REDUCES_TO or EQUIVALENT_UNDER "
        "relation whose contract, exact_claim and evidence refs match the receipt"
    )


def _action_id(
    *,
    action_type: str,
    mechanism: str,
    model_id: str,
    effort: str,
    task_mode: str,
    context_hash: str,
    dependency_hash: str,
    definitions_hash: str,
    tool_environment_hash: str,
    parameters_json: str,
    obligation_id: Optional[str],
    route_id: Optional[str],
) -> str:
    payload = {
        "action_type": action_type,
        "mechanism": mechanism,
        "model_id": model_id,
        "effort": effort,
        "task_mode": task_mode,
        "context_hash": context_hash,
        "dependency_hash": dependency_hash,
        "definitions_hash": definitions_hash,
        "tool_environment_hash": tool_environment_hash,
        "parameters_json": parameters_json,
        "obligation_id": obligation_id,
        "route_id": route_id,
    }
    return stable_id(_ACTION_DOMAIN, _ID_VERSION, canonical_json(payload))


def _progress_id(
    *,
    action_id: str,
    attempt_id: str,
    contract_id: str,
    kind: ProgressKind,
    claim: str,
    scope: MathematicalScope,
    old_frontier_hash: str,
    new_frontier_hash: str,
    new_evidence_refs: tuple[str, ...],
    affected_obligation_ids: tuple[str, ...],
    affected_route_ids: tuple[str, ...],
    next_action: Optional[str],
    old_tool_environment_hash: Optional[str],
    new_tool_environment_hash: Optional[str],
) -> str:
    payload = {
        "action_id": action_id,
        "attempt_id": attempt_id,
        "contract_id": contract_id,
        "kind": kind.value,
        "claim": claim,
        "scope": scope.identity_payload(),
        "old_frontier_hash": old_frontier_hash,
        "new_frontier_hash": new_frontier_hash,
        "new_evidence_refs": list(new_evidence_refs),
        "affected_obligation_ids": list(affected_obligation_ids),
        "affected_route_ids": list(affected_route_ids),
        "next_action": next_action,
        "old_tool_environment_hash": old_tool_environment_hash,
        "new_tool_environment_hash": new_tool_environment_hash,
    }
    return stable_id(_PROGRESS_DOMAIN, _ID_VERSION, canonical_json(payload))


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise SchemaValidationError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise SchemaValidationError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise SchemaValidationError(f"{label} missing fields: {sorted(missing)}")


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SchemaValidationError(f"{name} must be a nonempty str")
    return value


def _require_safe(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _SAFE_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} is not a safe identifier")
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} must be 64-char lowercase hex")
    return text


def _require_hex64_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list of 64-hex hashes")
    return tuple(_require_hex64(f"{name}[{i}]", item) for i, item in enumerate(value))


def _optional_hex64(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_hex64(name, value)


def _optional_text(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_nonempty(name, value)


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise SchemaValidationError(f"{name} has unknown value {value!r}") from None
    raise SchemaValidationError(f"{name} must be {enum_cls.__name__}")


def _require_canonical_object_json(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"{name} must be JSON") from exc
    if not isinstance(parsed, dict):
        raise SchemaValidationError(f"{name} must be a JSON object")
    try:
        recoded = canonical_json(parsed)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{name} is not JSON-safe") from exc
    if recoded != text:
        raise SchemaValidationError(f"{name} must already be canonical JSON")
    return recoded


def _require_utc(name: str, value: object) -> datetime:
    text = _require_nonempty(name, value)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise SchemaValidationError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise SchemaValidationError(f"{name} must be UTC")
    return dt.astimezone(_UTC)
