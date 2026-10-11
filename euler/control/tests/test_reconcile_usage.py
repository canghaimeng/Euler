"""UsageRecord settlement from native result receipts. Temp projects only."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from euler.control.context import ContextLimits
from euler.control.controller import EulerController
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.hooks import after_managed_round, before_managed_round
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.project import initialize_project
from euler.control.reconcile import ReconcileError
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import ObservationStatus
from euler.execution import layout as L
from euler.execution.launch_receipt import persist_launch_receipt, persist_result_receipt
from euler.execution.memory_digest import (
    finalize_round_end,
    hash_receipt_payload,
    prepare_round_start,
)

_TS = "2026-01-01T00:00:00Z"


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain="Z"),
        ),
        objects=(),
        parameter_domain="Z",
        definitions_hash=_hex("defs"),
    )


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="goldbach",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
        status=ObligationStatus.OPEN,
    )


def _policy() -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    return BudgetPolicy(
        cost_microusd=DimensionLimit.limited(10_000),
        input_tokens=unbound,
        output_tokens=unbound,
        reasoning_tokens=unbound,
        cached_tokens=unbound,
        wall_ms=unbound,
        tool_cpu_ms=unbound,
        tool_gpu_ms=unbound,
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _candidate(contract: ProblemContract, obligation: Obligation, tag: str) -> CandidateAction:
    spec, request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap="close the even-integer case",
        output_contract="return a KERNEL_CHECKED_PROOF",
        tool_environment_hash=_hex("tool"),
        acceptance_policy_hash=_hex("accept"),
        reference_versions_hash=_hex("refs"),
        limits=ContextLimits.unlimited(),
        action_type="prove",
        mechanism=tag,
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"k": tag}),
        dependency_hash=_hex("dep", tag),
        definitions_hash=_hex("defs"),
    )
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        mechanism_family=tag,
        action_kind=ActionKind.PROVE,
        slot_kind=slot_kind_for_action(ActionKind.PROVE),
        budget_class=BudgetClass.NORMAL,
        requested=ResourceVector(
            cost_microusd=500,
            input_tokens=1_000,
            output_tokens=200,
            reasoning_tokens=50,
            cached_tokens=10,
            wall_ms=1,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )


def _managed(tmp: Path):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": ["w1"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "workers" / "w1").mkdir(parents=True)
    contract = _contract()
    obligation = _obligation(contract)
    project = initialize_project(root, contract, obligation, _policy(), "tester", _TS)
    return project, contract, obligation


def _dispatch(project, contract, obligation):
    ctl = EulerController(project, actor="tester")
    ctl.propose(_candidate(contract, obligation, "alpha"), "w1", timestamp_utc=_TS)
    ctl.schedule(timestamp_utc=_TS)
    with patch("euler.control.controller.start_managed_worker", return_value="started"):
        binding = ctl.dispatch("alpha", timestamp_utc=_TS)
    return binding, L.WorkerLayout(project.project_dir / "workers" / "w1")


def _stream(*events) -> bytes:
    return ("\n".join(json.dumps(item) for item in events) + "\n").encode()


def _receipts_with_log(wl: L.WorkerLayout, data: bytes, *, provider: str = "codex", rc: int = 0):
    os.environ["EULER_RUN_ID"] = "run-usage-0001"
    start = prepare_round_start(wl, 1)
    launch = persist_launch_receipt(
        wl,
        start,
        "continue",
        {"MODEL": "m", "REASONING_EFFORT": "high", "PROVIDER": provider},
    )
    wl.logs.mkdir(parents=True, exist_ok=True)
    log = wl.logs / "round_1.log"
    log.write_bytes(data)
    result = persist_result_receipt(wl, launch, rc, 40, log_path=log)
    end = finalize_round_end(wl, start)
    return start, launch, result, end


def _settled_usage(project):
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type in {"runtime.lease_completed", "runtime.lease_infra_failed"}:
            return dict(envelope.payload)["usage"]
    raise AssertionError("no settled usage")


def _budget_actual(project):
    for _seq, envelope in project.event_store.iter_project(project.project_id):
        if envelope.event_type == "budget.settled":
            return dict(envelope.payload).get("actual_usage")
    return None


def test_native_result_settles_observed_tokens_and_keeps_unknown_dimensions():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            binding, wl = _dispatch(project, contract, obligation)
            start, launch, result, end = _receipts_with_log(
                wl,
                _stream({
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cached_input_tokens": 60,
                    },
                }),
            )
            before_managed_round(wl, start, launch)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            usage = _settled_usage(project)
            assert usage["input_tokens"]["status"] == ObservationStatus.OBSERVED.value
            assert usage["input_tokens"]["value"] == 100
            assert usage["output_tokens"]["value"] == 20
            assert usage["cached_tokens"]["value"] == 60
            assert usage["reasoning_tokens"]["status"] == ObservationStatus.UNKNOWN.value
            assert usage["cost_microusd"]["status"] == ObservationStatus.UNKNOWN.value
            assert usage["wall_ms"]["value"] == 40
            assert usage["tool_cpu_ms"]["status"] == ObservationStatus.UNKNOWN.value
            lease = next(
                item for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.status is LeaseStatus.COMPLETED
            assert lease.consumed.input_tokens == 100
            assert lease.consumed.output_tokens == 20
            assert lease.consumed.cached_tokens == 60
            # Unknown dimensions consume the reservation, they are not rewritten to 0.
            assert lease.consumed.reasoning_tokens == lease.requested.reasoning_tokens
            assert lease.consumed.cost_microusd == lease.requested.cost_microusd
            actual = _budget_actual(project)
            assert actual is not None
            assert actual["input_tokens"]["value"] == 100
            assert actual["cost_microusd"]["status"] == ObservationStatus.UNKNOWN.value
            assert actual["cost_microusd"]["value"] is None
        finally:
            project.close()


def test_grok_end_ticks_settle_cost_without_claiming_billed_usd():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            binding, wl = _dispatch(project, contract, obligation)
            start, launch, result, end = _receipts_with_log(
                wl,
                _stream(
                    {
                        "type": "usage",
                        "signature": "f1",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 40,
                            "reasoning_tokens": 1,
                        },
                    },
                    {
                        "type": "end",
                        "stopReason": "end_turn",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "cache_read_input_tokens": 40,
                            "cache_creation_input_tokens": 0,
                            "reasoning_tokens": 1,
                            "total_tokens": 55,
                        },
                        "total_cost_usd_ticks": 25000,
                    },
                ),
                provider="grok",
            )
            assert result["cost_usd"] is None
            assert result["native_usage"]["cost_microusd"] == 3
            before_managed_round(wl, start, launch)
            after_managed_round(wl, start, launch, result, end)
            usage = _settled_usage(project)
            assert usage["cost_microusd"]["status"] == ObservationStatus.OBSERVED.value
            assert usage["cost_microusd"]["value"] == 3
            assert usage["input_tokens"]["value"] == 12
            lease = next(
                item for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.consumed.cost_microusd == 3
        finally:
            project.close()


def test_replay_does_not_double_settle_usage():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            binding, wl = _dispatch(project, contract, obligation)
            start, launch, result, end = _receipts_with_log(
                wl,
                _stream({
                    "type": "turn.completed",
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                }),
            )
            before_managed_round(wl, start, launch)
            first = after_managed_round(wl, start, launch, result, end)
            second = after_managed_round(wl, start, launch, result, end)
            assert first.reconciliation_id == second.reconciliation_id
            settled = [
                envelope
                for _seq, envelope in project.event_store.iter_project(project.project_id)
                if envelope.event_type == "budget.settled"
            ]
            assert len(settled) == 1
            completed = [
                envelope
                for _seq, envelope in project.event_store.iter_project(project.project_id)
                if envelope.event_type == "runtime.lease_completed"
            ]
            assert len(completed) == 1
            lease = next(
                item for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.consumed.input_tokens == 7
        finally:
            project.close()


def test_schema1_result_replays_with_unknown_native_dimensions():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            binding, wl = _dispatch(project, contract, obligation)
            os.environ["EULER_RUN_ID"] = "run-usage-legacy"
            start = prepare_round_start(wl, 1)
            launch = persist_launch_receipt(
                wl, start, "continue",
                {"MODEL": "m", "REASONING_EFFORT": "high", "PROVIDER": "codex"},
            )
            result = {
                "schema_version": 1,
                "kind": "result",
                "round_id": 1,
                "run_id": launch.payload["run_id"],
                "project_id": launch.payload["project_id"],
                "worker_id": "w1",
                "launch_receipt_hash": launch.receipt_hash,
                "rc": 0,
                "elapsed_ms": 18,
                "completed_at": "2026-01-01T00:00:01Z",
                "usage_status": "not_reported",
                "tokens": None,
                "cost_usd": None,
            }
            result["receipt_hash"] = hash_receipt_payload(result)
            end = finalize_round_end(wl, start)
            before_managed_round(wl, start, launch)
            after_managed_round(wl, start, launch, result, end)
            usage = _settled_usage(project)
            assert usage["wall_ms"]["value"] == 18
            for name in (
                "input_tokens", "output_tokens", "reasoning_tokens",
                "cached_tokens", "cost_microusd",
            ):
                assert usage[name]["status"] == ObservationStatus.UNKNOWN.value
            lease = next(
                item for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.consumed.input_tokens == lease.requested.input_tokens
        finally:
            project.close()


def test_mismatched_native_provider_fails_before_settlement():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            _binding, wl = _dispatch(project, contract, obligation)
            start, launch, result, end = _receipts_with_log(
                wl,
                _stream({
                    "type": "turn.completed",
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                }),
            )
            result = dict(result)
            result["native_usage"] = dict(result["native_usage"])
            result["native_usage"]["provider"] = "grok"
            result["native_usage"]["output_format"] = "grok-streaming-json"
            result["receipt_hash"] = hash_receipt_payload(result)
            before_managed_round(wl, start, launch)
            try:
                after_managed_round(wl, start, launch, result, end)
                raise AssertionError("mismatched provider must fail")
            except ReconcileError:
                pass
            assert _budget_actual(project) is None
        finally:
            project.close()
