# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Deployment service hooks preserve process ownership and never silently fall back."""
from types import SimpleNamespace
import subprocess
import pytest
from euler.execution.scaffold import spawn_loop


def test_service_launcher_passes_path_as_single_argument(tmp_path, monkeypatch):
    worker = tmp_path / 'worker with spaces'
    monkeypatch.setenv('EULER_WORKER_LOOP_LAUNCHER', '/deployment/worker-launch')
    seen = []
    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(stdout='12345\n')
    monkeypatch.setattr(subprocess, 'run', run)
    assert spawn_loop(worker) == 12345
    assert seen == [['/deployment/worker-launch', str(worker.resolve())]]


@pytest.mark.parametrize('output', ['0', '1', '-1', 'not-a-pid'])
def test_invalid_service_pid_never_falls_back(tmp_path, monkeypatch, output):
    monkeypatch.setenv('EULER_WORKER_LOOP_LAUNCHER', '/deployment/worker-launch')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=output))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('unsafe fallback'))
    with pytest.raises(ValueError):
        spawn_loop(tmp_path)


def test_failed_or_relative_service_launcher_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv('EULER_WORKER_LOOP_LAUNCHER', 'relative')
    with pytest.raises(ValueError, match='absolute'):
        spawn_loop(tmp_path)
    monkeypatch.setenv('EULER_WORKER_LOOP_LAUNCHER', '/deployment/worker-launch')
    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, a[0])
    monkeypatch.setattr(subprocess, 'run', fail)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('unsafe fallback'))
    with pytest.raises(subprocess.CalledProcessError):
        spawn_loop(tmp_path)
