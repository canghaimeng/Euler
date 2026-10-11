"""Idempotent managed-round reconciliation over EventStore + read-only snapshot.

Stdlib plus existing control types. Does not import gateway/verify, does not
write Fact Graph or Global Memory, and does not launch providers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from euler.control.acceptance import collect_fact_admissions
from euler.control.adapters.project import snapshot_project
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.dispatch import (
    binding_for_lease,
    binding_for_round,
    hash_text,
    load_dispatch,
    mark_sidecar_settled,
    task_path,
)
from euler.control.event_store import IdempotencyConflict
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import (
    EVENT_DISPATCH_READY,
    EVENT_ROUND_BOUND,
    EVENT_ROUND_RECONCILED,
    DispatchBinding,
    ReconciliationResult,
    reconcile_id,
)
from euler.control.obligations import ObligationStatus, RelationType
from euler.control.progress import ProgressKind, ProgressReceipt, validate_progress_against_view
from euler.control.project import EulerProject, is_euler_project, open_project
from euler.control.runtime import Lease, LeaseConflict, LeaseStatus
from euler.control.schema import (
    EventEnvelope,
    MathematicalStatus,
    ObservationStatus,
    ObservedQuantity,
    UsageRecord,
)
from euler.execution.launch_receipt import (
    RoundReceiptError,
    bind_result_usage_to_launch,
)
from euler.control.views import ResearchView

_ACTOR = "reconcile"
_OUTCOME_DISCHARGED = "OBLIGATION_DISCHARGED"
_OUTCOME_REDUCTION = "REDUCTION_VERIFIED"
_OUTCOME_COUNTEREXAMPLE = "COUNTEREXAMPLE_CHECKED"
_OUTCOME_NO_PROGRESS = "NO_VERIFIABLE_PROGRESS"
_OUTCOME_INFRA = "INFRA_FAILED"
_PROVENANCE_FACT_SUBMIT = "fact_submit"

__all__ = [
    "ReconcileError",
    "action_frontier_hash",
    "context_version_digest",
    "frontier_hash_for",
    "mark_lease_running",
    "reconcile_managed_round",
    "scope_digest",
]


class ReconcileError(ValueError):
    """Managed round hook could not validate or reconcile fail-closed."""


def scope_digest(scope: MathematicalScope) -> str:
    return sha256_hex(canonical_json(scope.to_dict()).encode("utf-8"))


def context_version_digest(
    *,
    cache_key_hash: str,
    packet_hash: str,
    reference_versions_hash: str,
    tool_environment_hash: str,
    definitions_hash: str,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "cache_key_hash": cache_key_hash,
                "packet_hash": packet_hash,
                "reference_versions_hash": reference_versions_hash,
                "tool_environment_hash": tool_environment_hash,
                "definitions_hash": definitions_hash,
            }
        ).encode("utf-8")
    )


def action_frontier_hash(
    *,
    obligation_id: str,
    scope_hash: str,
    required_facts: Sequence[tuple[str, str]] | list[tuple[str, str]],
    dependency_hash: str,
    context_version_hash: str,
) -> str:
    """Hash the action-local obligation, required Fact versions, and context.

    Unrelated active Facts are excluded so concurrent work cannot invalidate
    an otherwise valid lease.
    """
    facts = sorted(
        ({"fact_id": fact_id, "content_sha256": version} for fact_id, version in required_facts),
        key=lambda item: item["fact_id"],
    )
    return sha256_hex(
        canonical_json(
            {
                "obligation_id": obligation_id,
                "scope_hash": scope_hash,
                "required_facts": facts,
                "dependency_hash": dependency_hash,
                "context_version_hash": context_version_hash,
            }
        ).encode("utf-8")
    )


def frontier_hash_for(
    snapshot: object,
    obligation_id: str,
    *,
    required_fact_ids: Sequence[str] | None = None,
    scope_hash: str = "",
    dependency_hash: str = "",
    context_version_hash: str = "",
) -> str:
    """Action-scoped fingerprint. ``required_fact_ids`` defaults to none.

    Legacy callers that passed only the snapshot still get an obligation-bound
    digest; they no longer hash the entire active Fact set.
    """
    active = {item.fact_id: item for item in getattr(snapshot, "active_facts", ())}
    wanted = tuple(required_fact_ids or ())
    pairs = []
    for fact_id in wanted:
        fact = active.get(fact_id)
        version = getattr(fact, "content_sha256", "") if fact is not None else ""
        pairs.append((fact_id, version))
    return action_frontier_hash(
        obligation_id=obligation_id,
        scope_hash=scope_hash,
        required_facts=pairs,
        dependency_hash=dependency_hash,
        context_version_hash=context_version_hash,
    )


def mark_lease_running(project_dir, worker, **kwargs):
    from .campaign import admission_lock
    with admission_lock(Path(project_dir)):
        return _mark_lease_running_admitted(project_dir, worker, **kwargs)


def _mark_lease_running_admitted(
    project_dir: str | Path,
    worker: str,
    *,
    start_payload: Mapping[str, Any],
    launch_payload: Mapping[str, Any],
    actor: str = _ACTOR,
    timestamp_utc: str | None = None,
) -> DispatchBinding:
    """Validate ready dispatch and mark the ISSUED lease RUNNING (idempotent)."""
    root = Path(project_dir)
    if not is_euler_project(root):
        raise ReconcileError(f"not an Euler-managed project: {root}")
    _assert_start_launch_link(worker, start_payload, launch_payload)
    binding = _load_ready_binding(root, worker)
    ts = timestamp_utc or str(launch_payload.get("created_at") or "")
    round_id = str(int(start_payload.get("round_id") or launch_payload.get("round_id") or 0))
    if round_id == "0":
        raise ReconcileError("start/launch round_id is required")
    project = open_project(root)
    try:
        _assert_ready_event(project, binding)
        _assert_frontier(project, binding)
        lease = _require_lease(project, binding)
        _assert_lease_matches_binding(lease, binding, worker)
        if lease.status is LeaseStatus.RUNNING:
            _bind_round(project, binding, round_id, start_payload, actor=actor, timestamp_utc=ts)
            return binding
        if lease.status is not LeaseStatus.ISSUED:
            raise ReconcileError(
                f"cannot start round: lease is {lease.status.value}, expected ISSUED"
            )
        from .campaign import assert_dispatch_allowed
        try:
            assert_dispatch_allowed(project, binding.candidate_id)
        except ValueError as exc:
            raise ReconcileError(str(exc)) from exc
        try:
            project.runtime.mark_running(
                project.project_id, binding.lease_id, actor=actor, timestamp_utc=ts
            )
        except LeaseConflict as exc:
            raise ReconcileError(str(exc)) from exc
        _bind_round(project, binding, round_id, start_payload, actor=actor, timestamp_utc=ts)
        return binding
    finally:
        project.close()


def reconcile_managed_round(
    project_dir: str | Path,
    worker: str,
    *,
    start_payload: Mapping[str, Any],
    launch_payload: Mapping[str, Any],
    result_payload: Mapping[str, Any],
    end_payload: Mapping[str, Any],
    actor: str = _ACTOR,
    timestamp_utc: str | None = None,
) -> ReconciliationResult:
    """Idempotent post-round reconciliation keyed by lease+round."""
    root = Path(project_dir)
    if not is_euler_project(root):
        raise ReconcileError(f"not an Euler-managed project: {root}")
    _assert_start_launch_link(worker, start_payload, launch_payload)
    _assert_result_end_link(
        worker, start_payload, launch_payload, result_payload, end_payload
    )
    round_id = str(int(result_payload["round_id"]))
    ts = timestamp_utc or str(result_payload.get("completed_at") or "")
    usage = _usage_from_result(result_payload, launch_payload)
    project = open_project(root)
    try:
        binding = _resolve_historical_binding(project, root, worker, round_id)
        existing = _existing_reconciliation(project, binding.lease_id, round_id)
        if existing is not None:
            return existing
        _assert_ready_event(project, binding)
        lease = _require_lease(project, binding)
        _assert_lease_matches_binding(lease, binding, worker)
        if lease.status not in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
            raise ReconcileError(
                f"cannot reconcile lease in status {lease.status.value}"
            )
        rc = int(result_payload["rc"])
        if rc != 0:
            record = _reconcile_infra(
                project, binding, round_id, usage, actor=actor, timestamp_utc=ts, rc=rc
            )
        else:
            record = _reconcile_success(
                project, binding, round_id, usage, actor=actor, timestamp_utc=ts
            )
        mark_sidecar_settled(root, worker, binding, record)
        return record
    finally:
        project.close()


def _reconcile_infra(
    project: EulerProject,
    binding: DispatchBinding,
    round_id: str,
    usage: UsageRecord,
    *,
    actor: str,
    timestamp_utc: str,
    rc: int,
) -> ReconciliationResult:
    record = ReconciliationResult.build(
        lease_id=binding.lease_id,
        round_id=round_id,
        outcome_ref=_OUTCOME_INFRA,
    )
    reason = f"provider round rc={rc}"
    try:
        with project.event_store.transaction(immediate=True):
            from .campaign import cancellation_requested
            if rc == 130 and cancellation_requested(project, binding.lease_id):
                project.runtime.cancel(project.project_id, binding.lease_id,
                                       actor=actor, timestamp_utc=timestamp_utc)
            elif rc == 124:
                project.runtime.expire(
                    project.project_id,
                    binding.lease_id,
                    usage=usage,
                    actor=actor,
                    timestamp_utc=timestamp_utc,
                )
            else:
                project.runtime.fail_infrastructure(
                    project.project_id,
                    binding.lease_id,
                    usage=usage,
                    reason=reason,
                    actor=actor,
                    timestamp_utc=timestamp_utc,
                )
            _append_reconciled(project, record, binding, actor=actor, timestamp_utc=timestamp_utc)
    except LeaseConflict as exc:
        raise ReconcileError(str(exc)) from exc
    return record


def _reconcile_success(
    project: EulerProject,
    binding: DispatchBinding,
    round_id: str,
    usage: UsageRecord,
    *,
    actor: str,
    timestamp_utc: str,
) -> ReconciliationResult:
    snapshot = snapshot_project(project.project_dir)
    _assert_frontier(project, binding, snapshot=snapshot)
    view = project.research_view()
    obligation = _bound_obligation(project, binding, view)
    contract = _require_contract(view, obligation.contract_id)
    old_hash = binding.frontier_hash
    admissible = [
        fact
        for fact in _admissible_target_facts(
            snapshot, binding, obligation.exact_statement
        )
        if evidence_satisfies(
            _preview_evidence(fact, contract, obligation, binding, timestamp_utc),
            contract.required_evidence_level,
            obligation.scope,
            contract.contract_id,
            exact_claim=obligation.exact_statement,
        )
    ]
    registered = _new_registered_evidence(view, binding, obligation, contract)
    if admissible or registered:
        return _discharge(
            project,
            binding,
            round_id,
            usage,
            snapshot=snapshot,
            view=view,
            contract=contract,
            obligation=obligation,
            facts=tuple(admissible),
            extra_evidence=tuple(registered),
            old_hash=old_hash,
            actor=actor,
            timestamp_utc=timestamp_utc,
        )
    reduction = _matching_reduction(view, binding, obligation, contract)
    if reduction is not None:
        return _typed_progress(
            project,
            binding,
            round_id,
            usage,
            view=view,
            contract=contract,
            obligation=obligation,
            kind=ProgressKind.REDUCTION_VERIFIED,
            outcome_ref=_OUTCOME_REDUCTION,
            claim=reduction.exact_claim,
            evidence_ids=tuple(reduction.evidence_refs),
            old_hash=old_hash,
            actor=actor,
            timestamp_utc=timestamp_utc,
            status=None,
        )
    counter = _matching_counterexample(view, binding, obligation, contract)
    if counter is not None:
        return _typed_progress(
            project,
            binding,
            round_id,
            usage,
            view=view,
            contract=contract,
            obligation=obligation,
            kind=ProgressKind.COUNTEREXAMPLE_CHECKED,
            outcome_ref=_OUTCOME_COUNTEREXAMPLE,
            claim=obligation.exact_statement,
            evidence_ids=(counter.artifact_id,),
            old_hash=old_hash,
            actor=actor,
            timestamp_utc=timestamp_utc,
            status=ObligationStatus.REFUTED,
        )
    return _no_progress(
        project,
        binding,
        round_id,
        usage,
        view=view,
        contract=contract,
        obligation=obligation,
        old_hash=old_hash,
        actor=actor,
        timestamp_utc=timestamp_utc,
    )


def _discharge(
    project: EulerProject,
    binding: DispatchBinding,
    round_id: str,
    usage: UsageRecord,
    *,
    snapshot,
    view: ResearchView,
    contract: ProblemContract,
    obligation,
    facts,
    extra_evidence: tuple[Any, ...] = (),
    old_hash: str,
    actor: str,
    timestamp_utc: str,
) -> ReconciliationResult:
    built: list[EvidenceArtifact] = []
    artifact_refs: list[str] = []
    fact_ids: list[str] = []
    fact_links: list[dict[str, Any]] = []
    for fact in facts:
        raw = fact.raw_markdown.encode("utf-8")
        ref = project.artifacts.put_bytes(raw, media_type="text/markdown")
        versions = _proof_dependency_versions(snapshot, fact.fact_id)
        evidence = EvidenceArtifact.build(
            content_hash=ref.content_hash,
            producer=binding.worker,
            contract_id=contract.contract_id,
            evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
            exact_claim=obligation.exact_statement,
            scope=obligation.scope,
            input_hashes=tuple(sorted(set(versions.values()))),
            environment_hash=binding.cache_key_hash,
            checker_id="fact_submit",
            checker_version="1",
            checker_result=CheckerResult.PASS,
            created_at=timestamp_utc,
        )
        if not evidence_satisfies(
            evidence,
            contract.required_evidence_level,
            obligation.scope,
            contract.contract_id,
            exact_claim=obligation.exact_statement,
        ):
            continue
        built.append(evidence)
        artifact_refs.append(ref.content_hash)
        fact_ids.append(fact.fact_id)
        fact_links.append({"fact_id": fact.fact_id, "obligation_id": obligation.obligation_id,
                           "evidence_id": evidence.artifact_id, "dependency_hashes": versions,
                           "scope_binding": binding.intent_id})
    for artifact in extra_evidence:
        built.append(artifact)
        artifact_refs.append(artifact.content_hash)
    if not built:
        return _no_progress(
            project,
            binding,
            round_id,
            usage,
            view=view,
            contract=contract,
            obligation=obligation,
            old_hash=old_hash,
            actor=actor,
            timestamp_utc=timestamp_utc,
        )
    evidence_ids = tuple(item.artifact_id for item in built)
    new_hash = sha256_hex(
        canonical_json(
            {
                "baseline": old_hash,
                "obligation_id": obligation.obligation_id,
                "fact_ids": fact_ids,
                "evidence_ids": list(evidence_ids),
            }
        ).encode("utf-8")
    )
    progress = ProgressReceipt.build(
        action_id=binding.action_id,
        attempt_id=binding.attempt_id,
        contract_id=contract.contract_id,
        kind=ProgressKind.OBLIGATION_DISCHARGED,
        claim=obligation.exact_statement,
        scope=obligation.scope,
        old_frontier_hash=old_hash,
        new_frontier_hash=new_hash,
        created_at=timestamp_utc,
        new_evidence_refs=evidence_ids,
        affected_obligation_ids=(obligation.obligation_id,),
    )
    record = ReconciliationResult.build(
        lease_id=binding.lease_id,
        round_id=round_id,
        progress_id=progress.receipt_id,
        evidence_ids=evidence_ids,
        fact_ids=tuple(fact_ids),
        outcome_ref=_OUTCOME_DISCHARGED,
    )
    try:
        with project.event_store.transaction(immediate=True):
            for link in fact_links:
                project.event_store.append(EventEnvelope.build(
                    project_id=project.project_id, actor=actor, event_type="euler.fact_linked",
                    idempotency_key=f"reconcile-fact-link:{binding.lease_id}:{round_id}:{link['fact_id']}",
                    payload=link, timestamp_utc=timestamp_utc, correlation_id=binding.lease_id))
            for index, evidence in enumerate(built):
                if any(
                    existing.artifact_id == evidence.artifact_id
                    for existing in view.evidence()
                ):
                    continue
                project.event_store.append(
                    EventEnvelope.build(
                        project_id=project.project_id,
                        actor=actor,
                        event_type="evidence.registered",
                        idempotency_key=(
                            f"reconcile-evidence:{binding.lease_id}:{round_id}:{index}"
                        ),
                        payload={"evidence": evidence.to_dict()},
                        timestamp_utc=timestamp_utc,
                        correlation_id=binding.lease_id,
                    )
                )
            project.event_store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor=actor,
                    event_type="obligation.status_changed",
                    idempotency_key=f"reconcile-discharge:{binding.lease_id}:{round_id}",
                    payload={
                        "obligation_id": obligation.obligation_id,
                        "status": "DISCHARGED",
                        "evidence_refs": list(evidence_ids),
                    },
                    timestamp_utc=timestamp_utc,
                    correlation_id=binding.lease_id,
                )
            )
            live_view = project.research_view()
            validate_progress_against_view(progress, live_view)
            project.runtime.complete(
                project.project_id,
                binding.lease_id,
                usage=usage,
                artifact_refs=tuple(artifact_refs),
                mathematical_status=MathematicalStatus.CANDIDATE,
                progress=progress,
                view=live_view,
                actor=actor,
                timestamp_utc=timestamp_utc,
            )
            _append_reconciled(project, record, binding, actor=actor, timestamp_utc=timestamp_utc)
    except Exception as exc:
        raise ReconcileError(str(exc)) from exc
    return record


def _typed_progress(
    project: EulerProject,
    binding: DispatchBinding,
    round_id: str,
    usage: UsageRecord,
    *,
    view: ResearchView,
    contract: ProblemContract,
    obligation,
    kind: ProgressKind,
    outcome_ref: str,
    claim: str,
    evidence_ids: tuple[str, ...],
    old_hash: str,
    actor: str,
    timestamp_utc: str,
    status: ObligationStatus | None,
) -> ReconciliationResult:
    new_hash = sha256_hex(
        canonical_json(
            {
                "baseline": old_hash,
                "obligation_id": obligation.obligation_id,
                "kind": kind.value,
                "evidence_ids": list(evidence_ids),
            }
        ).encode("utf-8")
    )
    progress = ProgressReceipt.build(
        action_id=binding.action_id,
        attempt_id=binding.attempt_id,
        contract_id=contract.contract_id,
        kind=kind,
        claim=claim,
        scope=obligation.scope,
        old_frontier_hash=old_hash,
        new_frontier_hash=new_hash,
        created_at=timestamp_utc,
        new_evidence_refs=evidence_ids,
        affected_obligation_ids=(obligation.obligation_id,),
    )
    record = ReconciliationResult.build(
        lease_id=binding.lease_id,
        round_id=round_id,
        progress_id=progress.receipt_id,
        evidence_ids=evidence_ids,
        outcome_ref=outcome_ref,
    )
    try:
        with project.event_store.transaction(immediate=True):
            if status is not None:
                project.event_store.append(
                    EventEnvelope.build(
                        project_id=project.project_id,
                        actor=actor,
                        event_type="obligation.status_changed",
                        idempotency_key=f"reconcile-{status.value.lower()}:{binding.lease_id}:{round_id}",
                        payload={
                            "obligation_id": obligation.obligation_id,
                            "status": status.value,
                            "evidence_refs": list(evidence_ids),
                        },
                        timestamp_utc=timestamp_utc,
                        correlation_id=binding.lease_id,
                    )
                )
            live_view = project.research_view()
            validate_progress_against_view(progress, live_view)
            project.runtime.complete(
                project.project_id,
                binding.lease_id,
                usage=usage,
                mathematical_status=(
                    MathematicalStatus.CANDIDATE
                    if status is not None
                    else MathematicalStatus.NO_CLAIM
                ),
                progress=progress,
                view=live_view,
                actor=actor,
                timestamp_utc=timestamp_utc,
            )
            _append_reconciled(project, record, binding, actor=actor, timestamp_utc=timestamp_utc)
    except Exception as exc:
        raise ReconcileError(str(exc)) from exc
    del view
    return record


def _no_progress(
    project: EulerProject,
    binding: DispatchBinding,
    round_id: str,
    usage: UsageRecord,
    *,
    view: ResearchView,
    contract: ProblemContract,
    obligation,
    old_hash: str,
    actor: str,
    timestamp_utc: str,
) -> ReconciliationResult:
    progress = ProgressReceipt.build(
        action_id=binding.action_id,
        attempt_id=binding.attempt_id,
        contract_id=contract.contract_id,
        kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        claim="no admissible target Fact",
        scope=obligation.scope,
        old_frontier_hash=old_hash,
        new_frontier_hash=old_hash,
        created_at=timestamp_utc,
    )
    record = ReconciliationResult.build(
        lease_id=binding.lease_id,
        round_id=round_id,
        progress_id=progress.receipt_id,
        outcome_ref=_OUTCOME_NO_PROGRESS,
    )
    try:
        with project.event_store.transaction(immediate=True):
            live_view = project.research_view()
            validate_progress_against_view(progress, live_view)
            project.runtime.complete(
                project.project_id,
                binding.lease_id,
                usage=usage,
                mathematical_status=MathematicalStatus.NO_CLAIM,
                progress=progress,
                view=live_view,
                actor=actor,
                timestamp_utc=timestamp_utc,
            )
            _append_reconciled(project, record, binding, actor=actor, timestamp_utc=timestamp_utc)
    except Exception as exc:
        raise ReconcileError(str(exc)) from exc
    return record


def _preview_evidence(fact, contract, obligation, binding, timestamp_utc: str) -> EvidenceArtifact:
    return EvidenceArtifact.build(
        content_hash=fact.content_sha256,
        producer=binding.worker,
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim=obligation.exact_statement,
        scope=obligation.scope,
        input_hashes=(fact.content_sha256,),
        environment_hash=binding.cache_key_hash,
        checker_id="fact_submit",
        checker_version="1",
        checker_result=CheckerResult.PASS,
        created_at=timestamp_utc,
    )


def _admissible_target_facts(snapshot, binding: DispatchBinding, exact_statement: str):
    baseline = set(binding.baseline_fact_ids)
    accepted = [item for item in collect_fact_admissions(snapshot)
                if item.accepted and item.provenance == _PROVENANCE_FACT_SUBMIT
                and item.fact_id is not None]
    admissions = {item.fact_id: item for item in accepted}
    # Re-auditing a pre-existing Fact is useful only with an actual fresh
    # verifier admission bound to this exact attempt and worker. A stored old
    # PASS or a human assertion cannot close the investigation.
    current_records = set()
    for memory in snapshot.memory_records:
        if memory.kind != "verification":
            continue
        raw = json.loads(memory.canonical_json)
        if (raw.get("attempt_id") == binding.attempt_id
                and raw.get("author") == binding.worker):
            current_records.add(memory.record_id)
    freshly_reviewed = {item.fact_id for item in accepted if item.record_id in current_records}
    out = []
    for fact in snapshot.active_facts:
        if fact.fact_id in baseline and fact.fact_id not in freshly_reviewed:
            continue
        admission = admissions.get(fact.fact_id)
        if admission is None:
            continue
        if fact.statement != exact_statement:
            continue
        if admission.exact_claim != exact_statement:
            continue
        out.append(fact)
    out.sort(key=lambda item: item.fact_id)
    return out


def _append_reconciled(
    project: EulerProject,
    record: ReconciliationResult,
    binding: DispatchBinding,
    *,
    actor: str,
    timestamp_utc: str,
) -> None:
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor=actor,
            event_type=EVENT_ROUND_RECONCILED,
            idempotency_key=record.reconciliation_id,
            payload={
                "reconciliation": record.to_dict(),
                "binding": binding.to_dict(),
            },
            timestamp_utc=timestamp_utc,
            correlation_id=binding.lease_id,
        )
    )

    from .intervention import reconcile_human_outcome
    reconcile_human_outcome(project, binding, record)
    from .campaign import settle_human_checkpoint
    settle_human_checkpoint(project)


def _existing_reconciliation(
    project: EulerProject, lease_id: str, round_id: str
) -> Optional[ReconciliationResult]:
    wanted = reconcile_id(lease_id=lease_id, round_id=round_id)
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != EVENT_ROUND_RECONCILED:
            continue
        payload = dict(envelope.payload)
        raw = payload.get("reconciliation")
        if not isinstance(raw, Mapping):
            continue
        record = ReconciliationResult.from_dict(raw)
        if record.reconciliation_id == wanted:
            return record
    return None


def _load_ready_binding(root: Path, worker: str) -> DispatchBinding:
    payload = load_dispatch(root, worker)
    if payload is None or payload.get("state") != "ready":
        raise ReconcileError(f"worker {worker!r} has no ready dispatch sidecar")
    try:
        binding = DispatchBinding.from_dict(payload.get("binding"))
    except Exception as exc:
        raise ReconcileError(f"dispatch sidecar binding is invalid: {exc}") from exc
    if binding.worker != worker:
        raise ReconcileError("sidecar worker does not match")
    if binding.ready_id is None:
        raise ReconcileError("ready sidecar missing ready_id")
    task = task_path(root, worker)
    if not task.is_file():
        raise ReconcileError(f"missing TASK.md for worker {worker!r}")
    if hash_text(task.read_text(encoding="utf-8")) != binding.task_hash:
        raise ReconcileError("TASK.md hash does not match dispatch binding")
    if payload.get("sidecar_hash") != _sidecar_body_hash(payload):
        raise ReconcileError("dispatch sidecar hash mismatch")
    return binding


def _sidecar_body_hash(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "sidecar_hash"}
    return hash_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _assert_ready_event(project: EulerProject, binding: DispatchBinding) -> None:
    found = False
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != EVENT_DISPATCH_READY:
            continue
        raw = dict(envelope.payload).get("binding")
        if isinstance(raw, Mapping) and raw.get("ready_id") == binding.ready_id:
            found = True
            stored = DispatchBinding.from_dict(raw)
            if stored.to_dict() != binding.to_dict():
                raise ReconcileError("ready event does not match sidecar binding")
            break
    if not found:
        raise ReconcileError("dispatch ready event is missing from EventStore")


def _assert_frontier(
    project: EulerProject,
    binding: DispatchBinding,
    *,
    snapshot=None,
) -> None:
    from .campaign import focused_record
    from .human_objects import resolve_object, assert_same_target
    focused = focused_record(project, binding.candidate_id)
    if focused:
        try:
            assert_same_target(resolve_object(project, focused['target']['object_type'],
                                              focused['target']['object_id']), focused['target'])
        except ValueError as exc:
            raise ReconcileError('source object changed during focused investigation: ' + str(exc)) from exc
    snap = snapshot if snapshot is not None else snapshot_project(project.project_dir)
    view = project.research_view()
    obligation = _bound_obligation(project, binding, view)
    revoked = set(getattr(snap, "revoked_fact_ids", ()) or ())
    active = {item.fact_id: item for item in getattr(snap, "active_facts", ())}
    required_ids = binding.required_fact_ids
    required_versions = binding.required_fact_versions
    if required_ids:
        if required_versions and len(required_versions) != len(required_ids):
            raise ReconcileError("required Fact baseline is internally inconsistent")
        for index, fact_id in enumerate(required_ids):
            if fact_id in revoked:
                raise ReconcileError(f"required Fact {fact_id} has been revoked")
            fact = active.get(fact_id)
            if fact is None:
                raise ReconcileError(f"required Fact {fact_id} is missing")
            if required_versions:
                expected = required_versions[index]
                if fact.content_sha256 != expected:
                    raise ReconcileError(
                        f"required Fact {fact_id} changed since dispatch"
                    )
    for evidence_id in binding.required_evidence_ids:
        artifact = view.get_evidence(evidence_id)
        if artifact is None:
            raise ReconcileError(f"required evidence {evidence_id} is missing")
        validity = view.evidence_validity(evidence_id) or artifact.validity
        if validity is not ValidityStatus.VALID:
            raise ReconcileError(f"required evidence {evidence_id} is no longer VALID")
    if binding.scope_hash:
        current_scope = scope_digest(obligation.scope)
        if current_scope != binding.scope_hash:
            raise ReconcileError("obligation scope drifted since dispatch")
    if binding.obligation_id and binding.obligation_id != obligation.obligation_id:
        raise ReconcileError("binding obligation_id does not match live obligation")
    if binding.frontier_hash and (required_ids or binding.scope_hash):
        current = action_frontier_hash(
            obligation_id=obligation.obligation_id,
            scope_hash=binding.scope_hash or scope_digest(obligation.scope),
            required_facts=tuple(
                (fact_id, active[fact_id].content_sha256) for fact_id in required_ids
            ),
            dependency_hash=binding.dependency_hash or "",
            context_version_hash=binding.context_version_hash or "",
        )
        if current != binding.frontier_hash:
            raise ReconcileError(
                "action-local dependency/scope/context fingerprint does not match"
            )


def _bound_obligation(project: EulerProject, binding: DispatchBinding, view: ResearchView):
    lease = None
    try:
        lease = _require_lease(project, binding)
    except ReconcileError:
        lease = None
    obligation_id = (
        binding.obligation_id
        or (lease.obligation_id if lease is not None else None)
        or _candidate_obligation_id(project, binding.candidate_id)
    )
    if not obligation_id:
        raise ReconcileError("dispatch binding has no target obligation")
    obligation = view.get_obligation(obligation_id)
    if obligation is None:
        raise ReconcileError(
            f"target obligation {obligation_id} missing from ResearchView"
        )
    return obligation


def _candidate_obligation_id(project: EulerProject, candidate_id: str) -> str | None:
    for item in project.load_proposed_candidates():
        if item.candidate.candidate_id == candidate_id:
            return item.candidate.obligation_id
    return None


def _resolve_historical_binding(
    project: EulerProject, root: Path, worker: str, round_id: str
) -> DispatchBinding:
    bound = binding_for_round(project, worker, round_id)
    if bound is not None:
        return bound
    try:
        sidecar = _load_ready_or_settled_binding(root, worker)
    except ReconcileError:
        sidecar = None
    if sidecar is not None:
        existing = _existing_reconciliation(project, sidecar.lease_id, round_id)
        if existing is not None:
            stored = binding_for_lease(project, sidecar.lease_id)
            return stored or sidecar
        lease = next(
            (
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == sidecar.lease_id
            ),
            None,
        )
        if lease is not None and lease.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
            return sidecar
    raise ReconcileError(
        f"no historical dispatch binding for worker {worker!r} round {round_id}"
    )


def _load_ready_or_settled_binding(root: Path, worker: str) -> DispatchBinding:
    payload = load_dispatch(root, worker)
    if payload is None or payload.get("state") not in {"ready", "settled"}:
        raise ReconcileError(f"worker {worker!r} has no ready dispatch sidecar")
    try:
        binding = DispatchBinding.from_dict(payload.get("binding"))
    except Exception as exc:
        raise ReconcileError(f"dispatch sidecar binding is invalid: {exc}") from exc
    if binding.worker != worker:
        raise ReconcileError("sidecar worker does not match")
    if payload.get("state") == "ready" and binding.ready_id is None:
        raise ReconcileError("ready sidecar missing ready_id")
    task = task_path(root, worker)
    if payload.get("state") == "ready":
        if not task.is_file():
            raise ReconcileError(f"missing TASK.md for worker {worker!r}")
        if hash_text(task.read_text(encoding="utf-8")) != binding.task_hash:
            raise ReconcileError("TASK.md hash does not match dispatch binding")
    if payload.get("sidecar_hash") != _sidecar_body_hash(payload):
        raise ReconcileError("dispatch sidecar hash mismatch")
    return binding


def _bind_round(
    project: EulerProject,
    binding: DispatchBinding,
    round_id: str,
    start_payload: Mapping[str, Any],
    *,
    actor: str,
    timestamp_utc: str,
) -> None:
    envelope = EventEnvelope.build(
        project_id=project.project_id,
        actor=actor,
        event_type=EVENT_ROUND_BOUND,
        idempotency_key=f"round-bound:{binding.lease_id}:{round_id}",
        payload={
            "binding": binding.to_dict(),
            "round_id": round_id,
            "lease_id": binding.lease_id,
            "worker": binding.worker,
            "action_id": binding.action_id,
            "start_receipt_hash": start_payload.get("receipt_hash"),
        },
        timestamp_utc=timestamp_utc or "1970-01-01T00:00:00Z",
        correlation_id=binding.lease_id,
    )
    try:
        project.event_store.append(envelope)
    except IdempotencyConflict:
        return


def _new_registered_evidence(view: ResearchView, binding: DispatchBinding, obligation, contract):
    baseline = set(binding.baseline_evidence_ids)
    out = []
    for artifact in view.evidence():
        if artifact.artifact_id in baseline:
            continue
        validity = view.evidence_validity(artifact.artifact_id) or artifact.validity
        if validity is not ValidityStatus.VALID:
            continue
        if artifact.checker_result is not CheckerResult.PASS:
            continue
        if artifact.exact_claim != obligation.exact_statement:
            continue
        if artifact.scope != obligation.scope:
            continue
        if not evidence_satisfies(
            artifact,
            contract.required_evidence_level,
            obligation.scope,
            contract.contract_id,
            exact_claim=obligation.exact_statement,
        ):
            continue
        out.append(artifact)
    return out


def _matching_reduction(view: ResearchView, binding: DispatchBinding, obligation, contract):
    # Legacy receipts without a relation baseline cannot prove that a reduction
    # is new to this action. It remains available in the frontier for reuse.
    if binding.baseline_relation_ids is None:
        return None
    baseline = set(binding.baseline_relation_ids)
    for relation in view.relations():
        if relation.relation_id in baseline:
            continue
        if relation.relation_type not in {
            RelationType.REDUCES_TO,
            RelationType.EQUIVALENT_UNDER,
        }:
            continue
        if relation.validity is not ValidityStatus.VALID:
            continue
        if relation.contract_id != contract.contract_id:
            continue
        if relation.scope != obligation.scope:
            continue
        if obligation.obligation_id not in {relation.source, relation.target}:
            continue
        if not relation.evidence_refs:
            continue
        return relation
    return None


def _proof_dependency_versions(snapshot, fact_id: str) -> dict[str, str]:
    active = {fact.fact_id: fact for fact in snapshot.active_facts}
    versions = {}
    visiting = set()

    def visit(fid):
        if fid in visiting:
            raise ReconcileError("cyclic proof dependency")
        if fid in versions:
            return
        fact = active.get(fid)
        if fact is None:
            raise ReconcileError("missing proof dependency")
        visiting.add(fid)
        for predecessor in fact.predecessors:
            visit(predecessor)
        visiting.remove(fid)
        versions[fid] = fact.content_sha256

    visit(fact_id)
    return versions


def _matching_counterexample(view: ResearchView, binding: DispatchBinding, obligation, contract):
    baseline = set(binding.baseline_evidence_ids)
    for artifact in view.evidence():
        if artifact.artifact_id in baseline:
            continue
        if artifact.evidence_kind is not EvidenceKind.CHECKED_COUNTEREXAMPLE:
            continue
        validity = view.evidence_validity(artifact.artifact_id) or artifact.validity
        if validity is not ValidityStatus.VALID:
            continue
        if artifact.checker_result is not CheckerResult.PASS:
            continue
        if artifact.exact_claim != obligation.exact_statement:
            continue
        if not evidence_satisfies(
            artifact,
            EvidenceKind.CHECKED_COUNTEREXAMPLE,
            obligation.scope,
            contract.contract_id,
            exact_claim=obligation.exact_statement,
        ):
            continue
        return artifact
    return None


def _require_lease(project: EulerProject, binding: DispatchBinding) -> Lease:
    snapshot = project.runtime_snapshot()
    for item in snapshot.leases:
        if item.lease_id == binding.lease_id:
            return item
    raise ReconcileError("runtime has no lease for dispatch binding")


def _assert_lease_matches_binding(lease: Lease, binding: DispatchBinding, worker: str) -> None:
    if lease.candidate_id != binding.candidate_id:
        raise ReconcileError("lease candidate_id does not match binding")
    if lease.action_id != binding.action_id:
        raise ReconcileError("lease action_id does not match binding")
    if lease.attempt_id != binding.attempt_id:
        raise ReconcileError("lease attempt_id does not match binding")
    if binding.worker != worker:
        raise ReconcileError("binding worker does not match")


def _assert_start_launch_link(
    worker: str, start_payload: Mapping[str, Any], launch_payload: Mapping[str, Any]
) -> None:
    if launch_payload.get("kind") != "launch":
        raise ReconcileError("launch receipt kind must be 'launch'")
    if int(start_payload.get("round_id") or 0) != int(launch_payload.get("round_id") or 0):
        raise ReconcileError("start/launch round_id mismatch")
    if launch_payload.get("worker_id") != worker:
        raise ReconcileError("launch worker_id does not match worker")
    if launch_payload.get("start_receipt_hash") != start_payload.get("receipt_hash"):
        raise ReconcileError("launch is not linked to start receipt")
    if start_payload.get("worker_id") not in (None, worker) and start_payload.get(
        "worker_id"
    ) != worker:
        raise ReconcileError("start worker_id does not match worker")
    if start_payload.get("run_id") and launch_payload.get("run_id"):
        if start_payload.get("run_id") != launch_payload.get("run_id"):
            raise ReconcileError("start/launch run_id mismatch")


def _assert_result_end_link(
    worker: str,
    start_payload: Mapping[str, Any],
    launch_payload: Mapping[str, Any],
    result_payload: Mapping[str, Any],
    end_payload: Mapping[str, Any],
) -> None:
    if result_payload.get("kind") != "result":
        raise ReconcileError("result receipt kind must be 'result'")
    if int(result_payload.get("round_id") or 0) != int(launch_payload.get("round_id") or 0):
        raise ReconcileError("result/launch round_id mismatch")
    if result_payload.get("worker_id") != worker:
        raise ReconcileError("result worker_id does not match worker")
    if result_payload.get("launch_receipt_hash") != launch_payload.get("receipt_hash"):
        raise ReconcileError("result is not linked to launch receipt")
    if result_payload.get("run_id") != launch_payload.get("run_id"):
        raise ReconcileError("result/launch run_id mismatch")
    if end_payload.get("start_receipt_hash") != start_payload.get("receipt_hash"):
        raise ReconcileError("end receipt is not linked to start receipt")
    if int(end_payload.get("round_id") or 0) != int(start_payload.get("round_id") or 0):
        raise ReconcileError("end/start round_id mismatch")
    try:
        bind_result_usage_to_launch(result_payload, launch_payload)
    except RoundReceiptError as exc:
        raise ReconcileError(str(exc.reason) if getattr(exc, "reason", None) else str(exc)) from exc


def _usage_from_result(
    payload: Mapping[str, Any],
    launch_payload: Mapping[str, Any] | None = None,
) -> UsageRecord:
    """Build a UsageRecord from a verified result, bound to the launch receipt.

    Native counters are provider-reported. Absent dimensions stay UNKNOWN and
    therefore consume the reservation at settlement. Schema 1 and incomplete
    streams never invent token or cost observations. Model/tool log text is
    not parsed here.
    """
    wall = payload.get("elapsed_ms")
    if isinstance(wall, bool) or not isinstance(wall, int) or wall < 0:
        raise ReconcileError("result elapsed_ms is not a nonnegative int")

    def unknown(unit: str) -> ObservedQuantity:
        return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)

    def observed(unit: str, value: int) -> ObservedQuantity:
        return ObservedQuantity(
            unit=unit, status=ObservationStatus.OBSERVED, value=value
        )

    if launch_payload is not None:
        try:
            bind_result_usage_to_launch(payload, launch_payload)
        except RoundReceiptError as exc:
            raise ReconcileError(
                str(exc.reason) if getattr(exc, "reason", None) else str(exc)
            ) from exc

    input_tokens = unknown("tokens")
    output_tokens = unknown("tokens")
    reasoning_tokens = unknown("tokens")
    cached_tokens = unknown("tokens")
    cost_microusd = unknown("microusd")
    if (
        payload.get("schema_version") != 1
        and payload.get("usage_status") == "provider_reported"
    ):
        observation = payload.get("native_usage")
        if not isinstance(observation, Mapping):
            raise ReconcileError("provider_reported usage is missing native_usage")
        counters = observation.get("counters")
        if not isinstance(counters, Mapping):
            raise ReconcileError("native_usage counters are missing")
        mapping = {
            "input_tokens": "tokens",
            "output_tokens": "tokens",
            "reasoning_tokens": "tokens",
            "cached_tokens": "tokens",
        }
        values: dict[str, ObservedQuantity] = {}
        for name, unit in mapping.items():
            value = counters.get(name)
            if value is None:
                values[name] = unknown(unit)
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReconcileError(f"native_usage {name} is not a nonnegative int")
            else:
                values[name] = observed(unit, value)
        input_tokens = values["input_tokens"]
        output_tokens = values["output_tokens"]
        reasoning_tokens = values["reasoning_tokens"]
        cached_tokens = values["cached_tokens"]
        cost = observation.get("cost_microusd")
        if cost is None:
            cost_microusd = unknown("microusd")
        elif isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ReconcileError("native_usage cost_microusd is not a nonnegative int")
        else:
            cost_microusd = observed("microusd", cost)

    return UsageRecord(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cost_microusd=cost_microusd,
        wall_ms=observed("ms", wall),
        tool_cpu_ms=unknown("ms"),
        tool_gpu_ms=unknown("ms"),
    )


def _require_contract(view: ResearchView, contract_id: str) -> ProblemContract:
    for item in view.contracts():
        if item.contract_id == contract_id:
            return item
    raise ReconcileError("contract missing from ResearchView")


def _target_obligation_id(project: EulerProject, binding: DispatchBinding | None = None) -> str:
    if binding is not None and binding.obligation_id:
        return binding.obligation_id
    return project.manifest.target_obligation_id
