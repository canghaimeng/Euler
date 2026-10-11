# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Public static shell, authenticated data, and a mutation-free HTTP surface."""

from fastapi.testclient import TestClient

from euler.observability import app


def test_token_protects_all_data_routes_but_leaves_login_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(tmp_path))
    monkeypatch.setenv("EULER_DASHBOARD_TOKEN", "test-only-access-token")
    client = TestClient(app)
    assert client.get("/").status_code == 200
    for route in ("/api/overview", "/api/factgraph", "/api/workspace", "/api/graph"):
        assert client.get(route).status_code == 401
        assert client.get(route + "?token=test-only-access-token").status_code == 401
        assert client.get(route, headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/overview", headers={"Authorization": "Bearer test-only-access-token"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "test-only-access-token" not in client.get("/").text


def test_default_loopback_use_and_no_mutation_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(tmp_path))
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    client = TestClient(app)
    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/overview").status_code == 405


def test_legacy_endpoints_reject_symlinks_to_private_project_data(monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(tmp_path))
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    private = tmp_path / "workers" / "worker" / "local_memory"
    private.mkdir(parents=True)
    (private / "secret.md").write_text("## statement\nprivate-proof-sentinel\n## proof\nPrivate.\n")
    (private / "secret.jsonl").write_text(json.dumps({"claim": "private-channel-sentinel"}) + "\n")
    facts = tmp_path / "fact_graph" / "facts"
    facts.mkdir(parents=True)
    (facts / "leak.md").symlink_to(private / "secret.md")
    channels = tmp_path / "global_memory"
    channels.mkdir()
    (channels / "plan.jsonl").symlink_to(private / "secret.jsonl")
    client = TestClient(app)
    assert client.get("/api/factgraph").json()["nodes"] == []
    assert client.get("/api/channel/plan").json()["entries"] == []
    (facts / "leak.md").unlink()
    facts.rmdir()
    facts.symlink_to(private, target_is_directory=True)
    assert client.get("/api/factgraph").json()["nodes"] == []


def test_malformed_fact_metadata_cannot_break_dashboard(monkeypatch, tmp_path):
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(tmp_path))
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("EULER_DASHBOARD_CONFIG", raising=False)
    facts = tmp_path / "fact_graph" / "facts"
    facts.mkdir(parents=True)
    (facts / "bad.md").write_text(
        "---\nfact_id: [unexpected]\nauthor: [unexpected]\npredecessors: not-a-list\n---\n"
        "## statement\nA partial record.\n## proof\nA proof.\n"
    )
    client = TestClient(app)
    for route in ("/api/overview", "/api/factgraph", "/api/workspace", "/api/graph"):
        assert client.get(route).status_code == 200
