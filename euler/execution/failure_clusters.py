# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Project-level failure-cluster detector + synthesis-request ledger (stage 4).

Infrastructure only: this module does **not** decide that a mathematical route is
dead and never auto-creates a confirmed ``dead_end``. It clusters NEW stamped
records, queues at most one synthesis request per cluster, and checks that the
assigned worker persisted a ``key_failures_summary`` obstacle or dead_end
*candidate*. The mathematical conclusion stays with the worker.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from euler.core import FactGraph, GlobalMemory
from euler.core._util import utc_now

from . import layout as L
from .memory_digest import canonical_json
from .round_contract import _context_match, _load_appended

SCHEMA_VERSION = 1

TRIGGER_BLOCKED_ATTEMPTS = "blocked_attempts"
TRIGGER_APPROACH_SIGNATURE = "approach_signature"
TRIGGER_WRONG_VERIFICATIONS = "wrong_verifications"
TRIGGER_OBSTACLE_SIGNATURE = "obstacle_signature"

TRIGGERS = (
    TRIGGER_BLOCKED_ATTEMPTS,
    TRIGGER_APPROACH_SIGNATURE,
    TRIGGER_WRONG_VERIFICATIONS,
    TRIGGER_OBSTACLE_SIGNATURE,
)

STATES = ("requested", "resolved", "superseded")
FAILED_ATTEMPT_RESULTS = frozenset({"blocked", "abandoned"})
COUNTABLE_VERDICTS = frozenset({"correct", "wrong"})
SYNTHESIS_RECORD_TYPE = "key_failures_summary"

STAMP_KEYS = ("project_id", "worker_id", "run_id", "branch_id", "subgoal_id")

_SIG_NOISE = re.compile(r"[^a-z0-9\s_-]+")
_WS = re.compile(r"\s+")

_DEFAULTS = {
    "EULER_FAIL_CLUSTER_BLOCKED_ATTEMPTS": 3,
    "EULER_FAIL_CLUSTER_APPROACH_WORKERS": 2,
    "EULER_FAIL_CLUSTER_WRONG_VERIFICATIONS": 3,
    "EULER_FAIL_CLUSTER_OBSTACLE_WORKERS": 2,
}


class FailureSynthesisError(Exception):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details

    def to_status_dict(self) -> Dict[str, Any]:
        return {"ok": False, "reason": self.reason, **self.details}


class FailureControlError(Exception):
    """Fail-closed error for the project-owned synthesis control ledger."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details

    def to_status_dict(self) -> Dict[str, Any]:
        return {"ok": False, "reason": self.reason, **self.details}


def _int_env(name: str) -> int:
    default = _DEFAULTS[name]
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def thresholds() -> Dict[str, int]:
    return {
        TRIGGER_BLOCKED_ATTEMPTS: _int_env("EULER_FAIL_CLUSTER_BLOCKED_ATTEMPTS"),
        TRIGGER_APPROACH_SIGNATURE: _int_env("EULER_FAIL_CLUSTER_APPROACH_WORKERS"),
        TRIGGER_WRONG_VERIFICATIONS: _int_env("EULER_FAIL_CLUSTER_WRONG_VERIFICATIONS"),
        TRIGGER_OBSTACLE_SIGNATURE: _int_env("EULER_FAIL_CLUSTER_OBSTACLE_WORKERS"),
    }


def failures_dir(project_dir: Path) -> Path:
    return Path(project_dir) / L.FAILURES_DIR


def ledger_path(project_dir: Path) -> Path:
    return failures_dir(project_dir) / "ledger.jsonl"


def request_path(project_dir: Path, cluster_id: str) -> Path:
    return failures_dir(project_dir) / "requests" / f"{cluster_id}.json"


def normalize_signature(text: Any) -> str:
    s = (text if isinstance(text, str) else "") or ""
    s = s.strip().lower()
    s = _WS.sub(" ", s)
    s = _SIG_NOISE.sub("", s)
    s = _WS.sub(" ", s).strip()
    return s[:256]


def is_stamped(entry: Mapping[str, Any]) -> bool:
    for k in STAMP_KEYS:
        v = entry.get(k)
        if not isinstance(v, str) or not v.strip():
            return False
    return True


def cluster_id_for(
    *,
    project_id: str,
    trigger: str,
    signature: str = "",
    branch_id: str = "",
    subgoal_id: str = "",
) -> str:
    payload = {
        "v": SCHEMA_VERSION,
        "project_id": project_id,
        "trigger": trigger,
        "signature": signature or "",
        "branch_id": branch_id or "",
        "subgoal_id": subgoal_id or "",
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def _ids(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for e in entries:
        eid = e.get("id")
        if isinstance(eid, str) and eid and eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


def _workers(entries: Sequence[Mapping[str, Any]]) -> List[str]:
    return sorted({e["worker_id"] for e in entries if isinstance(e.get("worker_id"), str)})


def _sort_key(entry: Mapping[str, Any]) -> Tuple[str, str]:
    ts = entry.get("timestamp_utc") if isinstance(entry.get("timestamp_utc"), str) else ""
    eid = entry.get("id") if isinstance(entry.get("id"), str) else ""
    return (ts, eid)


def _memory_watermark(project_dir: Path) -> Dict[str, Any]:
    gm = Path(project_dir) / "global_memory"
    files: Dict[str, int] = {}
    total = 0
    for kind in ("proof_attempt", "obstacle", "dead_end", "verification"):
        p = gm / f"{kind}.jsonl"
        size = p.stat().st_size if p.is_file() else 0
        files[kind] = size
        total += size
    return {"byte_size": total, "files": files}


def _load_stamped(gm: GlobalMemory, kind: str) -> List[Dict[str, Any]]:
    return [e for e in gm.read(kind) if is_stamped(e)]


def _cluster_payload(
    *,
    trigger: str,
    signature: str,
    project_id: str,
    branch_id: str,
    subgoal_id: str,
    members: Sequence[Mapping[str, Any]],
    attempt_ids: Optional[Sequence[str]] = None,
    verification_ids: Optional[Sequence[str]] = None,
    obstacle_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    cid = cluster_id_for(
        project_id=project_id,
        trigger=trigger,
        signature=signature,
        branch_id=branch_id,
        subgoal_id=subgoal_id,
    )
    return {
        "cluster_id": cid,
        "trigger": trigger,
        "signature": signature,
        "project_id": project_id,
        "branch_id": branch_id,
        "subgoal_id": subgoal_id,
        "attempt_ids": list(attempt_ids or []),
        "verification_ids": list(verification_ids or []),
        "obstacle_ids": list(obstacle_ids or []),
        "workers": _workers(members),
        "member_ids": _ids(members),
    }


def scan_clusters(project_dir: Path) -> List[Dict[str, Any]]:
    """Deterministic fired clusters over NEW stamped records only."""
    gm = GlobalMemory(project_dir)
    attempts = _load_stamped(gm, "proof_attempt")
    obstacles = _load_stamped(gm, "obstacle")
    verifs = _load_stamped(gm, "verification")
    thr = thresholds()
    found: List[Dict[str, Any]] = []

    failed_attempts = [
        e for e in attempts if e.get("result") in FAILED_ATTEMPT_RESULTS
    ]

    # (a) same subgoal >= N blocked/abandoned proof_attempts
    by_sub: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for e in failed_attempts:
        key = (e["project_id"], e["branch_id"], e["subgoal_id"])
        by_sub.setdefault(key, []).append(e)
    need_a = thr[TRIGGER_BLOCKED_ATTEMPTS]
    for (proj, branch, subgoal), group in by_sub.items():
        if len(group) < need_a:
            continue
        found.append(_cluster_payload(
            trigger=TRIGGER_BLOCKED_ATTEMPTS, signature="",
            project_id=proj, branch_id=branch, subgoal_id=subgoal,
            members=group, attempt_ids=_ids(group),
        ))

    # (b) same approach signature failed by >= N distinct workers
    by_approach: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for e in failed_attempts:
        sig = normalize_signature(e.get("approach"))
        if not sig:
            continue
        by_approach.setdefault((e["project_id"], sig), []).append(e)
    need_b = thr[TRIGGER_APPROACH_SIGNATURE]
    for (proj, sig), group in by_approach.items():
        if len(_workers(group)) < need_b:
            continue
        found.append(_cluster_payload(
            trigger=TRIGGER_APPROACH_SIGNATURE, signature=sig,
            project_id=proj, branch_id="", subgoal_id="",
            members=group, attempt_ids=_ids(group),
        ))

    # (c) same subgoal >= N consecutive ok+wrong verifications (infra skipped)
    by_v: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for e in verifs:
        key = (e["project_id"], e["branch_id"], e["subgoal_id"])
        by_v.setdefault(key, []).append(e)
    need_c = thr[TRIGGER_WRONG_VERIFICATIONS]
    for (proj, branch, subgoal), group in by_v.items():
        countable = [
            e for e in sorted(group, key=_sort_key)
            if e.get("transport_status") == "ok"
            and e.get("verdict") in COUNTABLE_VERDICTS
        ]
        streak: List[Dict[str, Any]] = []
        hit: List[Dict[str, Any]] = []
        for e in countable:
            if e.get("verdict") == "wrong":
                streak.append(e)
                if len(streak) >= need_c:
                    hit = list(streak)
            else:
                streak = []
        if len(hit) < need_c:
            continue
        found.append(_cluster_payload(
            trigger=TRIGGER_WRONG_VERIFICATIONS, signature="",
            project_id=proj, branch_id=branch, subgoal_id=subgoal,
            members=hit, verification_ids=_ids(hit),
        ))

    # (d) same obstacle signature encountered by >= N distinct workers
    by_obs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for e in obstacles:
        sig = normalize_signature(e.get("claim"))
        if not sig:
            continue
        by_obs.setdefault((e["project_id"], sig), []).append(e)
    need_d = thr[TRIGGER_OBSTACLE_SIGNATURE]
    for (proj, sig), group in by_obs.items():
        if len(_workers(group)) < need_d:
            continue
        found.append(_cluster_payload(
            trigger=TRIGGER_OBSTACLE_SIGNATURE, signature=sig,
            project_id=proj, branch_id="", subgoal_id="",
            members=group, obstacle_ids=_ids(group),
        ))

    found.sort(key=lambda c: (c["trigger"], c["cluster_id"]))
    return found


def _read_ledger(project_dir: Path) -> List[Dict[str, Any]]:
    path = ledger_path(project_dir)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _append_ledger(project_dir: Path, event: Mapping[str, Any]) -> None:
    path = ledger_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(event), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_request_exclusive(path: Path, obj: Mapping[str, Any]) -> bool:
    """Return True if created, False if it already existed. Never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(dict(obj), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError:
        return False
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short write while persisting synthesis request")
            remaining = remaining[written:]
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    return True


def _read_request_strict(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FailureControlError(
            "existing synthesis request is unreadable or corrupt",
            path=str(path), error=str(exc),
        ) from exc
    if not isinstance(obj, dict):
        raise FailureControlError(
            "existing synthesis request is not a JSON object", path=str(path),
        )
    return obj


def _validate_request_for_cluster(
    request: Mapping[str, Any], cluster: Mapping[str, Any], *, path: Path,
) -> None:
    """Validate the immutable sidecar before using it to heal the ledger."""
    cid = cluster.get("cluster_id")
    exact = {
        "schema_version": SCHEMA_VERSION,
        "op": "requested",
        "state": "requested",
        "cluster_id": cid,
        "trigger": cluster.get("trigger"),
        "signature": cluster.get("signature") or "",
        "project_id": cluster.get("project_id"),
        "attempt_ids": list(cluster.get("attempt_ids") or []),
        "verification_ids": list(cluster.get("verification_ids") or []),
        "obstacle_ids": list(cluster.get("obstacle_ids") or []),
        "workers": list(cluster.get("workers") or []),
        "member_ids": list(cluster.get("member_ids") or []),
    }
    mismatches = {
        key: {"expected": want, "actual": request.get(key)}
        for key, want in exact.items() if request.get(key) != want
    }
    for key in ("branch_id", "subgoal_id", "assigned_worker", "created_at"):
        if not isinstance(request.get(key), str) or not request.get(key).strip():
            mismatches[key] = {"expected": "nonempty string", "actual": request.get(key)}
    if not isinstance(request.get("watermark"), dict):
        mismatches["watermark"] = {
            "expected": "object", "actual": request.get("watermark"),
        }
    if mismatches:
        raise FailureControlError(
            "existing synthesis request does not match detected cluster",
            cluster_id=cid, path=str(path), mismatches=mismatches,
        )


def _validate_request_is_live_prefix(
    request: Mapping[str, Any], cluster: Mapping[str, Any], *, path: Path,
) -> None:
    """Validate an immutable request snapshot against a growing live cluster.

    Global memory is append-only, so a cluster may acquire additional members
    after its synthesis request has been persisted. The request remains the
    exact evidence snapshot assigned to the synthesis worker; it must not be
    rewritten merely because the live cluster grew. Scalar identity fields
    must still match, typed/member ID lists must be chronological prefixes of
    the live lists, and the original worker set must remain a subset of the
    live worker set.
    """
    cid = cluster.get("cluster_id")
    mismatches: Dict[str, Any] = {}

    live_scalars = {
        "schema_version": SCHEMA_VERSION,
        "op": "requested",
        "state": "requested",
        "cluster_id": cid,
        "trigger": cluster.get("trigger"),
        "signature": cluster.get("signature") or "",
        "project_id": cluster.get("project_id"),
    }
    for key, want in live_scalars.items():
        if request.get(key) != want:
            mismatches[key] = {"expected": want, "actual": request.get(key)}

    for key in ("attempt_ids", "verification_ids", "obstacle_ids", "member_ids"):
        snapshot = request.get(key)
        live = cluster.get(key)
        if not isinstance(snapshot, list) or not isinstance(live, list):
            mismatches[key] = {
                "expected": "request list is a prefix of the live list",
                "actual": snapshot,
                "live": live,
            }
            continue
        if live[:len(snapshot)] != snapshot:
            mismatches[key] = {
                "expected": "request list is a prefix of the live list",
                "actual": snapshot,
                "live": live,
            }

    snapshot_workers = request.get("workers")
    live_workers = cluster.get("workers")
    if (not isinstance(snapshot_workers, list)
            or not isinstance(live_workers, list)
            or not set(snapshot_workers).issubset(set(live_workers))):
        mismatches["workers"] = {
            "expected": "request workers are a subset of live workers",
            "actual": snapshot_workers,
            "live": live_workers,
        }

    if mismatches:
        raise FailureControlError(
            "existing synthesis request is not a stable prefix of detected cluster",
            cluster_id=cid, path=str(path), mismatches=mismatches,
        )


def _requested_event(events: Sequence[Mapping[str, Any]], cluster_id: str) -> Optional[Dict[str, Any]]:
    for event in events:
        if event.get("cluster_id") == cluster_id and event.get("op") == "requested":
            return dict(event)
    return None


def cluster_states(project_dir: Path) -> Dict[str, str]:
    states: Dict[str, str] = {}
    for ev in _read_ledger(project_dir):
        cid = ev.get("cluster_id")
        op = ev.get("op")
        if isinstance(cid, str) and op in STATES:
            states[cid] = op
    return states


def _last_seq(events: Sequence[Mapping[str, Any]]) -> int:
    seq = 0
    for ev in events:
        raw = ev.get("seq")
        try:
            seq = max(seq, int(raw))
        except (TypeError, ValueError):
            continue
    return seq


@contextmanager
def failures_lock(project_dir: Path) -> Iterator[None]:
    folder = failures_dir(project_dir)
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = folder / ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def persist_new_requests(
    wl: L.WorkerLayout,
    clusters: Sequence[Mapping[str, Any]],
    *,
    branch_id: str,
    subgoal_id: str,
) -> List[Dict[str, Any]]:
    """Under the project lock, request each new cluster once. No fact writes."""
    if not clusters:
        return []
    created: List[Dict[str, Any]] = []
    with failures_lock(wl.project_dir):
        events = _read_ledger(wl.project_dir)
        states = {}
        for ev in events:
            cid = ev.get("cluster_id")
            op = ev.get("op")
            if isinstance(cid, str) and op in STATES:
                states[cid] = op
        seq = _last_seq(events)
        watermark = _memory_watermark(wl.project_dir)
        now = utc_now()
        for cluster in clusters:
            cid = cluster.get("cluster_id")
            if not isinstance(cid, str) or not cid:
                continue
            current = states.get(cid)
            path = request_path(wl.project_dir, cid)
            if current in ("resolved", "superseded"):
                continue
            if current == "requested":
                event = _requested_event(events, cid)
                if event is None:
                    raise FailureControlError(
                        "requested cluster has no requested ledger event", cluster_id=cid,
                    )
                # The ledger row is the immutable evidence snapshot assigned
                # to the synthesis worker. It remains valid when append-only
                # global memory adds later members to the same live cluster.
                _validate_request_for_cluster(
                    event, event, path=ledger_path(wl.project_dir),
                )
                _validate_request_is_live_prefix(
                    event, cluster, path=ledger_path(wl.project_dir),
                )
                if path.exists():
                    sidecar = _read_request_strict(path)
                    # The sidecar must still agree exactly with the ledger
                    # snapshot; live-cluster growth never rewrites either.
                    _validate_request_for_cluster(sidecar, event, path=path)
                elif not _write_request_exclusive(path, event):
                    # Another process created it while holding an older lock fd;
                    # validate rather than trusting the create miss.
                    sidecar = _read_request_strict(path)
                    _validate_request_for_cluster(sidecar, event, path=path)
                continue
            seq += 1
            req = {
                "schema_version": SCHEMA_VERSION,
                "op": "requested",
                "state": "requested",
                "seq": seq,
                "cluster_id": cid,
                "trigger": cluster.get("trigger"),
                "signature": cluster.get("signature") or "",
                "project_id": cluster.get("project_id") or wl.project,
                "branch_id": cluster.get("branch_id") or branch_id,
                "subgoal_id": cluster.get("subgoal_id") or subgoal_id,
                "attempt_ids": list(cluster.get("attempt_ids") or []),
                "verification_ids": list(cluster.get("verification_ids") or []),
                "obstacle_ids": list(cluster.get("obstacle_ids") or []),
                "workers": list(cluster.get("workers") or []),
                "member_ids": list(cluster.get("member_ids") or []),
                "assigned_worker": wl.name,
                "created_at": now,
                "watermark": watermark,
            }
            if not _write_request_exclusive(path, req):
                # Crash recovery: the immutable request reached disk but its
                # ledger append did not. Validate the snapshot itself and as a
                # prefix of the current cluster, then make it visible with one
                # fresh monotonic requested event.
                existing = _read_request_strict(path)
                _validate_request_for_cluster(existing, existing, path=path)
                _validate_request_is_live_prefix(existing, cluster, path=path)
                recovered = dict(existing)
                recovered["seq"] = seq
                recovered["recovered_at"] = now
                recovered["recovered_from"] = "request_sidecar"
                _append_ledger(wl.project_dir, recovered)
                states[cid] = "requested"
                created.append(recovered)
                continue
            _append_ledger(wl.project_dir, req)
            states[cid] = "requested"
            created.append(req)
    return created


def after_successful_close(
    wl: L.WorkerLayout,
    *,
    branch_id: str,
    subgoal_id: str,
) -> List[Dict[str, Any]]:
    """Scan + persist new synthesis requests. Never writes facts or math findings."""
    clusters = scan_clusters(wl.project_dir)
    return persist_new_requests(
        wl, clusters, branch_id=branch_id, subgoal_id=subgoal_id,
    )


def list_clusters(project_dir: Path) -> List[Dict[str, Any]]:
    """Operator view: cluster states/IDs from the control ledger (no private memory)."""
    events = _read_ledger(project_dir)
    by_id: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        cid = ev.get("cluster_id")
        if not isinstance(cid, str) or not cid:
            continue
        cur = dict(by_id.get(cid) or {})
        cur.update({k: v for k, v in ev.items() if v is not None})
        op = ev.get("op")
        if op in STATES:
            cur["state"] = op
        by_id[cid] = cur
    rows = list(by_id.values())
    rows.sort(key=lambda r: (int(r.get("seq") or 0), r.get("cluster_id") or ""))
    return rows


def next_pending_request(wl: L.WorkerLayout) -> Optional[Dict[str, Any]]:
    """FIFO pending request assigned to this worker. One synthesis round at a time."""
    pending = [
        c for c in list_clusters(wl.project_dir)
        if c.get("state") == "requested" and c.get("assigned_worker") == wl.name
    ]
    if not pending:
        return None
    pending.sort(key=lambda r: (int(r.get("seq") or 0), r.get("cluster_id") or ""))
    return pending[0]


def worker_failure_view(wl: L.WorkerLayout) -> Dict[str, Any]:
    clusters = list_clusters(wl.project_dir)
    assigned = [
        {
            "cluster_id": c.get("cluster_id"),
            "trigger": c.get("trigger"),
            "state": c.get("state"),
            "seq": c.get("seq"),
        }
        for c in clusters
        if c.get("state") == "requested" and c.get("assigned_worker") == wl.name
    ]
    return {
        "pending_assigned": assigned,
        "project_requested": sum(1 for c in clusters if c.get("state") == "requested"),
        "project_resolved": sum(1 for c in clusters if c.get("state") == "resolved"),
        "project_superseded": sum(1 for c in clusters if c.get("state") == "superseded"),
    }


def render_synthesis_prompt(request: Mapping[str, Any]) -> str:
    """Orchestrator injection. Directs identify-key-failures; does not conclude math."""
    def _csv(key: str) -> str:
        vals = request.get(key) or []
        if not isinstance(vals, list):
            return ""
        return ", ".join(str(v) for v in vals if v)

    return (
        "FAILURE SYNTHESIS MODE\n"
        "This is not a normal proof round. The scheduler has NOT decided that a "
        "mathematical route is dead and will not auto-create a confirmed dead_end.\n"
        "Invoke $identify-key-failures FIRST using exactly these existing record IDs "
        "(do not invent IDs; do not write facts):\n"
        f"  cluster_id: {request.get('cluster_id')}\n"
        f"  trigger: {request.get('trigger')}\n"
        f"  project_id: {request.get('project_id')}\n"
        f"  branch_id: {request.get('branch_id')}\n"
        f"  subgoal_id: {request.get('subgoal_id')}\n"
        f"  attempt_ids: {_csv('attempt_ids')}\n"
        f"  verification_ids: {_csv('verification_ids')}\n"
        f"  obstacle_ids: {_csv('obstacle_ids')}\n"
        f"  source_record_ids: {_csv('member_ids')}\n"
        f"  workers: {_csv('workers')}\n"
        "Then persist ONE matching-context gm_add: kind obstacle OR dead_end with "
        "dead_end_status=candidate (never auto-confirm) and metadata "
        f"record_type={SYNTHESIS_RECORD_TYPE}, cluster_id=<the cluster_id above>, "
        "source_record_ids=<the exact source_record_ids above>, "
        "failed_plan_ids or failed_attempt_ids=<the exact attempt_ids above when nonempty>, "
        "common_failures, and "
        "implications_for_next_plans or recommended_actions.\n"
        "Stage 1–3 still apply (preflight, receipts, proof_attempt contract). "
        "A confirmed dead_end is allowed only via stage-3 evidence_class + "
        "evidence_refs that YOU supply as a mathematical judgment — this prompt "
        "does not supply that conclusion."
    )


def _nonempty_list(val: Any) -> bool:
    return isinstance(val, list) and any(str(x).strip() for x in val)


def _is_candidate_dead_end(entry: Mapping[str, Any]) -> bool:
    status = entry.get("dead_end_status") or entry.get("status")
    return status == "candidate" or status in (None, "", "open")


def evaluate_synthesis_close(
    wl: L.WorkerLayout,
    appended_ids: Mapping[str, Sequence[str]],
    request: Mapping[str, Any],
    *,
    run_id: str,
    branch_id: str,
    subgoal_id: str,
) -> Dict[str, Any]:
    """Require one matching-context summary. Confirmed dead_end does not resolve."""
    expected = {
        "project_id": wl.project,
        "worker_id": wl.name,
        "run_id": run_id or "",
        "branch_id": branch_id or "",
        "subgoal_id": subgoal_id or "",
    }
    want = request.get("cluster_id")
    gm = GlobalMemory(wl.project_dir)
    source_ids = request.get("member_ids")
    if (not isinstance(source_ids, list) or not source_ids
            or any(not isinstance(x, str) or not x for x in source_ids)
            or len(source_ids) != len(set(source_ids))):
        raise FailureSynthesisError(
            "synthesis request has invalid member_ids", cluster_id=want,
        )
    available_by_kind = {
        kind: {e.get("id") for e in gm.read(kind) if isinstance(e.get("id"), str)}
        for kind in ("proof_attempt", "verification", "obstacle")
    }
    typed_request_ids = {
        "proof_attempt": request.get("attempt_ids") or [],
        "verification": request.get("verification_ids") or [],
        "obstacle": request.get("obstacle_ids") or [],
    }
    for source_kind, ids in typed_request_ids.items():
        if (not isinstance(ids, list)
                or any(not isinstance(x, str) or not x for x in ids)
                or any(x not in available_by_kind[source_kind] for x in ids)):
            raise FailureSynthesisError(
                "synthesis request references missing or mistyped source records",
                cluster_id=want, source_kind=source_kind, source_ids=ids,
            )
    if set(source_ids) != set().union(*(set(v) for v in typed_request_ids.values())):
        raise FailureSynthesisError(
            "synthesis request member_ids do not equal its typed source IDs",
            cluster_id=want,
        )
    obstacles = _load_appended(gm, "obstacle", appended_ids.get("obstacle") or [], expected)
    dead_ends = _load_appended(gm, "dead_end", appended_ids.get("dead_end") or [], expected)

    def _ok_summary(entry: Mapping[str, Any], kind: str) -> Optional[Dict[str, Any]]:
        if entry.get("record_type") != SYNTHESIS_RECORD_TYPE:
            return None
        cid = entry.get("cluster_id")
        if cid != want:
            raise FailureSynthesisError(
                f"synthesis record cluster_id {cid!r} does not match request {want!r}",
                cluster_id=want, record_id=entry.get("id"), kind=kind,
            )
        supplied_sources = entry.get("source_record_ids")
        if supplied_sources != source_ids:
            raise FailureSynthesisError(
                "synthesis summary source_record_ids must exactly match the request",
                cluster_id=want, record_id=entry.get("id"),
                expected_source_record_ids=source_ids,
                actual_source_record_ids=supplied_sources,
            )
        if kind == "dead_end":
            if entry.get("dead_end_status") == "confirmed":
                raise FailureSynthesisError(
                    "synthesis dead_end must be candidate; confirmed is not automatic",
                    cluster_id=want, record_id=entry.get("id"),
                )
            if not _is_candidate_dead_end(entry):
                raise FailureSynthesisError(
                    "synthesis dead_end must be candidate",
                    cluster_id=want, record_id=entry.get("id"),
                )
        failed = (
            entry.get("failed_plan_ids")
            or entry.get("failed_attempt_ids")
            or entry.get("attempt_ids")
        )
        expected_attempts = request.get("attempt_ids") or []
        if expected_attempts and failed != expected_attempts:
            raise FailureSynthesisError(
                "synthesis summary failed attempt IDs must exactly match the request",
                cluster_id=want, record_id=entry.get("id"), expected=expected_attempts,
                actual=failed,
            )
        if not _nonempty_list(entry.get("common_failures")):
            raise FailureSynthesisError(
                "synthesis summary missing common_failures",
                cluster_id=want, record_id=entry.get("id"),
            )
        implications = (
            entry.get("implications_for_next_plans")
            or entry.get("recommended_actions")
        )
        if not _nonempty_list(implications):
            raise FailureSynthesisError(
                "synthesis summary missing implications/recommended_actions",
                cluster_id=want, record_id=entry.get("id"),
            )
        return {
            "ok": True,
            "kind": kind,
            "entry_id": entry.get("id"),
            "cluster_id": cid,
            "dead_end_status": entry.get("dead_end_status"),
        }

    matches: List[Dict[str, Any]] = []
    errors: List[FailureSynthesisError] = []
    for kind, rows in (("obstacle", obstacles), ("dead_end", dead_ends)):
        for e in rows:
            if e.get("record_type") != SYNTHESIS_RECORD_TYPE and e.get("cluster_id") is None:
                continue
            try:
                got = _ok_summary(e, kind)
            except FailureSynthesisError as err:
                errors.append(err)
                continue
            if got:
                matches.append(got)
    if matches:
        return matches[0]
    if errors:
        raise errors[0]
    raise FailureSynthesisError(
        "synthesis round did not append a matching-context "
        f"{SYNTHESIS_RECORD_TYPE} obstacle or dead_end candidate",
        cluster_id=want,
    )


def resolve_request(
    project_dir: Path,
    cluster_id: str,
    *,
    kind: str,
    entry_id: Optional[str],
    worker: str,
) -> Dict[str, Any]:
    if not isinstance(cluster_id, str) or not re.fullmatch(r"[0-9a-f]{16}", cluster_id):
        raise FailureControlError("invalid cluster_id", cluster_id=cluster_id)
    if kind not in ("obstacle", "dead_end"):
        raise FailureControlError("invalid synthesis result kind", cluster_id=cluster_id, kind=kind)
    if not isinstance(entry_id, str) or not re.fullmatch(r"[0-9a-f]{16}", entry_id):
        raise FailureControlError(
            "invalid synthesis result entry_id", cluster_id=cluster_id,
            entry_id=entry_id,
        )
    if not isinstance(worker, str) or not worker.strip():
        raise FailureControlError("missing resolving worker", cluster_id=cluster_id)
    now = utc_now()
    with failures_lock(project_dir):
        events = _read_ledger(project_dir)
        rows = {row.get("cluster_id"): row for row in list_clusters(project_dir)}
        current = rows.get(cluster_id)
        if current is None:
            raise FailureControlError(
                "cannot resolve an unknown synthesis request", cluster_id=cluster_id,
            )
        if current.get("state") != "requested":
            raise FailureControlError(
                "synthesis request is not currently requested",
                cluster_id=cluster_id, state=current.get("state"),
            )
        if current.get("assigned_worker") != worker:
            raise FailureControlError(
                "synthesis request is assigned to a different worker",
                cluster_id=cluster_id, assigned_worker=current.get("assigned_worker"),
                worker=worker,
            )
        seq = _last_seq(events) + 1
        ev = {
            "op": "resolved",
            "state": "resolved",
            "seq": seq,
            "cluster_id": cluster_id,
            "kind": kind,
            "entry_id": entry_id,
            "worker": worker,
            "resolved_at": now,
        }
        _append_ledger(project_dir, ev)
        return ev


def fact_graph_has_writes(project_dir: Path) -> bool:
    return bool(FactGraph(project_dir).list())
