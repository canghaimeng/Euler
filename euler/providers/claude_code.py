"""Native Claude Code CLI provider (`claude --print`, configurable wrapper)."""

from __future__ import annotations

from typing import Mapping

from .base import (
    AgentCommand,
    AgentRequest,
    ProviderName,
    ProviderReadiness,
    binary_ready,
    isolate_provider_env,
    resolve_binary,
)


class ClaudeCodeProvider:
    name = ProviderName.CLAUDE_CODE

    def binary(self) -> str:
        return resolve_binary("EULER_CLAUDE_CODE_BIN", "claude")

    def readiness(self, request: AgentRequest) -> ProviderReadiness:
        binary = self.binary()
        config = request.worker_dir / ".claude" / "mcp.json"
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
        binary = self.binary()
        model = request.model.strip()
        effort = request.effort or "high"
        config = request.worker_dir / ".claude" / "mcp.json"
        resume = (
            request.session_id
            or (request.recovery.get("CLAUDE_RESUME_SESSION") or "").strip()
            or (request.metadata.get("CLAUDE_RESUME_SESSION") or "").strip()
        )
        argv = [
            binary,
            "--print",
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
            "--mcp-config",
            str(config),
            "--strict-mcp-config",
        ]
        if model:
            argv.extend(["--model", model])
        if effort:
            argv.extend(["--effort", effort])
        if resume:
            argv.extend(["--resume", resume])
        argv.append(request.prompt)
        env = self.scrub_environment(request, request.env_with_base())
        return AgentCommand(
            provider=self.name,
            argv=tuple(argv),
            env=env,
            label=self.name.value,
            session_id=resume or None,
        )

    def recover_session(self, receipt: Mapping[str, object]) -> AgentCommand | None:
        session = receipt.get("session_id")
        prompt = receipt.get("prompt")
        if not isinstance(session, str) or not session.strip():
            return None
        if not isinstance(prompt, str):
            return None
        worker = receipt.get("worker_dir")
        project = receipt.get("project_dir")
        if not worker or not project:
            return None
        request = AgentRequest(
            project_dir=project,  # type: ignore[arg-type]
            worker_dir=worker,  # type: ignore[arg-type]
            worker_name=str(receipt.get("worker_name") or "worker"),
            project_id=str(receipt.get("project_id") or "project"),
            prompt=prompt,
            model=str(receipt.get("model") or ""),
            effort=str(receipt.get("effort") or ""),
            role_name=str(receipt.get("role_name") or ""),
            prompt_profile=str(receipt.get("prompt_profile") or ""),
            session_id=session.strip(),
        )
        return self.build_command(request)
