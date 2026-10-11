"""Frozen research-control schema types.

Stdlib only. Dataclasses are frozen and validate on construction and on
``from_dict`` load. Event envelope hashes are recomputed; a correct verifier
verdict plus a failed Fact commit is representable and is not a truth claim.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .ids import canonical_json, event_id, payload_hash

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_EVENT_SCHEMA_VERSION = 1
_UTC = timezone.utc
_ZERO = timedelta(0)

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cost_microusd",
    "wall_ms",
    "tool_cpu_ms",
    "tool_gpu_ms",
)
_USAGE_UNITS = {
    "input_tokens": "tokens",
    "output_tokens": "tokens",
    "reasoning_tokens": "tokens",
    "cached_tokens": "tokens",
    "cost_microusd": "microusd",
    "wall_ms": "ms",
    "tool_cpu_ms": "ms",
    "tool_gpu_ms": "ms",
}
_ALLOWED_VERDICTS = frozenset({"correct", "wrong"})

__all__ = [
    "SchemaValidationError",
    "ObservationStatus",
    "InfrastructureStatus",
    "MathematicalStatus",
    "FactCommitStatus",
    "ReservationState",
    "BudgetLimitKind",
    "ObservedQuantity",
    "UsageRecord",
    "EventEnvelope",
    "AttemptReceipt",
    "BudgetLimit",
    "BudgetReservation",
]


class SchemaValidationError(ValueError):
    """Raised when a schema object is malformed, incomplete, or tampered."""


class ObservationStatus(str, Enum):
    OBSERVED = "OBSERVED"
    UNKNOWN = "UNKNOWN"


class InfrastructureStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class MathematicalStatus(str, Enum):
    NO_CLAIM = "NO_CLAIM"
    CANDIDATE = "CANDIDATE"
    CHECKED_CORRECT = "CHECKED_CORRECT"
    CHECKED_WRONG = "CHECKED_WRONG"
    UNKNOWN = "UNKNOWN"


class FactCommitStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class BudgetLimitKind(str, Enum):
    LIMITED = "LIMITED"
    UNLIMITED = "UNLIMITED"


_ACTIVE_INFRASTRUCTURE_STATES = frozenset(
    {InfrastructureStatus.PENDING, InfrastructureStatus.RUNNING}
)
_TERMINAL_INFRASTRUCTURE_STATES = frozenset(
    {
        InfrastructureStatus.SUCCEEDED,
        InfrastructureStatus.FAILED,
        InfrastructureStatus.TIMED_OUT,
        InfrastructureStatus.CANCELLED,
    }
)
_TERMINAL_RESERVATION_STATES = frozenset(
    {
        ReservationState.SETTLED,
        ReservationState.RELEASED,
        ReservationState.EXPIRED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservedQuantity:
    """A measured amount. ``UNKNOWN`` cannot carry a numeric value, including 0."""

    unit: str
    status: ObservationStatus
    value: Optional[int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "unit", _require_nonempty_str("unit", self.unit))
        object.__setattr__(
            self, "status", _require_enum(ObservationStatus, self.status, "status")
        )
        if self.status is ObservationStatus.UNKNOWN:
            if self.value is not None:
                raise SchemaValidationError(
                    "UNKNOWN ObservedQuantity requires value=None"
                )
            return
        if self.status is ObservationStatus.OBSERVED:
            object.__setattr__(
                self, "value", _require_nonneg_int("value", self.value)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "status": self.status.value,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ObservedQuantity":
        payload = _mapping("ObservedQuantity", data)
        _reject_extra(payload, {"unit", "status", "value"}, "ObservedQuantity")
        _require_keys(payload, {"unit", "status", "value"}, "ObservedQuantity")
        return cls(
            unit=payload["unit"],
            status=payload["status"],
            value=payload["value"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageRecord:
    """Observed or unknown usage for one attempt."""

    input_tokens: ObservedQuantity
    output_tokens: ObservedQuantity
    reasoning_tokens: ObservedQuantity
    cached_tokens: ObservedQuantity
    cost_microusd: ObservedQuantity
    wall_ms: ObservedQuantity
    tool_cpu_ms: ObservedQuantity
    tool_gpu_ms: ObservedQuantity

    def __post_init__(self) -> None:
        for name in _USAGE_FIELDS:
            qty = getattr(self, name)
            if not isinstance(qty, ObservedQuantity):
                raise SchemaValidationError(
                    f"{name} must be ObservedQuantity, got {type(qty).__name__}"
                )
            expected_unit = _USAGE_UNITS[name]
            if qty.unit != expected_unit:
                raise SchemaValidationError(
                    f"{name} must use unit {expected_unit!r}, got {qty.unit!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).to_dict() for name in _USAGE_FIELDS}

    @classmethod
    def from_dict(cls, data: object) -> "UsageRecord":
        payload = _mapping("UsageRecord", data)
        _reject_extra(payload, set(_USAGE_FIELDS), "UsageRecord")
        _require_keys(payload, set(_USAGE_FIELDS), "UsageRecord")
        return cls(
            **{
                name: ObservedQuantity.from_dict(payload[name])
                for name in _USAGE_FIELDS
            }
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EventEnvelope:
    """Immutable control-plane event. Hash and event id are recomputed on load."""

    schema_version: int
    event_id: str
    project_id: str
    timestamp_utc: str
    actor: str
    event_type: str
    idempotency_key: str
    payload: Mapping[str, Any]
    payload_hash: str
    causation_id: Optional[str] = None
    correlation_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(_json_object(self.payload)))
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        project_id: str,
        actor: str,
        event_type: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        timestamp_utc: str,
        causation_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        schema_version: int = _EVENT_SCHEMA_VERSION,
    ) -> "EventEnvelope":
        payload_obj = _json_object(payload)
        digest = payload_hash(payload_obj)
        return cls(
            schema_version=schema_version,
            event_id=event_id(project_id, event_type, idempotency_key, digest),
            project_id=project_id,
            timestamp_utc=timestamp_utc,
            actor=actor,
            event_type=event_type,
            idempotency_key=idempotency_key,
            payload=payload_obj,
            payload_hash=digest,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )

    def validate(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ):
            raise SchemaValidationError("schema_version must be an int")
        if self.schema_version != _EVENT_SCHEMA_VERSION:
            raise SchemaValidationError(
                f"unsupported schema_version {self.schema_version}"
            )
        _require_safe_identifier("project_id", self.project_id)
        _require_utc("timestamp_utc", self.timestamp_utc)
        _require_nonempty_str("actor", self.actor)
        _require_safe_identifier("event_type", self.event_type)
        _require_nonempty_str("idempotency_key", self.idempotency_key)
        payload_obj = _json_object(self.payload)
        digest = payload_hash(payload_obj)
        _require_hex64("payload_hash", self.payload_hash)
        if self.payload_hash != digest:
            raise SchemaValidationError("payload_hash does not match payload")
        expected_id = event_id(
            self.project_id,
            self.event_type,
            self.idempotency_key,
            self.payload_hash,
        )
        _require_hex64("event_id", self.event_id)
        if self.event_id != expected_id:
            raise SchemaValidationError("event_id does not match identity fields")
        if self.causation_id is not None:
            _require_hex64("causation_id", self.causation_id)
        if self.correlation_id is not None:
            _require_nonempty_str("correlation_id", self.correlation_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "project_id": self.project_id,
            "timestamp_utc": self.timestamp_utc,
            "actor": self.actor,
            "event_type": self.event_type,
            "idempotency_key": self.idempotency_key,
            "payload": _json_object(self.payload),
            "payload_hash": self.payload_hash,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> "EventEnvelope":
        payload = _mapping("EventEnvelope", data)
        allowed = {
            "schema_version",
            "event_id",
            "project_id",
            "timestamp_utc",
            "actor",
            "event_type",
            "idempotency_key",
            "payload",
            "payload_hash",
            "causation_id",
            "correlation_id",
        }
        _reject_extra(payload, allowed, "EventEnvelope")
        required = allowed - {"causation_id", "correlation_id"}
        _require_keys(payload, required, "EventEnvelope")
        return cls(
            schema_version=payload["schema_version"],
            event_id=payload["event_id"],
            project_id=payload["project_id"],
            timestamp_utc=payload["timestamp_utc"],
            actor=payload["actor"],
            event_type=payload["event_type"],
            idempotency_key=payload["idempotency_key"],
            payload=payload["payload"],
            payload_hash=payload["payload_hash"],
            causation_id=payload.get("causation_id"),
            correlation_id=payload.get("correlation_id"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptReceipt:
    """Control-plane attempt record. Infra, math, verification, and Fact commit
    are independent; none of these fields is a verified Fact."""

    attempt_id: str
    project_id: str
    action_type: str
    model_id: str
    effort: str
    started_at: str
    infrastructure_status: InfrastructureStatus
    mathematical_status: MathematicalStatus
    fact_commit_status: FactCommitStatus
    usage: UsageRecord
    artifact_refs: tuple[str, ...] = ()
    ended_at: Optional[str] = None
    obligation_id: Optional[str] = None
    route_id: Optional[str] = None
    reservation_id: Optional[str] = None
    verify_ref: Optional[str] = None
    verdict: Optional[str] = None
    fact_id: Optional[str] = None
    write_error: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attempt_id", _require_hex64("attempt_id", self.attempt_id)
        )
        object.__setattr__(
            self, "project_id", _require_safe_identifier("project_id", self.project_id)
        )
        object.__setattr__(
            self,
            "action_type",
            _require_safe_identifier("action_type", self.action_type),
        )
        object.__setattr__(
            self, "model_id", _require_nonempty_str("model_id", self.model_id)
        )
        object.__setattr__(
            self, "effort", _require_nonempty_str("effort", self.effort)
        )
        started = _require_utc("started_at", self.started_at)
        object.__setattr__(
            self,
            "infrastructure_status",
            _require_enum(
                InfrastructureStatus, self.infrastructure_status, "infrastructure_status"
            ),
        )
        object.__setattr__(
            self,
            "mathematical_status",
            _require_enum(
                MathematicalStatus, self.mathematical_status, "mathematical_status"
            ),
        )
        object.__setattr__(
            self,
            "fact_commit_status",
            _require_enum(
                FactCommitStatus, self.fact_commit_status, "fact_commit_status"
            ),
        )
        if not isinstance(self.usage, UsageRecord):
            raise SchemaValidationError("usage must be a UsageRecord")
        object.__setattr__(
            self, "artifact_refs", _require_hex64_tuple("artifact_refs", self.artifact_refs)
        )
        if self.ended_at is not None:
            ended = _require_utc("ended_at", self.ended_at)
            if ended < started:
                raise SchemaValidationError("ended_at must not precede started_at")
        if (
            self.infrastructure_status in _ACTIVE_INFRASTRUCTURE_STATES
            and self.ended_at is not None
        ):
            raise SchemaValidationError(
                f"{self.infrastructure_status.value} attempt forbids ended_at"
            )
        if (
            self.infrastructure_status in _TERMINAL_INFRASTRUCTURE_STATES
            and self.ended_at is None
        ):
            raise SchemaValidationError(
                f"{self.infrastructure_status.value} attempt requires ended_at"
            )
        if self.obligation_id is not None:
            object.__setattr__(
                self,
                "obligation_id",
                _require_nonempty_str("obligation_id", self.obligation_id),
            )
        if self.route_id is not None:
            object.__setattr__(
                self, "route_id", _require_nonempty_str("route_id", self.route_id)
            )
        if self.reservation_id is not None:
            object.__setattr__(
                self,
                "reservation_id",
                _require_hex64("reservation_id", self.reservation_id),
            )
        if self.verify_ref is not None:
            object.__setattr__(
                self, "verify_ref", _require_nonempty_str("verify_ref", self.verify_ref)
            )
        if self.verdict is not None:
            object.__setattr__(
                self, "verdict", _require_nonempty_str("verdict", self.verdict)
            )
            if self.verdict not in _ALLOWED_VERDICTS:
                raise SchemaValidationError("verdict must be 'correct' or 'wrong'")
            if self.verify_ref is None:
                raise SchemaValidationError("verdict requires verify_ref provenance")
            if (
                self.verdict == "correct"
                and self.mathematical_status is not MathematicalStatus.CHECKED_CORRECT
            ):
                raise SchemaValidationError("correct verdict requires CHECKED_CORRECT")
            if (
                self.verdict == "wrong"
                and self.mathematical_status is not MathematicalStatus.CHECKED_WRONG
            ):
                raise SchemaValidationError("wrong verdict requires CHECKED_WRONG")
        if self.fact_id is not None:
            object.__setattr__(
                self, "fact_id", _require_nonempty_str("fact_id", self.fact_id)
            )
        if self.write_error is not None:
            object.__setattr__(
                self,
                "write_error",
                _require_nonempty_str("write_error", self.write_error),
            )
        if self.fact_commit_status is FactCommitStatus.COMMITTED:
            if self.verdict != "correct":
                raise SchemaValidationError("COMMITTED requires correct verdict")
            if self.mathematical_status is not MathematicalStatus.CHECKED_CORRECT:
                raise SchemaValidationError("COMMITTED requires CHECKED_CORRECT")
            if self.fact_id is None:
                raise SchemaValidationError("COMMITTED Fact commit requires fact_id")
            if self.write_error is not None:
                raise SchemaValidationError("COMMITTED forbids write_error")
        elif self.fact_commit_status is FactCommitStatus.FAILED:
            if self.verdict != "correct":
                raise SchemaValidationError("FAILED Fact commit requires correct verdict")
            if self.mathematical_status is not MathematicalStatus.CHECKED_CORRECT:
                raise SchemaValidationError("FAILED Fact commit requires CHECKED_CORRECT")
            if self.write_error is None:
                raise SchemaValidationError("FAILED Fact commit requires write_error")
            if self.fact_id is not None:
                raise SchemaValidationError("FAILED Fact commit forbids fact_id")
        elif self.write_error is not None:
            raise SchemaValidationError(
                "write_error is forbidden unless Fact commit FAILED"
            )
        elif self.fact_id is not None:
            raise SchemaValidationError(
                "fact_id is forbidden unless Fact commit COMMITTED"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "project_id": self.project_id,
            "action_type": self.action_type,
            "model_id": self.model_id,
            "effort": self.effort,
            "started_at": self.started_at,
            "infrastructure_status": self.infrastructure_status.value,
            "mathematical_status": self.mathematical_status.value,
            "fact_commit_status": self.fact_commit_status.value,
            "usage": self.usage.to_dict(),
            "artifact_refs": list(self.artifact_refs),
            "ended_at": self.ended_at,
            "obligation_id": self.obligation_id,
            "route_id": self.route_id,
            "reservation_id": self.reservation_id,
            "verify_ref": self.verify_ref,
            "verdict": self.verdict,
            "fact_id": self.fact_id,
            "write_error": self.write_error,
        }

    @classmethod
    def from_dict(cls, data: object) -> "AttemptReceipt":
        payload = _mapping("AttemptReceipt", data)
        allowed = {
            "attempt_id",
            "project_id",
            "action_type",
            "model_id",
            "effort",
            "started_at",
            "infrastructure_status",
            "mathematical_status",
            "fact_commit_status",
            "usage",
            "artifact_refs",
            "ended_at",
            "obligation_id",
            "route_id",
            "reservation_id",
            "verify_ref",
            "verdict",
            "fact_id",
            "write_error",
        }
        _reject_extra(payload, allowed, "AttemptReceipt")
        required = {
            "attempt_id",
            "project_id",
            "action_type",
            "model_id",
            "effort",
            "started_at",
            "infrastructure_status",
            "mathematical_status",
            "fact_commit_status",
            "usage",
        }
        _require_keys(payload, required, "AttemptReceipt")
        if "artifact_refs" not in payload:
            refs: tuple[str, ...] = ()
        else:
            raw_refs = payload["artifact_refs"]
            if isinstance(raw_refs, str) or not isinstance(raw_refs, (list, tuple)):
                raise SchemaValidationError(
                    "artifact_refs must be a list of 64-hex hashes"
                )
            refs = tuple(raw_refs)
        return cls(
            attempt_id=payload["attempt_id"],
            project_id=payload["project_id"],
            action_type=payload["action_type"],
            model_id=payload["model_id"],
            effort=payload["effort"],
            started_at=payload["started_at"],
            infrastructure_status=payload["infrastructure_status"],
            mathematical_status=payload["mathematical_status"],
            fact_commit_status=payload["fact_commit_status"],
            usage=UsageRecord.from_dict(payload["usage"]),
            artifact_refs=refs,
            ended_at=payload.get("ended_at"),
            obligation_id=payload.get("obligation_id"),
            route_id=payload.get("route_id"),
            reservation_id=payload.get("reservation_id"),
            verify_ref=payload.get("verify_ref"),
            verdict=payload.get("verdict"),
            fact_id=payload.get("fact_id"),
            write_error=payload.get("write_error"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetLimit:
    """Explicit limit: a nonnegative integer, or UNLIMITED. Null is not a kind."""

    kind: BudgetLimitKind
    value: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", _require_enum(BudgetLimitKind, self.kind, "kind")
        )
        if self.kind is BudgetLimitKind.UNLIMITED:
            if self.value is not None:
                raise SchemaValidationError(
                    "UNLIMITED BudgetLimit requires value=None"
                )
            return
        if self.kind is BudgetLimitKind.LIMITED:
            object.__setattr__(
                self, "value", _require_nonneg_int("value", self.value)
            )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, data: object) -> "BudgetLimit":
        payload = _mapping("BudgetLimit", data)
        _reject_extra(payload, {"kind", "value"}, "BudgetLimit")
        _require_keys(payload, {"kind"}, "BudgetLimit")
        return cls(kind=payload["kind"], value=payload.get("value"))


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetReservation:
    reservation_id: str
    project_id: str
    attempt_id: str
    idempotency_key: str
    reserved_cost_microusd: int
    reserved_input_tokens: int
    reserved_output_tokens: int
    state: ReservationState
    created_at: str
    settled_at: Optional[str] = None
    actual_usage: Optional[UsageRecord] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservation_id",
            _require_hex64("reservation_id", self.reservation_id),
        )
        object.__setattr__(
            self, "project_id", _require_safe_identifier("project_id", self.project_id)
        )
        object.__setattr__(
            self, "attempt_id", _require_hex64("attempt_id", self.attempt_id)
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _require_nonempty_str("idempotency_key", self.idempotency_key),
        )
        object.__setattr__(
            self,
            "reserved_cost_microusd",
            _require_nonneg_int("reserved_cost_microusd", self.reserved_cost_microusd),
        )
        object.__setattr__(
            self,
            "reserved_input_tokens",
            _require_nonneg_int("reserved_input_tokens", self.reserved_input_tokens),
        )
        object.__setattr__(
            self,
            "reserved_output_tokens",
            _require_nonneg_int("reserved_output_tokens", self.reserved_output_tokens),
        )
        object.__setattr__(
            self, "state", _require_enum(ReservationState, self.state, "state")
        )
        created = _require_utc("created_at", self.created_at)
        if self.settled_at is not None:
            settled = _require_utc("settled_at", self.settled_at)
            if settled < created:
                raise SchemaValidationError("settled_at must not precede created_at")
        if self.actual_usage is not None and not isinstance(self.actual_usage, UsageRecord):
            raise SchemaValidationError("actual_usage must be a UsageRecord")
        if self.state is ReservationState.RESERVED:
            if self.settled_at is not None:
                raise SchemaValidationError("RESERVED reservation forbids settled_at")
            if self.actual_usage is not None:
                raise SchemaValidationError("RESERVED reservation forbids actual_usage")
        elif self.state in _TERMINAL_RESERVATION_STATES:
            if self.settled_at is None:
                raise SchemaValidationError(
                    f"{self.state.value} reservation requires settled_at"
                )
            if self.state is ReservationState.SETTLED:
                if self.actual_usage is None:
                    raise SchemaValidationError(
                        "SETTLED reservation requires actual_usage"
                    )
            elif self.actual_usage is not None:
                raise SchemaValidationError(
                    f"{self.state.value} reservation forbids actual_usage"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "project_id": self.project_id,
            "attempt_id": self.attempt_id,
            "idempotency_key": self.idempotency_key,
            "reserved_cost_microusd": self.reserved_cost_microusd,
            "reserved_input_tokens": self.reserved_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "state": self.state.value,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
            "actual_usage": None if self.actual_usage is None else self.actual_usage.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "BudgetReservation":
        payload = _mapping("BudgetReservation", data)
        allowed = {
            "reservation_id",
            "project_id",
            "attempt_id",
            "idempotency_key",
            "reserved_cost_microusd",
            "reserved_input_tokens",
            "reserved_output_tokens",
            "state",
            "created_at",
            "settled_at",
            "actual_usage",
        }
        _reject_extra(payload, allowed, "BudgetReservation")
        required = allowed - {"settled_at", "actual_usage"}
        _require_keys(payload, required, "BudgetReservation")
        actual = payload.get("actual_usage")
        return cls(
            reservation_id=payload["reservation_id"],
            project_id=payload["project_id"],
            attempt_id=payload["attempt_id"],
            idempotency_key=payload["idempotency_key"],
            reserved_cost_microusd=payload["reserved_cost_microusd"],
            reserved_input_tokens=payload["reserved_input_tokens"],
            reserved_output_tokens=payload["reserved_output_tokens"],
            state=payload["state"],
            created_at=payload["created_at"],
            settled_at=payload.get("settled_at"),
            actual_usage=None if actual is None else UsageRecord.from_dict(actual),
        )


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


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SchemaValidationError(f"{name} must be a nonempty str")
    return value


def _require_safe_identifier(name: str, value: object) -> str:
    text = _require_nonempty_str(name, value)
    if _SAFE_IDENTIFIER_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} is not a safe identifier")
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty_str(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} must be 64-char lowercase hex")
    return text


def _require_hex64_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list of 64-hex hashes")
    return tuple(_require_hex64(f"{name}[{i}]", item) for i, item in enumerate(value))


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaValidationError(f"{name} must be a nonnegative int")
    if value < 0:
        raise SchemaValidationError(f"{name} must be a nonnegative int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise SchemaValidationError(f"{name} has unknown value {value!r}") from None
    raise SchemaValidationError(f"{name} must be {enum_cls.__name__}")


def _require_utc(name: str, value: object) -> datetime:
    text = _require_nonempty_str(name, value)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise SchemaValidationError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None:
        raise SchemaValidationError(f"{name} must be ISO-8601 UTC")
    if dt.utcoffset() != _ZERO:
        raise SchemaValidationError(f"{name} must be UTC")
    return dt.astimezone(_UTC)


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise SchemaValidationError("payload must be a JSON object")
    try:
        encoded = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"payload is not JSON-safe: {exc}") from exc
    loaded = json.loads(encoded)
    if not isinstance(loaded, dict):
        raise SchemaValidationError("payload must be a JSON object")
    return loaded
