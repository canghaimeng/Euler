"""Native provider protocol: command, readiness, env, session, registry."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from euler.execution import layout as L
from euler.execution import loop, scaffold
from euler.providers import (
    NATIVE_PROVIDER_NAMES,
    ProviderError,
    ProviderName,
    build_command,
    check_readiness,
    get_provider,
    provider_from_role,
    read_role_file,
    scrub_worker_env,
)
from euler.providers.base import CONSULT_SECRET_KEYS


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


def _wl(tmp: Path, name: str = "high") -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp / "proj" / "workers" / name)
    wl.dir.mkdir(parents=True, exist_ok=True)
    return wl


def _secrets() -> dict:
    return {
        "EULER_CODEX_API_KEY": "codex-secret",
        "ANTHROPIC_API_KEY": "claude-secret",
        "XAI_GATE_TOKEN": "grok-secret",
        "CURSOR_API_KEY": "cursor-secret",
        "SOL_GATE_TOKEN": "soltok",
        "EULER_CONSULT_API_KEY": "consult",
    }


def test_native_registry_names():
    assert [item.value for item in NATIVE_PROVIDER_NAMES] == [
        "codex", "claude-code", "grok", "cursor", "gemini",
    ]


def test_unknown_provider_fail_closed():
    try:
        provider_from_role({"PROVIDER": "mystery"})
        raise AssertionError("unknown PROVIDER must fail closed")
    except ProviderError as exc:
        assert "mystery" in str(exc)
    try:
        get_provider("not-a-provider")
        raise AssertionError("unknown lookup must fail closed")
    except ProviderError:
        pass


def test_backend_compatibility_for_native_names_only():
    assert provider_from_role({"BACKEND": "codex"}) is ProviderName.CODEX
    assert provider_from_role({"PROVIDER": "grok", "BACKEND": "codex"}) is ProviderName.GROK
    assert provider_from_role({"RUNNER": "cursor-agent"}) is ProviderName.CURSOR


def test_codex_command_and_readiness(tmp_path: Path):
    wl = _wl(tmp_path, "codex")
    fake = _executable(tmp_path / "codex")
    (wl.dir / ".codex").mkdir()
    (wl.dir / ".codex" / "config.toml").write_text("[mcp_servers.euler]\n", encoding="utf-8")
    wl.role.write_text("PROVIDER=codex\nMODEL=gpt-5.5\nREASONING_EFFORT=high\n")
    role = {"PROVIDER": "codex", "MODEL": "gpt-5.5", "REASONING_EFFORT": "high"}
    with _env(EULER_CODEX_BIN=str(fake), **_secrets()):
        spec = build_command(wl, role, "prove it")
        ready = check_readiness(wl)
    assert spec.provider is ProviderName.CODEX
    assert spec.argv[0] == str(fake)
    assert "exec" in spec.argv
    assert spec.argv[-1] == "prove it"
    assert "ANTHROPIC_API_KEY" not in spec.env
    assert "XAI_GATE_TOKEN" not in spec.env
    assert spec.env["EULER_CODEX_API_KEY"] == "codex-secret"
    assert spec.env["SOL_GATE_TOKEN"] == "soltok"
    assert ready.ok and ready.provider is ProviderName.CODEX


def test_claude_code_print_resume_and_default_bin(tmp_path: Path):
    wl = _wl(tmp_path, "claude")
    fake = _executable(tmp_path / "claude")
    (wl.dir / ".claude").mkdir()
    (wl.dir / ".claude" / "mcp.json").write_text("{}", encoding="utf-8")
    wl.role.write_text("PROVIDER=claude-code\nMODEL=claude-sonnet-4-5\nREASONING_EFFORT=xhigh\n")
    role = {
        "PROVIDER": "claude-code",
        "MODEL": "claude-sonnet-4-5",
        "REASONING_EFFORT": "xhigh",
        "CLAUDE_RESUME_SESSION": "sess-1",
    }
    with _env(EULER_CLAUDE_CODE_BIN=str(fake), **_secrets()):
        spec = build_command(wl, role, "verify")
        ready = check_readiness(wl)
    assert spec.argv[0] == str(fake)
    assert "--print" in spec.argv
    assert spec.argv[spec.argv.index("--resume") + 1] == "sess-1"
    assert spec.session_id == "sess-1"
    assert "EULER_CODEX_API_KEY" not in spec.env
    assert "SOL_GATE_TOKEN" not in spec.env
    assert spec.env["ANTHROPIC_API_KEY"] == "claude-secret"
    assert ready.ok
    recovered = get_provider("claude-code").recover_session({
        "session_id": "sess-1",
        "prompt": "again",
        "worker_dir": str(wl.dir),
        "project_dir": str(wl.project_dir),
        "worker_name": wl.name,
        "project_id": wl.project,
        "model": "claude-sonnet-4-5",
        "effort": "high",
    })
    assert recovered is not None
    assert "--resume" in recovered.argv


def test_grok_single_and_persistent_resume(tmp_path: Path):
    wl = _wl(tmp_path, "grok")
    fake = _executable(tmp_path / "grok")
    scaffold.write_grok_config(wl)
    wl.role.write_text("PROVIDER=grok\nMODEL=grok-4.6\nREASONING_EFFORT=high\n")
    role = {"PROVIDER": "grok", "MODEL": "grok-4.6", "REASONING_EFFORT": "high"}
    with _env(EULER_GROK_BIN=str(fake), **_secrets()):
        one_shot = build_command(wl, role, "task")
        persistent = build_command(
            wl,
            {**role, "GROK_RESUME_SESSION": "g-sess", "GROK_PERSISTENT": "1"},
            "continue",
        )
        ready = check_readiness(wl)
    assert "--single" in one_shot.argv
    assert one_shot.argv[-1] == "task"
    assert "--resume" in persistent.argv
    assert "--single" not in persistent.argv
    assert persistent.argv[-1] == "continue"
    assert one_shot.argv[one_shot.argv.index("--output-format") + 1] == "streaming-json"
    assert "EULER_CODEX_API_KEY" not in persistent.env
    assert persistent.env["XAI_GATE_TOKEN"] == "grok-secret"
    assert "SOL_GATE_TOKEN" not in persistent.env
    assert persistent.env["GROK_HOME"] == str(wl.dir / ".grok")
    assert persistent.env["GROK_CURSOR_MCPS_ENABLED"] == "false"
    assert persistent.env["GROK_CLAUDE_MCPS_ENABLED"] == "false"
    assert ready.ok
    assert ready.config == str(wl.dir / ".grok" / "config.toml")


def test_cursor_native_flags_and_disable(tmp_path: Path):
    wl = _wl(tmp_path, "cursor")
    fake = _executable(tmp_path / "cursor-agent")
    (wl.dir / ".cursor").mkdir()
    (wl.dir / ".cursor" / "mcp.json").write_text("{}", encoding="utf-8")
    wl.role.write_text("PROVIDER=cursor\nMODEL=auto\nREASONING_EFFORT=router-managed\n")
    role = {"PROVIDER": "cursor", "MODEL": "auto", "REASONING_EFFORT": "router-managed"}
    with _env(EULER_CURSOR_BIN=str(fake), **_secrets()):
        spec = build_command(wl, role, "check")
        ready = check_readiness(wl)
    assert spec.argv[spec.argv.index("--model") + 1] == "auto"
    assert "-p" in spec.argv and "--approve-mcps" in spec.argv
    assert spec.argv[spec.argv.index("--sandbox") + 1] == "disabled"
    assert spec.argv[spec.argv.index("--add-dir") + 1] == str(wl.project_dir)
    assert "EULER_CODEX_API_KEY" not in spec.env
    assert "XAI_GATE_TOKEN" not in spec.env
    assert ready.ok
    with _env(EULER_DISABLE_CURSOR="1"):
        try:
            build_command(wl, role, "check")
            raise AssertionError("disabled cursor must fail")
        except ProviderError as exc:
            assert "EULER_DISABLE_CURSOR=1" in str(exc)


def test_cross_provider_secret_isolation():
    payload = _secrets()
    grok = scrub_worker_env({"PROVIDER": "grok"}, env=payload)
    codex = scrub_worker_env({"PROVIDER": "codex"}, env=payload)
    claude = scrub_worker_env({"PROVIDER": "claude-code"}, env=payload)
    cursor = scrub_worker_env({"PROVIDER": "cursor"}, env=payload)
    assert grok["XAI_GATE_TOKEN"] == "grok-secret"
    assert "EULER_CODEX_API_KEY" not in grok
    assert "ANTHROPIC_API_KEY" not in grok
    assert "CURSOR_API_KEY" not in grok
    for key in CONSULT_SECRET_KEYS:
        if key in payload:
            assert key not in grok
    assert codex["EULER_CODEX_API_KEY"] == "codex-secret"
    assert "XAI_GATE_TOKEN" not in codex
    assert "ANTHROPIC_API_KEY" not in claude or claude["ANTHROPIC_API_KEY"] == "claude-secret"
    assert "EULER_CODEX_API_KEY" not in claude
    assert "EULER_CODEX_API_KEY" not in cursor
    assert "XAI_GATE_TOKEN" not in cursor


def test_no_model_prefix_dispatch():
    env = scrub_worker_env(
        {"PROVIDER": "codex", "MODEL": "grok-4.6"},
        env=_secrets(),
    )
    assert env["SOL_GATE_TOKEN"] == "soltok"
    assert "XAI_GATE_TOKEN" not in env


def test_scaffold_one_worker_per_native_provider(tmp_path: Path):
    fake_codex = _executable(tmp_path / "codex")
    fake_claude = _executable(tmp_path / "claude")
    fake_grok = _executable(tmp_path / "grok")
    fake_cursor = _executable(tmp_path / "cursor-agent")
    with _env(
        EULER_AGENTS_ROOT=str(tmp_path / "projects"),
        EULER_CODEX_BIN=str(fake_codex),
        EULER_CLAUDE_CODE_BIN=str(fake_claude),
        EULER_GROK_BIN=str(fake_grok),
        EULER_CURSOR_BIN=str(fake_cursor),
        EULER_WORKER_CONTRACT=str(tmp_path / "worker.md"),
        EULER_WORKER_SKILLS=str(tmp_path / "skills"),
    ):
        (tmp_path / "worker.md").write_text("# worker\n", encoding="utf-8")
        (tmp_path / "skills").mkdir()
        result = scaffold.do_new(
            "Q",
            roles="codex:1,claude-code:1,grok:1,cursor:1",
            model="gpt-5.5",
        )
    assert result["workers"] == ["codex", "claude-code", "grok", "cursor"]
    expected = {
        "codex": ("codex", ".codex/config.toml"),
        "claude-code": ("claude-code", ".claude/mcp.json"),
        "grok": ("grok", ".grok/config.toml"),
        "cursor": ("cursor", ".cursor/mcp.json"),
    }
    with _env(
        EULER_CODEX_BIN=str(fake_codex),
        EULER_CLAUDE_CODE_BIN=str(fake_claude),
        EULER_GROK_BIN=str(fake_grok),
        EULER_CURSOR_BIN=str(fake_cursor),
        EULER_AGENTS_ROOT=str(tmp_path / "projects"),
    ):
        for name, (provider, config) in expected.items():
            wl = L.WorkerLayout(L.worker_dir("Q", name))
            role = read_role_file(wl)
            assert role["PROVIDER"] == provider
            assert (wl.dir / config).is_file()
            assert check_readiness(wl).ok
            assert check_readiness(wl).provider.value == provider


def test_loop_fake_subprocess_per_native_provider(tmp_path: Path):
    dump = (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "json.dump({'argv0': sys.argv[0], 'argv': sys.argv[1:],"
        " 'sol': os.environ.get('SOL_GATE_TOKEN'),"
        " 'codex': os.environ.get('EULER_CODEX_API_KEY'),"
        " 'claude': os.environ.get('ANTHROPIC_API_KEY'),"
        " 'grok': os.environ.get('XAI_GATE_TOKEN'),"
        " 'cursor': os.environ.get('CURSOR_API_KEY')}, sys.stdout)\n"
    )
    cases = (
        ("codex", "EULER_CODEX_BIN", {"PROVIDER": "codex", "MODEL": "gpt-5.5",
                                     "REASONING_EFFORT": "high"}),
        ("claude", "EULER_CLAUDE_CODE_BIN", {"PROVIDER": "claude-code",
                                            "MODEL": "claude-sonnet-4-5",
                                            "REASONING_EFFORT": "high"}),
        ("grok", "EULER_GROK_BIN", {"PROVIDER": "grok", "MODEL": "grok-4.6",
                                   "REASONING_EFFORT": "high"}),
        ("cursor", "EULER_CURSOR_BIN", {"PROVIDER": "cursor", "MODEL": "auto",
                                       "REASONING_EFFORT": "router-managed"}),
    )
    secrets = _secrets()
    for name, env_key, role in cases:
        wl = _wl(tmp_path / name, name)
        if name == "cursor":
            (wl.dir / ".cursor").mkdir(exist_ok=True)
        fake = tmp_path / name / "bin"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(dump, encoding="utf-8")
        fake.chmod(0o755)
        log = wl.dir / "round.log"
        with _env(**{env_key: str(fake)}, **secrets):
            rc = loop.run_round(wl, role, "prompt-body", log, hard_timeout=30)
        assert rc == 0, name
        got = json.loads(log.read_text())
        assert Path(got["argv0"]).name == "bin"
        if name == "codex":
            assert got["sol"] == "soltok" and got["codex"] == "codex-secret"
            assert got["claude"] is None and got["grok"] is None
        elif name == "claude":
            assert got["sol"] is None and got["claude"] == "claude-secret"
            assert got["codex"] is None
        elif name == "grok":
            assert got["sol"] is None and got["grok"] == "grok-secret"
            assert got["codex"] is None
        else:
            assert got["sol"] is None and got["cursor"] == "cursor-secret"
            assert got["codex"] is None and got["grok"] is None
