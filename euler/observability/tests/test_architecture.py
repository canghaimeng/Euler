"""Proof-architecture overview: recorded structure only, no inferred status or percentages."""
import json

import pytest
from fastapi.testclient import TestClient

from euler.observability import app, monitor
from euler.observability.architecture import build_architecture
from .test_monitor import _fact, _write, seed_research_events


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    monitor._CACHE.clear()
    yield
    monitor._CACHE.clear()


def _seed_chain(root):
    """target REQUIRES middle REQUIRES base; middle BLOCKED_BY blocker; base discharged."""
    from euler.control.event_store import EventStore
    from euler.control.obligations import Relation, RelationType
    from euler.control.schema import EventEnvelope
    from euler.control.tests.test_obligation_views import _contract, _evidence, _obligation

    contract = _contract(problem_id=root.name)
    target = _obligation(contract, statement=contract.exact_statement)
    middle = _obligation(contract, statement="middle step")
    base = _obligation(contract, statement="base lemma")
    blocker = _obligation(contract, statement="unresolved blocker")
    evidence = _evidence(contract, claim="base lemma")

    def relation(kind, source, target_id, claim):
        return Relation.build(relation_type=kind, source=source, target=target_id, exact_claim=claim,
                              scope=contract.scope, contract_id=contract.contract_id,
                              definitions_hash=contract.scope.definitions_hash)

    payloads = [("problem.contract.created", {"contract": contract.to_dict()})]
    payloads += [("obligation.created", {"obligation": item.to_dict()}) for item in (target, middle, base, blocker)]
    payloads += [("evidence.registered", {"evidence": evidence.to_dict()}),
                 ("obligation.status_changed", {"obligation_id": base.obligation_id, "status": "DISCHARGED",
                                                "evidence_refs": [evidence.artifact_id]})]
    for kind, source, target_id in ((RelationType.REQUIRES, target.obligation_id, middle.obligation_id),
                                    (RelationType.REQUIRES, middle.obligation_id, base.obligation_id),
                                    (RelationType.BLOCKED_BY, middle.obligation_id, blocker.obligation_id)):
        payloads.append(("relation.created", {"relation": relation(kind, source, target_id, "recorded").to_dict()}))
    with EventStore(root / "control" / "control.sqlite3") as store:
        for index, (kind, payload) in enumerate(payloads):
            store.append(EventEnvelope.build(project_id=root.name, actor="alpha", event_type=kind,
                         idempotency_key=f"chain-{index}", timestamp_utc="2026-01-01T00:00:00Z", payload=payload))
    _write(root / "control" / "manifest.json", {"target_obligation_id": target.obligation_id})
    return {"target": target.obligation_id, "middle": middle.obligation_id,
            "base": base.obligation_id, "blocker": blocker.obligation_id}


def test_curated_module_edge_reports_open_records_on_hidden_chain(tmp_path, monkeypatch):
    root = tmp_path / "chain"
    _write(root / "project.json", {"name": "chain", "workers": []})
    ids = _seed_chain(root)
    config = tmp_path / "chain.json"
    _write(config, {"reading_order": [ids["target"], ids["base"]], "node_labels": {ids["base"]: "基础引理"}})
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    view = build_architecture(root)
    assert view["source"] == "curated" and view["layer"] == "research"
    nodes = {node["id"]: node for node in view["nodes"]}
    assert set(nodes) == {ids["target"], ids["base"]}
    base, target = nodes[ids["base"]], nodes[ids["target"]]
    assert base["title"] == "基础引理" and base["curated"] and base["state"] == "established"
    assert target["role"] == "target" and target["state"] == "open" and target["tier"] > base["tier"]
    # REQUIRES keeps prerequisite -> dependent; the hidden blocked middle stays visible as open work.
    [edge] = view["edges"]
    assert (edge["source"], edge["target"], edge["direct"]) == (ids["base"], ids["target"], False)
    assert edge["via_open"] == 1 and edge["via_items"][0]["id"] == ids["middle"]
    assert edge["via_items"][0]["state"] == "blocked"
    assert target["closure"]["obligations"] == {"established": 1, "blocked": 1, "open": 1}
    assert target["open_items"][0]["id"] == ids["blocker"] and target["open_items"][0]["frontier"]
    assert view["verdict"]["code"] == "open" and not view["verdict"]["formal_passed"]
    assert [item["id"] for item in view["reading_path"]] == [ids["target"], ids["base"]]
    assert "percent" not in json.dumps(view)


def test_route_prerequisites_form_the_uncurated_architecture(tmp_path, monkeypatch):
    root = tmp_path / "routes"
    _write(root / "project.json", {"name": "routes", "workers": []})
    ids = seed_research_events(root)
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    view = build_architecture(root)
    assert view["source"] == "recorded_premises" and view["available"]
    roles = {node["id"]: (node["role"], node["tier"]) for node in view["nodes"]}
    assert roles == {ids["lemma"]: ("module", 0), ids["route"]: ("route", 1), ids["target"]: ("target", 2)}
    assert {(e["source"], e["target"], e["direct"]) for e in view["edges"]} == {
        (ids["lemma"], ids["route"], True), (ids["route"], ids["target"], True)}
    [route] = view["routes"]
    assert route["targets_main"] and route["state"] == "proposed" and route["requires_states"] == {"established": 1}
    ladder = view["ladder"]
    assert ladder["routes"] == {"total": 1, "by_status": {"PROPOSED": 1}}
    assert ladder["checked_components"] == {"obligations": {"established": 1}, "checks": {"model": 1}}
    assert ladder["closure"]["target_status"] == "OPEN" and ladder["closure"]["outcome"] is None


def test_fresh_research_without_paper_facts_or_control_has_no_invented_structure(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    _write(root / "project.json", {"name": "fresh", "workers": [], "goal": "Find a counterexample"})
    _write(root / "global_memory/proof_attempt.jsonl",
           json.dumps({"id": "a1", "claim": "Try induction", "result": "success"}) + "\n"
           + json.dumps({"id": "a2", "claim": "Try exchange"}) + "\n")
    _write(root / "global_memory/obstacle.jsonl", json.dumps({"id": "o1", "claim": "Induction fails"}) + "\n")
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(root))
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    response = TestClient(app).get("/api/architecture")
    assert response.status_code == 200
    view = response.json()
    assert not view["available"] and view["layer"] is None and view["nodes"] == [] and view["edges"] == []
    assert view["verdict"]["code"] == "unregistered" and view["goal"] == "Find a counterexample"
    reports = view["ladder"]["self_reports"]
    assert reports["attempts"] == 2 and reports["by_result"] == {"success": 1, "unspecified": 1}
    assert reports["obstacles"] == 1
    assert TestClient(app).get("/api/architecture", params={"project": "../elsewhere"}).status_code == 404


def test_scope_valid_evidence_counts_as_checked_only_with_an_explicit_pass(tmp_path, monkeypatch):
    from euler.control.contracts import CheckerResult, EvidenceKind
    from euler.control.event_store import EventStore
    from euler.control.schema import EventEnvelope
    from euler.control.tests.test_obligation_views import _contract, _evidence, _obligation
    from euler.observability.architecture import _Graph, _card

    root = tmp_path / "receipts"
    _write(root / "project.json", {"name": "receipts", "workers": []})
    contract = _contract(problem_id=root.name)
    kernel = EvidenceKind.KERNEL_CHECKED_PROOF
    receipts = {"pass": _evidence(contract, kind=kernel, content="pass", result=CheckerResult.PASS),
                "fail": _evidence(contract, kind=kernel, content="fail", result=CheckerResult.FAIL),
                "unknown": _evidence(contract, kind=kernel, content="unknown", result=CheckerResult.UNKNOWN),
                "stale": _evidence(contract, kind=kernel, content="stale", result=CheckerResult.PASS),
                "missing": _evidence(contract, kind=EvidenceKind.HEURISTIC_OBSERVATION, content="missing")}
    target = _obligation(contract, statement=contract.exact_statement,
                         evidence_refs=tuple(item.artifact_id for item in receipts.values()))
    payloads = [("problem.contract.created", {"contract": contract.to_dict()})]
    payloads += [("evidence.registered", {"evidence": item.to_dict()}) for item in receipts.values()]
    payloads += [("obligation.created", {"obligation": target.to_dict()}),
                 ("evidence.invalidated", {"artifact_id": receipts["stale"].artifact_id})]
    with EventStore(root / "control" / "control.sqlite3") as store:
        for index, (kind, payload) in enumerate(payloads):
            store.append(EventEnvelope.build(project_id=root.name, actor="alpha", event_type=kind,
                         idempotency_key=f"receipt-{index}", timestamp_utc="2026-01-01T00:00:00Z", payload=payload))
    _write(root / "control" / "manifest.json", {"target_obligation_id": target.obligation_id})
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    view = build_architecture(root)
    [node] = view["nodes"]
    assert node["closure"]["evidence"] == {"kernel": 1, "failed": 1, "unconfirmed": 2, "stale": 1}
    expected = {"registered": 5, "valid": 4, "passed": 1, "failed": 1, "unconfirmed": 2, "stale": 1}
    assert {key: view["ladder"]["accepted_evidence"][key] for key in expected} == expected
    assert view["ladder"]["checked_components"]["checks"] == {"kernel": 1}
    graph = _Graph(monitor.snapshot(root)["research_graph"])
    cards = {name: _card(graph, item.artifact_id, "research") for name, item in receipts.items()}
    assert {name: card["state"] for name, card in cards.items()} == {
        "pass": "established", "fail": "failed", "unknown": "unconfirmed", "stale": "stale", "missing": "unconfirmed"}
    assert (cards["fail"]["evidence_kind"], cards["fail"]["checker_result"]) == ("KERNEL_CHECKED_PROOF", "FAIL")
    assert cards["missing"]["checker_result"] is None


def test_fact_only_project_exposes_top_level_facts_as_entry_points(tmp_path, monkeypatch):
    root = tmp_path / "facts"
    _write(root / "project.json", {"name": "facts", "workers": []})
    _fact(root, "base", "alpha")
    _fact(root, "step", "alpha", ["base"])
    _fact(root, "top", "beta", ["step"])
    _write(root / "fact_graph/facts/bare.md",
           "---\nfact_id: bare\nauthor: beta\npredecessors: []\n---\n## statement\nA recorded claim.\n## proof\n\n")
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    view = build_architecture(root)
    assert view["layer"] == "facts" and view["source"] == "top_facts" and view["target"] is None
    nodes = {node["id"]: node for node in view["nodes"]}
    assert set(nodes) == {"top", "bare"}
    assert nodes["top"]["has_proof"] and nodes["top"]["closure"]["facts"] == {"established": 2}
    # Acceptance alone is not shown as an available source proof.
    assert nodes["bare"]["state"] == "established" and not nodes["bare"]["has_proof"]
    assert view["verdict"]["code"] == "unregistered"
    assert view["ladder"]["accepted_evidence"]["facts_accepted"] == 4
