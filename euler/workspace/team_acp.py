# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Gemini and Cursor native ACP sessions, with isolated snapshot restoration.

These CLIs expose session/load, not a native session-fork API. Restoration
copies the captured native store into a branch-private provider home and resumes
there. The provider session ID is retained; the Euler team instance is distinct.
No transcript is summarized or converted into an invented replacement history.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil

from .milestones import _json
from .team import TeamError


BINARIES = {'codex': 'codex', 'grok': 'grok', 'claude-code': 'claude',
            'gemini': 'gemini', 'cursor': 'cursor-agent'}


def binary_for(provider):
    key = 'CLAUDE' if provider == 'claude-code' else provider.upper()
    binary = os.environ.get(f'EULER_TEAM_{key}_BIN') or shutil.which(BINARIES[provider])
    if not binary:
        raise TeamError(f'{BINARIES[provider]} CLI unavailable; install it or set EULER_TEAM_{key}_BIN')
    return binary


def copy_credential(source, target):
    """Only explicit credential files; no user hooks, MCPs or global settings."""
    source, target = Path(source), Path(target)
    if target.is_symlink():
        raise TeamError('provider credential target must not be a symlink')
    if source.is_file() and (not target.exists() or source.stat().st_mtime_ns > target.stat().st_mtime_ns):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target)
        target.chmod(0o600)
    return target.is_file()


def configure(project, member, home, env):
    provider = member['provider']
    binary = binary_for(provider)
    if provider == 'gemini':
        env['GEMINI_CLI_HOME'] = str(home)
        config = home / '.gemini'
        source = Path(os.environ.get('EULER_TEAM_GEMINI_AUTH_DIR', Path.home() / '.gemini'))
        if not copy_credential(source / 'oauth_creds.json', config / 'oauth_creds.json'):
            raise TeamError('Gemini sign-in missing; run gemini and sign in with Google first')
        copy_credential(source / 'google_accounts.json', config / 'google_accounts.json')
        # Gemini CLI 0.63 uses this explicit per-project storage registry.
        cwd = str(Path(project) / member['workdir'])
        _json(config / 'projects.json', {'projects': {cwd: 'euler-session'}})
        _json(config / 'settings.json', {
            'security': {'auth': {'selectedType': 'oauth-personal'}},
            'general': {'sessionRetention': {'enabled': False}},
            'tools': {'sandbox': False},
        })
        return [binary, '--acp', '--model', member['model'], '--approval-mode', 'yolo', '--skip-trust']
    if provider != 'cursor':
        raise TeamError('unsupported ACP provider')
    if os.environ.get('EULER_DISABLE_CURSOR') == '1':
        raise TeamError('Cursor disabled by EULER_DISABLE_CURSOR=1')
    env['CURSOR_CONFIG_DIR'] = str(home / 'cursor')
    env['CURSOR_DATA_DIR'] = str(home / 'cursor-data')
    env['XDG_CONFIG_HOME'] = str(home / 'config')
    if platform.system() != 'Darwin':
        source = Path(os.environ.get('EULER_TEAM_CURSOR_AUTH',
                      Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'cursor/auth.json'))
        if not copy_credential(source, home / 'config/cursor/auth.json'):
            raise TeamError('Cursor sign-in missing; run cursor-agent login first')
    # macOS Cursor uses its native account Keychain. No HOME redirection and no
    # credential extraction into checkpoint files are needed.
    return [binary, '--model', member['model'], '--force', '--approve-mcps',
            '--trust', '--sandbox', 'disabled', '--workspace', str(project), 'acp']


def authenticate(rpc, provider):
    result = rpc.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                         'clientInfo': {'name': 'euler_team', 'version': '0.3'}})
    if not result.get('agentCapabilities', {}).get('loadSession'):
        raise TeamError(f'{provider} CLI must support ACP session/load; upgrade the CLI')
    rpc.request('authenticate', {'methodId': 'oauth-personal' if provider == 'gemini' else 'cursor_login'})


def apply_model(rpc, sid, member, result):
    current = result.get('models', {}).get('currentModelId')
    options = result.get('configOptions', [])
    current = next((o.get('currentValue') for o in options if o.get('id') == 'model'), current)
    wanted = member['model']
    if member['provider'] == 'cursor' and wanted == 'auto':
        choices = next((o.get('options', []) for o in options if o.get('id') == 'model'), [])
        if any(o.get('value') == 'default[]' and o.get('name') == 'Auto' for o in choices):
            wanted = 'default[]'
    if current != wanted:
        # Successful set_model is the provider's validation of the exact ID.
        # No retry using an alternative model or silently dropped effort flag.
        rpc.request('session/set_model', {'sessionId': sid, 'modelId': wanted})
    return {'model': member['model'], 'native_model_id': wanted, 'effort_control': 'model-defined'}


def session_archive(home, member, sid, *, allow_empty=False):
    if member['provider'] == 'cursor':
        path = home / 'cursor/acp-sessions' / sid
        if not (path / 'store.db').is_file():
            if allow_empty and (path / 'meta.json').is_file():return None
            raise TeamError('Cursor ACP session store missing; native recovery cannot be guaranteed')
        return path
    paths = list((home / '.gemini/tmp').glob(f'*/chats/session-*{sid[:8]}*.json*'))
    paths = [p for p in paths if p.suffix in {'.json', '.jsonl'}]
    if len(paths) != 1:
        raise TeamError('Gemini native session persistence location is missing or ambiguous')
    return paths[0]


def restore_archive(home, member, archive, cwd):
    if archive.is_symlink():
        raise TeamError('native session archive must not be a symlink')
    if member['provider'] == 'cursor':
        target = home / 'cursor/acp-sessions' / member['restored_from']
        if not target.exists():
            shutil.copytree(archive, target)
            # Routing metadata only. The opaque conversation store is unchanged.
            meta = json.loads((target / 'meta.json').read_text())
            meta['cwd'] = cwd
            _json(target / 'meta.json', meta)
        return
    target = home / '.gemini/tmp/euler-session/chats' / archive.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(archive, target)


def prepare_member(project, member, state):
    from .team_provider import connection, _grok_mcp
    source = member.get('restored_from')
    archive = Path(member['archive']) if member.get('archive') else None
    if source and (not archive or not archive.exists()):
        raise TeamError('captured native session is missing')
    cwd = str(Path(project) / member['workdir'])
    with connection(project, member, state) as (rpc, home, _):
        args = {'cwd': cwd, 'mcpServers': _grok_mcp(project, member)}
        if source:
            restore_archive(home, member, archive, cwd)
            sid = source
            result = rpc.request('session/load', {**args, 'sessionId': sid})
        else:
            result = rpc.request('session/new', args)
            sid = result['sessionId']
        options = apply_model(rpc, sid, member, result)
        path = session_archive(home, member, sid, allow_empty=not source)
        return {'session_id': sid if path else None, 'archive': str(path) if path else None, 'session_cwd': cwd,
                'session_materialized': bool(path), 'provider_options': options,
                'recovery': 'ISOLATED_RESUME' if source else 'NEW_SESSION' if path else 'CLI_READY'}
