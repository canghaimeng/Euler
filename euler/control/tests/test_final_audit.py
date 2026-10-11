"""Focused tests for Gate 3C-2 independent final audit.

Injected verify transport only. No network, no provider launch, no truth writes
from the audit module.
"""

from __future__ import annotations

import json
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

from euler.control.acceptance import AcceptanceVerdict, ProjectOutcome
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
from euler.control.failures import FailureKind
from euler.control.final_audit import (
    FINAL_ACCEPTANCE_POLICY_HASH,
    FINAL_AUDIT_RETRY_SPEC,
    FinalAuditError,
    FinalAuditStatus,
    HttpVerifyTransport,
    VerifyResult,
    extract_proof_body,
    run_final_audit,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import (
    EVENT_ACCEPTANCE_RECORDED,
    EVENT_CHECKPOINT_CREATED,
)
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetPolicy, DimensionLimit, OnDeadlinePolicy
from euler.control.project import initialize_project
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import ScheduleDisposition, ScheduleReason
from euler.control.schema import EventEnvelope, MathematicalStatus

_TS = "2026-01-01T00:00:00Z"
_CLAIM = "every even integer greater than 2 is a sum of two primes"
_PROOF = "Goldbach's even integers decompose into two primes by the checked argument."


class _FakeTransport:
    def __init__(self, result: VerifyResult):
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def verify(self, statement: str, proof: str) -> VerifyResult:
        self.calls.append((statement, proof))
        return self.result


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
        exact_statement=_CLAIM,
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


def _policy(*, cost=None, verifier_slots=1, reserve_bp=1000, deadline=None, on_deadline=OnDeadlinePolicy.REJECT_NORMAL) -> BudgetPolicy:
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
        max_verifier_slots=verifier_slots,
        final_audit_reserve_bp=reserve_bp,
        deadline_utc=deadline,
        on_deadline=on_deadline,
    )


def _ok(verdict: str) -> VerifyResult:
    return VerifyResult(
        transport_status="ok",
        verdict=verdict,
        detail="",
        body={"verdict": verdict, "verification_report": "injected"},
    )


def _seed(tmp: Path, *, discharge=True, write_fact=True, write_admission=True, proof=_PROOF, **policy_kw):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": ["w1"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    (root / "workers" / "w1").mkdir(parents=True)
    contract = _contract()
    obligation = _obligation(contract)
    project = initialize_project(
        root, contract, obligation, _policy(**policy_kw), "tester", _TS
    )
    evidence = None
    fact_id = sha256_hex("target-fact")[:16]
    if discharge:
        proof_ref = project.artifacts.put_text("Independent proof evidence.")
        evidence = EvidenceArtifact.build(
            content_hash=proof_ref.content_hash,
            producer="worker",
            contract_id=contract.contract_id,
            evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
            exact_claim=_CLAIM,
            scope=contract.scope,
            environment_hash=_hex("proof-env"),
            checker_id="euler-verifier",
            checker_version="1",
            checker_result=CheckerResult.PASS,
            created_at=_TS,
        )
        project.event_store.append(
            EventEnvelope.build(
                project_id=project.project_id,
                actor="tester",
                event_type="evidence.registered",
                idempotency_key=f"evidence:{evidence.artifact_id}",
                payload={"evidence": evidence.to_dict()},
                timestamp_utc=_TS,
            )
        )
        project.event_store.append(
            EventEnvelope.build(
                project_id=project.project_id,
                actor="tester",
                event_type="obligation.status_changed",
                idempotency_key=f"discharge:{obligation.obligation_id}",
                payload={
                    "obligation_id": obligation.obligation_id,
                    "status": "DISCHARGED",
                    "evidence_refs": [evidence.artifact_id],
                },
                timestamp_utc=_TS,
            )
        )
    if write_fact:
        facts = root / "fact_graph" / "facts"
        facts.mkdir(parents=True)
        markdown = (
            f"---\n"
            f"fact_id: {fact_id}\n"
            f"problem_id: goldbach\n"
            f"author: worker\n"
            f"predecessors: []\n"
            f"glossary_introduces: {{}}\n"
            f"external_refs: []\n"
            f"---\n\n"
            f"## statement\n{_CLAIM}\n\n"
            f"## proof\n{proof}\n"
        )
        (facts / f"{fact_id}.md").write_text(markdown, encoding="utf-8")
    if write_admission:
        mem = root / "global_memory"
        mem.mkdir(parents=True, exist_ok=True)
        row = {
            "id": "admission-1",
            "kind": "verification",
            "claim": _CLAIM,
            "provenance": "fact_submit",
            "transport_status": "ok",
            "verdict": "correct",
            "fact_id": fact_id,
        }
        (mem / "verification.jsonl").write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return project, contract, obligation, fact_id, evidence


def _event_types(project) -> list[str]:
    return [
        envelope.event_type
        for _seq, envelope in project.event_store.iter_project(project.project_id)
    ]


def test_extract_proof_body():
    text = "## statement\nclaim\n\n## proof\nline one\nline two\n\n## intuition\nx\n"
    assert extract_proof_body(text) == "line one\nline two"
    try:
        extract_proof_body("## statement\nonly\n")
        raise AssertionError("empty proof must fail")
    except FinalAuditError:
        pass


def test_extract_proof_preserves_internal_headings_and_fenced_examples():
    proof = ("Introduction.\n\n## Theorem\nAll inputs.\n\n"
             "## Reduction\nA -> B.\n```markdown\n## intuition\n"
             "This is part of a fenced example.\n```\n\n"
             "## Correctness\nBoth implications.\n\n"
             "## Encoding\nPolynomial bit length.")
    text = "## statement\nclaim\n\n## proof\n" + proof + "\n\n## intuition\nmetadata\n"
    assert extract_proof_body(text) == proof
    assert extract_proof_body(text.split("\n\n## intuition\nmetadata")[0]) == proof


def test_extract_proof_preserves_significant_whitespace():
    proof = "Step one.  \n\n## Step two\n    indented content  \nDone."
    assert extract_proof_body("## proof\n" + proof + "\n") == proof


def test_correct_verify_solves_and_replays_without_second_call():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, _obligation, fact_id, _ev = _seed(Path(raw))
        try:
            transport = _FakeTransport(_ok("correct"))
            with patch("euler.execution.scaffold.spawn_loop") as spawn:
                run = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            spawn.assert_not_called()
            assert run.status is FinalAuditStatus.SOLVED
            assert run.replayed is False
            assert run.verdict == "correct"
            assert run.outcome == ProjectOutcome.SOLVED.value
            assert transport.calls == [(_CLAIM, _PROOF)]
            types = _event_types(project)
            assert EVENT_ACCEPTANCE_RECORDED in types
            assert EVENT_CHECKPOINT_CREATED in types
            lease = next(
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == run.lease_id
            )
            assert lease.status is LeaseStatus.COMPLETED
            assert lease.mathematical_status is MathematicalStatus.CHECKED_CORRECT
            assert lease.consumed.wall_ms >= 0
            mem = (project.project_dir / "global_memory" / "verification.jsonl").read_text(
                encoding="utf-8"
            )
            assert mem.count("\n") == 1
            facts = list((project.project_dir / "fact_graph" / "facts").glob("*.md"))
            assert len(facts) == 1
            again = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert again.replayed is True
            assert again.status is FinalAuditStatus.SOLVED
            assert again.acceptance_id == run.acceptance_id
            assert len(transport.calls) == 1
            reserved = [
                item
                for item in project.runtime_snapshot().leases
                if item.status is LeaseStatus.COMPLETED
            ]
            assert len(reserved) == 1
        finally:
            project.close()


def test_wrong_is_fail_not_refuted():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(Path(raw))
        try:
            transport = _FakeTransport(_ok("wrong"))
            run = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is FinalAuditStatus.AUDIT_FAIL
            assert run.outcome != ProjectOutcome.REFUTED.value
            lease = next(
                item
                for item in project.runtime_snapshot().leases
                if item.lease_id == run.lease_id
            )
            assert lease.mathematical_status is MathematicalStatus.CHECKED_WRONG
            acc = None
            for _seq, envelope in project.event_store.iter_project(project.project_id):
                if envelope.event_type == EVENT_ACCEPTANCE_RECORDED:
                    acc = envelope.payload["acceptance"]
            assert acc["verdict"] == AcceptanceVerdict.FAIL.value
            assert acc["project_outcome"] is None
            assert EVENT_CHECKPOINT_CREATED not in _event_types(project)
            live = project.research_view().get_obligation(
                project.manifest.target_obligation_id
            )
            assert live is not None
            assert live.status is ObligationStatus.OPEN
            failures = project.research_view().failures()
            assert failures
            objection = failures[-1]
            assert objection.kind is FailureKind.INVALID_PROOF
            assert objection.repair_conditions
            assert "objection" in objection.repair_conditions[0]
            try:
                run_final_audit(
                    project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
                )
                raise AssertionError("rejected audit must reopen and block replay")
            except FinalAuditError as exc:
                assert "DISCHARGED" in str(exc)
        finally:
            project.close()


def test_timeout_and_protocol_are_infrastructure():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(Path(raw) / "t")
        try:
            transport = _FakeTransport(
                VerifyResult(
                    transport_status="timeout",
                    verdict=None,
                    detail="timed out",
                    body={"transport_status": "timeout"},
                )
            )
            run = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is FinalAuditStatus.INFRA
            lease = next(iter(project.runtime_snapshot().leases))
            assert lease.status is LeaseStatus.INFRA_FAILED
            assert EVENT_ACCEPTANCE_RECORDED not in _event_types(project)
            again = run_final_audit(
                project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
            )
            assert again.replayed is True
            assert again.status is FinalAuditStatus.INFRA
            assert len(transport.calls) == 1
            recovered = run_final_audit(
                project,
                transport=_FakeTransport(_ok("correct")),
                timestamp_utc=_TS,
                retry_infra=True,
                retry_reason="verifier timeout recovered; retry with fresh reservation",
            )
            assert recovered.replayed is False
            assert recovered.status is FinalAuditStatus.SOLVED
            assert recovered.attempt_index == 2
            leases = [
                item
                for item in project.runtime_snapshot().leases
                if item.mechanism == "final-audit"
            ]
            assert len(leases) == 2
        finally:
            project.close()

        project, _c, _o, _f, _e = _seed(Path(raw) / "p")
        try:
            run = run_final_audit(
                project,
                transport=_FakeTransport(
                    VerifyResult(
                        transport_status="protocol_error",
                        verdict=None,
                        detail="bad json",
                        body={},
                    )
                ),
                timestamp_utc=_TS,
            )
            assert run.status is FinalAuditStatus.INFRA
        finally:
            project.close()


def test_deadline_still_selects_final_audit():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(
            Path(raw),
            deadline="2020-01-01T00:00:00Z",
            on_deadline=OnDeadlinePolicy.REJECT_NORMAL,
        )
        try:
            run = run_final_audit(
                project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
            )
            assert run.status is FinalAuditStatus.SOLVED
            assert run.disposition == ScheduleDisposition.SELECTED.value
        finally:
            project.close()


def test_budget_and_slot_defer_without_verify():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(
            Path(raw) / "b", cost=0, reserve_bp=0, verifier_slots=1
        )
        try:
            transport = _FakeTransport(_ok("correct"))
            run = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is FinalAuditStatus.DEFERRED
            assert run.reason == ScheduleReason.BUDGET_EXCEEDED.value
            assert transport.calls == []
            assert not project.runtime_snapshot().leases
        finally:
            project.close()

        project, _c, _o, _f, _e = _seed(
            Path(raw) / "s", verifier_slots=0, reserve_bp=0
        )
        try:
            transport = _FakeTransport(_ok("correct"))
            run = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is FinalAuditStatus.DEFERRED
            assert run.reason == ScheduleReason.SLOT_EXHAUSTED.value
            assert transport.calls == []
        finally:
            project.close()


def test_missing_fact_admission_evidence_and_proof_fail_closed():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(Path(raw) / "nd", discharge=False)
        try:
            try:
                run_final_audit(
                    project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
                )
                raise AssertionError("open obligation must fail")
            except FinalAuditError as exc:
                assert "DISCHARGED" in str(exc)
        finally:
            project.close()

        project, _c, _o, _f, _e = _seed(
            Path(raw) / "nf", write_fact=False, write_admission=True
        )
        try:
            try:
                run_final_audit(
                    project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
                )
                raise AssertionError("missing fact must fail")
            except FinalAuditError:
                pass
        finally:
            project.close()

        project, _c, _o, _f, _e = _seed(
            Path(raw) / "na", write_fact=True, write_admission=False
        )
        try:
            try:
                run_final_audit(
                    project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
                )
                raise AssertionError("missing admission must fail")
            except FinalAuditError as exc:
                assert "admission" in str(exc)
        finally:
            project.close()

        project, _c, _o, _f, _e = _seed(Path(raw) / "np", proof="")
        try:
            try:
                run_final_audit(
                    project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
                )
                raise AssertionError("empty proof must fail")
            except FinalAuditError as exc:
                assert "proof" in str(exc).lower()
        finally:
            project.close()


def test_cas_tamper_fails_replay_and_http_transport_is_default():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(Path(raw))
        try:
            run = run_final_audit(
                project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
            )
            blob = None
            for path in (project.control_dir / "artifacts").rglob("*"):
                if path.is_file() and not path.name.startswith("."):
                    if path.read_bytes() and run.verify_artifact_hash:
                        try:
                            if path.read_bytes() and project.artifacts.get_bytes(
                                run.verify_artifact_hash
                            ):
                                blob = path
                                break
                        except Exception:
                            continue
            assert run.verify_artifact_hash
            original = project.artifacts.get_bytes(run.verify_artifact_hash)
            # overwrite the stored blob in place
            dest = None
            for path in (project.control_dir / "artifacts").rglob("*"):
                if path.is_file() and path.read_bytes() == original:
                    dest = path
                    break
            assert dest is not None
            dest.write_bytes(b"tampered-not-the-original-bytes")
            try:
                run_final_audit(
                    project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
                )
                raise AssertionError("tampered CAS must fail closed")
            except FinalAuditError as exc:
                assert "tamper" in str(exc).lower() or "missing" in str(exc).lower()
            assert isinstance(HttpVerifyTransport(), HttpVerifyTransport)
            assert len(FINAL_ACCEPTANCE_POLICY_HASH) == 64
        finally:
            project.close()


def test_http_verify_transport_sends_force_fresh_true(monkeypatch):
    captured: dict = {}

    class _Resp:
        def read(self):
            return json.dumps(
                {
                    "verification_report": {"summary": "ok", "critical_errors": [], "gaps": []},
                    "verdict": "correct",
                    "repair_hints": "",
                }
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setenv("EULER_VERIFY_URL", "http://127.0.0.1:9/verify")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = HttpVerifyTransport().verify("stmt", "proof-body")
    assert result.transport_status == "ok"
    assert result.verdict == "correct"
    assert captured["body"]["statement"] == "stmt"
    assert captured["body"]["proof"] == "proof-body"
    assert captured["body"]["force_fresh"] is True
    assert FINAL_AUDIT_RETRY_SPEC["parameter"] == "retry_infra"
    assert FINAL_AUDIT_RETRY_SPEC["requires_nonempty_reason"] is True


def _rewrite_proof(project, fact_id: str, proof: str) -> None:
    path = project.project_dir / "fact_graph" / "facts" / f"{fact_id}.md"
    markdown = (
        f"---\n"
        f"fact_id: {fact_id}\n"
        f"problem_id: goldbach\n"
        f"author: worker\n"
        f"predecessors: []\n"
        f"glossary_introduces: {{}}\n"
        f"external_refs: []\n"
        f"---\n\n"
        f"## statement\n{_CLAIM}\n\n"
        f"## proof\n{proof}\n"
    )
    path.write_text(markdown, encoding="utf-8")


def test_changed_proof_or_evidence_triggers_new_audit():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation, fact_id, evidence = _seed(Path(raw))
        try:
            transport = _FakeTransport(_ok("correct"))
            first = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert first.status is FinalAuditStatus.SOLVED
            assert first.replayed is False
            _rewrite_proof(project, fact_id, "A revised checked argument for the same claim.")
            second = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert second.replayed is False
            assert second.status is FinalAuditStatus.SOLVED
            assert second.identity_hash != first.identity_hash
            assert second.proof_digest != first.proof_digest
            assert len(transport.calls) == 2
            assert transport.calls[1][1] == "A revised checked argument for the same claim."
            third = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert third.replayed is True
            assert third.identity_hash == second.identity_hash
            assert len(transport.calls) == 2
        finally:
            project.close()


def _register_repaired_evidence(project, contract, proof):
    """Simulate a new verifier admission; rejected evidence remains stale."""
    proof_ref = project.artifacts.put_text(proof)
    evidence = EvidenceArtifact.build(
        content_hash=proof_ref.content_hash, producer="repair-verifier",
        contract_id=contract.contract_id, evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exact_claim=_CLAIM, scope=contract.scope, environment_hash=_hex("repair-env"),
        checker_id="euler-verifier", checker_version="1", checker_result=CheckerResult.PASS,
        created_at=_TS,
    )
    project.event_store.append(EventEnvelope.build(
        project_id=project.project_id, actor="tester", event_type="evidence.registered",
        idempotency_key=f"repaired-evidence:{evidence.artifact_id}",
        payload={"evidence": evidence.to_dict()}, timestamp_utc=_TS,
    ))
    return evidence


def test_rejected_audit_redischarge_is_not_silent_fail_replay():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation, _f, evidence = _seed(Path(raw))
        try:
            failed = run_final_audit(
                project, transport=_FakeTransport(_ok("wrong")), timestamp_utc=_TS
            )
            assert failed.status is FinalAuditStatus.AUDIT_FAIL
            assert (
                project.research_view()
                .get_obligation(obligation.obligation_id)
                .status
                is ObligationStatus.OPEN
            )
            assert project.research_view().get_evidence(evidence.artifact_id).validity.value == "STALE"
            evidence = _register_repaired_evidence(project, _c, _PROOF)
            project.event_store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor="tester",
                    event_type="obligation.status_changed",
                    idempotency_key=f"redischarge:{obligation.obligation_id}",
                    payload={
                        "obligation_id": obligation.obligation_id,
                        "status": "DISCHARGED",
                        "evidence_refs": [evidence.artifact_id],
                    },
                    timestamp_utc=_TS,
                )
            )
            transport = _FakeTransport(_ok("correct"))
            recovered = run_final_audit(
                project, transport=transport, timestamp_utc=_TS
            )
            assert recovered.replayed is False
            assert recovered.status is FinalAuditStatus.SOLVED
            assert transport.calls == [(_CLAIM, _PROOF)]
        finally:
            project.close()


def test_mid_verify_source_change_is_not_solved():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, fact_id, _e = _seed(Path(raw))
        try:
            class _Mutating:
                def __init__(self):
                    self.calls = []

                def verify(self, statement, proof):
                    self.calls.append((statement, proof))
                    _rewrite_proof(
                        project, fact_id, "changed while the independent audit was running"
                    )
                    return _ok("correct")

            transport = _Mutating()
            run = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is FinalAuditStatus.INFRA
            assert run.reason == "sources_changed"
            assert run.outcome != ProjectOutcome.SOLVED.value
            types = _event_types(project)
            assert EVENT_CHECKPOINT_CREATED not in types
        finally:
            project.close()


def test_old_receipts_kept_after_redischarge_and_identity_change():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation, fact_id, evidence = _seed(Path(raw))
        try:
            failed = run_final_audit(
                project, transport=_FakeTransport(_ok("wrong")), timestamp_utc=_TS
            )
            assert failed.status is FinalAuditStatus.AUDIT_FAIL
            fail_acceptance = failed.acceptance_id
            proof = "Repaired proof after the rejected audit."
            _rewrite_proof(project, fact_id, proof)
            evidence = _register_repaired_evidence(project, _c, proof)
            project.event_store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor="tester",
                    event_type="obligation.status_changed",
                    idempotency_key=f"redischarge:{obligation.obligation_id}:2",
                    payload={
                        "obligation_id": obligation.obligation_id,
                        "status": "DISCHARGED",
                        "evidence_refs": [evidence.artifact_id],
                    },
                    timestamp_utc=_TS,
                )
            )
            recovered = run_final_audit(
                project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
            )
            assert recovered.status is FinalAuditStatus.SOLVED
            assert recovered.identity_hash != failed.identity_hash
            assert recovered.acceptance_id != fail_acceptance
            acceptances = [
                envelope.payload["acceptance"]["record_id"]
                for _seq, envelope in project.event_store.iter_project(project.project_id)
                if envelope.event_type == EVENT_ACCEPTANCE_RECORDED
            ]
            assert fail_acceptance in acceptances
            assert recovered.acceptance_id in acceptances
            replay = run_final_audit(
                project, transport=_FakeTransport(_ok("wrong")), timestamp_utc=_TS
            )
            assert replay.replayed is True
            assert replay.status is FinalAuditStatus.SOLVED
        finally:
            project.close()


def test_infra_retry_stays_version_bound_after_identity_change():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, fact_id, _e = _seed(Path(raw))
        try:
            timed_out = run_final_audit(
                project,
                transport=_FakeTransport(
                    VerifyResult(
                        transport_status="timeout",
                        verdict=None,
                        detail="timed out",
                        body={"transport_status": "timeout"},
                    )
                ),
                timestamp_utc=_TS,
            )
            assert timed_out.status is FinalAuditStatus.INFRA
            _rewrite_proof(project, fact_id, "New proof after an infrastructure timeout.")
            fresh = run_final_audit(
                project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
            )
            assert fresh.replayed is False
            assert fresh.status is FinalAuditStatus.SOLVED
            assert fresh.identity_hash != timed_out.identity_hash
        finally:
            project.close()


def test_retry_infra_requires_reason_and_context_uses_current_proof():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, fact_id, _e = _seed(Path(raw))
        try:
            try:
                run_final_audit(
                    project,
                    transport=_FakeTransport(_ok("correct")),
                    timestamp_utc=_TS,
                    retry_infra=True,
                )
                raise AssertionError("retry without reason must fail")
            except FinalAuditError as exc:
                assert "retry_reason" in str(exc)
            run = run_final_audit(
                project, transport=_FakeTransport(_ok("correct")), timestamp_utc=_TS
            )
            assert run.packet_hash
            packet = project.artifacts.get_bytes(run.packet_hash).decode("utf-8")
            assert _PROOF in packet
            assert _CLAIM in packet
            assert fact_id in packet
        finally:
            project.close()


def test_live_http_finite_cost_requires_configured_allowance(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("HTTP must not run without a billed allowance")

    monkeypatch.setenv("EULER_VERIFY_URL", "http://127.0.0.1:9/verify")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(Path(raw), cost=10_000)
        try:
            try:
                run_final_audit(project, timestamp_utc=_TS)
                raise AssertionError("missing live allowance must fail")
            except FinalAuditError as exc:
                assert "EULER_FINAL_AUDIT_RESOURCES" in str(exc) or "requested" in str(exc)
            assert calls == []
        finally:
            project.close()


def test_configured_proof_reservation_and_environment_replay(monkeypatch):
    from euler.control.policy import ResourceVector

    monkeypatch.setenv(
        "EULER_FINAL_AUDIT_RESOURCES",
        json.dumps({
            "cost_microusd": 4, "input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "wall_ms": 1,
            "tool_cpu_ms": 0, "tool_gpu_ms": 0,
        }),
    )
    monkeypatch.setenv("EULER_VERIFY_URL", "http://verify.example/proof")
    with tempfile.TemporaryDirectory() as raw:
        project, _c, _o, _f, _e = _seed(Path(raw), cost=10_000)
        try:
            transport = _FakeTransport(_ok("correct"))
            run = run_final_audit(
                project, transport=transport, timestamp_utc=_TS,
                requested=ResourceVector(
                    cost_microusd=4, input_tokens=0, output_tokens=0,
                    reasoning_tokens=0, cached_tokens=0, wall_ms=1,
                    tool_cpu_ms=0, tool_gpu_ms=0,
                ),
            )
            assert run.status is FinalAuditStatus.SOLVED
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == run.lease_id)
            assert lease.requested.cost_microusd == 4
            monkeypatch.setenv("EULER_VERIFY_URL", "http://verify.example/proof-v2")
            second = run_final_audit(project, transport=transport, timestamp_utc=_TS)
            assert second.replayed is False
            assert second.identity_hash != run.identity_hash
            assert len(transport.calls) == 2
        finally:
            project.close()


def test_http_verify_timeout_seconds_override(monkeypatch):
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"verdict": "correct", "verification_report": "ok"}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setenv("EULER_VERIFY_URL", "http://127.0.0.1:9/verify")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = HttpVerifyTransport(timeout_seconds=2).verify("stmt", "proof-body")
    assert result.verdict == "correct"
    assert captured["timeout"] == 2
