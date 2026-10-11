"""Real public kernel-check/alignment workflow, separated from Fact admission."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

import pytest

from euler.control.tests.test_kernel_evidence import _contract, _obligation, _write_lean_project
from euler.control.tests.test_final_audit import _policy
from euler.control.project import initialize_project
from euler.control.research_service import ResearchService, ResearchServiceError, _digest
from euler.control.contracts import CheckerResult, EvidenceArtifact, EvidenceKind, TargetKind
from euler.control.final_audit import FinalAuditStatus, VerifyResult, run_final_audit
from euler.control.ids import sha256_hex
from euler.control.kernel_service import (
    align_kernel,
    check_kernel,
    latest_kernel_alignment,
    select_kernel_bundle,
)
from euler.control.obligations import ObligationStatus
from euler.control.schema import EventEnvelope


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_real_kernel_action_child_alignment_and_stale_source(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        math = _write_lean_project(root / "math", "theorem two_add_two : 2 + 2 = 4 := rfl\n")
        project_dir = root / "demo"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(json.dumps({"name": "demo", "workers": []}))
        contract = _contract()
        target = _obligation(contract)
        child = _obligation(contract, statement="two plus two equals four as an intermediate lemma")
        project = initialize_project(project_dir, contract, target, _policy(), "tester", "2026-01-01T00:00:00Z")
        try:
            service = ResearchService(project, actor="independent-auditor")
            service.apply([{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}])
            request = {"project_dir": str(math), "module_name": "TinyKernel",
                       "theorem_name": "two_add_two", "expected_type": "2 + 2 = 4", "timeout_ms": 60000}
            with pytest.raises(ResearchServiceError, match="executable overrides"):
                check_kernel(project, child.obligation_id, {**request, "lake_bin": "/tmp/fake"}, request_id="fake", actor="auditor")
            result = check_kernel(project, child.obligation_id, request, request_id="real-1", actor="auditor")
            assert result["status"] == "PASS", result
            assert not project.research_view().evidence()
            repeat = check_kernel(project, child.obligation_id, request, request_id="real-1", actor="auditor")
            assert repeat["replayed"] and repeat["kernel_artifact_hash"] == result["kernel_artifact_hash"]
            kwargs = {"formal_statement": result["formal_statement"],
                      "confirmed_scope_hash": _digest(child.scope.to_dict()), "notes": "Reviewed exact arithmetic lemma and all assumptions", "actor": "independent-auditor"}
            with pytest.raises(ResearchServiceError, match="actual checked theorem type"):
                align_kernel(project, child.obligation_id, result["kernel_artifact_hash"], **{**kwargs, "formal_statement": "False"})
            admitted = align_kernel(project, child.obligation_id, result["kernel_artifact_hash"], **kwargs)
            time.sleep(1.05)
            assert align_kernel(project, child.obligation_id, result["kernel_artifact_hash"], **kwargs) == admitted
            assert latest_kernel_alignment(project, child.obligation_id).exact_claim == child.exact_statement
            service.apply([{"event_type": "obligation.status_changed", "payload": {
                "obligation_id": child.obligation_id, "status": "DISCHARGED", "evidence_refs": [admitted["evidence_id"]]}}])
            assert project.research_view().get_obligation(target.obligation_id).status.value == "OPEN"
            (math / "TinyKernel.lean").write_text("axiom fake : False\ntheorem two_add_two : 2 + 2 = 4 := rfl\n")
            with pytest.raises(ResearchServiceError, match="source version changed"):
                align_kernel(project, child.obligation_id, result["kernel_artifact_hash"], **kwargs)
            archive = json.loads(project.artifacts.get_bytes(result["source_archive_hash"]).decode("utf-8"))
            assert "TinyKernel.lean" in archive
            assert "lean-toolchain" in archive
            assert "lakefile.toml" in archive
        finally:
            project.close()


class FixtureVerifierTransport:
    """Deterministic verifier fixture. Not a live Euler verifier service."""

    def __init__(self, verdict: str = "correct"):
        self.verdict = verdict
        self.calls = []

    def verify(self, statement: str, proof: str) -> VerifyResult:
        self.calls.append((statement, proof))
        return VerifyResult(
            transport_status="ok",
            verdict=self.verdict,
            detail="fixture-verifier",
            body={"verdict": self.verdict, "verification_report": "fixture"},
        )


def _admit_fact(project, claim: str, proof: str, fact_id: str | None = None) -> str:
    fid = fact_id or sha256_hex("e2e-fact")[:16]
    facts = project.project_dir / "fact_graph" / "facts"
    facts.mkdir(parents=True, exist_ok=True)
    markdown = (
        f"---\nfact_id: {fid}\nproblem_id: two-plus-two\nauthor: worker\n"
        f"predecessors: []\nglossary_introduces: {{}}\nexternal_refs: []\n---\n\n"
        f"## statement\n{claim}\n\n## proof\n{proof}\n"
    )
    (facts / f"{fid}.md").write_text(markdown, encoding="utf-8")
    mem = project.project_dir / "global_memory"
    mem.mkdir(parents=True, exist_ok=True)
    row = {
        "id": "admission-e2e",
        "kind": "verification",
        "claim": claim,
        "provenance": "fact_submit",
        "transport_status": "ok",
        "verdict": "correct",
        "fact_id": fid,
    }
    (mem / "verification.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return fid


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_two_kernel_versions_select_coherent_alignment_and_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        math = _write_lean_project(root / "math", "theorem two_add_two : 2 + 2 = 4 := rfl\n")
        project_dir = root / "demo"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(json.dumps({"name": "demo", "workers": []}))
        contract = _contract()
        target = _obligation(contract)
        project = initialize_project(project_dir, contract, target, _policy(), "tester", "2026-01-01T00:00:00Z")
        try:
            request = {"project_dir": str(math), "module_name": "TinyKernel",
                       "theorem_name": "two_add_two", "expected_type": "2 + 2 = 4", "timeout_ms": 60000}
            first = check_kernel(project, target.obligation_id, request, request_id="v1", actor="auditor")
            kwargs = {"formal_statement": first["formal_statement"],
                      "confirmed_scope_hash": _digest(target.scope.to_dict()),
                      "notes": "Reviewed version 1 of the formalization", "actor": "independent-auditor"}
            aligned_v1 = align_kernel(project, target.obligation_id, first["kernel_artifact_hash"], **kwargs)
            (math / "TinyKernel.lean").write_text(
                "-- version 2\ntheorem two_add_two : 2 + 2 = 4 := rfl\n"
            )
            second = check_kernel(project, target.obligation_id, request, request_id="v2", actor="auditor")
            assert second["kernel_artifact_hash"] != first["kernel_artifact_hash"]
            kwargs2 = {**kwargs, "notes": "Reviewed version 2 of the formalization"}
            aligned_v2 = align_kernel(project, target.obligation_id, second["kernel_artifact_hash"], **kwargs2)
            assert aligned_v2["evidence_id"] != aligned_v1["evidence_id"]
            latest = latest_kernel_alignment(project, target.obligation_id)
            assert latest is not None
            assert latest.kernel_artifact_hash == second["kernel_artifact_hash"]
            bundle = select_kernel_bundle(project, target.obligation_id)
            assert bundle is not None
            assert bundle.evidence_id == aligned_v2["evidence_id"]
            assert bundle.alignment.attestation_id == latest.attestation_id
            older = select_kernel_bundle(
                project, target.obligation_id, preferred_alignment=kernel_alignment_for(project, aligned_v1)
            )
            assert older is not None
            assert older.evidence_id == aligned_v1["evidence_id"]
            assert older.kernel_artifact_hash == first["kernel_artifact_hash"]
            ResearchService(project, actor="independent-auditor").apply([{
                "event_type": "obligation.status_changed",
                "payload": {
                    "obligation_id": target.obligation_id,
                    "status": "DISCHARGED",
                    "evidence_refs": [aligned_v1["evidence_id"], aligned_v2["evidence_id"]],
                },
            }])
            _admit_fact(project, contract.exact_statement, "Two-version coherent audit fixture.")
            latest = latest_kernel_alignment(project, target.obligation_id)
            run = run_final_audit(
                project,
                transport=FixtureVerifierTransport("correct"),
                timestamp_utc="2026-01-01T00:00:00Z",
                alignment=latest,
            )
            assert run.status is FinalAuditStatus.SOLVED
            assert run.evidence_digest == second["kernel_artifact_hash"]
            assert run.alignment_digest == latest.attestation_id
            replay = run_final_audit(
                project,
                transport=FixtureVerifierTransport("wrong"),
                timestamp_utc="2026-01-01T00:00:00Z",
                alignment=latest,
            )
            assert replay.replayed is True and replay.status is FinalAuditStatus.SOLVED
            assert replay.identity_hash == run.identity_hash
        finally:
            project.close()


def kernel_alignment_for(project, admitted):
    from euler.control.kernel_evidence import StatementAlignmentAttestation
    blob = project.artifacts.get_bytes(admitted["alignment_hash"])
    return StatementAlignmentAttestation.from_dict(json.loads(blob))


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_formalization_e2e_lean_alignment_admission_and_final_audit():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        math = _write_lean_project(root / "math", "theorem two_add_two : 2 + 2 = 4 := rfl\n")
        project_dir = root / "demo"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(json.dumps({"name": "demo", "workers": ["w1"]}))
        (project_dir / "workers" / "w1").mkdir(parents=True)
        contract = _contract()
        assert contract.target_kind is TargetKind.FORMALIZATION
        assert contract.required_evidence_level is EvidenceKind.KERNEL_CHECKED_PROOF
        target = _obligation(contract)
        project = initialize_project(project_dir, contract, target, _policy(), "tester", "2026-01-01T00:00:00Z")
        try:
            service = ResearchService(project, actor="independent-auditor")
            request = {"project_dir": str(math), "module_name": "TinyKernel",
                       "theorem_name": "two_add_two", "expected_type": "2 + 2 = 4", "timeout_ms": 60000}
            with pytest.raises(ResearchServiceError, match="executable overrides"):
                check_kernel(project, target.obligation_id, {**request, "status": "PASS"}, request_id="json", actor="auditor")
            checked = check_kernel(project, target.obligation_id, request, request_id="e2e-1", actor="auditor")
            assert checked["status"] == "PASS"
            kwargs = {"formal_statement": checked["formal_statement"],
                      "confirmed_scope_hash": _digest(target.scope.to_dict()),
                      "notes": "Auditor reviewed that the Lean type matches the natural-language lemma",
                      "actor": "independent-auditor"}
            aligned = align_kernel(project, target.obligation_id, checked["kernel_artifact_hash"], **kwargs)
            fixture = FixtureVerifierTransport("correct")
            with pytest.raises(Exception):
                run_final_audit(project, transport=fixture, timestamp_utc="2026-01-01T00:00:00Z")
            service.apply([{
                "event_type": "obligation.status_changed",
                "payload": {
                    "obligation_id": target.obligation_id,
                    "status": "DISCHARGED",
                    "evidence_refs": [aligned["evidence_id"]],
                },
            }])
            with pytest.raises(Exception, match="admission"):
                run_final_audit(project, transport=fixture, timestamp_utc="2026-01-01T00:00:00Z")
            _admit_fact(project, contract.exact_statement, "Two plus two equals four by the kernel-checked argument.")
            alignment = latest_kernel_alignment(project, target.obligation_id)
            solved = run_final_audit(
                project, transport=fixture, timestamp_utc="2026-01-01T00:00:00Z", alignment=alignment
            )
            assert solved.status is FinalAuditStatus.SOLVED
            assert solved.replayed is False
            assert fixture.calls
            replay = run_final_audit(
                project, transport=FixtureVerifierTransport("wrong"), timestamp_utc="2026-01-01T00:00:00Z",
                alignment=alignment,
            )
            assert replay.replayed is True
            assert replay.status is FinalAuditStatus.SOLVED
        finally:
            project.close()


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_formalization_negatives_wrong_target_axiom_llm_and_failed_audit():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project_dir = root / "demo"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(json.dumps({"name": "demo", "workers": ["w1"]}))
        (project_dir / "workers" / "w1").mkdir(parents=True)
        contract = _contract()
        target = _obligation(contract)
        project = initialize_project(project_dir, contract, target, _policy(), "tester", "2026-01-01T00:00:00Z")
        try:
            request = {"project_dir": "", "module_name": "TinyKernel",
                       "theorem_name": "two_add_two", "expected_type": "2 + 2 = 4", "timeout_ms": 60000}
            sorry_math = _write_lean_project(root / "sorry", "theorem two_add_two : 2 + 2 = 4 := sorry\n")
            sorry = check_kernel(project, target.obligation_id, {**request, "project_dir": str(sorry_math)},
                                 request_id="sorry", actor="auditor")
            assert sorry["status"] == "FAIL"
            with pytest.raises(ResearchServiceError, match="successful registered checker"):
                align_kernel(
                    project, target.obligation_id, sorry["kernel_artifact_hash"],
                    formal_statement=sorry.get("formal_statement") or "2 + 2 = 4",
                    confirmed_scope_hash=_digest(target.scope.to_dict()),
                    notes="Must not align a failed checker run", actor="independent-auditor",
                )
            axiom_math = _write_lean_project(
                root / "ax", "axiom userAx : 2 + 2 = 4\ntheorem two_add_two : 2 + 2 = 4 := userAx\n"
            )
            axed = check_kernel(project, target.obligation_id, {**request, "project_dir": str(axiom_math)},
                                request_id="axiom", actor="auditor")
            assert axed["status"] == "FAIL"
            premise_math = _write_lean_project(
                root / "prem", "theorem two_add_two (p : True) : 2 + 2 = 4 := rfl\n"
            )
            prem = check_kernel(project, target.obligation_id, {**request, "project_dir": str(premise_math)},
                                request_id="premise", actor="auditor")
            assert prem["status"] == "FAIL"
            good = _write_lean_project(root / "good", "theorem two_add_two : 2 + 2 = 4 := rfl\n")
            wrong = check_kernel(
                project, target.obligation_id,
                {**request, "project_dir": str(good), "theorem_name": "missing_thm"},
                request_id="wrong-thm", actor="auditor",
            )
            assert wrong["status"] == "FAIL"
            llm = EvidenceArtifact.build(
                content_hash=project.artifacts.put_text("llm-only").content_hash,
                producer="worker",
                contract_id=contract.contract_id,
                evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                exact_claim=contract.exact_statement,
                scope=contract.scope,
                environment_hash=sha256_hex("llm-env"),
                checker_id="euler-verifier",
                checker_version="1",
                checker_result=CheckerResult.PASS,
                created_at="2026-01-01T00:00:00Z",
            )
            project.event_store.append(EventEnvelope.build(
                project_id=project.project_id, actor="tester", event_type="evidence.registered",
                idempotency_key="llm-ev", payload={"evidence": llm.to_dict()},
                timestamp_utc="2026-01-01T00:00:00Z",
            ))
            with pytest.raises(Exception):
                ResearchService(project, actor="independent-auditor").apply([{
                    "event_type": "obligation.status_changed",
                    "payload": {
                        "obligation_id": target.obligation_id,
                        "status": "DISCHARGED",
                        "evidence_refs": [llm.artifact_id],
                    },
                }])
            passed = check_kernel(project, target.obligation_id, {**request, "project_dir": str(good)},
                                  request_id="good", actor="auditor")
            aligned = align_kernel(
                project, target.obligation_id, passed["kernel_artifact_hash"],
                formal_statement=passed["formal_statement"],
                confirmed_scope_hash=_digest(target.scope.to_dict()),
                notes="Reviewed the successful kernel artifact", actor="independent-auditor",
            )
            ResearchService(project, actor="independent-auditor").apply([{
                "event_type": "obligation.status_changed",
                "payload": {"obligation_id": target.obligation_id, "status": "DISCHARGED",
                            "evidence_refs": [aligned["evidence_id"]]},
            }])
            _admit_fact(project, contract.exact_statement, "fixture proof for a rejected audit")
            failed = run_final_audit(
                project,
                transport=FixtureVerifierTransport("wrong"),
                timestamp_utc="2026-01-01T00:00:00Z",
                alignment=latest_kernel_alignment(project, target.obligation_id),
            )
            assert failed.status is FinalAuditStatus.AUDIT_FAIL
            assert project.research_view().get_obligation(target.obligation_id).status is ObligationStatus.OPEN
        finally:
            project.close()


def _attach_git_package(project: Path) -> Path:
    from euler.control.tests.test_kernel_dependencies import _git_entry, _init_git, _write_manifest

    cache = project / ".lake" / "packages" / "foo"
    cache.mkdir(parents=True)
    (cache / "lakefile.toml").write_text(
        'name = "foo"\nversion = "0.1.0"\n[[lean_lib]]\nname = "Foo"\n',
        encoding="utf-8",
    )
    (cache / "Foo.lean").write_text("def ping : Nat := 0\n", encoding="utf-8")
    pin = _init_git(cache, "https://example.com/foo.git")
    _write_manifest(project, [_git_entry("foo", pin)])
    return cache


@pytest.mark.skipif(not shutil.which("lake"), reason="real local Lean toolchain is not installed")
def test_kernel_service_success_and_stale_dependency_cannot_replay():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        math = _write_lean_project(root / "math", "theorem two_add_two : 2 + 2 = 4 := rfl\n")
        cache = _attach_git_package(math)
        project_dir = root / "demo"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(json.dumps({"name": "demo", "workers": []}))
        contract = _contract()
        target = _obligation(contract)
        project = initialize_project(project_dir, contract, target, _policy(), "tester", "2026-01-01T00:00:00Z")
        try:
            request = {
                "project_dir": str(math),
                "module_name": "TinyKernel",
                "theorem_name": "two_add_two",
                "expected_type": "2 + 2 = 4",
                "timeout_ms": 60000,
            }
            result = check_kernel(project, target.obligation_id, request, request_id="dep-1", actor="auditor")
            assert result["status"] == "PASS", result
            assert result["dependency_fingerprint"]
            assert len(result["dependency_fingerprint"]) == 64
            kwargs = {
                "formal_statement": result["formal_statement"],
                "confirmed_scope_hash": _digest(target.scope.to_dict()),
                "notes": "Reviewed the kernel-checked lemma against pinned dependencies",
                "actor": "independent-auditor",
            }
            admitted = align_kernel(project, target.obligation_id, result["kernel_artifact_hash"], **kwargs)
            assert admitted["evidence_id"]
            replay = check_kernel(project, target.obligation_id, request, request_id="dep-1", actor="auditor")
            assert replay["replayed"] is True
            (cache / "stale.txt").write_text("cache mutated\n", encoding="utf-8")
            with pytest.raises(ResearchServiceError, match="dirty"):
                check_kernel(project, target.obligation_id, request, request_id="dep-1", actor="auditor")
            with pytest.raises(ResearchServiceError, match="dirty"):
                align_kernel(project, target.obligation_id, result["kernel_artifact_hash"], **kwargs)
        finally:
            project.close()
