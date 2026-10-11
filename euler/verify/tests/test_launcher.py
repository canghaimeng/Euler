# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Offline tests for euler.verify.launcher — command shape + subprocess plumbing.

No real model is ever launched. The subprocess path is exercised by pointing the
selected backend binary at tiny purpose-built stub scripts written into a temp
dir and asserting on the HTTPException status the launcher raises.

Covers:
  * build_codex_command: exec prefix, -C home, -c gateway injection, sandbox flag,
    output path in the prompt, bin resolved via euler.codex.
  * build_grok_command: exact Grok Build binary, 4.6/high, isolated state, and
    verifier-only MCP config.
  * build_cursor_command: retained rollback command shape.
  * subprocess_env: PATH-prepend for a concrete path; NO cwd injection for bare
    "codex".
  * _allocate_run_id: unique-dir retry on collision (FileExistsError branch).
  * _verification_path: found (each filename) and None-when-absent.
  * run_verification: success readback, 504 timeout, 500 nonzero-exit,
    500 missing-output, 500 bad-json, 500 non-dict-json.

Runs standalone (``python -m euler.verify.tests.test_launcher``) and under pytest.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from euler import codex
from euler.verify import launcher

_STMT = "For every integer n, n + 0 equals n."
_PROOF = "Zero is the additive identity; adding it changes nothing, so n + 0 = n."


@contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_stub(dirpath: Path, name: str, body: str) -> Path:
    p = dirpath / name
    p.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


# stub that writes a valid verification.json to the prompt's output path
_STUB_OK = """\
import re, sys, json
from pathlib import Path
prompt = sys.argv[-1]
out = Path(re.search(r'this exact path:\\s*(\\S+)', prompt).group(1).rstrip('.'))
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"verification_report": {
                               "summary": "stub accept",
                               "critical_errors": [], "gaps": []},
                           "verdict": "correct", "repair_hints": ""}))
print("ok")
"""

# stub that exits nonzero and writes nothing
_STUB_FAIL = "import sys\nsys.stderr.write('boom\\n')\nsys.exit(7)\n"

# stub that exits 0 but writes NO output file
_STUB_NOOUT = "print('did nothing')\n"

# stub that writes invalid JSON
_STUB_BADJSON = """\
import re, sys
from pathlib import Path
prompt = sys.argv[-1]
out = Path(re.search(r'this exact path:\\s*(\\S+)', prompt).group(1).rstrip('.'))
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("{ this is not json ")
"""

# stub that writes valid JSON that is NOT an object (a list)
_STUB_NONDICT = """\
import re, sys, json
from pathlib import Path
prompt = sys.argv[-1]
out = Path(re.search(r'this exact path:\\s*(\\S+)', prompt).group(1).rstrip('.'))
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(["not", "a", "dict"]))
"""

# stub that sleeps long enough to trip a 1s timeout
_STUB_SLOW = "import time\ntime.sleep(10)\n"


@contextmanager
def _service(stub_body: str, *, timeout: str = "0"):
    """Point the launcher at a stub codex + isolated results/home dirs."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        stub = _write_stub(tmpd, "fake.py", stub_body)
        with _env(EULER_VERIFY_BACKEND="codex",
                  EULER_CODEX_BIN=str(stub), CODEX_BIN=None,
                  VERIFIER_RESULTS_DIR=str(tmpd / "runs"),
                  VERIFY_AGENT_HOME=str(tmpd / "home"),
                  CODEX_TIMEOUT_SECONDS=timeout):
            (tmpd / "home").mkdir(exist_ok=True)
            yield


@contextmanager
def _cursor_service(stub_body: str, *, timeout: str = "0"):
    """Point the launcher at a stub cursor-agent with isolated state."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        stub = _write_stub(tmpd, "fake-cursor-agent", stub_body)
        auth = tmpd / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        with _env(
            EULER_VERIFY_BACKEND="cursor",
            EULER_CURSOR_BIN=str(stub),
            EULER_VERIFY_CURSOR_MODEL="cursor-grok-4.6-xhigh",
            EULER_CURSOR_AUTH_FILE=str(auth),
            EULER_VERIFY_CURSOR_HOME=str(tmpd / "cursor-home"),
            VERIFIER_RESULTS_DIR=str(tmpd / "runs"),
            VERIFY_AGENT_HOME=str(tmpd / "home"),
            CODEX_TIMEOUT_SECONDS=timeout,
        ):
            yield


@contextmanager
def _grok_service(stub_body: str, *, timeout: str = "0"):
    """Point the launcher at a stub Grok Build with isolated state."""
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        stub = _write_stub(tmpd, "fake-grok", stub_body)
        auth = tmpd / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        with _env(
            EULER_VERIFY_BACKEND="grok",
            EULER_GROK_BIN=str(stub),
            EULER_VERIFY_GROK_MODEL="grok-4.6",
            EULER_VERIFY_GROK_EFFORT="high",
            EULER_GROK_AUTH_FILE=str(auth),
            EULER_VERIFY_GROK_HOME=str(tmpd / "grok-home"),
            VERIFIER_RESULTS_DIR=str(tmpd / "runs"),
            VERIFY_AGENT_HOME=str(tmpd / "home"),
            CODEX_TIMEOUT_SECONDS=timeout,
        ):
            yield


# --------------------------------------------------------------------------- #
# build_codex_command / config resolution                                     #
# --------------------------------------------------------------------------- #

def test_build_codex_command_shape():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(EULER_CODEX_BIN="/abs/codex",
                  EULER_VERIFY_PYTHON="/abs/verify-python",
                  VERIFY_AGENT_HOME=str(tmp),
                  EULER_VERIFY_MODEL="m-test", EULER_VERIFY_EFFORT="e-test",
                  EULER_CODEX_MODEL=None, EULER_CODEX_EFFORT=None):
            cmd = launcher.build_codex_command("RID", _STMT, _PROOF)
    assert cmd[0] == "/abs/codex" and cmd[1] == "exec"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "m-test"
    assert '--config' in cmd and 'model_reasoning_effort="e-test"' in cmd
    assert "-C" in cmd  # agent home
    # gateway injected via -c with role=verifier
    assert "-c" in cmd
    assert any('mcp_servers.euler=' in a and 'EULER_ROLE="verifier"' in a for a in cmd)
    assert any('command="/abs/verify-python"' in a for a in cmd)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    # the prompt (final arg) names the exact output path
    assert cmd[-1].endswith("verification.json.")
    assert "Run_id: RID" in cmd[-1] and _STMT in cmd[-1]


def test_build_cursor_command_shape():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(
            EULER_VERIFY_BACKEND="cursor",
            EULER_CURSOR_BIN="/abs/cursor-agent",
            EULER_VERIFY_CURSOR_MODEL="cursor-grok-4.6-xhigh",
            VERIFY_AGENT_HOME=str(Path(tmp) / "home"),
        ):
            cmd = launcher.build_verifier_command("RID", _STMT, _PROOF)
    assert cmd[0] == "/abs/cursor-agent"
    assert "--print" in cmd and cmd[cmd.index("--output-format") + 1] == "text"
    assert "--force" in cmd and "--approve-mcps" in cmd and "--trust" in cmd
    assert cmd[cmd.index("--model") + 1] == "cursor-grok-4.6-xhigh"
    assert cmd[cmd.index("--workspace") + 1] == str(Path(tmp, "home").resolve())
    assert cmd[-1].endswith("verification.json.")


def test_build_grok_command_shape():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(
            EULER_VERIFY_BACKEND="grok",
            EULER_GROK_BIN="/abs/grok",
            EULER_VERIFY_GROK_MODEL="grok-4.6",
            EULER_VERIFY_GROK_EFFORT="high",
            VERIFY_AGENT_HOME=str(Path(tmp) / "home"),
        ):
            cmd = launcher.build_verifier_command("RID", _STMT, _PROOF)
    assert cmd[0] == "/abs/grok"
    assert cmd[cmd.index("--model") + 1] == "grok-4.6"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
    assert "--no-subagents" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("--cwd") + 1] == str(Path(tmp, "home").resolve())
    assert cmd[-1].endswith("verification.json.")


def test_unset_backend_defaults_to_grok():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(
            EULER_VERIFY_BACKEND=None,
            EULER_GROK_BIN="/abs/grok",
            VERIFY_AGENT_HOME=str(Path(tmp) / "home"),
        ):
            cmd = launcher.build_verifier_command("RID", _STMT, _PROOF)
    assert launcher.VERIFY_BACKENDS == {"codex", "cursor", "grok"}
    assert cmd[0] == "/abs/grok"


def test_invalid_backend_fails_closed():
    with _env(EULER_VERIFY_BACKEND="unknown"):
        try:
            launcher.build_verifier_command("RID", _STMT, _PROOF)
            assert False, "expected unsupported backend failure"
        except RuntimeError as exc:
            assert "unsupported EULER_VERIFY_BACKEND" in str(exc)


def test_cursor_backend_respects_global_disable():
    with _env(EULER_VERIFY_BACKEND="cursor", EULER_DISABLE_CURSOR="1"):
        try:
            launcher.build_verifier_command("RID", _STMT, _PROOF)
            assert False, "expected disabled Cursor verifier failure"
        except RuntimeError as exc:
            assert "EULER_DISABLE_CURSOR=1" in str(exc)


def test_mcp_config_preserves_venv_symlink_spelling():
    with tempfile.TemporaryDirectory() as tmp:
        venv_python = Path(tmp) / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.symlink_to("/usr/bin/python3")
        with _env(EULER_VERIFY_PYTHON=str(venv_python)):
            arg = launcher._mcp_config_arg()
        assert f'command="{venv_python}"' in arg
        assert 'command="/usr/bin/python3"' not in arg


def test_subprocess_env_prepends_dir_for_concrete_path():
    with tempfile.TemporaryDirectory() as tmp:
        binp = str(Path(tmp) / "codex")
        env = codex.subprocess_env(binp)
        assert env["PATH"].split(os.pathsep)[0] == str(Path(tmp).resolve())


def test_subprocess_env_no_cwd_injection_for_bare_name():
    before = os.environ.get("PATH", "")
    env = codex.subprocess_env("codex")
    # bare name has no dir component -> PATH must be untouched (no "." / cwd added)
    assert env["PATH"] == before


# --------------------------------------------------------------------------- #
# _allocate_run_id — collision retry                                          #
# --------------------------------------------------------------------------- #

def test_allocate_run_id_retries_on_collision():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(VERIFIER_RESULTS_DIR=str(Path(tmp) / "runs")):
            base = launcher.generate_run_id(_STMT)
            root = launcher._results_root()
            root.mkdir(parents=True, exist_ok=True)
            # pre-create the base dir so the first mkdir raises FileExistsError,
            # forcing the numeric-suffix retry branch (lines 92-95).
            (root / base).mkdir()
            # generate_run_id is timestamp-based; freeze it so the retry collides
            # deterministically on `base`.
            orig = launcher.generate_run_id
            launcher.generate_run_id = lambda s: base  # type: ignore[assignment]
            try:
                rid = launcher._allocate_run_id(_STMT)
            finally:
                launcher.generate_run_id = orig  # type: ignore[assignment]
            assert rid == f"{base}_2"
            assert (root / rid).is_dir()


# --------------------------------------------------------------------------- #
# _verification_path                                                          #
# --------------------------------------------------------------------------- #

def test_verification_path_found_and_absent():
    with tempfile.TemporaryDirectory() as tmp:
        with _env(VERIFIER_RESULTS_DIR=str(Path(tmp) / "runs")):
            rid = "RID1"
            d = launcher._results_dir(rid)
            d.mkdir(parents=True)
            assert launcher._verification_path(rid) is None  # nothing yet
            (d / launcher.VERIFICATION_FILENAMES[1]).write_text("{}")
            # the alternate filename is also recognized
            assert launcher._verification_path(rid).name == launcher.VERIFICATION_FILENAMES[1]
            (d / launcher.VERIFICATION_FILENAMES[0]).write_text("{}")
            # primary filename takes precedence
            assert launcher._verification_path(rid).name == launcher.VERIFICATION_FILENAMES[0]


# --------------------------------------------------------------------------- #
# run_codex_verification — success + every error mapping                      #
# --------------------------------------------------------------------------- #

def _run(rid="RID"):
    return launcher.run_verification(rid, _STMT, _PROOF)


def test_run_success_reads_back_payload():
    with _service(_STUB_OK):
        out = _run()
        assert out["verdict"] == "correct"
        assert out["verification_report"]["critical_errors"] == []


def test_grok_run_success_and_isolated_mcp():
    with _grok_service(_STUB_OK):
        out = _run()
        home = launcher._agent_home()
        assert (home / ".git").is_dir()
        grok_home = Path(os.environ["EULER_VERIFY_GROK_HOME"])
        auth = grok_home / "auth.json"
        assert auth.is_symlink()
        assert (grok_home / ".euler-grok-verifier-home").is_file()
        user_config = (grok_home / "config.toml").read_text(encoding="utf-8")
        assert '[mcp_servers.euler]' in user_config
        assert 'EULER_ROLE = "verifier"' in user_config
        assert 'args = ["-m", "euler.gateway"]' in user_config
        assert "EULER_PROJECT_DIR" not in user_config
        # Project-scoped copies are ignored as untrusted; do not shadow user config.
        assert not (home / ".grok" / "config.toml").exists()
    assert out["verdict"] == "correct"


def test_grok_child_env_scrubs_unrelated_tokens():
    with _env(
        EULER_VERIFY_BACKEND="grok",
        SOL_GATE_TOKEN="secret",
        EULER_CONSULT_API_KEY="secret",
        AWS_SECRET_ACCESS_KEY="secret",
        GITHUB_TOKEN="secret",
        EULER_VERIFY_GROK_HOME="/isolated/grok-home",
        VERIFY_AGENT_HOME="/isolated/agent-home",
    ):
        env = launcher._subprocess_env(["grok"])
    assert "SOL_GATE_TOKEN" not in env
    assert "EULER_CONSULT_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["HOME"] == "/isolated/agent-home"
    assert env["GROK_HOME"] == "/isolated/grok-home"
    assert env["GROK_CURSOR_MCPS_ENABLED"] == "false"
    assert env["GROK_CLAUDE_MCPS_ENABLED"] == "false"


def test_grok_child_inherits_explicit_proxy():
    with _env(
        EULER_VERIFY_BACKEND="grok",
        HTTP_PROXY="http://127.0.0.1:9",
        EULER_VERIFY_GROK_HOME="/isolated/grok-home",
        VERIFY_AGENT_HOME="/isolated/agent-home",
    ):
        env = launcher._subprocess_env(["grok"])
    assert env["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert env["GROK_HOME"] == "/isolated/grok-home"


def test_grok_verifier_key_auth_without_auth_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        agent = root / "home"
        grok_home = root / "grok-home"
        empty = root / "empty"
        host = root / "empty-host"
        empty.mkdir()
        host.mkdir()
        with _env(
            EULER_VERIFY_BACKEND="grok",
            VERIFY_AGENT_HOME=str(agent),
            EULER_VERIFY_GROK_HOME=str(grok_home),
            EULER_GROK_AUTH_FILE=None,
            GROK_HOME=str(empty),
            HOME=str(host),
            XAI_API_KEY="unit-test-key",
        ):
            launcher.ensure_agent_home()
            user_config = (grok_home / "config.toml").read_text(encoding="utf-8")
            assert 'EULER_ROLE = "verifier"' in user_config
            assert not (grok_home / "auth.json").exists()
            env = launcher._subprocess_env(["grok"])
            assert env["XAI_API_KEY"] == "unit-test-key"
            assert env["GROK_HOME"] == str(grok_home.resolve())


def test_grok_verifier_migrates_only_owned_legacy_project_config():
    from euler.providers.grok import render_euler_mcp_toml

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        home = root / "agent"
        legacy = home / ".grok" / "config.toml"
        legacy.parent.mkdir(parents=True)
        auth = root / "host-auth.json"
        auth.write_text("{}", encoding="utf-8")
        with _env(
            EULER_VERIFY_BACKEND="grok", VERIFY_AGENT_HOME=str(home),
            EULER_VERIFY_GROK_HOME=str(root / "isolated"),
            EULER_GROK_AUTH_FILE=str(auth),
        ):
            text = render_euler_mcp_toml(
                launcher._verify_python(),
                {"EULER_ROLE": "verifier", "EULER_AUTHOR": "verifier"},
            ).replace(
                "# Auto-generated by euler; do not hand-edit.",
                "# Auto-generated by Euler; verifier-only Grok Build MCP.",
            )
            legacy.write_text(text, encoding="utf-8")
            launcher._ensure_grok_home(home)
            assert not legacy.exists()
            user_config = root / "isolated" / "config.toml"
            assert 'EULER_ROLE = "verifier"' in user_config.read_text(encoding="utf-8")
            launcher._ensure_grok_home(home)
            assert not legacy.exists()
            assert auth.read_text(encoding="utf-8") == "{}"


def test_grok_verifier_rejects_custom_or_wrong_role_project_config():
    from euler.providers.grok import render_euler_mcp_toml

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        auth = root / "host-auth.json"
        auth.write_text("{}", encoding="utf-8")
        for role in ("worker", "main", "custom-verifier"):
            home = root / role
            config = home / ".grok" / "config.toml"
            config.parent.mkdir(parents=True)
            content = render_euler_mcp_toml(
                launcher._verify_python(),
                {"EULER_ROLE": "verifier" if role == "custom-verifier" else role,
                 "EULER_AUTHOR": "verifier"},
            )
            if role != "custom-verifier":
                content = content.replace(
                    "# Auto-generated by euler; do not hand-edit.",
                    "# Auto-generated by Euler; verifier-only Grok Build MCP.",
                )
            config.write_text(content, encoding="utf-8")
            isolated = root / f"{role}-isolated"
            with _env(
                EULER_VERIFY_BACKEND="grok", VERIFY_AGENT_HOME=str(home),
                EULER_VERIFY_GROK_HOME=str(isolated), EULER_GROK_AUTH_FILE=str(auth),
            ):
                try:
                    launcher._ensure_grok_home(home)
                    raise AssertionError("custom/wrong-role project config must fail closed")
                except RuntimeError as exc:
                    assert "override isolated user config" in str(exc)
            assert config.read_text(encoding="utf-8") == content
            assert not isolated.exists()


def test_grok_verifier_rejects_symlinked_project_config_without_touching_target():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        outside = root / "host-global"
        outside.mkdir()
        config = outside / "config.toml"
        config.write_text("# user configuration must survive\n", encoding="utf-8")
        home = root / "agent"
        home.mkdir()
        (home / ".grok").symlink_to(outside, target_is_directory=True)
        try:
            launcher._retire_legacy_grok_project_config(home)
            raise AssertionError("external project config must fail closed")
        except RuntimeError as exc:
            assert "override isolated user config" in str(exc)
        assert config.read_text(encoding="utf-8") == "# user configuration must survive\n"


def test_cursor_run_success_and_isolated_mcp():
    with _cursor_service(_STUB_OK):
        out = _run()
        home = launcher._agent_home()
        config = json.loads((home / ".cursor" / "mcp.json").read_text())
        assert (home / ".git").is_dir()
        assert set(config["mcpServers"]) == {"euler"}
        server = config["mcpServers"]["euler"]
        assert server["args"] == ["-m", "euler.gateway"]
        assert server["env"]["EULER_ROLE"] == "verifier"
        assert "EULER_PROJECT_DIR" not in server["env"]
        cursor_home = Path(os.environ["EULER_VERIFY_CURSOR_HOME"])
        auth = cursor_home / ".config/cursor/auth.json"
        assert auth.is_symlink()
        assert (cursor_home / ".euler-verifier-home").is_file()
    assert out["verdict"] == "correct"


def test_cursor_child_env_scrubs_unrelated_tokens():
    with _env(
        EULER_VERIFY_BACKEND="cursor",
        SOL_GATE_TOKEN="secret",
        EULER_CONSULT_API_KEY="secret",
        EULER_CODEX_API_KEY="secret",
        AWS_SECRET_ACCESS_KEY="secret",
        GITHUB_TOKEN="secret",
        EULER_VERIFY_CURSOR_HOME="/isolated/cursor-home",
    ):
        env = launcher._subprocess_env(["cursor-agent"])
    assert "SOL_GATE_TOKEN" not in env
    assert "EULER_CONSULT_API_KEY" not in env
    assert "EULER_CODEX_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["HOME"] == "/isolated/cursor-home"
    assert env["XDG_CONFIG_HOME"] == "/isolated/cursor-home/.config"


def test_payload_schema_fails_closed():
    bad_payloads = [
        {"verdict": "correct", "repair_hints": ""},
        {"verification_report": {"summary": "x", "critical_errors": [], "gaps": []},
         "verdict": "maybe", "repair_hints": ""},
        {"verification_report": {"summary": "x", "critical_errors": [], "gaps": []},
         "verdict": "wrong", "repair_hints": ""},
        {"verification_report": {"summary": "x", "critical_errors": [
            {"location": "proof", "issue": "bad"}], "gaps": []},
         "verdict": "correct", "repair_hints": ""},
    ]
    for payload in bad_payloads:
        try:
            launcher._validate_payload(payload, Path("verification.json"))
            assert False, f"expected schema rejection for {payload!r}"
        except HTTPException as exc:
            assert exc.status_code == 500


def test_prepopulated_custom_cursor_home_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        agent_home = root / "agent"
        cursor_home = root / "custom-cursor-home"
        cursor_home.mkdir()
        (cursor_home / ".cursor").mkdir()
        (cursor_home / ".cursor" / "mcp.json").write_text("{}", encoding="utf-8")
        auth = root / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        with _env(
            EULER_VERIFY_BACKEND="cursor",
            VERIFY_AGENT_HOME=str(agent_home),
            EULER_VERIFY_CURSOR_HOME=str(cursor_home),
            EULER_CURSOR_AUTH_FILE=str(auth),
        ):
            try:
                launcher.ensure_agent_home()
                assert False, "expected pre-populated Cursor HOME rejection"
            except RuntimeError as exc:
                assert "refusing pre-populated" in str(exc)


def test_prepopulated_custom_grok_home_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        agent_home = root / "agent"
        grok_home = root / "custom-grok-home"
        grok_home.mkdir()
        (grok_home / "config.toml").write_text("", encoding="utf-8")
        auth = root / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        with _env(
            EULER_VERIFY_BACKEND="grok",
            VERIFY_AGENT_HOME=str(agent_home),
            EULER_VERIFY_GROK_HOME=str(grok_home),
            EULER_GROK_AUTH_FILE=str(auth),
        ):
            try:
                launcher.ensure_agent_home()
                assert False, "expected pre-populated Grok HOME rejection"
            except RuntimeError as exc:
                assert "refusing pre-populated" in str(exc)


def test_run_timeout_504():
    with _service(_STUB_SLOW, timeout="1"):
        try:
            _run()
            assert False, "expected 504"
        except HTTPException as e:
            assert e.status_code == 504 and "timed out" in e.detail


def test_run_nonzero_exit_500():
    with _service(_STUB_FAIL):
        try:
            _run()
            assert False, "expected 500"
        except HTTPException as e:
            assert e.status_code == 500 and "exit code 7" in e.detail


def test_run_missing_output_500():
    with _service(_STUB_NOOUT):
        try:
            _run()
            assert False, "expected 500"
        except HTTPException as e:
            assert e.status_code == 500 and "was not found" in e.detail


def test_run_bad_json_500():
    with _service(_STUB_BADJSON):
        try:
            _run()
            assert False, "expected 500"
        except HTTPException as e:
            assert e.status_code == 500 and "not valid JSON" in e.detail


def test_run_non_dict_json_500():
    with _service(_STUB_NONDICT):
        try:
            _run()
            assert False, "expected 500"
        except HTTPException as e:
            assert e.status_code == 500 and "must be a JSON object" in e.detail


def test_ensure_agent_home_provisions_missing_home():
    # A fresh checkout has no verify agent home; ensure_agent_home builds it
    # (AGENTS.md = verifier contract, .agents/skills = verify skills) so the codex
    # -C dir exists. Regression for the live-found bug: service 500 on a missing home.
    with tempfile.TemporaryDirectory(prefix="verify_home_") as d:
        home = Path(d) / "agent"
        with _env(EULER_VERIFY_BACKEND="codex", VERIFY_AGENT_HOME=str(home)):
            got = launcher.ensure_agent_home()
            assert got == home.resolve()
            agents_md = home / "AGENTS.md"
            skills = home / ".agents" / "skills"
            assert agents_md.exists(), "AGENTS.md must be provisioned"
            assert skills.exists(), ".agents/skills must be provisioned"
            # they point at the repo's canonical sources
            assert agents_md.resolve() == (launcher._REPO_ROOT / "agents" / "contracts" / "verifier.md").resolve()
            assert skills.resolve() == (launcher._REPO_ROOT / "agents" / "skills" / "verify").resolve()
            # idempotent: a second call is a no-op and still valid
            launcher.ensure_agent_home()
            assert agents_md.exists() and skills.exists()


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  [ok] {name}")
    print("ALL LAUNCHER TESTS PASSED")


if __name__ == "__main__":
    main()
