"""Conservative recurrence strikes and recommendations.

Stdlib only. Strike keys hash SCC nodes, projected edges, and premise version.
Evidence/tool frontier changes start a new cycle. History mutates only its own
counters, never ResearchView.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from euler.control.ids import canonical_json, stable_id
from euler.control.progress import ProgressKind
from euler.control.scc import (
    SCCClassification,
    SCCReport,
    classify_scc,
    credible_or_exits,
    project_research_graph,
    scc_pass_artifact_ids,
    scc_premise_hash,
    tarjan_scc,
)
from euler.control.views import ResearchView

DEFAULT_THRESHOLD = 3
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_PARK_REOPEN_CONDITIONS = (
    "new checked evidence on the parked route",
    "new applicable tool or environment",
    "new discharging premise or alternative OR exit",
)

__all__ = [
    "DEFAULT_THRESHOLD",
    "DEFAULT_PARK_REOPEN_CONDITIONS",
    "RecurrenceRecommendation",
    "FrontierFingerprint",
    "StrikeKey",
    "RecurrenceAssessment",
    "RecurrenceHistory",
]


class RecurrenceRecommendation(str, Enum):
    CONTINUE = "CONTINUE"
    RECOVER_OR_REUSE = "RECOVER_OR_REUSE"
    BOUNDED_AUDIT = "BOUNDED_AUDIT"
    RESTRUCTURE = "RESTRUCTURE"
    RUN_COUNTEREXAMPLE_TEST = "RUN_COUNTEREXAMPLE_TEST"
    CHANGE_ROUTE = "CHANGE_ROUTE"
    PARK_ROUTE_CANDIDATE = "PARK_ROUTE_CANDIDATE"


_AUDIT_REQUIRED = frozenset(
    {
        RecurrenceRecommendation.BOUNDED_AUDIT,
        RecurrenceRecommendation.RESTRUCTURE,
        RecurrenceRecommendation.PARK_ROUTE_CANDIDATE,
    }
)
_CLASSIFICATION_RANK = {
    SCCClassification.ACYCLIC: 0,
    SCCClassification.LEGAL_DESCENT: 1,
    SCCClassification.HAS_EXTERNAL_EXIT: 2,
    SCCClassification.CHANGED_FRONTIER: 3,
    SCCClassification.SUSPECTED_RECURRENCE: 4,
    SCCClassification.RECURRENT_NO_EXIT: 5,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class FrontierFingerprint:
    evidence_hash: str
    premise_hash: str
    tool_hash: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name, value in (
            ("evidence_hash", self.evidence_hash),
            ("premise_hash", self.premise_hash),
            ("tool_hash", self.tool_hash),
        ):
            _require_hex64(name, value)

    @classmethod
    def compute(
        cls, view: ResearchView, nodes: Iterable[str], *, tool_hash: str
    ) -> "FrontierFingerprint":
        ordered = tuple(sorted(set(nodes)))
        evidence_ids = scc_pass_artifact_ids(view, ordered)
        evidence_hash = stable_id("evidence-frontier", 1, *evidence_ids) if evidence_ids else stable_id(
            "evidence-frontier", 1, ""
        )
        fingerprint = cls(
            evidence_hash=evidence_hash,
            premise_hash=scc_premise_hash(view, ordered),
            tool_hash=_tool_hash_value(tool_hash),
        )
        fingerprint.validate()
        return fingerprint


@dataclass(frozen=True, slots=True, kw_only=True)
class StrikeKey:
    key_id: str
    nodes: tuple[str, ...]
    premise_hash: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _require_hex64("key_id", self.key_id)
        if isinstance(self.nodes, str) or not isinstance(self.nodes, tuple):
            raise ValueError("nodes must be a tuple of 64-hex hashes")
        for index, node in enumerate(self.nodes):
            _require_hex64(f"nodes[{index}]", node)
        _require_hex64("premise_hash", self.premise_hash)

    @classmethod
    def build(
        cls,
        nodes: Iterable[str],
        edges: Iterable[object],
        premise_hash: str,
    ) -> "StrikeKey":
        ordered_nodes = tuple(sorted(set(nodes)))
        edge_payloads = []
        for edge in edges:
            identity = getattr(edge, "identity_payload", None)
            if callable(identity):
                edge_payloads.append(identity())
            else:
                edge_payloads.append(
                    {
                        "kind": getattr(getattr(edge, "kind", None), "value", str(getattr(edge, "kind", ""))),
                        "source": getattr(edge, "source", ""),
                        "target": getattr(edge, "target", ""),
                        "route_id": getattr(edge, "route_id", None),
                        "relation_id": getattr(edge, "relation_id", None),
                    }
                )
        edge_payloads.sort(key=lambda item: canonical_json(item))
        key_id = stable_id(
            "strike",
            1,
            canonical_json(list(ordered_nodes)),
            canonical_json(edge_payloads),
            premise_hash,
        )
        return cls(key_id=key_id, nodes=ordered_nodes, premise_hash=premise_hash)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecurrenceAssessment:
    classification: SCCClassification
    recommendation: RecurrenceRecommendation
    audit_required: bool
    strike_count: int
    strike_key: StrikeKey
    fingerprint: FrontierFingerprint
    report: SCCReport
    exemptions: tuple[str, ...]
    exhausted_route_ids: tuple[str, ...]
    park_is_falsity: bool = False
    reopen_conditions: tuple[str, ...] = ()
    observation_id: str | None = None
    blocked_mechanism: bool = False

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.classification, SCCClassification):
            raise ValueError("classification must be an SCCClassification")
        if not isinstance(self.recommendation, RecurrenceRecommendation):
            raise ValueError("recommendation must be a RecurrenceRecommendation")
        if not isinstance(self.audit_required, bool):
            raise ValueError("audit_required must be a bool")
        if (
            isinstance(self.strike_count, bool)
            or not isinstance(self.strike_count, int)
            or self.strike_count < 0
        ):
            raise ValueError("strike_count must be a nonnegative int")
        if not isinstance(self.strike_key, StrikeKey):
            raise ValueError("strike_key must be a StrikeKey")
        self.strike_key.validate()
        if not isinstance(self.fingerprint, FrontierFingerprint):
            raise ValueError("fingerprint must be a FrontierFingerprint")
        self.fingerprint.validate()
        if not isinstance(self.report, SCCReport):
            raise ValueError("report must be an SCCReport")
        self.report.validate()
        if self.classification is not self.report.classification:
            raise ValueError("classification must match report.classification")
        if isinstance(self.exemptions, str) or not isinstance(self.exemptions, tuple):
            raise ValueError("exemptions must be a tuple of str")
        for item in self.exemptions:
            if not isinstance(item, str) or item == "":
                raise ValueError("exemptions must be a tuple of nonempty str")
        if isinstance(self.exhausted_route_ids, str) or not isinstance(
            self.exhausted_route_ids, tuple
        ):
            raise ValueError("exhausted_route_ids must be a tuple of 64-hex hashes")
        for index, route_id in enumerate(self.exhausted_route_ids):
            _require_hex64(f"exhausted_route_ids[{index}]", route_id)
        if (
            self.recommendation in _AUDIT_REQUIRED
            or self.classification is SCCClassification.RECURRENT_NO_EXIT
        ) and self.audit_required is not True:
            raise ValueError("audit_required is inconsistent with the recommendation matrix")
        if (
            self.recommendation is RecurrenceRecommendation.PARK_ROUTE_CANDIDATE
            and not self.exhausted_route_ids
        ):
            raise ValueError(
                "PARK_ROUTE_CANDIDATE requires exhausted credible-exit candidates"
            )
        if not isinstance(self.park_is_falsity, bool):
            raise ValueError("park_is_falsity must be a bool")
        if self.park_is_falsity is not False:
            raise ValueError("route parking is policy and must not assert theorem falsity")
        if isinstance(self.reopen_conditions, str) or not isinstance(
            self.reopen_conditions, tuple
        ):
            raise ValueError("reopen_conditions must be a tuple of str")
        for item in self.reopen_conditions:
            if not isinstance(item, str) or item == "":
                raise ValueError("reopen_conditions must be a tuple of nonempty str")
        if self.observation_id is not None:
            _require_hex64("observation_id", self.observation_id)
        if not isinstance(self.blocked_mechanism, bool):
            raise ValueError("blocked_mechanism must be a bool")
        if self.recommendation is RecurrenceRecommendation.PARK_ROUTE_CANDIDATE:
            if not self.reopen_conditions:
                raise ValueError("PARK_ROUTE_CANDIDATE requires reopen_conditions")


class RecurrenceHistory:
    """Per-strike counters. The only mutable state is this object's maps.

    ``analyze`` is read-only and must be used for health polls. ``observe``
    records a completed action or progress ID once. ``evaluate`` remains the
    in-memory tick used by existing callers; pass ``poll=True`` to avoid
    counting a poll as a strike.
    """

    def __init__(self, *, threshold: int = DEFAULT_THRESHOLD) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("threshold must be a positive int")
        self.threshold = threshold
        self._strikes: dict[str, int] = {}
        self._fingerprints: dict[str, FrontierFingerprint] = {}
        self._exhausted: dict[str, set[str]] = {}
        self._observed: set[str] = set()
        self._route_strikes: dict[str, int] = {}
        self._route_fingerprints: dict[str, FrontierFingerprint] = {}

    def analyze(
        self,
        view: ResearchView,
        *,
        tool_hash: str = "",
        executable_next_action: str | None = None,
        progress_kind: object = None,
        no_verifiable_progress_route_id: str | None = None,
        affected_route_id: str | None = None,
    ) -> RecurrenceAssessment:
        """Idempotent read-only assessment. Does not increment strikes."""
        return self._run(
            view,
            tool_hash=tool_hash,
            executable_next_action=executable_next_action,
            progress_kind=progress_kind,
            no_verifiable_progress_route_id=no_verifiable_progress_route_id,
            affected_route_id=affected_route_id,
            apply_strikes=False,
            observation_id=None,
        )

    def analyze_all(
        self,
        view: ResearchView,
        *,
        tool_hash: str = "",
        executable_next_action: str | None = None,
        progress_kind: object = None,
        no_verifiable_progress_route_id: str | None = None,
        affected_route_id: str | None = None,
    ) -> tuple[RecurrenceAssessment, ...]:
        """Read-only assessments for every SCC and blocked mechanism."""
        return self._assess_all(
            view,
            tool_hash=tool_hash,
            executable_next_action=executable_next_action,
            progress_kind=progress_kind,
            no_verifiable_progress_route_id=no_verifiable_progress_route_id,
            affected_route_id=affected_route_id,
            apply_strikes=False,
            observation_id=None,
        )

    def observe(
        self,
        observation_id: str,
        view: ResearchView,
        *,
        tool_hash: str = "",
        executable_next_action: str | None = None,
        progress_kind: object = None,
        no_verifiable_progress_route_id: str | None = None,
        affected_route_id: str | None = None,
    ) -> RecurrenceAssessment:
        """Record one completed action/progress ID. Replaying the same ID is a no-op."""
        observation_id = _require_hex64("observation_id", observation_id)
        if observation_id in self._observed:
            return self.analyze(
                view,
                tool_hash=tool_hash,
                executable_next_action=executable_next_action,
                progress_kind=progress_kind,
                no_verifiable_progress_route_id=no_verifiable_progress_route_id,
                affected_route_id=affected_route_id,
            )
        assessment = self._run(
            view,
            tool_hash=tool_hash,
            executable_next_action=executable_next_action,
            progress_kind=progress_kind,
            no_verifiable_progress_route_id=no_verifiable_progress_route_id,
            affected_route_id=affected_route_id,
            apply_strikes=True,
            observation_id=observation_id,
        )
        self._observed.add(observation_id)
        return assessment

    def snapshot_state(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "strikes": dict(self._strikes),
            "fingerprints": {
                key: {
                    "evidence_hash": item.evidence_hash,
                    "premise_hash": item.premise_hash,
                    "tool_hash": item.tool_hash,
                }
                for key, item in self._fingerprints.items()
            },
            "exhausted": {key: sorted(value) for key, value in self._exhausted.items()},
            "observed": sorted(self._observed),
            "route_strikes": dict(self._route_strikes),
            "route_fingerprints": {
                key: {
                    "evidence_hash": item.evidence_hash,
                    "premise_hash": item.premise_hash,
                    "tool_hash": item.tool_hash,
                }
                for key, item in self._route_fingerprints.items()
            },
        }

    def restore_state(self, state: object) -> None:
        if not isinstance(state, dict):
            raise ValueError("recurrence state must be a mapping")
        threshold = state.get("threshold", self.threshold)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("threshold must be a positive int")
        self.threshold = threshold
        strikes = state.get("strikes") or {}
        if not isinstance(strikes, dict):
            raise ValueError("strikes must be a mapping")
        self._strikes = {str(key): int(value) for key, value in strikes.items()}
        fingerprints = state.get("fingerprints") or {}
        if not isinstance(fingerprints, dict):
            raise ValueError("fingerprints must be a mapping")
        restored: dict[str, FrontierFingerprint] = {}
        for key, payload in fingerprints.items():
            restored[str(key)] = FrontierFingerprint(
                evidence_hash=payload["evidence_hash"],
                premise_hash=payload["premise_hash"],
                tool_hash=payload["tool_hash"],
            )
        self._fingerprints = restored
        exhausted = state.get("exhausted") or {}
        if not isinstance(exhausted, dict):
            raise ValueError("exhausted must be a mapping")
        self._exhausted = {
            str(key): set(value) for key, value in exhausted.items()
        }
        observed = state.get("observed") or ()
        self._observed = {str(item) for item in observed}
        route_strikes = state.get("route_strikes") or {}
        if not isinstance(route_strikes, dict):
            raise ValueError("route_strikes must be a mapping")
        self._route_strikes = {str(key): int(value) for key, value in route_strikes.items()}
        route_fps = state.get("route_fingerprints") or {}
        if not isinstance(route_fps, dict):
            raise ValueError("route_fingerprints must be a mapping")
        restored_routes: dict[str, FrontierFingerprint] = {}
        for key, payload in route_fps.items():
            restored_routes[str(key)] = FrontierFingerprint(
                evidence_hash=payload["evidence_hash"],
                premise_hash=payload["premise_hash"],
                tool_hash=payload["tool_hash"],
            )
        self._route_fingerprints = restored_routes

    def evaluate(
        self,
        view: ResearchView,
        *,
        tool_hash: str = "",
        executable_next_action: str | None = None,
        progress_kind: object = None,
        no_verifiable_progress_route_id: str | None = None,
        observation_id: str | None = None,
        poll: bool = False,
        affected_route_id: str | None = None,
    ) -> RecurrenceAssessment:
        if poll:
            return self.analyze(
                view,
                tool_hash=tool_hash,
                executable_next_action=executable_next_action,
                progress_kind=progress_kind,
                no_verifiable_progress_route_id=no_verifiable_progress_route_id,
                affected_route_id=affected_route_id,
            )
        if observation_id is not None:
            return self.observe(
                observation_id,
                view,
                tool_hash=tool_hash,
                executable_next_action=executable_next_action,
                progress_kind=progress_kind,
                no_verifiable_progress_route_id=no_verifiable_progress_route_id,
                affected_route_id=affected_route_id,
            )
        return self._run(
            view,
            tool_hash=tool_hash,
            executable_next_action=executable_next_action,
            progress_kind=progress_kind,
            no_verifiable_progress_route_id=no_verifiable_progress_route_id,
            affected_route_id=affected_route_id,
            apply_strikes=True,
            observation_id=None,
        )

    def _run(
        self,
        view: ResearchView,
        *,
        tool_hash: str,
        executable_next_action: str | None,
        progress_kind: object,
        no_verifiable_progress_route_id: str | None,
        affected_route_id: str | None,
        apply_strikes: bool,
        observation_id: str | None,
    ) -> RecurrenceAssessment:
        assessments = self._assess_all(
            view,
            tool_hash=tool_hash,
            executable_next_action=executable_next_action,
            progress_kind=progress_kind,
            no_verifiable_progress_route_id=no_verifiable_progress_route_id,
            affected_route_id=affected_route_id,
            apply_strikes=apply_strikes,
            observation_id=observation_id,
        )
        return assessments[0]

    def _assess_all(
        self,
        view: ResearchView,
        *,
        tool_hash: str,
        executable_next_action: str | None,
        progress_kind: object,
        no_verifiable_progress_route_id: str | None,
        affected_route_id: str | None,
        apply_strikes: bool,
        observation_id: str | None,
    ) -> tuple[RecurrenceAssessment, ...]:
        if not isinstance(view, ResearchView):
            raise TypeError("evaluate requires a ResearchView")
        kind = _parse_progress_kind(progress_kind)
        if no_verifiable_progress_route_id is not None:
            if (
                not isinstance(no_verifiable_progress_route_id, str)
                or no_verifiable_progress_route_id == ""
            ):
                raise ValueError("no_verifiable_progress_route_id must be a nonempty str")
            if kind is not ProgressKind.NO_VERIFIABLE_PROGRESS:
                raise ValueError(
                    "no_verifiable_progress_route_id requires progress_kind "
                    "NO_VERIFIABLE_PROGRESS"
                )
        nodes, edges = project_research_graph(view)
        components = tarjan_scc(nodes, edges)
        assessments: list[RecurrenceAssessment] = []
        if not components:
            empty_fp = FrontierFingerprint.compute(view, (), tool_hash=tool_hash)
            empty_key = StrikeKey.build((), (), empty_fp.premise_hash)
            empty_report = classify_scc(
                view,
                (),
                edges,
                evidence_frontier_hash=empty_fp.evidence_hash,
                premise_hash=empty_fp.premise_hash,
                tool_hash=empty_fp.tool_hash,
                strike_count=0,
                threshold=self.threshold,
            )
            assessments.append(
                RecurrenceAssessment(
                    classification=SCCClassification.ACYCLIC,
                    recommendation=RecurrenceRecommendation.CONTINUE,
                    audit_required=False,
                    strike_count=0,
                    strike_key=empty_key,
                    fingerprint=empty_fp,
                    report=empty_report,
                    exemptions=(),
                    exhausted_route_ids=(),
                    observation_id=observation_id,
                )
            )
        else:
            assessments.extend(
                self._evaluate_component(
                    view,
                    component,
                    edges,
                    tool_hash=tool_hash,
                    executable_next_action=executable_next_action,
                    progress_kind=kind,
                    no_verifiable_progress_route_id=no_verifiable_progress_route_id,
                    apply_strikes=apply_strikes,
                    observation_id=observation_id,
                    affected_route_id=affected_route_id,
                )
                for component in components
            )
        blocked = self._evaluate_blocked_mechanisms(
            view,
            tool_hash=tool_hash,
            apply_strikes=apply_strikes,
            observation_id=observation_id,
            affected_route_id=affected_route_id or no_verifiable_progress_route_id,
            progress_kind=kind,
        )
        assessments.extend(blocked)
        assessments.sort(
            key=lambda item: (
                -int(item.audit_required),
                -_CLASSIFICATION_RANK[item.classification],
                -int(item.blocked_mechanism),
                item.report.nodes,
            )
        )
        return tuple(assessments)

    def _evaluate_component(
        self,
        view: ResearchView,
        component: tuple[str, ...],
        edges: tuple,
        *,
        tool_hash: str,
        executable_next_action: str | None,
        progress_kind: ProgressKind | None,
        no_verifiable_progress_route_id: str | None,
        apply_strikes: bool = True,
        observation_id: str | None = None,
        affected_route_id: str | None = None,
    ) -> RecurrenceAssessment:
        kind = progress_kind
        fingerprint = FrontierFingerprint.compute(view, component, tool_hash=tool_hash)
        internal = tuple(
            edge
            for edge in edges
            if edge.source in component and edge.target in component
        )
        strike_key = StrikeKey.build(component, internal, fingerprint.premise_hash)
        previous = self._fingerprints.get(strike_key.key_id)
        effective_tool = fingerprint.tool_hash
        if kind is ProgressKind.REPRESENTATION_GAIN:
            has_explicit_tool_cycle = bool(
                tool_hash and isinstance(executable_next_action, str) and executable_next_action != ""
            )
            if not has_explicit_tool_cycle:
                if previous is not None:
                    effective_tool = previous.tool_hash
                    fingerprint = FrontierFingerprint(
                        evidence_hash=fingerprint.evidence_hash,
                        premise_hash=fingerprint.premise_hash,
                        tool_hash=effective_tool,
                    )
        evidence_changed = (
            previous is not None and previous.evidence_hash != fingerprint.evidence_hash
        )
        tool_changed = previous is not None and previous.tool_hash != fingerprint.tool_hash
        local_change = _component_contains_route(view, component, affected_route_id)
        frontier_changed = evidence_changed or tool_changed
        if frontier_changed and apply_strikes:
            self._strikes[strike_key.key_id] = 0
            self._exhausted[strike_key.key_id] = set()
        live_exits = frozenset(credible_or_exits(view, component, ()))
        exhausted = set(self._exhausted.get(strike_key.key_id, set())) & live_exits
        if (
            apply_strikes
            and no_verifiable_progress_route_id
            and kind is ProgressKind.NO_VERIFIABLE_PROGRESS
            and no_verifiable_progress_route_id in live_exits
        ):
            exhausted.add(no_verifiable_progress_route_id)
        if apply_strikes:
            self._exhausted[strike_key.key_id] = set(exhausted)

        stored_strike = self._strikes.get(strike_key.key_id, 0)
        report = classify_scc(
            view,
            component,
            edges,
            evidence_frontier_hash=fingerprint.evidence_hash,
            premise_hash=fingerprint.premise_hash,
            tool_hash=fingerprint.tool_hash,
            previous_evidence_hash=None if previous is None else previous.evidence_hash,
            previous_tool_hash=None if previous is None else previous.tool_hash,
            strike_count=stored_strike,
            threshold=self.threshold,
            exhausted_route_ids=exhausted,
        )
        if report.classification is SCCClassification.CHANGED_FRONTIER:
            if apply_strikes:
                stored_strike = 0
                self._strikes[strike_key.key_id] = 0
        elif report.classification in {
            SCCClassification.SUSPECTED_RECURRENCE,
            SCCClassification.RECURRENT_NO_EXIT,
        }:
            if apply_strikes and local_change:
                stored_strike += 1
                self._strikes[strike_key.key_id] = stored_strike
            report = classify_scc(
                view,
                component,
                edges,
                evidence_frontier_hash=fingerprint.evidence_hash,
                premise_hash=fingerprint.premise_hash,
                tool_hash=fingerprint.tool_hash,
                previous_evidence_hash=fingerprint.evidence_hash,
                previous_tool_hash=fingerprint.tool_hash,
                strike_count=stored_strike,
                threshold=self.threshold,
                exhausted_route_ids=exhausted,
            )
        if apply_strikes:
            self._fingerprints[strike_key.key_id] = fingerprint
        recommendation, audit = _recommendation(report, exhausted)
        audit = audit or recommendation in _AUDIT_REQUIRED
        exemptions: list[str] = []
        if report.classification is SCCClassification.LEGAL_DESCENT:
            exemptions.append("LEGAL_DESCENT")
        if report.external_exits:
            exemptions.append("OR_EXIT")
        if frontier_changed:
            exemptions.append("CHANGED_FRONTIER")
        reopen = (
            DEFAULT_PARK_REOPEN_CONDITIONS
            if recommendation is RecurrenceRecommendation.PARK_ROUTE_CANDIDATE
            else ()
        )
        return RecurrenceAssessment(
            classification=report.classification,
            recommendation=recommendation,
            audit_required=audit,
            strike_count=self._strikes.get(strike_key.key_id, 0),
            strike_key=strike_key,
            fingerprint=fingerprint,
            report=report,
            exemptions=tuple(exemptions),
            exhausted_route_ids=tuple(sorted(exhausted)),
            park_is_falsity=False,
            reopen_conditions=reopen,
            observation_id=observation_id,
        )

    def _evaluate_blocked_mechanisms(
        self,
        view: ResearchView,
        *,
        tool_hash: str,
        apply_strikes: bool,
        observation_id: str | None,
        affected_route_id: str | None,
        progress_kind: ProgressKind | None,
    ) -> list[RecurrenceAssessment]:
        """Acyclic repeated unchanged blocked mechanism is an audit cue."""
        from euler.control.obligations import RouteStatus

        assessments: list[RecurrenceAssessment] = []
        for route in view.routes():
            if route.status is not RouteStatus.BLOCKED:
                continue
            nodes = (route.target_obligation_id,)
            fingerprint = FrontierFingerprint.compute(view, nodes, tool_hash=tool_hash)
            premise = stable_id(
                "blocked-mechanism",
                1,
                route.route_id,
                route.mechanism,
                fingerprint.premise_hash,
            )
            strike_key = StrikeKey.build((route.route_id,), (), premise)
            previous = self._route_fingerprints.get(strike_key.key_id)
            local = affected_route_id in {None, route.route_id}
            frontier_changed = previous is not None and (
                previous.evidence_hash != fingerprint.evidence_hash
                or previous.tool_hash != fingerprint.tool_hash
            )
            stored = self._route_strikes.get(strike_key.key_id, 0)
            if apply_strikes and local and frontier_changed:
                stored = 0
                self._route_strikes[strike_key.key_id] = 0
            elif apply_strikes and local and not frontier_changed:
                stored += 1
                self._route_strikes[strike_key.key_id] = stored
            if apply_strikes and local:
                self._route_fingerprints[strike_key.key_id] = fingerprint
            report = classify_scc(
                view,
                nodes,
                (),
                evidence_frontier_hash=fingerprint.evidence_hash,
                premise_hash=fingerprint.premise_hash,
                tool_hash=fingerprint.tool_hash,
                previous_evidence_hash=None if previous is None else previous.evidence_hash,
                previous_tool_hash=None if previous is None else previous.tool_hash,
                strike_count=stored,
                threshold=self.threshold,
            )
            audit = stored >= self.threshold and not frontier_changed
            recommendation = (
                RecurrenceRecommendation.BOUNDED_AUDIT
                if audit
                else RecurrenceRecommendation.CONTINUE
            )
            assessments.append(
                RecurrenceAssessment(
                    classification=report.classification,
                    recommendation=recommendation,
                    audit_required=audit,
                    strike_count=stored,
                    strike_key=strike_key,
                    fingerprint=fingerprint,
                    report=report,
                    exemptions=("CHANGED_FRONTIER",) if frontier_changed else (),
                    exhausted_route_ids=(),
                    park_is_falsity=False,
                    reopen_conditions=(),
                    observation_id=observation_id,
                    blocked_mechanism=True,
                )
            )
        return assessments


def _component_contains_route(
    view: ResearchView, component: tuple[str, ...], route_id: str | None
) -> bool:
    if route_id is None:
        return True
    route = view.get_route(route_id)
    if route is None:
        return True
    return route.target_obligation_id in set(component)


def _recommendation(
    report: SCCReport, exhausted: set[str]
) -> tuple[RecurrenceRecommendation, bool]:
    classification = report.classification
    if classification is SCCClassification.HAS_EXTERNAL_EXIT:
        return RecurrenceRecommendation.CHANGE_ROUTE, False
    if classification is SCCClassification.RECURRENT_NO_EXIT:
        if exhausted:
            rec = RecurrenceRecommendation.PARK_ROUTE_CANDIDATE
        else:
            rec = RecurrenceRecommendation.BOUNDED_AUDIT
        return rec, True
    if classification is SCCClassification.SUSPECTED_RECURRENCE:
        return RecurrenceRecommendation.CONTINUE, False
    return RecurrenceRecommendation.CONTINUE, False


def _parse_progress_kind(kind: object) -> ProgressKind | None:
    if kind is None:
        return None
    if isinstance(kind, ProgressKind):
        return kind
    if isinstance(kind, str):
        try:
            return ProgressKind(kind)
        except ValueError as exc:
            raise ValueError(f"unknown progress_kind {kind!r}") from exc
    raise TypeError("progress_kind must be ProgressKind, str, or None")


def _require_hex64(name: str, value: object) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 64-char lowercase hex hash")
    return value


def _tool_hash_value(tool_hash: str) -> str:
    if not isinstance(tool_hash, str):
        raise TypeError("tool_hash must be a str")
    if _HEX64_RE.fullmatch(tool_hash):
        return tool_hash
    return stable_id("tool-frontier", 1, tool_hash)
