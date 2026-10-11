from pathlib import Path
from unittest.mock import patch
import tempfile
import pytest

from euler.control.ids import canonical_json, sha256_hex
from euler.control.local_actions import (
    LocalActionError,
    owner_is_alive,
    owner_process_identity,
    run_local_action,
)
from euler.control.runtime import LeaseStatus
from euler.control.tests.test_final_audit import _seed


def test_checker_reservation_replay_and_input_conflict():
    with tempfile.TemporaryDirectory() as tmp:
        project, _, obligation, _, _ = _seed(Path(tmp), discharge=False)
        try:
            calls = []
            def execute():
                calls.append(1)
                assert project.runtime_snapshot().verifier_slots_in_use == 1
                ref = project.artifacts.put_text("real checker output fixture")
                return {"status": "PASS", "check_hash": ref.content_hash}, (ref.content_hash,)
            kwargs = dict(obligation_id=obligation.obligation_id, request_id="check-1",
                          mechanism="kernel-check", parameters={"source": "v1"}, timeout_ms=1000,
                          actor="checker", execute=execute)
            first = run_local_action(project, **kwargs)
            second = run_local_action(project, **kwargs)
            assert not first["replayed"] and second["replayed"] and len(calls) == 1
            assert project.runtime_snapshot().verifier_slots_in_use == 0
            assert project.research_view().get_obligation(obligation.obligation_id).status.value == "OPEN"
            with pytest.raises(LocalActionError, match="different inputs"):
                run_local_action(project, **{**kwargs, "parameters": {"source": "v2"}})
        finally:
            project.close()


def test_no_slot_defers_without_running_checker():
    with tempfile.TemporaryDirectory() as tmp:
        project, _, obligation, _, _ = _seed(Path(tmp), discharge=False, verifier_slots=0)
        try:
            def forbidden():
                raise AssertionError("checker must not run without a slot")
            result = run_local_action(project, obligation_id=obligation.obligation_id,
                request_id="no-slot", mechanism="kernel-check", parameters={}, timeout_ms=1000,
                actor="checker", execute=forbidden)
            assert result["status"] == "DEFERRED"
            assert not project.runtime_snapshot().leases
        finally:
            project.close()


def test_live_owner_is_not_duplicated_and_dead_owner_releases_slot():
    with tempfile.TemporaryDirectory() as tmp:
        project, _, obligation, _, _ = _seed(Path(tmp), discharge=False)
        try:
            nested_error = {}

            def execute():
                def forbidden():
                    raise AssertionError("duplicate checker")
                try:
                    run_local_action(
                        project,
                        obligation_id=obligation.obligation_id,
                        request_id="own-1",
                        mechanism="kernel-check",
                        parameters={"source": "v1"},
                        timeout_ms=1000,
                        actor="checker",
                        execute=forbidden,
                    )
                except LocalActionError as exc:
                    nested_error["msg"] = str(exc)
                else:
                    nested_error["msg"] = "missing"
                assert project.runtime_snapshot().verifier_slots_in_use == 1
                running = next(
                    item
                    for item in project.runtime_snapshot().leases
                    if item.status is LeaseStatus.RUNNING
                )
                from euler.control.local_actions import _recover_previous_lease
                recovered = None
                request_hash = sha256_hex(canonical_json({
                    "obligation": obligation.obligation_id,
                    "mechanism": "kernel-check",
                    "parameters": {"source": "v1"},
                    "timeout_ms": 1000,
                    "kind": "verify",
                    "budget_class": "NORMAL",
                    "requested": {
                        "cost_microusd": 0, "input_tokens": 0, "output_tokens": 0,
                        "reasoning_tokens": 0, "cached_tokens": 0, "wall_ms": 1000,
                        "tool_cpu_ms": 1000, "tool_gpu_ms": 0,
                    },
                    "priority_class": "DISCRIMINATING_CHEAP",
                }))
                with patch("euler.control.local_actions.owner_is_alive", return_value=False):
                    recovered = _recover_previous_lease(
                        project,
                        running,
                        request_id="own-1",
                        request_hash=request_hash,
                        actor="checker",
                        key="local-action:own-1",
                    )
                assert recovered is not None and recovered["status"] == "INFRA"
                assert recovered.get("recovered") is True
                assert project.runtime_snapshot().verifier_slots_in_use == 0
                return {"status": "INFRA", "detail": "recovered in fixture"}, ()

            first = run_local_action(
                project,
                obligation_id=obligation.obligation_id,
                request_id="own-1",
                mechanism="kernel-check",
                parameters={"source": "v1"},
                timeout_ms=1000,
                actor="checker",
                execute=execute,
            )
            assert "still running" in nested_error["msg"]
            assert first["status"] == "INFRA"
            assert owner_is_alive(owner_process_identity())
            assert project.runtime_snapshot().verifier_slots_in_use == 0
        finally:
            project.close()


def test_infra_result_replays_then_retries_with_new_request_id():
    with tempfile.TemporaryDirectory() as tmp:
        project, _, obligation, _, _ = _seed(Path(tmp), discharge=False)
        try:
            def infra():
                ref = project.artifacts.put_text("infra-output")
                return {"status": "INFRA", "detail": "checker timeout fixture"}, (ref.content_hash,)

            kwargs = dict(
                obligation_id=obligation.obligation_id,
                request_id="infra-1",
                mechanism="kernel-check",
                parameters={"source": "v1"},
                timeout_ms=1000,
                actor="checker",
                execute=infra,
            )
            first = run_local_action(project, **kwargs)
            assert first["status"] == "INFRA" and not first["replayed"]
            replay = run_local_action(project, **kwargs)
            assert replay["replayed"] and replay["status"] == "INFRA"
            assert project.runtime_snapshot().verifier_slots_in_use == 0

            def ok():
                ref = project.artifacts.put_text("retry-output")
                return {"status": "PASS", "check_hash": ref.content_hash}, (ref.content_hash,)

            retried = run_local_action(project, **{**kwargs, "request_id": "infra-2", "execute": ok})
            assert retried["status"] == "PASS" and not retried["replayed"]
            leases = project.runtime_snapshot().leases
            assert sum(1 for item in leases if item.status is LeaseStatus.COMPLETED) >= 1
            assert sum(1 for item in leases if item.status is LeaseStatus.INFRA_FAILED) >= 1
        finally:
            project.close()


def test_final_audit_class_reserves_protected_allowance():
    from euler.control.policy import BudgetClass, ResourceVector
    from euler.control.scheduler import ActionKind

    with tempfile.TemporaryDirectory() as tmp:
        project, _, obligation, _, _ = _seed(Path(tmp), discharge=False, cost=10_000)
        try:
            allowance = ResourceVector(
                cost_microusd=1, input_tokens=0, output_tokens=0, reasoning_tokens=0,
                cached_tokens=0, wall_ms=1000, tool_cpu_ms=0, tool_gpu_ms=0,
            )

            def execute():
                snap = project.runtime_snapshot()
                running = next(item for item in snap.leases if item.status is LeaseStatus.RUNNING)
                assert running.budget_class is BudgetClass.FINAL_AUDIT
                assert running.requested.cost_microusd == 1
                ref = project.artifacts.put_text("final-audit-local")
                return {"status": "PASS"}, (ref.content_hash,)

            result = run_local_action(
                project,
                obligation_id=obligation.obligation_id,
                request_id="fa-1",
                mechanism="refutation-audit",
                parameters={"k": 1},
                timeout_ms=1000,
                actor="checker",
                execute=execute,
                kind=ActionKind.VERIFY,
                budget_class=BudgetClass.FINAL_AUDIT,
                requested=allowance,
            )
            assert result["status"] == "PASS"
            done = next(item for item in project.runtime_snapshot().leases if item.lease_id == result["lease_id"])
            assert done.budget_class is BudgetClass.FINAL_AUDIT
            assert project.runtime_snapshot().resources.consumed_final_audit.cost_microusd >= 1
        finally:
            project.close()
