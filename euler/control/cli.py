"""Public Euler control verbs shared by the CLI and euler-control MCP.

Stdlib plus existing control APIs. No provider launch except ``schedule(..., launch=True)``.
JSON payloads are deterministic (sorted keys). Fail closed on malformed input.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from euler.control.controller import EulerController, EulerControllerError
from euler.control.contracts import ProblemContract, TargetKind
from euler.control.failures import FailureRecord
from euler.control.final_audit import FinalAuditError, run_final_audit
from euler.control.refutation_audit import RefutationAuditError, run_refutation_audit
from euler.control.hooks import after_managed_round
from euler.control.live_records import ControlStatus
from euler.control.obligations import Obligation, ObligationStatus
from euler.control.policy import BudgetPolicy
from euler.control.project import (
    EulerProjectError,
    initialize_project,
    is_euler_project,
    open_project,
)
from euler.control.persistent_recurrence import analyze_project_recurrence
from euler.control.research_service import ResearchService, ResearchServiceError, frontier_payload
from euler.control.scheduler import CandidateAction
from euler.control.schema import EventEnvelope
from euler.execution import layout as L
from euler.execution.launch_receipt import read_and_verify_launch, read_and_verify_result
from euler.execution.memory_digest import read_and_verify_end, read_and_verify_start

_ACTOR_CLI = "euler-cli"
_ACTOR_MCP = "euler-control"
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ROUND_RE = re.compile(r"^round_(\d+)\.(start|launch|result|end)\.json$")

__all__ = [
    "ControlSurfaceError",
    "dumps",
    "do_init",
    "do_propose",
    "do_schedule",
    "do_record_failure",
    "do_analyze_recurrence",
    "do_reconcile",
    "do_checkpoint",
    "control_status_payload",
    "target_obligation_from_contract",
]


class ControlSurfaceError(ValueError):
    """Malformed control input or uninitialized project."""


def do_frontier(project: str) -> dict[str, Any]:
    with open_project(_require_euler_project(project)) as opened:
        return frontier_payload(opened)


def do_research_update(project: str, operations: Any, *, actor: str = _ACTOR_CLI) -> dict[str, Any]:
    if isinstance(operations, (str, Path)):
        operations = json.loads(Path(operations).read_text(encoding="utf-8"))
    if not isinstance(operations, list):
        raise ControlSurfaceError("research operations must be a JSON list")
    try:
        with open_project(_require_euler_project(project)) as opened:
            return ResearchService(opened, actor=actor).apply(operations)
    except (ValueError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc


def do_admit_fact(project: str, fact_id: str, obligation_id: str, *,
                  confirmed_scope_hash: str, alignment_note: str,
                  actor: str = _ACTOR_CLI) -> dict[str, Any]:
    try:
        with open_project(_require_euler_project(project)) as opened:
            return ResearchService(opened, actor=actor).admit_fact(
                fact_id, obligation_id, confirmed_scope_hash=confirmed_scope_hash,
                alignment_note=alignment_note,
            )
    except (ValueError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc


def do_kernel_check(project: str, obligation_id: str, request: Mapping[str, Any] | str | Path,
                    *, request_id: str, actor: str = _ACTOR_CLI) -> dict[str, Any]:
    from .kernel_service import check_kernel
    try:
        with open_project(_require_euler_project(project)) as opened:
            return check_kernel(opened, obligation_id, _as_object("kernel request", request),
                                request_id=request_id, actor=actor)
    except (ValueError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc


def do_kernel_align(project: str, obligation_id: str, kernel_artifact_hash: str,
                    review: Mapping[str, Any] | str | Path, *, actor: str = _ACTOR_CLI) -> dict[str, Any]:
    from .kernel_service import align_kernel
    data = _as_object("alignment review", review)
    allowed = {"formal_statement", "confirmed_scope_hash", "notes", "auditor_version", "version"}
    if set(data) - allowed:
        raise ControlSurfaceError("unknown alignment review fields")
    try:
        with open_project(_require_euler_project(project)) as opened:
            return align_kernel(opened, obligation_id, kernel_artifact_hash, actor=actor, **dict(data))
    except (ValueError, TypeError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc


def do_compute(project: str, request: Mapping[str, Any] | str | Path,
               *, actor: str = _ACTOR_CLI) -> dict[str, Any]:
    from .compute_service import run_polynomial_compute
    data = _as_object("compute request", request)
    allowed = {"obligation_id", "request_id", "predicate", "max_points", "max_wall_ms", "plugin_id", "consequences"}
    if set(data) - allowed:
        raise ControlSurfaceError("unknown compute request fields")
    try:
        with open_project(_require_euler_project(project)) as opened:
            return run_polynomial_compute(opened, actor=actor, **dict(data))
    except (ValueError, TypeError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc


def do_compute_review(project: str, review: Mapping[str, Any] | str | Path,
                      *, actor: str = _ACTOR_CLI) -> dict[str, Any]:
    from .compute_service import review_compute_realization
    data = _as_object("compute realization review", review)
    allowed = {"obligation_id", "request_id", "confirmed_scope_hash", "notes", "translation_confirmed",
               "original_object_realized", "route_id", "auditor_version"}
    if set(data) - allowed:
        raise ControlSurfaceError("unknown compute review fields")
    try:
        with open_project(_require_euler_project(project)) as opened:
            return review_compute_realization(opened, actor=actor, **dict(data))
    except (ValueError, TypeError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc


def dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def do_init(
    project: str,
    *,
    contract: Mapping[str, Any] | str | Path,
    policy: Mapping[str, Any] | str | Path,
    obligation: Mapping[str, Any] | str | Path | None = None,
    actor: str = _ACTOR_CLI,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    root = _require_project_dir(project)
    contract_obj = ProblemContract.from_dict(_as_object("contract", contract))
    policy_obj = BudgetPolicy.from_dict(_as_object("policy", policy))
    if obligation is None:
        obligation_obj = target_obligation_from_contract(contract_obj, created_by=actor)
    else:
        obligation_obj = Obligation.from_dict(_as_object("obligation", obligation))
    ts = timestamp_utc or utc_now()
    try:
        opened = initialize_project(
            root, contract_obj, obligation_obj, policy_obj, actor, ts
        )
    except EulerProjectError as exc:
        raise ControlSurfaceError(str(exc)) from exc
    try:
        status = opened.status()
        return _init_payload(project, status)
    finally:
        opened.close()


def do_propose(
    target: str,
    candidate: Mapping[str, Any] | str | Path,
    *,
    actor: str = _ACTOR_CLI,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    project, worker = _split_target(target)
    root = _require_euler_project(project)
    cand = CandidateAction.from_dict(_as_object("candidate", candidate))
    opened = open_project(root)
    try:
        ctl = EulerController(opened, actor=actor)
        record = ctl.propose(cand, worker, timestamp_utc=timestamp_utc or utc_now())
        return {
            "candidate_id": record.candidate.candidate_id,
            "project": project,
            "proposal_id": record.proposal_id,
            "worker": record.worker,
        }
    except (EulerControllerError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc
    finally:
        opened.close()


def do_schedule(
    project: str,
    *,
    dispatch: bool = False,
    launch: bool = False,
    actor: str = _ACTOR_CLI,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    root = _require_euler_project(project)
    if launch:
        dispatch = True
    ts = timestamp_utc or utc_now()
    opened = open_project(root)
    try:
        ctl = EulerController(opened, actor=actor)
        plan = ctl.schedule(timestamp_utc=ts)
        dispatched: list[dict[str, Any]] = []
        if dispatch:
            for candidate_id in plan.selected_candidate_ids:
                binding = ctl.dispatch(candidate_id, launch=launch, timestamp_utc=ts)
                dispatched.append(binding.to_dict())
        return {
            "dispatched": dispatched,
            "idle": plan.idle,
            "launch": launch,
            "plan": plan.to_dict(),
            "project": project,
            "selected_candidate_ids": list(plan.selected_candidate_ids),
        }
    except (EulerControllerError, EulerProjectError) as exc:
        raise ControlSurfaceError(str(exc)) from exc
    finally:
        opened.close()


def do_record_failure(
    project: str,
    failure: Mapping[str, Any] | str | Path,
    *,
    actor: str = _ACTOR_CLI,
    timestamp_utc: str | None = None,
) -> dict[str, Any]:
    root = _require_euler_project(project)
    record = FailureRecord.from_dict(_as_object("failure", failure))
    ts = timestamp_utc or utc_now()
    opened = open_project(root)
    try:
        envelope = EventEnvelope.build(
            project_id=opened.project_id,
            actor=actor,
            event_type="failure.recorded",
            idempotency_key=record.failure_id,
            payload={"failure": record.to_dict()},
            timestamp_utc=ts,
            correlation_id=opened.project_id,
        )
        stored = opened.event_store.append(envelope)
        return {
            "created": stored.created,
            "failure_id": record.failure_id,
            "kind": record.kind.value,
            "project": project,
        }
    except Exception as exc:
        raise ControlSurfaceError(str(exc)) from exc
    finally:
        opened.close()


def do_analyze_recurrence(
    project: str,
    *,
    tool_hash: str = "",
) -> dict[str, Any]:
    root = _require_euler_project(project)
    opened = open_project(root)
    try:
        return analyze_project_recurrence(opened, tool_hash=tool_hash)
    except Exception as exc:
        raise ControlSurfaceError(str(exc)) from exc
    finally:
        opened.close()


def do_reconcile(target: str, *, actor: str = _ACTOR_CLI) -> dict[str, Any]:
    project, worker = _split_target(target)
    root = _require_euler_project(project)
    wl = L.WorkerLayout(L.worker_dir(project, worker))
    start, launch, result, end_path = _latest_receipt_chain(wl)
    try:
        record = after_managed_round(wl, start, launch, result, end_path)
    except Exception as exc:
        raise ControlSurfaceError(str(exc)) from exc
    if record is None:
        raise ControlSurfaceError(f"project {project!r} is not Euler-managed")
    return {
        "project": project,
        "reconciliation": record.to_dict(),
        "worker": worker,
    }


def do_checkpoint(
    project: str,
    *,
    transport: Any = None,
    timestamp_utc: str | None = None,
    retry_infra: bool = False,
    retry_reason: str = "",
) -> dict[str, Any]:
    root = _require_euler_project(project)
    opened = open_project(root)
    try:
        kwargs: dict[str, Any] = {}
        if transport is not None:
            kwargs["transport"] = transport
        if timestamp_utc is not None:
            kwargs["timestamp_utc"] = timestamp_utc
        if retry_infra:
            kwargs.update(retry_infra=True, retry_reason=retry_reason)
        obligation = opened.research_view().get_obligation(opened.manifest.target_obligation_id)
        if obligation is not None and obligation.status is ObligationStatus.REFUTED:
            run = run_refutation_audit(opened, **kwargs)
        else:
            from .kernel_service import latest_kernel_alignment
            alignment = latest_kernel_alignment(opened, opened.manifest.target_obligation_id)
            if alignment is not None:
                kwargs["alignment"] = alignment
            run = run_final_audit(opened, **kwargs)
        return {"project": project, "run": run.to_dict()}
    except (FinalAuditError, RefutationAuditError) as exc:
        raise ControlSurfaceError(str(exc)) from exc
    finally:
        opened.close()


def control_status_payload(project: str) -> Optional[dict[str, Any]]:
    root = L.project_dir(project)
    if not root.is_dir() or not is_euler_project(root):
        return None
    opened = open_project(root)
    try:
        status = opened.status()
        from .campaign import campaign_snapshot
        human = campaign_snapshot(opened)
        payload = status.to_dict()
        if human['revision'] != 'initial':
            payload['human_control'] = human
        return payload
    finally:
        opened.close()


def target_obligation_from_contract(contract: ProblemContract, *, created_by: str) -> Obligation:
    desired = {
        TargetKind.PROOF: "proof",
        TargetKind.COUNTEREXAMPLE: "counterexample",
        TargetKind.CLASSIFICATION: "classification",
        TargetKind.FORMALIZATION: "formalization",
    }.get(contract.target_kind, "proof")
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output=desired,
        created_by=created_by,
        version=1,
    )


def _init_payload(project: str, status: ControlStatus) -> dict[str, Any]:
    return {
        "contract_id": status.contract_id,
        "control_plane": status.control_plane,
        "manifest_hash": status.manifest_hash,
        "project": project,
        "project_id": status.project_id,
        "target_obligation_id": status.target_obligation_id,
    }


def _require_project_dir(project: str) -> Path:
    if not _PROJECT_NAME_RE.match(project):
        raise ControlSurfaceError(f"invalid project name: {project!r}")
    root = L.project_dir(project)
    if not root.is_dir() or not (root / "project.json").is_file():
        raise ControlSurfaceError(
            f"project {project!r} does not exist; run `euler new {project}` first"
        )
    return root


def _require_euler_project(project: str) -> Path:
    root = _require_project_dir(project)
    if not is_euler_project(root):
        raise ControlSurfaceError(f"project {project!r} is not Euler-initialized")
    return root


def _split_target(target: str) -> tuple[str, str]:
    project, worker = L.resolve_target(target)
    if not worker:
        raise ControlSurfaceError(f"expected <project>/<worker>, got {target!r}")
    return project, worker


def _as_object(label: str, value: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        return value
    path = Path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlSurfaceError(f"cannot read {label} file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlSurfaceError(f"{label} must be a JSON object")
    return payload


def _latest_receipt_chain(wl: L.WorkerLayout) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    receipts = wl.receipts
    if not receipts.is_dir():
        raise ControlSurfaceError(f"no receipts for worker {wl.name!r}")
    rounds: dict[int, set[str]] = {}
    for path in receipts.iterdir():
        match = _ROUND_RE.match(path.name)
        if match is None:
            continue
        rounds.setdefault(int(match.group(1)), set()).add(match.group(2))
    complete = [
        rnd for rnd, kinds in rounds.items() if kinds >= {"start", "launch", "result", "end"}
    ]
    if not complete:
        raise ControlSurfaceError(f"no complete verified receipt chain for {wl.name!r}")
    rnd = max(complete)
    start_path = receipts / f"round_{rnd:04d}.start.json"
    launch_path = receipts / f"round_{rnd:04d}.launch.json"
    result_path = receipts / f"round_{rnd:04d}.result.json"
    end_path = receipts / f"round_{rnd:04d}.end.json"
    try:
        start = read_and_verify_start(start_path)
        launch = read_and_verify_launch(launch_path)
        result = read_and_verify_result(result_path)
        read_and_verify_end(end_path)
    except Exception as exc:
        raise ControlSurfaceError(f"receipt chain failed verification: {exc}") from exc
    return start, launch, result, end_path
