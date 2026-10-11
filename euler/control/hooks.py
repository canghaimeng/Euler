"""Named managed-round orchestration seams for the worker loop.

Lazy-imports execution receipt verifiers. Uninitialized projects return None.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from euler.control.dispatch import load_dispatch
from euler.control.live_records import DispatchBinding, ReconciliationResult
from euler.control.project import is_euler_project, open_project
from euler.control.reconcile import (
    ReconcileError,
    mark_lease_running,
    reconcile_managed_round,
)
from euler.control.runtime import LeaseStatus

__all__ = [
    "ReconcileError",
    "ManagedActionState",
    "before_managed_round",
    "after_managed_round",
    "inspect_managed_action",
    "recover_pending_managed_round",
]

_DISPOSITION_PROCEED = "proceed"
_DISPOSITION_WAIT = "waiting_dispatch"
_DISPOSITION_RECOVERED = "recovered"
_DISPOSITION_INTERRUPTED = "interrupted"
_DISPOSITION_UNMANAGED = "unmanaged"


def before_managed_round(
    worker_layout: Any,
    start_receipt: Any,
    launch_receipt: Any,
) -> Optional[DispatchBinding]:
    """Ready validation + RUNNING lease. None when the project is unmanaged."""
    project_dir = Path(worker_layout.project_dir)
    if not is_euler_project(project_dir):
        return None
    start_payload = _mapping(getattr(start_receipt, "payload", start_receipt))
    launch_payload = _mapping(getattr(launch_receipt, "payload", launch_receipt))
    return mark_lease_running(
        project_dir,
        worker_layout.name,
        start_payload=start_payload,
        launch_payload=launch_payload,
        timestamp_utc=str(launch_payload.get("created_at") or "") or None,
    )


def after_managed_round(
    worker_layout: Any,
    start: Any,
    launch: Any,
    result_payload: Mapping[str, Any],
    end_path: str | Path,
    *,
    auto_final_audit: bool = False,
    final_audit_transport: Any = None,
) -> Optional[ReconciliationResult]:
    """Reconcile one managed round. None when the project is unmanaged."""
    project_dir = Path(worker_layout.project_dir)
    if not is_euler_project(project_dir):
        return None
    start_payload = _mapping(getattr(start, "payload", start))
    launch_payload = _mapping(getattr(launch, "payload", launch))
    result = _verify_result(result_payload)
    end_payload = _verify_end(end_path)
    reconciled = reconcile_managed_round(
        project_dir,
        worker_layout.name,
        start_payload=start_payload,
        launch_payload=launch_payload,
        result_payload=result,
        end_payload=end_payload,
        timestamp_utc=str(result.get("completed_at") or "") or None,
    )
    if auto_final_audit and reconciled.outcome_ref == "OBLIGATION_DISCHARGED":
        from euler.control.final_audit import FinalAuditError, run_final_audit
        from euler.control.ids import sha256_hex
        from euler.control.obligations import ObligationStatus
        from euler.control.schema import EventEnvelope

        project = open_project(project_dir)
        try:
            root = project.research_view().get_obligation(
                project.manifest.target_obligation_id
            )
            if root is not None and root.status is ObligationStatus.DISCHARGED:
                try:
                    run_final_audit(
                        project,
                        transport=final_audit_transport,
                        timestamp_utc=str(result.get("completed_at") or "") or None,
                    )
                except FinalAuditError as exc:
                    # The completed worker round remains reconciled even when
                    # an independent audit needs configuration or repair.
                    detail = str(exc)
                    project.event_store.append(EventEnvelope.build(
                        project_id=project.project_id, actor="managed-round",
                        event_type="euler.auto_final_audit_blocked",
                        idempotency_key=(f"auto-final-audit-blocked:{reconciled.reconciliation_id}:"
                                         f"{sha256_hex(detail)}"),
                        payload={"obligation_id": root.obligation_id,
                                 "reconciliation_id": reconciled.reconciliation_id,
                                 "reason": detail},
                        timestamp_utc=str(result.get("completed_at") or "") or None,
                    ))
        finally:
            project.close()
    return reconciled


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedActionState:
    disposition: str
    lease_status: Optional[str] = None
    lease_id: Optional[str] = None
    reason: str = ""
    reconciliation: Optional[ReconciliationResult] = None


def inspect_managed_action(worker_layout: Any) -> ManagedActionState:
    """Honest loop disposition for a worker. Never invents receipts."""
    project_dir = Path(worker_layout.project_dir)
    if not is_euler_project(project_dir):
        return ManagedActionState(disposition=_DISPOSITION_UNMANAGED)
    payload = load_dispatch(project_dir, worker_layout.name)
    if payload is None:
        return ManagedActionState(
            disposition=_DISPOSITION_WAIT,
            reason="no_dispatch",
        )
    state = payload.get("state")
    raw = payload.get("binding")
    if not isinstance(raw, Mapping):
        return ManagedActionState(
            disposition=_DISPOSITION_WAIT,
            reason="invalid_sidecar",
        )
    project = open_project(project_dir)
    try:
        lease_id = str(raw.get("lease_id") or "")
        lease = next(
            (
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == lease_id
            ),
            None,
        )
        if lease is None:
            return ManagedActionState(
                disposition=_DISPOSITION_WAIT,
                reason="lease_missing",
            )
        if lease.status.value not in {LeaseStatus.ISSUED.value, LeaseStatus.RUNNING.value}:
            return ManagedActionState(
                disposition=_DISPOSITION_WAIT,
                lease_status=lease.status.value,
                lease_id=lease.lease_id,
                reason="lease_terminal",
            )
        if state == "settled":
            return ManagedActionState(
                disposition=_DISPOSITION_WAIT,
                lease_status=lease.status.value,
                lease_id=lease.lease_id,
                reason="sidecar_settled",
            )
        if state == "ready" and lease.status is LeaseStatus.ISSUED:
            return ManagedActionState(
                disposition=_DISPOSITION_PROCEED,
                lease_status=lease.status.value,
                lease_id=lease.lease_id,
            )
        if lease.status is LeaseStatus.RUNNING:
            return ManagedActionState(
                disposition=_DISPOSITION_INTERRUPTED,
                lease_status=lease.status.value,
                lease_id=lease.lease_id,
                reason="running_without_reconcile",
            )
        return ManagedActionState(
            disposition=_DISPOSITION_WAIT,
            lease_status=lease.status.value,
            lease_id=lease.lease_id,
            reason="not_ready",
        )
    finally:
        project.close()


def recover_pending_managed_round(worker_layout: Any) -> ManagedActionState:
    """Reconcile a complete receipt chain for the current lease, or wait honestly.

    Incomplete receipts are not invented. A distinct coordinator dispatch must
    issue a new attempt/lease for transient recovery.
    """
    inspected = inspect_managed_action(worker_layout)
    if inspected.disposition in {_DISPOSITION_UNMANAGED, _DISPOSITION_PROCEED, _DISPOSITION_WAIT}:
        return inspected
    chain = _complete_receipt_chain(worker_layout)
    if chain is None:
        return inspected
    start, launch, result, end_path = chain
    try:
        reconciled = after_managed_round(
            worker_layout,
            start,
            launch,
            result,
            end_path,
            auto_final_audit=True,
        )
    except ReconcileError as exc:
        return ManagedActionState(
            disposition=_DISPOSITION_INTERRUPTED,
            lease_status=inspected.lease_status,
            lease_id=inspected.lease_id,
            reason=str(exc),
        )
    return ManagedActionState(
        disposition=_DISPOSITION_RECOVERED,
        lease_status=LeaseStatus.COMPLETED.value,
        lease_id=inspected.lease_id,
        reason="reconciled_existing_receipts",
        reconciliation=reconciled,
    )


def _complete_receipt_chain(
    worker_layout: Any,
) -> Optional[tuple[Any, Any, Mapping[str, Any], Path]]:
    from euler.execution.launch_receipt import (
        launch_receipt_path,
        read_and_verify_launch,
        read_and_verify_result,
        result_receipt_path,
    )
    from euler.execution.memory_digest import (
        RoundReceiptError,
        end_receipt_path,
        read_and_verify_end,
        read_and_verify_start,
        start_receipt_path,
    )

    receipts = Path(worker_layout.receipts)
    if not receipts.is_dir():
        return None
    rounds: list[int] = []
    for path in receipts.glob("round_*.start.json"):
        try:
            rounds.append(int(path.name.split("_")[1].split(".")[0]))
        except (IndexError, ValueError):
            continue
    for round_id in sorted(rounds, reverse=True):
        start_path = start_receipt_path(worker_layout, round_id)
        launch_path = launch_receipt_path(worker_layout, round_id)
        result_path = result_receipt_path(worker_layout, round_id)
        end_path = end_receipt_path(worker_layout, round_id)
        if not all(p.is_file() for p in (start_path, launch_path, result_path, end_path)):
            continue
        try:
            start = read_and_verify_start(start_path)
            launch = read_and_verify_launch(launch_path)
            result = read_and_verify_result(result_path)
            read_and_verify_end(end_path)
        except (RoundReceiptError, OSError, ValueError, KeyError):
            continue
        return start, launch, result, end_path
    return None


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise ReconcileError("receipt payload must be a JSON object")
    return value


def _verify_result(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    from euler.execution.launch_receipt import verify_result_payload

    verify_result_payload(payload)
    return payload


def _verify_end(end_path: str | Path) -> Mapping[str, Any]:
    from euler.execution.memory_digest import read_and_verify_end

    return read_and_verify_end(Path(end_path))
