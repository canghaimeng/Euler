# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Run one worker's outer loop: ``python -m euler.execution <worker_dir>``.

This is how ``euler start`` launches each worker (detached, own process group).
"""

from __future__ import annotations

import sys

from .loop import main

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m euler.execution <worker_dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
