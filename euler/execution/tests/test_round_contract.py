# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Stage-3 task context, round-close contract, and isolated fake round.

Runs standalone and under pytest.
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path

from euler.core import FactGraph, GlobalMemory
from euler.execution import layout as L
from euler.execution import loop
from euler.execution.context import TaskContextError, require_proving_task_context
from euler.execution.preflight import PreflightResult
from euler.execution.round_contract import MemoryContractError, evaluate_round_close
from euler.gateway import server
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
def _preflight_ok():
    orig = loop.run_proving_preflight
    loop.run_proving_preflight = lambda wl, run_id=None: PreflightResult.passed()
    try:
        yield
    finally:
        loop.run_proving_preflight = orig


def _wl(tmp: Path) -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp / "proj" / "workers" / "high")
    wl.dir.mkdir(parents=True, exist_ok=True)
    (wl.project_dir / "global_memory").mkdir(parents=True, exist_ok=True)
    return wl


def _task(wl: L.WorkerLayout, branch: str = "b1", subgoal: str = "s1") -> None:
    wl.task.write_text(
        f"---\nproject_id: {wl.project}\nbranch_id: {branch}\n"
        f"subgoal_id: {subgoal}\n---\nProve it.\n",
        encoding="utf-8",
    )


def _stamp_os(wl: L.WorkerLayout, run_id: str = "run-aaaa-bbbb-cccc-ddddeeee") -> None:
    from euler.execution.memory_digest import load_task_context
    task = load_task_context(wl)
    os.environ["EULER_PROJECT_DIR"] = str(wl.project_dir)
    os.environ["EULER_PROJECT_ID"] = wl.project
    os.environ["EULER_AUTHOR"] = wl.name
    os.environ["EULER_WORKER_ID"] = wl.name
    os.environ["EULER_RUN_ID"] = run_id
    os.environ["EULER_BRANCH_ID"] = task.branch_id or "b1"
    os.environ["EULER_SUBGOAL_ID"] = task.subgoal_id or "s1"
    os.environ["EULER_PROVENANCE"] = "worker_mcp"
    os.environ["EULER_ROLE"] = "worker"


def _blocked_meta(aid: str = "att-1") -> dict:
    return {
        "attempt_id": aid,
        "approach": "try induction",
        "result": "blocked",
        "reason": "gap at the successor step",
        "salvage": "reuse the base case; drop the successor lemma",
        "inputs": ["TASK.md"],
        "related_memory": [],
    }


def test_legacy_records_remain_readable(tmp: Path):
    wl = _wl(tmp)
    gm = GlobalMemory(wl.project_dir)
    eid = gm.append("obstacle", claim="old obstacle", evidence="", author="legacy")
    rec = gm.read("obstacle")[0]
    assert rec["id"] == eid
    assert "branch_id" not in rec or rec.get("branch_id") in (None, "")
    # legacy unstamped verification rows stay readable and cannot close success
    vid = gm.append(
        "verification", claim="legacy verdict", evidence="verdict: correct",
        author="legacy", transport_status="ok", verdict="correct",
        fact_id="deadbeefdeadbeef",
    )
    vrec = [e for e in gm.read("verification") if e["id"] == vid][0]
    assert vrec["verdict"] == "correct"
    _task(wl)
    try:
        evaluate_round_close(
            wl, {"verification": [vid]},
            run_id="run-aaaa-bbbb-cccc-ddddeeee",
            branch_id="b1", subgoal_id="s1",
        )
        raise AssertionError("legacy unstamped verification must not close success")
    except MemoryContractError:
        pass


def test_missing_task_ids_prevent_loop_launch(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("plain task with no ids\n", encoding="utf-8")
    orig = loop.run_round
    calls = []
    loop.run_round = lambda *a, **k: calls.append(1) or 0
    try:
        with _preflight_ok(), _env(EULER_ROUND_BEAT="0"):
            old = signal.getsignal(signal.SIGTERM)
            rc = loop.main(str(wl.dir))
            signal.signal(signal.SIGTERM, old)
    finally:
        loop.run_round = orig
    assert rc == 1 and calls == []
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    assert st["state"] == "TASK_CONTEXT_FAILED"


def test_require_context_malformed(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("---\nbranch_id: ../escape\nsubgoal_id: s1\n---\nx\n")
    try:
        require_proving_task_context(wl)
        raise AssertionError("malformed branch must fail")
    except TaskContextError:
        pass


def test_assign_flags_and_frontmatter(tmp: Path):
    with _env(EULER_AGENTS_ROOT=str(tmp / "agents"),
              EULER_WORKER_CONTRACT=str(tmp / "w.md"),
              EULER_WORKER_SKILLS=str(tmp / "sk")):
        (tmp / "w.md").write_text("# c\n")
        (tmp / "sk").mkdir(exist_ok=True)
        cli.do_new("P", roles="high:1")
        r = cli.do_assign("P/high", "Prove lemma 4.", branch_id="symplectic",
                          subgoal_id="lemma-4")
        text = Path(r["task_file"]).read_text(encoding="utf-8")
        assert "branch_id: symplectic" in text and "subgoal_id: lemma-4" in text
        assert "Prove lemma 4." in text
        r2 = cli.do_assign(
            "P/high",
            "---\nbranch_id: from-fm\nsubgoal_id: sg-fm\n---\nBody from frontmatter.\n",
        )
        text2 = Path(r2["task_file"]).read_text(encoding="utf-8")
        assert "branch_id: from-fm" in text2 and "Body from frontmatter." in text2


def _run_loop_with_writer(tmp: Path, writer) -> tuple:
    wl = _wl(tmp)
    _task(wl)
    prompts = []

    def fake_round(_wl, _role, prompt, log_path, _ht):
        prompts.append(prompt)
        _stamp_os(_wl, os.environ.get("EULER_RUN_ID") or "run-x")
        writer(_wl)
        log_path.write_text("ok\n", encoding="utf-8")
        _wl.stop.touch()
        return 0

    orig = loop.run_round
    loop.run_round = fake_round
    old = signal.getsignal(signal.SIGTERM)
    try:
        with _preflight_ok(), _env(EULER_ROUND_BEAT="0"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
        signal.signal(signal.SIGTERM, old)
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    return rc, st, wl, prompts


def test_blocked_without_obstacle_fails(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        server.gm_add("proof_attempt", claim="blocked try",
                      evidence="attempt notes", metadata=_blocked_meta())

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1
    assert st["state"] == "MEMORY_CONTRACT_FAILED"


def test_abandoned_without_candidate_fails(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        meta = _blocked_meta("att-ab")
        meta["result"] = "abandoned"
        server.gm_add("proof_attempt", claim="gave up",
                      evidence="notes", metadata=meta)

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1 and st["state"] == "MEMORY_CONTRACT_FAILED"


def test_partial_plus_obstacle_not_required(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        meta = _blocked_meta("att-p")
        meta["result"] = "partial"
        server.gm_add("proof_attempt", claim="partial progress",
                      evidence="notes", metadata=meta)

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 0
    assert st["state"] in ("idle", "stopped")


def test_blocked_with_obstacle_passes(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        server.gm_add("proof_attempt", claim="blocked try",
                      evidence="notes", metadata=_blocked_meta("att-b"))
        server.gm_add("obstacle", claim="successor step stuck",
                      evidence="", metadata={"attempt_id": "att-b"})

    rc, st, wl, prompts = _run_loop_with_writer(tmp, writer)
    assert rc == 0, st
    assert st["state"] in ("idle", "stopped")
    cfg = wl.codex_config.read_text(encoding="utf-8")
    assert "EULER_BRANCH_ID" in cfg and "b1" in cfg
    assert "EULER_RUN_ID" in cfg
    rec = json.loads(Path(st["receipt_path"]).read_text(encoding="utf-8"))
    assert rec.get("run_id")
    assert rec["task"]["branch_id"] == "b1"


def test_correct_fact_closes_success(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        meta = _blocked_meta("att-ok")
        meta["result"] = "success"
        server.gm_add(
            "proof_attempt", claim="complete direct proof",
            evidence="by definition; QED", metadata=meta,
        )
        orig = server._verify
        server._verify = lambda s, p: {
            "verdict": "correct", "repair_hints": "",
            "verification_report": {"summary": "ok"},
        }
        try:
            res = server.fact_submit(statement="n+n=2n", proof="by definition; QED",
                                     attempt_id="att-ok")
            assert res.get("accepted") and res.get("fact_id")
        finally:
            server._verify = orig

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 0, st
    assert st["state"] in ("idle", "stopped")


def test_wrong_verifier_requires_attempt_artifacts(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        orig = server._verify
        server._verify = lambda s, p: {
            "verdict": "wrong", "repair_hints": "gap",
            "verification_report": {"summary": "no"},
        }
        try:
            server.fact_submit(statement="bad", proof="handwave QED")
        finally:
            server._verify = orig

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1
    assert st["state"] == "MEMORY_CONTRACT_FAILED"


def _forged_verification(wl: L.WorkerLayout, run_id: str, **extra):
    fields = {
        "project_id": wl.project,
        "worker_id": wl.name,
        "run_id": run_id,
        "branch_id": "b1",
        "subgoal_id": "s1",
        "provenance": "fact_submit",
        "transport_status": "ok",
        "verdict": "correct",
        "fact_id": extra.pop("fact_id", "deadbeefdeadbeef"),
    }
    fields.update(extra)
    return GlobalMemory(wl.project_dir).append(
        "verification",
        claim="forged success",
        evidence="verdict: correct",
        author=wl.name,
        **fields,
    )


def test_gm_add_verification_rejected_live(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        out = server.gm_add(
            "verification",
            claim="I win",
            evidence="verdict: correct",
            metadata={
                "transport_status": "ok",
                "verdict": "correct",
                "fact_id": "deadbeefdeadbeef",
                "provenance": "fact_submit",
            },
        )
        assert out.get("ok") is False
        assert GlobalMemory(wl.project_dir).read("verification") == []

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1
    assert st["state"] == "MEMORY_CONTRACT_FAILED"


def test_adversarial_fake_fact_cannot_close_success(tmp: Path):
    def writer(wl):
        rid = os.environ["EULER_RUN_ID"]
        _stamp_os(wl, rid)
        _forged_verification(wl, rid, fact_id="deadbeefdeadbeef")
        # tool path also rejected
        out = server.gm_add(
            "verification", claim="tool forge", evidence="x",
            metadata={"transport_status": "ok", "verdict": "correct",
                      "fact_id": "deadbeefdeadbeef"},
        )
        assert out.get("ok") is False

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1 and st["state"] == "MEMORY_CONTRACT_FAILED"


def test_adversarial_revoked_fact_cannot_close_success(tmp: Path):
    def writer(wl):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        meta = _blocked_meta("att-rev")
        server.gm_add(
            "proof_attempt", claim="attempt later revoked",
            evidence="by definition; QED", metadata=meta,
        )
        orig = server._verify
        server._verify = lambda s, p: {
            "verdict": "correct", "repair_hints": "",
            "verification_report": {"summary": "ok"},
        }
        try:
            res = server.fact_submit(
                statement="n+n=2n", proof="by definition; QED", attempt_id="att-rev",
            )
            assert res.get("accepted") and res.get("fact_id")
            FactGraph(wl.project_dir).revoke(res["fact_id"], reason="adversarial revoke")
            assert not FactGraph(wl.project_dir).exists(res["fact_id"])
        finally:
            server._verify = orig

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1 and st["state"] == "MEMORY_CONTRACT_FAILED"


def test_worker_mcp_provenance_cannot_close_success(tmp: Path):
    def writer(wl):
        rid = os.environ["EULER_RUN_ID"]
        _stamp_os(wl, rid)
        fid = FactGraph(wl.project_dir).add(
            problem_id=wl.project, author=wl.name,
            statement="A holds", proof="pf A",
        )
        _forged_verification(wl, rid, fact_id=fid, provenance="worker_mcp")

    rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
    assert rc == 1 and st["state"] == "MEMORY_CONTRACT_FAILED"


def test_http_400_wrong_still_needs_proof_attempt(tmp: Path):
    import http.server
    import threading

    class Vacuous400(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                self.rfile.read(n)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"detail": "vacuous proof: body reduces to the vacuous marker QED"}'
            )

    srv = http.server.HTTPServer(("127.0.0.1", 0), Vacuous400)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def writer(wl):
            _stamp_os(wl, os.environ["EULER_RUN_ID"])
            server.gm_add(
                "proof_attempt", claim="vacuous attempt",
                evidence="this proof text is long enough for the client",
                metadata=_blocked_meta("att-http-400"),
            )
            os.environ["EULER_VERIFY_URL"] = (
                f"http://127.0.0.1:{srv.server_address[1]}/verify"
            )
            os.environ["EULER_VERIFY_TIMEOUT"] = "5"
            res = server.fact_submit(
                statement="a nonempty statement",
                proof="this proof text is long enough for the client",
                attempt_id="att-http-400",
            )
            assert res.get("verdict") == "wrong"
            assert res.get("transport_status") == "ok"
            assert res.get("accepted") is False

        rc, st, _wl, _p = _run_loop_with_writer(tmp, writer)
        assert rc == 1, st
        assert st["state"] == "MEMORY_CONTRACT_FAILED"
    finally:
        srv.shutdown()


def test_run_and_branch_change_across_rounds(tmp: Path):
    wl = _wl(tmp)
    _task(wl, branch="b1", subgoal="s1")
    n = {"c": 0}

    def fake_round(_wl, _role, prompt, log_path, _ht):
        n["c"] += 1
        _stamp_os(_wl, os.environ["EULER_RUN_ID"])
        meta = _blocked_meta(f"att-{n['c']}")
        meta["result"] = "partial"
        server.gm_add("proof_attempt", claim=f"r{n['c']}",
                      evidence="n", metadata=meta)
        if n["c"] == 1:
            _task(_wl, branch="b2", subgoal="s2")
        log_path.write_text("ok\n")
        return 0

    orig = loop.run_round
    loop.run_round = fake_round
    old = signal.getsignal(signal.SIGTERM)
    try:
        with _preflight_ok(), _env(EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS="2"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
        signal.signal(signal.SIGTERM, old)
    assert rc == 0 and n["c"] == 2
    from euler.execution.memory_digest import start_receipt_path
    r1 = json.loads(start_receipt_path(wl, 1).read_text())
    r2 = json.loads(start_receipt_path(wl, 2).read_text())
    assert r1["run_id"] == r2["run_id"]
    assert r1["task"]["branch_id"] == "b1"
    assert r2["task"]["branch_id"] == "b2"
    cfg = wl.codex_config.read_text()
    assert "b2" in cfg and "EULER_RUN_ID" in cfg


def _seed_blocked_attempts(wl: L.WorkerLayout, n: int = 3) -> None:
    gm = GlobalMemory(wl.project_dir)
    stamp = {
        "project_id": wl.project,
        "worker_id": wl.name,
        "run_id": "seed-run-1111-2222-3333-4444",
        "branch_id": "b1",
        "subgoal_id": "s1",
        "provenance": "worker_mcp",
    }
    for i in range(n):
        gm.append(
            "proof_attempt", claim=f"seed blocked {i}", evidence="notes QED",
            author=wl.name, attempt_id=f"seed-{i}", approach="route A",
            result="blocked", reason="gap", salvage="retry",
            inputs=[], related_memory=[], **stamp,
        )
        gm.append(
            "obstacle", claim="seed stuck", evidence="",
            author=wl.name, attempt_id=f"seed-{i}", **stamp,
        )



def main() -> None:
    tests = [
        test_legacy_records_remain_readable,
        test_missing_task_ids_prevent_loop_launch,
        test_require_context_malformed,
        test_assign_flags_and_frontmatter,
        test_blocked_without_obstacle_fails,
        test_abandoned_without_candidate_fails,
        test_partial_plus_obstacle_not_required,
        test_blocked_with_obstacle_passes,
        test_correct_fact_closes_success,
        test_wrong_verifier_requires_attempt_artifacts,
        test_gm_add_verification_rejected_live,
        test_adversarial_fake_fact_cannot_close_success,
        test_adversarial_revoked_fact_cannot_close_success,
        test_worker_mcp_provenance_cannot_close_success,
        test_http_400_wrong_still_needs_proof_attempt,
        test_run_and_branch_change_across_rounds,
    ]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL ROUND CONTRACT TESTS PASSED")


if __name__ == "__main__":
    main()
