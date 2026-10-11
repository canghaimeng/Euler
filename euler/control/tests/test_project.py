"""Focused tests for managed Euler project persistence and live records.

Temp dirs only. No provider launch, TASK writes, Fact Graph, gateway, or verify.

Runs standalone (``python -m euler.control.tests.test_project``) and under pytest.
"""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import (
    EVENT_CANDIDATE_PROPOSED,
    ControlManifest,
    ControlStatus,
    DispatchBinding,
    ProposedCandidate,
    ReconciliationResult,
)
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.project import (
    EulerProjectConflict,
    EulerProjectError,
    EulerProjectIncomplete,
    initialize_project,
    is_euler_project,
    open_project,
)
from euler.control.context import ContextLimits
from euler.control.policy import BudgetClass
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import BudgetLimit, BudgetLimitKind, EventEnvelope, SchemaValidationError
from euler.control.views import InvalidResearchTransition

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_HEX16 = "0123456789abcdef"


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


def _policy(*, cost: int | None = 10_000) -> BudgetPolicy:
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
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _candidate(tag: str) -> CandidateAction:
    spec, request, seed = build_action_context_pair(
        contract_id=_hex("contract", tag),
        obligation_id=_hex("obligation", tag),
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
        parameters_json=canonical_json({"k": 1}),
        dependency_hash=_hex("dep"),
        definitions_hash=_hex("defs"),
    )
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=request.obligation_id,
        route_id=request.route_id,
        mechanism_family=spec.mechanism,
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


def _bare_project(tmp: Path, name: str = "demo", workers: tuple[str, ...] = ("w1", "w2")) -> Path:
    root = tmp / name
    root.mkdir()
    (root / "project.json").write_text(
        json.dumps({"name": name, "workers": list(workers)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def test_initialize_open_reopen_is_idempotent():
    with tempfile.TemporaryDirectory() as raw:
        root = _bare_project(Path(raw))
        contract = _contract()
        obligation = _obligation(contract)
        policy = _policy()
        first = initialize_project(
            root, contract, obligation, policy, "tester", _TS
        )
        try:
            assert is_euler_project(root)
            assert first.control_dir == root / "control"
            assert (root / "control" / "control.sqlite3").is_file()
            assert (root / "control" / "artifacts").is_dir()
            assert (root / "control" / "dispatch").is_dir()
            assert (root / "control" / "policy.json").is_file()
            assert (root / "control" / "manifest.json").is_file()
            meta = json.loads((root / "project.json").read_text(encoding="utf-8"))
            assert meta["control_plane"] == "euler"
            assert meta["euler_control"]["manifest_hash"] == first.manifest.manifest_hash
            assert meta["euler_control"]["control_dir"] == "control"
            status_a = first.status()
            view = first.research_view()
            assert len(view.contracts()) == 1
            assert view.contracts()[0].contract_id == contract.contract_id
            assert len(view.obligations()) == 1
            assert view.obligations()[0].obligation_id == obligation.obligation_id
            assert not (root / "fact_graph").exists()
            assert not (root / "TASK.md").exists()
            assert list(first.control_dir.glob("*.json")) 
        finally:
            first.close()
        second = initialize_project(
            root, contract, obligation, policy, "tester", _TS
        )
        try:
            assert second.manifest.manifest_hash == status_a.manifest_hash
            assert second.status().status_hash == status_a.status_hash
        finally:
            second.close()
        third = open_project(root)
        try:
            assert third.status().status_hash == status_a.status_hash
            assert third.project_id == "demo"
        finally:
            third.close()


def test_conflict_and_partial_state_fail_closed():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        root = _bare_project(tmp)
        contract = _contract()
        obligation = _obligation(contract)
        policy = _policy()
        proj = initialize_project(root, contract, obligation, policy, "tester", _TS)
        proj.close()
        other = ProblemContract.build(
            problem_id="other",
            version=1,
            exact_statement="a different statement",
            scope=_scope(),
            permitted_tools=("lean",),
            external_help_policy="none",
            target_kind=TargetKind.PROOF,
            required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
            completion_checks=("proof checked",),
            budget_policy_ref="default-budget",
        )
        other_ob = _obligation(other)
        _raises(
            EulerProjectConflict,
            initialize_project,
            root,
            other,
            other_ob,
            policy,
            "tester",
            _TS,
        )
        partial = _bare_project(tmp, name="partial")
        (partial / "control").mkdir()
        (partial / "control" / "policy.json").write_text("{}", encoding="utf-8")
        _raises(
            EulerProjectIncomplete,
            initialize_project,
            partial,
            contract,
            obligation,
            policy,
            "tester",
            _TS,
        )
        _raises(EulerProjectIncomplete, open_project, partial)
        assert is_euler_project(partial) is False
        missing = tmp / "no-json"
        missing.mkdir()
        _raises(EulerProjectError, initialize_project, missing, contract, obligation, policy, "tester", _TS)


def test_filtered_replay_skips_unrelated_and_fails_malformed_research():
    with tempfile.TemporaryDirectory() as raw:
        root = _bare_project(Path(raw))
        contract = _contract()
        obligation = _obligation(contract)
        policy = _policy()
        proj = initialize_project(root, contract, obligation, policy, "tester", _TS)
        try:
            cand = _candidate("p1")
            # bind candidate obligation to the live target so the record is valid
            spec, request, seed = build_action_context_pair(
                contract_id=contract.contract_id,
                obligation_id=obligation.obligation_id,
                concrete_gap="gap",
                output_contract="proof",
                tool_environment_hash=_hex("tool"),
                acceptance_policy_hash=_hex("accept"),
                reference_versions_hash=_hex("refs"),
                limits=ContextLimits.unlimited(),
                action_type="prove",
                mechanism="p1",
                model_id="grok-4.6",
                effort="high",
                task_mode="worker",
                parameters_json=canonical_json({"k": 1}),
                dependency_hash=_hex("dep"),
                definitions_hash=_hex("defs"),
            )
            cand = CandidateAction(
                candidate_id="p1",
                action_spec=spec,
                context_request=request,
                context_seed=seed,
                obligation_id=obligation.obligation_id,
                mechanism_family="p1",
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
            proj.append_proposed_candidate(cand, "w1", actor="tester", timestamp_utc=_TS)
            view = proj.research_view()
            assert len(view.contracts()) == 1
            assert len(view.obligations()) == 1
            types = [env.event_type for _seq, env in proj.event_store.iter_project(proj.project_id)]
            assert "budget.account_created" in types
            assert EVENT_CANDIDATE_PROPOSED in types
            bad = EventEnvelope.build(
                project_id=proj.project_id,
                actor="tester",
                event_type="obligation.created",
                idempotency_key="bad-ob",
                payload={"obligation": {"obligation_id": "nope"}},
                timestamp_utc=_TS,
            )
            proj.event_store.append(bad)
            _raises(InvalidResearchTransition, proj.research_view)
        finally:
            proj.close()


def test_candidate_roundtrip_order_and_idempotency():
    with tempfile.TemporaryDirectory() as raw:
        root = _bare_project(Path(raw))
        contract = _contract()
        obligation = _obligation(contract)
        proj = initialize_project(
            root, contract, obligation, _policy(), "tester", _TS
        )
        try:
            first = _live_candidate(contract, obligation, "alpha")
            second = _live_candidate(contract, obligation, "beta")
            a = proj.append_proposed_candidate(first, "w1", actor="tester", timestamp_utc=_TS)
            b = proj.append_proposed_candidate(second, "w2", actor="tester", timestamp_utc=_TS)
            again = proj.append_proposed_candidate(first, "w1", actor="tester", timestamp_utc=_TS)
            assert again.proposal_id == a.proposal_id
            loaded = proj.load_proposed_candidates()
            assert [item.proposal_id for item in loaded] == [a.proposal_id, b.proposal_id]
            assert loaded[0].candidate == first
            assert loaded[1].worker == "w2"
            status = proj.status()
            assert status.proposed_ids == (a.proposal_id, b.proposal_id)
            other = _live_candidate(contract, obligation, "gamma")
            # same proposal identity collision with different payload is not this worker/id pair
            _raises(
                EulerProjectError,
                proj.append_proposed_candidate,
                first,
                "missing-worker",
                actor="tester",
                timestamp_utc=_TS,
            )
            del other
        finally:
            proj.close()


def test_policy_account_consistency_and_path_containment():
    with tempfile.TemporaryDirectory() as raw:
        root = _bare_project(Path(raw))
        contract = _contract()
        obligation = _obligation(contract)
        policy = _policy(cost=42_000)
        proj = initialize_project(root, contract, obligation, policy, "tester", _TS)
        try:
            status = proj.status()
            assert status.budget_kind == "LIMITED"
            assert status.budget_value == 42_000
            account = proj.budget.ensure_account(
                proj.project_id,
                BudgetLimit(kind=BudgetLimitKind.LIMITED, value=42_000),
            )
            assert account.limit.value == 42_000
            control = proj.control_dir.resolve()
            assert control.is_relative_to(root.resolve())
            db = (control / "control.sqlite3").resolve()
            arts = (control / "artifacts").resolve()
            assert db.is_relative_to(control)
            assert arts.is_relative_to(control)
            assert not (root / "TASK.md").exists()
            assert not (root / "global_memory").exists()
        finally:
            proj.close()


def test_live_records_stable_ids_and_fact_width():
    candidate = _candidate("rec")
    proposed = ProposedCandidate.build(candidate=candidate, worker="w1", created_at=_TS)
    assert ProposedCandidate.from_dict(proposed.to_dict()) == proposed
    _raises(SchemaValidationError, ProposedCandidate.from_dict, {**proposed.to_dict(), "extra": 1})
    binding = DispatchBinding.build(
        worker="w1",
        candidate_id="c1",
        action_id=_hex("action"),
        lease_id=_hex("lease"),
        attempt_id=_hex("attempt"),
        packet_hash=_hex("packet"),
        cache_key_hash=_hex("cache"),
        task_hash=_hex("task"),
        baseline_fact_ids=(_HEX16,),
        frontier_hash=_hex("frontier"),
        ready=True,
    )
    assert len(binding.intent_id) == 64
    assert len(binding.ready_id or "") == 64
    assert binding.baseline_fact_ids == (_HEX16,)
    loaded = DispatchBinding.from_dict(binding.to_dict())
    assert loaded == binding
    _raises(
        SchemaValidationError,
        DispatchBinding.build,
        worker="w1",
        candidate_id="c1",
        action_id=_hex("action"),
        lease_id=_hex("lease"),
        attempt_id=_hex("attempt"),
        packet_hash=_hex("packet"),
        cache_key_hash=_hex("cache"),
        task_hash=_hex("task"),
        baseline_fact_ids=(_hex("not-16"),),
        frontier_hash=_hex("frontier"),
    )
    recon = ReconciliationResult.build(
        lease_id=_hex("lease"),
        round_id="1",
        fact_ids=(_HEX16,),
        evidence_ids=(_hex("ev"),),
        outcome_ref="SOLVED",
    )
    assert ReconciliationResult.from_dict(recon.to_dict()) == recon
    manifest = ControlManifest.build(
        project_id="demo",
        contract_id=_hex("c"),
        target_obligation_id=_hex("o"),
        policy_hash=_hex("p"),
        created_at=_TS,
        actor="tester",
    )
    assert ControlManifest.from_dict(manifest.to_dict()) == manifest
    status = ControlStatus.build(
        project_id="demo",
        contract_id=_hex("c"),
        target_obligation_id=_hex("o"),
        policy_hash=_hex("p"),
        manifest_hash=manifest.manifest_hash,
        created_at=_TS,
        actor="tester",
        max_seq=3,
        proposed_ids=(proposed.proposal_id,),
        budget_kind="UNLIMITED",
        budget_value=None,
    )
    assert ControlStatus.from_dict(status.to_dict()) == status


def test_production_modules_isolated_from_truth_provider_execution():
    forbidden_modules = {
        "euler.core",
        "euler.core.factgraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.verify",
        "euler.execution",
        "euler.providers",
        "euler.orchestration",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("project.py", "live_records.py"):
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
        assert named.isdisjoint(forbidden_names), (filename, named)
        assert "TASK.md" not in source


def _live_candidate(contract: ProblemContract, obligation: Obligation, tag: str) -> CandidateAction:
    spec, request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap=f"gap-{tag}",
        output_contract="proof",
        tool_environment_hash=_hex("tool"),
        acceptance_policy_hash=_hex("accept"),
        reference_versions_hash=_hex("refs"),
        limits=ContextLimits.unlimited(),
        action_type="prove",
        mechanism=tag,
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"tag": tag}),
        dependency_hash=_hex("dep"),
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


def main() -> None:
    tests = [
        test_initialize_open_reopen_is_idempotent,
        test_conflict_and_partial_state_fail_closed,
        test_filtered_replay_skips_unrelated_and_fails_malformed_research,
        test_candidate_roundtrip_order_and_idempotency,
        test_policy_account_consistency_and_path_containment,
        test_live_records_stable_ids_and_fact_width,
        test_production_modules_isolated_from_truth_provider_execution,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL PROJECT PERSISTENCE TESTS PASSED")


if __name__ == "__main__":
    main()
