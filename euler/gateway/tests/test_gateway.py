# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Tests for euler.gateway — role gating + tool wiring over euler.core.

The verify service is mocked (we replace ``server._verify``), so fact_submit is
exercised without a live verifier or codex. Config is read from the environment
at call time, so each test sets EULER_* around a temp project dir.

Runs standalone (``python -m euler.gateway.tests.test_gateway``) and under pytest.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from euler.core import FactGraph, GlobalMemory
from euler.gateway import build_app, tools_for
from euler.gateway import server


@contextmanager
def _env(**kv):
    """Temporarily set env vars (None deletes), restore after."""
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


@contextmanager
def _mock_verify(verdict, repair_hints="", raise_exc=None):
    """Replace server._verify with a stub; restore after."""
    orig = server._verify

    def fake(statement, proof):
        if raise_exc is not None:
            raise raise_exc
        return {"verdict": verdict, "repair_hints": repair_hints,
                "verification_report": {"summary": "mock"}}

    server._verify = fake
    try:
        yield
    finally:
        server._verify = orig


def _ctx(**more):
    env = dict(
        EULER_AUTHOR="worker_high", EULER_WORKER_ID="worker_high",
        EULER_PROJECT_ID="proj", EULER_RUN_ID="run-1111-2222-3333-44445555",
        EULER_BRANCH_ID="b1", EULER_SUBGOAL_ID="s1",
        EULER_PROVENANCE="worker_mcp", EULER_VERIFY_URL="http://mock",
        EULER_PROBLEM_ID="P", EULER_AGENTS_ROOT=None,
    )
    env.update(more)
    return env


def _legacy_ctx(**more):
    env = _ctx(
        EULER_PROJECT_ID=None,
        EULER_WORKER_ID=None,
        EULER_RUN_ID=None,
        EULER_BRANCH_ID=None,
        EULER_SUBGOAL_ID=None,
        EULER_PROVENANCE=None,
    )
    env.update(more)
    return env


def _add_attempt(attempt_id="att-1"):
    out = server.gm_add(
        "proof_attempt",
        claim=f"proof attempt {attempt_id}",
        evidence="full proof under review",
        metadata={
            "attempt_id": attempt_id,
            "approach": "direct proof",
            "result": "success",
            "reason": "self-contained proof obtained",
            "salvage": "reuse the complete proof",
            "inputs": [],
            "related_memory": [],
        },
    )
    assert out.get("ok") is True, out
    return out["id"]


def test_role_table():
    # main can never fabricate a fact
    assert "fact_submit" not in tools_for("main")
    assert "fact_revoke" in tools_for("main")
    # verifier is read-only: literature lookup ONLY
    assert tools_for("verifier") == ["search_arxiv_theorems"]
    # worker is the only role that can submit a fact
    assert "fact_submit" in tools_for("worker")
    # all three get the read view + literature grounding
    for r in ("worker", "main", "verifier"):
        assert "search_arxiv_theorems" in tools_for(r)
    # unknown / misconfigured role fails CLOSED to the read-only verifier set
    assert tools_for("nope") == tools_for("verifier")
    assert "fact_submit" not in tools_for("nope") and "gm_add" not in tools_for("nope")
    # build_app registers without error for every role
    for r in ("worker", "main", "verifier", "all"):
        assert build_app(r) is not None


def test_gm_and_fact_search_over_temp_project():
    with tempfile.TemporaryDirectory() as d, _env(
        EULER_PROJECT_DIR=d, EULER_AGENTS_ROOT=None, EULER_AUTHOR="tester"
    ):
        out = server.gm_add("plan", claim="reduce to q>=2", evidence="")
        assert out["kind"] == "plan" and out["id"]
        hits = server.gm_search("reduce")
        assert hits["results_by_kind"]["plan"]["count"] == 1
        # fact_search over an empty graph is well-formed
        assert server.fact_search("anything")["results"] == []


def test_fact_submit_accept_writes_fact_and_traces():
    with tempfile.TemporaryDirectory() as d, _env(**_legacy_ctx(EULER_PROJECT_DIR=d)), _mock_verify("correct"):
        res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED")
        assert res["accepted"] is True and res["fact_id"]
        # the fact really landed in the graph
        fg = FactGraph(Path(d))
        assert fg.exists(res["fact_id"])
        # a verification trace was always written to global memory
        gm = GlobalMemory(Path(d))
        traces = gm.read("verification")
        assert traces and traces[-1]["verdict"] == "correct"
        assert traces[-1]["fact_id"] == res["fact_id"]
        assert traces[-1]["provenance"] == "fact_submit"
        assert traces[-1]["transport_status"] == "ok"


def test_fact_submit_reject_writes_nothing_but_traces():
    with tempfile.TemporaryDirectory() as d, _env(**_legacy_ctx(EULER_PROJECT_DIR=d)), _mock_verify("wrong", repair_hints="gap in step 2"):
        res = server.fact_submit(statement="bad", proof="hand-wave")
        assert res["accepted"] is False and res["repair_hints"] == "gap in step 2"
        fg = FactGraph(Path(d))
        assert fg.list() == []  # nothing written
        gm = GlobalMemory(Path(d))
        rec = gm.read("verification")[-1]
        assert rec["verdict"] == "wrong"  # but traced
        assert rec["provenance"] == "fact_submit"


def test_fact_submit_verify_error_is_clean():
    with tempfile.TemporaryDirectory() as d, _env(**_legacy_ctx(EULER_PROJECT_DIR=d)), _mock_verify("correct", raise_exc=RuntimeError("service down")):
        res = server.fact_submit(statement="s", proof="p")
        assert res["accepted"] is False and res["verdict"] is None
        assert res["transport_status"] == "service_error"
        assert "service down" in res["error"]
        traces = GlobalMemory(Path(d)).read("verification")
        assert traces and traces[-1]["verdict"] is None
        assert traces[-1]["transport_status"] == "service_error"
        assert traces[-1]["provenance"] == "fact_submit"
        assert FactGraph(Path(d)).list() == []


def test_fact_submit_accept_but_write_failed_still_traces():
    # a revoked predecessor makes FactGraph.add raise; the verdict is still traced
    with tempfile.TemporaryDirectory() as d, _env(**_legacy_ctx(EULER_PROJECT_DIR=d)), _mock_verify("correct"):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        fg.revoke(base, reason="A was wrong")
        res = server.fact_submit(statement="B from A", proof="uses A", predecessors=[base])
        assert res["accepted"] is True and res["fact_id"] is None and res["write_error"]
        rec = GlobalMemory(Path(d)).read("verification")[-1]
        assert rec["verdict"] == "correct"
        assert rec["provenance"] == "fact_submit"


def test_fact_submit_glossary_check_never_blocks():
    # a raising undefined_symbols must not block submission (advisory heuristic)
    orig = FactGraph.undefined_symbols

    def boom(self, **kw):
        raise RuntimeError("glossary heuristic bug")

    FactGraph.undefined_symbols = boom
    try:
        with tempfile.TemporaryDirectory() as d, _env(**_legacy_ctx(EULER_PROJECT_DIR=d)), _mock_verify("correct"):
            res = server.fact_submit(statement="X thing", proof="because")
            assert res["accepted"] is True and res["undefined_symbols"] == []
    finally:
        FactGraph.undefined_symbols = orig


def test_fact_submit_nondict_verify_body_is_clean():
    # a valid-JSON but non-dict verify response must not crash the gate
    with tempfile.TemporaryDirectory() as d, _env(**_legacy_ctx(EULER_PROJECT_DIR=d)):
        orig = server._verify
        server._verify = lambda statement, proof: ["not", "a", "dict"]
        try:
            res = server.fact_submit(statement="s", proof="p")
            assert res["accepted"] is False and res["verdict"] is None
            assert res["transport_status"] == "protocol_error"
            assert "non-dict" in res["error"]
            assert FactGraph(Path(d)).list() == []  # nothing written
            traces = GlobalMemory(Path(d)).read("verification")
            assert traces[-1]["verdict"] is None
            assert traces[-1]["transport_status"] == "protocol_error"
            assert traces[-1]["provenance"] == "fact_submit"
        finally:
            server._verify = orig


def test_role_env_default_and_build_app():
    # build_app(None) reads EULER_ROLE (server._role) — exercises the env branch
    with _env(EULER_ROLE="worker"):
        assert server._role() == "worker"
        app = build_app()  # role=None -> defaults to _role() (env)
        assert app is not None
    with _env(EULER_ROLE=None):
        assert server._role() == "verifier"  # unset falls back read-only (fail-closed)


def test_project_by_name_without_agents_root_raises():
    # a project name is given but EULER_AGENTS_ROOT is unset -> RuntimeError
    with _env(EULER_AGENTS_ROOT=None, EULER_PROJECT_DIR="/tmp/whatever"):
        try:
            server._project("proj_a")
            assert False, "should require EULER_AGENTS_ROOT to resolve by name"
        except RuntimeError as e:
            assert "EULER_AGENTS_ROOT" in str(e)


def test_verify_http_roundtrip_and_errors():
    # exercise the REAL _verify (local HTTP, offline-safe on 127.0.0.1)
    import http.server
    import threading

    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            captured["body"] = self.rfile.read(n).decode("utf-8")
            captured["ctype"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"verdict": "correct", "verification_report": {"ok": true}}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/verify"
    try:
        # not set -> RuntimeError
        with _env(EULER_VERIFY_URL=None):
            try:
                server._verify("s", "p")
                assert False, "should raise when EULER_VERIFY_URL unset"
            except RuntimeError as e:
                assert "EULER_VERIFY_URL" in str(e)
        # a real POST round-trip; the body is the JSON we sent
        with _env(EULER_VERIFY_URL=url, EULER_VERIFY_TIMEOUT="5"):
            out = server._verify("S(n)=n^2", "induction")
            assert out["verdict"] == "correct"
        assert '"statement": "S(n)=n^2"' in captured["body"]
        assert captured["ctype"] == "application/json"
        # a garbage timeout falls back to the default (no crash)
        with _env(EULER_VERIFY_URL=url, EULER_VERIFY_TIMEOUT="not-an-int"):
            assert server._verify("s", "p")["verdict"] == "correct"
    finally:
        srv.shutdown()


def test_fact_revoke_cascades():
    with tempfile.TemporaryDirectory() as d, _env(
        EULER_PROJECT_DIR=d, EULER_AGENTS_ROOT=None, EULER_AUTHOR="main_agent",
    ):
        fg = FactGraph(Path(d))
        base = fg.add(problem_id="P", author="w", statement="A holds", proof="pf A")
        child = fg.add(problem_id="P", author="w", statement="B from A", proof="uses A",
                       predecessors=[base])
        out = server.fact_revoke(base, reason="A was wrong")
        assert set(out["revoked"]) == {base, child}
        assert not fg.exists(base) and not fg.exists(child)


def test_gm_add_stamp_cannot_be_overridden():
    with tempfile.TemporaryDirectory() as d, _env(EULER_PROJECT_DIR=d, **_ctx()):
        out = server.gm_add(
            "obstacle", claim="blocked", evidence="",
            metadata={"attempt_id": "a1", "project_id": "FORGED",
                      "worker_id": "nope", "run_id": "evil",
                      "branch_id": "other", "subgoal_id": "xx"},
        )
        assert out.get("ok") is True and out["id"]
        rec = GlobalMemory(Path(d)).read("obstacle")[-1]
        assert rec["project_id"] != "FORGED"
        assert rec["worker_id"] == "worker_high"
        assert rec["run_id"] == "run-1111-2222-3333-44445555"
        assert rec["branch_id"] == "b1" and rec["subgoal_id"] == "s1"
        assert rec["attempt_id"] == "a1"


def test_gm_add_trusted_bridge_provenance_cannot_be_forged():
    with tempfile.TemporaryDirectory() as d, _env(
        EULER_PROJECT_DIR=d, **_ctx(EULER_PROVENANCE="trusted_bridge"),
    ):
        out = server.gm_add(
            "proof_attempt",
            claim="externally supplied analysis",
            evidence="durable report path and verbatim source text",
            verifiable=False,
            metadata={
                "attempt_id": "trusted-bridge-62-deadbeef",
                "approach": "external analysis",
                "result": "partial",
                "reason": "informal expert analysis; not independently verified",
                "salvage": "helpers must check",
                "inputs": [],
                "related_memory": [],
                "provenance": "worker_mcp",
            },
        )
        assert out.get("ok") is True, out
        rec = GlobalMemory(Path(d)).read("proof_attempt")[-1]
        assert rec["provenance"] == "trusted_bridge"
        assert rec["result"] == "partial"
        assert rec["verifiable"] is False
        assert FactGraph(Path(d)).list() == []


def test_dead_end_candidate_cannot_confirm_without_evidence():
    with tempfile.TemporaryDirectory() as d, _env(EULER_PROJECT_DIR=d, **_ctx()):
        bad = server.gm_add(
            "dead_end", claim="this is confirmed?", evidence="",
            metadata={"attempt_id": "a1", "dead_end_status": "confirmed",
                      "evidence_class": "explicit_counterexample"},
        )
        assert bad.get("ok") is False
        ok = server.gm_add(
            "dead_end", claim="candidate only", evidence="",
            metadata={"attempt_id": "a1", "dead_end_status": "candidate"},
        )
        assert ok.get("ok") is True
        rec = GlobalMemory(Path(d)).read("dead_end")[-1]
        assert rec["dead_end_status"] == "candidate"
        fake = server.gm_add(
            "dead_end", claim="real confirmed", evidence="see refs",
            metadata={"attempt_id": "a1", "dead_end_status": "confirmed",
                      "evidence_class": "explicit_counterexample",
                      "evidence_refs": ["fact:abc"]},
        )
        assert fake.get("ok") is False
        assert "unknown" in fake.get("error", "")
        counterexample = server.gm_add(
            "counterexample", claim="route fails on x=0", evidence="direct calculation",
            metadata={"attempt_id": "a1", "target": "route-a"},
        )
        assert counterexample.get("ok") is True
        confirmed = server.gm_add(
            "dead_end", claim="real confirmed", evidence="see refs",
            metadata={"attempt_id": "a1", "dead_end_status": "confirmed",
                      "evidence_class": "explicit_counterexample",
                      "evidence_refs": [counterexample["id"]]},
        )
        assert confirmed.get("ok") is True
        rec2 = GlobalMemory(Path(d)).read("dead_end")[-1]
        assert rec2["dead_end_status"] == "confirmed"


def _json_handler(status: int, payload):
    import http.server

    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, (bytes, bytearray)) else payload

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n:
                self.rfile.read(n)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _serve(handler):
    import http.server
    import threading

    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_gm_add_rejects_verification_kind():
    with tempfile.TemporaryDirectory() as d, _env(**_ctx(EULER_PROJECT_DIR=d)):
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
        assert "verification" in (out.get("error") or "").lower()
        assert GlobalMemory(Path(d)).read("verification") == []


def test_fact_submit_legacy_without_round_context_remains_compatible():
    with tempfile.TemporaryDirectory() as d, _env(
        **_legacy_ctx(EULER_PROJECT_DIR=d)
    ), _mock_verify("correct"):
        res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED")
        assert res["accepted"] is True and res.get("fact_id")
        rec = GlobalMemory(Path(d)).read("verification")[-1]
        assert rec["verdict"] == "correct"
        assert rec["provenance"] == "fact_submit"
        assert "run_id" not in rec


def test_fact_submit_round_missing_attempt_fails_before_verify_or_write():
    called = {"n": 0}
    orig = server._verify

    def boom(statement, proof):
        called["n"] += 1
        raise AssertionError("verifier must not run without a full stamp")

    server._verify = boom
    try:
        with tempfile.TemporaryDirectory() as d, _env(**_ctx(EULER_PROJECT_DIR=d)):
            res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED")
            assert res["accepted"] is False
            assert res.get("attempt_error") is True
            assert "attempt_id is required" in res.get("error", "")
            assert res.get("verdict") is None
            assert called["n"] == 0
            assert GlobalMemory(Path(d)).read("verification") == []
            assert FactGraph(Path(d)).list() == []
    finally:
        server._verify = orig


def test_fact_submit_round_unknown_attempt_fails_before_verify_or_write():
    called = {"n": 0}
    orig = server._verify
    server._verify = lambda s, p: called.__setitem__("n", called["n"] + 1)
    try:
        with tempfile.TemporaryDirectory() as d, _env(**_ctx(EULER_PROJECT_DIR=d)):
            res = server.fact_submit(
                statement="S(n)=n^2", proof="induction; QED",
                attempt_id="att-unknown",
            )
            assert res["accepted"] is False and res.get("attempt_error") is True
            assert "unknown attempt_id" in res.get("error", "")
            assert called["n"] == 0
            assert GlobalMemory(Path(d)).read("verification") == []
            assert FactGraph(Path(d)).list() == []
    finally:
        server._verify = orig


def test_fact_submit_round_wrong_context_attempt_fails_closed():
    called = {"n": 0}
    orig = server._verify
    server._verify = lambda s, p: called.__setitem__("n", called["n"] + 1)
    try:
        with tempfile.TemporaryDirectory() as d, _env(**_ctx(EULER_PROJECT_DIR=d)):
            with _env(EULER_WORKER_ID="other-worker", EULER_AUTHOR="other-worker"):
                _add_attempt("att-cross-round")
            res = server.fact_submit(
                statement="S(n)=n^2", proof="induction; QED",
                attempt_id="att-cross-round",
            )
            assert res["accepted"] is False and res.get("attempt_error") is True
            assert "different worker round context" in res.get("error", "")
            assert "worker_id" in res.get("error", "")
            assert called["n"] == 0
            assert GlobalMemory(Path(d)).read("verification") == []
            assert FactGraph(Path(d)).list() == []
    finally:
        server._verify = orig


def test_fact_submit_round_matching_attempt_verifies_writes_and_persists_link():
    with tempfile.TemporaryDirectory() as d, _env(
        **_ctx(EULER_PROJECT_DIR=d)
    ), _mock_verify("correct"):
        proof_attempt_id = _add_attempt("att-match")
        res = server.fact_submit(
            statement="S(n)=n^2", proof="induction; QED",
            attempt_id="att-match",
        )
        assert res["accepted"] is True and res.get("fact_id")
        rec = GlobalMemory(Path(d)).read("verification")[-1]
        assert rec["verdict"] == "correct"
        assert rec["attempt_id"] == "att-match"
        assert rec["links"]["attempt_id"] == "att-match"
        assert rec["links"]["proof_attempt_id"] == proof_attempt_id
        for key in ("project_id", "worker_id", "run_id", "branch_id", "subgoal_id"):
            assert rec[key] == GlobalMemory(Path(d)).read("proof_attempt")[-1][key]


def test_fact_submit_round_accepted_attempt_is_idempotent():
    called = {"n": 0}
    orig = server._verify

    def verify(statement, proof):
        called["n"] += 1
        return {"verdict": "correct", "verification_report": {}}

    server._verify = verify
    try:
        with tempfile.TemporaryDirectory() as d, _env(
            **_ctx(EULER_PROJECT_DIR=d)
        ):
            _add_attempt("att-replay")
            first = server.fact_submit(
                statement="S(n)=n^2", proof="induction; QED",
                attempt_id="att-replay",
            )
            second = server.fact_submit(
                statement="S(n)=n^2", proof="a longer restatement; QED",
                attempt_id="att-replay",
            )
            assert first["accepted"] is True and first.get("fact_id")
            assert second == {
                "accepted": True,
                "fact_id": first["fact_id"],
                "transport_status": "not_called",
                "undefined_symbols": [],
                "idempotent_replay": True,
            }
            assert called["n"] == 1
            assert FactGraph(Path(d)).list() == [first["fact_id"]]
            assert len(GlobalMemory(Path(d)).read("verification")) == 1
    finally:
        server._verify = orig


def test_fact_submit_round_accepted_attempt_rejects_statement_change():
    called = {"n": 0}
    orig = server._verify

    def verify(statement, proof):
        called["n"] += 1
        return {"verdict": "correct", "verification_report": {}}

    server._verify = verify
    try:
        with tempfile.TemporaryDirectory() as d, _env(
            **_ctx(EULER_PROJECT_DIR=d)
        ):
            _add_attempt("att-conflict")
            first = server.fact_submit(
                statement="S(n)=n^2", proof="induction; QED",
                attempt_id="att-conflict",
            )
            second = server.fact_submit(
                statement="S(n)=n^3", proof="different theorem; QED",
                attempt_id="att-conflict",
            )
            assert first["accepted"] is True and first.get("fact_id")
            assert second["accepted"] is False
            assert second.get("attempt_error") is True
            assert "already accepted a different statement" in second["error"]
            assert called["n"] == 1
            assert FactGraph(Path(d)).list() == [first["fact_id"]]
            assert len(GlobalMemory(Path(d)).read("verification")) == 1
    finally:
        server._verify = orig


def test_fact_submit_malformed_stamp_is_context_error_no_write():
    called = {"n": 0}
    orig = server._verify
    server._verify = lambda s, p: called.__setitem__("n", called["n"] + 1) or {
        "verdict": "correct", "repair_hints": "", "verification_report": {}
    }
    try:
        with tempfile.TemporaryDirectory() as d, _env(**_ctx(
            EULER_PROJECT_DIR=d, EULER_BRANCH_ID="../escape",
        )):
            res = server.fact_submit(statement="S(n)=n^2", proof="induction; QED")
            assert res["accepted"] is False and res.get("context_error") is True
            assert called["n"] == 0
            assert GlobalMemory(Path(d)).read("verification") == []
            assert FactGraph(Path(d)).list() == []
    finally:
        server._verify = orig


def test_persist_verification_requires_full_stamp():
    with tempfile.TemporaryDirectory() as d, _env(
        EULER_PROJECT_DIR=d, EULER_AGENTS_ROOT=None, EULER_AUTHOR="w",
        EULER_PROJECT_ID=None, EULER_WORKER_ID=None, EULER_RUN_ID=None,
        EULER_BRANCH_ID=None, EULER_SUBGOAL_ID=None, EULER_PROVENANCE=None,
    ):
        gm = GlobalMemory(Path(d))
        try:
            server._persist_verification(
                gm, claim="x", evidence="y",
                extra={"transport_status": "ok", "verdict": "wrong"},
            )
            assert False, "incomplete stamp must not persist"
        except ValueError:
            pass
        assert gm.read("verification") == []


def test_fact_submit_infra_classes_persist_null_verdict_no_fact():
    import http.server

    class Hang(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_POST(self):
            import time
            time.sleep(2)

    hang = _serve(Hang)
    err500 = _serve(_json_handler(500, {"detail": "codex exec failed with exit code 7"}))
    try:
        with tempfile.TemporaryDirectory() as d:
            hang_env = _legacy_ctx(
                EULER_PROJECT_DIR=d,
                EULER_VERIFY_URL=f"http://127.0.0.1:{hang.server_address[1]}/verify",
                EULER_VERIFY_TIMEOUT="1",
            )
            with _env(**hang_env):
                res = server.fact_submit(statement="s", proof="p long enough")
                assert res["verdict"] is None and res["transport_status"] == "timeout"
                assert FactGraph(Path(d)).list() == []
                assert GlobalMemory(Path(d)).read("verification")[-1]["verdict"] is None
            http_env = _legacy_ctx(
                EULER_PROJECT_DIR=d,
                EULER_VERIFY_URL=f"http://127.0.0.1:{err500.server_address[1]}/verify",
                EULER_VERIFY_TIMEOUT="5",
            )
            with _env(**http_env):
                res = server.fact_submit(statement="s", proof="p long enough")
                assert res["verdict"] is None
                assert res["transport_status"] in ("service_error", "http_error")
                assert FactGraph(Path(d)).list() == []
            conn_env = _legacy_ctx(
                EULER_PROJECT_DIR=d,
                EULER_VERIFY_URL="http://127.0.0.1:1/verify",
                EULER_VERIFY_TIMEOUT="1",
            )
            with _env(**conn_env):
                res = server.fact_submit(statement="s", proof="p long enough")
                assert res["verdict"] is None
                assert res["transport_status"] in ("connection_error", "timeout")
                assert FactGraph(Path(d)).list() == []
            traces = GlobalMemory(Path(d)).read("verification")
            statuses = {t["transport_status"] for t in traces}
            assert "timeout" in statuses or "connection_error" in statuses
            assert "service_error" in statuses or "http_error" in statuses
            assert all(t.get("verdict") is None for t in traces)
            assert all(t.get("provenance") == "fact_submit" for t in traces)
    finally:
        hang.shutdown()
        err500.shutdown()


def test_fact_submit_http_400_precheck_is_wrong_loopback():
    vacuous = _serve(_json_handler(400, {"detail": "vacuous proof: too short after markdown noise"}))
    p1 = _serve(_json_handler(400, {"detail": "[P1 on proof]: Hard Prohibition P1: cites problem.md"}))
    try:
        with tempfile.TemporaryDirectory() as d:
            for srv, needle in ((vacuous, "vacuous"), (p1, "P1")):
                env = _legacy_ctx(
                    EULER_PROJECT_DIR=d,
                    EULER_VERIFY_URL=f"http://127.0.0.1:{srv.server_address[1]}/verify",
                    EULER_VERIFY_TIMEOUT="5",
                )
                with _env(**env):
                    res = server.fact_submit(
                        statement="a real statement here",
                        proof="this proof is long enough to not be local-vacuous",
                    )
                    assert res["accepted"] is False
                    assert res["verdict"] == "wrong"
                    assert res["transport_status"] == "ok"
                    assert needle.lower() in (res.get("repair_hints") or res.get("detail") or "").lower()
                    assert FactGraph(Path(d)).list() == []
                    rec = GlobalMemory(Path(d)).read("verification")[-1]
                    assert rec["verdict"] == "wrong"
                    assert rec["transport_status"] == "ok"
                    assert rec["provenance"] == "fact_submit"
                    assert rec.get("fact_id") in (None, "")
    finally:
        vacuous.shutdown()
        p1.shutdown()


def test_fact_submit_http_422_500_504_loopback_not_wrong():
    h422 = _serve(_json_handler(422, {
        "detail": [{"type": "missing", "loc": ["body", "proof"], "msg": "Field required"}],
    }))
    h500 = _serve(_json_handler(500, {"detail": "codex exec failed with exit code 7"}))
    h504 = _serve(_json_handler(504, {"detail": "codex exec timed out after 900s"}))
    # 500/504 bodies mentioning vacuous must still NOT be mathematical wrong
    h500_trap = _serve(_json_handler(500, {"detail": "vacuous proof: internal boom"}))
    h504_trap = _serve(_json_handler(504, {"detail": "vacuous timeout"}))
    servers = (h422, h500, h504, h500_trap, h504_trap)
    try:
        with tempfile.TemporaryDirectory() as d:
            cases = (
                (h422, "protocol_error"),
                (h500, "service_error"),
                (h504, "timeout"),
                (h500_trap, "service_error"),
                (h504_trap, "timeout"),
            )
            for srv, want in cases:
                env = _legacy_ctx(
                    EULER_PROJECT_DIR=d,
                    EULER_VERIFY_URL=f"http://127.0.0.1:{srv.server_address[1]}/verify",
                    EULER_VERIFY_TIMEOUT="5",
                )
                with _env(**env):
                    res = server.fact_submit(statement="s", proof="p long enough")
                    assert res["accepted"] is False
                    assert res["verdict"] is None, (want, res)
                    got = res["transport_status"]
                    if want == "service_error":
                        assert got in ("service_error", "http_error"), res
                    else:
                        assert got == want, res
                    assert FactGraph(Path(d)).list() == []
            traces = GlobalMemory(Path(d)).read("verification")
            assert len(traces) == 5
            assert all(t.get("verdict") is None for t in traces)
            assert all(t.get("provenance") == "fact_submit" for t in traces)
    finally:
        for srv in servers:
            srv.shutdown()


def test_search_arxiv_theorems_delegates(monkeypatch=None):
    # the tool is a thin wrapper over euler.integrations.search; stub it (offline)
    orig = server._arxiv_search
    server._arxiv_search = lambda query, num_results=10: {
        "query": query, "num_results": num_results, "results": [{"title": "T"}]}
    try:
        out = server.search_arxiv_theorems("Beatty sequence", num_results=3)
        assert out["query"] == "Beatty sequence" and out["num_results"] == 3
        assert out["results"] == [{"title": "T"}]
    finally:
        server._arxiv_search = orig


def test_project_resolution_by_name_and_validation():
    with tempfile.TemporaryDirectory() as root:
        (Path(root) / "proj_a").mkdir()
        with _env(EULER_AGENTS_ROOT=root, EULER_PROJECT_DIR=None, EULER_AUTHOR="main_agent"):
            # main addresses a project by name
            out = server.gm_add("master_guidance", claim="try route X", evidence="", project="proj_a")
            assert out["id"]
            assert GlobalMemory(Path(root) / "proj_a").read("master_guidance")
            # path-escape / bad names are rejected
            for bad in ("../evil", "a/b", "", "/abs"):
                try:
                    server.gm_search("x", project=bad)
                    assert False, f"should reject project name {bad!r}"
                except RuntimeError:
                    pass
            # unknown project rejected
            try:
                server.gm_search("x", project="missing")
                assert False, "should reject unknown project"
            except RuntimeError:
                pass


def test_main_module_builds_and_runs():
    # `python -m euler.gateway` builds an app from EULER_ROLE and calls .run();
    # stub FastMCP.run so no stdio server actually starts.
    import runpy
    from euler._mcp import FastMCP

    orig_run = FastMCP.run
    calls = {"n": 0}
    FastMCP.run = lambda self, *a, **k: calls.__setitem__("n", calls["n"] + 1)
    try:
        with _env(EULER_ROLE="verifier"):
            runpy.run_module("euler.gateway", run_name="__main__")
        assert calls["n"] == 1
    finally:
        FastMCP.run = orig_run


def main() -> None:
    test_role_table()
    print("  [ok] role table (main no fact_submit; verifier read-only; worker submits)")
    test_role_env_default_and_build_app()
    print("  [ok] build_app reads EULER_ROLE; _role default")
    test_project_by_name_without_agents_root_raises()
    print("  [ok] project-by-name without EULER_AGENTS_ROOT -> RuntimeError")
    test_verify_http_roundtrip_and_errors()
    print("  [ok] _verify HTTP round-trip + unset-URL + bad-timeout fallback")
    test_fact_revoke_cascades()
    print("  [ok] fact_revoke cascades to descendants")
    test_search_arxiv_theorems_delegates()
    print("  [ok] search_arxiv_theorems delegates to integrations.search")
    test_main_module_builds_and_runs()
    print("  [ok] python -m euler.gateway builds app + calls run()")
    test_gm_and_fact_search_over_temp_project()
    print("  [ok] gm_add / gm_search / fact_search over a temp project")
    test_fact_submit_accept_writes_fact_and_traces()
    print("  [ok] fact_submit accept -> writes fact + verification trace")
    test_fact_submit_reject_writes_nothing_but_traces()
    print("  [ok] fact_submit reject -> writes nothing, still traces")
    test_fact_submit_verify_error_is_clean()
    print("  [ok] fact_submit verify-error -> clean error, no verdict")
    test_fact_submit_accept_but_write_failed_still_traces()
    print("  [ok] fact_submit accept-but-write-failed -> fact_id None + write_error, still traces correct")
    test_fact_submit_glossary_check_never_blocks()
    print("  [ok] fact_submit glossary heuristic never blocks submission")
    test_fact_submit_nondict_verify_body_is_clean()
    print("  [ok] fact_submit non-dict verify body -> clean error, nothing written")
    test_gm_add_rejects_verification_kind()
    print("  [ok] gm_add rejects kind=verification")
    test_fact_submit_legacy_without_round_context_remains_compatible()
    print("  [ok] fact_submit legacy no-round context remains compatible")
    test_fact_submit_round_missing_attempt_fails_before_verify_or_write()
    print("  [ok] fact_submit worker round missing attempt -> fail closed")
    test_fact_submit_round_unknown_attempt_fails_before_verify_or_write()
    print("  [ok] fact_submit worker round unknown attempt -> fail closed")
    test_fact_submit_round_wrong_context_attempt_fails_closed()
    print("  [ok] fact_submit worker round wrong-context attempt -> fail closed")
    test_fact_submit_round_matching_attempt_verifies_writes_and_persists_link()
    print("  [ok] fact_submit worker round matching attempt -> verified linked fact")
    test_fact_submit_malformed_stamp_is_context_error_no_write()
    print("  [ok] fact_submit malformed stamp -> context_error")
    test_persist_verification_requires_full_stamp()
    print("  [ok] _persist_verification refuses incomplete stamp")
    test_gm_add_stamp_cannot_be_overridden()
    print("  [ok] gm_add stamp cannot be overridden")
    test_gm_add_trusted_bridge_provenance_cannot_be_forged()
    print("  [ok] gm_add trusted bridge provenance cannot be forged")
    test_dead_end_candidate_cannot_confirm_without_evidence()
    print("  [ok] dead_end confirmed requires evidence")
    test_fact_submit_infra_classes_persist_null_verdict_no_fact()
    print("  [ok] infra timeout/500/connection persist null verdict")
    test_fact_submit_http_400_precheck_is_wrong_loopback()
    print("  [ok] HTTP 400 precheck loopback -> verdict=wrong transport=ok")
    test_fact_submit_http_422_500_504_loopback_not_wrong()
    print("  [ok] HTTP 422/500/504 loopback -> verdict=null, never wrong")
    test_project_resolution_by_name_and_validation()
    print("  [ok] project resolution by name + path-escape validation")
    print("ALL GATEWAY TESTS PASSED")


if __name__ == "__main__":
    main()
