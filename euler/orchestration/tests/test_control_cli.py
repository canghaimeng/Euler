"""CLI tests for Euler live control verbs."""

from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from euler.control.cli import ControlSurfaceError, do_init, do_propose, do_schedule
from euler.control.context import ContextLimits
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.orchestration import cli


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


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
    with _env(**env):
        yield


def _run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


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


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _candidate(contract: ProblemContract, obligation, tag: str) -> CandidateAction:
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


def test_init_propose_schedule_status_checkpoint():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with _project_env(tmp):
            rc, _out = _run_main(["new", "P", "--roles", "high:1"])
            assert rc == 0
            contract = _contract()
            policy = _policy()
            cpath = _write_json(tmp / "contract.json", contract.to_dict())
            ppath = _write_json(tmp / "policy.json", policy.to_dict())
            rc, out = _run_main(
                ["init", "P", "--contract", str(cpath), "--policy", str(ppath)]
            )
            first = json.loads(out)
            assert rc == 0 and first["control_plane"] == "euler"
            rc, out = _run_main(
                ["init", "P", "--contract", str(cpath), "--policy", str(ppath)]
            )
            assert json.loads(out)["manifest_hash"] == first["manifest_hash"]
            other = ProblemContract.build(
                problem_id="other",
                version=1,
                exact_statement="different",
                scope=_scope(),
                permitted_tools=("lean",),
                external_help_policy="none",
                target_kind=TargetKind.PROOF,
                required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
                completion_checks=("proof checked",),
                budget_policy_ref="default-budget",
            )
            opath = _write_json(tmp / "other.json", other.to_dict())
            try:
                cli.main(["init", "P", "--contract", str(opath), "--policy", str(ppath)])
                raise AssertionError("expected conflict")
            except SystemExit as exc:
                assert "mismatch" in str(exc) or "conflict" in str(exc).lower() or str(exc)
            from euler.control.cli import target_obligation_from_contract

            ob = target_obligation_from_contract(contract, created_by="euler-cli")
            cand = _candidate(contract, ob, "alpha")
            cand_path = _write_json(tmp / "cand.json", cand.to_dict())
            rc, out = _run_main(["propose", "P/high", "--candidate", str(cand_path)])
            assert rc == 0 and json.loads(out)["candidate_id"] == "alpha"
            rc, out = _run_main(["schedule", "P"])
            payload = json.loads(out)
            assert payload["launch"] is False
            assert payload["dispatched"] == []
            assert "alpha" in payload["selected_candidate_ids"]
            with patch("euler.control.controller.start_managed_worker") as launched:
                rc, out = _run_main(["schedule", "P", "--dispatch"])
                assert launched.call_count == 0
                dispatched = json.loads(out)["dispatched"]
                assert len(dispatched) == 1
            rc, out = _run_main(["status", "P", "--json"])
            wrapped = json.loads(out)
            assert wrapped["control"]["manifest_hash"] == first["manifest_hash"]
            assert wrapped["workers"][0]["worker"] == "high"
            fake_run = {
                "run_id": "a" * 64,
                "obligation_id": first["target_obligation_id"],
                "fact_id": "0123456789abcdef",
                "status": "DEFERRED",
                "replayed": False,
                "plan_hash": "b" * 64,
                "disposition": "DEFERRED",
                "reason": "not-ready",
                "outcome": None,
            }

            class _Fake:
                def to_dict(self):
                    return fake_run

            with patch("euler.control.cli.run_final_audit", return_value=_Fake()):
                rc, out = _run_main(["checkpoint", "P"])
            assert rc == 0
            assert json.loads(out)["run"]["status"] == "DEFERRED"


def test_malformed_and_uninitialized():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with _project_env(tmp):
            rc, _out = _run_main(["new", "Q", "--roles", "high:1"])
            assert rc == 0
            bad = tmp / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            try:
                cli.main(["init", "Q", "--contract", str(bad), "--policy", str(bad)])
                raise AssertionError("expected malformed")
            except SystemExit:
                pass
            try:
                cli.main(["propose", "Q/high", "--candidate", str(bad)])
                raise AssertionError("expected uninitialized or malformed")
            except SystemExit:
                pass
            _raises = None
            try:
                do_schedule("Q")
            except ControlSurfaceError as exc:
                _raises = exc
            assert _raises is not None


def main() -> None:
    tests = [
        test_init_propose_schedule_status_checkpoint,
        test_malformed_and_uninitialized,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL CONTROL CLI TESTS PASSED")


if __name__ == "__main__":
    main()
