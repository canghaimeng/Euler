"""Exact finite-box integer polynomial counterexample search and check.

Stdlib only. Search evaluation uses integer ``pow``; the checker uses a
separately written repeated-multiplication path and never calls the search
evaluator. No generated code, float, shell, network, or truth-store I/O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from euler.control.compute import ComponentIdentity
from euler.control.contracts import CheckerResult
from euler.control.ids import canonical_json, sha256_hex, stable_id

_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_VARIABLES = 8
_MAX_TERMS = 256
_MAX_INT_BITS = 256
_MAX_EXPONENT = 64
_PAYLOAD_KEYS = frozenset({"variables", "intervals", "terms", "relation"})
_INTERVAL_KEYS = frozenset({"lower", "upper"})
_TERM_KEYS = frozenset({"coefficient", "exponents"})
_PLUGIN_ID = "polynomial-counterexample"
_SEARCHER_VERSION = "1"
_CHECKER_VERSION = "1"
_SEARCHER_COMPONENT_ID = stable_id(
    "compute-component", 1, "polynomial-counterexample-searcher"
)
_CHECKER_COMPONENT_ID = stable_id(
    "compute-component", 1, "polynomial-counterexample-checker"
)
_SEARCHER_CODE_HASH = stable_id(
    "compute-code", 1, "polynomial-counterexample-searcher", _SEARCHER_VERSION
)
_CHECKER_CODE_HASH = stable_id(
    "compute-code", 1, "polynomial-counterexample-checker", _CHECKER_VERSION
)

__all__ = [
    "PolynomialPluginError",
    "PolynomialRelation",
    "IntegerInterval",
    "PolynomialTerm",
    "PolynomialPredicate",
    "PolynomialSearcher",
    "PolynomialChecker",
    "PLUGIN_ID",
    "SEARCHER_COMPONENT_ID",
    "CHECKER_COMPONENT_ID",
    "SEARCHER_CODE_HASH",
    "CHECKER_CODE_HASH",
    "SEARCHER_VERSION",
    "CHECKER_VERSION",
]

PLUGIN_ID = _PLUGIN_ID
SEARCHER_COMPONENT_ID = _SEARCHER_COMPONENT_ID
CHECKER_COMPONENT_ID = _CHECKER_COMPONENT_ID
SEARCHER_CODE_HASH = _SEARCHER_CODE_HASH
CHECKER_CODE_HASH = _CHECKER_CODE_HASH
SEARCHER_VERSION = _SEARCHER_VERSION
CHECKER_VERSION = _CHECKER_VERSION


class PolynomialPluginError(ValueError):
    """Malformed polynomial spec, domain, assignment, or complexity cap."""


class PolynomialRelation(str, Enum):
    EQ = "EQ"
    NE = "NE"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"


@dataclass(frozen=True, slots=True, kw_only=True)
class IntegerInterval:
    """Inclusive finite integer interval ``[lower, upper]``."""

    lower: int
    upper: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "lower", _require_exact_int("lower", self.lower))
        object.__setattr__(self, "upper", _require_exact_int("upper", self.upper))
        _require_bit_length("lower", self.lower)
        _require_bit_length("upper", self.upper)
        if self.lower > self.upper:
            raise PolynomialPluginError("interval lower must be <= upper")

    def size(self) -> int:
        return self.upper - self.lower + 1

    def contains(self, value: object) -> bool:
        if not _is_exact_int(value):
            return False
        return self.lower <= value <= self.upper

    def to_dict(self) -> dict[str, int]:
        return {"lower": self.lower, "upper": self.upper}

    @classmethod
    def from_dict(cls, data: object) -> "IntegerInterval":
        payload = _mapping("IntegerInterval", data)
        _reject_extra(payload, _INTERVAL_KEYS, "IntegerInterval")
        _require_keys(payload, _INTERVAL_KEYS, "IntegerInterval")
        return cls(lower=payload["lower"], upper=payload["upper"])


@dataclass(frozen=True, slots=True, kw_only=True)
class PolynomialTerm:
    """One sparse monomial: exact coefficient times variables^exponents."""

    coefficient: int
    exponents: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "coefficient", _require_exact_int("coefficient", self.coefficient)
        )
        _require_bit_length("coefficient", self.coefficient)
        if isinstance(self.exponents, str) or not isinstance(self.exponents, tuple):
            raise PolynomialPluginError("exponents must be a tuple of int")
        cleaned: list[int] = []
        for index, exp in enumerate(self.exponents):
            value = _require_exact_int(f"exponents[{index}]", exp)
            if value < 0 or value > _MAX_EXPONENT:
                raise PolynomialPluginError(
                    f"exponents[{index}] must be in 0..{_MAX_EXPONENT}"
                )
            cleaned.append(value)
        object.__setattr__(self, "exponents", tuple(cleaned))

    def to_dict(self) -> dict[str, object]:
        return {"coefficient": self.coefficient, "exponents": list(self.exponents)}

    @classmethod
    def from_dict(cls, data: object, *, arity: int | None = None) -> "PolynomialTerm":
        payload = _mapping("PolynomialTerm", data)
        _reject_extra(payload, _TERM_KEYS, "PolynomialTerm")
        _require_keys(payload, _TERM_KEYS, "PolynomialTerm")
        exponents = payload["exponents"]
        if isinstance(exponents, str) or not isinstance(exponents, (list, tuple)):
            raise PolynomialPluginError("exponents must be a list of int")
        if arity is not None and len(exponents) != arity:
            raise PolynomialPluginError("exponent arity does not match variable count")
        return cls(coefficient=payload["coefficient"], exponents=tuple(exponents))


@dataclass(frozen=True, slots=True, kw_only=True)
class PolynomialPredicate:
    """Canonical sparse polynomial relation against exact integer zero."""

    variables: tuple[str, ...]
    intervals: tuple[IntegerInterval, ...]
    terms: tuple[PolynomialTerm, ...]
    relation: PolynomialRelation

    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", _require_variables(self.variables))
        if not isinstance(self.intervals, tuple):
            raise PolynomialPluginError("intervals must be a tuple")
        if len(self.intervals) != len(self.variables):
            raise PolynomialPluginError("intervals must match variable count")
        for index, interval in enumerate(self.intervals):
            if not isinstance(interval, IntegerInterval):
                raise PolynomialPluginError(f"intervals[{index}] must be IntegerInterval")
        object.__setattr__(
            self, "relation", _require_relation("relation", self.relation)
        )
        arity = len(self.variables)
        if not isinstance(self.terms, tuple):
            raise PolynomialPluginError("terms must be a tuple")
        if len(self.terms) > _MAX_TERMS:
            raise PolynomialPluginError(f"at most {_MAX_TERMS} terms")
        canonical = _canonicalize_terms(self.terms, arity)
        object.__setattr__(self, "terms", canonical)

    @classmethod
    def build(
        cls,
        *,
        variables: object,
        intervals: object,
        terms: object,
        relation: object,
    ) -> "PolynomialPredicate":
        names = _require_variables(variables)
        interval_items = _require_sequence("intervals", intervals)
        if len(interval_items) != len(names):
            raise PolynomialPluginError("intervals must match variable count")
        parsed_intervals = tuple(
            item
            if isinstance(item, IntegerInterval)
            else IntegerInterval.from_dict(item)
            for item in interval_items
        )
        term_items = _require_sequence("terms", terms)
        if len(term_items) > _MAX_TERMS:
            raise PolynomialPluginError(f"at most {_MAX_TERMS} terms")
        parsed_terms = tuple(
            item
            if isinstance(item, PolynomialTerm)
            else PolynomialTerm.from_dict(item, arity=len(names))
            for item in term_items
        )
        return cls(
            variables=names,
            intervals=parsed_intervals,
            terms=parsed_terms,
            relation=_require_relation("relation", relation),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "intervals": [item.to_dict() for item in self.intervals],
            "relation": self.relation.value,
            "terms": [item.to_dict() for item in self.terms],
            "variables": list(self.variables),
        }

    def to_plugin_payload_json(self) -> str:
        return canonical_json(self.to_dict())

    def payload_hash(self) -> str:
        return sha256_hex(self.to_plugin_payload_json())

    def domain_hash(self) -> str:
        return stable_id(
            "polynomial-domain",
            1,
            canonical_json(
                {
                    "intervals": [item.to_dict() for item in self.intervals],
                    "variables": list(self.variables),
                }
            ),
        )

    def total_points(self) -> int:
        total = 1
        for interval in self.intervals:
            total *= interval.size()
        return total

    def assignment_at(self, ordinal: object) -> dict[str, int]:
        index = _require_exact_int("ordinal", ordinal)
        total = self.total_points()
        if index < 0 or index >= total:
            raise PolynomialPluginError("ordinal is outside the finite domain")
        remaining = index
        values: list[int] = [0] * len(self.variables)
        for pos in range(len(self.variables) - 1, -1, -1):
            size = self.intervals[pos].size()
            digit = remaining % size
            remaining //= size
            values[pos] = self.intervals[pos].lower + digit
        return {
            name: values[pos] for pos, name in enumerate(self.variables)
        }

    def request_metadata(self) -> dict[str, object]:
        payload = self.to_plugin_payload_json()
        return {
            "declared_total_points": self.total_points(),
            "domain_hash": self.domain_hash(),
            "plugin_id": PLUGIN_ID,
            "plugin_payload_hash": sha256_hex(payload),
            "plugin_payload_json": payload,
            "plugin_version": SEARCHER_VERSION,
        }

    @classmethod
    def from_plugin_payload_json(cls, data: object) -> "PolynomialPredicate":
        payload = _parse_payload(data)
        return cls.build(
            variables=payload["variables"],
            intervals=payload["intervals"],
            terms=payload["terms"],
            relation=payload["relation"],
        )

    @classmethod
    def from_dict(cls, data: object) -> "PolynomialPredicate":
        return cls.from_plugin_payload_json(data)


@dataclass(frozen=True, slots=True)
class PolynomialSearcher:
    """Finite-box search evaluator. Uses integer pow; does not check candidates."""

    identity: ComponentIdentity = ComponentIdentity(
        component_id=SEARCHER_COMPONENT_ID,
        version=SEARCHER_VERSION,
        code_hash=SEARCHER_CODE_HASH,
    )

    def total_points(self, request: object) -> int:
        return _predicate_from_request(request).total_points()

    def assignment_at(self, request: object, ordinal: int) -> Mapping[str, int]:
        return _predicate_from_request(request).assignment_at(ordinal)

    def predicate_holds(self, request: object, assignment: Mapping[str, int]) -> bool:
        predicate = _predicate_from_request(request)
        values = _require_assignment_values(predicate, assignment, where="search")
        value = _eval_poly_pow(predicate.terms, values)
        return _relation_holds_search(predicate.relation, value)


@dataclass(frozen=True, slots=True)
class PolynomialChecker:
    """Independent checker. Reparses persisted bytes; uses repeated multiplication."""

    identity: ComponentIdentity = ComponentIdentity(
        component_id=CHECKER_COMPONENT_ID,
        version=CHECKER_VERSION,
        code_hash=CHECKER_CODE_HASH,
    )

    def check(
        self, request: object, persisted_candidate_bytes: bytes
    ) -> tuple[CheckerResult, str]:
        try:
            predicate = _predicate_from_request(request)
        except (PolynomialPluginError, TypeError, ValueError, KeyError) as exc:
            return CheckerResult.UNKNOWN, f"malformed plugin payload: {exc}"
        assignment = _parse_persisted_assignment(persisted_candidate_bytes)
        if isinstance(assignment, str):
            return CheckerResult.UNKNOWN, assignment
        keys = set(assignment)
        expected = set(predicate.variables)
        if keys != expected:
            return CheckerResult.FAIL, "candidate keys must be exactly the declared variables"
        values: list[int] = []
        for pos, name in enumerate(predicate.variables):
            raw = assignment[name]
            if isinstance(raw, bool) or not isinstance(raw, int):
                return CheckerResult.FAIL, f"{name} must be an exact int"
            if not predicate.intervals[pos].contains(raw):
                return CheckerResult.FAIL, f"{name} is outside the declared interval"
            values.append(raw)
        value = _eval_poly_mul(predicate.terms, values)
        if _relation_holds_check(predicate.relation, value):
            return CheckerResult.FAIL, "candidate satisfies the structured predicate"
        return CheckerResult.PASS, "candidate is in-domain and violates the structured predicate"


def _predicate_from_request(request: object) -> PolynomialPredicate:
    if getattr(request, "plugin_id", None) != PLUGIN_ID:
        raise PolynomialPluginError("request plugin_id does not match polynomial plugin")
    if getattr(request, "plugin_version", None) != SEARCHER_VERSION:
        raise PolynomialPluginError(
            "request plugin_version does not match polynomial plugin"
        )
    payload = getattr(request, "plugin_payload_json", None)
    predicate = PolynomialPredicate.from_plugin_payload_json(payload)
    expected_payload_hash = getattr(request, "plugin_payload_hash", None)
    if expected_payload_hash is not None:
        actual = predicate.payload_hash()
        if expected_payload_hash != actual:
            raise PolynomialPluginError("plugin_payload_hash does not match payload")
    expected_domain = getattr(request, "domain_hash", None)
    if expected_domain is not None and expected_domain != predicate.domain_hash():
        raise PolynomialPluginError("domain_hash does not match payload")
    declared = getattr(request, "declared_total_points", None)
    if declared is not None:
        total = predicate.total_points()
        if not _is_exact_int(declared) or declared != total:
            raise PolynomialPluginError("declared_total_points does not match domain")
    return predicate


def _eval_poly_pow(terms: tuple[PolynomialTerm, ...], values: list[int]) -> int:
    acc = 0
    for term in terms:
        monomial = 1
        for base, exp in zip(values, term.exponents):
            monomial *= pow(base, exp)
        acc += term.coefficient * monomial
    return acc


def _int_pow_by_mul(base: int, exp: int) -> int:
    result = 1
    step = 0
    while step < exp:
        result = result * base
        step = step + 1
    return result


def _eval_poly_mul(terms: tuple[PolynomialTerm, ...], values: list[int]) -> int:
    acc = 0
    term_index = 0
    while term_index < len(terms):
        term = terms[term_index]
        monomial = 1
        var_index = 0
        while var_index < len(term.exponents):
            monomial = monomial * _int_pow_by_mul(values[var_index], term.exponents[var_index])
            var_index = var_index + 1
        acc = acc + term.coefficient * monomial
        term_index = term_index + 1
    return acc


def _relation_holds_search(relation: PolynomialRelation, value: int) -> bool:
    if relation is PolynomialRelation.EQ:
        return value == 0
    if relation is PolynomialRelation.NE:
        return value != 0
    if relation is PolynomialRelation.LT:
        return value < 0
    if relation is PolynomialRelation.LE:
        return value <= 0
    if relation is PolynomialRelation.GT:
        return value > 0
    return value >= 0


def _relation_holds_check(relation: PolynomialRelation, value: int) -> bool:
    holds = False
    if relation is PolynomialRelation.GE:
        holds = value >= 0
    elif relation is PolynomialRelation.GT:
        holds = value > 0
    elif relation is PolynomialRelation.LE:
        holds = value <= 0
    elif relation is PolynomialRelation.LT:
        holds = value < 0
    elif relation is PolynomialRelation.NE:
        holds = value != 0
    elif relation is PolynomialRelation.EQ:
        holds = value == 0
    else:
        holds = False
    return holds


def _require_assignment_values(
    predicate: PolynomialPredicate, assignment: object, *, where: str
) -> list[int]:
    payload = _mapping("assignment", assignment)
    expected = set(predicate.variables)
    keys = set(payload)
    if keys != expected:
        raise PolynomialPluginError("assignment keys must be exactly the declared variables")
    values: list[int] = []
    for pos, name in enumerate(predicate.variables):
        raw = payload[name]
        if not _is_exact_int(raw):
            raise PolynomialPluginError(f"{where} assignment {name} must be an exact int")
        if not predicate.intervals[pos].contains(raw):
            raise PolynomialPluginError(f"{where} assignment {name} is out of domain")
        values.append(raw)
    return values


def _parse_persisted_assignment(data: object) -> dict[str, Any] | str:
    if not isinstance(data, (bytes, bytearray)):
        return "candidate is not bytes"
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return "candidate is not UTF-8"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return "candidate is not JSON"
    if not isinstance(parsed, dict):
        return "candidate is not a JSON object"
    for key in parsed:
        if not isinstance(key, str):
            return "candidate keys must be str"
    return parsed


def _canonicalize_terms(
    terms: tuple[PolynomialTerm, ...], arity: int
) -> tuple[PolynomialTerm, ...]:
    combined: dict[tuple[int, ...], int] = {}
    for term in terms:
        if not isinstance(term, PolynomialTerm):
            raise PolynomialPluginError("terms must contain PolynomialTerm")
        if len(term.exponents) != arity:
            raise PolynomialPluginError("exponent arity does not match variable count")
        combined[term.exponents] = combined.get(term.exponents, 0) + term.coefficient
    canonical: list[PolynomialTerm] = []
    for exponents in sorted(combined):
        coefficient = combined[exponents]
        if coefficient == 0:
            continue
        _require_bit_length("coefficient", coefficient)
        canonical.append(
            PolynomialTerm(coefficient=coefficient, exponents=exponents)
        )
    if len(canonical) > _MAX_TERMS:
        raise PolynomialPluginError(f"at most {_MAX_TERMS} canonical nonzero terms")
    return tuple(canonical)


def _parse_payload(data: object) -> dict[str, Any]:
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolynomialPluginError("plugin payload is not UTF-8") from exc
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise PolynomialPluginError("plugin payload is not JSON") from exc
        data = parsed
    payload = _mapping("plugin payload", data)
    _reject_extra(payload, _PAYLOAD_KEYS, "plugin payload")
    _require_keys(payload, _PAYLOAD_KEYS, "plugin payload")
    return payload


def _require_variables(value: object) -> tuple[str, ...]:
    items = _require_sequence("variables", value)
    if not items:
        raise PolynomialPluginError("variables must be nonempty")
    if len(items) > _MAX_VARIABLES:
        raise PolynomialPluginError(f"at most {_MAX_VARIABLES} variables")
    names: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, str) or item == "":
            raise PolynomialPluginError(f"variables[{index}] must be a nonempty str")
        if _VAR_RE.fullmatch(item) is None:
            raise PolynomialPluginError(f"variables[{index}] is not a valid identifier")
        if item in seen:
            raise PolynomialPluginError(f"duplicate variable name {item!r}")
        seen.add(item)
        names.append(item)
    return tuple(names)


def _require_relation(name: str, value: object) -> PolynomialRelation:
    if isinstance(value, PolynomialRelation):
        return value
    if isinstance(value, str):
        try:
            return PolynomialRelation(value)
        except ValueError as exc:
            raise PolynomialPluginError(f"{name} has unknown relation {value!r}") from exc
    raise PolynomialPluginError(f"{name} must be a PolynomialRelation")


def _require_sequence(name: str, value: object) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise PolynomialPluginError(f"{name} must be a list")
    return tuple(value)


def _mapping(name: str, data: object) -> dict[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise PolynomialPluginError(f"{name} must be a mapping")
    return {str(key): data[key] for key in data}


def _require_keys(payload: Mapping[str, Any], required: frozenset[str], name: str) -> None:
    missing = [key for key in required if key not in payload]
    if missing:
        raise PolynomialPluginError(f"{name} missing required field {missing[0]}")


def _reject_extra(payload: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    extra = [key for key in payload if key not in allowed]
    if extra:
        raise PolynomialPluginError(f"{name} has unknown field {extra[0]!r}")


def _is_exact_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_exact_int(name: str, value: object) -> int:
    if not _is_exact_int(value):
        raise PolynomialPluginError(f"{name} must be an exact int")
    return value


def _require_bit_length(name: str, value: int) -> None:
    if value.bit_length() > _MAX_INT_BITS:
        raise PolynomialPluginError(f"{name} exceeds {_MAX_INT_BITS}-bit cap")
