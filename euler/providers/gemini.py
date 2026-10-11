"""Gemini CLI worker adapter. Persistent browser teams use the native ACP adapter."""
from __future__ import annotations

from .base import (AgentCommand, AgentRequest, ProviderError, ProviderName,
                   ProviderReadiness, binary_ready, isolate_provider_env, resolve_binary)


class GeminiProvider:
    name = ProviderName.GEMINI

    def binary(self):
        return resolve_binary('EULER_GEMINI_BIN', 'gemini')

    def readiness(self, request):
        binary = self.binary()
        config = request.worker_dir / '.gemini/settings.json'
        ready = binary_ready(binary) and config.is_file()
        return ProviderReadiness(ok=ready, provider=self.name, binary=binary, config=str(config),
                                 reason='' if ready else 'Gemini CLI or worker MCP settings unavailable')

    def scrub_environment(self, request, env):
        return isolate_provider_env(self.name, env, drop_consult=True)

    def build_command(self, request):
        if request.effort not in {'', 'default', 'provider-default', 'model-defined'}:
            raise ProviderError('Gemini effort is model-defined; no generic --effort flag is supported')
        argv = [self.binary(), '--output-format', 'text', '--approval-mode', 'yolo',
                '--skip-trust', '--allowed-mcp-server-names', 'euler']
        if request.model:
            argv += ['--model', request.model]
        if request.session_id:
            argv += ['--resume', request.session_id]
        argv += ['--prompt', request.prompt]
        return AgentCommand(provider=self.name, argv=tuple(argv),
                            env=self.scrub_environment(request, request.env_with_base()),
                            label=self.name.value, session_id=request.session_id)

    def recover_session(self, receipt):
        if not receipt.get('session_id') or not receipt.get('worker_dir') or not receipt.get('project_dir'):
            return None
        return self.build_command(AgentRequest(
            project_dir=receipt['project_dir'], worker_dir=receipt['worker_dir'],
            worker_name=str(receipt.get('worker_name', 'gemini')),
            project_id=str(receipt.get('project_id', 'project')), prompt=str(receipt.get('prompt', '')),
            model=str(receipt.get('model', '')), effort=str(receipt.get('effort', 'model-defined')),
            role_name=str(receipt.get('role_name', 'helper')), prompt_profile=str(receipt.get('prompt_profile', '')),
            session_id=str(receipt['session_id'])))
