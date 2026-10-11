# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.integrations — external services the engine grounds proofs against.

Currently: arXiv theorem search via ``matlas``. Kept as a thin, swappable
adapter — ``search`` is the stable surface a future worker can back with another
provider without touching the gateway.
"""

from __future__ import annotations

from .matlas import RESULT_FIELDS, search

__all__ = ["search", "RESULT_FIELDS"]
