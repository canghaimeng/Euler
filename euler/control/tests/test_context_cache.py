"""Offline tests for semantic ContextCacheKey construction and comparison.

Covers every Phase 4 §9 cache invalidation source, cosmetic mapping order,
fact content/predecessor changes, evidence checker/environment, policy,
compiler version, limits, output contract, concrete gap, evidence expansion,
content-hash tamper, roundtrip, and fail-closed malformed inputs. No cache
writes, model calls, or truth-store imports.

Runs standalone (``python -m euler.control.tests.test_context_cache``).
"""

from __future__ import annotations

import ast
from pathlib import Path

from euler.control.cache import (
    ContextCacheError,
    ContextCacheKey,
    ContextInvalidationReason,
    build_context_cache_key,
    compare_context_keys,
)
from euler.control.contracts import (
    MathematicalScope,
    QuantifierBinding,
    QuantifierKind,
)
from euler.control.ids import sha256_hex

_ROOT = Path(__file__).resolve().parents[1]


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(
        f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}"
    )


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope(*, domain: str = "Z", assumptions: tuple[str, ...] = ("n is an integer",)) -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=assumptions,
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain=domain),
        ),
        objects=(),
        parameter_domain=domain,
        definitions_hash=_hex("defs", domain),
    )


def _limits(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "max_chars": {"kind": "LIMITED", "value": 8000},
        "max_bytes": {"kind": "UNLIMITED", "value": None},
        "max_failures": {"kind": "LIMITED", "value": 4},
        "max_relations": {"kind": "UNLIMITED", "value": None},
    }
    payload.update(overrides)
    return payload


def _base(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "contract_id": _hex("contract"),
        "obligation_id": _hex("obligation"),
        "route_id": _hex("route"),
        "action_id": _hex("action"),
        "definitions": _scope(),
        "facts": (
            {
                "fact_id": "lemma-a",
                "content_hash": _hex("fact-a"),
                "predecessors": ("axiom-1", "axiom-2"),
            },
            {
                "fact_id": "lemma-b",
                "content_hash": _hex("fact-b"),
                "predecessors": (),
            },
        ),
        "evidence": (
            {
                "evidence_id": _hex("ev-1"),
                "content_hash": _hex("ev-content"),
                "environment_hash": _hex("env"),
                "checker_id": "lean",
                "checker_version": "1",
                "checker_result": "PASS",
            },
        ),
        "failure_ids": (_hex("fail-1"), _hex("fail-2")),
        "relation_ids": (_hex("rel-1"),),
        "tool_environment_hash": _hex("tool"),
        "acceptance_policy_hash": _hex("accept"),
        "reference_versions_hash": _hex("refs"),
        "output_contract": {"format": "lean", "kernel": "lean4"},
        "concrete_gap": "prove the remaining even case",
        "expand_evidence_content": False,
        "packet_policy": {"expand_evidence_content": False},
        "limits": _limits(),
        "compiler_version": 1,
        "selection_policy_version": 1,
    }
    kwargs.update(overrides)
    return kwargs


def _key(**overrides: object) -> ContextCacheKey:
    return build_context_cache_key(**_base(**overrides))


def test_identical_keys_compare_empty():
    first = _key()
    second = _key()
    assert first == second
    assert first.key_id == second.key_id
    assert compare_context_keys(first, second) == ()
    for name in (
        "key_id",
        "contract_hash",
        "obligation_hash",
        "route_hash",
        "action_hash",
        "definitions_hash",
        "facts_hash",
        "evidence_hash",
        "failures_hash",
        "relations_hash",
        "tool_hash",
        "acceptance_hash",
        "reference_versions_hash",
        "output_contract_hash",
        "concrete_gap_hash",
        "packet_policy_hash",
        "limits_hash",
        "compiler_version_hash",
        "selection_policy_hash",
    ):
        value = getattr(first, name)
        assert isinstance(value, str) and len(value) == 64
        assert all(char in "0123456789abcdef" for char in value)


def test_cosmetic_mapping_order_does_not_alter_key():
    facts_a = {
        "lemma-b": {"content_hash": _hex("fact-b"), "predecessors": ()},
        "lemma-a": {
            "content_hash": _hex("fact-a"),
            "predecessors": ("axiom-1", "axiom-2"),
        },
    }
    facts_b = {
        "lemma-a": {
            "predecessors": ("axiom-1", "axiom-2"),
            "content_hash": _hex("fact-a"),
        },
        "lemma-b": {"predecessors": (), "content_hash": _hex("fact-b")},
    }
    evidence_a = {
        _hex("ev-1"): {
            "checker_result": "PASS",
            "content_hash": _hex("ev-content"),
            "environment_hash": _hex("env"),
            "checker_version": "1",
            "checker_id": "lean",
        }
    }
    evidence_b = {
        _hex("ev-1"): {
            "checker_id": "lean",
            "checker_version": "1",
            "checker_result": "PASS",
            "environment_hash": _hex("env"),
            "content_hash": _hex("ev-content"),
        }
    }
    output_a = {"kernel": "lean4", "format": "lean"}
    output_b = {"format": "lean", "kernel": "lean4"}
    limits_a = _limits()
    limits_b = {
        "max_relations": {"value": None, "kind": "UNLIMITED"},
        "max_bytes": {"kind": "UNLIMITED", "value": None},
        "max_failures": {"kind": "LIMITED", "value": 4},
        "max_chars": {"value": 8000, "kind": "LIMITED"},
    }
    policy_a = {"render": "full", "expand_evidence_content": False}
    policy_b = {"expand_evidence_content": False, "render": "full"}
    first = _key(
        facts=facts_a,
        evidence=evidence_a,
        output_contract=output_a,
        limits=limits_a,
        packet_policy=policy_a,
        failure_ids={_hex("fail-2"): True, _hex("fail-1"): True},
    )
    second = _key(
        facts=facts_b,
        evidence=evidence_b,
        output_contract=output_b,
        limits=limits_b,
        packet_policy=policy_b,
        failure_ids={_hex("fail-1"): True, _hex("fail-2"): True},
    )
    assert first.key_id == second.key_id
    assert compare_context_keys(first, second) == ()


def test_required_sequence_order_is_semantic():
    reversed_facts = (
        {
            "fact_id": "lemma-b",
            "content_hash": _hex("fact-b"),
            "predecessors": (),
        },
        {
            "fact_id": "lemma-a",
            "content_hash": _hex("fact-a"),
            "predecessors": ("axiom-1", "axiom-2"),
        },
    )
    changed = _key(facts=reversed_facts)
    assert compare_context_keys(_key(), changed) == (ContextInvalidationReason.FACTS,)
    reversed_failures = (_hex("fail-2"), _hex("fail-1"))
    assert compare_context_keys(_key(), _key(failure_ids=reversed_failures)) == (
        ContextInvalidationReason.FAILURES,
    )
    reversed_relations = (_hex("rel-2"), _hex("rel-1"))
    original_relations = (_hex("rel-1"), _hex("rel-2"))
    assert compare_context_keys(
        _key(relation_ids=original_relations),
        _key(relation_ids=reversed_relations),
    ) == (ContextInvalidationReason.RELATIONS,)


def test_predecessor_order_is_preserved():
    flipped = (
        {
            "fact_id": "lemma-a",
            "content_hash": _hex("fact-a"),
            "predecessors": ("axiom-2", "axiom-1"),
        },
        {
            "fact_id": "lemma-b",
            "content_hash": _hex("fact-b"),
            "predecessors": (),
        },
    )
    assert compare_context_keys(_key(), _key(facts=flipped)) == (
        ContextInvalidationReason.FACTS,
    )


def test_section9_invalidation_sources_alter_key():
    baseline = _key()
    cases = (
        (
            ContextInvalidationReason.DEFINITIONS,
            {"definitions": _scope(assumptions=("n is even",))},
        ),
        (
            ContextInvalidationReason.FACTS,
            {
                "facts": (
                    {
                        "fact_id": "lemma-a",
                        "content_hash": _hex("fact-a"),
                        "predecessors": ("axiom-1",),
                    },
                    {
                        "fact_id": "lemma-b",
                        "content_hash": _hex("fact-b"),
                        "predecessors": (),
                    },
                )
            },
        ),
        (
            ContextInvalidationReason.EVIDENCE,
            {
                "evidence": (
                    {
                        "evidence_id": _hex("ev-1"),
                        "content_hash": _hex("ev-content"),
                        "environment_hash": _hex("env-changed"),
                        "checker_id": "lean",
                        "checker_version": "1",
                        "checker_result": "PASS",
                    },
                )
            },
        ),
        (
            ContextInvalidationReason.TOOL,
            {"tool_environment_hash": _hex("tool-b")},
        ),
        (
            ContextInvalidationReason.REFERENCE_VERSIONS,
            {"reference_versions_hash": _hex("refs-b")},
        ),
        (
            ContextInvalidationReason.ACCEPTANCE,
            {"acceptance_policy_hash": _hex("accept-b")},
        ),
        (
            ContextInvalidationReason.SELECTION_POLICY,
            {"selection_policy_version": 2},
        ),
        (
            ContextInvalidationReason.LIMITS,
            {"limits": _limits(max_chars={"kind": "LIMITED", "value": 100})},
        ),
        (
            ContextInvalidationReason.CONTRACT,
            {"contract_id": _hex("contract-b")},
        ),
        (
            ContextInvalidationReason.OBLIGATION,
            {"obligation_id": _hex("obligation-b")},
        ),
        (
            ContextInvalidationReason.ROUTE,
            {"route_id": _hex("route-b")},
        ),
        (
            ContextInvalidationReason.ACTION,
            {"action_id": _hex("action-b")},
        ),
        (
            ContextInvalidationReason.FAILURES,
            {"failure_ids": (_hex("fail-9"),)},
        ),
        (
            ContextInvalidationReason.RELATIONS,
            {"relation_ids": (_hex("rel-9"),)},
        ),
        (
            ContextInvalidationReason.OUTPUT_CONTRACT,
            {"output_contract": {"format": "lean", "kernel": "lean4", "extra": True}},
        ),
        (
            ContextInvalidationReason.COMPILER_VERSION,
            {"compiler_version": 2},
        ),
        (
            ContextInvalidationReason.CONCRETE_GAP,
            {"concrete_gap": "a different remaining gap"},
        ),
        (
            ContextInvalidationReason.PACKET_POLICY,
            {
                "expand_evidence_content": True,
                "packet_policy": {"expand_evidence_content": True},
            },
        ),
        (
            ContextInvalidationReason.PACKET_POLICY,
            {
                "expand_proof_content": True,
                "packet_policy": {
                    "expand_evidence_content": False,
                    "expand_proof_content": True,
                },
            },
        ),
    )
    seen: set[ContextInvalidationReason] = set()
    for reason, overrides in cases:
        changed = _key(**overrides)
        assert changed.key_id != baseline.key_id
        assert compare_context_keys(baseline, changed) == (reason,)
        seen.add(reason)
    for reason in ContextInvalidationReason:
        assert reason in seen, reason


def test_fact_content_hash_change_invalidates_facts():
    facts = (
        {
            "fact_id": "lemma-a",
            "content_hash": _hex("fact-a-tampered"),
            "predecessors": ("axiom-1", "axiom-2"),
        },
        {
            "fact_id": "lemma-b",
            "content_hash": _hex("fact-b"),
            "predecessors": (),
        },
    )
    assert compare_context_keys(_key(), _key(facts=facts)) == (
        ContextInvalidationReason.FACTS,
    )


def test_evidence_checker_and_environment_changes():
    checker = (
        {
            "evidence_id": _hex("ev-1"),
            "content_hash": _hex("ev-content"),
            "environment_hash": _hex("env"),
            "checker_id": "lean",
            "checker_version": "2",
            "checker_result": "PASS",
        },
    )
    env = (
        {
            "evidence_id": _hex("ev-1"),
            "content_hash": _hex("ev-content"),
            "environment_hash": _hex("env-2"),
            "checker_id": "lean",
            "checker_version": "1",
            "checker_result": "PASS",
        },
    )
    result = (
        {
            "evidence_id": _hex("ev-1"),
            "content_hash": _hex("ev-content"),
            "environment_hash": _hex("env"),
            "checker_id": "lean",
            "checker_version": "1",
            "checker_result": "FAIL",
        },
    )
    assert compare_context_keys(_key(), _key(evidence=checker)) == (
        ContextInvalidationReason.EVIDENCE,
    )
    assert compare_context_keys(_key(), _key(evidence=env)) == (
        ContextInvalidationReason.EVIDENCE,
    )
    assert compare_context_keys(_key(), _key(evidence=result)) == (
        ContextInvalidationReason.EVIDENCE,
    )


def test_policy_version_limits_and_output_changes():
    baseline = _key()
    assert compare_context_keys(baseline, _key(selection_policy_version=3)) == (
        ContextInvalidationReason.SELECTION_POLICY,
    )
    assert compare_context_keys(baseline, _key(compiler_version=9)) == (
        ContextInvalidationReason.COMPILER_VERSION,
    )
    unlimited_chars = _limits(max_chars={"kind": "UNLIMITED", "value": None})
    assert compare_context_keys(baseline, _key(limits=unlimited_chars)) == (
        ContextInvalidationReason.LIMITS,
    )
    assert compare_context_keys(
        baseline, _key(output_contract="exact lean kernel packet")
    ) == (ContextInvalidationReason.OUTPUT_CONTRACT,)


def test_multiple_component_changes_return_all_reasons():
    changed = _key(
        tool_environment_hash=_hex("tool-b"),
        limits=_limits(max_failures={"kind": "LIMITED", "value": 1}),
        compiler_version=2,
    )
    assert compare_context_keys(_key(), changed) == (
        ContextInvalidationReason.TOOL,
        ContextInvalidationReason.LIMITS,
        ContextInvalidationReason.COMPILER_VERSION,
    )


def test_optional_route_absence_is_stable_and_distinct():
    with_route = _key()
    without = _key(route_id=None)
    assert compare_context_keys(with_route, without) == (ContextInvalidationReason.ROUTE,)
    assert compare_context_keys(without, _key(route_id=None)) == ()


def test_content_hash_tamper_fails_closed():
    facts = (
        {
            "fact_id": "lemma-a",
            "content_hash": _hex("fact-a"),
            "predecessors": (),
            "content": "this is not the hashed body",
        },
    )
    _raises(ContextCacheError, build_context_cache_key, **_base(facts=facts))
    matching = (
        {
            "fact_id": "lemma-a",
            "content_hash": sha256_hex("exact-body"),
            "predecessors": (),
            "content": "exact-body",
        },
    )
    ok = _key(facts=matching)
    also = _key(
        facts=(
            {
                "fact_id": "lemma-a",
                "content_hash": sha256_hex("exact-body"),
                "predecessors": (),
            },
        )
    )
    assert ok.key_id == also.key_id
    evidence = (
        {
            "evidence_id": _hex("ev-1"),
            "content_hash": _hex("ev-content"),
            "environment_hash": _hex("env"),
            "content": b"nope",
        },
    )
    _raises(ContextCacheError, build_context_cache_key, **_base(evidence=evidence))


def test_unknown_and_malformed_fields_fail_closed():
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(facts=({"fact_id": "x", "content_hash": _hex("a"), "extra": 1},)),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(definitions={"parameter_domain": "Z"}),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(limits=_limits(max_tokens={"kind": "LIMITED", "value": 1})),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(limits=_limits(max_chars={"kind": "LIMITED", "value": None})),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(limits=_limits(max_bytes={"kind": "UNLIMITED", "value": 0})),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(limits=_limits(max_chars={"kind": "LIMITED", "value": -1})),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(tool_environment_hash="not-hex"),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(compiler_version=True),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(failure_ids="not-a-list"),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(
            evidence=(
                {
                    "evidence_id": _hex("ev-1"),
                    "content_hash": _hex("ev-content"),
                    "environment_hash": _hex("env"),
                    "checker_id": "lean",
                },
            )
        ),
    )
    _raises(ContextCacheError, compare_context_keys, _key(), "nope")
    _raises(
        ContextCacheError,
        ContextCacheKey,
        key_id=_hex("k"),
        contract_hash=_hex("c"),
        obligation_hash=_hex("o"),
        route_hash=_hex("r"),
        action_hash=_hex("a"),
        definitions_hash=_hex("d"),
        facts_hash=_hex("f"),
        evidence_hash=_hex("e"),
        failures_hash=_hex("fail"),
        relations_hash=_hex("rel"),
        tool_hash=_hex("t"),
        acceptance_hash=_hex("acc"),
        reference_versions_hash=_hex("ref"),
        output_contract_hash=_hex("out"),
        concrete_gap_hash=_hex("gap"),
        packet_policy_hash=_hex("pkt"),
        limits_hash=_hex("lim"),
        compiler_version_hash=_hex("comp"),
        selection_policy_hash=_hex("sel"),
    )
    _raises(ContextCacheError, build_context_cache_key, **_base(concrete_gap=""))
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(expand_evidence_content=1, packet_policy={"expand_evidence_content": 1}),
    )
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(expand_evidence_content=None, packet_policy={}),
    )


def test_gap_and_expansion_change_key():
    baseline = _key()
    gap = _key(concrete_gap="close the odd Goldbach case")
    assert gap.key_id != baseline.key_id
    assert compare_context_keys(baseline, gap) == (ContextInvalidationReason.CONCRETE_GAP,)
    expanded = _key(
        expand_evidence_content=True,
        packet_policy={"expand_evidence_content": True},
    )
    assert expanded.key_id != baseline.key_id
    assert compare_context_keys(baseline, expanded) == (
        ContextInvalidationReason.PACKET_POLICY,
    )
    both = _key(
        concrete_gap="close the odd Goldbach case",
        expand_evidence_content=True,
        packet_policy={"expand_evidence_content": True},
    )
    assert compare_context_keys(baseline, both) == (
        ContextInvalidationReason.CONCRETE_GAP,
        ContextInvalidationReason.PACKET_POLICY,
    )
    top_level_only = build_context_cache_key(
        **{
            **_base(),
            "packet_policy": None,
            "expand_evidence_content": False,
        }
    )
    assert top_level_only.key_id == baseline.key_id


def test_packet_policy_mapping_order_is_cosmetic():
    first = _key(
        packet_policy={
            "notes": "render",
            "expand_evidence_content": True,
            "section": "manifest",
        },
        expand_evidence_content=True,
    )
    second = _key(
        packet_policy={
            "section": "manifest",
            "expand_evidence_content": True,
            "notes": "render",
        },
        expand_evidence_content=True,
    )
    assert first.key_id == second.key_id
    assert compare_context_keys(first, second) == ()


def test_cache_key_roundtrip_and_tamper_fail_closed():
    original = _key()
    payload = original.to_dict()
    restored = ContextCacheKey.from_dict(payload)
    assert restored == original
    assert restored.key_id == original.key_id
    shuffled = {key: payload[key] for key in reversed(list(payload))}
    assert ContextCacheKey.from_dict(shuffled) == original
    tampered = dict(payload)
    tampered["concrete_gap_hash"] = _hex("tampered-gap")
    _raises(ContextCacheError, ContextCacheKey.from_dict, tampered)
    missing = dict(payload)
    missing.pop("packet_policy_hash")
    _raises(ContextCacheError, ContextCacheKey.from_dict, missing)
    extra = dict(payload)
    extra["unexpected"] = _hex("extra")
    _raises(ContextCacheError, ContextCacheKey.from_dict, extra)
    _raises(ContextCacheError, ContextCacheKey.from_dict, "not-a-mapping")
    _raises(
        ContextCacheError,
        build_context_cache_key,
        **_base(
            expand_evidence_content=True,
            packet_policy={"expand_evidence_content": False},
        ),
    )


def test_scope_mapping_matches_mathematical_scope():
    scope = _scope()
    as_mapping = {
        "objects": [],
        "assumptions": list(scope.assumptions),
        "quantifiers": [item.to_dict() for item in scope.quantifiers],
        "parameter_domain": scope.parameter_domain,
        "definitions_hash": scope.definitions_hash,
    }
    assert _key(definitions=scope).key_id == _key(definitions=as_mapping).key_id


def test_production_module_has_no_cache_writes_or_truth_imports():
    forbidden_modules = {
        "euler.core.factgraph",
        "euler.core.FactGraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    path = _ROOT / "cache.py"
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
    assert imported.isdisjoint(forbidden_modules)
    assert named.isdisjoint(forbidden_names)
    assert "FactGraph.add" not in source
    assert "fact_submit" not in source
    assert "open(" not in source
    assert ".write(" not in source
    assert "urlopen" not in source
    assert "requests" not in source


def main() -> None:
    tests = [
        test_identical_keys_compare_empty,
        test_cosmetic_mapping_order_does_not_alter_key,
        test_required_sequence_order_is_semantic,
        test_predecessor_order_is_preserved,
        test_section9_invalidation_sources_alter_key,
        test_fact_content_hash_change_invalidates_facts,
        test_evidence_checker_and_environment_changes,
        test_policy_version_limits_and_output_changes,
        test_multiple_component_changes_return_all_reasons,
        test_optional_route_absence_is_stable_and_distinct,
        test_content_hash_tamper_fails_closed,
        test_unknown_and_malformed_fields_fail_closed,
        test_gap_and_expansion_change_key,
        test_packet_policy_mapping_order_is_cosmetic,
        test_cache_key_roundtrip_and_tamper_fail_closed,
        test_scope_mapping_matches_mathematical_scope,
        test_production_module_has_no_cache_writes_or_truth_imports,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL CONTEXT CACHE TESTS PASSED")


if __name__ == "__main__":
    main()
