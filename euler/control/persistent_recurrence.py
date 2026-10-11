"""Event-backed recurrence observe/replay.

Stdlib only. Counters are keyed by completed action/progress IDs, never by
health-poll counts. ``synchronize_project_recurrence`` persists
``euler.recurrence_observed`` snapshots; ``analyze_project_recurrence`` is the
read-only callable for parent ``do_analyze_recurrence``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from euler.control.ids import stable_id
from euler.control.progress import ProgressKind, ProgressReceipt
from euler.control.recurrence import (
    DEFAULT_THRESHOLD,
    RecurrenceAssessment,
    RecurrenceHistory,
)
from euler.control.schema import EventEnvelope
from euler.control.views import ResearchView

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_RECURRENCE_OBSERVED = "euler.recurrence_observed"
_EVENT_PROGRESS = "runtime.progress_recorded"
_EVENT_ATTEMPT_FINISHED = "runtime.attempt_finished"
_EVENT_LEASE_COMPLETED = "runtime.lease_completed"
_EVENT_CANDIDATE_PROPOSED = "euler.candidate_proposed"
_EVENT_DISPATCH_PLANNED = "euler.dispatch_planned"
_POLL_KINDS = frozenset({"POLL", "HEALTH_POLL", "LEASE_HEALTH"})
_RESEARCH_EVENT_TYPES = frozenset(
    {
        "problem.contract.created",
        "evidence.registered",
        "evidence.invalidated",
        "obligation.created",
        "obligation.status_changed",
        "obligation.reopened",
        "route.created",
        "route.status_changed",
        "relation.created",
        "failure.recorded",
    }
)
_MATHEMATICAL_PROGRESS = frozenset(
    {
        ProgressKind.OBLIGATION_DISCHARGED,
        ProgressKind.REDUCTION_VERIFIED,
        ProgressKind.COUNTEREXAMPLE_CHECKED,
        ProgressKind.REFERENCE_VALIDATED,
        ProgressKind.GAP_REPAIRED,
    }
)

__all__ = [
    "EVENT_RECURRENCE_OBSERVED",
    "PersistentRecurrenceError",
    "RecurrenceObservation",
    "analyze_recurrence",
    "observe_recurrence",
    "attempt_observation_id",
    "observation_from_progress",
    "observation_from_completed_action",
    "observation_event_payload",
    "replay_recurrence_history",
    "history_from_state",
    "analyze_project_recurrence",
    "synchronize_project_recurrence",
]


class PersistentRecurrenceError(ValueError):
    """Malformed recurrence observation or replay input."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RecurrenceObservation:
    """One completed action or progress event. Polls are not observations."""

    observation_id: str
    source: str
    action_id: Optional[str] = None
    attempt_id: Optional[str] = None
    progress_id: Optional[str] = None
    route_id: Optional[str] = None
    progress_kind: Optional[ProgressKind] = None
    tool_hash: str = ""
    executable_next_action: Optional[str] = None
    no_verifiable_progress_route_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            _require_hex64("observation_id", self.observation_id),
        )
        object.__setattr__(self, "source", _require_nonempty("source", self.source))
        if self.source.casefold() in {item.casefold() for item in _POLL_KINDS}:
            raise PersistentRecurrenceError("poll sources cannot be stored as observations")
        object.__setattr__(self, "action_id", _optional_hex64("action_id", self.action_id))
        object.__setattr__(
            self, "attempt_id", _optional_hex64("attempt_id", self.attempt_id)
        )
        object.__setattr__(
            self, "progress_id", _optional_hex64("progress_id", self.progress_id)
        )
        object.__setattr__(self, "route_id", _optional_hex64("route_id", self.route_id))
        kind = self.progress_kind
        if kind is not None and not isinstance(kind, ProgressKind):
            try:
                kind = ProgressKind(kind)
            except ValueError as exc:
                raise PersistentRecurrenceError(
                    f"unknown progress_kind {self.progress_kind!r}"
                ) from exc
            object.__setattr__(self, "progress_kind", kind)
        if not isinstance(self.tool_hash, str):
            raise PersistentRecurrenceError("tool_hash must be a str")
        if self.executable_next_action is not None:
            object.__setattr__(
                self,
                "executable_next_action",
                _require_nonempty("executable_next_action", self.executable_next_action),
            )
        object.__setattr__(
            self,
            "no_verifiable_progress_route_id",
            _optional_hex64(
                "no_verifiable_progress_route_id",
                self.no_verifiable_progress_route_id,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "source": self.source,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "progress_id": self.progress_id,
            "route_id": self.route_id,
            "progress_kind": None if self.progress_kind is None else self.progress_kind.value,
            "tool_hash": self.tool_hash,
            "executable_next_action": self.executable_next_action,
            "no_verifiable_progress_route_id": self.no_verifiable_progress_route_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> "RecurrenceObservation":
        payload = _mapping("RecurrenceObservation", data)
        allowed = {
            "observation_id",
            "source",
            "action_id",
            "attempt_id",
            "progress_id",
            "route_id",
            "progress_kind",
            "tool_hash",
            "executable_next_action",
            "no_verifiable_progress_route_id",
        }
        extra = set(payload) - allowed
        if extra:
            raise PersistentRecurrenceError(
                f"RecurrenceObservation has unknown fields: {sorted(extra)}"
            )
        missing = {"observation_id", "source"} - set(payload)
        if missing:
            raise PersistentRecurrenceError(
                f"RecurrenceObservation missing fields: {sorted(missing)}"
            )
        return cls(
            observation_id=payload["observation_id"],
            source=payload["source"],
            action_id=payload.get("action_id"),
            attempt_id=payload.get("attempt_id"),
            progress_id=payload.get("progress_id"),
            route_id=payload.get("route_id"),
            progress_kind=payload.get("progress_kind"),
            tool_hash=payload.get("tool_hash") or "",
            executable_next_action=payload.get("executable_next_action"),
            no_verifiable_progress_route_id=payload.get(
                "no_verifiable_progress_route_id"
            ),
        )


def analyze_recurrence(
    view: ResearchView,
    history: RecurrenceHistory | None = None,
    *,
    tool_hash: str = "",
) -> RecurrenceAssessment:
    """Idempotent read-only analysis. Health polls must use this path."""
    live = history if history is not None else RecurrenceHistory()
    return live.analyze(view, tool_hash=tool_hash)


def observe_recurrence(
    history: RecurrenceHistory,
    view: ResearchView,
    observation: RecurrenceObservation,
) -> RecurrenceAssessment:
    """Apply one completed action/progress observation. Idempotent per ID."""
    if not isinstance(history, RecurrenceHistory):
        raise PersistentRecurrenceError("history must be a RecurrenceHistory")
    if not isinstance(observation, RecurrenceObservation):
        raise PersistentRecurrenceError("observation must be a RecurrenceObservation")
    return history.observe(
        observation.observation_id,
        view,
        tool_hash=observation.tool_hash,
        progress_kind=observation.progress_kind,
        executable_next_action=observation.executable_next_action,
        no_verifiable_progress_route_id=_no_progress_route(observation),
        affected_route_id=observation.route_id,
    )


def _no_progress_route(observation: RecurrenceObservation) -> str | None:
    if observation.progress_kind is not ProgressKind.NO_VERIFIABLE_PROGRESS:
        return None
    return observation.no_verifiable_progress_route_id or observation.route_id


def attempt_observation_id(attempt_id: str) -> str:
    """Stable observation identity for one completed attempt."""
    return stable_id("recurrence-observation", 1, "attempt", attempt_id)


def observation_from_progress(
    receipt: ProgressReceipt,
    *,
    tool_hash: str = "",
    route_id: str | None = None,
) -> RecurrenceObservation:
    if not isinstance(receipt, ProgressReceipt):
        raise PersistentRecurrenceError("receipt must be a ProgressReceipt")
    affected = route_id
    if affected is None and receipt.affected_route_ids:
        affected = receipt.affected_route_ids[0]
    no_progress_route = None
    if receipt.kind is ProgressKind.NO_VERIFIABLE_PROGRESS:
        no_progress_route = affected
    resolved_tool = tool_hash or receipt.new_tool_environment_hash or ""
    return RecurrenceObservation(
        observation_id=attempt_observation_id(receipt.attempt_id),
        source="progress",
        action_id=receipt.action_id,
        attempt_id=receipt.attempt_id,
        progress_id=receipt.receipt_id,
        route_id=affected,
        progress_kind=receipt.kind,
        tool_hash=resolved_tool,
        executable_next_action=receipt.next_action,
        no_verifiable_progress_route_id=no_progress_route,
    )


def observation_from_completed_action(
    *,
    action_id: str,
    attempt_id: str,
    route_id: str | None = None,
    tool_hash: str = "",
    progress_kind: object = None,
    executable_next_action: str | None = None,
) -> RecurrenceObservation:
    return RecurrenceObservation(
        observation_id=attempt_observation_id(attempt_id),
        source="completed_action",
        action_id=action_id,
        attempt_id=attempt_id,
        route_id=route_id,
        progress_kind=progress_kind,
        tool_hash=tool_hash,
        executable_next_action=executable_next_action,
        no_verifiable_progress_route_id=(
            route_id if progress_kind is ProgressKind.NO_VERIFIABLE_PROGRESS else None
        ),
    )


def observation_event_payload(
    observation: RecurrenceObservation,
    *,
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Payload for parent/controller to append as ``euler.recurrence_observed``."""
    if not isinstance(observation, RecurrenceObservation):
        raise PersistentRecurrenceError("observation must be a RecurrenceObservation")
    payload: dict[str, Any] = {"observation": observation.to_dict()}
    if state is not None:
        payload["state"] = dict(state)
    return payload


def replay_recurrence_history(
    events: Iterable[object],
    view: ResearchView | None = None,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    tool_hash: str = "",
) -> tuple[RecurrenceHistory, RecurrenceAssessment]:
    """Rebuild history from durable events, ignoring health polls.

    Completions are keyed by attempt. A ProgressReceipt wins over a generic
    lease-completed event. Each observation is applied against the ResearchView
    as it existed at that completion, not the final project view.
    """
    history, assessment, _end_view = _replay_incremental(
        events, threshold=threshold, tool_hash=tool_hash, base_view=view
    )
    return history, assessment


def analyze_project_recurrence(
    project: object,
    *,
    tool_hash: str = "",
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Read-only replayed recurrence for parent ``do_analyze_recurrence``.

    Does not append events or count health polls. Call
    ``synchronize_project_recurrence`` first when newly completed leases must
    be persisted; this callable still in-memory-applies unsynced completions.
    """
    store = getattr(project, "event_store", None)
    if store is None or not hasattr(project, "research_view"):
        raise PersistentRecurrenceError("analyze_project_recurrence requires an open EulerProject")
    events = list(store.iter_project(project.project_id))
    history, assessment = replay_recurrence_history(
        events, threshold=threshold, tool_hash=tool_hash
    )
    return {
        "audit_required": assessment.audit_required,
        "auto_park": False,
        "blocked_mechanism": assessment.blocked_mechanism,
        "classification": assessment.classification.value,
        "exhausted_route_ids": list(assessment.exhausted_route_ids),
        "exemptions": list(assessment.exemptions),
        "observation_ids": sorted(history._observed),
        "park_is_falsity": False,
        "project": getattr(project, "project_id", ""),
        "recommendation": assessment.recommendation.value,
        "reopen_conditions": list(assessment.reopen_conditions),
        "strike_count": assessment.strike_count,
    }


def synchronize_project_recurrence(
    project: object,
    *,
    actor: str = "controller",
    timestamp_utc: str | None = None,
    tool_hash: str = "",
    threshold: int = DEFAULT_THRESHOLD,
) -> RecurrenceHistory:
    """Persist observations for genuine completed leases/progress. Idempotent."""
    from euler.control.event_store import IdempotencyConflict
    from euler.control.schema import EventEnvelope

    store = getattr(project, "event_store", None)
    if store is None or not hasattr(project, "research_view"):
        raise PersistentRecurrenceError(
            "synchronize_project_recurrence requires an open EulerProject"
        )
    ts = timestamp_utc or _utc_now()
    events = list(store.iter_project(project.project_id))
    stored_attempts = _stored_attempt_ids(events)
    persisted: list[tuple[RecurrenceObservation, Mapping[str, Any]]] = []

    def _on_observe(
        observation: RecurrenceObservation, history: RecurrenceHistory
    ) -> None:
        attempt_id = observation.attempt_id
        if attempt_id and attempt_id in stored_attempts:
            return
        if observation.observation_id in stored_attempts:
            return
        persisted.append((observation, history.snapshot_state()))
        if attempt_id:
            stored_attempts.add(attempt_id)
        stored_attempts.add(observation.observation_id)

    history, _assessment, _end_view = _replay_incremental(
        events,
        threshold=threshold,
        tool_hash=tool_hash,
        on_observe=_on_observe,
    )
    for observation, state in persisted:
        try:
            store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor=actor,
                    event_type=EVENT_RECURRENCE_OBSERVED,
                    idempotency_key=f"recurrence-observed:{observation.observation_id}",
                    payload=observation_event_payload(observation, state=state),
                    timestamp_utc=ts,
                    correlation_id=observation.observation_id,
                )
            )
        except IdempotencyConflict:
            continue
    return history


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def history_from_state(state: object, *, threshold: int = DEFAULT_THRESHOLD) -> RecurrenceHistory:
    history = RecurrenceHistory(threshold=threshold)
    history.restore_state(state)
    return history


def _replay_incremental(
    events: Iterable[object],
    *,
    threshold: int,
    tool_hash: str = "",
    on_observe: object = None,
    base_view: ResearchView | None = None,
) -> tuple[RecurrenceHistory, RecurrenceAssessment, ResearchView]:
    ordered = list(_iter_seq_events(events))
    completions = _completions_from_events(ordered)
    by_seq: dict[int, RecurrenceObservation] = {}
    for seq, observation in completions:
        by_seq[seq] = observation
    has_research = any(
        isinstance(envelope, EventEnvelope) and envelope.event_type in _RESEARCH_EVENT_TYPES
        for _seq, envelope in ordered
    )
    if has_research or not isinstance(base_view, ResearchView):
        view = ResearchView()
        apply_research = True
    else:
        view = base_view
        apply_research = False
    history = RecurrenceHistory(threshold=threshold)
    last_tool = tool_hash
    for seq, envelope in ordered:
        event_type = envelope.event_type if isinstance(envelope, EventEnvelope) else ""
        if apply_research and event_type in _RESEARCH_EVENT_TYPES:
            view.apply(seq, envelope)
        observation = by_seq.get(seq)
        if observation is None:
            continue
        if observation.observation_id in history._observed:
            continue
        if tool_hash and not observation.tool_hash:
            observation = RecurrenceObservation.from_dict(
                {**observation.to_dict(), "tool_hash": tool_hash}
            )
        observe_recurrence(history, view, observation)
        if observation.tool_hash:
            last_tool = observation.tool_hash
        if callable(on_observe):
            on_observe(observation, history)
    assessment = history.analyze(view, tool_hash=last_tool)
    return history, assessment, view


def _iter_seq_events(events: Iterable[object]) -> list[tuple[int, object]]:
    out: list[tuple[int, object]] = []
    auto = 0
    for item in events:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], int):
            out.append((item[0], item[1]))
            continue
        auto += 1
        out.append((auto, item))
    return out


def _completions_from_events(
    ordered: list[tuple[int, object]],
) -> list[tuple[int, RecurrenceObservation]]:
    tool_by_action: dict[str, str] = {}
    buckets: dict[str, list[tuple[int, RecurrenceObservation]]] = {}
    for seq, item in ordered:
        envelope, payload = _event_parts(item)
        event_type = envelope.event_type if envelope is not None else ""
        if not event_type and isinstance(item, Mapping):
            event_type = str(item.get("event_type") or "")
        _ingest_tool_map(event_type, payload, tool_by_action)
        if event_type in _POLL_KINDS or event_type.endswith(".health_poll"):
            continue
        if event_type == EVENT_RECURRENCE_OBSERVED:
            continue
        observation = _observation_from_event(item)
        if observation is None or not observation.attempt_id:
            continue
        observation = _bind_tool_hash(observation, tool_by_action)
        buckets.setdefault(observation.attempt_id, []).append((seq, observation))
    chosen: list[tuple[int, RecurrenceObservation]] = []
    for _attempt_id, items in buckets.items():
        items.sort(key=lambda pair: pair[0])
        seq, observation = _prefer_observation(items)
        chosen.append((seq, observation))
    if not chosen:
        for seq, item in ordered:
            envelope, payload = _event_parts(item)
            event_type = envelope.event_type if envelope is not None else ""
            if event_type != EVENT_RECURRENCE_OBSERVED:
                continue
            raw = payload.get("observation", payload)
            try:
                observation = RecurrenceObservation.from_dict(raw)
            except PersistentRecurrenceError:
                continue
            chosen.append((seq, observation))
    chosen.sort(key=lambda pair: pair[0])
    return chosen


def _prefer_observation(
    items: list[tuple[int, RecurrenceObservation]],
) -> tuple[int, RecurrenceObservation]:
    def rank(observation: RecurrenceObservation) -> tuple[int, int]:
        kind = observation.progress_kind
        if kind in _MATHEMATICAL_PROGRESS:
            score = 0
        elif kind is ProgressKind.NO_VERIFIABLE_PROGRESS:
            score = 1
        elif kind is ProgressKind.REPRESENTATION_GAIN:
            score = 2
        else:
            score = 3
        return (score, 0)

    best_seq, best = items[0]
    best_rank = rank(best)
    for seq, observation in items[1:]:
        current = rank(observation)
        if current[0] < best_rank[0] or (
            current[0] == best_rank[0] and seq >= best_seq
        ):
            best_seq, best, best_rank = seq, observation, current
    return best_seq, best


def _ingest_tool_map(
    event_type: str, payload: Mapping[str, Any], tool_by_action: dict[str, str]
) -> None:
    if event_type == _EVENT_CANDIDATE_PROPOSED:
        _tool_from_candidate_mapping(payload.get("candidate"), tool_by_action)
        return
    if event_type == _EVENT_DISPATCH_PLANNED:
        bound = payload.get("bound_candidates")
        if isinstance(bound, Mapping):
            for item in bound.values():
                _tool_from_candidate_mapping(item, tool_by_action)


def _tool_from_candidate_mapping(raw: object, tool_by_action: dict[str, str]) -> None:
    if not isinstance(raw, Mapping):
        return
    spec = raw.get("action_spec")
    request = raw.get("context_request")
    seed = raw.get("context_seed")
    action_id = None
    tool = ""
    if isinstance(spec, Mapping):
        action_id = spec.get("action_id")
        tool = spec.get("tool_environment_hash") or ""
    if not tool and isinstance(request, Mapping):
        tool = request.get("tool_environment_hash") or ""
        action_id = action_id or request.get("action_id")
    if not tool and isinstance(seed, Mapping):
        tool = seed.get("tool_environment_hash") or ""
    if isinstance(action_id, str) and action_id and isinstance(tool, str) and tool:
        tool_by_action[action_id] = tool


def _bind_tool_hash(
    observation: RecurrenceObservation, tool_by_action: Mapping[str, str]
) -> RecurrenceObservation:
    if observation.tool_hash:
        return observation
    action_id = observation.action_id
    if not action_id:
        return observation
    tool = tool_by_action.get(action_id, "")
    if not tool:
        return observation
    payload = observation.to_dict()
    payload["tool_hash"] = tool
    return RecurrenceObservation.from_dict(payload)


def _stored_attempt_ids(events: Iterable[object]) -> set[str]:
    stored: set[str] = set()
    for item in events:
        envelope, payload = _event_parts(item)
        event_type = envelope.event_type if envelope is not None else ""
        if event_type != EVENT_RECURRENCE_OBSERVED:
            continue
        raw = payload.get("observation", payload)
        try:
            observation = RecurrenceObservation.from_dict(raw)
        except PersistentRecurrenceError:
            continue
        stored.add(observation.observation_id)
        if observation.attempt_id:
            stored.add(observation.attempt_id)
    return stored


def _observation_from_event(item: object) -> RecurrenceObservation | None:
    envelope, payload = _event_parts(item)
    event_type = ""
    if envelope is not None:
        event_type = envelope.event_type
    elif isinstance(item, Mapping):
        event_type = str(item.get("event_type") or "")
    if event_type in _POLL_KINDS or event_type.endswith(".health_poll"):
        return None
    if event_type == EVENT_RECURRENCE_OBSERVED or "observation" in payload:
        raw = payload.get("observation", payload)
        try:
            return RecurrenceObservation.from_dict(raw)
        except PersistentRecurrenceError:
            return None
    if event_type == _EVENT_PROGRESS:
        progress = payload.get("progress")
        if not isinstance(progress, Mapping):
            return None
        try:
            receipt = ProgressReceipt.from_dict(progress)
        except Exception:
            return None
        tool_hash = ""
        if isinstance(progress.get("new_tool_environment_hash"), str):
            tool_hash = progress["new_tool_environment_hash"]
        return observation_from_progress(receipt, tool_hash=tool_hash)
    if event_type in {_EVENT_ATTEMPT_FINISHED, _EVENT_LEASE_COMPLETED}:
        lease = payload.get("lease")
        if not isinstance(lease, Mapping):
            return None
        action_id = lease.get("action_id")
        attempt_id = lease.get("attempt_id") or payload.get("attempt_id")
        if not isinstance(action_id, str) or not isinstance(attempt_id, str):
            return None
        route_id = lease.get("route_id")
        return observation_from_completed_action(
            action_id=action_id,
            attempt_id=attempt_id,
            route_id=route_id if isinstance(route_id, str) else None,
            tool_hash="",
        )
    return None


def _event_parts(item: object) -> tuple[EventEnvelope | None, Mapping[str, Any]]:
    if isinstance(item, tuple) and len(item) == 2:
        item = item[1]
    if isinstance(item, EventEnvelope):
        payload = item.payload
        if not isinstance(payload, Mapping):
            return item, {}
        return item, payload
    if isinstance(item, RecurrenceObservation):
        return None, {"observation": item.to_dict()}
    if isinstance(item, Mapping) and not isinstance(item, (str, bytes)):
        if "observation" in item or "observation_id" in item:
            return None, dict(item)
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            return None, payload
        return None, dict(item)
    return None, {}


def _mapping(label: str, data: object) -> Mapping[str, Any]:
    if not isinstance(data, Mapping) or isinstance(data, (str, bytes)):
        raise PersistentRecurrenceError(f"{label} must be a mapping")
    return data


def _require_nonempty(name: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise PersistentRecurrenceError(f"{name} must be a nonempty str")
    return value


def _require_hex64(name: str, value: object) -> str:
    text = _require_nonempty(name, value)
    if _HEX64_RE.fullmatch(text) is None:
        raise PersistentRecurrenceError(f"{name} must be 64-char lowercase hex")
    return text


def _optional_hex64(name: str, value: object) -> Optional[str]:
    if value is None:
        return None
    return _require_hex64(name, value)
