"""Atomic TASK/sidecar helpers and managed-start validation.

Stdlib plus Euler control records. Does not launch providers. File writes are
contained under the project directory. Recovery cancels a pre-start lease when
an intent exists without a matching ready sidecar/event.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from euler.control.ids import sha256_hex
from euler.control.live_records import (
    EVENT_DISPATCH_INTENT,
    EVENT_DISPATCH_READY,
    EVENT_ROUND_BOUND,
    EVENT_ROUND_RECONCILED,
    DispatchBinding,
    ReconciliationResult,
)
from euler.control.project import is_euler_project, open_project
from euler.control.runtime import LeaseStatus

_SIDECAR_SCHEMA = 1
_STATE_INTENT = "intent"
_STATE_READY = "ready"
_STATE_SETTLED = "settled"
_SIDECAR_STATES = frozenset({_STATE_INTENT, _STATE_READY, _STATE_SETTLED})

__all__ = [
    "DispatchError",
    "DispatchUnprepared",
    "sidecar_path",
    "task_path",
    "atomic_write_task_and_sidecar",
    "atomic_write_sidecar",
    "hash_text",
    "load_dispatch",
    "validate_dispatch",
    "validate_managed_start",
    "recover_unready_intent",
    "write_managed_task",
    "binding_for_lease",
    "binding_for_round",
    "mark_sidecar_settled",
]


class DispatchError(ValueError):
    """Managed dispatch files or EventStore bindings are unusable."""


class DispatchUnprepared(DispatchError):
    """Worker has no ready dispatch binding and must not start."""


def hash_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def sidecar_path(project_dir: str | Path, worker: str) -> Path:
    root = Path(project_dir)
    path = (root / "control" / "dispatch" / f"{worker}.json").resolve()
    _contained(root.resolve(), path)
    return root / "control" / "dispatch" / f"{worker}.json"


def task_path(project_dir: str | Path, worker: str) -> Path:
    from euler.execution.layout import WorkerLayout

    root = Path(project_dir)
    wl = WorkerLayout(root / "workers" / worker)
    _contained(root.resolve(), wl.task.resolve() if wl.task.exists() else wl.dir.resolve())
    return wl.task


def atomic_write_sidecar(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_replace(path, text)


def atomic_write_task_and_sidecar(
    task: Path,
    task_text: str,
    sidecar: Path,
    sidecar_payload: Mapping[str, Any],
) -> None:
    sidecar_text = (
        json.dumps(dict(sidecar_payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    _atomic_replace_pair((task, task_text), (sidecar, sidecar_text))


def write_managed_task(project_dir: str | Path, worker: str, text: str) -> Path:
    """Internal controller TASK write. Not the public ``euler assign`` path."""
    path = task_path(project_dir, worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace(path, text)
    return path


def load_dispatch(project_dir: str | Path, worker: str) -> dict[str, Any] | None:
    path = sidecar_path(project_dir, worker)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"unreadable dispatch sidecar {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DispatchError(f"dispatch sidecar is not an object: {path}")
    return payload


def validate_dispatch(
    project_dir: str | Path,
    worker: str,
    *,
    require_ready: bool = False,
) -> DispatchBinding:
    """Check sidecar + TASK against EventStore/runtime. Fail closed on mismatch."""
    root = Path(project_dir)
    if not is_euler_project(root):
        raise DispatchError(f"not an Euler-managed project: {root}")
    payload = load_dispatch(root, worker)
    if payload is None:
        raise DispatchUnprepared(f"no dispatch sidecar for worker {worker!r}")
    state = payload.get("state")
    if state not in _SIDECAR_STATES:
        raise DispatchError(f"dispatch sidecar has invalid state {state!r}")
    if require_ready and state != _STATE_READY:
        raise DispatchUnprepared(f"worker {worker!r} dispatch is not ready")
    if state == _STATE_SETTLED and require_ready:
        raise DispatchUnprepared(
            f"worker {worker!r} dispatch already settled; waiting for re-dispatch"
        )
    raw_binding = payload.get("binding")
    try:
        binding = DispatchBinding.from_dict(raw_binding)
    except Exception as exc:
        raise DispatchError(f"dispatch sidecar binding is invalid: {exc}") from exc
    if binding.worker != worker:
        raise DispatchError("sidecar worker does not match path")
    task = task_path(root, worker)
    if not task.is_file():
        raise DispatchError(f"missing TASK.md for worker {worker!r}")
    text = task.read_text(encoding="utf-8")
    if hash_text(text) != binding.task_hash:
        raise DispatchError("TASK.md hash does not match dispatch binding")
    if payload.get("sidecar_hash") != _sidecar_body_hash(payload):
        raise DispatchError("dispatch sidecar hash mismatch")
    project = open_project(root)
    try:
        intent = _find_event(
            project, EVENT_DISPATCH_INTENT, binding.intent_id, "intent_id"
        )
        if intent is None:
            raise DispatchError("dispatch intent is missing from EventStore")
        stored = DispatchBinding.from_dict(dict(intent.payload)["binding"])
        if stored.intent_id != binding.intent_id or stored.lease_id != binding.lease_id:
            raise DispatchError("sidecar binding does not match EventStore intent")
        if stored.candidate_id != binding.candidate_id:
            raise DispatchError("sidecar candidate_id does not match EventStore")
        stored_fields = {key: value for key, value in stored.to_dict().items() if key != "ready_id"}
        live_fields = {key: value for key, value in binding.to_dict().items() if key != "ready_id"}
        if stored_fields != live_fields:
            raise DispatchError("sidecar metadata differs from authoritative dispatch intent")
        snapshot = project.runtime_snapshot()
        lease = next((item for item in snapshot.leases if item.lease_id == binding.lease_id), None)
        if lease is None:
            raise DispatchError("runtime has no lease for dispatch binding")
        if lease.candidate_id != binding.candidate_id or lease.action_id != binding.action_id:
            raise DispatchError("lease identity does not match dispatch binding")
        if state == _STATE_READY:
            if binding.ready_id is None:
                raise DispatchError("ready sidecar missing ready_id")
            ready = _find_event(
                project, EVENT_DISPATCH_READY, binding.ready_id, "ready_id"
            )
            if ready is None:
                raise DispatchError("dispatch ready event is missing from EventStore")
            if DispatchBinding.from_dict(dict(ready.payload)["binding"]).to_dict() != binding.to_dict():
                raise DispatchError("sidecar binding differs from authoritative ready event")
            if lease.status is not LeaseStatus.ISSUED:
                raise DispatchError(
                    f"ready dispatch lease is {lease.status.value}, expected ISSUED"
                )
        else:
            if binding.ready_id is not None:
                raise DispatchError("intent sidecar must not carry ready_id")
    finally:
        project.close()
    return binding


def recover_unready_intent(
    project_dir: str | Path,
    worker: str,
    *,
    actor: str = "controller",
    timestamp_utc: str | None = None,
) -> Optional[DispatchBinding]:
    """Cancel a pre-start lease when intent exists without a ready sidecar/event."""
    root = Path(project_dir)
    if not is_euler_project(root):
        return None
    payload = None
    try:
        payload = load_dispatch(root, worker)
    except DispatchError:
        payload = None
    project = open_project(root)
    try:
        binding = None
        if payload is not None and payload.get("state") == _STATE_INTENT:
            try:
                binding = DispatchBinding.from_dict(payload.get("binding"))
            except Exception:
                binding = None
        if binding is None:
            binding = _latest_intent_for_worker(project, worker)
        if binding is None:
            return None
        ready = _find_event(project, EVENT_DISPATCH_READY, binding.intent_id, "intent_id")
        if ready is not None:
            return None
        snapshot = project.runtime_snapshot()
        lease = next((item for item in snapshot.leases if item.lease_id == binding.lease_id), None)
        if lease is not None and lease.status is LeaseStatus.ISSUED:
            project.runtime.cancel(
                project.project_id,
                lease.lease_id,
                actor=actor,
                timestamp_utc=timestamp_utc,
            )
        path = sidecar_path(root, worker)
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        return binding
    finally:
        project.close()


def validate_managed_start(project_dir: str | Path, worker: str) -> DispatchBinding:
    """Require a current ready dispatch before preflight/provider launch."""
    root = Path(project_dir)
    if not is_euler_project(root):
        raise DispatchError(f"not an Euler-managed project: {root}")
    recover_unready_intent(root, worker)
    payload = load_dispatch(root, worker)
    if payload is None or payload.get("state") != _STATE_READY:
        raise DispatchUnprepared(
            f"worker {worker!r} has no ready Euler dispatch; refusing start"
        )
    binding = validate_dispatch(root, worker, require_ready=True)
    project = open_project(root)
    try:
        from .campaign import assert_dispatch_allowed
        try:
            assert_dispatch_allowed(project, binding.candidate_id)
        except ValueError as exc:
            raise DispatchUnprepared(str(exc)) from exc
        _assert_unique_worker_binding(project, worker, binding)
    finally:
        project.close()
    return binding


def start_managed_worker(project_dir: str | Path, worker: str) -> str:
    """Call the existing start helper only after a ready dispatch validates."""
    validate_managed_start(project_dir, worker)
    from euler.execution.layout import WorkerLayout
    from euler.orchestration.cli import _start_one

    wl = WorkerLayout(Path(project_dir) / "workers" / worker)
    return _start_one(wl)


def sidecar_document(
    *,
    state: str,
    binding: DispatchBinding,
    plan_hash: str,
    packet_artifact_hash: str,
    reconciliation_id: str | None = None,
) -> dict[str, Any]:
    if state not in _SIDECAR_STATES:
        raise DispatchError(f"invalid sidecar state {state!r}")
    body = {
        "schema_version": _SIDECAR_SCHEMA,
        "state": state,
        "plan_hash": plan_hash,
        "packet_artifact_hash": packet_artifact_hash,
        "binding": binding.to_dict(),
    }
    if reconciliation_id:
        body["reconciliation_id"] = reconciliation_id
    body["sidecar_hash"] = _sidecar_body_hash(body)
    return body


def mark_sidecar_settled(
    project_dir: str | Path,
    worker: str,
    binding: DispatchBinding,
    record: ReconciliationResult,
) -> None:
    """Mark the current sidecar settled only if it still names this lease."""
    root = Path(project_dir)
    payload = load_dispatch(root, worker)
    if payload is None:
        return
    raw = payload.get("binding")
    if not isinstance(raw, Mapping) or raw.get("lease_id") != binding.lease_id:
        return
    if payload.get("state") not in {_STATE_READY, _STATE_SETTLED}:
        return
    settled = sidecar_document(
        state=_STATE_SETTLED,
        binding=binding,
        plan_hash=str(payload.get("plan_hash") or ""),
        packet_artifact_hash=str(payload.get("packet_artifact_hash") or ""),
        reconciliation_id=record.reconciliation_id,
    )
    atomic_write_sidecar(sidecar_path(root, worker), settled)


def binding_for_lease(project: Any, lease_id: str) -> Optional[DispatchBinding]:
    """Return the EventStore ready/intent binding for a lease, never the latest sidecar."""
    found: Optional[DispatchBinding] = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type not in {EVENT_DISPATCH_INTENT, EVENT_DISPATCH_READY}:
            continue
        raw = dict(envelope.payload).get("binding")
        if not isinstance(raw, Mapping):
            continue
        if raw.get("lease_id") != lease_id:
            continue
        found = DispatchBinding.from_dict(raw)
    return found


def binding_for_round(
    project: Any,
    worker: str,
    round_id: str,
) -> Optional[DispatchBinding]:
    """Resolve the binding that actually ran this worker round.

    Prefers the immutable ``euler.round_bound`` event, then a reconciliation
    record for the same worker/round, then a lease-scoped ready event. The
    mutable latest sidecar is not used for historical lookup.
    """
    wanted = str(round_id)
    found: Optional[DispatchBinding] = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        payload = dict(envelope.payload)
        if envelope.event_type == EVENT_ROUND_BOUND:
            if str(payload.get("round_id")) != wanted:
                continue
            raw = payload.get("binding")
            if not isinstance(raw, Mapping):
                continue
            binding = DispatchBinding.from_dict(raw)
            if binding.worker == worker:
                found = binding
            continue
        if envelope.event_type == EVENT_ROUND_RECONCILED:
            raw_rec = payload.get("reconciliation")
            raw_bind = payload.get("binding")
            if not isinstance(raw_rec, Mapping) or not isinstance(raw_bind, Mapping):
                continue
            record = ReconciliationResult.from_dict(raw_rec)
            if record.round_id != wanted:
                continue
            binding = DispatchBinding.from_dict(raw_bind)
            if binding.worker == worker:
                found = binding
    return found


def _sidecar_body_hash(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "sidecar_hash"}
    return hash_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _find_event(project: Any, event_type: str, identity: str, field: str):
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != event_type:
            continue
        payload = dict(envelope.payload)
        binding_raw = payload.get("binding")
        if isinstance(binding_raw, Mapping):
            if binding_raw.get(field) == identity or binding_raw.get("intent_id") == identity:
                return envelope
        if payload.get(field) == identity:
            return envelope
    return None


def _latest_intent_for_worker(project: Any, worker: str) -> Optional[DispatchBinding]:
    found = None
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != EVENT_DISPATCH_INTENT:
            continue
        raw = dict(envelope.payload).get("binding")
        if not isinstance(raw, Mapping):
            continue
        binding = DispatchBinding.from_dict(raw)
        if binding.worker == worker:
            found = binding
    return found


def _assert_unique_worker_binding(project: Any, worker: str, binding: DispatchBinding) -> None:
    snapshot = project.runtime_snapshot()
    active = [
        item
        for item in snapshot.leases
        if item.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}
        and _lease_worker(project, item.lease_id) == worker
        and item.lease_id != binding.lease_id
    ]
    if active:
        raise DispatchError(f"worker {worker!r} already has an active dispatch lease")


def _lease_worker(project: Any, lease_id: str) -> str | None:
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type != EVENT_DISPATCH_INTENT:
            continue
        raw = dict(envelope.payload).get("binding")
        if isinstance(raw, Mapping) and raw.get("lease_id") == lease_id:
            return str(raw.get("worker"))
    return None


def _contained(root: Path, path: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise DispatchError(f"path escapes project directory: {path}") from exc


def _atomic_replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _atomic_replace_pair(
    first: tuple[Path, str],
    second: tuple[Path, str],
) -> None:
    written: list[Path] = []
    try:
        for path, text in (first, second):
            _atomic_replace(path, text)
            written.append(path)
    except Exception:
        raise
