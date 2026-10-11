"""Exact action-attempt index and conservative candidate recall.

Stdlib only. Infrastructure retry is distinct from explicit new sampling.
Candidate normalization is NFKC/casefold/whitespace only; matches are advisory
and never merge IDs or increment recurrence strikes.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, Optional, Sequence

from euler.control.contracts import MathematicalScope
from euler.control.progress import ActionSpec, SamplingMode
from euler.control.schema import EventEnvelope, InfrastructureStatus, SchemaValidationError

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE = frozenset({InfrastructureStatus.PENDING, InfrastructureStatus.RUNNING})
_INFRA_FAIL = frozenset(
    {
        InfrastructureStatus.FAILED,
        InfrastructureStatus.TIMED_OUT,
        InfrastructureStatus.CANCELLED,
    }
)
_TERMINAL = _INFRA_FAIL | {InfrastructureStatus.SUCCEEDED}
_DEFAULT_TOKEN_OVERLAP_THRESHOLD = 0.5

_LEASE_ISSUED = "runtime.lease_issued"
_ATTEMPT_STARTED = "runtime.attempt_started"
_LEASE_RUNNING = "runtime.lease_running"
_ATTEMPT_FINISHED = "runtime.attempt_finished"
_LEASE_COMPLETED = "runtime.lease_completed"
_LEASE_INFRA_FAILED = "runtime.lease_infra_failed"
_LEASE_CANCELLED = "runtime.lease_cancelled"
_LEASE_EXPIRED = "runtime.lease_expired"
_CANDIDATE_PROPOSED = "euler.candidate_proposed"
_DISPATCH_INTENT = "euler.dispatch_intent"
_DISPATCH_READY = "euler.dispatch_ready"

__all__ = [
    "AttemptDisposition",
    "DedupeDecision",
    "CandidateMatchKind",
    "ActionAttemptRecord",
    "CandidateMatch",
    "ActionAttemptIndex",
    "normalize_candidate_text",
    "recall_candidates",
    "build_action_attempt_index",
    "ingest_runtime_lease",
]


class AttemptDisposition(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    INFRA_FAILED = "INFRA_FAILED"


class DedupeDecision(str, Enum):
    ISSUE = "ISSUE"
    WAIT_FOR_EXISTING = "WAIT_FOR_EXISTING"
    RECOVER_OR_REUSE = "RECOVER_OR_REUSE"
    RETRY_TRANSPORT = "RETRY_TRANSPORT"
    ALLOW_NEW_SAMPLE = "ALLOW_NEW_SAMPLE"


class CandidateMatchKind(str, Enum):
    ADVISORY = "ADVISORY"
    TOKEN_OVERLAP = "TOKEN_OVERLAP"
    SCOPE_DIFFERENT = "SCOPE_DIFFERENT"
    DISTINCT = "DISTINCT"


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionAttemptRecord:
    attempt_id: str
    action_id: str
    sampling_nonce: Optional[str]
    sampling_mode: SamplingMode
    infrastructure_status: InfrastructureStatus
    artifact_refs: tuple[str, ...] = ()
    result: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt_id", _require_hex64("attempt_id", self.attempt_id))
        object.__setattr__(self, "action_id", _require_hex64("action_id", self.action_id))
        nonce = self.sampling_nonce
        if nonce is not None:
            if not isinstance(nonce, str) or nonce == "":
                raise SchemaValidationError("sampling_nonce must be nonempty str when present")
        object.__setattr__(
            self,
            "sampling_mode",
            _require_enum(SamplingMode, self.sampling_mode, "sampling_mode"),
        )
        status = _require_enum(
            InfrastructureStatus, self.infrastructure_status, "infrastructure_status"
        )
        if status is InfrastructureStatus.UNKNOWN:
            raise SchemaValidationError("infrastructure_status UNKNOWN is not allowed")
        object.__setattr__(self, "infrastructure_status", status)
        refs = self.artifact_refs
        if isinstance(refs, str) or not isinstance(refs, (list, tuple)):
            raise SchemaValidationError("artifact_refs must be a list of 64-hex hashes")
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(_require_hex64(f"artifact_refs[{i}]", item) for i, item in enumerate(refs)),
        )
        result = self.result
        if result is not None and (not isinstance(result, str) or result == ""):
            raise SchemaValidationError("result must be nonempty str when present")

    def disposition(self) -> AttemptDisposition:
        if self.infrastructure_status is InfrastructureStatus.PENDING:
            return AttemptDisposition.PENDING
        if self.infrastructure_status is InfrastructureStatus.RUNNING:
            return AttemptDisposition.RUNNING
        if self.infrastructure_status is InfrastructureStatus.SUCCEEDED:
            return AttemptDisposition.SUCCEEDED
        if self.infrastructure_status in _INFRA_FAIL:
            return AttemptDisposition.INFRA_FAILED
        raise SchemaValidationError(
            f"unsupported infrastructure status {self.infrastructure_status}"
        )

    def has_recovered_payload(self) -> bool:
        return bool(self.artifact_refs) or bool(self.result)


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateMatch:
    candidate_id: str
    query_text: str
    candidate_text: str
    kind: CandidateMatchKind
    scope_equal: bool
    text_equal: bool
    token_similarity: float = 0.0
    merges: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or self.candidate_id == "":
            raise SchemaValidationError("candidate_id must be a nonempty str")
        if not isinstance(self.query_text, str):
            raise SchemaValidationError("query_text must be str")
        if not isinstance(self.candidate_text, str):
            raise SchemaValidationError("candidate_text must be str")
        object.__setattr__(
            self, "kind", _require_enum(CandidateMatchKind, self.kind, "kind")
        )
        if not isinstance(self.scope_equal, bool):
            raise SchemaValidationError("scope_equal must be bool")
        if not isinstance(self.text_equal, bool):
            raise SchemaValidationError("text_equal must be bool")
        object.__setattr__(
            self,
            "token_similarity",
            _require_unit_interval("token_similarity", self.token_similarity),
        )
        if self.merges is not False:
            raise SchemaValidationError("CandidateMatch.merges must be False")
        object.__setattr__(self, "merges", False)


class ActionAttemptIndex:
    """In-memory exact-action index. Does not mutate ResearchView."""

    def __init__(self) -> None:
        self._by_attempt: dict[str, ActionAttemptRecord] = {}
        self._by_key: dict[tuple[str, str], list[str]] = {}

    def start(self, spec: ActionSpec, attempt_id: str) -> ActionAttemptRecord:
        if not isinstance(spec, ActionSpec):
            raise SchemaValidationError("start requires an ActionSpec")
        spec.validate()
        _require_hex64("attempt_id", attempt_id)
        if attempt_id in self._by_attempt:
            raise SchemaValidationError(f"attempt {attempt_id} already exists")
        key = spec.sample_key()
        latest = self._latest(key)
        if latest is not None:
            if latest.infrastructure_status in _ACTIVE:
                raise SchemaValidationError(
                    "cannot start; matching attempt is still pending"
                )
            if latest.infrastructure_status is InfrastructureStatus.SUCCEEDED:
                raise SchemaValidationError(
                    "cannot start; identical action+nonce already succeeded"
                )
            if spec.sampling_mode is SamplingMode.EXPLICIT_NEW:
                raise SchemaValidationError(
                    "cannot start; explicit nonce already has a terminal attempt"
                )
            if latest.has_recovered_payload():
                raise SchemaValidationError(
                    "cannot start; recovered artifacts exist for this action+nonce"
                )
        record = ActionAttemptRecord(
            attempt_id=attempt_id,
            action_id=spec.action_id,
            sampling_nonce=spec.sampling_nonce,
            sampling_mode=spec.sampling_mode,
            infrastructure_status=InfrastructureStatus.PENDING,
        )
        self._by_attempt[attempt_id] = record
        self._by_key.setdefault(key, []).append(attempt_id)
        return record

    def finish(
        self,
        attempt_id: str,
        *,
        infrastructure_status: InfrastructureStatus,
        artifact_refs: Sequence[str] = (),
        result: Optional[str] = None,
    ) -> ActionAttemptRecord:
        record = self._by_attempt.get(attempt_id)
        if record is None:
            raise SchemaValidationError(f"unknown attempt {attempt_id}")
        status = infrastructure_status
        if not isinstance(status, InfrastructureStatus):
            try:
                status = InfrastructureStatus(status)
            except ValueError as exc:
                raise SchemaValidationError("invalid infrastructure status") from exc
        if status is InfrastructureStatus.UNKNOWN:
            raise SchemaValidationError("infrastructure_status UNKNOWN is not allowed")
        current = record.infrastructure_status
        if current in _TERMINAL:
            if status in _ACTIVE:
                raise SchemaValidationError("terminal attempt cannot be overwritten")
            refs = tuple(artifact_refs)
            for index, item in enumerate(refs):
                _require_hex64(f"artifact_refs[{index}]", item)
            if result is not None and (not isinstance(result, str) or result == ""):
                raise SchemaValidationError("result must be nonempty str when present")
            updated = replace(
                record,
                infrastructure_status=status,
                artifact_refs=refs,
                result=result,
            )
            if updated == record:
                return record
            raise SchemaValidationError("terminal attempt cannot be overwritten")
        if status in _ACTIVE:
            if current is InfrastructureStatus.RUNNING and status is InfrastructureStatus.PENDING:
                raise SchemaValidationError("cannot transition RUNNING to PENDING")
            if current is status:
                return record
            if current is InfrastructureStatus.PENDING and status is InfrastructureStatus.RUNNING:
                updated = replace(record, infrastructure_status=status)
                self._by_attempt[attempt_id] = updated
                return updated
            raise SchemaValidationError("illegal attempt transition")
        refs = tuple(artifact_refs)
        for index, item in enumerate(refs):
            _require_hex64(f"artifact_refs[{index}]", item)
        if result is not None and (not isinstance(result, str) or result == ""):
            raise SchemaValidationError("result must be nonempty str when present")
        updated = replace(
            record,
            infrastructure_status=status,
            artifact_refs=refs,
            result=result,
        )
        self._by_attempt[attempt_id] = updated
        return updated

    def ingest(self, record: ActionAttemptRecord) -> ActionAttemptRecord:
        """Replay a historical attempt without going through start().

        Used to rebuild the index from runtime events/leases. Identical
        terminal replay is idempotent.
        """
        if not isinstance(record, ActionAttemptRecord):
            raise SchemaValidationError("ingest requires an ActionAttemptRecord")
        existing = self._by_attempt.get(record.attempt_id)
        if existing is not None:
            if existing == record:
                return existing
            if existing.infrastructure_status in _TERMINAL and record != existing:
                raise SchemaValidationError("terminal attempt cannot be overwritten")
        self._by_attempt[record.attempt_id] = record
        key = (record.action_id, record.sampling_nonce or "")
        ids = self._by_key.setdefault(key, [])
        if record.attempt_id not in ids:
            ids.append(record.attempt_id)
        return record

    def records(self) -> tuple[ActionAttemptRecord, ...]:
        return tuple(self._by_attempt[key] for key in sorted(self._by_attempt))

    def decide(self, spec: ActionSpec) -> DedupeDecision:
        if not isinstance(spec, ActionSpec):
            raise SchemaValidationError("decide requires an ActionSpec")
        spec.validate()
        key = spec.sample_key()
        existing = self._latest(key)
        if spec.sampling_mode is SamplingMode.EXPLICIT_NEW:
            if existing is None:
                return DedupeDecision.ALLOW_NEW_SAMPLE
            return _decision_for_record(existing)
        if existing is not None:
            return _decision_for_record(existing)
        pending = self._pending_for_action(spec.action_id)
        if pending is not None:
            return DedupeDecision.WAIT_FOR_EXISTING
        recovered = self._recovered_for_action(spec.action_id)
        if recovered is not None:
            return DedupeDecision.RECOVER_OR_REUSE
        failed = self._infra_fail_without_payload(spec.action_id)
        if failed is not None:
            return DedupeDecision.RETRY_TRANSPORT
        return DedupeDecision.ISSUE

    def recovered_record(self, spec: ActionSpec) -> ActionAttemptRecord | None:
        """Succeeded or payload-bearing attempt for an exact action+nonce."""
        if not isinstance(spec, ActionSpec):
            raise SchemaValidationError("recovered_record requires an ActionSpec")
        spec.validate()
        latest = self._latest(spec.sample_key())
        if latest is not None and (
            latest.infrastructure_status is InfrastructureStatus.SUCCEEDED
            or latest.has_recovered_payload()
        ):
            return latest
        return self._recovered_for_action(spec.action_id)

    def _latest(self, key: tuple[str, str]) -> ActionAttemptRecord | None:
        ids = self._by_key.get(key) or ()
        if not ids:
            return None
        return self._by_attempt[ids[-1]]

    def _records_for_action(self, action_id: str) -> list[ActionAttemptRecord]:
        return [
            record
            for record in self._by_attempt.values()
            if record.action_id == action_id
        ]

    def _pending_for_action(self, action_id: str) -> ActionAttemptRecord | None:
        for record in self._records_for_action(action_id):
            if record.infrastructure_status in _ACTIVE:
                return record
        return None

    def _recovered_for_action(self, action_id: str) -> ActionAttemptRecord | None:
        for record in self._records_for_action(action_id):
            if record.infrastructure_status is InfrastructureStatus.SUCCEEDED:
                return record
            if record.infrastructure_status in _INFRA_FAIL and record.has_recovered_payload():
                return record
        return None

    def _infra_fail_without_payload(self, action_id: str) -> ActionAttemptRecord | None:
        for record in self._records_for_action(action_id):
            if (
                record.infrastructure_status in _INFRA_FAIL
                and not record.has_recovered_payload()
            ):
                return record
        return None


def ingest_runtime_lease(
    index: ActionAttemptIndex,
    lease: object,
    *,
    sampling_nonce: Optional[str] = None,
    sampling_mode: SamplingMode = SamplingMode.DEFAULT,
) -> ActionAttemptRecord:
    """Project a runtime lease mapping or object onto the attempt index."""
    payload = _lease_payload(lease)
    attempt_id = _require_hex64("attempt_id", payload.get("attempt_id"))
    action_id = _require_hex64("action_id", payload.get("action_id"))
    status = payload.get("infrastructure_status")
    refs = payload.get("artifact_refs") or ()
    nonce = sampling_nonce
    if nonce is None:
        raw_nonce = payload.get("sampling_nonce")
        nonce = raw_nonce if isinstance(raw_nonce, str) and raw_nonce else None
    record = ActionAttemptRecord(
        attempt_id=attempt_id,
        action_id=action_id,
        sampling_nonce=nonce,
        sampling_mode=sampling_mode,
        infrastructure_status=status,
        artifact_refs=tuple(refs),
        result=payload.get("result"),
    )
    return index.ingest(record)


def build_action_attempt_index(
    *,
    events: Sequence[object] = (),
    leases: Sequence[object] = (),
) -> ActionAttemptIndex:
    """Rebuild the exact-action index from runtime events and/or leases.

    Parent should call this from controller/reconcile before
    ``TransparentScheduler.select`` so completed exact actions are reused,
    pending work is not duplicated, explicit new samples remain allowed, and
    transport retries stay distinct from mathematical resampling.
    """
    index = ActionAttemptIndex()
    specs: dict[str, ActionSpec] = {}
    for item in events:
        envelope, payload, event_type = _event_parts(item)
        _ = envelope
        spec = _action_spec_from_payload(payload)
        if spec is not None:
            specs[spec.action_id] = spec
        if event_type in {
            _LEASE_ISSUED,
            _ATTEMPT_STARTED,
            _LEASE_RUNNING,
            _ATTEMPT_FINISHED,
            _LEASE_COMPLETED,
            _LEASE_INFRA_FAILED,
            _LEASE_CANCELLED,
            _LEASE_EXPIRED,
        }:
            lease = payload.get("lease")
            if lease is None and event_type == _ATTEMPT_STARTED:
                lease = {
                    "attempt_id": payload.get("attempt_id"),
                    "action_id": payload.get("action_id"),
                    "infrastructure_status": InfrastructureStatus.PENDING.value,
                    "artifact_refs": (),
                }
            if lease is None:
                continue
            action_id = None
            if isinstance(lease, Mapping):
                action_id = lease.get("action_id")
            spec = specs.get(action_id) if isinstance(action_id, str) else None
            ingest_runtime_lease(
                index,
                lease,
                sampling_nonce=None if spec is None else spec.sampling_nonce,
                sampling_mode=SamplingMode.DEFAULT if spec is None else spec.sampling_mode,
            )
    for lease in leases:
        payload = _lease_payload(lease)
        action_id = payload.get("action_id")
        spec = specs.get(action_id) if isinstance(action_id, str) else None
        ingest_runtime_lease(
            index,
            payload,
            sampling_nonce=None if spec is None else spec.sampling_nonce,
            sampling_mode=SamplingMode.DEFAULT if spec is None else spec.sampling_mode,
        )
    return index


def _lease_payload(lease: object) -> dict:
    if hasattr(lease, "to_dict") and callable(lease.to_dict):
        payload = lease.to_dict()
        if not isinstance(payload, dict):
            raise SchemaValidationError("lease.to_dict must return a mapping")
        return payload
    if isinstance(lease, Mapping) and not isinstance(lease, (str, bytes)):
        return dict(lease)
    raise SchemaValidationError("lease must be a mapping or lease record")


def _event_parts(item: object) -> tuple[EventEnvelope | None, dict, str]:
    if isinstance(item, tuple) and len(item) == 2:
        item = item[1]
    if isinstance(item, EventEnvelope):
        payload = item.payload
        if not isinstance(payload, Mapping):
            return item, {}, item.event_type
        return item, dict(payload), item.event_type
    if isinstance(item, Mapping) and not isinstance(item, (str, bytes)):
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            return None, dict(payload), str(item.get("event_type") or "")
        return None, dict(item), str(item.get("event_type") or "")
    raise SchemaValidationError("event must be an EventEnvelope or mapping")


def _action_spec_from_payload(payload: Mapping[str, object]) -> ActionSpec | None:
    candidate = payload.get("candidate")
    if isinstance(candidate, Mapping):
        spec_payload = candidate.get("action_spec")
        if isinstance(spec_payload, Mapping):
            try:
                return ActionSpec.from_dict(spec_payload)
            except SchemaValidationError:
                return None
        nested = candidate.get("candidate")
        if isinstance(nested, Mapping) and isinstance(nested.get("action_spec"), Mapping):
            try:
                return ActionSpec.from_dict(nested["action_spec"])
            except SchemaValidationError:
                return None
    spec_payload = payload.get("action_spec")
    if isinstance(spec_payload, Mapping):
        try:
            return ActionSpec.from_dict(spec_payload)
        except SchemaValidationError:
            return None
    return None


def normalize_candidate_text(text: str) -> str:
    """NFKC, casefold, and collapse whitespace. Does not rename variables."""
    if not isinstance(text, str):
        raise SchemaValidationError("candidate text must be str")
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def recall_candidates(
    query_text: str,
    query_scope: MathematicalScope,
    corpus: Sequence[tuple[str, str, MathematicalScope]],
    *,
    token_overlap_threshold: float = _DEFAULT_TOKEN_OVERLAP_THRESHOLD,
) -> tuple[CandidateMatch, ...]:
    """Advisory recall only. Never merges IDs or counts as a same-scope strike.

    ``token_overlap_threshold`` is Jaccard similarity on NFKC/casefold tokens.
    Scope or quantifier mismatch is always ``SCOPE_DIFFERENT``, even when tokens
    overlap.
    """
    if not isinstance(query_scope, MathematicalScope):
        raise SchemaValidationError("query_scope must be a MathematicalScope")
    threshold = _require_unit_interval(
        "token_overlap_threshold", token_overlap_threshold
    )
    query_norm = normalize_candidate_text(query_text)
    matches: list[CandidateMatch] = []
    for item in corpus:
        if not isinstance(item, tuple) or len(item) != 3:
            raise SchemaValidationError("corpus items must be (id, text, scope)")
        candidate_id, candidate_text, scope = item
        if not isinstance(candidate_id, str) or candidate_id == "":
            raise SchemaValidationError("candidate id must be nonempty str")
        if not isinstance(scope, MathematicalScope):
            raise SchemaValidationError("corpus scope must be a MathematicalScope")
        cand_norm = normalize_candidate_text(candidate_text)
        text_equal = query_norm == cand_norm
        scope_equal = query_scope == scope
        similarity = _token_jaccard(query_norm, cand_norm)
        overlap = (not text_equal) and similarity >= threshold
        if not scope_equal and (text_equal or overlap):
            kind = CandidateMatchKind.SCOPE_DIFFERENT
        elif text_equal and scope_equal:
            kind = CandidateMatchKind.ADVISORY
        elif overlap and scope_equal:
            kind = CandidateMatchKind.TOKEN_OVERLAP
        else:
            kind = CandidateMatchKind.DISTINCT
        matches.append(
            CandidateMatch(
                candidate_id=candidate_id,
                query_text=query_text,
                candidate_text=candidate_text,
                kind=kind,
                scope_equal=scope_equal,
                text_equal=text_equal,
                token_similarity=similarity,
                merges=False,
            )
        )
    return tuple(matches)


def _token_jaccard(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _require_hex64(name: str, value: object) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise SchemaValidationError(f"{name} must be 64-char lowercase hex")
    return value


def _require_enum(enum_cls: type[Enum], value: object, name: str):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value)
        except ValueError:
            raise SchemaValidationError(f"{name} has unknown value {value!r}") from None
    raise SchemaValidationError(f"{name} must be {enum_cls.__name__}")


def _require_unit_interval(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValidationError(f"{name} must be a finite number in [0, 1]")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise SchemaValidationError(f"{name} must be a finite number in [0, 1]")
    return number


def _decision_for_record(record: ActionAttemptRecord) -> DedupeDecision:
    if record.infrastructure_status in _ACTIVE:
        return DedupeDecision.WAIT_FOR_EXISTING
    if record.infrastructure_status is InfrastructureStatus.SUCCEEDED:
        return DedupeDecision.RECOVER_OR_REUSE
    if record.infrastructure_status in _INFRA_FAIL:
        if record.has_recovered_payload():
            return DedupeDecision.RECOVER_OR_REUSE
        return DedupeDecision.RETRY_TRANSPORT
    raise SchemaValidationError("cannot decide from UNKNOWN infrastructure status")
