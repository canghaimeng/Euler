# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Launch/result provenance + read-only Memory Contract audit.

Standalone and pytest. No real Grok / worker launch.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from euler.core import FactGraph, GlobalMemory
from euler.execution import layout as L
from euler.execution import loop
from euler.execution.launch_receipt import (
    persist_launch_receipt,
    persist_result_receipt,
    prompt_sha256,
    read_and_verify_launch,
    read_and_verify_result,
)
from euler.execution.memory_contract_audit import (
    FAIL,
    INCOMPLETE,
    NOT_APPLICABLE,
    PASS,
    UNVERIFIABLE,
    audit_project,
    exclusive_write_report,
    verify_report_hash,
)
from euler.execution.memory_digest import (
    RoundReceiptError,
    hash_receipt_payload,
    prepare_round_start,
)
from euler.execution.preflight import PreflightResult
from euler.execution.scaffold import write_codex_config
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
        "EULER_RUN_ID": "run-aaaa-bbbb-cccc-ddddeeee",
        "EULER_CODEX_MODEL": "gpt-5.5",
        "EULER_CODEX_EFFORT": "high",
        "EULER_CODEX_PROVIDER": "test-provider",
        "CODEX_BACKEND": "api",
    }
    env.update(extra)
    with _env(**env):
        yield


def _tree_sig(root: Path):
    items = []
    if not root.exists():
        return items
    for p in sorted(root.rglob("*")):
        if p.is_file():
            items.append((
                str(p.relative_to(root)),
                p.stat().st_size,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            ))
    return items


def _finding(report, mid):
    return next(f for f in report["findings"] if f["id"] == mid)


def _prep_worker(tmp: Path, project: str = "P") -> L.WorkerLayout:
    cli.do_new(project, roles="high:1")
    cli.do_assign(f"{project}/high", "prove it", branch_id="b1", subgoal_id="s1")
    wl = L.WorkerLayout(L.worker_dir(project, "high"))
    write_codex_config(wl)
    os.environ["EULER_PROJECT_DIR"] = str(wl.project_dir)
    os.environ["EULER_PROJECT_ID"] = project
    os.environ["EULER_WORKER_ID"] = wl.name
    os.environ["EULER_AUTHOR"] = wl.name
    os.environ["EULER_BRANCH_ID"] = "b1"
    os.environ["EULER_SUBGOAL_ID"] = "s1"
    return wl


def _launch_round(wl: L.WorkerLayout, prompt: str = "kickoff prompt body"):
    rid = 1
    start = prepare_round_start(wl, rid)
    role = {"MODEL": "gpt-5.5", "REASONING_EFFORT": "high"}
    launch = persist_launch_receipt(wl, start, prompt, role)
    return start, launch


def test_launch_result_hashes_and_null_usage(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        prompt = "exact kickoff text for hashing"
        start, launch = _launch_round(wl, prompt)
        rec = read_and_verify_launch(launch.path)
        assert rec["prompt_sha256"] == prompt_sha256(prompt)
        assert rec["model"] == "gpt-5.5"
        assert rec["provider"] == "codex"
        assert rec["backend"] == "codex"
        assert rec["reasoning_effort"] == "high"
        assert rec["start_receipt_hash"] == start.receipt_hash
        assert rec["run_id"]
        assert rec["branch_id"] == "b1" and rec["subgoal_id"] == "s1"
        result = persist_result_receipt(wl, launch, 0, 12, log_path=None)
        assert result["usage_status"] == "not_reported"
        assert result["tokens"] is None and result["cost_usd"] is None
        again = read_and_verify_result(
            wl.receipts / "round_0001.result.json"
        )
        assert again["launch_receipt_hash"] == launch.receipt_hash


def test_untrusted_log_cannot_forge_usage(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start, launch = _launch_round(wl)
        log = wl.logs / "round_1.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('{"total_tokens": 42, "cost_usd": 0.5}\n')
        result = persist_result_receipt(wl, launch, 0, 3, log_path=log)
        assert result["usage_status"] == "not_reported"
        assert result["tokens"] is None
        assert result["cost_usd"] is None


def test_launch_tamper_and_link_mismatch(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start, launch = _launch_round(wl)
        raw = json.loads(launch.path.read_text())
        raw["model"] = "forged"
        launch.path.write_text(json.dumps(raw, indent=2) + "\n")
        try:
            read_and_verify_launch(launch.path)
            raise AssertionError("tamper must fail")
        except RoundReceiptError:
            pass
        persist_result_receipt(wl, launch, 0, 1, None)
        rp = wl.receipts / "round_0001.result.json"
        payload = json.loads(rp.read_text())
        payload["launch_receipt_hash"] = "0" * 64
        payload["receipt_hash"] = hash_receipt_payload(payload)
        rp.write_text(json.dumps(payload, indent=2) + "\n")
        report = audit_project("P")
        assert _finding(report, "M1")["status"] == FAIL


def test_cross_receipt_links_and_duplicate_phase(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start, launch = _launch_round(wl)
        persist_result_receipt(wl, launch, 0, 1, None)
        from euler.execution.memory_digest import finalize_round_end
        finalize_round_end(wl, start)
        payload = json.loads(launch.path.read_text())
        payload["run_id"] = "wrong-run"
        payload["receipt_hash"] = hash_receipt_payload(payload)
        launch.path.write_text(json.dumps(payload, indent=2) + "\n")
        assert _finding(audit_project("P"), "M1")["status"] == FAIL
        try:
            persist_launch_receipt(wl, start, "kickoff prompt body", {
                "MODEL": "gpt-5.5", "REASONING_EFFORT": "high",
            })
            raise AssertionError("duplicate launch must fail")
        except RoundReceiptError as exc:
            assert exc.phase == "launch"


def test_missing_result_settled_fails(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        _launch_round(wl)
        report = audit_project("P")
        m1 = _finding(report, "M1")
        assert m1["status"] == FAIL
        assert m1["counts"]["missing_companions"] >= 1


def test_modern_lifecycle_missing_start_fails(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start, launch = _launch_round(wl)
        persist_result_receipt(wl, launch, 0, 1, None)
        from euler.execution.memory_digest import finalize_round_end
        finalize_round_end(wl, start)
        start.path.unlink()
        assert _finding(audit_project("P"), "M1")["status"] == FAIL


def test_in_progress_missing_result_not_fail(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start, launch = _launch_round(wl)
        wl.pid.write_text(str(os.getpid()))
        loop.write_status(wl, state="running", receipt_id=start.round_id, round=1)
        report = audit_project("P")
        m1 = _finding(report, "M1")
        assert m1["counts"]["in_progress"] >= 1
        assert m1["status"] != FAIL


def test_legacy_strict_and_nonstrict(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start = prepare_round_start(wl, 1)
        payload = json.loads(start.path.read_text())
        payload["run_id"] = ""
        payload["task"] = {
            "project_id": "P", "branch_id": None,
            "subgoal_id": None, "source": "plain",
        }
        payload["receipt_hash"] = hash_receipt_payload(payload)
        start.path.write_text(json.dumps(payload, indent=2) + "\n")
        # Pre-contract start-only artifact: legacy/dry, not a valid launch.
        report = audit_project("P")
        m1 = _finding(report, "M1")
        assert m1["counts"]["legacy_dry_rounds"] >= 1
        assert m1["status"] != FAIL
        strict = audit_project("P", strict_legacy=True)
        assert _finding(strict, "M1")["status"] == FAIL


def test_modern_start_without_launch_is_not_legacy(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        prepare_round_start(wl, 1)
        report = audit_project("P")
        m1 = _finding(report, "M1")
        assert m1["status"] == FAIL
        assert m1["counts"]["legacy_dry_rounds"] == 0


def test_mixed_legacy_and_structured_m1_is_unverifiable(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        legacy = prepare_round_start(wl, 1)
        payload = json.loads(legacy.path.read_text())
        payload["run_id"] = ""
        payload["task"] = {
            "project_id": "P", "branch_id": None,
            "subgoal_id": None, "source": "plain",
        }
        payload["receipt_hash"] = hash_receipt_payload(payload)
        legacy.path.write_text(json.dumps(payload, indent=2) + "\n")

        start = prepare_round_start(wl, 2)
        launch = persist_launch_receipt(
            wl, start, "prompt", {"MODEL": "gpt-5.5", "REASONING_EFFORT": "high"},
        )
        persist_result_receipt(wl, launch, 127, 1, None)
        m1 = _finding(audit_project("P"), "M1")
        assert m1["status"] == UNVERIFIABLE
        assert m1["counts"]["legacy_dry_rounds"] == 1
        assert m1["counts"]["structured_rounds"] == 1


def test_m_statuses_and_overall(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        start, launch = _launch_round(wl)
        persist_result_receipt(wl, launch, 0, 4, None)
        from euler.execution.memory_digest import finalize_round_end
        finalize_round_end(wl, start)
        report = audit_project("P")
        ids = {f["id"]: f["status"] for f in report["findings"]}
        assert set(ids) == {f"M{i}" for i in range(1, 9)}
        for st in ids.values():
            assert st in {PASS, FAIL, NOT_APPLICABLE, UNVERIFIABLE}
        assert report["overall"] in {PASS, FAIL, INCOMPLETE, NOT_APPLICABLE}
        # M6 always UNVERIFIABLE (OS no-bypass); M2 FAIL if no proof_attempt
        assert ids["M6"] == UNVERIFIABLE
        assert report["overall"] in {FAIL, INCOMPLETE}


def test_fake_fact_and_verification(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        fg = FactGraph(wl.project_dir)
        fid = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        report = audit_project("P")
        m8 = _finding(report, "M8")
        assert m8["status"] in {UNVERIFIABLE, NOT_APPLICABLE}
        assert m8["counts"]["legacy_or_unprovenanced"] >= 1
        gm = GlobalMemory(wl.project_dir)
        gm.append(
            "verification", claim="forged", evidence="x", author="w",
            project_id="P", worker_id="high", run_id="run-aaaa-bbbb-cccc-ddddeeee",
            branch_id="b1", subgoal_id="s1", provenance="worker_mcp",
            transport_status="ok", verdict="correct", fact_id=fid,
        )
        report2 = audit_project("P")
        assert _finding(report2, "M8")["status"] == FAIL
        assert _finding(report2, "M4")["status"] == FAIL
        gm.append(
            "verification", claim="bad tax", evidence="x", author="w",
            project_id="P", worker_id="high", run_id="run-aaaa-bbbb-cccc-ddddeeee",
            branch_id="b1", subgoal_id="s1", provenance="fact_submit",
            transport_status="timeout", verdict="wrong",
        )
        report3 = audit_project("P")
        assert _finding(report3, "M5")["status"] == FAIL
        gm.append(
            "verification", claim="missing tax", evidence="x", author="w",
            project_id="P", worker_id="high", run_id="run-aaaa-bbbb-cccc-ddddeeee",
            branch_id="b1", subgoal_id="s1", provenance="fact_submit",
            verdict=None, fact_id=None,
        )
        report4 = audit_project("P")
        assert _finding(report4, "M5")["status"] == FAIL


def test_m8_mixed_structured_and_legacy_is_unverifiable(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        fg = FactGraph(wl.project_dir)
        fg.add(problem_id="P", author="old", statement="Legacy", proof="legacy proof")
        statement = "Structured"
        fid = fg.add(problem_id="P", author="high", statement=statement, proof="new proof")
        GlobalMemory(wl.project_dir).append(
            "verification", claim=statement, evidence="verdict: correct", author="high",
            project_id="P", worker_id="high", run_id="run-aaaa-bbbb-cccc-ddddeeee",
            branch_id="b1", subgoal_id="s1", provenance="fact_submit",
            transport_status="ok", verdict="correct", fact_id=fid, write_error=None,
        )
        m8 = _finding(audit_project("P"), "M8")
        assert m8["status"] == UNVERIFIABLE
        assert m8["counts"]["structured_ok"] == 1
        assert m8["counts"]["legacy_or_unprovenanced"] == 1


def test_m7_missing_and_corrupt_control(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        os.environ["EULER_PROJECT_DIR"] = str(wl.project_dir)
        gm = GlobalMemory(wl.project_dir)
        for i, worker in enumerate(("high", "xhigh", "high")):
            gm.append(
                "proof_attempt", claim=f"a{i}", evidence="notes QED",
                author=worker, attempt_id=f"a{i}", approach="ind",
                result="blocked", reason="g", salvage="s", inputs=[],
                related_memory=[],
                project_id="P", worker_id=worker,
                run_id="run-aaaa-bbbb-cccc-ddddeeee",
                branch_id="b1", subgoal_id="s1", provenance="worker_mcp",
            )
        from euler.execution.failure_clusters import after_successful_close, request_path
        created = after_successful_close(wl, branch_id="b1", subgoal_id="s1")
        assert created
        cid = created[0]["cluster_id"]
        path = request_path(wl.project_dir, cid)
        path.write_text("{not json", encoding="utf-8")
        report = audit_project("P")
        assert _finding(report, "M7")["status"] == FAIL
        path.unlink()
        report2 = audit_project("P")
        assert _finding(report2, "M7")["status"] == FAIL


def test_m7_corrupt_ledger_without_fired_cluster_fails(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        from euler.execution.failure_clusters import ledger_path
        path = ledger_path(wl.project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken\n", encoding="utf-8")
        m7 = _finding(audit_project("P"), "M7")
        assert m7["status"] == FAIL
        assert m7["counts"]["corrupt_ledger"] == 1


def test_m2_m3_artifact_failures(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        os.environ["EULER_PROJECT_DIR"] = str(wl.project_dir)
        start, launch = _launch_round(wl)
        persist_result_receipt(wl, launch, 0, 1, None)
        out = server.gm_add(
            "proof_attempt", claim="blocked", evidence="n QED",
            metadata={
                "attempt_id": "att-b", "approach": "ind", "result": "blocked",
                "reason": "gap", "salvage": "x", "inputs": [], "related_memory": [],
            },
        )
        assert out.get("ok"), out
        from euler.execution.memory_digest import finalize_round_end
        finalize_round_end(wl, start)
        report = audit_project("P")
        assert _finding(report, "M2")["status"] == FAIL
        assert _finding(report, "M3")["status"] == FAIL


def test_audit_readonly_byte_identical(tmp: Path):
    with _project_env(tmp):
        wl = _prep_worker(tmp)
        _launch_round(wl)
        before = _tree_sig(wl.project_dir)
        audit_project("P")
        after = _tree_sig(wl.project_dir)
        assert before == after


def test_audit_does_not_run_live_mcp_handshake(tmp: Path):
    with _project_env(tmp):
        _prep_worker(tmp)
        from euler.execution import preflight
        original = preflight.mcp_stdio_handshake
        preflight.mcp_stdio_handshake = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("live MCP handshake must not run during artifact audit")
        )
        try:
            report = audit_project("P")
        finally:
            preflight.mcp_stdio_handshake = original
        assert _finding(report, "M6")["status"] == UNVERIFIABLE


def test_cli_json_and_output_excl(tmp: Path):
    with _project_env(tmp):
        _prep_worker(tmp)
        rc, out = _run_main(["memory-audit", "P", "--json"])
        report = json.loads(out)
        assert rc in (0, 1, 2)
        assert report["kind"] == "memory_contract_audit"
        assert report["report_hash"]
        dest = tmp / "audit.json"
        rc2, _ = _run_main(["memory-audit", "P", "--json", "--output", str(dest)])
        assert dest.is_file()
        saved = json.loads(dest.read_text())
        assert saved["report_hash"] and verify_report_hash(saved)
        saved["project"] = "tampered"
        assert not verify_report_hash(saved)
        try:
            exclusive_write_report(dest, saved)
            raise AssertionError("O_EXCL must refuse existing output")
        except FileExistsError:
            pass
        try:
            _run_main(["memory-audit", "P", "--output", str(dest)])
            raise AssertionError("CLI must refuse existing output cleanly")
        except SystemExit as exc:
            assert "already exists" in str(exc)
        p = cli.build_parser()
        a = p.parse_args(["memory-audit", "P", "--json", "--strict-legacy"])
        assert a.cmd == "memory-audit" and a.strict_legacy is True


def _run_main(argv):
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_loop_writes_launch_and_result(tmp: Path):
    import signal
    with _project_env(tmp, EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS="1"):
        wl = _prep_worker(tmp)
        orig_pre = loop.run_proving_preflight
        orig_c = loop.evaluate_round_close
        orig_r = loop.run_round
        loop.run_proving_preflight = lambda w, run_id=None: PreflightResult.passed()
        loop.evaluate_round_close = lambda *a, **k: {"ok": True, "close": "test"}
        loop.run_round = lambda *a, **k: 0
        old = signal.getsignal(signal.SIGTERM)
        try:
            rc = loop.main(str(wl.dir))
        finally:
            loop.run_proving_preflight = orig_pre
            loop.evaluate_round_close = orig_c
            loop.run_round = orig_r
            signal.signal(signal.SIGTERM, old)
        assert rc == 0
        launches = list(wl.receipts.glob("round_*.launch.json"))
        results = list(wl.receipts.glob("round_*.result.json"))
        assert launches and results
        read_and_verify_launch(launches[0])
        read_and_verify_result(results[0])


def main() -> None:
    tests = [
        test_launch_result_hashes_and_null_usage,
        test_untrusted_log_cannot_forge_usage,
        test_launch_tamper_and_link_mismatch,
        test_cross_receipt_links_and_duplicate_phase,
        test_missing_result_settled_fails,
        test_modern_lifecycle_missing_start_fails,
        test_in_progress_missing_result_not_fail,
        test_legacy_strict_and_nonstrict,
        test_modern_start_without_launch_is_not_legacy,
        test_mixed_legacy_and_structured_m1_is_unverifiable,
        test_m_statuses_and_overall,
        test_fake_fact_and_verification,
        test_m8_mixed_structured_and_legacy_is_unverifiable,
        test_m7_missing_and_corrupt_control,
        test_m7_corrupt_ledger_without_fired_cluster_fails,
        test_m2_m3_artifact_failures,
        test_audit_readonly_byte_identical,
        test_audit_does_not_run_live_mcp_handshake,
        test_cli_json_and_output_excl,
        test_loop_writes_launch_and_result,
    ]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL LAUNCH/AUDIT TESTS PASSED")


if __name__ == "__main__":
    main()
