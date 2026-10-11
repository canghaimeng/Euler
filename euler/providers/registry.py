"""Explicit Euler provider registry. No model-prefix dispatch."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Mapping, Optional

from euler import codex

from .base import (
    ALL_PROVIDER_NAMES,
    NATIVE_PROVIDER_NAMES,
    AgentCommand,
    AgentProvider,
    AgentRequest,
    ProviderError,
    ProviderName,
    ProviderReadiness,
    freeze_str_map,
)
from .claude_code import ClaudeCodeProvider
from .codex import CodexProvider
from .cursor import CursorProvider
from .grok import GrokProvider
from .gemini import GeminiProvider

__all__ = [
    "NATIVE_PROVIDER_NAMES",
    "ALL_PROVIDER_NAMES",
    "provider_from_role",
    "read_role_file",
    "request_from_worker",
    "get_provider",
    "registered_providers",
    "build_command",
    "check_readiness",
    "scrub_worker_env",
]

_LEGACY_BACKEND = {
    "codex": ProviderName.CODEX,
    "claude-code": ProviderName.CLAUDE_CODE,
    "grok": ProviderName.GROK,
    "cursor": ProviderName.CURSOR,
    "gemini": ProviderName.GEMINI,
}

_PROVIDERS: Dict[ProviderName, AgentProvider] = {
    ProviderName.CODEX: CodexProvider(),
    ProviderName.CLAUDE_CODE: ClaudeCodeProvider(),
    ProviderName.GROK: GrokProvider(),
    ProviderName.CURSOR: CursorProvider(),
    ProviderName.GEMINI: GeminiProvider(),
}

_SESSION_KEYS = (
    "CLAUDE_RESUME_SESSION",
    "GROK_RESUME_SESSION",
    "GEMINI_RESUME_SESSION",
    "SESSION_ID",
)
_RECOVERY_KEYS = (
    "GROK_PERSISTENT",
    "CLAUDE_RESUME_SESSION",
    "GROK_RESUME_SESSION",
    "GEMINI_RESUME_SESSION",
)
_CORE_ROLE_KEYS = {
    "PROVIDER",
    "BACKEND",
    "backend",
    "MODEL",
    "REASONING_EFFORT",
    "ROLE",
    "PROMPT_PROFILE",
    "EULER_AUTHOR",
    "RUNNER",
}


def registered_providers() -> Dict[ProviderName, AgentProvider]:
    return dict(_PROVIDERS)


def get_provider(name: ProviderName | str) -> AgentProvider:
    if isinstance(name, ProviderName):
        key = name
    else:
        raw = str(name or "").strip().lower()
        try:
            key = ProviderName(raw)
        except ValueError as exc:
            raise ProviderError(
                f"unknown worker provider {raw!r}; "
                f"expected one of {[item.value for item in ALL_PROVIDER_NAMES]}"
            ) from exc
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise ProviderError(f"unknown worker provider {key.value!r}")
    return provider


def provider_from_role(role: Mapping[str, str]) -> ProviderName:
    raw = (role.get("PROVIDER") or role.get("provider") or "").strip().lower()
    if raw:
        try:
            return ProviderName(raw)
        except ValueError as exc:
            raise ProviderError(
                f"unknown worker provider {raw!r}; "
                f"expected one of {[item.value for item in ALL_PROVIDER_NAMES]}"
            ) from exc
    legacy = (role.get("BACKEND") or role.get("backend") or "").strip().lower()
    if legacy:
        mapped = _LEGACY_BACKEND.get(legacy)
        if mapped is None:
            raise ProviderError(
                f"unknown legacy BACKEND {legacy!r}; set PROVIDER= explicitly"
            )
        return mapped
    runner = (role.get("RUNNER") or "").strip().lower()
    if runner == "cursor-agent":
        return ProviderName.CURSOR
    return ProviderName.CODEX


def read_role_file(wl) -> Dict[str, str]:
    out = {
        "MODEL": codex.model(),
        "REASONING_EFFORT": "high",
        "ROLE": "high",
        "EULER_AUTHOR": wl.name,
    }
    if wl.role.exists():
        for raw in wl.role.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    out["PROVIDER"] = provider_from_role(out).value
    return out


def request_from_worker(
    wl,
    role: Mapping[str, str],
    prompt: str,
    *,
    base_env: Mapping[str, str] | None = None,
) -> AgentRequest:
    session = ""
    for key in _SESSION_KEYS:
        session = (role.get(key) or "").strip()
        if session:
            break
    recovery = {
        key: str(role[key])
        for key in _RECOVERY_KEYS
        if role.get(key)
    }
    metadata = {
        key: str(value)
        for key, value in role.items()
        if key not in _CORE_ROLE_KEYS and isinstance(value, str)
    }
    return AgentRequest(
        project_dir=wl.project_dir,
        worker_dir=wl.dir,
        worker_name=wl.name,
        project_id=wl.project,
        prompt=prompt,
        model=str(role.get("MODEL") or ""),
        effort=str(role.get("REASONING_EFFORT") or ""),
        role_name=str(role.get("ROLE") or wl.name),
        prompt_profile=str(role.get("PROMPT_PROFILE") or ""),
        session_id=session or None,
        recovery=freeze_str_map(recovery),
        metadata=freeze_str_map(metadata),
        base_env=freeze_str_map(base_env if base_env is not None else dict(os.environ)),
    )


def build_command(
    wl,
    role: Mapping[str, str],
    prompt: str,
) -> AgentCommand:
    provider = get_provider(provider_from_role(role))
    request = request_from_worker(wl, role, prompt)
    return provider.build_command(request)


def check_readiness(wl) -> ProviderReadiness:
    role = read_role_file(wl)
    provider = get_provider(provider_from_role(role))
    request = request_from_worker(wl, role, "")
    return provider.readiness(request)


def scrub_worker_env(
    role: Mapping[str, str],
    env: Mapping[str, str] | None = None,
    *,
    prompt: str = "",
    worker_dir: Path | None = None,
) -> dict:
    """Scrub a mapping the way the selected provider would for a child process."""
    name = provider_from_role(role)
    provider = get_provider(name)
    base = dict(os.environ if env is None else env)
    if worker_dir is None:
        request = AgentRequest(
            project_dir=Path("."),
            worker_dir=Path("."),
            worker_name=str(role.get("EULER_AUTHOR") or "worker"),
            project_id="project",
            prompt=prompt,
            model=str(role.get("MODEL") or ""),
            effort=str(role.get("REASONING_EFFORT") or ""),
            role_name=str(role.get("ROLE") or ""),
            prompt_profile=str(role.get("PROMPT_PROFILE") or ""),
            base_env=freeze_str_map(base),
            metadata=freeze_str_map({
                key: str(value) for key, value in role.items()
                if key not in _CORE_ROLE_KEYS and isinstance(value, str)
            }),
        )
    else:
        from euler.execution.layout import WorkerLayout

        request = request_from_worker(
            WorkerLayout(Path(worker_dir)), role, prompt, base_env=base,
        )
    return dict(provider.scrub_environment(request, base))
