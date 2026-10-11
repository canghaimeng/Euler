"""Offline tests for research-control canonical IDs and frozen schema.

No network, no runtime projects, no provider CLIs. Covers hash stability,
tamper rejection, observed/unknown usage, independent attempt statuses, and
BudgetLimit LIMITED vs UNLIMITED.

Runs standalone (``python -m euler.control.tests.test_schema_ids``)
and under pytest.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path

from euler.control.ids import (
    canonical_json,
    event_id,
    payload_hash,
    sha256_hex,
    stable_id,
)
from euler.control.schema import (
    AttemptReceipt,
    BudgetLimit,
    BudgetLimitKind,
    BudgetReservation,
    EventEnvelope,
    FactCommitStatus,
    InfrastructureStatus,
    MathematicalStatus,
    ObservationStatus,
    ObservedQuantity,
    ReservationState,
    SchemaValidationError,
    UsageRecord,
)

_ROOT = Path(__file__).resolve().parents[1]


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} from {fn.__name__}")


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _unknown(unit: str) -> ObservedQuantity:
    return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)


def _observed(unit: str, value: int) -> ObservedQuantity:
    return ObservedQuantity(unit=unit, status=ObservationStatus.OBSERVED, value=value)


def _usage(**overrides: ObservedQuantity) -> UsageRecord:
    fields = {
        "input_tokens": _unknown("tokens"),
        "output_tokens": _unknown("tokens"),
        "reasoning_tokens": _unknown("tokens"),
        "cached_tokens": _unknown("tokens"),
        "cost_microusd": _unknown("microusd"),
        "wall_ms": _unknown("ms"),
        "tool_cpu_ms": _unknown("ms"),
        "tool_gpu_ms": _unknown("ms"),
    }
    fields.update(overrides)
    return UsageRecord(**fields)


def _roundtrip(obj):
    restored = type(obj).from_dict(obj.to_dict())
    assert restored.to_dict() == obj.to_dict()
    return restored


# --------------------------------------------------------------------------- #
# canonical JSON / hashes                                                     #
# --------------------------------------------------------------------------- #


def test_canonical_json_sorts_keys_and_is_compact():
    encoded = canonical_json({"b": 1, "a": {"z": 3, "m": 2}})
    assert encoded == '{"a":{"m":2,"z":3},"b":1}'
    assert " " not in encoded
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_preserves_unicode():
    encoded = canonical_json({"note": "命题"})
    assert "命题" in encoded
    assert "\\u" not in encoded
    assert json.loads(encoded)["note"] == "命题"


def test_canonical_json_rejects_nan_infinity_and_non_json():
    _raises(ValueError, canonical_json, float("nan"))
    _raises(ValueError, canonical_json, float("inf"))
    _raises(ValueError, canonical_json, float("-inf"))
    _raises(ValueError, canonical_json, math.nan)
    _raises(ValueError, canonical_json, b"bytes")
    _raises(ValueError, canonical_json, {1: "a"})
    _raises(ValueError, canonical_json, {"a": {None: 1}})
    _raises(ValueError, canonical_json, {"a": {1, 2}})
    _raises(ValueError, canonical_json, {"a": (1, 2)})
    _raises(ValueError, canonical_json, object())
    cyclic: list = []
    cyclic.append(cyclic)
    _raises(ValueError, canonical_json, cyclic)


def test_payload_hash_stable_and_sensitive():
    left = payload_hash({"b": 1, "a": [1, 2]})
    right = payload_hash({"a": [1, 2], "b": 1})
    assert left == right
    assert len(left) == 64 and left == left.lower()
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})
    assert payload_hash({"a": 1}) != payload_hash({"a": True})
    expected = hashlib.sha256(
        canonical_json({"a": 1, "b": 2}).encode("utf-8")
    ).hexdigest()
    assert payload_hash({"b": 2, "a": 1}) == expected
    _raises(TypeError, payload_hash, [1, 2])
    _raises(ValueError, payload_hash, {"x": float("nan")})


def test_sha256_hex_str_and_bytes():
    digest = sha256_hex("abc")
    assert digest == hashlib.sha256(b"abc").hexdigest()
    assert digest == sha256_hex(b"abc")
    assert len(digest) == 64 and digest == digest.lower()
    _raises(TypeError, sha256_hex, 1)


def test_stable_id_explicit_domain_version_separation():
    parts = ("proj", "attempt.started", "key-1")
    a = stable_id("event", 1, *parts)
    b = stable_id("event", 2, *parts)
    c = stable_id("budget", 1, *parts)
    assert a != b and a != c and b != c
    assert a == sha256_hex(canonical_json(["event:v1", *parts]))
    assert a != hashlib.sha256(("event:v1" + "".join(parts)).encode("utf-8")).hexdigest()
    assert len(a) == 64 and a == a.lower()
    _raises(ValueError, stable_id, "", 1, "p")
    _raises(ValueError, stable_id, "event", 0, "p")
    _raises(ValueError, stable_id, "event", True, "p")
    _raises(TypeError, stable_id, "event", 1, 2)


def test_stable_id_rejects_part_concatenation_ambiguity():
    left = stable_id("event", 1, "ab", "c")
    right = stable_id("event", 1, "a", "bc")
    assert left != right
    assert left == sha256_hex(canonical_json(["event:v1", "ab", "c"]))
    assert right == sha256_hex(canonical_json(["event:v1", "a", "bc"]))


def test_event_id_matches_frozen_formula_and_excludes_timestamp():
    digest = payload_hash({"n": 1})
    eid = event_id("proj", "attempt.started", "idem-1", digest)
    assert eid == stable_id("event", 1, "proj", "attempt.started", "idem-1", digest)
    assert eid == sha256_hex(
        canonical_json(["event:v1", "proj", "attempt.started", "idem-1", digest])
    )
    concat = "event:v1" + "proj" + "attempt.started" + "idem-1" + digest
    assert eid != hashlib.sha256(concat.encode("utf-8")).hexdigest()
    assert eid != event_id("proj", "attempt.started", "idem-2", digest)
    assert eid != event_id("proj", "attempt.started", "idem-1", payload_hash({"n": 2}))
    _raises(ValueError, event_id, "", "t", "k", digest)


# --------------------------------------------------------------------------- #
# ObservedQuantity / UsageRecord                                              #
# --------------------------------------------------------------------------- #


def test_unknown_usage_cannot_carry_zero_observed_zero_is_legal():
    unknown = ObservedQuantity(unit="tokens", status=ObservationStatus.UNKNOWN, value=None)
    assert unknown.value is None
    _roundtrip(unknown)
    _raises(
        SchemaValidationError,
        ObservedQuantity,
        unit="tokens",
        status=ObservationStatus.UNKNOWN,
        value=0,
    )
    _raises(
        SchemaValidationError,
        ObservedQuantity,
        unit="tokens",
        status=ObservationStatus.UNKNOWN,
        value=1,
    )
    zero = ObservedQuantity(unit="tokens", status=ObservationStatus.OBSERVED, value=0)
    assert zero.value == 0
    _roundtrip(zero)
    _raises(
        SchemaValidationError,
        ObservedQuantity,
        unit="tokens",
        status=ObservationStatus.OBSERVED,
        value=None,
    )
    _raises(
        SchemaValidationError,
        ObservedQuantity,
        unit="tokens",
        status=ObservationStatus.OBSERVED,
        value=-1,
    )
    _raises(
        SchemaValidationError,
        ObservedQuantity,
        unit="tokens",
        status=ObservationStatus.OBSERVED,
        value=True,
    )
    _raises(
        SchemaValidationError,
        ObservedQuantity,
        unit="tokens",
        status=ObservationStatus.OBSERVED,
        value=0.0,
    )
    _raises(
        SchemaValidationError,
        ObservedQuantity.from_dict,
        {"unit": "tokens", "status": "UNKNOWN", "value": 0},
    )
    _raises(
        SchemaValidationError,
        ObservedQuantity.from_dict,
        {"unit": "tokens", "status": "OBSERVED", "value": 1, "extra": True},
    )


def test_usage_record_roundtrip_and_load_validation():
    usage = _usage(
        input_tokens=_observed("tokens", 0),
        cost_microusd=_observed("microusd", 12),
    )
    restored = _roundtrip(usage)
    assert restored.input_tokens.value == 0
    assert restored.cost_microusd.value == 12
    assert restored.output_tokens.status is ObservationStatus.UNKNOWN
    broken = usage.to_dict()
    broken["wall_ms"]["value"] = 0
    broken["wall_ms"]["status"] = "UNKNOWN"
    _raises(SchemaValidationError, UsageRecord.from_dict, broken)
    wrong_unit = usage.to_dict()
    wrong_unit["wall_ms"] = _unknown("tokens").to_dict()
    _raises(SchemaValidationError, UsageRecord.from_dict, wrong_unit)


# --------------------------------------------------------------------------- #
# EventEnvelope                                                               #
# --------------------------------------------------------------------------- #


def _envelope(**overrides):
    kwargs = dict(
        project_id="proj",
        actor="worker-1",
        event_type="attempt.started",
        idempotency_key="idem-1",
        payload={"n": 1, "note": "命题"},
        timestamp_utc="2026-01-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return EventEnvelope.build(**kwargs)


def test_event_envelope_hash_recompute_and_retry_stability():
    first = _envelope(timestamp_utc="2026-01-01T00:00:00Z")
    retry = _envelope(timestamp_utc="2026-01-01T00:00:01.500Z")
    assert first.event_id == retry.event_id
    assert first.payload_hash == retry.payload_hash
    assert first.timestamp_utc != retry.timestamp_utc
    assert first.event_id == event_id(
        first.project_id, first.event_type, first.idempotency_key, first.payload_hash
    )
    assert first.payload_hash == payload_hash({"note": "命题", "n": 1})
    restored = _roundtrip(first)
    assert restored.event_id == first.event_id
    assert restored.payload["note"] == "命题"
    first.validate()


def test_event_envelope_tamper_rejection():
    env = _envelope()
    payload_tamper = env.to_dict()
    payload_tamper["payload"] = {"n": 2, "note": "命题"}
    _raises(SchemaValidationError, EventEnvelope.from_dict, payload_tamper)

    hash_tamper = env.to_dict()
    hash_tamper["payload_hash"] = "0" * 64
    _raises(SchemaValidationError, EventEnvelope.from_dict, hash_tamper)

    id_tamper = env.to_dict()
    id_tamper["event_id"] = "0" * 64
    _raises(SchemaValidationError, EventEnvelope.from_dict, id_tamper)

    case_tamper = env.to_dict()
    case_tamper["event_id"] = env.event_id.upper()
    _raises(SchemaValidationError, EventEnvelope.from_dict, case_tamper)

    type_tamper = env.to_dict()
    type_tamper["event_type"] = "attempt.finished"
    _raises(SchemaValidationError, EventEnvelope.from_dict, type_tamper)

    extra = env.to_dict()
    extra["unexpected"] = 1
    _raises(SchemaValidationError, EventEnvelope.from_dict, extra)

    version = env.to_dict()
    version["schema_version"] = 2
    _raises(SchemaValidationError, EventEnvelope.from_dict, version)

    nan_payload = env.to_dict()
    _raises(
        SchemaValidationError,
        EventEnvelope.from_dict,
        {**nan_payload, "payload": {"x": float("nan")}},
    )
    _raises(
        SchemaValidationError,
        EventEnvelope.build,
        project_id="../evil",
        actor="a",
        event_type="t",
        idempotency_key="k",
        payload={},
        timestamp_utc="2026-01-01T00:00:00Z",
    )
    _raises(
        SchemaValidationError,
        EventEnvelope.build,
        project_id="proj",
        actor="a",
        event_type="t",
        idempotency_key="k",
        payload={},
        timestamp_utc="2026-01-01T00:00:00",
    )
    _raises(
        SchemaValidationError,
        EventEnvelope.build,
        project_id="proj",
        actor="a",
        event_type="t",
        idempotency_key="k",
        payload={},
        timestamp_utc="2026-01-01T00:00:00+08:00",
    )


# --------------------------------------------------------------------------- #
# AttemptReceipt                                                              #
# --------------------------------------------------------------------------- #


def _receipt(**overrides):
    kwargs = dict(
        attempt_id=_hex("attempt"),
        project_id="proj",
        action_type="prove",
        model_id="grok-4.6",
        effort="high",
        started_at="2026-01-01T00:00:00Z",
        infrastructure_status=InfrastructureStatus.PENDING,
        mathematical_status=MathematicalStatus.NO_CLAIM,
        fact_commit_status=FactCommitStatus.NOT_ATTEMPTED,
        usage=_usage(),
    )
    kwargs.update(overrides)
    status = kwargs["infrastructure_status"]
    if (
        status
        in {
            InfrastructureStatus.SUCCEEDED,
            InfrastructureStatus.FAILED,
            InfrastructureStatus.TIMED_OUT,
            InfrastructureStatus.CANCELLED,
        }
        and "ended_at" not in overrides
    ):
        kwargs["ended_at"] = "2026-01-01T00:01:00Z"
    if kwargs.get("verdict") is not None and "verify_ref" not in overrides:
        kwargs["verify_ref"] = "verify-default"
    return AttemptReceipt(**kwargs)


def test_infra_and_mathematical_status_are_independent():
    failed_infra = _receipt(
        infrastructure_status=InfrastructureStatus.FAILED,
        mathematical_status=MathematicalStatus.NO_CLAIM,
    )
    assert failed_infra.infrastructure_status is InfrastructureStatus.FAILED
    assert failed_infra.mathematical_status is MathematicalStatus.NO_CLAIM
    cached_math = _receipt(
        infrastructure_status=InfrastructureStatus.TIMED_OUT,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.NOT_ATTEMPTED,
        verify_ref="run-1",
        verdict="correct",
    )
    assert cached_math.infrastructure_status is InfrastructureStatus.TIMED_OUT
    assert cached_math.mathematical_status is MathematicalStatus.CHECKED_CORRECT
    _roundtrip(failed_infra)
    _roundtrip(cached_math)
    _raises(
        SchemaValidationError,
        _receipt,
        infrastructure_status=InfrastructureStatus.RUNNING,
        ended_at="2026-01-01T00:01:00Z",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        infrastructure_status=InfrastructureStatus.FAILED,
        ended_at=None,
    )


def test_verdict_restricted_and_must_match_math_status():
    correct = _receipt(
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        verdict="correct",
    )
    wrong = _receipt(
        mathematical_status=MathematicalStatus.CHECKED_WRONG,
        verdict="wrong",
        fact_commit_status=FactCommitStatus.NOT_APPLICABLE,
    )
    _roundtrip(correct)
    _roundtrip(wrong)
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        verdict="maybe",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_WRONG,
        verdict="correct",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        verdict="wrong",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.NO_CLAIM,
        verdict="correct",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        verdict="correct",
        verify_ref=None,
    )


def test_correct_verdict_failed_fact_commit_is_not_truth():
    receipt = _receipt(
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.FAILED,
        verify_ref="verify-run-9",
        verdict="correct",
        fact_id=None,
        write_error="fact store write failed after correct verdict",
        ended_at="2026-01-01T00:01:00Z",
        artifact_refs=(_hex("artifact"),),
    )
    assert receipt.verdict == "correct"
    assert receipt.mathematical_status is MathematicalStatus.CHECKED_CORRECT
    assert receipt.fact_commit_status is FactCommitStatus.FAILED
    assert receipt.fact_commit_status is not FactCommitStatus.COMMITTED
    restored = _roundtrip(receipt)
    assert restored.write_error is not None
    assert restored.fact_id is None
    committed = _receipt(
        infrastructure_status=InfrastructureStatus.SUCCEEDED,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.COMMITTED,
        verdict="correct",
        fact_id="3b0c70d292e73ebe",
        write_error=None,
    )
    _roundtrip(committed)
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.COMMITTED,
        verdict="correct",
        fact_id=None,
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.COMMITTED,
        verdict="correct",
        fact_id="3b0c70d292e73ebe",
        write_error="should not be set",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.FAILED,
        verdict="correct",
        write_error="disk full",
        fact_id="3b0c70d292e73ebe",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_CORRECT,
        fact_commit_status=FactCommitStatus.FAILED,
        verdict="correct",
        write_error=None,
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.CHECKED_WRONG,
        fact_commit_status=FactCommitStatus.FAILED,
        verdict="wrong",
        write_error="disk full",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        mathematical_status=MathematicalStatus.NO_CLAIM,
        fact_commit_status=FactCommitStatus.NOT_ATTEMPTED,
        write_error="stray error",
    )
    _raises(
        SchemaValidationError,
        _receipt,
        fact_commit_status=FactCommitStatus.NOT_ATTEMPTED,
        fact_id="3b0c70d292e73ebe",
    )


def test_artifact_refs_are_hashes_not_files():
    digest = _hex("blob")
    receipt = _receipt(artifact_refs=(digest,))
    assert receipt.artifact_refs == (digest,)
    assert not Path(digest).exists()
    _roundtrip(receipt)
    _raises(
        SchemaValidationError,
        AttemptReceipt,
        attempt_id=_hex("attempt"),
        project_id="proj",
        action_type="prove",
        model_id="m",
        effort="high",
        started_at="2026-01-01T00:00:00Z",
        infrastructure_status=InfrastructureStatus.PENDING,
        mathematical_status=MathematicalStatus.NO_CLAIM,
        fact_commit_status=FactCommitStatus.NOT_ATTEMPTED,
        usage=_usage(),
        artifact_refs=("not-a-hash",),
    )
    data = receipt.to_dict()
    data["artifact_refs"] = digest
    _raises(SchemaValidationError, AttemptReceipt.from_dict, data)


# --------------------------------------------------------------------------- #
# BudgetLimit / BudgetReservation                                             #
# --------------------------------------------------------------------------- #


def test_budget_limit_distinguishes_limited_and_unlimited():
    limited = BudgetLimit(kind=BudgetLimitKind.LIMITED, value=0)
    unlimited = BudgetLimit(kind=BudgetLimitKind.UNLIMITED, value=None)
    assert limited.value == 0
    assert unlimited.value is None
    assert limited.kind is not unlimited.kind
    assert limited.to_dict()["kind"] == "LIMITED"
    assert unlimited.to_dict() == {"kind": "UNLIMITED", "value": None}
    _roundtrip(limited)
    _roundtrip(unlimited)
    _raises(
        SchemaValidationError,
        BudgetLimit,
        kind=BudgetLimitKind.LIMITED,
        value=None,
    )
    _raises(
        SchemaValidationError,
        BudgetLimit,
        kind=BudgetLimitKind.UNLIMITED,
        value=0,
    )
    _raises(
        SchemaValidationError,
        BudgetLimit,
        kind=BudgetLimitKind.LIMITED,
        value=-1,
    )
    _raises(
        SchemaValidationError,
        BudgetLimit.from_dict,
        {"kind": "LIMITED"},
    )
    restored_unlimited = BudgetLimit.from_dict({"kind": "UNLIMITED"})
    assert restored_unlimited.kind is BudgetLimitKind.UNLIMITED
    assert restored_unlimited.value is None


def _reservation(**overrides):
    kwargs = dict(
        reservation_id=_hex("res"),
        project_id="proj",
        attempt_id=_hex("attempt"),
        idempotency_key="idem-1",
        reserved_cost_microusd=1000,
        reserved_input_tokens=10,
        reserved_output_tokens=0,
        state=ReservationState.RESERVED,
        created_at="2026-01-01T00:00:00Z",
    )
    kwargs.update(overrides)
    return BudgetReservation(**kwargs)


def test_budget_reservation_roundtrip_and_state_rules():
    reserved = _reservation()
    _roundtrip(reserved)
    settled = _reservation(
        state=ReservationState.SETTLED,
        settled_at="2026-01-01T00:00:05Z",
        actual_usage=_usage(cost_microusd=_observed("microusd", 900)),
    )
    restored = _roundtrip(settled)
    assert restored.actual_usage is not None
    assert restored.actual_usage.cost_microusd.value == 900
    released = _reservation(
        state=ReservationState.RELEASED,
        settled_at="2026-01-01T00:00:05Z",
    )
    expired = _reservation(
        state=ReservationState.EXPIRED,
        settled_at="2026-01-01T00:00:05Z",
    )
    _roundtrip(released)
    _roundtrip(expired)
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.SETTLED,
        settled_at="2026-01-01T00:00:05Z",
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.SETTLED,
        actual_usage=_usage(),
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.RESERVED,
        settled_at="2026-01-01T00:00:05Z",
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.RESERVED,
        actual_usage=_usage(),
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.RELEASED,
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.EXPIRED,
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.RELEASED,
        settled_at="2026-01-01T00:00:05Z",
        actual_usage=_usage(),
    )
    _raises(
        SchemaValidationError,
        _reservation,
        state=ReservationState.EXPIRED,
        settled_at="2026-01-01T00:00:05Z",
        actual_usage=_usage(),
    )
    _raises(
        SchemaValidationError,
        _reservation,
        reserved_cost_microusd=-1,
    )


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
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    for filename in ("ids.py", "schema.py"):
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
        assert imported.isdisjoint(forbidden_modules), filename
        assert named.isdisjoint(forbidden_names), filename
        assert "fact_graph/" not in source
        assert "global_memory/" not in source


def main() -> None:
    tests = [
        test_canonical_json_sorts_keys_and_is_compact,
        test_canonical_json_preserves_unicode,
        test_canonical_json_rejects_nan_infinity_and_non_json,
        test_payload_hash_stable_and_sensitive,
        test_sha256_hex_str_and_bytes,
        test_stable_id_explicit_domain_version_separation,
        test_stable_id_rejects_part_concatenation_ambiguity,
        test_event_id_matches_frozen_formula_and_excludes_timestamp,
        test_unknown_usage_cannot_carry_zero_observed_zero_is_legal,
        test_usage_record_roundtrip_and_load_validation,
        test_event_envelope_hash_recompute_and_retry_stability,
        test_event_envelope_tamper_rejection,
        test_infra_and_mathematical_status_are_independent,
        test_verdict_restricted_and_must_match_math_status,
        test_correct_verdict_failed_fact_commit_is_not_truth,
        test_artifact_refs_are_hashes_not_files,
        test_budget_limit_distinguishes_limited_and_unlimited,
        test_budget_reservation_roundtrip_and_state_rules,
        test_production_modules_do_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL SCHEMA/ID TESTS PASSED")


if __name__ == "__main__":
    main()
