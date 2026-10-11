# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Dashboard contract and privacy regression tests using synthetic public data."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from euler.observability import app
from euler.observability.monitor import build_fact_detail, build_graph, build_workspace, snapshot


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")


def _fact(root: Path, identifier: str, author: str, predecessors=(), *, revoked=False) -> None:
    _write(root / "fact_graph" / ("_revoked" if revoked else "facts") / f"{identifier}.md",
           f"---\nfact_id: {identifier}\nauthor: {author}\npredecessors: [{', '.join(predecessors)}]\n---\n"
           f"## statement\nLemma {identifier}: $n+0=n$.\n## proof\nBy identity.\n## intuition\nAn identity.\n")


def seed_dashboard_project(root: Path) -> Path:
    """Reusable two-team fixture; all content is explicitly synthetic."""
    _write(root / "project.json", {"name": root.name, "coordinator": "sol", "workers": ["alpha", "beta", "gamma"]})
    for name, state in (("alpha", "stopped"), ("beta", "PRECHECK_FAILED"), ("gamma", "created")):
        _write(root / "workers" / name / ".status.json", {"worker": name, "state": state,
               "pid": None, "round": 3, "updated_at": time.time()})
        _write(root / "workers" / name / ".role", "PROVIDER=cursor\nMODEL=test-model\nREASONING_EFFORT=high\nROLE=researcher\nSECRET=do-not-serve\n")
        _write(root / "workers" / name / "TASK.md", f"# Prove the {name} lemma\nAssignment follows.")
        _write(root / "workers" / name / "local_memory" / "private.jsonl", "private-sentinel")
    _fact(root, "fact-a", "alpha")
    _fact(root, "fact-b", "beta", ["fact-a"])
    _fact(root, "fact-c", "gamma", ["fact-a", "fact-b"])
    _write(root / "global_memory" / "verification.jsonl", json.dumps({"id": "verified-a", "author": "alpha",
           "claim": "Base lemma accepted", "timestamp_utc": "2026-01-01T00:00:00Z", "verdict": "correct"}) + "\n{partial")
    config = root.parent / (root.name + "-dashboard.json")
    _write(config, {"name": "Synthetic research project", "description": "Offline demonstration fixture",
                   "teams": [{"id": "theory", "name": "Theory", "workers": ["alpha", "gamma"]},
                             {"id": "audit", "name": "Audit", "workers": ["beta"]}]})
    return config


def seed_research_events(root: Path, *, invalidate: bool = False, include_relation: bool = False) -> dict:
    """Use the real typed event writer to seed valid projection test input."""
    from euler.control.event_store import EventStore
    from euler.control.obligations import Relation, RelationType, Route
    from euler.control.schema import EventEnvelope
    from euler.control.tests.test_obligation_views import _contract, _evidence, _obligation

    contract = _contract(problem_id=root.name)
    target = _obligation(contract, statement=contract.exact_statement)
    lemma = _obligation(contract, statement="lemma holds")
    evidence = _evidence(contract)
    route = Route.build(target_obligation_id=target.obligation_id, mechanism="Reduce to the base lemma",
                        required_obligation_ids=[lemma.obligation_id])
    payloads = [("problem.contract.created", {"contract": contract.to_dict()}),
                ("obligation.created", {"obligation": target.to_dict()}),
                ("obligation.created", {"obligation": lemma.to_dict()}),
                ("evidence.registered", {"evidence": evidence.to_dict()}),
                ("obligation.status_changed", {"obligation_id": lemma.obligation_id,
                  "status": "DISCHARGED", "evidence_refs": [evidence.artifact_id]}),
                ("route.created", {"route": route.to_dict()})]
    if include_relation:
        relation = Relation.build(relation_type=RelationType.SUSPECTED_SAME,
                                  source=lemma.obligation_id, target=target.obligation_id,
                                  exact_claim="A candidate correspondence", scope=contract.scope,
                                  evidence_refs=[evidence.artifact_id], contract_id=contract.contract_id,
                                  definitions_hash=contract.scope.definitions_hash)
        payloads.append(("relation.created", {"relation": relation.to_dict()}))
    if invalidate:
        payloads.append(("evidence.invalidated", {"artifact_id": evidence.artifact_id}))
    with EventStore(root / "control" / "control.sqlite3") as store:
        for index, (kind, payload) in enumerate(payloads):
            store.append(EventEnvelope.build(project_id=root.name, actor="alpha", event_type=kind,
                         idempotency_key=f"fixture-{index}", timestamp_utc="2026-01-01T00:00:00Z", payload=payload))
    _write(root / "control" / "manifest.json", {"target_obligation_id": target.obligation_id})
    return {"target": target.obligation_id, "lemma": lemma.obligation_id,
            "route": route.route_id, "evidence": evidence.artifact_id}


def _tree_digest(root: Path) -> dict:
    return {str(path.relative_to(root)): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob("*") if path.is_file()}


def test_multiple_teams_live_metadata_and_private_boundary(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    original = Path.open

    def guarded(path, *args, **kwargs):
        if "local_memory" in path.parts or "chatDB" in path.parts:
            raise AssertionError("dashboard read private worker data")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    workspace = build_workspace(root, use_cache=False)
    assert workspace["metrics"] == {"facts": 3, "teams": 2, "workers": 3,
                                    "active_workers": 0, "blocked_workers": 1, "graph_edges": 3}
    assert [team["id"] for team in workspace["teams"]] == ["theory", "audit"]
    assert workspace["teams"][1]["status"] == "blocked"
    assert workspace["teams"][0]["workers"][0]["model"] == "test-model"
    assert workspace["teams"][0]["workers"][0]["task_summary"] == "Prove the alpha lemma"
    serialized = json.dumps(workspace)
    assert "private-sentinel" not in serialized and "do-not-serve" not in serialized
    assert any(event["id"] == "verified-a" for event in workspace["activity"])


def test_unassigned_workers_are_retained_and_default_is_one_team(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    _write(config, {"teams": [{"id": "theory", "workers": ["alpha"]},
                             {"id": "second", "workers": ["alpha", "absent"]}]})
    workspace = build_workspace(root, use_cache=False)
    assert {worker["id"] for team in workspace["teams"] for worker in team["workers"]} == {"alpha", "beta", "gamma"}
    assert len(workspace["warnings"]) == 2
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG")
    workspace = build_workspace(root, use_cache=False)
    assert workspace["metrics"]["teams"] == 1


def test_missing_and_revoked_predecessors_are_visible(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    _write(root / "workers" / "alpha" / ".status.json", {"worker": "alpha", "state": "created"})
    _fact(root, "revoked", "alpha", revoked=True)
    _fact(root, "dependent", "alpha", ["revoked", "missing"])
    graph = build_graph(root, use_cache=False)
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["revoked"]["status"] == "revoked"
    assert nodes["missing"]["status"] == "missing"
    assert nodes["dependent"]["status"] == "dependency_unavailable"
    assert {edge["source"] for edge in graph["edges"]} == {"revoked", "missing"}
    assert build_workspace(root)["metrics"]["facts"] == 0
    assert build_fact_detail(root, "dependent")["status"] == "dependency_unavailable"
    assert build_workspace(root)["teams"][0]["workers"][0]["accepted_facts"] == 0


def test_research_graph_replays_actual_routes_and_stale_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    ids = seed_research_events(root, invalidate=True, include_relation=True)
    graph = build_graph(root, "research", use_cache=False)
    assert graph["available"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes[ids["lemma"]]["validity"] != "VALID"
    assert nodes[ids["evidence"]]["status"] == "STALE"
    edges = {(edge["source"], edge["target"], edge["type"]) for edge in graph["edges"]}
    assert edges == {(ids["evidence"], ids["lemma"], "evidence"),
                     (ids["lemma"], ids["route"], "requires"), (ids["route"], ids["target"], "targets"),
                     (ids["lemma"], ids["target"], "SUSPECTED_SAME")}
    relation = next(edge for edge in graph["edges"] if edge["type"] == "SUSPECTED_SAME")
    assert relation["validity"] == "STALE" and relation["exact_claim"] == "A candidate correspondence"
    assert all(edge["validity"] == "VALID" for edge in graph["edges"] if edge["type"] != "SUSPECTED_SAME")
    assert build_workspace(root)["control"]["target_status"] == "OPEN"


def test_snapshot_does_not_modify_source_or_create_sqlite_sidecars(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    seed_dashboard_project(root)
    seed_research_events(root)
    before = _tree_digest(root)
    assert build_workspace(root, use_cache=False)["control"]["available"]
    assert _tree_digest(root) == before
    assert not (root / "control" / "control.sqlite3-wal").exists()


def test_snapshot_reads_uncheckpointed_wal_without_modifying_live_db(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    seed_research_events(root)
    source = sqlite3.connect(root / "control" / "control.sqlite3")
    source.execute("CREATE TABLE live_marker (value TEXT)")
    source.execute("INSERT INTO live_marker VALUES ('live')")
    source.commit()
    before = _tree_digest(root)
    try:
        assert build_workspace(root, use_cache=False)["control"]["available"]
        assert _tree_digest(root) == before
    finally:
        source.close()


def test_malformed_control_fails_closed_and_symlink_fact_cannot_escape(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    _write(root / "control" / "control.sqlite3", "not a database")
    outside = tmp_path / "private.md"
    _write(outside, "PRIVATE")
    (root / "fact_graph" / "facts").mkdir(parents=True)
    (root / "fact_graph" / "facts" / "escape.md").symlink_to(outside)
    result = snapshot(root, use_cache=False)
    assert not result["workspace"]["control"]["available"]
    assert result["workspace"]["project"]["state"] == "UNKNOWN"
    assert result["workspace"]["warnings"]
    assert "PRIVATE" not in json.dumps(result)


def test_in_project_symlinks_cannot_expose_private_worker_memory(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    private = root / "workers" / "alpha" / "local_memory"
    _write(private / "secret.md", "## statement\nPRIVATE-FACT-SENTINEL\n## proof\nprivate")
    _write(private / "secret.jsonl", '{"claim":"PRIVATE-CHANNEL-SENTINEL"}\n')
    facts = root / "fact_graph" / "facts"
    facts.mkdir(parents=True)
    (facts / "secret.md").symlink_to(private / "secret.md")
    memory = root / "global_memory"
    memory.mkdir()
    (memory / "plan.jsonl").symlink_to(private / "secret.jsonl")
    original = Path.open

    def guarded(path, *args, **kwargs):
        if "local_memory" in path.resolve().parts:
            raise AssertionError("dashboard opened redirected private data")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    result = snapshot(root, use_cache=False)
    assert not result["facts"]
    assert "PRIVATE-" not in json.dumps(result)
    # Also reject an entire public store redirected into the private tree.
    (facts / "secret.md").unlink()
    facts.rmdir()
    facts.symlink_to(private, target_is_directory=True)
    result = snapshot(root, use_cache=False)
    assert not result["facts"]
    assert "PRIVATE-" not in json.dumps(result)


def test_invalid_research_event_does_not_expose_partial_success(tmp_path, monkeypatch):
    from euler.control.event_store import EventStore
    from euler.control.schema import EventEnvelope
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    seed_research_events(root)
    with EventStore(root / "control" / "control.sqlite3") as store:
        store.append(EventEnvelope.build(project_id=root.name, actor="test", event_type="obligation.status_changed",
                     idempotency_key="invalid-status", timestamp_utc="2026-01-02T00:00:00Z",
                     payload={"obligation_id": "0" * 64, "status": "DISCHARGED"}))
    result = snapshot(root, use_cache=False)
    assert not result["research_graph"]["available"]
    assert result["workspace"]["project"]["state"] == "UNKNOWN"


@pytest.mark.parametrize("variant", ["matching", "wrong-outcome", "wrong-contract", "malformed"])
def test_control_acceptance_requires_valid_record_exact_contract_and_terminal_status(tmp_path, monkeypatch, variant):
    from euler.control.acceptance import AcceptanceRecord, AcceptanceVerdict, ProjectOutcome, _acceptance_id
    from euler.control.event_store import EventStore
    from euler.control.schema import EventEnvelope
    from euler.control.tests.test_obligation_views import _contract, _evidence

    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    root = tmp_path / "project"
    ids = seed_research_events(root)
    contract = _contract(problem_id=root.name)
    evidence = _evidence(contract, claim=contract.exact_statement, content="target-proof")
    fields = {"verdict": AcceptanceVerdict.PASS, "reasons": (),
              "contract_id": "0" * 64 if variant == "wrong-contract" else contract.contract_id,
              "obligation_id": ids["target"],
              "project_outcome": ProjectOutcome.REFUTED if variant == "wrong-outcome" else ProjectOutcome.SOLVED,
              "fact_id": None, "evidence_ids": (evidence.artifact_id,),
              "audit_receipt_id": "a" * 64, "admission_ids": ()}
    acceptance = AcceptanceRecord(record_id=_acceptance_id(**fields), created_at="2026-01-02T00:00:00Z", **fields).to_dict()
    if variant == "malformed":
        acceptance.pop("audit_receipt_id")
    with EventStore(root / "control" / "control.sqlite3") as store:
        for index, (kind, payload) in enumerate([
            ("evidence.registered", {"evidence": evidence.to_dict()}),
            ("obligation.status_changed", {"obligation_id": ids["target"], "status": "DISCHARGED",
                                            "evidence_refs": [evidence.artifact_id]}),
            ("euler.acceptance_recorded", {"acceptance": acceptance}),
        ]):
            store.append(EventEnvelope.build(project_id=root.name, actor="final-audit", event_type=kind,
                         idempotency_key=f"acceptance-{index}", timestamp_utc="2026-01-02T00:00:00Z", payload=payload))
    workspace = build_workspace(root, use_cache=False)
    if variant == "matching":
        assert workspace["project"]["state"] == "SOLVED"
    else:
        assert workspace["control"]["outcome"] is None
        assert workspace["project"]["state"] not in {"SOLVED", "REFUTED"}


def test_stale_coordinator_goal_complete_is_not_math_acceptance(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    monitor = tmp_path / "monitor.json"
    _write(monitor, {"project": root.name, "goal_status": "complete", "coordinator_ui": "idle",
                     "checked_at_utc": "2020-01-01T00:00:00Z"})
    _write(config, {"monitor_file": str(monitor)})
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    workspace = build_workspace(root, use_cache=False)
    assert workspace["project"]["state"] == "UNKNOWN"
    assert workspace["teams"][0]["coordinator"]["status"] == "stale"
    assert not workspace["acceptance"]["available"]
    assert any("stale" in warning for warning in workspace["warnings"])


def test_external_formal_acceptance_is_distinct_from_legacy_control(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    seed_research_events(root)
    acceptance = tmp_path / "acceptance.json"
    record = {"mathematical_goal_achieved": True, "checked_at": "2026-01-01T00:00:00Z",
              "kernel_main_declaration": "Main.theorem", "euler_main_fact_admission_claimed": False,
              "requirements": {"exact_target_kernel_closed": True,
                "fresh_independent_final_audit": {"verdict": "PASS", "checks": 8, "critical_errors": 0, "gaps": 0,
                                                  "session_id": "PRIVATE-ID"},
                "default_whole_lake_build": {"exit_code": 0, "jobs": 20},
                "all_prerequisite_build": {"exit_code": 0, "jobs": 18}}}
    _write(acceptance, record)
    _write(config, {"acceptance_file": str(acceptance)})
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    workspace = build_workspace(root, use_cache=False)
    assert workspace["control"]["target_status"] == "OPEN"
    assert workspace["acceptance"]["status"] == "passed"
    assert not workspace["acceptance"]["fact_admission_claimed"]
    assert "PRIVATE-ID" not in json.dumps(workspace)
    record["requirements"]["fresh_independent_final_audit"]["gaps"] = 1
    _write(acceptance, record)
    assert build_workspace(root, use_cache=False)["acceptance"]["status"] == "pending"


def test_reused_unrelated_pid_not_reported_live_and_container_metadata_is_explicit(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    _write(root / "workers" / "alpha" / ".status.json", {"pid": os.getpid(), "state": "running", "started_at": time.time()})
    workspace = build_workspace(root, use_cache=False)
    worker = workspace["teams"][0]["workers"][0]
    assert worker["alive"] is False and worker["status"] == "offline"
    monkeypatch.setenv("EULER_DASHBOARD_PID_MODE", "metadata")
    workspace = build_workspace(root, use_cache=False)
    worker = workspace["teams"][0]["workers"][0]
    assert worker["alive"] is None and worker["status"] == "running"
    assert worker["liveness_source"] == "metadata"
    assert workspace["teams"][0]["status"] == "unverified"
    assert workspace["metrics"]["active_workers"] is None


def test_task_summary_skips_managed_dispatch_frontmatter(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    _write(root / "workers" / "alpha" / "TASK.md", "---\nproject_id: synthetic\n---\n"
           "# Euler dispatch\ncandidate_id: example\n## ContextPacket\n"
           '===== CONTRACT =====\n{"exact_statement":"long payload"}\n'
           "# Finish the paper-to-Lean map\nActual assignment.\n")
    workspace = build_workspace(root, use_cache=False)
    assert workspace["teams"][0]["workers"][0]["task_summary"] == "Finish the paper-to-Lean map"


def test_http_routes_unknown_fact_and_layer(tmp_path, monkeypatch):
    root = tmp_path / "project"
    config = seed_dashboard_project(root)
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(root))
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    with TestClient(app) as client:
        assert client.get("/api/workspace").json()["metrics"]["teams"] == 2
        assert client.get("/api/graph?layer=facts").json()["available"]
        assert not client.get("/api/graph?layer=research").json()["available"]
        assert client.get("/api/graph?layer=private").status_code == 400
        assert client.get("/api/facts/fact-a").json()["proof"] == "By identity."
        assert client.get("/api/facts/unknown").status_code == 404
        assert client.get("/api/evidence/acceptance").json()["status"] == "unknown"


def test_empty_and_malformed_metadata_are_renderable(tmp_path, monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    workspace = build_workspace(tmp_path, use_cache=False)
    assert workspace["metrics"]["facts"] == 0
    assert workspace["project"]["state"] == "UNKNOWN"
    _write(tmp_path / "project.json", "not-json")
    _write(tmp_path / "workers" / "alpha" / ".status.json", "{")
    workspace = build_workspace(tmp_path, use_cache=False)
    assert workspace["warnings"]
    assert workspace["teams"][0]["workers"][0]["status"] == "unknown"
