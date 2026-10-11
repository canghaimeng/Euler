"""Integration tests for the polynomial compute service.

Temp projects only. Executes the real searcher/checker. No Fact writes.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest

from euler.control.compute import ComputeTermination
from euler.control.compute_plugins.polynomial_counterexample import (
    IntegerInterval,
    PolynomialPredicate,
    PolynomialRelation,
    PolynomialTerm,
)
from euler.control.compute_service import (
    ComputeServiceError,
    review_compute_realization,
    run_polynomial_compute,
)
from euler.control.contracts import (
    MathematicalScope,
    QuantifierBinding,
    QuantifierKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.failures import FailureKind
from euler.control.obligations import Obligation, ObligationStatus, Route, RouteStatus
from euler.control.policy import BudgetPolicy, DimensionLimit
from euler.control.project import initialize_project
from euler.control.research_service import ResearchService
from euler.control.tests.test_reconcile import _contract, _obligation

_TS = "2026-01-01T00:00:00Z"
_CHILD_CLAIM = "For every integer x from 0 through 3, x equals zero."


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _policy(*, compute_slots: int = 1) -> BudgetPolicy:
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
        max_compute_slots=compute_slots,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _box_scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("x is an integer in the recorded finite interval",),
        quantifiers=(
            QuantifierBinding(
                variable="x",
                kind=QuantifierKind.FORALL,
                domain="integers from 0 through 3",
            ),
        ),
        objects=(),
        parameter_domain="0 <= x <= 3",
        definitions_hash=_hex("compute-box"),
    )


def _predicate() -> PolynomialPredicate:
    return PolynomialPredicate.build(
        variables=("x",),
        intervals=(IntegerInterval(lower=0, upper=3),),
        terms=(PolynomialTerm(coefficient=1, exponents=(1,)),),
        relation=PolynomialRelation.EQ,
    )


def _project(tmp: Path, *, compute_slots: int = 1):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": ["w1"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "workers" / "w1").mkdir(parents=True)
    contract = _contract()
    root_ob = _obligation(contract)
    project = initialize_project(
        root, contract, root_ob, _policy(compute_slots=compute_slots), "tester", _TS
    )
    return project, contract, root_ob


def _child(contract, *, statement: str = _CHILD_CLAIM, scope=None) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=statement,
        scope=contract.scope if scope is None else scope,
        desired_output="checked counterexample",
        created_by="tester",
        version=1,
    )


def _scope_hash(obligation: Obligation) -> str:
    return sha256_hex(canonical_json(obligation.scope.to_dict()))


def _facts_dir(project) -> Path:
    return project.project_dir / "fact_graph" / "facts"


def test_polynomial_search_promotes_child_only_and_keeps_root_and_or_alternative():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root_ob = _project(Path(raw))
        try:
            service = ResearchService(project, actor="tester")
            child_a = _child(contract)
            child_b = Obligation.build(
                contract_id=contract.contract_id,
                exact_statement="an unrelated alternative lemma",
                scope=contract.scope,
                desired_output="proof",
                created_by="tester",
                version=1,
            )
            route_a = Route.build(
                target_obligation_id=root_ob.obligation_id,
                mechanism="finite-box-refutation",
                required_obligation_ids=(child_a.obligation_id,),
                alternative_group="goldbach-or",
            )
            route_b = Route.build(
                target_obligation_id=root_ob.obligation_id,
                mechanism="independent-alternative",
                required_obligation_ids=(child_b.obligation_id,),
                alternative_group="goldbach-or",
            )
            service.apply(
                [
                    {"event_type": "obligation.created", "payload": {"obligation": child_a.to_dict()}},
                    {"event_type": "obligation.created", "payload": {"obligation": child_b.to_dict()}},
                    {"event_type": "route.created", "payload": {"route": route_a.to_dict()}},
                    {"event_type": "route.created", "payload": {"route": route_b.to_dict()}},
                ]
            )
            ran = run_polynomial_compute(
                project,
                obligation_id=child_a.obligation_id,
                request_id="poly-1",
                predicate=_predicate(),
                max_points=4,
                max_wall_ms=5_000,
                actor="tester",
            )
            assert ran["status"] == "CERTIFIED_CANDIDATE"
            assert ran["not_a_proof"] is True
            assert ran["termination"] == ComputeTermination.CHECKED_COUNTEREXAMPLE.value
            assert ran["compute_result"]["coverage"]["examined_points"] == 2
            assert ran["compute_result"]["checker_record"]["checker_result"] == "PASS"
            assert (
                ran["request"]["searcher"]["component_id"]
                != ran["request"]["checker"]["component_id"]
            )
            assert ran["compute_result"]["candidate_ref"]["content_hash"]
            assert json.loads(
                project.artifacts.get_bytes(
                    ran["compute_result"]["candidate_ref"]["content_hash"]
                )
            ) == {"x": 1}
            view = project.research_view()
            assert view.get_obligation(child_a.obligation_id).status is ObligationStatus.OPEN
            promoted = review_compute_realization(
                project,
                obligation_id=child_a.obligation_id,
                request_id="poly-1",
                confirmed_scope_hash=_scope_hash(child_a),
                notes="The assignment x=1 is in the recorded box and falsifies x=0.",
                actor="tester",
                translation_confirmed=True,
                original_object_realized=True,
                route_id=route_a.route_id,
            )
            again = review_compute_realization(
                project,
                obligation_id=child_a.obligation_id,
                request_id="poly-1",
                confirmed_scope_hash=_scope_hash(child_a),
                notes="The assignment x=1 is in the recorded box and falsifies x=0.",
                actor="tester",
                translation_confirmed=True,
                original_object_realized=True,
                route_id=route_a.route_id,
            )
            assert again["replayed"] is True
            assert again["evidence_id"] == promoted["evidence_id"]
            view = project.research_view()
            assert view.get_obligation(child_a.obligation_id).status is ObligationStatus.REFUTED
            assert view.get_route(route_a.route_id).status is RouteStatus.REFUTED
            assert view.get_obligation(child_b.obligation_id).status is ObligationStatus.OPEN
            assert view.get_route(route_b.route_id).status is RouteStatus.PROPOSED
            assert view.get_obligation(root_ob.obligation_id).status is ObligationStatus.OPEN
            assert view.get_evidence(promoted["evidence_id"]) is not None
            cex = [
                item
                for item in view.failures()
                if item.kind is FailureKind.CERTIFIED_COUNTEREXAMPLE
            ]
            assert len(cex) == 1
            assert cex[0].target_obligation_id == child_a.obligation_id
            assert cex[0].route_id == route_a.route_id
            assert not _facts_dir(project).exists() or not list(_facts_dir(project).glob("*.md"))
        finally:
            project.close()


def test_incomplete_search_is_unknown_not_a_proof():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, _root = _project(Path(raw))
        try:
            child = _child(contract)
            ResearchService(project, actor="tester").apply(
                [{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}]
            )
            ran = run_polynomial_compute(
                project,
                obligation_id=child.obligation_id,
                request_id="poly-bound",
                predicate=_predicate(),
                max_points=1,
                max_wall_ms=5_000,
                actor="tester",
            )
            assert ran["status"] == "UNKNOWN"
            assert ran["epistemic"] == "UNKNOWN"
            assert ran["not_a_proof"] is True
            assert ran["termination"] == ComputeTermination.POINT_LIMIT_REACHED.value
            assert ran["compute_result"]["candidate_ref"] is None
            view = project.research_view()
            assert view.get_obligation(child.obligation_id).status is ObligationStatus.OPEN
            diagnostics = [
                item
                for item in view.failures()
                if item.kind is FailureKind.COMPUTE_INCONCLUSIVE
            ]
            assert len(diagnostics) == 1
            assert diagnostics[0].target_obligation_id == child.obligation_id
            assert diagnostics[0].exclusion_scope is None
            with pytest.raises(ComputeServiceError, match="checked finite candidate"):
                review_compute_realization(
                    project,
                    obligation_id=child.obligation_id,
                    request_id="poly-bound",
                    confirmed_scope_hash=_scope_hash(child),
                    notes="should not promote an incomplete search",
                    actor="tester",
                    translation_confirmed=True,
                    original_object_realized=True,
                )
            assert not _facts_dir(project).exists() or not list(_facts_dir(project).glob("*.md"))
        finally:
            project.close()


def test_mismatch_scope_witness_predicate_and_unsigned_pass_are_rejected():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, _root = _project(Path(raw))
        try:
            child = _child(contract)
            ResearchService(project, actor="tester").apply(
                [{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}]
            )
            with pytest.raises(ComputeServiceError, match="plugin"):
                run_polynomial_compute(
                    project,
                    obligation_id=child.obligation_id,
                    request_id="bad-plugin",
                    predicate=_predicate(),
                    max_points=4,
                    max_wall_ms=1_000,
                    actor="tester",
                    plugin_id="some-other-solver",
                )
            with pytest.raises(ComputeServiceError, match="predicate"):
                run_polynomial_compute(
                    project,
                    obligation_id=child.obligation_id,
                    request_id="bad-pred",
                    predicate={"status": "PASS", "witness": {"x": 1}},
                    max_points=4,
                    max_wall_ms=1_000,
                    actor="tester",
                )
            ran = run_polynomial_compute(
                project,
                obligation_id=child.obligation_id,
                request_id="poly-ok",
                predicate=_predicate(),
                max_points=4,
                max_wall_ms=5_000,
                actor="tester",
            )
            assert ran["status"] == "CERTIFIED_CANDIDATE"
            with pytest.raises(ComputeServiceError, match="scope"):
                review_compute_realization(
                    project,
                    obligation_id=child.obligation_id,
                    request_id="poly-ok",
                    confirmed_scope_hash="0" * 64,
                    notes="wrong scope",
                    actor="tester",
                    translation_confirmed=True,
                    original_object_realized=True,
                )
            with pytest.raises(ComputeServiceError, match="realization"):
                review_compute_realization(
                    project,
                    obligation_id=child.obligation_id,
                    request_id="poly-ok",
                    confirmed_scope_hash=_scope_hash(child),
                    notes="candidate exists but lifting was not reviewed",
                    actor="tester",
                    translation_confirmed=True,
                    original_object_realized=False,
                )
            foreign = _child(contract, statement="a finite-box representation", scope=_box_scope())
            ResearchService(project, actor="tester").apply(
                [{"event_type": "obligation.created", "payload": {"obligation": foreign.to_dict()}}]
            )
            foreign_run = run_polynomial_compute(
                project,
                obligation_id=foreign.obligation_id,
                request_id="poly-foreign",
                predicate=_predicate(),
                max_points=4,
                max_wall_ms=5_000,
                actor="tester",
            )
            assert foreign_run["status"] == "CERTIFIED_CANDIDATE"
            with pytest.raises(ComputeServiceError, match="scope"):
                review_compute_realization(
                    project,
                    obligation_id=foreign.obligation_id,
                    request_id="poly-foreign",
                    confirmed_scope_hash=_scope_hash(child),
                    notes="a root-scope review cannot stand in for the selected child scope",
                    actor="tester",
                    translation_confirmed=True,
                    original_object_realized=True,
                )
            assert project.research_view().get_obligation(child.obligation_id).status is ObligationStatus.OPEN
            assert project.research_view().get_obligation(foreign.obligation_id).status is ObligationStatus.OPEN
            reviewed = review_compute_realization(
                project, obligation_id=foreign.obligation_id, request_id="poly-foreign",
                confirmed_scope_hash=_scope_hash(foreign),
                notes="Reviewed the witness only for this finite-box child, not the root",
                actor="tester", translation_confirmed=True, original_object_realized=True,
            )
            assert reviewed["evidence_kind"] == "CHECKED_COUNTEREXAMPLE"
            assert project.research_view().get_obligation(foreign.obligation_id).status is ObligationStatus.REFUTED
            assert project.research_view().get_obligation(project.manifest.target_obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()


def test_compute_slot_deferral_and_idempotent_replay():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        blocked, contract, _root = _project(tmp / "blocked", compute_slots=0)
        try:
            child = _child(contract)
            ResearchService(blocked, actor="tester").apply(
                [{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}]
            )
            deferred = run_polynomial_compute(
                blocked,
                obligation_id=child.obligation_id,
                request_id="no-slot",
                predicate=_predicate(),
                max_points=4,
                max_wall_ms=1_000,
                actor="tester",
            )
            assert deferred["status"] == "DEFERRED"
            assert deferred["replayed"] is False
            assert not blocked.runtime_snapshot().leases
        finally:
            blocked.close()

        project, contract, _root = _project(tmp / "ok")
        try:
            child = _child(contract)
            ResearchService(project, actor="tester").apply(
                [{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}]
            )
            first = run_polynomial_compute(
                project,
                obligation_id=child.obligation_id,
                request_id="replay-1",
                predicate=_predicate(),
                max_points=4,
                max_wall_ms=5_000,
                actor="tester",
            )
            second = run_polynomial_compute(
                project,
                obligation_id=child.obligation_id,
                request_id="replay-1",
                predicate=_predicate(),
                max_points=4,
                max_wall_ms=5_000,
                actor="tester",
            )
            assert first["replayed"] is False
            assert second["replayed"] is True
            assert first["compute_result"]["request_id"] == second["compute_result"]["request_id"]
            finished = [
                envelope
                for _seq, envelope in project.event_store.iter_project(project.project_id)
                if envelope.event_type == "euler.local_action_finished"
            ]
            assert len(finished) == 1
            assert not _facts_dir(project).exists() or not list(_facts_dir(project).glob("*.md"))
        finally:
            project.close()
