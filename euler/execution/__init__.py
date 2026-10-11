# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.execution — the per-worker autonomous outer loop + on-disk layout.

The self-driving round loop (``loop``), the project/worker scaffolding + detached
launch (``scaffold``), and the canonical on-disk layout every other module reads
(``layout``). ``euler.orchestration`` (the CLI verbs) is the thin UX shell over
this library.

Run one worker's loop directly with ``python -m euler.execution <worker_dir>``
(this is how ``euler start`` launches it, detached).
"""

from __future__ import annotations

from . import layout
from .layout import (
    DEADLINE_FILE,
    LOCK_FILE,
    LOGS_DIR,
    RECEIPTS_DIR,
    FAILURES_DIR,
    PID_FILE,
    ROLE_FILE,
    STATUS_FILE,
    STOP_FILE,
    TASK_FILE,
    WorkerLayout,
    agents_root,
    list_projects,
    list_workers,
    parse_roles,
    project_dir,
    repo_root,
    resolve_target,
    target_worker_dirs,
    worker_dir,
    worker_md,
    worker_skills_dir,
    workers_dir,
)
from .loop import kickoff, main, run_round, write_status
from .memory_digest import (
    RoundReceiptError,
    allocate_receipt_id,
    finalize_round_end,
    prepare_round_start,
    retrieve_bounded,
)
from .preflight import (
    CheckResult,
    PreflightResult,
    classify_verify_identity,
    doctor_checks,
    mcp_stdio_handshake,
    run_proving_preflight,
)
from .scaffold import atomic_write, do_new, spawn_loop, symlink, write_codex_config

__all__ = [
    # layout module + API
    "layout",
    "WorkerLayout",
    "agents_root",
    "repo_root",
    "project_dir",
    "workers_dir",
    "worker_dir",
    "list_workers",
    "list_projects",
    "worker_md",
    "worker_skills_dir",
    "resolve_target",
    "target_worker_dirs",
    "parse_roles",
    # control-file name constants
    "TASK_FILE",
    "ROLE_FILE",
    "PID_FILE",
    "LOCK_FILE",
    "STOP_FILE",
    "STATUS_FILE",
    "LOGS_DIR",
    "RECEIPTS_DIR",
    "FAILURES_DIR",
    "DEADLINE_FILE",
    # loop
    "main",
    "run_round",
    "write_status",
    "kickoff",
    "prepare_round_start",
    "allocate_receipt_id",
    "finalize_round_end",
    "retrieve_bounded",
    "RoundReceiptError",
    # proving preflight (fail closed before any worker round/process)
    "run_proving_preflight",
    "classify_verify_identity",
    "mcp_stdio_handshake",
    "doctor_checks",
    "PreflightResult",
    "CheckResult",
    # scaffolding
    "do_new",
    "spawn_loop",
    "atomic_write",
    "symlink",
    "write_codex_config",
]
