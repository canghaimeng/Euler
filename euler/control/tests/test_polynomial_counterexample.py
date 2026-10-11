"""Offline tests for the exact polynomial counterexample plugin.

Covers every relation, negative/large exact arithmetic, canonicalization,
enumeration order, domain/hash sensitivity, search/checker agreement,
tamper/out-of-domain/missing/extra keys, independent path/identity, and
forbidden imports/calls.

Runs standalone (``python3 -B -m euler.control.tests.test_polynomial_counterexample``).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

from euler.control.compute_plugins.polynomial_counterexample import (
    CHECKER_CODE_HASH,
    CHECKER_COMPONENT_ID,
    SEARCHER_CODE_HASH,
    SEARCHER_COMPONENT_ID,
    IntegerInterval,
    PolynomialChecker,
    PolynomialPluginError,
    PolynomialPredicate,
    PolynomialRelation,
    PolynomialSearcher,
    PolynomialTerm,
)
from euler.control.contracts import CheckerResult
from euler.control.ids import canonical_json, sha256_hex

_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN = _ROOT / "compute_plugins" / "polynomial_counterexample.py"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(
        f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}"
    )


def _poly(
    *,
    variables: tuple[str, ...] = ("x",),
    lower: int = 0,
    upper: int = 3,
    terms: object | None = None,
    relation: PolynomialRelation = PolynomialRelation.EQ,
    intervals: object | None = None,
) -> PolynomialPredicate:
    if terms is None:
        terms = (PolynomialTerm(coefficient=1, exponents=(1,)),)
    if intervals is None:
        intervals = tuple(
            IntegerInterval(lower=lower, upper=upper) for _ in variables
        )
    return PolynomialPredicate.build(
        variables=variables,
        intervals=intervals,
        terms=terms,
        relation=relation,
    )


def _request(predicate: PolynomialPredicate) -> SimpleNamespace:
    meta = predicate.request_metadata()
    return SimpleNamespace(**meta)


def _hex64(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def test_all_relations_search_and_check():
    searcher = PolynomialSearcher()
    checker = PolynomialChecker()
    cases = (
        (PolynomialRelation.EQ, 0, True),
        (PolynomialRelation.NE, 0, False),
        (PolynomialRelation.LT, -1, True),
        (PolynomialRelation.LE, 0, True),
        (PolynomialRelation.GT, 1, True),
        (PolynomialRelation.GE, 0, True),
    )
    for relation, point, holds in cases:
        predicate = _poly(
            lower=-2,
            upper=2,
            terms=(PolynomialTerm(coefficient=1, exponents=(1,)),),
            relation=relation,
        )
        request = _request(predicate)
        assignment = {"x": point}
        assert searcher.predicate_holds(request, assignment) is holds
        payload = canonical_json(assignment).encode("utf-8")
        result, _detail = checker.check(request, payload)
        if holds:
            assert result is CheckerResult.FAIL
        else:
            assert result is CheckerResult.PASS


def test_negative_and_large_exact_arithmetic():
    huge = 2**200
    predicate = PolynomialPredicate.build(
        variables=("x", "y"),
        intervals=(
            IntegerInterval(lower=-3, upper=3),
            IntegerInterval(lower=-1, upper=1),
        ),
        terms=(
            PolynomialTerm(coefficient=huge, exponents=(3, 0)),
            PolynomialTerm(coefficient=-huge, exponents=(1, 0)),
            PolynomialTerm(coefficient=-1, exponents=(0, 1)),
        ),
        relation=PolynomialRelation.EQ,
    )
    searcher = PolynomialSearcher()
    request = _request(predicate)
    # huge*(x^3 - x) - y == 0 at x=1, y=0 because 1-1=0
    assert searcher.predicate_holds(request, {"x": 1, "y": 0}) is True
    assert searcher.predicate_holds(request, {"x": -1, "y": 0}) is True
    assert searcher.predicate_holds(request, {"x": 2, "y": 0}) is False
    value_at_two = huge * (8 - 2)
    assert value_at_two == huge * 6
    checker = PolynomialChecker()
    result, _ = checker.check(
        request, canonical_json({"x": 2, "y": 0}).encode("utf-8")
    )
    assert result is CheckerResult.PASS
    source = _PLUGIN.read_text(encoding="utf-8")
    assert "float(" not in source
    assert "math." not in source


def test_canonicalization_combines_and_drops_zeros():
    predicate = PolynomialPredicate.build(
        variables=("x", "y"),
        intervals=(
            IntegerInterval(lower=0, upper=1),
            IntegerInterval(lower=0, upper=1),
        ),
        terms=(
            PolynomialTerm(coefficient=2, exponents=(1, 0)),
            PolynomialTerm(coefficient=3, exponents=(0, 1)),
            PolynomialTerm(coefficient=1, exponents=(1, 0)),
            PolynomialTerm(coefficient=-5, exponents=(0, 1)),
            PolynomialTerm(coefficient=0, exponents=(2, 2)),
            PolynomialTerm(coefficient=4, exponents=(0, 0)),
            PolynomialTerm(coefficient=-4, exponents=(0, 0)),
        ),
        relation=PolynomialRelation.NE,
    )
    assert predicate.terms == (
        PolynomialTerm(coefficient=-2, exponents=(0, 1)),
        PolynomialTerm(coefficient=3, exponents=(1, 0)),
    )
    text = predicate.to_plugin_payload_json()
    assert text == canonical_json(json.loads(text))
    roundtrip = PolynomialPredicate.from_plugin_payload_json(text)
    assert roundtrip == predicate
    again = PolynomialPredicate.from_plugin_payload_json(
        {
            "relation": "NE",
            "terms": [
                {"exponents": [0, 1], "coefficient": -2},
                {"coefficient": 3, "exponents": [1, 0]},
            ],
            "variables": ["x", "y"],
            "intervals": [{"upper": 1, "lower": 0}, {"lower": 0, "upper": 1}],
        }
    )
    assert again.to_plugin_payload_json() == text


def test_rejects_bool_float_extra_duplicate_arity_and_caps():
    _raises(
        PolynomialPluginError,
        IntegerInterval,
        lower=True,
        upper=1,
    )
    _raises(
        PolynomialPluginError,
        PolynomialTerm,
        coefficient=1.5,
        exponents=(1,),
    )
    _raises(
        PolynomialPluginError,
        PolynomialPredicate.from_plugin_payload_json,
        {
            "variables": ["x"],
            "intervals": [{"lower": 0, "upper": 1}],
            "terms": [{"coefficient": 1, "exponents": [1]}],
            "relation": "EQ",
            "extra": True,
        },
    )
    _raises(
        PolynomialPluginError,
        PolynomialPredicate.build,
        variables=("x", "x"),
        intervals=(
            IntegerInterval(lower=0, upper=1),
            IntegerInterval(lower=0, upper=1),
        ),
        terms=(PolynomialTerm(coefficient=1, exponents=(1, 0)),),
        relation=PolynomialRelation.EQ,
    )
    _raises(
        PolynomialPluginError,
        PolynomialTerm.from_dict,
        {"coefficient": 1, "exponents": [1]},
        arity=2,
    )
    _raises(
        PolynomialPluginError,
        PolynomialPredicate.build,
        variables=tuple(f"v{i}" for i in range(9)),
        intervals=tuple(IntegerInterval(lower=0, upper=0) for _ in range(9)),
        terms=(),
        relation=PolynomialRelation.EQ,
    )
    _raises(
        PolynomialPluginError,
        PolynomialTerm,
        coefficient=1,
        exponents=(65,),
    )
    _raises(
        PolynomialPluginError,
        PolynomialTerm,
        coefficient=2**256,
        exponents=(1,),
    )
    too_many = [{"coefficient": 1, "exponents": [0]} for _ in range(257)]
    _raises(
        PolynomialPluginError,
        PolynomialPredicate.build,
        variables=("x",),
        intervals=(IntegerInterval(lower=0, upper=1),),
        terms=too_many,
        relation=PolynomialRelation.EQ,
    )


def test_lexicographic_enumeration_and_first_violation():
    predicate = PolynomialPredicate.build(
        variables=("x", "y"),
        intervals=(
            IntegerInterval(lower=0, upper=1),
            IntegerInterval(lower=2, upper=3),
        ),
        terms=(PolynomialTerm(coefficient=1, exponents=(1, 0)),),
        relation=PolynomialRelation.EQ,
    )
    searcher = PolynomialSearcher()
    request = _request(predicate)
    assert searcher.total_points(request) == 4
    expected = (
        {"x": 0, "y": 2},
        {"x": 0, "y": 3},
        {"x": 1, "y": 2},
        {"x": 1, "y": 3},
    )
    for ordinal, assignment in enumerate(expected):
        assert dict(searcher.assignment_at(request, ordinal)) == assignment
    first = None
    for ordinal in range(4):
        assignment = searcher.assignment_at(request, ordinal)
        if not searcher.predicate_holds(request, assignment):
            first = dict(assignment)
            break
    assert first == {"x": 1, "y": 2}


def test_domain_and_payload_hash_sensitivity():
    base = _poly(upper=3)
    wider = _poly(upper=4)
    other_rel = _poly(upper=3, relation=PolynomialRelation.NE)
    assert base.domain_hash() != wider.domain_hash()
    assert base.payload_hash() != wider.payload_hash()
    assert base.domain_hash() == _poly(upper=3, relation=PolynomialRelation.NE).domain_hash()
    assert base.payload_hash() != other_rel.payload_hash()
    assert _hex64(base.domain_hash())
    assert _hex64(base.payload_hash())
    wrong_plugin = _request(base)
    wrong_plugin.plugin_id = "other-plugin"
    _raises(
        PolynomialPluginError,
        PolynomialSearcher().total_points,
        wrong_plugin,
    )


def test_search_and_checker_agree_on_every_point():
    predicate = PolynomialPredicate.build(
        variables=("x", "y"),
        intervals=(
            IntegerInterval(lower=-2, upper=2),
            IntegerInterval(lower=-1, upper=1),
        ),
        terms=(
            PolynomialTerm(coefficient=1, exponents=(2, 0)),
            PolynomialTerm(coefficient=-1, exponents=(0, 2)),
            PolynomialTerm(coefficient=-1, exponents=(0, 0)),
        ),
        relation=PolynomialRelation.LT,
    )
    searcher = PolynomialSearcher()
    checker = PolynomialChecker()
    request = _request(predicate)
    total = searcher.total_points(request)
    assert total == 5 * 3
    for ordinal in range(total):
        assignment = dict(searcher.assignment_at(request, ordinal))
        holds = searcher.predicate_holds(request, assignment)
        payload = canonical_json(assignment).encode("utf-8")
        result, _ = checker.check(request, payload)
        if holds:
            assert result is CheckerResult.FAIL
        else:
            assert result is CheckerResult.PASS


def test_tamper_out_of_domain_missing_and_extra_keys():
    predicate = _poly(lower=0, upper=2, relation=PolynomialRelation.EQ)
    request = _request(predicate)
    checker = PolynomialChecker()
    good = canonical_json({"x": 1}).encode("utf-8")
    assert checker.check(request, good)[0] is CheckerResult.PASS
    missing = canonical_json({}).encode("utf-8")
    assert checker.check(request, missing)[0] is CheckerResult.FAIL
    extra = canonical_json({"x": 1, "y": 0}).encode("utf-8")
    assert checker.check(request, extra)[0] is CheckerResult.FAIL
    out = canonical_json({"x": 9}).encode("utf-8")
    assert checker.check(request, out)[0] is CheckerResult.FAIL
    boolean = canonical_json({"x": True}).encode("utf-8")
    assert checker.check(request, boolean)[0] is CheckerResult.FAIL
    unknown = checker.check(request, b"not-json")[0]
    assert unknown is CheckerResult.UNKNOWN
    floaty = b'{"x": 1.0}'
    assert checker.check(request, floaty)[0] in {CheckerResult.FAIL, CheckerResult.UNKNOWN}
    tampered_hash = SimpleNamespace(
        plugin_payload_json=predicate.to_plugin_payload_json(),
        plugin_payload_hash=sha256_hex("tampered"),
        domain_hash=predicate.domain_hash(),
        declared_total_points=predicate.total_points(),
    )
    assert checker.check(tampered_hash, good)[0] is CheckerResult.UNKNOWN
    searcher = PolynomialSearcher()
    _raises(PolynomialPluginError, searcher.predicate_holds, request, {"x": True})
    _raises(PolynomialPluginError, searcher.predicate_holds, request, {"x": 9})


def test_independent_path_and_identity():
    searcher = PolynomialSearcher()
    checker = PolynomialChecker()
    assert searcher.identity.component_id != checker.identity.component_id
    assert searcher.identity.code_hash != checker.identity.code_hash
    assert searcher.identity.component_id == SEARCHER_COMPONENT_ID
    assert checker.identity.component_id == CHECKER_COMPONENT_ID
    assert searcher.identity.code_hash == SEARCHER_CODE_HASH
    assert checker.identity.code_hash == CHECKER_CODE_HASH
    assert _hex64(searcher.identity.component_id)
    assert _hex64(checker.identity.code_hash)
    source = _PLUGIN.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_PLUGIN))
    pow_funcs = set()
    mul_funcs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            calls = {
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if node.name == "_eval_poly_pow":
                assert "pow" in names or "pow" in calls
                pow_funcs.add(node.name)
            if node.name in {"_eval_poly_mul", "_int_pow_by_mul"}:
                assert "pow" not in calls
                mul_funcs.add(node.name)
            if node.name == "check":
                assert "_eval_poly_pow" not in names
                assert "predicate_holds" not in names
                assert "pow" not in calls
    assert "_eval_poly_pow" in pow_funcs
    assert "_eval_poly_mul" in mul_funcs
    assert "_int_pow_by_mul" in mul_funcs


def test_source_forbids_codegen_network_and_truth_stores():
    forbidden_names = {"eval", "exec", "compile"}
    forbidden_attrs = {"Popen", "urlopen", "system"}
    forbidden_modules = {
        "subprocess",
        "socket",
        "requests",
        "httpx",
        "euler.core.factgraph",
        "euler.gateway",
        "euler.verify",
        "euler.control.views",
        "euler.control.runtime",
    }
    source = _PLUGIN.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_PLUGIN))
    imported: set[str] = set()
    named: set[str] = set()
    name_calls: set[str] = set()
    attr_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
                named.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                named.add(alias.name)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                name_calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                attr_calls.add(node.func.attr)
    assert imported.isdisjoint(forbidden_modules)
    assert "EvidenceArtifact" not in named
    assert "ResearchView" not in named
    assert "FactGraph" not in named
    assert name_calls.isdisjoint(forbidden_names)
    assert attr_calls.isdisjoint(forbidden_attrs)
    assert "subprocess" not in source
    assert "FactGraph.add" not in source


def main() -> None:
    tests = [
        test_all_relations_search_and_check,
        test_negative_and_large_exact_arithmetic,
        test_canonicalization_combines_and_drops_zeros,
        test_rejects_bool_float_extra_duplicate_arity_and_caps,
        test_lexicographic_enumeration_and_first_violation,
        test_domain_and_payload_hash_sensitivity,
        test_search_and_checker_agree_on_every_point,
        test_tamper_out_of_domain_missing_and_extra_keys,
        test_independent_path_and_identity,
        test_source_forbids_codegen_network_and_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL POLYNOMIAL COUNTEREXAMPLE TESTS PASSED")


if __name__ == "__main__":
    main()
