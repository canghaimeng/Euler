"""Bounded compute records, protocols, and the generic search/check runner.

Stdlib only plus existing research-control types. This module is a research-
evidence producer: it never creates EvidenceArtifact, never writes ResearchView
or EventStore, and never calls a provider. Wall and CPU milliseconds are
observed from runner start through search and checker return, before the
canonical report is written to CAS. They include coordinator overhead and are
cooperative per-point checks, not an OS hard-timeout.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from euler.control.artifacts import (
    ArtifactRef,
    ArtifactStore,
    ArtifactStoreError,
)
from euler.control.contracts import CheckerResult, MathematicalScope
from euler.control.ids import canonical_json, sha256_hex, stable_id
from euler.control.policy import DimensionLimit, PolicyError, ResourceLimitKind
from euler.control.schema import ObservationStatus, ObservedQuantity, UsageRecord

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC = timezone.utc
_ZERO = timedelta(0)
_REQUEST_DOMAIN = "compute-request"
_EVIDENCE_DOMAIN = "compute-evidence"
_ID_VERSION = 1
_ENUMERATION_ORDER = "lexicographic"
_MEDIA_JSON = "application/json"

__all__ = [
    "ComputeError",
    "ComputeArtifactError",
    "ComputeTermination",
    "ComputeEvidenceKind",
    "ComponentIdentity",
    "ComputeLimits",
    "CoverageRecord",
    "CheckerRecord",
    "ComputeRequest",
    "ComputeEvidenceRecord",
    "ComputeRunResult",
    "FiniteCounterexampleSearcher",
    "IndependentCandidateChecker",
    "ComputeRunner",
]


class ComputeError(Exception):
    """Malformed compute record or an illegal runner invocation."""


class ComputeArtifactError(ComputeError):
    """CAS write, read, or verify failed. Never returned as a successful run."""


class ComputeTermination(str, Enum):
    CHECKED_COUNTEREXAMPLE = "CHECKED_COUNTEREXAMPLE"
    DOMAIN_EXHAUSTED = "DOMAIN_EXHAUSTED"
    POINT_LIMIT_REACHED = "POINT_LIMIT_REACHED"
    TIME_LIMIT_REACHED = "TIME_LIMIT_REACHED"
    CHECKER_REJECTED = "CHECKER_REJECTED"
    CHECKER_UNKNOWN = "CHECKER_UNKNOWN"
    ERROR = "ERROR"


class ComputeEvidenceKind(str, Enum):
    CHECKED_COUNTEREXAMPLE_CANDIDATE = "CHECKED_COUNTEREXAMPLE_CANDIDATE"
    BOUNDED_EXHAUSTION_RECORD = "BOUNDED_EXHAUSTION_RECORD"


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentIdentity:
    component_id: str
    version: str
    code_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", _require_nonempty("component_id", self.component_id)
        )
        object.__setattr__(self, "version", _require_nonempty("version", self.version))
        object.__setattr__(
            self, "code_hash", _require_hex64("code_hash", self.code_hash)
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "component_id": self.component_id,
            "version": self.version,
            "code_hash": self.code_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ComponentIdentity":
        payload = _mapping("ComponentIdentity", data)
        _reject_extra(payload, {"component_id", "version", "code_hash"}, "ComponentIdentity")
        _require_keys(payload, {"component_id", "version", "code_hash"}, "ComponentIdentity")
        return cls(
            component_id=payload["component_id"],
            version=payload["version"],
            code_hash=payload["code_hash"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeLimits:
    max_points: DimensionLimit
    max_wall_ms: DimensionLimit

    def __post_init__(self) -> None:
        if not isinstance(self.max_points, DimensionLimit):
            raise ComputeError("max_points must be a DimensionLimit")
        if not isinstance(self.max_wall_ms, DimensionLimit):
            raise ComputeError("max_wall_ms must be a DimensionLimit")
        if (
            self.max_points.kind is ResourceLimitKind.UNLIMITED
            and self.max_wall_ms.kind is ResourceLimitKind.UNLIMITED
        ):
            raise ComputeError("at least one compute limit must be LIMITED")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_points": self.max_points.to_dict(),
            "max_wall_ms": self.max_wall_ms.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "ComputeLimits":
        payload = _mapping("ComputeLimits", data)
        _reject_extra(payload, {"max_points", "max_wall_ms"}, "ComputeLimits")
        _require_keys(payload, {"max_points", "max_wall_ms"}, "ComputeLimits")
        try:
            return cls(
                max_points=DimensionLimit.from_dict(
                    payload["max_points"], label="max_points"
                ),
                max_wall_ms=DimensionLimit.from_dict(
                    payload["max_wall_ms"], label="max_wall_ms"
                ),
            )
        except PolicyError as exc:
            raise ComputeError(str(exc)) from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class CoverageRecord:
    domain_hash: str
    total_points: int
    examined_points: int
    enumeration_order: str
    enumeration_complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "domain_hash", _require_hex64("domain_hash", self.domain_hash)
        )
        object.__setattr__(
            self, "total_points", _require_nonneg_int("total_points", self.total_points)
        )
        object.__setattr__(
            self,
            "examined_points",
            _require_nonneg_int("examined_points", self.examined_points),
        )
        object.__setattr__(
            self,
            "enumeration_order",
            _require_nonempty("enumeration_order", self.enumeration_order),
        )
        if not isinstance(self.enumeration_complete, bool):
            raise ComputeError("enumeration_complete must be bool")
        if self.examined_points > self.total_points:
            raise ComputeError("examined_points must not exceed total_points")
        if self.enumeration_complete and self.examined_points != self.total_points:
            raise ComputeError("complete coverage requires examined_points == total_points")

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain_hash": self.domain_hash,
            "total_points": self.total_points,
            "examined_points": self.examined_points,
            "enumeration_order": self.enumeration_order,
            "enumeration_complete": self.enumeration_complete,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CoverageRecord":
        payload = _mapping("CoverageRecord", data)
        allowed = {
            "domain_hash",
            "total_points",
            "examined_points",
            "enumeration_order",
            "enumeration_complete",
        }
        _reject_extra(payload, allowed, "CoverageRecord")
        _require_keys(payload, allowed, "CoverageRecord")
        return cls(
            domain_hash=payload["domain_hash"],
            total_points=payload["total_points"],
            examined_points=payload["examined_points"],
            enumeration_order=payload["enumeration_order"],
            enumeration_complete=payload["enumeration_complete"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckerRecord:
    checker: ComponentIdentity
    checker_result: CheckerResult
    candidate_content_hash: str
    detail: str
    checked_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.checker, ComponentIdentity):
            raise ComputeError("checker must be a ComponentIdentity")
        object.__setattr__(
            self,
            "checker_result",
            _require_enum(CheckerResult, self.checker_result, "checker_result"),
        )
        object.__setattr__(
            self,
            "candidate_content_hash",
            _require_hex64("candidate_content_hash", self.candidate_content_hash),
        )
        if not isinstance(self.detail, str):
            raise ComputeError("detail must be str")
        _parse_utc("checked_at", self.checked_at)
        object.__setattr__(self, "checked_at", self.checked_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checker": self.checker.to_dict(),
            "checker_result": self.checker_result.value,
            "candidate_content_hash": self.candidate_content_hash,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "CheckerRecord":
        payload = _mapping("CheckerRecord", data)
        allowed = {
            "checker",
            "checker_result",
            "candidate_content_hash",
            "detail",
            "checked_at",
        }
        _reject_extra(payload, allowed, "CheckerRecord")
        _require_keys(payload, allowed, "CheckerRecord")
        return cls(
            checker=ComponentIdentity.from_dict(payload["checker"]),
            checker_result=payload["checker_result"],
            candidate_content_hash=payload["candidate_content_hash"],
            detail=payload["detail"],
            checked_at=payload["checked_at"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeRequest:
    request_id: str
    contract_id: str
    obligation_id: str
    action_id: str
    tested_claim: str
    scope: MathematicalScope
    plugin_id: str
    plugin_version: str
    plugin_payload_json: str
    plugin_payload_hash: str
    domain_hash: str
    declared_total_points: int
    limits: ComputeLimits
    searcher: ComponentIdentity
    checker: ComponentIdentity
    tool_environment_hash: str
    created_at: str

    def __post_init__(self) -> None:
        self.validate()

    @classmethod
    def build(
        cls,
        *,
        contract_id: str,
        obligation_id: str,
        action_id: str,
        tested_claim: str,
        scope: MathematicalScope,
        plugin_id: str,
        plugin_version: str,
        plugin_payload: object,
        domain_hash: str,
        declared_total_points: int,
        limits: ComputeLimits,
        searcher: ComponentIdentity,
        checker: ComponentIdentity,
        tool_environment_hash: str,
        created_at: str,
    ) -> "ComputeRequest":
        payload_json, payload_hash = _canonicalize_plugin_payload(plugin_payload)
        request_id = _request_id(
            contract_id=contract_id,
            obligation_id=obligation_id,
            action_id=action_id,
            tested_claim=tested_claim,
            scope=scope,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            plugin_payload_json=payload_json,
            plugin_payload_hash=payload_hash,
            domain_hash=domain_hash,
            declared_total_points=declared_total_points,
            limits=limits,
            searcher=searcher,
            checker=checker,
            tool_environment_hash=tool_environment_hash,
        )
        return cls(
            request_id=request_id,
            contract_id=contract_id,
            obligation_id=obligation_id,
            action_id=action_id,
            tested_claim=tested_claim,
            scope=scope,
            plugin_id=plugin_id,
            plugin_version=plugin_version,
            plugin_payload_json=payload_json,
            plugin_payload_hash=payload_hash,
            domain_hash=domain_hash,
            declared_total_points=declared_total_points,
            limits=limits,
            searcher=searcher,
            checker=checker,
            tool_environment_hash=tool_environment_hash,
            created_at=created_at,
        )

    def validate(self) -> None:
        object.__setattr__(
            self, "request_id", _require_hex64("request_id", self.request_id)
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
            self, "tested_claim", _require_nonempty("tested_claim", self.tested_claim)
        )
        if not isinstance(self.scope, MathematicalScope):
            raise ComputeError("scope must be a MathematicalScope")
        object.__setattr__(
            self, "plugin_id", _require_nonempty("plugin_id", self.plugin_id)
        )
        object.__setattr__(
            self, "plugin_version", _require_nonempty("plugin_version", self.plugin_version)
        )
        payload_json, payload_hash = _canonicalize_plugin_payload(self.plugin_payload_json)
        if self.plugin_payload_json != payload_json:
            raise ComputeError("plugin_payload_json must already be canonical JSON")
        object.__setattr__(
            self,
            "plugin_payload_hash",
            _require_hex64("plugin_payload_hash", self.plugin_payload_hash),
        )
        if self.plugin_payload_hash != payload_hash:
            raise ComputeError("plugin_payload_hash does not match canonical payload")
        object.__setattr__(
            self, "domain_hash", _require_hex64("domain_hash", self.domain_hash)
        )
        object.__setattr__(
            self,
            "declared_total_points",
            _require_nonneg_int("declared_total_points", self.declared_total_points),
        )
        if not isinstance(self.limits, ComputeLimits):
            raise ComputeError("limits must be ComputeLimits")
        if not isinstance(self.searcher, ComponentIdentity):
            raise ComputeError("searcher must be a ComponentIdentity")
        if not isinstance(self.checker, ComponentIdentity):
            raise ComputeError("checker must be a ComponentIdentity")
        _require_distinct_components(self.searcher, self.checker)
        object.__setattr__(
            self,
            "tool_environment_hash",
            _require_hex64("tool_environment_hash", self.tool_environment_hash),
        )
        _parse_utc("created_at", self.created_at)
        expected = _request_id(
            contract_id=self.contract_id,
            obligation_id=self.obligation_id,
            action_id=self.action_id,
            tested_claim=self.tested_claim,
            scope=self.scope,
            plugin_id=self.plugin_id,
            plugin_version=self.plugin_version,
            plugin_payload_json=self.plugin_payload_json,
            plugin_payload_hash=self.plugin_payload_hash,
            domain_hash=self.domain_hash,
            declared_total_points=self.declared_total_points,
            limits=self.limits,
            searcher=self.searcher,
            checker=self.checker,
            tool_environment_hash=self.tool_environment_hash,
        )
        if self.request_id != expected:
            raise ComputeError("request_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "action_id": self.action_id,
            "tested_claim": self.tested_claim,
            "scope": self.scope.to_dict(),
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "plugin_payload_json": self.plugin_payload_json,
            "plugin_payload_hash": self.plugin_payload_hash,
            "domain_hash": self.domain_hash,
            "declared_total_points": self.declared_total_points,
            "limits": self.limits.to_dict(),
            "searcher": self.searcher.to_dict(),
            "checker": self.checker.to_dict(),
            "tool_environment_hash": self.tool_environment_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ComputeRequest":
        payload = _mapping("ComputeRequest", data)
        allowed = {
            "request_id",
            "contract_id",
            "obligation_id",
            "action_id",
            "tested_claim",
            "scope",
            "plugin_id",
            "plugin_version",
            "plugin_payload_json",
            "plugin_payload_hash",
            "domain_hash",
            "declared_total_points",
            "limits",
            "searcher",
            "checker",
            "tool_environment_hash",
            "created_at",
        }
        _reject_extra(payload, allowed, "ComputeRequest")
        _require_keys(payload, allowed, "ComputeRequest")
        return cls(
            request_id=payload["request_id"],
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            action_id=payload["action_id"],
            tested_claim=payload["tested_claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            plugin_id=payload["plugin_id"],
            plugin_version=payload["plugin_version"],
            plugin_payload_json=payload["plugin_payload_json"],
            plugin_payload_hash=payload["plugin_payload_hash"],
            domain_hash=payload["domain_hash"],
            declared_total_points=payload["declared_total_points"],
            limits=ComputeLimits.from_dict(payload["limits"]),
            searcher=ComponentIdentity.from_dict(payload["searcher"]),
            checker=ComponentIdentity.from_dict(payload["checker"]),
            tool_environment_hash=payload["tool_environment_hash"],
            created_at=payload["created_at"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeEvidenceRecord:
    evidence_id: str
    evidence_kind: ComputeEvidenceKind
    request_id: str
    contract_id: str
    obligation_id: str
    action_id: str
    tested_claim: str
    scope: MathematicalScope
    plugin_payload_hash: str
    domain_hash: str
    coverage: CoverageRecord
    searcher: ComponentIdentity
    environment_hash: str
    report_ref: ArtifactRef
    created_at: str
    checker_record: Optional[CheckerRecord] = None
    candidate_ref: Optional[ArtifactRef] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_id", _require_hex64("evidence_id", self.evidence_id)
        )
        object.__setattr__(
            self,
            "evidence_kind",
            _require_enum(ComputeEvidenceKind, self.evidence_kind, "evidence_kind"),
        )
        object.__setattr__(
            self, "request_id", _require_hex64("request_id", self.request_id)
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
            self, "tested_claim", _require_nonempty("tested_claim", self.tested_claim)
        )
        if not isinstance(self.scope, MathematicalScope):
            raise ComputeError("scope must be a MathematicalScope")
        object.__setattr__(
            self,
            "plugin_payload_hash",
            _require_hex64("plugin_payload_hash", self.plugin_payload_hash),
        )
        object.__setattr__(
            self, "domain_hash", _require_hex64("domain_hash", self.domain_hash)
        )
        if not isinstance(self.coverage, CoverageRecord):
            raise ComputeError("coverage must be a CoverageRecord")
        if self.coverage.domain_hash != self.domain_hash:
            raise ComputeError("coverage domain_hash must match evidence domain_hash")
        if not isinstance(self.searcher, ComponentIdentity):
            raise ComputeError("searcher must be a ComponentIdentity")
        object.__setattr__(
            self,
            "environment_hash",
            _require_hex64("environment_hash", self.environment_hash),
        )
        if not isinstance(self.report_ref, ArtifactRef):
            raise ComputeError("report_ref must be an ArtifactRef")
        _parse_utc("created_at", self.created_at)
        if self.candidate_ref is not None and not isinstance(self.candidate_ref, ArtifactRef):
            raise ComputeError("candidate_ref must be an ArtifactRef")
        if self.checker_record is not None and not isinstance(
            self.checker_record, CheckerRecord
        ):
            raise ComputeError("checker_record must be a CheckerRecord")
        if self.evidence_kind is ComputeEvidenceKind.CHECKED_COUNTEREXAMPLE_CANDIDATE:
            if self.checker_record is None:
                raise ComputeError("checked-candidate evidence requires checker_record")
            if self.checker_record.checker_result is not CheckerResult.PASS:
                raise ComputeError("checked-candidate evidence requires checker PASS")
            if self.candidate_ref is None:
                raise ComputeError("checked-candidate evidence requires candidate_ref")
            if (
                self.checker_record.candidate_content_hash
                != self.candidate_ref.content_hash
            ):
                raise ComputeError(
                    "checker_record candidate hash must match candidate_ref"
                )
            if self.coverage.enumeration_complete:
                raise ComputeError("checked-candidate evidence forbids complete coverage")
        elif self.evidence_kind is ComputeEvidenceKind.BOUNDED_EXHAUSTION_RECORD:
            if self.checker_record is not None:
                raise ComputeError("exhaustion record must not claim checker execution")
            if self.candidate_ref is not None:
                raise ComputeError("exhaustion record forbids candidate_ref")
            if not self.coverage.enumeration_complete:
                raise ComputeError("exhaustion record requires complete coverage")
        expected = _evidence_id_from_record(self)
        if self.evidence_id != expected:
            raise ComputeError("evidence_id does not match semantic fields")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "evidence_kind": self.evidence_kind.value,
            "request_id": self.request_id,
            "contract_id": self.contract_id,
            "obligation_id": self.obligation_id,
            "action_id": self.action_id,
            "tested_claim": self.tested_claim,
            "scope": self.scope.to_dict(),
            "plugin_payload_hash": self.plugin_payload_hash,
            "domain_hash": self.domain_hash,
            "coverage": self.coverage.to_dict(),
            "searcher": self.searcher.to_dict(),
            "checker_record": None
            if self.checker_record is None
            else self.checker_record.to_dict(),
            "environment_hash": self.environment_hash,
            "candidate_ref": _ref_to_dict(self.candidate_ref),
            "report_ref": _ref_to_dict(self.report_ref),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ComputeEvidenceRecord":
        payload = _mapping("ComputeEvidenceRecord", data)
        allowed = {
            "evidence_id",
            "evidence_kind",
            "request_id",
            "contract_id",
            "obligation_id",
            "action_id",
            "tested_claim",
            "scope",
            "plugin_payload_hash",
            "domain_hash",
            "coverage",
            "searcher",
            "checker_record",
            "environment_hash",
            "candidate_ref",
            "report_ref",
            "created_at",
        }
        _reject_extra(payload, allowed, "ComputeEvidenceRecord")
        _require_keys(payload, allowed, "ComputeEvidenceRecord")
        raw_checker = payload["checker_record"]
        raw_candidate = payload["candidate_ref"]
        return cls(
            evidence_id=payload["evidence_id"],
            evidence_kind=payload["evidence_kind"],
            request_id=payload["request_id"],
            contract_id=payload["contract_id"],
            obligation_id=payload["obligation_id"],
            action_id=payload["action_id"],
            tested_claim=payload["tested_claim"],
            scope=MathematicalScope.from_dict(payload["scope"]),
            plugin_payload_hash=payload["plugin_payload_hash"],
            domain_hash=payload["domain_hash"],
            coverage=CoverageRecord.from_dict(payload["coverage"]),
            searcher=ComponentIdentity.from_dict(payload["searcher"]),
            checker_record=None
            if raw_checker is None
            else CheckerRecord.from_dict(raw_checker),
            environment_hash=payload["environment_hash"],
            candidate_ref=_ref_from_dict(raw_candidate),
            report_ref=_ref_from_dict(payload["report_ref"], required=True),
            created_at=payload["created_at"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputeRunResult:
    request_id: str
    termination: ComputeTermination
    coverage: CoverageRecord
    report_ref: ArtifactRef
    usage: UsageRecord
    detail: str
    started_at: str
    ended_at: str
    candidate_ref: Optional[ArtifactRef] = None
    checker_record: Optional[CheckerRecord] = None
    evidence: Optional[ComputeEvidenceRecord] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_hex64("request_id", self.request_id)
        )
        object.__setattr__(
            self,
            "termination",
            _require_enum(ComputeTermination, self.termination, "termination"),
        )
        if not isinstance(self.coverage, CoverageRecord):
            raise ComputeError("coverage must be a CoverageRecord")
        if not isinstance(self.report_ref, ArtifactRef):
            raise ComputeError("report_ref must be an ArtifactRef")
        if not isinstance(self.usage, UsageRecord):
            raise ComputeError("usage must be a UsageRecord")
        if not isinstance(self.detail, str):
            raise ComputeError("detail must be str")
        started = _parse_utc("started_at", self.started_at)
        ended = _parse_utc("ended_at", self.ended_at)
        if ended < started:
            raise ComputeError("ended_at must not precede started_at")
        if self.candidate_ref is not None and not isinstance(self.candidate_ref, ArtifactRef):
            raise ComputeError("candidate_ref must be an ArtifactRef")
        if self.checker_record is not None and not isinstance(
            self.checker_record, CheckerRecord
        ):
            raise ComputeError("checker_record must be a CheckerRecord")
        if self.evidence is not None and not isinstance(
            self.evidence, ComputeEvidenceRecord
        ):
            raise ComputeError("evidence must be a ComputeEvidenceRecord")
        _validate_run_invariants(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "termination": self.termination.value,
            "coverage": self.coverage.to_dict(),
            "candidate_ref": _ref_to_dict(self.candidate_ref),
            "report_ref": _ref_to_dict(self.report_ref),
            "checker_record": None
            if self.checker_record is None
            else self.checker_record.to_dict(),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "usage": self.usage.to_dict(),
            "detail": self.detail,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ComputeRunResult":
        payload = _mapping("ComputeRunResult", data)
        allowed = {
            "request_id",
            "termination",
            "coverage",
            "candidate_ref",
            "report_ref",
            "checker_record",
            "evidence",
            "usage",
            "detail",
            "started_at",
            "ended_at",
        }
        _reject_extra(payload, allowed, "ComputeRunResult")
        _require_keys(payload, allowed, "ComputeRunResult")
        raw_checker = payload["checker_record"]
        raw_evidence = payload["evidence"]
        return cls(
            request_id=payload["request_id"],
            termination=payload["termination"],
            coverage=CoverageRecord.from_dict(payload["coverage"]),
            candidate_ref=_ref_from_dict(payload["candidate_ref"]),
            report_ref=_ref_from_dict(payload["report_ref"], required=True),
            checker_record=None
            if raw_checker is None
            else CheckerRecord.from_dict(raw_checker),
            evidence=None
            if raw_evidence is None
            else ComputeEvidenceRecord.from_dict(raw_evidence),
            usage=UsageRecord.from_dict(payload["usage"]),
            detail=payload["detail"],
            started_at=payload["started_at"],
            ended_at=payload["ended_at"],
        )


@runtime_checkable
class FiniteCounterexampleSearcher(Protocol):
    @property
    def identity(self) -> ComponentIdentity: ...

    def total_points(self, request: ComputeRequest) -> int: ...

    def assignment_at(self, request: ComputeRequest, ordinal: int) -> Mapping[str, int]: ...

    def predicate_holds(
        self, request: ComputeRequest, assignment: Mapping[str, int]
    ) -> bool: ...


@runtime_checkable
class IndependentCandidateChecker(Protocol):
    @property
    def identity(self) -> ComponentIdentity: ...

    def check(
        self, request: ComputeRequest, persisted_candidate_bytes: bytes
    ) -> tuple[CheckerResult, str]: ...


class ComputeRunner:
    """Generic finite-search runner. Does not import a concrete plugin."""

    def run(
        self,
        request: ComputeRequest,
        searcher: FiniteCounterexampleSearcher,
        checker: IndependentCandidateChecker,
        artifact_store: ArtifactStore,
    ) -> ComputeRunResult:
        if not isinstance(request, ComputeRequest):
            raise ComputeError("run requires a ComputeRequest")
        request.validate()
        if not isinstance(searcher, FiniteCounterexampleSearcher):
            raise ComputeError("searcher must satisfy FiniteCounterexampleSearcher")
        if not isinstance(checker, IndependentCandidateChecker):
            raise ComputeError("checker must satisfy IndependentCandidateChecker")
        if not isinstance(artifact_store, ArtifactStore):
            raise ComputeError("artifact_store must be an ArtifactStore")
        _require_runtime_identity("searcher", searcher.identity, request.searcher)
        _require_runtime_identity("checker", checker.identity, request.checker)
        _require_distinct_components(searcher.identity, checker.identity)

        started_at = _utc_now()
        start_mono = time.monotonic()
        start_cpu = time.process_time()

        def wall_ms() -> int:
            elapsed = time.monotonic() - start_mono
            if elapsed < 0:
                return 0
            return int(elapsed * 1000)

        def cpu_ms() -> int:
            elapsed = time.process_time() - start_cpu
            if elapsed < 0:
                return 0
            return int(elapsed * 1000)

        termination = ComputeTermination.ERROR
        detail = "compute run incomplete"
        examined = 0
        complete = False
        candidate_ref: ArtifactRef | None = None
        checker_record: CheckerRecord | None = None
        try:
            try:
                reported_total = searcher.total_points(request)
            except ComputeArtifactError:
                raise
            except Exception as exc:
                termination = ComputeTermination.ERROR
                detail = f"searcher.total_points failed: {exc}"
            else:
                if (
                    isinstance(reported_total, bool)
                    or not isinstance(reported_total, int)
                    or reported_total != request.declared_total_points
                ):
                    termination = ComputeTermination.ERROR
                    detail = "searcher.total_points disagrees with declared_total_points"
                else:
                    termination, detail, examined, complete, candidate_ref, checker_record = (
                        self._enumerate(
                            request,
                            searcher,
                            checker,
                            artifact_store,
                            wall_ms=wall_ms,
                        )
                    )
        except ComputeArtifactError:
            raise

        usage = _observed_usage(wall_ms=wall_ms(), cpu_ms=cpu_ms())
        coverage = CoverageRecord(
            domain_hash=request.domain_hash,
            total_points=request.declared_total_points,
            examined_points=examined,
            enumeration_order=_ENUMERATION_ORDER,
            enumeration_complete=complete,
        )
        search_ended_at = _utc_now()
        report_ref = _persist_report(
            artifact_store,
            request=request,
            termination=termination,
            coverage=coverage,
            candidate_ref=candidate_ref,
            checker_record=checker_record,
            usage=usage,
            detail=detail,
            started_at=started_at,
            ended_at=search_ended_at,
        )
        ended_at = _utc_now()
        evidence = _evidence_for(
            request,
            termination=termination,
            coverage=coverage,
            searcher=request.searcher,
            checker_record=checker_record,
            candidate_ref=candidate_ref,
            report_ref=report_ref,
            created_at=ended_at,
        )
        return ComputeRunResult(
            request_id=request.request_id,
            termination=termination,
            coverage=coverage,
            candidate_ref=candidate_ref,
            report_ref=report_ref,
            checker_record=checker_record,
            evidence=evidence,
            usage=usage,
            detail=detail,
            started_at=started_at,
            ended_at=ended_at,
        )

    def _enumerate(
        self,
        request: ComputeRequest,
        searcher: FiniteCounterexampleSearcher,
        checker: IndependentCandidateChecker,
        artifact_store: ArtifactStore,
        *,
        wall_ms,
    ) -> tuple[
        ComputeTermination,
        str,
        int,
        bool,
        ArtifactRef | None,
        CheckerRecord | None,
    ]:
        total = request.declared_total_points
        for ordinal in range(total):
            limit = _admit_next_point(request.limits, ordinal=ordinal, wall_ms=wall_ms())
            if limit is not None:
                return limit, _limit_detail(limit), ordinal, False, None, None
            try:
                raw_assignment = searcher.assignment_at(request, ordinal)
                assignment = _canonical_assignment(raw_assignment)
                holds = searcher.predicate_holds(request, MappingProxyType(assignment))
            except ComputeArtifactError:
                raise
            except ComputeError as exc:
                return (
                    ComputeTermination.ERROR,
                    str(exc),
                    ordinal,
                    False,
                    None,
                    None,
                )
            except Exception as exc:
                return (
                    ComputeTermination.ERROR,
                    f"searcher failed at ordinal {ordinal}: {exc}",
                    ordinal,
                    False,
                    None,
                    None,
                )
            if not isinstance(holds, bool):
                return (
                    ComputeTermination.ERROR,
                    "predicate_holds must return bool",
                    ordinal,
                    False,
                    None,
                    None,
                )
            if holds:
                continue
            candidate_bytes = canonical_json(assignment).encode("utf-8")
            candidate_ref = _persist_candidate(artifact_store, candidate_bytes)
            persisted = _read_verified_bytes(artifact_store, candidate_ref)
            if persisted != candidate_bytes:
                raise ComputeArtifactError(
                    "persisted candidate bytes do not match the stored assignment"
                )
            checker_record, termination, detail = _run_checker(
                request, checker, persisted, candidate_ref.content_hash
            )
            return termination, detail, ordinal + 1, False, candidate_ref, checker_record
        return (
            ComputeTermination.DOMAIN_EXHAUSTED,
            "declared finite domain examined without a candidate",
            total,
            True,
            None,
            None,
        )


def _admit_next_point(
    limits: ComputeLimits, *, ordinal: int, wall_ms: int
) -> ComputeTermination | None:
    if limits.max_wall_ms.kind is ResourceLimitKind.LIMITED:
        cap = int(limits.max_wall_ms.value or 0)
        if wall_ms >= cap:
            return ComputeTermination.TIME_LIMIT_REACHED
    if limits.max_points.kind is ResourceLimitKind.LIMITED:
        cap = int(limits.max_points.value or 0)
        if ordinal >= cap:
            return ComputeTermination.POINT_LIMIT_REACHED
    return None


def _limit_detail(termination: ComputeTermination) -> str:
    if termination is ComputeTermination.TIME_LIMIT_REACHED:
        return "wall limit reached before the next point"
    return "point limit reached before the next point"


def _run_checker(
    request: ComputeRequest,
    checker: IndependentCandidateChecker,
    persisted: bytes,
    candidate_hash: str,
) -> tuple[CheckerRecord, ComputeTermination, str]:
    checked_at = _utc_now()
    try:
        raw = checker.check(request, persisted)
    except Exception as exc:
        record = CheckerRecord(
            checker=request.checker,
            checker_result=CheckerResult.UNKNOWN,
            candidate_content_hash=candidate_hash,
            detail=f"checker raised: {exc}",
            checked_at=checked_at,
        )
        return record, ComputeTermination.CHECKER_UNKNOWN, record.detail
    result: CheckerResult | None = None
    detail = ""
    if isinstance(raw, tuple) and len(raw) == 2:
        try:
            result = _require_enum(CheckerResult, raw[0], "checker_result")
        except ComputeError:
            result = None
        if isinstance(raw[1], str):
            detail = raw[1]
    if result is None:
        record = CheckerRecord(
            checker=request.checker,
            checker_result=CheckerResult.UNKNOWN,
            candidate_content_hash=candidate_hash,
            detail="checker returned a malformed result",
            checked_at=checked_at,
        )
        return record, ComputeTermination.CHECKER_UNKNOWN, record.detail
    record = CheckerRecord(
        checker=request.checker,
        checker_result=result,
        candidate_content_hash=candidate_hash,
        detail=detail,
        checked_at=checked_at,
    )
    if result is CheckerResult.PASS:
        return (
            record,
            ComputeTermination.CHECKED_COUNTEREXAMPLE,
            "independent checker accepted the persisted candidate",
        )
    if result is CheckerResult.FAIL:
        return (
            record,
            ComputeTermination.CHECKER_REJECTED,
            "independent checker rejected the persisted candidate",
        )
    return (
        record,
        ComputeTermination.CHECKER_UNKNOWN,
        "independent checker returned UNKNOWN",
    )


def _persist_candidate(store: ArtifactStore, data: bytes) -> ArtifactRef:
    try:
        ref = store.put_bytes(data, media_type=_MEDIA_JSON)
        store.verify(ref.content_hash)
        return ref
    except ArtifactStoreError as exc:
        raise ComputeArtifactError(f"failed to persist candidate: {exc}") from exc


def _read_verified_bytes(store: ArtifactStore, ref: ArtifactRef) -> bytes:
    try:
        data = store.get_bytes(ref.content_hash)
        verified = store.verify(ref.content_hash)
    except ArtifactStoreError as exc:
        raise ComputeArtifactError(f"failed to read candidate: {exc}") from exc
    if verified.content_hash != ref.content_hash or len(data) != ref.size_bytes:
        raise ComputeArtifactError("candidate artifact failed integrity verification")
    return data


def _persist_report(
    store: ArtifactStore,
    *,
    request: ComputeRequest,
    termination: ComputeTermination,
    coverage: CoverageRecord,
    candidate_ref: ArtifactRef | None,
    checker_record: CheckerRecord | None,
    usage: UsageRecord,
    detail: str,
    started_at: str,
    ended_at: str,
) -> ArtifactRef:
    payload = {
        "request": request.to_dict(),
        "termination": termination.value,
        "coverage": coverage.to_dict(),
        "candidate_ref": _ref_to_dict(candidate_ref),
        "checker_record": None if checker_record is None else checker_record.to_dict(),
        "usage": usage.to_dict(),
        "detail": detail,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    body = canonical_json(payload).encode("utf-8")
    try:
        ref = store.put_bytes(body, media_type=_MEDIA_JSON)
        store.verify(ref.content_hash)
        return ref
    except ArtifactStoreError as exc:
        raise ComputeArtifactError(f"failed to persist compute report: {exc}") from exc


def _evidence_for(
    request: ComputeRequest,
    *,
    termination: ComputeTermination,
    coverage: CoverageRecord,
    searcher: ComponentIdentity,
    checker_record: CheckerRecord | None,
    candidate_ref: ArtifactRef | None,
    report_ref: ArtifactRef,
    created_at: str,
) -> ComputeEvidenceRecord | None:
    if termination is ComputeTermination.CHECKED_COUNTEREXAMPLE:
        kind = ComputeEvidenceKind.CHECKED_COUNTEREXAMPLE_CANDIDATE
        record_checker = checker_record
        record_candidate = candidate_ref
    elif termination is ComputeTermination.DOMAIN_EXHAUSTED:
        kind = ComputeEvidenceKind.BOUNDED_EXHAUSTION_RECORD
        record_checker = None
        record_candidate = None
    else:
        return None
    evidence_id = _evidence_id(
        evidence_kind=kind,
        request_id=request.request_id,
        contract_id=request.contract_id,
        obligation_id=request.obligation_id,
        action_id=request.action_id,
        tested_claim=request.tested_claim,
        scope=request.scope,
        plugin_payload_hash=request.plugin_payload_hash,
        domain_hash=request.domain_hash,
        coverage=coverage,
        searcher=searcher,
        checker_record=record_checker,
        environment_hash=request.tool_environment_hash,
        candidate_ref=record_candidate,
        report_ref=report_ref,
    )
    return ComputeEvidenceRecord(
        evidence_id=evidence_id,
        evidence_kind=kind,
        request_id=request.request_id,
        contract_id=request.contract_id,
        obligation_id=request.obligation_id,
        action_id=request.action_id,
        tested_claim=request.tested_claim,
        scope=request.scope,
        plugin_payload_hash=request.plugin_payload_hash,
        domain_hash=request.domain_hash,
        coverage=coverage,
        searcher=searcher,
        checker_record=record_checker,
        environment_hash=request.tool_environment_hash,
        candidate_ref=record_candidate,
        report_ref=report_ref,
        created_at=created_at,
    )


def _validate_run_invariants(result: ComputeRunResult) -> None:
    termination = result.termination
    coverage = result.coverage
    if result.checker_record is not None:
        if result.candidate_ref is None:
            raise ComputeError("checker_record requires candidate_ref")
        if (
            result.checker_record.candidate_content_hash
            != result.candidate_ref.content_hash
        ):
            raise ComputeError(
                "checker_record candidate hash must match candidate_ref"
            )
    elif result.candidate_ref is not None:
        raise ComputeError("candidate_ref requires checker_record")
    if result.evidence is not None:
        if result.evidence.request_id != result.request_id:
            raise ComputeError("evidence request_id must match result request_id")
        if result.evidence.coverage != result.coverage:
            raise ComputeError("evidence coverage must match result coverage")
        if result.evidence.report_ref != result.report_ref:
            raise ComputeError("evidence report_ref must match result report_ref")
        if result.evidence.candidate_ref != result.candidate_ref:
            raise ComputeError("evidence candidate_ref must match result candidate_ref")
        if result.evidence.checker_record != result.checker_record:
            raise ComputeError("evidence checker_record must match result checker_record")
    if coverage.enumeration_complete and termination is not ComputeTermination.DOMAIN_EXHAUSTED:
        raise ComputeError("only DOMAIN_EXHAUSTED may set enumeration_complete")
    if termination is ComputeTermination.DOMAIN_EXHAUSTED:
        if not coverage.enumeration_complete:
            raise ComputeError("DOMAIN_EXHAUSTED requires complete coverage")
        if coverage.examined_points != coverage.total_points:
            raise ComputeError("DOMAIN_EXHAUSTED requires examined_points == total_points")
        if result.checker_record is not None:
            raise ComputeError("DOMAIN_EXHAUSTED must not claim checker execution")
        if result.candidate_ref is not None:
            raise ComputeError("DOMAIN_EXHAUSTED forbids a candidate")
        if result.evidence is None:
            raise ComputeError("DOMAIN_EXHAUSTED requires bounded-exhaustion evidence")
        if result.evidence.evidence_kind is not ComputeEvidenceKind.BOUNDED_EXHAUSTION_RECORD:
            raise ComputeError("DOMAIN_EXHAUSTED evidence kind mismatch")
        return
    if coverage.enumeration_complete:
        raise ComputeError("incomplete termination forbids complete coverage")
    if termination is ComputeTermination.CHECKED_COUNTEREXAMPLE:
        if result.candidate_ref is None or result.checker_record is None:
            raise ComputeError("CHECKED_COUNTEREXAMPLE requires candidate and checker_record")
        if result.checker_record.checker_result is not CheckerResult.PASS:
            raise ComputeError("CHECKED_COUNTEREXAMPLE requires checker PASS")
        if result.evidence is None:
            raise ComputeError("CHECKED_COUNTEREXAMPLE requires checked-candidate evidence")
        if (
            result.evidence.evidence_kind
            is not ComputeEvidenceKind.CHECKED_COUNTEREXAMPLE_CANDIDATE
        ):
            raise ComputeError("CHECKED_COUNTEREXAMPLE evidence kind mismatch")
        return
    if result.evidence is not None:
        raise ComputeError(f"{termination.value} must set evidence=None")
    if termination is ComputeTermination.CHECKER_REJECTED:
        if (
            result.checker_record is None
            or result.checker_record.checker_result is not CheckerResult.FAIL
        ):
            raise ComputeError("CHECKER_REJECTED requires checker FAIL")
    if termination is ComputeTermination.CHECKER_UNKNOWN:
        if (
            result.checker_record is None
            or result.checker_record.checker_result is not CheckerResult.UNKNOWN
        ):
            raise ComputeError("CHECKER_UNKNOWN requires checker UNKNOWN")
    if termination in {
        ComputeTermination.POINT_LIMIT_REACHED,
        ComputeTermination.TIME_LIMIT_REACHED,
        ComputeTermination.ERROR,
    }:
        if result.candidate_ref is not None or result.checker_record is not None:
            raise ComputeError(
                f"{termination.value} forbids candidate/checker records"
            )


def _observed_usage(*, wall_ms: int, cpu_ms: int) -> UsageRecord:
    zero_tokens = ObservedQuantity(
        unit="tokens", status=ObservationStatus.OBSERVED, value=0
    )
    return UsageRecord(
        input_tokens=zero_tokens,
        output_tokens=zero_tokens,
        reasoning_tokens=zero_tokens,
        cached_tokens=zero_tokens,
        cost_microusd=ObservedQuantity(
            unit="microusd", status=ObservationStatus.OBSERVED, value=0
        ),
        wall_ms=ObservedQuantity(
            unit="ms",
            status=ObservationStatus.OBSERVED,
            value=_require_nonneg_int("wall_ms", wall_ms),
        ),
        tool_cpu_ms=ObservedQuantity(
            unit="ms",
            status=ObservationStatus.OBSERVED,
            value=_require_nonneg_int("tool_cpu_ms", cpu_ms),
        ),
        tool_gpu_ms=ObservedQuantity(
            unit="ms", status=ObservationStatus.OBSERVED, value=0
        ),
    )


def _canonicalize_plugin_payload(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ComputeError("plugin payload must be JSON") from exc
    elif isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        parsed = dict(value)
    else:
        raise ComputeError("plugin payload must be a JSON object or canonical JSON string")
    if not isinstance(parsed, dict):
        raise ComputeError("plugin payload must be a JSON object")
    _reject_floats(parsed, "plugin_payload")
    try:
        encoded = canonical_json(parsed)
    except (TypeError, ValueError) as exc:
        raise ComputeError(f"plugin payload is not JSON-safe: {exc}") from exc
    return encoded, sha256_hex(encoded)


def _request_id(
    *,
    contract_id: object,
    obligation_id: object,
    action_id: object,
    tested_claim: object,
    scope: object,
    plugin_id: object,
    plugin_version: object,
    plugin_payload_json: str,
    plugin_payload_hash: str,
    domain_hash: object,
    declared_total_points: object,
    limits: ComputeLimits,
    searcher: ComponentIdentity,
    checker: ComponentIdentity,
    tool_environment_hash: object,
) -> str:
    if not isinstance(scope, MathematicalScope):
        raise ComputeError("scope must be a MathematicalScope")
    if not isinstance(limits, ComputeLimits):
        raise ComputeError("limits must be ComputeLimits")
    payload = {
        "contract_id": _require_hex64("contract_id", contract_id),
        "obligation_id": _require_hex64("obligation_id", obligation_id),
        "action_id": _require_hex64("action_id", action_id),
        "tested_claim": _require_nonempty("tested_claim", tested_claim),
        "scope": scope.to_dict(),
        "plugin_id": _require_nonempty("plugin_id", plugin_id),
        "plugin_version": _require_nonempty("plugin_version", plugin_version),
        "plugin_payload_json": plugin_payload_json,
        "plugin_payload_hash": plugin_payload_hash,
        "domain_hash": _require_hex64("domain_hash", domain_hash),
        "declared_total_points": _require_nonneg_int(
            "declared_total_points", declared_total_points
        ),
        "limits": limits.to_dict(),
        "searcher": searcher.to_dict(),
        "checker": checker.to_dict(),
        "tool_environment_hash": _require_hex64(
            "tool_environment_hash", tool_environment_hash
        ),
    }
    return stable_id(_REQUEST_DOMAIN, _ID_VERSION, canonical_json(payload))


def _evidence_id_from_record(record: ComputeEvidenceRecord) -> str:
    return _evidence_id(
        evidence_kind=record.evidence_kind,
        request_id=record.request_id,
        contract_id=record.contract_id,
        obligation_id=record.obligation_id,
        action_id=record.action_id,
        tested_claim=record.tested_claim,
        scope=record.scope,
        plugin_payload_hash=record.plugin_payload_hash,
        domain_hash=record.domain_hash,
        coverage=record.coverage,
        searcher=record.searcher,
        checker_record=record.checker_record,
        environment_hash=record.environment_hash,
        candidate_ref=record.candidate_ref,
        report_ref=record.report_ref,
    )


def _evidence_id(
    *,
    evidence_kind: ComputeEvidenceKind,
    request_id: str,
    contract_id: str,
    obligation_id: str,
    action_id: str,
    tested_claim: str,
    scope: MathematicalScope,
    plugin_payload_hash: str,
    domain_hash: str,
    coverage: CoverageRecord,
    searcher: ComponentIdentity,
    checker_record: CheckerRecord | None,
    environment_hash: str,
    candidate_ref: ArtifactRef | None,
    report_ref: ArtifactRef,
) -> str:
    payload = {
        "evidence_kind": evidence_kind.value,
        "request_id": request_id,
        "contract_id": contract_id,
        "obligation_id": obligation_id,
        "action_id": action_id,
        "tested_claim": tested_claim,
        "scope": scope.to_dict(),
        "plugin_payload_hash": plugin_payload_hash,
        "domain_hash": domain_hash,
        "coverage": coverage.to_dict(),
        "searcher": searcher.to_dict(),
        "checker_record": None if checker_record is None else checker_record.to_dict(),
        "environment_hash": environment_hash,
        "candidate_ref": _ref_to_dict(candidate_ref),
        "report_ref": _ref_to_dict(report_ref),
    }
    return stable_id(_EVIDENCE_DOMAIN, _ID_VERSION, canonical_json(payload))


def _canonical_assignment(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or isinstance(value, (str, bytes)):
        raise ComputeError("assignment must be a mapping of str to exact int")
    out: dict[str, int] = {}
    for key, raw in value.items():
        name = _require_nonempty("assignment key", key)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ComputeError(
                f"assignment[{name!r}] must be an exact int, not {type(raw).__name__}"
            )
        out[name] = raw
    try:
        encoded = canonical_json(out)
    except (TypeError, ValueError) as exc:
        raise ComputeError(f"assignment is not JSON-safe: {exc}") from exc
    loaded = json.loads(encoded)
    if not isinstance(loaded, dict):
        raise ComputeError("assignment must canonicalize to a JSON object")
    return {str(key): int(item) for key, item in loaded.items()}


def _require_distinct_components(
    searcher: ComponentIdentity, checker: ComponentIdentity
) -> None:
    if searcher.component_id == checker.component_id:
        raise ComputeError("searcher and checker component_id must differ")
    if searcher.code_hash == checker.code_hash:
        raise ComputeError("searcher and checker code_hash must differ")


def _require_runtime_identity(
    label: str, runtime: object, expected: ComponentIdentity
) -> None:
    if not isinstance(runtime, ComponentIdentity):
        raise ComputeError(f"{label}.identity must be a ComponentIdentity")
    if runtime != expected:
        raise ComputeError(f"{label} runtime identity does not match the request")


def _reject_floats(value: object, path: str) -> None:
    if isinstance(value, float) and not isinstance(value, bool):
        raise ComputeError(f"{path} must not contain float")
    if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        for key, item in value.items():
            _reject_floats(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")


def _ref_to_dict(ref: ArtifactRef | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return {
        "content_hash": ref.content_hash,
        "size_bytes": ref.size_bytes,
        "media_type": ref.media_type,
    }


def _ref_from_dict(data: object, *, required: bool = False) -> ArtifactRef | None:
    if data is None:
        if required:
            raise ComputeError("artifact ref is required")
        return None
    payload = _mapping("ArtifactRef", data)
    _reject_extra(payload, {"content_hash", "size_bytes", "media_type"}, "ArtifactRef")
    _require_keys(payload, {"content_hash", "size_bytes"}, "ArtifactRef")
    try:
        return ArtifactRef(
            content_hash=payload["content_hash"],
            size_bytes=payload["size_bytes"],
            media_type=payload.get("media_type"),
        )
    except ArtifactStoreError as exc:
        raise ComputeError(str(exc)) from exc


def _utc_now() -> str:
    return datetime.now(_UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(name: str, value: object) -> datetime:
    if not isinstance(value, str) or value == "":
        raise ComputeError(f"{name} must be ISO-8601 UTC")
    parsed = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError as exc:
        raise ComputeError(f"{name} must be ISO-8601 UTC") from exc
    if dt.tzinfo is None or dt.utcoffset() != _ZERO:
        raise ComputeError(f"{name} must be UTC")
    return dt.astimezone(_UTC)


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ComputeError(f"{name} must be a nonempty str")
    return value


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise ComputeError(f"{name} must be 64-char lowercase hex")
    return text


def _require_nonneg_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComputeError(f"{name} must be a nonnegative int, not {type(value).__name__}")
    if value < 0:
        raise ComputeError(f"{name} must be a nonnegative int")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise ComputeError(f"{name} has unknown value {value!r}") from None
    raise ComputeError(f"{name} must be {enum_cls.__name__}")


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise ComputeError(f"{label} must be a JSON object")
    return data


def _reject_extra(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    extra = set(payload) - allowed
    if extra:
        raise ComputeError(f"{label} has unknown fields: {sorted(extra)}")


def _require_keys(payload: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(payload)
    if missing:
        raise ComputeError(f"{label} missing fields: {sorted(missing)}")
