# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""euler.strategy — the stateless strategic-consult gateway.

Feeds an elaboration to a strong model (default gpt-5.5-pro) over an
OpenAI-compatible Responses API and returns a uniform JSON envelope
(``reply`` + ``cost_usd`` + ``usage`` + ``transport``). It touches no truth
stores — the only file it writes is the per-project spend ledger.

Run as ``python -m euler.strategy`` (bin/consult wraps it).
"""

from __future__ import annotations

from .cli import main
from .config import ConsultConfig, load_config, resolve_transport
from .ledger import ledger_path, log_spend
from .transport import CodexTransport, GptProTransport, OffTransport, Transport, shape_envelope, tools_for

__all__ = [
    "main",
    "ConsultConfig",
    "load_config",
    "resolve_transport",
    "ledger_path",
    "log_spend",
    "Transport",
    "CodexTransport",
    "GptProTransport",
    "OffTransport",
    "shape_envelope",
    "tools_for",
]
