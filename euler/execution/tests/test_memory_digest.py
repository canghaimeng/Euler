# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Stage-2 memory retrieval, bounded digest, receipts, watermarks.

Runs standalone (``python -m euler.execution.tests.test_memory_digest``) and pytest.
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from euler.core import GlobalMemory
from euler.execution import layout as L
from euler.execution import loop
from euler.execution import memory_digest as MD
from euler.execution.preflight import PreflightResult


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
def _restore_sigterm():
    old = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


@contextmanager
def _preflight_ok():
    orig = loop.run_proving_preflight
    loop.run_proving_preflight = lambda wl, run_id=None: PreflightResult.passed()
    orig_c = loop.evaluate_round_close
    loop.evaluate_round_close = lambda *a, **k: {"ok": True, "close": "test"}
    try:
        yield
    finally:
        loop.run_proving_preflight = orig
        loop.evaluate_round_close = orig_c


def _wl(tmp: Path, name: str = "high") -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp / "proj" / "workers" / name)
    wl.dir.mkdir(parents=True, exist_ok=True)
    (wl.project_dir / "global_memory").mkdir(parents=True, exist_ok=True)
    if not wl.task.exists():
        wl.task.write_text(
            "---\nbranch_id: b1\nsubgoal_id: s1\n---\nProve it.\n",
            encoding="utf-8",
        )
    return wl


def _gm(wl: L.WorkerLayout) -> GlobalMemory:
    return GlobalMemory(wl.project_dir)


def _append(wl: L.WorkerLayout, kind: str, claim: str, *, branch: str = None,
            subgoal: str = None, author: str = "w") -> str:
    links = {}
    if branch:
        links["branch_id"] = branch
    if subgoal:
        links["subgoal_id"] = subgoal
    evidence = "construction QED" if kind in ("counterexample", "proof_attempt") else ""
    return _gm(wl).append(kind, claim=claim, evidence=evidence, author=author, links=links)


# --------------------------------------------------------------------------- #
# TASK.md metadata                                                             #
# --------------------------------------------------------------------------- #

def test_parse_plain_task_md():
    ctx = MD.parse_task_text("# Task\n\nProve lemma 4.\n")
    assert ctx.source == "plain"
    assert ctx.branch_id is None and ctx.subgoal_id is None
    assert "Prove lemma 4" in ctx.body


def test_parse_frontmatter_ids_without_breaking_body():
    text = (
        "---\n"
        "project_id: P\n"
        "branch_id: symplectic\n"
        "subgoal_id: lemma-4\n"
        "---\n"
        "Prove the rank drop.\n"
    )
    ctx = MD.parse_task_text(text)
    assert ctx.source == "frontmatter"
    assert ctx.project_id == "P"
    assert ctx.branch_id == "symplectic"
    assert ctx.subgoal_id == "lemma-4"
    assert ctx.body == "Prove the rank drop."


def test_load_missing_task_defaults_project(tmp: Path):
    wl = _wl(tmp)
    wl.task.unlink(missing_ok=True)
    ctx = MD.load_task_context(wl)
    assert ctx.project_id == "proj"
    assert ctx.body == ""


# --------------------------------------------------------------------------- #
# retrieval                                                                    #
# --------------------------------------------------------------------------- #

def test_empty_memory_retrieve(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("search for widgets\n", encoding="utf-8")
    task = MD.load_task_context(wl)
    ret = MD.retrieve_bounded(wl.project_dir, task)
    for kind in MD.RETRIEVAL_KINDS:
        assert ret["hits_by_kind"][kind] == []


def test_exact_subgoal_prioritized_over_bm25(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text(
        "---\nbranch_id: b1\nsubgoal_id: sg1\n---\n"
        "rank drop symplectic widgets\n",
        encoding="utf-8",
    )
    exact = _append(wl, "obstacle", "exact subgoal obstacle", branch="other", subgoal="sg1")
    bm = _append(wl, "obstacle", "rank drop symplectic widgets noise", branch="b1", subgoal="other")
    task = MD.load_task_context(wl)
    ret = MD.retrieve_bounded(wl.project_dir, task)
    hits = ret["hits_by_kind"]["obstacle"]
    assert hits[0]["id"] == exact
    assert hits[0]["provenance"] == "exact_subgoal"
    ids = [h["id"] for h in hits]
    assert bm in ids
    assert ids.index(exact) < ids.index(bm)


def test_bm25_fills_remaining_slots(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("unique-token-xyz lemma\n", encoding="utf-8")
    _append(wl, "dead_end", "unique-token-xyz failed on the first try")
    _append(wl, "dead_end", "unique-token-xyz also collapsed later")
    task = MD.load_task_context(wl)
    ret = MD.retrieve_bounded(wl.project_dir, task)
    hits = ret["hits_by_kind"]["dead_end"]
    assert len(hits) == 2
    assert all(h["provenance"] == "bm25" for h in hits)
    assert all(h["score"] and h["score"] > 0 for h in hits)


def test_top_five_per_kind_and_total_cap(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("alpha-token shared claim\n", encoding="utf-8")
    for i in range(7):
        _append(wl, "verification", f"alpha-token verification number {i}")
    task = MD.load_task_context(wl)
    ret = MD.retrieve_bounded(wl.project_dir, task, per_kind=5, cap=100)
    assert len(ret["hits_by_kind"]["verification"]) == 5

    # total cap binds across kinds: 5 kinds * 2 hits = 10, cap 6
    for kind in MD.RETRIEVAL_KINDS:
        for i in range(3):
            _append(wl, kind, f"alpha-token {kind} extra {i}")
    ret2 = MD.retrieve_bounded(wl.project_dir, task, per_kind=2, cap=6)
    total = sum(len(v) for v in ret2["hits_by_kind"].values())
    assert total == 6
    assert total < 5 * 2


def test_exact_hits_survive_total_cap(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text(
        "---\nsubgoal_id: keep-me\n---\nalpha-token filler\n",
        encoding="utf-8",
    )
    exact = _append(wl, "proof_attempt", "keep this exact", subgoal="keep-me")
    for kind in MD.RETRIEVAL_KINDS:
        for i in range(4):
            _append(wl, kind, f"alpha-token filler {kind} {i}")
    task = MD.load_task_context(wl)
    ret = MD.retrieve_bounded(wl.project_dir, task, per_kind=4, cap=5)
    ids = [h["id"] for hs in ret["hits_by_kind"].values() for h in hs]
    assert exact in ids
    assert sum(len(v) for v in ret["hits_by_kind"].values()) == 5


def test_digest_has_no_full_history(tmp: Path):
    wl = _wl(tmp)
    secret = "SECRET_EVIDENCE_STRING_SHOULD_NOT_LEAK"
    _gm(wl).append(
        "counterexample",
        claim="a short claim about widgets",
        evidence=secret,
        author="w",
    )
    wl.task.write_text("widgets\n", encoding="utf-8")
    task = MD.load_task_context(wl)
    ret = MD.retrieve_bounded(wl.project_dir, task)
    digest = MD.build_digest(
        project_id="proj", worker_id="high", round_id=1, task=task, retrieval=ret,
    )
    blob = json.dumps(digest)
    assert secret not in blob
    assert "evidence" not in blob
    prompt = MD.render_digest_prompt(digest, "/tmp/r.json", "abc")
    assert secret not in prompt
    assert "ORCHESTRATOR MEMORY DIGEST" in prompt


# --------------------------------------------------------------------------- #
# receipts / hash                                                              #
# --------------------------------------------------------------------------- #

def test_canonical_hash_deterministic_and_tamper_rejected(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("widgets\n", encoding="utf-8")
    _append(wl, "obstacle", "widgets block the route")
    a = MD.prepare_round_start(wl, 1)
    b = MD.hash_digest(a.digest)
    assert a.digest_hash == b
    # same digest hashes the same
    assert MD.hash_digest(json.loads(json.dumps(a.digest))) == a.digest_hash
    payload = json.loads(a.path.read_text(encoding="utf-8"))
    payload["digest"]["kinds"]["obstacle"][0]["claim_snippet"] = "tampered"
    try:
        MD.verify_start_payload(payload)
        raise AssertionError("tamper must be rejected")
    except MD.RoundReceiptError as e:
        assert e.phase == "start"
        assert "digest_hash" in e.reason


def test_prepare_round_start_empty_and_reread(tmp: Path):
    wl = _wl(tmp)
    start = MD.prepare_round_start(wl, 3)
    assert start.path.is_file()
    assert start.round_id == 3
    on_disk = MD.read_and_verify_start(start.path)
    assert on_disk["digest_hash"] == start.digest_hash
    assert on_disk["round_id"] == 3
    wm = on_disk["memory_watermark_start"]["kinds"]["obstacle"]
    assert wm["count"] == 0 and wm["last_id"] is None
    assert wm["byte_size"] == 0
    assert isinstance(wm["file_sha256"], str) and len(wm["file_sha256"]) == 64
    assert wm["prefix_sha256"] == wm["file_sha256"]
    assert on_disk["receipt_hash"] == start.receipt_hash
    assert on_disk["schema_version"] == MD.SCHEMA_VERSION


def test_concurrent_append_changes_end_watermark(tmp: Path):
    wl = _wl(tmp)
    start = MD.prepare_round_start(wl, 1)
    start_count = start.payload["memory_watermark_start"]["kinds"]["obstacle"]["count"]
    new_id = _append(wl, "obstacle", "sibling append during the round")
    end = MD.finalize_round_end(wl, start)
    end_payload = json.loads(end.read_text(encoding="utf-8"))
    assert end_payload["start_digest_hash"] == start.digest_hash
    assert new_id in end_payload["appended_ids"]["obstacle"]
    assert end_payload["memory_watermark_end"]["kinds"]["obstacle"]["count"] == start_count + 1
    # start receipt not rewritten
    again = json.loads(start.path.read_text(encoding="utf-8"))
    assert again["digest_hash"] == start.digest_hash
    assert "memory_watermark_end" not in again


def test_receipt_write_failure_prevents_run_round(tmp: Path):
    wl = _wl(tmp)
    calls = []

    def boom(*a, **k):
        raise OSError("disk full")

    orig = MD.exclusive_write_json
    orig_rr = loop.run_round
    orig_pre = loop.prepare_round_start
    loop.run_round = lambda *a, **k: calls.append(1) or 0
    MD.exclusive_write_json = boom
    old_sig = signal.getsignal(signal.SIGTERM)
    try:
        with _restore_sigterm(), _preflight_ok(), _env(EULER_ROUND_BEAT="0"):
            rc = loop.main(str(wl.dir))
    finally:
        MD.exclusive_write_json = orig
        loop.run_round = orig_rr
        loop.prepare_round_start = orig_pre
        signal.signal(signal.SIGTERM, old_sig)
    assert rc == 1
    assert calls == []
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    assert st["state"] == "RECEIPT_FAILED"
    assert st["receipt"]["phase"] == "start"


def test_receipt_read_verify_failure_prevents_run_round(tmp: Path):
    wl = _wl(tmp)
    calls = []

    def fake_prepare(w, rnd, **k):
        path = MD.start_receipt_path(w, rnd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        MD.read_and_verify_start(path)

    orig_pre = loop.prepare_round_start
    orig_rr = loop.run_round
    loop.prepare_round_start = fake_prepare
    loop.run_round = lambda *a, **k: calls.append(1) or 0
    try:
        with _restore_sigterm(), _preflight_ok(), _env(EULER_ROUND_BEAT="0"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.prepare_round_start = orig_pre
        loop.run_round = orig_rr
    assert rc == 1 and calls == []
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    assert st["state"] == "RECEIPT_FAILED"


def test_prompt_contains_digest_path_hash(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text(
        "---\nbranch_id: b1\nsubgoal_id: s1\n---\nwidgets\n",
        encoding="utf-8",
    )
    _append(wl, "obstacle", "widgets block the symplectic route")
    prompts = []

    def fake_round(_wl, _role, prompt, log_path, _ht):
        prompts.append(prompt)
        log_path.write_text("ok\n", encoding="utf-8")
        _wl.stop.touch()
        return 0

    orig = loop.run_round
    loop.run_round = fake_round
    try:
        with _restore_sigterm(), _preflight_ok(), _env(EULER_ROUND_BEAT="0"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
    assert rc == 0 and prompts
    p = prompts[0]
    start = MD.start_receipt_path(wl, 1)
    assert str(start) in p
    payload = json.loads(start.read_text(encoding="utf-8"))
    assert payload["digest_hash"] in p
    assert payload["receipt_hash"] in p
    assert "ORCHESTRATOR MEMORY DIGEST" in p
    assert "widgets block" in p


def test_continuation_rounds_get_distinct_receipts(tmp: Path):
    wl = _wl(tmp)
    n = {"c": 0}

    def fake_round(_wl, _role, prompt, log_path, _ht):
        n["c"] += 1
        log_path.write_text(f"round {n['c']}\n", encoding="utf-8")
        return 0

    orig = loop.run_round
    loop.run_round = fake_round
    try:
        with _restore_sigterm(), _preflight_ok(), _env(
                EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS="2"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
    assert rc == 0 and n["c"] == 2
    p1 = MD.start_receipt_path(wl, 1)
    p2 = MD.start_receipt_path(wl, 2)
    assert p1.is_file() and p2.is_file()
    assert p1 != p2
    r1 = json.loads(p1.read_text(encoding="utf-8"))
    r2 = json.loads(p2.read_text(encoding="utf-8"))
    assert r1["round_id"] == 1 and r2["round_id"] == 2
    assert MD.end_receipt_path(wl, 1).is_file()
    assert MD.end_receipt_path(wl, 2).is_file()


def test_log_name_uses_global_receipt_id_after_restart(tmp: Path):
    """A restarted loop must not overwrite a process-local round_1.log."""
    wl = _wl(tmp)
    MD.prepare_round_start(wl, 5)
    wl.logs.mkdir(parents=True, exist_ok=True)
    old_log = wl.logs / "round_1.log"
    old_log.write_text("old process log\n", encoding="utf-8")
    seen = []

    def fake_round(_wl, _role, _prompt, log_path, _ht):
        seen.append(log_path.name)
        log_path.write_text("new process log\n", encoding="utf-8")
        _wl.stop.touch()
        return 0

    orig = loop.run_round
    loop.run_round = fake_round
    try:
        with _restore_sigterm(), _preflight_ok(), _env(EULER_ROUND_BEAT="0"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
    assert rc == 0
    assert seen == ["round_6.log"]
    assert old_log.read_text(encoding="utf-8") == "old process log\n"
    assert (wl.logs / "round_6.log").read_text(encoding="utf-8") == "new process log\n"


def test_end_finalize_failure_does_not_launch_next_round(tmp: Path):
    wl = _wl(tmp)
    rounds = []

    def fake_round(_wl, _role, prompt, log_path, _ht):
        rounds.append(1)
        log_path.write_text("ok\n", encoding="utf-8")
        return 0

    def boom(*a, **k):
        raise MD.RoundReceiptError("end", "cannot fsync")

    orig_rr = loop.run_round
    orig_end = loop.finalize_round_end
    loop.run_round = fake_round
    loop.finalize_round_end = boom
    try:
        with _restore_sigterm(), _preflight_ok(), _env(
                EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS="3"):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig_rr
        loop.finalize_round_end = orig_end
    assert rc == 1
    assert rounds == [1]
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    assert st["state"] == "RECEIPT_FAILED"
    assert st["receipt"]["phase"] == "end"


def test_kickoff_injects_digest_block():
    text = loop.kickoff("P", "high", receipt_path="/r/start.json",
                        digest_hash="deadbeef", receipt_hash="cafebabe",
                        digest_text="ORCHESTRATOR MEMORY DIGEST\nhits: none")
    assert "/r/start.json" in text and "deadbeef" in text and "cafebabe" in text
    assert "ORCHESTRATOR MEMORY DIGEST" in text


def test_allocator_monotonic_after_existing_no_overwrite(tmp: Path):
    wl = _wl(tmp)
    first = MD.prepare_round_start(wl, 5)
    assert first.path.name == "round_0005.start.json"
    nxt = MD.allocate_receipt_id(wl)
    assert nxt >= 6
    try:
        MD.prepare_round_start(wl, 5)
        raise AssertionError("pre-existing start receipt must not be overwritten")
    except MD.RoundReceiptError as e:
        assert "already exists" in e.reason
    second = MD.prepare_round_start(wl, nxt)
    assert second.round_id == nxt
    assert first.path.is_file() and second.path.is_file()
    assert json.loads(first.path.read_text())["receipt_hash"] == first.receipt_hash


def test_concurrent_id_allocation(tmp: Path):
    wl = _wl(tmp)
    got: list = []

    def _run():
        got.append(MD.allocate_receipt_id(wl))

    threads = [threading.Thread(target=_run) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 16
    assert len(set(got)) == 16
    assert sorted(got) == list(range(min(got), min(got) + 16))


def test_receipt_hash_covers_watermark_queries_hits_task(tmp: Path):
    wl = _wl(tmp)
    wl.task.write_text("widgets\n", encoding="utf-8")
    _append(wl, "obstacle", "widgets block the route")
    start = MD.prepare_round_start(wl, 1)
    raw = json.loads(start.path.read_text(encoding="utf-8"))

    def _tamper(mutator):
        payload = json.loads(json.dumps(raw))
        mutator(payload)
        try:
            MD.verify_start_payload(payload)
            raise AssertionError("tamper must fail receipt_hash")
        except MD.RoundReceiptError as e:
            assert "receipt_hash" in e.reason

    _tamper(lambda p: p["memory_watermark_start"]["kinds"]["obstacle"].__setitem__("count", 99))
    _tamper(lambda p: p["queries"]["obstacle"].__setitem__("bm25_query", "tampered"))
    _tamper(lambda p: p["hits"].__setitem__("obstacle", []))
    _tamper(lambda p: p["task"].__setitem__("branch_id", "evil"))
    _tamper(lambda p: p.__setitem__("created_at", "1970-01-01T00:00:00+00:00"))


def test_prompt_char_and_byte_cap(tmp: Path):
    wl = _wl(tmp)
    token = "widget-token " + ("αβγ" * 40)
    wl.task.write_text(token + "\n", encoding="utf-8")
    for kind in MD.RETRIEVAL_KINDS:
        for i in range(4):
            _append(wl, kind, f"{token} claim {i} " + ("x" * 100))
    start = MD.prepare_round_start(wl, 1, char_cap=1200, byte_cap=2400)
    text = MD.render_digest_prompt(
        start.digest, str(start.path), start.digest_hash, start.receipt_hash,
    )
    assert len(text) <= 1200
    assert len(text.encode("utf-8")) <= 2400
    assert "evidence" not in json.dumps(start.digest)


def test_jsonl_truncation_fails_finalize(tmp: Path):
    wl = _wl(tmp)
    _append(wl, "obstacle", "first entry about widgets")
    _append(wl, "obstacle", "second entry about widgets")
    start = MD.prepare_round_start(wl, 1)
    path = _gm(wl)._path("obstacle")
    raw = path.read_bytes()
    assert len(raw) > 10
    path.write_bytes(raw[: len(raw) // 2])
    try:
        MD.finalize_round_end(wl, start)
        raise AssertionError("truncation must fail closed")
    except MD.RoundReceiptError as e:
        assert e.phase == "end"


def test_jsonl_rewrite_and_prefix_mutation_fail_finalize(tmp: Path):
    wl = _wl(tmp)
    _append(wl, "obstacle", "stable prefix line")
    start = MD.prepare_round_start(wl, 1)
    path = _gm(wl)._path("obstacle")
    original = path.read_bytes()
    path.write_bytes(b'{"id":"ffffffffffffffff","claim":"rewritten longer than before"}\n')
    try:
        MD.finalize_round_end(wl, start)
        raise AssertionError("rewrite must fail closed")
    except MD.RoundReceiptError as e:
        assert e.phase == "end"
    path.write_bytes(b"X" + original[1:])
    try:
        MD.finalize_round_end(wl, start)
        raise AssertionError("prefix mutation must fail closed")
    except MD.RoundReceiptError as e:
        assert e.phase == "end"


def test_immutable_end_receipt(tmp: Path):
    wl = _wl(tmp)
    start = MD.prepare_round_start(wl, 1)
    end = MD.finalize_round_end(wl, start)
    end_payload = json.loads(end.read_text(encoding="utf-8"))
    assert end_payload["receipt_hash"] == MD.hash_receipt_payload(end_payload)
    MD.verify_end_payload(end_payload)
    try:
        MD.finalize_round_end(wl, start)
        raise AssertionError("end receipt must not be overwritten")
    except MD.RoundReceiptError as e:
        assert "already exists" in e.reason
    end_payload["appended_ids"]["obstacle"] = ["tampered"]
    try:
        MD.verify_end_payload(end_payload)
        raise AssertionError("end tamper must fail")
    except MD.RoundReceiptError as e:
        assert "receipt_hash" in e.reason


# --------------------------------------------------------------------------- #
# runner                                                                       #
# --------------------------------------------------------------------------- #

_NO_TMP = {test_parse_plain_task_md, test_parse_frontmatter_ids_without_breaking_body,
           test_kickoff_injects_digest_block}


def main() -> None:
    tests = [
        test_parse_plain_task_md,
        test_parse_frontmatter_ids_without_breaking_body,
        test_load_missing_task_defaults_project,
        test_empty_memory_retrieve,
        test_exact_subgoal_prioritized_over_bm25,
        test_bm25_fills_remaining_slots,
        test_top_five_per_kind_and_total_cap,
        test_exact_hits_survive_total_cap,
        test_digest_has_no_full_history,
        test_canonical_hash_deterministic_and_tamper_rejected,
        test_prepare_round_start_empty_and_reread,
        test_concurrent_append_changes_end_watermark,
        test_receipt_write_failure_prevents_run_round,
        test_receipt_read_verify_failure_prevents_run_round,
        test_prompt_contains_digest_path_hash,
        test_continuation_rounds_get_distinct_receipts,
        test_log_name_uses_global_receipt_id_after_restart,
        test_end_finalize_failure_does_not_launch_next_round,
        test_kickoff_injects_digest_block,
        test_allocator_monotonic_after_existing_no_overwrite,
        test_concurrent_id_allocation,
        test_receipt_hash_covers_watermark_queries_hits_task,
        test_prompt_char_and_byte_cap,
        test_jsonl_truncation_fails_finalize,
        test_jsonl_rewrite_and_prefix_mutation_fail_finalize,
        test_immutable_end_receipt,
    ]
    for t in tests:
        if t in _NO_TMP:
            t()
        else:
            with tempfile.TemporaryDirectory() as d:
                t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL MEMORY DIGEST TESTS PASSED")


if __name__ == "__main__":
    main()
