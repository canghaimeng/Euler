"""Offline tests for Phase 3 ActionSpec, ProgressReceipt, and attempt dedupe.

Covers exact retry vs explicit new sample, identity hashes, candidate non-merge,
heuristic/FAIL evidence rejection, and representation-gain semantics.
No FactGraph / gateway / verify / Global Memory imports.

Runs standalone (``python -m euler.control.tests.test_progress_dedupe``)
and under pytest.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

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
from euler.control.dedupe import (
    ActionAttemptIndex,
    ActionAttemptRecord,
    CandidateMatch,
    CandidateMatchKind,
    DedupeDecision,
    normalize_candidate_text,
    recall_candidates,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import (
    Obligation,
    ObligationStatus,
    Relation,
    RelationType,
)
from euler.control.progress import (
    ActionSpec,
    ProgressKind,
    ProgressReceipt,
    SamplingMode,
    validate_progress_against_view,
)
from euler.control.schema import EventEnvelope, InfrastructureStatus, SchemaValidationError
from euler.control.views import ResearchView

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope(*, domain: str = "Z", variable: str = "n") -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(
                variable=variable, kind=QuantifierKind.FORALL, domain=domain
            ),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain, variable),
    )


def _params(**fields) -> str:
    return canonical_json(fields or {"k": 1})


def _spec(**overrides) -> ActionSpec:
    kwargs = dict(
        action_type="prove",
        mechanism="direct",
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        context_hash=_hex("ctx"),
        dependency_hash=_hex("dep"),
        definitions_hash=_hex("defs", "Z", "n"),
        tool_environment_hash=_hex("tool-v1"),
        parameters_json=_params(k=1),
    )
    kwargs.update(overrides)
    return ActionSpec.build(**kwargs)


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="goldbach",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _evidence(
    contract: ProblemContract,
    *,
    kind: EvidenceKind = EvidenceKind.LLM_CHECKED_PROOF,
    result: CheckerResult | None = CheckerResult.PASS,
    claim: str | None = None,
    content: str = "blob",
) -> EvidenceArtifact:
    checked = kind is not EvidenceKind.HEURISTIC_OBSERVATION
    return EvidenceArtifact.build(
        content_hash=_hex("blob", content),
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=kind,
        exact_claim=claim or contract.exact_statement,
        scope=contract.scope,
        environment_hash=_hex("env"),
        created_at=_TS,
        checker_id="checker" if checked else None,
        checker_version="1" if checked else None,
        checker_result=result if checked else None,
    )


def _obligation(contract: ProblemContract, **kwargs) -> Obligation:
    fields = dict(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
    )
    fields.update(kwargs)
    return Obligation.build(**fields)


def _event(event_type: str, payload: dict, key: str) -> EventEnvelope:
    return EventEnvelope.build(
        project_id="proj",
        actor="tester",
        event_type=event_type,
        idempotency_key=key,
        payload=payload,
        timestamp_utc=_TS,
    )


def _seed_view(*, discharged: bool = True, evidence: EvidenceArtifact | None = None):
    contract = _contract()
    ev = evidence if evidence is not None else _evidence(contract)
    obligation = _obligation(
        contract,
        status=ObligationStatus.DISCHARGED if discharged else ObligationStatus.OPEN,
        evidence_refs=(ev.artifact_id,) if discharged else (),
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("evidence.registered", {"evidence": ev.to_dict()}, "e"))
    if discharged:
        view.apply(3, _event("obligation.created", {"obligation": obligation.to_dict()}, "o"))
    else:
        open_ob = _obligation(contract, status=ObligationStatus.OPEN)
        view.apply(3, _event("obligation.created", {"obligation": open_ob.to_dict()}, "o"))
        obligation = open_ob
    return contract, ev, obligation, view


def _progress(contract: ProblemContract, **overrides) -> ProgressReceipt:
    spec = overrides.pop("spec", None) or _spec()
    kwargs = dict(
        action_id=spec.action_id,
        attempt_id=_hex("att"),
        contract_id=contract.contract_id,
        kind=ProgressKind.REFERENCE_VALIDATED,
        claim=contract.exact_statement,
        scope=contract.scope,
        old_frontier_hash=_hex("front-old"),
        new_frontier_hash=_hex("front-new"),
        created_at=_TS,
    )
    kwargs.update(overrides)
    return ProgressReceipt.build(**kwargs)


# --------------------------------------------------------------------------- #
# ActionSpec identity                                                         #
# --------------------------------------------------------------------------- #


def test_action_id_changes_with_scope_hash_tool_dependency_and_task_mode():
    base = _spec()
    assert _spec(sampling_mode=SamplingMode.EXPLICIT_NEW, sampling_nonce="n1").action_id == base.action_id
    assert _spec(sampling_mode=SamplingMode.EXPLICIT_NEW, sampling_nonce="n2").action_id == base.action_id
    spaced = _spec(mechanism="direct proof")
    assert " " in spaced.mechanism
    assert spaced.action_id != base.action_id
    changed = {
        "task_mode": "audit",
        "dependency_hash": _hex("dep-2"),
        "context_hash": _hex("ctx-2"),
        "definitions_hash": _hex("defs-2"),
        "tool_environment_hash": _hex("tool-v2"),
        "obligation_id": _hex("ob"),
        "parameters_json": _params(k=2),
    }
    for key, value in changed.items():
        other = _spec(**{key: value})
        assert other.action_id != base.action_id, key
    _raises(SchemaValidationError, _spec, parameters_json='{"b":1,"a":2}')
    _raises(SchemaValidationError, _spec, action_type="not safe")
    _raises(SchemaValidationError, _spec, context_hash="nope")
    _raises(SchemaValidationError, _spec, sampling_nonce="n1")
    _raises(SchemaValidationError, _spec, sampling_mode=SamplingMode.EXPLICIT_NEW)
    restored = ActionSpec.from_dict(base.to_dict())
    assert restored == base


def test_retry_reuse_versus_explicit_new_sample():
    spec = _spec()
    index = ActionAttemptIndex()
    assert index.decide(spec) is DedupeDecision.ISSUE
    first = index.start(spec, _hex("attempt-1"))
    assert index.decide(spec) is DedupeDecision.WAIT_FOR_EXISTING
    index.finish(
        first.attempt_id,
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        artifact_refs=(_hex("art"),),
        result="ok",
    )
    assert index.decide(spec) is DedupeDecision.RECOVER_OR_REUSE
    same_nonce = _spec(sampling_mode=SamplingMode.EXPLICIT_NEW, sampling_nonce="s1")
    index2 = ActionAttemptIndex()
    rec = index2.start(same_nonce, _hex("a-s1"))
    index2.finish(rec.attempt_id, infrastructure_status=InfrastructureStatus.SUCCEEDED, result="done")
    assert index2.decide(same_nonce) is DedupeDecision.RECOVER_OR_REUSE
    new_nonce = _spec(sampling_mode=SamplingMode.EXPLICIT_NEW, sampling_nonce="s2")
    assert new_nonce.action_id == same_nonce.action_id
    assert index2.decide(new_nonce) is DedupeDecision.ALLOW_NEW_SAMPLE
    fail_spec = _spec(task_mode="retry")
    fail_index = ActionAttemptIndex()
    failed = fail_index.start(fail_spec, _hex("fail-1"))
    fail_index.finish(failed.attempt_id, infrastructure_status=InfrastructureStatus.FAILED)
    assert fail_index.decide(fail_spec) is DedupeDecision.RETRY_TRANSPORT
    recovered_fail = ActionAttemptIndex()
    rf = recovered_fail.start(fail_spec, _hex("fail-2"))
    recovered_fail.finish(
        rf.attempt_id,
        infrastructure_status=InfrastructureStatus.TIMED_OUT,
        artifact_refs=(_hex("partial"),),
    )
    assert recovered_fail.decide(fail_spec) is DedupeDecision.RECOVER_OR_REUSE
    _raises(SchemaValidationError, index.start, spec, _hex("attempt-2"))
    retry = fail_index.start(fail_spec, _hex("fail-retry"))
    assert retry.infrastructure_status is InfrastructureStatus.PENDING
    _raises(SchemaValidationError, recovered_fail.start, fail_spec, _hex("fail-3"))
    _raises(SchemaValidationError, index2.start, same_nonce, _hex("a-s1-again"))
    replay = index.finish(
        first.attempt_id,
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        artifact_refs=(_hex("art"),),
        result="ok",
    )
    assert replay.attempt_id == first.attempt_id
    _raises(
        SchemaValidationError,
        index.finish,
        first.attempt_id,
        infrastructure_status=InfrastructureStatus.FAILED,
    )
    _raises(SchemaValidationError, index.start, spec, "A" * 64)
    _raises(SchemaValidationError, index.start, spec, "g" * 64)
    pending = ActionAttemptIndex()
    started = pending.start(spec, _hex("hex-check"))
    _raises(
        SchemaValidationError,
        pending.finish,
        started.attempt_id,
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        artifact_refs=("G" * 64,),
    )


# --------------------------------------------------------------------------- #
# candidates                                                                  #
# --------------------------------------------------------------------------- #


def test_candidate_normalization_does_not_merge_or_rename():
    assert normalize_candidate_text("  Lemma  A\n") == normalize_candidate_text("lemma a")
    left = _scope(domain="Z", variable="x")
    right = _scope(domain="Z", variable="x")
    other_domain = _scope(domain="Q", variable="x")
    renamed = _scope(domain="Z", variable="y")
    matches = recall_candidates(
        "Lemma  A",
        left,
        (
            ("c1", "lemma a", right),
            ("c2", "Lemma  A", other_domain),
            ("c3", "∀y P(y)", renamed),
        ),
    )
    assert matches[0].kind is CandidateMatchKind.ADVISORY
    assert matches[0].merges is False
    assert matches[1].kind is CandidateMatchKind.SCOPE_DIFFERENT
    assert matches[1].merges is False
    assert matches[2].kind is CandidateMatchKind.DISTINCT
    assert matches[2].text_equal is False
    overlap = recall_candidates(
        "every even integer is a sum of two primes",
        left,
        (
            ("t1", "every odd integer is a sum of two primes", left),
            ("t2", "every even integer is a sum of two primes over Q", other_domain),
        ),
        token_overlap_threshold=0.5,
    )
    assert overlap[0].kind is CandidateMatchKind.TOKEN_OVERLAP
    assert overlap[0].merges is False
    assert overlap[1].kind is CandidateMatchKind.SCOPE_DIFFERENT
    assert all(item.merges is False for item in matches)
    assert all(item.merges is False for item in overlap)


# --------------------------------------------------------------------------- #
# progress receipts                                                           #
# --------------------------------------------------------------------------- #


def test_mathematical_progress_requires_pass_registered_evidence():
    contract, evidence, obligation, view = _seed_view(discharged=True)
    spec = _spec()
    receipt = _progress(
        contract,
        spec=spec,
        kind=ProgressKind.OBLIGATION_DISCHARGED,
        new_evidence_refs=(evidence.artifact_id,),
        affected_obligation_ids=(obligation.obligation_id,),
    )
    before = (view.frontier(), view.obligations(), view.evidence())
    validate_progress_against_view(receipt, view)
    assert (view.frontier(), view.obligations(), view.evidence()) == before
    assert receipt.is_mathematical() is True

    heuristic = _evidence(contract, kind=EvidenceKind.HEURISTIC_OBSERVATION, content="h")
    fail_ev = _evidence(contract, result=CheckerResult.FAIL, content="f")
    open_contract, open_ev, open_ob, open_view = _seed_view(discharged=False)
    _raises(
        SchemaValidationError,
        validate_progress_against_view,
        _progress(
            open_contract,
            spec=spec,
            attempt_id=_hex("att-open"),
            kind=ProgressKind.OBLIGATION_DISCHARGED,
            new_evidence_refs=(open_ev.artifact_id,),
            affected_obligation_ids=(open_ob.obligation_id,),
        ),
        open_view,
    )
    view_h = ResearchView()
    view_h.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view_h.apply(2, _event("evidence.registered", {"evidence": heuristic.to_dict()}, "e"))
    _raises(
        SchemaValidationError,
        _progress,
        contract,
        spec=spec,
        attempt_id=_hex("no-ev"),
        kind=ProgressKind.GAP_REPAIRED,
        old_frontier_hash=_hex("a"),
        new_frontier_hash=_hex("a"),
        new_evidence_refs=(heuristic.artifact_id,),
        affected_obligation_ids=(_hex("missing-ob"),),
    )
    _raises(
        SchemaValidationError,
        _progress,
        contract,
        spec=spec,
        attempt_id=_hex("gap-none"),
        kind=ProgressKind.GAP_REPAIRED,
        new_evidence_refs=(heuristic.artifact_id,),
    )
    bad_h = _progress(
        contract,
        spec=spec,
        attempt_id=_hex("heur"),
        kind=ProgressKind.GAP_REPAIRED,
        new_evidence_refs=(heuristic.artifact_id,),
        affected_obligation_ids=(_hex("missing-ob"),),
    )
    _raises(SchemaValidationError, validate_progress_against_view, bad_h, view_h)
    view_f = ResearchView()
    view_f.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view_f.apply(2, _event("evidence.registered", {"evidence": fail_ev.to_dict()}, "e"))
    bad_f = _progress(
        contract,
        spec=spec,
        attempt_id=_hex("fail"),
        kind=ProgressKind.REFERENCE_VALIDATED,
        new_evidence_refs=(fail_ev.artifact_id,),
    )
    _raises(SchemaValidationError, validate_progress_against_view, bad_f, view_f)
    _raises(
        SchemaValidationError,
        _progress,
        contract,
        spec=spec,
        attempt_id=_hex("nvp"),
        kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        claim="no progress",
        new_evidence_refs=(evidence.artifact_id,),
    )


def test_reduction_verified_requires_matching_relation_and_scope():
    contract = _contract()
    claim = "left reduces to right"
    evidence = _evidence(contract, claim=claim, content="red")
    source = _obligation(contract, exact_statement="lemma source")
    target = _obligation(contract, exact_statement="lemma target")
    other_scope = _obligation(
        contract,
        exact_statement="other domain",
        scope=_scope(domain="Q"),
        version=2,
    )
    relation = Relation.build(
        relation_type=RelationType.REDUCES_TO,
        source=source.obligation_id,
        target=target.obligation_id,
        exact_claim=claim,
        scope=contract.scope,
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        evidence_refs=(evidence.artifact_id,),
        reviewer="reviewer",
        contract_id=contract.contract_id,
        definitions_hash=contract.scope.definitions_hash,
    )
    view = ResearchView()
    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("evidence.registered", {"evidence": evidence.to_dict()}, "e"))
    view.apply(3, _event("obligation.created", {"obligation": source.to_dict()}, "os"))
    view.apply(4, _event("obligation.created", {"obligation": target.to_dict()}, "ot"))
    view.apply(5, _event("relation.created", {"relation": relation.to_dict()}, "rel"))
    spec = _spec()
    ok = _progress(
        contract,
        spec=spec,
        attempt_id=_hex("red-ok"),
        kind=ProgressKind.REDUCTION_VERIFIED,
        claim=claim,
        new_evidence_refs=(evidence.artifact_id,),
        affected_obligation_ids=(source.obligation_id, target.obligation_id),
    )
    validate_progress_against_view(ok, view)
    wrong_claim = _progress(
        contract,
        spec=spec,
        attempt_id=_hex("red-claim"),
        kind=ProgressKind.REDUCTION_VERIFIED,
        claim="a different reduction",
        new_evidence_refs=(evidence.artifact_id,),
        affected_obligation_ids=(source.obligation_id, target.obligation_id),
    )
    _raises(SchemaValidationError, validate_progress_against_view, wrong_claim, view)
    view.apply(6, _event("obligation.created", {"obligation": other_scope.to_dict()}, "oq"))
    mismatched = _progress(
        contract,
        spec=spec,
        attempt_id=_hex("red-scope"),
        kind=ProgressKind.REDUCTION_VERIFIED,
        claim=claim,
        new_evidence_refs=(evidence.artifact_id,),
        affected_obligation_ids=(other_scope.obligation_id,),
    )
    _raises(SchemaValidationError, validate_progress_against_view, mismatched, view)


def test_representation_gain_is_not_verified_mathematical_progress():
    spec = _spec()
    contract = _contract()
    receipt = _progress(
        contract,
        spec=spec,
        attempt_id=_hex("rep"),
        kind=ProgressKind.REPRESENTATION_GAIN,
        claim="new encoding of the same lemma",
        old_frontier_hash=_hex("front-old"),
        new_frontier_hash=_hex("front-old"),
        next_action="run-finite-search",
        old_tool_environment_hash=_hex("tool-v1"),
        new_tool_environment_hash=_hex("tool-v2"),
    )
    assert receipt.is_mathematical() is False
    view = ResearchView()
    validate_progress_against_view(receipt, view)
    _raises(
        SchemaValidationError,
        _progress,
        contract,
        spec=spec,
        attempt_id=_hex("rep-bad"),
        kind=ProgressKind.REPRESENTATION_GAIN,
        claim="rename only",
        next_action="run-finite-search",
        old_tool_environment_hash=_hex("tool-v1"),
        new_tool_environment_hash=_hex("tool-v1"),
    )
    _raises(
        SchemaValidationError,
        _progress,
        contract,
        spec=spec,
        attempt_id=_hex("rep-no-next"),
        kind=ProgressKind.REPRESENTATION_GAIN,
        claim="no next",
        old_tool_environment_hash=_hex("tool-v1"),
        new_tool_environment_hash=_hex("tool-v2"),
    )


def test_cross_contract_and_unrelated_evidence_refs_fail():
    contract, evidence, obligation, view = _seed_view(discharged=True)
    other = ProblemContract.build(
        problem_id="other_problem",
        version=1,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )
    foreign = _evidence(other, content="foreign")
    view.apply(4, _event("problem.contract.created", {"contract": other.to_dict()}, "c2"))
    view.apply(5, _event("evidence.registered", {"evidence": foreign.to_dict()}, "e2"))
    _raises(
        SchemaValidationError,
        validate_progress_against_view,
        _progress(
            other,
            attempt_id=_hex("cross"),
            kind=ProgressKind.OBLIGATION_DISCHARGED,
            new_evidence_refs=(foreign.artifact_id,),
            affected_obligation_ids=(obligation.obligation_id,),
        ),
        view,
    )
    extra = _evidence(contract, content="extra")
    view.apply(6, _event("evidence.registered", {"evidence": extra.to_dict()}, "e3"))
    _raises(
        SchemaValidationError,
        validate_progress_against_view,
        _progress(
            contract,
            attempt_id=_hex("unrelated"),
            kind=ProgressKind.OBLIGATION_DISCHARGED,
            new_evidence_refs=(extra.artifact_id,),
            affected_obligation_ids=(obligation.obligation_id,),
        ),
        view,
    )


def test_invalid_threshold_match_record_and_illegal_transitions():
    spec = _spec()
    index = ActionAttemptIndex()
    started = index.start(spec, _hex("t-1"))
    running = index.finish(
        started.attempt_id, infrastructure_status=InfrastructureStatus.RUNNING
    )
    assert running.infrastructure_status is InfrastructureStatus.RUNNING
    again = index.finish(
        started.attempt_id, infrastructure_status=InfrastructureStatus.RUNNING
    )
    assert again.infrastructure_status is InfrastructureStatus.RUNNING
    _raises(
        SchemaValidationError,
        index.finish,
        started.attempt_id,
        infrastructure_status=InfrastructureStatus.PENDING,
    )
    index.finish(
        started.attempt_id,
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        result="done",
    )
    _raises(
        SchemaValidationError,
        index.finish,
        started.attempt_id,
        infrastructure_status=InfrastructureStatus.RUNNING,
    )
    scope = _scope()
    _raises(
        SchemaValidationError,
        recall_candidates,
        "lemma",
        scope,
        (),
        token_overlap_threshold=1.5,
    )
    _raises(
        SchemaValidationError,
        recall_candidates,
        "lemma",
        scope,
        (),
        token_overlap_threshold=math.nan,
    )
    _raises(
        SchemaValidationError,
        recall_candidates,
        "lemma",
        scope,
        (),
        token_overlap_threshold=-0.1,
    )
    _raises(
        SchemaValidationError,
        CandidateMatch,
        candidate_id="c1",
        query_text="a",
        candidate_text="b",
        kind="NOPE",
        scope_equal=True,
        text_equal=False,
    )
    _raises(
        SchemaValidationError,
        CandidateMatch,
        candidate_id="c1",
        query_text="a",
        candidate_text="b",
        kind=CandidateMatchKind.ADVISORY,
        scope_equal=True,
        text_equal=True,
        token_similarity=1.5,
    )
    _raises(
        SchemaValidationError,
        ActionAttemptRecord,
        attempt_id="not-hex",
        action_id=spec.action_id,
        sampling_nonce=None,
        sampling_mode=SamplingMode.DEFAULT,
        infrastructure_status=InfrastructureStatus.PENDING,
    )
    _raises(
        SchemaValidationError,
        ActionAttemptRecord,
        attempt_id=_hex("att"),
        action_id=spec.action_id,
        sampling_nonce=None,
        sampling_mode=SamplingMode.DEFAULT,
        infrastructure_status=InfrastructureStatus.UNKNOWN,
    )


def test_production_modules_do_not_import_truth_stores():
    forbidden_modules = {
        "euler.core",
        "euler.core.factgraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.verify",
        "euler.execution",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("progress.py", "dedupe.py"):
        path = _ROOT / filename
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported: set[str] = set()
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
                    named.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                for alias in node.names:
                    named.add(alias.name)
        assert imported.isdisjoint(forbidden_modules), (filename, imported)
        assert named.isdisjoint(forbidden_names), (filename, named)


def main() -> None:
    tests = [
        test_action_id_changes_with_scope_hash_tool_dependency_and_task_mode,
        test_retry_reuse_versus_explicit_new_sample,
        test_candidate_normalization_does_not_merge_or_rename,
        test_mathematical_progress_requires_pass_registered_evidence,
        test_reduction_verified_requires_matching_relation_and_scope,
        test_representation_gain_is_not_verified_mathematical_progress,
        test_cross_contract_and_unrelated_evidence_refs_fail,
        test_invalid_threshold_match_record_and_illegal_transitions,
        test_production_modules_do_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL PROGRESS/DEDUPE TESTS PASSED")


if __name__ == "__main__":
    main()
