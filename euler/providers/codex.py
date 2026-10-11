"""Native Codex CLI provider (`codex exec`)."""

from __future__ import annotations

from typing import Mapping

from euler import codex

from .base import (
    AgentCommand,
    AgentRequest,
    ProviderName,
    ProviderReadiness,
    binary_ready,
    isolate_provider_env,
)


class CodexProvider:
    name = ProviderName.CODEX

    def readiness(self, request: AgentRequest) -> ProviderReadiness:
        binary = codex.resolve_bin()
        config = request.worker_dir / ".codex" / "config.toml"
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
        return isolate_provider_env(self.name, env, drop_consult=False)

    def build_command(self, request: AgentRequest) -> AgentCommand:
        binary = codex.resolve_bin()
        model = request.model or codex.model()
        effort = request.effort or "high"
        argv = tuple(codex.exec_cmd(
            binary,
            model,
            effort,
            "-C",
            str(request.worker_dir),
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            request.prompt,
        ))
        env = self.scrub_environment(request, codex.subprocess_env(binary))
        return AgentCommand(
            provider=self.name,
            argv=argv,
            env=env,
            label=self.name.value,
            session_id=request.session_id,
            output_format="codex-json",
        )

    def recover_session(self, receipt: Mapping[str, object]) -> AgentCommand | None:
        return None
