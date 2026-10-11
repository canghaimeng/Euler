# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""``euler`` — the main agent's control surface over codex workers.

    euler list          [--json]
    euler new           <project> [--profile native|codex] [--roles ...]
    euler init          <project> --contract FILE --policy FILE [--obligation FILE]
    euler propose       <project>/<worker> --candidate FILE
    euler schedule      <project> [--dispatch] [--launch]
    euler assign        <project>/<worker> (--task "…" | --file P | --stdin)
    euler finalize      <project> [--paper <paper_id>] [<fact_id> ...]
    euler start         <project>[/<worker>]
    euler status        <project>[/<worker>] [--json]
    euler reconcile     <project>/<worker>
    euler checkpoint    <project>
    euler failures      <project> [--json]
    euler stop          <project>[/<worker>] [--force]
    euler doctor        [--json]
    euler verify-health [--json]
    euler memory-audit  <project> [--json] [--strict-legacy] [--output PATH]
    euler demo          [--json]

This module is the verbs/UX only. The worker outer loop, the on-disk layout, and
the scaffolding they drive live in ``euler.execution`` (imported here as a
library). Reads/writes only files under the project dir — the loop is autonomous;
this CLI just assigns / starts / monitors / stops it.

``list`` / ``status`` / ``failures`` / ``doctor`` / ``verify-health`` /
``memory-audit`` are
read-only and stay usable when the verify service is down. ``start`` is
fail-closed: proving preflight must pass before any worker subprocess is spawned.
There is no ``submit`` verb — fact writes go only through the worker MCP tool
``fact_submit``. Direct gateway imports and JSONL writes are prohibited.

Notes:
  - the layout + scaffolding + config template are imported from ``euler.execution``
    (no duplicated layout / config template);
  - the verbs are mode-agnostic and identical across deployments.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import time
from pathlib import Path
from typing import Dict, List, Optional

from euler.execution import layout as L
from euler.execution.loop import write_status
from euler.execution.preflight import (
    classify_verify_identity,
    doctor_checks,
    run_proving_preflight,
)
from euler.execution.context import (
    TaskContextError,
    render_task_markdown,
    require_proving_task_context,
    valid_context_id,
)
from euler.execution.memory_digest import parse_task_text
from euler.execution.memory_contract_audit import (
    audit_exit_code,
    audit_project,
    exclusive_write_report,
    format_audit,
)
from euler.execution.failure_clusters import list_clusters, worker_failure_view
from euler.execution.scaffold import atomic_write, do_new, spawn_loop, write_worker_configs
from euler.providers.registry import read_role_file
from euler.control.cli import (
    ControlSurfaceError,
    control_status_payload,
    do_checkpoint,
    do_init,
    do_propose,
    do_reconcile,
    do_schedule,
    do_frontier,
    do_research_update,
    do_admit_fact,
    do_kernel_check,
    do_kernel_align,
    do_compute,
    do_compute_review,
    dumps as _control_dumps,
)

__all__ = [
    "do_new", "do_assign", "do_start", "do_status", "worker_status",
    "do_list", "do_stop", "do_finalize", "do_verify_health", "do_doctor",
    "do_failures", "do_memory_audit",
    "do_init", "do_propose", "do_schedule", "do_reconcile", "do_checkpoint",
    "build_parser", "main",
]

# --------------------------------------------------------------------------- #
# read helpers                                                                 #
# --------------------------------------------------------------------------- #

def _read_pid(wl: L.WorkerLayout) -> Optional[int]:
    pf = wl.pid
    if not pf.exists():
        return None
    try:
        return int(pf.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    # The pid exists — but a zombie (killed, not yet reaped by its parent) is
    # effectively dead. Linux /proc tells us the process state.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        state = stat.rsplit(")", 1)[1].split()[0]  # field after "(comm)"
        return state != "Z"
    except (OSError, IndexError):
        return True


def _read_status(wl: L.WorkerLayout) -> Dict:
    sp = wl.status
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# --------------------------------------------------------------------------- #
# assign                                                                       #
# --------------------------------------------------------------------------- #

def do_assign(target: str, task: str,
              branch_id: Optional[str] = None,
              subgoal_id: Optional[str] = None) -> Dict:
    """Overwrite (replace, NOT append) a worker's TASK.md, ensuring a trailing
    newline. Rejects a bare project, a nonexistent worker, and an empty task.
    ``--branch-id`` / ``--subgoal-id`` (or validated TASK.md frontmatter) stamp
    proving context; flags override frontmatter. Euler-managed projects reject
    this public verb; controller dispatch writes TASK internally."""
    project, worker = L.resolve_target(target)
    if not worker:
        raise SystemExit("assign needs a specific worker: <project>/<worker>")
    from euler.control.project import is_euler_project

    if is_euler_project(L.project_dir(project)):
        raise SystemExit(
            "direct assign is disabled for Euler-managed projects; "
            "use EulerController.dispatch"
        )
    wl = L.WorkerLayout(L.worker_dir(project, worker))
    if not wl.dir.is_dir():
        raise SystemExit(f"no such worker: {project}/{worker}")
    if not task.strip():
        raise SystemExit("refusing to assign an empty task")
    ctx = parse_task_text(task)
    flags_used = branch_id is not None or subgoal_id is not None
    b = branch_id if branch_id is not None else ctx.branch_id
    s = subgoal_id if subgoal_id is not None else ctx.subgoal_id
    if flags_used or (ctx.source == "frontmatter" and (ctx.branch_id or ctx.subgoal_id)):
        if not valid_context_id(b) or not valid_context_id(s):
            raise SystemExit(
                "assign needs valid --branch-id and --subgoal-id "
                "(or matching TASK.md frontmatter)"
            )
        body = ctx.body if ctx.source == "frontmatter" else task
        text = render_task_markdown(
            project_id=project, branch_id=b, subgoal_id=s, body=body,
        )
    else:
        text = task if task.endswith("\n") else task + "\n"
    atomic_write(wl.task, text)
    return {"worker": f"{project}/{worker}", "task_file": str(wl.task),
            "branch_id": b, "subgoal_id": s}


# --------------------------------------------------------------------------- #
# finalize                                                                     #
# --------------------------------------------------------------------------- #

def do_finalize(project: str, fact_ids: List[str],
                paper_id: Optional[str] = None) -> Dict:
    """Record the finalized target theorem(s) for a PAPER of a project in that
    paper's TARGET.md — the durable slot write-paper reads (never a guess). The
    default paper writes the LEGACY ``<project>/TARGET.md``; a non-default
    ``paper_id`` writes ``<project>/papers/<paper_id>/TARGET.md`` (its own
    workspace). One fact graph per project; per-paper targets.

    Resolves the project dir, VALIDATES every ``fact_id`` against that project's
    fact graph (refuses an id the graph does not have — you cannot record a
    phantom target), then writes the ids to the paper's TARGET.md.

    With NO ``fact_ids`` (suggestion mode): prints the candidate terminal facts
    (facts that are no other fact's predecessor — the ``assemble._terminal_facts``
    helper) as SUGGESTIONS and writes NOTHING (returns ``{"suggested": [...]}``).

    Rejections raise ``SystemExit`` (nonzero exit) with a clear message."""
    from euler.core import FactGraph
    from euler.write_paper import assemble

    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    fg = FactGraph(pdir)

    if not fact_ids:
        # suggestion mode: never auto-pick — just list candidate terminal facts.
        return {"project": project, "paper_id": paper_id,
                "suggested": assemble._terminal_facts(fg)}

    unknown = [fid for fid in fact_ids if not fg.exists(fid)]
    if unknown:
        raise SystemExit(
            f"cannot finalize: unknown fact id(s) in {project}: {', '.join(unknown)} "
            f"(a target must be a verified fact in the project's graph)"
        )
    # validate a non-default paper_id as a single safe path segment before writing.
    try:
        if not assemble._is_default_paper(paper_id):
            assemble._validate_paper_id(paper_id)  # type: ignore[arg-type]
    except ValueError as e:
        raise SystemExit(f"cannot finalize: {e}")
    # de-dup while preserving order
    seen: set = set()
    ids: List[str] = []
    for fid in fact_ids:
        if fid not in seen:
            seen.add(fid)
            ids.append(fid)
    path = assemble.write_target_fact_ids(pdir, ids, paper_id)
    return {"project": project, "paper_id": paper_id,
            "target_file": str(path), "target_fact_ids": ids}


# --------------------------------------------------------------------------- #
# start                                                                        #
# --------------------------------------------------------------------------- #

def _start_one(wl: L.WorkerLayout) -> str:
    """Returns 'started' / 'already-running' / 'locked' / 'precheck-failed'.
    Idempotent via an flock on .pid.lock; clears a stale .stop before spawning.
    Proving preflight runs before ``spawn_loop``: on failure no worker
    subprocess is launched and status is ``PRECHECK_FAILED``."""
    wl.dir.mkdir(parents=True, exist_ok=True)
    wl.logs.mkdir(exist_ok=True)
    lock = open(wl.lock, "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        if _alive(_read_pid(wl)):
            return "already-running"
        from euler.control.dispatch import DispatchError, DispatchUnprepared, validate_managed_start
        from euler.control.project import is_euler_project

        if is_euler_project(wl.project_dir):
            try:
                validate_managed_start(wl.project_dir, wl.name)
            except DispatchUnprepared as exc:
                write_status(
                    wl, state="DISPATCH_UNPREPARED", round=0,
                    dispatch={"error": str(exc)},
                )
                return "unprepared"
            except DispatchError as exc:
                write_status(
                    wl, state="DISPATCH_INVALID", round=0,
                    dispatch={"error": str(exc)},
                )
                return "dispatch-invalid"
        wl.stop.unlink(missing_ok=True)  # clear a stale stop flag
        try:
            require_proving_task_context(wl)
        except TaskContextError as e:
            write_status(wl, state="TASK_CONTEXT_FAILED", round=0,
                         task_context=e.to_status_dict())
            return "task-context-failed"
        write_worker_configs(wl, read_role_file(wl))
        pre = run_proving_preflight(wl)
        if not pre.ok:
            write_status(wl, state="PRECHECK_FAILED", round=0,
                         precheck=pre.to_status_dict())
            return "precheck-failed"
        pid = spawn_loop(wl.dir)
        atomic_write(wl.pid, str(pid))
        return "started"
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def do_start(target: str, stagger: float = 0.2) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    out = []
    for i, wdir in enumerate(dirs):
        if i and stagger:
            time.sleep(stagger)
        wl = L.WorkerLayout(wdir)
        result = _start_one(wl)
        row: Dict = {"worker": wdir.name, "result": result}
        if result == "precheck-failed":
            st = _read_status(wl)
            if st.get("precheck"):
                row["precheck"] = st["precheck"]
        if result == "task-context-failed":
            st = _read_status(wl)
            if st.get("task_context"):
                row["task_context"] = st["task_context"]
        if result in {"unprepared", "dispatch-invalid"}:
            st = _read_status(wl)
            if st.get("dispatch"):
                row["dispatch"] = st["dispatch"]
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# status                                                                       #
# --------------------------------------------------------------------------- #

def worker_status(wl: L.WorkerLayout) -> Dict:
    pid = _read_pid(wl)
    alive = _alive(pid)
    st = _read_status(wl)
    state = st.get("state", "—")
    now = time.time()
    if alive and state == "running":
        # A restarted process may still carry a previous round's completion
        # timestamp in a legacy merged status document. Running age is the
        # current round age, never the age of that historical completion.
        last = st.get("round_started_at") or st.get("updated_at")
    else:
        last = st.get("last_round_at") or st.get("round_started_at") or st.get("updated_at")
    age = (now - last) if isinstance(last, (int, float)) else None

    if alive:
        # a round legitimately runs for hours; only flag truly stale running rounds
        rs = st.get("round_started_at")
        hard = int(os.environ.get("EULER_ROUND_HARD_TIMEOUT", "14400"))
        if state == "running" and isinstance(rs, (int, float)) and (now - rs) > hard * 1.5:
            label = "stuck?"
        else:
            label = "working"
    else:
        label = state if state in ("stopped", "deadline", "max_rounds", "error",
                                   "terminated", "created", "PRECHECK_FAILED",
                                   "RECEIPT_FAILED", "TASK_CONTEXT_FAILED",
                                   "MEMORY_CONTRACT_FAILED",
                                   "FAILURE_CONTROL_FAILED",
                                   "FAILURE_SYNTHESIS_FAILED") else "dead"
    row = {
        "worker": wl.name, "pid": pid, "alive": alive, "state": state,
        "round": st.get("round", 0), "age_s": round(age, 1) if age is not None else None,
        "last_fact_id": st.get("last_fact_id"), "label": label,
    }
    if state == "PRECHECK_FAILED" and st.get("precheck"):
        row["precheck"] = st["precheck"]
    if state == "RECEIPT_FAILED" and st.get("receipt"):
        row["receipt"] = st["receipt"]
    if state == "TASK_CONTEXT_FAILED" and st.get("task_context"):
        row["task_context"] = st["task_context"]
    memory_contract = st.get("memory_contract")
    expected = (
        memory_contract.get("expected")
        if isinstance(memory_contract, dict) else None
    )
    expected_run = expected.get("run_id") if isinstance(expected, dict) else None
    if memory_contract and (
        state == "MEMORY_CONTRACT_FAILED"
        or not st.get("run_id")
        or expected_run == st.get("run_id")
    ):
        row["memory_contract"] = st["memory_contract"]
    if state == "FAILURE_SYNTHESIS_FAILED" and st.get("failure_synthesis"):
        row["failure_synthesis"] = st["failure_synthesis"]
    if state == "FAILURE_CONTROL_FAILED" and st.get("failure_control"):
        row["failure_control"] = st["failure_control"]
    if st.get("failure_clusters"):
        row["failure_clusters"] = st["failure_clusters"]
    row["failures"] = worker_failure_view(wl)
    return row


def do_status(target: str) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [worker_status(L.WorkerLayout(d)) for d in dirs]


def _status_control_block(target: str) -> Optional[Dict]:
    project, _worker = L.resolve_target(target)
    try:
        return control_status_payload(project)
    except ControlSurfaceError:
        return None


def do_failures(project: str) -> Dict:
    """Read-only cluster ledger for a project. Does not read worker private memory."""
    pdir = L.project_dir(project)
    if not pdir.is_dir():
        raise SystemExit(f"no such project: {project}")
    clusters = list_clusters(pdir)
    return {
        "project": project,
        "clusters": [
            {
                "cluster_id": c.get("cluster_id"),
                "state": c.get("state"),
                "trigger": c.get("trigger"),
                "signature": c.get("signature") or "",
                "branch_id": c.get("branch_id"),
                "subgoal_id": c.get("subgoal_id"),
                "assigned_worker": c.get("assigned_worker"),
                "workers": c.get("workers") or [],
                "attempt_ids": c.get("attempt_ids") or [],
                "verification_ids": c.get("verification_ids") or [],
                "obstacle_ids": c.get("obstacle_ids") or [],
                "created_at": c.get("created_at"),
                "seq": c.get("seq"),
            }
            for c in clusters
        ],
    }


def _fmt_failures(payload: Dict) -> str:
    rows = payload.get("clusters") or []
    if not rows:
        return f"(no failure clusters for {payload.get('project')})"
    head = f"{'CLUSTER':<18}{'STATE':<12}{'TRIGGER':<24}{'WORKER':<14}{'SUBGOAL':<16}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{str(r.get('cluster_id') or '—'):<18}"
            f"{str(r.get('state') or '—'):<12}"
            f"{str(r.get('trigger') or '—'):<24}"
            f"{str(r.get('assigned_worker') or '—'):<14}"
            f"{str(r.get('subgoal_id') or '—'):<16}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# list                                                                         #
# --------------------------------------------------------------------------- #

def do_list() -> List[Dict]:
    """One row per project: roster + how many workers are live + model."""
    out: List[Dict] = []
    for project in L.list_projects():
        meta = {}
        mp = L.project_dir(project) / "project.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        workers = L.list_workers(project)
        live = sum(1 for w in workers
                   if _alive(_read_pid(L.WorkerLayout(L.worker_dir(project, w)))))
        out.append({"project": project, "workers": len(workers), "live": live,
                    "model": meta.get("model", "—")})
    return out


def _fmt_list(rows: List[Dict]) -> str:
    head = f"{'PROJECT':<24}{'WORKERS':>8}{'LIVE':>6}  {'MODEL':<12}"
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(f"{r['project']:<24}{r['workers']:>8}{r['live']:>6}  {str(r['model']):<12}")
    return "\n".join(lines) if rows else "(no projects under the agents root)"


def _fmt_status(rows: List[Dict]) -> str:
    head = f"{'WORKER':<14}{'LABEL':<12}{'STATE':<13}{'ROUND':>6}  {'AGE':>7}  {'LAST_FACT':<16}"
    lines = [head, "-" * len(head)]
    for r in rows:
        age = f"{r['age_s']:.0f}s" if r["age_s"] is not None else "—"
        lines.append(f"{r['worker']:<14}{r['label']:<12}{r['state']:<13}"
                     f"{r['round']:>6}  {age:>7}  {str(r['last_fact_id'] or '—'):<16}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# stop                                                                         #
# --------------------------------------------------------------------------- #

def _stop_one(wl: L.WorkerLayout, force: bool) -> str:
    pid = _read_pid(wl)
    if not force:
        if not _alive(pid):
            return "not-running"
        wl.stop.touch()      # graceful: loop exits at round boundary
        return "stopping (graceful)"
    # force: kill the loop's process group (loop + its codex child), then SIGKILL
    if not _alive(pid):
        wl.pid.unlink(missing_ok=True)
        return "not-running"
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(50):                          # up to ~5s for a clean exit
        if not _alive(pid):
            break
        time.sleep(0.1)
    if _alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    wl.pid.unlink(missing_ok=True)
    return "killed"


def do_stop(target: str, force: bool = False) -> List[Dict]:
    dirs = L.target_worker_dirs(target)
    if not dirs:
        raise SystemExit(f"no workers for target {target!r}")
    return [{"worker": d.name, "result": _stop_one(L.WorkerLayout(d), force)} for d in dirs]


# --------------------------------------------------------------------------- #
# verify-health / doctor (read-only; usable when verify is down)               #
# --------------------------------------------------------------------------- #

def do_verify_health() -> Dict:
    """Read-only verify-service identity. Never POSTs /verify (not a math verdict)."""
    return classify_verify_identity()


def do_doctor() -> Dict:
    """Read-only proving-readiness doctor. Does not write facts/memory or start workers."""
    checks = doctor_checks()
    failed = [c.as_dict() for c in checks if c.level == "fail"]
    warnings = [c.as_dict() for c in checks if c.level == "warn"]
    return {
        "ok": not failed,
        "checks": [c.as_dict() for c in checks],
        "failed": failed,
        "warnings": warnings,
    }


def do_memory_audit(project: str, *, strict_legacy: bool = False) -> Dict:
    """Read-only Memory Contract audit. Never writes project stores."""
    try:
        return audit_project(project, strict_legacy=strict_legacy)
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from e


def _fmt_verify_health(info: Dict) -> str:
    ident = info.get("identity", "down")
    pid = info.get("health_pid") or info.get("our_pid") or "—"
    lines = [
        f"verify: {ident}  pid={pid}  url={info.get('health_url', '')}",
    ]
    if info.get("reason"):
        lines.append(f"reason: {info['reason']}")
    lines.append(f"note: {info.get('note', 'transport identity only; not a mathematical verdict')}")
    return "\n".join(lines)


def _fmt_doctor(payload: Dict) -> str:
    lines = ["== Euler doctor =="]
    for c in payload.get("checks", []):
        level = c.get("level") or ("ok" if c.get("ok") else "fail")
        tag = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}.get(level, level)
        reason = c.get("reason") or ""
        extra = f" {reason}" if reason else ""
        lines.append(f"  {tag}  {c.get('component')}{extra}")
    lines.append("done.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# argparse                                                                      #
# --------------------------------------------------------------------------- #

def _task_from_args(args) -> str:
    import sys
    if args.task is not None:
        return args.task
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.read()
    raise SystemExit("assign needs one of --task, --file, or --stdin")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="euler", description="Control Euler workers.")
    sub = p.add_subparsers(dest="cmd", required=True)

    li = sub.add_parser("list", help="list all projects + live worker counts")
    li.add_argument("--json", action="store_true")

    n = sub.add_parser("new", help="scaffold a project + worker dirs")
    n.add_argument("project")
    n.add_argument(
        "--profile",
        choices=("native", "codex"),
        default="native",
        help=(
            "worker architecture (default: six-worker native-provider team)"
        ),
    )
    n.add_argument(
        "--roles",
        default=None,
        help=(
            "custom roster, e.g. codex:1,claude-code:1,grok:1,cursor:1 "
            "(or high:3,xhigh:4); overrides --profile"
        ),
    )
    n.add_argument("--model", default=None)

    ini = sub.add_parser("init", help="initialize Euler control on an existing project")
    ini.add_argument("project")
    ini.add_argument("--contract", required=True, help="ProblemContract JSON file")
    ini.add_argument("--policy", required=True, help="BudgetPolicy JSON file")
    ini.add_argument("--obligation", default=None, help="target Obligation JSON; default: exact contract statement")

    frontier = sub.add_parser("frontier", help="show active obligations, routes, blockers and evidence")
    frontier.add_argument("project")

    update = sub.add_parser("research-update", help="apply a validated atomic research-state change set")
    update.add_argument("project")
    update.add_argument("--operations", required=True, help="JSON list of typed research events")

    admit = sub.add_parser("admit-fact", help="link an existing admitted Fact to a reviewed obligation")
    admit.add_argument("project")
    admit.add_argument("fact_id")
    admit.add_argument("--obligation", required=True)
    admit.add_argument("--scope-hash", required=True)
    admit.add_argument("--alignment-note", required=True)

    kernel = sub.add_parser("kernel-check", help="execute a reserved Lean kernel check")
    kernel.add_argument("project")
    kernel.add_argument("--obligation", required=True)
    kernel.add_argument("--request", required=True)
    kernel.add_argument("--request-id", required=True, help="idempotency identifier; change only for an intentional new check")

    align = sub.add_parser("kernel-align", help="record reviewed statement alignment for an existing kernel check")
    align.add_argument("project")
    align.add_argument("--obligation", required=True)
    align.add_argument("--artifact", required=True)
    align.add_argument("--review", required=True)

    compute = sub.add_parser("compute", help="run a bounded supported search/check action")
    compute.add_argument("project")
    compute.add_argument("--request", required=True)
    compute_review = sub.add_parser("compute-review", help="review the original-object meaning of a checked candidate")
    compute_review.add_argument("project")
    compute_review.add_argument("--review", required=True)

    pr = sub.add_parser("propose", help="persist a CandidateAction for a worker")
    pr.add_argument("target", help="<project>/<worker>")
    pr.add_argument("--candidate", required=True, help="CandidateAction JSON file")

    sch = sub.add_parser("schedule", help="run the transparent scheduler")
    sch.add_argument("project")
    sch.add_argument("--dispatch", action="store_true", help="write lease-bound TASK/sidecar for SELECTED candidates")
    sch.add_argument("--launch", action="store_true", help="also start worker loops (implies --dispatch)")

    rec = sub.add_parser("reconcile", help="reconcile the latest verified receipt chain")
    rec.add_argument("target", help="<project>/<worker>")

    ck = sub.add_parser("checkpoint", help="trigger or replay final audit when the target is ready")
    ck.add_argument("project")
    ck.add_argument("--retry-infra", action="store_true")
    ck.add_argument("--retry-reason", default="")

    a = sub.add_parser("assign", help="write a worker's per-round TASK.md")
    a.add_argument("target", help="<project>/<worker>")
    a.add_argument("--task", default=None)
    a.add_argument("--file", default=None)
    a.add_argument("--stdin", action="store_true")
    a.add_argument("--branch-id", default=None, dest="branch_id")
    a.add_argument("--subgoal-id", default=None, dest="subgoal_id")

    f = sub.add_parser("finalize", help="record the finalized target fact_id(s) in "
                                        "a paper's TARGET.md (write-paper reads this)")
    f.add_argument("project")
    f.add_argument("--paper", default=None,
                   help="the paper_id (multiple papers per project). Default / 'main' "
                        "→ legacy <project>/TARGET.md; else "
                        "<project>/papers/<paper_id>/TARGET.md")
    f.add_argument("fact_ids", nargs="*",
                   help="the target fact id(s); omit to print candidate terminal facts")

    s = sub.add_parser("start", help="launch worker loop(s)")
    s.add_argument("target", help="<project> or <project>/<worker>")

    st = sub.add_parser("status", help="liveness + progress")
    st.add_argument("target", help="<project> or <project>/<worker>")
    st.add_argument("--json", action="store_true")

    fa = sub.add_parser("failures", help="read-only failure-cluster ledger (no private memory)")
    fa.add_argument("project")
    fa.add_argument("--json", action="store_true")

    sp = sub.add_parser("stop", help="stop worker loop(s)")
    sp.add_argument("target", help="<project> or <project>/<worker>")
    sp.add_argument("--force", action="store_true", help="kill now (else finish current round)")

    vh = sub.add_parser("verify-health", help="read-only verify-service identity (not a math verdict)")
    vh.add_argument("--json", action="store_true")

    doc = sub.add_parser("doctor", help="read-only proving-readiness checks (python, gateway, verify identity)")
    doc.add_argument("--json", action="store_true")

    ma = sub.add_parser(
        "memory-audit",
        help="read-only Memory Contract audit (M1–M8); never writes project stores",
    )
    ma.add_argument("project")
    ma.add_argument("--json", action="store_true")
    ma.add_argument(
        "--strict-legacy",
        action="store_true",
        help="fail M1 when pre-contract/dry start-only receipts are present",
    )
    ma.add_argument(
        "--output",
        default=None,
        help="write the JSON report to PATH (O_EXCL; refuses to overwrite)",
    )

    demo = sub.add_parser(
        "demo",
        help="offline integration demo of the live agent loop (not a benchmark)",
    )
    demo.add_argument("--json", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "list":
        rows = do_list()
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else _fmt_list(rows))
    elif args.cmd == "new":
        r = do_new(args.project, roles=args.roles, model=args.model, profile=args.profile)
        print(f"created {args.project} with {len(r['workers'])} workers: "
              f"{', '.join(r['workers'])}\n  {r['project_dir']}")
    elif args.cmd == "init":
        try:
            payload = do_init(
                args.project,
                contract=args.contract,
                policy=args.policy,
                obligation=args.obligation,
            )
        except ControlSurfaceError as exc:
            raise SystemExit(str(exc)) from exc
        print(_control_dumps(payload))
    elif args.cmd in {"frontier", "research-update", "admit-fact", "kernel-check", "kernel-align", "compute", "compute-review"}:
        try:
            if args.cmd == "frontier":
                payload = do_frontier(args.project)
            elif args.cmd == "research-update":
                payload = do_research_update(args.project, args.operations)
            elif args.cmd == "kernel-check":
                payload = do_kernel_check(args.project, args.obligation, args.request, request_id=args.request_id)
            elif args.cmd == "kernel-align":
                payload = do_kernel_align(args.project, args.obligation, args.artifact, args.review)
            elif args.cmd == "compute":
                payload = do_compute(args.project, args.request)
            elif args.cmd == "compute-review":
                payload = do_compute_review(args.project, args.review)
            else:
                payload = do_admit_fact(args.project, args.fact_id, args.obligation,
                                        confirmed_scope_hash=args.scope_hash,
                                        alignment_note=args.alignment_note)
        except (ControlSurfaceError, ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
        print(_control_dumps(payload))
    elif args.cmd == "propose":
        try:
            payload = do_propose(args.target, args.candidate)
        except ControlSurfaceError as exc:
            raise SystemExit(str(exc)) from exc
        print(_control_dumps(payload))
    elif args.cmd == "schedule":
        try:
            payload = do_schedule(
                args.project, dispatch=args.dispatch, launch=args.launch
            )
        except ControlSurfaceError as exc:
            raise SystemExit(str(exc)) from exc
        print(_control_dumps(payload))
    elif args.cmd == "reconcile":
        try:
            payload = do_reconcile(args.target)
        except ControlSurfaceError as exc:
            raise SystemExit(str(exc)) from exc
        print(_control_dumps(payload))
    elif args.cmd == "checkpoint":
        try:
            payload = do_checkpoint(args.project, retry_infra=args.retry_infra, retry_reason=args.retry_reason)
        except ControlSurfaceError as exc:
            raise SystemExit(str(exc)) from exc
        print(_control_dumps(payload))
    elif args.cmd == "assign":
        r = do_assign(args.target, _task_from_args(args),
                      branch_id=args.branch_id, subgoal_id=args.subgoal_id)
        extra = ""
        if r.get("branch_id") and r.get("subgoal_id"):
            extra = f" (branch={r['branch_id']} subgoal={r['subgoal_id']})"
        print(f"assigned {r['worker']} -> {r['task_file']}{extra}")
    elif args.cmd == "finalize":
        r = do_finalize(args.project, args.fact_ids, paper_id=args.paper)
        paper_note = f" (paper {args.paper})" if args.paper else ""
        paper_flag = f" --paper {args.paper}" if args.paper else ""
        if "suggested" in r:
            sug = r["suggested"]
            if sug:
                print(f"no fact_id given — candidate target facts for {r['project']}{paper_note} "
                      f"(terminal facts; nothing depends on them):")
                for fid in sug:
                    print(f"  {fid}")
                print(f"\nrun: euler finalize {r['project']}{paper_flag} <fact_id> [<fact_id> ...] to record")
            else:
                print(f"no candidate terminal facts in {r['project']} "
                      f"(is the fact graph empty?); nothing recorded")
        else:
            print(f"finalized target for {r['project']}{paper_note}: {', '.join(r['target_fact_ids'])}\n"
                  f"  wrote {r['target_file']}")
    elif args.cmd == "start":
        rows = do_start(args.target)
        failed = False
        for r in rows:
            extra = ""
            if r.get("result") == "precheck-failed":
                failed = True
                pre = r.get("precheck") or {}
                extra = f" ({pre.get('component')}: {pre.get('reason')})"
            if r.get("result") == "task-context-failed":
                failed = True
                tc = r.get("task_context") or {}
                extra = f" ({tc.get('reason', 'missing branch_id/subgoal_id')})"
            print(f"{r['worker']}: {r['result']}{extra}")
        return 1 if failed else 0
    elif args.cmd == "status":
        rows = do_status(args.target)
        control = _status_control_block(args.target)
        if args.json:
            project, worker = L.resolve_target(args.target)
            if control is not None and worker is None:
                print(_control_dumps({"control": control, "workers": rows}))
            else:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            if control is not None:
                print(
                    "CONTROL  plane={control_plane}  seq={max_seq}  "
                    "proposed={n}  budget={kind}".format(
                        control_plane=control.get("control_plane"),
                        max_seq=control.get("max_seq"),
                        n=len(control.get("proposed_ids") or ()),
                        kind=control.get("budget_kind"),
                    )
                )
            print(_fmt_status(rows))
    elif args.cmd == "failures":
        payload = do_failures(args.project)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _fmt_failures(payload))
    elif args.cmd == "stop":
        for r in do_stop(args.target, force=args.force):
            print(f"{r['worker']}: {r['result']}")
    elif args.cmd == "verify-health":
        info = do_verify_health()
        print(json.dumps(info, ensure_ascii=False, indent=2) if args.json else _fmt_verify_health(info))
        return int(info.get("exit_code", 0 if info.get("ok") else 5))
    elif args.cmd == "doctor":
        payload = do_doctor()
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else _fmt_doctor(payload))
        return 0 if payload.get("ok") else 1
    elif args.cmd == "memory-audit":
        report = do_memory_audit(args.project, strict_legacy=args.strict_legacy)
        if args.output:
            try:
                exclusive_write_report(Path(args.output), report)
            except FileExistsError as e:
                raise SystemExit(str(e)) from e
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_audit(report))
        return audit_exit_code(report)
    elif args.cmd == "demo":
        from euler.demo import run_offline_demo

        report = run_offline_demo()
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(report.format_text())
        return 0 if report.ok else 1
    return 0
