"""Actual route-premise dispatch and newly validated reduction accounting."""
from pathlib import Path
import tempfile

import pytest

from euler.control.contracts import EvidenceKind
from euler.control.controller import EulerControllerError
from euler.control.hooks import before_managed_round, after_managed_round
from euler.control.obligations import Obligation, Relation, RelationType, Route
from euler.control.research_service import ResearchService, _digest
from euler.control.dispatch import DispatchError, load_dispatch, validate_dispatch, sidecar_path, _sidecar_body_hash
from euler.control.tests.test_lifecycle_v02 import (
    _managed, _add_child, _dispatch, _receipts, _write_fact, _write_admission,
)


def test_dispatch_can_target_a_route_premise_but_not_an_unrelated_obligation():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root = _managed(Path(raw))
        try:
            child = _add_child(project, contract, "route premise")
            unrelated = _add_child(project, contract, "unrelated claim")
            route = Route.build(target_obligation_id=root.obligation_id, mechanism="decomposition",
                                required_obligation_ids=(child.obligation_id,))
            ResearchService(project, actor="planner").apply([
                {"event_type": "route.created", "payload": {"route": route.to_dict()}}])
            binding, wl, _ = _dispatch(project, contract, child, "child-route", route_id=route.route_id)
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            assert binding.obligation_id == child.obligation_id
            after_managed_round(wl, start, launch, result, end)
            with pytest.raises(EulerControllerError):
                _dispatch(project, contract, unrelated, "foreign-route", route_id=route.route_id)
        finally:
            project.close()


def test_old_reduction_is_not_counted_again_on_a_later_empty_round():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root = _managed(Path(raw))
        try:
            child = _add_child(project, contract, "new reduction premise")
            _, wl, _ = _dispatch(project, contract, root, "first")
            start, launch, result, end = _receipts(wl)
            before_managed_round(wl, start, launch)
            service = ResearchService(project, actor="auditor")
            closing = Obligation.build(contract_id=contract.contract_id,
                exact_statement="the premise implies the target", scope=contract.scope,
                desired_output="proof", created_by="auditor", version=1)
            service.apply([{"event_type": "obligation.created", "payload": {"obligation": closing.to_dict()}}])
            fid = "9" * 16
            _write_fact(project.project_dir, fid, closing.exact_statement)
            _write_admission(project.project_dir, fid, closing.exact_statement)
            evidence = service.admit_fact(fid, closing.obligation_id,
                confirmed_scope_hash=_digest(closing.scope.to_dict()), alignment_note="checked implication")
            relation = Relation.build(relation_type=RelationType.REDUCES_TO,
                source=root.obligation_id, target=child.obligation_id,
                exact_claim=closing.exact_statement, scope=contract.scope,
                evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                evidence_refs=(evidence["evidence_id"],), reviewer="auditor",
                contract_id=contract.contract_id, definitions_hash=contract.scope.definitions_hash)
            service.apply([{"event_type": "relation.created", "payload": {"relation": relation.to_dict()}}])
            first = after_managed_round(wl, start, launch, result, end)
            assert first.outcome_ref == "REDUCTION_VERIFIED"
            binding, wl, _ = _dispatch(project, contract, root, "second")
            assert relation.relation_id in binding.baseline_relation_ids
            start, launch, result, end = _receipts(wl, round_id=2)
            before_managed_round(wl, start, launch)
            second = after_managed_round(wl, start, launch, result, end)
            assert second.outcome_ref == "NO_VERIFIABLE_PROGRESS"
            assert project.research_view().get_obligation(root.obligation_id).status.value == "OPEN"
            assert project.research_view().get_obligation(child.obligation_id).status.value == "OPEN"
        finally:
            project.close()


def test_rehashed_sidecar_cannot_change_authoritative_dependency_metadata():
    import json
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root = _managed(Path(raw))
        try:
            binding, _, _ = _dispatch(project, contract, root, "protected-metadata")
            side = load_dispatch(project.project_dir, "w1")
            side["binding"]["frontier_hash"] = "0" * 64
            side["sidecar_hash"] = _sidecar_body_hash(side)
            sidecar_path(project.project_dir, "w1").write_text(json.dumps(side))
            with pytest.raises(DispatchError, match="authoritative"):
                validate_dispatch(project.project_dir, "w1", require_ready=True)
        finally:
            project.close()
