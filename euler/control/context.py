"""Minimum-sufficient Context Compiler.

Stdlib only. Compiles a deterministic ContextPacket from an explicit request,
ResearchView, ProjectSnapshot and ArtifactStore. Required mathematics is
never truncated. Optional failures/relations may be omitted with manifest
reasons. The compiler does not write caches or mutate stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from euler.control.adapters.project import (
    FactSnapshot,
    ProjectSnapshot,
)
from euler.control.artifacts import ArtifactNotFound, ArtifactStore
from euler.control.cache import ContextCacheKey, build_context_cache_key
from euler.control.contracts import ProblemContract, ValidityStatus
from euler.control.failures import FailureRecord
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, Relation, Route
from euler.control.recall import (
    LOOP_INTEGRATION,
    MissingDependency,
    compact_blocker_payload,
    dependency_interfaces,
    infer_dependency_roots,
    render_dependency_interface,
    select_blockers,
    validate_predecessor_closure,
)
from euler.control.views import ResearchView

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_COMPILER_SCHEMA_VERSION = 1
_SELECTION_POLICY_VERSION = 2
_SECTION_POLICY = "exact-id-scope-token-time"
_SECTION_ORDER = (
    "CONTRACT",
    "OBLIGATION",
    "ROUTE",
    "FACTS",
    "EVIDENCE",
    "FAILURES",
    "RELATIONS",
    "ACTION",
    "MANIFEST",
)

__all__ = [
    "ContextCompilerError",
    "ContextTooLarge",
    "MissingRequiredContext",
    "LimitKind",
    "ContextBound",
    "ContextLimits",
    "ContextRequest",
    "ContextOmission",
    "ContextManifest",
    "ContextPacket",
    "ContextCompiler",
]


class ContextCompilerError(ValueError):
    """Malformed request or unusable context inputs."""


class ContextTooLarge(ContextCompilerError):
    """Mandatory packet material exceeds explicit char/byte limits."""

    def __init__(
        self,
        message: str,
        *,
        measured_chars: int,
        measured_bytes: int,
        required_ids: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.measured_chars = measured_chars
        self.measured_bytes = measured_bytes
        self.required_ids = required_ids


class MissingRequiredContext(ContextCompilerError):
    """A required Fact, evidence, or graph ref is missing, revoked, or stale."""

    def __init__(self, message: str, *, missing_ids: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing_ids = missing_ids


class LimitKind(str, Enum):
    LIMITED = "LIMITED"
    UNLIMITED = "UNLIMITED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextBound:
    kind: LimitKind
    value: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _require_enum(LimitKind, self.kind, "kind"))
        if self.kind is LimitKind.UNLIMITED:
            if self.value is not None:
                raise ContextCompilerError("UNLIMITED limit requires value=None")
            return
        object.__setattr__(self, "value", _require_nonneg_int("value", self.value))

    def allows(self, count: int) -> bool:
        if self.kind is LimitKind.UNLIMITED:
            return True
        return count <= int(self.value or 0)

    def cap(self) -> int | None:
        if self.kind is LimitKind.UNLIMITED:
            return None
        return int(self.value or 0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "value": self.value}

    @classmethod
    def from_dict(cls, data: object, *, label: str = "ContextBound") -> "ContextBound":
        payload = _mapping(label, data)
        _reject_extra(payload, {"kind", "value"}, label)
        _require_keys(payload, {"kind"}, label)
        return cls(kind=payload["kind"], value=payload.get("value"))


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextLimits:
    max_chars: ContextBound
    max_bytes: ContextBound
    max_optional_failures: ContextBound
    max_optional_relations: ContextBound

    def __post_init__(self) -> None:
        if not isinstance(self.max_chars, ContextBound):
            raise ContextCompilerError("max_chars must be a ContextBound")
        if not isinstance(self.max_bytes, ContextBound):
            raise ContextCompilerError("max_bytes must be a ContextBound")
        if not isinstance(self.max_optional_failures, ContextBound):
            raise ContextCompilerError("max_optional_failures must be a ContextBound")
        if not isinstance(self.max_optional_relations, ContextBound):
            raise ContextCompilerError("max_optional_relations must be a ContextBound")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_chars": self.max_chars.to_dict(),
            "max_bytes": self.max_bytes.to_dict(),
            "max_optional_failures": self.max_optional_failures.to_dict(),
            "max_optional_relations": self.max_optional_relations.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ContextLimits":
        payload = _mapping("ContextLimits", data)
        allowed = {
            "max_chars",
            "max_bytes",
            "max_optional_failures",
            "max_optional_relations",
        }
        _reject_extra(payload, allowed, "ContextLimits")
        _require_keys(payload, allowed, "ContextLimits")
        return cls(
            max_chars=ContextBound.from_dict(payload["max_chars"], label="max_chars"),
            max_bytes=ContextBound.from_dict(payload["max_bytes"], label="max_bytes"),
            max_optional_failures=ContextBound.from_dict(
                payload["max_optional_failures"], label="max_optional_failures"
            ),
            max_optional_relations=ContextBound.from_dict(
                payload["max_optional_relations"], label="max_optional_relations"
            ),
        )

    @classmethod
    def unlimited(cls) -> "ContextLimits":
        unbound = ContextBound(kind=LimitKind.UNLIMITED, value=None)
        return cls(
            max_chars=unbound,
            max_bytes=unbound,
            max_optional_failures=unbound,
            max_optional_relations=unbound,
        )

    @classmethod
    def build(
        cls,
        *,
        max_chars: int | None = None,
        max_bytes: int | None = None,
        max_optional_failures: int | None = None,
        max_optional_relations: int | None = None,
    ) -> "ContextLimits":
        def bound(value: int | None) -> ContextBound:
            if value is None:
                return ContextBound(kind=LimitKind.UNLIMITED, value=None)
            return ContextBound(kind=LimitKind.LIMITED, value=value)

        return cls(
            max_chars=bound(max_chars),
            max_bytes=bound(max_bytes),
            max_optional_failures=bound(max_optional_failures),
            max_optional_relations=bound(max_optional_relations),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextRequest:
    request_id: str
    contract_id: str
    obligation_id: str
    action_id: str
    concrete_gap: str
    output_contract: str
    tool_environment_hash: str
    acceptance_policy_hash: str
    reference_versions_hash: str
    expand_evidence_content: bool
    limits: ContextLimits
    required_fact_ids: tuple[str, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    relevant_failure_ids: tuple[str, ...] = ()
    relevant_relation_ids: tuple[str, ...] = ()
    route_id: Optional[str] = None
    expand_proof_content: bool = False
    auto_blocker_recall: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_nonempty("request_id", self.request_id)
        )
        object.__setattr__(
            self, "contract_id", _require_hex64("contract_id", self.contract_id)
        )
        object.__setattr__(
            self, "obligation_id", _require_hex64("obligation_id", self.obligation_id)
        )
        object.__setattr__(
            self, "action_id", _require_hex64("action_id", self.action_id)
        )
        object.__setattr__(
            self, "concrete_gap", _require_nonempty("concrete_gap", self.concrete_gap)
        )
        object.__setattr__(
            self,
            "output_contract",
            _require_nonempty("output_contract", self.output_contract),
        )
        object.__setattr__(
            self,
            "tool_environment_hash",
            _require_hex64("tool_environment_hash", self.tool_environment_hash),
        )
        object.__setattr__(
            self,
            "acceptance_policy_hash",
            _require_hex64("acceptance_policy_hash", self.acceptance_policy_hash),
        )
        object.__setattr__(
            self,
            "reference_versions_hash",
            _require_hex64("reference_versions_hash", self.reference_versions_hash),
        )
        if not isinstance(self.expand_evidence_content, bool):
            raise ContextCompilerError("expand_evidence_content must be bool")
        if not isinstance(self.expand_proof_content, bool):
            raise ContextCompilerError("expand_proof_content must be bool")
        if not isinstance(self.auto_blocker_recall, bool):
            raise ContextCompilerError("auto_blocker_recall must be bool")
        if not isinstance(self.limits, ContextLimits):
            raise ContextCompilerError("limits must be ContextLimits")
        object.__setattr__(
            self,
            "required_fact_ids",
            _require_id_tuple("required_fact_ids", self.required_fact_ids, hex64=False),
        )
        object.__setattr__(
            self,
            "required_evidence_ids",
            _require_id_tuple("required_evidence_ids", self.required_evidence_ids),
        )
        object.__setattr__(
            self,
            "relevant_failure_ids",
            _require_id_tuple("relevant_failure_ids", self.relevant_failure_ids),
        )
        object.__setattr__(
            self,
            "relevant_relation_ids",
            _require_id_tuple("relevant_relation_ids", self.relevant_relation_ids),
        )
        object.__setattr__(
            self, "route_id", _optional_hex64("route_id", self.route_id)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "route_id": self.route_id,
            "action_id": self.action_id,
            "concrete_gap": self.concrete_gap,
            "output_contract": self.output_contract,
            "tool_environment_hash": self.tool_environment_hash,
            "acceptance_policy_hash": self.acceptance_policy_hash,
            "reference_versions_hash": self.reference_versions_hash,
            "expand_evidence_content": self.expand_evidence_content,
            "expand_proof_content": self.expand_proof_content,
            "auto_blocker_recall": self.auto_blocker_recall,
            "limits": self.limits.to_dict(),
            "required_fact_ids": list(self.required_fact_ids),
            "required_evidence_ids": list(self.required_evidence_ids),
            "relevant_failure_ids": list(self.relevant_failure_ids),
            "relevant_relation_ids": list(self.relevant_relation_ids),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ContextRequest":
        payload = _mapping("ContextRequest", data)
        allowed = {
            "request_id",
            "contract_id",
            "obligation_id",
            "route_id",
            "action_id",
            "concrete_gap",
            "output_contract",
            "tool_environment_hash",
            "acceptance_policy_hash",
            "reference_versions_hash",
            "expand_evidence_content",
            "expand_proof_content",
            "auto_blocker_recall",
            "limits",
            "required_fact_ids",
            "required_evidence_ids",
            "relevant_failure_ids",
            "relevant_relation_ids",
        }
        _reject_extra(payload, allowed, "ContextRequest")
        required = allowed - {
            "route_id",
            "required_fact_ids",
            "required_evidence_ids",
            "relevant_failure_ids",
            "relevant_relation_ids",
            "expand_proof_content",
            "auto_blocker_recall",
        }
        _require_keys(payload, required, "ContextRequest")
        return cls(
            request_id=payload["request_id"],
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            action_id=payload["action_id"],
            concrete_gap=payload["concrete_gap"],
            output_contract=payload["output_contract"],
            tool_environment_hash=payload["tool_environment_hash"],
            acceptance_policy_hash=payload["acceptance_policy_hash"],
            reference_versions_hash=payload["reference_versions_hash"],
            expand_evidence_content=payload["expand_evidence_content"],
            limits=ContextLimits.from_dict(payload["limits"]),
            required_fact_ids=tuple(payload.get("required_fact_ids") or ()),
            required_evidence_ids=tuple(payload.get("required_evidence_ids") or ()),
            relevant_failure_ids=tuple(payload.get("relevant_failure_ids") or ()),
            relevant_relation_ids=tuple(payload.get("relevant_relation_ids") or ()),
            route_id=payload.get("route_id"),
            expand_proof_content=bool(payload.get("expand_proof_content", False)),
            auto_blocker_recall=bool(payload.get("auto_blocker_recall", True)),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextOmission:
    item_id: str
    item_type: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _require_nonempty("item_id", self.item_id))
        object.__setattr__(
            self, "item_type", _require_nonempty("item_type", self.item_type)
        )
        object.__setattr__(self, "reason", _require_nonempty("reason", self.reason))

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "item_type": self.item_type,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextManifest:
    included_fact_ids: tuple[str, ...]
    included_evidence_ids: tuple[str, ...]
    included_failure_ids: tuple[str, ...]
    included_relation_ids: tuple[str, ...]
    omissions: tuple[ContextOmission, ...]
    measured_chars: int
    measured_bytes: int
    section_order: tuple[str, ...] = _SECTION_ORDER
    section_policy: str = _SECTION_POLICY

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "included_fact_ids",
            _require_id_tuple("included_fact_ids", self.included_fact_ids, hex64=False),
        )
        object.__setattr__(
            self,
            "included_evidence_ids",
            _require_id_tuple("included_evidence_ids", self.included_evidence_ids),
        )
        object.__setattr__(
            self,
            "included_failure_ids",
            _require_id_tuple("included_failure_ids", self.included_failure_ids),
        )
        object.__setattr__(
            self,
            "included_relation_ids",
            _require_id_tuple("included_relation_ids", self.included_relation_ids),
        )
        object.__setattr__(
            self, "omissions", _require_omissions("omissions", self.omissions)
        )
        object.__setattr__(
            self, "measured_chars", _require_nonneg_int("measured_chars", self.measured_chars)
        )
        object.__setattr__(
            self, "measured_bytes", _require_nonneg_int("measured_bytes", self.measured_bytes)
        )
        order = tuple(self.section_order)
        if order != _SECTION_ORDER:
            raise ContextCompilerError("section_order must match compiler section policy")
        object.__setattr__(self, "section_order", _SECTION_ORDER)
        object.__setattr__(
            self, "section_policy", _require_nonempty("section_policy", self.section_policy)
        )

    def render_payload(self, cache_key: ContextCacheKey) -> dict[str, Any]:
        return {
            "included_fact_ids": list(self.included_fact_ids),
            "included_evidence_ids": list(self.included_evidence_ids),
            "included_failure_ids": list(self.included_failure_ids),
            "included_relation_ids": list(self.included_relation_ids),
            "omissions": [item.to_dict() for item in self.omissions],
            "cache_key": cache_key.to_dict(),
            "section_order": list(self.section_order),
            "section_policy": self.section_policy,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "included_fact_ids": list(self.included_fact_ids),
            "included_evidence_ids": list(self.included_evidence_ids),
            "included_failure_ids": list(self.included_failure_ids),
            "included_relation_ids": list(self.included_relation_ids),
            "omissions": [item.to_dict() for item in self.omissions],
            "measured_chars": self.measured_chars,
            "measured_bytes": self.measured_bytes,
            "section_order": list(self.section_order),
            "section_policy": self.section_policy,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextPacket:
    request_id: str
    rendered_text: str
    concrete_gap: str
    output_contract: str
    manifest: ContextManifest
    cache_key: ContextCacheKey
    fact_markdown: tuple[str, ...]
    evidence_bytes: tuple[bytes, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_nonempty("request_id", self.request_id)
        )
        if not isinstance(self.rendered_text, str):
            raise ContextCompilerError("rendered_text must be str")
        object.__setattr__(
            self, "concrete_gap", _require_nonempty("concrete_gap", self.concrete_gap)
        )
        object.__setattr__(
            self,
            "output_contract",
            _require_nonempty("output_contract", self.output_contract),
        )
        if not isinstance(self.manifest, ContextManifest):
            raise ContextCompilerError("manifest must be ContextManifest")
        if not isinstance(self.cache_key, ContextCacheKey):
            raise ContextCompilerError("cache_key must be a ContextCacheKey")
        object.__setattr__(
            self, "fact_markdown", _require_str_tuple("fact_markdown", self.fact_markdown)
        )
        object.__setattr__(
            self, "evidence_bytes", _require_bytes_tuple("evidence_bytes", self.evidence_bytes)
        )

    def rendered_utf8(self) -> bytes:
        return self.rendered_text.encode("utf-8")


class ContextCompiler:
    """Read-only compiler. Never writes cache, view, or artifact stores.

    Automatic historical activation is only the two recall channels in
    ``euler.control.recall``. Do not add an extra digest outside this packet.
    """

    schema_version = _COMPILER_SCHEMA_VERSION
    selection_policy_version = _SELECTION_POLICY_VERSION
    loop_integration = LOOP_INTEGRATION

    def compile(
        self,
        request: ContextRequest,
        view: ResearchView,
        project_snapshot: ProjectSnapshot,
        artifact_store: ArtifactStore,
    ) -> ContextPacket:
        if not isinstance(request, ContextRequest):
            raise ContextCompilerError("request must be a ContextRequest")
        if not isinstance(view, ResearchView):
            raise ContextCompilerError("view must be a ResearchView")
        if not isinstance(project_snapshot, ProjectSnapshot):
            raise ContextCompilerError("project_snapshot must be a ProjectSnapshot")
        if not isinstance(artifact_store, ArtifactStore):
            raise ContextCompilerError("artifact_store must be an ArtifactStore")
        contract, obligation, route = _resolve_graph(request, view)
        facts = _resolve_facts(request, view, project_snapshot, obligation, route)
        evidence = _resolve_evidence(request, view)
        evidence_bytes = _expand_evidence(request, evidence, artifact_store)
        ranked_failures, ranked_relations, base_omissions = _select_optionals(
            request, view, obligation, route
        )
        kept_failures, kept_relations, omissions, rendered, cache_key = _fit_optionals(
            request,
            contract,
            obligation,
            route,
            facts,
            evidence,
            evidence_bytes,
            ranked_failures,
            ranked_relations,
            base_omissions,
        )
        encoded = rendered.encode("utf-8")
        manifest = ContextManifest(
            included_fact_ids=tuple(item.fact_id for item in facts),
            included_evidence_ids=tuple(item.artifact_id for item in evidence),
            included_failure_ids=tuple(item.failure_id for item in kept_failures),
            included_relation_ids=tuple(item.relation_id for item in kept_relations),
            omissions=tuple(omissions),
            measured_chars=len(rendered),
            measured_bytes=len(encoded),
        )
        return ContextPacket(
            request_id=request.request_id,
            rendered_text=rendered,
            concrete_gap=request.concrete_gap,
            output_contract=request.output_contract,
            manifest=manifest,
            cache_key=cache_key,
            fact_markdown=tuple(item.raw_markdown for item in facts),
            evidence_bytes=tuple(evidence_bytes),
        )


def _resolve_graph(
    request: ContextRequest, view: ResearchView
) -> tuple[ProblemContract, Obligation, Route | None]:
    contract = view.get_contract(request.contract_id)
    if contract is None:
        raise MissingRequiredContext(
            f"missing contract {request.contract_id}",
            missing_ids=(request.contract_id,),
        )
    obligation = view.get_obligation(request.obligation_id)
    if obligation is None:
        raise MissingRequiredContext(
            f"missing obligation {request.obligation_id}",
            missing_ids=(request.obligation_id,),
        )
    if obligation.contract_id != request.contract_id:
        raise MissingRequiredContext(
            "obligation contract_id does not match request",
            missing_ids=(request.obligation_id, request.contract_id),
        )
    route = None
    if request.route_id is not None:
        route = view.get_route(request.route_id)
        if route is None:
            raise MissingRequiredContext(
                f"missing route {request.route_id}",
                missing_ids=(request.route_id,),
            )
        if request.obligation_id not in {route.target_obligation_id, *route.required_obligation_ids}:
            raise MissingRequiredContext(
                "request obligation is neither the route target nor an actual route premise",
                missing_ids=(request.route_id, request.obligation_id),
            )
        if route.validity is not ValidityStatus.VALID:
            raise MissingRequiredContext(
                f"route {request.route_id} is stale",
                missing_ids=(request.route_id,),
            )
    return contract, obligation, route


def _resolve_facts(
    request: ContextRequest,
    view: ResearchView,
    snapshot: ProjectSnapshot,
    obligation: Obligation,
    route: Route | None,
) -> tuple[FactSnapshot, ...]:
    roots = infer_dependency_roots(
        snapshot=snapshot,
        view=view,
        obligation=obligation,
        route=route,
        explicit_fact_ids=request.required_fact_ids,
        explicit_evidence_ids=request.required_evidence_ids,
    )
    try:
        return validate_predecessor_closure(roots, snapshot)
    except MissingDependency as exc:
        raise MissingRequiredContext(str(exc), missing_ids=exc.missing_ids) from exc


def _resolve_evidence(request: ContextRequest, view: ResearchView):
    selected = []
    seen: set[str] = set()
    for artifact_id in request.required_evidence_ids:
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        artifact = view.get_evidence(artifact_id)
        if artifact is None:
            raise MissingRequiredContext(
                f"required evidence {artifact_id} is missing",
                missing_ids=(artifact_id,),
            )
        validity = view.evidence_validity(artifact_id) or artifact.validity
        if validity is not ValidityStatus.VALID or artifact.validity is not ValidityStatus.VALID:
            raise MissingRequiredContext(
                f"required evidence {artifact_id} is stale",
                missing_ids=(artifact_id,),
            )
        if artifact.contract_id != request.contract_id:
            raise MissingRequiredContext(
                f"required evidence {artifact_id} contract mismatch",
                missing_ids=(artifact_id,),
            )
        selected.append(artifact)
    return tuple(selected)


def _expand_evidence(
    request: ContextRequest, evidence, artifact_store: ArtifactStore
) -> tuple[bytes, ...]:
    if not request.expand_evidence_content:
        return ()
    blobs: list[bytes] = []
    for artifact in evidence:
        try:
            data = artifact_store.get_bytes(artifact.content_hash)
        except ArtifactNotFound as exc:
            raise MissingRequiredContext(
                f"required evidence bytes {artifact.content_hash} are missing",
                missing_ids=(artifact.artifact_id,),
            ) from exc
        if sha256_hex(data) != artifact.content_hash:
            raise ContextCompilerError(
                f"evidence content hash tamper for {artifact.artifact_id}"
            )
        blobs.append(data)
    return tuple(blobs)


def _select_optionals(
    request: ContextRequest,
    view: ResearchView,
    obligation: Obligation,
    route: Route | None,
) -> tuple[list[FailureRecord], list[Relation], list[ContextOmission]]:
    omissions: list[ContextOmission] = []
    if request.auto_blocker_recall or request.relevant_failure_ids:
        ranked, recall_omissions = select_blockers(
            view,
            obligation,
            route,
            explicit_failure_ids=request.relevant_failure_ids,
        )
        for item in recall_omissions:
            omissions.append(
                ContextOmission(
                    item_id=item.item_id,
                    item_type=item.item_type,
                    reason=item.reason,
                )
            )
        failures = ranked
    else:
        failures = _rank_failures(
            request.relevant_failure_ids, view, obligation, route, omissions
        )
    relations = _rank_relations(
        request.relevant_relation_ids, view, obligation, route, omissions
    )
    fail_cap = request.limits.max_optional_failures.cap()
    rel_cap = request.limits.max_optional_relations.cap()
    kept_failures, omissions = _cap_records(
        failures, fail_cap, "failure", omissions
    )
    kept_relations, omissions = _cap_records(
        relations, rel_cap, "relation", omissions, id_attr="relation_id"
    )
    return kept_failures, kept_relations, omissions


def _fit_optionals(
    request: ContextRequest,
    contract: ProblemContract,
    obligation: Obligation,
    route: Route | None,
    facts: tuple[FactSnapshot, ...],
    evidence,
    evidence_bytes: tuple[bytes, ...],
    failures: list[FailureRecord],
    relations: list[Relation],
    omissions: list[ContextOmission],
) -> tuple[
    tuple[FailureRecord, ...],
    tuple[Relation, ...],
    list[ContextOmission],
    str,
    ContextCacheKey,
]:
    kept_failures = list(failures)
    kept_relations = list(relations)
    size_omissions: list[ContextOmission] = []
    while True:
        text, key = _render_with_key(
            request,
            contract,
            obligation,
            route,
            facts,
            evidence,
            evidence_bytes,
            tuple(kept_failures),
            tuple(kept_relations),
            omissions + size_omissions,
        )
        if _within_size(request, text):
            return (
                tuple(kept_failures),
                tuple(kept_relations),
                omissions + size_omissions,
                text,
                key,
            )
        if kept_relations:
            dropped = kept_relations.pop()
            size_omissions.append(
                ContextOmission(
                    item_id=dropped.relation_id,
                    item_type="relation",
                    reason="over_limit",
                )
            )
            continue
        if kept_failures:
            dropped = kept_failures.pop()
            size_omissions.append(
                ContextOmission(
                    item_id=dropped.failure_id,
                    item_type="failure",
                    reason="over_limit",
                )
            )
            continue
        _raise_if_oversize(request, text, contract, obligation, facts)


def _render_with_key(
    request: ContextRequest,
    contract: ProblemContract,
    obligation: Obligation,
    route: Route | None,
    facts: tuple[FactSnapshot, ...],
    evidence,
    evidence_bytes: tuple[bytes, ...],
    failures: tuple[FailureRecord, ...],
    relations: tuple[Relation, ...],
    omissions: list[ContextOmission],
) -> tuple[str, ContextCacheKey]:
    cache_key = _build_cache_key(
        request, contract, obligation, route, facts, evidence, failures, relations
    )
    manifest = ContextManifest(
        included_fact_ids=tuple(item.fact_id for item in facts),
        included_evidence_ids=tuple(item.artifact_id for item in evidence),
        included_failure_ids=tuple(item.failure_id for item in failures),
        included_relation_ids=tuple(item.relation_id for item in relations),
        omissions=tuple(omissions),
        measured_chars=0,
        measured_bytes=0,
    )
    text = _render_packet(
        request,
        contract,
        obligation,
        route,
        facts,
        evidence,
        evidence_bytes,
        failures,
        relations,
        manifest,
        cache_key,
    )
    return text, cache_key


def _rank_failures(
    ids: tuple[str, ...],
    view: ResearchView,
    obligation: Obligation,
    route: Route | None,
    omissions: list[ContextOmission],
) -> list[FailureRecord]:
    records: list[FailureRecord] = []
    seen: set[str] = set()
    for failure_id in ids:
        if failure_id in seen:
            continue
        seen.add(failure_id)
        record = view.get_failure(failure_id)
        if record is None:
            omissions.append(
                ContextOmission(
                    item_id=failure_id, item_type="failure", reason="missing"
                )
            )
            continue
        if record.scope != obligation.scope:
            omissions.append(
                ContextOmission(
                    item_id=failure_id,
                    item_type="failure",
                    reason="scope_mismatch",
                )
            )
            continue
        if record.contract_id is not None and record.contract_id != obligation.contract_id:
            omissions.append(
                ContextOmission(
                    item_id=failure_id,
                    item_type="failure",
                    reason="contract_mismatch",
                )
            )
            continue
        records.append(record)
    records.sort(key=lambda item: item.failure_id)
    records.sort(key=lambda item: item.created_at, reverse=True)
    records.sort(
        key=lambda item: (
            _link_rank(item.target_obligation_id, item.route_id, obligation, route),
            0 if item.scope == obligation.scope else 1,
            0
            if _token_overlap(
                f"{item.kind.value} {item.exact_failed_claim}", obligation, route
            )
            else 1,
        )
    )
    return records


def _rank_relations(
    ids: tuple[str, ...],
    view: ResearchView,
    obligation: Obligation,
    route: Route | None,
    omissions: list[ContextOmission],
) -> list[Relation]:
    records: list[Relation] = []
    seen: set[str] = set()
    for relation_id in ids:
        if relation_id in seen:
            continue
        seen.add(relation_id)
        record = view.get_relation(relation_id)
        if record is None:
            omissions.append(
                ContextOmission(
                    item_id=relation_id, item_type="relation", reason="missing"
                )
            )
            continue
        if record.validity is not ValidityStatus.VALID:
            omissions.append(
                ContextOmission(
                    item_id=relation_id, item_type="relation", reason="stale"
                )
            )
            continue
        if record.scope != obligation.scope:
            omissions.append(
                ContextOmission(
                    item_id=relation_id,
                    item_type="relation",
                    reason="scope_mismatch",
                )
            )
            continue
        if record.contract_id != obligation.contract_id:
            omissions.append(
                ContextOmission(
                    item_id=relation_id,
                    item_type="relation",
                    reason="contract_mismatch",
                )
            )
            continue
        records.append(record)
    records.sort(key=lambda item: item.relation_id)
    records.sort(
        key=lambda item: (
            _link_rank(item.source, item.target, obligation, route),
            0 if item.scope == obligation.scope else 1,
            0
            if _token_overlap(
                f"{item.relation_type.value} {item.exact_claim}", obligation, route
            )
            else 1,
        )
    )
    return records


def _cap_records(
    records: list,
    cap: int | None,
    item_type: str,
    omissions: list[ContextOmission],
    *,
    id_attr: str = "failure_id",
) -> tuple[list, list[ContextOmission]]:
    if cap is None:
        return records, omissions
    kept = records[:cap]
    for item in records[cap:]:
        omissions.append(
            ContextOmission(
                item_id=getattr(item, id_attr),
                item_type=item_type,
                reason="optional_cap",
            )
        )
    return kept, omissions


def _link_rank(
    left: Optional[str],
    right: Optional[str],
    obligation: Obligation,
    route: Route | None,
) -> int:
    ids = {obligation.obligation_id}
    if route is not None:
        ids.add(route.route_id)
        ids.add(route.target_obligation_id)
    if left in ids or right in ids:
        return 0
    return 1


def _token_overlap(kind: str, obligation: Obligation, route: Route | None) -> bool:
    haystack = set(_tokenize(obligation.exact_statement))
    if route is not None:
        haystack |= set(_tokenize(route.mechanism))
    return bool(set(_tokenize(kind)) & haystack)


def _tokenize(text: str) -> list[str]:
    return [part for part in re.split(r"[^A-Za-z0-9]+", text.casefold()) if part]


def _render_packet(
    request: ContextRequest,
    contract: ProblemContract,
    obligation: Obligation,
    route: Route | None,
    facts: tuple[FactSnapshot, ...],
    evidence,
    evidence_bytes: tuple[bytes, ...],
    failures: tuple[FailureRecord, ...],
    relations: tuple[Relation, ...],
    manifest: ContextManifest,
    cache_key: ContextCacheKey,
) -> str:
    parts: list[str] = []
    parts.append("===== CONTRACT =====")
    parts.append(canonical_json(contract.to_dict()))
    parts.append("===== CONTRACT_STATEMENT =====")
    parts.append(contract.exact_statement)
    parts.append("===== SCOPE =====")
    parts.append(canonical_json(contract.scope.to_dict()))
    parts.append("===== OBLIGATION =====")
    parts.append(canonical_json(obligation.to_dict()))
    parts.append("===== OBLIGATION_STATEMENT =====")
    parts.append(obligation.exact_statement)
    parts.append("===== ROUTE =====")
    if route is not None:
        parts.append(canonical_json(route.to_dict()))
    parts.append("===== FACTS =====")
    interfaces = dependency_interfaces(facts, scope=obligation.scope)
    for fact, interface in zip(facts, interfaces):
        parts.append(render_dependency_interface(interface))
        if request.expand_proof_content:
            parts.append(fact.raw_markdown)
    parts.append("===== EVIDENCE =====")
    for index, artifact in enumerate(evidence):
        parts.append(canonical_json(artifact.to_dict()))
        if request.expand_evidence_content:
            payload = evidence_bytes[index]
            try:
                parts.append(payload.decode("utf-8"))
            except UnicodeDecodeError:
                parts.append(payload.hex())
    parts.append("===== FAILURES =====")
    for failure in failures:
        parts.append(canonical_json(compact_blocker_payload(failure)))
    parts.append("===== RELATIONS =====")
    for relation in relations:
        parts.append(canonical_json(relation.to_dict()))
    parts.append("===== ACTION =====")
    parts.append(request.action_id)
    parts.append(request.concrete_gap)
    parts.append(request.output_contract)
    parts.append("===== MANIFEST =====")
    parts.append(canonical_json(manifest.render_payload(cache_key)))
    return "\n".join(parts)


def _within_size(request: ContextRequest, text: str) -> bool:
    encoded = text.encode("utf-8")
    return request.limits.max_chars.allows(len(text)) and request.limits.max_bytes.allows(
        len(encoded)
    )


def _raise_if_oversize(
    request: ContextRequest,
    text: str,
    contract: ProblemContract,
    obligation: Obligation,
    facts: tuple[FactSnapshot, ...],
) -> None:
    encoded = text.encode("utf-8")
    if request.limits.max_chars.allows(len(text)) and request.limits.max_bytes.allows(
        len(encoded)
    ):
        return
    raise ContextTooLarge(
        "mandatory context exceeds char/byte limits",
        measured_chars=len(text),
        measured_bytes=len(encoded),
        required_ids=(
            (
                contract.contract_id,
                obligation.obligation_id,
                request.action_id,
            )
            + (() if request.route_id is None else (request.route_id,))
            + tuple(item.fact_id for item in facts)
            + tuple(request.required_evidence_ids)
        ),
    )


def _build_cache_key(
    request: ContextRequest,
    contract: ProblemContract,
    obligation: Obligation,
    route: Route | None,
    facts: tuple[FactSnapshot, ...],
    evidence,
    failures: tuple[FailureRecord, ...],
    relations: tuple[Relation, ...],
) -> ContextCacheKey:
    fact_records = [
        {
            "fact_id": item.fact_id,
            "content_sha256": item.content_sha256,
            "predecessors": list(item.predecessors),
        }
        for item in facts
    ]
    evidence_records = [
        {
            "artifact_id": item.artifact_id,
            "content_hash": item.content_hash,
            "environment_hash": item.environment_hash,
            "checker_id": item.checker_id,
            "checker_version": item.checker_version,
            "checker_result": None
            if item.checker_result is None
            else item.checker_result.value,
        }
        for item in evidence
    ]
    return build_context_cache_key(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        action_id=request.action_id,
        definitions=obligation.scope,
        tool_environment_hash=request.tool_environment_hash,
        acceptance_policy_hash=request.acceptance_policy_hash,
        reference_versions_hash=request.reference_versions_hash,
        output_contract=request.output_contract,
        concrete_gap=request.concrete_gap,
        expand_evidence_content=request.expand_evidence_content,
        expand_proof_content=request.expand_proof_content,
        packet_policy={
            "expand_evidence_content": request.expand_evidence_content,
            "expand_proof_content": request.expand_proof_content,
        },
        limits={
            "max_chars": request.limits.max_chars,
            "max_bytes": request.limits.max_bytes,
            "max_failures": request.limits.max_optional_failures,
            "max_relations": request.limits.max_optional_relations,
        },
        facts=fact_records,
        evidence=evidence_records,
        failure_ids=tuple(item.failure_id for item in failures),
        relation_ids=tuple(item.relation_id for item in relations),
        route_id=None if route is None else route.route_id,
        compiler_version=_COMPILER_SCHEMA_VERSION,
        selection_policy_version=_SELECTION_POLICY_VERSION,
    )


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise ContextCompilerError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise ContextCompilerError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise ContextCompilerError(f"{label} missing fields: {sorted(missing)}")


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ContextCompilerError(f"{name} must be a nonempty str")
    return value


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise ContextCompilerError(f"{name} must be 64-char lowercase hex")
    return text


def _optional_hex64(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_hex64(name, value)


def _require_id_tuple(name: str, value: object, *, hex64: bool = True) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ContextCompilerError(f"{name} must be a list of IDs")
    out: list[str] = []
    for index, item in enumerate(value):
        if hex64:
            out.append(_require_hex64(f"{name}[{index}]", item))
        else:
            out.append(_require_nonempty(f"{name}[{index}]", item))
    return tuple(out)


def _require_omissions(name: str, value: object) -> tuple[ContextOmission, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ContextCompilerError(f"{name} must be a list of ContextOmission")
    out: list[ContextOmission] = []
    for index, item in enumerate(value):
        if not isinstance(item, ContextOmission):
            raise ContextCompilerError(f"{name}[{index}] must be a ContextOmission")
        out.append(item)
    return tuple(out)


def _require_str_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ContextCompilerError(f"{name} must be a tuple of str")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ContextCompilerError(f"{name}[{index}] must be str")
        out.append(item)
    return tuple(out)


def _require_bytes_tuple(name: str, value: object) -> tuple[bytes, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ContextCompilerError(f"{name} must be a tuple of bytes")
    out: list[bytes] = []
    for index, item in enumerate(value):
        if not isinstance(item, (bytes, bytearray)):
            raise ContextCompilerError(f"{name}[{index}] must be bytes")
        out.append(bytes(item))
    return tuple(out)


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextCompilerError(f"{name} must be a nonnegative int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise ContextCompilerError(f"{name} has unknown value {value!r}") from None
    raise ContextCompilerError(f"{name} must be {enum_cls.__name__}")
