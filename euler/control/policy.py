"""Multi-dimensional resource vectors, budget policy, and pure snapshots.

Stdlib only. Every dimension is an explicit LIMITED or UNLIMITED integer bound.
Unknown settlement consumes the reserved quantity. Methods never mutate the
original snapshot. No provider calls and no truth-store writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from euler.control.schema import (
    ObservationStatus,
    ObservedQuantity,
    SchemaValidationError,
    UsageRecord,
)

_UTC = timezone.utc
_ZERO = timedelta(0)
_BASIS_POINT_MAX = 10000

RESOURCE_DIMENSIONS = (
    "cost_microusd",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "wall_ms",
    "tool_cpu_ms",
    "tool_gpu_ms",
)

_UNITS = {
    "cost_microusd": "microusd",
    "input_tokens": "tokens",
    "output_tokens": "tokens",
    "reasoning_tokens": "tokens",
    "cached_tokens": "tokens",
    "wall_ms": "ms",
    "tool_cpu_ms": "ms",
    "tool_gpu_ms": "ms",
}

__all__ = [
    "PolicyError",
    "BudgetClass",
    "SlotKind",
    "ResourceLimitKind",
    "OnDeadlinePolicy",
    "OnStagnationPolicy",
    "RESOURCE_DIMENSIONS",
    "ResourceVector",
    "DimensionLimit",
    "ResourceUsage",
    "BudgetPolicy",
    "ResourceSnapshot",
]


class PolicyError(ValueError):
    """Malformed resource policy, vector, or snapshot transition."""


class BudgetClass(str, Enum):
    NORMAL = "NORMAL"
    EXPLORATION = "EXPLORATION"
    FINAL_AUDIT = "FINAL_AUDIT"


class SlotKind(str, Enum):
    REASONING = "REASONING"
    COMPUTE = "COMPUTE"
    RETRIEVAL = "RETRIEVAL"
    VERIFIER = "VERIFIER"


class ResourceLimitKind(str, Enum):
    LIMITED = "LIMITED"
    UNLIMITED = "UNLIMITED"


class OnDeadlinePolicy(str, Enum):
    REJECT_NORMAL = "REJECT_NORMAL"
    REJECT_ALL = "REJECT_ALL"
    ALLOW = "ALLOW"


class OnStagnationPolicy(str, Enum):
    CONTINUE = "CONTINUE"
    DEFER_EXPLORATION = "DEFER_EXPLORATION"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceVector:
    """Nonnegative integer quantities for all eight resource dimensions."""

    cost_microusd: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    wall_ms: int
    tool_cpu_ms: int
    tool_gpu_ms: int

    def __post_init__(self) -> None:
        for name in RESOURCE_DIMENSIONS:
            object.__setattr__(
                self, name, _require_nonneg_int(name, getattr(self, name))
            )

    @classmethod
    def zero(cls) -> "ResourceVector":
        return cls(
            cost_microusd=0,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=0,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        )

    def get(self, name: str) -> int:
        _require_dimension(name)
        return int(getattr(self, name))

    def add(self, other: "ResourceVector") -> "ResourceVector":
        _require_vector("other", other)
        return ResourceVector(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in RESOURCE_DIMENSIONS
            }
        )

    def sub(self, other: "ResourceVector") -> "ResourceVector":
        _require_vector("other", other)
        return ResourceVector(
            **{
                name: _require_nonneg_int(
                    name, getattr(self, name) - getattr(other, name)
                )
                for name in RESOURCE_DIMENSIONS
            }
        )

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in RESOURCE_DIMENSIONS}

    @classmethod
    def from_dict(cls, data: object) -> "ResourceVector":
        payload = _mapping("ResourceVector", data)
        _reject_extra(payload, set(RESOURCE_DIMENSIONS), "ResourceVector")
        _require_keys(payload, set(RESOURCE_DIMENSIONS), "ResourceVector")
        return cls(**{name: payload[name] for name in RESOURCE_DIMENSIONS})


@dataclass(frozen=True, slots=True, kw_only=True)
class DimensionLimit:
    """Explicit LIMITED(value) or UNLIMITED. Null is not a kind."""

    kind: ResourceLimitKind
    value: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "kind", _require_enum(ResourceLimitKind, self.kind, "kind")
        )
        if self.kind is ResourceLimitKind.UNLIMITED:
            if self.value is not None:
                raise PolicyError("UNLIMITED DimensionLimit requires value=None")
            return
        object.__setattr__(self, "value", _require_nonneg_int("value", self.value))

    @classmethod
    def limited(cls, value: int) -> "DimensionLimit":
        return cls(kind=ResourceLimitKind.LIMITED, value=value)

    @classmethod
    def unlimited(cls) -> "DimensionLimit":
        return cls(kind=ResourceLimitKind.UNLIMITED, value=None)

    def allows(self, amount: int) -> bool:
        quantity = _require_nonneg_int("amount", amount)
        if self.kind is ResourceLimitKind.UNLIMITED:
            return True
        return quantity <= int(self.value or 0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, data: object, *, label: str = "DimensionLimit") -> "DimensionLimit":
        payload = _mapping(label, data)
        _reject_extra(payload, {"kind", "value"}, label)
        _require_keys(payload, {"kind"}, label)
        return cls(kind=payload["kind"], value=payload.get("value"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceUsage:
    """Observed or unknown usage for the eight scheduler dimensions."""

    cost_microusd: ObservedQuantity
    input_tokens: ObservedQuantity
    output_tokens: ObservedQuantity
    reasoning_tokens: ObservedQuantity
    cached_tokens: ObservedQuantity
    wall_ms: ObservedQuantity
    tool_cpu_ms: ObservedQuantity
    tool_gpu_ms: ObservedQuantity

    def __post_init__(self) -> None:
        for name in RESOURCE_DIMENSIONS:
            qty = getattr(self, name)
            if not isinstance(qty, ObservedQuantity):
                raise PolicyError(
                    f"{name} must be ObservedQuantity, got {type(qty).__name__}"
                )
            expected = _UNITS[name]
            if qty.unit != expected:
                raise PolicyError(
                    f"{name} must use unit {expected!r}, got {qty.unit!r}"
                )

    @classmethod
    def unknown(cls) -> "ResourceUsage":
        return cls(
            **{
                name: ObservedQuantity(
                    unit=_UNITS[name],
                    status=ObservationStatus.UNKNOWN,
                    value=None,
                )
                for name in RESOURCE_DIMENSIONS
            }
        )

    @classmethod
    def observed(cls, **values: int) -> "ResourceUsage":
        extra = set(values) - set(RESOURCE_DIMENSIONS)
        if extra:
            raise PolicyError(f"ResourceUsage.observed unknown fields: {sorted(extra)}")
        missing = set(RESOURCE_DIMENSIONS) - set(values)
        if missing:
            raise PolicyError(
                f"ResourceUsage.observed missing fields: {sorted(missing)}"
            )
        return cls(
            **{
                name: ObservedQuantity(
                    unit=_UNITS[name],
                    status=ObservationStatus.OBSERVED,
                    value=_require_nonneg_int(name, values[name]),
                )
                for name in RESOURCE_DIMENSIONS
            }
        )

    @classmethod
    def from_usage_record(cls, usage: UsageRecord) -> "ResourceUsage":
        if not isinstance(usage, UsageRecord):
            raise PolicyError(
                f"usage must be UsageRecord, got {type(usage).__name__}"
            )
        return cls(**{name: getattr(usage, name) for name in RESOURCE_DIMENSIONS})

    def settled_vector(self, reserved: ResourceVector) -> ResourceVector:
        _require_vector("reserved", reserved)
        amounts: dict[str, int] = {}
        for name in RESOURCE_DIMENSIONS:
            amounts[name] = _settled_amount(getattr(self, name), reserved.get(name))
        return ResourceVector(**amounts)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).to_dict() for name in RESOURCE_DIMENSIONS}

    @classmethod
    def from_dict(cls, data: object) -> "ResourceUsage":
        payload = _mapping("ResourceUsage", data)
        _reject_extra(payload, set(RESOURCE_DIMENSIONS), "ResourceUsage")
        _require_keys(payload, set(RESOURCE_DIMENSIONS), "ResourceUsage")
        try:
            return cls(
                **{
                    name: ObservedQuantity.from_dict(payload[name])
                    for name in RESOURCE_DIMENSIONS
                }
            )
        except SchemaValidationError as exc:
            raise PolicyError(str(exc)) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetPolicy:
    """Explicit eight-dimension limits, slot caps, UTC deadline, and basis-point reserves."""

    cost_microusd: DimensionLimit
    input_tokens: DimensionLimit
    output_tokens: DimensionLimit
    reasoning_tokens: DimensionLimit
    cached_tokens: DimensionLimit
    wall_ms: DimensionLimit
    tool_cpu_ms: DimensionLimit
    tool_gpu_ms: DimensionLimit
    max_reasoning_slots: int = 2
    max_compute_slots: int = 1
    max_retrieval_slots: int = 1
    max_verifier_slots: int = 1
    final_audit_reserve_bp: int = 0
    exploration_reserve_bp: int = 0
    deadline_utc: Optional[str] = None
    on_deadline: OnDeadlinePolicy = OnDeadlinePolicy.REJECT_NORMAL
    on_stagnation: OnStagnationPolicy = OnStagnationPolicy.CONTINUE

    def __post_init__(self) -> None:
        for name in RESOURCE_DIMENSIONS:
            limit = getattr(self, name)
            if not isinstance(limit, DimensionLimit):
                raise PolicyError(f"{name} must be a DimensionLimit")
        object.__setattr__(
            self,
            "max_reasoning_slots",
            _require_nonneg_int("max_reasoning_slots", self.max_reasoning_slots),
        )
        object.__setattr__(
            self,
            "max_compute_slots",
            _require_nonneg_int("max_compute_slots", self.max_compute_slots),
        )
        object.__setattr__(
            self,
            "max_retrieval_slots",
            _require_nonneg_int("max_retrieval_slots", self.max_retrieval_slots),
        )
        object.__setattr__(
            self,
            "max_verifier_slots",
            _require_nonneg_int("max_verifier_slots", self.max_verifier_slots),
        )
        fa = _require_basis_points(
            "final_audit_reserve_bp", self.final_audit_reserve_bp
        )
        exp = _require_basis_points(
            "exploration_reserve_bp", self.exploration_reserve_bp
        )
        if fa + exp > _BASIS_POINT_MAX:
            raise PolicyError(
                "final-audit and exploration reserves must sum to at most 10000"
            )
        object.__setattr__(self, "final_audit_reserve_bp", fa)
        object.__setattr__(self, "exploration_reserve_bp", exp)
        object.__setattr__(
            self,
            "on_deadline",
            _require_enum(OnDeadlinePolicy, self.on_deadline, "on_deadline"),
        )
        object.__setattr__(
            self,
            "on_stagnation",
            _require_enum(OnStagnationPolicy, self.on_stagnation, "on_stagnation"),
        )
        if self.deadline_utc is not None:
            _parse_utc("deadline_utc", self.deadline_utc)
            object.__setattr__(self, "deadline_utc", self.deadline_utc)
            if self.on_deadline is not OnDeadlinePolicy.ALLOW:
                if fa <= 0:
                    raise PolicyError(
                        "rejecting deadline requires final_audit_reserve_bp > 0"
                    )
                if self.max_verifier_slots <= 0:
                    raise PolicyError(
                        "rejecting deadline requires max_verifier_slots > 0"
                    )

    def dimension(self, name: str) -> DimensionLimit:
        _require_dimension(name)
        return getattr(self, name)

    def max_slots(self, kind: SlotKind) -> int:
        slot = _require_enum(SlotKind, kind, "slot_kind")
        if slot is SlotKind.REASONING:
            return self.max_reasoning_slots
        if slot is SlotKind.COMPUTE:
            return self.max_compute_slots
        if slot is SlotKind.RETRIEVAL:
            return self.max_retrieval_slots
        return self.max_verifier_slots

    def exclusive_quota(self, name: str, budget_class: BudgetClass) -> int | None:
        """Exclusive LIMITED pool for a class, or None when the dimension is UNLIMITED."""
        limit = self.dimension(name)
        if limit.kind is ResourceLimitKind.UNLIMITED:
            return None
        total = int(limit.value or 0)
        klass = _require_enum(BudgetClass, budget_class, "budget_class")
        if klass is BudgetClass.FINAL_AUDIT:
            return _basis_share(total, self.final_audit_reserve_bp)
        if klass is BudgetClass.EXPLORATION:
            return _basis_share(total, self.exploration_reserve_bp)
        return 0

    def unreserved_quota(self, name: str) -> int | None:
        limit = self.dimension(name)
        if limit.kind is ResourceLimitKind.UNLIMITED:
            return None
        total = int(limit.value or 0)
        return (
            total
            - _basis_share(total, self.final_audit_reserve_bp)
            - _basis_share(total, self.exploration_reserve_bp)
        )

    def deadline_reached(self, now_utc: str) -> bool:
        if self.deadline_utc is None:
            return False
        return _parse_utc("now_utc", now_utc) >= _parse_utc(
            "deadline_utc", self.deadline_utc
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            name: getattr(self, name).to_dict() for name in RESOURCE_DIMENSIONS
        }
        payload.update(
            {
                "max_reasoning_slots": self.max_reasoning_slots,
                "max_compute_slots": self.max_compute_slots,
                "max_retrieval_slots": self.max_retrieval_slots,
                "max_verifier_slots": self.max_verifier_slots,
                "final_audit_reserve_bp": self.final_audit_reserve_bp,
                "exploration_reserve_bp": self.exploration_reserve_bp,
                "deadline_utc": self.deadline_utc,
                "on_deadline": self.on_deadline.value,
                "on_stagnation": self.on_stagnation.value,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: object) -> "BudgetPolicy":
        payload = _mapping("BudgetPolicy", data)
        allowed = set(RESOURCE_DIMENSIONS) | {
            "max_reasoning_slots",
            "max_compute_slots",
            "max_retrieval_slots",
            "max_verifier_slots",
            "final_audit_reserve_bp",
            "exploration_reserve_bp",
            "deadline_utc",
            "on_deadline",
            "on_stagnation",
        }
        _reject_extra(payload, allowed, "BudgetPolicy")
        _require_keys(payload, set(RESOURCE_DIMENSIONS), "BudgetPolicy")
        kwargs: dict[str, Any] = {
            name: DimensionLimit.from_dict(payload[name], label=name)
            for name in RESOURCE_DIMENSIONS
        }
        for key in allowed - set(RESOURCE_DIMENSIONS):
            if key in payload:
                kwargs[key] = payload[key]
        return cls(**kwargs)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceSnapshot:
    """Immutable reserved/consumed vectors and slot occupancy."""

    reserved: ResourceVector
    consumed: ResourceVector
    reserved_normal: ResourceVector
    reserved_exploration: ResourceVector
    reserved_final_audit: ResourceVector
    consumed_normal: ResourceVector
    consumed_exploration: ResourceVector
    consumed_final_audit: ResourceVector
    reasoning_slots_in_use: int
    compute_slots_in_use: int
    retrieval_slots_in_use: int
    verifier_slots_in_use: int

    def __post_init__(self) -> None:
        for name in (
            "reserved",
            "consumed",
            "reserved_normal",
            "reserved_exploration",
            "reserved_final_audit",
            "consumed_normal",
            "consumed_exploration",
            "consumed_final_audit",
        ):
            _require_vector(name, getattr(self, name))
        object.__setattr__(
            self,
            "reasoning_slots_in_use",
            _require_nonneg_int("reasoning_slots_in_use", self.reasoning_slots_in_use),
        )
        object.__setattr__(
            self,
            "compute_slots_in_use",
            _require_nonneg_int("compute_slots_in_use", self.compute_slots_in_use),
        )
        object.__setattr__(
            self,
            "retrieval_slots_in_use",
            _require_nonneg_int("retrieval_slots_in_use", self.retrieval_slots_in_use),
        )
        object.__setattr__(
            self,
            "verifier_slots_in_use",
            _require_nonneg_int("verifier_slots_in_use", self.verifier_slots_in_use),
        )
        reserved_sum = (
            self.reserved_normal.add(self.reserved_exploration).add(
                self.reserved_final_audit
            )
        )
        consumed_sum = (
            self.consumed_normal.add(self.consumed_exploration).add(
                self.consumed_final_audit
            )
        )
        if reserved_sum != self.reserved:
            raise PolicyError("reserved vectors do not sum to reserved")
        if consumed_sum != self.consumed:
            raise PolicyError("consumed vectors do not sum to consumed")

    @classmethod
    def empty(cls) -> "ResourceSnapshot":
        zero = ResourceVector.zero()
        return cls(
            reserved=zero,
            consumed=zero,
            reserved_normal=zero,
            reserved_exploration=zero,
            reserved_final_audit=zero,
            consumed_normal=zero,
            consumed_exploration=zero,
            consumed_final_audit=zero,
            reasoning_slots_in_use=0,
            compute_slots_in_use=0,
            retrieval_slots_in_use=0,
            verifier_slots_in_use=0,
        )

    def used(self) -> ResourceVector:
        return self.reserved.add(self.consumed)

    def used_by_class(self, budget_class: BudgetClass) -> ResourceVector:
        reserved, consumed = self._class_vectors(budget_class)
        return reserved.add(consumed)

    def slots_in_use(self, kind: SlotKind) -> int:
        slot = _require_enum(SlotKind, kind, "slot_kind")
        if slot is SlotKind.REASONING:
            return self.reasoning_slots_in_use
        if slot is SlotKind.COMPUTE:
            return self.compute_slots_in_use
        if slot is SlotKind.RETRIEVAL:
            return self.retrieval_slots_in_use
        return self.verifier_slots_in_use

    def available(
        self,
        policy: BudgetPolicy,
        budget_class: BudgetClass,
        name: str,
    ) -> int | None:
        if not isinstance(policy, BudgetPolicy):
            raise PolicyError("policy must be a BudgetPolicy")
        klass = _require_enum(BudgetClass, budget_class, "budget_class")
        limit = policy.dimension(name)
        if limit.kind is ResourceLimitKind.UNLIMITED:
            return None
        total = int(limit.value or 0)
        fa_ex = _basis_share(total, policy.final_audit_reserve_bp)
        exp_ex = _basis_share(total, policy.exploration_reserve_bp)
        unreserved = total - fa_ex - exp_ex
        used_n = self.used_by_class(BudgetClass.NORMAL).get(name)
        used_e = self.used_by_class(BudgetClass.EXPLORATION).get(name)
        used_f = self.used_by_class(BudgetClass.FINAL_AUDIT).get(name)
        overflow_e = used_e - exp_ex if used_e > exp_ex else 0
        overflow_f = used_f - fa_ex if used_f > fa_ex else 0
        unreserved_used = used_n + overflow_e + overflow_f
        unreserved_left = unreserved - unreserved_used
        if unreserved_left < 0:
            unreserved_left = 0
        if klass is BudgetClass.NORMAL:
            return unreserved_left
        if klass is BudgetClass.EXPLORATION:
            exclusive_left = exp_ex - used_e if exp_ex > used_e else 0
            return exclusive_left + unreserved_left
        exclusive_left = fa_ex - used_f if fa_ex > used_f else 0
        return exclusive_left + unreserved_left

    def remaining_total(self, policy: BudgetPolicy, name: str) -> int | None:
        if not isinstance(policy, BudgetPolicy):
            raise PolicyError("policy must be a BudgetPolicy")
        limit = policy.dimension(name)
        if limit.kind is ResourceLimitKind.UNLIMITED:
            return None
        return int(limit.value or 0) - self.used().get(name)

    def can_reserve(
        self,
        requested: ResourceVector,
        *,
        budget_class: BudgetClass,
        slot_kind: SlotKind,
        policy: BudgetPolicy,
    ) -> bool:
        return self.reservation_block(
            requested,
            budget_class=budget_class,
            slot_kind=slot_kind,
            policy=policy,
        ) is None

    def reservation_block(
        self,
        requested: ResourceVector,
        *,
        budget_class: BudgetClass,
        slot_kind: SlotKind,
        policy: BudgetPolicy,
    ) -> str | None:
        """Return 'budget', 'reserve', or 'slot' when reservation is blocked."""
        _require_vector("requested", requested)
        if not isinstance(policy, BudgetPolicy):
            raise PolicyError("policy must be a BudgetPolicy")
        _require_enum(BudgetClass, budget_class, "budget_class")
        slot = _require_enum(SlotKind, slot_kind, "slot_kind")
        budget = False
        reserve = False
        for name in RESOURCE_DIMENSIONS:
            amount = requested.get(name)
            remaining = self.remaining_total(policy, name)
            if remaining is not None and amount > remaining:
                budget = True
            available = self.available(policy, budget_class, name)
            if available is not None and amount > available:
                reserve = True
        if budget:
            return "budget"
        if reserve:
            return "reserve"
        if self.slots_in_use(slot) + 1 > policy.max_slots(slot):
            return "slot"
        return None

    def reserve(
        self,
        requested: ResourceVector,
        *,
        budget_class: BudgetClass,
        slot_kind: SlotKind,
        policy: BudgetPolicy,
    ) -> "ResourceSnapshot":
        block = self.reservation_block(
            requested,
            budget_class=budget_class,
            slot_kind=slot_kind,
            policy=policy,
        )
        if block is not None:
            raise PolicyError(f"cannot reserve: {block}")
        reserved_field, consumed_field = _class_field_names(budget_class)
        slot_field = _slot_field_name(slot_kind)
        updates = {
            "reserved": self.reserved.add(requested),
            reserved_field: getattr(self, reserved_field).add(requested),
            slot_field: self.slots_in_use(slot_kind) + 1,
        }
        return _replace_snapshot(self, **updates)

    def settle(
        self,
        reserved: ResourceVector,
        usage: ResourceUsage,
        *,
        budget_class: BudgetClass,
        slot_kind: SlotKind,
    ) -> "ResourceSnapshot":
        _require_vector("reserved", reserved)
        if not isinstance(usage, ResourceUsage):
            raise PolicyError(f"usage must be ResourceUsage, got {type(usage).__name__}")
        reserved_field, consumed_field = _class_field_names(budget_class)
        current_reserved = getattr(self, reserved_field)
        try:
            new_class_reserved = current_reserved.sub(reserved)
        except PolicyError as exc:
            raise PolicyError("settle exceeds reserved quantity for class") from exc
        try:
            new_reserved = self.reserved.sub(reserved)
        except PolicyError as exc:
            raise PolicyError("settle exceeds reserved quantity") from exc
        settled = usage.settled_vector(reserved)
        slot_field = _slot_field_name(slot_kind)
        in_use = self.slots_in_use(slot_kind)
        if in_use < 1:
            raise PolicyError("settle requires a slot in use")
        return _replace_snapshot(
            self,
            reserved=new_reserved,
            consumed=self.consumed.add(settled),
            **{
                reserved_field: new_class_reserved,
                consumed_field: getattr(self, consumed_field).add(settled),
                slot_field: in_use - 1,
            },
        )

    def release(
        self,
        reserved: ResourceVector,
        *,
        budget_class: BudgetClass,
        slot_kind: SlotKind,
    ) -> "ResourceSnapshot":
        _require_vector("reserved", reserved)
        reserved_field, _consumed_field = _class_field_names(budget_class)
        try:
            new_class_reserved = getattr(self, reserved_field).sub(reserved)
            new_reserved = self.reserved.sub(reserved)
        except PolicyError as exc:
            raise PolicyError("release exceeds reserved quantity") from exc
        slot_field = _slot_field_name(slot_kind)
        in_use = self.slots_in_use(slot_kind)
        if in_use < 1:
            raise PolicyError("release requires a slot in use")
        return _replace_snapshot(
            self,
            reserved=new_reserved,
            **{
                reserved_field: new_class_reserved,
                slot_field: in_use - 1,
            },
        )

    def _class_vectors(self, budget_class: BudgetClass) -> tuple[ResourceVector, ResourceVector]:
        klass = _require_enum(BudgetClass, budget_class, "budget_class")
        if klass is BudgetClass.NORMAL:
            return self.reserved_normal, self.consumed_normal
        if klass is BudgetClass.EXPLORATION:
            return self.reserved_exploration, self.consumed_exploration
        return self.reserved_final_audit, self.consumed_final_audit

    def to_dict(self) -> dict[str, Any]:
        return {
            "reserved": self.reserved.to_dict(),
            "consumed": self.consumed.to_dict(),
            "reserved_normal": self.reserved_normal.to_dict(),
            "reserved_exploration": self.reserved_exploration.to_dict(),
            "reserved_final_audit": self.reserved_final_audit.to_dict(),
            "consumed_normal": self.consumed_normal.to_dict(),
            "consumed_exploration": self.consumed_exploration.to_dict(),
            "consumed_final_audit": self.consumed_final_audit.to_dict(),
            "reasoning_slots_in_use": self.reasoning_slots_in_use,
            "compute_slots_in_use": self.compute_slots_in_use,
            "retrieval_slots_in_use": self.retrieval_slots_in_use,
            "verifier_slots_in_use": self.verifier_slots_in_use,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ResourceSnapshot":
        payload = _mapping("ResourceSnapshot", data)
        allowed = {
            "reserved",
            "consumed",
            "reserved_normal",
            "reserved_exploration",
            "reserved_final_audit",
            "consumed_normal",
            "consumed_exploration",
            "consumed_final_audit",
            "reasoning_slots_in_use",
            "compute_slots_in_use",
            "retrieval_slots_in_use",
            "verifier_slots_in_use",
        }
        _reject_extra(payload, allowed, "ResourceSnapshot")
        _require_keys(payload, allowed, "ResourceSnapshot")
        return cls(
            reserved=ResourceVector.from_dict(payload["reserved"]),
            consumed=ResourceVector.from_dict(payload["consumed"]),
            reserved_normal=ResourceVector.from_dict(payload["reserved_normal"]),
            reserved_exploration=ResourceVector.from_dict(
                payload["reserved_exploration"]
            ),
            reserved_final_audit=ResourceVector.from_dict(
                payload["reserved_final_audit"]
            ),
            consumed_normal=ResourceVector.from_dict(payload["consumed_normal"]),
            consumed_exploration=ResourceVector.from_dict(
                payload["consumed_exploration"]
            ),
            consumed_final_audit=ResourceVector.from_dict(
                payload["consumed_final_audit"]
            ),
            reasoning_slots_in_use=payload["reasoning_slots_in_use"],
            compute_slots_in_use=payload["compute_slots_in_use"],
            retrieval_slots_in_use=payload["retrieval_slots_in_use"],
            verifier_slots_in_use=payload["verifier_slots_in_use"],
        )


def _settled_amount(qty: ObservedQuantity, reserved: int) -> int:
    if qty.status is ObservationStatus.UNKNOWN:
        return reserved
    if qty.status is ObservationStatus.OBSERVED:
        return _require_nonneg_int("observed value", qty.value)
    raise PolicyError(f"unsupported observation status {qty.status!r}")


def _replace_snapshot(snapshot: ResourceSnapshot, **changes: object) -> ResourceSnapshot:
    payload = snapshot.to_dict()
    for key, value in changes.items():
        if isinstance(value, ResourceVector):
            payload[key] = value.to_dict()
        else:
            payload[key] = value
    return ResourceSnapshot.from_dict(payload)


def _class_field_names(budget_class: BudgetClass) -> tuple[str, str]:
    klass = _require_enum(BudgetClass, budget_class, "budget_class")
    if klass is BudgetClass.NORMAL:
        return "reserved_normal", "consumed_normal"
    if klass is BudgetClass.EXPLORATION:
        return "reserved_exploration", "consumed_exploration"
    return "reserved_final_audit", "consumed_final_audit"


def _slot_field_name(slot_kind: SlotKind) -> str:
    slot = _require_enum(SlotKind, slot_kind, "slot_kind")
    if slot is SlotKind.REASONING:
        return "reasoning_slots_in_use"
    if slot is SlotKind.COMPUTE:
        return "compute_slots_in_use"
    if slot is SlotKind.RETRIEVAL:
        return "retrieval_slots_in_use"
    return "verifier_slots_in_use"


def _basis_share(total: int, bp: int) -> int:
    return (total * bp) // _BASIS_POINT_MAX


def _require_dimension(name: object) -> str:
    if name not in RESOURCE_DIMENSIONS:
        raise PolicyError(f"unknown resource dimension {name!r}")
    return str(name)


def _require_vector(name: str, value: object) -> ResourceVector:
    if not isinstance(value, ResourceVector):
        raise PolicyError(f"{name} must be a ResourceVector")
    return value


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{name} must be a nonnegative int, not {type(value).__name__}")
    if value < 0:
        raise PolicyError(f"{name} must be a nonnegative int")
    return value


def _require_basis_points(name: str, value: object) -> int:
    amount = _require_nonneg_int(name, value)
    if amount > _BASIS_POINT_MAX:
        raise PolicyError(f"{name} must be at most 10000 basis points")
    return amount


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise PolicyError(f"{name} has unknown value {value!r}") from None
    raise PolicyError(f"{name} must be {enum_cls.__name__}")


def _parse_utc(name: str, value: object) -> datetime:
    if not isinstance(value, str) or value == "":
        raise PolicyError(f"{name} must be ISO-8601 UTC")
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise PolicyError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise PolicyError(f"{name} must be UTC")
    return dt.astimezone(_UTC)


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise PolicyError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise PolicyError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise PolicyError(f"{label} missing fields: {sorted(missing)}")
