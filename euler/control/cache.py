"""Semantic ContextCacheKey, validation, and invalidation reasons.

Stdlib only. The key is a frozen digest of compiler inputs; this module never
writes a cache, calls a model, or touches truth stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from euler.control.contracts import MathematicalScope
from euler.control.ids import canonical_json, sha256_hex, stable_id

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHE_DOMAIN = "context-cache"
_CACHE_VERSION = 1
_COMPILER_SCHEMA_VERSION = 1
_SELECTION_POLICY_VERSION = 1

_SCOPE_KEYS = frozenset(
    {
        "assumptions",
        "quantifiers",
        "objects",
        "parameter_domain",
        "definitions_hash",
    }
)
_FACT_KEYS = frozenset(
    {"fact_id", "content_hash", "content_sha256", "predecessors", "content"}
)
_EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "artifact_id",
        "content_hash",
        "environment_hash",
        "checker_id",
        "checker_version",
        "checker_result",
        "content",
    }
)
_LIMIT_KEYS = frozenset(
    {"max_chars", "max_bytes", "max_failures", "max_relations"}
)
_REQUIRED_LIMITS = frozenset({"max_chars", "max_bytes"})
_LIMIT_ENTRY_KEYS = frozenset({"kind", "value"})
_LIMITED = "LIMITED"
_UNLIMITED = "UNLIMITED"

__all__ = [
    "ContextCacheError",
    "ContextInvalidationReason",
    "ContextCacheKey",
    "build_context_cache_key",
    "compare_context_keys",
]


class ContextCacheError(ValueError):
    """Malformed cache-key input or an inconsistent ContextCacheKey."""


class ContextInvalidationReason(str, Enum):
    CONTRACT = "CONTRACT"
    OBLIGATION = "OBLIGATION"
    ROUTE = "ROUTE"
    ACTION = "ACTION"
    DEFINITIONS = "DEFINITIONS"
    FACTS = "FACTS"
    EVIDENCE = "EVIDENCE"
    FAILURES = "FAILURES"
    RELATIONS = "RELATIONS"
    TOOL = "TOOL"
    ACCEPTANCE = "ACCEPTANCE"
    REFERENCE_VERSIONS = "REFERENCE_VERSIONS"
    OUTPUT_CONTRACT = "OUTPUT_CONTRACT"
    LIMITS = "LIMITS"
    COMPILER_VERSION = "COMPILER_VERSION"
    SELECTION_POLICY = "SELECTION_POLICY"
    CONCRETE_GAP = "CONCRETE_GAP"
    PACKET_POLICY = "PACKET_POLICY"


_COMPONENT_REASONS: tuple[tuple[str, ContextInvalidationReason], ...] = (
    ("contract_hash", ContextInvalidationReason.CONTRACT),
    ("obligation_hash", ContextInvalidationReason.OBLIGATION),
    ("route_hash", ContextInvalidationReason.ROUTE),
    ("action_hash", ContextInvalidationReason.ACTION),
    ("definitions_hash", ContextInvalidationReason.DEFINITIONS),
    ("facts_hash", ContextInvalidationReason.FACTS),
    ("evidence_hash", ContextInvalidationReason.EVIDENCE),
    ("failures_hash", ContextInvalidationReason.FAILURES),
    ("relations_hash", ContextInvalidationReason.RELATIONS),
    ("tool_hash", ContextInvalidationReason.TOOL),
    ("acceptance_hash", ContextInvalidationReason.ACCEPTANCE),
    ("reference_versions_hash", ContextInvalidationReason.REFERENCE_VERSIONS),
    ("output_contract_hash", ContextInvalidationReason.OUTPUT_CONTRACT),
    ("concrete_gap_hash", ContextInvalidationReason.CONCRETE_GAP),
    ("packet_policy_hash", ContextInvalidationReason.PACKET_POLICY),
    ("limits_hash", ContextInvalidationReason.LIMITS),
    ("compiler_version_hash", ContextInvalidationReason.COMPILER_VERSION),
    ("selection_policy_hash", ContextInvalidationReason.SELECTION_POLICY),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCacheKey:
    """Frozen component digests plus one stable key_id. All hashes are 64-hex."""

    key_id: str
    contract_hash: str
    obligation_hash: str
    route_hash: str
    action_hash: str
    definitions_hash: str
    facts_hash: str
    evidence_hash: str
    failures_hash: str
    relations_hash: str
    tool_hash: str
    acceptance_hash: str
    reference_versions_hash: str
    output_contract_hash: str
    concrete_gap_hash: str
    packet_policy_hash: str
    limits_hash: str
    compiler_version_hash: str
    selection_policy_hash: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name in _component_names():
            _require_hex64(name, getattr(self, name))
        _require_hex64("key_id", self.key_id)
        expected = _compose_key_id(self)
        if self.key_id != expected:
            raise ContextCacheError("key_id does not match component hashes")

    def to_dict(self) -> dict[str, str]:
        payload = {"key_id": self.key_id}
        for name in _component_names():
            payload[name] = getattr(self, name)
        return payload

    @classmethod
    def from_dict(cls, data: object) -> "ContextCacheKey":
        if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
            raise ContextCacheError("ContextCacheKey must be a mapping")
        payload = {str(key): data[key] for key in data}
        allowed = frozenset(("key_id", *_component_names()))
        _reject_extra(payload, allowed, "ContextCacheKey")
        _require_keys(payload, allowed, "ContextCacheKey")
        return cls(
            key_id=payload["key_id"],
            **{name: payload[name] for name in _component_names()},
        )


def build_context_cache_key(
    *,
    contract_id: str,
    obligation_id: str,
    action_id: str,
    definitions: object,
    tool_environment_hash: str,
    acceptance_policy_hash: str,
    reference_versions_hash: str,
    output_contract: object,
    limits: object,
    concrete_gap: str,
    packet_policy: object = None,
    expand_evidence_content: object = None,
    expand_proof_content: object = None,
    facts: object = (),
    evidence: object = (),
    failure_ids: object = (),
    relation_ids: object = (),
    route_id: str | None = None,
    compiler_version: object = _COMPILER_SCHEMA_VERSION,
    selection_policy_version: object = _SELECTION_POLICY_VERSION,
) -> ContextCacheKey:
    """Hash semantic compiler inputs into a frozen ContextCacheKey.

    Sequences keep caller order. Mappings are sorted by key because mapping
    insertion order is not semantic. Predecessor lists stay in given order.
    """
    contract_id = _require_hex64("contract_id", contract_id)
    obligation_id = _require_hex64("obligation_id", obligation_id)
    action_id = _require_hex64("action_id", action_id)
    if route_id is not None:
        route_id = _require_hex64("route_id", route_id)
    tool_environment_hash = _require_hex64(
        "tool_environment_hash", tool_environment_hash
    )
    acceptance_policy_hash = _require_hex64(
        "acceptance_policy_hash", acceptance_policy_hash
    )
    reference_versions_hash = _require_hex64(
        "reference_versions_hash", reference_versions_hash
    )
    compiler_version = _require_positive_int("compiler_version", compiler_version)
    selection_policy_version = _require_positive_int(
        "selection_policy_version", selection_policy_version
    )
    concrete_gap = _require_nonempty_str("concrete_gap", concrete_gap)
    policy_payload = _packet_policy_payload(
        packet_policy, expand_evidence_content, expand_proof_content
    )
    facts_payload = _facts_payload(facts)
    evidence_payload = _evidence_payload(evidence)
    failure_payload = _id_list_payload("failure_ids", failure_ids, hex_ids=True)
    relation_payload = _id_list_payload("relation_ids", relation_ids, hex_ids=True)
    scope_payload = _definitions_payload(definitions)
    output_payload = _output_contract_payload(output_contract)
    limits_payload = _limits_payload(limits)
    components = {
        "contract_hash": _component_hash("contract", contract_id),
        "obligation_hash": _component_hash("obligation", obligation_id),
        "route_hash": _component_hash("route", "" if route_id is None else route_id),
        "action_hash": _component_hash("action", action_id),
        "definitions_hash": _component_hash("definitions", canonical_json(scope_payload)),
        "facts_hash": _component_hash("facts", canonical_json(facts_payload)),
        "evidence_hash": _component_hash("evidence", canonical_json(evidence_payload)),
        "failures_hash": _component_hash("failures", canonical_json(failure_payload)),
        "relations_hash": _component_hash("relations", canonical_json(relation_payload)),
        "tool_hash": _component_hash("tool", tool_environment_hash),
        "acceptance_hash": _component_hash("acceptance", acceptance_policy_hash),
        "reference_versions_hash": _component_hash(
            "reference-versions", reference_versions_hash
        ),
        "output_contract_hash": _component_hash(
            "output-contract", canonical_json(output_payload)
        ),
        "concrete_gap_hash": _component_hash("concrete-gap", concrete_gap),
        "packet_policy_hash": _component_hash(
            "packet-policy", canonical_json(policy_payload)
        ),
        "limits_hash": _component_hash("limits", canonical_json(limits_payload)),
        "compiler_version_hash": _component_hash(
            "compiler-version", canonical_json(compiler_version)
        ),
        "selection_policy_hash": _component_hash(
            "selection-policy", canonical_json(selection_policy_version)
        ),
    }
    key_id = stable_id(
        _CACHE_DOMAIN,
        _CACHE_VERSION,
        "key",
        *[components[name] for name in _component_names()],
    )
    return ContextCacheKey(key_id=key_id, **components)


def compare_context_keys(
    old: object, new: object
) -> tuple[ContextInvalidationReason, ...]:
    """Return one reason per changed component. Identical keys yield ``()``."""
    if not isinstance(old, ContextCacheKey) or not isinstance(new, ContextCacheKey):
        raise ContextCacheError("compare_context_keys requires ContextCacheKey values")
    old.validate()
    new.validate()
    reasons: list[ContextInvalidationReason] = []
    for field, reason in _COMPONENT_REASONS:
        if getattr(old, field) != getattr(new, field):
            reasons.append(reason)
    if not reasons and old.key_id != new.key_id:
        raise ContextCacheError("identical components with distinct key_id")
    if reasons and old.key_id == new.key_id:
        raise ContextCacheError("changed components with identical key_id")
    return tuple(reasons)


def _component_names() -> tuple[str, ...]:
    return tuple(name for name, _reason in _COMPONENT_REASONS)


def _compose_key_id(key: ContextCacheKey) -> str:
    parts = [getattr(key, name) for name in _component_names()]
    return stable_id(_CACHE_DOMAIN, _CACHE_VERSION, "key", *parts)


def _component_hash(component: str, payload: str) -> str:
    return stable_id(_CACHE_DOMAIN, _CACHE_VERSION, component, payload)


def _definitions_payload(definitions: object) -> dict[str, Any]:
    if isinstance(definitions, MathematicalScope):
        payload = definitions.to_dict()
    elif isinstance(definitions, Mapping) and not isinstance(definitions, (str, bytes)):
        payload = {str(key): definitions[key] for key in definitions}
        _reject_extra(payload, _SCOPE_KEYS, "definitions")
        _require_keys(payload, _SCOPE_KEYS, "definitions")
        payload = {
            "assumptions": _text_list("definitions.assumptions", payload["assumptions"]),
            "quantifiers": _json_list("definitions.quantifiers", payload["quantifiers"]),
            "objects": _json_list("definitions.objects", payload["objects"]),
            "parameter_domain": _require_nonempty_str(
                "definitions.parameter_domain", payload["parameter_domain"]
            ),
            "definitions_hash": _require_hex64(
                "definitions.definitions_hash", payload["definitions_hash"]
            ),
        }
    else:
        raise ContextCacheError("definitions must be a MathematicalScope or mapping")
    _require_hex64("definitions.definitions_hash", payload["definitions_hash"])
    return payload


def _facts_payload(facts: object) -> list[dict[str, Any]]:
    records = _ordered_records("facts", facts, id_attr="fact_id")
    payload: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        prefix = f"facts[{index}]"
        mapping = _mapping_record(record, prefix, _FACT_KEYS)
        fact_id = _require_nonempty_str(f"{prefix}.fact_id", mapping.get("fact_id"))
        content_hash = mapping.get("content_hash")
        content_sha = mapping.get("content_sha256")
        if content_hash is not None and content_sha is not None and content_hash != content_sha:
            raise ContextCacheError(f"{prefix} content_hash does not match content_sha256")
        content_hash = _require_hex64(
            f"{prefix}.content_hash",
            content_hash if content_hash is not None else content_sha,
        )
        _verify_optional_content(f"{prefix}.content", mapping.get("content"), content_hash)
        predecessors = _predecessor_list(f"{prefix}.predecessors", mapping.get("predecessors", ()))
        payload.append(
            {
                "content_hash": content_hash,
                "fact_id": fact_id,
                "predecessors": predecessors,
            }
        )
    return payload


def _evidence_payload(evidence: object) -> list[dict[str, Any]]:
    records = _ordered_records("evidence", evidence, id_attr="evidence_id")
    payload: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        prefix = f"evidence[{index}]"
        mapping = _mapping_record(record, prefix, _EVIDENCE_KEYS)
        evidence_id = mapping.get("evidence_id", mapping.get("artifact_id"))
        evidence_id = _require_hex64(f"{prefix}.evidence_id", evidence_id)
        content_hash = _require_hex64(f"{prefix}.content_hash", mapping.get("content_hash"))
        environment_hash = _require_hex64(
            f"{prefix}.environment_hash", mapping.get("environment_hash")
        )
        _verify_optional_content(f"{prefix}.content", mapping.get("content"), content_hash)
        checker = _checker_payload(prefix, mapping)
        item = {
            "checker_id": checker["checker_id"],
            "checker_result": checker["checker_result"],
            "checker_version": checker["checker_version"],
            "content_hash": content_hash,
            "environment_hash": environment_hash,
            "evidence_id": evidence_id,
        }
        payload.append(item)
    return payload


def _checker_payload(prefix: str, mapping: Mapping[str, Any]) -> dict[str, Any]:
    checker_id = mapping.get("checker_id")
    checker_version = mapping.get("checker_version")
    checker_result = mapping.get("checker_result")
    present = [item is not None for item in (checker_id, checker_version, checker_result)]
    if any(present) and not all(present):
        raise ContextCacheError(
            f"{prefix} checker_id, checker_version, and checker_result must all be set"
        )
    if not any(present):
        return {
            "checker_id": None,
            "checker_result": None,
            "checker_version": None,
        }
    result = checker_result
    if hasattr(result, "value"):
        result = result.value
    return {
        "checker_id": _require_nonempty_str(f"{prefix}.checker_id", checker_id),
        "checker_result": _require_nonempty_str(f"{prefix}.checker_result", result),
        "checker_version": _require_nonempty_str(
            f"{prefix}.checker_version", checker_version
        ),
    }


def _id_list_payload(name: str, value: object, *, hex_ids: bool) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        ids: list[str] = []
        for index, key in enumerate(sorted(value, key=lambda item: str(item))):
            text = _require_nonempty_str(f"{name}[{index}]", key)
            ids.append(_require_hex64(f"{name}[{index}]", text) if hex_ids else text)
        return ids
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContextCacheError(f"{name} must be a sequence or mapping of ids")
    ids = []
    for index, item in enumerate(value):
        text = _require_nonempty_str(f"{name}[{index}]", item)
        ids.append(_require_hex64(f"{name}[{index}]", text) if hex_ids else text)
    return ids


def _packet_policy_payload(
    packet_policy: object,
    expand_evidence_content: object,
    expand_proof_content: object = None,
) -> dict[str, Any]:
    if packet_policy is None:
        payload: dict[str, Any] = {}
    elif isinstance(packet_policy, Mapping) and not isinstance(packet_policy, (str, bytes)):
        payload = {str(key): packet_policy[key] for key in packet_policy}
        try:
            canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise ContextCacheError("packet_policy is not JSON-safe") from exc
    else:
        raise ContextCacheError("packet_policy must be a mapping")
    expansion = expand_evidence_content
    if "expand_evidence_content" in payload:
        nested = payload["expand_evidence_content"]
        if not isinstance(nested, bool):
            raise ContextCacheError("packet_policy.expand_evidence_content must be a bool")
        if expansion is None:
            expansion = nested
        elif not isinstance(expansion, bool) or expansion is not nested:
            raise ContextCacheError(
                "expand_evidence_content does not match packet_policy"
            )
    if not isinstance(expansion, bool):
        raise ContextCacheError("expand_evidence_content must be a bool")
    payload["expand_evidence_content"] = expansion
    proof_expansion = expand_proof_content
    if "expand_proof_content" in payload:
        nested_proof = payload["expand_proof_content"]
        if not isinstance(nested_proof, bool):
            raise ContextCacheError("packet_policy.expand_proof_content must be a bool")
        if proof_expansion is None:
            proof_expansion = nested_proof
        elif not isinstance(proof_expansion, bool) or proof_expansion is not nested_proof:
            raise ContextCacheError("expand_proof_content does not match packet_policy")
    if proof_expansion is None:
        proof_expansion = False
    if not isinstance(proof_expansion, bool):
        raise ContextCacheError("expand_proof_content must be a bool")
    payload["expand_proof_content"] = proof_expansion
    return payload


def _output_contract_payload(output_contract: object) -> object:
    if isinstance(output_contract, str):
        if output_contract == "":
            raise ContextCacheError("output_contract must be a nonempty str or mapping")
        return output_contract
    if isinstance(output_contract, Mapping) and not isinstance(output_contract, (str, bytes)):
        try:
            canonical_json(dict(output_contract))
        except (TypeError, ValueError) as exc:
            raise ContextCacheError("output_contract is not JSON-safe") from exc
        return dict(output_contract)
    raise ContextCacheError("output_contract must be a nonempty str or mapping")


def _limits_payload(limits: object) -> dict[str, Any]:
    if not isinstance(limits, Mapping) or isinstance(limits, (str, bytes)):
        raise ContextCacheError("limits must be a mapping")
    payload = {str(key): limits[key] for key in limits}
    _reject_extra(payload, _LIMIT_KEYS, "limits")
    _require_keys(payload, _REQUIRED_LIMITS, "limits")
    normalized: dict[str, Any] = {}
    for name in sorted(_LIMIT_KEYS):
        if name not in payload:
            continue
        normalized[name] = _limit_entry(name, payload[name])
    return normalized


def _limit_entry(name: str, value: object) -> dict[str, Any]:
    if hasattr(value, "kind") and hasattr(value, "value") and not isinstance(value, Mapping):
        kind = getattr(getattr(value, "kind"), "value", getattr(value, "kind"))
        raw_value = getattr(value, "value")
        entry = {"kind": kind, "value": raw_value}
    elif isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        entry = {str(key): value[key] for key in value}
        _reject_extra(entry, _LIMIT_ENTRY_KEYS, f"limits.{name}")
        _require_keys(entry, {"kind"}, f"limits.{name}")
    else:
        raise ContextCacheError(f"limits.{name} must be a mapping with kind/value")
    kind = entry.get("kind")
    kind = getattr(kind, "value", kind)
    kind = _require_nonempty_str(f"limits.{name}.kind", kind)
    if kind == _UNLIMITED:
        if entry.get("value", None) is not None:
            raise ContextCacheError(f"limits.{name} UNLIMITED requires value=None")
        return {"kind": _UNLIMITED, "value": None}
    if kind != _LIMITED:
        raise ContextCacheError(f"limits.{name}.kind must be LIMITED or UNLIMITED")
    amount = _require_nonneg_int(f"limits.{name}.value", entry.get("value"))
    return {"kind": _LIMITED, "value": amount}


def _ordered_records(name: str, value: object, *, id_attr: str) -> list[object]:
    if value is None:
        return []
    if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        ordered = []
        for key in sorted(value, key=lambda item: str(item)):
            item = value[key]
            if isinstance(item, Mapping):
                merged = dict(item)
                merged.setdefault(id_attr, key)
                if id_attr == "evidence_id":
                    merged.setdefault("artifact_id", key)
                ordered.append(merged)
            else:
                ordered.append(item)
        return ordered
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContextCacheError(f"{name} must be a sequence or mapping")
    return list(value)


def _mapping_record(
    record: object, prefix: str, allowed: frozenset[str]
) -> dict[str, Any]:
    if isinstance(record, Mapping) and not isinstance(record, (str, bytes)):
        mapping = {str(key): record[key] for key in record}
        _reject_extra(mapping, allowed, prefix)
        return mapping
    if record is None or isinstance(record, (str, bytes, int, float, bool)):
        raise ContextCacheError(f"{prefix} must be a mapping or record object")
    mapping = {}
    for key in allowed:
        if hasattr(record, key):
            mapping[key] = getattr(record, key)
    if not mapping:
        raise ContextCacheError(f"{prefix} must be a mapping or record object")
    return mapping


def _predecessor_list(name: str, value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContextCacheError(f"{name} must be a sequence of ids")
    return [_require_nonempty_str(f"{name}[{index}]", item) for index, item in enumerate(value)]


def _text_list(name: str, value: object) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContextCacheError(f"{name} must be a list of str")
    return [_require_nonempty_str(f"{name}[{index}]", item) for index, item in enumerate(value)]


def _json_list(name: str, value: object) -> list[object]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ContextCacheError(f"{name} must be a list")
    items: list[object] = []
    for index, item in enumerate(value):
        if hasattr(item, "to_dict") and callable(item.to_dict):
            items.append(item.to_dict())
            continue
        if isinstance(item, Mapping) and not isinstance(item, (str, bytes)):
            items.append(dict(item))
            continue
        raise ContextCacheError(f"{name}[{index}] must be a mapping")
    return items


def _verify_optional_content(name: str, content: object, expected_hash: str) -> None:
    if content is None:
        return
    if isinstance(content, str):
        digest = sha256_hex(content)
    elif isinstance(content, bytes):
        digest = sha256_hex(content)
    else:
        raise ContextCacheError(f"{name} must be str or bytes")
    if digest != expected_hash:
        raise ContextCacheError(f"{name} does not match content_hash")


def _require_hex64(name: str, value: object) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise ContextCacheError(f"{name} must be a 64-char lowercase hex hash")
    return value


def _require_nonempty_str(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ContextCacheError(f"{name} must be a nonempty str")
    return value


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContextCacheError(f"{name} must be a positive int")
    return value


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextCacheError(f"{name} must be a nonnegative int")
    return value


def _require_keys(payload: Mapping[str, Any], required: Iterable[str], name: str) -> None:
    missing = [key for key in required if key not in payload]
    if missing:
        raise ContextCacheError(f"{name} missing required field {missing[0]}")


def _reject_extra(payload: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    extra = [key for key in payload if key not in allowed]
    if extra:
        raise ContextCacheError(f"{name} has unknown field {extra[0]!r}")
