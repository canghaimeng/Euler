"""Final acceptance, conservative outcomes, and checkpoint records.

Stdlib only plus existing research-control types. Pure reads of ResearchView,
ProjectSnapshot, ArtifactStore, and RuntimeSnapshot. Never imports
gateway/verify, never writes Fact Graph, EventStore, or project files, and never
calls a model or network service.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from euler.control.adapters.project import ProjectSnapshot
from euler.control.artifacts import ArtifactStore, ArtifactStoreError
from euler.control.compute import (
    ComputeEvidenceKind,
    ComputeRunResult,
    ComputeTermination,
)
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    TargetKind,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.kernel_evidence import (
    KernelCheckArtifact,
    KernelCheckStatus,
    KernelEvidenceError,
    StatementAlignmentAttestation,
    load_kernel_artifact,
    require_trusted_kernel_artifact,
)
from euler.control.ids import canonical_json, sha256_hex, stable_id
from euler.control.obligations import Obligation, ObligationStatus, RouteStatus
from euler.control.policy import (
    RESOURCE_DIMENSIONS,
    BudgetPolicy,
    ResourceLimitKind,
    ResourceSnapshot,
)
from euler.control.runtime import LeaseStatus, RuntimeSnapshot
from euler.control.schema import MathematicalStatus
from euler.control.views import ResearchView

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC = timezone.utc
_ZERO = timedelta(0)
_AUDIT_DOMAIN = "final-audit"
_ACCEPTANCE_DOMAIN = "acceptance"
_CHECKPOINT_DOMAIN = "checkpoint"
_ID_VERSION = 1
_VERIFICATION_KIND = "verification"
_FACT_SUBMIT = "fact_submit"
_TRANSPORT_OK = "ok"
_VERDICT_CORRECT = "correct"
_VERDICT_WRONG = "wrong"
_FORBIDDEN_PHRASES = (
    "mathematically impossible",
    "unsolvable",
    "proof of no solution",
)

__all__ = [
    "AcceptanceError",
    "FinalAuditKind",
    "AcceptanceVerdict",
    "ProjectOutcome",
    "FactAdmissionEvidence",
    "FinalAuditReceipt",
    "AcceptanceRecord",
    "StallEvidence",
    "ExternalInputEvidence",
    "InfrastructureBlockEvidence",
    "OutcomeEvidence",
    "CheckpointReport",
    "collect_fact_admissions",
    "collect_statement_alignments",
    "promote_compute_counterexample",
    "assess_final_acceptance",
    "derive_project_outcome",
    "build_checkpoint_report",
    "kernel_acceptance_required",
]


class AcceptanceError(ValueError):
    """Malformed acceptance record or illegal pure-function input."""


class FinalAuditKind(str, Enum):
    PROOF = "PROOF"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"


class AcceptanceVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ProjectOutcome(str, Enum):
    SOLVED = "SOLVED"
    REFUTED = "REFUTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FRONTIER_STALLED = "FRONTIER_STALLED"
    NEEDS_EXTERNAL_INPUT = "NEEDS_EXTERNAL_INPUT"
    INFRA_BLOCKED = "INFRA_BLOCKED"


@dataclass(frozen=True, slots=True, kw_only=True)
class FactAdmissionEvidence:
    record_id: str
    provenance: str
    transport_status: str
    verdict: Optional[str]
    fact_id: Optional[str]
    exact_claim: str
    write_error: Optional[str]
    accepted: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record_id", _require_nonempty("record_id", self.record_id)
        )
        object.__setattr__(
            self, "provenance", _require_nonempty("provenance", self.provenance)
        )
        object.__setattr__(
            self,
            "transport_status",
            _require_nonempty("transport_status", self.transport_status),
        )
        if self.verdict is not None:
            object.__setattr__(
                self, "verdict", _require_nonempty("verdict", self.verdict)
            )
        if self.fact_id is not None:
            if self.accepted:
                object.__setattr__(
                    self, "fact_id", _require_fact_id("fact_id", self.fact_id)
                )
            else:
                object.__setattr__(
                    self, "fact_id", _require_nonempty("fact_id", self.fact_id)
                )
        if not isinstance(self.exact_claim, str):
            raise AcceptanceError("exact_claim must be str")
        if self.write_error is not None:
            object.__setattr__(
                self, "write_error", _require_nonempty("write_error", self.write_error)
            )
        if not isinstance(self.accepted, bool):
            raise AcceptanceError("accepted must be bool")
        object.__setattr__(self, "reasons", _require_str_tuple("reasons", self.reasons))
        if self.accepted and self.reasons:
            raise AcceptanceError("accepted admission cannot carry rejection reasons")
        if self.accepted:
            if self.provenance != _FACT_SUBMIT:
                raise AcceptanceError("accepted admission requires provenance=fact_submit")
            if self.transport_status != _TRANSPORT_OK:
                raise AcceptanceError("accepted admission requires transport ok")
            if self.verdict != _VERDICT_CORRECT:
                raise AcceptanceError("accepted admission requires verdict correct")
            if self.write_error is not None:
                raise AcceptanceError("accepted admission forbids write_error")
            if self.fact_id is None or self.exact_claim == "":
                raise AcceptanceError("accepted admission requires fact_id and claim")
            _require_fact_id("fact_id", self.fact_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "provenance": self.provenance,
            "transport_status": self.transport_status,
            "verdict": self.verdict,
            "fact_id": self.fact_id,
            "exact_claim": self.exact_claim,
            "write_error": self.write_error,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: object) -> "FactAdmissionEvidence":
        payload = _mapping("FactAdmissionEvidence", data)
        allowed = {
            "record_id",
            "provenance",
            "transport_status",
            "verdict",
            "fact_id",
            "exact_claim",
            "write_error",
            "accepted",
            "reasons",
        }
        _reject_extra(payload, allowed, "FactAdmissionEvidence")
        _require_keys(payload, allowed, "FactAdmissionEvidence")
        return cls(
            record_id=payload["record_id"],
            provenance=payload["provenance"],
            transport_status=payload["transport_status"],
            verdict=payload["verdict"],
            fact_id=payload["fact_id"],
            exact_claim=payload["exact_claim"],
            write_error=payload["write_error"],
            accepted=payload["accepted"],
            reasons=tuple(payload["reasons"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalAuditReceipt:
    receipt_id: str
    audit_kind: FinalAuditKind
    checker_result: CheckerResult
    contract_id: str
    obligation_id: str
    action_id: str
    exact_claim: str
    scope: MathematicalScope
    acceptance_policy_hash: str
    auditor_id: str
    auditor_version: str
    environment_hash: str
    audit_artifact_hash: str
    created_at: str
    translation_confirmed: bool
    original_object_realized: bool
    fact_id: Optional[str] = None
    evidence_ids: tuple[str, ...] = ()
    candidate_content_hash: Optional[str] = None
    report_content_hash: Optional[str] = None
    kernel_artifact_hash: Optional[str] = None
    alignment_attestation_hash: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "receipt_id", _require_hex64("receipt_id", self.receipt_id)
        )
        object.__setattr__(
            self, "audit_kind", _require_enum(FinalAuditKind, self.audit_kind, "audit_kind")
        )
        object.__setattr__(
            self,
            "checker_result",
            _require_enum(CheckerResult, self.checker_result, "checker_result"),
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(self, "action_id", _require_hex64("action_id", self.action_id))
        object.__setattr__(
            self, "exact_claim", _require_nonempty("exact_claim", self.exact_claim)
        )
        if not isinstance(self.scope, MathematicalScope):
            raise AcceptanceError("scope must be a MathematicalScope")
        object.__setattr__(
            self,
            "acceptance_policy_hash",
            _require_hex64("acceptance_policy_hash", self.acceptance_policy_hash),
        )
        object.__setattr__(
            self, "auditor_id", _require_nonempty("auditor_id", self.auditor_id)
        )
        object.__setattr__(
            self,
            "auditor_version",
            _require_nonempty("auditor_version", self.auditor_version),
        )
        object.__setattr__(
            self,
            "environment_hash",
            _require_hex64("environment_hash", self.environment_hash),
        )
        object.__setattr__(
            self,
            "audit_artifact_hash",
            _require_hex64("audit_artifact_hash", self.audit_artifact_hash),
        )
        _parse_utc("created_at", self.created_at)
        if not isinstance(self.translation_confirmed, bool):
            raise AcceptanceError("translation_confirmed must be bool")
        if not isinstance(self.original_object_realized, bool):
            raise AcceptanceError("original_object_realized must be bool")
        if self.fact_id is not None:
            object.__setattr__(self, "fact_id", _require_fact_id("fact_id", self.fact_id))
        object.__setattr__(
            self, "evidence_ids", _require_hex_tuple("evidence_ids", self.evidence_ids)
        )
        if self.candidate_content_hash is not None:
            object.__setattr__(
                self,
                "candidate_content_hash",
                _require_hex64("candidate_content_hash", self.candidate_content_hash),
            )
        if self.report_content_hash is not None:
            object.__setattr__(
                self,
                "report_content_hash",
                _require_hex64("report_content_hash", self.report_content_hash),
            )
        if self.kernel_artifact_hash is not None:
            object.__setattr__(
                self,
                "kernel_artifact_hash",
                _require_hex64("kernel_artifact_hash", self.kernel_artifact_hash),
            )
        if self.alignment_attestation_hash is not None:
            object.__setattr__(
                self,
                "alignment_attestation_hash",
                _require_hex64(
                    "alignment_attestation_hash", self.alignment_attestation_hash
                ),
            )
        if self.checker_result is CheckerResult.PASS:
            _require_pass_audit_refs(self)
        expected = _audit_id_from_record(self)
        if self.receipt_id != expected:
            raise AcceptanceError("receipt_id does not match semantic fields")

    @classmethod
    def build(
        cls,
        *,
        audit_kind: FinalAuditKind,
        checker_result: CheckerResult,
        contract_id: str,
        obligation_id: str,
        action_id: str,
        exact_claim: str,
        scope: MathematicalScope,
        acceptance_policy_hash: str,
        auditor_id: str,
        auditor_version: str,
        environment_hash: str,
        audit_artifact_hash: str,
        created_at: str,
        translation_confirmed: bool,
        original_object_realized: bool,
        fact_id: Optional[str] = None,
        evidence_ids: Sequence[str] = (),
        candidate_content_hash: Optional[str] = None,
        report_content_hash: Optional[str] = None,
        kernel_artifact_hash: Optional[str] = None,
        alignment_attestation_hash: Optional[str] = None,
    ) -> "FinalAuditReceipt":
        receipt_id = _audit_id(
            audit_kind=audit_kind,
            checker_result=checker_result,
            contract_id=contract_id,
            obligation_id=obligation_id,
            action_id=action_id,
            exact_claim=exact_claim,
            scope=scope,
            acceptance_policy_hash=acceptance_policy_hash,
            auditor_id=auditor_id,
            auditor_version=auditor_version,
            environment_hash=environment_hash,
            audit_artifact_hash=audit_artifact_hash,
            translation_confirmed=translation_confirmed,
            original_object_realized=original_object_realized,
            fact_id=fact_id,
            evidence_ids=tuple(evidence_ids),
            candidate_content_hash=candidate_content_hash,
            report_content_hash=report_content_hash,
            kernel_artifact_hash=kernel_artifact_hash,
            alignment_attestation_hash=alignment_attestation_hash,
        )
        return cls(
            receipt_id=receipt_id,
            audit_kind=audit_kind,
            checker_result=checker_result,
            contract_id=contract_id,
            obligation_id=obligation_id,
            action_id=action_id,
            exact_claim=exact_claim,
            scope=scope,
            acceptance_policy_hash=acceptance_policy_hash,
            auditor_id=auditor_id,
            auditor_version=auditor_version,
            environment_hash=environment_hash,
            audit_artifact_hash=audit_artifact_hash,
            created_at=created_at,
            translation_confirmed=translation_confirmed,
            original_object_realized=original_object_realized,
            fact_id=fact_id,
            evidence_ids=tuple(evidence_ids),
            candidate_content_hash=candidate_content_hash,
            report_content_hash=report_content_hash,
            kernel_artifact_hash=kernel_artifact_hash,
            alignment_attestation_hash=alignment_attestation_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "audit_kind": self.audit_kind.value,
            "checker_result": self.checker_result.value,
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "action_id": self.action_id,
            "exact_claim": self.exact_claim,
            "scope": self.scope.to_dict(),
            "acceptance_policy_hash": self.acceptance_policy_hash,
            "fact_id": self.fact_id,
            "evidence_ids": list(self.evidence_ids),
            "candidate_content_hash": self.candidate_content_hash,
            "report_content_hash": self.report_content_hash,
            "kernel_artifact_hash": self.kernel_artifact_hash,
            "alignment_attestation_hash": self.alignment_attestation_hash,
            "auditor_id": self.auditor_id,
            "auditor_version": self.auditor_version,
            "environment_hash": self.environment_hash,
            "audit_artifact_hash": self.audit_artifact_hash,
            "created_at": self.created_at,
            "translation_confirmed": self.translation_confirmed,
            "original_object_realized": self.original_object_realized,
        }

    @classmethod
    def from_dict(cls, data: object) -> "FinalAuditReceipt":
        payload = _mapping("FinalAuditReceipt", data)
        allowed = {
            "receipt_id",
            "audit_kind",
            "checker_result",
            "contract_id",
            "obligation_id",
            "action_id",
            "exact_claim",
            "scope",
            "acceptance_policy_hash",
            "fact_id",
            "evidence_ids",
            "candidate_content_hash",
            "report_content_hash",
            "kernel_artifact_hash",
            "alignment_attestation_hash",
            "auditor_id",
            "auditor_version",
            "environment_hash",
            "audit_artifact_hash",
            "created_at",
            "translation_confirmed",
            "original_object_realized",
        }
        _reject_extra(payload, allowed, "FinalAuditReceipt")
        required = allowed - {"kernel_artifact_hash", "alignment_attestation_hash"}
        _require_keys(payload, required, "FinalAuditReceipt")
        return cls(
            receipt_id=payload["receipt_id"],
            audit_kind=payload["audit_kind"],
            checker_result=payload["checker_result"],
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            action_id=payload["action_id"],
            exact_claim=payload["exact_claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            acceptance_policy_hash=payload["acceptance_policy_hash"],
            fact_id=payload["fact_id"],
            evidence_ids=tuple(payload["evidence_ids"]),
            candidate_content_hash=payload["candidate_content_hash"],
            report_content_hash=payload["report_content_hash"],
            kernel_artifact_hash=payload.get("kernel_artifact_hash"),
            alignment_attestation_hash=payload.get("alignment_attestation_hash"),
            auditor_id=payload["auditor_id"],
            auditor_version=payload["auditor_version"],
            environment_hash=payload["environment_hash"],
            audit_artifact_hash=payload["audit_artifact_hash"],
            created_at=payload["created_at"],
            translation_confirmed=payload["translation_confirmed"],
            original_object_realized=payload["original_object_realized"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptanceRecord:
    record_id: str
    verdict: AcceptanceVerdict
    reasons: tuple[str, ...]
    contract_id: str
    obligation_id: str
    created_at: str
    project_outcome: Optional[ProjectOutcome] = None
    fact_id: Optional[str] = None
    evidence_ids: tuple[str, ...] = ()
    audit_receipt_id: Optional[str] = None
    admission_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record_id", _require_hex64("record_id", self.record_id)
        )
        object.__setattr__(
            self, "verdict", _require_enum(AcceptanceVerdict, self.verdict, "verdict")
        )
        object.__setattr__(self, "reasons", _require_str_tuple("reasons", self.reasons))
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        _parse_utc("created_at", self.created_at)
        if self.project_outcome is not None:
            object.__setattr__(
                self,
                "project_outcome",
                _require_enum(ProjectOutcome, self.project_outcome, "project_outcome"),
            )
        if self.fact_id is not None:
            object.__setattr__(self, "fact_id", _require_fact_id("fact_id", self.fact_id))
        object.__setattr__(
            self, "evidence_ids", _require_hex_tuple("evidence_ids", self.evidence_ids)
        )
        if self.audit_receipt_id is not None:
            object.__setattr__(
                self,
                "audit_receipt_id",
                _require_hex64("audit_receipt_id", self.audit_receipt_id),
            )
        object.__setattr__(
            self,
            "admission_ids",
            _require_str_tuple("admission_ids", self.admission_ids),
        )
        if self.verdict is AcceptanceVerdict.PASS:
            if self.project_outcome not in {
                ProjectOutcome.SOLVED,
                ProjectOutcome.REFUTED,
            }:
                raise AcceptanceError("PASS acceptance must bind SOLVED or REFUTED")
            if not self.audit_receipt_id:
                raise AcceptanceError("PASS acceptance requires a final audit receipt")
            if self.reasons:
                raise AcceptanceError("PASS acceptance cannot carry rejection reasons")
        else:
            if self.project_outcome is not None:
                raise AcceptanceError("non-PASS acceptance cannot grant a terminal outcome")
            if not self.reasons:
                raise AcceptanceError("non-PASS acceptance requires explicit reasons")
        expected = _acceptance_id_from_record(self)
        if self.record_id != expected:
            raise AcceptanceError("record_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "project_outcome": None
            if self.project_outcome is None
            else self.project_outcome.value,
            "fact_id": self.fact_id,
            "evidence_ids": list(self.evidence_ids),
            "audit_receipt_id": self.audit_receipt_id,
            "admission_ids": list(self.admission_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "AcceptanceRecord":
        payload = _mapping("AcceptanceRecord", data)
        allowed = {
            "record_id",
            "verdict",
            "reasons",
            "contract_id",
            "obligation_id",
            "project_outcome",
            "fact_id",
            "evidence_ids",
            "audit_receipt_id",
            "admission_ids",
            "created_at",
        }
        _reject_extra(payload, allowed, "AcceptanceRecord")
        _require_keys(payload, allowed, "AcceptanceRecord")
        return cls(
            record_id=payload["record_id"],
            verdict=payload["verdict"],
            reasons=tuple(payload["reasons"]),
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            project_outcome=payload["project_outcome"],
            fact_id=payload["fact_id"],
            evidence_ids=tuple(payload["evidence_ids"]),
            audit_receipt_id=payload["audit_receipt_id"],
            admission_ids=tuple(payload["admission_ids"]),
            created_at=payload["created_at"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class StallEvidence:
    frontier_obligation_ids: tuple[str, ...]
    untested_route_ids: tuple[str, ...]
    restart_condition: str
    recurrence_audit: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "frontier_obligation_ids",
            _require_hex_tuple("frontier_obligation_ids", self.frontier_obligation_ids),
        )
        object.__setattr__(
            self,
            "untested_route_ids",
            _require_hex_tuple("untested_route_ids", self.untested_route_ids),
        )
        object.__setattr__(
            self,
            "restart_condition",
            _require_restart("restart_condition", self.restart_condition),
        )
        object.__setattr__(
            self,
            "recurrence_audit",
            _require_restart("recurrence_audit", self.recurrence_audit),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frontier_obligation_ids": list(self.frontier_obligation_ids),
            "untested_route_ids": list(self.untested_route_ids),
            "restart_condition": self.restart_condition,
            "recurrence_audit": self.recurrence_audit,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalInputEvidence:
    missing_material: tuple[str, ...]
    missing_permission: tuple[str, ...]
    missing_decision: tuple[str, ...]
    restart_condition: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "missing_material",
            _require_str_tuple("missing_material", self.missing_material),
        )
        object.__setattr__(
            self,
            "missing_permission",
            _require_str_tuple("missing_permission", self.missing_permission),
        )
        object.__setattr__(
            self,
            "missing_decision",
            _require_str_tuple("missing_decision", self.missing_decision),
        )
        object.__setattr__(
            self,
            "restart_condition",
            _require_restart("restart_condition", self.restart_condition),
        )
        if not (
            self.missing_material or self.missing_permission or self.missing_decision
        ):
            raise AcceptanceError("external-input evidence requires a missing item")

    def to_dict(self) -> dict[str, Any]:
        return {
            "missing_material": list(self.missing_material),
            "missing_permission": list(self.missing_permission),
            "missing_decision": list(self.missing_decision),
            "restart_condition": self.restart_condition,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class InfrastructureBlockEvidence:
    lease_ids: tuple[str, ...]
    infrastructure_failures: tuple[str, ...]
    recovery_condition: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "lease_ids", _require_hex_tuple("lease_ids", self.lease_ids)
        )
        if not self.lease_ids:
            raise AcceptanceError("infra block requires at least one lease_id")
        object.__setattr__(
            self,
            "infrastructure_failures",
            _require_str_tuple(
                "infrastructure_failures", self.infrastructure_failures
            ),
        )
        if not self.infrastructure_failures:
            raise AcceptanceError("infra block requires at least one failure")
        object.__setattr__(
            self,
            "recovery_condition",
            _require_restart("recovery_condition", self.recovery_condition),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_ids": list(self.lease_ids),
            "infrastructure_failures": list(self.infrastructure_failures),
            "recovery_condition": self.recovery_condition,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OutcomeEvidence:
    outcome: ProjectOutcome
    mathematical_status: MathematicalStatus
    reasons: tuple[str, ...]
    pause_reason: str
    restart_conditions: tuple[str, ...]
    reached_budget_dimensions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "outcome", _require_enum(ProjectOutcome, self.outcome, "outcome")
        )
        object.__setattr__(
            self,
            "mathematical_status",
            _require_enum(
                MathematicalStatus, self.mathematical_status, "mathematical_status"
            ),
        )
        object.__setattr__(self, "reasons", _require_str_tuple("reasons", self.reasons))
        object.__setattr__(
            self, "pause_reason", _require_nonempty("pause_reason", self.pause_reason)
        )
        object.__setattr__(
            self,
            "restart_conditions",
            _require_str_tuple("restart_conditions", self.restart_conditions),
        )
        object.__setattr__(
            self,
            "reached_budget_dimensions",
            _require_str_tuple(
                "reached_budget_dimensions", self.reached_budget_dimensions
            ),
        )
        _reject_forbidden_text("pause_reason", self.pause_reason)
        for index, item in enumerate(self.restart_conditions):
            _reject_forbidden_text(f"restart_conditions[{index}]", item)
        for index, item in enumerate(self.reasons):
            _reject_forbidden_text(f"reasons[{index}]", item)
        if self.outcome in {ProjectOutcome.SOLVED, ProjectOutcome.REFUTED}:
            expected = (
                MathematicalStatus.CHECKED_CORRECT
                if self.outcome is ProjectOutcome.SOLVED
                else MathematicalStatus.CHECKED_WRONG
            )
            if self.mathematical_status is not expected:
                raise AcceptanceError("terminal math outcome has the wrong status")
        else:
            if self.mathematical_status is not MathematicalStatus.UNKNOWN:
                raise AcceptanceError("pause outcomes must preserve mathematical UNKNOWN")
            if not self.restart_conditions:
                raise AcceptanceError("pause outcomes require restart conditions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "mathematical_status": self.mathematical_status.value,
            "reasons": list(self.reasons),
            "pause_reason": self.pause_reason,
            "restart_conditions": list(self.restart_conditions),
            "reached_budget_dimensions": list(self.reached_budget_dimensions),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointReport:
    report_id: str
    contract_id: str
    outcome: ProjectOutcome
    source_event_seq: int
    resources: ResourceSnapshot
    accepted_result_ids: tuple[str, ...]
    open_obligation_ids: tuple[str, ...]
    scope_aware_failure_ids: tuple[str, ...]
    untested_route_ids: tuple[str, ...]
    active_lease_ids: tuple[str, ...]
    pause_reason: str
    mathematical_status: MathematicalStatus
    restart_conditions: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "report_id", _require_hex64("report_id", self.report_id)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "outcome", _require_enum(ProjectOutcome, self.outcome, "outcome")
        )
        object.__setattr__(
            self,
            "source_event_seq",
            _require_nonneg_int("source_event_seq", self.source_event_seq),
        )
        if not isinstance(self.resources, ResourceSnapshot):
            raise AcceptanceError("resources must be a ResourceSnapshot")
        object.__setattr__(
            self,
            "accepted_result_ids",
            _require_result_id_tuple("accepted_result_ids", self.accepted_result_ids),
        )
        object.__setattr__(
            self,
            "open_obligation_ids",
            _require_hex_tuple("open_obligation_ids", self.open_obligation_ids),
        )
        object.__setattr__(
            self,
            "scope_aware_failure_ids",
            _require_hex_tuple("scope_aware_failure_ids", self.scope_aware_failure_ids),
        )
        object.__setattr__(
            self,
            "untested_route_ids",
            _require_hex_tuple("untested_route_ids", self.untested_route_ids),
        )
        object.__setattr__(
            self,
            "active_lease_ids",
            _require_hex_tuple("active_lease_ids", self.active_lease_ids),
        )
        object.__setattr__(
            self, "pause_reason", _require_nonempty("pause_reason", self.pause_reason)
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
            "restart_conditions",
            _require_str_tuple("restart_conditions", self.restart_conditions),
        )
        _parse_utc("created_at", self.created_at)
        _reject_forbidden_text("pause_reason", self.pause_reason)
        for index, item in enumerate(self.restart_conditions):
            _reject_forbidden_text(f"restart_conditions[{index}]", item)
        expected = _checkpoint_id_from_record(self)
        if self.report_id != expected:
            raise AcceptanceError("report_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "contract_id": self.contract_id,
            "outcome": self.outcome.value,
            "source_event_seq": self.source_event_seq,
            "resources": self.resources.to_dict(),
            "accepted_result_ids": list(self.accepted_result_ids),
            "open_obligation_ids": list(self.open_obligation_ids),
            "scope_aware_failure_ids": list(self.scope_aware_failure_ids),
            "untested_route_ids": list(self.untested_route_ids),
            "active_lease_ids": list(self.active_lease_ids),
            "pause_reason": self.pause_reason,
            "mathematical_status": self.mathematical_status.value,
            "restart_conditions": list(self.restart_conditions),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CheckpointReport":
        payload = _mapping("CheckpointReport", data)
        allowed = {
            "report_id",
            "contract_id",
            "outcome",
            "source_event_seq",
            "resources",
            "accepted_result_ids",
            "open_obligation_ids",
            "scope_aware_failure_ids",
            "untested_route_ids",
            "active_lease_ids",
            "pause_reason",
            "mathematical_status",
            "restart_conditions",
            "created_at",
        }
        _reject_extra(payload, allowed, "CheckpointReport")
        _require_keys(payload, allowed, "CheckpointReport")
        return cls(
            report_id=payload["report_id"],
            contract_id=payload["contract_id"],
            outcome=payload["outcome"],
            source_event_seq=payload["source_event_seq"],
            resources=ResourceSnapshot.from_dict(payload["resources"]),
            accepted_result_ids=tuple(payload["accepted_result_ids"]),
            open_obligation_ids=tuple(payload["open_obligation_ids"]),
            scope_aware_failure_ids=tuple(payload["scope_aware_failure_ids"]),
            untested_route_ids=tuple(payload["untested_route_ids"]),
            active_lease_ids=tuple(payload["active_lease_ids"]),
            pause_reason=payload["pause_reason"],
            mathematical_status=payload["mathematical_status"],
            restart_conditions=tuple(payload["restart_conditions"]),
            created_at=payload["created_at"],
        )


def collect_fact_admissions(
    snapshot: ProjectSnapshot,
) -> tuple[FactAdmissionEvidence, ...]:
    if not isinstance(snapshot, ProjectSnapshot):
        raise AcceptanceError("snapshot must be a ProjectSnapshot")
    active = {fact.fact_id: fact for fact in snapshot.active_facts}
    revoked = set(snapshot.revoked_fact_ids)
    parsed: list[FactAdmissionEvidence] = []
    for record in snapshot.memory_records:
        if record.kind != _VERIFICATION_KIND:
            continue
        parsed.append(_parse_verification_record(record.record_id, record.canonical_json))
    contradictory = _contradictory_fact_ids(parsed)
    out: list[FactAdmissionEvidence] = []
    for item in parsed:
        reasons = list(item.reasons)
        accepted = False
        if item.fact_id is not None and item.fact_id in contradictory:
            reasons.append("admission_contradictory")
        elif item.reasons:
            pass
        elif item.provenance != _FACT_SUBMIT:
            reasons.append("replay_only_or_foreign_provenance")
        elif item.transport_status != _TRANSPORT_OK:
            reasons.append("admission_transport_not_ok")
        elif item.verdict == _VERDICT_WRONG:
            reasons.append("verifier_wrong_is_not_acceptance")
        elif item.verdict != _VERDICT_CORRECT:
            reasons.append("admission_verdict_not_correct")
        elif item.write_error is not None:
            reasons.append("admission_write_error")
        elif item.fact_id is None:
            reasons.append("admission_missing_fact_id")
        elif _HEX16_RE.fullmatch(item.fact_id) is None:
            reasons.append("malformed_fact_id")
        elif item.fact_id in revoked:
            reasons.append("fact_revoked")
        elif item.fact_id not in active:
            reasons.append("fact_missing")
        elif active[item.fact_id].statement != item.exact_claim:
            reasons.append("admission_claim_mismatch")
        elif item.exact_claim == "":
            reasons.append("admission_missing_claim")
        else:
            accepted = True
            reasons = []
        out.append(
            FactAdmissionEvidence(
                record_id=item.record_id,
                provenance=item.provenance,
                transport_status=item.transport_status,
                verdict=item.verdict,
                fact_id=item.fact_id,
                exact_claim=item.exact_claim,
                write_error=item.write_error,
                accepted=accepted,
                reasons=tuple(reasons),
            )
        )
    return tuple(out)


def collect_statement_alignments(
    snapshot: ProjectSnapshot,
) -> tuple[StatementAlignmentAttestation, ...]:
    if not isinstance(snapshot, ProjectSnapshot):
        raise AcceptanceError("snapshot must be a ProjectSnapshot")
    found: list[StatementAlignmentAttestation] = []
    for record in snapshot.memory_records:
        if record.kind not in {"statement_alignment", "verification"}:
            continue
        try:
            payload = json.loads(record.canonical_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        raw = payload.get("attestation") if record.kind == "statement_alignment" else None
        if raw is None and payload.get("kind") == "statement_alignment":
            raw = payload
        if not isinstance(raw, Mapping):
            continue
        try:
            found.append(StatementAlignmentAttestation.from_dict(raw))
        except (KernelEvidenceError, AcceptanceError, KeyError, TypeError):
            continue
    return tuple(found)


def kernel_acceptance_required(contract: ProblemContract) -> bool:
    if not isinstance(contract, ProblemContract):
        raise AcceptanceError("contract must be a ProblemContract")
    return (
        contract.required_evidence_level is EvidenceKind.KERNEL_CHECKED_PROOF
        or contract.target_kind is TargetKind.FORMALIZATION
    )


def promote_compute_counterexample(
    contract: ProblemContract,
    obligation: Obligation,
    compute_result: ComputeRunResult,
    audit: FinalAuditReceipt,
    artifacts: ArtifactStore,
    producer: str,
    created_at: str,
) -> EvidenceArtifact:
    if not isinstance(contract, ProblemContract):
        raise AcceptanceError("contract must be a ProblemContract")
    if not isinstance(obligation, Obligation):
        raise AcceptanceError("obligation must be an Obligation")
    if not isinstance(compute_result, ComputeRunResult):
        raise AcceptanceError("compute_result must be a ComputeRunResult")
    if not isinstance(audit, FinalAuditReceipt):
        raise AcceptanceError("audit must be a FinalAuditReceipt")
    if not isinstance(artifacts, ArtifactStore):
        raise AcceptanceError("artifacts must be an ArtifactStore")
    producer = _require_nonempty("producer", producer)
    _parse_utc("created_at", created_at)
    if obligation.contract_id != contract.contract_id:
        raise AcceptanceError("obligation is not bound to the contract")
    # A child can have a different scoped claim under the same project contract.
    # The candidate, checked evidence and realization audit must all bind THAT
    # exact obligation; changing the root target still requires a new contract.
    evidence = compute_result.evidence
    if (
        compute_result.termination is not ComputeTermination.CHECKED_COUNTEREXAMPLE
        or evidence is None
        or evidence.evidence_kind
        is not ComputeEvidenceKind.CHECKED_COUNTEREXAMPLE_CANDIDATE
        or compute_result.candidate_ref is None
        or compute_result.checker_record is None
        or compute_result.checker_record.checker_result is not CheckerResult.PASS
    ):
        raise AcceptanceError("promotion requires a checked compute candidate")
    if evidence.contract_id != contract.contract_id:
        raise AcceptanceError("compute evidence contract_id mismatch")
    if evidence.obligation_id != obligation.obligation_id:
        raise AcceptanceError("compute evidence obligation_id mismatch")
    if evidence.tested_claim != obligation.exact_statement:
        raise AcceptanceError("compute evidence claim does not match the obligation")
    if evidence.scope != obligation.scope:
        raise AcceptanceError("compute evidence scope mismatch")
    if evidence.action_id != audit.action_id:
        raise AcceptanceError("compute evidence action_id does not match the audit")
    if evidence.request_id != compute_result.request_id:
        raise AcceptanceError("compute evidence request_id mismatch")
    candidate = compute_result.candidate_ref
    report = compute_result.report_ref
    if evidence.candidate_ref is None or evidence.candidate_ref.content_hash != candidate.content_hash:
        raise AcceptanceError("compute evidence candidate hash mismatch")
    if evidence.report_ref.content_hash != report.content_hash:
        raise AcceptanceError("compute evidence report hash mismatch")
    if (
        compute_result.checker_record.candidate_content_hash
        != candidate.content_hash
    ):
        raise AcceptanceError("checker record candidate hash mismatch")
    if audit.checker_result is not CheckerResult.PASS:
        raise AcceptanceError("promotion requires a PASS final audit")
    if audit.audit_kind is not FinalAuditKind.COUNTEREXAMPLE:
        raise AcceptanceError("promotion requires a COUNTEREXAMPLE audit")
    if not audit.translation_confirmed or not audit.original_object_realized:
        raise AcceptanceError(
            "promotion requires translation and original-object realization PASS"
        )
    if audit.contract_id != contract.contract_id:
        raise AcceptanceError("audit contract_id mismatch")
    if audit.obligation_id != obligation.obligation_id:
        raise AcceptanceError("audit obligation_id mismatch")
    if audit.exact_claim != obligation.exact_statement:
        raise AcceptanceError("audit claim mismatch")
    if audit.scope != obligation.scope:
        raise AcceptanceError("audit scope mismatch")
    if audit.candidate_content_hash != candidate.content_hash:
        raise AcceptanceError("audit candidate hash mismatch")
    if audit.report_content_hash != report.content_hash:
        raise AcceptanceError("audit report hash mismatch")
    try:
        artifacts.verify(candidate.content_hash)
        artifacts.verify(report.content_hash)
        artifacts.verify(audit.audit_artifact_hash)
        candidate_bytes = artifacts.get_bytes(candidate.content_hash)
        report_bytes = artifacts.get_bytes(report.content_hash)
        artifacts.get_bytes(audit.audit_artifact_hash)
    except ArtifactStoreError as exc:
        raise AcceptanceError(f"compute artifacts failed verification: {exc}") from exc
    if len(candidate_bytes) != candidate.size_bytes:
        raise AcceptanceError("candidate size does not match the stored ref")
    if len(report_bytes) != report.size_bytes:
        raise AcceptanceError("report size does not match the stored ref")
    return EvidenceArtifact.build(
        content_hash=candidate.content_hash,
        producer=producer,
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        exact_claim=obligation.exact_statement,
        scope=obligation.scope,
        input_hashes=(candidate.content_hash, report.content_hash, audit.audit_artifact_hash),
        environment_hash=audit.environment_hash,
        checker_id=audit.auditor_id,
        checker_version=audit.auditor_version,
        checker_result=CheckerResult.PASS,
        created_at=created_at,
    )


def assess_final_acceptance(
    contract: ProblemContract,
    obligation: Obligation,
    view: ResearchView,
    snapshot: ProjectSnapshot,
    audit: FinalAuditReceipt | None,
    *,
    acceptance_policy_hash: str,
    artifacts: ArtifactStore,
    alignment: StatementAlignmentAttestation | None = None,
) -> AcceptanceRecord:
    if not isinstance(contract, ProblemContract):
        raise AcceptanceError("contract must be a ProblemContract")
    if not isinstance(obligation, Obligation):
        raise AcceptanceError("obligation must be an Obligation")
    if not isinstance(view, ResearchView):
        raise AcceptanceError("view must be a ResearchView")
    if not isinstance(snapshot, ProjectSnapshot):
        raise AcceptanceError("snapshot must be a ProjectSnapshot")
    if audit is not None and not isinstance(audit, FinalAuditReceipt):
        raise AcceptanceError("audit must be a FinalAuditReceipt")
    acceptance_policy_hash = _require_hex64(
        "acceptance_policy_hash", acceptance_policy_hash
    )
    if not isinstance(artifacts, ArtifactStore):
        raise AcceptanceError("artifacts must be an ArtifactStore")
    live = view.get_obligation(obligation.obligation_id)
    if live is None:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("obligation_missing_from_view",),
            created_at=_utc_now() if audit is None else audit.created_at,
            audit=audit,
        )
    obligation = live
    if obligation.contract_id != contract.contract_id:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("target_contract_mismatch",),
            created_at=audit.created_at if audit is not None else _utc_now(),
            audit=audit,
        )
    if obligation.scope != contract.scope:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("target_scope_mismatch",),
            created_at=audit.created_at if audit is not None else _utc_now(),
            audit=audit,
        )
    if obligation.exact_statement != contract.exact_statement:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("target_claim_mismatch",),
            created_at=audit.created_at if audit is not None else _utc_now(),
            audit=audit,
        )
    if audit is None:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.UNKNOWN,
            reasons=("final_audit_omitted",),
            created_at=_utc_now(),
        )
    if audit.acceptance_policy_hash != acceptance_policy_hash:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_policy_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    if audit.contract_id != contract.contract_id or audit.obligation_id != obligation.obligation_id:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_target_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    if audit.exact_claim != obligation.exact_statement or audit.scope != obligation.scope:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_claim_or_scope_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    if audit.checker_result is CheckerResult.UNKNOWN:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.UNKNOWN,
            reasons=("final_audit_unknown",),
            created_at=audit.created_at,
            audit=audit,
        )
    if audit.checker_result is CheckerResult.FAIL:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("final_audit_fail",),
            created_at=audit.created_at,
            audit=audit,
        )
    try:
        artifacts.verify(audit.audit_artifact_hash)
        artifacts.get_bytes(audit.audit_artifact_hash)
    except ArtifactStoreError:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_artifact_missing",),
            created_at=audit.created_at,
            audit=audit,
        )
    if obligation.status is ObligationStatus.DISCHARGED:
        return _assess_solved(
            contract,
            obligation,
            view,
            snapshot,
            audit,
            artifacts,
            alignment=alignment,
        )
    if obligation.status is ObligationStatus.REFUTED:
        return _assess_refuted(contract, obligation, view, snapshot, audit)
    return _acceptance(
        contract,
        obligation,
        verdict=AcceptanceVerdict.FAIL,
        reasons=("obligation_not_terminal",),
        created_at=audit.created_at,
        audit=audit,
    )


def derive_project_outcome(
    contract: ProblemContract,
    acceptance: AcceptanceRecord,
    policy: BudgetPolicy,
    resources: ResourceSnapshot,
    view: ResearchView,
    runtime: RuntimeSnapshot,
    pause_evidence: StallEvidence
    | ExternalInputEvidence
    | InfrastructureBlockEvidence
    | None = None,
) -> OutcomeEvidence:
    if not isinstance(contract, ProblemContract):
        raise AcceptanceError("contract must be a ProblemContract")
    if not isinstance(acceptance, AcceptanceRecord):
        raise AcceptanceError("acceptance must be an AcceptanceRecord")
    if not isinstance(policy, BudgetPolicy):
        raise AcceptanceError("policy must be a BudgetPolicy")
    if not isinstance(resources, ResourceSnapshot):
        raise AcceptanceError("resources must be a ResourceSnapshot")
    if not isinstance(view, ResearchView):
        raise AcceptanceError("view must be a ResearchView")
    if not isinstance(runtime, RuntimeSnapshot):
        raise AcceptanceError("runtime must be a RuntimeSnapshot")
    if acceptance.contract_id != contract.contract_id:
        raise AcceptanceError("acceptance is not bound to the contract")
    if resources != runtime.resources:
        raise AcceptanceError("resources must equal runtime.resources")
    if acceptance.verdict is AcceptanceVerdict.PASS:
        if acceptance.project_outcome is ProjectOutcome.SOLVED:
            return OutcomeEvidence(
                outcome=ProjectOutcome.SOLVED,
                mathematical_status=MathematicalStatus.CHECKED_CORRECT,
                reasons=("final_acceptance_pass",),
                pause_reason="not_applicable",
                restart_conditions=(),
            )
        if acceptance.project_outcome is ProjectOutcome.REFUTED:
            return OutcomeEvidence(
                outcome=ProjectOutcome.REFUTED,
                mathematical_status=MathematicalStatus.CHECKED_WRONG,
                reasons=("final_acceptance_pass",),
                pause_reason="not_applicable",
                restart_conditions=(),
            )
        raise AcceptanceError("PASS acceptance is missing a terminal outcome")
    if isinstance(pause_evidence, StallEvidence):
        _require_stall_matches_view(pause_evidence, view)
        return OutcomeEvidence(
            outcome=ProjectOutcome.FRONTIER_STALLED,
            mathematical_status=MathematicalStatus.UNKNOWN,
            reasons=("frontier_stalled", pause_evidence.recurrence_audit),
            pause_reason="frontier_stalled",
            restart_conditions=(pause_evidence.restart_condition,),
        )
    if isinstance(pause_evidence, ExternalInputEvidence):
        return OutcomeEvidence(
            outcome=ProjectOutcome.NEEDS_EXTERNAL_INPUT,
            mathematical_status=MathematicalStatus.UNKNOWN,
            reasons=("needs_external_input",),
            pause_reason="needs_external_input",
            restart_conditions=(pause_evidence.restart_condition,),
        )
    if isinstance(pause_evidence, InfrastructureBlockEvidence):
        _require_infra_leases(pause_evidence, runtime)
        return OutcomeEvidence(
            outcome=ProjectOutcome.INFRA_BLOCKED,
            mathematical_status=MathematicalStatus.UNKNOWN,
            reasons=("infrastructure_blocked",),
            pause_reason="infrastructure_blocked",
            restart_conditions=(pause_evidence.recovery_condition,),
        )
    if pause_evidence is not None:
        raise AcceptanceError("pause_evidence must be a typed pause record")
    reached = _reached_limited_dimensions(policy, resources)
    if reached:
        return OutcomeEvidence(
            outcome=ProjectOutcome.BUDGET_EXHAUSTED,
            mathematical_status=MathematicalStatus.UNKNOWN,
            reasons=("limited_budget_reached",),
            pause_reason="budget_exhausted",
            restart_conditions=("raise_or_reallocate_a_LIMITED_budget_dimension",),
            reached_budget_dimensions=reached,
        )
    raise AcceptanceError("no PASS acceptance and no typed pause or budget evidence")


def build_checkpoint_report(
    contract: ProblemContract,
    outcome: OutcomeEvidence,
    view: ResearchView,
    runtime: RuntimeSnapshot,
    source_event_seq: int,
    accepted_result_ids: Sequence[str] = (),
    *,
    created_at: str | None = None,
) -> CheckpointReport:
    if not isinstance(contract, ProblemContract):
        raise AcceptanceError("contract must be a ProblemContract")
    if not isinstance(outcome, OutcomeEvidence):
        raise AcceptanceError("outcome must be an OutcomeEvidence")
    if not isinstance(view, ResearchView):
        raise AcceptanceError("view must be a ResearchView")
    if not isinstance(runtime, RuntimeSnapshot):
        raise AcceptanceError("runtime must be a RuntimeSnapshot")
    created_at = _utc_now() if created_at is None else created_at
    _parse_utc("created_at", created_at)
    open_ids = view.frontier()
    failure_ids = tuple(
        item.failure_id
        for item in view.failures()
        if item.scope == contract.scope
    )
    untested = tuple(
        route.route_id
        for route in view.routes()
        if route.validity is ValidityStatus.VALID
        and route.status
        not in {
            RouteStatus.COMPLETED,
            RouteStatus.REFUTED,
            RouteStatus.BLOCKED,
            RouteStatus.PARKED,
        }
    )
    report_id = _checkpoint_id(
        contract_id=contract.contract_id,
        outcome=outcome.outcome,
        source_event_seq=source_event_seq,
        resources=runtime.resources,
        accepted_result_ids=tuple(accepted_result_ids),
        open_obligation_ids=open_ids,
        scope_aware_failure_ids=failure_ids,
        untested_route_ids=untested,
        active_lease_ids=runtime.active_lease_ids,
        pause_reason=outcome.pause_reason,
        mathematical_status=outcome.mathematical_status,
        restart_conditions=outcome.restart_conditions,
    )
    return CheckpointReport(
        report_id=report_id,
        contract_id=contract.contract_id,
        outcome=outcome.outcome,
        source_event_seq=source_event_seq,
        resources=runtime.resources,
        accepted_result_ids=tuple(accepted_result_ids),
        open_obligation_ids=open_ids,
        scope_aware_failure_ids=failure_ids,
        untested_route_ids=untested,
        active_lease_ids=runtime.active_lease_ids,
        pause_reason=outcome.pause_reason,
        mathematical_status=outcome.mathematical_status,
        restart_conditions=outcome.restart_conditions,
        created_at=created_at,
    )


def _assess_solved(
    contract: ProblemContract,
    obligation: Obligation,
    view: ResearchView,
    snapshot: ProjectSnapshot,
    audit: FinalAuditReceipt,
    artifacts: ArtifactStore,
    alignment: StatementAlignmentAttestation | None = None,
) -> AcceptanceRecord:
    if audit.audit_kind is not FinalAuditKind.PROOF:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_kind_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    satisfying = _satisfying_evidence(
        view,
        obligation,
        required=contract.required_evidence_level,
    )
    if not satisfying:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("evidence_below_required_level",),
            created_at=audit.created_at,
            audit=audit,
        )
    if not audit.evidence_ids or any(
        item not in {art.artifact_id for art in satisfying} for item in audit.evidence_ids
    ):
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_evidence_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    admissions = collect_fact_admissions(snapshot)
    accepted = [
        item
        for item in admissions
        if item.accepted and item.exact_claim == contract.exact_statement
    ]
    if not accepted:
        reasons = ("missing_or_invalid_admission",)
        if any(
            item.verdict == _VERDICT_WRONG and item.provenance == _FACT_SUBMIT
            for item in admissions
        ):
            reasons = ("verifier_wrong_is_not_acceptance",)
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=reasons,
            created_at=audit.created_at,
            audit=audit,
            admission_ids=tuple(item.record_id for item in admissions),
        )
    fact_ids = {item.fact_id for item in accepted if item.fact_id is not None}
    if audit.fact_id is None or audit.fact_id not in fact_ids:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_fact_mismatch",),
            created_at=audit.created_at,
            audit=audit,
            admission_ids=tuple(item.record_id for item in accepted),
        )
    if kernel_acceptance_required(contract):
        kernel_reason = _kernel_acceptance_reason(
            contract,
            obligation,
            satisfying,
            audit,
            artifacts,
            snapshot,
            alignment,
        )
        if kernel_reason is not None:
            return _acceptance(
                contract,
                obligation,
                verdict=AcceptanceVerdict.FAIL,
                reasons=(kernel_reason,),
                created_at=audit.created_at,
                audit=audit,
                admission_ids=tuple(item.record_id for item in accepted),
            )
    return _acceptance(
        contract,
        obligation,
        verdict=AcceptanceVerdict.PASS,
        reasons=(),
        created_at=audit.created_at,
        audit=audit,
        project_outcome=ProjectOutcome.SOLVED,
        fact_id=audit.fact_id,
        evidence_ids=tuple(item.artifact_id for item in satisfying),
        admission_ids=tuple(item.record_id for item in accepted),
    )


def _assess_refuted(
    contract: ProblemContract,
    obligation: Obligation,
    view: ResearchView,
    snapshot: ProjectSnapshot,
    audit: FinalAuditReceipt,
) -> AcceptanceRecord:
    admissions = collect_fact_admissions(snapshot)
    if any(
        item.verdict == _VERDICT_WRONG
        and item.provenance == _FACT_SUBMIT
        and item.transport_status == _TRANSPORT_OK
        and not item.accepted
        for item in admissions
    ) and not any(item.accepted for item in admissions):
        # Verifier rejection never counts as refutation, even if the obligation
        # is already REFUTED in the view without independent counterexample audit.
        pass
    if audit.audit_kind is not FinalAuditKind.COUNTEREXAMPLE:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_kind_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    if not audit.translation_confirmed or not audit.original_object_realized:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("missing_object_lifting_audit",),
            created_at=audit.created_at,
            audit=audit,
        )
    satisfying = _satisfying_evidence(
        view,
        obligation,
        required=EvidenceKind.CHECKED_COUNTEREXAMPLE,
    )
    if not satisfying:
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("missing_checked_counterexample",),
            created_at=audit.created_at,
            audit=audit,
        )
    if audit.evidence_ids and any(
        item not in {art.artifact_id for art in satisfying} for item in audit.evidence_ids
    ):
        return _acceptance(
            contract,
            obligation,
            verdict=AcceptanceVerdict.FAIL,
            reasons=("audit_evidence_mismatch",),
            created_at=audit.created_at,
            audit=audit,
        )
    return _acceptance(
        contract,
        obligation,
        verdict=AcceptanceVerdict.PASS,
        reasons=(),
        created_at=audit.created_at,
        audit=audit,
        project_outcome=ProjectOutcome.REFUTED,
        evidence_ids=tuple(item.artifact_id for item in satisfying),
        admission_ids=tuple(item.record_id for item in admissions if item.accepted),
    )


def _kernel_acceptance_reason(
    contract: ProblemContract,
    obligation: Obligation,
    satisfying: tuple[EvidenceArtifact, ...],
    audit: FinalAuditReceipt,
    artifacts: ArtifactStore,
    snapshot: ProjectSnapshot,
    alignment: StatementAlignmentAttestation | None,
) -> Optional[str]:
    kernel_items = [
        item
        for item in satisfying
        if item.evidence_kind is EvidenceKind.KERNEL_CHECKED_PROOF
        and item.checker_result is CheckerResult.PASS
    ]
    if not kernel_items:
        return "kernel_evidence_required"
    if audit.evidence_ids:
        matched_ids = [item for item in kernel_items if item.artifact_id in audit.evidence_ids]
        if matched_ids:
            kernel_items = matched_ids
    if alignment is not None:
        matched_hash = [
            item for item in kernel_items if item.content_hash == alignment.kernel_artifact_hash
        ]
        if matched_hash:
            kernel_items = matched_hash
    kernel_ev = kernel_items[0]
    try:
        kernel = load_kernel_artifact(kernel_ev.content_hash, artifacts)
    except KernelEvidenceError:
        return "kernel_artifact_missing"
    if kernel.status is not KernelCheckStatus.PASS:
        return "kernel_check_not_pass"
    try:
        require_trusted_kernel_artifact(kernel)
    except KernelEvidenceError:
        return "kernel_checker_provenance_invalid"
    if kernel.contract_id != contract.contract_id or kernel.obligation_id != obligation.obligation_id:
        return "kernel_target_mismatch"
    if kernel.exact_claim != obligation.exact_statement:
        return "kernel_claim_mismatch"
    if kernel.provenance_kind != "lake-env":
        return "kernel_untrusted_provenance"
    if not kernel.lean_githash or Path(kernel.lake_executable or "").name not in {"lake", "lake.exe"}:
        return "kernel_missing_lake_env_fingerprint"
    resolved = _resolve_alignment(
        contract,
        obligation,
        kernel,
        audit,
        artifacts,
        snapshot,
        alignment,
        kernel_hash=kernel_ev.content_hash,
    )
    if resolved is None:
        return "missing_statement_alignment"
    if not resolved.aligned:
        return "alignment_not_attested"
    if resolved.version < 1:
        return "alignment_unversioned"
    if resolved.kernel_artifact_hash != kernel_ev.content_hash:
        return "alignment_kernel_mismatch"
    if resolved.exact_claim != obligation.exact_statement:
        return "alignment_claim_mismatch"
    if resolved.contract_id != contract.contract_id:
        return "alignment_contract_mismatch"
    if resolved.obligation_id != obligation.obligation_id:
        return "alignment_obligation_mismatch"
    if resolved.theorem_name != kernel.theorem_name or resolved.module_name != kernel.module_name:
        return "alignment_theorem_mismatch"
    if resolved.formal_statement != kernel.theorem_type:
        return "alignment_formal_statement_mismatch"
    if (
        audit.kernel_artifact_hash is not None
        and audit.kernel_artifact_hash != kernel_ev.content_hash
    ):
        return "audit_kernel_mismatch"
    if (
        audit.alignment_attestation_hash is not None
        and audit.alignment_attestation_hash
        != sha256_hex(canonical_json(resolved.to_dict()).encode("utf-8"))
        and audit.alignment_attestation_hash != resolved.attestation_id
    ):
        return "audit_alignment_mismatch"
    return None


def _resolve_alignment(
    contract: ProblemContract,
    obligation: Obligation,
    kernel: KernelCheckArtifact,
    audit: FinalAuditReceipt,
    artifacts: ArtifactStore,
    snapshot: ProjectSnapshot,
    alignment: StatementAlignmentAttestation | None,
    *,
    kernel_hash: str,
) -> StatementAlignmentAttestation | None:
    if alignment is not None:
        return alignment
    if audit.alignment_attestation_hash:
        try:
            artifacts.verify(audit.alignment_attestation_hash)
            blob = artifacts.get_bytes(audit.alignment_attestation_hash)
            payload = json.loads(blob.decode("utf-8"))
            if isinstance(payload, Mapping) and "attestation" in payload:
                payload = payload["attestation"]
            return StatementAlignmentAttestation.from_dict(payload)
        except (ArtifactStoreError, KernelEvidenceError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        for item in collect_statement_alignments(snapshot):
            if item.attestation_id == audit.alignment_attestation_hash:
                return item
    for item in collect_statement_alignments(snapshot):
        if (
            item.contract_id == contract.contract_id
            and item.obligation_id == obligation.obligation_id
            and item.kernel_artifact_hash == kernel_hash
            and item.exact_claim == obligation.exact_statement
        ):
            return item
    return None


def _satisfying_evidence(
    view: ResearchView,
    obligation: Obligation,
    *,
    required: EvidenceKind,
) -> tuple[EvidenceArtifact, ...]:
    found: list[EvidenceArtifact] = []
    for artifact_id in obligation.evidence_refs:
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            continue
        if evidence_satisfies(
            artifact,
            required,
            obligation.scope,
            obligation.contract_id,
            exact_claim=obligation.exact_statement,
        ):
            found.append(artifact)
    return tuple(found)


def _acceptance(
    contract: ProblemContract,
    obligation: Obligation,
    *,
    verdict: AcceptanceVerdict,
    reasons: tuple[str, ...] | list[str],
    created_at: str,
    audit: FinalAuditReceipt | None = None,
    project_outcome: ProjectOutcome | None = None,
    fact_id: str | None = None,
    evidence_ids: tuple[str, ...] = (),
    admission_ids: tuple[str, ...] = (),
) -> AcceptanceRecord:
    reasons_t = tuple(reasons)
    audit_id = None if audit is None else audit.receipt_id
    record_id = _acceptance_id(
        verdict=verdict,
        reasons=reasons_t,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        project_outcome=project_outcome,
        fact_id=fact_id,
        evidence_ids=evidence_ids,
        audit_receipt_id=audit_id,
        admission_ids=admission_ids,
    )
    return AcceptanceRecord(
        record_id=record_id,
        verdict=verdict,
        reasons=reasons_t,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        project_outcome=project_outcome,
        fact_id=fact_id,
        evidence_ids=evidence_ids,
        audit_receipt_id=audit_id,
        admission_ids=admission_ids,
        created_at=created_at,
    )


def _parse_verification_record(record_id: str, encoded: str) -> FactAdmissionEvidence:
    reasons: list[str] = []
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError:
        return FactAdmissionEvidence(
            record_id=record_id,
            provenance="malformed",
            transport_status="malformed",
            verdict=None,
            fact_id=None,
            exact_claim="",
            write_error=None,
            accepted=False,
            reasons=("malformed_json",),
        )
    if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
        return FactAdmissionEvidence(
            record_id=record_id,
            provenance="malformed",
            transport_status="malformed",
            verdict=None,
            fact_id=None,
            exact_claim="",
            write_error=None,
            accepted=False,
            reasons=("malformed_record",),
        )
    provenance = payload.get("provenance")
    transport = payload.get("transport_status")
    verdict = payload.get("verdict")
    fact_id = payload.get("fact_id")
    claim = payload.get("claim")
    write_error = payload.get("write_error")
    if not isinstance(provenance, str) or provenance == "":
        reasons.append("malformed_provenance")
        provenance = "malformed"
    if not isinstance(transport, str) or transport == "":
        reasons.append("malformed_transport_status")
        transport = "malformed"
    if verdict is not None and (not isinstance(verdict, str) or verdict == ""):
        reasons.append("malformed_verdict")
        verdict = None
    if fact_id is not None and (not isinstance(fact_id, str) or fact_id == ""):
        reasons.append("malformed_fact_id")
        fact_id = None
    if not isinstance(claim, str):
        reasons.append("malformed_claim")
        claim = ""
    if write_error is not None:
        if not isinstance(write_error, str) or write_error == "":
            if write_error == "":
                write_error = None
            else:
                reasons.append("malformed_write_error")
                write_error = "malformed"
    return FactAdmissionEvidence(
        record_id=record_id,
        provenance=provenance,
        transport_status=transport,
        verdict=verdict,
        fact_id=fact_id,
        exact_claim=claim,
        write_error=write_error,
        accepted=False,
        reasons=tuple(reasons),
    )


def _contradictory_fact_ids(rows: Sequence[FactAdmissionEvidence]) -> set[str]:
    grouped: dict[str, set[tuple[str, Optional[str]]]] = {}
    for item in rows:
        if item.fact_id is None:
            continue
        grouped.setdefault(item.fact_id, set()).add((item.exact_claim, item.verdict))
    return {fact_id for fact_id, variants in grouped.items() if len(variants) > 1}


def _reached_limited_dimensions(
    policy: BudgetPolicy, resources: ResourceSnapshot
) -> tuple[str, ...]:
    reached: list[str] = []
    for name in RESOURCE_DIMENSIONS:
        remaining = resources.remaining_total(policy, name)
        if remaining is not None and remaining <= 0:
            reached.append(name)
        elif (
            policy.dimension(name).kind is ResourceLimitKind.LIMITED
            and remaining is not None
            and remaining <= 0
        ):
            reached.append(name)
    return tuple(reached)


def _require_stall_matches_view(stall: StallEvidence, view: ResearchView) -> None:
    if tuple(stall.frontier_obligation_ids) != view.frontier():
        raise AcceptanceError("stall frontier_obligation_ids must equal view.frontier()")
    for route_id in stall.untested_route_ids:
        route = view.get_route(route_id)
        if route is None:
            raise AcceptanceError(f"untested route {route_id} is missing")
        if route.validity is not ValidityStatus.VALID:
            raise AcceptanceError(f"untested route {route_id} is not VALID")
        if route.status in {RouteStatus.COMPLETED, RouteStatus.REFUTED}:
            raise AcceptanceError(
                f"untested route {route_id} is completed or refuted"
            )


def _require_infra_leases(
    evidence: InfrastructureBlockEvidence, runtime: RuntimeSnapshot
) -> None:
    by_id = {item.lease_id: item for item in runtime.leases}
    for lease_id in evidence.lease_ids:
        lease = by_id.get(lease_id)
        if lease is None:
            raise AcceptanceError(f"infra block names unknown lease {lease_id}")
        if lease.status not in {LeaseStatus.INFRA_FAILED, LeaseStatus.EXPIRED}:
            raise AcceptanceError(
                "infra block requires INFRA_FAILED or EXPIRED leases"
            )


def _require_pass_audit_refs(audit: FinalAuditReceipt) -> None:
    if audit.audit_kind is FinalAuditKind.PROOF:
        if audit.fact_id is None:
            raise AcceptanceError("PROOF PASS requires fact_id")
        if not audit.evidence_ids:
            raise AcceptanceError("PROOF PASS requires evidence_ids")
        if audit.translation_confirmed or audit.original_object_realized:
            raise AcceptanceError("PROOF PASS cannot claim counterexample lifting")
        return
    if not audit.translation_confirmed or not audit.original_object_realized:
        raise AcceptanceError(
            "COUNTEREXAMPLE PASS requires translation and original-object realization"
        )
    if not audit.evidence_ids and (
        audit.candidate_content_hash is None or audit.report_content_hash is None
    ):
        raise AcceptanceError(
            "COUNTEREXAMPLE PASS requires evidence_ids or candidate/report hashes"
        )


def _audit_id_from_record(audit: FinalAuditReceipt) -> str:
    return _audit_id(
        audit_kind=audit.audit_kind,
        checker_result=audit.checker_result,
        contract_id=audit.contract_id,
        obligation_id=audit.obligation_id,
        action_id=audit.action_id,
        exact_claim=audit.exact_claim,
        scope=audit.scope,
        acceptance_policy_hash=audit.acceptance_policy_hash,
        auditor_id=audit.auditor_id,
        auditor_version=audit.auditor_version,
        environment_hash=audit.environment_hash,
        audit_artifact_hash=audit.audit_artifact_hash,
        translation_confirmed=audit.translation_confirmed,
        original_object_realized=audit.original_object_realized,
        fact_id=audit.fact_id,
        evidence_ids=audit.evidence_ids,
        candidate_content_hash=audit.candidate_content_hash,
        report_content_hash=audit.report_content_hash,
        kernel_artifact_hash=audit.kernel_artifact_hash,
        alignment_attestation_hash=audit.alignment_attestation_hash,
    )


def _audit_id(
    *,
    audit_kind: FinalAuditKind,
    checker_result: CheckerResult,
    contract_id: str,
    obligation_id: str,
    action_id: str,
    exact_claim: str,
    scope: MathematicalScope,
    acceptance_policy_hash: str,
    auditor_id: str,
    auditor_version: str,
    environment_hash: str,
    audit_artifact_hash: str,
    translation_confirmed: bool,
    original_object_realized: bool,
    fact_id: Optional[str],
    evidence_ids: tuple[str, ...],
    candidate_content_hash: Optional[str],
    report_content_hash: Optional[str],
    kernel_artifact_hash: Optional[str] = None,
    alignment_attestation_hash: Optional[str] = None,
) -> str:
    kind = _require_enum(FinalAuditKind, audit_kind, "audit_kind")
    result = _require_enum(CheckerResult, checker_result, "checker_result")
    payload = {
        "audit_kind": kind.value,
        "checker_result": result.value,
        "contract_id": contract_id,
        "obligation_id": obligation_id,
        "action_id": action_id,
        "exact_claim": exact_claim,
        "scope": scope.to_dict(),
        "acceptance_policy_hash": acceptance_policy_hash,
        "auditor_id": auditor_id,
        "auditor_version": auditor_version,
        "environment_hash": environment_hash,
        "audit_artifact_hash": audit_artifact_hash,
        "translation_confirmed": translation_confirmed,
        "original_object_realized": original_object_realized,
        "fact_id": fact_id,
        "evidence_ids": list(evidence_ids),
        "candidate_content_hash": candidate_content_hash,
        "report_content_hash": report_content_hash,
    }
    if kernel_artifact_hash is not None:
        payload["kernel_artifact_hash"] = kernel_artifact_hash
    if alignment_attestation_hash is not None:
        payload["alignment_attestation_hash"] = alignment_attestation_hash
    return stable_id(_AUDIT_DOMAIN, _ID_VERSION, canonical_json(payload))


def _acceptance_id_from_record(record: AcceptanceRecord) -> str:
    return _acceptance_id(
        verdict=record.verdict,
        reasons=record.reasons,
        contract_id=record.contract_id,
        obligation_id=record.obligation_id,
        project_outcome=record.project_outcome,
        fact_id=record.fact_id,
        evidence_ids=record.evidence_ids,
        audit_receipt_id=record.audit_receipt_id,
        admission_ids=record.admission_ids,
    )


def _acceptance_id(
    *,
    verdict: AcceptanceVerdict,
    reasons: tuple[str, ...],
    contract_id: str,
    obligation_id: str,
    project_outcome: Optional[ProjectOutcome],
    fact_id: Optional[str],
    evidence_ids: tuple[str, ...],
    audit_receipt_id: Optional[str],
    admission_ids: tuple[str, ...],
) -> str:
    payload = {
        "verdict": _require_enum(AcceptanceVerdict, verdict, "verdict").value,
        "reasons": list(reasons),
        "contract_id": contract_id,
        "obligation_id": obligation_id,
        "project_outcome": None
        if project_outcome is None
        else _require_enum(ProjectOutcome, project_outcome, "project_outcome").value,
        "fact_id": fact_id,
        "evidence_ids": list(evidence_ids),
        "audit_receipt_id": audit_receipt_id,
        "admission_ids": list(admission_ids),
    }
    return stable_id(_ACCEPTANCE_DOMAIN, _ID_VERSION, canonical_json(payload))


def _checkpoint_id_from_record(report: CheckpointReport) -> str:
    return _checkpoint_id(
        contract_id=report.contract_id,
        outcome=report.outcome,
        source_event_seq=report.source_event_seq,
        resources=report.resources,
        accepted_result_ids=report.accepted_result_ids,
        open_obligation_ids=report.open_obligation_ids,
        scope_aware_failure_ids=report.scope_aware_failure_ids,
        untested_route_ids=report.untested_route_ids,
        active_lease_ids=report.active_lease_ids,
        pause_reason=report.pause_reason,
        mathematical_status=report.mathematical_status,
        restart_conditions=report.restart_conditions,
    )


def _checkpoint_id(
    *,
    contract_id: str,
    outcome: ProjectOutcome,
    source_event_seq: int,
    resources: ResourceSnapshot,
    accepted_result_ids: tuple[str, ...],
    open_obligation_ids: tuple[str, ...],
    scope_aware_failure_ids: tuple[str, ...],
    untested_route_ids: tuple[str, ...],
    active_lease_ids: tuple[str, ...],
    pause_reason: str,
    mathematical_status: MathematicalStatus,
    restart_conditions: tuple[str, ...],
) -> str:
    payload = {
        "contract_id": contract_id,
        "outcome": _require_enum(ProjectOutcome, outcome, "outcome").value,
        "source_event_seq": _require_nonneg_int("source_event_seq", source_event_seq),
        "resources": resources.to_dict(),
        "accepted_result_ids": list(accepted_result_ids),
        "open_obligation_ids": list(open_obligation_ids),
        "scope_aware_failure_ids": list(scope_aware_failure_ids),
        "untested_route_ids": list(untested_route_ids),
        "active_lease_ids": list(active_lease_ids),
        "pause_reason": pause_reason,
        "mathematical_status": _require_enum(
            MathematicalStatus, mathematical_status, "mathematical_status"
        ).value,
        "restart_conditions": list(restart_conditions),
    }
    return stable_id(_CHECKPOINT_DOMAIN, _ID_VERSION, canonical_json(payload))


def _utc_now() -> str:
    return datetime.now(_UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(name: str, value: object) -> datetime:
    if not isinstance(value, str) or value == "":
        raise AcceptanceError(f"{name} must be ISO-8601 UTC")
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise AcceptanceError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise AcceptanceError(f"{name} must be UTC")
    return dt.astimezone(_UTC)


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise AcceptanceError(f"{name} must be a nonempty str")
    return value


def _require_restart(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    _reject_forbidden_text(name, text)
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise AcceptanceError(f"{name} must be 64-char lowercase hex")
    return text


def _require_fact_id(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX16_RE.fullmatch(text) is None:
        raise AcceptanceError(f"{name} must be 16-char lowercase hex")
    return text


def _require_result_id(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX16_RE.fullmatch(text) is None and _HEX64_RE.fullmatch(text) is None:
        raise AcceptanceError(f"{name} must be 16-hex fact_id or 64-hex id")
    return text


def _require_result_id_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise AcceptanceError(f"{name} must be a list of result ids")
    return tuple(
        _require_result_id(f"{name}[{index}]", item) for index, item in enumerate(value)
    )


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AcceptanceError(
            f"{name} must be a nonnegative int, not {type(value).__name__}"
        )
    if value < 0:
        raise AcceptanceError(f"{name} must be a nonnegative int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise AcceptanceError(f"{name} has unknown value {value!r}") from None
    raise AcceptanceError(f"{name} must be {enum_cls.__name__}")


def _require_str_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise AcceptanceError(f"{name} must be a list of str")
    out: list[str] = []
    for index, item in enumerate(value):
        out.append(_require_nonempty(f"{name}[{index}]", item))
    return tuple(out)


def _require_hex_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise AcceptanceError(f"{name} must be a list of 64-hex ids")
    return tuple(_require_hex64(f"{name}[{index}]", item) for index, item in enumerate(value))


def _reject_forbidden_text(name: str, value: str) -> None:
    lowered = value.lower()
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in lowered:
            raise AcceptanceError(f"{name} forbids unsolvability wording")


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise AcceptanceError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise AcceptanceError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise AcceptanceError(f"{label} missing fields: {sorted(missing)}")
