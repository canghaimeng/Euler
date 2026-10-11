# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Run the human-summary service as a stdio MCP server:
``python -m euler.human_summary``.

Launched by ``bin/human-summary-mcp`` (which exports
EULER_HUMAN_SUMMARY_SKILL_DIR and the codex/project env).
"""

from .server import build_app

if __name__ == "__main__":
    build_app().run()
