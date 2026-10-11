"""Root REFUTED checkpoint path: real polynomial search plus a labeled fixture verifier.

Temp projects only. No live verify HTTP, no provider launch, no Fact Graph writes
from the audit module.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from euler.control.acceptance import ProjectOutcome
from euler.control.compute import ComputeTermination
from euler.control.compute_plugins.polynomial_counterexample import (
    IntegerInterval,
    PolynomialPredicate,
    PolynomialRelation,
    PolynomialTerm,
)
from euler.control.compute_service import review_compute_realization, run_polynomial_compute
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
from euler.control.final_audit import VerifyResult
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import EVENT_CHECKPOINT_CREATED
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetPolicy, DimensionLimit
from euler.control.project import initialize_project
from euler.control.refutation_audit import (
    RefutationAuditError,
    RefutationAuditStatus,
    run_refutation_audit,
)
from euler.control.research_service import ResearchService
from euler.control.runtime import LeaseStatus
from euler.control.schema import EventEnvelope

_TS = "2026-01-01T00:00:00Z"
_ROOT_CLAIM = "For every integer x from 0 through 3, x equals zero."
_CHILD_CLAIM = "For every integer y from 0 through 3, y equals zero."


class FixtureVerifyTransport:
    """Labeled offline verifier. Never opens a network connection."""

    label = "fixture-verifier"

    def __init__(self, *results: VerifyResult):
        self._queue = list(results)
        self.calls: list[tuple[str, str]] = []

    def verify(self, statement: str, proof: str) -> VerifyResult:
        self.calls.append((statement, proof))
        if not self._queue:
            raise AssertionError("fixture verifier has no remaining programmed result")
        return self._queue.pop(0)


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("x is an integer in the recorded finite interval",),
        quantifiers=(
            QuantifierBinding(
                variable="x", kind=QuantifierKind.FORALL, domain="integers from 0 through 3",
            ),
        ),
        objects=(),
        parameter_domain="0 <= x <= 3",
        definitions_hash=_hex("refute-box"),
    )


def _child_scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("y is an integer in the recorded finite interval",),
        quantifiers=(
            QuantifierBinding(
                variable="y", kind=QuantifierKind.FORALL, domain="integers from 0 through 3",
            ),
        ),
        objects=(),
        parameter_domain="0 <= y <= 3",
        definitions_hash=_hex("refute-child-box"),
    )


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="finite-zero",
        version=1,
        exact_statement=_ROOT_CLAIM,
        scope=_scope(),
        permitted_tools=("compute",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("counterexample checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="checked counterexample",
        created_by="tester",
        version=1,
        status=ObligationStatus.OPEN,
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
        final_audit_reserve_bp=1_000,
    )


def _predicate() -> PolynomialPredicate:
    return PolynomialPredicate.build(
        variables=("x",),
        intervals=(IntegerInterval(lower=0, upper=3),),
        terms=(PolynomialTerm(coefficient=1, exponents=(1,)),),
        relation=PolynomialRelation.EQ,
    )


def _ok(verdict: str, *, detail: str = "") -> VerifyResult:
    return VerifyResult(
        transport_status="ok",
        verdict=verdict,
        detail=detail,
        body={"verdict": verdict, "verification_report": "fixture-verifier", "label": "fixture-verifier"},
    )


def _timeout() -> VerifyResult:
    return VerifyResult(
        transport_status="timeout",
        verdict=None,
        detail="fixture verifier timeout",
        body={"transport_status": "timeout", "label": "fixture-verifier"},
    )


def _project(tmp: Path):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": ["w1"]}, indent=2) + "\n", encoding="utf-8",
    )
    (root / "workers" / "w1").mkdir(parents=True)
    contract = _contract()
    obligation = _obligation(contract)
    project = initialize_project(root, contract, obligation, _policy(), "tester", _TS)
    return project, contract, obligation


def _scope_hash(obligation: Obligation) -> str:
    return sha256_hex(canonical_json(obligation.scope.to_dict()).encode("utf-8"))


def _refute_root(project, obligation: Obligation, *, request_id: str = "poly-root"):
    ran = run_polynomial_compute(
        project,
        obligation_id=obligation.obligation_id,
        request_id=request_id,
        predicate=_predicate(),
        max_points=4,
        max_wall_ms=5_000,
        actor="tester",
    )
    assert ran["status"] == "CERTIFIED_CANDIDATE"
    assert ran["termination"] == ComputeTermination.CHECKED_COUNTEREXAMPLE.value
    promoted = review_compute_realization(
        project,
        obligation_id=obligation.obligation_id,
        request_id=request_id,
        confirmed_scope_hash=_scope_hash(obligation),
        notes="The assignment x=1 is in the recorded box and falsifies x=0.",
        actor="tester",
        translation_confirmed=True,
        original_object_realized=True,
    )
    live = project.research_view().get_obligation(obligation.obligation_id)
    assert live is not None and live.status is ObligationStatus.REFUTED
    return ran, promoted


def _event_types(project) -> list[str]:
    return [
        envelope.event_type
        for _seq, envelope in project.event_store.iter_project(project.project_id)
    ]


def _fact_files(project) -> list[Path]:
    facts = project.project_dir / "fact_graph" / "facts"
    if not facts.is_dir():
        return []
    return list(facts.glob("*.md"))


def test_public_checkpoint_uses_refutation_path_and_mcp_replays(monkeypatch):
    from euler.control.cli import do_checkpoint
    from euler.control.mcp import euler_checkpoint
    with tempfile.TemporaryDirectory() as raw:
        project, _contract, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
        finally:
            project.close()
        monkeypatch.setenv("EULER_AGENTS_ROOT", raw)
        transport = FixtureVerifyTransport(_ok("correct"))
        result = do_checkpoint("demo", transport=transport)
        assert result["run"]["status"] == "REFUTED"
        assert result["run"]["fact_id"] is None
        monkeypatch.setenv("EULER_ROLE", "worker")
        with pytest.raises(RuntimeError, match="main"):
            euler_checkpoint("demo")
        monkeypatch.setenv("EULER_ROLE", "main")
        replay = euler_checkpoint("demo")
        assert replay["run"]["replayed"]
        assert replay["run"]["identity_hash"] == result["run"]["identity_hash"]
        assert len(transport.calls) == 1


def test_root_witness_fresh_audit_refutes_and_replays_without_second_verify():
    with tempfile.TemporaryDirectory() as raw:
        project, _contract, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            transport = FixtureVerifyTransport(_ok("correct"))
            run = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is RefutationAuditStatus.REFUTED
            assert run.replayed is False
            assert run.to_dict()["fact_id"] is None
            assert run.outcome == ProjectOutcome.REFUTED.value
            assert run.verdict == "correct"
            statement, proof = transport.calls[0]
            assert "valid counterexample" in statement
            assert "not a proof of the original universal statement" in statement
            assert _ROOT_CLAIM in statement
            assert "realization_review_hash" in proof
            assert EVENT_CHECKPOINT_CREATED in _event_types(project)
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == run.lease_id)
            assert lease.status is LeaseStatus.COMPLETED
            assert lease.budget_class.value == "FINAL_AUDIT"
            assert lease.consumed.cost_microusd == 1
            checkpoint = None
            for _seq, envelope in project.event_store.iter_project(project.project_id):
                if envelope.event_type == EVENT_CHECKPOINT_CREATED:
                    checkpoint = envelope.payload["checkpoint"]
            assert checkpoint is not None
            assert run.lease_id not in checkpoint["active_lease_ids"]
            assert checkpoint["resources"]["consumed_final_audit"]["cost_microusd"] == 1
            assert _fact_files(project) == []
            again = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert again.replayed is True
            assert again.status is RefutationAuditStatus.REFUTED
            assert again.acceptance_id == run.acceptance_id
            assert len(transport.calls) == 1
            completed = [item for item in project.runtime_snapshot().leases if item.status is LeaseStatus.COMPLETED]
            assert any(item.lease_id == run.lease_id for item in completed)
        finally:
            project.close()


def test_timeout_replays_then_explicit_retry_is_fresh():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            transport = FixtureVerifyTransport(_timeout(), _ok("correct"))
            first = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert first.status is RefutationAuditStatus.INFRA
            assert first.replayed is False
            replay = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert replay.replayed is True
            assert replay.status is RefutationAuditStatus.INFRA
            assert len(transport.calls) == 1
            with pytest.raises(RefutationAuditError, match="retry_infra"):
                run_refutation_audit(
                    project, transport=transport, timestamp_utc=_TS, retry_infra=True, retry_reason="",
                )
            retried = run_refutation_audit(
                project, transport=transport, timestamp_utc=_TS,
                retry_infra=True, retry_reason="fixture timeout recovered",
            )
            assert retried.status is RefutationAuditStatus.REFUTED
            assert retried.replayed is False
            assert retried.attempt_index >= 2
            assert len(transport.calls) == 2
        finally:
            project.close()


def test_rejected_counterexample_reopens_root_and_never_proves_original():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            transport = FixtureVerifyTransport(_ok("wrong", detail="witness does not realize the object"))
            run = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is RefutationAuditStatus.AUDIT_FAIL
            assert run.outcome != ProjectOutcome.SOLVED.value
            assert run.outcome != ProjectOutcome.REFUTED.value
            live = project.research_view().get_obligation(obligation.obligation_id)
            assert live is not None
            assert live.status is ObligationStatus.OPEN
            view = project.research_view()
            failures = view.failures()
            assert any(item.kind is FailureKind.INVALID_PROOF for item in failures)
            stale = [
                view.get_evidence(ref) for ref in (live.evidence_refs or ())
            ]
            assert any(item is not None and item.validity.value == "STALE" for item in stale) or any(
                ev.validity.value == "STALE" for ev in view.evidence()
                if ev.evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE
            )
            assert EVENT_CHECKPOINT_CREATED not in _event_types(project)
            with pytest.raises(RefutationAuditError, match="not REFUTED"):
                run_refutation_audit(project, transport=FixtureVerifyTransport(_ok("correct")), timestamp_utc=_TS)
        finally:
            project.close()


def test_child_only_refutation_leaves_root_open():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, root_ob = _project(Path(raw))
        try:
            child = Obligation.build(
                contract_id=contract.contract_id,
                exact_statement=_CHILD_CLAIM,
                scope=_child_scope(),
                desired_output="checked counterexample",
                created_by="tester",
                version=1,
            )
            ResearchService(project, actor="tester").apply(
                [{"event_type": "obligation.created", "payload": {"obligation": child.to_dict()}}],
                timestamp_utc=_TS,
            )
            child_pred = PolynomialPredicate.build(
                variables=("x",),
                intervals=(IntegerInterval(lower=0, upper=3),),
                terms=(PolynomialTerm(coefficient=1, exponents=(1,)),),
                relation=PolynomialRelation.EQ,
            )
            ran = run_polynomial_compute(
                project, obligation_id=child.obligation_id, request_id="poly-child",
                predicate=child_pred, max_points=4, max_wall_ms=5_000, actor="tester",
            )
            assert ran["status"] == "CERTIFIED_CANDIDATE"
            review_compute_realization(
                project, obligation_id=child.obligation_id, request_id="poly-child",
                confirmed_scope_hash=_scope_hash(child),
                notes="x=1 falsifies the child claim in its own box.",
                actor="tester", translation_confirmed=True, original_object_realized=True,
            )
            view = project.research_view()
            assert view.get_obligation(child.obligation_id).status is ObligationStatus.REFUTED
            assert view.get_obligation(root_ob.obligation_id).status is ObligationStatus.OPEN
            with pytest.raises(RefutationAuditError, match="not REFUTED"):
                run_refutation_audit(project, transport=FixtureVerifyTransport(_ok("correct")), timestamp_utc=_TS)
            assert view.get_obligation(root_ob.obligation_id).status is ObligationStatus.OPEN
            assert _fact_files(project) == []
        finally:
            project.close()


def test_stale_missing_and_wrong_scope_artifacts_fail_closed():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _project(Path(raw))
        try:
            with pytest.raises(RefutationAuditError, match="not REFUTED"):
                run_refutation_audit(project, transport=FixtureVerifyTransport(_ok("correct")), timestamp_utc=_TS)

            fake_hash = _hex("absent-witness")
            evidence = EvidenceArtifact.build(
                content_hash=fake_hash,
                producer="tester",
                contract_id=contract.contract_id,
                evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
                exact_claim=_ROOT_CLAIM,
                scope=contract.scope,
                environment_hash=_hex("env"),
                checker_id="tester",
                checker_version="1",
                checker_result=CheckerResult.PASS,
                created_at=_TS,
            )
            project.event_store.append(EventEnvelope.build(
                project_id=project.project_id, actor="tester",
                event_type="evidence.registered",
                idempotency_key="evidence:" + evidence.artifact_id,
                payload={"evidence": evidence.to_dict()}, timestamp_utc=_TS,
            ))
            project.event_store.append(EventEnvelope.build(
                project_id=project.project_id, actor="tester",
                event_type="obligation.status_changed",
                idempotency_key="refute:" + obligation.obligation_id,
                payload={
                    "obligation_id": obligation.obligation_id,
                    "status": "REFUTED",
                    "evidence_refs": [evidence.artifact_id],
                },
                timestamp_utc=_TS,
            ))
            with pytest.raises(RefutationAuditError, match="missing stored"):
                run_refutation_audit(project, transport=FixtureVerifyTransport(_ok("correct")), timestamp_utc=_TS)

            _refute_root(project, obligation, request_id="poly-real")
            live = project.research_view().get_obligation(obligation.obligation_id)
            assert live.status is ObligationStatus.REFUTED
            cex = next(
                project.research_view().get_evidence(ref)
                for ref in live.evidence_refs
                if project.research_view().get_evidence(ref) is not None
                and project.research_view().get_evidence(ref).evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE
            )
            project.event_store.append(EventEnvelope.build(
                project_id=project.project_id, actor="tester",
                event_type="evidence.invalidated",
                idempotency_key="invalidate:" + cex.artifact_id,
                payload={"artifact_id": cex.artifact_id}, timestamp_utc=_TS,
            ))
            with pytest.raises(RefutationAuditError, match="stale|VALID"):
                run_refutation_audit(project, transport=FixtureVerifyTransport(_ok("correct")), timestamp_utc=_TS)
        finally:
            project.close()


def test_changed_witness_identity_forces_new_audit():
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation, request_id="poly-a")
            transport = FixtureVerifyTransport(_ok("correct"), _ok("correct"))
            first = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert first.status is RefutationAuditStatus.REFUTED
            live = project.research_view().get_obligation(obligation.obligation_id)
            cex = next(
                project.research_view().get_evidence(ref)
                for ref in live.evidence_refs
                if project.research_view().get_evidence(ref) is not None
                and project.research_view().get_evidence(ref).evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE
                and project.research_view().get_evidence(ref).validity.value == "VALID"
            )
            project.event_store.append(EventEnvelope.build(
                project_id=project.project_id, actor="tester",
                event_type="evidence.invalidated",
                idempotency_key="invalidate-a:" + cex.artifact_id,
                payload={"artifact_id": cex.artifact_id}, timestamp_utc=_TS,
            ))
            ResearchService(project, actor="tester").apply(
                [{
                    "event_type": "obligation.reopened",
                    "payload": {
                        "obligation_id": obligation.obligation_id,
                        "reason": "witness version retired for a new finite-box search",
                        "new_premises": "re-run compute with a new request_id",
                    },
                }],
                timestamp_utc=_TS,
            )
            _refute_root(project, obligation, request_id="poly-b")
            second = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert second.status is RefutationAuditStatus.REFUTED
            assert second.replayed is False
            assert second.identity_hash != first.identity_hash
            assert len(transport.calls) == 2
        finally:
            project.close()


def test_configured_reservation_and_env_change_and_post_verify_mutation(monkeypatch):
    from euler.control.audit_resources import ENV_RESOURCES, ENV_VERIFY_URL
    from euler.control.policy import ResourceVector

    allowance = ResourceVector(
        cost_microusd=7, input_tokens=0, output_tokens=0, reasoning_tokens=0,
        cached_tokens=0, wall_ms=9, tool_cpu_ms=0, tool_gpu_ms=0,
    )
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            monkeypatch.setenv(ENV_RESOURCES, json.dumps(allowance.to_dict()))
            monkeypatch.setenv(ENV_VERIFY_URL, "http://verify.example/v1")
            transport = FixtureVerifyTransport(_ok("correct"), _ok("correct"))
            run = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert run.status is RefutationAuditStatus.REFUTED
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == run.lease_id)
            assert lease.requested.cost_microusd == 7
            assert lease.requested.wall_ms == 9
            monkeypatch.setenv(ENV_VERIFY_URL, "http://verify.example/v2")
            again = run_refutation_audit(project, transport=transport, timestamp_utc=_TS)
            assert again.replayed is False
            assert again.identity_hash != run.identity_hash
            assert len(transport.calls) == 2
        finally:
            project.close()

    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            evidence_id = next(
                ev.artifact_id for ev in project.research_view().evidence()
                if ev.evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE
            )

            class MutatingFixture(FixtureVerifyTransport):
                def verify(self, statement, proof):
                    project.event_store.append(EventEnvelope.build(
                        project_id=project.project_id, actor="tester",
                        event_type="evidence.invalidated",
                        idempotency_key="mutate:" + evidence_id,
                        payload={"artifact_id": evidence_id}, timestamp_utc=_TS,
                    ))
                    return super().verify(statement, proof)

            mutating = MutatingFixture(_ok("correct"))
            run = run_refutation_audit(project, transport=mutating, timestamp_utc=_TS)
            assert run.status is RefutationAuditStatus.INFRA
            assert run.reason == "sources_changed"
            assert run.verify_artifact_hash
            replay = run_refutation_audit(project, transport=mutating, timestamp_utc=_TS)
            assert replay.status is RefutationAuditStatus.INFRA
            assert replay.replayed is True
            assert len(mutating.calls) == 1
            dest = project.artifacts.path_for(run.verify_artifact_hash)
            dest.write_bytes(b"tampered-not-the-original-bytes")
            with pytest.raises(RefutationAuditError, match="tamper|missing"):
                run_refutation_audit(project, transport=mutating, timestamp_utc=_TS)
            assert len(mutating.calls) == 1
        finally:
            project.close()


def test_live_http_finite_policy_requires_allowance_before_http(monkeypatch):
    from euler.control.final_audit import HttpVerifyTransport

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("HTTP must not be called without allowances")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setenv("EULER_VERIFY_URL", "http://127.0.0.1:9/verify")
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            with pytest.raises(RefutationAuditError, match="positive requested|EULER_FINAL_AUDIT_RESOURCES"):
                run_refutation_audit(project, timestamp_utc=_TS, timeout_ms=1000)
            assert calls == []
        finally:
            project.close()


def test_timeout_ms_enforced_on_default_http_transport(monkeypatch):
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"verdict": "correct", "verification_report": "fixture"}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setenv("EULER_VERIFY_URL", "http://127.0.0.1:9/verify")
    monkeypatch.setenv(
        "EULER_FINAL_AUDIT_RESOURCES",
        json.dumps({
            "cost_microusd": 3, "input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "wall_ms": 1,
            "tool_cpu_ms": 0, "tool_gpu_ms": 0,
        }),
    )
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with tempfile.TemporaryDirectory() as raw:
        project, _c, obligation = _project(Path(raw))
        try:
            _refute_root(project, obligation)
            run = run_refutation_audit(project, timestamp_utc=_TS, timeout_ms=2500)
            assert run.status in {RefutationAuditStatus.REFUTED, RefutationAuditStatus.INFRA, RefutationAuditStatus.AUDIT_FAIL}
            assert captured["timeout"] == 2.5
            assert captured["body"]["force_fresh"] is True
            lease = next(item for item in project.runtime_snapshot().leases if item.lease_id == run.lease_id)
            assert lease.requested.wall_ms == 2500
            assert lease.requested.cost_microusd == 3
        finally:
            project.close()
