# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Cold-start agent launcher for the verify service.

Each /verify spawns a fresh verifier agent, driven by AGENT_HOME/AGENTS.md + the
verify skills, which writes ``verification.json`` to the run dir.  The backend is
selected explicitly (``grok``, ``codex``, or legacy ``cursor``); all inject only the read-only
Euler MCP role ``verifier``.  Configuration is read at CALL time so the service
is testable and can be restarted onto another backend without import-time drift.

Config (env):
  EULER_VERIFY_BACKEND (grok | cursor | codex; default grok),
  EULER_GROK_BIN,
  EULER_VERIFY_GROK_MODEL (default grok-4.6),
  EULER_VERIFY_GROK_EFFORT (default high),
  EULER_VERIFY_GROK_HOME,
  EULER_GROK_AUTH_FILE,
  EULER_CODEX_BIN,
  EULER_VERIFY_MODEL (default gpt-5.5),
  EULER_VERIFY_EFFORT (default xhigh),
  EULER_CURSOR_BIN,
  EULER_VERIFY_CURSOR_MODEL (default cursor-grok-4.6-xhigh),
  CODEX_TIMEOUT_SECONDS (0 = no timeout),
  VERIFY_AGENT_HOME (verifier workspace: AGENTS.md + .agents/skills),
  VERIFIER_RESULTS_DIR (run dirs; gitignored).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from euler import codex
from euler.providers.grok import (
    GROK_KEY_AUTH_VARS,
    apply_isolated_grok_launch_env,
    grok_key_auth_present,
    link_grok_auth,
    render_euler_mcp_toml,
    resolve_grok_auth_source,
)

_HERE = Path(__file__).resolve().parent  # euler/verify/
_REPO_ROOT = _HERE.parent.parent         # repo root (euler/verify -> euler -> root)
VERIFICATION_FILENAMES = ("verification.json", "verificationt.json")
VERIFY_BACKENDS = frozenset({"codex", "cursor", "grok"})


# --------------------------------------------------------------------------- #
# config resolution (env read at call time)                                   #
# --------------------------------------------------------------------------- #

def _agent_home() -> Path:
    return Path(os.getenv("VERIFY_AGENT_HOME", str(_HERE / "agent"))).resolve()


def _relink(link: Path, target: Path) -> None:
    """Point ``link`` (a symlink) at absolute ``target``, replacing a stale link."""
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def ensure_agent_home() -> Path:
    """Provision the verifier workspace if absent, then return it.

    Unlike a worker home (assembled per project by ``euler new``), the verify
    agent home is a singleton with no scaffolder — so a fresh checkout has none and
    the codex ``-C`` dir would not exist. This builds it the same way a worker home
    is built: ``AGENTS.md`` (the verifier contract) + ``.agents/skills`` (the verify
    skills), symlinked to the repo's canonical sources so they stay in sync.
    Idempotent (a no-op once the links exist); skips silently if the canonical
    sources are absent (e.g. an installed package without the ``agents/`` tree),
    leaving the existing missing-home error to surface honestly."""
    home = _agent_home()
    contract = _REPO_ROOT / "agents" / "contracts" / "verifier.md"
    skills = _REPO_ROOT / "agents" / "skills" / "verify"
    agents_md = home / "AGENTS.md"
    skills_link = home / ".agents" / "skills"
    if not (agents_md.exists() and skills_link.exists()):
        if not (contract.exists() and skills.exists()):
            return home  # nothing to link from — do not create broken links
        (home / ".agents").mkdir(parents=True, exist_ok=True)
        _relink(agents_md, contract)
        _relink(skills_link, skills)
    if _backend() == "cursor":
        _ensure_cursor_home(home)
    elif _backend() == "grok":
        _ensure_grok_home(home)
    return home



def _results_root() -> Path:
    return Path(os.getenv("VERIFIER_RESULTS_DIR", str(_HERE / "runs"))).resolve()


def _model() -> str:
    return codex.model("EULER_VERIFY_MODEL")


def _effort() -> str:
    return codex.effort("EULER_VERIFY_EFFORT")


def _backend() -> str:
    backend = os.getenv("EULER_VERIFY_BACKEND", "grok").strip().lower()
    if backend not in VERIFY_BACKENDS:
        raise RuntimeError(
            f"unsupported EULER_VERIFY_BACKEND={backend!r}; "
            f"expected one of {sorted(VERIFY_BACKENDS)}"
        )
    if backend == "cursor" and os.getenv("EULER_DISABLE_CURSOR", "0").strip() == "1":
        raise RuntimeError(
            "EULER_VERIFY_BACKEND=cursor is forbidden while "
            "EULER_DISABLE_CURSOR=1"
        )
    return backend


def _grok_binary() -> str:
    configured = os.getenv("EULER_GROK_BIN", "grok")
    if os.path.sep in configured:
        return str(Path(configured).expanduser())
    return shutil.which(configured) or configured


def _grok_model() -> str:
    return os.getenv("EULER_VERIFY_GROK_MODEL", "grok-4.6").strip()


def _grok_effort() -> str:
    return os.getenv("EULER_VERIFY_GROK_EFFORT", "high").strip()


def _grok_home() -> Path:
    configured = os.getenv("EULER_VERIFY_GROK_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (_REPO_ROOT / "runtime" / "grok-verifier-home").resolve()


def _grok_auth_source() -> Optional[Path]:
    return resolve_grok_auth_source(os.environ, isolated_home=_grok_home())


def _cursor_binary() -> str:
    configured = os.getenv("EULER_CURSOR_BIN", "cursor-agent")
    if os.path.sep in configured:
        return str(Path(configured).expanduser())
    return shutil.which(configured) or configured


def _cursor_model() -> str:
    return os.getenv(
        "EULER_VERIFY_CURSOR_MODEL", "cursor-grok-4.6-xhigh"
    ).strip()


def _cursor_home() -> Path:
    configured = os.getenv("EULER_VERIFY_CURSOR_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (_agent_home() / ".cursor-home").resolve()


def _cursor_auth_source() -> Path:
    configured = os.getenv("EULER_CURSOR_AUTH_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    service_home = Path(os.getenv("HOME", str(Path.home()))).expanduser()
    return (service_home / ".config" / "cursor" / "auth.json").resolve()


def _verify_python() -> str:
    # Preserve venv symlink spelling; resolving it would lose pyvenv.cfg.
    return os.path.abspath(
        os.path.expanduser(os.getenv("EULER_VERIFY_PYTHON", sys.executable))
    )


def _timeout() -> Optional[int]:
    return int(os.getenv("CODEX_TIMEOUT_SECONDS", "0")) or None


def _mcp_config_arg() -> str:
    """Inject the euler gateway (role=verifier) into the codex agent via `-c`,
    independent of CODEX_HOME.

    Use the verify service's concrete Python interpreter, not the bare
    ``python3`` found from the verifier agent's working-directory PATH.  The
    latter may resolve to ``/usr/bin/python3`` and fail to import the installed
    Euler package, silently leaving the verifier with no MCP tools.
    """
    python = _verify_python()
    return (
        "mcp_servers.euler={command="
        # Do not resolve symlinks here.  A venv's ``bin/python`` is commonly a
        # symlink to the base interpreter; spelling the resolved target in the
        # subprocess command loses the adjacent ``pyvenv.cfg`` and therefore
        # the venv site-packages which contain Euler.
        + json.dumps(python)
        + ',args=["-m","euler.gateway"],env={EULER_ROLE="verifier"}}'
    )


def _ensure_cursor_home(home: Path) -> None:
    """Make ``home`` an isolated Cursor project with only verifier MCP access.

    Cursor discovers project MCP configuration from the nearest project root.
    The verifier home sits inside the outer Euler checkout, so a metadata-only
    nested Git repository prevents Cursor from walking upward and loading an
    unrelated outer ``.cursor/mcp.json``.  The local config contains exactly one
    server: the Euler gateway with the read-only ``verifier`` role.
    """
    # Run init unconditionally: an interrupted earlier initialization can leave
    # a partial ``.git`` directory which existence checks would mistake for a
    # valid project boundary.
    subprocess.run(
        ["git", "init", "-q", str(home)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    config = home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mcpServers": {
            "euler": {
                "type": "stdio",
                "command": _verify_python(),
                "args": ["-m", "euler.gateway"],
                "env": {
                    "EULER_ROLE": "verifier",
                    "EULER_AUTHOR": "verifier",
                },
            }
        }
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not config.exists() or config.read_text(encoding="utf-8") != text:
        tmp = config.with_suffix(config.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, config)

    # Cursor also loads user-global MCP configuration from HOME. Give the
    # verifier a dedicated HOME containing only the Cursor authentication file;
    # this prevents ~/.cursor/mcp.json, hooks, rules, and unrelated skills from
    # joining the verifier's effective tool surface.
    isolated_home = _cursor_home()
    sentinel = isolated_home / ".euler-verifier-home"
    if isolated_home.exists() and not sentinel.exists() and any(isolated_home.iterdir()):
        raise RuntimeError(
            f"refusing pre-populated EULER_VERIFY_CURSOR_HOME={isolated_home}; "
            "use a new empty directory so global MCP/hooks/rules cannot leak in"
        )
    isolated_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    isolated_home.chmod(0o700)
    if not sentinel.exists():
        sentinel.write_text("Euler isolated Cursor verifier home.\n", encoding="utf-8")
        sentinel.chmod(0o600)
    forbidden = (
        isolated_home / ".cursor" / "mcp.json",
        isolated_home / ".cursor" / "hooks.json",
        isolated_home / ".cursor" / "rules",
        isolated_home / ".cursor" / "agents",
    )
    present = [str(path) for path in forbidden if path.exists() or path.is_symlink()]
    if present:
        raise RuntimeError(
            "isolated Cursor verifier HOME contains forbidden global configuration: "
            + ", ".join(present)
        )
    auth_source = _cursor_auth_source()
    if not auth_source.is_file():
        raise RuntimeError(
            "Cursor verifier authentication is unavailable: expected "
            f"{auth_source}; run `cursor-agent login` or set "
            "EULER_CURSOR_AUTH_FILE"
        )
    auth_dir = isolated_home / ".config" / "cursor"
    auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth_dir.chmod(0o700)
    auth_link = auth_dir / "auth.json"
    if not auth_link.is_symlink() or auth_link.resolve() != auth_source:
        _relink(auth_link, auth_source)


def _ensure_grok_home(home: Path) -> None:
    """Provision an isolated Grok Build verifier with one read-only MCP.

    Grok ignores untrusted project-scoped ``.grok/config.toml`` at execution
    time, and a cwd copy of that file is treated as repo-local and suppressed.
    The verifier therefore stamps the Euler gateway (role=verifier) only as
    user config inside dedicated ``GROK_HOME``. Auth is linked from
    ``EULER_GROK_AUTH_FILE`` or the original ``GROK_HOME`` / default home;
    key-based auth is also accepted.
    """
    _retire_legacy_grok_project_config(home)
    subprocess.run(
        ["git", "init", "-q", str(home)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    text = render_euler_mcp_toml(
        _verify_python(),
        {"EULER_ROLE": "verifier", "EULER_AUTHOR": "verifier"},
    )
    isolated_home = _grok_home()
    sentinel = isolated_home / ".euler-grok-verifier-home"
    if isolated_home.exists() and not sentinel.exists() and any(isolated_home.iterdir()):
        raise RuntimeError(
            f"refusing pre-populated EULER_VERIFY_GROK_HOME={isolated_home}; "
            "use a new empty directory so global MCP/hooks/rules cannot leak in"
        )
    isolated_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    isolated_home.chmod(0o700)
    if not sentinel.exists():
        sentinel.write_text("Euler isolated Grok Build verifier home.\n", encoding="utf-8")
        sentinel.chmod(0o600)

    user_config = isolated_home / "config.toml"
    if not user_config.exists() or user_config.read_text(encoding="utf-8") != text:
        tmp = user_config.with_suffix(user_config.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, user_config)
        user_config.chmod(0o600)

    auth_source = _grok_auth_source()
    if auth_source is not None and auth_source.is_file():
        link_grok_auth(isolated_home, os.environ)
    elif grok_key_auth_present(os.environ):
        pass
    else:
        expected = auth_source or Path("~/.grok/auth.json").expanduser()
        raise RuntimeError(
            "Grok verifier authentication is unavailable: expected "
            f"{expected}; run `grok login`, set EULER_GROK_AUTH_FILE, "
            "or provide XAI_API_KEY / EULER_GROK_API_KEY / GROK_API_KEY"
        )


def _retire_legacy_grok_project_config(home: Path) -> None:
    """Remove only the exact old Euler verifier config; reject other shadows."""
    directory = home / ".grok"
    config = directory / "config.toml"
    if not config.exists() and not config.is_symlink():
        return
    refusal = (
        f"verifier project Grok config would override isolated user config: {config}; "
        "use a clean VERIFY_AGENT_HOME or move the custom configuration manually"
    )
    if directory.is_symlink() or config.is_symlink():
        raise RuntimeError(refusal)
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib  # type: ignore
    text = config.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except ValueError as exc:
        raise RuntimeError(refusal) from exc
    expected = {
        "mcp_servers": {"euler": {
            "command": _verify_python(),
            "args": ["-m", "euler.gateway"],
            "enabled": True,
            "startup_timeout_sec": 20,
            "tool_timeout_sec": 3600,
            "env": {"EULER_ROLE": "verifier", "EULER_AUTHOR": "verifier"},
        }},
    }
    if not text.startswith("# Auto-generated by Euler; verifier-only Grok Build MCP.\n") or parsed != expected:
        raise RuntimeError(refusal)
    config.unlink()


# --------------------------------------------------------------------------- #
# run-dir allocation                                                          #
# --------------------------------------------------------------------------- #

def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generate_run_id(statement: str) -> str:
    return f"{_utc_timestamp()}_{hashlib.sha256(statement.encode('utf-8')).hexdigest()[:12]}"


def _allocate_run_id(statement: str) -> str:
    """Claim a unique run dir atomically (mkdir exist_ok=False, retry with a
    numeric suffix) so concurrent verifiers sharing RESULTS_ROOT never clobber."""
    root = _results_root()
    root.mkdir(parents=True, exist_ok=True)
    base = generate_run_id(statement)
    run_id, suffix = base, 1
    for _ in range(10000):
        try:
            (root / run_id).mkdir(parents=False, exist_ok=False)
            return run_id
        except FileExistsError:
            suffix += 1
            run_id = f"{base}_{suffix}"
    raise RuntimeError(f"could not allocate a unique run_id under {root} for base={base}")


def _results_dir(run_id: str) -> Path:
    return _results_root() / run_id


def _verification_path(run_id: str) -> Optional[Path]:
    for filename in VERIFICATION_FILENAMES:
        path = _results_dir(run_id) / filename
        if path.exists():
            return path
    return None


def build_prompt(run_id: str, statement: str, proof: str) -> str:
    output_path = _results_dir(run_id) / VERIFICATION_FILENAMES[0]
    return (
        f"Run_id: {run_id}. "
        f"Statement: {statement}. "
        f"Proof:\n{proof}\n\n"
        "Use AGENTS.md to verify the above proof for the statement. "
        f"Write the verification JSON to this exact path: {output_path}."
    )


def build_codex_command(run_id: str, statement: str, proof: str) -> List[str]:
    return codex.exec_cmd(
        codex.resolve_bin(), _model(), _effort(),
        "-C", str(_agent_home()),
        # on an install without .git (tarball download), codex's
        # trusted-directory check refuses to run (exit 1 → /verify HTTP 500)
        "--skip-git-repo-check",
        "-c", _mcp_config_arg(),
        "--dangerously-bypass-approvals-and-sandbox",
        build_prompt(run_id=run_id, statement=statement, proof=proof),
    )


def build_cursor_command(run_id: str, statement: str, proof: str) -> List[str]:
    return [
        _cursor_binary(),
        "--print",
        "--output-format",
        "text",
        "--force",
        "--approve-mcps",
        "--trust",
        "--workspace",
        str(_agent_home()),
        "--model",
        _cursor_model(),
        build_prompt(run_id=run_id, statement=statement, proof=proof),
    ]


def build_grok_command(run_id: str, statement: str, proof: str) -> List[str]:
    return [
        _grok_binary(),
        "--cwd",
        str(_agent_home()),
        "--model",
        _grok_model(),
        "--reasoning-effort",
        _grok_effort(),
        "--permission-mode",
        "bypassPermissions",
        "--no-subagents",
        "--output-format",
        "plain",
        "--single",
        build_prompt(run_id=run_id, statement=statement, proof=proof),
    ]


def build_verifier_command(run_id: str, statement: str, proof: str) -> List[str]:
    backend = _backend()
    if backend == "cursor":
        return build_cursor_command(run_id, statement, proof)
    if backend == "grok":
        return build_grok_command(run_id, statement, proof)
    return build_codex_command(run_id, statement, proof)


def _subprocess_env(command: List[str]) -> Dict[str, str]:
    if _backend() == "codex":
        return codex.subprocess_env(command[0])
    # Allowlist only process/runtime fields the subscription CLIs may need. In particular do
    # not inherit cloud, GitHub, SSH, research-model, or consult credentials.
    allowed = (
        "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "CURSOR_API_KEY", "CURSOR_API_ENDPOINT",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    if _backend() == "grok":
        isolated_home = _grok_home()
        env = apply_isolated_grok_launch_env(env, isolated_home)
        env["HOME"] = str(_agent_home())
        env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
        env["XDG_CACHE_HOME"] = str(isolated_home / ".cache")
        env["XDG_DATA_HOME"] = str(isolated_home / ".local" / "share")
        env["XDG_STATE_HOME"] = str(isolated_home / ".local" / "state")
        for key in GROK_KEY_AUTH_VARS:
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    isolated_home = _cursor_home()
    env["HOME"] = str(isolated_home)
    env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
    env["XDG_CACHE_HOME"] = str(isolated_home / ".cache")
    env["XDG_DATA_HOME"] = str(isolated_home / ".local" / "share")
    env["XDG_STATE_HOME"] = str(isolated_home / ".local" / "state")
    return env


def _validate_payload(payload: Any, path: Path) -> Dict[str, Any]:
    """Validate the agent-produced verdict before it reaches the write-gate."""
    prefix = f"verification output at {path}"
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail=f"{prefix} must be a JSON object")
    report = payload.get("verification_report")
    verdict = payload.get("verdict")
    repair_hints = payload.get("repair_hints")
    if not isinstance(report, dict):
        raise HTTPException(status_code=500, detail=f"{prefix} has no object verification_report")
    if verdict not in {"correct", "wrong"}:
        raise HTTPException(status_code=500, detail=f"{prefix} has invalid verdict")
    if not isinstance(repair_hints, str):
        raise HTTPException(status_code=500, detail=f"{prefix} has non-string repair_hints")
    if not isinstance(report.get("summary"), str):
        raise HTTPException(status_code=500, detail=f"{prefix} has non-string summary")
    for field in ("critical_errors", "gaps"):
        findings = report.get(field)
        if not isinstance(findings, list):
            raise HTTPException(status_code=500, detail=f"{prefix} has non-list {field}")
        for finding in findings:
            if not isinstance(finding, dict) or not all(
                isinstance(finding.get(key), str) and finding[key].strip()
                for key in ("location", "issue")
            ):
                raise HTTPException(status_code=500, detail=f"{prefix} has malformed {field} entry")
    clean = not report["critical_errors"] and not report["gaps"]
    if (verdict == "correct") != clean:
        raise HTTPException(status_code=500, detail=f"{prefix} has inconsistent verdict and findings")
    if verdict == "correct" and repair_hints:
        raise HTTPException(status_code=500, detail=f"{prefix} has repair_hints for a correct verdict")
    if verdict == "wrong" and not repair_hints.strip():
        raise HTTPException(status_code=500, detail=f"{prefix} lacks repair_hints for a wrong verdict")
    return payload


def run_verification(run_id: str, statement: str, proof: str) -> Dict[str, Any]:
    """Spawn the configured cold-start verifier; return its verification JSON.

    Raises HTTPException 504 (timeout) / 500 (nonzero exit, no output, or
    malformed JSON).  Callers translate these into the fact_submit
    infrastructure-error path; no result is accepted on failure.
    """
    results_dir = _results_dir(run_id)
    results_dir.mkdir(parents=True, exist_ok=True)
    log_path = results_dir / "log.md"
    ensure_agent_home()
    cmd = build_verifier_command(run_id=run_id, statement=statement, proof=proof)
    env = _subprocess_env(cmd)
    backend = _backend()

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            log_handle.write(f"started_at_utc: {started_at}\n")
            log_handle.write(f"backend: {backend}\n")
            log_handle.write(f"command: {shlex.join(cmd)}\n\n")
            log_handle.flush()
            completed = subprocess.run(
                cmd, cwd=_agent_home(), env=env,
                stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                text=True, timeout=_timeout(), check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504,
                            detail=f"{backend} verifier timed out after {exc.timeout}s. See log at {log_path}") from exc

    if completed.returncode != 0:
        raise HTTPException(status_code=500,
                            detail=f"{backend} verifier failed with exit code {completed.returncode}. See log at {log_path}")

    verification_path = _verification_path(run_id)
    if verification_path is None:
        expected = results_dir / VERIFICATION_FILENAMES[0]
        raise HTTPException(status_code=500,
                            detail=f"verification output was not found at {expected}. See log at {log_path}")
    try:
        payload = json.loads(verification_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500,
                            detail=f"verification output at {verification_path} is not valid JSON") from exc
    return _validate_payload(payload, verification_path)


def run_codex_verification(run_id: str, statement: str, proof: str) -> Dict[str, Any]:
    """Backward-compatible entry point; uses the configured verifier backend.

    The historical name is kept for callers outside this checkout.  New code
    should call :func:`run_verification`.
    """
    return run_verification(run_id, statement, proof)
