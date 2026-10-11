# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.verify — the cold-start proof-verifier HTTP service (the sole write-gate).

``service.app`` is the FastAPI app (POST /verify, GET /health). Run as
``python -m euler.verify`` (or ``uvicorn euler.verify.service:app``).
"""

from __future__ import annotations

from .service import VerifyRequest, app
from .launcher import run_codex_verification, run_verification
from .prechecks import run_prechecks

__all__ = [
    "app", "VerifyRequest", "run_verification", "run_codex_verification",
    "run_prechecks",
]
