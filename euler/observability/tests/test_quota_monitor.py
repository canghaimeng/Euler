# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Quota views remain read-only, private, and conservative under stale data."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from euler.observability import app, monitor
from euler.observability.tests.test_monitor import _write, seed_research_events


@pytest.fixture
def quota_project(tmp_path, monkeypatch):
    project = tmp_path / "cascade"
    _write(project / "project.json", {"name": "cascade", "coordinator": "Astra"})
    config = tmp_path / "dashboard.json"
    quota = tmp_path / "quota.json"
    _write(config, {"name": "Example project", "independent_project": True,
                    "quota_file": "quota.json", "monitor_file": "team.json",
                    "teams": [{"id": "cascade", "workers": [], "coordinator": {
                        "name": "Astra", "model": "gpt-6-astra", "effort": "ultra"}}]})
    _write(tmp_path / "team.json", {"project": "cascade", "checked_at_utc": "1970-01-01T00:16:40Z",
                                    "coordinator_ui": "running", "goal_status": "active",
                                    "coordinator_task": "Develop the next research route",
                                    "verifier": {"backend": "cursor", "model": "cursor-grok-4.6-high",
                                                 "effort": "high", "state": "on_demand",
                                                 "access_token": "private-verifier-token"}})
    monkeypatch.setenv("EULER_DASHBOARD_CONFIG", str(config))
    monkeypatch.setenv("EULER_DASHBOARD_PROJECT", str(project))
    monkeypatch.delenv("EULER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(monitor.time, "time", lambda: 1000.0)
    record = {"checked_at": 1000.0, "permit": True, "remaining_percent": 22.5,
              "reason": "healthy", "status": "healthy", "threshold_percent": 3,
              "latched": False,
              "providers": {"codex": {"remaining_percent": 22.5,
                            "windows": [{"remaining_percent": 22.5, "used_percent": 77.5,
                                         "window_minutes": 300, "resets_at": 1200}]},
                            "cursor": {"remaining_percent": 60, "on_demand_enabled": False}}}
    _write(quota, record)
    return project, config, quota, record


def view(fixture, record=None):
    project, _, path, original = fixture
    if record is not None:
        _write(path, record)
    return monitor.build_workspace(project, use_cache=False)["quota"]


def test_known_quota_allows_and_uses_minimum_window(quota_project):
    result = view(quota_project)
    assert result["permit"] is True and result["status"] == "healthy"
    assert result["remaining_percent"] == 22.5 and result["threshold_percent"] == 3
    assert result["max_age_seconds"] == 45
    assert result["providers"]["codex"]["windows"][0]["window_minutes"] == 300
    record = deepcopy(quota_project[3])
    record["remaining_percent"] = 90
    record["providers"]["codex"]["remaining_percent"] = 80
    record["providers"]["codex"]["windows"][0]["remaining_percent"] = 2
    result = view(quota_project, record)
    assert result["remaining_percent"] == 2 and result["permit"] is False


@pytest.mark.parametrize("remaining,permit", [(0, False), (2.99, False), (3, False), (3.01, True)])
def test_conservative_three_percent_boundary(quota_project, remaining, permit):
    record = deepcopy(quota_project[3])
    record["remaining_percent"] = remaining
    assert view(quota_project, record)["permit"] is permit


@pytest.mark.parametrize("remaining", [None, True, "90", -1, 101, float("nan"), float("inf")])
def test_invalid_or_unknown_percentage_never_permits(quota_project, remaining):
    record = deepcopy(quota_project[3])
    record["remaining_percent"] = remaining
    result = view(quota_project, record)
    assert result["permit"] is False and result["remaining_percent"] is None
    assert result["status"] == "unknown"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("checked,status", [(None, "unknown"), (0, "unknown"), (1031, "unknown"), (954, "stale")])
def test_timestamp_missing_future_or_stale_pauses(quota_project, checked, status):
    record = deepcopy(quota_project[3])
    record["checked_at"] = checked
    result = view(quota_project, record)
    assert result["permit"] is False and result["status"] == status


@pytest.mark.parametrize("change", ["missing_provider", "unknown_provider", "latched", "denied", "on_demand", "on_demand_unknown", "unknown_status"])
def test_unconfirmed_provider_or_guard_state_never_permits(quota_project, change):
    record = deepcopy(quota_project[3])
    if change == "missing_provider":
        del record["providers"]["cursor"]
    elif change == "unknown_provider":
        record["providers"]["codex"]["remaining_percent"] = None
    elif change == "latched":
        record["latched"] = True
    elif change == "denied":
        record["permit"] = False
    elif change == "on_demand":
        record["providers"]["cursor"]["on_demand_enabled"] = True
    elif change == "on_demand_unknown":
        del record["providers"]["cursor"]["on_demand_enabled"]
    else:
        record["status"] = "unknown"
    assert view(quota_project, record)["permit"] is False


def test_quota_output_is_allowlisted_and_bounded(quota_project):
    record = deepcopy(quota_project[3])
    record.update(account_id="private-account", access_token="private-token", balance=76543210,
                  reason="private-reason-token", permit=False)
    record["providers"]["codex"].update(account_id="private-nested-account", balance=9999)
    record["providers"]["codex"]["windows"] *= 20
    record["providers"]["codex"]["windows"][0]["name"] = "private-window-account"
    record["providers"]["private-provider-account"] = {"balance": 9999}
    result = view(quota_project, record)
    serialized = json.dumps(result)
    assert "private-" not in serialized and "balance" not in serialized and "access_token" not in serialized
    assert set(result["providers"]) == {"codex", "cursor"}
    assert len(result["providers"]["codex"]["windows"]) == 8
    assert result["reason_code"] == "guard_denied"


def test_absent_optional_source_does_not_change_existing_project(quota_project):
    config = quota_project[1]
    _write(config, {})
    result = monitor.build_workspace(quota_project[0], use_cache=False)
    assert result["quota"] == {"available": False}
    assert result["project"]["independent"] is False


def test_configured_missing_or_malformed_source_shows_pause(quota_project):
    quota_project[2].unlink()
    assert view(quota_project)["available"] is True
    assert view(quota_project)["permit"] is False
    quota_project[2].write_text("{partial", encoding="utf-8")
    assert view(quota_project)["status"] == "unknown"


def test_separate_lead_identity_and_read_only_http_view(quota_project):
    root, config, quota, _ = quota_project
    seed_research_events(root)
    before = (quota.stat().st_mtime_ns, quota.read_bytes(), config.read_bytes())
    workspace = monitor.build_workspace(root, use_cache=False)
    assert workspace["project"]["id"] == "cascade" and workspace["project"]["independent"] is True
    assert workspace["project"]["state"] == "OPEN"
    assert workspace["acceptance"]["available"] is False
    lead = workspace["teams"][0]["coordinator"]
    assert lead["name"] == "Astra" and lead["effort"] == "ultra" and lead["model"] == "gpt-6-astra"
    assert lead["goal_status"] == "active" and lead["task_summary"] == "Develop the next research route"
    assert workspace["verifier"]["status"] == "on_demand"
    assert workspace["verifier"]["model"] == "cursor-grok-4.6-high"
    assert "private-verifier-token" not in json.dumps(workspace)
    client = TestClient(app)
    response = client.get("/api/quota")
    assert response.status_code == 200 and response.json()["permit"] is True
    assert client.post("/api/quota").status_code == 405
    assert (quota.stat().st_mtime_ns, quota.read_bytes(), config.read_bytes()) == before


@pytest.mark.parametrize("field,value", [("status", {}), ("status", ["healthy"]), ("latched", "false")])
def test_malformed_guard_fields_pause_without_exception(quota_project, field, value):
    record = deepcopy(quota_project[3])
    record[field] = value
    assert view(quota_project, record)["permit"] is False
    record["reason"] = {"access_token": "private-reason"}
    record["permit"] = False
    assert "private-reason" not in json.dumps(view(quota_project, record))
