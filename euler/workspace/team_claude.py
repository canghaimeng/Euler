# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Claude Code print-mode sessions; native forks are materialized on first send."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .milestones import _json
from .team import TeamError
from .team_acp import binary_for, copy_credential


def runtime(project, member, state):
    from .team_provider import _home, _mcp, isolation_policy, team_environment
    home = _home(project, member)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = team_environment(project, member, home, state)
    config = home / 'claude'
    config.mkdir(exist_ok=True)
    env['CLAUDE_CONFIG_DIR'] = str(config)
    source = Path(os.environ.get('EULER_TEAM_CLAUDE_AUTH', Path.home() / '.claude/.credentials.json'))
    have_file = copy_credential(source, config / '.credentials.json')
    if platform.system() == 'Darwin' and not env.get('CLAUDE_CODE_OAUTH_TOKEN'):
        # Claude's default macOS login lives in Keychain. Never print its value.
        result = subprocess.run(['security', 'find-generic-password', '-w', '-s', 'Claude Code-credentials'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            try:
                env['CLAUDE_CODE_OAUTH_TOKEN'] = json.loads(result.stdout)['claudeAiOauth']['accessToken']
            except (ValueError, KeyError, TypeError):
                raise TeamError('Claude Keychain credential format unsupported; sign in with claude auth login') from None
    if not have_file and not env.get('CLAUDE_CODE_OAUTH_TOKEN'):
        raise TeamError('Claude sign-in missing; run claude auth login first')
    _json(home / 'mcp.json', {'mcpServers': {'euler_team': _mcp(project, member)}})
    _json(home / 'isolation.json', isolation_policy(project, state))
    return home, env


def prepare_member(project, member, state):
    binary_for('claude-code')
    home, _ = runtime(project, member, state)
    source = member.get('restored_from')
    archive = Path(member['archive']) if member.get('archive') else None
    if source:
        if not archive or not archive.is_file() or archive.is_symlink():
            raise TeamError('captured Claude native session is missing')
        frozen = home / 'frozen' / archive.name
        frozen.parent.mkdir(exist_ok=True)
        if not frozen.exists():
            shutil.copyfile(archive, frozen)
    else:
        frozen = None
    return {'session_id': str(uuid.uuid4()), 'session_cwd': str(Path(project) / member['workdir']),
            'archive': str(frozen) if frozen else None, 'resume_path': str(frozen) if frozen else None,
            'session_materialized': False, 'recovery': 'FORK_READY' if source else 'CLI_READY'}


def persistence(project, member):
    from .team_provider import _home
    sid = member.get('session_id')
    if not sid:
        return {}
    matches = list((_home(project, member) / 'claude/projects').glob(f'**/{sid}.jsonl'))
    if len(matches) > 1:
        raise TeamError('Claude session archive identity is ambiguous')
    if not matches:
        return {}
    return {'archive': str(matches[0]), 'resume_path': None, 'session_materialized': True,
            'recovery': 'NATIVE_FORK' if member.get('restored_from') else 'NEW_SESSION'}


def argv(project, member, home, instructions):
    command = [binary_for('claude-code'), '--print', '--output-format', 'stream-json', '--verbose',
               '--dangerously-skip-permissions', '--strict-mcp-config', '--mcp-config', str(home / 'mcp.json'),
               '--setting-sources', '', '--model', member['model'], '--effort', member['effort'],
               '--append-system-prompt', instructions, '--disallowedTools', 'Agent,Task',
               '--add-dir', str(project)]
    if member.get('resume_path'):
        command += ['--resume', member['resume_path'], '--fork-session', '--session-id', member['session_id']]
    elif member.get('session_materialized'):
        command += ['--resume', member['session_id']]
    else:
        command += ['--session-id', member['session_id']]
    return command


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def run_turn(project, member, state, question, cancelled):
    from .team_provider import _instructions, runtime_launch
    home, env = runtime(project, member, state)
    command = argv(project, member, home, _instructions(project, member, state))
    with runtime_launch(project, member, state, home, env, command) as (wrapped, child_env):
        return _run_process(project, member, state, question, cancelled, wrapped, child_env)


def _run_process(project, member, state, question, cancelled, command, env):
    process = subprocess.Popen(command, cwd=Path(project) / member['workdir'], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, start_new_session=True)
    results = []
    def read():
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Never copy assistant thinking blocks or tool output to the UI.
            if event.get('type') == 'result':
                results.append({k: event.get(k) for k in ('result', 'session_id', 'is_error', 'subtype')})
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.stdin.write(question + '\n')
        process.stdin.close()
        deadline = time.monotonic() + state['policy']['turn_seconds']
        while process.poll() is None:
            if cancelled():
                raise TeamError('Claude turn interrupted by team control or quota guard')
            if time.monotonic() >= deadline:
                raise TeamError('bounded Claude turn deadline reached')
            time.sleep(.1)
        reader.join(timeout=5)
        result = results[-1] if results else {}
        if process.returncode or result.get('is_error') or result.get('subtype') != 'success':
            raise TeamError('Claude native turn failed; check CLI sign-in, model and permission settings')
        if result.get('session_id') != member['session_id']:
            raise TeamError('Claude returned a different native session identity')
        saved = persistence(project, member)
        if not saved:
            raise TeamError('Claude reply arrived without a persisted native session')
        return {'reply': result.get('result', ''), 'native_session_id': result['session_id'],
                'goal': member.get('goal'), **saved}
    finally:
        stop(process)
        reader.join(timeout=2)
        process.stdout.close()
