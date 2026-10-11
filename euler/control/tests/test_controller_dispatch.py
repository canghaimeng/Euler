"""Focused tests for Gate 3B scheduler/context/lease mandatory dispatch.

Temp projects only. No live provider, verify HTTP, or Fact admission.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from euler.control.context import ContextLimits
from euler.control.controller import EulerController, EulerControllerError
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.dispatch import (
    DispatchError,
    DispatchUnprepared,
    load_dispatch,
    recover_unready_intent,
    validate_managed_start,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import (
    EVENT_DISPATCH_INTENT,
    EVENT_DISPATCH_PLANNED,
    EVENT_DISPATCH_READY,
)
from euler.control.schema import EventEnvelope
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.project import initialize_project, is_euler_project
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    ScheduleDisposition,
    ScheduleReason,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.execution import layout as L
from euler.execution import scaffold
from euler.orchestration import cli

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


def _policy(*, cost: int | None = 10_000, reasoning_slots: int = 2) -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    cost_limit = unbound if cost is None else DimensionLimit.limited(cost)
    return BudgetPolicy(
        cost_microusd=cost_limit,
        input_tokens=unbound,
        output_tokens=unbound,
        reasoning_tokens=unbound,
        cached_tokens=unbound,
        wall_ms=unbound,
        tool_cpu_ms=unbound,
        tool_gpu_ms=unbound,
        max_reasoning_slots=reasoning_slots,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _candidate(
    contract: ProblemContract,
    obligation: Obligation,
    tag: str,
    *,
    cost: int = 1,
    validity: DependencyValidity = DependencyValidity.VALID,
) -> CandidateAction:
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
            cost_microusd=cost,
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=1,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=validity,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )


def _managed(tmp: Path, *, workers: tuple[str, ...] = ("w1", "w2"), **policy_kw):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": list(workers)}, indent=2) + "\n",
        encoding="utf-8",
    )
    for name in workers:
        (root / "workers" / name).mkdir(parents=True)
    contract = _contract()
    obligation = _obligation(contract)
    policy = _policy(**policy_kw)
    project = initialize_project(root, contract, obligation, policy, "tester", _TS)
    return project, contract, obligation


@contextmanager
def _project_env(tmp: Path):
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    env = {
        "EULER_AGENTS_ROOT": str(tmp / "agents"),
        "EULER_WORKER_CONTRACT": str(contract),
        "EULER_WORKER_SKILLS": str(skills),
    }
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _event_types(project) -> list[str]:
    return [
        envelope.event_type
        for _seq, envelope in project.event_store.iter_project(project.project_id)
    ]


def test_schedule_selected_deferred_budget_slot_stale():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, contract, obligation = _managed(tmp, cost=5, reasoning_slots=1)
        try:
            ctl = EulerController(project, actor="tester")
            good = _candidate(contract, obligation, "good")
            stale = _candidate(
                contract, obligation, "stale", validity=DependencyValidity.STALE
            )
            hungry = _candidate(contract, obligation, "hungry", cost=10_000)
            ctl.propose(good, "w1", timestamp_utc=_TS)
            ctl.propose(stale, "w2", timestamp_utc=_TS)
            ctl.propose(hungry, "w1", timestamp_utc=_TS)
            plan = ctl.schedule(timestamp_utc=_TS)
            by_id = {item.candidate_id: item for item in plan.decisions}
            assert by_id["good"].disposition is ScheduleDisposition.SELECTED
            assert by_id["stale"].disposition is ScheduleDisposition.REJECTED
            assert by_id["stale"].reason is ScheduleReason.STALE_DEPENDENCY
            assert by_id["hungry"].disposition is not ScheduleDisposition.SELECTED
            assert by_id["hungry"].reason in {
                ScheduleReason.BUDGET_EXCEEDED,
                ScheduleReason.NOT_SELECTED,
                ScheduleReason.CLASS_RESERVE,
            }
            types = _event_types(project)
            assert types.count(EVENT_DISPATCH_PLANNED) == 1
            again = ctl.schedule(timestamp_utc=_TS)
            assert again.to_dict() == plan.to_dict()
            assert _event_types(project).count(EVENT_DISPATCH_PLANNED) == 1
        finally:
            project.close()

        project, contract, obligation = _managed(
            tmp / "slots", workers=("w1",), reasoning_slots=0
        )
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "slot"), "w1", timestamp_utc=_TS)
            plan = ctl.schedule(timestamp_utc=_TS)
            decision = plan.decision_for("slot")
            assert decision.disposition is ScheduleDisposition.DEFERRED
            assert decision.reason is ScheduleReason.SLOT_EXHAUSTED
        finally:
            project.close()


def test_dispatch_writes_packet_lease_intent_ready():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            cand = _candidate(contract, obligation, "alpha")
            ctl.propose(cand, "w1", timestamp_utc=_TS)
            plan = ctl.schedule(timestamp_utc=_TS)
            assert "alpha" in plan.selected_candidate_ids
            launches = []
            with patch(
                "euler.control.controller.start_managed_worker",
                side_effect=lambda *a, **k: launches.append(a) or "started",
            ):
                binding = ctl.dispatch("alpha", timestamp_utc=_TS)
            assert launches == []
            assert binding.ready_id
            task = (project.project_dir / "workers" / "w1" / "TASK.md").read_text(
                encoding="utf-8"
            )
            assert "return a KERNEL_CHECKED_PROOF" in task
            assert cand.context_request.output_contract in task
            assert binding.lease_id in task
            assert binding.action_id in task
            from euler.control.adapters.project import snapshot_project
            from euler.control.context import ContextCompiler

            packet = ContextCompiler().compile(
                cand.context_request,
                project.research_view(),
                snapshot_project(project.project_dir),
                project.artifacts,
            )
            assert packet.rendered_text.strip() in task
            sidecar = load_dispatch(project.project_dir, "w1")
            assert sidecar is not None and sidecar["state"] == "ready"
            types = _event_types(project)
            assert EVENT_DISPATCH_INTENT in types
            assert EVENT_DISPATCH_READY in types
            snap = project.runtime_snapshot()
            lease = next(item for item in snap.leases if item.lease_id == binding.lease_id)
            assert lease.status is LeaseStatus.ISSUED
            assert lease.context_cache_key == binding.cache_key_hash
            again = ctl.dispatch("alpha", timestamp_utc=_TS)
            assert again.lease_id == binding.lease_id
            reserved = [
                item
                for item in project.runtime_snapshot().leases
                if item.status is LeaseStatus.ISSUED
            ]
            assert len(reserved) == 1
            validate_managed_start(project.project_dir, "w1")
        finally:
            project.close()


def test_dispatch_rejects_unselected_and_duplicate_worker():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "ok"), "w1", timestamp_utc=_TS)
            ctl.propose(
                _candidate(
                    contract, obligation, "bad", validity=DependencyValidity.STALE
                ),
                "w2",
                timestamp_utc=_TS,
            )
            ctl.schedule(timestamp_utc=_TS)
            try:
                ctl.dispatch("bad", timestamp_utc=_TS)
                raise AssertionError("unselected dispatch must fail")
            except EulerControllerError:
                pass
            ctl.dispatch("ok", timestamp_utc=_TS)
            ctl.propose(_candidate(contract, obligation, "other"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            try:
                ctl.dispatch("other", timestamp_utc=_TS)
                raise AssertionError("second active binding on w1 must fail")
            except EulerControllerError:
                pass
        finally:
            project.close()


def test_terminal_dispatch_can_be_replaced_without_rewriting_history():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "first"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            ctl.dispatch("first", timestamp_utc=_TS)
            old = project.runtime_snapshot().leases[0]
            project.runtime.mark_running(project.project_id, old.lease_id, timestamp_utc=_TS)
            project.runtime.fail_infrastructure(
                project.project_id, old.lease_id, reason="network failure", timestamp_utc=_TS,
            )
            ctl.propose(_candidate(contract, obligation, "retry"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            ctl.dispatch("retry", timestamp_utc=_TS)
            validate_managed_start(project.project_dir, "w1")
            leases = project.runtime_snapshot().leases
            assert len(leases) == 2
            assert next(x for x in leases if x.lease_id == old.lease_id).status is LeaseStatus.INFRA_FAILED
            assert len(project.runtime_snapshot().active_lease_ids) == 1
        finally:
            project.close()


def test_withdrawal_blocks_stale_plan_dispatch_and_preserves_other_proposals():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "old"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            ctl.withdraw("old", reason="replaced assignment", timestamp_utc=_TS)
            try:
                ctl.dispatch("old", timestamp_utc=_TS)
                raise AssertionError("withdrawn proposal must not dispatch from an old plan")
            except EulerControllerError:
                pass
            ctl.propose(_candidate(contract, obligation, "new"), "w1", timestamp_utc=_TS)
            plan = ctl.schedule(timestamp_utc=_TS)
            assert plan.selected_candidate_ids == ("new",)
            ctl.dispatch("new", timestamp_utc=_TS)
            try:
                ctl.withdraw("new", reason="cannot retire active work", timestamp_utc=_TS)
                raise AssertionError("active dispatch must not be withdrawn")
            except EulerControllerError:
                pass
            assert "euler.candidate_withdrawn" in _event_types(project)
        finally:
            project.close()


def test_file_failure_releases_lease_and_does_not_launch():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "alpha"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            launches = []
            with patch(
                "euler.control.controller.atomic_write_task_and_sidecar",
                side_effect=OSError("disk full"),
            ), patch(
                "euler.control.controller.start_managed_worker",
                side_effect=lambda *a, **k: launches.append(True),
            ):
                try:
                    ctl.dispatch("alpha", launch=True, timestamp_utc=_TS)
                    raise AssertionError("file failure must fail closed")
                except EulerControllerError as exc:
                    assert "lease released" in str(exc)
            assert launches == []
            snap = project.runtime_snapshot()
            assert snap.leases
            assert all(item.status is LeaseStatus.CANCELLED for item in snap.leases)
            try:
                validate_managed_start(project.project_dir, "w1")
                raise AssertionError("unprepared start must fail")
            except DispatchUnprepared:
                pass
        finally:
            project.close()


def test_ready_sidecar_tamper_and_crash_recovery():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "alpha"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            ctl.dispatch("alpha", timestamp_utc=_TS)
            task = project.project_dir / "workers" / "w1" / "TASK.md"
            task.write_text(task.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
            try:
                validate_managed_start(project.project_dir, "w1")
                raise AssertionError("tampered TASK must fail")
            except DispatchError:
                pass
        finally:
            project.close()

        project, contract, obligation = _managed(Path(raw) / "rec")
        try:
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, obligation, "beta"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            with patch(
                "euler.control.controller.atomic_write_sidecar",
                side_effect=OSError("crash before ready"),
            ):
                try:
                    ctl.dispatch("beta", timestamp_utc=_TS)
                except EulerControllerError:
                    pass
            recovered = recover_unready_intent(
                project.project_dir, "w1", actor="tester", timestamp_utc=_TS
            )
            snap = project.runtime_snapshot()
            assert all(item.status is not LeaseStatus.ISSUED for item in snap.leases)
            try:
                validate_managed_start(project.project_dir, "w1")
                raise AssertionError("recovered worker must not start")
            except DispatchUnprepared:
                pass
            assert recovered is not None or snap.leases
        finally:
            project.close()


def test_public_assign_and_start_rejected_for_euler_projects():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with _project_env(tmp):
            scaffold.do_new("P", roles="high:1", profile="codex")
            pdir = L.project_dir("P")
            contract = _contract()
            obligation = _obligation(contract)
            proj = initialize_project(
                pdir, contract, obligation, _policy(), "tester", _TS
            )
            try:
                assert is_euler_project(pdir)
                try:
                    cli.do_assign("P/high", "prove it\n", branch_id="b1", subgoal_id="s1")
                    raise AssertionError("direct assign must be rejected")
                except SystemExit as exc:
                    assert "Euler-managed" in str(exc)
                spawned = []
                with patch("euler.orchestration.cli.spawn_loop", side_effect=lambda d: spawned.append(d) or 1):
                    rows = cli.do_start("P/high", stagger=0)
                assert rows[0]["result"] == "unprepared"
                assert spawned == []
                ctl = EulerController(proj, actor="tester")
                cand = _candidate(contract, obligation, "go")
                ctl.propose(cand, "high", timestamp_utc=_TS)
                ctl.schedule(timestamp_utc=_TS)
                ctl.dispatch("go", timestamp_utc=_TS)
                with patch(
                    "euler.orchestration.cli.run_proving_preflight",
                    return_value=type("P", (), {"ok": True, "to_status_dict": lambda self: {}})(),
                ), patch(
                    "euler.orchestration.cli.spawn_loop",
                    side_effect=lambda d: spawned.append(d) or 99,
                ):
                    rows = cli.do_start("P/high", stagger=0)
                assert rows[0]["result"] == "started"
                assert spawned == [L.worker_dir("P", "high")]
            finally:
                proj.close()


def test_dispatch_binds_child_obligation_not_root():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root_ob = _managed(Path(raw), workers=("w1",))
        try:
            child = Obligation.build(
                contract_id=contract.contract_id,
                exact_statement="the Goldbach bridge lemma holds",
                scope=contract.scope,
                desired_output="proof",
                created_by="tester",
                version=1,
                status=ObligationStatus.OPEN,
            )
            project.event_store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor="tester",
                    event_type="obligation.created",
                    idempotency_key=f"obligation:{child.obligation_id}",
                    payload={"obligation": child.to_dict()},
                    timestamp_utc=_TS,
                    correlation_id=project.project_id,
                )
            )
            ctl = EulerController(project, actor="tester")
            ctl.propose(_candidate(contract, child, "child"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            binding = ctl.dispatch("child", timestamp_utc=_TS)
            assert binding.obligation_id == child.obligation_id
            assert binding.obligation_id != root_ob.obligation_id
            assert binding.scope_hash
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == binding.lease_id)
            assert lease.obligation_id == child.obligation_id
        finally:
            project.close()


def test_legacy_assign_still_works_without_control_metadata():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with _project_env(tmp):
            scaffold.do_new("Q", roles="high:1", profile="codex")
            out = cli.do_assign("Q/high", "legacy task\n", branch_id="b1", subgoal_id="s1")
            text = Path(out["task_file"]).read_text(encoding="utf-8")
            assert "legacy task" in text
            assert not is_euler_project(L.project_dir("Q"))
