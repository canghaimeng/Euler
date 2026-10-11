"""Unit tests for shared final-audit resource and environment helpers."""
from __future__ import annotations

import json

import pytest

from euler.control.audit_resources import (
    ENV_RESOURCES,
    AuditResourceError,
    endpoint_identity,
    fixture_allowance,
    load_env_resource_vector,
    resolve_audit_reservation,
    verifier_environment_hash,
)
from euler.control.policy import BudgetPolicy, DimensionLimit, ResourceVector


def _policy(*, cost=None) -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    return BudgetPolicy(
        cost_microusd=unbound if cost is None else DimensionLimit.limited(cost),
        input_tokens=unbound,
        output_tokens=unbound,
        reasoning_tokens=unbound,
        cached_tokens=unbound,
        wall_ms=unbound,
        tool_cpu_ms=unbound,
        tool_gpu_ms=unbound,
        max_reasoning_slots=1,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
        final_audit_reserve_bp=1000,
    )


def test_endpoint_identity_drops_credentials_and_query():
    hashed = endpoint_identity("https://user:secret@verify.example:8443/v1?token=abc#frag")
    assert hashed != "unset"
    assert "secret" not in hashed
    assert "token" not in hashed
    assert hashed == endpoint_identity("https://verify.example:8443/v1")


def test_audit_identity_changes_with_configured_model_and_operator_revision(monkeypatch):
    before = verifier_environment_hash(policy_hash="test-policy", timeout_seconds=60)
    monkeypatch.setenv("EULER_VERIFY_GROK_MODEL", "model-revision-2")
    changed_model = verifier_environment_hash(policy_hash="test-policy", timeout_seconds=60)
    assert changed_model != before
    monkeypatch.setenv("EULER_VERIFY_CONFIG_ID", "deployment-revision-3")
    changed_deployment = verifier_environment_hash(policy_hash="test-policy", timeout_seconds=60)
    assert changed_deployment != changed_model


def test_resource_json_unit_validation(monkeypatch):
    monkeypatch.setenv(ENV_RESOURCES, "{not-json")
    with pytest.raises(AuditResourceError, match="JSON"):
        load_env_resource_vector()
    monkeypatch.setenv(ENV_RESOURCES, json.dumps({"cost_microusd": -1}))
    with pytest.raises(AuditResourceError):
        load_env_resource_vector()
    monkeypatch.setenv(
        ENV_RESOURCES,
        json.dumps({
            "cost_microusd": 2, "input_tokens": 0, "output_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "wall_ms": 1,
            "tool_cpu_ms": 0, "tool_gpu_ms": 0, "extra": 1,
        }),
    )
    with pytest.raises(AuditResourceError):
        load_env_resource_vector()


def test_live_limited_cost_requires_positive_allowance():
    policy = _policy(cost=10_000)
    with pytest.raises(AuditResourceError, match="cost_microusd"):
        resolve_audit_reservation(
            policy=policy, requested=None, timeout_ms=1000, live_http=True,
        )
    requested = ResourceVector(
        cost_microusd=5, input_tokens=0, output_tokens=0, reasoning_tokens=0,
        cached_tokens=0, wall_ms=1, tool_cpu_ms=0, tool_gpu_ms=0,
    )
    vector = resolve_audit_reservation(
        policy=policy, requested=requested, timeout_ms=1500, live_http=True,
    )
    assert vector.cost_microusd == 5
    assert vector.wall_ms == 1500


def test_fixture_tiny_allowance_and_unlimited_live_zero_cost():
    assert fixture_allowance().cost_microusd == 1
    assert fixture_allowance().wall_ms == 1
    vector = resolve_audit_reservation(
        policy=_policy(), requested=None, timeout_ms=4000, live_http=True,
    )
    assert vector.cost_microusd == 0
    assert vector.wall_ms == 4000
    fixture = resolve_audit_reservation(
        policy=_policy(cost=10_000), requested=None, timeout_ms=4000, live_http=False,
    )
    assert fixture == fixture_allowance()


def test_environment_hash_changes_with_endpoint(monkeypatch):
    monkeypatch.setenv("EULER_VERIFY_URL", "http://a.example/v1")
    first = verifier_environment_hash(policy_hash="a" * 64, timeout_seconds=1, transport=object())
    monkeypatch.setenv("EULER_VERIFY_URL", "http://b.example/v1")
    second = verifier_environment_hash(policy_hash="a" * 64, timeout_seconds=1, transport=object())
    assert first != second
