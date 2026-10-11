"""Stdio MCP ``euler-control`` — main-agent live control tools.

Fail-closed to ``EULER_ROLE=main``. Workers and verifiers see no control tools.
Main still has no ``fact_submit``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

from euler._mcp import FastMCP
from euler.control.cli import (
    ControlSurfaceError,
    control_status_payload,
    do_analyze_recurrence,
    do_checkpoint,
    do_init,
    do_propose,
    do_record_failure,
    do_schedule,
    do_frontier,
    do_research_update,
    do_admit_fact,
    do_kernel_check,
    do_kernel_align,
    do_compute,
    do_compute_review,
    utc_now,
)

_ACTOR = "euler-control"

CONTROL_TOOLS: Tuple[str, ...] = (
    "euler_frontier",
    "euler_update_frontier",
    "euler_admit_fact",
    "euler_kernel_check",
    "euler_kernel_align",
    "euler_compute",
    "euler_compute_review",
    "euler_project_status",
    "euler_register_contract",
    "euler_propose_action",
    "euler_schedule",
    "euler_record_failure",
    "euler_analyze_recurrence",
    "euler_checkpoint",
)

ROLE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "main": CONTROL_TOOLS,
    "all": CONTROL_TOOLS,
    "worker": (),
    "verifier": (),
}


def tools_for(role: str) -> List[str]:
    """Control tools visible to ``role``. Unknown roles get none (fail-closed)."""
    return list(ROLE_TOOLS.get(role, ()))


def _role() -> str:
    return os.environ.get("EULER_ROLE", "")


def _require_main() -> None:
    role = _role()
    if role not in {"main", "all"}:
        raise RuntimeError("euler-control MCP requires EULER_ROLE=main")


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ControlSurfaceError as exc:
        raise RuntimeError(str(exc)) from exc


def euler_project_status(project: str) -> dict:
    """Deterministic control-plane status for an initialized Euler project."""
    _require_main()
    payload = control_status_payload(project)
    if payload is None:
        raise RuntimeError(f"project {project!r} is not Euler-initialized")
    return payload


def euler_frontier(project: str) -> dict:
    """Read the active obligation/route/blocker/evidence view."""
    _require_main()
    return _wrap(do_frontier, project)


def euler_update_frontier(project: str, operations: List[Dict[str, Any]]) -> dict:
    """Apply a validated, atomic proposal/state change set; no arbitrary evidence import."""
    _require_main()
    return _wrap(do_research_update, project, operations, actor=_ACTOR)


def euler_admit_fact(project: str, fact_id: str, obligation_id: str,
                     confirmed_scope_hash: str, alignment_note: str) -> dict:
    """Link an already admitted Fact to a reviewed obligation; never write a Fact."""
    _require_main()
    return _wrap(do_admit_fact, project, fact_id, obligation_id,
                 confirmed_scope_hash=confirmed_scope_hash, alignment_note=alignment_note,
                 actor=_ACTOR)


def euler_kernel_check(project: str, obligation_id: str, request_id: str,
                       request: Dict[str, Any]) -> dict:
    """Execute the configured Lean checker under a reserved local action."""
    _require_main()
    return _wrap(do_kernel_check, project, obligation_id, request, request_id=request_id, actor=_ACTOR)


def euler_kernel_align(project: str, obligation_id: str, kernel_artifact_hash: str,
                       review: Dict[str, Any]) -> dict:
    """Register an explicit auditor statement-alignment review after a real kernel check."""
    _require_main()
    return _wrap(do_kernel_align, project, obligation_id, kernel_artifact_hash, review, actor=_ACTOR)


def euler_compute(project: str, request: Dict[str, Any]) -> dict:
    """Run the supported bounded search and independent checker under a resource lease."""
    _require_main()
    return _wrap(do_compute, project, request, actor=_ACTOR)


def euler_compute_review(project: str, review: Dict[str, Any]) -> dict:
    """Record original-object realization review and promote only the selected scoped witness."""
    _require_main()
    return _wrap(do_compute_review, project, review, actor=_ACTOR)


def euler_register_contract(
    project: str,
    contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    obligation: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Initialize or assert-same the managed control plane on an existing project."""
    _require_main()
    return _wrap(
        do_init,
        project,
        contract=contract,
        policy=policy,
        obligation=obligation,
        actor=_ACTOR,
        timestamp_utc=utc_now(),
    )


def euler_propose_action(project: str, worker: str, candidate: Mapping[str, Any]) -> dict:
    """Persist a CandidateAction for ``worker`` on ``project``."""
    _require_main()
    return _wrap(
        do_propose,
        f"{project}/{worker}",
        candidate,
        actor=_ACTOR,
        timestamp_utc=utc_now(),
    )


def euler_schedule(
    project: str,
    dispatch: bool = False,
    launch: bool = False,
) -> dict:
    """Run the transparent scheduler. Launch is false by default."""
    _require_main()
    return _wrap(
        do_schedule,
        project,
        dispatch=dispatch,
        launch=launch,
        actor=_ACTOR,
        timestamp_utc=utc_now(),
    )


def euler_record_failure(project: str, failure: Mapping[str, Any]) -> dict:
    """Append a typed FailureRecord as a research-graph event."""
    _require_main()
    return _wrap(
        do_record_failure,
        project,
        failure,
        actor=_ACTOR,
        timestamp_utc=utc_now(),
    )


def euler_analyze_recurrence(project: str, tool_hash: str = "") -> dict:
    """Read-only recurrence assessment. Never auto-parks a route."""
    _require_main()
    return _wrap(do_analyze_recurrence, project, tool_hash=tool_hash)


def euler_checkpoint(project: str, retry_infra: bool = False, retry_reason: str = "") -> dict:
    """Trigger or replay final audit when the target is ready."""
    _require_main()
    return _wrap(do_checkpoint, project, retry_infra=retry_infra, retry_reason=retry_reason)


_TOOLS = {
    "euler_frontier": euler_frontier,
    "euler_update_frontier": euler_update_frontier,
    "euler_admit_fact": euler_admit_fact,
    "euler_kernel_check": euler_kernel_check,
    "euler_kernel_align": euler_kernel_align,
    "euler_compute": euler_compute,
    "euler_compute_review": euler_compute_review,
    "euler_project_status": euler_project_status,
    "euler_register_contract": euler_register_contract,
    "euler_propose_action": euler_propose_action,
    "euler_schedule": euler_schedule,
    "euler_record_failure": euler_record_failure,
    "euler_analyze_recurrence": euler_analyze_recurrence,
    "euler_checkpoint": euler_checkpoint,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    """Build the stdio MCP app. Non-main roles get zero tools."""
    resolved = role if role is not None else _role()
    app = FastMCP("euler-control")
    for name in tools_for(resolved):
        app.tool(name=name)(_TOOLS[name])
    return app


def main() -> None:
    role = _role()
    if role not in {"main", "all"}:
        raise SystemExit("euler-control MCP requires EULER_ROLE=main")
    build_app(role).run()


if __name__ == "__main__":
    main()
