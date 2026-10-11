# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Round-close memory contract (stage 3).

Inspect IDs appended this round with matching stamped context. Does not judge
whether a mathematical dead end is *true* — only that required artifacts exist
and are consistently linked. Timeouts/crashes must not fabricate attempts.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

from euler.core import FactGraph, GlobalMemory

from . import layout as L
from .memory_digest import load_task_context


class MemoryContractError(Exception):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details

    def to_status_dict(self) -> Dict[str, Any]:
        return {"ok": False, "reason": self.reason, **self.details}


def _attempt_id(entry: Mapping[str, Any]) -> Optional[str]:
    val = entry.get("attempt_id")
    if isinstance(val, str) and val.strip():
        return val.strip()
    links = entry.get("links") if isinstance(entry.get("links"), dict) else {}
    val = links.get("attempt_id")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _context_match(entry: Mapping[str, Any], expected: Mapping[str, str]) -> bool:
    for key in ("project_id", "worker_id", "run_id", "branch_id", "subgoal_id"):
        if expected.get(key) and entry.get(key) != expected.get(key):
            return False
    return True


def _load_appended(
    gm: GlobalMemory,
    kind: str,
    ids: Sequence[str],
    expected: Mapping[str, str],
) -> List[Dict[str, Any]]:
    want = set(ids)
    out: List[Dict[str, Any]] = []
    for e in gm.read(kind):
        if e.get("id") not in want:
            continue
        if not _context_match(e, expected):
            continue
        out.append(e)
    return out


def evaluate_round_close(
    wl: L.WorkerLayout,
    appended_ids: Mapping[str, Sequence[str]],
    *,
    run_id: str,
    branch_id: Optional[str] = None,
    subgoal_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a summary dict, or raise MemoryContractError (no next round)."""
    task = load_task_context(wl)
    expected = {
        "project_id": wl.project,
        "worker_id": wl.name,
        "run_id": run_id or os.environ.get("EULER_RUN_ID") or "",
        "branch_id": branch_id or task.branch_id or "",
        "subgoal_id": subgoal_id or task.subgoal_id or "",
    }
    gm = GlobalMemory(wl.project_dir)
    fg = FactGraph(wl.project_dir)
    verifs = _load_appended(gm, "verification", appended_ids.get("verification") or [], expected)
    for v in verifs:
        fact_id = v.get("fact_id")
        if not (isinstance(fact_id, str) and fact_id.strip()):
            continue
        fact_id = fact_id.strip()
        if (
            v.get("transport_status") == "ok"
            and v.get("verdict") == "correct"
            and v.get("provenance") == "fact_submit"
            and fg.exists(fact_id)
        ):
            return {"ok": True, "close": "success_fact", "fact_id": fact_id}

    attempts = _load_appended(
        gm, "proof_attempt", appended_ids.get("proof_attempt") or [], expected,
    )
    if not attempts:
        raise MemoryContractError(
            "no matching proof_attempt this round and no successful verification "
            "(timeouts/crashes must not fabricate math attempts)",
            expected=expected,
        )

    obstacles = _load_appended(gm, "obstacle", appended_ids.get("obstacle") or [], expected)
    dead_ends = _load_appended(gm, "dead_end", appended_ids.get("dead_end") or [], expected)
    obs_ids = {_attempt_id(e) for e in obstacles}
    obs_ids.discard(None)
    de_ids = {_attempt_id(e) for e in dead_ends}
    de_ids.discard(None)

    for pa in attempts:
        result = pa.get("result")
        aid = _attempt_id(pa)
        if result == "blocked":
            if not aid or aid not in obs_ids:
                raise MemoryContractError(
                    f"blocked proof_attempt {aid!r} has no matching obstacle link",
                    attempt_id=aid, result=result,
                )
        elif result == "abandoned":
            if not aid or aid not in de_ids:
                raise MemoryContractError(
                    f"abandoned proof_attempt {aid!r} has no matching dead_end link",
                    attempt_id=aid, result=result,
                )
        elif result in ("partial", "success"):
            continue
        elif result is None:
            raise MemoryContractError(
                "proof_attempt missing result=success|partial|blocked|abandoned",
                entry_id=pa.get("id"),
            )
        else:
            raise MemoryContractError(
                f"proof_attempt has unknown result {result!r}",
                attempt_id=aid,
            )
    return {
        "ok": True,
        "close": "artifacts",
        "attempts": len(attempts),
        "obstacles": len(obstacles),
        "dead_ends": len(dead_ends),
    }
