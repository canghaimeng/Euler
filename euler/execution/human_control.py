# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Cooperative lease-scoped cancellation for native subprocess workers."""
from __future__ import annotations

import os
import signal
import subprocess
import time

from euler.control.campaign import admission_lock, cancellation_requested, focused_record
from euler.control.dispatch import load_dispatch
from euler.control.project import is_euler_project, open_project
from euler.control.runtime import LeaseStatus


def managed_execution(project_dir, worker):
    if not is_euler_project(project_dir):
        return None
    sidecar = load_dispatch(project_dir, worker)
    if not sidecar or not isinstance(sidecar.get('binding'), dict):
        return None
    binding = sidecar['binding']
    with open_project(project_dir) as p:
        lease = next((l for l in p.runtime_snapshot().leases if l.lease_id == binding['lease_id']), None)
        if lease is None or lease.status not in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
            return {'cancelled': True, 'lease_id': binding['lease_id'], 'wall_seconds': None}
        return {'cancelled': cancellation_requested(p, lease.lease_id), 'lease_id': lease.lease_id,
                'wall_seconds': (max(0.001, lease.requested.wall_ms / 1000) if focused_record(p, lease.candidate_id) else None)}


def stop_local_process(proc):
    """Only the child group created by this worker; never an arbitrary saved PID."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def wait_with_control(proc, project_dir, worker, lease_id, timeout_seconds, log):
    start = time.monotonic()
    while True:
        try:
            return proc.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            with open_project(project_dir) as project:
                cancelled = cancellation_requested(project, lease_id)
            if cancelled:
                stop_local_process(proc)
                log.write('\n[worker_loop] human cancellation: local process group stopped; remote state unknown\n')
                return 130
            if timeout_seconds and time.monotonic() - start >= timeout_seconds:
                stop_local_process(proc)
                log.write('\n[worker_loop] focused/round physical wall-time allowance reached\n')
                return 124
