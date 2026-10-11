"""Context-seed construction and transparent deterministic action selection.

Stdlib only. Seed is hashed before ActionSpec; ContextRequest then binds the
resulting action_id. The final compiler cache key is never fed back into the
same ActionSpec. Selection never mutates ResearchView, the dedupe index, or
recurrence history. Audit-required recurrence is deferred or dispatched as an
explicit AUDIT action; it is never an automatic park.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

from euler.control.context import ContextLimits, ContextRequest
from euler.control.dedupe import ActionAttemptIndex, DedupeDecision
from euler.control.recall import RecalledSelection, recall_for_action
from euler.control.ids import canonical_json, stable_id
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    OnDeadlinePolicy,
    OnStagnationPolicy,
    PolicyError,
    ResourceSnapshot,
    ResourceVector,
    SlotKind,
)
from euler.control.progress import ActionSpec, SamplingMode
from euler.control.recurrence import (
    FrontierFingerprint,
    RecurrenceAssessment,
    RecurrenceHistory,
    RecurrenceRecommendation,
    StrikeKey,
)
from euler.control.scc import ProjectedEdge, ProjectedEdgeKind, SCCClassification, SCCReport
from euler.control.schema import SchemaValidationError
from euler.control.views import ResearchView, RouteReadiness

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SEED_DOMAIN = "context-seed"
_SEED_VERSION = 1
_REQUEST_DOMAIN = "context-request"
_REQUEST_VERSION = 1

_AUDIT_REQUIRED = frozenset(
    {
        RecurrenceRecommendation.BOUNDED_AUDIT,
        RecurrenceRecommendation.RESTRUCTURE,
        RecurrenceRecommendation.PARK_ROUTE_CANDIDATE,
    }
)

_SLOT_FOR_ACTION = {
    "PROVE": SlotKind.REASONING,
    "VERIFY": SlotKind.VERIFIER,
    "AUDIT": SlotKind.REASONING,
    "COUNTEREXAMPLE": SlotKind.COMPUTE,
    "COMPUTE": SlotKind.COMPUTE,
    "LITERATURE": SlotKind.RETRIEVAL,
}

_PRIORITY_RANK = {
    "FINAL_ACCEPTANCE": 0,
    "LOAD_BEARING": 1,
    "DISCRIMINATING_CHEAP": 2,
    "NORMAL": 3,
    "EXPLORATION": 4,
}

__all__ = [
    "SchedulerError",
    "ActionKind",
    "PriorityClass",
    "ScheduleDisposition",
    "ScheduleReason",
    "DependencyValidity",
    "ContextSeed",
    "ActiveWork",
    "CandidateAction",
    "CandidateDecision",
    "DispatchPlan",
    "build_action_context_pair",
    "build_recalled_action_context_pair",
    "rebind_candidate_with_recall",
    "slot_kind_for_action",
    "TransparentScheduler",
]


class SchedulerError(ValueError):
    """Malformed scheduler input or an inconsistent candidate."""


class ActionKind(str, Enum):
    PROVE = "PROVE"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    LITERATURE = "LITERATURE"
    COMPUTE = "COMPUTE"
    VERIFY = "VERIFY"
    AUDIT = "AUDIT"


class PriorityClass(str, Enum):
    FINAL_ACCEPTANCE = "FINAL_ACCEPTANCE"
    LOAD_BEARING = "LOAD_BEARING"
    DISCRIMINATING_CHEAP = "DISCRIMINATING_CHEAP"
    NORMAL = "NORMAL"
    EXPLORATION = "EXPLORATION"


class ScheduleDisposition(str, Enum):
    SELECTED = "SELECTED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"


class ScheduleReason(str, Enum):
    SELECTED = "SELECTED"
    MALFORMED = "MALFORMED"
    STALE_DEPENDENCY = "STALE_DEPENDENCY"
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    DEDUPE_WAIT = "DEDUPE_WAIT"
    DEDUPE_RECOVER = "DEDUPE_RECOVER"
    RECURRENCE_AUDIT = "RECURRENCE_AUDIT"
    CLASS_RESERVE = "CLASS_RESERVE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DEADLINE = "DEADLINE"
    SLOT_EXHAUSTED = "SLOT_EXHAUSTED"
    MECHANISM_CONFLICT = "MECHANISM_CONFLICT"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    STAGNATION = "STAGNATION"
    NOT_SELECTED = "NOT_SELECTED"


class DependencyValidity(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSeed:
    """Pre-ActionSpec hash of Phase 5 semantic context inputs. No action_id."""

    seed_hash: str
    contract_id: str
    obligation_id: str
    concrete_gap: str
    output_contract: str
    tool_environment_hash: str
    acceptance_policy_hash: str
    reference_versions_hash: str
    limits: ContextLimits
    expand_evidence_content: bool
    required_fact_ids: tuple[str, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    relevant_failure_ids: tuple[str, ...] = ()
    relevant_relation_ids: tuple[str, ...] = ()
    route_id: Optional[str] = None
    expand_proof_content: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "seed_hash", _require_hex64("seed_hash", self.seed_hash))
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self, "concrete_gap", _require_nonempty("concrete_gap", self.concrete_gap)
        )
        object.__setattr__(
            self,
            "output_contract",
            _require_nonempty("output_contract", self.output_contract),
        )
        object.__setattr__(
            self,
            "tool_environment_hash",
            _require_hex64("tool_environment_hash", self.tool_environment_hash),
        )
        object.__setattr__(
            self,
            "acceptance_policy_hash",
            _require_hex64("acceptance_policy_hash", self.acceptance_policy_hash),
        )
        object.__setattr__(
            self,
            "reference_versions_hash",
            _require_hex64("reference_versions_hash", self.reference_versions_hash),
        )
        if not isinstance(self.limits, ContextLimits):
            raise SchedulerError("limits must be ContextLimits")
        if not isinstance(self.expand_evidence_content, bool):
            raise SchedulerError("expand_evidence_content must be bool")
        if not isinstance(self.expand_proof_content, bool):
            raise SchedulerError("expand_proof_content must be bool")
        object.__setattr__(
            self,
            "required_fact_ids",
            _require_id_tuple("required_fact_ids", self.required_fact_ids, hex64=False),
        )
        object.__setattr__(
            self,
            "required_evidence_ids",
            _require_id_tuple("required_evidence_ids", self.required_evidence_ids),
        )
        object.__setattr__(
            self,
            "relevant_failure_ids",
            _require_id_tuple("relevant_failure_ids", self.relevant_failure_ids),
        )
        object.__setattr__(
            self,
            "relevant_relation_ids",
            _require_id_tuple("relevant_relation_ids", self.relevant_relation_ids),
        )
        object.__setattr__(self, "route_id", _optional_hex64("route_id", self.route_id))
        expected = _seed_hash_from_fields(
            contract_id=self.contract_id,
            obligation_id=self.obligation_id,
            route_id=self.route_id,
            concrete_gap=self.concrete_gap,
            output_contract=self.output_contract,
            tool_environment_hash=self.tool_environment_hash,
            acceptance_policy_hash=self.acceptance_policy_hash,
            reference_versions_hash=self.reference_versions_hash,
            limits=self.limits,
            expand_evidence_content=self.expand_evidence_content,
            expand_proof_content=self.expand_proof_content,
            required_fact_ids=self.required_fact_ids,
            required_evidence_ids=self.required_evidence_ids,
            relevant_failure_ids=self.relevant_failure_ids,
            relevant_relation_ids=self.relevant_relation_ids,
        )
        if self.seed_hash != expected:
            raise SchedulerError("seed_hash does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_hash": self.seed_hash,
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "route_id": self.route_id,
            "concrete_gap": self.concrete_gap,
            "output_contract": self.output_contract,
            "tool_environment_hash": self.tool_environment_hash,
            "acceptance_policy_hash": self.acceptance_policy_hash,
            "reference_versions_hash": self.reference_versions_hash,
            "limits": self.limits.to_dict(),
            "expand_evidence_content": self.expand_evidence_content,
            "expand_proof_content": self.expand_proof_content,
            "required_fact_ids": list(self.required_fact_ids),
            "required_evidence_ids": list(self.required_evidence_ids),
            "relevant_failure_ids": list(self.relevant_failure_ids),
            "relevant_relation_ids": list(self.relevant_relation_ids),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ContextSeed":
        payload = _mapping("ContextSeed", data)
        allowed = {
            "seed_hash",
            "contract_id",
            "obligation_id",
            "route_id",
            "concrete_gap",
            "output_contract",
            "tool_environment_hash",
            "acceptance_policy_hash",
            "reference_versions_hash",
            "limits",
            "expand_evidence_content",
            "expand_proof_content",
            "required_fact_ids",
            "required_evidence_ids",
            "relevant_failure_ids",
            "relevant_relation_ids",
        }
        _reject_extra(payload, allowed, "ContextSeed")
        _require_keys(payload, allowed - {"route_id", "expand_proof_content"}, "ContextSeed")
        return cls(
            seed_hash=payload["seed_hash"],
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            route_id=payload.get("route_id"),
            concrete_gap=payload["concrete_gap"],
            output_contract=payload["output_contract"],
            tool_environment_hash=payload["tool_environment_hash"],
            acceptance_policy_hash=payload["acceptance_policy_hash"],
            reference_versions_hash=payload["reference_versions_hash"],
            limits=ContextLimits.from_dict(payload["limits"]),
            expand_evidence_content=payload["expand_evidence_content"],
            expand_proof_content=bool(payload.get("expand_proof_content", False)),
            required_fact_ids=tuple(payload["required_fact_ids"]),
            required_evidence_ids=tuple(payload["required_evidence_ids"]),
            relevant_failure_ids=tuple(payload["relevant_failure_ids"]),
            relevant_relation_ids=tuple(payload["relevant_relation_ids"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ActiveWork:
    obligation_id: str
    mechanism_family: str
    action_id: str
    slot_kind: SlotKind

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self,
            "mechanism_family",
            _require_nonempty("mechanism_family", self.mechanism_family),
        )
        object.__setattr__(self, "action_id", _require_hex64("action_id", self.action_id))
        object.__setattr__(
            self, "slot_kind", _require_enum(SlotKind, self.slot_kind, "slot_kind")
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateAction:
    candidate_id: str
    action_spec: ActionSpec
    context_request: ContextRequest
    context_seed: ContextSeed
    obligation_id: str
    mechanism_family: str
    action_kind: ActionKind
    slot_kind: SlotKind
    budget_class: BudgetClass
    requested: ResourceVector
    priority_class: PriorityClass
    dependency_validity: DependencyValidity
    recurrence_recommendation: RecurrenceRecommendation
    recurrence_audit_required: bool
    created_sequence: int
    route_id: Optional[str] = None
    recurrence_assessment: Optional[RecurrenceAssessment] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", _require_nonempty("candidate_id", self.candidate_id)
        )
        if not isinstance(self.action_spec, ActionSpec):
            raise SchedulerError("action_spec must be an ActionSpec")
        if not isinstance(self.context_request, ContextRequest):
            raise SchedulerError("context_request must be a ContextRequest")
        if not isinstance(self.context_seed, ContextSeed):
            raise SchedulerError("context_seed must be a ContextSeed")
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self,
            "mechanism_family",
            _require_nonempty("mechanism_family", self.mechanism_family),
        )
        object.__setattr__(
            self, "action_kind", _require_enum(ActionKind, self.action_kind, "action_kind")
        )
        object.__setattr__(
            self, "slot_kind", _require_enum(SlotKind, self.slot_kind, "slot_kind")
        )
        object.__setattr__(
            self,
            "budget_class",
            _require_enum(BudgetClass, self.budget_class, "budget_class"),
        )
        if not isinstance(self.requested, ResourceVector):
            raise SchedulerError("requested must be a ResourceVector")
        object.__setattr__(
            self,
            "priority_class",
            _require_enum(PriorityClass, self.priority_class, "priority_class"),
        )
        object.__setattr__(
            self,
            "dependency_validity",
            _require_enum(
                DependencyValidity, self.dependency_validity, "dependency_validity"
            ),
        )
        object.__setattr__(
            self,
            "recurrence_recommendation",
            _require_enum(
                RecurrenceRecommendation,
                self.recurrence_recommendation,
                "recurrence_recommendation",
            ),
        )
        if not isinstance(self.recurrence_audit_required, bool):
            raise SchedulerError("recurrence_audit_required must be bool")
        object.__setattr__(
            self,
            "created_sequence",
            _require_nonneg_int("created_sequence", self.created_sequence),
        )
        object.__setattr__(self, "route_id", _optional_hex64("route_id", self.route_id))
        if self.slot_kind is not slot_kind_for_action(self.action_kind):
            raise SchedulerError("slot_kind does not match action_kind")
        if self.priority_class is PriorityClass.FINAL_ACCEPTANCE:
            if self.budget_class is not BudgetClass.FINAL_AUDIT:
                raise SchedulerError(
                    "FINAL_ACCEPTANCE requires BudgetClass.FINAL_AUDIT"
                )
            if self.action_kind is not ActionKind.VERIFY:
                raise SchedulerError("FINAL_ACCEPTANCE requires ActionKind.VERIFY")
            if self.slot_kind is not SlotKind.VERIFIER:
                raise SchedulerError("FINAL_ACCEPTANCE requires SlotKind.VERIFIER")
        if self.priority_class is PriorityClass.EXPLORATION:
            if self.budget_class is not BudgetClass.EXPLORATION:
                raise SchedulerError(
                    "EXPLORATION priority requires BudgetClass.EXPLORATION"
                )
        if self.action_spec.context_hash != self.context_seed.seed_hash:
            raise SchedulerError("ActionSpec.context_hash must equal ContextSeed.seed_hash")
        if self.context_request.action_id != self.action_spec.action_id:
            raise SchedulerError("ContextRequest.action_id must equal ActionSpec.action_id")
        if self.context_request.obligation_id != self.obligation_id:
            raise SchedulerError("obligation_id does not match ContextRequest")
        if self.context_seed.obligation_id != self.obligation_id:
            raise SchedulerError("obligation_id does not match ContextSeed")
        if self.context_request.contract_id != self.context_seed.contract_id:
            raise SchedulerError("contract_id does not match ContextSeed")
        if self.context_request.route_id != self.route_id:
            raise SchedulerError("route_id does not match ContextRequest")
        if self.context_seed.route_id != self.route_id:
            raise SchedulerError("route_id does not match ContextSeed")
        if (
            self.action_spec.obligation_id is not None
            and self.action_spec.obligation_id != self.obligation_id
        ):
            raise SchedulerError("ActionSpec.obligation_id does not match candidate")
        if self.action_spec.route_id is not None and self.action_spec.route_id != self.route_id:
            raise SchedulerError("ActionSpec.route_id does not match candidate")
        if (
            self.recurrence_recommendation in _AUDIT_REQUIRED
            and self.recurrence_audit_required is not True
        ):
            raise SchedulerError("audit-required recommendation must set audit_required")
        if (
            self.recurrence_assessment is not None
            and not isinstance(self.recurrence_assessment, RecurrenceAssessment)
        ):
            raise SchedulerError("recurrence_assessment must be RecurrenceAssessment")
        if self.recurrence_assessment is not None:
            if self.recurrence_assessment.recommendation is not self.recurrence_recommendation:
                raise SchedulerError("recurrence_assessment recommendation mismatch")
            if self.recurrence_assessment.audit_required is not self.recurrence_audit_required:
                raise SchedulerError("recurrence_assessment audit_required mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "action_spec": self.action_spec.to_dict(),
            "context_request": self.context_request.to_dict(),
            "context_seed": self.context_seed.to_dict(),
            "obligation_id": self.obligation_id,
            "route_id": self.route_id,
            "mechanism_family": self.mechanism_family,
            "action_kind": self.action_kind.value,
            "slot_kind": self.slot_kind.value,
            "budget_class": self.budget_class.value,
            "requested": self.requested.to_dict(),
            "priority_class": self.priority_class.value,
            "dependency_validity": self.dependency_validity.value,
            "recurrence_recommendation": self.recurrence_recommendation.value,
            "recurrence_audit_required": self.recurrence_audit_required,
            "created_sequence": self.created_sequence,
            "recurrence_assessment": (
                None
                if self.recurrence_assessment is None
                else _assessment_to_dict(self.recurrence_assessment)
            ),
        }

    @classmethod
    def from_dict(cls, data: object) -> "CandidateAction":
        payload = _mapping("CandidateAction", data)
        allowed = {
            "candidate_id",
            "action_spec",
            "context_request",
            "context_seed",
            "obligation_id",
            "route_id",
            "mechanism_family",
            "action_kind",
            "slot_kind",
            "budget_class",
            "requested",
            "priority_class",
            "dependency_validity",
            "recurrence_recommendation",
            "recurrence_audit_required",
            "created_sequence",
            "recurrence_assessment",
        }
        _reject_extra(payload, allowed, "CandidateAction")
        _require_keys(payload, allowed - {"route_id", "recurrence_assessment"}, "CandidateAction")
        raw_assessment = payload.get("recurrence_assessment")
        return cls(
            candidate_id=payload["candidate_id"],
            action_spec=ActionSpec.from_dict(payload["action_spec"]),
            context_request=ContextRequest.from_dict(payload["context_request"]),
            context_seed=ContextSeed.from_dict(payload["context_seed"]),
            obligation_id=payload["obligation_id"],
            route_id=payload.get("route_id"),
            mechanism_family=payload["mechanism_family"],
            action_kind=payload["action_kind"],
            slot_kind=payload["slot_kind"],
            budget_class=payload["budget_class"],
            requested=ResourceVector.from_dict(payload["requested"]),
            priority_class=payload["priority_class"],
            dependency_validity=payload["dependency_validity"],
            recurrence_recommendation=payload["recurrence_recommendation"],
            recurrence_audit_required=payload["recurrence_audit_required"],
            created_sequence=payload["created_sequence"],
            recurrence_assessment=(
                None if raw_assessment is None else _assessment_from_dict(raw_assessment)
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateDecision:
    candidate_id: str
    disposition: ScheduleDisposition
    reason: ScheduleReason
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", _require_nonempty("candidate_id", self.candidate_id)
        )
        object.__setattr__(
            self,
            "disposition",
            _require_enum(ScheduleDisposition, self.disposition, "disposition"),
        )
        object.__setattr__(
            self, "reason", _require_enum(ScheduleReason, self.reason, "reason")
        )
        object.__setattr__(self, "detail", _require_nonempty("detail", self.detail))
        if self.disposition is ScheduleDisposition.SELECTED:
            if self.reason is not ScheduleReason.SELECTED:
                raise SchedulerError("SELECTED disposition requires SELECTED reason")
        else:
            if self.reason is ScheduleReason.SELECTED:
                raise SchedulerError("SELECTED reason requires SELECTED disposition")
        if self.reason is ScheduleReason.MALFORMED:
            if self.disposition is not ScheduleDisposition.REJECTED:
                raise SchedulerError("MALFORMED must be REJECTED")
        if self.reason in {ScheduleReason.STALE_DEPENDENCY, ScheduleReason.MISSING_DEPENDENCY}:
            if self.disposition is not ScheduleDisposition.REJECTED:
                raise SchedulerError("stale/missing dependency must be REJECTED")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CandidateDecision":
        payload = _mapping("CandidateDecision", data)
        allowed = {"candidate_id", "disposition", "reason", "detail"}
        _reject_extra(payload, allowed, "CandidateDecision")
        _require_keys(payload, allowed, "CandidateDecision")
        return cls(
            candidate_id=payload["candidate_id"],
            disposition=payload["disposition"],
            reason=payload["reason"],
            detail=payload["detail"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchPlan:
    decisions: tuple[CandidateDecision, ...]
    selected_candidate_ids: tuple[str, ...]
    idle: bool

    def __post_init__(self) -> None:
        if isinstance(self.decisions, str) or not isinstance(self.decisions, tuple):
            raise SchedulerError("decisions must be a tuple of CandidateDecision")
        for index, item in enumerate(self.decisions):
            if not isinstance(item, CandidateDecision):
                raise SchedulerError(f"decisions[{index}] must be a CandidateDecision")
        ids = tuple(item.candidate_id for item in self.decisions)
        if len(ids) != len(set(ids)):
            raise SchedulerError("DispatchPlan has duplicate candidate_id")
        ordered = tuple(sorted(self.decisions, key=lambda item: item.candidate_id))
        object.__setattr__(self, "decisions", ordered)
        if isinstance(self.selected_candidate_ids, str) or not isinstance(
            self.selected_candidate_ids, tuple
        ):
            raise SchedulerError("selected_candidate_ids must be a tuple of str")
        selected = tuple(
            _require_nonempty(f"selected_candidate_ids[{i}]", item)
            for i, item in enumerate(self.selected_candidate_ids)
        )
        object.__setattr__(self, "selected_candidate_ids", selected)
        if not isinstance(self.idle, bool):
            raise SchedulerError("idle must be bool")
        from_decisions = tuple(
            item.candidate_id
            for item in ordered
            if item.disposition is ScheduleDisposition.SELECTED
        )
        if set(selected) != set(from_decisions):
            raise SchedulerError("selected_candidate_ids must match SELECTED decisions")
        if self.idle is not (len(selected) == 0):
            raise SchedulerError("idle must be True exactly when nothing is selected")

    def decision_for(self, candidate_id: str) -> CandidateDecision:
        for item in self.decisions:
            if item.candidate_id == candidate_id:
                return item
        raise SchedulerError(f"no decision for candidate {candidate_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": [item.to_dict() for item in self.decisions],
            "selected_candidate_ids": list(self.selected_candidate_ids),
            "idle": self.idle,
        }

    @classmethod
    def from_dict(cls, data: object) -> "DispatchPlan":
        payload = _mapping("DispatchPlan", data)
        allowed = {"decisions", "selected_candidate_ids", "idle"}
        _reject_extra(payload, allowed, "DispatchPlan")
        _require_keys(payload, allowed, "DispatchPlan")
        raw_decisions = payload["decisions"]
        if isinstance(raw_decisions, str) or not isinstance(raw_decisions, list):
            raise SchedulerError("decisions must be a list of CandidateDecision")
        return cls(
            decisions=tuple(CandidateDecision.from_dict(item) for item in raw_decisions),
            selected_candidate_ids=tuple(payload["selected_candidate_ids"]),
            idle=payload["idle"],
        )


def slot_kind_for_action(kind: ActionKind) -> SlotKind:
    action = _require_enum(ActionKind, kind, "action_kind")
    return _SLOT_FOR_ACTION[action.value]


def build_action_context_pair(
    *,
    contract_id: str,
    obligation_id: str,
    concrete_gap: str,
    output_contract: str,
    tool_environment_hash: str,
    acceptance_policy_hash: str,
    reference_versions_hash: str,
    limits: ContextLimits,
    action_type: str,
    mechanism: str,
    model_id: str,
    effort: str,
    task_mode: str,
    parameters_json: str,
    dependency_hash: str,
    definitions_hash: str,
    expand_evidence_content: bool = False,
    expand_proof_content: bool = False,
    required_fact_ids: Sequence[str] = (),
    required_evidence_ids: Sequence[str] = (),
    relevant_failure_ids: Sequence[str] = (),
    relevant_relation_ids: Sequence[str] = (),
    route_id: Optional[str] = None,
    sampling_nonce: Optional[str] = None,
    sampling_mode: SamplingMode = SamplingMode.DEFAULT,
    recalled: RecalledSelection | None = None,
) -> tuple[ActionSpec, ContextRequest, ContextSeed]:
    """Build ContextSeed, then ActionSpec, then ContextRequest(action_id).

    The compiler cache key is not computed here and must not be written back
    into ActionSpec.context_hash.
    """
    if not isinstance(limits, ContextLimits):
        raise SchedulerError("limits must be ContextLimits")
    if not isinstance(expand_evidence_content, bool):
        raise SchedulerError("expand_evidence_content must be bool")
    if not isinstance(expand_proof_content, bool):
        raise SchedulerError("expand_proof_content must be bool")
    fact_ids = tuple(required_fact_ids)
    evidence_ids = tuple(required_evidence_ids)
    failure_ids = tuple(relevant_failure_ids)
    relation_ids = tuple(relevant_relation_ids)
    if recalled is not None:
        if not isinstance(recalled, RecalledSelection):
            raise SchedulerError("recalled must be a RecalledSelection")
        fact_ids = _merge_ids(fact_ids, recalled.required_fact_ids)
        evidence_ids = _merge_ids(evidence_ids, recalled.required_evidence_ids)
        failure_ids = _merge_ids(failure_ids, recalled.relevant_failure_ids)
        relation_ids = _merge_ids(relation_ids, recalled.relevant_relation_ids)
    seed = ContextSeed(
        seed_hash=_seed_hash_from_fields(
            contract_id=contract_id,
            obligation_id=obligation_id,
            route_id=route_id,
            concrete_gap=concrete_gap,
            output_contract=output_contract,
            tool_environment_hash=tool_environment_hash,
            acceptance_policy_hash=acceptance_policy_hash,
            reference_versions_hash=reference_versions_hash,
            limits=limits,
            expand_evidence_content=expand_evidence_content,
            expand_proof_content=expand_proof_content,
            required_fact_ids=fact_ids,
            required_evidence_ids=evidence_ids,
            relevant_failure_ids=failure_ids,
            relevant_relation_ids=relation_ids,
        ),
        contract_id=contract_id,
        obligation_id=obligation_id,
        concrete_gap=concrete_gap,
        output_contract=output_contract,
        tool_environment_hash=tool_environment_hash,
        acceptance_policy_hash=acceptance_policy_hash,
        reference_versions_hash=reference_versions_hash,
        limits=limits,
        expand_evidence_content=expand_evidence_content,
        expand_proof_content=expand_proof_content,
        required_fact_ids=fact_ids,
        required_evidence_ids=evidence_ids,
        relevant_failure_ids=failure_ids,
        relevant_relation_ids=relation_ids,
        route_id=route_id,
    )
    try:
        spec = ActionSpec.build(
            action_type=action_type,
            mechanism=mechanism,
            model_id=model_id,
            effort=effort,
            task_mode=task_mode,
            context_hash=seed.seed_hash,
            dependency_hash=dependency_hash,
            definitions_hash=definitions_hash,
            tool_environment_hash=tool_environment_hash,
            parameters_json=parameters_json,
            obligation_id=obligation_id,
            route_id=route_id,
            sampling_nonce=sampling_nonce,
            sampling_mode=sampling_mode,
        )
    except SchemaValidationError as exc:
        raise SchedulerError(f"invalid ActionSpec: {exc}") from exc
    request_id = stable_id(_REQUEST_DOMAIN, _REQUEST_VERSION, spec.action_id, seed.seed_hash)
    try:
        request = ContextRequest(
            request_id=request_id,
            contract_id=seed.contract_id,
            obligation_id=seed.obligation_id,
            action_id=spec.action_id,
            concrete_gap=seed.concrete_gap,
            output_contract=seed.output_contract,
            tool_environment_hash=seed.tool_environment_hash,
            acceptance_policy_hash=seed.acceptance_policy_hash,
            reference_versions_hash=seed.reference_versions_hash,
            expand_evidence_content=seed.expand_evidence_content,
            expand_proof_content=seed.expand_proof_content,
            limits=seed.limits,
            required_fact_ids=seed.required_fact_ids,
            required_evidence_ids=seed.required_evidence_ids,
            relevant_failure_ids=seed.relevant_failure_ids,
            relevant_relation_ids=seed.relevant_relation_ids,
            route_id=seed.route_id,
        )
    except Exception as exc:
        raise SchedulerError(f"invalid ContextRequest: {exc}") from exc
    return spec, request, seed


def build_recalled_action_context_pair(
    *,
    view: ResearchView,
    snapshot: object,
    obligation_id: str,
    **kwargs: Any,
) -> tuple[ActionSpec, ContextRequest, ContextSeed]:
    """Infer dependency/blocker IDs then build the seed/spec/request triple.

    Parent wires this into controller dispatch so recall is used by actual
    scheduling rather than remaining a unit-only helper.
    """
    from euler.control.adapters.project import ProjectSnapshot

    if not isinstance(view, ResearchView):
        raise SchedulerError("view must be a ResearchView")
    if not isinstance(snapshot, ProjectSnapshot):
        raise SchedulerError("snapshot must be a ProjectSnapshot")
    obligation = view.get_obligation(obligation_id)
    if obligation is None:
        raise SchedulerError(f"missing obligation {obligation_id}")
    route_id = kwargs.get("route_id")
    route = None if route_id is None else view.get_route(str(route_id))
    recalled = recall_for_action(
        view=view,
        snapshot=snapshot,
        obligation=obligation,
        route=route,
        explicit_fact_ids=tuple(kwargs.get("required_fact_ids") or ()),
        explicit_evidence_ids=tuple(kwargs.get("required_evidence_ids") or ()),
        explicit_failure_ids=tuple(kwargs.get("relevant_failure_ids") or ()),
        explicit_relation_ids=tuple(kwargs.get("relevant_relation_ids") or ()),
    )
    kwargs["recalled"] = recalled
    kwargs["obligation_id"] = obligation_id
    kwargs.setdefault("contract_id", obligation.contract_id)
    return build_action_context_pair(**kwargs)


def rebind_candidate_with_recall(
    candidate: CandidateAction,
    view: ResearchView,
    snapshot: object,
) -> CandidateAction:
    """Bind inferred dependency/blocker IDs into seed, ActionSpec, and request.

    Action identity hashes include the recalled IDs so compilation cannot
    execute a selected action with unbound required assumptions.
    """
    if not isinstance(candidate, CandidateAction):
        raise SchedulerError("candidate must be a CandidateAction")
    if not isinstance(view, ResearchView):
        raise SchedulerError("view must be a ResearchView")
    obligation = view.get_obligation(candidate.obligation_id)
    if obligation is None:
        raise SchedulerError(f"missing obligation {candidate.obligation_id}")
    route = None
    if candidate.route_id is not None:
        route = view.get_route(candidate.route_id)
    spec = candidate.action_spec
    seed = candidate.context_seed
    new_spec, request, new_seed = build_action_context_pair(
        contract_id=seed.contract_id,
        obligation_id=candidate.obligation_id,
        concrete_gap=seed.concrete_gap,
        output_contract=seed.output_contract,
        tool_environment_hash=seed.tool_environment_hash,
        acceptance_policy_hash=seed.acceptance_policy_hash,
        reference_versions_hash=seed.reference_versions_hash,
        limits=seed.limits,
        action_type=spec.action_type,
        mechanism=spec.mechanism,
        model_id=spec.model_id,
        effort=spec.effort,
        task_mode=spec.task_mode,
        parameters_json=spec.parameters_json,
        dependency_hash=spec.dependency_hash,
        definitions_hash=spec.definitions_hash,
        expand_evidence_content=seed.expand_evidence_content,
        expand_proof_content=seed.expand_proof_content,
        required_fact_ids=seed.required_fact_ids,
        required_evidence_ids=seed.required_evidence_ids,
        relevant_failure_ids=seed.relevant_failure_ids,
        relevant_relation_ids=seed.relevant_relation_ids,
        route_id=candidate.route_id,
        sampling_nonce=spec.sampling_nonce,
        sampling_mode=spec.sampling_mode,
        recalled=recall_for_action(
            view=view,
            snapshot=snapshot,
            obligation=obligation,
            route=route,
            explicit_fact_ids=seed.required_fact_ids,
            explicit_evidence_ids=seed.required_evidence_ids,
            explicit_failure_ids=seed.relevant_failure_ids,
            explicit_relation_ids=seed.relevant_relation_ids,
        ),
    )
    return replace(
        candidate,
        action_spec=new_spec,
        context_request=request,
        context_seed=new_seed,
    )


class TransparentScheduler:
    """Pure, deterministic candidate selection. Does not launch providers."""

    def select(
        self,
        candidates: Sequence[CandidateAction],
        *,
        policy: BudgetPolicy,
        snapshot: ResourceSnapshot,
        now_utc: str,
        dedupe_index: ActionAttemptIndex | None = None,
        active: Sequence[ActiveWork] = (),
        view: ResearchView | None = None,
        history: RecurrenceHistory | None = None,
        stagnating: bool = False,
    ) -> DispatchPlan:
        if not isinstance(policy, BudgetPolicy):
            raise SchedulerError("policy must be a BudgetPolicy")
        if not isinstance(snapshot, ResourceSnapshot):
            raise SchedulerError("snapshot must be a ResourceSnapshot")
        if not isinstance(now_utc, str) or now_utc == "":
            raise SchedulerError("now_utc must be a nonempty UTC timestamp")
        policy.deadline_reached(now_utc)
        if not isinstance(stagnating, bool):
            raise SchedulerError("stagnating must be bool")
        if view is not None and not isinstance(view, ResearchView):
            raise SchedulerError("view must be a ResearchView")
        if history is not None and not isinstance(history, RecurrenceHistory):
            raise SchedulerError("history must be a RecurrenceHistory")
        if dedupe_index is not None and not isinstance(dedupe_index, ActionAttemptIndex):
            raise SchedulerError("dedupe_index must be an ActionAttemptIndex")
        items = _require_candidate_sequence(candidates)
        ids = [item.candidate_id for item in items]
        if len(ids) != len(set(ids)):
            raise SchedulerError("duplicate candidate_id")
        active_work = _require_active(active)
        # history is accepted only as a read-only witness; never evaluate/write it.
        _ = history
        return _select(
            items,
            policy=policy,
            snapshot=snapshot,
            now_utc=now_utc,
            dedupe_index=dedupe_index,
            active=active_work,
            view=view,
            stagnating=stagnating,
        )


def _select(
    items: tuple[CandidateAction, ...],
    *,
    policy: BudgetPolicy,
    snapshot: ResourceSnapshot,
    now_utc: str,
    dedupe_index: ActionAttemptIndex | None,
    active: tuple[ActiveWork, ...],
    view: ResearchView | None,
    stagnating: bool,
) -> DispatchPlan:
    decisions: dict[str, CandidateDecision] = {}
    eligible: list[CandidateAction] = []
    for candidate in sorted(items, key=_sort_key):
        independent = _independent_reason(candidate, dedupe_index=dedupe_index, view=view)
        if independent is not None:
            decisions[candidate.candidate_id] = independent
            continue
        eligible.append(candidate)

    working = snapshot
    selected: list[str] = []
    occupied_keys = {(item.obligation_id, item.mechanism_family) for item in active}
    occupied_actions = {item.action_id for item in active}
    deadline_hit = policy.deadline_reached(now_utc)
    for candidate in eligible:
        greedy = _greedy_reason(
            candidate,
            policy=policy,
            snapshot=working,
            deadline_hit=deadline_hit,
            stagnating=stagnating,
            occupied_keys=occupied_keys,
            occupied_actions=occupied_actions,
        )
        if greedy is not None:
            decisions[candidate.candidate_id] = greedy
            continue
        try:
            working = working.reserve(
                candidate.requested,
                budget_class=candidate.budget_class,
                slot_kind=candidate.slot_kind,
                policy=policy,
            )
        except PolicyError:
            decisions[candidate.candidate_id] = _decision(
                candidate.candidate_id,
                ScheduleDisposition.DEFERRED,
                ScheduleReason.BUDGET_EXCEEDED,
                "reservation failed after admission checks",
            )
            continue
        occupied_keys.add((candidate.obligation_id, candidate.mechanism_family))
        occupied_actions.add(candidate.action_spec.action_id)
        selected.append(candidate.candidate_id)
        decisions[candidate.candidate_id] = _decision(
            candidate.candidate_id,
            ScheduleDisposition.SELECTED,
            ScheduleReason.SELECTED,
            "selected by transparent priority and resource rules",
        )

    missing = [item.candidate_id for item in items if item.candidate_id not in decisions]
    for candidate_id in missing:
        decisions[candidate_id] = _decision(
            candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.NOT_SELECTED,
            "not selected",
        )
    ordered_decisions = tuple(
        decisions[candidate_id]
        for candidate_id in sorted(decisions)
    )
    return DispatchPlan(
        decisions=ordered_decisions,
        selected_candidate_ids=tuple(selected),
        idle=len(selected) == 0,
    )


def _independent_reason(
    candidate: CandidateAction,
    *,
    dedupe_index: ActionAttemptIndex | None,
    view: ResearchView | None,
) -> CandidateDecision | None:
    dependency = _dependency_status(candidate, view)
    if dependency is DependencyValidity.MISSING:
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.REJECTED,
            ScheduleReason.MISSING_DEPENDENCY,
            "missing obligation or route dependency",
        )
    if dependency is DependencyValidity.STALE:
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.REJECTED,
            ScheduleReason.STALE_DEPENDENCY,
            "stale obligation or route dependency",
        )
    if dedupe_index is not None:
        try:
            verdict = dedupe_index.decide(candidate.action_spec)
        except SchemaValidationError as exc:
            return _decision(
                candidate.candidate_id,
                ScheduleDisposition.REJECTED,
                ScheduleReason.MALFORMED,
                f"dedupe rejected ActionSpec: {exc}",
            )
        if verdict is DedupeDecision.WAIT_FOR_EXISTING:
            return _decision(
                candidate.candidate_id,
                ScheduleDisposition.DEFERRED,
                ScheduleReason.DEDUPE_WAIT,
                "identical action is already pending or running",
            )
        if verdict is DedupeDecision.RECOVER_OR_REUSE:
            recovered = None
            try:
                recovered = dedupe_index.recovered_record(candidate.action_spec)
            except SchemaValidationError:
                recovered = None
            if recovered is not None:
                refs = ",".join(recovered.artifact_refs) or "none"
                detail = (
                    f"reuse attempt {recovered.attempt_id} artifacts={refs} "
                    f"obligation={candidate.obligation_id}; do not re-execute"
                )
            else:
                detail = (
                    f"identical action already succeeded for obligation "
                    f"{candidate.obligation_id}; do not re-execute"
                )
            return _decision(
                candidate.candidate_id,
                ScheduleDisposition.DEFERRED,
                ScheduleReason.DEDUPE_RECOVER,
                detail,
            )
        if verdict is DedupeDecision.RETRY_TRANSPORT:
            return None
        if verdict is DedupeDecision.ALLOW_NEW_SAMPLE:
            return None
    if candidate.recurrence_audit_required and candidate.action_kind is not ActionKind.AUDIT:
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.RECURRENCE_AUDIT,
            "audit-required recurrence defers automatic work; explicit AUDIT only",
        )
    return None


def _greedy_reason(
    candidate: CandidateAction,
    *,
    policy: BudgetPolicy,
    snapshot: ResourceSnapshot,
    deadline_hit: bool,
    stagnating: bool,
    occupied_keys: set[tuple[str, str]],
    occupied_actions: set[str],
) -> CandidateDecision | None:
    block = snapshot.reservation_block(
        candidate.requested,
        budget_class=candidate.budget_class,
        slot_kind=candidate.slot_kind,
        policy=policy,
    )
    if block == "budget":
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.BUDGET_EXCEEDED,
            "requested resources exceed remaining limited capacity",
        )
    if block == "reserve":
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.CLASS_RESERVE,
            "NORMAL/other class must not consume exclusive final-audit or exploration reserve",
        )
    if deadline_hit and _deadline_blocks(candidate, policy):
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.DEADLINE,
            "deadline rejects new normal work; final audit is preserved",
        )
    if block == "slot":
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.SLOT_EXHAUSTED,
            f"{candidate.slot_kind.value} slot cap reached; idle is allowed",
        )
    if stagnating and _stagnation_blocks(candidate, policy):
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.STAGNATION,
            "stagnation policy defers exploration",
        )
    action_id = candidate.action_spec.action_id
    if action_id in occupied_actions:
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.DUPLICATE_ACTION,
            "identical action_id is already in flight",
        )
    key = (candidate.obligation_id, candidate.mechanism_family)
    if key in occupied_keys:
        return _decision(
            candidate.candidate_id,
            ScheduleDisposition.DEFERRED,
            ScheduleReason.MECHANISM_CONFLICT,
            "same obligation and mechanism family cannot run concurrently",
        )
    return None


def _deadline_exempt(candidate: CandidateAction) -> bool:
    if candidate.priority_class is PriorityClass.FINAL_ACCEPTANCE:
        return True
    return (
        candidate.budget_class is BudgetClass.FINAL_AUDIT
        and candidate.action_kind is ActionKind.VERIFY
    )


def _deadline_blocks(candidate: CandidateAction, policy: BudgetPolicy) -> bool:
    if policy.on_deadline is OnDeadlinePolicy.ALLOW:
        return False
    if _deadline_exempt(candidate):
        return False
    if policy.on_deadline is OnDeadlinePolicy.REJECT_ALL:
        return True
    if candidate.budget_class is BudgetClass.FINAL_AUDIT:
        return False
    return True


def _stagnation_blocks(candidate: CandidateAction, policy: BudgetPolicy) -> bool:
    if policy.on_stagnation is OnStagnationPolicy.CONTINUE:
        return False
    return (
        candidate.budget_class is BudgetClass.EXPLORATION
        or candidate.priority_class is PriorityClass.EXPLORATION
    )


def _dependency_status(
    candidate: CandidateAction, view: ResearchView | None
) -> DependencyValidity:
    status = candidate.dependency_validity
    if view is None:
        return status
    obligation = view.get_obligation(candidate.obligation_id)
    if obligation is None:
        return DependencyValidity.MISSING
    stale = view.stale_dependencies()
    if candidate.obligation_id in stale.obligation_ids:
        return DependencyValidity.STALE
    if candidate.route_id is not None:
        route = view.get_route(candidate.route_id)
        if route is None:
            return DependencyValidity.MISSING
        if candidate.route_id in stale.route_ids:
            return DependencyValidity.STALE
        if view.route_readiness(candidate.route_id) is RouteReadiness.STALE:
            return DependencyValidity.STALE
    return status


def _decision(
    candidate_id: str,
    disposition: ScheduleDisposition,
    reason: ScheduleReason,
    detail: str,
) -> CandidateDecision:
    return CandidateDecision(
        candidate_id=candidate_id,
        disposition=disposition,
        reason=reason,
        detail=detail,
    )


def _sort_key(candidate: CandidateAction) -> tuple[int, int, str]:
    return (
        _PRIORITY_RANK[candidate.priority_class.value],
        candidate.created_sequence,
        candidate.candidate_id,
    )


def _seed_hash_from_fields(
    *,
    contract_id: object,
    obligation_id: object,
    route_id: object,
    concrete_gap: object,
    output_contract: object,
    tool_environment_hash: object,
    acceptance_policy_hash: object,
    reference_versions_hash: object,
    limits: ContextLimits,
    expand_evidence_content: bool,
    expand_proof_content: bool = False,
    required_fact_ids: object = (),
    required_evidence_ids: object = (),
    relevant_failure_ids: object = (),
    relevant_relation_ids: object = (),
) -> str:
    payload = {
        "contract_id": _require_hex64("contract_id", contract_id),
        "obligation_id": _require_hex64("obligation_id", obligation_id),
        "route_id": None if route_id is None else _require_hex64("route_id", route_id),
        "concrete_gap": _require_nonempty("concrete_gap", concrete_gap),
        "output_contract": _require_nonempty("output_contract", output_contract),
        "tool_environment_hash": _require_hex64(
            "tool_environment_hash", tool_environment_hash
        ),
        "acceptance_policy_hash": _require_hex64(
            "acceptance_policy_hash", acceptance_policy_hash
        ),
        "reference_versions_hash": _require_hex64(
            "reference_versions_hash", reference_versions_hash
        ),
        "limits": limits.to_dict(),
        "packet_policy": {
            "expand_evidence_content": expand_evidence_content,
            "expand_proof_content": expand_proof_content,
        },
        "required_fact_ids": list(
            _require_id_tuple("required_fact_ids", required_fact_ids, hex64=False)
        ),
        "required_evidence_ids": list(
            _require_id_tuple("required_evidence_ids", required_evidence_ids)
        ),
        "relevant_failure_ids": list(
            _require_id_tuple("relevant_failure_ids", relevant_failure_ids)
        ),
        "relevant_relation_ids": list(
            _require_id_tuple("relevant_relation_ids", relevant_relation_ids)
        ),
    }
    return stable_id(_SEED_DOMAIN, _SEED_VERSION, canonical_json(payload))


def _merge_ids(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in (*left, *right):
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


def _require_candidate_sequence(value: object) -> tuple[CandidateAction, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise SchedulerError("candidates must be a sequence of CandidateAction")
    out: list[CandidateAction] = []
    for index, item in enumerate(value):
        if not isinstance(item, CandidateAction):
            raise SchedulerError(f"candidates[{index}] must be a CandidateAction")
        out.append(item)
    return tuple(out)


def _require_active(value: object) -> tuple[ActiveWork, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise SchedulerError("active must be a sequence of ActiveWork")
    out: list[ActiveWork] = []
    for index, item in enumerate(value):
        if not isinstance(item, ActiveWork):
            raise SchedulerError(f"active[{index}] must be an ActiveWork")
        out.append(item)
    return tuple(out)


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SchedulerError(f"{name} must be a nonempty str")
    return value


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise SchedulerError(f"{name} must be 64-char lowercase hex")
    return text


def _optional_hex64(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_hex64(name, value)


def _require_id_tuple(name: str, value: object, *, hex64: bool = True) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchedulerError(f"{name} must be a list of IDs")
    out: list[str] = []
    for index, item in enumerate(value):
        if hex64:
            out.append(_require_hex64(f"{name}[{index}]", item))
        else:
            out.append(_require_nonempty(f"{name}[{index}]", item))
    return tuple(out)


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchedulerError(
            f"{name} must be a nonnegative int, not {type(value).__name__}"
        )
    if value < 0:
        raise SchedulerError(f"{name} must be a nonnegative int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise SchedulerError(f"{name} has unknown value {value!r}") from None
    raise SchedulerError(f"{name} must be {enum_cls.__name__}")


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise SchedulerError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise SchedulerError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise SchedulerError(f"{label} missing fields: {sorted(missing)}")


def _assessment_to_dict(item: RecurrenceAssessment) -> dict[str, Any]:
    return {
        "classification": item.classification.value,
        "recommendation": item.recommendation.value,
        "audit_required": item.audit_required,
        "strike_count": item.strike_count,
        "strike_key": {
            "key_id": item.strike_key.key_id,
            "nodes": list(item.strike_key.nodes),
            "premise_hash": item.strike_key.premise_hash,
        },
        "fingerprint": {
            "evidence_hash": item.fingerprint.evidence_hash,
            "premise_hash": item.fingerprint.premise_hash,
            "tool_hash": item.fingerprint.tool_hash,
        },
        "report": {
            "nodes": list(item.report.nodes),
            "internal_edges": [edge.identity_payload() for edge in item.report.internal_edges],
            "external_exits": list(item.report.external_exits),
            "descent_edges": [edge.identity_payload() for edge in item.report.descent_edges],
            "classification": item.report.classification.value,
            "evidence_frontier_hash": item.report.evidence_frontier_hash,
            "premise_hash": item.report.premise_hash,
            "tool_hash": item.report.tool_hash,
            "nontrivial": item.report.nontrivial,
            "scopes_compatible": item.report.scopes_compatible,
        },
        "exemptions": list(item.exemptions),
        "exhausted_route_ids": list(item.exhausted_route_ids),
        "park_is_falsity": item.park_is_falsity,
        "reopen_conditions": list(item.reopen_conditions),
        "observation_id": item.observation_id,
        "blocked_mechanism": item.blocked_mechanism,
    }


def _assessment_from_dict(data: object) -> RecurrenceAssessment:
    payload = _mapping("recurrence_assessment", data)
    allowed = {
        "classification",
        "recommendation",
        "audit_required",
        "strike_count",
        "strike_key",
        "fingerprint",
        "report",
        "exemptions",
        "exhausted_route_ids",
        "park_is_falsity",
        "reopen_conditions",
        "observation_id",
        "blocked_mechanism",
    }
    _reject_extra(payload, allowed, "recurrence_assessment")
    _require_keys(
        payload,
        allowed
        - {
            "park_is_falsity",
            "reopen_conditions",
            "observation_id",
            "blocked_mechanism",
        },
        "recurrence_assessment",
    )
    key_payload = _mapping("strike_key", payload["strike_key"])
    finger_payload = _mapping("fingerprint", payload["fingerprint"])
    report_payload = _mapping("report", payload["report"])
    return RecurrenceAssessment(
        classification=SCCClassification(payload["classification"]),
        recommendation=RecurrenceRecommendation(payload["recommendation"]),
        audit_required=payload["audit_required"],
        strike_count=payload["strike_count"],
        strike_key=StrikeKey(
            key_id=key_payload["key_id"],
            nodes=tuple(key_payload["nodes"]),
            premise_hash=key_payload["premise_hash"],
        ),
        fingerprint=FrontierFingerprint(
            evidence_hash=finger_payload["evidence_hash"],
            premise_hash=finger_payload["premise_hash"],
            tool_hash=finger_payload["tool_hash"],
        ),
        report=SCCReport(
            nodes=tuple(report_payload["nodes"]),
            internal_edges=tuple(
                _edge_from_dict(item) for item in report_payload["internal_edges"]
            ),
            external_exits=tuple(report_payload["external_exits"]),
            descent_edges=tuple(
                _edge_from_dict(item) for item in report_payload["descent_edges"]
            ),
            classification=SCCClassification(report_payload["classification"]),
            evidence_frontier_hash=report_payload["evidence_frontier_hash"],
            premise_hash=report_payload["premise_hash"],
            tool_hash=report_payload["tool_hash"],
            nontrivial=report_payload["nontrivial"],
            scopes_compatible=report_payload["scopes_compatible"],
        ),
        exemptions=tuple(payload["exemptions"]),
        exhausted_route_ids=tuple(payload["exhausted_route_ids"]),
        park_is_falsity=bool(payload.get("park_is_falsity", False)),
        reopen_conditions=tuple(payload.get("reopen_conditions") or ()),
        observation_id=payload.get("observation_id"),
        blocked_mechanism=bool(payload.get("blocked_mechanism", False)),
    )


def _edge_from_dict(data: object) -> ProjectedEdge:
    payload = _mapping("ProjectedEdge", data)
    sides = payload.get("side_conditions") or ()
    return ProjectedEdge(
        kind=ProjectedEdgeKind(payload["kind"]),
        source=payload["source"],
        target=payload["target"],
        route_id=payload.get("route_id"),
        relation_id=payload.get("relation_id"),
        legal_descent=payload.get("legal_descent", False),
        well_founded_measure=payload.get("well_founded_measure"),
        side_conditions=tuple(sides),
    )
