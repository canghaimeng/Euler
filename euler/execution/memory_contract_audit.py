# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Read-only Memory Contract audit (M1–M8).

Never writes project stores. Never heals the failure ledger. Never claims PASS
for an unobservable invariant (semantic route changes, OS-level no-bypass,
absolute verification completeness).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from euler.core import FactGraph, GlobalMemory
from euler.core._util import utc_now
from euler.core.factgraph import statement_of

from . import layout as L
from .failure_clusters import (
    is_stamped,
    ledger_path,
    request_path,
    scan_clusters,
)
from .launch_receipt import (
    launch_receipt_path,
    read_and_verify_launch,
    read_and_verify_result,
    result_receipt_path,
)
from .memory_digest import (
    RoundReceiptError,
    canonical_json,
    end_receipt_path,
    read_and_verify_end,
    read_and_verify_start,
    start_receipt_path,
)
from .round_contract import MemoryContractError, _attempt_id, evaluate_round_close

PASS = "PASS"
FAIL = "FAIL"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNVERIFIABLE = "UNVERIFIABLE"
INCOMPLETE = "INCOMPLETE"

_ROUND_FILE_RE = re.compile(r"^round_(\d+)\.(start|end|launch|result)\.json$")
_OK_VERDICTS = frozenset({"correct", "wrong"})
_NON_OK = frozenset({
    "timeout", "connection_error", "http_error", "service_error", "protocol_error",
})


def _finding(
    mid: str,
    title: str,
    status: str,
    *,
    evidence: Optional[List[Any]] = None,
    counts: Optional[Dict[str, Any]] = None,
    limitations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "id": mid,
        "title": title,
        "status": status,
        "evidence": evidence or [],
        "counts": counts or {},
        "limitations": limitations or [],
    }


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


def _worker_live_round(wl: L.WorkerLayout) -> Optional[int]:
    try:
        st = json.loads(wl.status.read_text(encoding="utf-8")) if wl.status.is_file() else {}
    except (json.JSONDecodeError, OSError):
        st = {}
    pid = None
    if wl.pid.is_file():
        try:
            pid = int(wl.pid.read_text().strip())
        except (ValueError, OSError):
            pid = None
    if st.get("state") == "running" and _alive(pid):
        rid = st.get("receipt_id")
        try:
            return int(rid)
        except (TypeError, ValueError):
            return None
    return None


def _round_ids(wl: L.WorkerLayout) -> List[int]:
    folder = wl.receipts
    if not folder.is_dir():
        return []
    ids = set()
    for p in folder.iterdir():
        m = _ROUND_FILE_RE.match(p.name)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


def _load_optional(path: Path, reader) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.is_file():
        return None, None
    try:
        return reader(path), None
    except RoundReceiptError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def _raw_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "JSON root is not an object"
    return value, None


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _legacy_start(payload: Optional[Mapping[str, Any]]) -> bool:
    """Identify pre-contract/dry receipts from their missing structured scope."""
    if not payload:
        return False
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    return not (
        _nonempty_text(payload.get("run_id"))
        and _nonempty_text(task.get("branch_id"))
        and _nonempty_text(task.get("subgoal_id"))
    )


def report_hash(report: Mapping[str, Any]) -> str:
    body = {k: v for k, v in report.items() if k != "report_hash"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def verify_report_hash(report: Mapping[str, Any]) -> bool:
    stored = report.get("report_hash")
    return isinstance(stored, str) and stored == report_hash(report)


def _aggregate(findings: Sequence[Mapping[str, Any]]) -> str:
    statuses = [f.get("status") for f in findings]
    if FAIL in statuses:
        return FAIL
    if UNVERIFIABLE in statuses:
        return INCOMPLETE
    if statuses and all(s == NOT_APPLICABLE for s in statuses):
        return NOT_APPLICABLE
    if not statuses:
        return NOT_APPLICABLE
    return PASS


def _audit_m1(project: str, *, strict_legacy: bool) -> Dict[str, Any]:
    evidence: List[Any] = []
    limitations = [
        "legacy/dry start-only receipts are not valid launches",
        "semantic contents of a prompt cannot be re-derived from the hash",
    ]
    counts = {
        "structured_rounds": 0,
        "legacy_dry_rounds": 0,
        "in_progress": 0,
        "hash_failures": 0,
        "missing_companions": 0,
        "usage_reported": 0,
        "usage_not_reported": 0,
        "provider_unknown": 0,
    }
    failed = False
    saw_artifact = False
    provenance_incomplete = False
    for worker in L.list_workers(project):
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        live = _worker_live_round(wl)
        for rid in _round_ids(wl):
            saw_artifact = True
            start_p = start_receipt_path(wl, rid)
            launch_p = launch_receipt_path(wl, rid)
            result_p = result_receipt_path(wl, rid)
            end_p = end_receipt_path(wl, rid)

            raw_start, raw_start_err = _raw_json(start_p)
            if raw_start_err:
                failed = True
                counts["hash_failures"] += 1
                evidence.append({
                    "worker": worker, "round_id": rid,
                    "error": f"unreadable start receipt: {raw_start_err}",
                })
                continue
            is_legacy = start_p.is_file() and _legacy_start(raw_start)
            if is_legacy:
                counts["legacy_dry_rounds"] += 1
                evidence.append({
                    "worker": worker, "round_id": rid, "kind": "legacy_dry",
                })
                # A legacy start must not be combined with modern lifecycle
                # companions; that is an inconsistent/tampered lifecycle.
                modern_companions = [
                    name for name, path in (
                        ("launch", launch_p), ("result", result_p),
                    ) if path.is_file()
                ]
                if modern_companions:
                    failed = True
                    counts["missing_companions"] += 1
                    evidence.append({
                        "worker": worker, "round_id": rid,
                        "error": f"legacy start has modern companions {modern_companions}",
                    })
                if strict_legacy:
                    failed = True
                    evidence.append({
                        "worker": worker, "round_id": rid,
                        "error": "legacy/dry receipt fails under --strict-legacy",
                    })
                continue

            start, start_err = _load_optional(start_p, read_and_verify_start)
            launch, launch_err = _load_optional(launch_p, read_and_verify_launch)
            result, result_err = _load_optional(result_p, read_and_verify_result)
            end, end_err = _load_optional(end_p, read_and_verify_end)
            for err in (start_err, launch_err, result_err, end_err):
                if err:
                    failed = True
                    counts["hash_failures"] += 1
                    evidence.append({"worker": worker, "round_id": rid, "error": err})

            # Any modern artifact set requires a valid start receipt.
            if start is None:
                failed = True
                counts["missing_companions"] += 1
                evidence.append({
                    "worker": worker, "round_id": rid,
                    "error": "modern lifecycle missing valid start receipt",
                })

            def mismatch(message: str) -> None:
                nonlocal failed
                failed = True
                evidence.append({"worker": worker, "round_id": rid, "error": message})

            if start and launch:
                task = start.get("task") if isinstance(start.get("task"), dict) else {}
                expected = {
                    "project_id": start.get("project_id"),
                    "worker_id": start.get("worker_id"),
                    "round_id": start.get("round_id"),
                    "run_id": start.get("run_id"),
                    "branch_id": task.get("branch_id"),
                    "subgoal_id": task.get("subgoal_id"),
                }
                for key, want in expected.items():
                    if launch.get(key) != want:
                        mismatch(f"launch {key} mismatch: {launch.get(key)!r} != {want!r}")
                if launch.get("start_receipt_hash") != start.get("receipt_hash"):
                    mismatch("launch start_receipt_hash mismatch")
                if launch.get("start_receipt") != str(start_p):
                    mismatch("launch start_receipt path mismatch")
                if not launch.get("model") or not launch.get("provider") or not launch.get("prompt_sha256"):
                    mismatch("launch missing model/provider/prompt_sha256")
                if launch.get("provider") == "unknown":
                    provenance_incomplete = True
                    counts["provider_unknown"] += 1
                    evidence.append({
                        "worker": worker, "round_id": rid,
                        "warning": "launch provider is unknown",
                    })

            if launch and result:
                for key in ("project_id", "worker_id", "round_id", "run_id"):
                    if result.get(key) != launch.get(key):
                        mismatch(f"result {key} mismatch")
                if result.get("launch_receipt_hash") != launch.get("receipt_hash"):
                    mismatch("result launch_receipt_hash mismatch")
                if result.get("launch_receipt") != str(launch_p):
                    mismatch("result launch_receipt path mismatch")
                counts["usage_not_reported"] += 1
                if (result.get("usage_status") != "not_reported"
                        or result.get("tokens") is not None
                        or result.get("cost_usd") is not None):
                    mismatch("schema-v1 result contains unauthenticated/invented usage")

            if start and end:
                for key in ("project_id", "worker_id", "round_id"):
                    if end.get(key) != start.get(key):
                        mismatch(f"end {key} mismatch")
                if end.get("start_receipt_hash") != start.get("receipt_hash"):
                    mismatch("end start_receipt_hash mismatch")
                if end.get("start_digest_hash") != start.get("digest_hash"):
                    mismatch("end start_digest_hash mismatch")
                if end.get("start_receipt") != str(start_p):
                    mismatch("end start_receipt path mismatch")

            if live == rid:
                counts["in_progress"] += 1
                # The status is written just before the launch receipt, so a
                # valid start without launch is an observable pre-exec window.
                evidence.append({
                    "worker": worker, "round_id": rid, "kind": "in_progress",
                })
                continue
            counts["structured_rounds"] += 1
            missing = []
            if not start_p.is_file():
                missing.append("start")
            if not launch_p.is_file():
                missing.append("launch")
            if not result_p.is_file():
                missing.append("result")
            if not end_p.is_file() and not (
                result and result.get("rc") == 127
            ):
                missing.append("end")
            if missing:
                failed = True
                counts["missing_companions"] += 1
                evidence.append({
                    "worker": worker, "round_id": rid,
                    "error": f"settled missing companion {missing}",
                })
    if failed:
        status = FAIL
    elif not saw_artifact:
        status = NOT_APPLICABLE
    elif counts["legacy_dry_rounds"]:
        # Historical activity exists, but it predates the auditable lifecycle.
        status = UNVERIFIABLE
    elif provenance_incomplete:
        status = UNVERIFIABLE
    else:
        status = PASS
    return _finding(
        "M1", "receipt hashes, launch/result pairing, provenance fields",
        status, evidence=evidence, counts=counts, limitations=limitations,
    )


def _completed_structured(project: str) -> List[Tuple[L.WorkerLayout, int, Dict[str, Any]]]:
    out: List[Tuple[L.WorkerLayout, int, Dict[str, Any]]] = []
    for worker in L.list_workers(project):
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        live = _worker_live_round(wl)
        for rid in _round_ids(wl):
            if live == rid:
                continue
            launch_p = launch_receipt_path(wl, rid)
            end_p = end_receipt_path(wl, rid)
            if not launch_p.is_file() or not end_p.is_file():
                continue
            try:
                end = read_and_verify_end(end_p)
            except Exception:
                continue
            out.append((wl, rid, end))
    return out


def _audit_m2(project: str) -> Dict[str, Any]:
    evidence: List[Any] = []
    counts = {"rounds": 0, "ok": 0, "failed": 0}
    failed = False
    for wl, rid, end in _completed_structured(project):
        counts["rounds"] += 1
        start_p = start_receipt_path(wl, rid)
        try:
            start = read_and_verify_start(start_p)
        except Exception as e:
            failed = True
            counts["failed"] += 1
            evidence.append({"worker": wl.name, "round_id": rid, "error": str(e)})
            continue
        task = start.get("task") or {}
        try:
            evaluate_round_close(
                wl, end.get("appended_ids") or {},
                run_id=str(start.get("run_id") or ""),
                branch_id=task.get("branch_id"),
                subgoal_id=task.get("subgoal_id"),
            )
            counts["ok"] += 1
        except MemoryContractError as e:
            failed = True
            counts["failed"] += 1
            evidence.append({
                "worker": wl.name, "round_id": rid, "error": e.reason,
            })
    if failed:
        status = FAIL
    elif counts["rounds"] == 0:
        status = NOT_APPLICABLE
    else:
        status = PASS
    return _finding(
        "M2", "completed structured rounds satisfy the round-close contract",
        status, evidence=evidence, counts=counts,
        limitations=["re-audit uses end-receipt appended_ids, not a live journal"],
    )


def _audit_m3(project: str) -> Dict[str, Any]:
    evidence: List[Any] = []
    counts = {"rounds": 0, "blocked": 0, "abandoned": 0, "link_failures": 0}
    failed = False
    gm_cache: Dict[str, GlobalMemory] = {}
    for wl, rid, end in _completed_structured(project):
        counts["rounds"] += 1
        gm = gm_cache.setdefault(str(wl.project_dir), GlobalMemory(wl.project_dir))
        ids = end.get("appended_ids") or {}
        start = {}
        try:
            start = read_and_verify_start(start_receipt_path(wl, rid))
        except Exception:
            continue
        task = start.get("task") or {}
        expected = {
            "project_id": wl.project,
            "worker_id": wl.name,
            "run_id": str(start.get("run_id") or ""),
            "branch_id": task.get("branch_id") or "",
            "subgoal_id": task.get("subgoal_id") or "",
        }
        from .round_contract import _load_appended
        attempts = _load_appended(gm, "proof_attempt", ids.get("proof_attempt") or [], expected)
        obstacles = _load_appended(gm, "obstacle", ids.get("obstacle") or [], expected)
        dead_ends = _load_appended(gm, "dead_end", ids.get("dead_end") or [], expected)
        obs_ids = {_attempt_id(e) for e in obstacles}
        obs_ids.discard(None)
        de_ids = {_attempt_id(e) for e in dead_ends}
        de_ids.discard(None)
        for pa in attempts:
            aid = _attempt_id(pa)
            if pa.get("result") == "blocked":
                counts["blocked"] += 1
                if not aid or aid not in obs_ids:
                    failed = True
                    counts["link_failures"] += 1
                    evidence.append({
                        "worker": wl.name, "round_id": rid,
                        "error": f"blocked {aid!r} missing obstacle",
                    })
            elif pa.get("result") == "abandoned":
                counts["abandoned"] += 1
                if not aid or aid not in de_ids:
                    failed = True
                    counts["link_failures"] += 1
                    evidence.append({
                        "worker": wl.name, "round_id": rid,
                        "error": f"abandoned {aid!r} missing dead_end",
                    })
    limitations = [
        "semantic route changes inside a round are UNVERIFIABLE from artifacts",
    ]
    if failed:
        status = FAIL
    elif counts["rounds"] == 0:
        status = NOT_APPLICABLE
    else:
        status = UNVERIFIABLE
        evidence.append(
            "observable blocked/abandoned links validated; intra-round route identity cannot be proven"
        )
    return _finding(
        "M3", "blocked→obstacle and abandoned→dead_end links",
        status, evidence=evidence, counts=counts, limitations=limitations,
    )


def _audit_m4(project: str) -> Dict[str, Any]:
    gm = GlobalMemory(L.project_dir(project))
    fg = FactGraph(L.project_dir(project))
    rows = gm.read("verification")
    observed = [e for e in rows if is_stamped(e)]
    evidence: List[Any] = []
    counts = {
        "observed": len(observed),
        "unstructured": len(rows) - len(observed),
        "shape_failures": 0,
    }
    failed = False
    for e in observed:
        bad = []
        if e.get("provenance") != "fact_submit":
            bad.append("provenance")
        tstatus = e.get("transport_status")
        verdict = e.get("verdict")
        if tstatus != "ok" and tstatus not in _NON_OK:
            bad.append("transport_status")
        fid = e.get("fact_id")
        if fid and not fg.exists(str(fid)):
            bad.append("dangling_fact_id")
        if tstatus == "ok" and verdict == "correct" and not fid:
            # A correct verdict with a fact write error is a valid observed
            # verification record, but it must say why no fact exists.
            if not _nonempty_text(e.get("write_error")):
                bad.append("correct_without_fact_or_write_error")
        if bad:
            failed = True
            counts["shape_failures"] += 1
            evidence.append({"id": e.get("id"), "errors": bad})
    limitations = [
        "absolute completeness of verification rows is UNVERIFIABLE "
        "(no pre-call crash journal)",
    ]
    if failed:
        status = FAIL
    elif observed or rows or fg.list() or any(
        _round_ids(L.WorkerLayout(L.worker_dir(project, worker)))
        for worker in L.list_workers(project)
    ):
        status = UNVERIFIABLE
        evidence.append(
            "observed rows validated where structured; absolute invocation completeness is unprovable"
        )
    else:
        status = NOT_APPLICABLE
    return _finding(
        "M4", "observed structured verification rows",
        status, evidence=evidence, counts=counts, limitations=limitations,
    )


def _audit_m5(project: str) -> Dict[str, Any]:
    gm = GlobalMemory(L.project_dir(project))
    all_rows = gm.read("verification")
    rows = [e for e in all_rows if is_stamped(e)]
    evidence: List[Any] = []
    counts = {
        "checked": len(rows),
        "legacy_unstructured": len(all_rows) - len(rows),
        "violations": 0,
    }
    failed = False
    for e in rows:
        tstatus = e.get("transport_status")
        verdict = e.get("verdict")
        if tstatus == "ok":
            good = verdict in _OK_VERDICTS
        elif tstatus in _NON_OK:
            good = verdict is None
        else:
            good = False
        if not good:
            failed = True
            counts["violations"] += 1
            evidence.append({
                "id": e.get("id"),
                "transport_status": tstatus,
                "verdict": verdict,
            })
    if failed:
        status = FAIL
    elif not rows:
        status = UNVERIFIABLE if all_rows else NOT_APPLICABLE
    else:
        status = PASS
    return _finding(
        "M5", "transport_status=ok iff verdict is correct|wrong",
        status, evidence=evidence, counts=counts,
        limitations=["checked against observed structured verification rows only"],
    )


def _inspect_mcp_config_static(wl: L.WorkerLayout) -> Tuple[bool, str]:
    """Inspect configured transport without spawning MCP or touching stores."""
    path = wl.codex_config
    if not path.is_file():
        return False, f"missing MCP config {path}"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return False, f"invalid MCP config: {exc}"
    section = ((data.get("mcp_servers") or {}).get("euler")
               if isinstance(data.get("mcp_servers"), dict) else None)
    if not isinstance(section, dict):
        return False, "missing [mcp_servers.euler]"
    command = section.get("command")
    if not _nonempty_text(command):
        return False, "missing MCP command"
    command_path = Path(str(command))
    if not command_path.is_absolute() or not command_path.is_file():
        return False, f"MCP command is not an existing absolute file: {command!r}"
    args = section.get("args")
    if args != ["-m", "euler.gateway"]:
        return False, f"unexpected MCP args {args!r}"
    env = section.get("env")
    if not isinstance(env, dict):
        return False, "missing MCP env"
    if env.get("EULER_ROLE") != "worker":
        return False, "MCP role is not worker"
    try:
        configured_project = Path(str(env.get("EULER_PROJECT_DIR") or "")).resolve()
        if configured_project != wl.project_dir.resolve():
            return False, "MCP project directory mismatch"
    except OSError as exc:
        return False, f"cannot resolve MCP project directory: {exc}"
    if not _nonempty_text(env.get("EULER_VERIFY_URL")):
        return False, "missing EULER_VERIFY_URL"
    return True, "static config valid; live handshake intentionally not run"


def _audit_m6(project: str) -> Dict[str, Any]:
    evidence: List[Any] = []
    counts = {"workers": 0, "mcp_ok": 0, "mcp_fail": 0}
    mcp_failed = False
    for worker in L.list_workers(project):
        wl = L.WorkerLayout(L.worker_dir(project, worker))
        counts["workers"] += 1
        ok, reason = _inspect_mcp_config_static(wl)
        if ok:
            counts["mcp_ok"] += 1
            evidence.append({"worker": worker, "mcp_config": "ok"})
        else:
            mcp_failed = True
            counts["mcp_fail"] += 1
            evidence.append({"worker": worker, "mcp_config": reason})
        # provenance evidence from latest launch if any
        ids = _round_ids(wl)
        if ids:
            launch_p = launch_receipt_path(wl, ids[-1])
            if launch_p.is_file():
                try:
                    launch = read_and_verify_launch(launch_p)
                    evidence.append({
                        "worker": worker,
                        "latest_launch": {
                            "model": launch.get("model"),
                            "provider": launch.get("provider"),
                            "reasoning_effort": launch.get("reasoning_effort"),
                            "prompt_sha256": launch.get("prompt_sha256"),
                        },
                    })
                except RoundReceiptError as e:
                    evidence.append({"worker": worker, "launch_error": str(e)})
    limitations = [
        "PASS of MCP config is scoped to orchestrator-written worker launches",
        "global claim of no direct filesystem bypass is UNVERIFIABLE "
        "(same-UID dangerous sandbox cannot be proven from artifacts)",
        "audit does not mutate stores or run live /health or MCP handshakes",
    ]
    if mcp_failed:
        status = FAIL
    elif counts["workers"] == 0:
        status = NOT_APPLICABLE
    else:
        status = UNVERIFIABLE
        evidence.append("orchestrator MCP config inspected; OS-level no-bypass unprovable")
    return _finding(
        "M6", "MCP/preflight contract and provenance evidence (no live probes)",
        status, evidence=evidence, counts=counts, limitations=limitations,
    )


def _audit_m7(project: str) -> Dict[str, Any]:
    pdir = L.project_dir(project)
    fired = scan_clusters(pdir)
    workers = set(L.list_workers(project))
    evidence: List[Any] = []
    counts = {
        "fired": len(fired),
        "ledger": 0,
        "missing_request": 0,
        "corrupt_sidecar": 0,
        "missing_worker": 0,
        "corrupt_ledger": 0,
        "orphan_sidecar": 0,
    }
    failed = False
    events: List[Dict[str, Any]] = []
    lp = ledger_path(pdir)
    last_seq = 0
    if lp.is_file():
        try:
            lines = lp.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            lines = []
            failed = True
            counts["corrupt_ledger"] += 1
            evidence.append({"error": f"unreadable ledger: {exc}"})
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                failed = True
                counts["corrupt_ledger"] += 1
                evidence.append({"line": lineno, "error": f"invalid ledger JSON: {exc}"})
                continue
            if not isinstance(event, dict):
                failed = True
                counts["corrupt_ledger"] += 1
                evidence.append({"line": lineno, "error": "ledger event is not an object"})
                continue
            cid, op, seq = event.get("cluster_id"), event.get("op"), event.get("seq")
            if (not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{16}", cid)
                    or op not in ("requested", "resolved", "superseded")
                    or event.get("state") != op
                    or isinstance(seq, bool) or not isinstance(seq, int)
                    or seq <= last_seq):
                failed = True
                counts["corrupt_ledger"] += 1
                evidence.append({"line": lineno, "error": "invalid ledger id/op/state/sequence"})
                continue
            last_seq = seq
            events.append(event)

    requested_event: Dict[str, Dict[str, Any]] = {}
    states: Dict[str, str] = {}
    for event in events:
        cid = str(event["cluster_id"])
        op = str(event["op"])
        if op == "requested":
            if cid in requested_event:
                failed = True
                counts["corrupt_ledger"] += 1
                evidence.append({"cluster_id": cid, "error": "duplicate requested transition"})
            requested_event[cid] = event
        elif cid not in requested_event:
            failed = True
            counts["corrupt_ledger"] += 1
            evidence.append({"cluster_id": cid, "error": f"{op} without requested transition"})
        states[cid] = op
    counts["ledger"] = len(states)

    compare_keys = (
        "schema_version", "op", "state", "cluster_id", "trigger", "signature",
        "project_id", "branch_id", "subgoal_id", "attempt_ids",
        "verification_ids", "obstacle_ids", "workers", "member_ids",
        "assigned_worker", "created_at", "watermark",
    )
    for cid, event in requested_event.items():
        path = request_path(pdir, cid)
        sidecar, sidecar_err = _raw_json(path)
        if sidecar_err or sidecar is None:
            if states.get(cid) == "requested":
                failed = True
                counts["corrupt_sidecar"] += 1
                evidence.append({
                    "cluster_id": cid,
                    "error": f"requested sidecar missing/corrupt: {sidecar_err or 'missing'}",
                })
        else:
            mismatched = [key for key in compare_keys if sidecar.get(key) != event.get(key)]
            if mismatched:
                failed = True
                counts["corrupt_sidecar"] += 1
                evidence.append({"cluster_id": cid, "error": "sidecar mismatch", "fields": mismatched})
        if states.get(cid) == "requested":
            assigned = event.get("assigned_worker")
            if assigned not in workers:
                failed = True
                counts["missing_worker"] += 1
                evidence.append({
                    "cluster_id": cid,
                    "error": f"assigned worker {assigned!r} does not exist",
                })

    requests_dir = pdir / L.FAILURES_DIR / "requests"
    if requests_dir.is_dir():
        for path in requests_dir.glob("*.json"):
            if path.stem not in requested_event:
                failed = True
                counts["orphan_sidecar"] += 1
                evidence.append({"path": str(path), "error": "sidecar has no requested ledger event"})

    for item in fired:
        cid = item.get("cluster_id")
        if states.get(str(cid)) not in ("requested", "resolved", "superseded"):
            failed = True
            counts["missing_request"] += 1
            evidence.append({"cluster_id": cid, "error": "fired cluster not in ledger"})
    limitations = [
        "read-only: audit never heals sidecar/ledger crash gaps",
        "ledger path is not mutated even if a repair would succeed",
    ]
    if failed:
        status = FAIL
    elif not fired and not states and not (requests_dir.is_dir() and any(requests_dir.glob("*.json"))):
        status = NOT_APPLICABLE
    else:
        status = PASS
    evidence.append({"ledger": str(ledger_path(pdir))})
    return _finding(
        "M7", "fired clusters vs append-only failure ledger/sidecars",
        status, evidence=evidence, counts=counts, limitations=limitations,
    )


def _audit_m8(project: str) -> Dict[str, Any]:
    pdir = L.project_dir(project)
    fg = FactGraph(pdir)
    gm = GlobalMemory(pdir)
    verifs = gm.read("verification")
    by_fid: Dict[str, List[Dict[str, Any]]] = {}
    for v in verifs:
        fid = v.get("fact_id")
        if isinstance(fid, str) and fid:
            by_fid.setdefault(fid, []).append(v)
    evidence: List[Any] = []
    counts = {
        "facts": 0,
        "structured_ok": 0,
        "legacy_or_unprovenanced": 0,
        "fake_or_mismatch": 0,
    }
    failed = False
    def norm(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    structured_verifs = [v for v in verifs if is_stamped(v)]
    for row in structured_verifs:
        if (row.get("transport_status") == "ok"
                and row.get("verdict") == "correct"):
            fid = row.get("fact_id")
            if fid and not fg.exists(str(fid)):
                failed = True
                counts["fake_or_mismatch"] += 1
                evidence.append({"verification_id": row.get("id"), "error": "correct verification points to missing fact"})
            if not fid and not _nonempty_text(row.get("write_error")):
                failed = True
                counts["fake_or_mismatch"] += 1
                evidence.append({"verification_id": row.get("id"), "error": "correct verification has neither fact nor write_error"})

    for fid in fg.list():
        counts["facts"] += 1
        rows = by_fid.get(fid) or []
        structured_rows = [v for v in rows if is_stamped(v)]
        raw = fg.get_raw(fid) or ""
        fact_statement = norm(statement_of(raw))
        matching = [
            v for v in structured_rows
            if v.get("transport_status") == "ok"
            and v.get("verdict") == "correct"
            and v.get("provenance") == "fact_submit"
            and not v.get("write_error")
            and fg.exists(fid)
            and norm(v.get("claim")) == fact_statement
        ]
        if matching:
            counts["structured_ok"] += 1
        elif structured_rows:
            failed = True
            counts["fake_or_mismatch"] += 1
            evidence.append({
                "fact_id": fid,
                "error": "fact lacks matching structured fact_submit verification/statement",
            })
        else:
            counts["legacy_or_unprovenanced"] += 1
            evidence.append({"fact_id": fid, "kind": "legacy_or_unprovenanced_fact"})
    limitations = [
        "facts without a structured fact_submit verification are never silently PASS",
    ]
    if failed:
        status = FAIL
    elif counts["legacy_or_unprovenanced"]:
        status = UNVERIFIABLE
        limitations.append(
            f"{counts['legacy_or_unprovenanced']} facts lack structured provenance; "
            "artifacts alone cannot distinguish legacy from a direct bypass write"
        )
    elif counts["structured_ok"]:
        status = PASS
    else:
        status = NOT_APPLICABLE
    return _finding(
        "M8", "structured facts require matching fact_submit verification",
        status, evidence=evidence, counts=counts, limitations=limitations,
    )


def audit_project(project: str, *, strict_legacy: bool = False) -> Dict[str, Any]:
    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise FileNotFoundError(f"no such project: {project}")
    findings = [
        _audit_m1(project, strict_legacy=strict_legacy),
        _audit_m2(project),
        _audit_m3(project),
        _audit_m4(project),
        _audit_m5(project),
        _audit_m6(project),
        _audit_m7(project),
        _audit_m8(project),
    ]
    report: Dict[str, Any] = {
        "schema_version": 1,
        "kind": "memory_contract_audit",
        "project": project,
        "project_dir": str(pdir),
        "created_at": utc_now(),
        "strict_legacy": bool(strict_legacy),
        "findings": findings,
        "overall": _aggregate(findings),
        "limitations": [
            "audit cannot prove semantic route changes inside a round",
            "audit cannot prove OS-level no-filesystem-bypass on a same-UID sandbox",
            "P0-1 through P0-8 have in-tree mechanisms, with the explicit semantic, "
            "absolute-completeness, same-UID bypass, and usage-telemetry limitations "
            "reported by M3/M4/M6/provenance coverage",
        ],
    }
    report["report_hash"] = report_hash(report)
    return report


def exclusive_write_report(path: Path, report: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError as e:
        raise FileExistsError(f"audit output already exists (will not overwrite): {path}") from e
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short write while persisting audit report")
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
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    saved, error = _raw_json(path)
    if error or saved is None or not verify_report_hash(saved):
        try:
            path.unlink()
        except OSError:
            pass
        raise OSError(f"persisted audit report failed hash verification: {error or path}")


def audit_exit_code(report: Mapping[str, Any]) -> int:
    overall = report.get("overall")
    if overall == PASS:
        return 0
    if overall == FAIL:
        return 1
    return 2


def format_audit(report: Mapping[str, Any]) -> str:
    lines = [
        f"memory-audit {report.get('project')}  overall={report.get('overall')}",
        f"created_at={report.get('created_at')}  report_hash={report.get('report_hash')}",
    ]
    for f in report.get("findings") or []:
        lines.append(f"  {f.get('id')} {f.get('status'):<16} {f.get('title')}")
    return "\n".join(lines)
