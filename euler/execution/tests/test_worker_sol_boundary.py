# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Worker Sol-secret boundary: Python helper + env.sh / bin wrappers.

No live Grok, no gitignored secrets, no /home/box globals.
The env.sh / bin/consult checks are wrapper guards, not an OS sandbox.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from euler.execution import layout as L
from euler.execution import loop

_SCRUB = (
    "SOL_GATE_TOKEN",
    "EULER_CONSULT_API_KEY",
    "SOL_CODEX_HOME",
    "SOL_CODEX_BIN",
)
_KEEP = ("EULER_CODEX_API_KEY", "XAI_GATE_TOKEN")


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _repo() -> Path:
    return L.repo_root()


def _sol_payload() -> dict:
    return {
        "SOL_GATE_TOKEN": "soltok",
        "EULER_CONSULT_API_KEY": "ckey",
        "SOL_CODEX_HOME": "/sol/home",
        "SOL_CODEX_BIN": "/sol/bin",
        "EULER_CODEX_API_KEY": "xai-keep",
        "XAI_GATE_TOKEN": "xai-gate",
        "EULER_ENV_VERBOSE": "0",
    }


def _dump_keys_py() -> str:
    keys = list(_SCRUB) + list(_KEEP) + ["EULER_ROLE"]
    return (
        "import os, json; print(json.dumps({k: os.environ.get(k) for k in "
        + repr(keys)
        + "}))"
    )


def test_env_sh_unsets_sol_after_config_load_for_worker():
    env_sh = _repo() / "scripts" / "env.sh"
    text = env_sh.read_text(encoding="utf-8")
    assert text.index("config/euler.env") < text.index("unset SOL_GATE_TOKEN")
    unset_line = [ln for ln in text.splitlines() if "unset SOL_GATE_TOKEN" in ln][0]
    assert "EULER_CODEX_API_KEY" not in unset_line

    env = os.environ.copy()
    env.update(_sol_payload())
    env["EULER_ROLE"] = "worker"
    script = (
        "set +u\n"
        f". {shlex.quote(str(env_sh))}\n"
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(_dump_keys_py())}\n"
    )
    r = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    for key in _SCRUB:
        assert got[key] is None, key
    # xAI key may be rehydrated from config/providers.env; must remain set.
    assert got["EULER_CODEX_API_KEY"]
    assert got["XAI_GATE_TOKEN"] == "xai-gate"
    assert got["EULER_ROLE"] == "worker"


def test_env_sh_keeps_sol_when_not_worker():
    env_sh = _repo() / "scripts" / "env.sh"
    env = os.environ.copy()
    env.update(_sol_payload())
    env.pop("EULER_ROLE", None)
    script = (
        "set +u\n"
        f". {shlex.quote(str(env_sh))}\n"
        f"exec {shlex.quote(sys.executable)} -c {shlex.quote(_dump_keys_py())}\n"
    )
    r = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["SOL_GATE_TOKEN"] == "soltok"
    assert got["EULER_CONSULT_API_KEY"] == "ckey"
    assert got["SOL_CODEX_HOME"] == "/sol/home"
    assert got["SOL_CODEX_BIN"] == "/sol/bin"
    assert got["EULER_CODEX_API_KEY"]


def test_bin_codex_unsets_sol_after_sourcing_env():
    text = (_repo() / "bin" / "codex").read_text(encoding="utf-8")
    assert text.index("env.sh") < text.index("unset SOL_GATE_TOKEN")
    assert 'EULER_ROLE:-}' in text or "${EULER_ROLE:-}" in text


def test_bin_consult_refuses_worker_before_sourcing_env():
    script = _repo() / "bin" / "consult"
    text = script.read_text(encoding="utf-8")
    assert text.index("EULER_ROLE") < text.index("env.sh")
    assert "not an OS sandbox" in text
    env = os.environ.copy()
    env["EULER_ROLE"] = "worker"
    env["EULER_ENV_VERBOSE"] = "1"
    r = subprocess.run(
        [str(script), "--help"], env=env, capture_output=True, text=True,
    )
    assert r.returncode == 4
    assert "wrapper guard" in r.stderr.lower() or "not an OS sandbox" in r.stderr
    assert "EULER_ROOT=" not in r.stderr
    assert "EULER_ROOT=" not in r.stdout


def test_worker_child_env_grok_scrubs_keeps_xai():
    payload = _sol_payload()
    with _env(**payload, EULER_ROLE="main"):
        out = loop.worker_child_env("/opt/grok", {"PROVIDER": "grok", "MODEL": "grok-4.6"})
    for key in _SCRUB:
        assert key not in out, key
    assert "EULER_CODEX_API_KEY" not in out
    assert out["XAI_GATE_TOKEN"] == "xai-gate"
    assert out["EULER_ROLE"] == "worker"


def test_worker_child_env_non_grok_keeps_sol_stamps_role():
    payload = _sol_payload()
    with _env(**payload):
        out = loop.worker_child_env(
            "/opt/codex/bin/codex",
            {"PROVIDER": "codex", "MODEL": "gpt-5.5"},
        )
    assert out["SOL_GATE_TOKEN"] == "soltok"
    assert out["EULER_CONSULT_API_KEY"] == "ckey"
    assert out["EULER_CODEX_API_KEY"] == "xai-keep"
    assert "XAI_GATE_TOKEN" not in out
    assert out["EULER_ROLE"] == "worker"


def test_run_round_grok_child_env_has_no_sol(tmp_path: Path):
    wl = L.WorkerLayout(tmp_path / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    fake = tmp_path / "fake_codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, json, sys\n"
        "keys = "
        + repr(list(_SCRUB) + list(_KEEP) + ["EULER_ROLE"])
        + "\n"
        "json.dump({k: os.environ.get(k) for k in keys}, sys.stdout)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    log = wl.dir / "round.log"
    payload = _sol_payload()
    with _env(**payload, EULER_GROK_BIN=str(fake)):
        rc = loop.run_round(
            wl, {"PROVIDER": "grok", "MODEL": "grok-4.6", "REASONING_EFFORT": "xhigh"},
            "prompt", log, hard_timeout=30,
        )
    assert rc == 0
    got = json.loads(log.read_text())
    for key in _SCRUB:
        assert got[key] is None, key
    assert got["EULER_CODEX_API_KEY"] is None
    assert got["XAI_GATE_TOKEN"] == "xai-gate"
    assert got["EULER_ROLE"] == "worker"
