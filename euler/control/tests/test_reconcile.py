"""Focused tests for managed-round hooks and idempotent reconciliation."""

from __future__ import annotations

import ast
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from euler.control.context import ContextLimits
from euler.control.event_store import EventStore
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
from euler.control.live_records import EVENT_ROUND_RECONCILED
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
_ROOT = Path(__file__).resolve().parents[1]
_FACT_A = "aaaaaaaaaaaaaaaa"
_FACT_B = "bbbbbbbbbbbbbbbb"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


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


def _contract(*, evidence=EvidenceKind.LLM_CHECKED_PROOF) -> ProblemContract:
    return ProblemContract.build(
        problem_id="goldbach",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=evidence,
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
        recurrence_audit_required=False,
        created_sequence=1,
    )


def _managed(tmp: Path, *, evidence=EvidenceKind.LLM_CHECKED_PROOF):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": ["w1"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "workers" / "w1").mkdir(parents=True)
    contract = _contract(evidence=evidence)
    obligation = _obligation(contract)
    project = initialize_project(root, contract, obligation, _policy(), "tester", _TS)
    return project, contract, obligation


def _dispatch_ready(project, contract, obligation):
    ctl = EulerController(project, actor="tester")
    cand = _candidate(contract, obligation, "alpha")
    ctl.propose(cand, "w1", timestamp_utc=_TS)
    ctl.schedule(timestamp_utc=_TS)
    with patch("euler.control.controller.start_managed_worker", return_value="started"):
        binding = ctl.dispatch("alpha", timestamp_utc=_TS)
    return binding, L.WorkerLayout(project.project_dir / "workers" / "w1")


def _receipts(wl: L.WorkerLayout, *, rc: int = 0, elapsed_ms: int = 12):
    os.environ["EULER_RUN_ID"] = "run-test-0001"
    start = prepare_round_start(wl, 1)
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


def _write_admission(
    root: Path,
    fact_id: str,
    statement: str,
    *,
    provenance: str = "fact_submit",
    transport_status: str = "ok",
    verdict: str = "correct",
    write_error=None,
) -> None:
    path = root / "global_memory" / "verification.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": f"ver-{fact_id}",
        "kind": "verification",
        "provenance": provenance,
        "transport_status": transport_status,
        "verdict": verdict,
        "fact_id": fact_id,
        "claim": statement,
        "write_error": write_error,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _event_types(project) -> list[str]:
    return [
        envelope.event_type
        for _seq, envelope in project.event_store.iter_project(project.project_id)
    ]


def test_happy_discharge_and_duplicate_hook():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            first = before_managed_round(wl, start, launch)
            assert first.lease_id == binding.lease_id
            again = before_managed_round(wl, start, launch)
            assert again.lease_id == binding.lease_id
            lease = next(
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.status is LeaseStatus.RUNNING
            _write_fact(project.project_dir, _FACT_A, contract.exact_statement)
            _write_admission(project.project_dir, _FACT_A, contract.exact_statement)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec is not None
            assert rec.outcome_ref == "OBLIGATION_DISCHARGED"
            assert rec.fact_ids == (_FACT_A,)
            view = project.research_view()
            target = view.get_obligation(obligation.obligation_id)
            assert target is not None and target.status is ObligationStatus.DISCHARGED
            dup = after_managed_round(wl, start, launch, result, end)
            assert dup.reconciliation_id == rec.reconciliation_id
            assert _event_types(project).count(EVENT_ROUND_RECONCILED) == 1
            completed = [
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            ]
            assert completed[0].status is LeaseStatus.COMPLETED
        finally:
            project.close()


def test_no_fact_unrelated_and_bad_admissions():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, contract, obligation = _managed(tmp / "none")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec is not None
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert rec.fact_ids == ()
            target = project.research_view().get_obligation(obligation.obligation_id)
            assert target is not None and target.status is ObligationStatus.OPEN
        finally:
            project.close()

        project, contract, obligation = _managed(tmp / "unrelated")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_B, "some other lemma")
            _write_admission(project.project_dir, _FACT_B, "some other lemma")
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()

        project, contract, obligation = _managed(tmp / "foreign")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, contract.exact_statement)
            _write_admission(
                project.project_dir,
                _FACT_A,
                contract.exact_statement,
                provenance="manual",
            )
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
        finally:
            project.close()

        project, contract, obligation = _managed(tmp / "missing")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, contract.exact_statement)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
        finally:
            project.close()


def test_rc_failure_unknown_usage_and_mismatched_receipts():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl, rc=1, elapsed_ms=33)
            before_managed_round(wl, start, launch)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "INFRA_FAILED"
            lease = next(
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.status is LeaseStatus.INFRA_FAILED
            usage = None
            for _seq, env in project.event_store.iter_project(project.project_id):
                if env.event_type == "runtime.lease_infra_failed":
                    usage = dict(env.payload)["usage"]
            assert usage is not None
            assert usage["wall_ms"]["status"] == ObservationStatus.OBSERVED.value
            assert usage["wall_ms"]["value"] == 33
            for name in (
                "cost_microusd",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "tool_cpu_ms",
                "tool_gpu_ms",
            ):
                assert usage[name]["status"] == ObservationStatus.UNKNOWN.value
                assert usage[name]["value"] is None
            view = project.research_view()
            assert view.get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
            dup = after_managed_round(wl, start, launch, result, end)
            assert dup.reconciliation_id == rec.reconciliation_id
        finally:
            project.close()


def test_tamper_mismatch_stronger_evidence_rollback_legacy():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, contract, obligation = _managed(tmp / "tamper")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            bad = dict(result)
            bad["receipt_hash"] = "0" * 64
            _raises(Exception, after_managed_round, wl, start, launch, bad, end)
            mismatch = dict(result)
            mismatch["worker_id"] = "other"
            mismatch["receipt_hash"] = hash_receipt_payload(mismatch)
            _raises(ReconcileError, after_managed_round, wl, start, launch, mismatch, end)
        finally:
            project.close()

        project, contract, obligation = _managed(
            tmp / "kernel", evidence=EvidenceKind.KERNEL_CHECKED_PROOF
        )
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, contract.exact_statement)
            _write_admission(project.project_dir, _FACT_A, contract.exact_statement)
            rec = after_managed_round(wl, start, launch, result, end)
            assert rec.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()

        project, contract, obligation = _managed(tmp / "rollback")
        try:
            binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            _write_fact(project.project_dir, _FACT_A, contract.exact_statement)
            _write_admission(project.project_dir, _FACT_A, contract.exact_statement)
            real_append = EventStore.append

            def _boom(self, envelope):
                if envelope.event_type == EVENT_ROUND_RECONCILED:
                    raise RuntimeError("injected rollback")
                return real_append(self, envelope)

            EventStore.append = _boom  # type: ignore[method-assign]
            try:
                _raises(ReconcileError, after_managed_round, wl, start, launch, result, end)
            finally:
                EventStore.append = real_append  # type: ignore[method-assign]
            assert EVENT_ROUND_RECONCILED not in _event_types(project)
            assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
            lease = next(
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == binding.lease_id
            )
            assert lease.status is LeaseStatus.RUNNING
        finally:
            project.close()

        bare = tmp / "legacy" / "proj" / "workers" / "w1"
        bare.mkdir(parents=True)
        wl = L.WorkerLayout(bare)
        assert before_managed_round(wl, {"round_id": 1}, {"kind": "launch", "round_id": 1}) is None
        assert after_managed_round(wl, {"round_id": 1}, {"kind": "launch"}, {"kind": "result"}, bare) is None


def test_before_rejects_terminal_and_task_tamper():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        project, contract, obligation = _managed(tmp / "term")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, result, end = _receipts(wl, rc=1)
            before_managed_round(wl, start, launch)
            after_managed_round(wl, start, launch, result, end)
            _raises(ReconcileError, before_managed_round, wl, start, launch)
        finally:
            project.close()
        project, contract, obligation = _managed(tmp / "task")
        try:
            _binding, wl = _dispatch_ready(project, contract, obligation)
            start, launch, _result, _end = _receipts(wl)
            wl.task.write_text(wl.task.read_text(encoding="utf-8") + "\n# tamper\n", encoding="utf-8")
            _raises(ReconcileError, before_managed_round, wl, start, launch)
        finally:
            project.close()


def test_production_modules_isolated_from_truth_and_providers():
    forbidden_modules = {
        "euler.core.factgraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.verify",
        "euler.providers",
        "euler.orchestration",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("reconcile.py",):
        path = _ROOT / filename
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported: set[str] = set()
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    named.add(alias.name)
        assert imported.isdisjoint(forbidden_modules), (filename, imported)
        assert named.isdisjoint(forbidden_names), (filename, named & forbidden_names)


def main() -> None:
    tests = [
        test_happy_discharge_and_duplicate_hook,
        test_no_fact_unrelated_and_bad_admissions,
        test_rc_failure_unknown_usage_and_mismatched_receipts,
        test_tamper_mismatch_stronger_evidence_rollback_legacy,
        test_before_rejects_terminal_and_task_tamper,
        test_production_modules_isolated_from_truth_and_providers,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL RECONCILE TESTS PASSED")


if __name__ == "__main__":
    main()
