"""Independent ROOT REFUTED checkpoint path.

Stdlib plus existing control/acceptance/compute APIs. Default verify transport
is the same force-fresh HTTP classifier as proof final audit, but this module
never writes the Fact Graph. ``do_checkpoint`` selects this path when the root
is already REFUTED with checked counterexample evidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from euler.control.acceptance import (
    AcceptanceRecord,
    AcceptanceVerdict,
    FinalAuditKind,
    FinalAuditReceipt,
    ProjectOutcome,
    assess_final_acceptance,
    build_checkpoint_report,
    derive_project_outcome,
)
from euler.control.adapters.project import snapshot_project
from euler.control.compute import ComputeRunResult, ComputeTermination
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    ProblemContract,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.failures import ConfidenceClass, FailureKind, FailureRecord
from euler.control.audit_resources import (
    AuditResourceError,
    configured_timeout_seconds,
    is_live_http_transport,
    resolve_audit_reservation,
    timeout_ms_from_seconds,
    verifier_environment_hash,
)
from euler.control.final_audit import (
    FINAL_ACCEPTANCE_POLICY_HASH,
    HttpVerifyTransport,
    VerifyResult,
    VerifyTransport,
)
from euler.control.ids import canonical_json, sha256_hex, stable_id
from euler.control.live_records import EVENT_CHECKPOINT_CREATED
from euler.control.local_actions import (
    LocalActionError,
    owner_is_alive,
    run_local_action,
)
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetClass, ResourceVector
from euler.control.project import EulerProject
from euler.control.research_service import ResearchService, ResearchServiceError
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import ActionKind, PriorityClass
from euler.control.schema import EventEnvelope

_ACTOR = "refutation-audit"
_AUDITOR_ID = "euler-refutation-audit"
_AUDITOR_VERSION = "1"
_MECHANISM = "refutation-audit"
_EVENT_RECORDED = "euler.refutation_audit_recorded"
_OK = "ok"
_CORRECT = "correct"
_WRONG = "wrong"
_IDENTITY_DOMAIN = "refutation-audit-identity"
_RUN_DOMAIN = "refutation-audit-run"

REFUTATION_AUDIT_RETRY_SPEC = {
    "parameter": "retry_infra",
    "reason_parameter": "retry_reason",
    "cli_flags": ["--retry-infra", "--retry-reason TEXT"],
    "mcp_fields": ["retry_infra", "retry_reason"],
    "applies_when": "RefutationAuditStatus.INFRA",
    "requires_nonempty_reason": True,
    "effect": (
        "issue a fresh FINAL_AUDIT attempt and reservation bound to the current "
        "witness identity; a successful completed REFUTED audit of the same "
        "identity is replayed without a second verify call"
    ),
    "identity_binds": (
        "obligation_id",
        "contract_id",
        "evidence_id",
        "evidence_digest",
        "candidate_digest",
        "report_digest",
        "review_digest",
        "acceptance_policy_hash",
        "environment_hash",
        "verifier_endpoint_identity",
        "timeout_seconds",
        "protocol",
    ),
}

__all__ = [
    "REFUTATION_AUDIT_RETRY_SPEC",
    "RefutationAuditError",
    "RefutationAuditStatus",
    "RefutationAuditIdentity",
    "RefutationAuditRun",
    "run_refutation_audit",
]


class RefutationAuditError(ValueError):
    """Root refutation preconditions failed or a stored audit artifact is unusable."""


class RefutationAuditStatus(str, Enum):
    REFUTED = "REFUTED"
    DEFERRED = "DEFERRED"
    AUDIT_FAIL = "AUDIT_FAIL"
    INFRA = "INFRA"


@dataclass(frozen=True, slots=True, kw_only=True)
class RefutationAuditIdentity:
    """Version-bound witness identity. Contains no Fact ID."""

    obligation_id: str
    contract_id: str
    evidence_id: str
    evidence_digest: str
    candidate_digest: str
    report_digest: str
    review_digest: str
    policy_hash: str
    environment_hash: str
    identity_hash: str
    compute_request_id: str
    scope_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "evidence_id": self.evidence_id,
            "evidence_digest": self.evidence_digest,
            "candidate_digest": self.candidate_digest,
            "report_digest": self.report_digest,
            "review_digest": self.review_digest,
            "policy_hash": self.policy_hash,
            "environment_hash": self.environment_hash,
            "identity_hash": self.identity_hash,
            "compute_request_id": self.compute_request_id,
            "scope_digest": self.scope_digest,
        }

    @classmethod
    def from_dict(cls, data: object) -> "RefutationAuditIdentity":
        if not isinstance(data, Mapping):
            raise RefutationAuditError("RefutationAuditIdentity must be a mapping")
        payload = dict(data)
        return cls(**{key: str(payload[key]) for key in (
            "obligation_id", "contract_id", "evidence_id", "evidence_digest",
            "candidate_digest", "report_digest", "review_digest", "policy_hash",
            "environment_hash", "identity_hash", "compute_request_id", "scope_digest",
        )})


@dataclass(frozen=True, slots=True, kw_only=True)
class RefutationAuditRun:
    """Typed result of ``run_refutation_audit``. No fabricated Fact ID."""

    run_id: str
    obligation_id: str
    contract_id: str
    status: RefutationAuditStatus
    replayed: bool
    reason: str
    identity_hash: str
    attempt_index: int = 1
    lease_id: Optional[str] = None
    action_id: Optional[str] = None
    transport_status: Optional[str] = None
    verdict: Optional[str] = None
    audit_receipt_id: Optional[str] = None
    acceptance_id: Optional[str] = None
    checkpoint_id: Optional[str] = None
    verify_artifact_hash: Optional[str] = None
    evidence_id: Optional[str] = None
    outcome: Optional[str] = None
    failure_id: Optional[str] = None
    retry_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RefutationAuditStatus):
            object.__setattr__(self, "status", RefutationAuditStatus(self.status))
        if not isinstance(self.replayed, bool):
            raise RefutationAuditError("replayed must be bool")
        if not isinstance(self.attempt_index, int) or isinstance(self.attempt_index, bool):
            raise RefutationAuditError("attempt_index must be int")
        if self.attempt_index < 1:
            raise RefutationAuditError("attempt_index must be >= 1")
        expected = _run_id_fields(
            obligation_id=self.obligation_id,
            contract_id=self.contract_id,
            status=self.status,
            identity_hash=self.identity_hash,
            attempt_index=self.attempt_index,
            lease_id=self.lease_id,
            verify_artifact_hash=self.verify_artifact_hash,
            acceptance_id=self.acceptance_id,
            checkpoint_id=self.checkpoint_id,
        )
        if self.run_id != expected:
            raise RefutationAuditError("run_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "obligation_id": self.obligation_id,
            "contract_id": self.contract_id,
            "status": self.status.value,
            "replayed": self.replayed,
            "reason": self.reason,
            "identity_hash": self.identity_hash,
            "attempt_index": self.attempt_index,
            "lease_id": self.lease_id,
            "action_id": self.action_id,
            "transport_status": self.transport_status,
            "verdict": self.verdict,
            "audit_receipt_id": self.audit_receipt_id,
            "acceptance_id": self.acceptance_id,
            "checkpoint_id": self.checkpoint_id,
            "verify_artifact_hash": self.verify_artifact_hash,
            "evidence_id": self.evidence_id,
            "outcome": self.outcome,
            "failure_id": self.failure_id,
            "retry_reason": self.retry_reason,
            "fact_id": None,
        }

    @classmethod
    def from_dict(cls, data: object) -> "RefutationAuditRun":
        if not isinstance(data, Mapping):
            raise RefutationAuditError("RefutationAuditRun must be a mapping")
        payload = dict(data)
        payload.pop("fact_id", None)
        return cls(
            run_id=str(payload["run_id"]),
            obligation_id=str(payload["obligation_id"]),
            contract_id=str(payload["contract_id"]),
            status=RefutationAuditStatus(payload["status"]),
            replayed=bool(payload["replayed"]),
            reason=str(payload["reason"]),
            identity_hash=str(payload["identity_hash"]),
            attempt_index=int(payload.get("attempt_index") or 1),
            lease_id=payload.get("lease_id"),
            action_id=payload.get("action_id"),
            transport_status=payload.get("transport_status"),
            verdict=payload.get("verdict"),
            audit_receipt_id=payload.get("audit_receipt_id"),
            acceptance_id=payload.get("acceptance_id"),
            checkpoint_id=payload.get("checkpoint_id"),
            verify_artifact_hash=payload.get("verify_artifact_hash"),
            evidence_id=payload.get("evidence_id"),
            outcome=payload.get("outcome"),
            failure_id=payload.get("failure_id"),
            retry_reason=payload.get("retry_reason"),
        )

    @classmethod
    def build(cls, **fields: Any) -> "RefutationAuditRun":
        status = fields["status"]
        if not isinstance(status, RefutationAuditStatus):
            status = RefutationAuditStatus(status)
        run_id = _run_id_fields(
            obligation_id=str(fields["obligation_id"]),
            contract_id=str(fields["contract_id"]),
            status=status,
            identity_hash=str(fields["identity_hash"]),
            attempt_index=int(fields.get("attempt_index") or 1),
            lease_id=fields.get("lease_id"),
            verify_artifact_hash=fields.get("verify_artifact_hash"),
            acceptance_id=fields.get("acceptance_id"),
            checkpoint_id=fields.get("checkpoint_id"),
        )
        return cls(run_id=run_id, status=status, **{
            key: value for key, value in fields.items() if key not in {"status", "run_id"}
        })


def run_refutation_audit(
    project: EulerProject,
    *,
    transport: VerifyTransport | None = None,
    retry_infra: bool = False,
    retry_reason: str = "",
    timestamp_utc: str | None = None,
    requested: ResourceVector | None = None,
    timeout_ms: int | None = None,
) -> RefutationAuditRun:
    """Force-fresh independent audit of a ROOT checked counterexample.

    ``verdict=correct`` means the stored witness is a valid counterexample of
    the original target under its scope. It does not prove the original
    universal statement. Realization review remains the stored original-object
    artifact. Never writes the Fact Graph. ``timeout_ms`` is enforced on the
    default HttpVerifyTransport. Scheduler admission is not provider-side
    hard billing enforcement.
    """
    if not isinstance(project, EulerProject):
        raise RefutationAuditError("project must be an EulerProject")
    if not isinstance(retry_infra, bool):
        raise RefutationAuditError("retry_infra must be bool")
    reason_text = str(retry_reason or "").strip()
    if retry_infra and not reason_text:
        raise RefutationAuditError("retry_infra requires a nonempty retry_reason")
    if not retry_infra:
        reason_text = ""
    if timeout_ms is None:
        timeout_ms = timeout_ms_from_seconds(configured_timeout_seconds())
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise RefutationAuditError("timeout_ms must be a positive int")
    ts = timestamp_utc or _utc_now()
    ResearchService(project, actor=_ACTOR).sync_fact_dependencies(timestamp_utc=ts)
    live_http = is_live_http_transport(transport)
    if transport is None:
        timeout_seconds = timeout_ms / 1000.0
        bus = HttpVerifyTransport(timeout_seconds=timeout_seconds)
    else:
        bus = transport
        timeout_seconds = (
            bus.timeout_seconds if isinstance(bus, HttpVerifyTransport)
            else timeout_ms / 1000.0
        )
    env_hash = verifier_environment_hash(
        policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    try:
        bundle = _require_root_witness(project, environment_hash=env_hash)
    except RefutationAuditError as prep_error:
        blocked = _load_matching_infra_for_root(
            project, project.manifest.target_obligation_id, env_hash,
        )
        if blocked is not None and not retry_infra:
            _assert_replay_artifacts(project, blocked)
            return RefutationAuditRun.from_dict({**blocked.to_dict(), "replayed": True})
        raise prep_error
    identity = bundle["identity"]
    existing = _load_matching_success(project, identity)
    if existing is not None:
        _assert_replay_artifacts(project, existing)
        return RefutationAuditRun.from_dict({**existing.to_dict(), "replayed": True})
    pending = _load_pending_acceptance(project, identity)
    if pending is not None and _identity_lease_completed(project, identity):
        return _seal_checkpoint(project, pending, identity, ts, replayed=True)
    _reject_duplicate_running_audit(project, identity)
    if not retry_infra:
        blocked = _load_matching_infra(project, identity)
        if blocked is not None:
            _assert_replay_artifacts(project, blocked)
            return RefutationAuditRun.from_dict({**blocked.to_dict(), "replayed": True})
    try:
        allowance = resolve_audit_reservation(
            policy=project.policy,
            requested=requested,
            timeout_ms=timeout_ms_from_seconds(timeout_seconds) if live_http else timeout_ms,
            live_http=live_http,
        )
    except AuditResourceError as exc:
        raise RefutationAuditError(str(exc)) from exc
    attempt_index = _next_attempt_index(project, identity)
    request_id = f"refutation-audit:{identity.identity_hash}:{attempt_index}"
    parameters = {
        "identity": identity.to_dict(),
        "policy_hash": FINAL_ACCEPTANCE_POLICY_HASH,
        "attempt_index": attempt_index,
        "retry_infra": retry_infra,
    }

    def execute() -> tuple[dict[str, Any], tuple[str, ...]]:
        lease = _running_lease(project, identity.obligation_id)
        try:
            live = _require_root_witness(project, environment_hash=env_hash)
        except RefutationAuditError:
            result = _run_payload(
                identity, attempt_index, RefutationAuditStatus.INFRA,
                reason="sources_changed", action_id=lease.action_id, lease_id=lease.lease_id,
                retry_reason=reason_text,
            )
            _record_run(project, result, identity, ts)
            return {**result, "status": "INFRA"}, ()
        if live["identity"].identity_hash != identity.identity_hash:
            result = _run_payload(
                identity, attempt_index, RefutationAuditStatus.INFRA,
                reason="sources_changed", action_id=lease.action_id, lease_id=lease.lease_id,
                retry_reason=reason_text,
            )
            _record_run(project, result, identity, ts)
            return {**result, "status": "INFRA"}, ()
        outcome = _execute_verify(
            project, bundle=live, identity=identity, bus=bus, ts=ts,
            attempt_index=attempt_index, action_id=lease.action_id,
            lease_id=lease.lease_id, retry_reason=reason_text,
            environment_hash=env_hash,
        )
        refs = tuple(item for item in (
            outcome.get("verify_artifact_hash"),
            identity.candidate_digest,
            identity.report_digest,
            identity.review_digest,
        ) if item)
        return outcome, refs

    try:
        raw = run_local_action(
            project,
            obligation_id=identity.obligation_id,
            request_id=request_id,
            mechanism=_MECHANISM,
            parameters=parameters,
            timeout_ms=timeout_ms,
            actor=_ACTOR,
            execute=execute,
            kind=ActionKind.VERIFY,
            budget_class=BudgetClass.FINAL_AUDIT,
            requested=allowance,
            priority_class=PriorityClass.FINAL_ACCEPTANCE,
        )
    except LocalActionError as exc:
        raise RefutationAuditError(str(exc)) from exc
    if raw.get("status") == "DEFERRED":
        return RefutationAuditRun.build(
            obligation_id=identity.obligation_id,
            contract_id=identity.contract_id,
            status=RefutationAuditStatus.DEFERRED,
            replayed=False,
            reason=str(raw.get("reason") or "deferred"),
            identity_hash=identity.identity_hash,
            attempt_index=attempt_index,
            evidence_id=identity.evidence_id,
        )
    stored = dict(raw.get("run") if isinstance(raw.get("run"), Mapping) else raw)
    stored["replayed"] = bool(raw.get("replayed"))
    if not stored.get("lease_id") and raw.get("lease_id"):
        stored["lease_id"] = raw["lease_id"]
        stored["run_id"] = _run_id_fields(
            obligation_id=str(stored["obligation_id"]),
            contract_id=str(stored["contract_id"]),
            status=RefutationAuditStatus(stored["status"]),
            identity_hash=str(stored["identity_hash"]),
            attempt_index=int(stored.get("attempt_index") or 1),
            lease_id=stored.get("lease_id"),
            verify_artifact_hash=stored.get("verify_artifact_hash"),
            acceptance_id=stored.get("acceptance_id"),
            checkpoint_id=stored.get("checkpoint_id"),
        )
    run = RefutationAuditRun.from_dict(stored)
    if run.status is RefutationAuditStatus.INFRA:
        _assert_replay_artifacts(project, run)
    if run.status is RefutationAuditStatus.REFUTED and not run.checkpoint_id:
        return _seal_checkpoint(project, run.to_dict(), identity, ts, replayed=bool(raw.get("replayed")))
    if raw.get("replayed") and run.status is RefutationAuditStatus.REFUTED:
        _assert_replay_artifacts(project, run)
    return run


def _require_root_witness(project: EulerProject, *, environment_hash: str) -> dict[str, Any]:
    view = project.research_view()
    contracts = view.contracts()
    if not contracts:
        raise RefutationAuditError("project has no ProblemContract")
    contract = next(
        (item for item in contracts if item.contract_id == project.manifest.contract_id),
        contracts[0],
    )
    root_id = project.manifest.target_obligation_id
    obligation = view.get_obligation(root_id)
    if obligation is None:
        raise RefutationAuditError("target obligation missing from ResearchView")
    if obligation.obligation_id != root_id:
        raise RefutationAuditError("refutation audit requires the ROOT obligation")
    if obligation.status is not ObligationStatus.REFUTED:
        raise RefutationAuditError("target obligation is not REFUTED")
    if obligation.validity is not ValidityStatus.VALID:
        raise RefutationAuditError("REFUTED root evidence is stale or invalid")
    if obligation.contract_id != contract.contract_id:
        raise RefutationAuditError("root contract_id mismatch")
    if obligation.exact_statement != contract.exact_statement:
        raise RefutationAuditError("root exact claim does not match the contract")
    if obligation.scope != contract.scope:
        raise RefutationAuditError("root scope does not match the contract")
    evidence = _root_counterexample(view, contract, obligation)
    candidate_hash, report_hash, review_hash, compute_request_id, review = _require_artifacts(
        project, obligation, evidence,
    )
    scope_digest = sha256_hex(canonical_json(obligation.scope.to_dict()).encode("utf-8"))
    identity_hash = stable_id(
        _IDENTITY_DOMAIN, 1,
        obligation.obligation_id, contract.contract_id, evidence.artifact_id,
        evidence.content_hash, candidate_hash, report_hash, review_hash,
        FINAL_ACCEPTANCE_POLICY_HASH, environment_hash, scope_digest,
    )
    identity = RefutationAuditIdentity(
        obligation_id=obligation.obligation_id,
        contract_id=contract.contract_id,
        evidence_id=evidence.artifact_id,
        evidence_digest=evidence.content_hash,
        candidate_digest=candidate_hash,
        report_digest=report_hash,
        review_digest=review_hash,
        policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        environment_hash=environment_hash,
        identity_hash=identity_hash,
        compute_request_id=compute_request_id,
        scope_digest=scope_digest,
    )
    return {
        "contract": contract,
        "obligation": obligation,
        "evidence": evidence,
        "identity": identity,
        "review": review,
        "candidate_bytes": project.artifacts.get_bytes(candidate_hash),
        "report_bytes": project.artifacts.get_bytes(report_hash),
        "review_bytes": project.artifacts.get_bytes(review_hash),
    }


def _root_counterexample(view, contract: ProblemContract, obligation: Obligation) -> EvidenceArtifact:
    matching = []
    for artifact_id in obligation.evidence_refs:
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            continue
        if artifact.validity is not ValidityStatus.VALID:
            continue
        if evidence_satisfies(
            artifact,
            EvidenceKind.CHECKED_COUNTEREXAMPLE,
            obligation.scope,
            contract.contract_id,
            exact_claim=contract.exact_statement,
        ):
            matching.append(artifact)
    if not matching:
        raise RefutationAuditError(
            "REFUTED root has no VALID exact-scoped checked counterexample evidence"
        )
    return matching[0]


def _require_artifacts(
    project: EulerProject, obligation: Obligation, evidence: EvidenceArtifact,
) -> tuple[str, str, str, str, dict[str, Any]]:
    reviews = [
        dict(event.payload)
        for _, event in project.event_store.iter_project(project.project_id)
        if event.event_type == "euler.compute_realization_reviewed"
        and dict(event.payload).get("obligation_id") == obligation.obligation_id
        and dict(event.payload).get("evidence_id") == evidence.artifact_id
    ]
    if not reviews:
        raise RefutationAuditError("missing stored realization review for the root witness")
    review_event = reviews[-1]
    request_id = str(review_event.get("request_id") or "")
    review_hash = str(review_event.get("review_hash") or "")
    if not request_id or not review_hash:
        raise RefutationAuditError("realization review is missing request or artifact hash")
    runs = [
        dict(event.payload)
        for _, event in project.event_store.iter_project(project.project_id)
        if event.event_type == "euler.compute_ran"
        and dict(event.payload).get("request_id") == request_id
        and dict(event.payload).get("obligation_id") == obligation.obligation_id
    ]
    if not runs or not isinstance(runs[-1].get("compute_result"), Mapping):
        raise RefutationAuditError("missing stored compute candidate/checker artifacts")
    try:
        compute_result = ComputeRunResult.from_dict(runs[-1]["compute_result"])
    except Exception as exc:
        raise RefutationAuditError(f"stored compute result is unusable: {exc}") from exc
    if compute_result.termination is not ComputeTermination.CHECKED_COUNTEREXAMPLE:
        raise RefutationAuditError("stored compute run is not a checked counterexample")
    if compute_result.candidate_ref is None or compute_result.checker_record is None:
        raise RefutationAuditError("stored compute run is missing candidate or checker")
    if compute_result.checker_record.checker_result is not CheckerResult.PASS:
        raise RefutationAuditError("stored checker record is not PASS")
    candidate_hash = compute_result.candidate_ref.content_hash
    report_hash = compute_result.report_ref.content_hash
    if evidence.content_hash != candidate_hash:
        raise RefutationAuditError("checked evidence is not bound to the stored candidate")
    if compute_result.evidence is not None:
        if compute_result.evidence.tested_claim != obligation.exact_statement:
            raise RefutationAuditError("stored compute claim does not match the root")
        if compute_result.evidence.scope != obligation.scope:
            raise RefutationAuditError("stored compute scope does not match the root")
        if compute_result.evidence.obligation_id != obligation.obligation_id:
            raise RefutationAuditError("stored compute result is not bound to the root")
    try:
        project.artifacts.verify(candidate_hash)
        project.artifacts.verify(report_hash)
        project.artifacts.verify(review_hash)
        review_bytes = project.artifacts.get_bytes(review_hash)
    except Exception as exc:
        raise RefutationAuditError(f"witness artifacts missing or corrupt: {exc}") from exc
    try:
        review = json.loads(review_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RefutationAuditError("realization review artifact is not JSON") from exc
    if not isinstance(review, dict):
        raise RefutationAuditError("realization review artifact is not an object")
    if review.get("translation_confirmed") is not True or review.get("original_object_realized") is not True:
        raise RefutationAuditError("realization review is not a confirmed original-object record")
    scope_digest = sha256_hex(canonical_json(obligation.scope.to_dict()).encode("utf-8"))
    if str(review.get("scope_hash") or "") != scope_digest:
        raise RefutationAuditError("realization review scope does not match the root")
    if str(review.get("candidate_content_hash") or "") != candidate_hash:
        raise RefutationAuditError("realization review is not bound to the candidate artifact")
    if str(review.get("report_content_hash") or "") != report_hash:
        raise RefutationAuditError("realization review is not bound to the checker report")
    return candidate_hash, report_hash, review_hash, request_id, review


def _execute_verify(
    project: EulerProject, *, bundle: dict[str, Any], identity: RefutationAuditIdentity,
    bus: VerifyTransport, ts: str, attempt_index: int, action_id: str, lease_id: str,
    retry_reason: str, environment_hash: str,
) -> dict[str, Any]:
    contract: ProblemContract = bundle["contract"]
    obligation: Obligation = bundle["obligation"]
    evidence: EvidenceArtifact = bundle["evidence"]
    statement = _refutation_statement(contract, obligation, bundle["candidate_bytes"])
    proof = _refutation_proof(bundle, identity)
    result = bus.verify(statement, proof)
    verify_ref = project.artifacts.put_bytes(
        canonical_json(result.to_dict()).encode("utf-8"), media_type="application/json",
    )
    if result.transport_status != _OK or result.verdict not in {_CORRECT, _WRONG}:
        payload = _run_payload(
            identity, attempt_index, RefutationAuditStatus.INFRA,
            reason=result.transport_status, action_id=action_id, lease_id=lease_id,
            transport_status=result.transport_status,
            verify_artifact_hash=verify_ref.content_hash, retry_reason=retry_reason,
        )
        _record_run(project, payload, identity, ts)
        return {**payload, "status": "INFRA"}
    try:
        live = _require_root_witness(project, environment_hash=environment_hash)
    except RefutationAuditError:
        payload = _run_payload(
            identity, attempt_index, RefutationAuditStatus.INFRA,
            reason="sources_changed", action_id=action_id, lease_id=lease_id,
            transport_status=result.transport_status,
            verify_artifact_hash=verify_ref.content_hash, retry_reason=retry_reason,
        )
        _record_run(project, payload, identity, ts)
        return {**payload, "status": "INFRA"}
    if live["identity"].identity_hash != identity.identity_hash:
        payload = _run_payload(
            identity, attempt_index, RefutationAuditStatus.INFRA,
            reason="sources_changed", action_id=action_id, lease_id=lease_id,
            transport_status=result.transport_status,
            verify_artifact_hash=verify_ref.content_hash, retry_reason=retry_reason,
        )
        _record_run(project, payload, identity, ts)
        return {**payload, "status": "INFRA"}
    checker = CheckerResult.PASS if result.verdict == _CORRECT else CheckerResult.FAIL
    audit = FinalAuditReceipt.build(
        audit_kind=FinalAuditKind.COUNTEREXAMPLE,
        checker_result=checker,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=action_id,
        exact_claim=contract.exact_statement,
        scope=contract.scope,
        acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        auditor_id=_AUDITOR_ID,
        auditor_version=_AUDITOR_VERSION,
        environment_hash=identity.environment_hash,
        audit_artifact_hash=verify_ref.content_hash,
        created_at=ts,
        translation_confirmed=True,
        original_object_realized=True,
        evidence_ids=(evidence.artifact_id,) if result.verdict == _CORRECT else (),
        candidate_content_hash=identity.candidate_digest,
        report_content_hash=identity.report_digest,
        alignment_attestation_hash=identity.review_digest,
    )
    snapshot = snapshot_project(project.project_dir)
    view = project.research_view()
    live_ob = view.get_obligation(obligation.obligation_id) or obligation
    acceptance = assess_final_acceptance(
        contract, live_ob, view, snapshot, audit,
        acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        artifacts=project.artifacts,
    )
    if result.verdict == _WRONG:
        if acceptance.project_outcome is ProjectOutcome.SOLVED:
            raise RefutationAuditError("rejected counterexample audit must not mark the original proved")
        if acceptance.project_outcome is ProjectOutcome.REFUTED:
            raise RefutationAuditError("verifier wrong must not become REFUTED")
        failure = _record_objection(project, contract, obligation, evidence, result, verify_ref.content_hash, ts)
        _reopen_root(project, obligation, evidence, result, verify_ref.content_hash, ts)
        _append_acceptance(project, acceptance, audit, identity, ts)
        payload = _run_payload(
            identity, attempt_index, RefutationAuditStatus.AUDIT_FAIL,
            reason="final_audit_fail", action_id=action_id, lease_id=lease_id,
            transport_status=result.transport_status, verdict=result.verdict,
            audit_receipt_id=audit.receipt_id, acceptance_id=acceptance.record_id,
            verify_artifact_hash=verify_ref.content_hash, failure_id=failure.failure_id,
            outcome=None if acceptance.project_outcome is None else acceptance.project_outcome.value,
        )
        _record_run(project, payload, identity, ts)
        return {**payload, "status": "AUDIT_FAIL"}
    if acceptance.verdict is not AcceptanceVerdict.PASS:
        raise RefutationAuditError(
            "correct counterexample verify did not PASS acceptance: " + ",".join(acceptance.reasons)
        )
    if acceptance.project_outcome is not ProjectOutcome.REFUTED:
        raise RefutationAuditError("correct refutation audit must bind REFUTED")
    if acceptance.project_outcome is ProjectOutcome.SOLVED:
        raise RefutationAuditError("refutation audit must not bind SOLVED")
    _append_acceptance(project, acceptance, audit, identity, ts)
    payload = _run_payload(
        identity, attempt_index, RefutationAuditStatus.REFUTED,
        reason="final_acceptance_pass", action_id=action_id, lease_id=lease_id,
        transport_status=result.transport_status, verdict=result.verdict,
        audit_receipt_id=audit.receipt_id, acceptance_id=acceptance.record_id,
        verify_artifact_hash=verify_ref.content_hash,
        outcome=acceptance.project_outcome.value,
    )
    return {**payload, "status": "REFUTED"}


def _refutation_statement(contract: ProblemContract, obligation: Obligation, candidate: bytes) -> str:
    try:
        witness = candidate.decode("utf-8")
    except UnicodeDecodeError:
        witness = candidate.hex()
    return (
        "Independent counterexample audit. A verdict of correct means the stored "
        "witness is a valid counterexample of the original target under its recorded "
        "scope; it is not a proof of the original universal statement.\n"
        f"Original target: {contract.exact_statement}\n"
        f"Root obligation: {obligation.exact_statement}\n"
        f"Scope digest: {sha256_hex(canonical_json(obligation.scope.to_dict()).encode('utf-8'))}\n"
        f"Witness assignment: {witness}"
    )


def _refutation_proof(bundle: Mapping[str, Any], identity: RefutationAuditIdentity) -> str:
    def _text(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()
    return (
        f"candidate_hash: {identity.candidate_digest}\n"
        f"report_hash: {identity.report_digest}\n"
        f"realization_review_hash: {identity.review_digest}\n"
        f"evidence_id: {identity.evidence_id}\n"
        f"Checker report:\n{_text(bundle['report_bytes'])}\n"
        f"Realization review (original-object bound):\n{_text(bundle['review_bytes'])}\n"
    )


def _running_lease(project: EulerProject, obligation_id: str):
    for lease in project.runtime_snapshot().leases:
        if lease.obligation_id == obligation_id and lease.status is LeaseStatus.RUNNING:
            if lease.mechanism == _MECHANISM:
                return lease
    raise RefutationAuditError("no running refutation-audit lease")


def _record_objection(project, contract, obligation, evidence, result: VerifyResult, verify_hash: str, ts: str) -> FailureRecord:
    detail = result.detail or "independent refutation audit returned wrong"
    failure = FailureRecord.build(
        kind=FailureKind.INVALID_PROOF,
        exact_failed_claim=_refutation_statement(contract, obligation, b""),
        scope=obligation.scope,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="refutation-audit",
        created_at=ts,
        reopen_conditions=(
            "repair the rejected counterexample witness and re-refute with checked evidence",
        ),
        repair_conditions=(
            f"address the independent refutation-audit objection: {detail}",
        ),
        target_obligation_id=obligation.obligation_id,
        contract_id=contract.contract_id,
        rejected_step=detail[:500],
        evidence_kind=evidence.evidence_kind,
    )
    project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor=_ACTOR, event_type="failure.recorded",
        idempotency_key=f"refutation-audit-objection:{obligation.obligation_id}:{verify_hash}",
        payload={"failure": failure.to_dict()}, timestamp_utc=ts,
        correlation_id=obligation.obligation_id,
    ))
    return failure


def _reopen_root(project, obligation, evidence, result: VerifyResult, verify_hash: str, ts: str) -> None:
    detail = result.detail or "independent refutation audit returned wrong"
    project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor=_ACTOR, event_type="evidence.invalidated",
        idempotency_key=f"refutation-audit-rejected-evidence:{evidence.artifact_id}:{verify_hash}",
        payload={"artifact_id": evidence.artifact_id}, timestamp_utc=ts,
        correlation_id=obligation.obligation_id,
    ))
    try:
        ResearchService(project, actor=_ACTOR).apply(
            [{
                "event_type": "obligation.reopened",
                "payload": {
                    "obligation_id": obligation.obligation_id,
                    "reason": "fresh refutation audit rejected the stored counterexample",
                    "new_premises": f"repair requirement: {detail}",
                },
                "idempotency_key": f"refutation-audit-reopen:{obligation.obligation_id}:{verify_hash}",
            }],
            timestamp_utc=ts,
        )
    except ResearchServiceError as exc:
        raise RefutationAuditError(str(exc)) from exc


def _append_acceptance(project, acceptance, audit, identity, ts: str) -> None:
    payload = {
        "acceptance": acceptance.to_dict(),
        "audit": audit.to_dict(),
        "refutation_identity": identity.to_dict(),
    }
    project.artifacts.put_bytes(canonical_json(payload).encode("utf-8"), media_type="application/json")
    project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor=_ACTOR, event_type=_EVENT_RECORDED,
        idempotency_key=f"refutation-audit-acceptance:{acceptance.obligation_id}:{acceptance.record_id}",
        payload=payload, timestamp_utc=ts, correlation_id=acceptance.obligation_id,
    ))


def _record_run(project, payload: Mapping[str, Any], identity: RefutationAuditIdentity, ts: str) -> None:
    run = RefutationAuditRun.from_dict(payload)
    project.artifacts.put_bytes(canonical_json(run.to_dict()).encode("utf-8"), media_type="application/json")
    project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor=_ACTOR, event_type=_EVENT_RECORDED,
        idempotency_key=f"refutation-audit-run:{identity.identity_hash}:{run.status.value}:{run.attempt_index}",
        payload={"run": run.to_dict(), "refutation_identity": identity.to_dict()},
        timestamp_utc=ts, correlation_id=identity.obligation_id,
    ))


def _load_matching_success(project, identity: RefutationAuditIdentity) -> RefutationAuditRun | None:
    found = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != _EVENT_RECORDED:
            continue
        payload = dict(envelope.payload)
        stored_id = payload.get("refutation_identity")
        if not isinstance(stored_id, Mapping):
            continue
        if str(stored_id.get("identity_hash") or "") != identity.identity_hash:
            continue
        raw_run = payload.get("run")
        if (
            isinstance(raw_run, Mapping)
            and raw_run.get("status") == RefutationAuditStatus.REFUTED.value
            and raw_run.get("checkpoint_id")
        ):
            found = RefutationAuditRun.from_dict(raw_run)
            continue
    return found


def _load_pending_acceptance(project, identity: RefutationAuditIdentity) -> dict[str, Any] | None:
    found = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != _EVENT_RECORDED:
            continue
        payload = dict(envelope.payload)
        stored_id = payload.get("refutation_identity")
        acc = payload.get("acceptance")
        if not isinstance(stored_id, Mapping) or not isinstance(acc, Mapping):
            continue
        if str(stored_id.get("identity_hash") or "") != identity.identity_hash:
            continue
        if acc.get("verdict") == AcceptanceVerdict.PASS.value and acc.get("project_outcome") == ProjectOutcome.REFUTED.value:
            found = {
                "acceptance_id": acc.get("record_id"),
                "audit_receipt_id": (payload.get("audit") or {}).get("receipt_id"),
                "verify_artifact_hash": (payload.get("audit") or {}).get("audit_artifact_hash"),
                "evidence_id": identity.evidence_id,
                "action_id": (payload.get("audit") or {}).get("action_id"),
            }
    if found is None:
        return None
    lease = _completed_identity_lease(project, identity)
    if lease is not None:
        found["lease_id"] = lease.lease_id
        found["action_id"] = found.get("action_id") or lease.action_id
    return found


def _completed_identity_lease(project, identity: RefutationAuditIdentity):
    marker = f"refutation-audit:{identity.identity_hash}:"
    found = None
    for item in project.runtime_snapshot().leases:
        if item.mechanism != _MECHANISM or item.status is not LeaseStatus.COMPLETED:
            continue
        if marker in (item.idempotency_key or ""):
            found = item
    return found


def _identity_lease_completed(project, identity: RefutationAuditIdentity) -> bool:
    return _completed_identity_lease(project, identity) is not None


def _reject_duplicate_running_audit(project, identity: RefutationAuditIdentity) -> None:
    running = [
        item for item in project.runtime_snapshot().leases
        if item.mechanism == _MECHANISM
        and item.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}
        and item.obligation_id == identity.obligation_id
    ]
    if not running:
        return
    prefix = f"refutation-audit:{identity.identity_hash}:"
    for _seq, event in project.event_store.iter_project(project.project_id):
        if event.event_type != "euler.local_action_selected":
            continue
        payload = dict(event.payload)
        request_id = str(payload.get("request_id") or "")
        if not request_id.startswith(prefix):
            continue
        if owner_is_alive(payload.get("owner")):
            raise RefutationAuditError(
                "refutation audit still running in owner process; do not duplicate the verifier"
            )


def _seal_checkpoint(
    project: EulerProject,
    pending: Mapping[str, Any],
    identity: RefutationAuditIdentity,
    ts: str,
    *,
    replayed: bool,
) -> RefutationAuditRun:
    view = project.research_view()
    contract = view.get_contract(identity.contract_id)
    obligation = view.get_obligation(identity.obligation_id)
    if contract is None or obligation is None:
        raise RefutationAuditError("cannot seal refutation checkpoint without live root records")
    runtime = project.runtime_snapshot()
    lease_id = pending.get("lease_id")
    if lease_id and lease_id in runtime.active_lease_ids:
        raise RefutationAuditError("refutation audit lease is still running; checkpoint waits for settlement")
    acceptance = None
    audit = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != _EVENT_RECORDED:
            continue
        payload = dict(envelope.payload)
        stored_id = payload.get("refutation_identity")
        acc = payload.get("acceptance")
        if not isinstance(stored_id, Mapping) or not isinstance(acc, Mapping):
            continue
        if str(stored_id.get("identity_hash") or "") != identity.identity_hash:
            continue
        record = AcceptanceRecord.from_dict(acc)
        if record.verdict is AcceptanceVerdict.PASS and record.project_outcome is ProjectOutcome.REFUTED:
            acceptance = record
            if isinstance(payload.get("audit"), Mapping):
                audit = FinalAuditReceipt.from_dict(payload["audit"])
    if acceptance is None:
        raise RefutationAuditError("missing PASS REFUTED acceptance to seal checkpoint")
    outcome = derive_project_outcome(
        contract, acceptance, project.policy, runtime.resources, view, runtime,
    )
    if outcome.outcome is not ProjectOutcome.REFUTED:
        raise RefutationAuditError("derived outcome must be REFUTED")
    checkpoint = build_checkpoint_report(
        contract, outcome, view, runtime, _max_seq(project),
        accepted_result_ids=(identity.evidence_id,), created_at=ts,
    )
    if lease_id and lease_id in checkpoint.active_lease_ids:
        raise RefutationAuditError("settled refutation checkpoint still lists a running audit lease")
    project.artifacts.put_bytes(
        canonical_json(checkpoint.to_dict()).encode("utf-8"), media_type="application/json",
    )
    project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor=_ACTOR,
        event_type=EVENT_CHECKPOINT_CREATED,
        idempotency_key=f"refutation-audit-checkpoint:{identity.obligation_id}:{checkpoint.report_id}",
        payload={"checkpoint": checkpoint.to_dict()},
        timestamp_utc=ts, correlation_id=identity.obligation_id,
    ))
    payload = _run_payload(
        identity, int(pending.get("attempt_index") or 1), RefutationAuditStatus.REFUTED,
        reason="final_acceptance_pass",
        action_id=str(pending.get("action_id") or ""),
        lease_id=str(lease_id or ""),
        transport_status=pending.get("transport_status"),
        verdict=pending.get("verdict") or _CORRECT,
        audit_receipt_id=pending.get("audit_receipt_id") or (audit.receipt_id if audit else None),
        acceptance_id=pending.get("acceptance_id") or acceptance.record_id,
        checkpoint_id=checkpoint.report_id,
        verify_artifact_hash=pending.get("verify_artifact_hash"),
        outcome=outcome.outcome.value,
    )
    _record_run(project, payload, identity, ts)
    return RefutationAuditRun.from_dict({**payload, "replayed": replayed})


def _load_matching_infra_for_root(
    project, obligation_id: str, env_hash: str,
) -> RefutationAuditRun | None:
    found = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != _EVENT_RECORDED:
            continue
        payload = dict(envelope.payload)
        stored_id = payload.get("refutation_identity")
        raw_run = payload.get("run")
        if not isinstance(stored_id, Mapping) or not isinstance(raw_run, Mapping):
            continue
        if str(stored_id.get("obligation_id") or "") != obligation_id:
            continue
        if str(stored_id.get("environment_hash") or "") != env_hash:
            continue
        if raw_run.get("status") == RefutationAuditStatus.INFRA.value:
            found = RefutationAuditRun.from_dict(raw_run)
    return found


def _load_matching_infra(project, identity: RefutationAuditIdentity) -> RefutationAuditRun | None:
    found = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != _EVENT_RECORDED:
            continue
        payload = dict(envelope.payload)
        stored_id = payload.get("refutation_identity")
        raw_run = payload.get("run")
        if not isinstance(stored_id, Mapping) or not isinstance(raw_run, Mapping):
            continue
        if str(stored_id.get("identity_hash") or "") != identity.identity_hash:
            continue
        if raw_run.get("status") == RefutationAuditStatus.INFRA.value:
            found = RefutationAuditRun.from_dict(raw_run)
    return found


def _next_attempt_index(project, identity: RefutationAuditIdentity) -> int:
    highest = 0
    prefix = f"refutation-audit:{identity.identity_hash}:"
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        payload = dict(envelope.payload)
        if envelope.event_type == "euler.local_action_finished":
            request_id = str(payload.get("request_id") or "")
            if request_id.startswith(prefix):
                try:
                    highest = max(highest, int(request_id.rsplit(":", 1)[-1]))
                except ValueError:
                    continue
        raw_run = payload.get("run")
        if isinstance(raw_run, Mapping):
            stored_id = payload.get("refutation_identity")
            if isinstance(stored_id, Mapping) and str(stored_id.get("identity_hash") or "") == identity.identity_hash:
                highest = max(highest, int(raw_run.get("attempt_index") or 0))
    return highest + 1


def _assert_replay_artifacts(project, run: RefutationAuditRun) -> None:
    if not run.verify_artifact_hash:
        return
    try:
        project.artifacts.verify(run.verify_artifact_hash)
        project.artifacts.get_bytes(run.verify_artifact_hash)
    except Exception as exc:
        raise RefutationAuditError(
            f"stored refutation-audit artifact is missing or tampered: {exc}"
        ) from exc


def _max_seq(project) -> int:
    last = 0
    for seq, _envelope in project.event_store.iter_project(project.project_id):
        last = seq
    return last


def _run_id_fields(
    *,
    obligation_id: str,
    contract_id: str,
    status: RefutationAuditStatus,
    identity_hash: str,
    attempt_index: int,
    lease_id: Optional[str],
    verify_artifact_hash: Optional[str],
    acceptance_id: Optional[str],
    checkpoint_id: Optional[str],
) -> str:
    return stable_id(
        _RUN_DOMAIN, 1,
        obligation_id, contract_id, status.value, identity_hash,
        str(attempt_index), lease_id or "", verify_artifact_hash or "",
        acceptance_id or "", checkpoint_id or "",
    )


def _run_payload(
    identity: RefutationAuditIdentity, attempt_index: int, status: RefutationAuditStatus,
    *, reason: str, action_id: str = "", lease_id: str = "",
    transport_status: Optional[str] = None,
    verdict: Optional[str] = None, audit_receipt_id: Optional[str] = None,
    acceptance_id: Optional[str] = None, checkpoint_id: Optional[str] = None,
    verify_artifact_hash: Optional[str] = None, failure_id: Optional[str] = None,
    outcome: Optional[str] = None, retry_reason: str = "",
) -> dict[str, Any]:
    run = RefutationAuditRun.build(
        obligation_id=identity.obligation_id,
        contract_id=identity.contract_id,
        status=status,
        replayed=False,
        reason=reason,
        identity_hash=identity.identity_hash,
        attempt_index=attempt_index,
        lease_id=lease_id or None,
        action_id=action_id or None,
        transport_status=transport_status,
        verdict=verdict,
        audit_receipt_id=audit_receipt_id,
        acceptance_id=acceptance_id,
        checkpoint_id=checkpoint_id,
        verify_artifact_hash=verify_artifact_hash,
        evidence_id=identity.evidence_id,
        outcome=outcome,
        failure_id=failure_id,
        retry_reason=retry_reason or None,
    )
    return run.to_dict()


def _infra_payload(identity, attempt_index: int, *, reason: str, retry_reason: str) -> dict[str, Any]:
    payload = _run_payload(
        identity, attempt_index, RefutationAuditStatus.INFRA,
        reason=reason, retry_reason=retry_reason,
    )
    return {**payload, "status": "INFRA"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
