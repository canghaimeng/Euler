# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Fail-closed proving preflight — run BEFORE any worker round / subprocess.

A worker may not launch ``codex exec`` (and ``euler start`` may not spawn the
loop) unless every check below passes. A failure is **not** a mathematical
verdict: it is a transport / wiring / store-access refusal. Status is
``PRECHECK_FAILED`` with a structured ``precheck.{component,reason}``.

Checks (all required):

  python       configured interpreter imports ``euler`` + MCP (``euler._mcp``)
  gateway      worker role exposes the complete required tool set (table +
               registered FastMCP tools)
  mcp_config   this worker's ``.codex/config.toml`` launches
               ``python -m euler.gateway`` with ``EULER_ROLE=worker`` against
               this project and the configured verify URL, proven by a live
               stdio MCP ``initialize`` + ``tools/list`` handshake
  verify       ``GET /health`` is ok **and** the responder is THIS deployment
               (matching pidfile pid **and** nonempty matching ``euler_root``),
               not merely anything on :8091. Missing ``euler_root`` is foreign.
  stores       project ``fact_graph/`` and ``global_memory/`` plus this worker's
               ``local_memory/`` are contained in their expected roots (symlink
               escape fails closed) and writable via a unique atomic probe that
               is removed; never a fact or memory JSONL write

Production fallbacks from missing MCP tools to direct JSONL writes or
``from euler.gateway.server import fact_submit`` are **prohibited**. Workers may
read shared stores where the contract allows; they must never bypass writes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import layout as L
from .scaffold import _verify_url
from euler.providers.base import ProviderName

# The complete worker tool surface the gateway must expose (ARCHITECTURE.md §3).
# ROLE_TOOLS["worker"] must be a superset; FastMCP must register every name.
REQUIRED_WORKER_TOOLS: Tuple[str, ...] = (
    "gm_add",
    "gm_search",
    "fact_submit",
    "fact_search",
    "search_arxiv_theorems",
)

# Unique atomic probe prefix — not a fact, not a memory kind JSONL.
_STORE_PROBE_PREFIX = ".euler_preflight_probe."

_VERIFY_HEALTH_NOTE = (
    "transport identity only; not a mathematical verdict"
)

# bash euler_verify_health exit codes (scripts/env.sh) + unhealthy-ours (Python).
_IDENTITY_EXIT = {
    "ours": 0,
    "unhealthy": 6,  # pid+root match but HTTP/status not ok — never 0
    "foreign": 3,
    "stale": 4,
    "down": 5,
}

_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_HANDSHAKE_TIMEOUT = 8.0


# --------------------------------------------------------------------------- #
# result types                                                                 #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CheckResult:
    """One named preflight / doctor component."""

    component: str
    ok: bool
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    level: str = "ok"  # ok | warn | fail  (doctor: warn is not a hard failure)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "ok": self.ok,
            "reason": self.reason,
            "level": self.level,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class PreflightResult:
    """Aggregate of proving-preflight checks. ``ok`` is True iff every check passed."""

    ok: bool
    checks: Tuple[CheckResult, ...] = ()

    @property
    def failure(self) -> Optional[CheckResult]:
        for c in self.checks:
            if not c.ok:
                return c
        return None

    def to_status_dict(self) -> Dict[str, Any]:
        fail = self.failure
        return {
            "ok": self.ok,
            "component": None if fail is None else fail.component,
            "reason": "" if fail is None else fail.reason,
            "checks": [c.as_dict() for c in self.checks],
        }

    @classmethod
    def passed(cls, checks: Sequence[CheckResult] = ()) -> "PreflightResult":
        return cls(ok=True, checks=tuple(checks))


def _fail(component: str, reason: str, **details: Any) -> CheckResult:
    return CheckResult(component=component, ok=False, reason=reason,
                       details=details, level="fail")


def _ok(component: str, reason: str = "", **details: Any) -> CheckResult:
    return CheckResult(component=component, ok=True, reason=reason,
                       details=details, level="ok")


def _warn(component: str, reason: str, **details: Any) -> CheckResult:
    return CheckResult(component=component, ok=True, reason=reason,
                       details=details, level="warn")


# --------------------------------------------------------------------------- #
# roots                                                                        #
# --------------------------------------------------------------------------- #

def deployment_root() -> Path:
    """This Euler checkout. ``EULER_ROOT`` if set, else the repo that holds this package."""
    env = os.environ.get("EULER_ROOT")
    if env:
        return Path(env).resolve()
    return L.repo_root()


def runtime_dir() -> Path:
    env = os.environ.get("EULER_RUNTIME")
    if env:
        return Path(env).resolve()
    return deployment_root() / "runtime"


def verify_pidfile() -> Path:
    return runtime_dir() / "run" / "verify.pid"


def verify_url() -> str:
    return _verify_url()


def verify_health_url() -> str:
    """``EULER_VERIFY_URL`` names POST /verify; health is GET /health on the same origin."""
    url = verify_url().rstrip("/")
    if url.endswith("/verify"):
        return url[: -len("/verify")] + "/health"
    if url.endswith("/health"):
        return url
    return url + "/health"


# --------------------------------------------------------------------------- #
# python + MCP                                                                 #
# --------------------------------------------------------------------------- #

def check_python(python: Optional[str] = None) -> CheckResult:
    """The configured interpreter can ``import euler`` and ``euler._mcp.FastMCP`` from ``/``."""
    exe = python or sys.executable
    probe = (
        "from euler._mcp import FastMCP; "
        "import euler; "
        "assert FastMCP is not None"
    )
    try:
        proc = subprocess.run(
            [exe, "-c", probe],
            cwd="/",
            capture_output=True,
            text=True,
            timeout=20,
        )
    except OSError as e:
        return _fail("python", f"cannot execute {exe}: {e}", python=exe)
    except subprocess.TimeoutExpired:
        return _fail("python", f"timed out importing euler+mcp via {exe}", python=exe)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return _fail("python", f"configured python cannot import euler+mcp: {err}",
                     python=exe, returncode=proc.returncode)
    return _ok("python", python=exe)


# --------------------------------------------------------------------------- #
# gateway capability                                                           #
# --------------------------------------------------------------------------- #

def _registered_tool_names(app: Any) -> Optional[set]:
    """Best-effort inspect of a FastMCP / MCPServer tool registry. None = cannot inspect."""
    tm = getattr(app, "_tool_manager", None)
    if tm is not None:
        tools = getattr(tm, "_tools", None)
        if isinstance(tools, dict):
            return set(tools)
        get_tool = getattr(tm, "get_tool", None)
        if callable(get_tool):
            found: set = set()
            for name in REQUIRED_WORKER_TOOLS:
                try:
                    if get_tool(name) is not None:
                        found.add(name)
                except Exception:
                    continue
            return found
    for attr in ("_tools", "tools"):
        tools = getattr(app, attr, None)
        if isinstance(tools, dict):
            return set(tools)
    return None


def check_gateway() -> CheckResult:
    """Worker role table + registered FastMCP tools include the complete required set."""
    try:
        from euler.gateway.roles import ROLE_TOOLS, tools_for
        from euler.gateway.server import _TOOLS, build_app
    except Exception as e:
        return _fail("gateway", f"cannot import euler.gateway: {e}")

    table = list(tools_for("worker"))
    missing_table = [t for t in REQUIRED_WORKER_TOOLS if t not in table]
    if missing_table:
        return _fail(
            "gateway",
            f"worker ROLE_TOOLS missing {missing_table}",
            required=list(REQUIRED_WORKER_TOOLS),
            role_tools=table,
            role_tools_raw=list(ROLE_TOOLS.get("worker", ())),
        )
    missing_impl = [t for t in REQUIRED_WORKER_TOOLS if t not in _TOOLS]
    if missing_impl:
        return _fail(
            "gateway",
            f"gateway _TOOLS has no implementation for {missing_impl}",
            required=list(REQUIRED_WORKER_TOOLS),
        )
    try:
        app = build_app("worker")
    except Exception as e:
        return _fail("gateway", f"build_app('worker') failed: {e}")
    registered = _registered_tool_names(app)
    if registered is None:
        return _fail("gateway", "cannot inspect gateway tool registry (fail closed)")
    missing_reg = [t for t in REQUIRED_WORKER_TOOLS if t not in registered]
    if missing_reg:
        return _fail(
            "gateway",
            f"worker FastMCP is missing tools {missing_reg}",
            required=list(REQUIRED_WORKER_TOOLS),
            registered=sorted(registered),
        )
    return _ok(
        "gateway",
        required=list(REQUIRED_WORKER_TOOLS),
        registered=sorted(registered),
    )


# --------------------------------------------------------------------------- #
# MCP config (worker .codex/config.toml)                                       #
# --------------------------------------------------------------------------- #

def _load_toml(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "TOML parser unavailable (need Python 3.11+ tomllib or the tomli package)"
            ) from e
        return tomllib.loads(text)
    return tomllib.loads(text)


def _as_str_list(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        out.append(item)
    return out


def check_mcp_config(wl: L.WorkerLayout, python: Optional[str] = None) -> CheckResult:
    """Worker MCP config must launch this interpreter's ``euler.gateway`` as role=worker."""
    path = wl.codex_config
    if not path.is_file():
        return _fail("mcp_config", f"missing worker MCP config: {path}", path=str(path))
    try:
        data = _load_toml(path)
    except Exception as e:
        return _fail("mcp_config", f"unreadable/invalid MCP config {path}: {e}",
                     path=str(path))

    servers = data.get("mcp_servers")
    if not isinstance(servers, dict) or "euler" not in servers:
        return _fail("mcp_config", "MCP config has no [mcp_servers.euler] table",
                     path=str(path))
    section = servers["euler"]
    if not isinstance(section, dict):
        return _fail("mcp_config", "[mcp_servers.euler] is not a table", path=str(path))

    exe = python or sys.executable
    command = section.get("command")
    if not isinstance(command, str) or not command:
        return _fail("mcp_config", "MCP command is missing or not a string", path=str(path))
    try:
        if Path(command).resolve() != Path(exe).resolve():
            return _fail(
                "mcp_config",
                f"MCP command {command!r} is not this interpreter {exe!r}",
                path=str(path), command=command, python=exe,
            )
    except OSError as e:
        return _fail("mcp_config", f"cannot resolve MCP command {command!r}: {e}",
                     path=str(path))

    args = _as_str_list(section.get("args"))
    if args != ["-m", "euler.gateway"]:
        return _fail(
            "mcp_config",
            f"MCP args must be ['-m', 'euler.gateway'], got {args!r}",
            path=str(path), args=args,
        )

    env = section.get("env")
    if not isinstance(env, dict):
        return _fail("mcp_config", "MCP env table [mcp_servers.euler.env] is missing",
                     path=str(path))
    role = env.get("EULER_ROLE")
    if role != "worker":
        return _fail(
            "mcp_config",
            f"MCP EULER_ROLE must be 'worker', got {role!r}",
            path=str(path), role=role,
        )
    project_dir = env.get("EULER_PROJECT_DIR")
    if not isinstance(project_dir, str) or not project_dir:
        return _fail("mcp_config", "MCP EULER_PROJECT_DIR is missing", path=str(path))
    try:
        if Path(project_dir).resolve() != wl.project_dir.resolve():
            return _fail(
                "mcp_config",
                f"MCP EULER_PROJECT_DIR {project_dir!r} is not this worker's project "
                f"{str(wl.project_dir)!r}",
                path=str(path), project_dir=project_dir,
                expected=str(wl.project_dir),
            )
    except OSError as e:
        return _fail("mcp_config", f"cannot resolve EULER_PROJECT_DIR: {e}", path=str(path))

    cfg_verify = env.get("EULER_VERIFY_URL")
    expected_verify = verify_url()
    if cfg_verify != expected_verify:
        return _fail(
            "mcp_config",
            f"MCP EULER_VERIFY_URL {cfg_verify!r} does not match {expected_verify!r}",
            path=str(path), verify_url=cfg_verify, expected=expected_verify,
        )

    str_env = {str(k): str(v) for k, v in env.items()}
    hs = mcp_stdio_handshake(
        command, args, str_env,
        cwd=str(wl.dir),
        timeout=_handshake_timeout(),
    )
    if not hs.ok:
        return _fail("mcp_config", hs.reason, path=str(path), **hs.details)
    return _ok(
        "mcp_config", path=str(path), role="worker", command=command,
        tools=hs.details.get("tools"),
    )


def check_grok_user_config(wl: L.WorkerLayout) -> CheckResult:
    """Grok workers must expose Euler as isolated *user* config, role=worker."""
    from euler.providers.grok import (
        isolated_worker_grok_home,
        validate_effective_grok_config,
    )

    home = isolated_worker_grok_home(wl.dir)
    path = home / "config.toml"
    reason = validate_effective_grok_config(
        path, expected_role="worker", expected_python=sys.executable,
        expected_project_dir=wl.project_dir, expected_author=wl.name,
        expected_verify_url=verify_url(),
    )
    if reason:
        return _fail(
            "grok_user_config",
            reason,
            path=str(path),
            grok_home=str(home),
        )
    return _ok(
        "grok_user_config",
        path=str(path),
        grok_home=str(home),
        role="worker",
        effective="user",
    )


def _handshake_timeout() -> float:
    raw = os.environ.get("EULER_MCP_HANDSHAKE_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return _MCP_HANDSHAKE_TIMEOUT


def _stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _readline_until(stream: Any, deadline: float) -> bytes:
    holder: List[bytes] = []
    errors: List[BaseException] = []

    def _read() -> None:
        try:
            holder.append(stream.readline())
        except BaseException as exc:  # noqa: BLE001 — surface I/O errors to caller
            errors.append(exc)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=max(0.0, deadline - time.time()))
    if t.is_alive():
        raise TimeoutError("timed out waiting for MCP JSON-RPC line")
    if errors:
        raise errors[0]
    return holder[0] if holder else b""


def _read_rpc(stream: Any, deadline: float, expect_id: Any) -> Dict[str, Any]:
    while time.time() < deadline:
        line = _readline_until(stream, deadline)
        if not line:
            raise RuntimeError("MCP process closed stdout before JSON-RPC response")
        stripped = line.strip()
        if not stripped:
            continue
        try:
            msg = json.loads(stripped)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"MCP stdout is not JSON-RPC: {stripped[:200]!r}"
            ) from e
        if not isinstance(msg, dict):
            raise RuntimeError(f"MCP JSON-RPC message is not an object: {msg!r}")
        if msg.get("id") == expect_id:
            return msg
        if "error" in msg and msg.get("id") == expect_id:
            return msg
    raise TimeoutError(f"timed out waiting for MCP JSON-RPC id={expect_id}")


def mcp_stdio_handshake(
    command: str,
    args: Sequence[str],
    env: Mapping[str, str],
    *,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    required_tools: Sequence[str] = REQUIRED_WORKER_TOOLS,
) -> CheckResult:
    """Spawn the configured stdio MCP command and require initialize + tools/list.

    This is the live proof that the *configured* process (not an in-process
    FastMCP registry) exposes the complete worker tool set.
    """
    limit = _handshake_timeout() if timeout is None else float(timeout)
    proc_env = os.environ.copy()
    for k, v in env.items():
        proc_env[str(k)] = str(v)
    cmd = [command, *list(args)]
    stderr_chunks: List[bytes] = []
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(  # noqa: S603 — command comes from worker MCP config
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proc_env,
            cwd=cwd or None,
            start_new_session=True,
        )
    except OSError as e:
        return _fail("mcp", f"MCP process failed to start {cmd!r}: {e}",
                     command=command, args=list(args))

    def _drain_err() -> None:
        try:
            assert proc is not None and proc.stderr is not None
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        except OSError:
            pass

    err_thread = threading.Thread(target=_drain_err, daemon=True)
    err_thread.start()
    deadline = time.time() + limit
    try:
        assert proc.stdin is not None and proc.stdout is not None
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "euler-preflight", "version": "0"},
            },
        }
        proc.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
        proc.stdin.flush()
        init_msg = _read_rpc(proc.stdout, deadline, 1)
        if proc.poll() not in (None,) and "result" not in init_msg:
            err = b"".join(stderr_chunks).decode("utf-8", "replace")[:500]
            return _fail(
                "mcp",
                f"MCP process exited {proc.returncode} during initialize"
                + (f": {err}" if err else ""),
                command=command, args=list(args), returncode=proc.returncode,
            )
        if "error" in init_msg or "result" not in init_msg:
            return _fail(
                "mcp",
                f"MCP initialize failed: {init_msg.get('error') or init_msg}",
                command=command, args=list(args),
            )
        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
             + "\n").encode("utf-8")
        )
        proc.stdin.flush()
        list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        proc.stdin.write((json.dumps(list_req) + "\n").encode("utf-8"))
        proc.stdin.flush()
        list_msg = _read_rpc(proc.stdout, deadline, 2)
        if "error" in list_msg or "result" not in list_msg:
            return _fail(
                "mcp",
                f"MCP tools/list failed: {list_msg.get('error') or list_msg}",
                command=command, args=list(args),
            )
        result = list_msg.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return _fail("mcp", "MCP tools/list result has no tools array",
                         command=command, args=list(args))
        names: List[str] = []
        for item in tools:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
        missing = [t for t in required_tools if t not in names]
        if missing:
            return _fail(
                "mcp",
                f"MCP tools/list missing required worker tools {missing}",
                command=command, args=list(args),
                tools=names, missing=missing,
                required=list(required_tools),
            )
        return _ok("mcp", command=command, args=list(args), tools=sorted(set(names)))
    except TimeoutError as e:
        return _fail("mcp", f"MCP handshake timed out after {limit}s: {e}",
                     command=command, args=list(args), timeout=limit)
    except Exception as e:
        err = b"".join(stderr_chunks).decode("utf-8", "replace")[:500]
        extra = f" ({err})" if err else ""
        rc = None if proc is None else proc.poll()
        if rc is not None and rc != 0:
            return _fail(
                "mcp",
                f"MCP process failed to start or exited {rc}{extra}: {e}",
                command=command, args=list(args), returncode=rc,
            )
        return _fail("mcp", f"MCP handshake failed: {e}{extra}",
                     command=command, args=list(args))
    finally:
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            _stop_proc(proc)
            err_thread.join(timeout=0.5)


# --------------------------------------------------------------------------- #
# verify identity (transport only — never a mathematical verdict)              #
# --------------------------------------------------------------------------- #

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours to signal
    except OSError:
        return False


def _read_pidfile(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _http_health(url: str, timeout: float = 5.0) -> Tuple[Optional[int], Optional[Dict[str, Any]], str]:
    """GET /health. Returns (http_status, json_or_none, error_or_empty)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback)
            raw = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except OSError:
            raw = b""
        status = e.code
        body: Optional[Dict[str, Any]]
        try:
            parsed = json.loads(raw.decode("utf-8") or "null")
            body = parsed if isinstance(parsed, dict) else None
        except (ValueError, UnicodeDecodeError):
            body = None
        return status, body, ""
    except Exception as e:
        return None, None, str(e)
    try:
        parsed = json.loads(raw.decode("utf-8") or "null")
    except (ValueError, UnicodeDecodeError):
        return status, None, "health body is not JSON"
    if not isinstance(parsed, dict):
        return status, None, "health body is not a JSON object"
    return status, parsed, ""


def classify_verify_identity(timeout: float = 5.0) -> Dict[str, Any]:
    """Classify the /health responder the same way ``euler_verify_health`` does.

    Identity is **transport** only: ours / foreign / stale / down. A mathematical
    verdict is never inferred here (this function does not POST /verify).
    """
    url = verify_health_url()
    pf = verify_pidfile()
    our_pid = _read_pidfile(pf)
    expected_root = str(deployment_root())
    http_status, body, err = _http_health(url, timeout=timeout)
    out: Dict[str, Any] = {
        "identity": "down",
        "ok": False,
        "health_url": url,
        "pidfile": str(pf),
        "our_pid": our_pid,
        "health_pid": None,
        "health_status": None,
        "http_status": http_status,
        "euler_root": None,
        "expected_euler_root": expected_root,
        "reason": "",
        "note": _VERIFY_HEALTH_NOTE,
        "exit_code": _IDENTITY_EXIT["down"],
    }
    if body is None and http_status is None:
        # nothing answered
        if our_pid is not None and _pid_alive(our_pid):
            out["identity"] = "down"
            out["reason"] = f"pidfile process {our_pid} is alive but {url} did not answer"
            out["exit_code"] = _IDENTITY_EXIT["down"]
        elif our_pid is not None:
            out["identity"] = "stale"
            out["reason"] = f"stale pidfile {pf} (pid {our_pid} is dead) and {url} did not answer"
            out["exit_code"] = _IDENTITY_EXIT["stale"]
        else:
            out["identity"] = "down"
            out["reason"] = f"nothing answers {url} and no verify pidfile process"
            out["exit_code"] = _IDENTITY_EXIT["down"]
        if err and not out["reason"].endswith(err):
            out["reason"] = out["reason"] + f" ({err})"
        return out

    health_pid = None
    if isinstance(body, dict) and "pid" in body:
        try:
            health_pid = int(body["pid"])
        except (TypeError, ValueError):
            health_pid = None
    health_root = None
    if isinstance(body, dict):
        raw_root = body.get("euler_root")
        if isinstance(raw_root, str) and raw_root.strip():
            try:
                health_root = str(Path(raw_root.strip()).resolve())
            except (OSError, ValueError):
                health_root = raw_root.strip()
        elif raw_root not in (None, ""):
            try:
                s = str(raw_root).strip()
                if s:
                    health_root = str(Path(s).resolve())
            except (OSError, TypeError, ValueError):
                health_root = str(raw_root)
    out["health_pid"] = health_pid
    out["euler_root"] = health_root
    out["health_status"] = None if not isinstance(body, dict) else body.get("status")

    pid_match = (
        our_pid is not None
        and health_pid is not None
        and our_pid == health_pid
    )
    root_match = False
    if health_root is not None:
        try:
            root_match = Path(health_root).resolve() == Path(expected_root).resolve()
        except OSError:
            root_match = False

    # Ours requires BOTH matching pid AND nonempty matching euler_root.
    if pid_match and root_match:
        status_ok = isinstance(body, dict) and body.get("status") == "ok"
        http_ok = http_status == 200
        out["identity"] = "ours"
        out["ok"] = bool(status_ok and http_ok)
        if out["ok"]:
            out["exit_code"] = _IDENTITY_EXIT["ours"]
            out["reason"] = ""
        else:
            # Never report exit_code 0 for an unhealthy own responder.
            out["exit_code"] = _IDENTITY_EXIT["unhealthy"]
            if not http_ok:
                out["reason"] = (
                    f"responder is this deployment (pid {health_pid}, euler_root "
                    f"match) but HTTP {http_status} (not a mathematical verdict)"
                )
            else:
                out["reason"] = (
                    f"responder is this deployment (pid {health_pid}, euler_root "
                    f"match) but /health status={out['health_status']!r} "
                    f"(not a mathematical verdict)"
                )
        return out

    out["identity"] = "foreign"
    out["ok"] = False
    out["exit_code"] = _IDENTITY_EXIT["foreign"]
    if pid_match and health_root is None:
        out["reason"] = (
            f"{url} pid matches pidfile {our_pid} but euler_root is missing/empty "
            f"(fail closed; not proven to be this deployment)"
        )
    elif health_root is not None and not root_match:
        out["reason"] = (
            f"/health euler_root={health_root!r} is not this deployment "
            f"{expected_root!r}"
        )
    elif our_pid is None:
        out["reason"] = (
            f"{url} answered (pid={health_pid}) but we have no verify pidfile"
        )
    elif health_pid is None:
        out["reason"] = (
            f"{url} answered but did not self-identify with a pid "
            f"(not this deployment's verify)"
        )
    else:
        out["reason"] = (
            f"{url} pid={health_pid} does not match pidfile {our_pid} "
            f"(another process holds this port)"
        )
    return out


def _ident_details(ident: Mapping[str, Any]) -> Dict[str, Any]:
    """Identity payload as CheckResult.details (never collide with reason/ok)."""
    return {k: v for k, v in ident.items() if k not in ("ok", "reason")}


def check_verify() -> CheckResult:
    """Fail closed unless /health is ok AND the responder is this deployment."""
    ident = classify_verify_identity()
    details = _ident_details(ident)
    if ident.get("identity") == "ours" and ident.get("ok"):
        return _ok("verify", **details)
    reason = ident.get("reason") or f"verify identity is {ident.get('identity')}"
    return _fail("verify", reason, **details)


def check_verify_for_doctor() -> CheckResult:
    """Doctor: foreign is FAIL; down/stale are warnings (list/status still work)."""
    ident = classify_verify_identity()
    details = _ident_details(ident)
    identity = ident.get("identity")
    if identity == "ours" and ident.get("ok"):
        return _ok("verify", **details)
    if identity == "foreign":
        return _fail("verify", ident.get("reason") or "foreign verify process", **details)
    # down / stale / unhealthy-ours: warn, do not hard-fail doctor
    return _warn("verify", ident.get("reason") or f"verify identity is {identity}",
                 **details)


# --------------------------------------------------------------------------- #
# store access (scoped probe — never a fact or memory write)                   #
# --------------------------------------------------------------------------- #

def _is_contained(path: Path, root: Path) -> bool:
    """True iff ``path`` resolves inside ``root`` (symlink targets included)."""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def _probe_dir(path: Path, label: str, expected_root: Path) -> Optional[str]:
    """Write+unlink a unique exclusive probe. Return an error reason or None."""
    if not path.exists():
        return f"{label} does not exist: {path}"
    if not path.is_dir():
        return f"{label} is not a directory: {path}"
    if not _is_contained(path, expected_root):
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        return (
            f"{label} escapes expected root {expected_root} "
            f"(resolves to {resolved})"
        )
    fd: Optional[int] = None
    probe: Optional[Path] = None
    err: Optional[str] = None
    try:
        fd, name = tempfile.mkstemp(prefix=_STORE_PROBE_PREFIX, dir=str(path))
        probe = Path(name)
        if not probe.name.startswith(_STORE_PROBE_PREFIX):
            err = f"{label} probe used a non-scoped name: {probe.name}"
        elif not _is_contained(probe, expected_root):
            try:
                resolved = str(probe.resolve())
            except OSError:
                resolved = str(probe)
            err = (
                f"{label} probe escaped expected root {expected_root} "
                f"(resolves to {resolved})"
            )
        else:
            os.write(fd, b"euler-preflight-probe\n")
            os.fsync(fd)
    except OSError as e:
        err = f"{label} is not writable: {path} ({e})"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe is not None:
            try:
                probe.unlink()
            except OSError as e:
                if err is None:
                    err = f"{label} probe could not be removed: {probe} ({e})"
    if err is None and probe is not None and probe.exists():
        err = f"{label} probe was not removed: {probe}"
    return err


def check_stores(wl: L.WorkerLayout) -> CheckResult:
    """Project truth/memory dirs are contained, writable, via a unique removed probe."""
    pdir = wl.project_dir
    if not pdir.is_dir():
        return _fail("stores", f"project dir is not a directory: {pdir}",
                     project_dir=str(pdir))
    errors: List[str] = []
    probed = (
        ("fact_graph", pdir / "fact_graph", pdir),
        ("global_memory", pdir / "global_memory", pdir),
        ("local_memory", wl.local_memory, wl.dir),
    )
    for label, path, root in probed:
        err = _probe_dir(path, label, root)
        if err:
            errors.append(err)
    if errors:
        return _fail("stores", "; ".join(errors),
                     project_dir=str(pdir), probe_prefix=_STORE_PROBE_PREFIX)
    return _ok("stores", project_dir=str(pdir), probe_prefix=_STORE_PROBE_PREFIX,
               probed=[label for label, _, _ in probed])


# --------------------------------------------------------------------------- #
# preflight                                                                    #
# --------------------------------------------------------------------------- #

def run_proving_preflight(wl: L.WorkerLayout, run_id: Optional[str] = None) -> PreflightResult:
    """Run every proving-readiness check. Never launches codex. Never writes facts."""
    from euler.providers.registry import check_readiness

    ready = check_readiness(wl)
    backend_check = (
        _ok(
            "worker_backend",
            provider=ready.provider.value,
            backend=ready.provider.value,
            binary=ready.binary,
            config=ready.config,
        )
        if ready.ok else
        _fail(
            "worker_backend",
            ready.reason,
            provider=ready.provider.value,
            backend=ready.provider.value,
            binary=ready.binary,
            config=ready.config,
        )
    )
    checks: List[CheckResult] = [
        check_python(),
        check_gateway(),
        check_mcp_config(wl),
        backend_check,
    ]
    if ready.provider is ProviderName.GROK:
        checks.append(check_grok_user_config(wl))
    if run_id is not None:
        from .context import check_round_context
        checks.append(check_round_context(wl, run_id))
    checks.extend([check_verify(), check_stores(wl)])
    return PreflightResult(ok=all(c.ok for c in checks), checks=tuple(checks))


def doctor_checks() -> List[CheckResult]:
    """Deployment doctor (no project required). Read-only; verify down is a warning."""
    extra: List[CheckResult] = []
    # verify-service library deps (soft-fail as fail if missing — workers cannot
    # submit without the service, but doctor still distinguishes from identity).
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        import uvicorn  # noqa: F401
        extra.append(_ok("verify_deps", "fastapi/uvicorn/pydantic importable"))
    except Exception as e:
        extra.append(_fail("verify_deps", f"verify-service deps missing: {e}"))
    return [
        check_python(),
        check_gateway(),
        *extra,
        check_verify_for_doctor(),
    ]
