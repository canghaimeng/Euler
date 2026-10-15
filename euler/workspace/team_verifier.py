# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Runner-owned, loopback-only verifier using Euler's existing cold-start service.

A configured EULER_VERIFY_URL takes precedence. Starting the local service makes
no model call. A proof submission or final audit is the only inference trigger.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

from .team_acp import binary_for
from .milestones import configured


def verifier_profile(state):
    """Prefer an explicit review member, then an assistant, then the lead."""
    for role in ('verifier', 'assistant', 'lead'):
        for member in state['members']:
            if member['role'] != role or member['provider'] not in {'grok', 'codex', 'cursor'}:
                continue
            try:
                binary = binary_for(member['provider'])
            except ValueError:
                continue
            return {k: member[k] for k in ('provider', 'model', 'effort')} | {'binary': binary, 'member_id': member['id']}
    return None


def _stop(process):
    # All verifier subprocesses inherit this process group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


@contextmanager
def managed_verifier(project, state):
    if os.environ.get('EULER_VERIFY_URL'):
        yield
        return
    from euler.control.project import is_euler_project
    profile = verifier_profile(state) if is_euler_project(project) else None
    if profile is None:
        yield  # Research can proceed; proof tools report the missing verifier explicitly.
        return
    settings = configured(project)
    if not settings:
        raise ValueError('verification storage requires configured team milestones')
    home = Path(settings['store']) / 'verification' / state['instance_id']
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = os.environ.copy()
    env.update(EULER_VERIFY_BACKEND=profile['provider'],
        VERIFY_AGENT_HOME=str(home / 'agent'), VERIFIER_RESULTS_DIR=str(home / 'runs'),
        EULER_ROOT=str(Path(__file__).resolve().parents[2]))
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), env.get('PYTHONPATH')]))
    # Native authentication can refresh an expired account into the member's
    # private store. Reuse that current login for a cold verifier; the original
    # user auth file can still contain the expired token that seeded the session.
    member_auth = Path(project).resolve() / '.euler-provider' / profile['member_id'] / 'auth.json'
    have_member_auth = (member_auth.is_file() and not member_auth.is_symlink()
                        and member_auth.resolve().is_relative_to(Path(project).resolve()))
    if profile['provider'] == 'grok':
        env.update(EULER_GROK_BIN=profile['binary'], EULER_VERIFY_GROK_MODEL=profile['model'],
                   EULER_VERIFY_GROK_EFFORT=profile['effort'], EULER_VERIFY_GROK_HOME=str(home / 'grok'))
        if have_member_auth:
            env.setdefault('EULER_GROK_AUTH_FILE', str(member_auth))
        elif os.environ.get('EULER_TEAM_GROK_AUTH'):
            env.setdefault('EULER_GROK_AUTH_FILE', os.environ['EULER_TEAM_GROK_AUTH'])
    elif profile['provider'] == 'cursor':
        env.update(EULER_CURSOR_BIN=profile['binary'], EULER_VERIFY_CURSOR_MODEL=profile['model'],
                   EULER_VERIFY_CURSOR_HOME=str(home / 'cursor'))
    else:
        codex_home = home / 'codex'
        codex_home.mkdir(exist_ok=True, mode=0o700)
        source = member_auth if have_member_auth else Path(os.environ.get('EULER_TEAM_CODEX_AUTH', str(Path.home() / '.codex/auth.json')))
        if source.is_file():
            shutil.copyfile(source, codex_home / 'auth.json')
            (codex_home / 'auth.json').chmod(0o600)
        env.update(CODEX_HOME=str(codex_home), EULER_CODEX_BIN=profile['binary'],
                   EULER_VERIFY_MODEL=profile['model'], EULER_VERIFY_EFFORT=profile['effort'])
        from .team import quota_threshold
        if quota_threshold(state) is not None:
            env.pop('OPENAI_API_KEY', None)
            env.pop('CODEX_API_KEY', None)
            (codex_home / 'config.toml').write_text('forced_login_method = "chatgpt"\n')
    with socket.socket() as listener, (home / 'service.log').open('ab') as log:
        listener.bind(('127.0.0.1', 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'euler.verify.service:app',
            '--fd', str(listener.fileno()), '--log-level', 'warning'], env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            pass_fds=(listener.fileno(),), start_new_session=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        endpoint = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise ValueError('local verifier service exited; inspect its private service log')
                try:
                    with opener.open(endpoint + '/health', timeout=0.3) as response:
                        health = json.load(response)
                    if health.get('pid') == process.pid:
                        break
                except (OSError, ValueError):
                    time.sleep(0.05)
            else:
                raise ValueError('local verifier service did not become ready')
            os.environ['EULER_VERIFY_URL'] = endpoint + '/verify'
            try:
                yield
            finally:
                os.environ.pop('EULER_VERIFY_URL', None)
        finally:
            _stop(process)
