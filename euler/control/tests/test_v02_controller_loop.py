"""v0.2 controller + managed-loop wiring for recall, recurrence, and reuse.

Temp projects only. No live provider, verifier, or Fact admission.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from euler.control.adapters.project import snapshot_project
from euler.control.context import ContextCompiler, ContextLimits
from euler.control.controller import (
    EVENT_ACTION_REUSED,
    EulerController,
    EulerControllerError,
    analyze_project_recurrence,
    synchronize_project_recurrence,
)
from euler.control.contracts import (
    CheckerResult,
    EvidenceArtifact,
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.obligations import Obligation, ObligationStatus, Route
from euler.control.persistent_recurrence import (
    EVENT_RECURRENCE_OBSERVED,
    RecurrenceObservation,
    observe_recurrence,
)
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.progress import ProgressKind, ProgressReceipt
from euler.control.project import initialize_project
from euler.control.recurrence import RecurrenceHistory, RecurrenceRecommendation
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    ScheduleDisposition,
    ScheduleReason,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.control.schema import (
    EventEnvelope,
    MathematicalStatus,
    ObservationStatus,
    ObservedQuantity,
    UsageRecord,
)
from euler.control.views import ResearchView
from euler.execution import layout as L
from euler.execution.loop import kickoff
from euler.execution.memory_digest import prepare_round_start

_TS = "2026-01-01T00:00:00Z"


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts))


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain="Z"),
        ),
        objects=(),
        parameter_domain="Z",
        definitions_hash=_hex("defs"),
    )


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="goldbach",
        version=1,
        exact_statement="every even integer greater than 2 is a sum of two primes",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("proof checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract, **kwargs) -> Obligation:
    fields = dict(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="proof",
        created_by="researcher",
        version=1,
        status=ObligationStatus.OPEN,
    )
    fields.update(kwargs)
    return Obligation.build(**fields)


def _policy(*, reasoning_slots: int = 2) -> BudgetPolicy:
    unbound = DimensionLimit.unlimited()
    return BudgetPolicy(
        cost_microusd=unbound,
        input_tokens=unbound,
        output_tokens=unbound,
        reasoning_tokens=unbound,
        cached_tokens=unbound,
        wall_ms=unbound,
        tool_cpu_ms=unbound,
        tool_gpu_ms=unbound,
        max_reasoning_slots=reasoning_slots,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=1,
    )


def _usage() -> UsageRecord:
    def qty(unit: str, value: int | None = 1) -> ObservedQuantity:
        return ObservedQuantity(
            unit=unit,
            status=ObservationStatus.OBSERVED if value is not None else ObservationStatus.UNKNOWN,
            value=value,
        )

    return UsageRecord(
        input_tokens=qty("tokens"),
        output_tokens=qty("tokens"),
        reasoning_tokens=qty("tokens", 0),
        cached_tokens=qty("tokens", 0),
        cost_microusd=qty("microusd"),
        wall_ms=qty("ms"),
        tool_cpu_ms=qty("ms", 0),
        tool_gpu_ms=qty("ms", 0),
    )


def _candidate(contract, obligation, tag: str, **overrides) -> CandidateAction:
    kwargs = dict(
        contract_id=contract.contract_id,
        obligation_id=obligation.obligation_id,
        concrete_gap="close the even-integer case",
        output_contract="return a KERNEL_CHECKED_PROOF",
        tool_environment_hash=_hex("tool"),
        acceptance_policy_hash=_hex("accept"),
        reference_versions_hash=_hex("refs"),
        limits=ContextLimits.unlimited(),
        action_type="prove",
        mechanism=tag,
        model_id="grok-4.6",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"k": tag}),
        dependency_hash=_hex("dep", tag),
        definitions_hash=_hex("defs"),
    )
    kwargs.update(overrides)
    spec, request, seed = build_action_context_pair(**kwargs)
    return CandidateAction(
        candidate_id=tag,
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation.obligation_id,
        route_id=request.route_id,
        mechanism_family=spec.mechanism,
        action_kind=ActionKind.PROVE,
        slot_kind=slot_kind_for_action(ActionKind.PROVE),
        budget_class=BudgetClass.NORMAL,
        requested=ResourceVector(
            cost_microusd=1,
            input_tokens=1,
            output_tokens=1,
            reasoning_tokens=0,
            cached_tokens=0,
            wall_ms=1,
            tool_cpu_ms=0,
            tool_gpu_ms=0,
        ),
        priority_class=PriorityClass.NORMAL,
        dependency_validity=DependencyValidity.VALID,
        recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
        recurrence_audit_required=False,
        created_sequence=1,
    )


def _managed(tmp: Path, *, workers: tuple[str, ...] = ("w1", "w2")):
    root = tmp / "demo"
    root.mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps({"name": "demo", "workers": list(workers)}, indent=2) + "\n",
        encoding="utf-8",
    )
    for name in workers:
        (root / "workers" / name).mkdir(parents=True)
    contract = _contract()
    obligation = _obligation(contract)
    project = initialize_project(
        root, contract, obligation, _policy(reasoning_slots=len(workers)), "tester", _TS
    )
    return project, contract, obligation


def _event(project, event_type: str, payload: dict, key: str, ts: str = _TS) -> None:
    project.event_store.append(
        EventEnvelope.build(
            project_id=project.project_id,
            actor="tester",
            event_type=event_type,
            idempotency_key=key,
            payload=payload,
            timestamp_utc=ts,
        )
    )


def _install_cycle(project, contract, parent: Obligation):
    child = _obligation(contract, exact_statement="cycle child", version=2)
    r1 = Route.build(
        target_obligation_id=parent.obligation_id,
        mechanism="to-child",
        required_obligation_ids=(child.obligation_id,),
        alternative_group="or-1",
    )
    r_back = Route.build(
        target_obligation_id=child.obligation_id,
        mechanism="to-parent",
        required_obligation_ids=(parent.obligation_id,),
    )
    _event(project, "obligation.created", {"obligation": child.to_dict()}, "cycle-child")
    _event(project, "route.created", {"route": r1.to_dict()}, "cycle-r1")
    _event(project, "route.created", {"route": r_back.to_dict()}, "cycle-back")
    return child, r1, r_back


def _progress_receipt(lease, contract, *, kind, tool_hash: str, route_id: str | None = None):
    kwargs = dict(
        action_id=lease.action_id,
        attempt_id=lease.attempt_id,
        contract_id=contract.contract_id,
        kind=kind,
        claim=contract.exact_statement,
        scope=contract.scope,
        old_frontier_hash=_hex("front-old"),
        new_frontier_hash=_hex("front-old"),
        created_at=_TS,
        old_tool_environment_hash=tool_hash,
        new_tool_environment_hash=tool_hash,
    )
    if route_id is not None:
        kwargs["affected_route_ids"] = (route_id,)
        kwargs["affected_obligation_ids"] = ()
    return ProgressReceipt.build(**kwargs)


def _complete_with_progress(project, binding, contract, *, kind, tool_hash, route_id=None, ts=_TS):
    blob = project.artifacts.put_bytes(b"attempt-bytes", media_type="text/plain")
    lease = next(
        item
        for item in project.runtime_snapshot().leases
        if item.lease_id == binding.lease_id
    )
    progress = _progress_receipt(
        lease, contract, kind=kind, tool_hash=tool_hash, route_id=route_id
    )
    return project.runtime.complete(
        project.project_id,
        binding.lease_id,
        usage=_usage(),
        artifact_refs=(blob.content_hash,),
        mathematical_status=MathematicalStatus.NO_CLAIM,
        progress=progress,
        view=project.research_view(),
        timestamp_utc=ts,
    )


def _observation_events(project):
    return [
        env
        for _seq, env in project.event_store.iter_project(project.project_id)
        if env.event_type == EVENT_RECURRENCE_OBSERVED
    ]


def test_schedule_uses_event_index_and_durable_recurrence():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw))
        try:
            ctl = EulerController(project, actor="tester")
            cand = _candidate(contract, obligation, "alpha")
            ctl.propose(cand, "w1", timestamp_utc=_TS)
            plan = ctl.schedule(timestamp_utc=_TS)
            assert "alpha" in plan.selected_candidate_ids
            binding = ctl.dispatch("alpha", timestamp_utc=_TS)
            task = (project.project_dir / "workers" / "w1" / "TASK.md").read_text(
                encoding="utf-8"
            )
            assert "===== CONTRACT =====" in task
            assert "proof_handle:" in task or "===== FACTS =====" in task
            blob = project.artifacts.put_bytes(b"proof-bytes", media_type="text/plain")
            project.runtime.complete(
                project.project_id,
                binding.lease_id,
                usage=_usage(),
                artifact_refs=(blob.content_hash,),
                mathematical_status=MathematicalStatus.NO_CLAIM,
                timestamp_utc="2026-01-01T00:00:01Z",
            )
            history = synchronize_project_recurrence(project, actor="tester", timestamp_utc=_TS)
            types = [
                env.event_type
                for _seq, env in project.event_store.iter_project(project.project_id)
            ]
            assert EVENT_RECURRENCE_OBSERVED in types
            first = analyze_project_recurrence(project)
            poll = analyze_project_recurrence(project)
            assert poll["strike_count"] == first["strike_count"]
            assert poll["observation_ids"] == first["observation_ids"]
            assert history._observed

            twin = _candidate(contract, obligation, "alpha-reuse", mechanism="alpha")
            # same mechanism+params as alpha except candidate_id/tag in parameters
            reuse = _candidate(contract, obligation, "reuse")
            # Force identical action by proposing a clone of the bound dispatched spec.
            bound = None
            for _seq, env in project.event_store.iter_project(project.project_id):
                if env.event_type != "euler.dispatch_planned":
                    continue
                raw_bound = dict(env.payload).get("bound_candidates") or {}
                if "alpha" in raw_bound:
                    bound = CandidateAction.from_dict(raw_bound["alpha"])
            assert bound is not None
            clone = CandidateAction(
                candidate_id="clone",
                action_spec=bound.action_spec,
                context_request=bound.context_request,
                context_seed=bound.context_seed,
                obligation_id=bound.obligation_id,
                route_id=bound.route_id,
                mechanism_family=bound.mechanism_family,
                action_kind=bound.action_kind,
                slot_kind=bound.slot_kind,
                budget_class=bound.budget_class,
                requested=bound.requested,
                priority_class=bound.priority_class,
                dependency_validity=bound.dependency_validity,
                recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
                recurrence_audit_required=False,
                created_sequence=2,
            )
            ctl.propose(clone, "w2", timestamp_utc=_TS)
            plan2 = ctl.schedule(timestamp_utc="2026-01-01T00:00:02Z")
            decision = plan2.decision_for("clone")
            assert decision.disposition is ScheduleDisposition.DEFERRED
            assert decision.reason is ScheduleReason.DEDUPE_RECOVER
            assert "reuse attempt" in decision.detail
            assert "do not re-execute" in decision.detail
            reused = [
                env
                for _seq, env in project.event_store.iter_project(project.project_id)
                if env.event_type == EVENT_ACTION_REUSED
            ]
            assert reused
            payload = dict(reused[-1].payload)
            assert payload["obligation_id"] == obligation.obligation_id
            assert payload["artifact_refs"]
            _ = twin, reuse
        finally:
            project.close()


def test_observe_positive_progress_and_no_progress_after_recreation():
    contract = _contract()
    left = _obligation(contract, exact_statement="left")
    right = _obligation(contract, exact_statement="right", version=2)
    from euler.control.obligations import Route

    r1 = Route.build(
        target_obligation_id=left.obligation_id,
        mechanism="to-right",
        required_obligation_ids=(right.obligation_id,),
    )
    r2 = Route.build(
        target_obligation_id=right.obligation_id,
        mechanism="to-left",
        required_obligation_ids=(left.obligation_id,),
    )
    view = ResearchView()
    def _event(etype, payload, key):
        return EventEnvelope.build(
            project_id="proj",
            actor="tester",
            event_type=etype,
            idempotency_key=key,
            payload=payload,
            timestamp_utc=_TS,
        )

    view.apply(1, _event("problem.contract.created", {"contract": contract.to_dict()}, "c"))
    view.apply(2, _event("obligation.created", {"obligation": left.to_dict()}, "o1"))
    view.apply(3, _event("obligation.created", {"obligation": right.to_dict()}, "o2"))
    view.apply(4, _event("route.created", {"route": r1.to_dict()}, "r1"))
    view.apply(5, _event("route.created", {"route": r2.to_dict()}, "r2"))
    history = RecurrenceHistory(threshold=3)
    proof = RecurrenceObservation(
        observation_id=_hex("proof-1"),
        source="progress",
        action_id=_hex("act-1"),
        attempt_id=_hex("att-1"),
        route_id=r1.route_id,
        progress_kind=ProgressKind.REDUCTION_VERIFIED,
    )
    observe_recurrence(history, view, proof)
    assert r1.route_id not in history.analyze(view).exhausted_route_ids
    stuck = RecurrenceObservation(
        observation_id=_hex("nop-1"),
        source="progress",
        action_id=_hex("act-2"),
        attempt_id=_hex("att-2"),
        route_id=r1.route_id,
        progress_kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
        no_verifiable_progress_route_id=r1.route_id,
    )
    observe_recurrence(history, view, stuck)
    state = history.snapshot_state()
    restored = RecurrenceHistory(threshold=3)
    restored.restore_state(state)
    again = restored.analyze(view)
    assert again.strike_count == history.analyze(view).strike_count
    poll = restored.analyze(view)
    assert poll.strike_count == again.strike_count


def test_revoked_required_evidence_rejects_dispatch():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            from euler.control.contracts import CheckerResult, EvidenceArtifact

            evidence = EvidenceArtifact.build(
                content_hash=_hex("ev-bytes"),
                producer="worker-1",
                contract_id=contract.contract_id,
                evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                exact_claim=contract.exact_statement,
                scope=contract.scope,
                environment_hash=_hex("env"),
                created_at=_TS,
                checker_id="checker",
                checker_version="1",
                checker_result=CheckerResult.PASS,
            )
            project.event_store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor="tester",
                    event_type="evidence.registered",
                    idempotency_key="ev",
                    payload={"evidence": evidence.to_dict()},
                    timestamp_utc=_TS,
                )
            )
            cand = _candidate(
                contract,
                obligation,
                "needs-ev",
                required_evidence_ids=(evidence.artifact_id,),
            )
            ctl = EulerController(project, actor="tester")
            ctl.propose(cand, "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            project.event_store.append(
                EventEnvelope.build(
                    project_id=project.project_id,
                    actor="tester",
                    event_type="evidence.invalidated",
                    idempotency_key="inv",
                    payload={"artifact_id": evidence.artifact_id},
                    timestamp_utc=_TS,
                )
            )
            try:
                ctl.dispatch("needs-ev", timestamp_utc=_TS)
                raise AssertionError("revoked evidence must reject dispatch")
            except EulerControllerError as exc:
                assert "evidence" in str(exc).lower() or "defer" in str(exc).lower()
        finally:
            project.close()


def test_managed_kickoff_uses_compiled_packet_not_digest_dump():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            ctl = EulerController(project, actor="tester")
            cand = _candidate(contract, obligation, "alpha")
            ctl.propose(cand, "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            ctl.dispatch("alpha", timestamp_utc=_TS)
            worker = project.project_dir / "workers" / "w1"
            task = (worker / "TASK.md").read_text(encoding="utf-8")
            assert contract.exact_statement in task
            assert "===== FACTS =====" in task
            wl = L.WorkerLayout(worker)
            receipts = worker / "receipts"
            receipts.mkdir(exist_ok=True)
            start = prepare_round_start(wl, 1)
            prompt = kickoff(
                "demo",
                "w1",
                receipt_path=str(start.path),
                digest_hash=start.digest_hash,
                receipt_hash=start.receipt_hash,
                digest_text="ORCHESTRATOR MEMORY DIGEST\nsecret episode text",
                managed_context=True,
            )
            assert "compiled ContextPacket in TASK.md" in prompt
            assert "secret episode text" not in prompt
            assert "ORCHESTRATOR MEMORY DIGEST" not in prompt
            assert start.digest_hash in prompt
            assert str(start.path) in prompt
            unmanaged = kickoff(
                "demo",
                "w1",
                receipt_path=str(start.path),
                digest_hash=start.digest_hash,
                receipt_hash=start.receipt_hash,
                digest_text="ORCHESTRATOR MEMORY DIGEST\nsecret episode text",
                managed_context=False,
            )
            assert "secret episode text" in unmanaged
            packet = ContextCompiler().compile(
                cand.context_request,
                project.research_view(),
                snapshot_project(project.project_dir),
                project.artifacts,
            )
            assert packet.manifest.measured_bytes == len(packet.rendered_utf8())
            assert packet.rendered_text.strip() in task
        finally:
            project.close()


def test_one_lease_with_progress_is_one_observation():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1",))
        try:
            _install_cycle(project, contract, obligation)
            ctl = EulerController(project, actor="tester")
            cand = _candidate(
                contract, obligation, "once", tool_environment_hash=_hex("tool")
            )
            ctl.propose(cand, "w1", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            binding = ctl.dispatch("once", timestamp_utc=_TS)
            _complete_with_progress(
                project,
                binding,
                contract,
                kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                tool_hash=_hex("tool"),
            )
            runtime_types = [
                env.event_type
                for _seq, env in project.event_store.iter_project(project.project_id)
            ]
            assert runtime_types.count("runtime.attempt_finished") == 1
            assert runtime_types.count("runtime.lease_completed") == 1
            assert runtime_types.count("runtime.progress_recorded") == 1
            history = synchronize_project_recurrence(project, actor="tester")
            observed = _observation_events(project)
            assert len(observed) == 1
            payload = dict(observed[0].payload)["observation"]
            assert payload["attempt_id"] == binding.attempt_id
            assert payload["progress_kind"] == ProgressKind.NO_VERIFIABLE_PROGRESS.value
            assert len(history._observed) == 1
            first = analyze_project_recurrence(project)
            ctl.schedule(timestamp_utc="2026-01-01T00:00:05Z")
            poll = analyze_project_recurrence(project)
            assert len(_observation_events(project)) == 1
            assert poll["observation_ids"] == first["observation_ids"]
            assert poll["strike_count"] == first["strike_count"]
            restarted = analyze_project_recurrence(project)
            assert restarted["observation_ids"] == first["observation_ids"]
        finally:
            project.close()


def test_eager_and_delayed_sync_match_across_evidence_change():
    def _run(*, delayed: bool) -> dict:
        with tempfile.TemporaryDirectory() as raw:
            workers = ("w1", "w2", "w3") if delayed else ("w1",)
            project, contract, obligation = _managed(Path(raw), workers=workers)
            try:
                _install_cycle(project, contract, obligation)
                ctl = EulerController(project, actor="tester")
                tags = ("a1", "a2", "a3")
                bindings = []
                if delayed:
                    for tag, worker in zip(tags, workers):
                        ctl.propose(
                            _candidate(
                                contract,
                                obligation,
                                tag,
                                tool_environment_hash=_hex("tool"),
                            ),
                            worker,
                            timestamp_utc=_TS,
                        )
                    ctl.schedule(timestamp_utc=_TS)
                    for tag in tags:
                        bindings.append(ctl.dispatch(tag, timestamp_utc=_TS))
                    _complete_with_progress(
                        project,
                        bindings[0],
                        contract,
                        kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                        tool_hash=_hex("tool"),
                        ts="2026-01-01T00:00:01Z",
                    )
                    _complete_with_progress(
                        project,
                        bindings[1],
                        contract,
                        kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                        tool_hash=_hex("tool"),
                        ts="2026-01-01T00:00:02Z",
                    )
                    evidence = EvidenceArtifact.build(
                        content_hash=_hex("ev-relevant"),
                        producer="worker-1",
                        contract_id=contract.contract_id,
                        evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                        exact_claim=obligation.exact_statement,
                        scope=contract.scope,
                        environment_hash=_hex("env"),
                        created_at="2026-01-01T00:00:03Z",
                        checker_id="checker",
                        checker_version="1",
                        checker_result=CheckerResult.PASS,
                    )
                    _event(
                        project,
                        "evidence.registered",
                        {"evidence": evidence.to_dict()},
                        "ev-rel",
                        ts="2026-01-01T00:00:03Z",
                    )
                    _event(
                        project,
                        "obligation.status_changed",
                        {
                            "obligation_id": obligation.obligation_id,
                            "status": "IN_PROGRESS",
                            "evidence_refs": [evidence.artifact_id],
                        },
                        "ob-rel",
                        ts="2026-01-01T00:00:03Z",
                    )
                    _complete_with_progress(
                        project,
                        bindings[2],
                        contract,
                        kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                        tool_hash=_hex("tool"),
                        ts="2026-01-01T00:00:04Z",
                    )
                    synchronize_project_recurrence(
                        project, actor="tester", timestamp_utc="2026-01-01T00:00:05Z"
                    )
                else:
                    for index, tag in enumerate(tags):
                        ctl.propose(
                            _candidate(
                                contract,
                                obligation,
                                tag,
                                tool_environment_hash=_hex("tool"),
                            ),
                            "w1",
                            timestamp_utc=_TS,
                        )
                        ctl.schedule(timestamp_utc=_TS)
                        binding = ctl.dispatch(tag, timestamp_utc=_TS)
                        if index == 2:
                            evidence = EvidenceArtifact.build(
                                content_hash=_hex("ev-relevant"),
                                producer="worker-1",
                                contract_id=contract.contract_id,
                                evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                                exact_claim=obligation.exact_statement,
                                scope=contract.scope,
                                environment_hash=_hex("env"),
                                created_at="2026-01-01T00:00:03Z",
                                checker_id="checker",
                                checker_version="1",
                                checker_result=CheckerResult.PASS,
                            )
                            _event(
                                project,
                                "evidence.registered",
                                {"evidence": evidence.to_dict()},
                                "ev-rel",
                                ts="2026-01-01T00:00:03Z",
                            )
                            _event(
                                project,
                                "obligation.status_changed",
                                {
                                    "obligation_id": obligation.obligation_id,
                                    "status": "IN_PROGRESS",
                                    "evidence_refs": [evidence.artifact_id],
                                },
                                "ob-rel",
                                ts="2026-01-01T00:00:03Z",
                            )
                        _complete_with_progress(
                            project,
                            binding,
                            contract,
                            kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                            tool_hash=_hex("tool"),
                            ts=f"2026-01-01T00:00:0{index + 1}Z",
                        )
                        synchronize_project_recurrence(project, actor="tester")
                outsider = _obligation(
                    contract, exact_statement="unrelated later", version=9
                )
                _event(
                    project,
                    "obligation.created",
                    {"obligation": outsider.to_dict()},
                    "outsider",
                )
                unrelated = EvidenceArtifact.build(
                    content_hash=_hex("ev-unrelated"),
                    producer="worker-1",
                    contract_id=contract.contract_id,
                    evidence_kind=EvidenceKind.LLM_CHECKED_PROOF,
                    exact_claim=outsider.exact_statement,
                    scope=contract.scope,
                    environment_hash=_hex("env"),
                    created_at="2026-01-01T00:00:09Z",
                    checker_id="checker",
                    checker_version="1",
                    checker_result=CheckerResult.PASS,
                )
                _event(
                    project,
                    "evidence.registered",
                    {"evidence": unrelated.to_dict()},
                    "ev-unrel",
                )
                result = analyze_project_recurrence(project)
                result["n_obs"] = len(_observation_events(project))
                return result
            finally:
                project.close()

    delayed = _run(delayed=True)
    eager = _run(delayed=False)
    assert delayed["n_obs"] == 3
    assert eager["n_obs"] == 3
    assert delayed["strike_count"] == eager["strike_count"]
    assert delayed["recommendation"] == eager["recommendation"]
    assert delayed["classification"] == eager["classification"]


def test_context_rewording_does_not_reset_tool_change_does():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(Path(raw), workers=("w1", "w2", "w3"))
        try:
            _install_cycle(project, contract, obligation)
            ctl = EulerController(project, actor="tester")
            same_tool = _hex("tool-v1")
            first = _candidate(
                contract,
                obligation,
                "gap-a",
                tool_environment_hash=same_tool,
                concrete_gap="close the even-integer case",
            )
            second = _candidate(
                contract,
                obligation,
                "gap-b",
                tool_environment_hash=same_tool,
                concrete_gap="a reworded description of the same remaining case",
            )
            ctl.propose(first, "w1", timestamp_utc=_TS)
            ctl.propose(second, "w2", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            b1 = ctl.dispatch("gap-a", timestamp_utc=_TS)
            b2 = ctl.dispatch("gap-b", timestamp_utc=_TS)
            _complete_with_progress(
                project,
                b1,
                contract,
                kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                tool_hash=same_tool,
                ts="2026-01-01T00:00:01Z",
            )
            _complete_with_progress(
                project,
                b2,
                contract,
                kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                tool_hash=same_tool,
                ts="2026-01-01T00:00:02Z",
            )
            synchronize_project_recurrence(project, actor="tester")
            mid = analyze_project_recurrence(project)
            assert mid["strike_count"] == 2
            assert mid["classification"] != "CHANGED_FRONTIER"
            third = _candidate(
                contract,
                obligation,
                "gap-c",
                tool_environment_hash=_hex("tool-v2"),
                concrete_gap="close the even-integer case",
            )
            ctl.propose(third, "w3", timestamp_utc=_TS)
            ctl.schedule(timestamp_utc=_TS)
            b3 = ctl.dispatch("gap-c", timestamp_utc=_TS)
            _complete_with_progress(
                project,
                b3,
                contract,
                kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                tool_hash=_hex("tool-v2"),
                ts="2026-01-01T00:00:03Z",
            )
            synchronize_project_recurrence(project, actor="tester")
            after = analyze_project_recurrence(project)
            assert after["strike_count"] == 0
            assert after["classification"] in {
                "CHANGED_FRONTIER",
                "SUSPECTED_RECURRENCE",
                "ACYCLIC",
            }
        finally:
            project.close()


def test_overlay_spares_independent_or_route():
    with tempfile.TemporaryDirectory() as raw:
        project, contract, obligation = _managed(
            Path(raw), workers=("w1", "w2", "w3", "w4")
        )
        try:
            child_a = _obligation(contract, exact_statement="route a child", version=2)
            child_b = _obligation(contract, exact_statement="route b child", version=3)
            route_a = Route.build(
                target_obligation_id=obligation.obligation_id,
                mechanism="direct-search",
                required_obligation_ids=(child_a.obligation_id,),
                alternative_group="or-1",
            )
            route_b = Route.build(
                target_obligation_id=obligation.obligation_id,
                mechanism="other-mechanism",
                required_obligation_ids=(child_b.obligation_id,),
                alternative_group="or-1",
            )
            _event(project, "obligation.created", {"obligation": child_a.to_dict()}, "or-a-child")
            _event(project, "obligation.created", {"obligation": child_b.to_dict()}, "or-b-child")
            _event(project, "route.created", {"route": route_a.to_dict()}, "or-a")
            _event(project, "route.created", {"route": route_b.to_dict()}, "or-b")
            _event(
                project,
                "route.status_changed",
                {"route_id": route_a.route_id, "status": "BLOCKED"},
                "block-a",
            )
            ctl = EulerController(project, actor="tester")
            for index, tag in enumerate(("r1", "r2", "r3")):
                ctl.propose(
                    _candidate(
                        contract,
                        obligation,
                        tag,
                        route_id=route_a.route_id,
                        tool_environment_hash=_hex("tool"),
                    ),
                    ("w1", "w2", "w3")[index],
                    timestamp_utc=_TS,
                )
            ctl.schedule(timestamp_utc=_TS)
            for tag in ("r1", "r2", "r3"):
                binding = ctl.dispatch(tag, timestamp_utc=_TS)
                _complete_with_progress(
                    project,
                    binding,
                    contract,
                    kind=ProgressKind.NO_VERIFIABLE_PROGRESS,
                    tool_hash=_hex("tool"),
                    route_id=route_a.route_id,
                )
            synchronize_project_recurrence(project, actor="tester")
            rec = analyze_project_recurrence(project)
            assert rec["audit_required"] is True
            assert rec["blocked_mechanism"] is True
            healthy = _candidate(
                contract,
                obligation,
                "or-healthy",
                route_id=route_b.route_id,
                tool_environment_hash=_hex("tool"),
            )
            ctl.propose(healthy, "w4", timestamp_utc=_TS)
            plan = ctl.schedule(timestamp_utc="2026-01-01T00:01:00Z")
            decision = plan.decision_for("or-healthy")
            assert decision.disposition is ScheduleDisposition.SELECTED
            assert decision.reason is ScheduleReason.SELECTED
            audited = [
                item
                for item in plan.decisions
                if item.reason is ScheduleReason.RECURRENCE_AUDIT
            ]
            assert audited == []
        finally:
            project.close()
