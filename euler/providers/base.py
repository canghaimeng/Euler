"""Provider-neutral protocol for native Euler agent adapters.

Adapters own CLI flags, binary/model/effort/readiness, session recovery, and
environment scrubbing. The execution loop consumes only AgentRequest/AgentCommand.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Protocol, Sequence

__all__ = [
    "ProviderName",
    "ProviderError",
    "AgentRequest",
    "AgentCommand",
    "ProviderReadiness",
    "AgentProvider",
    "NATIVE_PROVIDER_NAMES",
    "ALL_PROVIDER_NAMES",
    "CONSULT_SECRET_KEYS",
    "SECRET_KEYS_BY_PROVIDER",
    "freeze_str_map",
    "isolate_provider_env",
    "resolve_binary",
    "binary_ready",
]


class ProviderName(str, Enum):
    CODEX = "codex"
    CLAUDE_CODE = "claude-code"
    GROK = "grok"
    CURSOR = "cursor"
    GEMINI = "gemini"


class ProviderError(ValueError):
    """Unknown provider, illegal request, or adapter configuration failure."""


NATIVE_PROVIDER_NAMES: tuple[ProviderName, ...] = (
    ProviderName.CODEX,
    ProviderName.CLAUDE_CODE,
    ProviderName.GROK,
    ProviderName.CURSOR,
    ProviderName.GEMINI,
)
ALL_PROVIDER_NAMES: tuple[ProviderName, ...] = NATIVE_PROVIDER_NAMES

CONSULT_SECRET_KEYS: tuple[str, ...] = (
    "SOL_GATE_TOKEN",
    "EULER_CONSULT_API_KEY",
    "SOL_CODEX_HOME",
    "SOL_CODEX_BIN",
)

SECRET_KEYS_BY_PROVIDER: Mapping[ProviderName, tuple[str, ...]] = {
    ProviderName.CODEX: (
        "EULER_CODEX_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
    ),
    ProviderName.CLAUDE_CODE: (
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "EULER_CLAUDE_API_KEY",
        "EULER_CONSULT_CLAUDE_API_KEY",
    ),
    ProviderName.GROK: (
        "XAI_API_KEY",
        "XAI_GATE_TOKEN",
        "EULER_GROK_API_KEY",
        "GROK_API_KEY",
    ),
    ProviderName.CURSOR: (
        "CURSOR_API_KEY",
        "EULER_CURSOR_API_KEY",
    ),
    ProviderName.GEMINI: ("GEMINI_API_KEY", "GOOGLE_API_KEY", "EULER_GEMINI_API_KEY"),
}


def freeze_str_map(value: Mapping[str, str] | None) -> Mapping[str, str]:
    data: dict[str, str] = {}
    if value:
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ProviderError("environment and metadata maps must be str→str")
            data[key] = item
    return MappingProxyType(data)


def isolate_provider_env(
    provider: ProviderName,
    env: Mapping[str, str],
    *,
    drop_consult: bool,
) -> dict[str, str]:
    """Drop other adapters' secrets. Never log the resulting values."""
    out = dict(env)
    for control_key in ('EULER_CONTROL_TOKEN', 'EULER_HUMAN_CONFIG', 'EULER_CONTROL_ACTOR'):
        out.pop(control_key, None)
    own = set(SECRET_KEYS_BY_PROVIDER.get(provider, ()))
    for name, keys in SECRET_KEYS_BY_PROVIDER.items():
        if name is provider:
            continue
        for key in keys:
            if key not in own:
                out.pop(key, None)
    if drop_consult:
        for key in CONSULT_SECRET_KEYS:
            out.pop(key, None)
    out["EULER_ROLE"] = "worker"
    return out


def resolve_binary(env_key: str, default: str) -> str:
    configured = os.environ.get(env_key) or default
    if os.path.sep in configured:
        return str(Path(configured).expanduser())
    return shutil.which(configured) or configured


def binary_ready(binary: str) -> bool:
    if os.path.sep in binary:
        path = Path(binary)
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(binary) is not None


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentRequest:
    """Provider-neutral launch request. Adapters must not require extra fields."""

    project_dir: Path
    worker_dir: Path
    worker_name: str
    project_id: str
    prompt: str
    model: str
    effort: str
    role_name: str
    prompt_profile: str
    session_id: Optional[str] = None
    recovery: Mapping[str, str] = MappingProxyType({})
    metadata: Mapping[str, str] = MappingProxyType({})
    base_env: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_dir", Path(self.project_dir))
        object.__setattr__(self, "worker_dir", Path(self.worker_dir))
        if not str(self.worker_name).strip():
            raise ProviderError("worker_name is required")
        if not str(self.project_id).strip():
            raise ProviderError("project_id is required")
        if not isinstance(self.prompt, str):
            raise ProviderError("prompt must be str")
        object.__setattr__(self, "model", str(self.model or "").strip())
        object.__setattr__(self, "effort", str(self.effort or "").strip())
        object.__setattr__(self, "role_name", str(self.role_name or "").strip())
        object.__setattr__(self, "prompt_profile", str(self.prompt_profile or ""))
        session = self.session_id
        if session is not None:
            session = str(session).strip() or None
        object.__setattr__(self, "session_id", session)
        object.__setattr__(self, "recovery", freeze_str_map(self.recovery))
        object.__setattr__(self, "metadata", freeze_str_map(self.metadata))
        object.__setattr__(self, "base_env", freeze_str_map(self.base_env))

    def env_with_base(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(self.base_env or os.environ)
        if extra:
            env.update(extra)
        return env


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentCommand:
    provider: ProviderName
    argv: tuple[str, ...]
    env: Mapping[str, str]
    label: str
    session_id: Optional[str] = None
    output_format: str = "plain"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider", ProviderName(self.provider)
        )
        argv = tuple(str(part) for part in self.argv)
        if not argv:
            raise ProviderError("AgentCommand.argv must be nonempty")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "env", freeze_str_map(self.env))
        object.__setattr__(self, "label", str(self.label or self.provider.value))
        session = self.session_id
        if session is not None:
            session = str(session).strip() or None
        object.__setattr__(self, "session_id", session)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderReadiness:
    ok: bool
    provider: ProviderName
    reason: str = ""
    binary: str = ""
    config: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", ProviderName(self.provider))
        object.__setattr__(self, "ok", bool(self.ok))
        object.__setattr__(self, "reason", str(self.reason or ""))
        object.__setattr__(self, "binary", str(self.binary or ""))
        object.__setattr__(self, "config", str(self.config or ""))


class AgentProvider(Protocol):
    name: ProviderName

    def readiness(self, request: AgentRequest) -> ProviderReadiness: ...

    def build_command(self, request: AgentRequest) -> AgentCommand: ...

    def scrub_environment(
        self, request: AgentRequest, env: Mapping[str, str]
    ) -> Mapping[str, str]: ...

    def recover_session(self, receipt: Mapping[str, object]) -> AgentCommand | None: ...
