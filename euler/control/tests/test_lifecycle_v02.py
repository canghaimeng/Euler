"""Action-scoped lifecycle regressions for Euler v0.2 runtime.

Temp projects only. No live provider, verify HTTP, or Fact-graph writes from
the control plane. Facts are planted as files to simulate verifier admission.
"""

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
from euler.control.dispatch import hash_text, load_dispatch
from euler.control.hooks import (
    after_managed_round,
    before_managed_round,
    recover_pending_managed_round,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import DispatchBinding, EVENT_ROUND_BOUND
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
from euler.control.schema import EventEnvelope
from euler.execution import layout as L
from euler.execution.launch_receipt import persist_launch_receipt, persist_result_receipt
from euler.execution.memory_digest import finalize_round_end, prepare_round_start

_TS = "2026-01-01T00:00:00Z"
_FACT_A = "aaaaaaaaaaaaaaaa"
_FACT_B = "bbbbbbbbbbbbbbbb"
_FACT_C = "cccccccccccccccc"
_FACT_D = "dddddddddddddddd"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope(*, domain: str = "Z") -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain=domain),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
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


def _obligation(contract: ProblemContract, statement: str | None = None) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=statement or contract.exact_statement,
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
        max_reasoning_slots=4,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _candidate(
    contract: ProblemContract,
    obligation: Obligation,
    tag: str,
    *,
    required_fact_ids: tuple[str, ...] = (),
    route_id: str | None = None,
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
        required_fact_ids=required_fact_ids,
        route_id=route_id,
    )
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        mechanism_family=tag,
        route_id=route_id,
        action_kind=ActionKind.PROVE,
        slot_kind=slot_kind_for_action(ActionKind.PROVE),
        budget_class=BudgetClass.NORMAL,
        requested=ResourceVector(
            cost_microusd=1,
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=1,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        created_sequence=1,
        recurrence_audit_required=False,
    )


def _managed(tmp: Path, *, workers: tuple[str, ...] = ("w1", "w2")):
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
    project = initialize_project(root, contract, obligation, _policy(), "tester", _TS)
    return project, contract, obligation


def _add_child(project, contract, statement: str) -> Obligation:
    child = _obligation(contract, statement)
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
    return child


def _dispatch(project, contract, obligation, tag: str, worker: str = "w1", **cand_kw):
    ctl = EulerController(project, actor="tester")
    ctl.propose(_candidate(contract, obligation, tag, **cand_kw), worker, timestamp_utc=_TS)
    ctl.schedule(timestamp_utc=_TS)
    with patch("euler.control.controller.start_managed_worker", return_value="started"):
        binding = ctl.dispatch(tag, timestamp_utc=_TS)
    return binding, L.WorkerLayout(project.project_dir / "workers" / worker), ctl


def _receipts(wl: L.WorkerLayout, *, rc: int = 0, elapsed_ms: int = 12, round_id: int = 1):
    os.environ["EULER_RUN_ID"] = f"run-test-{round_id:04d}"
    start = prepare_round_start(wl, round_id)
    launch = persist_launch_receipt(
        wl, start, "continue", {"MODEL": "m", "REASONING_EFFORT": "high"}
    )
    result = persist_result_receipt(wl, launch, rc, elapsed_ms)
    end = finalize_round_end(wl, start)
    return start, launch, result, end


def _write_fact(root: Path, fact_id: str, statement: str) -> None:
    path = root / "fact_graph" / "facts" / f"{fact_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"fact_id: {fact_id}\n"
        "problem_id: goldbach\n"
        "author: tester\n"
        "predecessors: []\n"
        "glossary_introduces: {}\n"
        "external_refs: []\n"
        "---\n\n"
        "## statement\n"
        f"{statement}\n\n"
        "## proof\n"
        "By inspection.\n",
        encoding="utf-8",
    )


def _write_admission(root: Path, fact_id: str, statement: str) -> None:
    path = root / "global_memory" / "verification.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": f"ver-{fact_id}",
        "kind": "verification",
        "provenance": "fact_submit",
        "transport_status": "ok",
        "verdict": "correct",
        "fact_id": fact_id,
        "claim": statement,
        "write_error": None,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _revoke(root: Path, fact_id: str) -> None:
    src = root / "fact_graph" / "facts" / f"{fact_id}.md"
    dst = root / "fact_graph" / "_revoked"
    dst.mkdir(parents=True, exist_ok=True)
    src.replace(dst / f"{fact_id}.md")


def test_child_obligation_progress_keeps_root_open():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root_ob = _managed(Path(raw), workers=("w1",))
        try:
            child = _add_child(project, contract, "the Goldbach bridge lemma holds")
            binding, wl, _ctl = _dispatch(project, contract, child, "child")
            assert binding.obligation_id == child.obligation_id
            assert binding.obligation_id != root_ob.obligation_id
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, child.exact_statement)
            _write_admission(project.project_dir, _FACT_A, child.exact_statement)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "OBLIGATION_DISCHARGED"
            assert rec.fact_ids == (_FACT_A,)
            view = project.research_view()
            assert view.get_obligation(child.obligation_id).status is ObligationStatus.DISCHARGED
            assert view.get_obligation(root_ob.obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()


def test_unrelated_concurrent_fact_does_not_invalidate_start():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            binding, wl, _ctl = _dispatch(project, contract, obligation, "alpha")
            _write_fact(project.project_dir, _FACT_B, "an unrelated concurrent lemma")
            _write_admission(project.project_dir, _FACT_B, "an unrelated concurrent lemma")
            start, launch, result, end = _receipts(wl)
            again = before_managed_round(wl, start, launch)
            assert again.lease_id == binding.lease_id
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()


def test_revoked_required_dependency_rejects_start():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            _write_fact(project.project_dir, _FACT_C, "a required premise")
            _write_admission(project.project_dir, _FACT_C, "a required premise")
            _binding, wl, _ctl = _dispatch(
                project, contract, obligation, "dep", required_fact_ids=(_FACT_C,)
            )
            assert load_dispatch(project.project_dir, "w1")["binding"]["required_fact_ids"] == [_FACT_C]
            _revoke(project.project_dir, _FACT_C)
            start, launch, _result, _end = _receipts(wl)
            _raises(ReconcileError, before_managed_round, wl, start, launch)
        finally:
            project.close()


def test_same_worker_next_action_after_terminal_lease():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            first, wl, ctl = _dispatch(project, contract, obligation, "first")
            start, launch, result, end = _receipts(wl, round_id=1)
            before_managed_round(wl, start, launch)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == first.lease_id)
            assert lease.status is LeaseStatus.COMPLETED
            sidecar = load_dispatch(project.project_dir, "w1")
            assert sidecar is not None and sidecar["state"] == "settled"
            ctl.propose(_candidate(contract, obligation, "second"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            second = ctl.dispatch("second", timestamp_utc=_TS)
            assert second.lease_id != first.lease_id
            assert second.attempt_id != first.attempt_id
            ready = load_dispatch(project.project_dir, "w1")
            assert ready is not None and ready["state"] == "ready"
            start2, launch2, result2, end2 = _receipts(wl, round_id=2)
            before_managed_round(wl, start2, launch2)
            rec2 = after_managed_round(wl, start2, launch2, result2, end2)
            assert rec2.reconciliation_id != rec.reconciliation_id
            assert rec2.outcome_ref == "NO_VERIFIABLE_PROGRESS"
        finally:
            project.close()


def test_crash_reconciliation_uses_existing_receipts():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            binding, wl, _ctl = _dispatch(project, contract, obligation, "crash")
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, obligation.exact_statement)
            _write_admission(project.project_dir, _FACT_A, obligation.exact_statement)
            with patch.dict(os.environ, {"EULER_FINAL_AUDIT_RESOURCES": ""}), patch(
                "euler.control.final_audit.HttpVerifyTransport.verify",
                side_effect=AssertionError("unconfigured audit must not contact HTTP"),
            ):
                recovered = recover_pending_managed_round(wl)
            assert recovered.disposition == "recovered"
            assert recovered.reconciliation is not None
            assert recovered.reconciliation.outcome_ref == "OBLIGATION_DISCHARGED"
            again = after_managed_round(wl, start, launch, result, end)
            assert again.reconciliation_id == recovered.reconciliation.reconciliation_id
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == binding.lease_id)
            assert lease.status is LeaseStatus.COMPLETED
            blocked = [event for _, event in project.event_store.iter_project(project.project_id)
                       if event.event_type == "euler.auto_final_audit_blocked"]
            assert len(blocked) == 1
            assert "EULER_FINAL_AUDIT_RESOURCES" in blocked[0].payload["reason"]
        finally:
            project.close()


def test_concurrent_child_progress_does_not_close_root():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root_ob = _managed(Path(raw), workers=("w1", "w2"))
        try:
            left = _add_child(project, contract, "left Goldbach summand is prime")
            right = _add_child(project, contract, "right Goldbach summand is prime")
            b1, w1, _ctl = _dispatch(project, contract, left, "left", worker="w1")
            b2, w2, _ctl2 = _dispatch(project, contract, right, "right", worker="w2")
            assert b1.obligation_id == left.obligation_id
            assert b2.obligation_id == right.obligation_id
            s1, l1, r1, e1 = _receipts(w1, round_id=1)
            s2, l2, r2, e2 = _receipts(w2, round_id=1)
            before_managed_round(w1, s1, l1)
            before_managed_round(w2, s2, l2)
            _write_fact(project.project_dir, _FACT_A, left.exact_statement)
            _write_admission(project.project_dir, _FACT_A, left.exact_statement)
            _write_fact(project.project_dir, _FACT_B, right.exact_statement)
            _write_admission(project.project_dir, _FACT_B, right.exact_statement)
            rec1 = after_managed_round(w1, s1, l1, r1, e1)
            rec2 = after_managed_round(w2, s2, l2, r2, e2)
            assert rec1.outcome_ref == "OBLIGATION_DISCHARGED"
            assert rec2.outcome_ref == "OBLIGATION_DISCHARGED"
            view = project.research_view()
            assert view.get_obligation(left.obligation_id).status is ObligationStatus.DISCHARGED
            assert view.get_obligation(right.obligation_id).status is ObligationStatus.DISCHARGED
            assert view.get_obligation(root_ob.obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()


def test_no_useful_result_and_multiple_admissible_facts():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, contract, obligation = _managed(tmp / "none", workers=("w1",))
        try:
            _binding, wl, _ctl = _dispatch(project, contract, obligation, "empty")
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert rec.fact_ids == ()
        finally:
            project.close()

        project, contract, obligation = _managed(tmp / "multi", workers=("w1",))
        try:
            _binding, wl, _ctl = _dispatch(project, contract, obligation, "multi")
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, obligation.exact_statement)
            _write_admission(project.project_dir, _FACT_A, obligation.exact_statement)
            _write_fact(project.project_dir, _FACT_D, obligation.exact_statement)
            _write_admission(project.project_dir, _FACT_D, obligation.exact_statement)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "OBLIGATION_DISCHARGED"
            assert set(rec.fact_ids) == {_FACT_A, _FACT_D}
            assert len(rec.evidence_ids) == 2
            assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.DISCHARGED
        finally:
            project.close()


def test_historical_lookup_and_legacy_sidecar_migration():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            first, wl, ctl = _dispatch(project, contract, obligation, "old")
            start, launch, result, end = _receipts(wl, round_id=1)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, obligation.exact_statement)
            _write_admission(project.project_dir, _FACT_A, obligation.exact_statement)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "OBLIGATION_DISCHARGED"
            ctl.propose(_candidate(contract, obligation, "new"), "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            second = ctl.dispatch("new", timestamp_utc=_TS)
            assert second.lease_id != first.lease_id
            sidecar = load_dispatch(project.project_dir, "w1")
            assert sidecar["binding"]["lease_id"] == second.lease_id
            replayed = after_managed_round(wl, start, launch, result, end)
            assert replayed.reconciliation_id == rec.reconciliation_id
            assert replayed.lease_id == first.lease_id
            types = [
                envelope.event_type
                for _seq, envelope in project.event_store.iter_project(project.project_id)
            ]
            assert types.count(EVENT_ROUND_BOUND) >= 1
            v1 = {
                "intent_id": first.intent_id,
                "ready_id": first.ready_id,
                "worker": first.worker,
                "candidate_id": first.candidate_id,
                "action_id": first.action_id,
                "lease_id": first.lease_id,
                "attempt_id": first.attempt_id,
                "packet_hash": first.packet_hash,
                "cache_key_hash": first.cache_key_hash,
                "task_hash": first.task_hash,
                "baseline_fact_ids": list(first.baseline_fact_ids),
                "frontier_hash": first.frontier_hash,
            }
            loaded = DispatchBinding.from_dict(v1)
            assert loaded.obligation_id is None
            assert loaded.required_fact_ids == ()
            assert loaded.lease_id == first.lease_id
        finally:
            project.close()


def test_legacy_sidecar_without_optional_fields_accepts_unrelated_facts():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            # Produce legacy records at both authority and sidecar boundaries.
            # Removing fields from only a modern sidecar is a forbidden downgrade.
            optional_fields = (
                "obligation_id",
                "scope_hash",
                "dependency_hash",
                "context_version_hash",
                "required_fact_ids",
                "required_fact_versions",
                "required_evidence_ids",
                "baseline_evidence_ids",
                "baseline_relation_ids",
            )
            current_encoder = DispatchBinding.to_dict

            def legacy_encoder(record):
                return {key: value for key, value in current_encoder(record).items()
                        if key not in optional_fields}

            with patch.object(DispatchBinding, "to_dict", legacy_encoder):
                binding, wl, _ctl = _dispatch(project, contract, obligation, "legacy")
            payload = load_dispatch(project.project_dir, "w1")
            assert not set(optional_fields) & set(payload["binding"])
            _write_fact(project.project_dir, _FACT_B, "unrelated after legacy sidecar")
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert binding.lease_id == rec.lease_id
        finally:
            project.close()


def main() -> None:
    tests = [
        test_child_obligation_progress_keeps_root_open,
        test_unrelated_concurrent_fact_does_not_invalidate_start,
        test_revoked_required_dependency_rejects_start,
        test_same_worker_next_action_after_terminal_lease,
        test_crash_reconciliation_uses_existing_receipts,
        test_concurrent_child_progress_does_not_close_root,
        test_no_useful_result_and_multiple_admissible_facts,
        test_historical_lookup_and_legacy_sidecar_migration,
        test_legacy_sidecar_without_optional_fields_accepts_unrelated_facts,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL LIFECYCLE V02 TESTS PASSED")


if __name__ == "__main__":
    main()
