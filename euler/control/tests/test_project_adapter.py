"""Offline tests for the read-only Euler project adapter.

Uses a temporary synthetic project. No network, no runtime projects, no
provider CLIs. Covers active/revoked separation, memory and receipt
preservation, deterministic ordering, malformed fail-closed, missing optional
paths, and a static read-only guard.

Runs standalone (``python -m euler.control.tests.test_project_adapter``)
and under pytest.
"""

from __future__ import annotations

import ast
import hashlib
import json
import tempfile
from types import MappingProxyType
from pathlib import Path

from euler.core.schema import GLOBAL_KINDS
from euler.control.adapters.project import (
    ProjectAdapterError,
    snapshot_project,
)
from euler.control.ids import canonical_json

_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER = _ROOT / "adapters" / "project.py"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {fn.__name__}")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fact_markdown(fact_id: str, statement: str, *, predecessors=(), refs=None) -> str:
    pred = ", ".join(predecessors)
    refs_json = json.dumps(refs if refs is not None else [], ensure_ascii=False)
    return (
        "---\n"
        f"fact_id: {fact_id}\n"
        "problem_id: toy\n"
        "author: tester\n"
        f"predecessors: [{pred}]\n"
        "glossary_introduces: {}\n"
        f"external_refs: {refs_json}\n"
        "---\n\n"
        "## statement\n"
        f"{statement}\n\n"
        "## proof\n"
        "By inspection.\n"
    )


def _project(tmp: Path) -> Path:
    root = tmp / "toy"
    root.mkdir()
    _write(
        root / "fact_graph" / "facts" / "bbbbbbbbbbbbbbbb.md",
        _fact_markdown(
            "bbbbbbbbbbbbbbbb",
            "second statement",
            predecessors=("aaaaaaaaaaaaaaaa",),
            refs=[{"title": "Paper B", "key": "b1"}],
        ),
    )
    _write(
        root / "fact_graph" / "facts" / "aaaaaaaaaaaaaaaa.md",
        _fact_markdown("aaaaaaaaaaaaaaaa", "first statement"),
    )
    _write(
        root / "fact_graph" / "_revoked" / "deaddeaddeaddead.md",
        _fact_markdown("deaddeaddeaddead", "revoked statement"),
    )
    _write(
        root / "fact_graph" / "revocation_log.jsonl",
        json.dumps({"fact_id": "feedfeedfeedfeed", "reason": "test"}) + "\n",
    )
    _write(
        root / "global_memory" / "conclusion.jsonl",
        json.dumps(
            {"claim": "A", "id": "mem-2", "kind": "conclusion", "z": 1, "a": 2},
            ensure_ascii=False,
        )
        + "\n\n"
        + json.dumps({"id": "mem-1", "kind": "conclusion", "claim": "B"}, ensure_ascii=False)
        + "\n",
    )
    _write(
        root / "global_memory" / "plan.jsonl",
        json.dumps({"id": "plan-1", "kind": "plan", "claim": "try lemma"}, ensure_ascii=False)
        + "\n",
    )
    _write(
        root / "receipts" / "grok2" / "round_0002.end.json",
        json.dumps({"round_id": 2, "kind": "end", "b": 1, "a": 0}, indent=2),
    )
    _write(
        root / "receipts" / "grok1" / "round_0001.start.json",
        json.dumps({"kind": "start", "round_id": 1, "z": True}),
    )
    _write(
        root / "receipts" / "grok1" / "round_0001.launch.json",
        json.dumps({"kind": "launch", "round_id": 1}),
    )
    _write(
        root / "receipts" / "grok1" / "round_0001.result.json",
        json.dumps({"kind": "result", "round_id": 1, "rc": 0}),
    )
    _write(root / "receipts" / "grok1" / "notes.json", '{"ignored": true}')
    _write(root / "receipts" / "stray.start.json", '{"ignored": "not nested"}')
    return root


def test_active_and_revoked_facts_are_separated_and_preserved():
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(Path(tmp))
        snap = snapshot_project(root)
        assert [fact.fact_id for fact in snap.active_facts] == [
            "aaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbb",
        ]
        assert snap.revoked_fact_ids == ("deaddeaddeaddead", "feedfeedfeedfeed")
        first = snap.active_facts[0]
        raw_path = root / "fact_graph" / "facts" / "aaaaaaaaaaaaaaaa.md"
        raw = raw_path.read_bytes()
        assert first.raw_markdown == raw.decode("utf-8")
        assert first.content_sha256 == hashlib.sha256(raw).hexdigest()
        assert first.statement == "first statement"
        assert first.predecessors == ()
        assert first.relative_path == "fact_graph/facts/aaaaaaaaaaaaaaaa.md"
        second = snap.active_facts[1]
        assert second.predecessors == ("aaaaaaaaaaaaaaaa",)
        assert second.external_refs[0]["key"] == "b1"
        assert second.external_refs[0]["title"] == "Paper B"
        assert isinstance(second.external_refs[0], MappingProxyType)
        assert "deaddeaddeaddead" not in {fact.fact_id for fact in snap.active_facts}


def test_memory_and_receipts_are_canonical_sorted_and_stable():
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(Path(tmp))
        first = snapshot_project(root)
        second = snapshot_project(root)
        assert first == second
        kinds = [record.kind for record in first.memory_records]
        assert kinds == sorted(kinds)
        ids = [(record.kind, record.record_id) for record in first.memory_records]
        assert ids == sorted(ids)
        assert {record.kind for record in first.memory_records} <= set(GLOBAL_KINDS)
        conclusion = [r for r in first.memory_records if r.record_id == "mem-2"][0]
        payload = json.loads(conclusion.canonical_json)
        assert conclusion.canonical_json == canonical_json(payload)
        assert conclusion.relative_path == "global_memory/conclusion.jsonl"
        paths = [item.relative_path for item in first.receipts]
        assert paths == sorted(paths)
        assert paths == [
            "receipts/grok1/round_0001.launch.json",
            "receipts/grok1/round_0001.result.json",
            "receipts/grok1/round_0001.start.json",
            "receipts/grok2/round_0002.end.json",
        ]
        end = next(item for item in first.receipts if item.relative_path.endswith(".end.json"))
        assert json.loads(end.canonical_json)["round_id"] == 2
        launch = next(item for item in first.receipts if item.relative_path.endswith(".launch.json"))
        assert json.loads(launch.canonical_json)["kind"] == "launch"
        result = next(item for item in first.receipts if item.relative_path.endswith(".result.json"))
        assert json.loads(result.canonical_json)["rc"] == 0
        assert end.canonical_json == canonical_json(json.loads(end.canonical_json))
        assert "notes.json" not in "".join(paths)
        assert "stray.start.json" not in "".join(paths)


def test_missing_optional_paths_yield_empty_collections():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "empty"
        root.mkdir()
        snap = snapshot_project(root)
        assert snap.active_facts == ()
        assert snap.revoked_fact_ids == ()
        assert snap.memory_records == ()
        assert snap.receipts == ()
        _raises(ProjectAdapterError, snapshot_project, root / "missing")


def test_malformed_memory_and_receipts_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad"
        root.mkdir()
        _write(root / "global_memory" / "plan.jsonl", "{not json\n")
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad2"
        root.mkdir()
        _write(root / "global_memory" / "plan.jsonl", "[1, 2]\n")
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad3"
        root.mkdir()
        _write(root / "global_memory" / "plan.jsonl", "1\n")
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad4"
        root.mkdir()
        _write(root / "global_memory" / "plan.jsonl", '{"claim": "no id"}\n')
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad5"
        root.mkdir()
        _write(
            root / "receipts" / "w" / "round_0001.start.json",
            "{not json",
        )
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad6"
        root.mkdir()
        _write(root / "receipts" / "w" / "round_0001.end.json", "[1]")
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "blank"
        root.mkdir()
        _write(root / "global_memory" / "plan.jsonl", "\n\n")
        snap = snapshot_project(root)
        assert snap.memory_records == ()


def test_fact_graph_identity_and_predecessor_integrity_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad-id"
        _write(
            root / "fact_graph" / "facts" / "aaaaaaaaaaaaaaaa.md",
            _fact_markdown("bbbbbbbbbbbbbbbb", "mismatch"),
        )
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "dangling"
        _write(
            root / "fact_graph" / "facts" / "aaaaaaaaaaaaaaaa.md",
            _fact_markdown(
                "aaaaaaaaaaaaaaaa", "dangling", predecessors=("bbbbbbbbbbbbbbbb",)
            ),
        )
        _raises(ProjectAdapterError, snapshot_project, root)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "overlap"
        _write(
            root / "fact_graph" / "facts" / "aaaaaaaaaaaaaaaa.md",
            _fact_markdown("aaaaaaaaaaaaaaaa", "active"),
        )
        _write(
            root / "fact_graph" / "revocation_log.jsonl",
            json.dumps({"fact_id": "aaaaaaaaaaaaaaaa"}) + "\n",
        )
        _raises(ProjectAdapterError, snapshot_project, root)


def test_snapshot_has_no_write_side_effect():
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(Path(tmp))
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        snapshot_project(root)
        after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        assert before == after


def test_production_adapter_is_read_only():
    source = _ADAPTER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_ADAPTER))
    forbidden_modules = {
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
        "euler.control.event_store",
        "euler.core.global_memory",
    }
    forbidden_names = {
        "FactGraph",
        "GlobalMemory",
        "fact_submit",
        "EventStore",
        "set_status",
        "write_text",
        "write_bytes",
    }
    imported: set[str] = set()
    named: set[str] = set()
    write_opens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
                named.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                named.add(alias.name)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.Name):
            named.add(node.id)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                    if any(flag in mode for flag in "wax"):
                        write_opens.append(mode)
            if isinstance(func, ast.Attribute) and func.attr == "open":
                if node.args and isinstance(node.args[0], ast.Constant):
                    mode = str(node.args[0].value)
                    if any(flag in mode for flag in "wax"):
                        write_opens.append(mode)
    assert imported.isdisjoint(forbidden_modules), imported
    assert named.isdisjoint(forbidden_names), named & forbidden_names
    assert not write_opens
    assert "FactGraph.add" not in source
    assert "fact_submit" not in source
    assert "GlobalMemory.append" not in source


def main() -> None:
    tests = [
        test_active_and_revoked_facts_are_separated_and_preserved,
        test_memory_and_receipts_are_canonical_sorted_and_stable,
        test_missing_optional_paths_yield_empty_collections,
        test_malformed_memory_and_receipts_fail_closed,
        test_fact_graph_identity_and_predecessor_integrity_fail_closed,
        test_snapshot_has_no_write_side_effect,
        test_production_adapter_is_read_only,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL PROJECT ADAPTER TESTS PASSED")


if __name__ == "__main__":
    main()
