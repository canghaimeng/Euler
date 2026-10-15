# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Read-only BSD process observations for hosts without Linux procfs."""
from __future__ import annotations
import os
import subprocess


def bsd_process_field(pid: int, field: str) -> str:
    if type(pid) is not int or pid <= 0 or field not in {'stat', 'lstart', 'command'}:
        return ''
    try:
        result = subprocess.run(['/bin/ps', '-ww', '-p', str(pid), '-o', field + '='],
            capture_output=True, text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''
