# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.orchestration — the ``euler`` CLI verbs (the main agent's control surface).

The verbs ``list`` / ``new`` / ``assign`` / ``start`` / ``status`` / ``stop`` that
scaffold a project, hand each worker its per-round assignment, and start / monitor
/ stop the autonomous loops. The loop + on-disk layout they drive live in
``euler.execution``; this module is the verbs/UX only.

Run as ``python -m euler.orchestration`` (this is what ``bin/euler`` execs).
"""

from __future__ import annotations

from .cli import (
    build_parser,
    do_assign,
    do_list,
    do_new,
    do_start,
    do_status,
    do_stop,
    main,
    worker_status,
)

__all__ = [
    "do_new",
    "do_assign",
    "do_start",
    "do_status",
    "worker_status",
    "do_list",
    "do_stop",
    "build_parser",
    "main",
]
