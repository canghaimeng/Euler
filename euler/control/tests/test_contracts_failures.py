"""Offline tests for Phase 2 contracts, scope identity, and FailureRecord.

No network, no runtime projects, no provider CLIs. Covers quantifier order,
Z/Q domains, finite/uniform parameters, object identity, definitions hash,
contract version/ID, evidence metadata, counterexample scope mismatch, and
infra failures carrying no mathematical exclusion.

Runs standalone (``python -m euler.control.tests.test_contracts_failures``)
and under pytest.
"""

from __future__ import annotations

import ast
from pathlib import Path

from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ObjectBinding,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
    ValidityStatus,
    evidence_satisfies,
)
from euler.control.failures import (
    ConfidenceClass,
    FailureKind,
    FailureRecord,
)
from euler.control.ids import sha256_hex
from euler.control.schema import SchemaValidationError

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_DEFS = sha256_hex("defs-v1")
_DEFS2 = sha256_hex("defs-v2")
_BLOB = sha256_hex("blob")
_ENV = sha256_hex("env")
_WITNESS = sha256_hex("witness")


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {fn.__name__}")


def _roundtrip(obj):
    restored = type(obj).from_dict(obj.to_dict())
    assert restored.to_dict() == obj.to_dict()
    assert restored == obj
    return restored


def _q(variable: str, kind: QuantifierKind, domain: str, depends_on=()) -> QuantifierBinding:
    return QuantifierBinding(
        variable=variable, kind=kind, domain=domain, depends_on=tuple(depends_on)
    )


def _scope(
    *,
    quantifiers=None,
    parameter_domain: str = "n ∈ ℕ",
    definitions_hash: str = _DEFS,
    assumptions=("n ≥ 1",),
    objects=(),
) -> MathematicalScope:
    if quantifiers is None:
        quantifiers = (_q("n", QuantifierKind.FORALL, "ℕ"),)
    return MathematicalScope.build(
        assumptions=assumptions,
        quantifiers=quantifiers,
        objects=objects,
        parameter_domain=parameter_domain,
        definitions_hash=definitions_hash,
    )


def _contract(*, scope=None, version: int = 1, **overrides) -> ProblemContract:
    kwargs = dict(
        problem_id="odd_sum",
        version=version,
        exact_statement="∀n∈ℕ,  n is a sum of odd numbers.",
        scope=scope if scope is not None else _scope(),
        permitted_tools=("lean", "python"),
        external_help_policy="no-web",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.KERNEL_CHECKED_PROOF,
        completion_checks=("aligned with statement",),
        budget_policy_ref="budget-v1",
    )
    kwargs.update(overrides)
    return ProblemContract.build(**kwargs)


def _evidence(*, scope=None, contract=None, **overrides) -> EvidenceArtifact:
    if contract is None:
        contract = _contract(scope=scope)
    kwargs = dict(
        content_hash=_BLOB,
        producer="worker-1",
        contract_id=contract.contract_id,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        exact_claim=contract.exact_statement,
        scope=contract.scope,
        input_hashes=(),
        environment_hash=_ENV,
        checker_id="finite-search",
        checker_version="1",
        checker_result=CheckerResult.PASS,
        created_at=_TS,
        validity=ValidityStatus.VALID,
    )
    kwargs.update(overrides)
    return EvidenceArtifact.build(**kwargs)


def _failure(*, scope=None, **overrides) -> FailureRecord:
    used = scope if scope is not None else _scope()
    kwargs = dict(
        kind=FailureKind.INFRASTRUCTURE_FAILURE,
        exact_failed_claim="∀n∈ℕ,  n is a sum of odd numbers.",
        scope=used,
        confidence=ConfidenceClass.HIGH,
        context_version=1,
        provenance="attempt timeout",
        created_at=_TS,
        reopen_conditions=("retry with same claim",),
    )
    kwargs.update(overrides)
    return FailureRecord.build(**kwargs)


# --------------------------------------------------------------------------- #
# scope identity                                                              #
# --------------------------------------------------------------------------- #


def test_quantifier_order_changes_scope_and_contract_id():
    forall_exists = _scope(
        quantifiers=(
            _q("x", QuantifierKind.FORALL, "ℤ"),
            _q("y", QuantifierKind.EXISTS, "ℤ", depends_on=("x",)),
        )
    )
    exists_forall = _scope(
        quantifiers=(
            _q("y", QuantifierKind.EXISTS, "ℤ"),
            _q("x", QuantifierKind.FORALL, "ℤ", depends_on=("y",)),
        )
    )
    assert forall_exists != exists_forall
    assert forall_exists.content_id() != exists_forall.content_id()
    left = _contract(scope=forall_exists)
    right = _contract(scope=exists_forall)
    assert left.contract_id != right.contract_id
    assert left.exact_statement == right.exact_statement
    _roundtrip(forall_exists)
    _roundtrip(left)


def test_z_versus_q_domain_changes_scope():
    over_z = _scope(quantifiers=(_q("x", QuantifierKind.FORALL, "ℤ"),))
    over_q = _scope(quantifiers=(_q("x", QuantifierKind.FORALL, "ℚ"),))
    assert over_z != over_q
    assert _contract(scope=over_z).contract_id != _contract(scope=over_q).contract_id


def test_finite_versus_uniform_parameter_domain_changes_scope():
    finite = _scope(parameter_domain="n ≤ N")
    uniform = _scope(parameter_domain="all n ∈ ℕ")
    assert finite != uniform
    assert finite.content_id() != uniform.content_id()
    assert _contract(scope=finite).contract_id != _contract(scope=uniform).contract_id


def test_object_identity_is_semantic():
    separate = _scope(
        objects=(
            ObjectBinding(name="G", object_type="group", identity_key="left-copy"),
            ObjectBinding(name="G", object_type="group", identity_key="right-copy"),
        )
    )
    same = _scope(
        objects=(
            ObjectBinding(name="G", object_type="group", identity_key="shared"),
            ObjectBinding(name="H", object_type="group", identity_key="shared"),
        )
    )
    assert separate != same
    assert separate.content_id() != same.content_id()


def test_definitions_hash_change_produces_new_scope():
    v1 = _scope(definitions_hash=_DEFS)
    v2 = _scope(definitions_hash=_DEFS2)
    assert v1 != v2
    assert _contract(scope=v1).contract_id != _contract(scope=v2).contract_id


def test_original_mathematical_text_is_preserved():
    text = "  ∀ n ∈ ℕ,  存在 y  such that  n = 2y+1.\n"
    scope = _scope(assumptions=(text,), parameter_domain="n ∈ ℕ  (uniform)")
    contract = _contract(exact_statement=text, scope=scope)
    restored = _roundtrip(contract)
    assert restored.exact_statement == text
    assert restored.scope.assumptions == (text,)
    assert restored.scope.parameter_domain == "n ∈ ℕ  (uniform)"


# --------------------------------------------------------------------------- #
# ProblemContract IDs                                                         #
# --------------------------------------------------------------------------- #


def test_contract_semantic_changes_require_new_id_and_positive_version():
    base = _contract(version=1)
    v2 = _contract(version=2)
    assert base.contract_id != v2.contract_id
    assert base.problem_id == v2.problem_id
    _raises(SchemaValidationError, _contract, version=0)
    _raises(SchemaValidationError, _contract, version=-1)
    _raises(SchemaValidationError, _contract, version=True)

    changed = {
        "exact_statement": "∀n∈ℕ, n is even.",
        "permitted_tools": ("sage",),
        "external_help_policy": "arxiv-ok",
        "target_kind": TargetKind.COUNTEREXAMPLE,
        "required_evidence_level": EvidenceKind.CHECKED_COUNTEREXAMPLE,
        "completion_checks": ("has counterexample",),
        "budget_policy_ref": "budget-v2",
        "scope": _scope(parameter_domain="n ≤ 10"),
    }
    for key, value in changed.items():
        other = _contract(**{key: value})
        assert other.contract_id != base.contract_id, key

    tampered = base.to_dict()
    tampered["contract_id"] = sha256_hex("nope")
    _raises(SchemaValidationError, ProblemContract.from_dict, tampered)
    extra = base.to_dict()
    extra["unexpected"] = 1
    _raises(SchemaValidationError, ProblemContract.from_dict, extra)
    _roundtrip(base)


# --------------------------------------------------------------------------- #
# EvidenceArtifact                                                            #
# --------------------------------------------------------------------------- #


def test_evidence_artifact_is_metadata_not_a_fact():
    contract = _contract()
    artifact = _evidence(contract=contract)
    assert artifact.content_hash == _BLOB
    assert not Path(_BLOB).exists()
    assert artifact.artifact_id != artifact.content_hash
    later = _evidence(contract=contract, created_at="2026-01-02T00:00:00Z")
    assert later.artifact_id == artifact.artifact_id
    stale = _evidence(contract=contract, validity=ValidityStatus.STALE)
    assert stale.artifact_id == artifact.artifact_id
    assert stale.validity is ValidityStatus.STALE
    _roundtrip(artifact)
    heuristic = _evidence(
        contract=contract,
        evidence_kind=EvidenceKind.HEURISTIC_OBSERVATION,
        checker_id=None,
        checker_version=None,
        checker_result=None,
    )
    assert heuristic.artifact_id != artifact.artifact_id
    _raises(
        SchemaValidationError,
        _evidence,
        contract=contract,
        evidence_kind=EvidenceKind.KERNEL_CHECKED_PROOF,
        checker_id=None,
        checker_version=None,
        checker_result=None,
    )
    _raises(
        SchemaValidationError,
        _evidence,
        contract=contract,
        evidence_kind=EvidenceKind.HEURISTIC_OBSERVATION,
        checker_id="only-one",
        checker_version=None,
        checker_result=None,
    )
    extra = artifact.to_dict()
    extra["fact_id"] = "not-a-fact"
    _raises(SchemaValidationError, EvidenceArtifact.from_dict, extra)
    _raises(
        SchemaValidationError,
        _evidence,
        contract=contract,
        checker_result="pass",
    )
    failed = _evidence(contract=contract, checker_result=CheckerResult.FAIL)
    unknown = _evidence(contract=contract, checker_result=CheckerResult.UNKNOWN)
    assert failed.checker_result is CheckerResult.FAIL
    assert unknown.checker_result is CheckerResult.UNKNOWN
    _roundtrip(failed)
    _roundtrip(unknown)
    assert failed.artifact_id != artifact.artifact_id


# --------------------------------------------------------------------------- #
# FailureRecord rules                                                         #
# --------------------------------------------------------------------------- #


def test_infra_extraction_budget_failures_have_no_mathematical_exclusion():
    for kind in (
        FailureKind.INFRASTRUCTURE_FAILURE,
        FailureKind.EXTRACTION_FAILURE,
        FailureKind.BUDGET_EXHAUSTION,
    ):
        ok = _failure(kind=kind, exclusion_scope=None)
        assert ok.exclusion_scope is None
        _roundtrip(ok)
        _raises(
            SchemaValidationError,
            _failure,
            kind=kind,
            exclusion_scope=_scope(),
        )


def test_invalid_proof_does_not_refute_a_proposition():
    record = _failure(
        kind=FailureKind.INVALID_PROOF,
        rejected_step="gap in lemma 2",
        witness_refs=(_WITNESS,),
        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
        exclusion_scope=None,
    )
    assert record.exclusion_scope is None
    _roundtrip(record)
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.INVALID_PROOF,
        exclusion_scope=_scope(),
    )


def test_certified_counterexample_requires_checked_evidence_and_exact_scope():
    claim_scope = _scope(quantifiers=(_q("n", QuantifierKind.FORALL, "ℤ"),))
    other_scope = _scope(quantifiers=(_q("n", QuantifierKind.FORALL, "ℚ"),))
    ok = _failure(
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        scope=claim_scope,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        witness_refs=(_WITNESS,),
        exclusion_scope=claim_scope,
        confidence=ConfidenceClass.CERTAIN,
    )
    assert ok.exclusion_scope == claim_scope
    _roundtrip(ok)
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        scope=claim_scope,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        witness_refs=(_WITNESS,),
        exclusion_scope=other_scope,
    )
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        scope=claim_scope,
        evidence_kind=EvidenceKind.HEURISTIC_OBSERVATION,
        witness_refs=(_WITNESS,),
        exclusion_scope=claim_scope,
    )
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        scope=claim_scope,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        witness_refs=(),
        exclusion_scope=claim_scope,
    )
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        scope=claim_scope,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        witness_refs=(_WITNESS,),
        exclusion_scope=None,
    )


def test_semantic_recurrence_is_a_warning_and_reopen_conditions_persist():
    conditions = ("new premise", "new definition hash", "new tool representation")
    record = _failure(
        kind=FailureKind.SEMANTIC_RECURRENCE,
        confidence=ConfidenceClass.MEDIUM,
        reopen_conditions=conditions,
        repair_conditions=("re-audit scope",),
        exclusion_scope=None,
    )
    restored = _roundtrip(record)
    assert restored.reopen_conditions == conditions
    assert restored.exclusion_scope is None
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.SEMANTIC_RECURRENCE,
        confidence=ConfidenceClass.CERTAIN,
    )
    _raises(
        SchemaValidationError,
        _failure,
        kind=FailureKind.SEMANTIC_RECURRENCE,
        confidence=ConfidenceClass.MEDIUM,
        exclusion_scope=_scope(),
    )
    later = _failure(
        kind=FailureKind.SEMANTIC_RECURRENCE,
        confidence=ConfidenceClass.MEDIUM,
        reopen_conditions=conditions,
        repair_conditions=("re-audit scope",),
        created_at="2026-02-01T00:00:00Z",
    )
    assert later.failure_id == record.failure_id


def test_evidence_satisfaction_matrix_is_not_a_total_order():
    contract = _contract()
    kinds = (
        EvidenceKind.HEURISTIC_OBSERVATION,
        EvidenceKind.EXACT_FINITE_RESULT,
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        EvidenceKind.LLM_CHECKED_PROOF,
        EvidenceKind.CERTIFICATE_CHECKED_RESULT,
        EvidenceKind.KERNEL_CHECKED_PROOF,
    )
    allowed = {
        EvidenceKind.HEURISTIC_OBSERVATION: set(kinds),
        EvidenceKind.EXACT_FINITE_RESULT: {
            EvidenceKind.EXACT_FINITE_RESULT,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        },
        EvidenceKind.CHECKED_COUNTEREXAMPLE: {
            EvidenceKind.CHECKED_COUNTEREXAMPLE,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
        },
        EvidenceKind.LLM_CHECKED_PROOF: {
            EvidenceKind.LLM_CHECKED_PROOF,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        },
        EvidenceKind.CERTIFICATE_CHECKED_RESULT: {
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        },
        EvidenceKind.KERNEL_CHECKED_PROOF: {EvidenceKind.KERNEL_CHECKED_PROOF},
    }
    artifacts = {}
    for kind in kinds:
        kwargs = {"contract": contract, "evidence_kind": kind}
        if kind is EvidenceKind.HEURISTIC_OBSERVATION:
            kwargs.update(checker_id=None, checker_version=None, checker_result=None)
        artifacts[kind] = _evidence(**kwargs)
    for required, actual in ((req, act) for req in kinds for act in kinds):
        got = evidence_satisfies(
            artifacts[actual],
            required,
            contract.scope,
            contract.contract_id,
            contract.exact_statement,
        )
        assert got is (actual in allowed[required]), (required, actual, got)
    heuristic = artifacts[EvidenceKind.HEURISTIC_OBSERVATION]
    for required in kinds:
        if required is EvidenceKind.HEURISTIC_OBSERVATION:
            continue
        assert evidence_satisfies(
            heuristic, required, contract.scope, contract.contract_id
        ) is False


def test_checker_fail_unknown_and_mismatches_do_not_satisfy():
    contract = _contract()
    passing = _evidence(contract=contract, evidence_kind=EvidenceKind.KERNEL_CHECKED_PROOF)
    assert evidence_satisfies(
        passing,
        EvidenceKind.KERNEL_CHECKED_PROOF,
        contract.scope,
        contract.contract_id,
        contract.exact_statement,
    )
    for result in (CheckerResult.FAIL, CheckerResult.UNKNOWN):
        historic = _evidence(
            contract=contract,
            evidence_kind=EvidenceKind.KERNEL_CHECKED_PROOF,
            checker_result=result,
        )
        assert historic.checker_result is result
        assert evidence_satisfies(
            historic,
            EvidenceKind.KERNEL_CHECKED_PROOF,
            contract.scope,
            contract.contract_id,
        ) is False
        assert evidence_satisfies(
            historic,
            EvidenceKind.HEURISTIC_OBSERVATION,
            contract.scope,
            contract.contract_id,
        ) is False
    stale = _evidence(
        contract=contract,
        evidence_kind=EvidenceKind.KERNEL_CHECKED_PROOF,
        validity=ValidityStatus.STALE,
    )
    assert evidence_satisfies(
        stale, EvidenceKind.KERNEL_CHECKED_PROOF, contract.scope, contract.contract_id
    ) is False
    other_scope = _scope(parameter_domain="n ≤ N")
    assert evidence_satisfies(
        passing, EvidenceKind.KERNEL_CHECKED_PROOF, other_scope, contract.contract_id
    ) is False
    other_contract = _contract(version=2)
    assert evidence_satisfies(
        passing,
        EvidenceKind.KERNEL_CHECKED_PROOF,
        contract.scope,
        other_contract.contract_id,
    ) is False
    assert evidence_satisfies(
        passing,
        EvidenceKind.KERNEL_CHECKED_PROOF,
        contract.scope,
        contract.contract_id,
        exact_claim="a different claim",
    ) is False
    assert evidence_satisfies(
        passing,
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        contract.scope,
        contract.contract_id,
    ) is False


def test_failure_self_declared_evidence_kind_must_be_cross_checked():
    contract = _contract()
    heuristic = _evidence(
        contract=contract,
        evidence_kind=EvidenceKind.HEURISTIC_OBSERVATION,
        checker_id=None,
        checker_version=None,
        checker_result=None,
    )
    failed_check = _evidence(
        contract=contract,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        checker_result=CheckerResult.FAIL,
    )
    record = _failure(
        kind=FailureKind.CERTIFIED_COUNTEREXAMPLE,
        scope=contract.scope,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        witness_refs=(heuristic.artifact_id, failed_check.artifact_id),
        exclusion_scope=contract.scope,
        contract_id=contract.contract_id,
        confidence=ConfidenceClass.CERTAIN,
    )
    assert record.evidence_kind is EvidenceKind.CHECKED_COUNTEREXAMPLE
    assert evidence_satisfies(
        heuristic,
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        contract.scope,
        contract.contract_id,
    ) is False
    assert evidence_satisfies(
        failed_check,
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        contract.scope,
        contract.contract_id,
    ) is False
    passing = _evidence(
        contract=contract,
        evidence_kind=EvidenceKind.CHECKED_COUNTEREXAMPLE,
        checker_result=CheckerResult.PASS,
    )
    assert evidence_satisfies(
        passing,
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        record.scope,
        record.contract_id,
        record.exact_failed_claim,
    )


def test_failure_and_contract_ids_use_phase1_stable_id():
    contract = _contract()
    assert len(contract.contract_id) == 64
    assert contract.contract_id == contract.contract_id.lower()
    record = _failure()
    assert len(record.failure_id) == 64 and record.failure_id == record.failure_id.lower()
    later = _failure(created_at="2026-06-01T00:00:00Z")
    assert later.failure_id == record.failure_id
    assert later.created_at != record.created_at


# --------------------------------------------------------------------------- #
# truth-boundary static guard                                                 #
# --------------------------------------------------------------------------- #


def test_production_modules_do_not_import_truth_stores():
    forbidden_modules = {
        "euler.core",
        "euler.core.factgraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("contracts.py", "failures.py"):
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
        assert "fact_graph/" not in source
        assert "global_memory/" not in source


def main() -> None:
    tests = [
        test_quantifier_order_changes_scope_and_contract_id,
        test_z_versus_q_domain_changes_scope,
        test_finite_versus_uniform_parameter_domain_changes_scope,
        test_object_identity_is_semantic,
        test_definitions_hash_change_produces_new_scope,
        test_original_mathematical_text_is_preserved,
        test_contract_semantic_changes_require_new_id_and_positive_version,
        test_evidence_artifact_is_metadata_not_a_fact,
        test_infra_extraction_budget_failures_have_no_mathematical_exclusion,
        test_invalid_proof_does_not_refute_a_proposition,
        test_certified_counterexample_requires_checked_evidence_and_exact_scope,
        test_semantic_recurrence_is_a_warning_and_reopen_conditions_persist,
        test_evidence_satisfaction_matrix_is_not_a_total_order,
        test_checker_fail_unknown_and_mismatches_do_not_satisfy,
        test_failure_self_declared_evidence_kind_must_be_cross_checked,
        test_failure_and_contract_ids_use_phase1_stable_id,
        test_production_modules_do_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL CONTRACT/FAILURE TESTS PASSED")


if __name__ == "__main__":
    main()
