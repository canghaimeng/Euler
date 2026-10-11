"""Reserved, idempotent local checker actions using the existing Euler runtime."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import time
from typing import Any, Callable, Mapping, Optional

from .context import ContextCompiler, ContextLimits
from .ids import canonical_json, sha256_hex
from .policy import BudgetClass, ResourceVector
from .project import EulerProject
from .recurrence import RecurrenceRecommendation
from .runtime import Lease, LeaseStatus
from .scheduler import (ActionKind, ActiveWork, CandidateAction, DependencyValidity,
                        PriorityClass, ScheduleDisposition, TransparentScheduler,
                        build_action_context_pair, slot_kind_for_action)
from .schema import EventEnvelope, MathematicalStatus, ObservationStatus, ObservedQuantity, UsageRecord


class LocalActionError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(value):
    return sha256_hex(canonical_json(value))


def _usage(elapsed):
    def unknown(unit):
        return ObservedQuantity(unit=unit, status=ObservationStatus.UNKNOWN, value=None)
    return UsageRecord(input_tokens=unknown("tokens"), output_tokens=unknown("tokens"),
        reasoning_tokens=unknown("tokens"), cached_tokens=unknown("tokens"),
        cost_microusd=unknown("microusd"),
        wall_ms=ObservedQuantity(unit="ms", status=ObservationStatus.OBSERVED, value=elapsed),
        tool_cpu_ms=unknown("ms"), tool_gpu_ms=unknown("ms"))


def _read_proc(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _proc_starttime(pid: int) -> str:
    text = _read_proc(f"/proc/{pid}/stat")
    if not text:
        return ""
    close = text.rfind(")")
    if close < 0:
        return ""
    rest = text[close + 2 :].split()
    if len(rest) < 20:
        return ""
    return rest[19]


def owner_process_identity() -> dict[str, str]:
    """Durable owner token: PID plus boot/start identity to survive PID reuse."""
    pid = os.getpid()
    return {
        "pid": str(pid),
        "boot_id": _read_proc("/proc/sys/kernel/random/boot_id"),
        "starttime": _proc_starttime(pid),
    }


def owner_is_alive(owner: Mapping[str, Any] | None) -> bool:
    if not isinstance(owner, Mapping):
        return False
    try:
        pid = int(str(owner.get("pid") or ""))
    except ValueError:
        return False
    if not Path(f"/proc/{pid}").exists():
        return False
    recorded_start = str(owner.get("starttime") or "")
    if not recorded_start:
        return False
    if _proc_starttime(pid) != recorded_start:
        return False
    recorded_boot = str(owner.get("boot_id") or "")
    current_boot = _read_proc("/proc/sys/kernel/random/boot_id")
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return False
    return True


def _owner_for_request(project: EulerProject, request_id: str) -> Optional[dict[str, Any]]:
    owner = None
    for _seq, event in project.event_store.iter_project(project.project_id):
        if event.event_type != "euler.local_action_selected":
            continue
        payload = dict(event.payload)
        if payload.get("request_id") == request_id and isinstance(payload.get("owner"), Mapping):
            owner = dict(payload["owner"])
    return owner


def run_local_action(project: EulerProject, *, obligation_id: str, request_id: str,
                     mechanism: str, parameters: Mapping[str, Any], timeout_ms: int,
                     actor: str, execute: Callable[[], tuple[dict[str, Any], tuple[str, ...]]],
                     kind: ActionKind = ActionKind.VERIFY,
                     budget_class: BudgetClass = BudgetClass.NORMAL,
                     requested: ResourceVector | None = None,
                     priority_class: PriorityClass | None = None) -> dict[str, Any]:
    """Reserve before executing a built-in checker; never accept a model callback.

    `execute` is an internal adapter, never exposed by CLI/MCP. An interrupted
    running action is reported for recovery rather than blindly duplicated.
    Optional ``budget_class`` / ``requested`` select the FINAL_AUDIT pool and a
    concrete allowance; the scheduler still admits the action under policy.
    """
    if not isinstance(request_id, str) or not request_id.strip():
        raise LocalActionError("a nonempty idempotency request_id is required")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        raise LocalActionError("positive timeout_ms required")
    if not isinstance(kind, ActionKind):
        raise LocalActionError("kind must be an ActionKind")
    if not isinstance(budget_class, BudgetClass):
        raise LocalActionError("budget_class must be a BudgetClass")
    if requested is None:
        requested = ResourceVector(
            cost_microusd=1 if budget_class is BudgetClass.FINAL_AUDIT else 0,
            input_tokens=0, output_tokens=0, reasoning_tokens=0, cached_tokens=0,
            wall_ms=timeout_ms, tool_cpu_ms=timeout_ms, tool_gpu_ms=0,
        )
    elif not isinstance(requested, ResourceVector):
        raise LocalActionError("requested must be a ResourceVector")
    if priority_class is None:
        if budget_class is BudgetClass.FINAL_AUDIT and kind is ActionKind.VERIFY:
            priority_class = PriorityClass.FINAL_ACCEPTANCE
        else:
            priority_class = PriorityClass.DISCRIMINATING_CHEAP
    elif not isinstance(priority_class, PriorityClass):
        raise LocalActionError("priority_class must be a PriorityClass")
    request_hash = _hash({"obligation": obligation_id, "mechanism": mechanism,
                          "parameters": dict(parameters), "timeout_ms": timeout_ms,
                          "kind": kind.value, "budget_class": budget_class.value,
                          "requested": requested.to_dict(),
                          "priority_class": priority_class.value})
    key = "local-action:" + request_id
    pending_recover = None
    with project.event_store.transaction(immediate=True):
        for _, event in project.event_store.iter_project(project.project_id):
            if event.event_type != "euler.local_action_finished":
                continue
            payload = dict(event.payload)
            if payload.get("request_id") == request_id:
                if payload.get("request_hash") != request_hash:
                    raise LocalActionError("request_id was already used with different inputs")
                for ref in payload["artifact_refs"]:
                    project.artifacts.verify(ref)
                return {**payload["result"], "replayed": True, "lease_id": payload["lease_id"]}
        view = project.research_view()
        obligation = view.get_obligation(obligation_id)
        if obligation is None:
            raise LocalActionError("unknown obligation")
        contract = view.get_contract(obligation.contract_id)
        runtime = project.runtime_snapshot()
        previous = [lease for lease in runtime.leases if lease.idempotency_key == key]
        if previous:
            pending_recover = previous[-1]
        else:
            spec, context, seed = build_action_context_pair(
                contract_id=contract.contract_id, obligation_id=obligation_id,
                concrete_gap=obligation.exact_statement,
                output_contract="A checked artifact with precise scope; not automatic Fact admission",
                tool_environment_hash=_hash({"checker": mechanism, "parameters": parameters}),
                acceptance_policy_hash=_hash("local-checker-v1"),
                reference_versions_hash=obligation.scope.definitions_hash,
                limits=ContextLimits.unlimited(), action_type=kind.value.lower(), mechanism=mechanism,
                model_id="local-checker", effort="deterministic", task_mode="checker",
                parameters_json=canonical_json(dict(parameters)), dependency_hash=request_hash,
                definitions_hash=obligation.scope.definitions_hash,
            )
            candidate = CandidateAction(candidate_id=_hash([request_id, request_hash]),
                action_spec=spec, context_request=context, context_seed=seed,
                obligation_id=obligation_id, mechanism_family=mechanism, action_kind=kind,
                slot_kind=slot_kind_for_action(kind), budget_class=budget_class,
                requested=requested, priority_class=priority_class,
                dependency_validity=DependencyValidity.VALID,
                recurrence_recommendation=RecurrenceRecommendation.CONTINUE,
                recurrence_audit_required=False, created_sequence=1)
            active = tuple(ActiveWork(obligation_id=l.obligation_id, mechanism_family=l.mechanism,
                                     action_id=l.action_id, slot_kind=l.slot_kind)
                           for l in runtime.leases if l.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}
                           and l.obligation_id)
            plan = TransparentScheduler().select((candidate,), policy=project.policy,
                snapshot=runtime.resources, now_utc=_now(), view=view, active=active)
            decision = plan.decision_for(candidate.candidate_id)
            if decision.disposition is not ScheduleDisposition.SELECTED:
                return {"status": "DEFERRED", "reason": decision.reason.value, "replayed": False}
            from .adapters.project import snapshot_project
            packet = ContextCompiler().compile(context, view, snapshot_project(project.project_dir), project.artifacts)
            packet_ref = project.artifacts.put_bytes(packet.rendered_utf8(), media_type="text/plain")
            project.event_store.append(EventEnvelope.build(project_id=project.project_id, actor=actor,
                event_type="euler.local_action_selected", idempotency_key=key,
                payload={"request_id": request_id, "request_hash": request_hash,
                         "plan": plan.to_dict(), "packet_hash": packet_ref.content_hash,
                         "owner": owner_process_identity()},
                timestamp_utc=_now()))
            lease = project.runtime.issue(project.project_id, candidate, idempotency_key=key,
                                          actor=actor, context_cache_key=packet.cache_key.key_id)
            project.runtime.mark_running(project.project_id, lease.lease_id, actor=actor)
    if pending_recover is not None:
        recovered = _recover_previous_lease(
            project, pending_recover, request_id=request_id, request_hash=request_hash,
            actor=actor, key=key,
        )
        if recovered is not None:
            return recovered
        raise LocalActionError(
            "failed local action exists; retry with an explicit new request_id"
        )
    started = time.monotonic()
    try:
        result, refs = execute()
        refs = tuple(refs) + (packet_ref.content_hash,)
        with project.event_store.transaction(immediate=True):
            usage = _usage(max(0, int((time.monotonic() - started) * 1000)))
            current = next(
                (item for item in project.runtime_snapshot().leases if item.lease_id == lease.lease_id),
                lease,
            )
            if current.status not in {LeaseStatus.INFRA_FAILED, LeaseStatus.COMPLETED, LeaseStatus.CANCELLED}:
                if result.get("status") in {"INFRA", "ERROR", "TIMEOUT"}:
                    project.runtime.fail_infrastructure(project.project_id, lease.lease_id,
                        usage=usage, artifact_refs=refs, reason="local checker infrastructure result", actor=actor)
                else:
                    project.runtime.complete(project.project_id, lease.lease_id,
                        usage=usage, artifact_refs=refs, mathematical_status=MathematicalStatus.NO_CLAIM, actor=actor)
            project.event_store.append(EventEnvelope.build(project_id=project.project_id, actor=actor,
                event_type="euler.local_action_finished", idempotency_key=key,
                payload={"request_id": request_id, "request_hash": request_hash,
                         "lease_id": lease.lease_id, "artifact_refs": list(refs), "result": result},
                timestamp_utc=_now()))
        return {**result, "replayed": False, "lease_id": lease.lease_id}
    except Exception as exc:
        current = next(
            (item for item in project.runtime_snapshot().leases if item.lease_id == lease.lease_id),
            lease,
        )
        if current.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
            project.runtime.fail_infrastructure(project.project_id, lease.lease_id,
                reason=f"local checker failed: {type(exc).__name__}", actor=actor)
        raise


def _recover_previous_lease(
    project: EulerProject,
    lease: Lease,
    *,
    request_id: str,
    request_hash: str,
    actor: str,
    key: str,
) -> Optional[dict[str, Any]]:
    if lease.status in {LeaseStatus.ISSUED, LeaseStatus.RUNNING}:
        owner = _owner_for_request(project, request_id)
        if owner_is_alive(owner):
            raise LocalActionError(
                "local action still running in owner process; do not duplicate the checker"
            )
        project.runtime.fail_infrastructure(
            project.project_id,
            lease.lease_id,
            usage=_usage(0),
            reason="local checker owner process is dead; verifier slot released",
            actor=actor,
        )
        result = {
            "status": "INFRA",
            "detail": "recovered interrupted local action; retry with a new request_id",
            "recovered": True,
        }
        project.event_store.append(EventEnvelope.build(
            project_id=project.project_id, actor=actor,
            event_type="euler.local_action_finished", idempotency_key=key + ":recovered",
            payload={"request_id": request_id, "request_hash": request_hash,
                     "lease_id": lease.lease_id, "artifact_refs": [], "result": result},
            timestamp_utc=_now(),
        ))
        return {**result, "replayed": False, "lease_id": lease.lease_id}
    if lease.status is LeaseStatus.INFRA_FAILED:
        return None
    if lease.status is LeaseStatus.COMPLETED:
        return None
    return None
