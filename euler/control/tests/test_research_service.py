"""Public-state transactions, admission boundaries and revocation replay."""
from pathlib import Path
import tempfile

import pytest

from euler.control.tests.test_reconcile import _managed, _write_fact, _write_admission
from euler.control.obligations import Obligation, Route, Relation, RelationType
from euler.control.contracts import EvidenceKind
from euler.control.research_service import ResearchService, ResearchServiceError, frontier_payload, _digest
from euler.control.views import InvalidResearchTransition


def child_for(contract):
    return Obligation.build(contract_id=contract.contract_id, exact_statement="A useful intermediate lemma",
                            scope=contract.scope, desired_output="proof", created_by="reviewer", version=1)


def test_invalid_batch_rolls_back_prior_valid_creation():
    with tempfile.TemporaryDirectory() as tmp:
        project, contract, _root = _managed(Path(tmp))
        try:
            service = ResearchService(project, actor="reviewer")
            child = child_for(contract)
            count = len(list(project.event_store.iter_project(project.project_id)))
            with pytest.raises(InvalidResearchTransition):
                service.apply([
                    {"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}},
                    {"event_type": "obligation.status_changed", "payload": {
                        "obligation_id": child.obligation_id, "status": "DISCHARGED", "evidence_refs": []}},
                ])
            assert len(list(project.event_store.iter_project(project.project_id))) == count
            assert project.research_view().get_obligation(child.obligation_id) is None
        finally:
            project.close()


def test_child_admission_discharge_revocation_and_idempotent_replay():
    with tempfile.TemporaryDirectory() as tmp:
        project, contract, root = _managed(Path(tmp))
        try:
            service = ResearchService(project, actor="reviewer")
            child = child_for(contract)
            operation = {"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}
            service.apply([operation])
            assert not service.apply([operation])["events"][0]["created"]
            fact_id = "1" * 16
            _write_fact(project.project_dir, fact_id, child.exact_statement)
            with pytest.raises(ResearchServiceError, match="admission"):
                service.admit_fact(fact_id, child.obligation_id,
                                   confirmed_scope_hash=_digest(child.scope.to_dict()), alignment_note="Reviewed exact scope")
            _write_admission(project.project_dir, fact_id, child.exact_statement)
            with pytest.raises(ResearchServiceError, match="scope"):
                service.admit_fact(fact_id, child.obligation_id,
                                   confirmed_scope_hash="0" * 64, alignment_note="Reviewed exact scope")
            evidence = service.admit_fact(fact_id, child.obligation_id,
                                          confirmed_scope_hash=_digest(child.scope.to_dict()), alignment_note="Reviewed exact scope")
            same = service.admit_fact(fact_id, child.obligation_id,
                                      confirmed_scope_hash=_digest(child.scope.to_dict()), alignment_note="Reviewed exact scope")
            assert same == evidence
            service.apply([{"event_type": "obligation.status_changed", "payload": {
                "obligation_id": child.obligation_id, "status": "DISCHARGED", "evidence_refs": [evidence["evidence_id"]]}}])
            assert project.research_view().get_obligation(root.obligation_id).status.value == "OPEN"
            assert child.obligation_id not in {o["obligation_id"] for o in frontier_payload(project)["obligations"]}
            fact = project.project_dir / "fact_graph/facts" / f"{fact_id}.md"
            revoked = project.project_dir / "fact_graph/_revoked"
            revoked.mkdir()
            fact.rename(revoked / fact.name)
            result = service.sync_fact_dependencies()
            assert result["reopened_obligations"] == [child.obligation_id]
            assert project.research_view().get_obligation(child.obligation_id).status.value == "OPEN"
            assert service.sync_fact_dependencies()["invalidated_evidence"] == []
        finally:
            project.close()


def test_public_update_cannot_fabricate_checked_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        project, _, _ = _managed(Path(tmp))
        try:
            with pytest.raises(ResearchServiceError, match="adapter"):
                ResearchService(project, actor="reviewer").apply([
                    {"event_type": "evidence.registered", "payload": {"evidence": {"checker_result": "PASS"}}}])
        finally:
            project.close()


def test_direct_controller_reuses_admitted_evidence_and_reopens_revoked_claim():
    from euler.control.controller import EulerController
    with tempfile.TemporaryDirectory() as tmp:
        project, contract, root = _managed(Path(tmp))
        try:
            child = child_for(contract)
            service = ResearchService(project, actor="reviewer")
            service.apply([{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}])
            fid = "4" * 16
            _write_fact(project.project_dir, fid, child.exact_statement)
            _write_admission(project.project_dir, fid, child.exact_statement)
            service.admit_fact(fid, child.obligation_id,
                confirmed_scope_hash=_digest(child.scope.to_dict()), alignment_note="Checked exact interface")
            ctl = EulerController(project)
            assert ctl.schedule().idle
            assert project.research_view().get_obligation(child.obligation_id).status.value == "DISCHARGED"
            assert project.research_view().get_obligation(root.obligation_id).status.value == "OPEN"
            assert not project.runtime_snapshot().leases
            (project.project_dir / "fact_graph/facts" / f"{fid}.md").unlink()
            ctl.schedule()
            assert project.research_view().get_obligation(child.obligation_id).status.value == "OPEN"
        finally:
            project.close()


def test_scheduling_does_not_reuse_evidence_rejected_by_final_audit():
    from euler.control.controller import EulerController
    from euler.control.final_audit import run_final_audit, FinalAuditStatus
    from euler.control.tests.test_final_audit import _FakeTransport, _ok
    with tempfile.TemporaryDirectory() as tmp:
        project, _contract, root = _managed(Path(tmp))
        try:
            service = ResearchService(project, actor="reviewer")
            fid = "5" * 16
            _write_fact(project.project_dir, fid, root.exact_statement)
            _write_admission(project.project_dir, fid, root.exact_statement)
            admitted = service.admit_fact(fid, root.obligation_id,
                confirmed_scope_hash=_digest(root.scope.to_dict()), alignment_note="Reviewed original scope")
            ctl = EulerController(project)
            ctl.schedule()
            rejected = run_final_audit(project, transport=_FakeTransport(_ok("wrong")))
            assert rejected.status is FinalAuditStatus.AUDIT_FAIL
            ctl.schedule()
            view = project.research_view()
            assert view.get_obligation(root.obligation_id).status.value == "OPEN"
            assert view.get_evidence(admitted["evidence_id"]).validity.value == "STALE"
        finally:
            project.close()


def test_cli_and_mcp_share_the_real_research_service(monkeypatch, capsys):
    import json
    from euler.orchestration.cli import main
    from euler.control import mcp
    with tempfile.TemporaryDirectory() as tmp:
        project, contract, root = _managed(Path(tmp))
        project.close()
        monkeypatch.setenv("EULER_AGENTS_ROOT", tmp)
        child = child_for(contract)
        ops = Path(tmp) / "operations.json"
        ops.write_text(json.dumps([{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}]))
        main(["research-update", "demo", "--operations", str(ops)])
        result = json.loads(capsys.readouterr().out)
        assert len(result["frontier"]["obligations"]) == 2
        monkeypatch.setenv("EULER_ROLE", "worker")
        with pytest.raises(RuntimeError, match="main"):
            mcp.euler_frontier("demo")
        monkeypatch.setenv("EULER_ROLE", "main")
        assert mcp.euler_frontier("demo")["root_obligation_id"] == root.obligation_id
        main(["frontier", "demo"])
        assert len(json.loads(capsys.readouterr().out)["obligations"]) == 2


def test_checked_reduction_does_not_prove_its_premise_or_discharge_the_root():
    with tempfile.TemporaryDirectory() as tmp:
        project, contract, root = _managed(Path(tmp))
        try:
            service = ResearchService(project, actor="reviewer")
            child = child_for(contract)
            closing = Obligation.build(contract_id=contract.contract_id,
                exact_statement="The intermediate lemma implies the target", scope=contract.scope,
                desired_output="proof", created_by="reviewer", version=1)
            route = Route.build(target_obligation_id=root.obligation_id, mechanism="checked reduction",
                required_obligation_ids=(child.obligation_id,), progress_claim=closing.exact_statement)
            service.apply([
                {"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}},
                {"event_type": "obligation.created", "payload": {"obligation": closing.to_dict()}},
                {"event_type": "route.created", "payload": {"route": route.to_dict()}},
            ])
            refs = {}
            for fact_id, obligation in [("2" * 16, closing), ("3" * 16, child)]:
                _write_fact(project.project_dir, fact_id, obligation.exact_statement)
                _write_admission(project.project_dir, fact_id, obligation.exact_statement)
                refs[obligation.obligation_id] = service.admit_fact(fact_id, obligation.obligation_id,
                    confirmed_scope_hash=_digest(obligation.scope.to_dict()), alignment_note="Scope and source checked")["evidence_id"]
            reduction = Relation.build(relation_type=RelationType.REDUCES_TO,
                source=route.route_id, target=root.obligation_id, exact_claim=closing.exact_statement,
                scope=contract.scope, evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                evidence_refs=(refs[closing.obligation_id],), reviewer="reviewer",
                contract_id=contract.contract_id, definitions_hash=contract.scope.definitions_hash)
            service.apply([{"event_type": "relation.created", "payload": {"relation": reduction.to_dict()}}])
            finish = {"event_type": "route.status_changed", "payload": {
                "route_id": route.route_id, "status": "COMPLETED", "evidence_refs": [refs[closing.obligation_id]]}}
            with pytest.raises(ResearchServiceError, match="AND premise"):
                service.apply([finish])
            assert project.research_view().get_obligation(child.obligation_id).status.value == "OPEN"
            service.apply([{"event_type": "obligation.status_changed", "payload": {
                "obligation_id": child.obligation_id, "status": "DISCHARGED", "evidence_refs": [refs[child.obligation_id]]}}])
            wrong = {"event_type": "route.status_changed", "payload": {
                **finish["payload"], "evidence_refs": [refs[child.obligation_id]]}}
            with pytest.raises(ResearchServiceError, match="closing implication"):
                service.apply([wrong])
            service.apply([finish])
            assert project.research_view().get_route(route.route_id).status.value == "COMPLETED"
            assert project.research_view().get_obligation(root.obligation_id).status.value == "OPEN"
        finally:
            project.close()
