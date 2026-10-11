"""Frozen live-control records for managed Euler projects.

Stdlib only. These records persist as ``euler.*`` EventStore payloads. They are
not mathematical truth and never write the Fact Graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from euler.control.ids import canonical_json, stable_id
from euler.control.scheduler import CandidateAction
from euler.control.schema import SchemaValidationError

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UTC = timezone.utc
_ZERO = timedelta(0)

_MANIFEST_DOMAIN = "control-manifest"
_PROPOSAL_DOMAIN = "proposed-candidate"
_INTENT_DOMAIN = "dispatch-intent"
_READY_DOMAIN = "dispatch-ready"
_RECONCILE_DOMAIN = "reconciliation"
_STATUS_DOMAIN = "control-status"
_ID_VERSION = 1
_SCHEMA_VERSION = 1

EVENT_CANDIDATE_PROPOSED = "euler.candidate_proposed"
EVENT_DISPATCH_PLANNED = "euler.dispatch_planned"
EVENT_DISPATCH_INTENT = "euler.dispatch_intent"
EVENT_DISPATCH_READY = "euler.dispatch_ready"
EVENT_ROUND_BOUND = "euler.round_bound"
EVENT_ROUND_RECONCILED = "euler.round_reconciled"
EVENT_ACCEPTANCE_RECORDED = "euler.acceptance_recorded"
EVENT_CHECKPOINT_CREATED = "euler.checkpoint_created"

EULER_EVENT_TYPES = frozenset(
    {
        EVENT_CANDIDATE_PROPOSED,
        EVENT_DISPATCH_PLANNED,
        EVENT_DISPATCH_INTENT,
        EVENT_DISPATCH_READY,
        EVENT_ROUND_BOUND,
        EVENT_ROUND_RECONCILED,
        EVENT_ACCEPTANCE_RECORDED,
        EVENT_CHECKPOINT_CREATED,
    }
)

__all__ = [
    "EVENT_CANDIDATE_PROPOSED",
    "EVENT_DISPATCH_PLANNED",
    "EVENT_DISPATCH_INTENT",
    "EVENT_DISPATCH_READY",
    "EVENT_ROUND_BOUND",
    "EVENT_ROUND_RECONCILED",
    "EVENT_ACCEPTANCE_RECORDED",
    "EVENT_CHECKPOINT_CREATED",
    "EULER_EVENT_TYPES",
    "ControlManifest",
    "ProposedCandidate",
    "DispatchBinding",
    "ReconciliationResult",
    "ControlStatus",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlManifest:
    schema_version: int
    project_id: str
    contract_id: str
    target_obligation_id: str
    policy_hash: str
    manifest_hash: str
    created_at: str
    actor: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != _SCHEMA_VERSION
        ):
            raise SchemaValidationError("schema_version must be 1")
        object.__setattr__(
            self, "project_id", _require_safe("project_id", self.project_id)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self,
            "target_obligation_id",
            _require_hex64("target_obligation_id", self.target_obligation_id),
        )
        object.__setattr__(
            self, "policy_hash", _require_hex64("policy_hash", self.policy_hash)
        )
        object.__setattr__(self, "created_at", _require_utc("created_at", self.created_at))
        object.__setattr__(self, "actor", _require_nonempty("actor", self.actor))
        expected = manifest_hash(
            project_id=self.project_id,
            contract_id=self.contract_id,
            target_obligation_id=self.target_obligation_id,
            policy_hash=self.policy_hash,
        )
        object.__setattr__(
            self, "manifest_hash", _require_hex64("manifest_hash", self.manifest_hash)
        )
        if self.manifest_hash != expected:
            raise SchemaValidationError("manifest_hash does not match identity fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "contract_id": self.contract_id,
            "target_obligation_id": self.target_obligation_id,
            "policy_hash": self.policy_hash,
            "manifest_hash": self.manifest_hash,
            "created_at": self.created_at,
            "actor": self.actor,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ControlManifest":
        payload = _mapping("ControlManifest", data)
        allowed = {
            "schema_version",
            "project_id",
            "contract_id",
            "target_obligation_id",
            "policy_hash",
            "manifest_hash",
            "created_at",
            "actor",
        }
        _reject_extra(payload, allowed, "ControlManifest")
        _require_keys(payload, allowed, "ControlManifest")
        return cls(
            schema_version=payload["schema_version"],
            project_id=payload["project_id"],
            contract_id=payload["contract_id"],
            target_obligation_id=payload["target_obligation_id"],
            policy_hash=payload["policy_hash"],
            manifest_hash=payload["manifest_hash"],
            created_at=payload["created_at"],
            actor=payload["actor"],
        )

    @classmethod
    def build(
        cls,
        *,
        project_id: str,
        contract_id: str,
        target_obligation_id: str,
        policy_hash: str,
        created_at: str,
        actor: str,
        schema_version: int = _SCHEMA_VERSION,
    ) -> "ControlManifest":
        digest = manifest_hash(
            project_id=project_id,
            contract_id=contract_id,
            target_obligation_id=target_obligation_id,
            policy_hash=policy_hash,
        )
        return cls(
            schema_version=schema_version,
            project_id=project_id,
            contract_id=contract_id,
            target_obligation_id=target_obligation_id,
            policy_hash=policy_hash,
            manifest_hash=digest,
            created_at=created_at,
            actor=actor,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposedCandidate:
    proposal_id: str
    worker: str
    candidate: CandidateAction
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, CandidateAction):
            raise SchemaValidationError("candidate must be a CandidateAction")
        object.__setattr__(self, "worker", _require_safe("worker", self.worker))
        object.__setattr__(self, "created_at", _require_utc("created_at", self.created_at))
        object.__setattr__(
            self, "proposal_id", _require_hex64("proposal_id", self.proposal_id)
        )
        expected = proposal_id(
            candidate_id=self.candidate.candidate_id,
            worker=self.worker,
            action_id=self.candidate.action_spec.action_id,
        )
        if self.proposal_id != expected:
            raise SchemaValidationError("proposal_id does not match identity fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "worker": self.worker,
            "candidate": self.candidate.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ProposedCandidate":
        payload = _mapping("ProposedCandidate", data)
        allowed = {"proposal_id", "worker", "candidate", "created_at"}
        _reject_extra(payload, allowed, "ProposedCandidate")
        _require_keys(payload, allowed, "ProposedCandidate")
        return cls(
            proposal_id=payload["proposal_id"],
            worker=payload["worker"],
            candidate=CandidateAction.from_dict(payload["candidate"]),
            created_at=payload["created_at"],
        )

    @classmethod
    def build(
        cls,
        *,
        candidate: CandidateAction,
        worker: str,
        created_at: str,
    ) -> "ProposedCandidate":
        digest = proposal_id(
            candidate_id=candidate.candidate_id,
            worker=worker,
            action_id=candidate.action_spec.action_id,
        )
        return cls(
            proposal_id=digest,
            worker=worker,
            candidate=candidate,
            created_at=created_at,
        )


_BINDING_OPTIONAL = frozenset(
    {
        "ready_id",
        "obligation_id",
        "scope_hash",
        "dependency_hash",
        "context_version_hash",
        "required_fact_ids",
        "required_fact_versions",
        "required_evidence_ids",
        "baseline_evidence_ids",
        "baseline_relation_ids",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchBinding:
    intent_id: str
    worker: str
    candidate_id: str
    action_id: str
    lease_id: str
    attempt_id: str
    packet_hash: str
    cache_key_hash: str
    task_hash: str
    baseline_fact_ids: tuple[str, ...]
    frontier_hash: str
    ready_id: Optional[str] = None
    obligation_id: Optional[str] = None
    scope_hash: Optional[str] = None
    dependency_hash: Optional[str] = None
    context_version_hash: Optional[str] = None
    required_fact_ids: tuple[str, ...] = ()
    required_fact_versions: tuple[str, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    baseline_evidence_ids: tuple[str, ...] = ()
    baseline_relation_ids: Optional[tuple[str, ...]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _require_hex64("intent_id", self.intent_id))
        object.__setattr__(self, "worker", _require_safe("worker", self.worker))
        object.__setattr__(
            self, "candidate_id", _require_nonempty("candidate_id", self.candidate_id)
        )
        object.__setattr__(self, "action_id", _require_hex64("action_id", self.action_id))
        object.__setattr__(self, "lease_id", _require_hex64("lease_id", self.lease_id))
        object.__setattr__(
            self, "attempt_id", _require_hex64("attempt_id", self.attempt_id)
        )
        object.__setattr__(
            self, "packet_hash", _require_hex64("packet_hash", self.packet_hash)
        )
        object.__setattr__(
            self, "cache_key_hash", _require_hex64("cache_key_hash", self.cache_key_hash)
        )
        object.__setattr__(self, "task_hash", _require_hex64("task_hash", self.task_hash))
        object.__setattr__(
            self,
            "baseline_fact_ids",
            _require_hex16_tuple("baseline_fact_ids", self.baseline_fact_ids),
        )
        object.__setattr__(
            self, "frontier_hash", _require_hex64("frontier_hash", self.frontier_hash)
        )
        object.__setattr__(self, "ready_id", _optional_hex64("ready_id", self.ready_id))
        object.__setattr__(
            self, "obligation_id", _optional_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self, "scope_hash", _optional_hex64("scope_hash", self.scope_hash)
        )
        object.__setattr__(
            self,
            "dependency_hash",
            _optional_hex64("dependency_hash", self.dependency_hash),
        )
        object.__setattr__(
            self,
            "context_version_hash",
            _optional_hex64("context_version_hash", self.context_version_hash),
        )
        object.__setattr__(
            self,
            "required_fact_ids",
            _require_hex16_tuple("required_fact_ids", self.required_fact_ids),
        )
        object.__setattr__(
            self,
            "required_fact_versions",
            _require_hex64_tuple("required_fact_versions", self.required_fact_versions),
        )
        object.__setattr__(
            self,
            "required_evidence_ids",
            _require_hex64_tuple("required_evidence_ids", self.required_evidence_ids),
        )
        object.__setattr__(
            self,
            "baseline_evidence_ids",
            _require_hex64_tuple("baseline_evidence_ids", self.baseline_evidence_ids),
        )
        if len(self.required_fact_ids) != len(self.required_fact_versions):
            raise SchemaValidationError(
                "required_fact_ids and required_fact_versions must be parallel"
            )
        if self.baseline_relation_ids is not None:
            object.__setattr__(self, "baseline_relation_ids",
                               _require_hex64_tuple("baseline_relation_ids", self.baseline_relation_ids))
        expected = intent_id(
            lease_id=self.lease_id,
            candidate_id=self.candidate_id,
            action_id=self.action_id,
            worker=self.worker,
        )
        if self.intent_id != expected:
            raise SchemaValidationError("intent_id does not match identity fields")
        if self.ready_id is not None:
            expected_ready = ready_id(
                intent_id=self.intent_id,
                task_hash=self.task_hash,
                packet_hash=self.packet_hash,
            )
            if self.ready_id != expected_ready:
                raise SchemaValidationError("ready_id does not match identity fields")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "intent_id": self.intent_id,
            "ready_id": self.ready_id,
            "worker": self.worker,
            "candidate_id": self.candidate_id,
            "action_id": self.action_id,
            "lease_id": self.lease_id,
            "attempt_id": self.attempt_id,
            "packet_hash": self.packet_hash,
            "cache_key_hash": self.cache_key_hash,
            "task_hash": self.task_hash,
            "baseline_fact_ids": list(self.baseline_fact_ids),
            "frontier_hash": self.frontier_hash,
        }
        if self.obligation_id is not None:
            payload["obligation_id"] = self.obligation_id
        if self.scope_hash is not None:
            payload["scope_hash"] = self.scope_hash
        if self.dependency_hash is not None:
            payload["dependency_hash"] = self.dependency_hash
        if self.context_version_hash is not None:
            payload["context_version_hash"] = self.context_version_hash
        if self.required_fact_ids:
            payload["required_fact_ids"] = list(self.required_fact_ids)
        if self.required_fact_versions:
            payload["required_fact_versions"] = list(self.required_fact_versions)
        if self.required_evidence_ids:
            payload["required_evidence_ids"] = list(self.required_evidence_ids)
        if self.baseline_evidence_ids:
            payload["baseline_evidence_ids"] = list(self.baseline_evidence_ids)
        if self.baseline_relation_ids is not None:
            payload["baseline_relation_ids"] = list(self.baseline_relation_ids)
        return payload

    @classmethod
    def from_dict(cls, data: object) -> "DispatchBinding":
        payload = _mapping("DispatchBinding", data)
        allowed = {
            "intent_id",
            "ready_id",
            "worker",
            "candidate_id",
            "action_id",
            "lease_id",
            "attempt_id",
            "packet_hash",
            "cache_key_hash",
            "task_hash",
            "baseline_fact_ids",
            "frontier_hash",
        } | _BINDING_OPTIONAL
        _reject_extra(payload, allowed, "DispatchBinding")
        _require_keys(payload, allowed - _BINDING_OPTIONAL, "DispatchBinding")
        return cls(
            intent_id=payload["intent_id"],
            ready_id=payload.get("ready_id"),
            worker=payload["worker"],
            candidate_id=payload["candidate_id"],
            action_id=payload["action_id"],
            lease_id=payload["lease_id"],
            attempt_id=payload["attempt_id"],
            packet_hash=payload["packet_hash"],
            cache_key_hash=payload["cache_key_hash"],
            task_hash=payload["task_hash"],
            baseline_fact_ids=tuple(payload["baseline_fact_ids"]),
            frontier_hash=payload["frontier_hash"],
            obligation_id=payload.get("obligation_id"),
            scope_hash=payload.get("scope_hash"),
            dependency_hash=payload.get("dependency_hash"),
            context_version_hash=payload.get("context_version_hash"),
            required_fact_ids=tuple(payload.get("required_fact_ids") or ()),
            required_fact_versions=tuple(payload.get("required_fact_versions") or ()),
            required_evidence_ids=tuple(payload.get("required_evidence_ids") or ()),
            baseline_evidence_ids=tuple(payload.get("baseline_evidence_ids") or ()),
            baseline_relation_ids=(tuple(payload["baseline_relation_ids"])
                                   if payload.get("baseline_relation_ids") is not None else None),
        )

    @classmethod
    def build(
        cls,
        *,
        worker: str,
        candidate_id: str,
        action_id: str,
        lease_id: str,
        attempt_id: str,
        packet_hash: str,
        cache_key_hash: str,
        task_hash: str,
        baseline_fact_ids: tuple[str, ...] | list[str],
        frontier_hash: str,
        ready: bool = False,
        obligation_id: Optional[str] = None,
        scope_hash: Optional[str] = None,
        dependency_hash: Optional[str] = None,
        context_version_hash: Optional[str] = None,
        required_fact_ids: tuple[str, ...] | list[str] = (),
        required_fact_versions: tuple[str, ...] | list[str] = (),
        required_evidence_ids: tuple[str, ...] | list[str] = (),
        baseline_evidence_ids: tuple[str, ...] | list[str] = (),
        baseline_relation_ids: tuple[str, ...] | list[str] | None = None,
    ) -> "DispatchBinding":
        digest = intent_id(
            lease_id=lease_id,
            candidate_id=candidate_id,
            action_id=action_id,
            worker=worker,
        )
        ready_digest = (
            ready_id(intent_id=digest, task_hash=task_hash, packet_hash=packet_hash)
            if ready
            else None
        )
        return cls(
            intent_id=digest,
            ready_id=ready_digest,
            worker=worker,
            candidate_id=candidate_id,
            action_id=action_id,
            lease_id=lease_id,
            attempt_id=attempt_id,
            packet_hash=packet_hash,
            cache_key_hash=cache_key_hash,
            task_hash=task_hash,
            baseline_fact_ids=tuple(baseline_fact_ids),
            frontier_hash=frontier_hash,
            obligation_id=obligation_id,
            scope_hash=scope_hash,
            dependency_hash=dependency_hash,
            context_version_hash=context_version_hash,
            required_fact_ids=tuple(required_fact_ids),
            required_fact_versions=tuple(required_fact_versions),
            required_evidence_ids=tuple(required_evidence_ids),
            baseline_evidence_ids=tuple(baseline_evidence_ids),
            baseline_relation_ids=None if baseline_relation_ids is None else tuple(baseline_relation_ids),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationResult:
    reconciliation_id: str
    lease_id: str
    round_id: str
    progress_id: Optional[str] = None
    evidence_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    outcome_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lease_id", _require_hex64("lease_id", self.lease_id))
        object.__setattr__(self, "round_id", _require_nonempty("round_id", self.round_id))
        object.__setattr__(
            self, "progress_id", _optional_hex64("progress_id", self.progress_id)
        )
        object.__setattr__(
            self, "evidence_ids", _require_hex64_tuple("evidence_ids", self.evidence_ids)
        )
        object.__setattr__(
            self, "fact_ids", _require_hex16_tuple("fact_ids", self.fact_ids)
        )
        if self.outcome_ref is not None:
            object.__setattr__(
                self, "outcome_ref", _require_nonempty("outcome_ref", self.outcome_ref)
            )
        expected = reconcile_id(lease_id=self.lease_id, round_id=self.round_id)
        object.__setattr__(
            self,
            "reconciliation_id",
            _require_hex64("reconciliation_id", self.reconciliation_id),
        )
        if self.reconciliation_id != expected:
            raise SchemaValidationError(
                "reconciliation_id does not match identity fields"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "lease_id": self.lease_id,
            "round_id": self.round_id,
            "progress_id": self.progress_id,
            "evidence_ids": list(self.evidence_ids),
            "fact_ids": list(self.fact_ids),
            "outcome_ref": self.outcome_ref,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ReconciliationResult":
        payload = _mapping("ReconciliationResult", data)
        allowed = {
            "reconciliation_id",
            "lease_id",
            "round_id",
            "progress_id",
            "evidence_ids",
            "fact_ids",
            "outcome_ref",
        }
        _reject_extra(payload, allowed, "ReconciliationResult")
        _require_keys(payload, {"reconciliation_id", "lease_id", "round_id"}, "ReconciliationResult")
        return cls(
            reconciliation_id=payload["reconciliation_id"],
            lease_id=payload["lease_id"],
            round_id=payload["round_id"],
            progress_id=payload.get("progress_id"),
            evidence_ids=tuple(payload.get("evidence_ids") or ()),
            fact_ids=tuple(payload.get("fact_ids") or ()),
            outcome_ref=payload.get("outcome_ref"),
        )

    @classmethod
    def build(
        cls,
        *,
        lease_id: str,
        round_id: str,
        progress_id: Optional[str] = None,
        evidence_ids: tuple[str, ...] | list[str] = (),
        fact_ids: tuple[str, ...] | list[str] = (),
        outcome_ref: Optional[str] = None,
    ) -> "ReconciliationResult":
        return cls(
            reconciliation_id=reconcile_id(lease_id=lease_id, round_id=round_id),
            lease_id=lease_id,
            round_id=round_id,
            progress_id=progress_id,
            evidence_ids=tuple(evidence_ids),
            fact_ids=tuple(fact_ids),
            outcome_ref=outcome_ref,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlStatus:
    project_id: str
    control_plane: str
    schema_version: int
    contract_id: str
    target_obligation_id: str
    policy_hash: str
    manifest_hash: str
    created_at: str
    actor: str
    max_seq: int
    proposed_ids: tuple[str, ...]
    budget_kind: str
    budget_value: Optional[int]
    status_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_id", _require_safe("project_id", self.project_id)
        )
        object.__setattr__(
            self, "control_plane", _require_nonempty("control_plane", self.control_plane)
        )
        if self.control_plane != "euler":
            raise SchemaValidationError("control_plane must be 'euler'")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != _SCHEMA_VERSION
        ):
            raise SchemaValidationError("schema_version must be 1")
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self,
            "target_obligation_id",
            _require_hex64("target_obligation_id", self.target_obligation_id),
        )
        object.__setattr__(
            self, "policy_hash", _require_hex64("policy_hash", self.policy_hash)
        )
        object.__setattr__(
            self, "manifest_hash", _require_hex64("manifest_hash", self.manifest_hash)
        )
        object.__setattr__(self, "created_at", _require_utc("created_at", self.created_at))
        object.__setattr__(self, "actor", _require_nonempty("actor", self.actor))
        if isinstance(self.max_seq, bool) or not isinstance(self.max_seq, int) or self.max_seq < 0:
            raise SchemaValidationError("max_seq must be a nonnegative int")
        object.__setattr__(
            self, "proposed_ids", _require_hex64_tuple("proposed_ids", self.proposed_ids)
        )
        object.__setattr__(
            self, "budget_kind", _require_nonempty("budget_kind", self.budget_kind)
        )
        if self.budget_value is not None:
            if (
                isinstance(self.budget_value, bool)
                or not isinstance(self.budget_value, int)
                or self.budget_value < 0
            ):
                raise SchemaValidationError("budget_value must be a nonnegative int")
        expected = status_hash_for(
            project_id=self.project_id,
            contract_id=self.contract_id,
            target_obligation_id=self.target_obligation_id,
            policy_hash=self.policy_hash,
            manifest_hash=self.manifest_hash,
            max_seq=self.max_seq,
            proposed_ids=self.proposed_ids,
            budget_kind=self.budget_kind,
            budget_value=self.budget_value,
        )
        object.__setattr__(
            self, "status_hash", _require_hex64("status_hash", self.status_hash)
        )
        if self.status_hash != expected:
            raise SchemaValidationError("status_hash does not match identity fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "control_plane": self.control_plane,
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "target_obligation_id": self.target_obligation_id,
            "policy_hash": self.policy_hash,
            "manifest_hash": self.manifest_hash,
            "created_at": self.created_at,
            "actor": self.actor,
            "max_seq": self.max_seq,
            "proposed_ids": list(self.proposed_ids),
            "budget_kind": self.budget_kind,
            "budget_value": self.budget_value,
            "status_hash": self.status_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ControlStatus":
        payload = _mapping("ControlStatus", data)
        allowed = {
            "project_id",
            "control_plane",
            "schema_version",
            "contract_id",
            "target_obligation_id",
            "policy_hash",
            "manifest_hash",
            "created_at",
            "actor",
            "max_seq",
            "proposed_ids",
            "budget_kind",
            "budget_value",
            "status_hash",
        }
        _reject_extra(payload, allowed, "ControlStatus")
        _require_keys(payload, allowed - {"budget_value"}, "ControlStatus")
        return cls(
            project_id=payload["project_id"],
            control_plane=payload["control_plane"],
            schema_version=payload["schema_version"],
            contract_id=payload["contract_id"],
            target_obligation_id=payload["target_obligation_id"],
            policy_hash=payload["policy_hash"],
            manifest_hash=payload["manifest_hash"],
            created_at=payload["created_at"],
            actor=payload["actor"],
            max_seq=payload["max_seq"],
            proposed_ids=tuple(payload["proposed_ids"]),
            budget_kind=payload["budget_kind"],
            budget_value=payload.get("budget_value"),
            status_hash=payload["status_hash"],
        )

    @classmethod
    def build(
        cls,
        *,
        project_id: str,
        contract_id: str,
        target_obligation_id: str,
        policy_hash: str,
        manifest_hash: str,
        created_at: str,
        actor: str,
        max_seq: int,
        proposed_ids: tuple[str, ...] | list[str],
        budget_kind: str,
        budget_value: Optional[int],
        schema_version: int = _SCHEMA_VERSION,
        control_plane: str = "euler",
    ) -> "ControlStatus":
        ids = tuple(proposed_ids)
        digest = status_hash_for(
            project_id=project_id,
            contract_id=contract_id,
            target_obligation_id=target_obligation_id,
            policy_hash=policy_hash,
            manifest_hash=manifest_hash,
            max_seq=max_seq,
            proposed_ids=ids,
            budget_kind=budget_kind,
            budget_value=budget_value,
        )
        return cls(
            project_id=project_id,
            control_plane=control_plane,
            schema_version=schema_version,
            contract_id=contract_id,
            target_obligation_id=target_obligation_id,
            policy_hash=policy_hash,
            manifest_hash=manifest_hash,
            created_at=created_at,
            actor=actor,
            max_seq=max_seq,
            proposed_ids=ids,
            budget_kind=budget_kind,
            budget_value=budget_value,
            status_hash=digest,
        )


def manifest_hash(
    *,
    project_id: str,
    contract_id: str,
    target_obligation_id: str,
    policy_hash: str,
) -> str:
    return stable_id(
        _MANIFEST_DOMAIN,
        _ID_VERSION,
        project_id,
        contract_id,
        target_obligation_id,
        policy_hash,
    )


def proposal_id(*, candidate_id: str, worker: str, action_id: str) -> str:
    return stable_id(
        _PROPOSAL_DOMAIN, _ID_VERSION, candidate_id, worker, action_id
    )


def intent_id(*, lease_id: str, candidate_id: str, action_id: str, worker: str) -> str:
    return stable_id(
        _INTENT_DOMAIN, _ID_VERSION, lease_id, candidate_id, action_id, worker
    )


def ready_id(*, intent_id: str, task_hash: str, packet_hash: str) -> str:
    return stable_id(_READY_DOMAIN, _ID_VERSION, intent_id, task_hash, packet_hash)


def reconcile_id(*, lease_id: str, round_id: str) -> str:
    return stable_id(_RECONCILE_DOMAIN, _ID_VERSION, lease_id, round_id)


def status_hash_for(
    *,
    project_id: str,
    contract_id: str,
    target_obligation_id: str,
    policy_hash: str,
    manifest_hash: str,
    max_seq: int,
    proposed_ids: tuple[str, ...],
    budget_kind: str,
    budget_value: Optional[int],
) -> str:
    return stable_id(
        _STATUS_DOMAIN,
        _ID_VERSION,
        canonical_json(
            {
                "project_id": project_id,
                "contract_id": contract_id,
                "target_obligation_id": target_obligation_id,
                "policy_hash": policy_hash,
                "manifest_hash": manifest_hash,
                "max_seq": max_seq,
                "proposed_ids": list(proposed_ids),
                "budget_kind": budget_kind,
                "budget_value": budget_value,
            }
        ),
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


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SchemaValidationError(f"{name} must be a nonempty str")
    return value


def _require_safe(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _SAFE_IDENTIFIER_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} is not a safe identifier")
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} must be 64-char lowercase hex")
    return text


def _optional_hex64(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_hex64(name, value)


def _require_hex16(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX16_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} must be 16-char lowercase hex")
    return text


def _require_hex16_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list of 16-hex Fact IDs")
    return tuple(_require_hex16(f"{name}[{i}]", item) for i, item in enumerate(value))


def _require_hex64_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list of 64-hex hashes")
    return tuple(_require_hex64(f"{name}[{i}]", item) for i, item in enumerate(value))


def _require_utc(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise SchemaValidationError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise SchemaValidationError(f"{name} must be UTC")
    return text
