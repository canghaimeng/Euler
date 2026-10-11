"""Codex-owned cross-module tests for the Phase 1 foundation."""

from __future__ import annotations

import ast
import importlib
import tempfile
from pathlib import Path

from euler.control.budget import BudgetExceeded, BudgetLedger
from euler.control.event_store import EventStore
from euler.control.ids import sha256_hex, stable_id
from euler.control.schema import (
    BudgetLimit,
    BudgetLimitKind,
    EventEnvelope,
    ObservationStatus,
    ObservedQuantity,
    UsageRecord,
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _hex(label: str) -> str:
    return sha256_hex(label)


def _quantity(unit: str, value: int | None) -> ObservedQuantity:
    return ObservedQuantity(
        unit=unit,
        status=(
            ObservationStatus.UNKNOWN
            if value is None
            else ObservationStatus.OBSERVED
        ),
        value=value,
    )


def _usage(cost: int | None) -> UsageRecord:
    return UsageRecord(
        input_tokens=_quantity("tokens", None),
        output_tokens=_quantity("tokens", None),
        reasoning_tokens=_quantity("tokens", None),
        cached_tokens=_quantity("tokens", None),
        cost_microusd=_quantity("microusd", cost),
        wall_ms=_quantity("ms", None),
        tool_cpu_ms=_quantity("ms", None),
        tool_gpu_ms=_quantity("ms", None),
    )


def _attempt_event(key: str, state: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="codex-main",
        event_type=f"attempt.{state}",
        idempotency_key=key,
        payload={"attempt_id": _hex(key), "state": state},
        timestamp_utc="2026-09-17T00:00:00Z",
        correlation_id=_hex(key),
    )


def test_attempt_event_and_budget_reservation_share_atomic_boundary():
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "control.sqlite3"
        with EventStore(path) as store:
            ledger = BudgetLedger(store)
            ledger.ensure_account(
                "proj",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=1000),
            )
            attempt = _hex("atomic-attempt")
            try:
                with store.transaction(immediate=True):
                    ledger.reserve(
                        project_id="proj",
                        attempt_id=attempt,
                        idempotency_key="atomic-reserve",
                        reserved_cost_microusd=400,
                    )
                    store.append(_attempt_event("atomic-attempt", "leased"))
                    raise RuntimeError("simulate crash before commit")
            except RuntimeError:
                pass

            reservation_id = stable_id(
                "reservation", 1, "proj", attempt, "atomic-reserve"
            )
            assert ledger.get_reservation(reservation_id) is None
            assert all(
                event.event_type != "budget.reserved"
                for _seq, event in store.iter_project("proj")
            )
            assert all(
                event.event_type != "attempt.leased"
                for _seq, event in store.iter_project("proj")
            )
            assert ledger.get_account("proj").consumed_cost_microusd == 0


def test_attempt_and_budget_commit_and_cursor_replay():
    with tempfile.TemporaryDirectory() as raw:
        with EventStore(Path(raw) / "control.sqlite3") as store:
            ledger = BudgetLedger(store)
            ledger.ensure_account(
                "proj",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=1000),
            )
            attempt = _hex("committed-attempt")
            with store.transaction(immediate=True):
                reservation = ledger.reserve(
                    project_id="proj",
                    attempt_id=attempt,
                    idempotency_key="committed-reserve",
                    reserved_cost_microusd=400,
                )
                store.append(_attempt_event("committed-attempt", "leased"))

            rows = list(store.iter_project("proj"))
            assert [seq for seq, _event in rows] == sorted(seq for seq, _event in rows)
            assert any(event.event_type == "budget.reserved" for _seq, event in rows)
            assert any(event.event_type == "attempt.leased" for _seq, event in rows)
            tail = list(store.iter_project("proj", after_seq=rows[-2][0]))
            assert tail == rows[-1:]
            assert ledger.get_reservation(reservation.reservation_id) is not None


def test_actual_overrun_is_recorded_and_closes_future_reservations():
    with tempfile.TemporaryDirectory() as raw:
        with EventStore(Path(raw) / "control.sqlite3") as store:
            ledger = BudgetLedger(store)
            ledger.ensure_account(
                "proj",
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=1000),
            )
            reservation = ledger.reserve(
                project_id="proj",
                attempt_id=_hex("overrun-attempt"),
                idempotency_key="overrun",
                reserved_cost_microusd=100,
            )
            ledger.settle(reservation.reservation_id, _usage(1100))
            account = ledger.get_account("proj")
            assert account is not None
            assert account.consumed_cost_microusd == 1100
            assert account.remaining_cost_microusd == -100
            try:
                ledger.reserve(
                    project_id="proj",
                    attempt_id=_hex("after-overrun"),
                    idempotency_key="after-overrun",
                    reserved_cost_microusd=0,
                )
            except BudgetExceeded:
                pass
            else:  # pragma: no cover - fail loudly in standalone mode
                raise AssertionError("over-budget account accepted a new reservation")


def test_all_phase1_production_files_respect_truth_boundary():
    forbidden_import_roots = {
        "euler.core",
        "euler.gateway",
        "euler.verify",
        "euler.execution",
    }
    forbidden_names = {"FactGraph", "GlobalMemory", "fact_submit"}
    live_surface = {
        "cli.py",
        "controller.py",
        "dispatch.py",
        "final_audit.py",
        "hooks.py",
        "mcp.py",
        "reconcile.py",
    }
    for path in sorted(_PACKAGE_ROOT.glob("*.py")):
        if path.name == "__init__.py" or path.name in live_surface:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(
                        alias.name == root or alias.name.startswith(root + ".")
                        for root in forbidden_import_roots
                    ), path.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not any(
                    node.module == root or node.module.startswith(root + ".")
                    for root in forbidden_import_roots
                ), path.name
                assert forbidden_names.isdisjoint(alias.name for alias in node.names)
        assert "fact_graph/" not in source
        assert "global_memory/" not in source
        assert "FactGraph.add" not in source
        # Provenance strings such as producer="fact_submit:..." are not gateway
        # calls. AST checks above still forbid importing/calling fact_submit.
        if path.name == "acceptance.py":
            assert source.count('"fact_submit"') == 1
        else:
            tree_names = set()
            for node in ast.walk(ast.parse(source, filename=str(path))):
                if isinstance(node, ast.Name):
                    tree_names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    tree_names.add(node.attr)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    tree_names.add(node.func.id)
            assert "fact_submit" not in tree_names
            if path.name not in {"acceptance.py", "research_service.py"}:
                assert "fact_submit" not in source


def test_public_phase1_api_imports_from_package_root():
    module = importlib.import_module("euler.control")
    expected = {
        "ArtifactStore",
        "AcceptanceRecord",
        "ActionSpec",
        "AttemptReceipt",
        "BudgetLedger",
        "BudgetPolicy",
        "CandidateAction",
        "ContextCompiler",
        "ComputeRequest",
        "ComputeRunner",
        "ContextCacheKey",
        "ProjectSnapshot",
        "EvidenceArtifact",
        "EventEnvelope",
        "EventStore",
        "EvaluationSummary",
        "FailureRecord",
        "Obligation",
        "ObservedQuantity",
        "ProblemContract",
        "PolynomialPredicate",
        "ResourceSnapshot",
        "RuntimeCoordinator",
        "FinalAuditReceipt",
        "RecurrenceHistory",
        "ResearchView",
        "Relation",
        "Route",
        "SCCReport",
        "TransparentScheduler",
        "build_action_context_pair",
        "canonical_json",
        "stable_id",
        "snapshot_project",
    }
    assert expected.issubset(set(module.__all__))
    for name in expected:
        assert getattr(module, name) is not None


def main() -> None:
    tests = [
        test_attempt_event_and_budget_reservation_share_atomic_boundary,
        test_attempt_and_budget_commit_and_cursor_replay,
        test_actual_overrun_is_recorded_and_closes_future_reservations,
        test_all_phase1_production_files_respect_truth_boundary,
        test_public_phase1_api_imports_from_package_root,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL PHASE 1 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
