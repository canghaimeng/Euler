"""Relation-map focus: recorded main line, substantive frontier and stable relation identity."""
import json

import pytest
from fastapi.testclient import TestClient

from euler.observability import app, monitor
from euler.observability.focus import frontier, mainline, phase
from .test_monitor import _write, seed_research_events


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    monitor._CACHE.clear()
    yield
    monitor._CACHE.clear()


def _node(key, kind="obligation", status="OPEN", validity="VALID", **extra):
    return {"id": key, "kind": kind, "status": status, "validity": validity, **extra}


def _edge(source, target, kind="REQUIRES", validity="VALID"):
    return {"id": f"{source}>{kind}>{target}", "source": source, "target": target, "type": kind, "validity": validity}


def test_completed_main_line_follows_only_the_valid_route_that_closed_the_goal():
    graph = {"nodes": [_node("goal", status="DISCHARGED"), _node("lemma", status="DISCHARGED"),
                       _node("aux", status="DISCHARGED"), _node("old", status="DISCHARGED"),
                       _node("done", "route", "COMPLETED"), _node("shelved", "route", "PARKED"),
                       _node("pass", "evidence", "VALID", checker_result="PASS"),
                       _node("unknown", "evidence", "VALID", checker_result="UNKNOWN")],
             "edges": [_edge("done", "goal", "targets"), _edge("shelved", "goal", "targets"),
                       _edge("lemma", "done", "requires"), _edge("aux", "lemma"),
                       _edge("old", "lemma", validity="STALE"),
                       _edge("pass", "goal", "evidence"), _edge("unknown", "goal", "evidence")]}
    line = mainline(graph, "goal")
    assert line["basis"] == "completed_route" and line["routes"] == ["done"]
    assert set(line["nodes"]) == {"goal", "done", "lemma", "aux", "pass"}
    assert line["evidence"] == ["pass"]
    assert "old>REQUIRES>lemma" not in line["edges"] and "shelved>targets>goal" not in line["edges"]
    assert set(line["edges"]) == {"done>targets>goal", "lemma>requires>done", "aux>REQUIRES>lemma", "pass>evidence>goal"}


def test_registered_premises_closed_requires_at_least_one_valid_closed_premise():
    nodes = [_node("goal"), _node("a", status="DISCHARGED", status_changed_at="2026-10-05T10:00:00+00:00"),
             _node("b"), _node("blocker"),
             _node("ready"), _node("bare"), _node("stale_edge"), _node("half"), _node("held"), _node("parked", status="PARKED")]
    edges = [_edge("a", "ready"), _edge("ready", "goal"),
             _edge("a", "stale_edge", validity="STALE"),
             _edge("a", "half"), _edge("b", "half"),
             _edge("a", "held"), _edge("blocker", "held", "BLOCKED_BY"),
             _edge("a", "parked")]
    front = frontier({"nodes": nodes, "edges": edges}, "goal")
    ready = [item for item in front["items"] if item["kind"] == "ready"]
    assert [item["id"] for item in ready] == ["ready"]
    [item] = ready
    assert item["premises"] == ["a"] and item["connected"] and item["path"] == ["ready", "goal"]
    assert item["at"] == "2026-10-05T10:00:00+00:00" and item["time_source"] == "premises_closed"
    # The closed premise itself is the newest recorded closure; its time is the status event.
    closed = [entry for entry in front["items"] if entry["kind"] == "closed"]
    assert [(entry["id"], entry["time_source"]) for entry in closed] == [("a", "status_event")]
    assert {"a>REQUIRES>ready", "ready>REQUIRES>goal"} <= set(front["edges"])


def test_frontier_does_not_invent_links_or_order_without_recorded_times():
    nodes = [_node("goal"), _node("island", status="DISCHARGED", status_changed_at="2026-10-05T12:00:00Z"),
             _node("untimed", status="DISCHARGED"), _node("gone", status="REFUTED", status_changed_at="2026-10-05T11:00:00Z")]
    front = frontier({"nodes": nodes, "edges": []}, "goal")
    assert [(item["id"], item["kind"], item["connected"]) for item in front["items"]] == [
        ("island", "closed", False), ("gone", "refuted", False)]
    assert front["edges"] == []


def test_reopened_target_is_in_progress_despite_a_historical_solved_outcome():
    workspace = {"control": {"outcome": "SOLVED", "target_status": "OPEN"},
                 "acceptance": {"status": "passed", "mathematical_goal_achieved": True}}
    reopened = {"target_id": "goal", "nodes": [_node("goal", status="OPEN")], "edges": []}
    result = phase(reopened, workspace)
    assert not result["complete"] and result["target_state"] == "open"
    assert result["recorded_outcome"] == "SOLVED" and result["formal_passed"]
    stale = {"target_id": "goal", "nodes": [_node("goal", status="DISCHARGED", validity="STALE")], "edges": []}
    assert not phase(stale, workspace)["complete"]
    closed = {"target_id": "goal", "nodes": [_node("goal", status="DISCHARGED")], "edges": []}
    unconfigured = phase(closed, {"control": {}, "acceptance": {"available": False, "status": "unknown"}})
    assert unconfigured["complete"] and unconfigured["controller_closed"] and not unconfigured["formal_available"]
    # Without a controller target only the formal acceptance record is available.
    assert phase({"nodes": [], "edges": []}, workspace)["complete"]


def test_edges_carry_stable_identities_and_relation_ids(tmp_path):
    root = tmp_path / "stable"
    _write(root / "project.json", {"name": "stable", "workers": []})
    ids = seed_research_events(root, include_relation=True)
    first = monitor.snapshot(root, use_cache=False)["research_graph"]
    monitor._CACHE.clear()
    second = monitor.snapshot(root, use_cache=False)["research_graph"]
    keys = [edge["id"] for edge in first["edges"]]
    assert len(set(keys)) == len(keys) and keys == [edge["id"] for edge in second["edges"]]
    [relation] = [edge for edge in first["edges"] if edge["type"] == "SUSPECTED_SAME"]
    assert relation["relation_id"] and relation["id"] == "rel:" + relation["relation_id"]
    assert all(edge["id"].startswith("e:") for edge in first["edges"] if edge is not relation)
    nodes = {node["id"]: node for node in first["nodes"]}
    assert nodes[ids["lemma"]]["status_changed_at"] == "2026-01-01T00:00:00+00:00"
    assert nodes[ids["target"]]["status_changed_at"] is None


def test_map_focus_lists_unaccepted_records_with_structured_links_only(tmp_path, monkeypatch):
    root = tmp_path / "live"
    _write(root / "project.json", {"name": "live", "workers": []})
    ids = seed_research_events(root)
    rows = {
        "proof_attempt": [{"id": "try", "claim": "Exchange argument", "result": "blocked", "reason": "Loses feasibility",
                           "author": "alpha", "timestamp_utc": "2026-10-05T12:00:00Z", "status": "unverified",
                           "links": {"obligation_id": ids["lemma"], "note": ids["target"]}}],
        "conclusion": [{"id": "said", "claim": f"Settles {ids['target']} directly", "author": "beta",
                        "timestamp_utc": "2026-10-05T13:00:00Z", "links": {"related": "text only"}}],
        "plan": [{"id": "plan", "claim": "Search small cases", "timestamp_utc": "2026-10-05T14:00:00Z"}],
    }
    for kind, items in rows.items():
        _write(root / f"global_memory/{kind}.jsonl", "".join(json.dumps(item) + "\n" for item in items))
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(root))
    view = TestClient(app).get("/api/map-focus").json()
    records = view["frontier"]["records"]
    assert [record["id"] for record in records] == ["said", "try"]
    said, attempt = records
    # A graph id mentioned in free text or an unknown link field never becomes a link.
    assert said["links"] == [] and said["verification"] == "unaccepted_public_record"
    assert attempt["links"] == [{"id": ids["lemma"], "layer": "research", "field": "obligation_id"}]
    assert (attempt["result"], attempt["reported_status"], attempt["reason"]) == ("blocked", "unverified", "Loses feasibility")
    assert view["phase"]["complete"] is False and view["default_view"] == "frontier"
    assert view["anchor"] == ids["target"]
    line = view["mainline"]
    assert line["basis"] == "goal_structure" and ids["route"] in line["nodes"] and ids["lemma"] in line["nodes"]
    edge_ids = {edge["id"] for edge in TestClient(app).get("/api/graph", params={"layer": "research"}).json()["edges"]}
    assert set(line["edges"]) <= edge_ids
    assert TestClient(app).get("/api/map-focus", params={"layer": "nope"}).status_code == 400
