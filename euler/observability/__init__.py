# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.observability — the strictly read-only dashboard.

A FastAPI workbench for one project's teams, proof and research graphs, public
activity and optional acceptance evidence. Reads public files and projects
control events without instantiating mutable runtime stores.

    python -m euler.observability --project <dir> [--port 8099]

See ``app.py`` for the server + parsers.
"""

from __future__ import annotations

from .app import (
    CHANNELS,
    app,
    build_channel,
    build_channels,
    build_factgraph,
    build_overview,
    main,
)

__all__ = [
    "app",
    "main",
    "CHANNELS",
    "build_overview",
    "build_factgraph",
    "build_channels",
    "build_channel",
]
