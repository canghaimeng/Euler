# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Offline tests for euler.orchestration — the ``euler`` CLI verbs.

Filesystem verbs (new/assign/status/list) are deterministic. The loop tests are
integration: they spawn the real ``python -m euler.execution`` loop subprocess but
stub codex with a fake shell binary (``EULER_CODEX_BIN``) so nothing real is
invoked and no API is spent. All processes are force-cleaned in ``finally``.

Runs standalone (``python -m euler.orchestration.tests.test_orchestration``) and
under pytest.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from euler.execution import layout as L
from euler.execution.tests.test_preflight import ours_verify
from euler.orchestration import cli


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
    """Agents root + stub worker contract/skills so tests never touch the repo's
    agents/ tree; merge any extra env (codex stub, round vars)."""
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    runtime = tmp / "runtime"
    (runtime / "run").mkdir(parents=True, exist_ok=True)
    env = {"EULER_AGENTS_ROOT": str(tmp / "agents"),
           "EULER_WORKER_CONTRACT": str(contract),
           "EULER_WORKER_SKILLS": str(skills),
           "EULER_RUNTIME": str(runtime)}
    env.update(extra)
    with _env(**env):
        yield


def _fake_codex(d: Path) -> Path:
    """A stub codex: print a round marker, record a partial proof_attempt, exit 0."""
    p = d / "fake_codex"
    p.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "print('fake codex round')\n"
        "time.sleep(float(os.environ.get('FAKE_CODEX_SLEEP', '0')))\n"
        "try:\n"
        "    from euler.gateway import server\n"
        "    rid = (os.environ.get('EULER_RUN_ID') or 'x')[:8]\n"
        "    server.gm_add('proof_attempt', claim='fake round attempt',\n"
        "        evidence='isolated fake; not a math verdict',\n"
        "        metadata={'attempt_id': 'fake-' + rid,\n"
        "                  'approach': 'fake_codex stub',\n"
        "                  'result': 'partial',\n"
        "                  'reason': 'stub round for tests',\n"
        "                  'salvage': 'none; test harness',\n"
        "                  'inputs': [], 'related_memory': []})\n"
        "except Exception as e:\n"
        "    print('gm_add skipped:', e)\n"
        "sys.exit(0)\n"
    )
    p.chmod(0o755)
    return p


def _stamp(project: str) -> None:
    for w in L.list_workers(project):
        cli.do_assign(f"{project}/{w}", "prove the assignment\n",
                      branch_id="b1", subgoal_id="s1")


def _wait_until(pred, timeout=15.0, interval=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _st(project: str, worker: str) -> dict:
    return cli.worker_status(L.WorkerLayout(L.worker_dir(project, worker)))


def _kill_project(project: str):
    try:
        cli.do_stop(project, force=True)
    except SystemExit:
        pass
    for d in L.target_worker_dirs(project):
        pid = cli._read_pid(L.WorkerLayout(d))
        if pid:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass


# --- filesystem verb tests ------------------------------------------------- #

def test_assign_replace_and_rejects(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "explore direction 3: the symplectic-rank route")
        assert L.WorkerLayout(L.worker_dir("P", "high")).task.read_text() == \
            "explore direction 3: the symplectic-rank route\n"
        cli.do_assign("P/high", "switch to direction 5")   # replace, not append
        assert L.WorkerLayout(L.worker_dir("P", "high")).task.read_text() == "switch to direction 5\n"
        for bad in ["P", "P/nope"]:
            try:
                cli.do_assign(bad, "x")
                assert False, f"should reject {bad!r}"
            except SystemExit:
                pass
        try:
            cli.do_assign("P/high", "   ")
            assert False, "should reject empty task"
        except SystemExit:
            pass


def test_status_before_start(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:1")
        s = _st("P", "high")
        assert s["alive"] is False and s["state"] == "created" and s["label"] == "created"


def test_list(tmp: Path):
    with _project_env(tmp):
        cli.do_new("P", roles="high:2", model="gpt-5.5")
        cli.do_new("Q", roles="xhigh:1", model="gpt-x")
        rows = {r["project"]: r for r in cli.do_list()}
        assert rows["P"]["workers"] == 2 and rows["P"]["live"] == 0 and rows["P"]["model"] == "gpt-5.5"
        assert rows["Q"]["workers"] == 1 and rows["Q"]["model"] == "gpt-x"


# --- loop integration tests (stubbed codex) -------------------------------- #

def test_loop_runs_rounds_then_exits(tmp: Path):
    fc = _fake_codex(tmp)
    with ours_verify(tmp) as (venv, _httpd), _project_env(
            tmp, EULER_CODEX_BIN=str(fc), EULER_ROUND_BEAT="0",
            EULER_MAX_ROUNDS="2", FAKE_CODEX_SLEEP="0", **venv):
        cli.do_new("P", roles="high:1")
        _stamp("P")
        from euler.workspace.milestones import configured
        checkpoint = Path(configured(L.project_dir("P"))["store"]) / "checkpoint-status.json"
        checkpoint.unlink(missing_ok=True)
        try:
            res = cli.do_start("P/high")
            assert res[0]["result"] == "started"
            assert _wait_until(lambda: not _st("P", "high")["alive"]), "loop should exit at backstop"
            # A non-live worker must have finished all epilogue writes, not
            # merely unlinked its PID while the Git snapshot is still running.
            assert json.loads(checkpoint.read_text())["state"] in {"saved", "unchanged"}
            s = _st("P", "high")
            assert s["state"] == "max_rounds" and s["round"] == 2
            wl = L.WorkerLayout(L.worker_dir("P", "high"))
            assert (wl.logs / "round_1.log").exists() and (wl.logs / "round_2.log").exists()
        finally:
            _kill_project("P")


def test_graceful_stop(tmp: Path):
    fc = _fake_codex(tmp)
    with ours_verify(tmp) as (venv, _httpd), _project_env(
            tmp, EULER_CODEX_BIN=str(fc), EULER_ROUND_BEAT="0.1",
            EULER_MAX_ROUNDS="0", FAKE_CODEX_SLEEP="0.1", **venv):
        cli.do_new("P", roles="high:1")
        _stamp("P")
        try:
            cli.do_start("P/high")
            assert _wait_until(lambda: _st("P", "high")["round"] >= 1), "should start a round"
            assert _st("P", "high")["alive"] is True
            r = cli.do_stop("P/high")            # graceful
            assert "graceful" in r[0]["result"]
            assert _wait_until(lambda: not _st("P", "high")["alive"]), "loop should exit after .stop"
            assert cli._read_pid(L.WorkerLayout(L.worker_dir("P", "high"))) is None  # pid cleaned
        finally:
            _kill_project("P")


def test_force_stop(tmp: Path):
    fc = _fake_codex(tmp)
    with ours_verify(tmp) as (venv, _httpd), _project_env(
            tmp, EULER_CODEX_BIN=str(fc), EULER_ROUND_BEAT="0",
            EULER_MAX_ROUNDS="0", FAKE_CODEX_SLEEP="30", **venv):
        cli.do_new("P", roles="high:1")
        _stamp("P")
        try:
            cli.do_start("P/high")
            assert _wait_until(lambda: _st("P", "high")["state"] == "running"), "round should run"
            r = cli.do_stop("P/high", force=True)
            assert r[0]["result"] == "killed"
            assert _wait_until(lambda: not _st("P", "high")["alive"], timeout=8), "force kills fast"
        finally:
            _kill_project("P")


def test_idempotent_start(tmp: Path):
    fc = _fake_codex(tmp)
    with ours_verify(tmp) as (venv, _httpd), _project_env(
            tmp, EULER_CODEX_BIN=str(fc), EULER_ROUND_BEAT="0",
            EULER_MAX_ROUNDS="0", FAKE_CODEX_SLEEP="30", **venv):
        cli.do_new("P", roles="high:1")
        _stamp("P")
        try:
            assert cli.do_start("P/high")[0]["result"] == "started"
            assert _wait_until(lambda: _st("P", "high")["alive"])
            assert cli.do_start("P/high")[0]["result"] == "already-running"
        finally:
            _kill_project("P")


def test_project_wide_targets(tmp: Path):
    fc = _fake_codex(tmp)
    with ours_verify(tmp) as (venv, _httpd), _project_env(
            tmp, EULER_CODEX_BIN=str(fc), EULER_ROUND_BEAT="0",
            EULER_MAX_ROUNDS="1", FAKE_CODEX_SLEEP="0", **venv):
        cli.do_new("P", roles="high:2")
        _stamp("P")
        try:
            res = cli.do_start("P")              # whole project
            assert {r["worker"] for r in res} == {"high", "high2"}
            assert _wait_until(lambda: all(not _st("P", w)["alive"] for w in ("high", "high2")))
            assert len(cli.do_status("P")) == 2
        finally:
            _kill_project("P")


def test_start_fails_closed_when_verify_down(tmp: Path):
    """No fake verify: start must record PRECHECK_FAILED and never launch the loop."""
    fc = _fake_codex(tmp)
    with _project_env(tmp, EULER_CODEX_BIN=str(fc), EULER_ROUND_BEAT="0",
                      EULER_VERIFY_URL="http://127.0.0.1:1/verify"):
        cli.do_new("P", roles="high:1")
        _stamp("P")
        res = cli.do_start("P/high")
        assert res[0]["result"] == "precheck-failed"
        assert res[0]["precheck"]["component"] == "verify"
        s = _st("P", "high")
        assert s["alive"] is False and s["state"] == "PRECHECK_FAILED"
        wl = L.WorkerLayout(L.worker_dir("P", "high"))
        assert not (wl.logs / "round_1.log").exists()
        assert cli._read_pid(wl) is None


def test_missing_codex_fails_backend_preflight(tmp: Path):
    with ours_verify(tmp) as (venv, _httpd), _project_env(
            tmp, EULER_CODEX_BIN="/nonexistent/codex-bin",
            EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS="0", **venv):
        cli.do_new("P", roles="high:1")
        _stamp("P")
        try:
            rows = cli.do_start("P/high")
            # Missing selected backends are rejected before a loop is spawned.
            assert rows[0]["result"] == "precheck-failed"
            assert _wait_until(lambda: not _st("P", "high")["alive"])
            s = _st("P", "high")
            assert s["state"] == "PRECHECK_FAILED"
            assert s["precheck"]["component"] == "worker_backend"
            assert "not executable" in s["precheck"]["reason"]
        finally:
            _kill_project("P")


# --- runner ---------------------------------------------------------------- #

def main() -> None:
    fs_tests = [test_assign_replace_and_rejects, test_status_before_start, test_list]
    loop_tests = [test_loop_runs_rounds_then_exits, test_graceful_stop, test_force_stop,
                  test_idempotent_start, test_project_wide_targets,
                  test_start_fails_closed_when_verify_down,
                  test_missing_codex_fails_backend_preflight]
    for t in fs_tests + loop_tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL ORCHESTRATION TESTS PASSED")


if __name__ == "__main__":
    main()
