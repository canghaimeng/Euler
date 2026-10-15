# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""The ``FastMCP`` server class, resolved across mcp versions.

mcp 2.0 removed ``mcp.server.fastmcp`` and renamed the class to ``MCPServer``
(``mcp.server.mcpserver``). The surface Euler uses — ``FastMCP(name)``,
``app.tool(name=...)(fn)``, ``app.run()`` — is the same on both, so the three MCP
services import the class from here and work on either version.

Resolved by capability (try the import) rather than by version number: mcp exposes
no ``__version__``, and the 2.0 pre-releases already dropped the old module while
still comparing as ``< 2.0.0``, so a version test picks the wrong branch.
"""

from __future__ import annotations

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
except ImportError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError

__all__ = ["FastMCP", "ToolError"]
