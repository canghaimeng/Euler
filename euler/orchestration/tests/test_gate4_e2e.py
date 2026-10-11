"""Gate 4 live-process E2E. Uses the product demo harness; does not rebuild state."""

from __future__ import annotations

import ast
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from euler.demo import DemoReport, run_offline_demo
from euler.orchestration import cli

_E2E = Path(__file__).resolve()
_DEMO = Path(__file__).resolve().parents[2] / "demo.py"

def test_e2e_source_has_no_manual_truth_construction():
    tree = ast.parse(_E2E.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.update(alias.name for alias in node.names)
    for banned in ("FactGraph", "EvidenceArtifact", "FinalAuditReceipt", "ProjectSnapshot"):
        assert banned not in imported
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for banned in ("gm_add", "fact_submit", "run_round", "reconcile_managed_round"):
        assert banned not in names
        assert banned not in attrs
    demo_src = ast.parse(_DEMO.read_text(encoding="utf-8"))
    for node in ast.walk(demo_src):
        if isinstance(node, ast.Attribute) and node.attr == "add":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "FactGraph":
                raise AssertionError("demo.py must not call FactGraph.add")


def test_gate4_live_loop_reaches_solved(monkeypatch):
    monkeypatch.setenv("EULER_WORKER_LOOP_LAUNCHER", "/must-not-launch-deployment")
    report = run_offline_demo()
    assert isinstance(report, DemoReport)
    assert report.ok, "\n".join(report.errors)
    assert report.kind == "offline_integration_demo"
    assert "not a benchmark" in report.note.lower()
    assert report.receipts["start"] and report.receipts["launch"]
    assert report.receipts["result"] and report.receipts["end"]
    assert report.ids.get("fact_id") and len(str(report.ids["fact_id"])) == 16
    assert report.ids.get("packet_hash")
    assert report.ids.get("outcome") == "SOLVED"
    assert report.ids.get("acceptance_id")
    assert report.ids.get("checkpoint_id")
    assert report.ids.get("lease_id")
    assert report.ids.get("final_audit_lease_id")
    assert report.events.get("euler.dispatch_ready", 0) >= 1
    assert report.events.get("euler.round_reconciled") == 1
    assert report.events.get("evidence.registered", 0) >= 1
    assert report.events.get("obligation.status_changed", 0) >= 1
    assert report.events.get("euler.acceptance_recorded", 0) >= 1
    assert report.events.get("euler.checkpoint_created", 0) >= 1
    assert report.verifier_invocations >= 2


def test_demo_cli_json():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["demo", "--json"])
    payload = json.loads(buf.getvalue())
    assert rc == 0, payload.get("errors")
    assert payload["ok"] is True
    assert payload["kind"] == "offline_integration_demo"
    assert payload["ids"]["outcome"] == "SOLVED"
    assert payload["verifier_invocations"] >= 2
