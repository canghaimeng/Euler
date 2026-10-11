# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Run the euler gateway as a stdio MCP server: ``python -m euler.gateway``.

Role is taken from ``EULER_ROLE`` (env). Launched by ``bin/euler-mcp`` (main) and
by each worker's ``.codex/config.toml`` (worker) / the verifier's ``-c`` override.
"""

from .server import build_app

if __name__ == "__main__":
    build_app().run()
