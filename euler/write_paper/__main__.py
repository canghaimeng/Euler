# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Run the write-paper service as a stdio MCP server:
``python -m euler.write_paper``.

Launched by ``bin/write-paper-mcp`` (which exports EULER_WRITE_PAPER_SKILL_DIR and the
codex/project env).
"""

from .server import build_app

if __name__ == "__main__":
    build_app().run()
