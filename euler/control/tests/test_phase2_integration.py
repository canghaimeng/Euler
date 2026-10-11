"""Codex-owned end-to-end replay tests for Phase 2."""

from __future__ import annotations

import tempfile
from pathlib import Path

from euler.control.artifacts import ArtifactStore
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.event_store import EventStore
from euler.control.ids import sha256_hex
from euler.control.obligations import Obligation, Route
from euler.control.schema import EventEnvelope
from euler.control.views import ResearchView, RouteReadiness


def _event(seq_name: str, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="phase2",
        actor="codex-main",
        event_type=event_type,
        idempotency_key=seq_name,
        payload=payload,
        timestamp_utc="2026-09-17T00:00:00Z",
        correlation_id="phase2-integration",
    )


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is a nonnegative integer",),
        quantifiers=(
            QuantifierBinding(
                variable="n",
                kind=QuantifierKind.FORALL,
                domain="Z_{>=0}",
            ),
        ),
        objects=(),
        parameter_domain="all n in Z_{>=0}",
        definitions_hash=sha256_hex("phase2-definitions-v1"),
    )


def test_artifact_event_replay_invalidation_and_reopen():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        artifact_store = ArtifactStore(root / "artifacts")
        blob = artifact_store.put_text("checked proof body", media_type="text/plain")
        scope = _scope()
        contract = ProblemContract.build(
            problem_id="phase2",
            version=1,
            exact_statement="For every n, the target identity holds.",
            scope=scope,
            permitted_tools=("llm",),
            external_help_policy="none",
            target_kind=TargetKind.PROOF,
            required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
            completion_checks=("independent verifier",),
            budget_policy_ref="budget-v1",
        )
        child = Obligation.build(
            contract_id=contract.contract_id,
            exact_statement="For every n, the bridge identity holds.",
            scope=scope,
            desired_output="proof",
            created_by="codex-main",
            version=1,
        )
        target = Obligation.build(
            contract_id=contract.contract_id,
            exact_statement=contract.exact_statement,
            scope=scope,
            desired_output="proof",
            created_by="codex-main",
            version=1,
        )
        proof = EvidenceArtifact.build(
            content_hash=blob.content_hash,
            producer="verifier",
            contract_id=contract.contract_id,
            evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
            exact_claim=child.exact_statement,
            scope=scope,
            input_hashes=(),
            environment_hash=sha256_hex("verifier-env-v1"),
            checker_id="cold-start-verifier",
            checker_version="1",
            checker_result=CheckerResult.PASS,
            created_at="2026-09-17T00:00:00Z",
        )
        route = Route.build(
            target_obligation_id=target.obligation_id,
            mechanism="derive target after bridge review",
            required_obligation_ids=(child.obligation_id,),
            alternative_group="main",
        )

        events = [
            _event("1", "problem.contract.created", {"contract": contract.to_dict()}),
            _event("2", "evidence.registered", {"evidence": proof.to_dict()}),
            _event("3", "obligation.created", {"obligation": child.to_dict()}),
            _event("4", "obligation.created", {"obligation": target.to_dict()}),
            _event("5", "route.created", {"route": route.to_dict()}),
            _event(
                "6",
                "obligation.status_changed",
                {
                    "obligation_id": child.obligation_id,
                    "status": "DISCHARGED",
                    "evidence_refs": [proof.artifact_id],
                },
            ),
        ]

        with EventStore(root / "control.sqlite3") as store:
            for event in events:
                store.append(event)
            view = ResearchView()
            view.replay(store.iter_project("phase2"))
            assert view.get_obligation(child.obligation_id).status.value == "DISCHARGED"
            assert view.get_obligation(target.obligation_id).status.value == "OPEN"
            assert view.route_readiness(route.route_id) is RouteReadiness.READY_FOR_REVIEW

            store.append(
                _event(
                    "7",
                    "evidence.invalidated",
                    {"artifact_id": proof.artifact_id},
                )
            )
            stale = ResearchView()
            stale.replay(store.iter_project("phase2"))
            assert child.obligation_id in stale.stale_dependencies().obligation_ids
            assert stale.route_readiness(route.route_id) is RouteReadiness.STALE
            assert stale.get_obligation(child.obligation_id).status.value == "DISCHARGED"

            store.append(
                _event(
                    "8",
                    "obligation.reopened",
                    {
                        "obligation_id": child.obligation_id,
                        "reason": "new formal tool is available",
                        "new_tool": "lean-v-next",
                    },
                )
            )
            reopened = ResearchView()
            reopened.replay(store.iter_project("phase2"))
            child_now = reopened.get_obligation(child.obligation_id)
            assert child_now.status.value == "OPEN"
            assert child_now.validity.value == "VALID"
            assert child_now.evidence_refs == ()
            assert reopened.route_readiness(route.route_id) is RouteReadiness.WAITING
            assert reopened.get_obligation(target.obligation_id).status.value == "OPEN"


def main() -> None:
    test_artifact_event_replay_invalidation_and_reopen()
    print("  [ok] test_artifact_event_replay_invalidation_and_reopen")
    print("ALL PHASE 2 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
