# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Strategy-layer config — read from the environment at CALL time (never import
time) so the gateway is testable and reconfigurable, mirroring euler.core /
euler.gateway.

The consult gateway talks to any OpenAI-compatible Responses endpoint. The
endpoint/model/pricing are all env driven via the ``EULER_CONSULT_*`` names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Default OpenAI-compatible model id (large-reasoning "pro" tier).
DEFAULT_MODEL = "gpt-5.5-pro"
# Default model for the claude_code (subscription) transport. Kept
# separate from DEFAULT_MODEL so opting into claude never picks up the gpt default.
DEFAULT_CLAUDE_CODE_MODEL = "claude-sonnet-4-5"
# Default per-1M-token USD pricing. Override via env to match your own contract —
# no magic constants baked into the transport.
DEFAULT_PRICE_IN = 31.5
DEFAULT_PRICE_OUT = 189.0
# Conservative default accounting rates for Claude consultation. Override via
# EULER_CONSULT_CLAUDE_CODE_PRICE_IN / _OUT to match the selected model and plan.
DEFAULT_CLAUDE_CODE_PRICE_IN = 10.0
DEFAULT_CLAUDE_CODE_PRICE_OUT = 50.0
# Native Anthropic-API transport (`--transport claude_api`): same default model and
# list price as the claude transport (both consult Claude; this one bills per-token
# to YOUR Anthropic API key instead of drawing on a subscription login).
DEFAULT_CLAUDE_API_MODEL = DEFAULT_CLAUDE_CODE_MODEL
DEFAULT_CLAUDE_API_PRICE_IN = DEFAULT_CLAUDE_CODE_PRICE_IN
DEFAULT_CLAUDE_API_PRICE_OUT = DEFAULT_CLAUDE_CODE_PRICE_OUT
# Default refusal-fallback model (claude-sonnet-4-5's safety classifiers can decline a
# request; the API then re-serves it on this model in the same call). "off"/"none"
# disables the fallback parameter entirely.
DEFAULT_CLAUDE_API_FALLBACK = "claude-opus-4-1"


def _first(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _bool(*names: str, default: bool) -> bool:
    """A boolean env knob: ``1/true/yes/on`` / ``0/false/no/off`` (case-free);
    anything unrecognized keeps the default."""
    raw = _first(*names)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class ConsultConfig:
    """A snapshot of the consult endpoint config, resolved from the env."""

    api_key: str | None
    base_url: str | None
    model: str
    price_in: float
    price_out: float
    timeout: float
    # Transport-level Responses parameters. Defaults suit OpenAI; a stricter
    # compatible gateway may reject one of them, and the consult then fails with
    # that endpoint's error naming it. The caller changes it on the next call
    # (``--background off`` / ``--store on``); these env values are the persistent
    # default for a deployment that always needs the override.
    background: bool = True
    store: bool = False

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def load_config() -> ConsultConfig:
    """Resolve the consult config from the environment (call time), reading the
    ``EULER_CONSULT_*`` names.
    """

    def _float(*names: str, default: float) -> float:
        raw = _first(*names)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return ConsultConfig(
        api_key=_first("EULER_CONSULT_API_KEY"),
        base_url=_first("EULER_CONSULT_BASE_URL"),
        model=_first("EULER_CONSULT_MODEL", default=DEFAULT_MODEL),
        price_in=_float("EULER_CONSULT_PRICE_IN", default=DEFAULT_PRICE_IN),
        price_out=_float("EULER_CONSULT_PRICE_OUT", default=DEFAULT_PRICE_OUT),
        timeout=_float("EULER_CONSULT_TIMEOUT", default=7200.0),
        background=_bool("EULER_CONSULT_BACKGROUND", default=True),
        store=_bool("EULER_CONSULT_STORE", default=False),
    )


def resolve_transport(cli_value: str | None) -> str:
    """Pick the transport: explicit CLI flag > ``EULER_CONSULT_TRANSPORT`` env
    > ``gpt_pro`` (the default / core direction-guidance path).

    Recognized transports: ``gpt_pro`` (paid OpenAI-compatible), ``codex``
    (ChatGPT Codex subscription via local sol-gate), ``claude_api``
    (paid Anthropic API, native SDK), ``claude_code`` (the Claude Code CLI via
    ``claude -p``, subscription auth), and ``off``. Any other value resolves to
    the ``gpt_pro`` default.
    """
    val = (cli_value or os.environ.get("EULER_CONSULT_TRANSPORT") or "gpt_pro").strip().lower()
    return val if val in ("off", "gpt_pro", "codex", "claude_api", "claude_code") else "gpt_pro"


@dataclass(frozen=True)
class ClaudeCodeConfig:
    """A snapshot of the claude_code (subscription) consult knobs, resolved from the env."""

    model: str
    claude_bin: str
    max_wall: float
    price_in: float
    price_out: float


def load_claude_code_config() -> ClaudeCodeConfig:
    """Resolve the ``--transport claude_code`` knobs from the environment (call time).

    Independent of the gpt_pro-path ``EULER_CONSULT_MODEL`` (which defaults to the gpt
    tier), so opting into claude does not require touching the gpt_pro config. The
    model can still be overridden per-call by the ``--model`` CLI flag.
    """

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return ClaudeCodeConfig(
        model=_first("EULER_CONSULT_CLAUDE_CODE_MODEL", default=DEFAULT_CLAUDE_CODE_MODEL),
        claude_bin=_first("EULER_CONSULT_CLAUDE_CODE_BIN", default="claude"),
        max_wall=_float("EULER_CONSULT_CLAUDE_CODE_MAX_WALL", 1800.0),
        price_in=_float("EULER_CONSULT_CLAUDE_CODE_PRICE_IN", DEFAULT_CLAUDE_CODE_PRICE_IN),
        price_out=_float("EULER_CONSULT_CLAUDE_CODE_PRICE_OUT", DEFAULT_CLAUDE_CODE_PRICE_OUT),
    )


@dataclass(frozen=True)
class ClaudeApiConfig:
    """A snapshot of the native Anthropic-API consult knobs, resolved from the env."""

    api_key: str | None
    base_url: str | None
    model: str
    fallback_model: str | None
    price_in: float
    price_out: float
    timeout: float

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def load_claude_api_config() -> ClaudeApiConfig:
    """Resolve the ``--transport claude_api`` knobs from the environment (call time).

    Independent of the gpt_pro-path ``EULER_CONSULT_*`` credentials and of the
    claude_code knobs, so opting into the Anthropic API touches neither.
    The key falls back to a plain ``ANTHROPIC_API_KEY`` for convenience — but note
    the ``claude_code`` transport deliberately scrubs that variable from ITS child env
    (a subscription consult must never silently turn into per-token billing);
    here per-token billing is exactly what was asked for.
    """

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    fallback = _first("EULER_CONSULT_CLAUDE_API_FALLBACK",
                      default=DEFAULT_CLAUDE_API_FALLBACK)
    if fallback and fallback.strip().lower() in ("off", "none", "disabled"):
        fallback = None
    return ClaudeApiConfig(
        api_key=_first("EULER_CONSULT_CLAUDE_API_KEY", "ANTHROPIC_API_KEY"),
        base_url=_first("EULER_CONSULT_CLAUDE_API_BASE_URL"),
        model=_first("EULER_CONSULT_CLAUDE_API_MODEL", default=DEFAULT_CLAUDE_API_MODEL),
        fallback_model=fallback,
        price_in=_float("EULER_CONSULT_CLAUDE_API_PRICE_IN", DEFAULT_CLAUDE_API_PRICE_IN),
        price_out=_float("EULER_CONSULT_CLAUDE_API_PRICE_OUT", DEFAULT_CLAUDE_API_PRICE_OUT),
        timeout=_float("EULER_CONSULT_TIMEOUT", 7200.0),
    )


@dataclass(frozen=True)
class CodexConsultConfig:
    """ChatGPT Codex subscription consult, always via local sol-gate."""

    base_url: str
    api_key: str | None
    model: str
    trigger: str
    role: str
    theorem: str
    timeout: float

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def load_codex_consult_config() -> CodexConsultConfig:
    """Resolve the ``--transport codex`` knobs. This never reads OPENAI_API_KEY."""

    def _float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    return CodexConsultConfig(
        base_url=(_first("EULER_CONSULT_BASE_URL", default="http://127.0.0.1:8088/v1") or "").rstrip("/"),
        api_key=_first("EULER_CONSULT_API_KEY", "SOL_GATE_TOKEN"),
        model=_first("EULER_CONSULT_MODEL", default="gpt-5.6"),
        trigger=_first("EULER_SOL_TRIGGER", default="CHECKPOINT") or "CHECKPOINT",
        role=_first("EULER_SOL_ROLE", default="strategy") or "strategy",
        theorem=_first("EULER_SOL_THEOREM", default="unspecified") or "unspecified",
        timeout=_float("EULER_CONSULT_TIMEOUT", 1800.0),
    )
