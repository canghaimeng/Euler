"""Public service regressions using isolated projects and a fixture verifier.

Polynomial checks below execute the real local deterministic checker. Final
audits use an explicitly fake verifier; they never contact a live team/service.
"""

from pathlib import Path
import shutil

import pytest

from euler.control.compute_service import (
    review_compute_realization,
    run_polynomial_compute,
)
from euler.control.final_audit import FinalAuditError, FinalAuditStatus, run_final_audit
from euler.control.obligations import ObligationStatus, Route, RouteStatus
from euler.control.kernel_service import check_kernel
from euler.control.research_service import ResearchService, ResearchServiceError, _digest
from euler.control.tests.test_compute_service import (
    _child,
    _predicate,
    _project,
    _scope_hash,
)
from euler.control.tests.test_final_audit import _FakeTransport, _ok
from euler.control.tests.test_kernel_evidence import _write_lean_project
from euler.control.tests.test_reconcile import _managed, _write_admission, _write_fact
from euler.control.views import InvalidResearchTransition


def test_public_update_cannot_refute_an_unrelated_or_route(tmp_path: Path):
    project, contract, root = _project(tmp_path)
    try:
        service = ResearchService(project, actor="auditor")
        child_a = _child(contract)
        child_b = _child(contract, statement="An unrelated alternative premise")
        route_a = Route.build(
            target_obligation_id=root.obligation_id,
            mechanism="route-a",
            required_obligation_ids=(child_a.obligation_id,),
            alternative_group="independent-or-alternatives",
        )
        route_b = Route.build(
            target_obligation_id=root.obligation_id,
            mechanism="route-b",
            required_obligation_ids=(child_b.obligation_id,),
            alternative_group="independent-or-alternatives",
        )
        service.apply([
            *[{"event_type": "obligation.created", "payload": {"obligation": item.to_dict()}}
              for item in (child_a, child_b)],
            *[{"event_type": "route.created", "payload": {"route": item.to_dict()}}
              for item in (route_a, route_b)],
        ])
        run_polynomial_compute(
            project,
            obligation_id=child_a.obligation_id,
            request_id="counterexample-a",
            predicate=_predicate(),
            max_points=4,
            max_wall_ms=5_000,
            actor="auditor",
        )
        evidence = review_compute_realization(
            project,
            obligation_id=child_a.obligation_id,
            request_id="counterexample-a",
            confirmed_scope_hash=_scope_hash(child_a),
            notes="The assignment x=1 falsifies premise A in its recorded box.",
            actor="auditor",
            translation_confirmed=True,
            original_object_realized=True,
            route_id=route_a.route_id,
        )
        with pytest.raises((InvalidResearchTransition, ResearchServiceError)):
            service.apply([{
                "event_type": "route.status_changed",
                "payload": {
                    "route_id": route_b.route_id,
                    "status": "REFUTED",
                    "evidence_refs": [evidence["evidence_id"]],
                    "reason": "Reuse unrelated A counterexample against B",
                },
            }])
        view = project.research_view()
        assert view.get_route(route_a.route_id).status is RouteStatus.REFUTED
        assert view.get_route(route_b.route_id).status is RouteStatus.PROPOSED
        assert view.get_obligation(child_b.obligation_id).status is ObligationStatus.OPEN
        assert view.get_obligation(root.obligation_id).status is ObligationStatus.OPEN
    finally:
        project.close()


def _admit_target_with_ancestor(project, obligation):
    ancestor_id, target_id = "1" * 16, "2" * 16
    ancestor_claim = "An essential supporting lemma"
    _write_fact(project.project_dir, ancestor_id, ancestor_claim)
    _write_admission(project.project_dir, ancestor_id, ancestor_claim)
    _write_fact(project.project_dir, target_id, obligation.exact_statement)
    _write_admission(project.project_dir, target_id, obligation.exact_statement)
    target_path = project.project_dir / "fact_graph" / "facts" / f"{target_id}.md"
    target_path.write_text(
        target_path.read_text().replace("predecessors: []", f"predecessors: [{ancestor_id}]"),
        encoding="utf-8",
    )
    service = ResearchService(project, actor="auditor")
    evidence = service.admit_fact(
        target_id,
        obligation.obligation_id,
        confirmed_scope_hash=_digest(obligation.scope.to_dict()),
        alignment_note="Reviewed the target statement, exact scope and dependencies.",
    )
    service.apply([{
        "event_type": "obligation.status_changed",
        "payload": {
            "obligation_id": obligation.obligation_id,
            "status": "DISCHARGED",
            "evidence_refs": [evidence["evidence_id"]],
        },
    }])
    return project.project_dir / "fact_graph" / "facts" / f"{ancestor_id}.md"


def _change_ancestor(path):
    path.write_text(
        path.read_text().replace("By inspection.", "Changed supporting proof after its original review."),
        encoding="utf-8",
    )


def test_final_audit_does_not_replay_solved_after_ancestor_changes(tmp_path: Path):
    project, _contract, obligation = _managed(tmp_path)
    try:
        ancestor_path = _admit_target_with_ancestor(project, obligation)
        solved = run_final_audit(project, transport=_FakeTransport(_ok("correct")))
        assert solved.status is FinalAuditStatus.SOLVED
        _change_ancestor(ancestor_path)
        verifier = _FakeTransport(_ok("wrong"))
        with pytest.raises(FinalAuditError):
            run_final_audit(project, transport=verifier)
        assert not verifier.calls
        assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
    finally:
        project.close()


def test_final_audit_detects_ancestor_changes_during_verification(tmp_path: Path):
    project, _contract, obligation = _managed(tmp_path)
    try:
        ancestor_path = _admit_target_with_ancestor(project, obligation)

        class MutatingFixtureVerifier:
            def verify(self, statement, proof):
                _change_ancestor(ancestor_path)
                return _ok("correct")

        result = run_final_audit(project, transport=MutatingFixtureVerifier())
        assert result.status is FinalAuditStatus.INFRA
        assert result.reason == "sources_changed"
        assert project.research_view().get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
    finally:
        project.close()


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_real_kernel_rejects_unapproved_axioms_in_multiline_report(tmp_path: Path):
    project, _contract, obligation = _managed(tmp_path)
    try:
        names = [f"unapproved_axiom_{i}_" + "longname" * 9 for i in range(7)]
        statement = " ∧ ".join(["True"] * len(names))
        source = (
            "\n".join(f"axiom {name} : True" for name in names)
            + f"\ntheorem probe : {statement} := ⟨"
            + ", ".join(names)
            + "⟩\n"
        )
        math_project = _write_lean_project(tmp_path / "math", source)
        result = check_kernel(
            project,
            obligation.obligation_id,
            {
                "project_dir": str(math_project),
                "module_name": "TinyKernel",
                "theorem_name": "probe",
                "expected_type": statement,
                "timeout_ms": 60_000,
            },
            request_id="multiline-axioms",
            actor="auditor",
        )
        assert result["status"] == "FAIL", result
        assert "unapproved axiom" in result["detail"], result
        assert set(result["axioms"]) == set(names)
    finally:
        project.close()


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_real_kernel_rejects_namespace_lookalike_of_approved_axiom(tmp_path: Path):
    project, _contract, obligation = _managed(tmp_path)
    try:
        math_project = _write_lean_project(
            tmp_path / "math",
            "namespace Lean\n"
            "axiom propext : False\n"
            "end Lean\n"
            "theorem forged : False := Lean.propext\n",
        )
        result = check_kernel(
            project,
            obligation.obligation_id,
            {
                "project_dir": str(math_project),
                "module_name": "TinyKernel",
                "theorem_name": "forged",
                "expected_type": "False",
                "timeout_ms": 60_000,
            },
            request_id="namespace-lookalike-axiom",
            actor="auditor",
        )
        assert result["status"] == "FAIL", result
        assert "unapproved axiom Lean.propext" in result["detail"], result
        assert result["axioms"] == ["Lean.propext"]
    finally:
        project.close()
