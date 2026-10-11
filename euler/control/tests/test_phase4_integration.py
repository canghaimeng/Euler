"""Codex-owned source snapshot → bounded context integration test."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from euler.control.adapters.project import snapshot_project
from euler.control.artifacts import ArtifactStore
from euler.control.cache import ContextInvalidationReason, compare_context_keys
from euler.control.context import ContextCompiler, ContextLimits, ContextRequest
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    TargetKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation
from euler.control.progress import ActionSpec
from euler.control.schema import EventEnvelope
from euler.control.views import ResearchView


def _fact(fid: str, statement: str, proof: str, predecessors: tuple[str, ...]) -> str:
    return (
        "---\n"
        f"fact_id: {fid}\n"
        "problem_id: phase4\n"
        "author: verifier\n"
        f"predecessors: [{', '.join(predecessors)}]\n"
        "glossary_introduces: {}\n"
        "external_refs: []\n"
        "---\n\n"
        f"## statement\n{statement}\n\n"
        f"## proof\n{proof}\n"
    )


def _event(key: str, event_type: str, payload: dict) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="phase4",
        actor="codex-main",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc="2026-09-17T00:00:00Z",
    )


def test_source_snapshot_compiles_exact_bounded_packet_and_semantic_key():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        project = root / "project"
        facts = project / "fact_graph" / "facts"
        facts.mkdir(parents=True)
        parent_id = "1111111111111111"
        child_id = "2222222222222222"
        unrelated_id = "3333333333333333"
        parent_raw = _fact(parent_id, "Parent statement", "Exact parent proof.", ())
        child_raw = _fact(
            child_id,
            "Child statement",
            "Exact child proof using parent.",
            (parent_id,),
        )
        (facts / f"{parent_id}.md").write_text(parent_raw, encoding="utf-8")
        (facts / f"{child_id}.md").write_text(child_raw, encoding="utf-8")
        (facts / f"{unrelated_id}.md").write_text(
            _fact(unrelated_id, "Unrelated", "Must not leak.", ()),
            encoding="utf-8",
        )
        snapshot = snapshot_project(project)

        scope = MathematicalScope.build(
            assumptions=("fixed assumption",),
            quantifiers=(),
            objects=(),
            parameter_domain="fixed domain",
            definitions_hash=sha256_hex("phase4-definitions"),
        )
        contract = ProblemContract.build(
            problem_id="phase4",
            version=1,
            exact_statement="Exact target statement",
            scope=scope,
            permitted_tools=("reasoning",),
            external_help_policy="none",
            target_kind=TargetKind.PROOF,
            required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
            completion_checks=("verify",),
            budget_policy_ref="budget-v1",
        )
        obligation = Obligation.build(
            contract_id=contract.contract_id,
            exact_statement=contract.exact_statement,
            scope=scope,
            desired_output="complete proof",
            created_by="codex-main",
            version=1,
        )
        store = ArtifactStore(root / "artifacts")
        proof_bytes = b"verifier evidence payload"
        ref = store.put_bytes(proof_bytes)
        evidence = EvidenceArtifact.build(
            content_hash=ref.content_hash,
            producer="verifier",
            contract_id=contract.contract_id,
            evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
            exact_claim=contract.exact_statement,
            scope=scope,
            input_hashes=(),
            environment_hash=sha256_hex("verify-env"),
            checker_id="verifier",
            checker_version="1",
            checker_result=CheckerResult.PASS,
            created_at="2026-09-17T00:00:00Z",
        )
        view = ResearchView()
        view.apply(1, _event("1", "problem.contract.created", {"contract": contract.to_dict()}))
        view.apply(2, _event("2", "evidence.registered", {"evidence": evidence.to_dict()}))
        view.apply(3, _event("3", "obligation.created", {"obligation": obligation.to_dict()}))

        action = ActionSpec.build(
            action_type="prove",
            mechanism="compile exact context",
            model_id="grok-4.6",
            effort="high",
            task_mode="direct",
            context_hash=sha256_hex("context-placeholder"),
            dependency_hash=sha256_hex("dependencies"),
            definitions_hash=scope.definitions_hash,
            tool_environment_hash=sha256_hex("tools"),
            parameters_json=canonical_json({}),
            obligation_id=obligation.obligation_id,
        )
        base_kwargs = dict(
            request_id="phase4-request",
            contract_id=contract.contract_id,
            obligation_id=obligation.obligation_id,
            action_id=action.action_id,
            concrete_gap="Prove the final bridge without changing scope.",
            output_contract="Return one complete proof with cited facts.",
            tool_environment_hash=action.tool_environment_hash,
            acceptance_policy_hash=sha256_hex("acceptance-v1"),
            reference_versions_hash=sha256_hex("references-v1"),
            expand_evidence_content=True,
            limits=ContextLimits.unlimited(),
            required_fact_ids=(child_id,),
            required_evidence_ids=(evidence.artifact_id,),
        )
        compiler = ContextCompiler()
        packet = compiler.compile(ContextRequest(**base_kwargs), view, snapshot, store)
        assert packet.manifest.included_fact_ids == (parent_id, child_id)
        assert packet.fact_markdown == (parent_raw, child_raw)
        assert proof_bytes in packet.evidence_bytes
        assert "Parent statement" in packet.rendered_text
        assert "Child statement" in packet.rendered_text
        assert "proof_handle:" in packet.rendered_text
        assert parent_id in packet.rendered_text
        assert child_id in packet.rendered_text
        assert "Exact parent proof." not in packet.rendered_text
        assert "Exact child proof using parent." not in packet.rendered_text
        assert "Exact parent proof." in packet.fact_markdown[0]
        assert "Exact child proof using parent." in packet.fact_markdown[1]
        expanded = compiler.compile(
            ContextRequest(**{**base_kwargs, "expand_proof_content": True}),
            view,
            snapshot,
            store,
        )
        assert "Exact parent proof." in expanded.rendered_text
        assert "Exact child proof using parent." in expanded.rendered_text
        assert "Must not leak." not in packet.rendered_text
        assert packet.concrete_gap in packet.rendered_text
        assert packet.cache_key.key_id in packet.rendered_text
        assert packet.manifest.measured_bytes == len(packet.rendered_utf8())

        changed = dict(base_kwargs)
        changed["acceptance_policy_hash"] = sha256_hex("acceptance-v2")
        packet2 = compiler.compile(ContextRequest(**changed), view, snapshot, store)
        assert compare_context_keys(packet.cache_key, packet2.cache_key) == (
            ContextInvalidationReason.ACCEPTANCE,
        )


def main() -> None:
    test_source_snapshot_compiles_exact_bounded_packet_and_semantic_key()
    print("  [ok] test_source_snapshot_compiles_exact_bounded_packet_and_semantic_key")
    print("ALL PHASE 4 INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
