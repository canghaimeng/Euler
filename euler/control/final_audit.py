"""Automatic independent final-audit action and acceptance.

Stdlib plus existing control/runtime/acceptance APIs. The default verify
transport classifies HTTP/protocol exactly as the truth gateway, but this
module never writes the Fact Graph or global memory. Provider launch is out
of scope.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol

from euler.control.acceptance import (
    AcceptanceRecord,
    AcceptanceVerdict,
    FinalAuditKind,
    FinalAuditReceipt,
    ProjectOutcome,
    assess_final_acceptance,
    build_checkpoint_report,
    collect_fact_admissions,
    derive_project_outcome,
    kernel_acceptance_required,
)
from euler.control.adapters.project import FactSnapshot, snapshot_project
from euler.control.artifacts import ArtifactStoreError
from euler.control.context import ContextCompiler, ContextLimits
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    ProblemContract,
    evidence_satisfies,
)
from euler.control.failures import ConfidenceClass, FailureKind, FailureRecord
from euler.control.ids import canonical_json, sha256_hex, stable_id
from euler.control.kernel_evidence import StatementAlignmentAttestation
from euler.control.live_records import (
    EVENT_ACCEPTANCE_RECORDED,
    EVENT_CHECKPOINT_CREATED,
    EVENT_DISPATCH_PLANNED,
)
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.audit_resources import (
    AuditResourceError,
    configured_timeout_seconds,
    is_live_http_transport,
    resolve_audit_reservation,
    timeout_ms_from_seconds,
    verifier_environment_hash,
)
from euler.control.policy import BudgetClass, ResourceVector
from euler.control.project import EulerProject
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    DispatchPlan,
    PriorityClass,
    ScheduleDisposition,
    ScheduleReason,
    TransparentScheduler,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import (
    EventEnvelope,
    MathematicalStatus,
    ObservationStatus,
    ObservedQuantity,
    UsageRecord,
)
from euler.control.recurrence import RecurrenceRecommendation

_POLICY_DOMAIN = "final-acceptance-policy"
_RUN_DOMAIN = "final-audit-run"
_CANDIDATE_DOMAIN = "final-audit-candidate"
_POLICY_VERSION = 1
_AUDITOR_ID = "euler-final-audit"
_AUDITOR_VERSION = "1"
_ACTOR = "final-audit"
_MECHANISM = "final-audit"
_OK = "ok"
_CORRECT = "correct"
_WRONG = "wrong"
_HEX16 = 16

FINAL_ACCEPTANCE_POLICY_VERSION = _POLICY_VERSION
FINAL_ACCEPTANCE_POLICY_HASH = stable_id(
    _POLICY_DOMAIN,
    _POLICY_VERSION,
    "independent-second-verify",
    "wrong-is-not-refutation",
)

FINAL_AUDIT_RETRY_SPEC = {
    "parameter": "retry_infra",
    "reason_parameter": "retry_reason",
    "cli_flags": ["--retry-infra", "--retry-reason TEXT"],
    "mcp_fields": ["retry_infra", "retry_reason"],
    "applies_when": "FinalAuditStatus.INFRA",
    "requires_nonempty_reason": True,
    "effect": (
        "issue a fresh attempt and reservation bound to the current audit "
        "identity; a successful completed audit of the same identity is "
        "replayed without a second verify call"
    ),
    "identity_binds": (
        "obligation_id",
        "contract_id",
        "fact_id",
        "fact_digest",
        "proof_digest",
        "evidence_id",
        "evidence_digest",
        "acceptance_policy_hash",
        "environment_hash",
        "alignment_digest",
    ),
}

__all__ = [
    "FINAL_ACCEPTANCE_POLICY_VERSION",
    "FINAL_ACCEPTANCE_POLICY_HASH",
    "FINAL_AUDIT_RETRY_SPEC",
    "FinalAuditError",
    "FinalAuditStatus",
    "FinalAuditIdentity",
    "VerifyResult",
    "VerifyTransport",
    "HttpVerifyTransport",
    "FinalAuditRun",
    "extract_proof_body",
    "build_final_audit_identity",
    "run_final_audit",
]


class FinalAuditError(ValueError):
    """Precondition failed or a stored audit artifact is unusable."""


class FinalAuditStatus(str, Enum):
    SOLVED = "SOLVED"
    DEFERRED = "DEFERRED"
    AUDIT_FAIL = "AUDIT_FAIL"
    INFRA = "INFRA"


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalAuditIdentity:
    obligation_id: str
    contract_id: str
    fact_id: str
    fact_digest: str
    proof_digest: str
    evidence_id: str
    evidence_digest: str
    policy_hash: str
    environment_hash: str
    identity_hash: str
    alignment_digest: str = ""
    dependency_digest: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "fact_id": self.fact_id,
            "fact_digest": self.fact_digest,
            "proof_digest": self.proof_digest,
            "evidence_id": self.evidence_id,
            "evidence_digest": self.evidence_digest,
            "policy_hash": self.policy_hash,
            "environment_hash": self.environment_hash,
            "identity_hash": self.identity_hash,
            "alignment_digest": self.alignment_digest,
            "dependency_digest": self.dependency_digest,
        }

    @classmethod
    def from_dict(cls, data: object) -> "FinalAuditIdentity":
        if not isinstance(data, Mapping):
            raise FinalAuditError("FinalAuditIdentity must be a mapping")
        payload = dict(data)
        return cls(
            obligation_id=str(payload["obligation_id"]),
            contract_id=str(payload["contract_id"]),
            fact_id=str(payload["fact_id"]),
            fact_digest=str(payload["fact_digest"]),
            proof_digest=str(payload["proof_digest"]),
            evidence_id=str(payload["evidence_id"]),
            evidence_digest=str(payload["evidence_digest"]),
            policy_hash=str(payload["policy_hash"]),
            environment_hash=str(payload["environment_hash"]),
            identity_hash=str(payload["identity_hash"]),
            alignment_digest=str(payload.get("alignment_digest") or ""),
            dependency_digest=str(payload.get("dependency_digest") or ""),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifyResult:
    """Gateway-shaped verify classification. Never a Fact write."""

    transport_status: str
    verdict: Optional[str]
    detail: str
    body: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transport_status", str(self.transport_status or "").strip()
        )
        if not self.transport_status:
            raise FinalAuditError("transport_status is required")
        verdict = self.verdict
        if verdict is not None:
            verdict = str(verdict).strip() or None
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "detail", str(self.detail or ""))
        body = dict(self.body or {})
        object.__setattr__(self, "body", MappingProxyType(body))

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport_status": self.transport_status,
            "verdict": self.verdict,
            "detail": self.detail,
            "body": dict(self.body),
        }


class VerifyTransport(Protocol):
    def verify(self, statement: str, proof: str) -> VerifyResult:
        """POST-equivalent verify. Must not write Fact Graph or GM."""


class HttpVerifyTransport:
    """POST ``{statement, proof}`` to ``EULER_VERIFY_URL``. Stdlib only.

    ``timeout_seconds=None`` reads ``EULER_VERIFY_TIMEOUT`` (default 3600) at
    call time so existing callers stay unchanged.
    """

    live_http = True

    def __init__(self, timeout_seconds: int | float | None = None) -> None:
        if timeout_seconds is None:
            self._timeout_seconds = None
        else:
            self._timeout_seconds = configured_timeout_seconds(timeout_seconds)

    @property
    def timeout_seconds(self) -> int | float:
        if self._timeout_seconds is not None:
            return self._timeout_seconds
        return configured_timeout_seconds()

    def verify(self, statement: str, proof: str) -> VerifyResult:
        from euler.gateway.server import (
            classify_http_verify_error,
            classify_verify_transport_error,
        )

        url = os.environ.get("EULER_VERIFY_URL", "")
        if not url:
            return VerifyResult(
                transport_status="service_error",
                verdict=None,
                detail="EULER_VERIFY_URL is not set",
                body={"error": "EULER_VERIFY_URL is not set"},
            )
        timeout = self.timeout_seconds
        data = json.dumps(
            {"statement": statement, "proof": proof, "force_fresh": True}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            classified = classify_http_verify_error(exc)
            return VerifyResult(
                transport_status=str(classified["transport_status"]),
                verdict=classified.get("verdict"),
                detail=str(classified.get("detail") or ""),
                body={
                    "transport_status": classified["transport_status"],
                    "verdict": classified.get("verdict"),
                    "detail": classified.get("detail"),
                    "repair_hints": classified.get("repair_hints"),
                },
            )
        except Exception as exc:
            status, detail = classify_verify_transport_error(exc)
            return VerifyResult(
                transport_status=str(status),
                verdict=None,
                detail=str(detail),
                body={"transport_status": status, "detail": detail},
            )
        if not isinstance(payload, dict):
            return VerifyResult(
                transport_status="protocol_error",
                verdict=None,
                detail="verify response is not a JSON object",
                body={"raw": payload},
            )
        verdict = payload.get("verdict")
        if verdict not in {_CORRECT, _WRONG}:
            return VerifyResult(
                transport_status="protocol_error",
                verdict=None,
                detail="verify response missing verdict correct|wrong",
                body=payload,
            )
        return VerifyResult(
            transport_status=_OK,
            verdict=str(verdict),
            detail=str(payload.get("detail") or payload.get("repair_hints") or ""),
            body=payload,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalAuditRun:
    run_id: str
    obligation_id: str
    fact_id: str
    status: FinalAuditStatus
    replayed: bool
    plan_hash: str
    disposition: str
    reason: str
    lease_id: Optional[str] = None
    action_id: Optional[str] = None
    transport_status: Optional[str] = None
    verdict: Optional[str] = None
    audit_receipt_id: Optional[str] = None
    acceptance_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    packet_hash: Optional[str] = None
    verify_artifact_hash: Optional[str] = None
    outcome: Optional[str] = None
    identity_hash: Optional[str] = None
    attempt_index: int = 1
    failure_id: Optional[str] = None
    contract_id: Optional[str] = None
    proof_digest: Optional[str] = None
    fact_digest: Optional[str] = None
    evidence_digest: Optional[str] = None
    retry_reason: Optional[str] = None
    alignment_digest: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_hex64("run_id", self.run_id))
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(self, "fact_id", _require_hex16("fact_id", self.fact_id))
        object.__setattr__(
            self, "status", _require_status(self.status)
        )
        if not isinstance(self.replayed, bool):
            raise FinalAuditError("replayed must be bool")
        object.__setattr__(self, "plan_hash", _require_hex64("plan_hash", self.plan_hash))
        object.__setattr__(self, "disposition", str(self.disposition))
        object.__setattr__(self, "reason", str(self.reason))
        if not isinstance(self.attempt_index, int) or isinstance(self.attempt_index, bool):
            raise FinalAuditError("attempt_index must be int")
        if self.attempt_index < 1:
            raise FinalAuditError("attempt_index must be >= 1")
        expected = _run_id(
            obligation_id=self.obligation_id,
            fact_id=self.fact_id,
            status=self.status.value,
            plan_hash=self.plan_hash,
            lease_id=self.lease_id or "",
            verify_artifact_hash=self.verify_artifact_hash or "",
            acceptance_id=self.acceptance_id or "",
            identity_hash=self.identity_hash or "",
            attempt_index=self.attempt_index,
        )
        if self.run_id != expected:
            raise FinalAuditError("run_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "obligation_id": self.obligation_id,
            "fact_id": self.fact_id,
            "status": self.status.value,
            "replayed": self.replayed,
            "plan_hash": self.plan_hash,
            "disposition": self.disposition,
            "reason": self.reason,
            "lease_id": self.lease_id,
            "action_id": self.action_id,
            "transport_status": self.transport_status,
            "verdict": self.verdict,
            "audit_receipt_id": self.audit_receipt_id,
            "acceptance_id": self.acceptance_id,
            "checkpoint_id": self.checkpoint_id,
            "packet_hash": self.packet_hash,
            "verify_artifact_hash": self.verify_artifact_hash,
            "outcome": self.outcome,
            "identity_hash": self.identity_hash,
            "attempt_index": self.attempt_index,
            "failure_id": self.failure_id,
            "contract_id": self.contract_id,
            "proof_digest": self.proof_digest,
            "fact_digest": self.fact_digest,
            "evidence_digest": self.evidence_digest,
            "retry_reason": self.retry_reason,
            "alignment_digest": self.alignment_digest,
        }

    @classmethod
    def from_dict(cls, data: object) -> "FinalAuditRun":
        if not isinstance(data, Mapping):
            raise FinalAuditError("FinalAuditRun must be a mapping")
        payload = dict(data)
        return cls(
            run_id=str(payload["run_id"]),
            obligation_id=str(payload["obligation_id"]),
            fact_id=str(payload["fact_id"]),
            status=FinalAuditStatus(payload["status"]),
            replayed=bool(payload["replayed"]),
            plan_hash=str(payload["plan_hash"]),
            disposition=str(payload["disposition"]),
            reason=str(payload["reason"]),
            lease_id=payload.get("lease_id"),
            action_id=payload.get("action_id"),
            transport_status=payload.get("transport_status"),
            verdict=payload.get("verdict"),
            audit_receipt_id=payload.get("audit_receipt_id"),
            acceptance_id=payload.get("acceptance_id"),
            checkpoint_id=payload.get("checkpoint_id"),
            packet_hash=payload.get("packet_hash"),
            verify_artifact_hash=payload.get("verify_artifact_hash"),
            outcome=payload.get("outcome"),
            identity_hash=payload.get("identity_hash"),
            attempt_index=int(payload.get("attempt_index") or 1),
            failure_id=payload.get("failure_id"),
            contract_id=payload.get("contract_id"),
            proof_digest=payload.get("proof_digest"),
            fact_digest=payload.get("fact_digest"),
            evidence_digest=payload.get("evidence_digest"),
            retry_reason=payload.get("retry_reason"),
            alignment_digest=payload.get("alignment_digest"),
        )

    @classmethod
    def build(cls, **fields: Any) -> "FinalAuditRun":
        obligation_id = str(fields["obligation_id"])
        fact_id = str(fields["fact_id"])
        status = _require_status(fields["status"])
        plan_hash = str(fields["plan_hash"])
        lease_id = fields.get("lease_id")
        verify_artifact_hash = fields.get("verify_artifact_hash")
        acceptance_id = fields.get("acceptance_id")
        identity_hash = fields.get("identity_hash")
        attempt_index = int(fields.get("attempt_index") or 1)
        run_id = _run_id(
            obligation_id=obligation_id,
            fact_id=fact_id,
            status=status.value,
            plan_hash=plan_hash,
            lease_id=lease_id or "",
            verify_artifact_hash=verify_artifact_hash or "",
            acceptance_id=acceptance_id or "",
            identity_hash=identity_hash or "",
            attempt_index=attempt_index,
        )
        return cls(run_id=run_id, status=status, **{
            key: value for key, value in fields.items() if key != "status"
        })


def extract_proof_body(markdown: str) -> str:
    """Extract proof text, preserving its internal Markdown section headings.

    Fact serialization places proof last, followed only by optional reserved
    ``## intuition`` metadata. Arbitrary level-two proof headings are content,
    not section boundaries. Fenced examples must not introduce boundaries.
    """
    if not isinstance(markdown, str):
        raise FinalAuditError("fact markdown must be str")
    out: list[str] = []
    in_proof = False
    fence = None
    for line in markdown.splitlines():
        stripped = line.strip()
        if not in_proof:
            if stripped.lower() == "## proof":
                in_proof = True
            continue
        if fence is None and stripped.startswith(("```", "~~~")):
            char = stripped[0]
            fence = (char, len(stripped) - len(stripped.lstrip(char)))
        elif fence is not None:
            char, length = fence
            if stripped and set(stripped) == {char} and len(stripped) >= length:
                fence = None
        elif line.lower() == "## intuition":
            break
        out.append(line)
    proof = "\n".join(out).strip()
    if not proof:
        raise FinalAuditError("active target Fact has no ## proof body")
    return proof


def build_final_audit_identity(
    *,
    contract: ProblemContract,
    obligation: Obligation,
    fact: FactSnapshot,
    proof: str,
    evidence: EvidenceArtifact,
    environment_hash: str,
    policy_hash: str = FINAL_ACCEPTANCE_POLICY_HASH,
    alignment: StatementAlignmentAttestation | None = None,
    dependency_digest: str = "",
) -> FinalAuditIdentity:
    fact_digest = str(fact.content_sha256)
    proof_digest = sha256_hex(proof.encode("utf-8"))
    evidence_digest = evidence.content_hash
    alignment_digest = "" if alignment is None else alignment.attestation_id
    identity_hash = stable_id(
        "final-audit-identity",
        3,
        obligation.obligation_id,
        contract.contract_id,
        fact.fact_id,
        fact_digest,
        proof_digest,
        evidence.artifact_id,
        evidence_digest,
        policy_hash,
        environment_hash,
        alignment_digest,
        dependency_digest,
    )
    return FinalAuditIdentity(
        obligation_id=obligation.obligation_id,
        contract_id=contract.contract_id,
        fact_id=fact.fact_id,
        fact_digest=fact_digest,
        proof_digest=proof_digest,
        evidence_id=evidence.artifact_id,
        evidence_digest=evidence_digest,
        policy_hash=policy_hash,
        environment_hash=environment_hash,
        identity_hash=identity_hash,
        alignment_digest=alignment_digest,
        dependency_digest=dependency_digest,
    )


def run_final_audit(
    project: EulerProject,
    *,
    transport: VerifyTransport | None = None,
    timestamp_utc: str | None = None,
    retry_infra: bool = False,
    retry_reason: str | None = None,
    alignment: StatementAlignmentAttestation | None = None,
    requested: ResourceVector | None = None,
) -> FinalAuditRun:
    """Schedule and execute the independent final audit. Never writes truth stores.

    Live HTTP reservations use ``requested`` or ``EULER_FINAL_AUDIT_RESOURCES``.
    Scheduler admission is not provider-side hard billing enforcement.
    """
    if not isinstance(project, EulerProject):
        raise FinalAuditError("project must be an EulerProject")
    if not isinstance(retry_infra, bool):
        raise FinalAuditError("retry_infra must be bool")
    if retry_infra:
        reason_text = str(retry_reason or "").strip()
        if not reason_text:
            raise FinalAuditError("retry_infra requires a nonempty retry_reason")
        retry_reason = reason_text
    elif retry_reason is not None and str(retry_reason).strip() == "":
        retry_reason = None
    ts = timestamp_utc or _utc_now()
    from .research_service import ResearchService
    ResearchService(project, actor=_ACTOR).sync_fact_dependencies(timestamp_utc=ts)
    view = project.research_view()
    contracts = view.contracts()
    if not contracts:
        raise FinalAuditError("project has no ProblemContract")
    contract = next(
        (item for item in contracts if item.contract_id == project.manifest.contract_id),
        contracts[0],
    )
    obligation = view.get_obligation(project.manifest.target_obligation_id)
    if obligation is None:
        raise FinalAuditError("target obligation missing from ResearchView")
    if obligation.status is not ObligationStatus.DISCHARGED:
        raise FinalAuditError("target obligation is not DISCHARGED")
    snapshot = snapshot_project(project.project_dir)
    fact, admission, evidence, alignment = _require_target_truth(
        contract, obligation, view, snapshot, project, alignment=alignment
    )
    proof = extract_proof_body(fact.raw_markdown)
    bus = transport if transport is not None else HttpVerifyTransport()
    timeout_seconds = (
        bus.timeout_seconds if isinstance(bus, HttpVerifyTransport)
        else configured_timeout_seconds()
    )
    env_hash = _auditor_environment_hash(
        timeout_seconds=timeout_seconds, transport=transport,
    )
    identity = build_final_audit_identity(
        contract=contract,
        obligation=obligation,
        fact=fact,
        proof=proof,
        evidence=evidence,
        environment_hash=env_hash,
        alignment=alignment,
        dependency_digest=_fact_dependency_digest(snapshot, fact),
    )
    existing = _load_matching_success(project, identity)
    if existing is not None:
        _assert_replay_artifacts(project, existing)
        return FinalAuditRun.from_dict({**existing.to_dict(), "replayed": True})
    if not retry_infra:
        blocked = _load_matching_infra(project, identity)
        if blocked is not None:
            _assert_replay_artifacts(project, blocked)
            return FinalAuditRun.from_dict({**blocked.to_dict(), "replayed": True})

    attempt_index = _next_attempt_index(project, identity)
    live_http = is_live_http_transport(transport)
    timeout_ms = timeout_ms_from_seconds(timeout_seconds)
    try:
        allowance = resolve_audit_reservation(
            policy=project.policy,
            requested=requested,
            timeout_ms=timeout_ms,
            live_http=live_http,
        )
    except AuditResourceError as exc:
        raise FinalAuditError(str(exc)) from exc
    candidate = _build_candidate(
        contract,
        obligation,
        identity,
        view,
        evidence_kind=evidence.evidence_kind,
        requested=allowance,
    )
    runtime = project.runtime_snapshot()
    plan = TransparentScheduler().select(
        (candidate,),
        policy=project.policy,
        snapshot=runtime.resources,
        now_utc=ts,
        view=view,
        active=_active_work(runtime),
    )
    plan_blob = canonical_json(plan.to_dict()).encode("utf-8")
    plan_hash = sha256_hex(plan_blob)
    project.artifacts.put_bytes(plan_blob, media_type="application/json")
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=_ACTOR,
            event_type=EVENT_DISPATCH_PLANNED,
            idempotency_key=f"final-audit-planned:{obligation.obligation_id}:{plan_hash}",
            payload={
                "plan": plan.to_dict(),
                "plan_hash": plan_hash,
                "obligation_id": obligation.obligation_id,
                "fact_id": fact.fact_id,
            },
            timestamp_utc=ts,
            correlation_id=obligation.obligation_id,
        )
    )
    decision = plan.decision_for(candidate.candidate_id)
    if decision.disposition is not ScheduleDisposition.SELECTED:
        return _finish_run(
            project,
            obligation_id=obligation.obligation_id,
            fact_id=fact.fact_id,
            status=FinalAuditStatus.DEFERRED,
            plan_hash=plan_hash,
            disposition=decision.disposition.value,
            reason=decision.reason.value,
            action_id=candidate.action_spec.action_id,
            replayed=False,
            **_identity_fields(identity, attempt_index),
        )

    packet = ContextCompiler().compile(
        candidate.context_request, view, snapshot, project.artifacts
    )
    packet_ref = project.artifacts.put_bytes(
        packet.rendered_utf8(), media_type="text/plain"
    )
    lease_key = f"final-audit-lease:{identity.identity_hash}:attempt:{attempt_index}"
    lease = project.runtime.issue(
        project.project_id,
        candidate,
        idempotency_key=lease_key,
        actor=_ACTOR,
        timestamp_utc=ts,
        context_cache_key=packet.cache_key.key_id,
    )
    current = next(
        (item for item in project.runtime_snapshot().leases if item.lease_id == lease.lease_id),
        lease,
    )
    if current.status is LeaseStatus.COMPLETED:
        replayed = _load_matching_success(project, identity)
        if replayed is not None:
            _assert_replay_artifacts(project, replayed)
            return FinalAuditRun.from_dict({**replayed.to_dict(), "replayed": True})
        raise FinalAuditError("final-audit lease already completed without a stored success")
    if current.status is LeaseStatus.INFRA_FAILED:
        if not retry_infra:
            replayed = _load_matching_infra(project, identity)
            if replayed is not None:
                _assert_replay_artifacts(project, replayed)
                return FinalAuditRun.from_dict({**replayed.to_dict(), "replayed": True})
        raise FinalAuditError("final-audit lease already terminal without a stored run")
    if current.status is LeaseStatus.ISSUED:
        lease = project.runtime.mark_running(
            project.project_id, lease.lease_id, actor=_ACTOR, timestamp_utc=ts
        )

    started = time.monotonic()
    result = bus.verify(contract.exact_statement, proof)
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    verify_ref = project.artifacts.put_bytes(
        canonical_json(result.to_dict()).encode("utf-8"),
        media_type="application/json",
    )
    usage = _wall_only_usage(elapsed_ms)

    if result.transport_status != _OK or result.verdict not in {_CORRECT, _WRONG}:
        project.runtime.fail_infrastructure(
            project.project_id,
            lease.lease_id,
            usage=usage,
            artifact_refs=(verify_ref.content_hash,),
            reason=f"final-audit {result.transport_status}: {result.detail}",
            actor=_ACTOR,
            timestamp_utc=ts,
        )
        return _finish_run(
            project,
            obligation_id=obligation.obligation_id,
            fact_id=fact.fact_id,
            status=FinalAuditStatus.INFRA,
            plan_hash=plan_hash,
            disposition=decision.disposition.value,
            reason=result.transport_status,
            lease_id=lease.lease_id,
            action_id=candidate.action_spec.action_id,
            transport_status=result.transport_status,
            packet_hash=packet_ref.content_hash,
            verify_artifact_hash=verify_ref.content_hash,
            replayed=False,
            retry_reason=retry_reason,
            **_identity_fields(identity, attempt_index),
        )

    current_identity = _identity_from_live_sources(project, contract, env_hash)
    if current_identity is None or current_identity.identity_hash != identity.identity_hash:
        project.runtime.fail_infrastructure(
            project.project_id,
            lease.lease_id,
            usage=usage,
            artifact_refs=(verify_ref.content_hash,),
            reason="final-audit sources changed during verify",
            actor=_ACTOR,
            timestamp_utc=ts,
        )
        return _finish_run(
            project,
            obligation_id=obligation.obligation_id,
            fact_id=fact.fact_id,
            status=FinalAuditStatus.INFRA,
            plan_hash=plan_hash,
            disposition=decision.disposition.value,
            reason="sources_changed",
            lease_id=lease.lease_id,
            action_id=candidate.action_spec.action_id,
            transport_status=result.transport_status,
            packet_hash=packet_ref.content_hash,
            verify_artifact_hash=verify_ref.content_hash,
            replayed=False,
            retry_reason=retry_reason,
            **_identity_fields(identity, attempt_index),
        )

    math_status = (
        MathematicalStatus.CHECKED_CORRECT
        if result.verdict == _CORRECT
        else MathematicalStatus.CHECKED_WRONG
    )
    checker = CheckerResult.PASS if result.verdict == _CORRECT else CheckerResult.FAIL
    environment_hash = sha256_hex(
        canonical_json(
            {
                "auditor_id": _AUDITOR_ID,
                "auditor_version": _AUDITOR_VERSION,
                "policy_hash": FINAL_ACCEPTANCE_POLICY_HASH,
            }
        ).encode("utf-8")
    )
    audit = FinalAuditReceipt.build(
        audit_kind=FinalAuditKind.PROOF,
        checker_result=checker,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=candidate.action_spec.action_id,
        exact_claim=contract.exact_statement,
        scope=contract.scope,
        acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        auditor_id=_AUDITOR_ID,
        auditor_version=_AUDITOR_VERSION,
        environment_hash=environment_hash,
        audit_artifact_hash=verify_ref.content_hash,
        created_at=ts,
        translation_confirmed=False,
        original_object_realized=False,
        fact_id=None if result.verdict != _CORRECT else fact.fact_id,
        evidence_ids=() if result.verdict != _CORRECT else (evidence.artifact_id,),
        kernel_artifact_hash=(
            evidence.content_hash
            if evidence.evidence_kind is EvidenceKind.KERNEL_CHECKED_PROOF
            else None
        ),
        alignment_attestation_hash=(
            None if alignment is None else alignment.attestation_id
        ),
    )
    live = view.get_obligation(obligation.obligation_id) or obligation
    acceptance = assess_final_acceptance(
        contract,
        live,
        view,
        snapshot,
        audit,
        acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        artifacts=project.artifacts,
        alignment=alignment,
    )
    if result.verdict == _WRONG:
        if acceptance.project_outcome is ProjectOutcome.REFUTED:
            raise FinalAuditError("verifier wrong must not become REFUTED")
        project.runtime.complete(
            project.project_id,
            lease.lease_id,
            usage=usage,
            artifact_refs=(verify_ref.content_hash, packet_ref.content_hash),
            mathematical_status=math_status,
            actor=_ACTOR,
            timestamp_utc=ts,
        )
        failure = _record_audit_objection(
            project,
            contract=contract,
            obligation=obligation,
            evidence=evidence,
            result=result,
            verify_hash=verify_ref.content_hash,
            ts=ts,
        )
        _reopen_rejected_obligation(
            project,
            obligation=obligation,
            evidence=evidence,
            result=result,
            verify_hash=verify_ref.content_hash,
            ts=ts,
        )
        _append_acceptance(project, acceptance, audit, ts, identity=identity)
        return _finish_run(
            project,
            obligation_id=obligation.obligation_id,
            fact_id=fact.fact_id,
            status=FinalAuditStatus.AUDIT_FAIL,
            plan_hash=plan_hash,
            disposition=decision.disposition.value,
            reason="final_audit_fail",
            lease_id=lease.lease_id,
            action_id=candidate.action_spec.action_id,
            transport_status=result.transport_status,
            verdict=result.verdict,
            audit_receipt_id=audit.receipt_id,
            acceptance_id=acceptance.record_id,
            packet_hash=packet_ref.content_hash,
            verify_artifact_hash=verify_ref.content_hash,
            outcome=None
            if acceptance.project_outcome is None
            else acceptance.project_outcome.value,
            replayed=False,
            failure_id=failure.failure_id,
            **_identity_fields(identity, attempt_index),
        )

    if acceptance.verdict is not AcceptanceVerdict.PASS:
        raise FinalAuditError(
            "correct verify did not PASS acceptance: " + ",".join(acceptance.reasons)
        )
    if acceptance.project_outcome is not ProjectOutcome.SOLVED:
        raise FinalAuditError("correct final audit must bind SOLVED")
    project.runtime.complete(
        project.project_id,
        lease.lease_id,
        usage=usage,
        artifact_refs=(verify_ref.content_hash, packet_ref.content_hash),
        mathematical_status=math_status,
        actor=_ACTOR,
        timestamp_utc=ts,
    )
    _append_acceptance(project, acceptance, audit, ts, identity=identity)
    outcome = derive_project_outcome(
        contract,
        acceptance,
        project.policy,
        project.runtime_snapshot().resources,
        view,
        project.runtime_snapshot(),
    )
    if outcome.outcome is ProjectOutcome.REFUTED:
        raise FinalAuditError("final audit must not derive REFUTED")
    seq = _max_seq(project)
    checkpoint = build_checkpoint_report(
        contract,
        outcome,
        view,
        project.runtime_snapshot(),
        seq,
        accepted_result_ids=(fact.fact_id,),
        created_at=ts,
    )
    project.artifacts.put_bytes(
        canonical_json(checkpoint.to_dict()).encode("utf-8"),
        media_type="application/json",
    )
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=_ACTOR,
            event_type=EVENT_CHECKPOINT_CREATED,
            idempotency_key=f"final-audit-checkpoint:{obligation.obligation_id}:{checkpoint.report_id}",
            payload={"checkpoint": checkpoint.to_dict()},
            timestamp_utc=ts,
            correlation_id=obligation.obligation_id,
        )
    )
    return _finish_run(
        project,
        obligation_id=obligation.obligation_id,
        fact_id=fact.fact_id,
        status=FinalAuditStatus.SOLVED,
        plan_hash=plan_hash,
        disposition=decision.disposition.value,
        reason=decision.reason.value,
        lease_id=lease.lease_id,
        action_id=candidate.action_spec.action_id,
        transport_status=result.transport_status,
        verdict=result.verdict,
        audit_receipt_id=audit.receipt_id,
        acceptance_id=acceptance.record_id,
        checkpoint_id=checkpoint.report_id,
        packet_hash=packet_ref.content_hash,
        verify_artifact_hash=verify_ref.content_hash,
        outcome=outcome.outcome.value,
        replayed=False,
        **_identity_fields(identity, attempt_index),
    )


def _require_target_truth(
    contract,
    obligation,
    view,
    snapshot,
    project=None,
    alignment: StatementAlignmentAttestation | None = None,
):
    admissions = [
        item
        for item in collect_fact_admissions(snapshot)
        if item.accepted and item.exact_claim == contract.exact_statement
    ]
    if not admissions:
        raise FinalAuditError("missing accepted fact_submit admission for the target claim")
    fact_id = admissions[0].fact_id
    if fact_id is None:
        raise FinalAuditError("accepted admission has no fact_id")
    facts = [item for item in snapshot.active_facts if item.fact_id == fact_id]
    if not facts:
        raise FinalAuditError("accepted fact_id is not an active Fact")
    fact = facts[0]
    if fact.statement != contract.exact_statement:
        raise FinalAuditError("active Fact statement does not match the target claim")
    satisfying = [
        item
        for item in view.evidence()
        if evidence_satisfies(
            item,
            contract.required_evidence_level,
            obligation.scope,
            contract.contract_id,
            exact_claim=contract.exact_statement,
        )
        and item.artifact_id in obligation.evidence_refs
    ]
    if not satisfying:
        raise FinalAuditError("DISCHARGED target has no satisfying EvidenceArtifact")
    bound_alignment = alignment
    evidence = satisfying[0]
    if kernel_acceptance_required(contract) and project is not None:
        from euler.control.kernel_service import select_kernel_bundle

        bundle = select_kernel_bundle(
            project,
            obligation.obligation_id,
            preferred_alignment=alignment,
            evidence_ids=tuple(obligation.evidence_refs),
        )
        if alignment is not None and bundle is None:
            raise FinalAuditError(
                "supplied alignment does not match a discharged kernel evidence version"
            )
        if bundle is not None:
            evidence = bundle.evidence
            bound_alignment = bundle.alignment
            if evidence.artifact_id not in {item.artifact_id for item in satisfying}:
                raise FinalAuditError("kernel bundle evidence is not on the discharged target")
    return fact, admissions[0], evidence, bound_alignment


def _build_candidate(
    contract: ProblemContract,
    obligation: Obligation,
    identity: FinalAuditIdentity,
    view,
    *,
    evidence_kind: EvidenceKind,
    requested: ResourceVector,
) -> CandidateAction:
    env_hash = identity.environment_hash
    failure_ids = tuple(
        item.failure_id
        for item in view.failures()
        if item.target_obligation_id == obligation.obligation_id
    )
    spec, request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap="independent final audit of the discharged target",
        output_contract=obligation.desired_output,
        tool_environment_hash=env_hash,
        acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        reference_versions_hash=env_hash,
        limits=ContextLimits.unlimited(),
        action_type="verify",
        mechanism=_MECHANISM,
        model_id=_AUDITOR_ID,
        effort="high",
        task_mode="final-audit",
        parameters_json=canonical_json(
            {
                "fact_id": identity.fact_id,
                "fact_digest": identity.fact_digest,
                "proof_digest": identity.proof_digest,
                "evidence_id": identity.evidence_id,
                "evidence_digest": identity.evidence_digest,
                "identity_hash": identity.identity_hash,
                "evidence_kind": evidence_kind.value,
            }
        ),
        dependency_hash=identity.identity_hash,
        definitions_hash=contract.scope.definitions_hash,
        expand_evidence_content=True,
        expand_proof_content=True,
        required_fact_ids=(identity.fact_id,),
        required_evidence_ids=(identity.evidence_id,),
        relevant_failure_ids=failure_ids,
    )
    candidate_id = stable_id(
        _CANDIDATE_DOMAIN,
        2,
        identity.identity_hash,
        FINAL_ACCEPTANCE_POLICY_HASH,
    )
    return CandidateAction(
        candidate_id=candidate_id,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        mechanism_family=_MECHANISM,
        action_kind=ActionKind.VERIFY,
        slot_kind=slot_kind_for_action(ActionKind.VERIFY),
        budget_class=BudgetClass.FINAL_AUDIT,
        requested=requested,
        priority_class=PriorityClass.FINAL_ACCEPTANCE,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )


def _active_work(runtime) -> tuple:
    from euler.control.scheduler import ActiveWork

    out = []
    for lease in runtime.leases:
        if lease.status not in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
            continue
        if not lease.obligation_id:
            continue
        out.append(
            ActiveWork(
                obligation_id=lease.obligation_id,
                mechanism_family=lease.mechanism,
                action_id=lease.action_id,
                slot_kind=lease.slot_kind,
            )
        )
    return tuple(out)


def _wall_only_usage(wall_ms: int) -> UsageRecord:
    unknown = {
        name: ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)
        for name, unit in (
            ("input_tokens", "tokens"),
            ("output_tokens", "tokens"),
            ("reasoning_tokens", "tokens"),
            ("cached_tokens", "tokens"),
            ("cost_microusd", "microusd"),
            ("tool_cpu_ms", "ms"),
            ("tool_gpu_ms", "ms"),
        )
    }
    unknown["wall_ms"] = ObservedQuantity(
        unit="ms", status=ObservationStatus.OBSERVED, value=int(wall_ms)
    )
    return UsageRecord(**unknown)


def _append_acceptance(
    project, acceptance, audit, ts: str, identity: FinalAuditIdentity | None = None
) -> None:
    payload = {
        "acceptance": acceptance.to_dict(),
        "audit": audit.to_dict(),
    }
    if identity is not None:
        payload["identity"] = identity.to_dict()
    project.artifacts.put_bytes(
        canonical_json(payload).encode("utf-8"),
        media_type="application/json",
    )
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=_ACTOR,
            event_type=EVENT_ACCEPTANCE_RECORDED,
            idempotency_key=f"final-audit-acceptance:{acceptance.obligation_id}:{acceptance.record_id}",
            payload=payload,
            timestamp_utc=ts,
            correlation_id=acceptance.obligation_id,
        )
    )


def _finish_run(project, **fields) -> FinalAuditRun:
    run = FinalAuditRun.build(**fields)
    project.artifacts.put_bytes(
        canonical_json(run.to_dict()).encode("utf-8"),
        media_type="application/json",
    )
    return run


def _load_matching_success(project, identity: FinalAuditIdentity) -> FinalAuditRun | None:
    found = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != EVENT_ACCEPTANCE_RECORDED:
            continue
        payload = dict(envelope.payload)
        raw_identity = payload.get("identity")
        if not isinstance(raw_identity, Mapping):
            continue
        stored = FinalAuditIdentity.from_dict(raw_identity)
        if stored.identity_hash != identity.identity_hash:
            continue
        acc = payload.get("acceptance")
        if not isinstance(acc, Mapping):
            continue
        acceptance = AcceptanceRecord.from_dict(acc)
        if acceptance.verdict is not AcceptanceVerdict.PASS:
            continue
        found = envelope
    if found is None:
        return None
    payload = dict(found.payload)
    acceptance = AcceptanceRecord.from_dict(payload["acceptance"])
    audit = FinalAuditReceipt.from_dict(payload["audit"])
    checkpoint_id = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type == EVENT_CHECKPOINT_CREATED:
            raw = dict(envelope.payload).get("checkpoint")
            if isinstance(raw, Mapping):
                checkpoint_id = str(raw.get("report_id"))
    lease_id = None
    for item in project.runtime_snapshot().leases:
        if (
            item.mechanism == _MECHANISM
            and item.status is LeaseStatus.COMPLETED
            and item.idempotency_key.startswith(
                f"final-audit-lease:{identity.identity_hash}:"
            )
        ):
            lease_id = item.lease_id
    return FinalAuditRun.build(
        obligation_id=identity.obligation_id,
        fact_id=identity.fact_id,
        status=FinalAuditStatus.SOLVED,
        plan_hash=sha256_hex(b"replay-acceptance"),
        disposition=ScheduleDisposition.SELECTED.value,
        reason="replay",
        lease_id=lease_id,
        action_id=audit.action_id,
        audit_receipt_id=audit.receipt_id,
        acceptance_id=acceptance.record_id,
        checkpoint_id=checkpoint_id,
        verify_artifact_hash=audit.audit_artifact_hash,
        verdict=_CORRECT,
        transport_status=_OK,
        outcome=None
        if acceptance.project_outcome is None
        else acceptance.project_outcome.value,
        replayed=True,
        **_identity_fields(identity, 1),
    )


def _load_matching_infra(project, identity: FinalAuditIdentity) -> FinalAuditRun | None:
    matches = [
        item
        for item in project.runtime_snapshot().leases
        if item.mechanism == _MECHANISM
        and item.status is LeaseStatus.INFRA_FAILED
        and item.idempotency_key.startswith(
            f"final-audit-lease:{identity.identity_hash}:"
        )
    ]
    if not matches:
        return None
    lease = matches[-1]
    artifact = lease.artifact_refs[0] if lease.artifact_refs else ""
    attempt = _attempt_from_key(lease.idempotency_key)
    return FinalAuditRun.build(
        obligation_id=identity.obligation_id,
        fact_id=identity.fact_id,
        status=FinalAuditStatus.INFRA,
        plan_hash=sha256_hex(b"replay-infra"),
        disposition=ScheduleDisposition.SELECTED.value,
        reason="infra_replay",
        lease_id=lease.lease_id,
        action_id=lease.action_id,
        transport_status="replay",
        verify_artifact_hash=artifact or None,
        replayed=True,
        **_identity_fields(identity, attempt),
    )


def _next_attempt_index(project, identity: FinalAuditIdentity) -> int:
    highest = 0
    prefix = f"final-audit-lease:{identity.identity_hash}:"
    for item in project.runtime_snapshot().leases:
        if item.mechanism != _MECHANISM:
            continue
        if not item.idempotency_key.startswith(prefix):
            continue
        highest = max(highest, _attempt_from_key(item.idempotency_key))
    return highest + 1


def _attempt_from_key(idempotency_key: str) -> int:
    marker = ":attempt:"
    if marker not in idempotency_key:
        return 1
    tail = idempotency_key.rsplit(marker, 1)[-1]
    try:
        value = int(tail)
    except ValueError:
        return 1
    return value if value >= 1 else 1


def _identity_fields(identity: FinalAuditIdentity, attempt_index: int) -> dict[str, Any]:
    return {
        "identity_hash": identity.identity_hash,
        "attempt_index": attempt_index,
        "contract_id": identity.contract_id,
        "proof_digest": identity.proof_digest,
        "fact_digest": identity.fact_digest,
        "evidence_digest": identity.evidence_digest,
        "alignment_digest": identity.alignment_digest,
    }


def _identity_from_live_sources(project, contract, env_hash: str) -> FinalAuditIdentity | None:
    try:
        from .research_service import ResearchService
        ResearchService(project, actor=_ACTOR).sync_fact_dependencies()
        view = project.research_view()
        obligation = view.get_obligation(project.manifest.target_obligation_id)
        if obligation is None or obligation.status is not ObligationStatus.DISCHARGED:
            return None
        snapshot = snapshot_project(project.project_dir)
        fact, _admission, evidence, live_alignment = _require_target_truth(
            contract, obligation, view, snapshot, project
        )
        proof = extract_proof_body(fact.raw_markdown)
        return build_final_audit_identity(
            contract=contract,
            obligation=obligation,
            fact=fact,
            proof=proof,
            evidence=evidence,
            environment_hash=env_hash,
            alignment=live_alignment,
            dependency_digest=_fact_dependency_digest(snapshot, fact),
        )
    except (ValueError, OSError):
        return None


def _fact_dependency_digest(snapshot, fact) -> str:
    active = {item.fact_id: item for item in snapshot.active_facts}
    versions = {}
    visiting = set()

    def visit(fid):
        if fid in visiting:
            raise FinalAuditError("cyclic final-proof dependency")
        if fid in versions:
            return
        item = active.get(fid)
        if item is None:
            raise FinalAuditError("missing final-proof dependency")
        visiting.add(fid)
        for predecessor in item.predecessors:
            visit(predecessor)
        visiting.remove(fid)
        versions[fid] = item.content_sha256

    visit(fact.fact_id)
    return sha256_hex(canonical_json(versions))


def _auditor_environment_hash(
    *,
    timeout_seconds: int | float | None = None,
    transport: object | None = None,
) -> str:
    seconds = configured_timeout_seconds() if timeout_seconds is None else timeout_seconds
    return verifier_environment_hash(
        policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        timeout_seconds=seconds,
        transport=transport,
    )


def _record_audit_objection(
    project,
    *,
    contract: ProblemContract,
    obligation: Obligation,
    evidence: EvidenceArtifact,
    result: VerifyResult,
    verify_hash: str,
    ts: str,
) -> FailureRecord:
    detail = result.detail or "independent final audit returned wrong"
    failure = FailureRecord.build(
        kind=FailureKind.INVALID_PROOF,
        exact_failed_claim=contract.exact_statement,
        scope=contract.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="final-audit",
        created_at=ts,
        reopen_conditions=(
            "repair the rejected proof and re-discharge with checked evidence",
        ),
        repair_conditions=(
            f"address the independent final-audit objection: {detail}",
        ),
        target_obligation_id=obligation.obligation_id,
        contract_id=contract.contract_id,
        rejected_step=detail[:500],
        evidence_kind=evidence.evidence_kind,
    )
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=_ACTOR,
            event_type="failure.recorded",
            idempotency_key=f"final-audit-objection:{obligation.obligation_id}:{verify_hash}",
            payload={"failure": failure.to_dict()},
            timestamp_utc=ts,
            correlation_id=obligation.obligation_id,
        )
    )
    return failure


def _reopen_rejected_obligation(
    project,
    *,
    obligation: Obligation,
    evidence: EvidenceArtifact,
    result: VerifyResult,
    verify_hash: str,
    ts: str,
) -> None:
    detail = result.detail or "independent final audit returned wrong"
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=_ACTOR,
            event_type="evidence.invalidated",
            idempotency_key=f"final-audit-rejected-evidence:{evidence.artifact_id}:{verify_hash}",
            payload={"artifact_id": evidence.artifact_id},
            timestamp_utc=ts,
            correlation_id=obligation.obligation_id,
        )
    )
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=_ACTOR,
            event_type="obligation.reopened",
            idempotency_key=f"final-audit-reopen:{obligation.obligation_id}:{verify_hash}",
            payload={
                "obligation_id": obligation.obligation_id,
                "reason": "fresh final audit rejected the assembled proof",
                "new_premises": f"repair requirement: {detail}",
            },
            timestamp_utc=ts,
            correlation_id=obligation.obligation_id,
        )
    )


def _assert_replay_artifacts(project, run: FinalAuditRun) -> None:
    if run.verify_artifact_hash:
        try:
            project.artifacts.verify(run.verify_artifact_hash)
            project.artifacts.get_bytes(run.verify_artifact_hash)
        except ArtifactStoreError as exc:
            raise FinalAuditError(
                f"stored final-audit artifact is missing or tampered: {exc}"
            ) from exc


def _max_seq(project) -> int:
    last = 0
    for seq, _envelope in project.event_store.iter_project(project.project_id):
        last = seq
    return last


def _run_id(
    *,
    obligation_id: str,
    fact_id: str,
    status: str,
    plan_hash: str,
    lease_id: str,
    verify_artifact_hash: str,
    acceptance_id: str,
    identity_hash: str = "",
    attempt_index: int = 1,
) -> str:
    return stable_id(
        _RUN_DOMAIN,
        2,
        obligation_id,
        fact_id,
        status,
        plan_hash,
        lease_id,
        verify_artifact_hash,
        acceptance_id,
        identity_hash,
        str(attempt_index),
    )


def _require_hex64(name: str, value: object) -> str:
    text = str(value or "")
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise FinalAuditError(f"{name} must be 64-char lowercase hex")
    return text


def _require_hex16(name: str, value: object) -> str:
    text = str(value or "")
    if len(text) != _HEX16 or any(ch not in "0123456789abcdef" for ch in text):
        raise FinalAuditError(f"{name} must be 16-char lowercase hex")
    return text


def _require_status(value: object) -> FinalAuditStatus:
    if isinstance(value, FinalAuditStatus):
        return value
    return FinalAuditStatus(str(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
