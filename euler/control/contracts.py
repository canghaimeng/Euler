"""Problem contracts, mathematical scope, and evidence metadata.

Stdlib only. Original mathematical text is stored verbatim. Content IDs use
Phase 1 ``stable_id`` over semantic fields; status, validity, and timestamps
are excluded from mathematical identity. EvidenceArtifact is not a Fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from .ids import canonical_json, stable_id
from .schema import SchemaValidationError

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UTC = timezone.utc
_ZERO = timedelta(0)
_CONTRACT_DOMAIN = "contract"
_EVIDENCE_DOMAIN = "evidence"
_SCOPE_DOMAIN = "scope"
_IDENTITY_VERSION = 1

__all__ = [
    "QuantifierKind",
    "TargetKind",
    "EvidenceKind",
    "CheckerResult",
    "ValidityStatus",
    "QuantifierBinding",
    "ObjectBinding",
    "MathematicalScope",
    "ProblemContract",
    "EvidenceArtifact",
    "evidence_satisfies",
]


class QuantifierKind(str, Enum):
    FORALL = "FORALL"
    EXISTS = "EXISTS"


class TargetKind(str, Enum):
    PROOF = "PROOF"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    CLASSIFICATION = "CLASSIFICATION"
    FORMALIZATION = "FORMALIZATION"


class EvidenceKind(str, Enum):
    HEURISTIC_OBSERVATION = "HEURISTIC_OBSERVATION"
    EXACT_FINITE_RESULT = "EXACT_FINITE_RESULT"
    CHECKED_COUNTEREXAMPLE = "CHECKED_COUNTEREXAMPLE"
    LLM_CHECKED_PROOF = "LLM_CHECKED_PROOF"
    CERTIFICATE_CHECKED_RESULT = "CERTIFICATE_CHECKED_RESULT"
    KERNEL_CHECKED_PROOF = "KERNEL_CHECKED_PROOF"


class CheckerResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ValidityStatus(str, Enum):
    VALID = "VALID"
    STALE = "STALE"


_CHECKED_EVIDENCE_KINDS = frozenset(
    {
        EvidenceKind.EXACT_FINITE_RESULT,
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        EvidenceKind.LLM_CHECKED_PROOF,
        EvidenceKind.CERTIFICATE_CHECKED_RESULT,
        EvidenceKind.KERNEL_CHECKED_PROOF,
    }
)
_CHECKED_COUNTEREXAMPLE_KINDS = frozenset(
    {
        EvidenceKind.CHECKED_COUNTEREXAMPLE,
        EvidenceKind.CERTIFICATE_CHECKED_RESULT,
        EvidenceKind.EXACT_FINITE_RESULT,
    }
)
# Required kind -> actual kinds that may satisfy it. Not a total order: a kernel
# proof does not satisfy a counterexample requirement, and a heuristic never
# discharges proof/certificate/kernel/counterexample requirements.
_EVIDENCE_SATISFACTION_MATRIX: dict[EvidenceKind, frozenset[EvidenceKind]] = {
    EvidenceKind.HEURISTIC_OBSERVATION: frozenset(
        {
            EvidenceKind.HEURISTIC_OBSERVATION,
            EvidenceKind.EXACT_FINITE_RESULT,
            EvidenceKind.CHECKED_COUNTEREXAMPLE,
            EvidenceKind.LLM_CHECKED_PROOF,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        }
    ),
    EvidenceKind.EXACT_FINITE_RESULT: frozenset(
        {
            EvidenceKind.EXACT_FINITE_RESULT,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        }
    ),
    EvidenceKind.CHECKED_COUNTEREXAMPLE: frozenset(
        {
            EvidenceKind.CHECKED_COUNTEREXAMPLE,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
        }
    ),
    EvidenceKind.LLM_CHECKED_PROOF: frozenset(
        {
            EvidenceKind.LLM_CHECKED_PROOF,
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        }
    ),
    EvidenceKind.CERTIFICATE_CHECKED_RESULT: frozenset(
        {
            EvidenceKind.CERTIFICATE_CHECKED_RESULT,
            EvidenceKind.KERNEL_CHECKED_PROOF,
        }
    ),
    EvidenceKind.KERNEL_CHECKED_PROOF: frozenset({EvidenceKind.KERNEL_CHECKED_PROOF}),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class QuantifierBinding:
    """One ordered quantifier. ``domain`` is original mathematical text."""

    variable: str
    kind: QuantifierKind
    domain: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "variable", _require_original_text("variable", self.variable)
        )
        object.__setattr__(
            self, "kind", _require_enum(QuantifierKind, self.kind, "kind")
        )
        object.__setattr__(
            self, "domain", _require_original_text("domain", self.domain)
        )
        object.__setattr__(
            self,
            "depends_on",
            _require_original_tuple("depends_on", self.depends_on),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "variable": self.variable,
            "kind": self.kind.value,
            "domain": self.domain,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, data: object) -> "QuantifierBinding":
        payload = _mapping("QuantifierBinding", data)
        _reject_extra(
            payload, {"variable", "kind", "domain", "depends_on"}, "QuantifierBinding"
        )
        _require_keys(payload, {"variable", "kind", "domain"}, "QuantifierBinding")
        return cls(
            variable=payload["variable"],
            kind=payload["kind"],
            domain=payload["domain"],
            depends_on=_tuple_or_empty(payload.get("depends_on", ())),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ObjectBinding:
    """A named mathematical object. ``identity_key`` is semantic identity."""

    name: str
    object_type: str
    identity_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_original_text("name", self.name))
        object.__setattr__(
            self,
            "object_type",
            _require_original_text("object_type", self.object_type),
        )
        object.__setattr__(
            self,
            "identity_key",
            _require_original_text("identity_key", self.identity_key),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "object_type": self.object_type,
            "identity_key": self.identity_key,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ObjectBinding":
        payload = _mapping("ObjectBinding", data)
        _reject_extra(
            payload, {"name", "object_type", "identity_key"}, "ObjectBinding"
        )
        _require_keys(
            payload, {"name", "object_type", "identity_key"}, "ObjectBinding"
        )
        return cls(
            name=payload["name"],
            object_type=payload["object_type"],
            identity_key=payload["identity_key"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MathematicalScope:
    """Structured scope used for comparison and scheduling, not a formal proof."""

    assumptions: tuple[str, ...]
    quantifiers: tuple[QuantifierBinding, ...]
    objects: tuple[ObjectBinding, ...]
    parameter_domain: str
    definitions_hash: str

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        assumptions: tuple[str, ...] | list[str] = (),
        quantifiers: tuple[QuantifierBinding, ...] | list[QuantifierBinding] = (),
        objects: tuple[ObjectBinding, ...] | list[ObjectBinding] = (),
        parameter_domain: str,
        definitions_hash: str,
    ) -> "MathematicalScope":
        return cls(
            assumptions=tuple(assumptions),
            quantifiers=tuple(quantifiers),
            objects=tuple(objects),
            parameter_domain=parameter_domain,
            definitions_hash=definitions_hash,
        )

    def validate(self) -> None:
        object.__setattr__(
            self,
            "assumptions",
            _require_original_tuple("assumptions", self.assumptions),
        )
        if isinstance(self.quantifiers, str) or not isinstance(
            self.quantifiers, (list, tuple)
        ):
            raise SchemaValidationError("quantifiers must be a list of bindings")
        quants = []
        for item in self.quantifiers:
            if isinstance(item, QuantifierBinding):
                quants.append(item)
            else:
                quants.append(QuantifierBinding.from_dict(item))
        object.__setattr__(self, "quantifiers", tuple(quants))
        if isinstance(self.objects, str) or not isinstance(self.objects, (list, tuple)):
            raise SchemaValidationError("objects must be a list of bindings")
        objs = []
        for item in self.objects:
            if isinstance(item, ObjectBinding):
                objs.append(item)
            else:
                objs.append(ObjectBinding.from_dict(item))
        object.__setattr__(self, "objects", tuple(objs))
        object.__setattr__(
            self,
            "parameter_domain",
            _require_original_text("parameter_domain", self.parameter_domain),
        )
        object.__setattr__(
            self,
            "definitions_hash",
            _require_hex64("definitions_hash", self.definitions_hash),
        )

    def identity_payload(self) -> dict[str, Any]:
        return self.to_dict()

    def content_id(self) -> str:
        return stable_id(_SCOPE_DOMAIN, _IDENTITY_VERSION, canonical_json(self.identity_payload()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "assumptions": list(self.assumptions),
            "quantifiers": [item.to_dict() for item in self.quantifiers],
            "objects": [item.to_dict() for item in self.objects],
            "parameter_domain": self.parameter_domain,
            "definitions_hash": self.definitions_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> "MathematicalScope":
        payload = _mapping("MathematicalScope", data)
        allowed = {
            "assumptions",
            "quantifiers",
            "objects",
            "parameter_domain",
            "definitions_hash",
        }
        _reject_extra(payload, allowed, "MathematicalScope")
        _require_keys(payload, allowed, "MathematicalScope")
        return cls(
            assumptions=_tuple_or_empty(payload["assumptions"]),
            quantifiers=tuple(
                QuantifierBinding.from_dict(item) for item in _seq("quantifiers", payload["quantifiers"])
            ),
            objects=tuple(
                ObjectBinding.from_dict(item) for item in _seq("objects", payload["objects"])
            ),
            parameter_domain=payload["parameter_domain"],
            definitions_hash=payload["definitions_hash"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProblemContract:
    """Versioned problem contract. Semantic edits require a new version and ID."""

    contract_id: str
    problem_id: str
    version: int
    exact_statement: str
    scope: MathematicalScope
    permitted_tools: tuple[str, ...]
    external_help_policy: str
    target_kind: TargetKind
    required_evidence_level: EvidenceKind
    completion_checks: tuple[str, ...]
    budget_policy_ref: str

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        problem_id: str,
        version: int,
        exact_statement: str,
        scope: MathematicalScope,
        permitted_tools: tuple[str, ...] | list[str] = (),
        external_help_policy: str,
        target_kind: TargetKind,
        required_evidence_level: EvidenceKind,
        completion_checks: tuple[str, ...] | list[str] = (),
        budget_policy_ref: str,
    ) -> "ProblemContract":
        return cls(
            contract_id=_contract_id(
                problem_id=problem_id,
                version=version,
                exact_statement=exact_statement,
                scope=scope,
                permitted_tools=tuple(permitted_tools),
                external_help_policy=external_help_policy,
                target_kind=_require_enum(TargetKind, target_kind, "target_kind"),
                required_evidence_level=_require_enum(
                    EvidenceKind, required_evidence_level, "required_evidence_level"
                ),
                completion_checks=tuple(completion_checks),
                budget_policy_ref=budget_policy_ref,
            ),
            problem_id=problem_id,
            version=version,
            exact_statement=exact_statement,
            scope=scope,
            permitted_tools=tuple(permitted_tools),
            external_help_policy=external_help_policy,
            target_kind=target_kind,
            required_evidence_level=required_evidence_level,
            completion_checks=tuple(completion_checks),
            budget_policy_ref=budget_policy_ref,
        )

    def validate(self) -> None:
        object.__setattr__(
            self, "problem_id", _require_safe_identifier("problem_id", self.problem_id)
        )
        object.__setattr__(self, "version", _require_positive_int("version", self.version))
        object.__setattr__(
            self,
            "exact_statement",
            _require_original_text("exact_statement", self.exact_statement),
        )
        if not isinstance(self.scope, MathematicalScope):
            raise SchemaValidationError("scope must be a MathematicalScope")
        self.scope.validate()
        object.__setattr__(
            self,
            "permitted_tools",
            _require_original_tuple("permitted_tools", self.permitted_tools),
        )
        object.__setattr__(
            self,
            "external_help_policy",
            _require_original_text("external_help_policy", self.external_help_policy),
        )
        object.__setattr__(
            self,
            "target_kind",
            _require_enum(TargetKind, self.target_kind, "target_kind"),
        )
        object.__setattr__(
            self,
            "required_evidence_level",
            _require_enum(
                EvidenceKind, self.required_evidence_level, "required_evidence_level"
            ),
        )
        object.__setattr__(
            self,
            "completion_checks",
            _require_original_tuple("completion_checks", self.completion_checks),
        )
        object.__setattr__(
            self,
            "budget_policy_ref",
            _require_original_text("budget_policy_ref", self.budget_policy_ref),
        )
        expected = _contract_id(
            problem_id=self.problem_id,
            version=self.version,
            exact_statement=self.exact_statement,
            scope=self.scope,
            permitted_tools=self.permitted_tools,
            external_help_policy=self.external_help_policy,
            target_kind=self.target_kind,
            required_evidence_level=self.required_evidence_level,
            completion_checks=self.completion_checks,
            budget_policy_ref=self.budget_policy_ref,
        )
        object.__setattr__(self, "contract_id", _require_hex64("contract_id", self.contract_id))
        if self.contract_id != expected:
            raise SchemaValidationError("contract_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "problem_id": self.problem_id,
            "version": self.version,
            "exact_statement": self.exact_statement,
            "scope": self.scope.to_dict(),
            "permitted_tools": list(self.permitted_tools),
            "external_help_policy": self.external_help_policy,
            "target_kind": self.target_kind.value,
            "required_evidence_level": self.required_evidence_level.value,
            "completion_checks": list(self.completion_checks),
            "budget_policy_ref": self.budget_policy_ref,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ProblemContract":
        payload = _mapping("ProblemContract", data)
        allowed = {
            "contract_id",
            "problem_id",
            "version",
            "exact_statement",
            "scope",
            "permitted_tools",
            "external_help_policy",
            "target_kind",
            "required_evidence_level",
            "completion_checks",
            "budget_policy_ref",
        }
        _reject_extra(payload, allowed, "ProblemContract")
        _require_keys(payload, allowed, "ProblemContract")
        return cls(
            contract_id=payload["contract_id"],
            problem_id=payload["problem_id"],
            version=payload["version"],
            exact_statement=payload["exact_statement"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            permitted_tools=_tuple_or_empty(payload["permitted_tools"]),
            external_help_policy=payload["external_help_policy"],
            target_kind=payload["target_kind"],
            required_evidence_level=payload["required_evidence_level"],
            completion_checks=_tuple_or_empty(payload["completion_checks"]),
            budget_policy_ref=payload["budget_policy_ref"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceArtifact:
    """Metadata for a Phase 1 content hash. This is not a verified Fact."""

    artifact_id: str
    content_hash: str
    producer: str
    contract_id: str
    evidence_kind: EvidenceKind
    exact_claim: str
    scope: MathematicalScope
    input_hashes: tuple[str, ...]
    environment_hash: str
    created_at: str
    checker_id: Optional[str] = None
    checker_version: Optional[str] = None
    checker_result: Optional[CheckerResult] = None
    validity: ValidityStatus = ValidityStatus.VALID

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        content_hash: str,
        producer: str,
        contract_id: str,
        evidence_kind: EvidenceKind,
        exact_claim: str,
        scope: MathematicalScope,
        input_hashes: tuple[str, ...] | list[str] = (),
        environment_hash: str,
        checker_id: Optional[str] = None,
        checker_version: Optional[str] = None,
        checker_result: Optional[CheckerResult] = None,
        created_at: str,
        validity: ValidityStatus = ValidityStatus.VALID,
    ) -> "EvidenceArtifact":
        kind = _require_enum(EvidenceKind, evidence_kind, "evidence_kind")
        result = _optional_checker_result(checker_result)
        return cls(
            artifact_id=_evidence_id(
                content_hash=content_hash,
                producer=producer,
                contract_id=contract_id,
                evidence_kind=kind,
                exact_claim=exact_claim,
                scope=scope,
                input_hashes=tuple(input_hashes),
                environment_hash=environment_hash,
                checker_id=checker_id,
                checker_version=checker_version,
                checker_result=result,
            ),
            content_hash=content_hash,
            producer=producer,
            contract_id=contract_id,
            evidence_kind=kind,
            exact_claim=exact_claim,
            scope=scope,
            input_hashes=tuple(input_hashes),
            environment_hash=environment_hash,
            checker_id=checker_id,
            checker_version=checker_version,
            checker_result=result,
            created_at=created_at,
            validity=validity,
        )

    def validate(self) -> None:
        object.__setattr__(
            self, "content_hash", _require_hex64("content_hash", self.content_hash)
        )
        object.__setattr__(
            self, "producer", _require_original_text("producer", self.producer)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self,
            "evidence_kind",
            _require_enum(EvidenceKind, self.evidence_kind, "evidence_kind"),
        )
        object.__setattr__(
            self,
            "exact_claim",
            _require_original_text("exact_claim", self.exact_claim),
        )
        if not isinstance(self.scope, MathematicalScope):
            raise SchemaValidationError("scope must be a MathematicalScope")
        self.scope.validate()
        object.__setattr__(
            self,
            "input_hashes",
            _require_hex64_tuple("input_hashes", self.input_hashes),
        )
        object.__setattr__(
            self,
            "environment_hash",
            _require_hex64("environment_hash", self.environment_hash),
        )
        checker_id = _optional_text("checker_id", self.checker_id)
        checker_version = _optional_text("checker_version", self.checker_version)
        checker_result = _optional_checker_result(self.checker_result)
        object.__setattr__(self, "checker_id", checker_id)
        object.__setattr__(self, "checker_version", checker_version)
        object.__setattr__(self, "checker_result", checker_result)
        checker_fields = (checker_id, checker_version, checker_result)
        present = sum(field is not None for field in checker_fields)
        if self.evidence_kind in _CHECKED_EVIDENCE_KINDS:
            if present != 3:
                raise SchemaValidationError(
                    f"{self.evidence_kind.value} requires checker_id, "
                    "checker_version, and checker_result"
                )
        elif present not in (0, 3):
            raise SchemaValidationError(
                "checker fields must all be set or all be omitted"
            )
        _require_utc("created_at", self.created_at)
        object.__setattr__(
            self,
            "validity",
            _require_enum(ValidityStatus, self.validity, "validity"),
        )
        expected = _evidence_id(
            content_hash=self.content_hash,
            producer=self.producer,
            contract_id=self.contract_id,
            evidence_kind=self.evidence_kind,
            exact_claim=self.exact_claim,
            scope=self.scope,
            input_hashes=self.input_hashes,
            environment_hash=self.environment_hash,
            checker_id=self.checker_id,
            checker_version=self.checker_version,
            checker_result=self.checker_result,
        )
        object.__setattr__(
            self, "artifact_id", _require_hex64("artifact_id", self.artifact_id)
        )
        if self.artifact_id != expected:
            raise SchemaValidationError("artifact_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "producer": self.producer,
            "contract_id": self.contract_id,
            "evidence_kind": self.evidence_kind.value,
            "exact_claim": self.exact_claim,
            "scope": self.scope.to_dict(),
            "input_hashes": list(self.input_hashes),
            "environment_hash": self.environment_hash,
            "checker_id": self.checker_id,
            "checker_version": self.checker_version,
            "checker_result": None
            if self.checker_result is None
            else self.checker_result.value,
            "created_at": self.created_at,
            "validity": self.validity.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> "EvidenceArtifact":
        payload = _mapping("EvidenceArtifact", data)
        allowed = {
            "artifact_id",
            "content_hash",
            "producer",
            "contract_id",
            "evidence_kind",
            "exact_claim",
            "scope",
            "input_hashes",
            "environment_hash",
            "checker_id",
            "checker_version",
            "checker_result",
            "created_at",
            "validity",
        }
        _reject_extra(payload, allowed, "EvidenceArtifact")
        required = allowed - {
            "checker_id",
            "checker_version",
            "checker_result",
            "validity",
        }
        _require_keys(payload, required, "EvidenceArtifact")
        return cls(
            artifact_id=payload["artifact_id"],
            content_hash=payload["content_hash"],
            producer=payload["producer"],
            contract_id=payload["contract_id"],
            evidence_kind=payload["evidence_kind"],
            exact_claim=payload["exact_claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            input_hashes=_tuple_or_empty(payload["input_hashes"]),
            environment_hash=payload["environment_hash"],
            checker_id=payload.get("checker_id"),
            checker_version=payload.get("checker_version"),
            checker_result=payload.get("checker_result"),
            created_at=payload["created_at"],
            validity=payload.get("validity", ValidityStatus.VALID),
        )


def checked_evidence_kinds() -> frozenset[EvidenceKind]:
    return _CHECKED_EVIDENCE_KINDS


def checked_counterexample_kinds() -> frozenset[EvidenceKind]:
    return _CHECKED_COUNTEREXAMPLE_KINDS


def evidence_satisfies(
    artifact: EvidenceArtifact,
    required_kind: EvidenceKind,
    scope: MathematicalScope,
    contract_id: str,
    exact_claim: str | None = None,
) -> bool:
    """Return whether ``artifact`` may discharge ``required_kind``.

    Requires VALID status, exact scope, matching contract ID, optional exact
    claim, the frozen required-vs-actual kind matrix, and checker PASS except
    for heuristic observations. FAIL/UNKNOWN checked artifacts stay valid
    historical records but do not satisfy.
    """
    if not isinstance(artifact, EvidenceArtifact):
        raise SchemaValidationError("artifact must be an EvidenceArtifact")
    required = _require_enum(EvidenceKind, required_kind, "required_kind")
    if not isinstance(scope, MathematicalScope):
        raise SchemaValidationError("scope must be a MathematicalScope")
    contract_id = _require_hex64("contract_id", contract_id)
    if exact_claim is not None:
        exact_claim = _require_original_text("exact_claim", exact_claim)
    if artifact.validity is not ValidityStatus.VALID:
        return False
    if artifact.contract_id != contract_id:
        return False
    if artifact.scope != scope:
        return False
    if exact_claim is not None and artifact.exact_claim != exact_claim:
        return False
    if artifact.evidence_kind not in _EVIDENCE_SATISFACTION_MATRIX[required]:
        return False
    if artifact.evidence_kind is EvidenceKind.HEURISTIC_OBSERVATION:
        return True
    return artifact.checker_result is CheckerResult.PASS


def _contract_id(
    *,
    problem_id: str,
    version: int,
    exact_statement: str,
    scope: MathematicalScope,
    permitted_tools: tuple[str, ...],
    external_help_policy: str,
    target_kind: TargetKind,
    required_evidence_level: EvidenceKind,
    completion_checks: tuple[str, ...],
    budget_policy_ref: str,
) -> str:
    payload = {
        "problem_id": problem_id,
        "version": version,
        "exact_statement": exact_statement,
        "scope": scope.identity_payload(),
        "permitted_tools": list(permitted_tools),
        "external_help_policy": external_help_policy,
        "target_kind": target_kind.value,
        "required_evidence_level": required_evidence_level.value,
        "completion_checks": list(completion_checks),
        "budget_policy_ref": budget_policy_ref,
    }
    return stable_id(_CONTRACT_DOMAIN, _IDENTITY_VERSION, canonical_json(payload))


def _evidence_id(
    *,
    content_hash: str,
    producer: str,
    contract_id: str,
    evidence_kind: EvidenceKind,
    exact_claim: str,
    scope: MathematicalScope,
    input_hashes: tuple[str, ...],
    environment_hash: str,
    checker_id: Optional[str],
    checker_version: Optional[str],
    checker_result: Optional[CheckerResult],
) -> str:
    payload = {
        "content_hash": content_hash,
        "producer": producer,
        "contract_id": contract_id,
        "evidence_kind": evidence_kind.value,
        "exact_claim": exact_claim,
        "scope": scope.identity_payload(),
        "input_hashes": list(input_hashes),
        "environment_hash": environment_hash,
        "checker_id": checker_id,
        "checker_version": checker_version,
        "checker_result": None if checker_result is None else checker_result.value,
    }
    return stable_id(_EVIDENCE_DOMAIN, _IDENTITY_VERSION, canonical_json(payload))


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise SchemaValidationError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise SchemaValidationError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise SchemaValidationError(f"{label} missing fields: {sorted(missing)}")


def _require_original_text(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SchemaValidationError(f"{name} must be original nonempty text")
    return value


def _require_safe_identifier(name: str, value: object) -> str:
    text = _require_original_text(name, value)
    if _SAFE_IDENTIFIER_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} is not a safe identifier")
    return text


def _require_hex64(name: str, value: object) -> str:
    text = _require_original_text(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise SchemaValidationError(f"{name} must be 64-char lowercase hex")
    return text


def _require_hex64_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list of 64-hex hashes")
    return tuple(_require_hex64(f"{name}[{i}]", item) for i, item in enumerate(value))


def _require_original_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list of original text")
    return tuple(
        _require_original_text(f"{name}[{i}]", item) for i, item in enumerate(value)
    )


def _tuple_or_empty(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError("expected a list")
    return tuple(value)


def _seq(name: str, value: object) -> tuple:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise SchemaValidationError(f"{name} must be a list")
    return tuple(value)


def _require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchemaValidationError(f"{name} must be a positive int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise SchemaValidationError(f"{name} has unknown value {value!r}") from None
    raise SchemaValidationError(f"{name} must be {enum_cls.__name__}")


def _optional_text(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_original_text(name, value)


def _optional_checker_result(value: object) -> Optional[CheckerResult]:
    if value is None:
        return None
    return _require_enum(CheckerResult, value, "checker_result")


def _require_utc(name: str, value: object) -> datetime:
    text = _require_original_text(name, value)
    parsed = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise SchemaValidationError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise SchemaValidationError(f"{name} must be UTC")
    return dt.astimezone(_UTC)
