# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Fail-closed proving preflight + verify identity (no real codex, no live verify).

Covers:
  - verifier down / foreign / stale / ours
  - missing gateway capability
  - missing / bad MCP config
  - unwritable store
  - store probe is scoped and removed (never a fact or memory write)
  - loop.main never calls run_round on preflight failure
  - spawn_loop is not invoked (tested via orchestration tests)

Runs standalone (``python -m euler.execution.tests.test_preflight``) and pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from euler.execution import layout as L
from euler.execution import loop, scaffold
from euler.execution import preflight as P


# --------------------------------------------------------------------------- #
# env helpers                                                                  #
# --------------------------------------------------------------------------- #

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


@contextmanager
def _project_env(tmp: Path, **extra):
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    runtime = tmp / "runtime"
    (runtime / "run").mkdir(parents=True, exist_ok=True)
    env = {
        "EULER_AGENTS_ROOT": str(tmp / "agents"),
        "EULER_WORKER_CONTRACT": str(contract),
        "EULER_WORKER_SKILLS": str(skills),
        "EULER_RUNTIME": str(runtime),
        "EULER_ROOT": str(L.repo_root()),
    }
    env.update(extra)
    with _env(**env):
        yield


def _wl(project: str, worker: str) -> L.WorkerLayout:
    return L.WorkerLayout(L.worker_dir(project, worker))


# --------------------------------------------------------------------------- #
# tiny /health stub                                                            #
# --------------------------------------------------------------------------- #

class _HealthHTTP(HTTPServer):
    body: Dict[str, Any]
    status: int
    hang: bool


def serve_health(body: Dict[str, Any], status: int = 200, hang: bool = False) -> _HealthHTTP:
    """Loopback /health server. Caller must shutdown()."""

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            httpd: _HealthHTTP = self.server  # type: ignore[assignment]
            if httpd.hang:
                time.sleep(30)
            path = self.path.split("?", 1)[0]
            if path != "/health":
                self.send_error(404)
                return
            raw = json.dumps(httpd.body).encode("utf-8")
            self.send_response(httpd.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a, **k):
            return

    httpd = _HealthHTTP(("127.0.0.1", 0), H)
    httpd.body = body
    httpd.status = status
    httpd.hang = hang
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


@contextmanager
def ours_verify(tmp: Path, *, pid: Optional[int] = None,
                euler_root: Optional[str] = None,
                status: str = "ok"):
    """Start an 'ours' /health responder + matching pidfile; yield env extras."""
    runtime = tmp / "runtime"
    (runtime / "run").mkdir(parents=True, exist_ok=True)
    ident_pid = os.getpid() if pid is None else pid
    root = euler_root if euler_root is not None else str(L.repo_root())
    httpd = serve_health({"status": status, "pid": ident_pid, "euler_root": root})
    port = httpd.server_address[1]
    (runtime / "run" / "verify.pid").write_text(str(ident_pid), encoding="utf-8")
    extra = {
        "EULER_RUNTIME": str(runtime),
        "EULER_VERIFY_URL": f"http://127.0.0.1:{port}/verify",
        "VERIFY_PORT": str(port),
        "EULER_ROOT": str(L.repo_root()),
    }
    try:
        yield extra, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------- #
# verify identity                                                              #
# --------------------------------------------------------------------------- #

def test_verify_identity_down(tmp: Path):
    with _project_env(tmp, EULER_VERIFY_URL="http://127.0.0.1:1/verify"):
        info = P.classify_verify_identity(timeout=0.3)
        assert info["identity"] == "down"
        assert info["ok"] is False
        assert info["exit_code"] == 5
        assert "mathematical verdict" in info["note"]


def test_verify_identity_stale(tmp: Path):
    with _project_env(tmp, EULER_VERIFY_URL="http://127.0.0.1:1/verify"):
        P.verify_pidfile().write_text("2000000000\n", encoding="utf-8")
        info = P.classify_verify_identity(timeout=0.3)
        assert info["identity"] == "stale"
        assert info["ok"] is False
        assert info["exit_code"] == 4


def test_verify_identity_foreign_pid_mismatch(tmp: Path):
    with _project_env(tmp):
        httpd = serve_health({"status": "ok", "pid": 1, "euler_root": str(L.repo_root())})
        try:
            port = httpd.server_address[1]
            P.verify_pidfile().write_text(str(os.getpid()), encoding="utf-8")
            with _env(EULER_VERIFY_URL=f"http://127.0.0.1:{port}/verify"):
                info = P.classify_verify_identity(timeout=1.0)
            assert info["identity"] == "foreign"
            assert info["ok"] is False
            assert info["exit_code"] == 3
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_verify_identity_foreign_euler_root(tmp: Path):
    """Pid matches pidfile but euler_root is another checkout → foreign."""
    with _project_env(tmp):
        httpd = serve_health({
            "status": "ok", "pid": os.getpid(),
            "euler_root": "/not/this/deployment",
        })
        try:
            port = httpd.server_address[1]
            P.verify_pidfile().write_text(str(os.getpid()), encoding="utf-8")
            with _env(EULER_VERIFY_URL=f"http://127.0.0.1:{port}/verify"):
                info = P.classify_verify_identity(timeout=1.0)
            assert info["identity"] == "foreign"
            assert "euler_root" in info["reason"]
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_verify_identity_ours(tmp: Path):
    with ours_verify(tmp) as (extra, _httpd):
        with _env(**extra):
            info = P.classify_verify_identity(timeout=1.0)
        assert info["identity"] == "ours"
        assert info["ok"] is True
        assert info["exit_code"] == 0
        assert info["health_pid"] == os.getpid()


def test_verify_identity_missing_euler_root_is_foreign(tmp: Path):
    """Pid matches pidfile but euler_root is absent → foreign, fail closed."""
    with _project_env(tmp):
        httpd = serve_health({"status": "ok", "pid": os.getpid()})
        try:
            port = httpd.server_address[1]
            P.verify_pidfile().write_text(str(os.getpid()), encoding="utf-8")
            with _env(EULER_VERIFY_URL=f"http://127.0.0.1:{port}/verify",
                      EULER_ROOT=str(L.repo_root())):
                info = P.classify_verify_identity(timeout=1.0)
            assert info["identity"] == "foreign"
            assert info["ok"] is False
            assert info["exit_code"] != 0
            assert "euler_root" in info["reason"]
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_verify_identity_ours_unhealthy_status_nonzero_exit(tmp: Path):
    with _project_env(tmp):
        httpd = serve_health({
            "status": "degraded", "pid": os.getpid(),
            "euler_root": str(L.repo_root()),
        })
        try:
            port = httpd.server_address[1]
            P.verify_pidfile().write_text(str(os.getpid()), encoding="utf-8")
            with _env(EULER_VERIFY_URL=f"http://127.0.0.1:{port}/verify",
                      EULER_ROOT=str(L.repo_root())):
                info = P.classify_verify_identity(timeout=1.0)
            assert info["identity"] == "ours"
            assert info["ok"] is False
            assert info["exit_code"] != 0
        finally:
            httpd.shutdown()
            httpd.server_close()


def _bash_health(tmp: Path, port: int, pid: int) -> subprocess.CompletedProcess:
    """Call the sourced scripts/env.sh classifier against a controlled server."""
    runtime = tmp / "runtime-shell"
    (runtime / "run").mkdir(parents=True, exist_ok=True)
    (runtime / "run" / "verify.pid").write_text(str(pid), encoding="utf-8")
    script = L.repo_root() / "scripts" / "env.sh"
    command = (
        f'. "{script}"; '
        f'export EULER_RUNTIME="{runtime}" VERIFY_PORT="{port}" '
        f'EULER_ROOT="{L.repo_root()}"; '
        'euler_verify_health'
    )
    return subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False,
    )


def test_bash_health_requires_pid_root_and_healthy_status(tmp: Path):
    """The shell helper must enforce the same identity contract as Python."""
    pid = os.getpid()

    ours = serve_health({
        "status": "ok", "pid": pid, "euler_root": str(L.repo_root()),
    })
    try:
        result = _bash_health(tmp, ours.server_address[1], pid)
        assert result.returncode == 0 and result.stdout.strip() == "ours", result
    finally:
        ours.shutdown()
        ours.server_close()

    missing_root = serve_health({"status": "ok", "pid": pid})
    try:
        result = _bash_health(tmp, missing_root.server_address[1], pid)
        assert result.returncode == 3 and result.stdout.strip() == "foreign", result
    finally:
        missing_root.shutdown()
        missing_root.server_close()

    unhealthy = serve_health({
        "status": "degraded", "pid": pid, "euler_root": str(L.repo_root()),
    })
    try:
        result = _bash_health(tmp, unhealthy.server_address[1], pid)
        assert result.returncode == 6 and result.stdout.strip() == "unhealthy", result
    finally:
        unhealthy.shutdown()
        unhealthy.server_close()


def test_verify_identity_ours_http_non_200_nonzero_exit(tmp: Path):
    with _project_env(tmp):
        httpd = serve_health({
            "status": "ok", "pid": os.getpid(),
            "euler_root": str(L.repo_root()),
        }, status=503)
        try:
            port = httpd.server_address[1]
            P.verify_pidfile().write_text(str(os.getpid()), encoding="utf-8")
            with _env(EULER_VERIFY_URL=f"http://127.0.0.1:{port}/verify",
                      EULER_ROOT=str(L.repo_root())):
                info = P.classify_verify_identity(timeout=1.0)
            assert info["identity"] == "ours"
            assert info["ok"] is False
            assert info["exit_code"] != 0
            assert info["http_status"] == 503
        finally:
            httpd.shutdown()
            httpd.server_close()


# --------------------------------------------------------------------------- #
# python / gateway                                                             #
# --------------------------------------------------------------------------- #

def test_check_python_ok():
    r = P.check_python()
    assert r.ok and r.component == "python"


def test_check_gateway_ok():
    r = P.check_gateway()
    assert r.ok, r.reason
    for name in P.REQUIRED_WORKER_TOOLS:
        assert name in r.details["registered"]


def test_missing_gateway_capability(tmp: Path):
    import euler.gateway.roles as roles
    orig = roles.ROLE_TOOLS
    roles.ROLE_TOOLS = {**orig, "worker": ("gm_search", "fact_search")}
    try:
        r = P.check_gateway()
        assert r.ok is False and r.component == "gateway"
        assert "missing" in r.reason
        assert "fact_submit" in r.reason or "fact_submit" in str(r.details)
    finally:
        roles.ROLE_TOOLS = orig


# --------------------------------------------------------------------------- #
# MCP config                                                                   #
# --------------------------------------------------------------------------- #

def test_missing_mcp_config(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        wl.codex_config.unlink()
        r = P.check_mcp_config(wl)
        assert r.ok is False and r.component == "mcp_config"
        assert "missing" in r.reason


def test_bad_mcp_config_role_main(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        text = wl.codex_config.read_text(encoding="utf-8")
        wl.codex_config.write_text(text.replace('EULER_ROLE = "worker"',
                                                'EULER_ROLE = "main"'))
        r = P.check_mcp_config(wl)
        assert r.ok is False and r.component == "mcp_config"
        assert "EULER_ROLE" in r.reason


def test_bad_mcp_config_wrong_module(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        text = wl.codex_config.read_text(encoding="utf-8")
        wl.codex_config.write_text(
            text.replace('args = ["-m", "euler.gateway"]',
                         'args = ["-m", "euler.gateway.server"]')
        )
        r = P.check_mcp_config(wl)
        assert r.ok is False and r.component == "mcp_config"
        assert "args" in r.reason


def test_good_mcp_config(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        r = P.check_mcp_config(wl)
        assert r.ok, r.reason


# --------------------------------------------------------------------------- #
# live MCP stdio handshake (fake / mutated processes)                          #
# --------------------------------------------------------------------------- #

def _write_mcp_script(tmp: Path, body: str, name: str = "fake_mcp.py") -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _mcp_ok_script(tools: list) -> str:
    tool_json = json.dumps([{"name": n} for n in tools])
    return (
        "import json, sys\n"
        "def recv():\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        raise SystemExit(0)\n"
        "    return json.loads(line)\n"
        "def send(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "msg = recv()\n"
        "send({'jsonrpc':'2.0','id':msg['id'],'result':{"
        "'protocolVersion':'2024-11-05','capabilities':{'tools':{}},"
        "'serverInfo':{'name':'fake','version':'0'}}})\n"
        "recv()  # notifications/initialized\n"
        "msg = recv()\n"
        f"send({{'jsonrpc':'2.0','id':msg['id'],'result':{{'tools': {tool_json}}}}})\n"
        "sys.stdin.read()\n"
    )


def test_mcp_handshake_success(tmp: Path):
    script = _write_mcp_script(tmp, _mcp_ok_script(list(P.REQUIRED_WORKER_TOOLS)))
    r = P.mcp_stdio_handshake(sys.executable, [str(script)], {}, timeout=5.0)
    assert r.ok, r.reason
    for name in P.REQUIRED_WORKER_TOOLS:
        assert name in r.details["tools"]


def test_mcp_handshake_startup_failure(tmp: Path):
    script = _write_mcp_script(tmp, "import sys\nsys.exit(1)\n", name="die.py")
    r = P.mcp_stdio_handshake(sys.executable, [str(script)], {}, timeout=3.0)
    assert r.ok is False
    assert "exited" in r.reason or "failed" in r.reason or "closed stdout" in r.reason


def test_mcp_handshake_incomplete_list(tmp: Path):
    script = _write_mcp_script(tmp, _mcp_ok_script(["gm_search", "fact_search"]))
    r = P.mcp_stdio_handshake(sys.executable, [str(script)], {}, timeout=5.0)
    assert r.ok is False
    assert "missing" in r.reason
    assert "fact_submit" in r.reason or "fact_submit" in str(r.details)


def test_mcp_handshake_timeout(tmp: Path):
    script = _write_mcp_script(tmp, "import time\ntime.sleep(30)\n", name="hang.py")
    r = P.mcp_stdio_handshake(sys.executable, [str(script)], {}, timeout=0.4)
    assert r.ok is False
    assert "timed out" in r.reason


def test_mcp_config_handshake_uses_configured_command_args_env(tmp: Path):
    """check_mcp_config must handshake the exact toml command/args/env."""
    seen: dict = {}

    def fake_hs(command, args, env, cwd=None, timeout=None):
        seen["command"] = command
        seen["args"] = list(args)
        seen["env"] = dict(env)
        seen["cwd"] = cwd
        return P._ok("mcp", tools=list(P.REQUIRED_WORKER_TOOLS))

    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        orig = P.mcp_stdio_handshake
        P.mcp_stdio_handshake = fake_hs
        try:
            r = P.check_mcp_config(wl)
        finally:
            P.mcp_stdio_handshake = orig
        assert r.ok, r.reason
        assert Path(seen["command"]).resolve() == Path(sys.executable).resolve()
        assert seen["args"] == ["-m", "euler.gateway"]
        assert seen["env"].get("EULER_ROLE") == "worker"
        assert Path(seen["env"]["EULER_PROJECT_DIR"]).resolve() == wl.project_dir.resolve()


# --------------------------------------------------------------------------- #
# stores                                                                       #
# --------------------------------------------------------------------------- #

def test_unwritable_store(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        gm = wl.project_dir / "global_memory"
        mode = gm.stat().st_mode
        try:
            os.chmod(gm, 0o555)
            r = P.check_stores(wl)
            if os.geteuid() == 0:
                return  # root can still write; skip assertion
            assert r.ok is False and r.component == "stores"
            assert "global_memory" in r.reason
        finally:
            os.chmod(gm, mode)


def test_store_probe_removed_never_writes_facts(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        r = P.check_stores(wl)
        assert r.ok, r.reason
        pdir = wl.project_dir
        leftover = (
            list((pdir / "fact_graph").glob(P._STORE_PROBE_PREFIX + "*"))
            + list((pdir / "global_memory").glob(P._STORE_PROBE_PREFIX + "*"))
            + list(wl.local_memory.glob(P._STORE_PROBE_PREFIX + "*"))
        )
        assert leftover == []
        facts = pdir / "fact_graph" / "facts"
        if facts.is_dir():
            assert list(facts.iterdir()) == []
        # no memory JSONL created by the probe
        jsonl = list((pdir / "global_memory").glob("*.jsonl"))
        assert jsonl == []
        assert list(wl.local_memory.glob("*.jsonl")) == []


def test_missing_local_memory_store(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        import shutil
        shutil.rmtree(wl.local_memory)
        r = P.check_stores(wl)
        assert r.ok is False and r.component == "stores"
        assert "local_memory" in r.reason


def test_symlinked_store_escaping_root_fails(tmp: Path):
    import shutil
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        outside = tmp / "outside_graph"
        outside.mkdir()
        fg = wl.project_dir / "fact_graph"
        shutil.rmtree(fg)
        fg.symlink_to(outside)
        r = P.check_stores(wl)
        assert r.ok is False and r.component == "stores"
        assert "fact_graph" in r.reason
        assert "escapes" in r.reason


def test_concurrent_probes_do_not_delete_each_other(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        errors: list = []

        def _run():
            r = P.check_stores(wl)
            if not r.ok:
                errors.append(r.reason)

        threads = [threading.Thread(target=_run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], errors
        leftover = list((wl.project_dir / "fact_graph").glob(P._STORE_PROBE_PREFIX + "*"))
        assert leftover == []


# --------------------------------------------------------------------------- #
# full preflight positive + loop never run_round                               #
# --------------------------------------------------------------------------- #

def test_preflight_positive(tmp: Path):
    with ours_verify(tmp) as (extra, _httpd), _project_env(tmp, **extra):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        pre = P.run_proving_preflight(wl)
        assert pre.ok, pre.to_status_dict()
        assert pre.failure is None


def test_preflight_verify_down_structured(tmp: Path):
    """Full preflight with verify down records component=verify (no TypeError)."""
    with _project_env(tmp, EULER_VERIFY_URL="http://127.0.0.1:1/verify"):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        pre = P.run_proving_preflight(wl)
        assert pre.ok is False
        assert pre.failure is not None and pre.failure.component == "verify"
        d = pre.to_status_dict()
        assert d["component"] == "verify" and d["reason"]
        assert d["ok"] is False


def test_preflight_foreign_verify(tmp: Path):
    with _project_env(tmp):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        httpd = serve_health({"status": "ok", "pid": 1, "euler_root": str(L.repo_root())})
        try:
            port = httpd.server_address[1]
            P.verify_pidfile().write_text(str(os.getpid()), encoding="utf-8")
            url = f"http://127.0.0.1:{port}/verify"
            with _env(EULER_VERIFY_URL=url):
                scaffold.write_codex_config(wl)
                pre = P.run_proving_preflight(wl)
            assert pre.ok is False
            assert pre.failure is not None and pre.failure.component == "verify"
            assert pre.failure.details.get("identity") == "foreign"
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_preflight_stale_verify(tmp: Path):
    with _project_env(tmp, EULER_VERIFY_URL="http://127.0.0.1:1/verify"):
        scaffold.do_new("P", roles="high:1")
        wl = _wl("P", "high")
        P.verify_pidfile().write_text("2000000000\n", encoding="utf-8")
        pre = P.run_proving_preflight(wl)
        assert pre.ok is False
        assert pre.failure is not None and pre.failure.component == "verify"
        assert pre.failure.details.get("identity") == "stale"


def test_loop_main_never_calls_run_round_on_preflight_failure(tmp: Path):
    """Verify down + no stores → PRECHECK_FAILED; run_round must not run."""
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    calls = []

    def boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("run_round must not be invoked on preflight failure")

    orig = loop.run_round
    loop.run_round = boom
    old_sig = None
    try:
        import signal
        old_sig = signal.getsignal(signal.SIGTERM)
        with _env(EULER_ROUND_BEAT="0",
                  EULER_RUNTIME=str(tmp / "runtime"),
                  EULER_VERIFY_URL="http://127.0.0.1:1/verify",
                  EULER_ROOT=str(L.repo_root())):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
        if old_sig is not None:
            import signal
            signal.signal(signal.SIGTERM, old_sig)
    assert rc == 1
    assert calls == []
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    assert st["state"] == "PRECHECK_FAILED"
    assert st["precheck"]["ok"] is False
    assert st["precheck"]["component"] in ("verify", "stores", "mcp_config")


def test_loop_main_runs_round_when_preflight_ok(tmp: Path):
    calls = []

    def fake_round(*a, **k):
        calls.append(1)
        # stop after one round
        wl.stop.touch()
        return 0

    orig = loop.run_proving_preflight
    orig_rr = loop.run_round
    orig_c = loop.evaluate_round_close
    import signal
    old_sig = signal.getsignal(signal.SIGTERM)
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.task.write_text("---\nbranch_id: b1\nsubgoal_id: s1\n---\nProve it.\n",
                       encoding="utf-8")
    try:
        loop.run_proving_preflight = lambda w, run_id=None: P.PreflightResult.passed()
        loop.evaluate_round_close = lambda *a, **k: {"ok": True}
        loop.run_round = fake_round
        with _env(EULER_ROUND_BEAT="0"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_proving_preflight = orig
        loop.run_round = orig_rr
        loop.evaluate_round_close = orig_c
        signal.signal(signal.SIGTERM, old_sig)
    assert rc == 0
    assert calls == [1]


def test_loop_preflight_fails_between_rounds_skips_round_2(tmp: Path):
    """Round 1 succeeds; preflight then fails; run_round is not called again."""
    rounds = []
    fail = P.PreflightResult(ok=False, checks=(
        P.CheckResult(component="verify", ok=False, reason="down between rounds",
                      level="fail"),
    ))

    def fake_pre(_wl, run_id=None):
        if rounds:
            return fail
        return P.PreflightResult.passed()

    def fake_round(*_a, **_k):
        rounds.append(1)
        return 0

    orig_pre = loop.run_proving_preflight
    orig_rr = loop.run_round
    orig_c = loop.evaluate_round_close
    import signal
    old_sig = signal.getsignal(signal.SIGTERM)
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True)
    wl.task.write_text("---\nbranch_id: b1\nsubgoal_id: s1\n---\nProve it.\n",
                       encoding="utf-8")
    try:
        loop.run_proving_preflight = fake_pre
        loop.evaluate_round_close = lambda *a, **k: {"ok": True}
        loop.run_round = fake_round
        with _env(EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS="5"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_proving_preflight = orig_pre
        loop.run_round = orig_rr
        loop.evaluate_round_close = orig_c
        signal.signal(signal.SIGTERM, old_sig)
    assert rc == 1
    assert rounds == [1], "round 2 must not launch after mid-loop preflight failure"
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    assert st["state"] == "PRECHECK_FAILED"
    assert st["precheck"]["component"] == "verify"
    assert st["round"] == 1  # last completed round; round 2 never started


# --------------------------------------------------------------------------- #
# runner                                                                       #
# --------------------------------------------------------------------------- #

_NO_TMP = {test_check_python_ok, test_check_gateway_ok}


def main() -> None:
    tests = [
        test_verify_identity_down,
        test_verify_identity_stale,
        test_verify_identity_foreign_pid_mismatch,
        test_verify_identity_foreign_euler_root,
        test_verify_identity_ours,
        test_verify_identity_missing_euler_root_is_foreign,
        test_verify_identity_ours_unhealthy_status_nonzero_exit,
        test_bash_health_requires_pid_root_and_healthy_status,
        test_verify_identity_ours_http_non_200_nonzero_exit,
        test_check_python_ok,
        test_check_gateway_ok,
        test_missing_gateway_capability,
        test_missing_mcp_config,
        test_bad_mcp_config_role_main,
        test_bad_mcp_config_wrong_module,
        test_good_mcp_config,
        test_mcp_handshake_success,
        test_mcp_handshake_startup_failure,
        test_mcp_handshake_incomplete_list,
        test_mcp_handshake_timeout,
        test_mcp_config_handshake_uses_configured_command_args_env,
        test_unwritable_store,
        test_store_probe_removed_never_writes_facts,
        test_missing_local_memory_store,
        test_symlinked_store_escaping_root_fails,
        test_concurrent_probes_do_not_delete_each_other,
        test_preflight_positive,
        test_preflight_verify_down_structured,
        test_preflight_foreign_verify,
        test_preflight_stale_verify,
        test_loop_main_never_calls_run_round_on_preflight_failure,
        test_loop_main_runs_round_when_preflight_ok,
        test_loop_preflight_fails_between_rounds_skips_round_2,
    ]
    for t in tests:
        if t in _NO_TMP:
            t()
        else:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL PREFLIGHT TESTS PASSED")


if __name__ == "__main__":
    main()
