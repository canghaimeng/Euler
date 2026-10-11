"""Native Cursor Agent CLI provider (`cursor-agent -p`)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .base import (
    AgentCommand,
    AgentRequest,
    ProviderError,
    ProviderName,
    ProviderReadiness,
    binary_ready,
    isolate_provider_env,
    resolve_binary,
)

CURSOR_HELPER_MARKER = "CURSOR HELPER ROLE BOUNDARY"


def cursor_disabled() -> bool:
    return os.environ.get("EULER_DISABLE_CURSOR", "0").strip() == "1"


def cursor_exec_cmd(
    binary: str,
    workspace: Path,
    prompt: str,
    *,
    model: str = "auto",
    add_dir: Path | None = None,
) -> list:
    """Exact Cursor helper argv: declared model, no fast/context flags."""
    argv = [
        binary,
        "-p",
        "--output-format", "text",
        "--force",
        "--approve-mcps",
        "--trust",
        "--sandbox", "disabled",
        "--workspace", str(workspace),
        "--model", model,
    ]
    if add_dir is not None:
        argv.extend(["--add-dir", str(add_dir)])
    argv.append(prompt)
    return argv


def cursor_helper_prompt(prompt: str, worker_name: str) -> str:
    """Pin Cursor to the helper/auditor role for every round."""
    return (
        f"{CURSOR_HELPER_MARKER}\n"
        f"You are Euler Cursor worker {worker_name}. Follow the assigned research "
        "contract and remain within this worker's scope. Your normal jobs include "
        "mathematical exploration, engineering, bounded diagnostics, and independent "
        "checking. Do not delegate or assign work. Submit mathematical claims only "
        "through Euler's verifier-gated fact path.\n"
        "Storage rules: use gm_search/gm_add for Global Memory and never read "
        "or write its JSONL files directly. Use fact_search first. You may then "
        "read only an exact verifier-accepted fact Markdown identified by the "
        "search result or signed round receipt. Never edit, enumerate, or "
        "bulk-scan the Fact Graph or its glossary; all fact admission goes "
        "through fact_submit. You may read the assigned Expert report and your "
        "own local_memory, but never another worker's local_memory, another "
        "project, parent/home configuration, credentials, or unrelated files. "
        "If a required MCP tool is missing or a requested action crosses this "
        "boundary, fail closed and report an obstacle.\n\n"
        "ORCHESTRATOR ASSIGNMENT AND SIGNED MEMORY DIGEST\n"
        f"{prompt}"
    )


class CursorProvider:
    name = ProviderName.CURSOR

    def binary(self) -> str:
        return resolve_binary("EULER_CURSOR_BIN", "cursor-agent")

    def readiness(self, request: AgentRequest) -> ProviderReadiness:
        if cursor_disabled():
            return ProviderReadiness(
                ok=False,
                provider=self.name,
                reason="Cursor provider is disabled by EULER_DISABLE_CURSOR=1",
            )
        binary = self.binary()
        config = request.worker_dir / ".cursor" / "mcp.json"
        if not binary_ready(binary):
            return ProviderReadiness(
                ok=False,
                provider=self.name,
                reason=f"provider binary is not executable: {binary}",
                binary=binary,
            )
        if not config.is_file():
            return ProviderReadiness(
                ok=False,
                provider=self.name,
                reason=f"provider MCP config is missing: {config}",
                binary=binary,
                config=str(config),
            )
        return ProviderReadiness(
            ok=True, provider=self.name, binary=binary, config=str(config),
        )

    def scrub_environment(
        self, request: AgentRequest, env: Mapping[str, str]
    ) -> Mapping[str, str]:
        return isolate_provider_env(self.name, env, drop_consult=True)

    def build_command(self, request: AgentRequest) -> AgentCommand:
        if cursor_disabled():
            raise ProviderError(
                "Cursor provider is disabled by EULER_DISABLE_CURSOR=1; "
                "use another enabled native provider instead"
            )
        binary = self.binary()
        model = request.model or "auto"
        prompt = cursor_helper_prompt(request.prompt, request.worker_name)
        argv = cursor_exec_cmd(
            binary,
            request.worker_dir,
            prompt,
            model=model,
            add_dir=request.project_dir,
        )
        env = self.scrub_environment(request, request.env_with_base())
        return AgentCommand(
            provider=self.name,
            argv=tuple(argv),
            env=env,
            label=self.name.value,
            session_id=request.session_id,
        )

    def recover_session(self, receipt: Mapping[str, object]) -> AgentCommand | None:
        return None
