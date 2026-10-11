# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Stage-4 failure-cluster detector + evidence-constrained synthesis.

Runs standalone and under pytest. No real Grok / verify service.
"""

from __future__ import annotations

import json
import os
import signal
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from euler.core import FactGraph, GlobalMemory
from euler.execution import layout as L
from euler.execution import loop
from euler.execution.failure_clusters import (
    FailureControlError,
    FailureSynthesisError,
    TRIGGER_APPROACH_SIGNATURE,
    TRIGGER_BLOCKED_ATTEMPTS,
    TRIGGER_OBSTACLE_SIGNATURE,
    TRIGGER_WRONG_VERIFICATIONS,
    after_successful_close,
    cluster_states,
    evaluate_synthesis_close,
    fact_graph_has_writes,
    ledger_path,
    list_clusters,
    next_pending_request,
    persist_new_requests,
    request_path,
    resolve_request,
    scan_clusters,
    _write_request_exclusive,
)
from euler.execution.preflight import PreflightResult
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


def _wl(tmp: Path, name: str = "high") -> L.WorkerLayout:
    wl = L.WorkerLayout(tmp / "proj" / "workers" / name)
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


def _stamp(wl: L.WorkerLayout, *, worker: str = "", run: str = "run-aaaa-bbbb-cccc-ddddeeee",
           branch: str = "b1", subgoal: str = "s1") -> dict:
    return {
        "project_id": wl.project,
        "worker_id": worker or wl.name,
        "run_id": run,
        "branch_id": branch,
        "subgoal_id": subgoal,
        "provenance": "worker_mcp",
    }


def _add_attempt(gm: GlobalMemory, wl: L.WorkerLayout, aid: str, *,
                 result: str = "blocked", approach: str = "try induction",
                 worker: str = "", **kw) -> str:
    stamp = _stamp(wl, worker=worker, **{k: kw[k] for k in ("run", "branch", "subgoal") if k in kw})
    if "branch" in kw:
        stamp["branch_id"] = kw["branch"]
    if "subgoal" in kw:
        stamp["subgoal_id"] = kw["subgoal"]
    if "run" in kw:
        stamp["run_id"] = kw["run"]
    return gm.append(
        "proof_attempt", claim=f"attempt {aid}", evidence="construction notes QED",
        author=stamp["worker_id"], attempt_id=aid, approach=approach, result=result,
        reason="gap", salvage="retry", inputs=[], related_memory=[], **stamp,
    )


def _add_obstacle(gm: GlobalMemory, wl: L.WorkerLayout, claim: str, aid: str, *,
                  worker: str = "", **kw) -> str:
    stamp = _stamp(wl, worker=worker)
    stamp.update({k: kw[k] for k in stamp.keys() if k in kw})
    if "branch" in kw:
        stamp["branch_id"] = kw["branch"]
    if "subgoal" in kw:
        stamp["subgoal_id"] = kw["subgoal"]
    return gm.append(
        "obstacle", claim=claim, evidence="", author=stamp["worker_id"],
        attempt_id=aid, **stamp,
    )


def _add_verif(gm: GlobalMemory, wl: L.WorkerLayout, *, verdict, transport: str = "ok",
               worker: str = "", ts: str = "", **kw) -> str:
    stamp = _stamp(wl, worker=worker)
    if "subgoal" in kw:
        stamp["subgoal_id"] = kw["subgoal"]
    extra = dict(stamp)
    extra.update({
        "transport_status": transport,
        "verdict": verdict,
        "provenance": "fact_submit",
    })
    if ts:
        # timestamp is assigned by append; we sort by it. Fine if sequential.
        pass
    return gm.append(
        "verification", claim="stmt", evidence=f"verdict:{verdict}",
        author=stamp["worker_id"], **extra,
    )


def _detect(wl: L.WorkerLayout, branch: str = "b1", subgoal: str = "s1"):
    return after_successful_close(wl, branch_id=branch, subgoal_id=subgoal)


def _blocked_meta(aid: str, approach: str = "try induction") -> dict:
    return {
        "attempt_id": aid,
        "approach": approach,
        "result": "blocked",
        "reason": "gap at the successor step",
        "salvage": "reuse the base case",
        "inputs": ["TASK.md"],
        "related_memory": [],
    }


def _summary_meta(request: dict, aid: str) -> dict:
    return {
        "attempt_id": aid,
        "record_type": "key_failures_summary",
        "cluster_id": request["cluster_id"],
        "source_record_ids": request["member_ids"],
        "failed_plan_ids": request.get("attempt_ids") or [],
        "common_failures": ["successor gap repeats"],
        "implications_for_next_plans": ["try a different reduction"],
        "recommended_actions": ["abandon this successor lemma"],
    }


def test_trigger_blocked_attempts(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    _add_attempt(gm, wl, "a1")
    _add_attempt(gm, wl, "a2")
    assert scan_clusters(wl.project_dir) == []
    _add_attempt(gm, wl, "a3")
    fired = scan_clusters(wl.project_dir)
    assert any(c["trigger"] == TRIGGER_BLOCKED_ATTEMPTS for c in fired)
    created = _detect(wl)
    assert created and created[0]["trigger"] == TRIGGER_BLOCKED_ATTEMPTS
    assert not fact_graph_has_writes(wl.project_dir)


def test_trigger_approach_distinct_workers(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    _add_attempt(gm, wl, "a1", approach="Fourier decay", worker="high")
    _add_attempt(gm, wl, "a2", approach="fourier   decay!", worker="high")
    assert not any(c["trigger"] == TRIGGER_APPROACH_SIGNATURE for c in scan_clusters(wl.project_dir))
    _add_attempt(gm, wl, "a3", approach="FOURIER DECAY", worker="xhigh")
    fired = [c for c in scan_clusters(wl.project_dir) if c["trigger"] == TRIGGER_APPROACH_SIGNATURE]
    assert len(fired) == 1
    assert set(fired[0]["workers"]) == {"high", "xhigh"}


def test_trigger_wrong_verifications_400_not_500(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    _add_verif(gm, wl, verdict="wrong", transport="ok")
    _add_verif(gm, wl, verdict=None, transport="service_error")  # HTTP 500
    _add_verif(gm, wl, verdict=None, transport="timeout")        # HTTP 504
    _add_verif(gm, wl, verdict="wrong", transport="ok")
    assert not any(c["trigger"] == TRIGGER_WRONG_VERIFICATIONS for c in scan_clusters(wl.project_dir))
    _add_verif(gm, wl, verdict="wrong", transport="ok")  # 3rd ok+wrong; infra skipped
    fired = [c for c in scan_clusters(wl.project_dir) if c["trigger"] == TRIGGER_WRONG_VERIFICATIONS]
    assert len(fired) == 1
    assert len(fired[0]["verification_ids"]) >= 3


def test_trigger_obstacle_distinct_workers(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    claim = "Successor lemma has no bound"
    _add_obstacle(gm, wl, claim, "a1", worker="high")
    _add_obstacle(gm, wl, claim, "a2", worker="high")
    assert not any(c["trigger"] == TRIGGER_OBSTACLE_SIGNATURE for c in scan_clusters(wl.project_dir))
    _add_obstacle(gm, wl, "successor lemma has no bound.", "a3", worker="xhigh")
    fired = [c for c in scan_clusters(wl.project_dir) if c["trigger"] == TRIGGER_OBSTACLE_SIGNATURE]
    assert len(fired) == 1
    assert set(fired[0]["workers"]) == {"high", "xhigh"}


def test_no_trigger_below_threshold(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    _add_attempt(gm, wl, "a1")
    _add_attempt(gm, wl, "a2")
    assert scan_clusters(wl.project_dir) == []
    assert _detect(wl) == []


def test_legacy_unstamped_ignored(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    gm.append("proof_attempt", claim="legacy", evidence="old QED", author="legacy",
              attempt_id="legacy-1", approach="induction", result="blocked",
              reason="x", salvage="y", inputs=[], related_memory=[])
    _add_attempt(gm, wl, "a1")
    _add_attempt(gm, wl, "a2")
    assert scan_clusters(wl.project_dir) == []
    _add_attempt(gm, wl, "a3")
    fired = [c for c in scan_clusters(wl.project_dir) if c["trigger"] == TRIGGER_BLOCKED_ATTEMPTS]
    assert len(fired) == 1
    assert "legacy-1" not in (fired[0].get("attempt_ids") or [])


def test_concurrent_dedup(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    barrier = threading.Barrier(2)
    bags: list = []

    def run():
        barrier.wait()
        bags.append(_detect(wl))

    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    requested = [c for c in list_clusters(wl.project_dir) if c.get("state") == "requested"]
    assert len(requested) == 1
    ops = [c for c in list_clusters(wl.project_dir)]
    assert sum(1 for c in ops if c.get("op") == "requested" or c.get("state") == "requested") >= 1
    created_total = sum(len(x) for x in bags)
    assert created_total == 1


def test_orphan_request_sidecar_recovers_exactly_once(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"orphan-{i}")
    first = _detect(wl)
    assert first
    cid = first[0]["cluster_id"]
    ledger_path(wl.project_dir).unlink()

    recovered = _detect(wl)
    assert len(recovered) == 1
    assert recovered[0]["cluster_id"] == cid
    assert recovered[0]["recovered_from"] == "request_sidecar"
    assert _detect(wl) == []
    rows = list_clusters(wl.project_dir)
    assert len(rows) == 1 and rows[0]["state"] == "requested"


def test_requested_ledger_recreates_missing_sidecar(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"missing-sidecar-{i}")
    req = _detect(wl)[0]
    path = request_path(wl.project_dir, req["cluster_id"])
    path.unlink()
    assert _detect(wl) == []
    assert path.is_file()
    events = [json.loads(line) for line in ledger_path(wl.project_dir).read_text().splitlines()]
    assert sum(e.get("op") == "requested" for e in events) == 1


def test_requested_cluster_allows_append_only_member_growth(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for _ in range(3):
        _add_verif(gm, wl, verdict="wrong")

    req = _detect(wl)[0]
    cid = req["cluster_id"]
    path = request_path(wl.project_dir, cid)
    snapshot = path.read_text(encoding="utf-8")
    assert len(req["verification_ids"]) == 3

    _add_verif(gm, wl, verdict="wrong")
    live = [c for c in scan_clusters(wl.project_dir) if c["cluster_id"] == cid][0]
    assert len(live["verification_ids"]) == 4
    assert live["verification_ids"][:3] == req["verification_ids"]

    # The pending request remains the original immutable three-record
    # evidence snapshot and is not requeued or rejected merely because the
    # append-only live cluster acquired a fourth member.
    assert _detect(wl) == []
    assert path.read_text(encoding="utf-8") == snapshot
    events = [json.loads(line) for line in ledger_path(wl.project_dir).read_text().splitlines()]
    assert sum(e.get("op") == "requested" for e in events) == 1
    row = list_clusters(wl.project_dir)[0]
    assert row["state"] == "requested"
    assert row["verification_ids"] == req["verification_ids"]


def test_signature_cluster_growth_keeps_request_task_coordinates(tmp: Path):
    wl = _wl(tmp)
    _task(wl, branch="task-branch", subgoal="task-subgoal")
    gm = GlobalMemory(wl.project_dir)
    _add_attempt(gm, wl, "sig-1", approach="same route", worker="zeta")
    _add_attempt(gm, wl, "sig-2", approach="same route", worker="omega")

    req = _detect(wl, branch="task-branch", subgoal="task-subgoal")[0]
    assert req["trigger"] == TRIGGER_APPROACH_SIGNATURE
    assert req["branch_id"] == "task-branch"
    assert req["subgoal_id"] == "task-subgoal"

    # Signature clusters intentionally have no intrinsic task coordinate.
    # Adding a worker may also insert its name earlier in sorted worker order;
    # neither event invalidates the immutable request snapshot.
    _add_attempt(gm, wl, "sig-3", approach="same route", worker="alpha")
    created = _detect(wl, branch="task-branch", subgoal="task-subgoal")
    assert all(item["cluster_id"] != req["cluster_id"] for item in created)
    row = [
        item for item in list_clusters(wl.project_dir)
        if item["cluster_id"] == req["cluster_id"]
    ][0]
    assert row["branch_id"] == "task-branch"
    assert row["subgoal_id"] == "task-subgoal"


def test_requested_sidecar_mutation_still_fails_closed(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for _ in range(3):
        _add_verif(gm, wl, verdict="wrong")

    req = _detect(wl)[0]
    path = request_path(wl.project_dir, req["cluster_id"])
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    sidecar["member_ids"].append("f" * 16)
    path.write_text(json.dumps(sidecar), encoding="utf-8")

    try:
        _detect(wl)
        raise AssertionError("mutated requested sidecar must fail closed")
    except FailureControlError:
        pass


def test_orphan_corrupt_or_mismatched_fails_closed(tmp: Path):
    for mode in ("corrupt", "mismatch"):
        wl = _wl(tmp / mode)
        _task(wl)
        gm = GlobalMemory(wl.project_dir)
        for i in range(3):
            _add_attempt(gm, wl, f"{mode}-{i}")
        cluster = scan_clusters(wl.project_dir)[0]
        path = request_path(wl.project_dir, cluster["cluster_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "corrupt":
            path.write_text("{not-json\n", encoding="utf-8")
        else:
            obj = {
                "schema_version": 1, "op": "requested", "state": "requested",
                "seq": 1, "cluster_id": cluster["cluster_id"],
                "trigger": "wrong-trigger", "signature": cluster["signature"],
                "project_id": cluster["project_id"], "branch_id": "b1",
                "subgoal_id": "s1", "attempt_ids": cluster["attempt_ids"],
                "verification_ids": [], "obstacle_ids": [],
                "workers": cluster["workers"], "member_ids": cluster["member_ids"],
                "assigned_worker": wl.name, "created_at": "now", "watermark": {},
            }
            path.write_text(json.dumps(obj), encoding="utf-8")
        try:
            _detect(wl)
            raise AssertionError("bad orphan request must fail closed")
        except FailureControlError:
            pass
        assert list_clusters(wl.project_dir) == []


def test_concurrent_orphan_recovery_is_single_ledger_transition(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"recover-race-{i}")
    req = _detect(wl)[0]
    ledger_path(wl.project_dir).unlink()
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        results.append(_detect(wl))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(len(x) for x in results) == 1
    assert list_clusters(wl.project_dir)[0]["cluster_id"] == req["cluster_id"]


def test_same_cluster_not_requeued_after_resolved(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    first = _detect(wl)
    assert first
    cid = first[0]["cluster_id"]
    resolve_request(wl.project_dir, cid, kind="obstacle", entry_id="a" * 16, worker=wl.name)
    _add_attempt(gm, wl, "a4")
    again = _detect(wl)
    assert again == []
    assert cluster_states(wl.project_dir)[cid] == "resolved"


def test_resolve_rejects_unknown_wrong_worker_and_second_transition(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    try:
        resolve_request(wl.project_dir, "deadbeefdeadbeef", kind="obstacle",
                        entry_id="a" * 16, worker=wl.name)
        raise AssertionError("unknown cluster must not resolve")
    except FailureControlError:
        pass
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"resolve-{i}")
    req = _detect(wl)[0]
    for bad_kind, bad_entry in (("fact", "a" * 16), ("obstacle", "not-an-id")):
        try:
            resolve_request(wl.project_dir, req["cluster_id"], kind=bad_kind,
                            entry_id=bad_entry, worker=wl.name)
            raise AssertionError("invalid resolve kind/id must fail")
        except FailureControlError:
            pass
    try:
        resolve_request(wl.project_dir, req["cluster_id"], kind="obstacle",
                        entry_id="a" * 16, worker="other")
        raise AssertionError("wrong worker must not resolve")
    except FailureControlError:
        pass
    resolve_request(wl.project_dir, req["cluster_id"], kind="obstacle",
                    entry_id="a" * 16, worker=wl.name)
    try:
        resolve_request(wl.project_dir, req["cluster_id"], kind="obstacle",
                        entry_id="a" * 16, worker=wl.name)
        raise AssertionError("resolved request must not resolve twice")
    except FailureControlError:
        pass


def test_queue_order(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}", approach="route A")
    _add_attempt(gm, wl, "b1", approach="route B", worker="xhigh")
    _add_obstacle(gm, wl, "shared wall", "o1", worker="high")
    _add_obstacle(gm, wl, "shared wall", "o2", worker="xhigh")
    created = _detect(wl)
    assert len(created) >= 2
    seqs = [c["seq"] for c in created]
    assert seqs == sorted(seqs)
    pending = next_pending_request(wl)
    assert pending["seq"] == min(seqs)


def test_no_fact_writes_on_detect(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    _detect(wl)
    assert FactGraph(wl.project_dir).list() == []


def test_obstacle_summary_resolves(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    _stamp_os(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    created = _detect(wl)
    req = created[0]
    out = server.gm_add(
        "proof_attempt", claim="synthesis attempt", evidence="notes QED",
        metadata=_blocked_meta("syn-1"),
    )
    assert out.get("ok")
    meta = _summary_meta(req, "syn-1")
    out = server.gm_add("obstacle", claim="common stuck: successor", evidence="",
                        metadata=meta)
    assert out.get("ok"), out
    syn = evaluate_synthesis_close(
        wl, {"obstacle": [out["id"]], "proof_attempt": []},
        req, run_id=os.environ["EULER_RUN_ID"], branch_id="b1", subgoal_id="s1",
    )
    assert syn["kind"] == "obstacle"
    resolve_request(wl.project_dir, req["cluster_id"], kind="obstacle",
                    entry_id=syn["entry_id"], worker=wl.name)
    assert cluster_states(wl.project_dir)[req["cluster_id"]] == "resolved"
    rec = [e for e in gm.read("obstacle") if e["id"] == out["id"]][0]
    assert rec["record_type"] == "key_failures_summary"
    assert rec.get("dead_end_status") != "confirmed"


def test_dead_end_candidate_resolves_not_confirmed(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    _stamp_os(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    req = _detect(wl)[0]
    server.gm_add("proof_attempt", claim="syn", evidence="n QED",
                  metadata={**_blocked_meta("syn-d"), "result": "abandoned"})
    meta = _summary_meta(req, "syn-d")
    meta["dead_end_status"] = "candidate"
    out = server.gm_add("dead_end", claim="candidate synthesis", evidence="see ids",
                        metadata=meta)
    assert out.get("ok"), out
    syn = evaluate_synthesis_close(
        wl, {"dead_end": [out["id"]]}, req,
        run_id=os.environ["EULER_RUN_ID"], branch_id="b1", subgoal_id="s1",
    )
    assert syn["kind"] == "dead_end"
    rec = [e for e in gm.read("dead_end") if e["id"] == out["id"]][0]
    assert rec["dead_end_status"] == "candidate"


def test_confirmed_dead_end_rejects_fake_failure_synthesis_ref(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    _stamp_os(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    req = _detect(wl)[0]
    meta = _summary_meta(req, "syn-c")
    meta["dead_end_status"] = "confirmed"
    meta["evidence_class"] = "failure_synthesis"
    meta["evidence_refs"] = ["att-1"]
    out = server.gm_add("dead_end", claim="confirmed illegally", evidence="see refs",
                        metadata=meta)
    assert not out.get("ok"), out
    assert "unknown" in out.get("error", "")
    assert cluster_states(wl.project_dir)[req["cluster_id"]] == "requested"


def test_invalid_cluster_link_fails(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    _stamp_os(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"a{i}")
    req = _detect(wl)[0]
    meta = _summary_meta(req, "syn-x")
    meta["cluster_id"] = "deadbeefdeadbeef"
    out = server.gm_add("obstacle", claim="wrong cluster", evidence="", metadata=meta)
    try:
        evaluate_synthesis_close(
            wl, {"obstacle": [out["id"]]}, req,
            run_id=os.environ["EULER_RUN_ID"], branch_id="b1", subgoal_id="s1",
        )
        raise AssertionError("wrong cluster_id must fail")
    except FailureSynthesisError as e:
        assert "cluster_id" in str(e).lower() or "does not match" in str(e)


def test_synthesis_rejects_fake_source_and_attempt_ids(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    _stamp_os(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"source-{i}")
    req = _detect(wl)[0]
    server.gm_add("proof_attempt", claim="syn", evidence="QED",
                  metadata={**_blocked_meta("syn-fake"), "result": "partial"})
    for bad_meta in (
        {**_summary_meta(req, "syn-fake"), "source_record_ids": ["fake"]},
        {**_summary_meta(req, "syn-fake"), "failed_plan_ids": ["fake"]},
    ):
        out = server.gm_add("obstacle", claim="bad summary", evidence="",
                            metadata=bad_meta)
        try:
            evaluate_synthesis_close(
                wl, {"obstacle": [out["id"]]}, req,
                run_id=os.environ["EULER_RUN_ID"], branch_id="b1", subgoal_id="s1",
            )
            raise AssertionError("fake synthesis source IDs must fail")
        except FailureSynthesisError:
            pass


def test_verification_and_obstacle_clusters_accept_exact_source_ids(tmp: Path):
    for shape in ("verification", "obstacle"):
        wl = _wl(tmp / shape)
        _task(wl)
        _stamp_os(wl)
        gm = GlobalMemory(wl.project_dir)
        if shape == "verification":
            for _ in range(3):
                _add_verif(gm, wl, verdict="wrong")
            req = next(c for c in _detect(wl)
                       if c["trigger"] == TRIGGER_WRONG_VERIFICATIONS)
        else:
            _add_obstacle(gm, wl, "same obstruction", "o1", worker="high")
            _add_obstacle(gm, wl, "same obstruction", "o2", worker="xhigh")
            req = next(c for c in _detect(wl)
                       if c["trigger"] == TRIGGER_OBSTACLE_SIGNATURE)
        attempt = f"syn-{shape}"
        server.gm_add("proof_attempt", claim="synthesis", evidence="QED",
                      metadata={**_blocked_meta(attempt), "result": "partial"})
        out = server.gm_add("obstacle", claim="exact source summary", evidence="",
                            metadata=_summary_meta(req, attempt))
        accepted = evaluate_synthesis_close(
            wl, {"obstacle": [out["id"]]}, req,
            run_id=os.environ["EULER_RUN_ID"], branch_id="b1", subgoal_id="s1",
        )
        assert accepted["ok"] is True


def _run_loop(tmp: Path, writer, *, max_rounds: int = 1) -> tuple:
    wl = _wl(tmp)
    _task(wl)
    prompts = []

    def fake_round(_wl, _role, prompt, log_path, _ht):
        prompts.append(prompt)
        _stamp_os(_wl, os.environ.get("EULER_RUN_ID") or "run-x")
        writer(_wl, prompt, len(prompts))
        log_path.write_text("ok\n", encoding="utf-8")
        if len(prompts) >= max_rounds:
            _wl.stop.touch()
        return 0

    orig = loop.run_round
    loop.run_round = fake_round
    old = signal.getsignal(signal.SIGTERM)
    try:
        with _preflight_ok(), _env(EULER_ROUND_BEAT="0", EULER_MAX_ROUNDS=str(max_rounds)):
            rc = loop.main(str(wl.dir))
    finally:
        loop.run_round = orig
        signal.signal(signal.SIGTERM, old)
    st = json.loads(wl.status.read_text(encoding="utf-8"))
    return rc, st, wl, prompts


def test_prompt_injection_and_missing_blocks_next(tmp: Path):
    wl_seed = _wl(tmp)
    _task(wl_seed)
    gm = GlobalMemory(wl_seed.project_dir)
    _add_attempt(gm, wl_seed, "p1")
    _add_attempt(gm, wl_seed, "p2")

    def writer(wl, prompt, n):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        if n == 1:
            assert "FAILURE SYNTHESIS MODE" not in prompt
            server.gm_add("proof_attempt", claim="third fail", evidence="n QED",
                          metadata=_blocked_meta("p3"))
            server.gm_add("obstacle", claim="stuck", evidence="",
                          metadata={"attempt_id": "p3"})
        else:
            assert "FAILURE SYNTHESIS MODE" in prompt
            server.gm_add("proof_attempt", claim="forgot summary", evidence="n QED",
                          metadata={**_blocked_meta("p4"), "result": "partial"})

    rc, st, wl, prompts = _run_loop(tmp, writer, max_rounds=2)
    assert rc == 1
    assert st["state"] == "FAILURE_SYNTHESIS_FAILED"
    assert any("FAILURE SYNTHESIS MODE" in p for p in prompts)


def test_two_round_demo_obstacle_summary(tmp: Path):
    """Isolated fake two-round demo: round 1 creates the third failure; round 2
    writes an obstacle key_failures_summary. No real Grok."""
    wl_seed = _wl(tmp)
    _task(wl_seed)
    gm = GlobalMemory(wl_seed.project_dir)
    _add_attempt(gm, wl_seed, "d1")
    _add_attempt(gm, wl_seed, "d2")

    def writer(wl, prompt, n):
        _stamp_os(wl, os.environ["EULER_RUN_ID"])
        if n == 1:
            server.gm_add("proof_attempt", claim="third fail", evidence="n QED",
                          metadata=_blocked_meta("d3"))
            server.gm_add("obstacle", claim="stuck again", evidence="",
                          metadata={"attempt_id": "d3"})
        else:
            assert "FAILURE SYNTHESIS MODE" in prompt
            cid = None
            for line in prompt.splitlines():
                if line.strip().startswith("cluster_id:"):
                    cid = line.split(":", 1)[1].strip()
            assert cid
            server.gm_add("proof_attempt", claim="synthesis round", evidence="n QED",
                          metadata={**_blocked_meta("d-syn"), "result": "partial"})
            server.gm_add(
                "obstacle", claim="key failures: successor gap", evidence="from d1-d3",
                metadata=_summary_meta(next_pending_request(wl), "d-syn"),
            )

    rc, st, wl, prompts = _run_loop(tmp, writer, max_rounds=2)
    assert rc == 0, st
    assert st["state"] in ("idle", "stopped")
    assert any("FAILURE SYNTHESIS MODE" in p for p in prompts)
    states = cluster_states(wl.project_dir)
    assert states
    assert "resolved" in states.values()
    summaries = [
        e for e in GlobalMemory(wl.project_dir).read("obstacle")
        if e.get("record_type") == "key_failures_summary"
    ]
    assert summaries
    assert summaries[-1].get("cluster_id") in states
    assert FactGraph(wl.project_dir).list() == []
    rows = list_clusters(wl.project_dir)
    assert any(r.get("state") == "resolved" for r in rows)


def test_loop_surfaces_corrupt_failure_control_before_round(tmp: Path):
    wl = _wl(tmp)
    _task(wl)
    gm = GlobalMemory(wl.project_dir)
    for i in range(3):
        _add_attempt(gm, wl, f"control-{i}")
    cluster = scan_clusters(wl.project_dir)[0]
    path = request_path(wl.project_dir, cluster["cluster_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken", encoding="utf-8")
    calls = []

    def writer(*args):
        calls.append(args)

    rc, st, _wl_out, _prompts = _run_loop(tmp, writer, max_rounds=1)
    assert rc == 1
    assert calls == []
    assert st["state"] == "FAILURE_CONTROL_FAILED"
    assert st["failure_control"]["ok"] is False


def test_cli_failures_and_status_json(tmp: Path):
    with _env(EULER_AGENTS_ROOT=str(tmp / "agents"),
              EULER_WORKER_CONTRACT=str(tmp / "w.md"),
              EULER_WORKER_SKILLS=str(tmp / "sk")):
        (tmp / "w.md").write_text("# c\n")
        (tmp / "sk").mkdir(exist_ok=True)
        cli.do_new("P", roles="high:1")
        cli.do_assign("P/high", "prove", branch_id="b1", subgoal_id="s1")
        empty = cli.do_failures("P")
        assert empty["project"] == "P" and empty["clusters"] == []
        wl = L.WorkerLayout(L.worker_dir("P", "high"))
        gm = GlobalMemory(wl.project_dir)
        for i in range(3):
            _add_attempt(gm, wl, f"c{i}")
        _detect(wl)
        payload = cli.do_failures("P")
        assert payload["clusters"] and payload["clusters"][0]["cluster_id"]
        assert payload["clusters"][0]["state"] == "requested"
        rows = cli.do_status("P/high")
        assert "failures" in rows[0]
        assert rows[0]["failures"]["project_requested"] >= 1
        p = cli.build_parser()
        assert p.parse_args(["failures", "P", "--json"]).cmd == "failures"


def main() -> None:
    tests = [
        test_trigger_blocked_attempts,
        test_trigger_approach_distinct_workers,
        test_trigger_wrong_verifications_400_not_500,
        test_trigger_obstacle_distinct_workers,
        test_no_trigger_below_threshold,
        test_legacy_unstamped_ignored,
        test_concurrent_dedup,
        test_orphan_request_sidecar_recovers_exactly_once,
        test_requested_ledger_recreates_missing_sidecar,
        test_requested_cluster_allows_append_only_member_growth,
        test_signature_cluster_growth_keeps_request_task_coordinates,
        test_requested_sidecar_mutation_still_fails_closed,
        test_orphan_corrupt_or_mismatched_fails_closed,
        test_concurrent_orphan_recovery_is_single_ledger_transition,
        test_same_cluster_not_requeued_after_resolved,
        test_resolve_rejects_unknown_wrong_worker_and_second_transition,
        test_queue_order,
        test_no_fact_writes_on_detect,
        test_obstacle_summary_resolves,
        test_dead_end_candidate_resolves_not_confirmed,
        test_confirmed_dead_end_does_not_resolve,
        test_invalid_cluster_link_fails,
        test_synthesis_rejects_fake_source_and_attempt_ids,
        test_verification_and_obstacle_clusters_accept_exact_source_ids,
        test_prompt_injection_and_missing_blocks_next,
        test_two_round_demo_obstacle_summary,
        test_loop_surfaces_corrupt_failure_control_before_round,
        test_cli_failures_and_status_json,
    ]
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
        print(f"  [ok] {t.__name__}")
    print("ALL FAILURE CLUSTER TESTS PASSED")


if __name__ == "__main__":
    main()
