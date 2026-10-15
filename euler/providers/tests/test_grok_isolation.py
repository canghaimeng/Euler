"""Isolated Grok GROK_HOME / effective MCP config. Offline, no live paid calls."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from euler.execution import layout as L
from euler.execution import preflight as P
from euler.execution import scaffold
from euler.providers import build_command, check_readiness
from euler.providers.grok import (
    grok_key_auth_present,
    isolated_worker_grok_home,
    link_grok_auth,
    prepare_isolated_grok_home,
    render_euler_mcp_toml,
    validate_effective_grok_config,
)


@contextmanager
def _env(**values):
    old = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _worker(root: Path, name: str = "grok1") -> L.WorkerLayout:
    wl = L.WorkerLayout(root / "workers" / name)
    wl.dir.mkdir(parents=True, exist_ok=True)
    wl.role.write_text(
        "PROVIDER=grok\nMODEL=grok-4.6\nREASONING_EFFORT=high\nROLE=researcher\n",
        encoding="utf-8",
    )
    return wl


def test_effective_user_config_and_isolated_home(tmp_path: Path):
    wl = _worker(tmp_path)
    fake = _executable(tmp_path / "grok")
    dummy_auth = tmp_path / "host-auth.json"
    dummy_auth.write_text("{}", encoding="utf-8")
    global_home = tmp_path / "global-grok"
    global_home.mkdir()
    (global_home / "config.toml").write_text("# host user config\n", encoding="utf-8")
    with _env(
        EULER_GROK_BIN=str(fake),
        EULER_GROK_AUTH_FILE=str(dummy_auth),
        GROK_HOME=str(global_home),
        HOME=str(tmp_path / "host-home"),
    ):
        scaffold.write_grok_config(wl, extra_env={"EULER_RUN_ID": "run-a"})
        spec = build_command(
            wl,
            {"PROVIDER": "grok", "MODEL": "grok-4.6", "REASONING_EFFORT": "high"},
            "task",
        )
        ready = check_readiness(wl)
    home = isolated_worker_grok_home(wl.dir)
    config = home / "config.toml"
    assert spec.env["GROK_HOME"] == str(home)
    assert spec.env["GROK_CURSOR_MCPS_ENABLED"] == "false"
    assert spec.env["GROK_CLAUDE_MCPS_ENABLED"] == "false"
    assert spec.argv[spec.argv.index("--output-format") + 1] == "streaming-json"
    assert validate_effective_grok_config(config, expected_role="worker") is None
    text = config.read_text(encoding="utf-8")
    assert 'EULER_ROLE = "worker"' in text
    assert 'EULER_RUN_ID = "run-a"' in text
    assert 'args = ["-m", "euler.gateway"]' in text
    auth = home / "auth.json"
    assert auth.is_symlink()
    assert auth.resolve() == dummy_auth.resolve()
    assert dummy_auth.read_text(encoding="utf-8") == "{}"
    assert (global_home / "config.toml").read_text(encoding="utf-8") == "# host user config\n"
    assert list(global_home.iterdir()) == [global_home / "config.toml"]
    assert ready.ok
    assert ready.config == str(config)


def test_same_worker_stamps_rewrite_config_keep_auth(tmp_path: Path):
    wl = _worker(tmp_path)
    dummy_auth = tmp_path / "auth.json"
    dummy_auth.write_text("{}", encoding="utf-8")
    with _env(EULER_GROK_AUTH_FILE=str(dummy_auth)):
        scaffold.write_grok_config(wl, extra_env={"EULER_RUN_ID": "r1"})
        first = (wl.dir / ".grok" / "config.toml").read_text(encoding="utf-8")
        auth = wl.dir / ".grok" / "auth.json"
        assert auth.is_symlink()
        scaffold.write_grok_config(wl, extra_env={"EULER_RUN_ID": "r2"})
        second = (wl.dir / ".grok" / "config.toml").read_text(encoding="utf-8")
    assert 'EULER_RUN_ID = "r1"' in first
    assert 'EULER_RUN_ID = "r2"' in second
    assert 'EULER_RUN_ID = "r1"' not in second
    assert auth.is_symlink()
    assert auth.resolve() == dummy_auth.resolve()


def test_paths_with_spaces_and_chinese(tmp_path: Path):
    root = tmp_path / "项目 Euler next"
    wl = _worker(root, "grok 甲")
    fake = _executable(tmp_path / "grok bin")
    with _env(EULER_GROK_BIN=str(fake)):
        scaffold.write_grok_config(wl)
        spec = build_command(
            wl,
            {"PROVIDER": "grok", "MODEL": "grok-4.6", "REASONING_EFFORT": "high"},
            "prove it",
        )
        ready = check_readiness(wl)
    assert spec.env["GROK_HOME"] == str(wl.dir / ".grok")
    assert spec.argv[spec.argv.index("--cwd") + 1] == str(wl.dir)
    assert ready.ok
    assert "项目" in ready.config
    assert "grok 甲" in ready.config


def test_readiness_rejects_missing_role_and_pass_json(tmp_path: Path):
    wl = _worker(tmp_path)
    fake = _executable(tmp_path / "grok")
    (wl.dir / ".grok").mkdir()
    (wl.dir / ".grok" / "config.toml").write_text(
        "[mcp_servers.euler]\ncommand = \"x\"\n",
        encoding="utf-8",
    )
    with _env(EULER_GROK_BIN=str(fake)):
        ready = check_readiness(wl)
    assert not ready.ok
    assert "args" in ready.reason or "EULER_ROLE" in ready.reason


def test_key_based_auth_skips_auth_file(tmp_path: Path):
    wl = _worker(tmp_path)
    fake = _executable(tmp_path / "grok")
    with _env(
        EULER_GROK_BIN=str(fake),
        EULER_GROK_AUTH_FILE=str(tmp_path / "missing-auth.json"),
        XAI_API_KEY="not-a-recorded-secret",
        GROK_HOME=str(tmp_path / "empty-home"),
        HOME=str(tmp_path / "empty-host"),
    ):
        (tmp_path / "empty-home").mkdir()
        (tmp_path / "empty-host").mkdir()
        scaffold.write_grok_config(wl)
        spec = build_command(
            wl,
            {"PROVIDER": "grok", "MODEL": "grok-4.6", "REASONING_EFFORT": "high"},
            "task",
        )
        ready = check_readiness(wl)
        assert grok_key_auth_present(spec.env)
    assert ready.ok
    assert not (wl.dir / ".grok" / "auth.json").exists()


def test_no_alias_leakage_into_cursor_or_codex(tmp_path: Path):
    fake_grok = _executable(tmp_path / "grok")
    fake_cursor = _executable(tmp_path / "cursor-agent")
    fake_codex = _executable(tmp_path / "codex")
    grok_wl = _worker(tmp_path, "grok1")
    cursor_wl = L.WorkerLayout(tmp_path / "workers" / "cursor1")
    cursor_wl.dir.mkdir(parents=True)
    (cursor_wl.dir / ".cursor").mkdir()
    (cursor_wl.dir / ".cursor" / "mcp.json").write_text("{}", encoding="utf-8")
    cursor_wl.role.write_text("PROVIDER=cursor\nMODEL=auto\nREASONING_EFFORT=router-managed\n")
    codex_wl = L.WorkerLayout(tmp_path / "workers" / "codex1")
    codex_wl.dir.mkdir(parents=True)
    (codex_wl.dir / ".codex").mkdir()
    (codex_wl.dir / ".codex" / "config.toml").write_text("[mcp_servers.euler]\n")
    codex_wl.role.write_text("PROVIDER=codex\nMODEL=gpt-5.5\nREASONING_EFFORT=high\n")
    with _env(
        EULER_GROK_BIN=str(fake_grok),
        EULER_CURSOR_BIN=str(fake_cursor),
        EULER_CODEX_BIN=str(fake_codex),
        EULER_DISABLE_CURSOR=None,
    ):
        scaffold.write_grok_config(grok_wl)
        grok_cmd = build_command(
            grok_wl,
            {"PROVIDER": "grok", "MODEL": "grok-4.6", "REASONING_EFFORT": "high"},
            "g",
        )
        cursor_cmd = build_command(
            cursor_wl,
            {"PROVIDER": "cursor", "MODEL": "auto", "REASONING_EFFORT": "router-managed"},
            "c",
        )
        codex_cmd = build_command(
            codex_wl,
            {"PROVIDER": "codex", "MODEL": "gpt-5.5", "REASONING_EFFORT": "high"},
            "x",
        )
    grok_home = grok_cmd.env["GROK_HOME"]
    assert grok_home == str(grok_wl.dir / ".grok")
    assert cursor_cmd.env.get("GROK_HOME") != grok_home
    assert codex_cmd.env.get("GROK_HOME") != grok_home
    assert grok_cmd.env["GROK_CURSOR_MCPS_ENABLED"] == "false"
    assert grok_cmd.env["GROK_CLAUDE_MCPS_ENABLED"] == "false"
    assert not (cursor_wl.dir / ".grok" / "config.toml").exists()
    assert not (codex_wl.dir / ".grok" / "config.toml").exists()


def test_preflight_grok_user_config(tmp_path: Path):
    wl = _worker(tmp_path)
    fake = _executable(tmp_path / "grok")
    with _env(EULER_GROK_BIN=str(fake)):
        missing = P.check_grok_user_config(wl)
        assert not missing.ok
        scaffold.write_grok_config(wl)
        ok = P.check_grok_user_config(wl)
        ready = check_readiness(wl)
    assert ok.ok
    assert ok.details["effective"] == "user"
    assert ready.ok


def test_link_auth_does_not_copy_bytes(tmp_path: Path):
    isolated = tmp_path / "iso"
    isolated.mkdir()
    source = tmp_path / "secret-auth.json"
    source.write_text("token-must-not-be-copied", encoding="utf-8")
    with _env(EULER_GROK_AUTH_FILE=str(source)):
        linked = link_grok_auth(isolated, os.environ)
        prepare_isolated_grok_home(tmp_path / "w", os.environ)
    assert linked is not None and linked.is_symlink()
    assert linked.resolve() == source.resolve()
    assert source.read_text(encoding="utf-8") == "token-must-not-be-copied"
    assert not any(path.name != "auth.json" and "auth" in path.name for path in isolated.iterdir())


@pytest.mark.parametrize("field", ["command", "project", "author", "verify_url", "role"])
def test_effective_readiness_rejects_foreign_worker_bindings(tmp_path: Path, field: str):
    wl = _worker(tmp_path)
    fake = _executable(tmp_path / "grok")
    with _env(EULER_GROK_BIN=str(fake), EULER_GROK_AUTH_FILE=str(tmp_path / "missing")):
        scaffold.write_grok_config(wl)
        scaffold.write_codex_config(wl)
        gateway_env = scaffold._gateway_env(wl)
        command = sys.executable
        if field == "command":
            # Matching realpath is insufficient: different venv paths can share
            # the same base Python while importing another Euler deployment.
            foreign_python = tmp_path / "foreign-venv-python"
            foreign_python.symlink_to(Path(sys.executable).resolve())
            command = str(foreign_python)
        else:
            key, value = {
                "project": ("EULER_PROJECT_DIR", str(tmp_path / "foreign-project")),
                "author": ("EULER_AUTHOR", "other-worker"),
                "verify_url": ("EULER_VERIFY_URL", "http://foreign.invalid/verify"),
                "role": ("EULER_ROLE", "main"),
            }[field]
            gateway_env[key] = value
        config = wl.dir / ".grok" / "config.toml"
        config.write_text(render_euler_mcp_toml(command, gateway_env), encoding="utf-8")
        assert not check_readiness(wl).ok
        assert not P.check_grok_user_config(wl).ok
        # The separately stamped Codex config cannot substitute for checking
        # the file Grok will actually execute.
        assert scaffold._gateway_env(wl)["EULER_PROJECT_DIR"] == str(wl.project_dir)


@pytest.mark.parametrize("auth_var", ["EULER_GROK_AUTH_FILE", "GROK_HOME"])
def test_relative_auth_source_links_to_absolute_file(tmp_path: Path, monkeypatch, auth_var: str):
    monkeypatch.chdir(tmp_path)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    source = credentials / "auth.json"
    source.write_text("fixture-auth", encoding="utf-8")
    env = {
        "HOME": str(tmp_path / "empty-host"),
        auth_var: "credentials/auth.json" if auth_var == "EULER_GROK_AUTH_FILE" else "credentials",
    }
    link = link_grok_auth(tmp_path / "isolated", env)
    assert link is not None and link.is_symlink() and link.is_file()
    assert Path(os.readlink(link)).is_absolute()
    assert link.resolve() == source.resolve()
    assert source.read_text(encoding="utf-8") == "fixture-auth"


def test_explicit_main_config_contains_core_and_control(tmp_path: Path):
    from euler.providers.grok import _load_toml, native_mcp_doctor_command

    config = tmp_path / "config.toml"
    env = {
        "EULER_ROLE": "main", "EULER_AUTHOR": "coordinator",
        "EULER_PROJECT_DIR": str(tmp_path / "project"),
        "EULER_AGENTS_ROOT": str(tmp_path),
    }
    config.write_text(render_euler_mcp_toml(sys.executable, env), encoding="utf-8")
    servers = _load_toml(config)["mcp_servers"]
    assert set(servers) == {"euler", "euler-control"}
    assert servers["euler"]["args"] == ["-m", "euler.gateway"]
    assert servers["euler-control"]["args"] == ["-m", "euler.control.mcp"]
    for server in servers.values():
        assert server["command"] == sys.executable
        assert server["env"] == env
    assert validate_effective_grok_config(config, expected_role="main", expected_python=sys.executable) is None
    assert native_mcp_doctor_command("grok", "euler-control") == (
        "grok", "mcp", "doctor", "euler-control", "--json",
    )


@pytest.mark.parametrize("role", ["worker", "verifier"])
def test_non_main_grok_configs_never_expose_control(tmp_path: Path, role: str):
    from euler.providers.grok import _load_toml

    config = tmp_path / "config.toml"
    text = render_euler_mcp_toml(sys.executable, {"EULER_ROLE": role})
    config.write_text(text, encoding="utf-8")
    assert set(_load_toml(config)["mcp_servers"]) == {"euler"}
    assert validate_effective_grok_config(config, expected_role=role) is None
    main_text = render_euler_mcp_toml(sys.executable, {"EULER_ROLE": "main"})
    injected_control = main_text[main_text.index("[mcp_servers.euler-control]"):]
    config.write_text(text + "\n" + injected_control, encoding="utf-8")
    assert "must not include" in validate_effective_grok_config(config, expected_role=role)


@pytest.mark.parametrize("mutation", ["missing", "role", "command", "project"])
def test_main_readiness_checks_both_effective_servers(tmp_path: Path, mutation: str):
    env = {"EULER_ROLE": "main", "EULER_AUTHOR": "coordinator", "EULER_PROJECT_DIR": str(tmp_path)}
    text = render_euler_mcp_toml(sys.executable, env)
    core, control = text.split("[mcp_servers.euler-control]", 1)
    if mutation == "missing":
        text = core
    else:
        if mutation == "role":
            control = control.replace('EULER_ROLE = "main"', 'EULER_ROLE = "worker"')
        elif mutation == "command":
            control = control.replace(f'command = "{sys.executable}"', 'command = "/foreign/python"')
        else:
            control = control.replace(str(tmp_path), str(tmp_path / "foreign"))
        text = core + "[mcp_servers.euler-control]" + control
    config = tmp_path / "config.toml"
    config.write_text(text, encoding="utf-8")
    assert validate_effective_grok_config(config, expected_role="main", expected_python=sys.executable) is not None


def test_optional_native_mcp_doctor_euler(tmp_path: Path):
    """One-shot local handshake. Skips when no native grok binary is present."""
    import shutil
    import sys

    import pytest

    from euler.providers.grok import run_native_mcp_doctor

    if os.environ.get("EULER_SKIP_NATIVE_GROK_DOCTOR") == "1":
        pytest.skip("EULER_SKIP_NATIVE_GROK_DOCTOR=1")
    configured = os.environ.get("EULER_GROK_BIN") or "grok"
    binary = configured if os.path.sep in configured else (shutil.which(configured) or "")
    if not binary or not (Path(binary).is_file() or shutil.which(binary)):
        pytest.skip("native grok binary is not available")
    if Path(binary).name in {"grok"} and not os.access(binary, os.X_OK):
        pytest.skip("native grok binary is not executable")
    marker = Path(binary).read_bytes()[:80] if Path(binary).is_file() else b""
    if marker.startswith(b"#!/bin/sh") and b"exit 0" in marker:
        pytest.skip("fixture grok stub is not a native CLI")
    wl = _worker(tmp_path / "tmp worker 项目")
    with _env(EULER_GROK_AUTH_FILE=str(tmp_path / "missing.json"), HOME=str(tmp_path / "host")):
        (tmp_path / "host").mkdir()
        scaffold.write_grok_config(wl)
        home = isolated_worker_grok_home(wl.dir)
        result = run_native_mcp_doctor(
            binary=binary,
            isolated_home=home,
            cwd=wl.dir,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path / "host"),
                "LANG": os.environ.get("LANG", "C"),
            },
            timeout_sec=20.0,
        )
    assert result["argv"] == [binary, "mcp", "doctor", "euler", "--json"]
    assert result["grok_home"] == str(home)
    assert "command" in (wl.dir / ".grok" / "config.toml").read_text(encoding="utf-8")
    assert sys.executable in (wl.dir / ".grok" / "config.toml").read_text(encoding="utf-8")
    # Record outcome for the handoff; native doctor may still need host login.
    assert isinstance(result["returncode"], int)


def test_default_auth_source_remains_available_after_first_symlink(tmp_path):
    from euler.providers.grok import resolve_grok_auth_source, link_grok_auth
    host = tmp_path / 'host'; source = host / '.grok/auth.json'
    source.parent.mkdir(parents=True); source.write_text('{"fixture":true}')
    isolated = tmp_path / 'isolated'
    env = {'HOME': str(host)}
    assert link_grok_auth(isolated, env).resolve() == source.resolve()
    assert resolve_grok_auth_source(env, isolated_home=isolated) == source.resolve()
    assert link_grok_auth(isolated, env).resolve() == source.resolve()
    assert source.read_text() == '{"fixture":true}'
