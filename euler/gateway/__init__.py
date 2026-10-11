# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.gateway — the role-gated MCP server (the only door to the truth stores).

See ``server.build_app`` (constructs the stdio MCP app for a role) and ``roles``
(the permission table). Run as ``python -m euler.gateway``.
"""

from __future__ import annotations

from .roles import ALL_TOOLS, ROLE_TOOLS, tools_for
from .server import build_app

__all__ = ["build_app", "tools_for", "ROLE_TOOLS", "ALL_TOOLS"]
