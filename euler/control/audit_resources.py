"""Shared final-audit verifier environment identity and reservation policy.

Scheduler admission is not provider-side hard billing enforcement. Backend
usage may exceed the reserved vector. Unknown usage stays unknown and settles
to the reserved quantity. Credentials never enter the public environment hash.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from euler.control.ids import canonical_json, sha256_hex
from euler.control.policy import (
    RESOURCE_DIMENSIONS,
    BudgetPolicy,
    PolicyError,
    ResourceLimitKind,
    ResourceVector,
)
from euler.control.schema import SchemaValidationError

ENV_RESOURCES = "EULER_FINAL_AUDIT_RESOURCES"
ENV_VERIFY_URL = "EULER_VERIFY_URL"
ENV_VERIFY_TIMEOUT = "EULER_VERIFY_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 3600
FIXTURE_WALL_MS = 1
BILLED_TOKEN_DIMENSIONS = (
    "cost_microusd",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
)

__all__ = [
    "ENV_RESOURCES",
    "ENV_VERIFY_URL",
    "ENV_VERIFY_TIMEOUT",
    "DEFAULT_TIMEOUT_SECONDS",
    "FIXTURE_WALL_MS",
    "BILLED_TOKEN_DIMENSIONS",
    "AuditResourceError",
    "configured_timeout_seconds",
    "timeout_ms_from_seconds",
    "endpoint_identity",
    "is_live_http_transport",
    "verifier_environment_hash",
    "load_env_resource_vector",
    "fixture_allowance",
    "resolve_audit_reservation",
]


class AuditResourceError(ValueError):
    """Malformed audit resource configuration or missing live allowances."""


def configured_timeout_seconds(override: int | float | None = None) -> int | float:
    if override is not None:
        if isinstance(override, bool) or not isinstance(override, (int, float)) or override <= 0:
            raise AuditResourceError("timeout_seconds must be a positive number")
        return override
    raw = os.environ.get(ENV_VERIFY_TIMEOUT, str(DEFAULT_TIMEOUT_SECONDS))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return value


def timeout_ms_from_seconds(seconds: int | float) -> int:
    return max(1, int(round(float(seconds) * 1000)))


def endpoint_identity(url: str | None = None) -> str:
    """Hash scheme/host/port/path only. Userinfo and query are dropped."""
    text = (url if url is not None else os.environ.get(ENV_VERIFY_URL, "")).strip()
    if not text:
        return "unset"
    parsed = urlparse(text)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    effective = f"{parsed.scheme}://{host}{port}{path}"
    return sha256_hex(effective.encode("utf-8"))


def is_live_http_transport(transport: object | None) -> bool:
    if transport is None:
        return True
    return bool(getattr(transport, "live_http", False))


def verifier_environment_hash(
    *,
    policy_hash: str,
    timeout_seconds: int | float,
    transport: object | None = None,
) -> str:
    """Hash configured endpoint/protocol/policy/timeout. No credentials.

    Injected test transports are not part of the identity; production replay
    uses the same configured verifier environment.
    """
    _ = transport
    payload = {
        "protocol": "http-post-json-force-fresh",
        "endpoint_identity": endpoint_identity(),
        "timeout_seconds": float(timeout_seconds),
        "policy_hash": policy_hash,
        "verifier_configuration": {
            name: os.environ.get(name, "") for name in (
                "EULER_VERIFY_CONFIG_ID", "EULER_VERIFY_BACKEND",
                "EULER_VERIFY_MODEL", "EULER_VERIFY_EFFORT",
                "EULER_VERIFY_GROK_MODEL", "EULER_VERIFY_GROK_EFFORT",
                "EULER_VERIFY_CURSOR_MODEL",
            )
        },
    }
    return sha256_hex(canonical_json(payload).encode("utf-8"))


def load_env_resource_vector() -> ResourceVector | None:
    raw = os.environ.get(ENV_RESOURCES)
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AuditResourceError(
            "EULER_FINAL_AUDIT_RESOURCES must be a JSON object with ResourceVector fields"
        ) from exc
    if not isinstance(data, dict):
        raise AuditResourceError(
            "EULER_FINAL_AUDIT_RESOURCES must be a JSON object with ResourceVector fields"
        )
    try:
        return ResourceVector.from_dict(data)
    except (PolicyError, SchemaValidationError, TypeError, ValueError, KeyError) as exc:
        raise AuditResourceError(f"EULER_FINAL_AUDIT_RESOURCES invalid: {exc}") from exc


def fixture_allowance() -> ResourceVector:
    """Tiny offline allowance for explicitly injected test transports."""
    return ResourceVector(
        cost_microusd=1,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        cached_tokens=0,
        wall_ms=FIXTURE_WALL_MS,
        tool_cpu_ms=0,
        tool_gpu_ms=0,
    )


def resolve_audit_reservation(
    *,
    policy: BudgetPolicy,
    requested: ResourceVector | None,
    timeout_ms: int,
    live_http: bool,
) -> ResourceVector:
    """Build the FINAL_AUDIT reservation.

    Live HTTP under LIMITED billed/token dimensions needs a positive explicit
    allowance (``requested`` or ``EULER_FINAL_AUDIT_RESOURCES``). Unlimited
    dimensions may reserve zero. Live wall_ms always matches the HTTP timeout.
    Injected fixture transports may use the documented tiny allowance.
    """
    if requested is not None and not isinstance(requested, ResourceVector):
        raise AuditResourceError("requested must be a ResourceVector")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise AuditResourceError("timeout_ms must be a positive int")
    configured = requested if requested is not None else load_env_resource_vector()
    if live_http:
        base = configured if configured is not None else ResourceVector.zero()
        vector = ResourceVector(
            **{name: (timeout_ms if name == "wall_ms" else base.get(name))
               for name in RESOURCE_DIMENSIONS}
        )
        missing = [
            name
            for name in BILLED_TOKEN_DIMENSIONS
            if policy.dimension(name).kind is ResourceLimitKind.LIMITED
            and vector.get(name) <= 0
        ]
        if missing:
            raise AuditResourceError(
                "live HTTP final audit requires a positive requested or "
                "EULER_FINAL_AUDIT_RESOURCES allowance for limited billed/token "
                f"dimensions {missing}; scheduler admission is not provider-side "
                "hard billing enforcement"
            )
        return vector
    if configured is not None:
        return configured
    return fixture_allowance()
