"""Callable compute service over the existing polynomial search/check plugin.

Runs the deterministic integer-box plugin under ``run_local_action(kind=COMPUTE)``.
A checked candidate is not a CHECKED_COUNTEREXAMPLE until an explicit recorded
auditor realization review. Bounded search without a witness is UNKNOWN, never
a universal proof. This module does not write the Fact Graph.
"""

from __future__ import annotations

from typing import Any, Mapping

from euler.control.acceptance import (
    FinalAuditKind,
    FinalAuditReceipt,
    promote_compute_counterexample,
)
from euler.control.compute import (
    ComputeLimits,
    ComputeRequest,
    ComputeRunResult,
    ComputeRunner,
    ComputeTermination,
)
from euler.control.compute_plugins.polynomial_counterexample import (
    PLUGIN_ID,
    SEARCHER_VERSION,
    PolynomialChecker,
    PolynomialPluginError,
    PolynomialPredicate,
    PolynomialSearcher,
)
from euler.control.contracts import CheckerResult, EvidenceKind, ValidityStatus
from euler.control.failures import ConfidenceClass, FailureKind, FailureRecord
from euler.control.final_audit import FINAL_ACCEPTANCE_POLICY_HASH
from euler.control.ids import canonical_json, sha256_hex
from euler.control.local_actions import LocalActionError, run_local_action
from euler.control.obligations import ObligationStatus, RouteStatus
from euler.control.policy import DimensionLimit
from euler.control.project import EulerProject
from euler.control.research_service import ResearchService, ResearchServiceError, _now
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import ActionKind

__all__ = [
    "ComputeServiceError",
    "SUPPORTED_PLUGIN_ID",
    "OUTCOME_CONSEQUENCES",
    "run_polynomial_compute",
    "review_compute_realization",
]

SUPPORTED_PLUGIN_ID = PLUGIN_ID
_MECHANISM = "polynomial-counterexample"
_FORBIDDEN_RESULT_KEYS = frozenset(
    {"checker_result", "status", "pass", "PASS", "evidence", "termination"}
)
OUTCOME_CONSEQUENCES = {
    "CHECKED_COUNTEREXAMPLE": (
        "Finite checked witness of the tested claim only. Promotion to "
        "CHECKED_COUNTEREXAMPLE requires an explicit recorded auditor "
        "realization review. After promotion it refutes only the named "
        "obligation and, if given, that route version."
    ),
    "NO_WITNESS": (
        "The declared finite box was examined without a witness, or a bound "
        "stopped the search. This is UNKNOWN, never a universal proof."
    ),
    "TIMEOUT": "Search stopped on a declared wall or point bound. UNKNOWN.",
    "ERROR": "Infrastructure or checker error. Not a mathematical refutation.",
}


class ComputeServiceError(ValueError):
    """Invalid compute request, missing review, or illegal promotion."""


def _digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def _target(project: EulerProject, obligation_id: str):
    view = project.research_view()
    obligation = view.get_obligation(obligation_id)
    if obligation is None:
        raise ComputeServiceError("unknown obligation")
    contract = view.get_contract(obligation.contract_id)
    if contract is None:
        raise ComputeServiceError("missing contract")
    return contract, obligation


def _predicate(raw: object) -> PolynomialPredicate:
    if isinstance(raw, PolynomialPredicate):
        return raw
    try:
        return PolynomialPredicate.from_dict(raw)
    except (PolynomialPluginError, TypeError, ValueError, KeyError) as exc:
        raise ComputeServiceError(f"unsupported polynomial predicate: {exc}") from exc


def _public_status(termination: ComputeTermination) -> str:
    if termination is ComputeTermination.CHECKED_COUNTEREXAMPLE:
        return "CERTIFIED_CANDIDATE"
    if termination is ComputeTermination.TIME_LIMIT_REACHED:
        return "UNKNOWN"
    if termination in {
        ComputeTermination.POINT_LIMIT_REACHED,
        ComputeTermination.DOMAIN_EXHAUSTED,
    }:
        return "UNKNOWN"
    return "ERROR"


def run_polynomial_compute(
    project: EulerProject,
    *,
    obligation_id: str,
    request_id: str,
    predicate: Mapping[str, Any] | PolynomialPredicate,
    max_points: int,
    max_wall_ms: int,
    actor: str,
    plugin_id: str = SUPPORTED_PLUGIN_ID,
    consequences: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Search the exact finite box for the bound obligation.

    Executes the existing polynomial searcher/checker under a COMPUTE lease.
    A found witness remains a candidate until ``review_compute_realization``.
    """
    if plugin_id != SUPPORTED_PLUGIN_ID:
        raise ComputeServiceError(
            f"only plugin {SUPPORTED_PLUGIN_ID!r} is supported"
        )
    if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points < 1:
        raise ComputeServiceError("max_points must be a positive int")
    if isinstance(max_wall_ms, bool) or not isinstance(max_wall_ms, int) or max_wall_ms < 1:
        raise ComputeServiceError("max_wall_ms must be a positive int")
    contract, obligation = _target(project, obligation_id)
    spec = _predicate(predicate)
    outcome_notes = dict(OUTCOME_CONSEQUENCES)
    if consequences:
        outcome_notes.update(dict(consequences))
    parameters = {
        "plugin_id": SUPPORTED_PLUGIN_ID,
        "plugin_version": SEARCHER_VERSION,
        "predicate": spec.to_dict(),
        "domain_hash": spec.domain_hash(),
        "declared_total_points": spec.total_points(),
        "max_points": max_points,
        "max_wall_ms": max_wall_ms,
        "tested_claim": obligation.exact_statement,
        "obligation_id": obligation_id,
        "contract_id": contract.contract_id,
        "scope_hash": _digest(obligation.scope.to_dict()),
        "consequences": outcome_notes,
    }
    if set(parameters) & _FORBIDDEN_RESULT_KEYS:
        raise ComputeServiceError("compute input cannot include checker results")
    searcher = PolynomialSearcher()
    checker = PolynomialChecker()
    env_hash = _digest(
        {
            "plugin": SUPPORTED_PLUGIN_ID,
            "searcher": searcher.identity.to_dict(),
            "checker": checker.identity.to_dict(),
            "payload_hash": spec.payload_hash(),
        }
    )

    def execute():
        running = next(
            (
                item
                for item in project.runtime_snapshot().leases
                if item.status is LeaseStatus.RUNNING
                and item.action_kind == ActionKind.COMPUTE
                and item.mechanism == _MECHANISM
                and item.obligation_id == obligation_id
            ),
            None,
        )
        if running is None:
            raise ComputeServiceError("compute lease is not running")
        request = ComputeRequest.build(
            contract_id=contract.contract_id,
            obligation_id=obligation_id,
            action_id=running.action_id,
            tested_claim=obligation.exact_statement,
            scope=obligation.scope,
            plugin_id=SUPPORTED_PLUGIN_ID,
            plugin_version=SEARCHER_VERSION,
            plugin_payload=spec.to_dict(),
            domain_hash=spec.domain_hash(),
            declared_total_points=spec.total_points(),
            limits=ComputeLimits(
                max_points=DimensionLimit.limited(max_points),
                max_wall_ms=DimensionLimit.limited(max_wall_ms),
            ),
            searcher=searcher.identity,
            checker=checker.identity,
            tool_environment_hash=env_hash,
            created_at=_now(),
        )
        ran = ComputeRunner().run(request, searcher, checker, project.artifacts)
        refs = [ran.report_ref.content_hash]
        if ran.candidate_ref is not None:
            refs.append(ran.candidate_ref.content_hash)
        payload = {
            "status": _public_status(ran.termination),
            "termination": ran.termination.value,
            "epistemic": (
                "CANDIDATE"
                if ran.termination is ComputeTermination.CHECKED_COUNTEREXAMPLE
                else "UNKNOWN"
            ),
            "not_a_proof": True,
            "compute_result": ran.to_dict(),
            "request": request.to_dict(),
            "obligation_id": obligation_id,
            "consequences": outcome_notes,
        }
        return payload, tuple(refs)

    try:
        result = run_local_action(
            project,
            obligation_id=obligation_id,
            request_id=request_id,
            mechanism=_MECHANISM,
            parameters=parameters,
            timeout_ms=max_wall_ms,
            actor=actor,
            execute=execute,
            kind=ActionKind.COMPUTE,
        )
    except LocalActionError as exc:
        raise ComputeServiceError(str(exc)) from exc
    if result.get("status") == "DEFERRED":
        return {**result, "not_a_proof": True, "epistemic": "UNKNOWN"}
    service = ResearchService(project, actor=actor)
    ts = _now()
    service._append(
        "euler.compute_ran",
        {
            "request_id": request_id,
            "obligation_id": obligation_id,
            "contract_id": contract.contract_id,
            "lease_id": result.get("lease_id"),
            "status": result.get("status"),
            "termination": result.get("termination"),
            "epistemic": result.get("epistemic"),
            "not_a_proof": True,
            "compute_result": result.get("compute_result"),
            "request": result.get("request"),
        },
        "compute-run:" + request_id,
        ts,
    )
    if (
        not result.get("replayed")
        and result.get("status") == "UNKNOWN"
        and result.get("compute_result")
    ):
        diagnostic = FailureRecord.build(
            kind=FailureKind.COMPUTE_INCONCLUSIVE,
            exact_failed_claim=obligation.exact_statement,
            scope=obligation.scope,
            confidence=ConfidenceClass.LOW,
            context_version=1,
            provenance="polynomial-counterexample bounded search",
            created_at=ts,
            reopen_conditions=("retry with a larger declared bound or a new request_id",),
            missing_conditions=("no independently checked witness in the declared box",),
            target_obligation_id=obligation_id,
            contract_id=contract.contract_id,
        )
        try:
            service.apply(
                [
                    {
                        "event_type": "failure.recorded",
                        "payload": {"failure": diagnostic.to_dict()},
                        "idempotency_key": "compute-unknown:" + request_id,
                    }
                ],
                timestamp_utc=ts,
            )
        except ResearchServiceError as exc:
            raise ComputeServiceError(str(exc)) from exc
    return result


def review_compute_realization(
    project: EulerProject,
    *,
    obligation_id: str,
    request_id: str,
    confirmed_scope_hash: str,
    notes: str,
    actor: str,
    translation_confirmed: bool,
    original_object_realized: bool,
    route_id: str | None = None,
    auditor_version: str = "1",
) -> dict[str, Any]:
    """Record an explicit auditor lifting review, then promote scoped evidence.

    Generic semantic lifting is not automated. The caller must affirm that the
    finite-box witness realizes the original obligation object and that the
    scopes match. Only the named child/route is refuted.
    """
    if not isinstance(notes, str) or not notes.strip():
        raise ComputeServiceError("nonempty realization notes are required")
    if translation_confirmed is not True or original_object_realized is not True:
        raise ComputeServiceError(
            "promotion requires recorded translation and original-object realization"
        )
    contract, obligation = _target(project, obligation_id)
    if confirmed_scope_hash != _digest(obligation.scope.to_dict()):
        raise ComputeServiceError("reviewed scope does not match the obligation")
    runs = [
        dict(event.payload)
        for _, event in project.event_store.iter_project(project.project_id)
        if event.event_type == "euler.compute_ran"
        and dict(event.payload).get("request_id") == request_id
        and dict(event.payload).get("obligation_id") == obligation_id
    ]
    if not runs:
        raise ComputeServiceError("no registered compute run for this request and obligation")
    ran_payload = runs[-1]
    if ran_payload.get("termination") != ComputeTermination.CHECKED_COUNTEREXAMPLE.value:
        raise ComputeServiceError("realization review requires a checked finite candidate")
    compute_result = ComputeRunResult.from_dict(ran_payload["compute_result"])
    if compute_result.candidate_ref is None or compute_result.evidence is None:
        raise ComputeServiceError("checked candidate artifacts are missing")
    project.artifacts.verify(compute_result.candidate_ref.content_hash)
    project.artifacts.verify(compute_result.report_ref.content_hash)
    request = ComputeRequest.from_dict(ran_payload["request"])
    if request.tested_claim != obligation.exact_statement:
        raise ComputeServiceError("stored tested claim does not match the obligation")
    if request.scope != obligation.scope:
        raise ComputeServiceError("stored compute scope does not match the obligation")
    review_identity = _digest(
        {
            "request_id": request_id,
            "obligation_id": obligation_id,
            "confirmed_scope_hash": confirmed_scope_hash,
            "notes": notes,
            "translation_confirmed": True,
            "original_object_realized": True,
            "actor": actor,
            "auditor_version": auditor_version,
            "route_id": route_id,
            "candidate": compute_result.candidate_ref.content_hash,
            "report": compute_result.report_ref.content_hash,
        }
    )
    service = ResearchService(project, actor=actor)
    for _, event in project.event_store.iter_project(project.project_id):
        data = dict(event.payload)
        if (
            event.event_type == "euler.compute_realization_reviewed"
            and data.get("review_identity") == review_identity
        ):
            return {
                "obligation_id": obligation_id,
                "evidence_id": data["evidence_id"],
                "review_hash": data["review_hash"],
                "replayed": True,
                "evidence_kind": EvidenceKind.CHECKED_COUNTEREXAMPLE.value,
            }
    ts = _now()
    review_blob = canonical_json(
        {
            "review_identity": review_identity,
            "notes": notes,
            "translation_confirmed": True,
            "original_object_realized": True,
            "actor": actor,
            "auditor_version": auditor_version,
            "candidate_content_hash": compute_result.candidate_ref.content_hash,
            "report_content_hash": compute_result.report_ref.content_hash,
            "scope_hash": confirmed_scope_hash,
        }
    ).encode("utf-8")
    review_ref = project.artifacts.put_bytes(review_blob, media_type="application/json")
    audit = FinalAuditReceipt.build(
        audit_kind=FinalAuditKind.COUNTEREXAMPLE,
        checker_result=CheckerResult.PASS,
        contract_id=contract.contract_id,
        obligation_id=obligation_id,
        action_id=compute_result.evidence.action_id,
        exact_claim=obligation.exact_statement,
        scope=obligation.scope,
        acceptance_policy_hash=FINAL_ACCEPTANCE_POLICY_HASH,
        auditor_id=actor,
        auditor_version=auditor_version,
        environment_hash=review_ref.content_hash,
        audit_artifact_hash=review_ref.content_hash,
        created_at=ts,
        translation_confirmed=True,
        original_object_realized=True,
        candidate_content_hash=compute_result.candidate_ref.content_hash,
        report_content_hash=compute_result.report_ref.content_hash,
        alignment_attestation_hash=review_ref.content_hash,
    )
    try:
        evidence = promote_compute_counterexample(
            contract,
            obligation,
            compute_result,
            audit,
            project.artifacts,
            actor,
            ts,
        )
    except Exception as exc:
        raise ComputeServiceError(f"promotion rejected: {exc}") from exc
    operations: list[dict[str, Any]] = []
    if obligation.status is not ObligationStatus.REFUTED:
        operations.append(
            {
                "event_type": "obligation.status_changed",
                "payload": {
                    "obligation_id": obligation_id,
                    "status": "REFUTED",
                    "evidence_refs": [evidence.artifact_id],
                },
                "idempotency_key": "compute-refute:" + request_id,
            }
        )
    if route_id:
        route = project.research_view().get_route(route_id)
        if route is None:
            raise ComputeServiceError("unknown route")
        if obligation_id not in route.required_obligation_ids:
            raise ComputeServiceError("route does not depend on this obligation")
        if route.status is not RouteStatus.REFUTED:
            operations.append(
                {
                    "event_type": "route.status_changed",
                    "payload": {
                        "route_id": route_id,
                        "status": "REFUTED",
                        "evidence_refs": [evidence.artifact_id],
                        "reason": "checked counterexample of a required premise",
                    },
                    "idempotency_key": "compute-refute-route:" + route_id,
                }
            )
    failure = FailureRecord.build(
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        exact_failed_claim=obligation.exact_statement,
        scope=obligation.scope,
        confidence=ConfidenceClass.CERTAIN,
        context_version=1,
        provenance="explicit auditor realization of a polynomial compute witness",
        created_at=ts,
        witness_refs=(evidence.artifact_id,),
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        target_obligation_id=obligation_id,
        route_id=route_id,
        contract_id=contract.contract_id,
        exclusion_scope=obligation.scope,
        rejected_step="finite-box counterexample after recorded realization review",
    )
    operations.append(
        {
            "event_type": "failure.recorded",
            "payload": {"failure": failure.to_dict()},
            "idempotency_key": "compute-cex:" + request_id,
        }
    )
    try:
        with project.event_store.transaction(immediate=True):
            existing = project.research_view().get_evidence(evidence.artifact_id)
            if existing is None:
                service._append(
                    "evidence.registered",
                    {"evidence": evidence.to_dict()},
                    "compute-evidence:" + evidence.artifact_id,
                    ts,
                )
            elif existing.validity is not ValidityStatus.VALID:
                raise ComputeServiceError("compute evidence was invalidated")
            service._append(
                "euler.compute_realization_reviewed",
                {
                    "request_id": request_id,
                    "obligation_id": obligation_id,
                    "review_identity": review_identity,
                    "review_hash": review_ref.content_hash,
                    "evidence_id": evidence.artifact_id,
                    "route_id": route_id,
                },
                "compute-review:" + review_identity,
                ts,
            )
            if operations:
                service.apply(operations, timestamp_utc=ts)
            project.research_view()
    except ComputeServiceError:
        raise
    except (ResearchServiceError, Exception) as exc:
        raise ComputeServiceError(f"research registration rejected: {exc}") from exc
    return {
        "obligation_id": obligation_id,
        "evidence_id": evidence.artifact_id,
        "review_hash": review_ref.content_hash,
        "replayed": False,
        "evidence_kind": evidence.evidence_kind.value,
    }
