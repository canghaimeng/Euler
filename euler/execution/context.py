# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Proving-round structured context (branch/subgoal/run).

Stage 3: new proving rounds fail closed unless TASK.md carries a valid
``branch_id`` and ``subgoal_id``. Project/worker come from layout. ``run_id`` is
stable for one worker process and is stamped into MCP config + receipts.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, Optional

from . import layout as L
from .memory_digest import TaskContext, load_task_context
from .preflight import _fail, _ok


CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

STAMP_KEYS = (
    "project_id",
    "worker_id",
    "run_id",
    "branch_id",
    "subgoal_id",
    "provenance",
)


class TaskContextError(Exception):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details

    def to_status_dict(self) -> Dict[str, Any]:
        return {"ok": False, "reason": self.reason, **self.details}


def valid_context_id(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(CONTEXT_ID_RE.match(value))


def new_run_id() -> str:
    return str(uuid.uuid4())


def render_task_markdown(
    *,
    project_id: str,
    branch_id: str,
    subgoal_id: str,
    body: str,
) -> str:
    body = (body or "").strip("\n")
    return (
        f"---\n"
        f"project_id: {project_id}\n"
        f"branch_id: {branch_id}\n"
        f"subgoal_id: {subgoal_id}\n"
        f"---\n"
        f"{body}\n"
    )


def require_proving_task_context(wl: L.WorkerLayout) -> TaskContext:
    """Fail closed if branch_id or subgoal_id is missing/malformed."""
    ctx = load_task_context(wl)
    bad = []
    if not valid_context_id(ctx.branch_id):
        bad.append("branch_id")
    if not valid_context_id(ctx.subgoal_id):
        bad.append("subgoal_id")
    if bad:
        raise TaskContextError(
            f"missing or malformed {', '.join(bad)} "
            f"(euler assign --branch-id/--subgoal-id or TASK.md frontmatter)",
            fields=bad,
        )
    return ctx


def stamp_env(wl: L.WorkerLayout, task: TaskContext, run_id: str) -> Dict[str, str]:
    """Non-forgeable context the gateway reads. Project/worker from layout."""
    return {
        "EULER_PROJECT_ID": wl.project,
        "EULER_WORKER_ID": wl.name,
        "EULER_AUTHOR": wl.name,
        "EULER_RUN_ID": run_id,
        "EULER_BRANCH_ID": task.branch_id or "",
        "EULER_SUBGOAL_ID": task.subgoal_id or "",
        "EULER_PROVENANCE": "worker_mcp",
        "EULER_PROJECT_DIR": str(wl.project_dir),
        "EULER_ROLE": "worker",
    }


def apply_stamp_env(env: Dict[str, str]) -> None:
    for k, v in env.items():
        os.environ[k] = v


def extra_mcp_env(stamp: Dict[str, str]) -> Dict[str, str]:
    """Keys written into worker .codex/config.toml beyond the scaffold defaults."""
    keys = (
        "EULER_PROJECT_ID", "EULER_WORKER_ID", "EULER_RUN_ID",
        "EULER_BRANCH_ID", "EULER_SUBGOAL_ID", "EULER_PROVENANCE",
    )
    return {k: stamp[k] for k in keys if stamp.get(k)}


def check_round_context(wl: L.WorkerLayout, run_id: str) -> CheckResult:
    """MCP config env must match current TASK.md + this process run_id."""
    try:
        task = require_proving_task_context(wl)
    except TaskContextError as e:
        return _fail("round_context", e.reason, **e.details)
    path = wl.codex_config
    if not path.is_file():
        return _fail("round_context", f"missing MCP config {path}", path=str(path))
    from .preflight import _load_toml
    try:
        data = _load_toml(path)
    except Exception as e:
        return _fail("round_context", f"unreadable MCP config: {e}", path=str(path))
    env = ((data.get("mcp_servers") or {}).get("euler") or {}).get("env")
    if not isinstance(env, dict):
        return _fail("round_context", "MCP env table missing", path=str(path))
    expected = {
        "EULER_BRANCH_ID": task.branch_id,
        "EULER_SUBGOAL_ID": task.subgoal_id,
        "EULER_RUN_ID": run_id,
        "EULER_WORKER_ID": wl.name,
        "EULER_PROJECT_ID": wl.project,
        "EULER_ROLE": "worker",
    }
    mismatches = []
    for key, want in expected.items():
        got = env.get(key)
        if got != want:
            mismatches.append(f"{key}={got!r} expected {want!r}")
    if mismatches:
        return _fail(
            "round_context",
            "MCP config context does not match current task/run: "
            + "; ".join(mismatches),
            path=str(path),
        )
    return _ok("round_context", run_id=run_id, branch_id=task.branch_id,
               subgoal_id=task.subgoal_id)
