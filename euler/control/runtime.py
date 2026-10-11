"""Idempotent runtime leases over EventStore, BudgetLedger, and ArtifactStore.

Stdlib only. Issue/complete/fail/cancel/expire are same-transaction and never
launch a provider, mutate ResearchView, or write Fact Graph / gateway / verify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from euler.control.artifacts import ArtifactStoreError
from euler.control.budget import BudgetError, ReservationConflict
from euler.control.event_store import EventStoreError, IdempotencyConflict
from euler.control.ids import stable_id
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    PolicyError,
    RESOURCE_DIMENSIONS,
    ResourceSnapshot,
    ResourceUsage,
    ResourceVector,
    SlotKind,
)
from euler.control.progress import ProgressReceipt, validate_progress_against_view
from euler.control.scheduler import ActionKind, CandidateAction
from euler.control.schema import (
    EventEnvelope,
    FactCommitStatus,
    InfrastructureStatus,
    MathematicalStatus,
    ObservationStatus,
    ObservedQuantity,
    SchemaValidationError,
    UsageRecord,
)
from euler.control.views import ResearchView

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LEASE_DOMAIN = "lease"
_ATTEMPT_DOMAIN = "attempt"
_ID_VERSION = 1
_DEFAULT_ACTOR = "runtime"
_EVENT_LEASE_ISSUED = "runtime.lease_issued"
_EVENT_ATTEMPT_STARTED = "runtime.attempt_started"
_EVENT_LEASE_RUNNING = "runtime.lease_running"
_EVENT_ATTEMPT_FINISHED = "runtime.attempt_finished"
_EVENT_LEASE_COMPLETED = "runtime.lease_completed"
_EVENT_LEASE_INFRA_FAILED = "runtime.lease_infra_failed"
_EVENT_LEASE_CANCELLED = "runtime.lease_cancelled"
_EVENT_LEASE_EXPIRED = "runtime.lease_expired"
_EVENT_PROGRESS = "runtime.progress_recorded"
_RUNTIME_EVENTS = frozenset(
    {
        _EVENT_LEASE_ISSUED,
        _EVENT_ATTEMPT_STARTED,
        _EVENT_LEASE_RUNNING,
        _EVENT_ATTEMPT_FINISHED,
        _EVENT_LEASE_COMPLETED,
        _EVENT_LEASE_INFRA_FAILED,
        _EVENT_LEASE_CANCELLED,
        _EVENT_LEASE_EXPIRED,
        _EVENT_PROGRESS,
    }
)
_ACTIVE_LEASE = frozenset({"ISSUED", "RUNNING"})
_USAGE_UNITS = {
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
    "RuntimeError",
    "LeaseConflict",
    "LeaseStatus",
    "ResourceDisposition",
    "Lease",
    "RuntimeSnapshot",
    "RuntimeCoordinator",
]


class RuntimeError(Exception):
    """Malformed runtime input, illegal history, or a rolled-back control-plane write."""


class LeaseConflict(RuntimeError):
    """Idempotency key or terminal replay reused with a different payload."""


class LeaseStatus(str, Enum):
    ISSUED = "ISSUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INFRA_FAILED = "INFRA_FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ResourceDisposition(str, Enum):
    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True, kw_only=True)
class Lease:
    """Control-plane lease. Infra and math statuses are independent of truth."""

    lease_id: str
    project_id: str
    candidate_id: str
    action_id: str
    attempt_id: str
    reservation_id: str
    idempotency_key: str
    slot_kind: SlotKind
    budget_class: BudgetClass
    action_kind: ActionKind
    status: LeaseStatus
    infrastructure_status: InfrastructureStatus
    mathematical_status: MathematicalStatus
    resource_disposition: ResourceDisposition
    issued_at: str
    action_type: str
    model_id: str
    effort: str
    mechanism: str
    contract_id: str
    context_seed_hash: str
    requested: ResourceVector
    consumed: ResourceVector
    context_cache_key: str | None = None
    started_at: str | None = None
    running_at: str | None = None
    ended_at: str | None = None
    artifact_refs: tuple[str, ...] = ()
    obligation_id: str | None = None
    route_id: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_hex64("lease_id", self.lease_id)
        _require_nonempty_str("project_id", self.project_id)
        _require_nonempty_str("candidate_id", self.candidate_id)
        _require_hex64("action_id", self.action_id)
        _require_hex64("attempt_id", self.attempt_id)
        _require_hex64("reservation_id", self.reservation_id)
        _require_nonempty_str("idempotency_key", self.idempotency_key)
        if not isinstance(self.slot_kind, SlotKind):
            raise RuntimeError("slot_kind must be a SlotKind")
        if not isinstance(self.budget_class, BudgetClass):
            raise RuntimeError("budget_class must be a BudgetClass")
        if not isinstance(self.action_kind, ActionKind):
            raise RuntimeError("action_kind must be an ActionKind")
        if not isinstance(self.status, LeaseStatus):
            raise RuntimeError("status must be a LeaseStatus")
        if not isinstance(self.infrastructure_status, InfrastructureStatus):
            raise RuntimeError("infrastructure_status must be an InfrastructureStatus")
        if not isinstance(self.mathematical_status, MathematicalStatus):
            raise RuntimeError("mathematical_status must be a MathematicalStatus")
        if not isinstance(self.resource_disposition, ResourceDisposition):
            raise RuntimeError("resource_disposition must be a ResourceDisposition")
        _require_nonempty_str("issued_at", self.issued_at)
        _require_nonempty_str("action_type", self.action_type)
        _require_nonempty_str("model_id", self.model_id)
        _require_nonempty_str("effort", self.effort)
        _require_nonempty_str("mechanism", self.mechanism)
        _require_hex64("contract_id", self.contract_id)
        _require_hex64("context_seed_hash", self.context_seed_hash)
        if not isinstance(self.requested, ResourceVector):
            raise RuntimeError("requested must be a ResourceVector")
        if not isinstance(self.consumed, ResourceVector):
            raise RuntimeError("consumed must be a ResourceVector")
        if self.context_cache_key is not None:
            _require_hex64("context_cache_key", self.context_cache_key)
        if self.obligation_id is not None:
            _require_nonempty_str("obligation_id", self.obligation_id)
        if self.route_id is not None:
            _require_nonempty_str("route_id", self.route_id)
        for index, ref in enumerate(self.artifact_refs):
            _require_hex64(f"artifact_refs[{index}]", ref)
        if self.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING} and self.ended_at is not None:
            raise RuntimeError(f"{self.status.value} lease forbids ended_at")
        if self.status.value not in _ACTIVE_LEASE and self.ended_at is None:
            raise RuntimeError(f"{self.status.value} lease requires ended_at")
        if self.status.value in _ACTIVE_LEASE:
            if self.resource_disposition is not ResourceDisposition.RESERVED:
                raise RuntimeError("active lease must have RESERVED resources")
        elif self.status is LeaseStatus.CANCELLED and self.running_at is None:
            if self.resource_disposition is not ResourceDisposition.RELEASED:
                raise RuntimeError("pre-start cancel must RELEASE resources")
        elif self.resource_disposition is not ResourceDisposition.SETTLED:
            raise RuntimeError(f"{self.status.value} must SETTLE resources")

    def requested_eight(self) -> dict[str, int]:
        return self.requested.to_dict()

    def consumed_eight(self) -> dict[str, int]:
        return self.consumed.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_kind": self.action_kind.value,
            "action_type": self.action_type,
            "artifact_refs": list(self.artifact_refs),
            "attempt_id": self.attempt_id,
            "budget_class": self.budget_class.value,
            "candidate_id": self.candidate_id,
            "consumed": self.consumed.to_dict(),
            "context_cache_key": self.context_cache_key,
            "context_seed_hash": self.context_seed_hash,
            "contract_id": self.contract_id,
            "effort": self.effort,
            "ended_at": self.ended_at,
            "idempotency_key": self.idempotency_key,
            "infrastructure_status": self.infrastructure_status.value,
            "issued_at": self.issued_at,
            "lease_id": self.lease_id,
            "mathematical_status": self.mathematical_status.value,
            "mechanism": self.mechanism,
            "model_id": self.model_id,
            "obligation_id": self.obligation_id,
            "project_id": self.project_id,
            "requested": self.requested.to_dict(),
            "reservation_id": self.reservation_id,
            "resource_disposition": self.resource_disposition.value,
            "route_id": self.route_id,
            "running_at": self.running_at,
            "slot_kind": self.slot_kind.value,
            "started_at": self.started_at,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "Lease":
        payload = _mapping("Lease", data)
        _reject_extra(payload, _LEASE_DICT_KEYS, "Lease")
        _require_keys(payload, _LEASE_DICT_KEYS, "Lease")
        refs = payload["artifact_refs"]
        if isinstance(refs, str) or not isinstance(refs, (list, tuple)):
            raise RuntimeError("artifact_refs must be a list of hashes")
        return cls(
            lease_id=payload["lease_id"],
            project_id=payload["project_id"],
            candidate_id=payload["candidate_id"],
            action_id=payload["action_id"],
            attempt_id=payload["attempt_id"],
            reservation_id=payload["reservation_id"],
            idempotency_key=payload["idempotency_key"],
            slot_kind=SlotKind(payload["slot_kind"]),
            budget_class=BudgetClass(payload["budget_class"]),
            action_kind=ActionKind(payload["action_kind"]),
            status=LeaseStatus(payload["status"]),
            infrastructure_status=InfrastructureStatus(payload["infrastructure_status"]),
            mathematical_status=MathematicalStatus(payload["mathematical_status"]),
            resource_disposition=ResourceDisposition(payload["resource_disposition"]),
            issued_at=payload["issued_at"],
            action_type=payload["action_type"],
            model_id=payload["model_id"],
            effort=payload["effort"],
            mechanism=payload["mechanism"],
            contract_id=payload["contract_id"],
            context_seed_hash=payload["context_seed_hash"],
            requested=ResourceVector.from_dict(payload["requested"]),
            consumed=ResourceVector.from_dict(payload["consumed"]),
            context_cache_key=payload["context_cache_key"],
            started_at=payload["started_at"],
            running_at=payload["running_at"],
            ended_at=payload["ended_at"],
            artifact_refs=tuple(refs),
            obligation_id=payload["obligation_id"],
            route_id=payload["route_id"],
        )


_LEASE_DICT_KEYS = frozenset(
    {
        "action_id",
        "action_kind",
        "action_type",
        "artifact_refs",
        "attempt_id",
        "budget_class",
        "candidate_id",
        "consumed",
        "context_cache_key",
        "context_seed_hash",
        "contract_id",
        "effort",
        "ended_at",
        "idempotency_key",
        "infrastructure_status",
        "issued_at",
        "lease_id",
        "mathematical_status",
        "mechanism",
        "model_id",
        "obligation_id",
        "project_id",
        "requested",
        "reservation_id",
        "resource_disposition",
        "route_id",
        "running_at",
        "slot_kind",
        "started_at",
        "status",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeSnapshot:
    """Rebuilt runtime view: leases and a full policy ResourceSnapshot."""

    project_id: str
    leases: tuple[Lease, ...]
    active_lease_ids: tuple[str, ...]
    resources: ResourceSnapshot
    verifier_slots_in_use: int

    def __post_init__(self) -> None:
        _require_nonempty_str("project_id", self.project_id)
        if not isinstance(self.leases, tuple):
            raise RuntimeError("leases must be a tuple")
        for item in self.leases:
            if not isinstance(item, Lease):
                raise RuntimeError("leases must contain Lease values")
            item.validate()
        if not isinstance(self.resources, ResourceSnapshot):
            raise RuntimeError("resources must be a ResourceSnapshot")
        _require_nonneg_int("verifier_slots_in_use", self.verifier_slots_in_use)
        if self.verifier_slots_in_use != self.resources.verifier_slots_in_use:
            raise RuntimeError(
                "verifier_slots_in_use must equal resources.verifier_slots_in_use"
            )


class RuntimeCoordinator:
    """Issue and reconcile leases. Never calls a model or mutates ResearchView."""

    def __init__(
        self,
        policy: object,
        store: object,
        budget: object,
        artifacts: object,
    ) -> None:
        if not isinstance(policy, BudgetPolicy):
            raise RuntimeError("RuntimeCoordinator requires a BudgetPolicy")
        if not hasattr(store, "append") or not hasattr(store, "transaction"):
            raise RuntimeError("RuntimeCoordinator requires an EventStore")
        if not hasattr(budget, "reserve") or not hasattr(budget, "settle"):
            raise RuntimeError("RuntimeCoordinator requires a BudgetLedger")
        if not hasattr(artifacts, "verify"):
            raise RuntimeError("RuntimeCoordinator requires an ArtifactStore")
        self._policy = policy
        self._store = store
        self._budget = budget
        self._artifacts = artifacts

    def issue(
        self,
        project_id: str,
        candidate: object,
        *,
        idempotency_key: str,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
        context_cache_key: str | None = None,
    ) -> Lease:
        project_id = _require_nonempty_str("project_id", project_id)
        if not isinstance(candidate, CandidateAction):
            raise RuntimeError("issue requires a CandidateAction")
        idempotency_key = _require_nonempty_str("idempotency_key", idempotency_key)
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        if context_cache_key is not None:
            context_cache_key = _require_hex64("context_cache_key", context_cache_key)
        lease_id = stable_id(
            _LEASE_DOMAIN,
            _ID_VERSION,
            project_id,
            candidate.candidate_id,
            idempotency_key,
        )
        attempt_id = stable_id(_ATTEMPT_DOMAIN, _ID_VERSION, project_id, lease_id)
        reserve_key = f"lease-reserve:{lease_id}"
        try:
            with self._store.transaction(immediate=True):
                existing = _find_issued_lease(self._store, project_id, idempotency_key)
                if existing is not None:
                    _assert_issue_identity(
                        existing,
                        project_id=project_id,
                        candidate=candidate,
                        context_cache_key=context_cache_key,
                    )
                    return existing
                snapshot = self.replay(project_id)
                block = snapshot.resources.reservation_block(
                    candidate.requested,
                    budget_class=candidate.budget_class,
                    slot_kind=candidate.slot_kind,
                    policy=self._policy,
                )
                if block is not None:
                    raise RuntimeError(f"cannot issue: {block}")
                reservation = self._budget.reserve(
                    project_id,
                    attempt_id,
                    reserve_key,
                    candidate.requested.cost_microusd,
                    candidate.requested.input_tokens,
                    candidate.requested.output_tokens,
                    actor=actor,
                    timestamp_utc=ts,
                )
                lease = Lease(
                    lease_id=lease_id,
                    project_id=project_id,
                    candidate_id=candidate.candidate_id,
                    action_id=candidate.action_spec.action_id,
                    attempt_id=attempt_id,
                    reservation_id=reservation.reservation_id,
                    idempotency_key=idempotency_key,
                    slot_kind=candidate.slot_kind,
                    budget_class=candidate.budget_class,
                    action_kind=candidate.action_kind,
                    status=LeaseStatus.ISSUED,
                    infrastructure_status=InfrastructureStatus.PENDING,
                    mathematical_status=MathematicalStatus.NO_CLAIM,
                    resource_disposition=ResourceDisposition.RESERVED,
                    issued_at=ts,
                    started_at=ts,
                    action_type=candidate.action_spec.action_type,
                    model_id=candidate.action_spec.model_id,
                    effort=candidate.action_spec.effort,
                    mechanism=candidate.mechanism_family,
                    contract_id=candidate.context_seed.contract_id,
                    context_seed_hash=candidate.context_seed.seed_hash,
                    context_cache_key=context_cache_key,
                    requested=candidate.requested,
                    consumed=ResourceVector.zero(),
                    obligation_id=candidate.obligation_id,
                    route_id=candidate.route_id,
                )
                issued = self._append(
                    project_id=project_id,
                    actor=actor,
                    event_type=_EVENT_LEASE_ISSUED,
                    idempotency_key=idempotency_key,
                    payload={"lease": lease.to_dict()},
                    timestamp_utc=ts,
                    correlation_id=lease_id,
                )
                self._append(
                    project_id=project_id,
                    actor=actor,
                    event_type=_EVENT_ATTEMPT_STARTED,
                    idempotency_key=f"attempt-started:{idempotency_key}",
                    payload={
                        "action_id": lease.action_id,
                        "attempt_id": lease.attempt_id,
                        "lease_id": lease.lease_id,
                        "reservation_id": lease.reservation_id,
                    },
                    timestamp_utc=ts,
                    correlation_id=lease_id,
                    causation_id=issued.event_id,
                )
                return lease
        except LeaseConflict:
            raise
        except IdempotencyConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except ReservationConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except (BudgetError, EventStoreError, SchemaValidationError, PolicyError) as exc:
            raise RuntimeError(str(exc)) from exc

    def mark_running(
        self,
        project_id: str,
        lease_id: str,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> Lease:
        return self._transition(
            project_id,
            lease_id,
            actor=actor,
            timestamp_utc=timestamp_utc,
            event_type=_EVENT_LEASE_RUNNING,
            idempotency_prefix="lease-running",
            mutate=_to_running,
        )

    def complete(
        self,
        project_id: str,
        lease_id: str,
        *,
        usage: UsageRecord,
        artifact_refs: object = (),
        mathematical_status: object = MathematicalStatus.NO_CLAIM,
        progress: ProgressReceipt | None = None,
        view: ResearchView | None = None,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> Lease:
        if not isinstance(usage, UsageRecord):
            raise RuntimeError("complete requires a UsageRecord")
        refs = _artifact_ref_tuple(artifact_refs)
        math_status = _require_math(mathematical_status)
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        project_id = _require_nonempty_str("project_id", project_id)
        lease_id = _require_hex64("lease_id", lease_id)
        try:
            with self._store.transaction(immediate=True):
                current = self._require_lease(project_id, lease_id)
                self._verify_artifacts(refs)
                consumed_vec = _consumed_from_usage(current, usage)
                updated = replace(
                    current,
                    status=LeaseStatus.COMPLETED,
                    infrastructure_status=InfrastructureStatus.SUCCEEDED,
                    mathematical_status=math_status,
                    resource_disposition=ResourceDisposition.SETTLED,
                    ended_at=ts,
                    artifact_refs=refs,
                    consumed=consumed_vec,
                )
                if current.status is LeaseStatus.COMPLETED:
                    _assert_same_terminal(
                        current,
                        replace(updated, ended_at=current.ended_at, running_at=current.running_at),
                    )
                    return current
                _require_active(current, "complete")
                if progress is not None:
                    _validate_progress(progress, updated, view)
                self._budget.settle(
                    current.reservation_id, usage, actor=actor, timestamp_utc=ts
                )
                finished = self._append(
                    project_id=project_id,
                    actor=actor,
                    event_type=_EVENT_ATTEMPT_FINISHED,
                    idempotency_key=f"attempt-finished:{lease_id}",
                    payload={
                        "attempt_id": current.attempt_id,
                        "fact_commit_status": FactCommitStatus.NOT_APPLICABLE.value,
                        "lease": updated.to_dict(),
                        "usage": usage.to_dict(),
                    },
                    timestamp_utc=ts,
                    correlation_id=lease_id,
                )
                self._append(
                    project_id=project_id,
                    actor=actor,
                    event_type=_EVENT_LEASE_COMPLETED,
                    idempotency_key=f"lease-completed:{lease_id}",
                    payload={
                        "lease": updated.to_dict(),
                        "resources": self._resources_after(project_id, updated).to_dict(),
                        "usage": usage.to_dict(),
                    },
                    timestamp_utc=ts,
                    correlation_id=lease_id,
                    causation_id=finished.event_id,
                )
                if progress is not None:
                    self._append(
                        project_id=project_id,
                        actor=actor,
                        event_type=_EVENT_PROGRESS,
                        idempotency_key=f"progress:{lease_id}",
                        payload={"lease_id": lease_id, "progress": progress.to_dict()},
                        timestamp_utc=ts,
                        correlation_id=lease_id,
                    )
                return updated
        except LeaseConflict:
            raise
        except IdempotencyConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except ReservationConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except (
            BudgetError,
            EventStoreError,
            ArtifactStoreError,
            SchemaValidationError,
            PolicyError,
        ) as exc:
            raise RuntimeError(str(exc)) from exc

    def fail_infrastructure(
        self,
        project_id: str,
        lease_id: str,
        *,
        usage: UsageRecord | None = None,
        artifact_refs: object = None,
        reason: str = "infrastructure failure",
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> Lease:
        return self._settle_terminal(
            project_id,
            lease_id,
            usage=usage,
            artifact_refs=artifact_refs,
            actor=actor,
            timestamp_utc=timestamp_utc,
            status=LeaseStatus.INFRA_FAILED,
            infrastructure_status=InfrastructureStatus.FAILED,
            event_type=_EVENT_LEASE_INFRA_FAILED,
            idempotency_prefix="lease-infra-failed",
            extra_payload={"reason": _require_nonempty_str("reason", reason)},
        )

    def cancel(
        self,
        project_id: str,
        lease_id: str,
        *,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> Lease:
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        project_id = _require_nonempty_str("project_id", project_id)
        lease_id = _require_hex64("lease_id", lease_id)
        try:
            with self._store.transaction(immediate=True):
                current = self._require_lease(project_id, lease_id)
                if current.status is LeaseStatus.CANCELLED:
                    return current
                if current.status is LeaseStatus.RUNNING or current.running_at is not None:
                    return self._settle_terminal_locked(
                        current,
                        usage=None,
                        artifact_refs=current.artifact_refs,
                        actor=actor,
                        timestamp_utc=ts,
                        status=LeaseStatus.CANCELLED,
                        infrastructure_status=InfrastructureStatus.CANCELLED,
                        event_type=_EVENT_LEASE_CANCELLED,
                        idempotency_prefix="lease-cancelled",
                        extra_payload={},
                    )
                updated = replace(
                    current,
                    status=LeaseStatus.CANCELLED,
                    infrastructure_status=InfrastructureStatus.CANCELLED,
                    resource_disposition=ResourceDisposition.RELEASED,
                    ended_at=ts,
                    consumed=ResourceVector.zero(),
                )
                if current.status is LeaseStatus.CANCELLED:
                    _assert_same_terminal(
                        current,
                        replace(updated, ended_at=current.ended_at, running_at=current.running_at),
                    )
                    return current
                _require_active(current, "cancel")
                self._budget.release(
                    current.reservation_id, actor=actor, timestamp_utc=ts
                )
                self._append(
                    project_id=project_id,
                    actor=actor,
                    event_type=_EVENT_LEASE_CANCELLED,
                    idempotency_key=f"lease-cancelled:{lease_id}",
                    payload={
                        "lease": updated.to_dict(),
                        "resources": self._resources_after(project_id, updated).to_dict(),
                    },
                    timestamp_utc=ts,
                    correlation_id=lease_id,
                )
                return updated
        except LeaseConflict:
            raise
        except IdempotencyConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except (BudgetError, EventStoreError, SchemaValidationError, PolicyError) as exc:
            raise RuntimeError(str(exc)) from exc

    def expire(
        self,
        project_id: str,
        lease_id: str,
        *,
        usage: UsageRecord | None = None,
        actor: str = _DEFAULT_ACTOR,
        timestamp_utc: str | None = None,
    ) -> Lease:
        return self._settle_terminal(
            project_id,
            lease_id,
            usage=usage,
            artifact_refs=None,
            actor=actor,
            timestamp_utc=timestamp_utc,
            status=LeaseStatus.EXPIRED,
            infrastructure_status=InfrastructureStatus.TIMED_OUT,
            event_type=_EVENT_LEASE_EXPIRED,
            idempotency_prefix="lease-expired",
            extra_payload={},
        )

    def replay(self, project_id: str, after_seq: int = 0) -> RuntimeSnapshot:
        project_id = _require_nonempty_str("project_id", project_id)
        if after_seq != 0:
            raise RuntimeError("replay requires after_seq=0")
        leases: dict[str, Lease] = {}
        started: set[str] = set()
        try:
            for _seq, envelope in self._store.iter_project(project_id, after_seq=0):
                _apply_runtime_event(leases, started, envelope)
        except LeaseConflict:
            raise
        except (EventStoreError, SchemaValidationError, ValueError, KeyError, TypeError, PolicyError) as exc:
            raise RuntimeError(f"malformed runtime history: {exc}") from exc
        ordered = tuple(sorted(leases.values(), key=lambda item: item.lease_id))
        active = tuple(item for item in ordered if item.status.value in _ACTIVE_LEASE)
        resources = _resources_from_leases(ordered, self._policy)
        return RuntimeSnapshot(
            project_id=project_id,
            leases=ordered,
            active_lease_ids=tuple(item.lease_id for item in active),
            resources=resources,
            verifier_slots_in_use=resources.verifier_slots_in_use,
        )

    def _settle_terminal(
        self,
        project_id: str,
        lease_id: str,
        *,
        usage: UsageRecord | None,
        artifact_refs: object,
        actor: str,
        timestamp_utc: str | None,
        status: LeaseStatus,
        infrastructure_status: InfrastructureStatus,
        event_type: str,
        idempotency_prefix: str,
        extra_payload: dict[str, Any],
    ) -> Lease:
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        project_id = _require_nonempty_str("project_id", project_id)
        lease_id = _require_hex64("lease_id", lease_id)
        try:
            with self._store.transaction(immediate=True):
                current = self._require_lease(project_id, lease_id)
                refs = current.artifact_refs if artifact_refs is None else _artifact_ref_tuple(artifact_refs)
                return self._settle_terminal_locked(
                    current,
                    usage=usage,
                    artifact_refs=refs,
                    actor=actor,
                    timestamp_utc=ts,
                    status=status,
                    infrastructure_status=infrastructure_status,
                    event_type=event_type,
                    idempotency_prefix=idempotency_prefix,
                    extra_payload=extra_payload,
                )
        except LeaseConflict:
            raise
        except IdempotencyConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except ReservationConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except (
            BudgetError,
            EventStoreError,
            ArtifactStoreError,
            SchemaValidationError,
            PolicyError,
        ) as exc:
            raise RuntimeError(str(exc)) from exc

    def _settle_terminal_locked(
        self,
        current: Lease,
        *,
        usage: UsageRecord | None,
        artifact_refs: tuple[str, ...],
        actor: str,
        timestamp_utc: str,
        status: LeaseStatus,
        infrastructure_status: InfrastructureStatus,
        event_type: str,
        idempotency_prefix: str,
        extra_payload: dict[str, Any],
    ) -> Lease:
        if artifact_refs:
            self._verify_artifacts(artifact_refs)
        record = usage if usage is not None else _unknown_usage_record()
        consumed_vec = _consumed_from_usage(current, record)
        updated = replace(
            current,
            status=status,
            infrastructure_status=infrastructure_status,
            resource_disposition=ResourceDisposition.SETTLED,
            ended_at=timestamp_utc,
            artifact_refs=artifact_refs,
            consumed=consumed_vec,
        )
        if current.status is status:
            _assert_same_terminal(
                current,
                replace(updated, ended_at=current.ended_at, running_at=current.running_at),
            )
            return current
        _require_active(current, status.value.lower())
        self._budget.settle(
            current.reservation_id, record, actor=actor, timestamp_utc=timestamp_utc
        )
        payload = {
            "lease": updated.to_dict(),
            "resources": self._resources_after(current.project_id, updated).to_dict(),
            "usage": record.to_dict(),
        }
        payload.update(extra_payload)
        self._append(
            project_id=current.project_id,
            actor=actor,
            event_type=event_type,
            idempotency_key=f"{idempotency_prefix}:{current.lease_id}",
            payload=payload,
            timestamp_utc=timestamp_utc,
            correlation_id=current.lease_id,
        )
        return updated

    def _transition(
        self,
        project_id: str,
        lease_id: str,
        *,
        actor: str,
        timestamp_utc: str | None,
        event_type: str,
        idempotency_prefix: str,
        mutate: Any,
    ) -> Lease:
        actor = _require_nonempty_str("actor", actor)
        ts = timestamp_utc if timestamp_utc is not None else _utc_now()
        project_id = _require_nonempty_str("project_id", project_id)
        lease_id = _require_hex64("lease_id", lease_id)
        try:
            with self._store.transaction(immediate=True):
                current = self._require_lease(project_id, lease_id)
                updated = mutate(current, ts)
                if current.status is updated.status:
                    return current
                self._append(
                    project_id=project_id,
                    actor=actor,
                    event_type=event_type,
                    idempotency_key=f"{idempotency_prefix}:{lease_id}",
                    payload={"lease": updated.to_dict()},
                    timestamp_utc=ts,
                    correlation_id=lease_id,
                )
                return updated
        except LeaseConflict:
            raise
        except IdempotencyConflict as exc:
            raise LeaseConflict(str(exc)) from exc
        except (BudgetError, EventStoreError, SchemaValidationError) as exc:
            raise RuntimeError(str(exc)) from exc

    def _append(
        self,
        *,
        project_id: str,
        actor: str,
        event_type: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        timestamp_utc: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        envelope = EventEnvelope.build(
            project_id=project_id,
            actor=actor,
            event_type=event_type,
            idempotency_key=idempotency_key,
            payload=dict(payload),
            timestamp_utc=timestamp_utc,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return self._store.append(envelope).event

    def _require_lease(self, project_id: str, lease_id: str) -> Lease:
        snapshot = self.replay(project_id)
        for item in snapshot.leases:
            if item.lease_id == lease_id:
                return item
        raise RuntimeError(f"unknown lease {lease_id!r}")

    def _verify_artifacts(self, refs: tuple[str, ...]) -> None:
        for content_hash in refs:
            self._artifacts.verify(content_hash)

    def _resources_after(self, project_id: str, updated: Lease) -> ResourceSnapshot:
        current = {item.lease_id: item for item in self.replay(project_id).leases}
        current[updated.lease_id] = updated
        ordered = tuple(sorted(current.values(), key=lambda item: item.lease_id))
        return _resources_from_leases(ordered, self._policy)


def _to_running(current: Lease, ts: str) -> Lease:
    if current.status is LeaseStatus.RUNNING:
        return current
    if current.status is not LeaseStatus.ISSUED:
        raise LeaseConflict(
            f"cannot mark_running lease {current.lease_id} in status {current.status.value}"
        )
    return replace(
        current,
        status=LeaseStatus.RUNNING,
        infrastructure_status=InfrastructureStatus.RUNNING,
        running_at=ts,
    )


def _require_active(lease: Lease, action: str) -> None:
    if lease.status.value not in _ACTIVE_LEASE:
        raise LeaseConflict(
            f"cannot {action} lease {lease.lease_id} in status {lease.status.value}"
        )


def _assert_issue_identity(
    existing: Lease,
    *,
    project_id: str,
    candidate: CandidateAction,
    context_cache_key: str | None,
) -> None:
    """Fail closed if a reused idempotency key does not match issue semantics."""
    checks = (
        ("project_id", existing.project_id, project_id),
        ("candidate_id", existing.candidate_id, candidate.candidate_id),
        ("action_id", existing.action_id, candidate.action_spec.action_id),
        ("slot_kind", existing.slot_kind, candidate.slot_kind),
        ("budget_class", existing.budget_class, candidate.budget_class),
        ("action_kind", existing.action_kind, candidate.action_kind),
        ("action_type", existing.action_type, candidate.action_spec.action_type),
        ("model_id", existing.model_id, candidate.action_spec.model_id),
        ("effort", existing.effort, candidate.action_spec.effort),
        ("mechanism", existing.mechanism, candidate.mechanism_family),
        ("contract_id", existing.contract_id, candidate.context_seed.contract_id),
        (
            "context_seed_hash",
            existing.context_seed_hash,
            candidate.context_seed.seed_hash,
        ),
        ("context_cache_key", existing.context_cache_key, context_cache_key),
        ("requested", existing.requested, candidate.requested),
        ("obligation_id", existing.obligation_id, candidate.obligation_id),
        ("route_id", existing.route_id, candidate.route_id),
    )
    for name, left, right in checks:
        if left != right:
            raise LeaseConflict(
                f"idempotency key reused with different {name}"
            )


def _assert_same_terminal(current: Lease, updated: Lease) -> None:
    if current.to_dict() != updated.to_dict():
        raise LeaseConflict(
            f"conflicting replay for lease {current.lease_id} in status {current.status.value}"
        )


def _find_issued_lease(
    store: object, project_id: str, idempotency_key: str
) -> Lease | None:
    for _seq, envelope in store.iter_project(project_id):
        if (
            envelope.event_type == _EVENT_LEASE_ISSUED
            and envelope.idempotency_key == idempotency_key
        ):
            payload = _mapping("runtime.lease_issued", envelope.payload)
            return Lease.from_dict(payload["lease"])
    return None


def _apply_runtime_event(
    leases: dict[str, Lease], started: set[str], envelope: EventEnvelope
) -> None:
    event_type = envelope.event_type
    if not event_type.startswith("runtime."):
        return
    if event_type not in _RUNTIME_EVENTS:
        raise RuntimeError(f"unknown runtime event {event_type!r}")
    payload = _mapping(event_type, envelope.payload)
    if event_type == _EVENT_LEASE_ISSUED:
        _require_keys(payload, {"lease"}, event_type)
        lease = Lease.from_dict(payload["lease"])
        existing = leases.get(lease.lease_id)
        if existing is not None:
            _assert_same_terminal(existing, lease)
            return
        if lease.status is not LeaseStatus.ISSUED:
            raise RuntimeError("lease_issued payload must have status ISSUED")
        leases[lease.lease_id] = lease
        return
    if event_type == _EVENT_ATTEMPT_STARTED:
        _require_keys(
            payload, {"action_id", "attempt_id", "lease_id", "reservation_id"}, event_type
        )
        lease_id = _require_hex64("lease_id", payload["lease_id"])
        current = leases.get(lease_id)
        if current is None:
            raise RuntimeError(f"attempt_started for unknown lease {lease_id}")
        if payload["attempt_id"] != current.attempt_id:
            raise RuntimeError("attempt_started attempt_id does not match lease")
        if payload["action_id"] != current.action_id:
            raise RuntimeError("attempt_started action_id does not match lease")
        if payload["reservation_id"] != current.reservation_id:
            raise RuntimeError("attempt_started reservation_id does not match lease")
        started.add(lease_id)
        return
    if event_type == _EVENT_PROGRESS:
        _require_keys(payload, {"lease_id", "progress"}, event_type)
        lease_id = _require_hex64("lease_id", payload["lease_id"])
        if lease_id not in leases:
            raise RuntimeError(f"progress for unknown lease {lease_id}")
        ProgressReceipt.from_dict(payload["progress"])
        return
    _require_keys(payload, {"lease"}, event_type)
    incoming = Lease.from_dict(payload["lease"])
    current = leases.get(incoming.lease_id)
    if current is None:
        raise RuntimeError(f"{event_type} for unknown lease {incoming.lease_id}")
    if incoming.lease_id not in started:
        raise RuntimeError(f"{event_type} before attempt_started")
    expected_status = {
        _EVENT_LEASE_RUNNING: LeaseStatus.RUNNING,
        _EVENT_ATTEMPT_FINISHED: LeaseStatus.COMPLETED,
        _EVENT_LEASE_COMPLETED: LeaseStatus.COMPLETED,
        _EVENT_LEASE_INFRA_FAILED: LeaseStatus.INFRA_FAILED,
        _EVENT_LEASE_CANCELLED: LeaseStatus.CANCELLED,
        _EVENT_LEASE_EXPIRED: LeaseStatus.EXPIRED,
    }[event_type]
    if incoming.status is not expected_status:
        raise RuntimeError(f"{event_type} payload status must be {expected_status.value}")
    if current.status is incoming.status:
        _assert_same_terminal(current, incoming)
        return
    if current.status.value not in _ACTIVE_LEASE:
        raise RuntimeError(f"illegal {event_type} from status {current.status.value}")
    if event_type == _EVENT_LEASE_RUNNING and current.status is not LeaseStatus.ISSUED:
        raise RuntimeError("lease_running requires ISSUED")
    leases[incoming.lease_id] = incoming


def _resources_from_leases(
    leases: tuple[Lease, ...], policy: BudgetPolicy
) -> ResourceSnapshot:
    snap = ResourceSnapshot.empty()
    for lease in leases:
        if lease.resource_disposition is ResourceDisposition.RESERVED:
            snap = snap.reserve(
                lease.requested,
                budget_class=lease.budget_class,
                slot_kind=lease.slot_kind,
                policy=policy,
            )
        elif lease.resource_disposition is ResourceDisposition.SETTLED:
            snap = _add_consumed(snap, lease.consumed, lease.budget_class)
    return snap


def _add_consumed(
    snapshot: ResourceSnapshot, vector: ResourceVector, budget_class: BudgetClass
) -> ResourceSnapshot:
    field = {
        BudgetClass.NORMAL: "consumed_normal",
        BudgetClass.EXPLORATION: "consumed_exploration",
        BudgetClass.FINAL_AUDIT: "consumed_final_audit",
    }[budget_class]
    payload = snapshot.to_dict()
    payload["consumed"] = snapshot.consumed.add(vector).to_dict()
    payload[field] = getattr(snapshot, field).add(vector).to_dict()
    return ResourceSnapshot.from_dict(payload)


def _consumed_from_usage(lease: Lease, usage: UsageRecord) -> ResourceVector:
    return ResourceUsage.from_usage_record(usage).settled_vector(lease.requested)


def _unknown_usage_record() -> UsageRecord:
    return UsageRecord(
        **{
            name: ObservedQuantity(
                unit=_USAGE_UNITS[name],
                status=ObservationStatus.UNKNOWN,
                value=None,
            )
            for name in RESOURCE_DIMENSIONS
        }
    )


def _validate_progress(
    progress: object, lease: Lease, view: ResearchView | None
) -> None:
    if not isinstance(progress, ProgressReceipt):
        raise RuntimeError("progress must be a ProgressReceipt")
    if view is None:
        raise RuntimeError("progress requires a ResearchView")
    if not isinstance(view, ResearchView):
        raise RuntimeError("view must be a ResearchView")
    if progress.action_id != lease.action_id:
        raise RuntimeError("progress action_id does not match lease")
    if progress.attempt_id != lease.attempt_id:
        raise RuntimeError("progress attempt_id does not match lease")
    if progress.contract_id != lease.contract_id:
        raise RuntimeError("progress contract_id does not match lease")
    validate_progress_against_view(progress, view)


def _artifact_ref_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise RuntimeError("artifact_refs must be a list of hashes")
    return tuple(
        _require_hex64(f"artifact_refs[{i}]", item) for i, item in enumerate(value)
    )


def _require_math(value: object) -> MathematicalStatus:
    if isinstance(value, MathematicalStatus):
        return value
    if isinstance(value, str):
        try:
            return MathematicalStatus(value)
        except ValueError as exc:
            raise RuntimeError(f"unknown mathematical_status {value!r}") from exc
    raise RuntimeError("mathematical_status must be a MathematicalStatus")


def _mapping(name: str, data: object) -> dict[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise RuntimeError(f"{name} must be a mapping")
    return {str(key): data[key] for key in data}


def _require_keys(
    payload: Mapping[str, Any], required: set[str] | frozenset[str], name: str
) -> None:
    missing = [key for key in required if key not in payload]
    if missing:
        raise RuntimeError(f"{name} missing required field {missing[0]}")


def _reject_extra(payload: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    extra = [key for key in payload if key not in allowed]
    if extra:
        raise RuntimeError(f"{name} has unknown field {extra[0]!r}")


def _require_hex64(name: str, value: object) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise RuntimeError(f"{name} must be a 64-char lowercase hex hash")
    return value


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise RuntimeError(f"{name} must be a nonempty str")
    return value


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{name} must be a nonnegative int")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
