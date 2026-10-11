"""Reader-focus regressions for refutations and legacy formal completion reports."""
from euler.observability.focus import frontier, phase


def node(identifier, status, **fields):
    return dict(id=identifier, kind="obligation", status=status, validity="VALID",
                created_at="2026-10-01T00:00:00Z", **fields)


def test_refuted_frontier_stays_visible_without_a_support_path():
    graph = {"nodes": [node("claim", "REFUTED", status_changed_at="2026-10-03T00:00:00Z"),
                       node("goal", "OPEN")],
             "edges": [{"source": "claim", "target": "goal", "type": "REQUIRES", "validity": "VALID", "id": "e"}]}
    result = frontier(graph, "goal")
    item = next(item for item in result["items"] if item["id"] == "claim")
    assert item["kind"] == "refuted" and not item["connected"]
    assert item["path"] == ["claim"] and "e" not in result["edges"]


def test_later_formal_report_and_later_reopening_have_distinct_reading_modes():
    target = node("goal", "OPEN")
    graph = {"nodes": [target], "edges": [], "target_id": "goal"}
    workspace = {"control": {"target_status": "OPEN", "outcome": None},
                 "acceptance": {"status": "passed", "mathematical_goal_achieved": True,
                                "checked_at": "2026-10-02T00:00:00Z"}}
    # The existing formalized project has a report, but its original controller was never closed.
    result = phase(graph, workspace)
    assert result["complete"] and result["formal_current"] and result["target_status"] == "OPEN"
    # Reopening after that report does not remain in completed mode.
    target["status_changed_at"] = "2026-10-03T00:00:00Z"
    workspace["control"]["outcome"] = "SOLVED"
    result = phase(graph, workspace)
    assert not result["complete"] and not result["formal_current"] and result["formal_passed"]
    # A newer claim also cannot reuse an older report without a new acceptance.
    target.pop("status_changed_at")
    target["created_at"] = "2026-10-03T00:00:00Z"
    assert not phase(graph, workspace)["complete"]
