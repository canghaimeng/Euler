"""Offline tests for Phase 6 compute records and generic runner.

Covers malformed serialization, ID sensitivity, identity mismatch, exact
persisted candidate bytes, limit/error/checker matrix, coverage/result
invariants, CAS faults, observed usage, and an AST scan for truth/provider
isolation. Wall/CPU milliseconds are observed through search/check and do not
include the final report CAS write.

Runs standalone (``python3 -B -m euler.control.tests.test_compute_contract``).
"""

from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

from euler.control.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
)
from euler.control.compute import (
    ComponentIdentity,
    ComputeArtifactError,
    ComputeError,
    ComputeEvidenceKind,
    ComputeLimits,
    ComputeRequest,
    ComputeRunner,
    ComputeTermination,
    CoverageRecord,
)
from euler.control.contracts import (
    CheckerResult,
    MathematicalScope,
    QuantifierBinding,
    QuantifierKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.policy import DimensionLimit
from euler.control.schema import ObservationStatus

_ROOT = Path(__file__).resolve().parents[1]
_TS = "2026-01-01T00:00:00Z"
_CLAIM = "no integer in the recorded box violates the frozen predicate"


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


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


def _searcher_id(*, tag: str = "search") -> ComponentIdentity:
    return ComponentIdentity(
        component_id=f"poly-searcher-{tag}",
        version="1",
        code_hash=_hex("search-code", tag),
    )


def _checker_id(*, tag: str = "check") -> ComponentIdentity:
    return ComponentIdentity(
        component_id=f"poly-checker-{tag}",
        version="1",
        code_hash=_hex("check-code", tag),
    )


def _limits(*, points: int | None = 8, wall: int | None = None) -> ComputeLimits:
    max_points = (
        DimensionLimit.unlimited() if points is None else DimensionLimit.limited(points)
    )
    max_wall = DimensionLimit.unlimited() if wall is None else DimensionLimit.limited(wall)
    return ComputeLimits(max_points=max_points, max_wall_ms=max_wall)


def _payload(**fields: object) -> dict[str, object]:
    body: dict[str, object] = {"variables": ["n"], "relation": "EQ"}
    body.update(fields)
    return body


class ScriptedSearcher:
    def __init__(
        self,
        identity: ComponentIdentity,
        assignments: list[dict[str, int]],
        *,
        violations: frozenset[int] = frozenset(),
        total: int | None = None,
        fail: str | None = None,
        bad_assign: object = None,
        bad_pred: object = None,
    ) -> None:
        self._identity = identity
        self._assignments = [dict(item) for item in assignments]
        self._violations = frozenset(violations)
        self._total = len(self._assignments) if total is None else total
        self._fail = fail
        self._bad_assign = bad_assign
        self._bad_pred = bad_pred

    @property
    def identity(self) -> ComponentIdentity:
        return self._identity

    def total_points(self, request: ComputeRequest) -> int:
        if self._fail == "total":
            raise RuntimeError("searcher total failed")
        return self._total

    def assignment_at(self, request: ComputeRequest, ordinal: int) -> dict[str, int]:
        if self._fail == "assign":
            raise RuntimeError("searcher assignment failed")
        if self._bad_assign is not None:
            return self._bad_assign  # type: ignore[return-value]
        return dict(self._assignments[ordinal])

    def predicate_holds(self, request: ComputeRequest, assignment: object) -> bool:
        if self._fail == "pred":
            raise RuntimeError("searcher predicate failed")
        if self._bad_pred is not None:
            return self._bad_pred  # type: ignore[return-value]
        encoded = canonical_json(dict(assignment))
        for index in self._violations:
            if canonical_json(self._assignments[index]) == encoded:
                return False
        return True


class ScriptedChecker:
    def __init__(
        self,
        identity: ComponentIdentity,
        *,
        result: CheckerResult = CheckerResult.PASS,
        detail: str = "ok",
        fail: bool = False,
        malformed: bool = False,
    ) -> None:
        self._identity = identity
        self._result = result
        self._detail = detail
        self._fail = fail
        self._malformed = malformed
        self.seen: bytes | None = None

    @property
    def identity(self) -> ComponentIdentity:
        return self._identity

    def check(self, request: ComputeRequest, persisted_candidate_bytes: bytes):
        self.seen = persisted_candidate_bytes
        if self._fail:
            raise RuntimeError("checker failed")
        if self._malformed:
            return "not-a-result"
        return self._result, self._detail


class FaultyStore(ArtifactStore):
    def __init__(self, root: Path, *, fail_on: str) -> None:
        super().__init__(root)
        self.fail_on = fail_on
        self.puts = 0

    def put_bytes(self, data: bytes, *, media_type: str | None = None):
        self.puts += 1
        if self.fail_on == "put":
            raise ArtifactStoreError("injected put failure")
        if self.fail_on == "put_report" and self.puts >= 2:
            raise ArtifactStoreError("injected report put failure")
        return super().put_bytes(data, media_type=media_type)

    def get_bytes(self, content_hash: str) -> bytes:
        if self.fail_on == "get":
            raise ArtifactStoreError("injected get failure")
        return super().get_bytes(content_hash)

    def verify(self, content_hash: str):
        if self.fail_on == "verify":
            raise ArtifactIntegrityError("injected verify failure")
        return super().verify(content_hash)


def _request(
    *,
    assignments: list[dict[str, int]] | None = None,
    **overrides: object,
) -> ComputeRequest:
    points = len(assignments) if assignments is not None else 3
    kwargs: dict[str, object] = {
        "contract_id": _hex("contract"),
        "obligation_id": _hex("obligation"),
        "action_id": _hex("action"),
        "tested_claim": _CLAIM,
        "scope": _scope(),
        "plugin_id": "polynomial_counterexample",
        "plugin_version": "1",
        "plugin_payload": _payload(),
        "domain_hash": _hex("domain"),
        "declared_total_points": points,
        "limits": _limits(points=max(points, 1)),
        "searcher": _searcher_id(),
        "checker": _checker_id(),
        "tool_environment_hash": _hex("tool"),
        "created_at": _TS,
    }
    kwargs.update(overrides)
    return ComputeRequest.build(**kwargs)  # type: ignore[arg-type]


def _run(
    request: ComputeRequest,
    searcher: ScriptedSearcher,
    checker: ScriptedChecker,
    store: ArtifactStore,
):
    return ComputeRunner().run(request, searcher, checker, store)


# --------------------------------------------------------------------------- #
# records / serialization                                                     #
# --------------------------------------------------------------------------- #


def test_malformed_serialization_and_limit_rules():
    _raises(
        ComputeError,
        ComputeLimits,
        max_points=DimensionLimit.unlimited(),
        max_wall_ms=DimensionLimit.unlimited(),
    )
    _raises(ComputeError, ComputeLimits.from_dict, {"max_points": {"kind": "LIMITED"}})
    ok = _limits(points=4, wall=None)
    restored = ComputeLimits.from_dict(ok.to_dict())
    assert restored == ok
    _raises(ComputeError, CoverageRecord.from_dict, {"domain_hash": _hex("d")})
    _raises(
        ComputeError,
        CoverageRecord,
        domain_hash=_hex("d"),
        total_points=2,
        examined_points=3,
        enumeration_order="lexicographic",
        enumeration_complete=False,
    )
    _raises(
        ComputeError,
        CoverageRecord,
        domain_hash=_hex("d"),
        total_points=2,
        examined_points=1,
        enumeration_order="lexicographic",
        enumeration_complete=True,
    )
    request = _request()
    payload = request.to_dict()
    payload["extra"] = True
    _raises(ComputeError, ComputeRequest.from_dict, payload)
    missing = request.to_dict()
    del missing["plugin_payload_hash"]
    _raises(ComputeError, ComputeRequest.from_dict, missing)
    noncanonical = request.to_dict()
    noncanonical["plugin_payload_json"] = '{"variables":["n"],"relation":"EQ"}'
    _raises(ComputeError, ComputeRequest.from_dict, noncanonical)
    bad_hash = request.to_dict()
    bad_hash["plugin_payload_hash"] = _hex("tampered")
    _raises(ComputeError, ComputeRequest.from_dict, bad_hash)
    _raises(ComputeError, ComputeRequest.build, **{**_request_kwargs(), "declared_total_points": True})
    _raises(ComputeError, ComputeRequest.build, **{**_request_kwargs(), "declared_total_points": 1.5})
    _raises(
        ComputeError,
        ComputeRequest.build,
        **{**_request_kwargs(), "plugin_payload": {"n": 1.25}},
    )
    _raises(
        ComputeError,
        ComputeRequest.build,
        **{**_request_kwargs(), "created_at": "2026-01-01T00:00:00+01:00"},
    )
    _raises(
        ComputeError,
        ComputeRequest.build,
        **{**_request_kwargs(), "searcher": _checker_id(), "checker": _checker_id()},
    )
    same_hash = ComponentIdentity(
        component_id="other",
        version="1",
        code_hash=_searcher_id().code_hash,
    )
    _raises(
        ComputeError,
        ComputeRequest.build,
        **{**_request_kwargs(), "checker": same_hash},
    )
    roundtrip = ComputeRequest.from_dict(request.to_dict())
    assert roundtrip == request


def _request_kwargs() -> dict[str, object]:
    return {
        "contract_id": _hex("contract"),
        "obligation_id": _hex("obligation"),
        "action_id": _hex("action"),
        "tested_claim": _CLAIM,
        "scope": _scope(),
        "plugin_id": "polynomial_counterexample",
        "plugin_version": "1",
        "plugin_payload": _payload(),
        "domain_hash": _hex("domain"),
        "declared_total_points": 3,
        "limits": _limits(points=3),
        "searcher": _searcher_id(),
        "checker": _checker_id(),
        "tool_environment_hash": _hex("tool"),
        "created_at": _TS,
    }


def test_request_id_binds_semantics_and_excludes_created_at():
    base = _request()
    later = _request(created_at="2026-01-02T00:00:00Z")
    assert later.request_id == base.request_id
    assert later.created_at != base.created_at
    changes = {
        "contract_id": _hex("contract-2"),
        "obligation_id": _hex("obligation-2"),
        "action_id": _hex("action-2"),
        "tested_claim": "a different tested claim",
        "scope": _scope(domain="N"),
        "plugin_id": "other-plugin",
        "plugin_version": "2",
        "plugin_payload": _payload(relation="NE"),
        "domain_hash": _hex("domain-2"),
        "declared_total_points": 4,
        "limits": _limits(points=9),
        "searcher": _searcher_id(tag="alt"),
        "checker": _checker_id(tag="alt"),
        "tool_environment_hash": _hex("tool-2"),
    }
    for key, value in changes.items():
        other = _request(**{key: value})
        assert other.request_id != base.request_id, key
    unsorted = _request(plugin_payload={"relation": "EQ", "variables": ["n"]})
    sorted_payload = _request(plugin_payload={"variables": ["n"], "relation": "EQ"})
    assert unsorted.plugin_payload_json == sorted_payload.plugin_payload_json
    assert unsorted.plugin_payload_hash == sorted_payload.plugin_payload_hash
    assert unsorted.request_id == sorted_payload.request_id


# --------------------------------------------------------------------------- #
# runner                                                                      #
# --------------------------------------------------------------------------- #


def test_identity_mismatch_and_distinct_components():
    request = _request(assignments=[{"n": 0}])
    searcher = ScriptedSearcher(_searcher_id(tag="other"), [{"n": 0}])
    checker = ScriptedChecker(_checker_id())
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        _raises(ComputeError, _run, request, searcher, checker, store)
        _raises(
            ComputeError,
            _run,
            request,
            ScriptedSearcher(_searcher_id(), [{"n": 0}]),
            ScriptedChecker(_searcher_id()),
            store,
        )


def test_persisted_candidate_bytes_are_exactly_the_bytes_checked():
    assignment = {"b": 2, "a": 1}
    request = _request(assignments=[assignment], declared_total_points=1, limits=_limits(points=1))
    searcher = ScriptedSearcher(
        _searcher_id(),
        [assignment],
        violations=frozenset({0}),
    )
    checker = ScriptedChecker(_checker_id())
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        result = _run(request, searcher, checker, store)
        expected = canonical_json({"a": 1, "b": 2}).encode("utf-8")
        assert checker.seen == expected
        assert result.candidate_ref is not None
        assert store.get_bytes(result.candidate_ref.content_hash) == expected
        assert result.termination is ComputeTermination.CHECKED_COUNTEREXAMPLE
        assert result.checker_record is not None
        assert result.checker_record.candidate_content_hash == sha256_hex(expected)
        assert result.evidence is not None
        assert (
            result.evidence.evidence_kind
            is ComputeEvidenceKind.CHECKED_COUNTEREXAMPLE_CANDIDATE
        )
        report = json.loads(store.get_bytes(result.report_ref.content_hash))
        assert "evidence" not in report
        assert "evidence_id" not in report
        assert report["termination"] == "CHECKED_COUNTEREXAMPLE"

        tampered_candidate = result.to_dict()
        assert tampered_candidate["candidate_ref"] is not None
        tampered_candidate["candidate_ref"]["content_hash"] = _hex("swapped")
        _raises(ComputeError, type(result).from_dict, tampered_candidate)

        tampered_evidence = result.to_dict()
        assert tampered_evidence["evidence"] is not None
        tampered_evidence["evidence"]["request_id"] = _hex("other-request")
        _raises(ComputeError, type(result).from_dict, tampered_evidence)

        malformed_ref = result.to_dict()
        malformed_ref["report_ref"]["size_bytes"] = True
        _raises(ComputeError, type(result).from_dict, malformed_ref)


def test_limit_error_checker_matrix_and_coverage_invariants():
    points = [{"n": 0}, {"n": 1}, {"n": 2}]
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        runner = ComputeRunner()

        exhausted = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=3)),
            ScriptedSearcher(_searcher_id(), points),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert exhausted.termination is ComputeTermination.DOMAIN_EXHAUSTED
        assert exhausted.coverage.enumeration_complete is True
        assert exhausted.coverage.examined_points == 3
        assert exhausted.coverage.total_points == 3
        assert exhausted.checker_record is None
        assert exhausted.candidate_ref is None
        assert exhausted.evidence is not None
        assert (
            exhausted.evidence.evidence_kind
            is ComputeEvidenceKind.BOUNDED_EXHAUSTION_RECORD
        )
        assert exhausted.evidence.checker_record is None
        assert "UNSAT" not in exhausted.detail
        assert "UNSAT" not in exhausted.termination.value

        equal_cap = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=3)),
            ScriptedSearcher(_searcher_id(), points),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert equal_cap.termination is ComputeTermination.DOMAIN_EXHAUSTED

        point_cap = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=2)),
            ScriptedSearcher(_searcher_id(), points),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert point_cap.termination is ComputeTermination.POINT_LIMIT_REACHED
        assert point_cap.coverage.examined_points == 2
        assert point_cap.coverage.enumeration_complete is False
        assert point_cap.evidence is None

        timed = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=None, wall=0)),
            ScriptedSearcher(_searcher_id(), points),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert timed.termination is ComputeTermination.TIME_LIMIT_REACHED
        assert timed.coverage.examined_points == 0
        assert timed.coverage.enumeration_complete is False
        assert timed.evidence is None

        mismatch = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=8)),
            ScriptedSearcher(_searcher_id(), points, total=9),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert mismatch.termination is ComputeTermination.ERROR
        assert mismatch.evidence is None
        assert mismatch.coverage.enumeration_complete is False

        boom = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=8)),
            ScriptedSearcher(_searcher_id(), points, fail="assign"),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert boom.termination is ComputeTermination.ERROR
        assert boom.evidence is None

        bool_assign = runner.run(
            _request(assignments=points, declared_total_points=3, limits=_limits(points=8)),
            ScriptedSearcher(_searcher_id(), points, bad_assign={"n": True}),
            ScriptedChecker(_checker_id()),
            store,
        )
        assert bool_assign.termination is ComputeTermination.ERROR

        witness_req = _request(
            assignments=points, declared_total_points=3, limits=_limits(points=8)
        )
        found = runner.run(
            witness_req,
            ScriptedSearcher(_searcher_id(), points, violations=frozenset({1})),
            ScriptedChecker(_checker_id(), result=CheckerResult.PASS),
            store,
        )
        assert found.termination is ComputeTermination.CHECKED_COUNTEREXAMPLE
        assert found.coverage.examined_points == 2
        assert found.coverage.enumeration_complete is False
        assert found.checker_record.checker_result is CheckerResult.PASS

        rejected = runner.run(
            witness_req,
            ScriptedSearcher(_searcher_id(), points, violations=frozenset({0})),
            ScriptedChecker(_checker_id(), result=CheckerResult.FAIL, detail="nope"),
            store,
        )
        assert rejected.termination is ComputeTermination.CHECKER_REJECTED
        assert rejected.evidence is None
        assert rejected.checker_record.checker_result is CheckerResult.FAIL

        unknown = runner.run(
            witness_req,
            ScriptedSearcher(_searcher_id(), points, violations=frozenset({0})),
            ScriptedChecker(_checker_id(), result=CheckerResult.UNKNOWN, detail="?"),
            store,
        )
        assert unknown.termination is ComputeTermination.CHECKER_UNKNOWN
        assert unknown.evidence is None

        raised = runner.run(
            witness_req,
            ScriptedSearcher(_searcher_id(), points, violations=frozenset({0})),
            ScriptedChecker(_checker_id(), fail=True),
            store,
        )
        assert raised.termination is ComputeTermination.CHECKER_UNKNOWN
        assert raised.checker_record.checker_result is CheckerResult.UNKNOWN

        for result in (exhausted, point_cap, timed, mismatch, found, rejected, unknown, raised):
            restored = type(result).from_dict(result.to_dict())
            assert restored.termination is result.termination
            assert restored.coverage.examined_points == result.coverage.examined_points
            report = json.loads(store.get_bytes(result.report_ref.content_hash))
            assert report["request"]["request_id"] == request_id_of(result)
            assert "UNSAT" not in json.dumps(report)


def request_id_of(result) -> str:
    return result.request_id


def test_cas_fault_does_not_return_success():
    points = [{"n": 0}]
    request = _request(assignments=points, declared_total_points=1, limits=_limits(points=1))
    searcher = ScriptedSearcher(_searcher_id(), points, violations=frozenset({0}))
    checker = ScriptedChecker(_checker_id())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _raises(
            ComputeArtifactError,
            _run,
            request,
            searcher,
            checker,
            FaultyStore(root / "put", fail_on="put"),
        )
        _raises(
            ComputeArtifactError,
            _run,
            request,
            searcher,
            checker,
            FaultyStore(root / "get", fail_on="get"),
        )
        _raises(
            ComputeArtifactError,
            _run,
            request,
            searcher,
            checker,
            FaultyStore(root / "verify", fail_on="verify"),
        )
        _raises(
            ComputeArtifactError,
            _run,
            request,
            searcher,
            checker,
            FaultyStore(root / "report", fail_on="put_report"),
        )


def test_usage_is_observed_zero_for_provider_and_honest_about_timing():
    points = [{"n": 0}, {"n": 1}]
    request = _request(assignments=points, declared_total_points=2, limits=_limits(points=2))
    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp) / "cas")
        result = _run(
            request,
            ScriptedSearcher(_searcher_id(), points),
            ScriptedChecker(_checker_id()),
            store,
        )
        usage = result.usage
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            qty = getattr(usage, name)
            assert qty.status is ObservationStatus.OBSERVED
            assert qty.value == 0
            assert qty.unit == "tokens"
        assert usage.cost_microusd.status is ObservationStatus.OBSERVED
        assert usage.cost_microusd.value == 0
        assert usage.tool_gpu_ms.status is ObservationStatus.OBSERVED
        assert usage.tool_gpu_ms.value == 0
        assert usage.wall_ms.status is ObservationStatus.OBSERVED
        assert usage.tool_cpu_ms.status is ObservationStatus.OBSERVED
        assert isinstance(usage.wall_ms.value, int) and usage.wall_ms.value >= 0
        assert isinstance(usage.tool_cpu_ms.value, int) and usage.tool_cpu_ms.value >= 0
        report = json.loads(store.get_bytes(result.report_ref.content_hash))
        assert report["usage"]["wall_ms"]["value"] == usage.wall_ms.value
        # Timing boundary: usage is captured before report persist, so the
        # report can include usage without hashing a write that contains itself.
        assert result.started_at <= result.ended_at


def test_production_module_does_not_import_truth_or_providers():
    path = _ROOT / "compute.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    forbidden_modules = {
        "euler.gateway",
        "euler.verify",
        "euler.execution",
        "euler.core.factgraph",
        "euler.core.global_memory",
        "euler.control.views",
        "euler.control.event_store",
        "euler.control.compute_plugins",
        "euler.control.compute_plugins.polynomial_counterexample",
        "subprocess",
        "openai",
        "http.client",
        "urllib.request",
    }
    forbidden_names = {
        "FactGraph",
        "fact_submit",
        "GlobalMemory",
        "EvidenceArtifact",
        "ResearchView",
        "EventStore",
        "UNSAT",
        "Popen",
        "urlopen",
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
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "UNSAT":
                named.add("UNSAT")
    assert imported.isdisjoint(forbidden_modules), imported
    assert named.isdisjoint(forbidden_names), named & forbidden_names
    assert "UNSAT" not in source


def main() -> None:
    tests = [
        test_malformed_serialization_and_limit_rules,
        test_request_id_binds_semantics_and_excludes_created_at,
        test_identity_mismatch_and_distinct_components,
        test_persisted_candidate_bytes_are_exactly_the_bytes_checked,
        test_limit_error_checker_matrix_and_coverage_invariants,
        test_cas_fault_does_not_return_success,
        test_usage_is_observed_zero_for_provider_and_honest_about_timing,
        test_production_module_does_not_import_truth_or_providers,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL COMPUTE CONTRACT TESTS PASSED")


if __name__ == "__main__":
    main()
