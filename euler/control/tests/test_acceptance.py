"""Offline tests for Phase 7 acceptance, outcomes, and checkpoints.

Covers normative gates 1–9 plus AST isolation. No gateway/verify writes.

Runs standalone (``python3 -B -m euler.control.tests.test_acceptance``).
"""

from __future__ import annotations

import ast
import json
import shutil
import tempfile
from pathlib import Path

from euler.control.acceptance import (
    AcceptanceError,
    AcceptanceVerdict,
    ExternalInputEvidence,
    FinalAuditKind,
    FinalAuditReceipt,
    InfrastructureBlockEvidence,
    ProjectOutcome,
    StallEvidence,
    assess_final_acceptance,
    build_checkpoint_report,
    collect_fact_admissions,
    derive_project_outcome,
    kernel_acceptance_required,
    promote_compute_counterexample,
)
from euler.control.kernel_evidence import (
    TRUSTED_PROVENANCE,
    KERNEL_CHECKER_VERSION,
    KernelCheckArtifact,
    KernelCheckStatus,
    KernelSourceManifest,
    attest_statement_alignment,
)
from euler.control.adapters.project import (
    FactSnapshot,
    MemoryRecord,
    ProjectSnapshot,
)
from euler.control.artifacts import ArtifactStore
from euler.control.compute import (
    ComponentIdentity,
    ComputeLimits,
    ComputeRequest,
    ComputeRunner,
    ComputeTermination,
)
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
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, ObligationStatus, Route
from euler.control.policy import (
    BudgetClass,
    BudgetPolicy,
    DimensionLimit,
    ResourceSnapshot,
    ResourceVector,
    SlotKind,
)
from euler.control.runtime import (
    Lease,
    LeaseStatus,
    ResourceDisposition,
    RuntimeSnapshot,
)
from euler.control.scheduler import ActionKind
from euler.control.schema import (
    EventEnvelope,
    InfrastructureStatus,
    MathematicalStatus,
)
from euler.control.views import InvalidResearchTransition, ResearchView

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_CLAIM = "every even integer greater than 2 is a sum of two primes"
_AUDIT_BYTES = b"audit-art"
_TEMPDIRS: list[tempfile.TemporaryDirectory] = []


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _fact_id(*parts: str) -> str:
    return sha256_hex("|".join(parts))[:16]


def _store() -> ArtifactStore:
    tmp = tempfile.TemporaryDirectory()
    _TEMPDIRS.append(tmp)
    store = ArtifactStore(Path(tmp.name) / "cas")
    store.put_bytes(_AUDIT_BYTES)
    return store


def _assess(contract, obligation, view, snapshot, audit, store: ArtifactStore | None = None):
    return assess_final_acceptance(
        contract,
        obligation,
        view,
        snapshot,
        audit,
        acceptance_policy_hash=_hex("policy"),
        artifacts=_store() if store is None else store,
    )


def _scope(*, domain: str = "Z") -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain=domain),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
    )


def _contract(
    *,
    statement: str = _CLAIM,
    scope: MathematicalScope | None = None,
    required: EvidenceKind = EvidenceKind.LLM_CHECKED_PROOF,
    target_kind: TargetKind = TargetKind.PROOF,
) -> ProblemContract:
    return ProblemContract.build(
        problem_id="goldbach",
        version=1,
        exact_statement=statement,
        scope=_scope() if scope is None else scope,
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=target_kind,
        required_evidence_level=required,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract, *, statement: str | None = None) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement if statement is None else statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )


def _evidence(
    contract: ProblemContract,
    *,
    claim: str,
    kind: EvidenceKind = EvidenceKind.LLM_CHECKED_PROOF,
    content: str = "blob",
    result: CheckerResult | None = CheckerResult.PASS,
) -> EvidenceArtifact:
    checked = kind is not EvidenceKind.HEURISTIC_OBSERVATION
    return EvidenceArtifact.build(
        content_hash=_hex("blob", content),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=kind,
        exact_claim=claim,
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker" if checked else None,
        checker_version="1" if checked else None,
        checker_result=result if checked else None,
    )


def _event(event_type: str, payload: dict, key: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="tester",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _view(contract: ProblemContract, obligation: Obligation, *evidence: EvidenceArtifact) -> ResearchView:
    view = ResearchView()
    seq = 1
    view.apply(seq, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    seq += 1
    for index, item in enumerate(evidence):
        view.apply(
            seq,
            _event("evidence.registered", {"evidence": item.to_dict()}, f"e-{index}"),
        )
        seq += 1
    view.apply(seq, _event("obligation.created", {"obligation": obligation.to_dict()}, "o"))
    return view


def _discharge(view: ResearchView, obligation: Obligation, evidence: EvidenceArtifact, seq: int) -> int:
    view.apply(
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": obligation.obligation_id,
                "status": "DISCHARGED",
                "evidence_refs": [evidence.artifact_id],
            },
            f"d-{seq}",
        ),
    )
    return seq + 1


def _refute(view: ResearchView, obligation: Obligation, evidence: EvidenceArtifact, seq: int) -> int:
    view.apply(
        seq,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": obligation.obligation_id,
                "status": "REFUTED",
                "evidence_refs": [evidence.artifact_id],
            },
            f"r-{seq}",
        ),
    )
    return seq + 1


def _fact(statement: str, fact_id: str | None = None) -> FactSnapshot:
    fid = _fact_id("fact", statement) if fact_id is None else fact_id
    raw = f"## statement\n{statement}\n"
    return FactSnapshot(
        fact_id=fid,
        relative_path=f"fact_graph/facts/{fid}.md",
        raw_markdown=raw,
        content_sha256=sha256_hex(raw.encode("utf-8")),
        statement=statement,
        predecessors=(),
        external_refs=(),
    )


def _memory(
    record_id: str,
    *,
    claim: str,
    provenance: str = "fact_submit",
    transport: str = "ok",
    verdict: str | None = "correct",
    fact_id: str | None = None,
    write_error: str | None = None,
) -> MemoryRecord:
    payload: dict[str, object] = {
        "id": record_id,
        "kind": "verification",
        "claim": claim,
        "provenance": provenance,
        "transport_status": transport,
        "verdict": verdict,
        "fact_id": fact_id,
    }
    if write_error is not None:
        payload["write_error"] = write_error
    return MemoryRecord(
        kind="verification",
        record_id=record_id,
        relative_path="global_memory/verification.jsonl",
        canonical_json=canonical_json(payload),
    )


def _snapshot(
    facts: tuple[FactSnapshot, ...] = (),
    memory: tuple[MemoryRecord, ...] = (),
    revoked: tuple[str, ...] = (),
) -> ProjectSnapshot:
    return ProjectSnapshot(
        active_facts=facts,
        revoked_fact_ids=revoked,
        memory_records=memory,
        receipts=(),
    )


def _policy(**overrides: object) -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    kwargs: dict[str, object] = {
        "cost_microusd": unbound,
        "input_tokens": unbound,
        "output_tokens": unbound,
        "reasoning_tokens": unbound,
        "cached_tokens": unbound,
        "wall_ms": unbound,
        "tool_cpu_ms": unbound,
        "tool_gpu_ms": unbound,
    }
    kwargs.update(overrides)
    return BudgetPolicy(**kwargs)  # type: ignore[arg-type]


def _runtime(
    resources: ResourceSnapshot | None = None,
    leases: tuple[Lease, ...] = (),
) -> RuntimeSnapshot:
    snap = ResourceSnapshot.empty() if resources is None else resources
    return RuntimeSnapshot(
        project_id="proj",
        leases=leases,
        active_lease_ids=tuple(
            item.lease_id
            for item in leases
            if item.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}
        ),
        resources=snap,
        verifier_slots_in_use=snap.verifier_slots_in_use,
    )


def _zero_vector() -> ResourceVector:
    return ResourceVector(
        cost_microusd=0,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        cached_tokens=0,
        wall_ms=0,
        tool_cpu_ms=0,
        tool_gpu_ms=0,
    )


def _lease(*, status: LeaseStatus, contract_id: str, tag: str) -> Lease:
    failed = status in {LeaseStatus.INFRA_FAILED, LeaseStatus.EXPIRED, LeaseStatus.CANCELLED}
    infra = {
        LeaseStatus.INFRA_FAILED: InfrastructureStatus.FAILED,
        LeaseStatus.EXPIRED: InfrastructureStatus.TIMED_OUT,
        LeaseStatus.CANCELLED: InfrastructureStatus.CANCELLED,
        LeaseStatus.COMPLETED: InfrastructureStatus.SUCCEEDED,
        LeaseStatus.ISSUED: InfrastructureStatus.PENDING,
        LeaseStatus.RUNNING: InfrastructureStatus.RUNNING,
    }[status]
    disposition = (
        ResourceDisposition.SETTLED
        if failed or status is LeaseStatus.COMPLETED
        else ResourceDisposition.RESERVED
    )
    if status is LeaseStatus.CANCELLED:
        disposition = ResourceDisposition.SETTLED
    return Lease(
        lease_id=_hex("lease", tag),
        project_id="proj",
        candidate_id=f"cand-{tag}",
        action_id=_hex("action", tag),
        attempt_id=_hex("attempt", tag),
        reservation_id=_hex("res", tag),
        idempotency_key=f"idem-{tag}",
        slot_kind=SlotKind.REASONING,
        budget_class=BudgetClass.NORMAL,
        action_kind=ActionKind.PROVE,
        status=status,
        infrastructure_status=infra,
        mathematical_status=MathematicalStatus.UNKNOWN,
        resource_disposition=disposition,
        issued_at=_TS,
        ended_at=_TS if status not in {LeaseStatus.ISSUED, LeaseStatus.RUNNING} else None,
        started_at=_TS if status is not LeaseStatus.ISSUED else None,
        running_at=_TS if status is not LeaseStatus.ISSUED else None,
        action_type="prove",
        model_id="grok-4.6",
        effort="high",
        mechanism="direct",
        contract_id=contract_id,
        context_seed_hash=_hex("seed", tag),
        requested=_zero_vector(),
        consumed=_zero_vector(),
    )


def _audit_proof(
    contract: ProblemContract,
    obligation: Obligation,
    *,
    evidence: EvidenceArtifact,
    fact_id: str,
    result: CheckerResult = CheckerResult.PASS,
    created_at: str = _TS,
) -> FinalAuditReceipt:
    return FinalAuditReceipt.build(
        audit_kind=FinalAuditKind.PROOF,
        checker_result=result,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=_hex("action-proof"),
        exact_claim=obligation.exact_statement,
        scope=obligation.scope,
        acceptance_policy_hash=_hex("policy"),
        auditor_id="auditor",
        auditor_version="1",
        environment_hash=_hex("audit-env"),
        audit_artifact_hash=_hex("audit-art"),
        created_at=created_at,
        translation_confirmed=False,
        original_object_realized=False,
        fact_id=None if result is not CheckerResult.PASS else fact_id,
        evidence_ids=() if result is not CheckerResult.PASS else (evidence.artifact_id,),
    )


def _audit_cex(
    contract: ProblemContract,
    obligation: Obligation,
    *,
    evidence_ids: tuple[str, ...] = (),
    candidate: str | None = None,
    report: str | None = None,
    translation: bool = True,
    realized: bool = True,
    result: CheckerResult = CheckerResult.PASS,
    action_id: str | None = None,
    claim: str | None = None,
) -> FinalAuditReceipt:
    return FinalAuditReceipt.build(
        audit_kind=FinalAuditKind.COUNTEREXAMPLE,
        checker_result=result,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=_hex("action-cex") if action_id is None else action_id,
        exact_claim=obligation.exact_statement if claim is None else claim,
        scope=obligation.scope,
        acceptance_policy_hash=_hex("policy"),
        auditor_id="auditor",
        auditor_version="1",
        environment_hash=_hex("audit-env"),
        audit_artifact_hash=_hex("audit-art"),
        created_at=_TS,
        translation_confirmed=translation if result is CheckerResult.PASS else False,
        original_object_realized=realized if result is CheckerResult.PASS else False,
        evidence_ids=evidence_ids,
        candidate_content_hash=candidate,
        report_content_hash=report,
    )


class _Searcher:
    def __init__(self, identity: ComponentIdentity, assignment: dict[str, int]) -> None:
        self._identity = identity
        self._assignment = dict(assignment)

    @property
    def identity(self) -> ComponentIdentity:
        return self._identity

    def total_points(self, request: ComputeRequest) -> int:
        return 1

    def assignment_at(self, request: ComputeRequest, ordinal: int) -> dict[str, int]:
        return dict(self._assignment)

    def predicate_holds(self, request: ComputeRequest, assignment: object) -> bool:
        return False


class _Checker:
    def __init__(self, identity: ComponentIdentity) -> None:
        self._identity = identity

    @property
    def identity(self) -> ComponentIdentity:
        return self._identity

    def check(self, request: ComputeRequest, persisted_candidate_bytes: bytes):
        return CheckerResult.PASS, "ok"


def _compute_checked(contract: ProblemContract, obligation: Obligation, store: ArtifactStore):
    searcher_id = ComponentIdentity(
        component_id="poly-searcher", version="1", code_hash=_hex("search-code")
    )
    checker_id = ComponentIdentity(
        component_id="poly-checker", version="1", code_hash=_hex("check-code")
    )
    request = ComputeRequest.build(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=_hex("action-cex"),
        tested_claim=obligation.exact_statement,
        scope=obligation.scope,
        plugin_id="polynomial_counterexample",
        plugin_version="1",
        plugin_payload={"variables": ["n"], "relation": "EQ"},
        domain_hash=_hex("domain"),
        declared_total_points=1,
        limits=ComputeLimits(
            max_points=DimensionLimit.limited(1),
            max_wall_ms=DimensionLimit.unlimited(),
        ),
        searcher=searcher_id,
        checker=checker_id,
        tool_environment_hash=_hex("tool"),
        created_at=_TS,
    )
    return ComputeRunner().run(
        request,
        _Searcher(searcher_id, {"n": 1}),
        _Checker(checker_id),
        store,
    )


def test_unrelated_counterexample_cannot_refute_via_acceptance_or_view():
    contract = _contract()
    obligation = _obligation(contract)
    unrelated = _evidence(
        contract,
        claim="a different lemma",
        kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        content="unrelated",
    )
    view = _view(contract, obligation, unrelated)
    _raises(
        InvalidResearchTransition,
        view.apply,
        4,
        _event(
            "obligation.status_changed",
            {
                "obligation_id": obligation.obligation_id,
                "status": "REFUTED",
                "evidence_refs": [unrelated.artifact_id],
            },
            "bad",
        ),
    )
    assert view.get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN


def test_revoked_missing_and_contradictory_admissions_cannot_solve():
    contract = _contract()
    obligation = _obligation(contract)
    proof = _evidence(contract, claim=_CLAIM, content="proof")
    view = _view(contract, obligation, proof)
    _discharge(view, obligation, proof, 4)
    live = view.get_obligation(obligation.obligation_id)
    fact = _fact(_CLAIM)
    audit = _audit_proof(contract, live, evidence=proof, fact_id=fact.fact_id)

    missing = _assess(contract, live, view, _snapshot(), audit)
    assert missing.verdict is AcceptanceVerdict.FAIL
    assert "missing_or_invalid_admission" in missing.reasons

    revoked = _assess(
        contract,
        live,
        view,
        _snapshot(
            memory=(
                _memory(
                    "adm-rev",
                    claim=_CLAIM,
                    fact_id=fact.fact_id,
                ),
            ),
            revoked=(fact.fact_id,),
        ),
        audit,
    )
    assert revoked.verdict is AcceptanceVerdict.FAIL

    replay = collect_fact_admissions(
        _snapshot(
            facts=(fact,),
            memory=(
                _memory(
                    "adm-replay",
                    claim=_CLAIM,
                    provenance="worker_mcp",
                    fact_id=fact.fact_id,
                ),
            ),
        )
    )
    assert replay[0].accepted is False
    assert "replay_only_or_foreign_provenance" in replay[0].reasons

    contradictory = collect_fact_admissions(
        _snapshot(
            facts=(fact,),
            memory=(
                _memory("adm-a", claim=_CLAIM, fact_id=fact.fact_id, verdict="correct"),
                _memory("adm-b", claim=_CLAIM, fact_id=fact.fact_id, verdict="wrong"),
            ),
        )
    )
    assert all(item.accepted is False for item in contradictory)
    assert any("admission_contradictory" in item.reasons for item in contradictory)

    write_err = collect_fact_admissions(
        _snapshot(
            facts=(fact,),
            memory=(
                _memory(
                    "adm-w",
                    claim=_CLAIM,
                    fact_id=fact.fact_id,
                    write_error="disk full",
                ),
            ),
        )
    )
    assert write_err[0].accepted is False
    assert "admission_write_error" in write_err[0].reasons


def test_target_scope_or_definition_mismatch_cannot_solve():
    contract = _contract()
    other = _contract(scope=_scope(domain="N"))
    obligation = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=other.scope,
        desired_output="proof",
        created_by="researcher",
        version=2,
    )
    proof = _evidence(contract, claim=_CLAIM, content="proof-scope")
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("evidence.registered", {"evidence": proof.to_dict()}, "e"))
    view.apply(3, _event("obligation.created", {"obligation": obligation.to_dict()}, "o"))
    fact = _fact(_CLAIM)
    audit = FinalAuditReceipt.build(
        audit_kind=FinalAuditKind.PROOF,
        checker_result=CheckerResult.PASS,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=_hex("action-proof"),
        exact_claim=obligation.exact_statement,
        scope=obligation.scope,
        acceptance_policy_hash=_hex("policy"),
        auditor_id="auditor",
        auditor_version="1",
        environment_hash=_hex("audit-env"),
        audit_artifact_hash=_hex("audit-art"),
        created_at=_TS,
        translation_confirmed=False,
        original_object_realized=False,
        fact_id=fact.fact_id,
        evidence_ids=(proof.artifact_id,),
    )
    record = _assess(
        contract,
        obligation,
        view,
        _snapshot(facts=(fact,), memory=(_memory("adm", claim=_CLAIM, fact_id=fact.fact_id),)),
        audit,
    )
    assert record.verdict is AcceptanceVerdict.FAIL
    assert "target_scope_mismatch" in record.reasons


def test_evidence_below_required_level_cannot_solve():
    contract = _contract(required=EvidenceKind.KERNEL_CHECKED_PROOF)
    obligation = _obligation(contract)
    llm = _evidence(contract, claim=_CLAIM, kind=EvidenceKind.LLM_CHECKED_PROOF, content="llm")
    kernel = _evidence(
        contract,
        claim=_CLAIM,
        kind=EvidenceKind.KERNEL_CHECKED_PROOF,
        content="kernel",
    )
    view = _view(contract, obligation, llm, kernel)
    _raises(
        InvalidResearchTransition,
        _discharge,
        view,
        obligation,
        llm,
        5,
    )
    _discharge(view, obligation, kernel, 5)
    live = view.get_obligation(obligation.obligation_id)
    fact = _fact(_CLAIM)
    weak = _audit_proof(contract, live, evidence=llm, fact_id=fact.fact_id)
    record = _assess(
        contract,
        live,
        view,
        _snapshot(facts=(fact,), memory=(_memory("adm", claim=_CLAIM, fact_id=fact.fact_id),)),
        weak,
    )
    assert record.verdict is AcceptanceVerdict.FAIL
    assert record.project_outcome is None
    assert "audit_evidence_mismatch" in record.reasons or "evidence_below_required_level" in record.reasons


def test_final_audit_fail_unknown_or_omitted_cannot_solve_or_refute():
    contract = _contract()
    obligation = _obligation(contract)
    proof = _evidence(contract, claim=_CLAIM, content="p")
    cex = _evidence(
        contract,
        claim=_CLAIM,
        kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        content="cex",
    )
    view = _view(contract, obligation, proof, cex)
    _discharge(view, obligation, proof, 5)
    live = view.get_obligation(obligation.obligation_id)
    fact = _fact(_CLAIM)
    snap = _snapshot(facts=(fact,), memory=(_memory("adm", claim=_CLAIM, fact_id=fact.fact_id),))
    omitted = _assess(contract, live, view, snap, None)
    assert omitted.verdict is AcceptanceVerdict.UNKNOWN
    assert "final_audit_omitted" in omitted.reasons
    failed = _assess(
        contract,
        live,
        view,
        snap,
        _audit_proof(contract, live, evidence=proof, fact_id=fact.fact_id, result=CheckerResult.FAIL),
    )
    assert failed.verdict is AcceptanceVerdict.FAIL
    assert "final_audit_fail" in failed.reasons
    unknown = _assess(
        contract,
        live,
        view,
        snap,
        _audit_proof(
            contract, live, evidence=proof, fact_id=fact.fact_id, result=CheckerResult.UNKNOWN
        ),
    )
    assert unknown.verdict is AcceptanceVerdict.UNKNOWN
    assert "final_audit_unknown" in unknown.reasons


def test_compute_promotion_requires_lifting_audit_and_stays_unregistered():
    contract = _contract()
    obligation = _obligation(contract)
    view = _view(contract, obligation)
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        store.put_bytes(_AUDIT_BYTES)
        result = _compute_checked(contract, obligation, store)
        assert result.termination is ComputeTermination.CHECKED_COUNTEREXAMPLE
        assert result.candidate_ref is not None
        _raises(
            AcceptanceError,
            _audit_cex,
            contract,
            obligation,
            candidate=result.candidate_ref.content_hash,
            report=result.report_ref.content_hash,
            translation=False,
            realized=True,
            action_id=result.evidence.action_id,
        )
        failed_audit = _audit_cex(
            contract,
            obligation,
            candidate=result.candidate_ref.content_hash,
            report=result.report_ref.content_hash,
            result=CheckerResult.FAIL,
            action_id=result.evidence.action_id,
        )
        _raises(
            AcceptanceError,
            promote_compute_counterexample,
            contract,
            obligation,
            result,
            failed_audit,
            store,
            "promoter",
            _TS,
        )
        audit = _audit_cex(
            contract,
            obligation,
            candidate=result.candidate_ref.content_hash,
            report=result.report_ref.content_hash,
            action_id=result.evidence.action_id,
        )
        artifact = promote_compute_counterexample(
            contract, obligation, result, audit, store, "promoter", _TS
        )
        assert artifact.evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE
        assert artifact.exact_claim == obligation.exact_statement
        assert artifact.checker_result is CheckerResult.PASS
        assert view.get_evidence(artifact.artifact_id) is None
        assert view.get_obligation(obligation.obligation_id).status is ObligationStatus.OPEN
        view.apply(
            4,
            _event("evidence.registered", {"evidence": artifact.to_dict()}, "promoted"),
        )
        _refute(view, obligation, artifact, 5)
        live = view.get_obligation(obligation.obligation_id)
        accepted = _assess(
            contract,
            live,
            view,
            _snapshot(),
            _audit_cex(contract, live, evidence_ids=(artifact.artifact_id,)),
            store,
        )
        assert accepted.verdict is AcceptanceVerdict.PASS
        assert accepted.project_outcome is ProjectOutcome.REFUTED


def test_solved_path_and_pause_checkpoint_fields():
    contract = _contract()
    obligation = _obligation(contract)
    proof = _evidence(contract, claim=_CLAIM, content="proof-ok")
    view = _view(contract, obligation, proof)
    _discharge(view, obligation, proof, 4)
    live = view.get_obligation(obligation.obligation_id)
    fact = _fact(_CLAIM)
    snap = _snapshot(facts=(fact,), memory=(_memory("adm-ok", claim=_CLAIM, fact_id=fact.fact_id),))
    later = _audit_proof(
        contract, live, evidence=proof, fact_id=fact.fact_id, created_at="2026-02-01T00:00:00Z"
    )
    earlier = _audit_proof(contract, live, evidence=proof, fact_id=fact.fact_id)
    assert later.receipt_id == earlier.receipt_id
    accepted = _assess(contract, live, view, snap, earlier)
    assert accepted.verdict is AcceptanceVerdict.PASS
    assert accepted.project_outcome is ProjectOutcome.SOLVED
    outcome = derive_project_outcome(
        contract, accepted, _policy(), ResourceSnapshot.empty(), view, _runtime()
    )
    assert outcome.outcome is ProjectOutcome.SOLVED
    assert outcome.mathematical_status is MathematicalStatus.CHECKED_CORRECT
    checkpoint = build_checkpoint_report(
        contract,
        outcome,
        view,
        _runtime(),
        source_event_seq=4,
        accepted_result_ids=(fact.fact_id, proof.artifact_id),
    )
    required = {
        "report_id",
        "contract_id",
        "outcome",
        "source_event_seq",
        "resources",
        "accepted_result_ids",
        "open_obligation_ids",
        "scope_aware_failure_ids",
        "untested_route_ids",
        "active_lease_ids",
        "pause_reason",
        "mathematical_status",
        "restart_conditions",
        "created_at",
    }
    assert set(checkpoint.to_dict()) == required
    assert checkpoint.outcome is ProjectOutcome.SOLVED
    roundtrip = type(checkpoint).from_dict(checkpoint.to_dict())
    assert roundtrip.report_id == checkpoint.report_id

    open_ob = _obligation(_contract(statement=_CLAIM))
    open_view = _view(contract, open_ob)
    stall = StallEvidence(
        frontier_obligation_ids=open_view.frontier(),
        untested_route_ids=(),
        restart_condition="resume after bounded recurrence audit",
        recurrence_audit="BOUNDED_AUDIT",
    )
    failed = _assess(contract, open_ob, open_view, _snapshot(), None)
    stalled = derive_project_outcome(
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        open_view,
        _runtime(),
        pause_evidence=stall,
    )
    assert stalled.outcome is ProjectOutcome.FRONTIER_STALLED
    assert stalled.mathematical_status is MathematicalStatus.UNKNOWN
    report = build_checkpoint_report(
        contract, stalled, open_view, _runtime(), 3, created_at=_TS
    )
    assert report.created_at == _TS
    assert report.pause_reason == "frontier_stalled"
    assert report.restart_conditions == ("resume after bounded recurrence audit",)
    blob = json.dumps(report.to_dict()).lower()
    assert "mathematically impossible" not in blob
    assert "unsolvable" not in blob
    assert "proof of no solution" not in blob
    _raises(
        AcceptanceError,
        StallEvidence,
        frontier_obligation_ids=(),
        untested_route_ids=(),
        restart_condition="this is unsolvable",
        recurrence_audit="BOUNDED_AUDIT",
    )


def test_budget_and_infra_pause_preserve_mathematical_unknown():
    contract = _contract()
    obligation = _obligation(contract)
    view = _view(contract, obligation)
    failed = _assess(contract, obligation, view, _snapshot(), None)
    policy = _policy(cost_microusd=DimensionLimit.limited(5))
    used = ResourceSnapshot.empty().reserve(
        ResourceVector(
            cost_microusd=5,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=0,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=policy,
    )
    budgeted = derive_project_outcome(
        contract, failed, policy, used, view, _runtime(used)
    )
    assert budgeted.outcome is ProjectOutcome.BUDGET_EXHAUSTED
    assert budgeted.mathematical_status is MathematicalStatus.UNKNOWN
    assert "cost_microusd" in budgeted.reached_budget_dimensions
    failed_lease = _lease(
        status=LeaseStatus.INFRA_FAILED,
        contract_id=contract.contract_id,
        tag="infra",
    )
    infra = derive_project_outcome(
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(leases=(failed_lease,)),
        pause_evidence=InfrastructureBlockEvidence(
            lease_ids=(failed_lease.lease_id,),
            infrastructure_failures=("checker_timeout",),
            recovery_condition="retry after checker availability",
        ),
    )
    assert infra.outcome is ProjectOutcome.INFRA_BLOCKED
    assert infra.mathematical_status is MathematicalStatus.UNKNOWN
    external = derive_project_outcome(
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(),
        pause_evidence=ExternalInputEvidence(
            missing_material=("library X",),
            missing_permission=(),
            missing_decision=(),
            restart_condition="provide library X then resume",
        ),
    )
    assert external.outcome is ProjectOutcome.NEEDS_EXTERNAL_INPUT
    assert external.mathematical_status is MathematicalStatus.UNKNOWN
    report = build_checkpoint_report(contract, budgeted, view, _runtime(used), 2)
    assert report.mathematical_status is MathematicalStatus.UNKNOWN
    assert report.outcome is ProjectOutcome.BUDGET_EXHAUSTED


def test_fact_id_is_16_hex_not_64():
    fact = _fact(_CLAIM)
    assert len(fact.fact_id) == 16
    contract = _contract()
    obligation = _obligation(contract)
    proof = _evidence(contract, claim=_CLAIM, content="id-len")
    _raises(
        AcceptanceError,
        _audit_proof,
        contract,
        obligation,
        evidence=proof,
        fact_id=_hex("too-long-fact"),
    )
    ok = _audit_proof(contract, obligation, evidence=proof, fact_id=fact.fact_id)
    assert ok.fact_id == fact.fact_id
    view = _view(contract, obligation, proof)
    _discharge(view, obligation, proof, 4)
    live = view.get_obligation(obligation.obligation_id)
    record = _assess(
        contract,
        live,
        view,
        _snapshot(facts=(fact,), memory=(_memory("adm", claim=_CLAIM, fact_id=fact.fact_id),)),
        ok,
    )
    assert record.fact_id == fact.fact_id
    assert len(record.fact_id) == 16


def test_policy_hash_and_audit_artifact_are_required_for_pass():
    contract = _contract()
    obligation = _obligation(contract)
    proof = _evidence(contract, claim=_CLAIM, content="policy")
    view = _view(contract, obligation, proof)
    _discharge(view, obligation, proof, 4)
    live = view.get_obligation(obligation.obligation_id)
    fact = _fact(_CLAIM)
    snap = _snapshot(facts=(fact,), memory=(_memory("adm", claim=_CLAIM, fact_id=fact.fact_id),))
    audit = _audit_proof(contract, live, evidence=proof, fact_id=fact.fact_id)
    mismatched = assess_final_acceptance(
        contract,
        live,
        view,
        snap,
        audit,
        acceptance_policy_hash=_hex("other-policy"),
        artifacts=_store(),
    )
    assert mismatched.verdict is AcceptanceVerdict.FAIL
    assert "audit_policy_mismatch" in mismatched.reasons
    empty = ArtifactStore(Path(tempfile.mkdtemp()) / "empty-cas")
    missing_art = assess_final_acceptance(
        contract,
        live,
        view,
        snap,
        audit,
        acceptance_policy_hash=_hex("policy"),
        artifacts=empty,
    )
    assert missing_art.verdict is AcceptanceVerdict.FAIL
    assert "audit_artifact_missing" in missing_art.reasons


def test_promote_verifies_audit_artifact_in_cas():
    contract = _contract()
    obligation = _obligation(contract)
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        result = _compute_checked(contract, obligation, store)
        audit = _audit_cex(
            contract,
            obligation,
            candidate=result.candidate_ref.content_hash,
            report=result.report_ref.content_hash,
            action_id=result.evidence.action_id,
        )
        _raises(
            AcceptanceError,
            promote_compute_counterexample,
            contract,
            obligation,
            result,
            audit,
            store,
            "promoter",
            _TS,
        )
        store.put_bytes(_AUDIT_BYTES)
        artifact = promote_compute_counterexample(
            contract, obligation, result, audit, store, "promoter", _TS
        )
        assert artifact.evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE


def test_derive_requires_matching_resources_frontier_and_routes():
    contract = _contract()
    obligation = _obligation(contract)
    view = _view(contract, obligation)
    failed = _assess(contract, obligation, view, _snapshot(), None)
    used = ResourceSnapshot.empty().reserve(
        ResourceVector(
            cost_microusd=1,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=0,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        budget_class=BudgetClass.NORMAL,
        slot_kind=SlotKind.REASONING,
        policy=_policy(cost_microusd=DimensionLimit.limited(9)),
    )
    _raises(
        AcceptanceError,
        derive_project_outcome,
        contract,
        failed,
        _policy(),
        used,
        view,
        _runtime(),
    )
    _raises(
        AcceptanceError,
        derive_project_outcome,
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(),
        StallEvidence(
            frontier_obligation_ids=(),
            untested_route_ids=(),
            restart_condition="resume after audit",
            recurrence_audit="BOUNDED_AUDIT",
        ),
    )
    route = Route.build(
        target_obligation_id=obligation.obligation_id,
        mechanism="direct",
        required_obligation_ids=(),
    )
    view.apply(4, _event("route.created", {"route": route.to_dict()}, "route-1"))
    _raises(
        AcceptanceError,
        derive_project_outcome,
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(),
        StallEvidence(
            frontier_obligation_ids=view.frontier(),
            untested_route_ids=(_hex("missing-route"),),
            restart_condition="resume after audit",
            recurrence_audit="BOUNDED_AUDIT",
        ),
    )
    ok_stall = StallEvidence(
        frontier_obligation_ids=view.frontier(),
        untested_route_ids=(route.route_id,),
        restart_condition="resume after audit",
        recurrence_audit="BOUNDED_AUDIT",
    )
    stalled = derive_project_outcome(
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(),
        pause_evidence=ok_stall,
    )
    assert stalled.outcome is ProjectOutcome.FRONTIER_STALLED


def test_infra_block_rejects_cancelled_or_unknown_leases():
    contract = _contract()
    obligation = _obligation(contract)
    view = _view(contract, obligation)
    failed = _assess(contract, obligation, view, _snapshot(), None)
    cancelled = _lease(
        status=LeaseStatus.CANCELLED,
        contract_id=contract.contract_id,
        tag="cancel",
    )
    _raises(
        AcceptanceError,
        derive_project_outcome,
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(leases=(cancelled,)),
        InfrastructureBlockEvidence(
            lease_ids=(cancelled.lease_id,),
            infrastructure_failures=("cancelled",),
            recovery_condition="do not treat cancel as infra block",
        ),
    )
    missing = _hex("missing-lease")
    _raises(
        AcceptanceError,
        derive_project_outcome,
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(),
        InfrastructureBlockEvidence(
            lease_ids=(missing,),
            infrastructure_failures=("ghost",),
            recovery_condition="lease must exist",
        ),
    )
    expired = _lease(
        status=LeaseStatus.EXPIRED,
        contract_id=contract.contract_id,
        tag="expired",
    )
    blocked = derive_project_outcome(
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(leases=(expired,)),
        pause_evidence=InfrastructureBlockEvidence(
            lease_ids=(expired.lease_id,),
            infrastructure_failures=("timed_out",),
            recovery_condition="retry after expiry",
        ),
    )
    assert blocked.outcome is ProjectOutcome.INFRA_BLOCKED


def test_checkpoint_created_at_is_optional_and_excluded_from_id():
    contract = _contract()
    obligation = _obligation(contract)
    view = _view(contract, obligation)
    failed = _assess(contract, obligation, view, _snapshot(), None)
    outcome = derive_project_outcome(
        contract,
        failed,
        _policy(),
        ResourceSnapshot.empty(),
        view,
        _runtime(),
        pause_evidence=ExternalInputEvidence(
            missing_material=("library",),
            missing_permission=(),
            missing_decision=(),
            restart_condition="provide library",
        ),
    )
    first = build_checkpoint_report(
        contract, outcome, view, _runtime(), 2, created_at=_TS
    )
    second = build_checkpoint_report(
        contract, outcome, view, _runtime(), 2, created_at="2026-02-01T00:00:00Z"
    )
    assert first.report_id == second.report_id
    assert first.created_at == _TS
    assert second.created_at == "2026-02-01T00:00:00Z"
    _raises(
        AcceptanceError,
        build_checkpoint_report,
        contract,
        outcome,
        view,
        _runtime(),
        2,
        created_at="2026-01-01T00:00:00+01:00",
    )


def _kernel_bundle(contract, obligation, store: ArtifactStore, *, aligned: bool = True):
    # Pure acceptance fixture. Actual Lean execution is covered by the separate
    # kernel-service integration tests; this file supplies recorded tool bytes.
    lake_path = store._root.parent / "fixture-tools" / "lake"
    lake_path.parent.mkdir(exist_ok=True)
    lake_path.write_text("test-only recorded checker identity\n")
    digest = sha256_hex(lake_path.read_bytes())
    manifest = KernelSourceManifest.build(
        toolchain="leanprover/lean4:v4.34.1",
        lakefile_hash=_hex("lake"),
        source_hashes=(("Tiny.lean", _hex("src")),),
    )
    kernel = KernelCheckArtifact.build(
        status=KernelCheckStatus.PASS,
        checker_id="euler-lean-kernel",
        checker_version=KERNEL_CHECKER_VERSION,
        toolchain="leanprover/lean4:v4.34.1",
        lean_githash="deadbeef",
        module_name="Tiny",
        theorem_name="two_add_two",
        theorem_type="2 + 2 = 4",
        axioms=(),
        source_manifest=manifest,
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        exact_claim=obligation.exact_statement,
        command=("lake", "env", "lean", "_EulerKernelCheck.lean"),
        stdout='EULER_KERNEL_RESULT {"name":"two_add_two","type":"2 + 2 = 4","axioms":[],"isProp":true}\n',
        stderr="",
        detail="target theorem kernel-checked",
        created_at=_TS,
        provenance_kind=TRUSTED_PROVENANCE,
        lake_executable=str(lake_path if lake_path.is_file() else Path("/usr/bin/lake")),
        lake_executable_digest=digest,
        build_status="PASS",
        check_status="PASS",
        build_returncode=0,
        check_returncode=0,
    )
    kernel_ref = store.put_bytes(kernel.content_bytes())
    alignment = attest_statement_alignment(
        version=1,
        contract=contract,
        obligation=obligation,
        kernel_artifact=kernel,
        auditor_id="main-auditor",
        auditor_version="1",
        aligned=aligned,
        created_at=_TS,
        notes="explicit NL/formal alignment",
        kernel_artifact_hash=kernel_ref.content_hash,
    )
    evidence = None
    if aligned:
        align_ref = store.put_bytes(
            canonical_json(alignment.to_dict()).encode("utf-8")
        )
        evidence = EvidenceArtifact.build(
            content_hash=kernel_ref.content_hash,
            producer="kernel-adapter",
            contract_id=contract.contract_id,
            evidence_kind=EvidenceKind.KERNEL_CHECKED_PROOF,
            exact_claim=obligation.exact_statement,
            scope=obligation.scope,
            input_hashes=(kernel_ref.content_hash, align_ref.content_hash),
            environment_hash=_hex("kernel-env"),
            checker_id="euler-lean-kernel",
            checker_version=KERNEL_CHECKER_VERSION,
            checker_result=CheckerResult.PASS,
            created_at=_TS,
        )
    return kernel, alignment, evidence


def test_kernel_formalization_requires_alignment_and_can_solve():
    contract = _contract(
        required=EvidenceKind.KERNEL_CHECKED_PROOF,
        target_kind=TargetKind.FORMALIZATION,
    )
    obligation = _obligation(contract)
    assert kernel_acceptance_required(contract) is True
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        store.put_bytes(_AUDIT_BYTES)
        kernel, alignment, evidence = _kernel_bundle(contract, obligation, store)
        view = _view(contract, obligation, evidence)
        _discharge(view, obligation, evidence, 4)
        live = view.get_obligation(obligation.obligation_id)
        fact = _fact(_CLAIM)
        snap = _snapshot(
            facts=(fact,),
            memory=(_memory("adm-k", claim=_CLAIM, fact_id=fact.fact_id),),
        )
        audit = FinalAuditReceipt.build(
            audit_kind=FinalAuditKind.PROOF,
            checker_result=CheckerResult.PASS,
            contract_id=contract.contract_id,
            obligation_id=live.obligation_id,
            action_id=_hex("action-kernel"),
            exact_claim=live.exact_statement,
            scope=live.scope,
            acceptance_policy_hash=_hex("policy"),
            auditor_id="auditor",
            auditor_version="1",
            environment_hash=_hex("audit-env"),
            audit_artifact_hash=_hex("audit-art"),
            created_at=_TS,
            translation_confirmed=False,
            original_object_realized=False,
            fact_id=fact.fact_id,
            evidence_ids=(evidence.artifact_id,),
            kernel_artifact_hash=evidence.content_hash,
            alignment_attestation_hash=alignment.attestation_id,
        )
        missing = assess_final_acceptance(
            contract,
            live,
            view,
            snap,
            audit,
            acceptance_policy_hash=_hex("policy"),
            artifacts=store,
        )
        assert missing.verdict is AcceptanceVerdict.FAIL
        assert "missing_statement_alignment" in missing.reasons
        accepted = assess_final_acceptance(
            contract,
            live,
            view,
            snap,
            audit,
            acceptance_policy_hash=_hex("policy"),
            artifacts=store,
            alignment=alignment,
        )
        assert accepted.verdict is AcceptanceVerdict.PASS
        assert accepted.project_outcome is ProjectOutcome.SOLVED
        unaligned = attest_statement_alignment(
            version=1,
            contract=contract,
            obligation=live,
            kernel_artifact=kernel,
            auditor_id="main-auditor",
            auditor_version="1",
            aligned=False,
            created_at=_TS,
            kernel_artifact_hash=evidence.content_hash,
        )
        refused = assess_final_acceptance(
            contract,
            live,
            view,
            snap,
            audit,
            acceptance_policy_hash=_hex("policy"),
            artifacts=store,
            alignment=unaligned,
        )
        assert refused.verdict is AcceptanceVerdict.FAIL
        assert "alignment_not_attested" in refused.reasons


def test_child_kernel_claim_differs_from_root_contract():
    contract = _contract()
    child = Obligation.build(
        contract_id=contract.contract_id,
        exact_statement="two plus two equals four as a local lemma",
        scope=contract.scope,
        desired_output="KERNEL_CHECKED_PROOF",
        created_by="researcher",
        version=2,
    )
    assert child.exact_statement != contract.exact_statement
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        store.put_bytes(_AUDIT_BYTES)
        _kernel, alignment, evidence = _kernel_bundle(contract, child, store)
        assert evidence is not None
        assert evidence.exact_claim == child.exact_statement
        assert alignment.exact_claim == child.exact_statement
        root = _obligation(contract)
        view = _view(contract, root, evidence)
        assert view.get_obligation(root.obligation_id).status is ObligationStatus.OPEN


def test_formalization_llm_evidence_is_not_a_kernel_downgrade():
    contract = _contract(
        required=EvidenceKind.LLM_CHECKED_PROOF,
        target_kind=TargetKind.FORMALIZATION,
    )
    obligation = _obligation(contract)
    llm = _evidence(contract, claim=_CLAIM, kind=EvidenceKind.LLM_CHECKED_PROOF, content="llm-form")
    view = _view(contract, obligation, llm)
    _discharge(view, obligation, llm, 4)
    live = view.get_obligation(obligation.obligation_id)
    fact = _fact(_CLAIM)
    snap = _snapshot(facts=(fact,), memory=(_memory("adm-f", claim=_CLAIM, fact_id=fact.fact_id),))
    audit = _audit_proof(contract, live, evidence=llm, fact_id=fact.fact_id)
    record = _assess(contract, live, view, snap, audit)
    assert record.verdict is AcceptanceVerdict.FAIL
    assert "kernel_evidence_required" in record.reasons
    assert record.project_outcome is None


def test_production_module_does_not_import_truth_or_providers():
    path = _ROOT / "acceptance.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    forbidden_modules = {
        "euler.gateway",
        "euler.verify",
        "euler.execution",
        "euler.core.factgraph",
        "euler.core.global_memory",
        "subprocess",
        "openai",
        "http.client",
        "urllib.request",
    }
    forbidden_names = {
        "FactGraph",
        "fact_submit",
        "GlobalMemory",
        "Popen",
        "urlopen",
        "snapshot_project",
    }
    imported: set[str] = set()
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                named.add(alias.name)
        elif isinstance(node, ast.Attribute):
            named.add(node.attr)
        elif isinstance(node, ast.Name):
            named.add(node.id)
    assert imported.isdisjoint(forbidden_modules), imported
    assert named.isdisjoint(forbidden_names), named & forbidden_names
    assert "view.apply" not in source


def main() -> None:
    tests = [
        test_unrelated_counterexample_cannot_refute_via_acceptance_or_view,
        test_revoked_missing_and_contradictory_admissions_cannot_solve,
        test_target_scope_or_definition_mismatch_cannot_solve,
        test_evidence_below_required_level_cannot_solve,
        test_final_audit_fail_unknown_or_omitted_cannot_solve_or_refute,
        test_compute_promotion_requires_lifting_audit_and_stays_unregistered,
        test_solved_path_and_pause_checkpoint_fields,
        test_budget_and_infra_pause_preserve_mathematical_unknown,
        test_fact_id_is_16_hex_not_64,
        test_policy_hash_and_audit_artifact_are_required_for_pass,
        test_promote_verifies_audit_artifact_in_cas,
        test_derive_requires_matching_resources_frontier_and_routes,
        test_infra_block_rejects_cancelled_or_unknown_leases,
        test_checkpoint_created_at_is_optional_and_excluded_from_id,
        test_kernel_formalization_requires_alignment_and_can_solve,
        test_child_kernel_claim_differs_from_root_contract,
        test_formalization_llm_evidence_is_not_a_kernel_downgrade,
        test_production_module_does_not_import_truth_or_providers,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL ACCEPTANCE TESTS PASSED")


if __name__ == "__main__":
    main()
