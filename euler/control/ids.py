"""Canonical JSON encoding and content-addressed identifiers.

Stdlib only. IDs and hashes are full lowercase SHA-256; they are never truncated.
Stable IDs hash the canonical JSON array ``["<domain>:v<version>", *parts]``.
Event IDs delegate to ``stable_id`` and omit timestamp, so retries keep the same id.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, List, Mapping, Union

JSONValue = Union[None, bool, int, float, str, List["JSONValue"], Dict[str, "JSONValue"]]

_EVENT_DOMAIN = "event"
_EVENT_ID_VERSION = 1

__all__ = [
    "JSONValue",
    "canonical_json",
    "sha256_hex",
    "payload_hash",
    "stable_id",
    "event_id",
]


def canonical_json(value: object) -> str:
    """Return compact, key-sorted JSON that preserves Unicode.

    Rejects NaN, Infinity, and any value that is not a JSON-safe primitive,
    list, or string-keyed object. Encoding is stable across dict key order.
    """
    return json.dumps(
        _to_json_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_hex(value: str | bytes) -> str:
    """Return the full 64-char lowercase SHA-256 hex digest of ``value``."""
    if isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, bytes):
        data = value
    else:
        raise TypeError(f"sha256_hex requires str or bytes, got {type(value).__name__}")
    return hashlib.sha256(data).hexdigest()


def payload_hash(payload: Mapping[str, JSONValue]) -> str:
    """SHA-256 of the canonical JSON encoding of a JSON object payload."""
    if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
        raise TypeError("payload must be a JSON object")
    return sha256_hex(canonical_json(payload))


def stable_id(domain: str, version: int, *parts: str) -> str:
    """Content-addressed id with explicit domain/version separation.

    Material is the canonical JSON array ``["<domain>:v<version>", *parts]``.
    Raw string concatenation is forbidden because ``("ab", "c")`` and
    ``("a", "bc")`` would otherwise collide.
    """
    _require_nonempty_str("domain", domain)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"version must be a positive int, got {version!r}")
    for index, part in enumerate(parts):
        if not isinstance(part, str):
            raise TypeError(f"stable_id part {index} must be str, got {type(part).__name__}")
    return sha256_hex(canonical_json([f"{domain}:v{version}", *parts]))


def event_id(
    project_id: str,
    event_type: str,
    idempotency_key: str,
    payload_hash: str,
) -> str:
    """Stable event id; timestamp is intentionally excluded.

    ``event_id = stable_id("event", 1, project_id, event_type, idempotency_key,
    payload_hash)`` as frozen in the Phase 1 contract.
    """
    _require_nonempty_str("project_id", project_id)
    _require_nonempty_str("event_type", event_type)
    _require_nonempty_str("idempotency_key", idempotency_key)
    _require_nonempty_str("payload_hash", payload_hash)
    return stable_id(
        _EVENT_DOMAIN,
        _EVENT_ID_VERSION,
        project_id,
        event_type,
        idempotency_key,
        payload_hash,
    )


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{name} must be a nonempty str")
    return value


def _to_json_data(value: object, _stack: set[int] | None = None) -> JSONValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects NaN and Infinity")
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        sid = id(value)
        stack = _stack if _stack is not None else set()
        if sid in stack:
            raise ValueError("canonical JSON rejects cyclic values")
        stack.add(sid)
        try:
            return [_to_json_data(item, stack) for item in value]
        finally:
            stack.discard(sid)
    if isinstance(value, Mapping):
        sid = id(value)
        stack = _stack if _stack is not None else set()
        if sid in stack:
            raise ValueError("canonical JSON rejects cyclic values")
        stack.add(sid)
        try:
            out: Dict[str, JSONValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("canonical JSON object keys must be str")
                out[key] = _to_json_data(item, stack)
            return out
        finally:
            stack.discard(sid)
    raise ValueError(
        f"canonical JSON rejects non-JSON-safe value of type {type(value).__name__}"
    )
