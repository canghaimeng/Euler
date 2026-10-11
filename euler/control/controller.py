"""Live Euler planning and lease-bound dispatch.

Stdlib plus existing control/runtime/context APIs. Does not write the Fact Graph
or launch a provider unless ``dispatch(..., launch=True)`` after ready.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from euler.control.adapters.project import snapshot_project
from euler.control.context import (
    ContextCompiler,
    ContextTooLarge,
    MissingRequiredContext,
)
from euler.control.contracts import ValidityStatus
from euler.control.dedupe import build_action_attempt_index
from euler.control.dispatch import (
    DispatchError,
    atomic_write_sidecar,
    atomic_write_task_and_sidecar,
    hash_text,
    load_dispatch,
    recover_unready_intent,
    sidecar_document,
    sidecar_path,
    start_managed_worker,
    task_path,
    validate_dispatch,
)
from euler.control.event_store import IdempotencyConflict
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import (
    EVENT_DISPATCH_INTENT,
    EVENT_DISPATCH_PLANNED,
    EVENT_DISPATCH_READY,
    DispatchBinding,
    ProposedCandidate,
)
from euler.control.project import EulerProject, EulerProjectError
from euler.control.reconcile import (
    action_frontier_hash,
    context_version_digest,
    scope_digest,
)
from euler.control.runtime import LeaseStatus
from euler.control.persistent_recurrence import (
    analyze_project_recurrence,
    synchronize_project_recurrence,
)
from euler.control.recurrence import RecurrenceAssessment
from euler.control.scheduler import (
    ActiveWork,
    CandidateAction,
    DependencyValidity,
    DispatchPlan,
    ScheduleDisposition,
    ScheduleReason,
    TransparentScheduler,
    rebind_candidate_with_recall,
)
from euler.control.schema import EventEnvelope
from euler.execution.context import CONTEXT_ID_RE, render_task_markdown

_ACTOR = "controller"
EVENT_ACTION_REUSED = "euler.action_reused"

__all__ = [
    "EulerControllerError",
    "EulerController",
    "EVENT_ACTION_REUSED",
    "analyze_project_recurrence",
    "synchronize_project_recurrence",
]


class EulerControllerError(EulerProjectError):
    """Scheduling or dispatch could not proceed fail-closed."""


class EulerController:
    """Propose → schedule → lease-bound dispatch over an opened EulerProject."""

    def __init__(self, project: EulerProject, *, actor: str = _ACTOR) -> None:
        if not isinstance(project, EulerProject):
            raise EulerControllerError("project must be an EulerProject")
        if not actor:
            raise EulerControllerError("actor must be nonempty")
        self._project = project
        self._actor = actor
        self._scheduler = TransparentScheduler()
        self._compiler = ContextCompiler()

    @property
    def project(self) -> EulerProject:
        return self._project

    def propose(
        self,
        candidate: CandidateAction,
        worker: str,
        *,
        timestamp_utc: str | None = None,
    ) -> ProposedCandidate:
        ts = timestamp_utc or _utc_now()
        try:
            return self._project.append_proposed_candidate(
                candidate, worker, actor=self._actor, timestamp_utc=ts
            )
        except EulerProjectError:
            raise
        except Exception as exc:
            raise EulerControllerError(str(exc)) from exc

    def withdraw(self, candidate_id: str, *, reason: str,
                 timestamp_utc: str | None = None) -> None:
        """Retire an unlaunched proposal with an auditable control event."""
        self._proposal(candidate_id)
        if self._is_dispatched(candidate_id):
            raise EulerControllerError("cannot withdraw a dispatched candidate")
        if not reason.strip():
            raise EulerControllerError("withdrawal requires a reason")
        self._project.event_store.append(EventEnvelope.build(
            project_id=self._project.project_id,
            actor=self._actor,
            event_type="euler.candidate_withdrawn",
            idempotency_key=f"candidate-withdrawn:{candidate_id}",
            payload={"candidate_id": candidate_id, "reason": reason},
            timestamp_utc=timestamp_utc or _utc_now(),
            correlation_id=self._project.project_id,
        ))

    def schedule(self, *, timestamp_utc: str | None = None) -> DispatchPlan:
        ts = timestamp_utc or _utc_now()
        from euler.control.research_service import ResearchService

        research = ResearchService(self._project, actor=self._actor)
        research.sync_fact_dependencies(timestamp_utc=ts)
        for obligation in self._project.research_view().obligations():
            if obligation.status.value in {"OPEN", "IN_PROGRESS"}:
                research.reuse_verified_evidence(
                    obligation.obligation_id, timestamp_utc=ts, synchronize=False
                )
        view = self._project.research_view()
        runtime = self._project.runtime_snapshot()
        snapshot = snapshot_project(self._project.project_dir)
        history = synchronize_project_recurrence(
            self._project, actor=self._actor, timestamp_utc=ts
        )
        events = list(self._project.event_store.iter_project(self._project.project_id))
        dedupe_index = build_action_attempt_index(events=events, leases=runtime.leases)
        assessments = history.analyze_all(view)
        proposals = self._nonterminal_candidates(runtime)
        bound: dict[str, CandidateAction] = {}
        selectable: list[CandidateAction] = []
        for proposal in proposals:
            try:
                candidate = self._connect_durable_memory(
                    proposal.candidate, view, snapshot=snapshot
                )
            except EulerControllerError:
                candidate = replace(
                    proposal.candidate,
                    dependency_validity=DependencyValidity.MISSING,
                )
            candidate = _overlay_recurrence(candidate, assessments)
            bound[candidate.candidate_id] = candidate
            selectable.append(candidate)
        active = _active_work(runtime)
        plan = self._scheduler.select(
            tuple(selectable),
            policy=self._project.policy,
            snapshot=runtime.resources,
            now_utc=ts,
            view=view,
            active=active,
            dedupe_index=dedupe_index,
            history=history,
        )
        self._persist_reuse_records(plan, bound, dedupe_index, ts)
        blob = canonical_json(plan.to_dict()).encode("utf-8")
        plan_hash = sha256_hex(blob)
        ref = self._project.artifacts.put_bytes(blob, media_type="application/json")
        if ref.content_hash != plan_hash:
            raise EulerControllerError("dispatch plan CAS hash mismatch")
        envelope = EventEnvelope.build(
            project_id=self._project.project_id,
            actor=self._actor,
            event_type=EVENT_DISPATCH_PLANNED,
            idempotency_key=f"dispatch-planned:{plan_hash}",
            payload={
                "plan": plan.to_dict(),
                "plan_hash": plan_hash,
                "artifact_hash": ref.content_hash,
                "selected_candidate_ids": list(plan.selected_candidate_ids),
                "bound_candidates": {
                    cid: bound[cid].to_dict() for cid in sorted(bound)
                },
            },
            timestamp_utc=ts,
            correlation_id=self._project.project_id,
        )
        try:
            stored = self._project.event_store.append(envelope)
        except IdempotencyConflict as exc:
            raise EulerControllerError(str(exc)) from exc
        return DispatchPlan.from_dict(dict(stored.event.payload)["plan"])

    def current_plan(self) -> DispatchPlan | None:
        found = None
        for _seq, envelope in self._project.event_store.iter_project(self._project.project_id):
            if envelope.event_type == EVENT_DISPATCH_PLANNED:
                found = DispatchPlan.from_dict(dict(envelope.payload)["plan"])
        return found

    def dispatch(self, candidate_id: str, *, launch: bool = False,
                 timestamp_utc: str | None = None) -> DispatchBinding:
        from .campaign import admission_lock, assert_dispatch_allowed, CampaignError
        with admission_lock(self._project.project_dir):
            try:
                assert_dispatch_allowed(self._project, candidate_id)
            except (CampaignError, ValueError) as exc:
                raise EulerControllerError(str(exc)) from exc
            return self._dispatch_admitted(candidate_id, launch=launch, timestamp_utc=timestamp_utc)

    def _dispatch_admitted(
        self,
        candidate_id: str,
        *,
        launch: bool = False,
        timestamp_utc: str | None = None,
    ) -> DispatchBinding:
        if not candidate_id:
            raise EulerControllerError("candidate_id is required")
        ts = timestamp_utc or _utc_now()
        proposal = self._proposal(candidate_id)
        if any(
            envelope.event_type == "euler.candidate_withdrawn"
            and dict(envelope.payload).get("candidate_id") == candidate_id
            for _seq, envelope in self._project.event_store.iter_project(self._project.project_id)
        ):
            raise EulerControllerError("cannot dispatch a withdrawn candidate")
        recover_unready_intent(
            self._project.project_dir, proposal.worker, actor=self._actor, timestamp_utc=ts
        )
        existing = self._ready_binding(candidate_id)
        if existing is not None:
            if launch:
                start_managed_worker(self._project.project_dir, existing.worker)
            return existing
        plan = self.current_plan()
        if plan is None:
            raise EulerControllerError("no dispatch plan; call schedule() first")
        try:
            decision = plan.decision_for(candidate_id)
        except Exception as exc:
            raise EulerControllerError(
                f"candidate {candidate_id!r} is not in the current plan"
            ) from exc
        if decision.disposition is not ScheduleDisposition.SELECTED:
            raise EulerControllerError(
                f"candidate {candidate_id!r} is {decision.disposition.value}, not SELECTED"
            )
        if self._is_dispatched(candidate_id):
            raise EulerControllerError(f"candidate {candidate_id!r} is already dispatched")
        self._assert_worker_free(proposal.worker, candidate_id)

        view = self._project.research_view()
        snapshot = snapshot_project(self._project.project_dir)
        candidate = self._bound_candidate(candidate_id) or proposal.candidate
        candidate = self._connect_durable_memory(candidate, view, snapshot=snapshot)
        try:
            packet = self._compiler.compile(
                candidate.context_request,
                view,
                snapshot,
                self._project.artifacts,
            )
        except (MissingRequiredContext, ContextTooLarge) as exc:
            raise EulerControllerError(
                f"required context unavailable; defer/reject dispatch: {exc}"
            ) from exc
        except Exception as exc:
            raise EulerControllerError(f"context compile failed: {exc}") from exc
        packet_bytes = packet.rendered_utf8()
        packet_ref = self._project.artifacts.put_bytes(
            packet_bytes, media_type="text/plain"
        )
        packet_hash = packet_ref.content_hash
        cache_key_hash = packet.cache_key.key_id
        obligation = view.get_obligation(candidate.obligation_id)
        if obligation is None:
            raise EulerControllerError(
                f"candidate obligation {candidate.obligation_id} is missing"
            )
        required_ids = tuple(candidate.context_request.required_fact_ids)
        active = {item.fact_id: item for item in snapshot.active_facts}
        required_pairs: list[tuple[str, str]] = []
        for fact_id in required_ids:
            fact = active.get(fact_id)
            if fact is None:
                raise EulerControllerError(
                    f"required Fact {fact_id} is missing at dispatch"
                )
            required_pairs.append((fact_id, fact.content_sha256))
        scope_hash = scope_digest(obligation.scope)
        dependency_hash = candidate.action_spec.dependency_hash
        context_version_hash = context_version_digest(
            cache_key_hash=cache_key_hash,
            packet_hash=packet_hash,
            reference_versions_hash=candidate.context_request.reference_versions_hash,
            tool_environment_hash=candidate.context_request.tool_environment_hash,
            definitions_hash=candidate.action_spec.definitions_hash,
        )
        frontier_hash = action_frontier_hash(
            obligation_id=candidate.obligation_id,
            scope_hash=scope_hash,
            required_facts=required_pairs,
            dependency_hash=dependency_hash,
            context_version_hash=context_version_hash,
        )
        baseline_evidence_ids = tuple(
            item.artifact_id for item in view.evidence()
        )
        generation = self._lease_generation(candidate_id)
        lease_key = f"dispatch-lease:{candidate_id}:{generation}"
        try:
            with self._project.event_store.transaction(immediate=True):
                lease = self._project.runtime.issue(
                    self._project.project_id,
                    candidate,
                    idempotency_key=lease_key,
                    actor=self._actor,
                    timestamp_utc=ts,
                    context_cache_key=cache_key_hash,
                )
                task_text = _render_task(
                    project_id=self._project.project_id,
                    candidate=candidate,
                    worker=proposal.worker,
                    packet_text=packet.rendered_text,
                    packet_hash=packet_hash,
                    cache_key_hash=cache_key_hash,
                    lease_id=lease.lease_id,
                    attempt_id=lease.attempt_id,
                )
                task_hash = hash_text(task_text)
                binding = DispatchBinding.build(
                    worker=proposal.worker,
                    candidate_id=candidate_id,
                    action_id=candidate.action_spec.action_id,
                    lease_id=lease.lease_id,
                    attempt_id=lease.attempt_id,
                    packet_hash=packet_hash,
                    cache_key_hash=cache_key_hash,
                    task_hash=task_hash,
                    baseline_fact_ids=tuple(
                        item.fact_id for item in snapshot.active_facts
                    ),
                    frontier_hash=frontier_hash,
                    ready=False,
                    obligation_id=candidate.obligation_id,
                    scope_hash=scope_hash,
                    dependency_hash=dependency_hash,
                    context_version_hash=context_version_hash,
                    required_fact_ids=required_ids,
                    required_fact_versions=tuple(item[1] for item in required_pairs),
                    required_evidence_ids=tuple(
                        candidate.context_request.required_evidence_ids
                    ),
                    baseline_evidence_ids=baseline_evidence_ids,
                    baseline_relation_ids=tuple(r.relation_id for r in view.relations()),
                )
                self._project.event_store.append(
                    EventEnvelope.build(
                        project_id=self._project.project_id,
                        actor=self._actor,
                        event_type=EVENT_DISPATCH_INTENT,
                        idempotency_key=binding.intent_id,
                        payload={
                            "binding": binding.to_dict(),
                            "plan_hash": _plan_hash(plan),
                            "packet_artifact_hash": packet_hash,
                            "lease_idempotency_key": lease_key,
                        },
                        timestamp_utc=ts,
                        correlation_id=lease.lease_id,
                    )
                )
        except Exception as exc:
            raise EulerControllerError(f"lease+intent commit failed: {exc}") from exc

        sidecar = sidecar_path(self._project.project_dir, proposal.worker)
        task = task_path(self._project.project_dir, proposal.worker)
        intent_doc = sidecar_document(
            state="intent",
            binding=binding,
            plan_hash=_plan_hash(plan),
            packet_artifact_hash=packet_hash,
        )
        try:
            atomic_write_task_and_sidecar(task, task_text, sidecar, intent_doc)
        except OSError as exc:
            self._project.runtime.cancel(
                self._project.project_id,
                binding.lease_id,
                actor=self._actor,
                timestamp_utc=ts,
            )
            raise EulerControllerError(
                f"dispatch file write failed; lease released: {exc}"
            ) from exc

        try:
            if hash_text(task.read_text(encoding="utf-8")) != task_hash:
                raise DispatchError("TASK.md hash mismatch after write")
            loaded = load_dispatch(self._project.project_dir, proposal.worker)
            if loaded is None or loaded.get("state") != "intent":
                raise DispatchError("intent sidecar missing after write")
        except Exception as exc:
            self._project.runtime.cancel(
                self._project.project_id,
                binding.lease_id,
                actor=self._actor,
                timestamp_utc=ts,
            )
            raise EulerControllerError(
                f"dispatch file verify failed; lease released: {exc}"
            ) from exc

        ready_binding = DispatchBinding.build(
            worker=proposal.worker,
            candidate_id=candidate_id,
            action_id=candidate.action_spec.action_id,
            lease_id=binding.lease_id,
            attempt_id=binding.attempt_id,
            packet_hash=packet_hash,
            cache_key_hash=cache_key_hash,
            task_hash=task_hash,
            baseline_fact_ids=binding.baseline_fact_ids,
            frontier_hash=frontier_hash,
            ready=True,
            obligation_id=binding.obligation_id,
            scope_hash=binding.scope_hash,
            dependency_hash=binding.dependency_hash,
            context_version_hash=binding.context_version_hash,
            required_fact_ids=binding.required_fact_ids,
            required_fact_versions=binding.required_fact_versions,
            required_evidence_ids=binding.required_evidence_ids,
            baseline_evidence_ids=binding.baseline_evidence_ids,
            baseline_relation_ids=binding.baseline_relation_ids,
        )
        ready_doc = sidecar_document(
            state="ready",
            binding=ready_binding,
            plan_hash=_plan_hash(plan),
            packet_artifact_hash=packet_hash,
        )
        try:
            atomic_write_sidecar(sidecar, ready_doc)
            self._project.event_store.append(
                EventEnvelope.build(
                    project_id=self._project.project_id,
                    actor=self._actor,
                    event_type=EVENT_DISPATCH_READY,
                    idempotency_key=ready_binding.ready_id or ready_binding.intent_id,
                    payload={
                        "binding": ready_binding.to_dict(),
                        "plan_hash": _plan_hash(plan),
                        "packet_artifact_hash": packet_hash,
                    },
                    timestamp_utc=ts,
                    correlation_id=binding.lease_id,
                    causation_id=binding.intent_id,
                )
            )
        except Exception as exc:
            self._project.runtime.cancel(
                self._project.project_id,
                binding.lease_id,
                actor=self._actor,
                timestamp_utc=ts,
            )
            raise EulerControllerError(
                f"dispatch ready failed; lease released: {exc}"
            ) from exc

        validate_dispatch(
            self._project.project_dir, proposal.worker, require_ready=True
        )
        if launch:
            start_managed_worker(self._project.project_dir, proposal.worker)
        return ready_binding

    def _proposal(self, candidate_id: str) -> ProposedCandidate:
        found = None
        for item in self._project.load_proposed_candidates():
            if item.candidate.candidate_id == candidate_id:
                found = item
        if found is None:
            raise EulerControllerError(f"unknown candidate {candidate_id!r}")
        return found

    def _nonterminal_candidates(self, runtime: Any) -> tuple[ProposedCandidate, ...]:
        active_ids = {
            item.candidate_id
            for item in runtime.leases
            if item.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}
        }
        ready_ids = self._ready_candidate_ids()
        withdrawn_ids = {
            dict(envelope.payload).get("candidate_id")
            for _seq, envelope in self._project.event_store.iter_project(self._project.project_id)
            if envelope.event_type == "euler.candidate_withdrawn"
        }
        out = []
        for item in self._project.load_proposed_candidates():
            cid = item.candidate.candidate_id
            if cid in active_ids or cid in ready_ids or cid in withdrawn_ids:
                continue
            from .campaign import assert_dispatch_allowed
            try:
                assert_dispatch_allowed(self._project, cid)
            except ValueError:
                continue
            out.append(item)
        return tuple(out)

    def _ready_candidate_ids(self) -> set[str]:
        ids: set[str] = set()
        for _seq, envelope in self._project.event_store.iter_project(self._project.project_id):
            if envelope.event_type != EVENT_DISPATCH_READY:
                continue
            raw = dict(envelope.payload).get("binding")
            if isinstance(raw, dict) and raw.get("candidate_id"):
                ids.add(str(raw["candidate_id"]))
        # Pause releases unstarted leases. They may be admitted afresh after
        # resume; completed or previously running work is never replayed here.
        runtime = self._project.runtime_snapshot()
        for cid in tuple(ids):
            leases = [l for l in runtime.leases if l.candidate_id == cid]
            if leases and all(l.status is LeaseStatus.CANCELLED and l.running_at is None for l in leases):
                ids.discard(cid)
        return ids

    def _ready_binding(self, candidate_id: str) -> DispatchBinding | None:
        found = None
        for _seq, envelope in self._project.event_store.iter_project(self._project.project_id):
            if envelope.event_type != EVENT_DISPATCH_READY:
                continue
            raw = dict(envelope.payload).get("binding")
            if isinstance(raw, dict) and raw.get("candidate_id") == candidate_id:
                found = DispatchBinding.from_dict(raw)
        if found:
            lease = next((l for l in self._project.runtime_snapshot().leases if l.lease_id == found.lease_id), None)
            if lease and lease.status is LeaseStatus.CANCELLED and lease.running_at is None:
                return None
        return found

    def _is_dispatched(self, candidate_id: str) -> bool:
        if candidate_id in self._ready_candidate_ids():
            return True
        runtime = self._project.runtime_snapshot()
        return any(
            item.candidate_id == candidate_id
            and item.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}
            for item in runtime.leases
        )

    def _assert_worker_free(self, worker: str, candidate_id: str) -> None:
        runtime = self._project.runtime_snapshot()
        payload = load_dispatch(self._project.project_dir, worker)
        if payload is not None and payload.get("state") == "ready":
            raw = payload.get("binding")
            if isinstance(raw, dict) and raw.get("candidate_id") != candidate_id:
                previous = next(
                    (lease for lease in runtime.leases
                     if lease.lease_id == raw.get("lease_id")), None,
                )
                if previous is None or previous.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
                    raise EulerControllerError(
                        f"worker {worker!r} already has a ready dispatch"
                    )
        for lease in runtime.leases:
            if lease.status not in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
                continue
            if lease.candidate_id == candidate_id:
                continue
            other = self._worker_for_lease(lease.lease_id)
            if other == worker:
                raise EulerControllerError(
                    f"worker {worker!r} already has an active lease"
                )

    def _worker_for_lease(self, lease_id: str) -> str | None:
        for _seq, envelope in self._project.event_store.iter_project(self._project.project_id):
            if envelope.event_type != EVENT_DISPATCH_INTENT:
                continue
            raw = dict(envelope.payload).get("binding")
            if isinstance(raw, dict) and raw.get("lease_id") == lease_id:
                return str(raw.get("worker"))
        return None

    def _lease_generation(self, candidate_id: str) -> int:
        n = 0
        runtime = self._project.runtime_snapshot()
        for lease in runtime.leases:
            if lease.candidate_id == candidate_id:
                n += 1
        return n + 1

    def _connect_durable_memory(
        self,
        candidate: CandidateAction,
        view: object,
        *,
        snapshot: object = None,
    ) -> CandidateAction:
        """Bind inferred dependency/blocker IDs into action identity and request."""
        if not isinstance(candidate, CandidateAction):
            raise EulerControllerError("candidate must be a CandidateAction")
        from euler.control.views import ResearchView

        if not isinstance(view, ResearchView):
            raise EulerControllerError("view must be a ResearchView")
        if snapshot is None:
            snapshot = snapshot_project(self._project.project_dir)
        try:
            bound = rebind_candidate_with_recall(candidate, view, snapshot)
        except Exception as exc:
            raise EulerControllerError(f"durable recall bind failed: {exc}") from exc
        self._assert_required_evidence(bound, view)
        return bound

    def _assert_required_evidence(self, candidate: CandidateAction, view: object) -> None:
        from euler.control.views import ResearchView

        if not isinstance(view, ResearchView):
            raise EulerControllerError("view must be a ResearchView")
        for artifact_id in candidate.context_request.required_evidence_ids:
            artifact = view.get_evidence(artifact_id)
            if artifact is None:
                raise EulerControllerError(
                    f"required evidence {artifact_id} is missing; defer/reject"
                )
            validity = view.evidence_validity(artifact_id) or artifact.validity
            if validity is not ValidityStatus.VALID or artifact.validity is not ValidityStatus.VALID:
                raise EulerControllerError(
                    f"required evidence {artifact_id} is revoked or stale; defer/reject"
                )

    def _bound_candidate(self, candidate_id: str) -> CandidateAction | None:
        found = None
        for _seq, envelope in self._project.event_store.iter_project(self._project.project_id):
            if envelope.event_type != EVENT_DISPATCH_PLANNED:
                continue
            raw = dict(envelope.payload).get("bound_candidates")
            if not isinstance(raw, dict):
                continue
            item = raw.get(candidate_id)
            if isinstance(item, dict):
                found = CandidateAction.from_dict(item)
        return found

    def _persist_reuse_records(
        self,
        plan: DispatchPlan,
        bound: dict[str, CandidateAction],
        dedupe_index: object,
        timestamp_utc: str,
    ) -> None:
        recovered_fn = getattr(dedupe_index, "recovered_record", None)
        for decision in plan.decisions:
            if decision.reason is not ScheduleReason.DEDUPE_RECOVER:
                continue
            candidate = bound.get(decision.candidate_id)
            if candidate is None or recovered_fn is None:
                continue
            record = recovered_fn(candidate.action_spec)
            if record is None:
                continue
            try:
                self._project.event_store.append(
                    EventEnvelope.build(
                        project_id=self._project.project_id,
                        actor=self._actor,
                        event_type=EVENT_ACTION_REUSED,
                        idempotency_key=(
                            f"action-reused:{candidate.action_spec.action_id}:"
                            f"{record.attempt_id}"
                        ),
                        payload={
                            "candidate_id": candidate.candidate_id,
                            "obligation_id": candidate.obligation_id,
                            "action_id": candidate.action_spec.action_id,
                            "attempt_id": record.attempt_id,
                            "artifact_refs": list(record.artifact_refs),
                            "result": record.result,
                            "detail": decision.detail,
                        },
                        timestamp_utc=timestamp_utc,
                        correlation_id=candidate.obligation_id,
                    )
                )
            except IdempotencyConflict:
                continue


def _overlay_recurrence(
    candidate: CandidateAction,
    assessments: RecurrenceAssessment | tuple[RecurrenceAssessment, ...],
) -> CandidateAction:
    if candidate.action_kind.value == "AUDIT":
        return candidate
    if isinstance(assessments, RecurrenceAssessment):
        items = (assessments,)
    else:
        items = tuple(assessments)
    if candidate.route_id is None:
        return candidate
    for assessment in items:
        if not assessment.audit_required:
            continue
        if not _candidate_on_assessed_route(candidate, assessment):
            continue
        return replace(
            candidate,
            recurrence_recommendation=assessment.recommendation,
            recurrence_audit_required=True,
            recurrence_assessment=assessment,
        )
    return candidate


def _candidate_on_assessed_route(
    candidate: CandidateAction, assessment: RecurrenceAssessment
) -> bool:
    route_id = candidate.route_id
    if route_id is None:
        return False
    if route_id in assessment.exhausted_route_ids:
        return True
    if assessment.blocked_mechanism and route_id in assessment.strike_key.nodes:
        return True
    for edge in assessment.report.internal_edges:
        if edge.route_id == route_id:
            return True
    return False


def _active_work(runtime: Any) -> tuple[ActiveWork, ...]:
    out: list[ActiveWork] = []
    for lease in runtime.leases:
        if lease.status not in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
            continue
        if not lease.obligation_id:
            continue
        out.append(
            ActiveWork(
                obligation_id=lease.obligation_id,
                mechanism_family=lease.mechanism,
                action_id=lease.action_id,
                slot_kind=lease.slot_kind,
            )
        )
    return tuple(out)


def _plan_hash(plan: DispatchPlan) -> str:
    return sha256_hex(canonical_json(plan.to_dict()).encode("utf-8"))


def _render_task(
    *,
    project_id: str,
    candidate: CandidateAction,
    worker: str,
    packet_text: str,
    packet_hash: str,
    cache_key_hash: str,
    lease_id: str = "pending",
    attempt_id: str = "pending",
) -> str:
    del worker
    branch = candidate.obligation_id
    subgoal = candidate.candidate_id
    if CONTEXT_ID_RE.match(subgoal) is None:
        subgoal = sha256_hex(subgoal.encode("utf-8"))
    body = (
        f"# Euler dispatch\n"
        f"candidate_id: {candidate.candidate_id}\n"
        f"action_id: {candidate.action_spec.action_id}\n"
        f"lease_id: {lease_id}\n"
        f"attempt_id: {attempt_id}\n"
        f"output_contract: {candidate.context_request.output_contract}\n"
        f"packet_hash: {packet_hash}\n"
        f"cache_key_hash: {cache_key_hash}\n"
        f"\n## ContextPacket\n\n{packet_text.rstrip()}\n"
    )
    return render_task_markdown(
        project_id=project_id, branch_id=branch, subgoal_id=subgoal, body=body
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
