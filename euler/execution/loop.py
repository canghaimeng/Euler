# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""The per-worker autonomous outer loop — the round driver.

Launched detached by ``euler start`` (``python -m euler.execution <worker_dir>``).
Self-contained. Each round runs ONE native provider session
whose internal control loop (worker.md + the worker skills) drives toward a full
verified result — a round is *continue solving from persisted memory*, NOT one
increment. The round ends when codex's session ends (its stopping rule, the
per-round hard timeout, or it bails); the loop then relaunches a fresh session
that resumes from memory. Stops on the ``.stop`` flag (graceful, at a round
boundary), the project deadline, or a round backstop.

Config:
  - codex binary resolved via the shared ``euler.codex`` launcher
    (``EULER_CODEX_BIN`` / ``CODEX_BIN`` alias / PATH);
  - all config read at CALL time from env (matches core/gateway/verify).

Env (all optional; tests inject these):
  EULER_CODEX_BIN            codex binary (default "codex")
  EULER_ROUND_BEAT           seconds to sleep between rounds (default 5)
  EULER_ROUND_HARD_TIMEOUT   per-round hard timeout, seconds (default 14400 = 4h)
  EULER_MAX_ROUNDS           round backstop, 0 = unlimited (default 0)
  EULER_MAX_CONSEC_FAILURES  bail after this many consecutive failed rounds (default 5)
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from euler.providers.base import ProviderError, ProviderName
from euler.providers.cursor import cursor_exec_cmd as _cursor_exec_cmd
from euler.providers.registry import (
    build_command,
    provider_from_role,
    read_role_file,
    scrub_worker_env,
)

from . import layout as L
from . import scaffold
from .context import (
    TaskContextError,
    apply_stamp_env,
    extra_mcp_env,
    new_run_id,
    require_proving_task_context,
    stamp_env,
)
from .launch_receipt import persist_launch_receipt, persist_result_receipt
from .memory_digest import (
    RoundReceiptError,
    allocate_receipt_id,
    finalize_round_end,
    prepare_round_start,
    render_digest_prompt,
)
from .preflight import run_proving_preflight
from .failure_clusters import (
    FailureControlError,
    FailureSynthesisError,
    after_successful_close,
    next_pending_request,
    render_synthesis_prompt,
    resolve_request,
    evaluate_synthesis_close,
)
from .round_contract import MemoryContractError, evaluate_round_close
_FACT_ID_RE = re.compile(r'"?fact_id"?\s*[:=]\s*"?([0-9a-f]{16})"?')


# --- the per-round prompt (continuation semantics; see worker.md) ----------- #

def kickoff(project: str, worker: str, *,
            receipt_path: str = "", digest_hash: str = "",
            receipt_hash: str = "", digest_text: str = "",
            synthesis_text: str = "",
            managed_context: bool = False) -> str:
    text = (
        f"You are worker '{worker}' on project '{project}'. Continue solving the "
        f"problem (this is a continuation round, not a fresh start).\n"
        f"1. Read TASK.md — your current assignment (which direction/subgoal is yours).\n"
        f"2. Follow AGENTS.md (worker.md) exactly — your standing contract (the adaptive "
        f"control loop, memory discipline, the fact_submit gate). Drive toward a full "
        f"verified result.\n"
    )
    if managed_context:
        text += (
            "3. Resume from the compiled ContextPacket already bound in TASK.md. That "
            "packet is the bounded automatic history (dependency interfaces and scoped "
            "blockers). Receipt fields below are provenance, not extra transcript.\n"
        )
    else:
        text += (
            "3. Resume from the orchestrator memory digest below (already retrieved) plus "
            "the fact graph and the latest master_guidance — DO NOT restart from zero; "
            "do not skip this digest.\n"
        )
    text += (
        f"4. Keep going: assess -> pick skills adaptively -> act -> persist, repeatedly. "
        f"An open problem is not a reason to stop. Do NOT finalize prematurely.\n"
        f"5. Persist as you go: rough progress to local memory; shareable findings via "
        f"gm_add; any verified result via fact_submit."
    )
    extra: list = []
    if receipt_path:
        extra.append(f"receipt_path: {receipt_path}")
    if digest_hash:
        extra.append(f"digest_hash: {digest_hash}")
    if receipt_hash:
        extra.append(f"receipt_hash: {receipt_hash}")
    if managed_context:
        extra.append(
            "automatic_history: compiled ContextPacket in TASK.md (bounded compiler)"
        )
        extra.append("do not treat receipt metadata as extra historical transcript")
    elif digest_text:
        extra.append(digest_text)
    elif receipt_path or digest_hash:
        extra.append(render_digest_prompt({}, receipt_path, digest_hash, receipt_hash))
    if extra:
        text += "\n\n" + "\n".join(extra)
    if synthesis_text:
        text += "\n\n" + synthesis_text
    return text


def cursor_exec_cmd(binary: str, workspace: Path, prompt: str) -> list:
    """Exact Cursor helper argv: auto model, no fast/context flags."""
    return _cursor_exec_cmd(binary, workspace, prompt)


def worker_child_env(binary: str, role: dict, *, base: dict | None = None) -> dict:
    """Provider-owned child env. ``binary`` is ignored; PROVIDER selects scrubbing."""
    del binary
    return scrub_worker_env(role, env=base)


# --- config (read at call time) -------------------------------------------- #

# codex binary + model/effort defaults are resolved via the shared euler.codex
# launcher (EULER_CODEX_BIN / EULER_CODEX_MODEL / EULER_CODEX_EFFORT).


# --- small helpers --------------------------------------------------------- #

def _read_role(wl: L.WorkerLayout) -> dict:
    return read_role_file(wl)


def write_status(wl: L.WorkerLayout, *, replace: bool = False, **fields) -> None:
    """Atomically write status; ``replace`` starts a fresh process epoch.

    Ordinary updates merge so later fields do not erase current-round context.
    A newly spawned worker must replace the prior process's document, otherwise
    transient failures such as ``memory_contract`` remain attached to a new
    run_id and misreport healthy continuation rounds.
    """
    path = wl.status
    cur = {}
    if path.exists() and not replace:
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cur = {}
    cur.update(fields)
    cur["worker"] = wl.name
    cur["pid"] = os.getpid()
    cur["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _deadline_passed(project_dir: Path) -> bool:
    f = project_dir / L.DEADLINE_FILE
    if not f.exists():
        return False
    try:
        return time.time() >= float(f.read_text().strip())
    except (ValueError, OSError):
        return False


def _parse_last_fact_id(log_path: Path) -> Optional[str]:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    ids = _FACT_ID_RE.findall(text)
    return ids[-1] if ids else None


def _managed_loop_gate(wl: L.WorkerLayout, round_no: int) -> Optional[int]:
    """Stop a completed Euler lease instead of allocating a new round.

    Returns an exit code when the loop must not continue, otherwise None.
    Incomplete receipts are never invented; coordinator re-dispatch must issue
    a distinct attempt/lease for transient recovery.
    """
    from euler.control.hooks import (
        ReconcileError,
        recover_pending_managed_round,
    )
    from euler.control.project import is_euler_project

    if not is_euler_project(wl.project_dir):
        return None
    try:
        recovered = recover_pending_managed_round(wl)
    except ReconcileError as err:
        write_status(
            wl, state="EULER_RECONCILE_FAILED", round=round_no,
            reconcile={"reason": str(err), "ok": False},
        )
        print(f"EULER_RECONCILE_FAILED reason={err}", file=sys.stderr)
        return 1
    if recovered.disposition == "unmanaged":
        return None
    if recovered.disposition == "proceed":
        return None
    if recovered.disposition == "recovered":
        rec = recovered.reconciliation
        write_status(
            wl, state="waiting_dispatch", round=round_no,
            last_round_at=time.time(),
            reconcile={
                "ok": True,
                "recovered": True,
                "outcome_ref": None if rec is None else rec.outcome_ref,
                "reconciliation_id": None if rec is None else rec.reconciliation_id,
            },
            lease_id=recovered.lease_id,
            lease_status=recovered.lease_status,
        )
        return 0
    if recovered.disposition == "waiting_dispatch":
        write_status(
            wl, state="waiting_dispatch", round=round_no,
            lease_id=recovered.lease_id,
            lease_status=recovered.lease_status,
            reconcile={"ok": True, "reason": recovered.reason},
        )
        return 0
    write_status(
        wl, state="managed_interrupted", round=round_no,
        lease_id=recovered.lease_id,
        lease_status=recovered.lease_status,
        reconcile={"ok": False, "reason": recovered.reason or "incomplete_receipts"},
    )
    print(
        f"MANAGED_INTERRUPTED reason={recovered.reason or 'incomplete_receipts'}",
        file=sys.stderr,
    )
    return 0


# --- one round ------------------------------------------------------------- #

class _Child:
    """Holds the running codex subprocess so the SIGTERM handler can kill it."""
    proc: "subprocess.Popen | None" = None


def run_round(wl: L.WorkerLayout, role: dict, prompt: str, log_path: Path,
              hard_timeout: int) -> int:
    """Exec one configured provider session. Returns its rc, 124 on timeout,
    or 127 if the selected provider binary is missing."""
    wdir = wl.dir
    try:
        spec = build_command(wl, role, prompt)
    except (ProviderError, ValueError, OSError) as exc:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"[worker_loop] provider configuration failed: {exc}\n")
        return 127
    from euler.execution.human_control import managed_execution, wait_with_control
    from euler.control.campaign import admission_lock
    from contextlib import nullcontext
    managed = managed_execution(wl.project_dir, wl.name)
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            with admission_lock(wl.project_dir) if managed else nullcontext():
                managed = managed_execution(wl.project_dir, wl.name) if managed else None
                if managed and managed['cancelled']:
                    logf.write('[worker_loop] cancelled before provider launch\n')
                    return 130
                _Child.proc = subprocess.Popen(
                    list(spec.argv), stdout=logf, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, cwd=str(wdir),
                    env=dict(spec.env), start_new_session=True,
                )
        except FileNotFoundError:
            logf.write(f"[worker_loop] {spec.label} binary not found: {spec.argv[0]}\n")
            return 127
        try:
            if managed:
                timeout = managed['wall_seconds']
                if hard_timeout > 0:
                    timeout = min(timeout, hard_timeout) if timeout else hard_timeout
                return wait_with_control(_Child.proc, wl.project_dir, wl.name,
                                         managed['lease_id'], timeout, logf)
            return _Child.proc.wait(timeout=hard_timeout if hard_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            _Child.proc.terminate()
            try:
                _Child.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _Child.proc.kill()
            logf.write(f"\n[worker_loop] round hard-timeout after {hard_timeout}s\n")
            return 124
        finally:
            _Child.proc = None


# --- the loop -------------------------------------------------------------- #

def _cleanup_pid(wl: L.WorkerLayout) -> None:
    """Remove our own .pid if it still points at us (clean exit only)."""
    pf = wl.pid
    try:
        if pf.exists() and pf.read_text().strip() == str(os.getpid()):
            pf.unlink(missing_ok=True)
    except OSError:
        pass


def main(worker_dir: str) -> int:
    wdir = Path(worker_dir).resolve()
    if not wdir.is_dir():
        print(f"worker dir not found: {wdir}", file=sys.stderr)
        return 2
    wl = L.WorkerLayout(wdir)
    project_dir = wl.project_dir
    project = wl.project
    worker = wl.name
    role = _read_role(wl)

    # Pin the worker's gateway to THIS interpreter (sys.executable = the venv
    # python euler runs on), rewritten every start: a moved/rebuilt venv is
    # picked up, and a bare `python3` on codex's PATH can never resolve the
    # gateway to a different install.
    scaffold.write_worker_configs(wl, role)

    beat = float(os.environ.get("EULER_ROUND_BEAT", "5"))
    hard_timeout = int(os.environ.get("EULER_ROUND_HARD_TIMEOUT", "14400"))
    max_rounds = int(os.environ.get("EULER_MAX_ROUNDS", "0"))
    max_fail = int(os.environ.get("EULER_MAX_CONSEC_FAILURES", "5"))
    wl.logs.mkdir(parents=True, exist_ok=True)

    def _on_term(signum, _frame):
        if _Child.proc is not None:
            _Child.proc.terminate()
        write_status(wl, state="terminated")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    def _fail_precheck(pre, round_no: int) -> int:
        write_status(wl, state="PRECHECK_FAILED", round=round_no,
                     precheck=pre.to_status_dict())
        fail = pre.failure
        msg = (f"PRECHECK_FAILED component={fail.component} reason={fail.reason}"
               if fail else "PRECHECK_FAILED")
        print(msg, file=sys.stderr)
        return 1

    def _fail_receipt(err: RoundReceiptError, round_no: int) -> int:
        write_status(wl, state="RECEIPT_FAILED", round=round_no,
                     receipt=err.to_status_dict())
        print(f"RECEIPT_FAILED phase={err.phase} reason={err.reason}", file=sys.stderr)
        return 1

    def _fail_task_context(err: TaskContextError, round_no: int) -> int:
        write_status(wl, state="TASK_CONTEXT_FAILED", round=round_no,
                     task_context=err.to_status_dict())
        print(f"TASK_CONTEXT_FAILED reason={err.reason}", file=sys.stderr)
        return 1

    def _fail_memory_contract(err: MemoryContractError, round_no: int) -> int:
        write_status(wl, state="MEMORY_CONTRACT_FAILED", round=round_no,
                     memory_contract=err.to_status_dict())
        print(f"MEMORY_CONTRACT_FAILED reason={err.reason}", file=sys.stderr)
        return 1

    def _fail_failure_control(err: FailureControlError, round_no: int) -> int:
        write_status(wl, state="FAILURE_CONTROL_FAILED", round=round_no,
                     failure_control=err.to_status_dict())
        print(f"FAILURE_CONTROL_FAILED reason={err.reason}", file=sys.stderr)
        return 1

    def _fail_reconcile(err: Exception, round_no: int) -> int:
        write_status(
            wl, state="EULER_RECONCILE_FAILED", round=round_no,
            reconcile={"reason": str(err), "ok": False},
        )
        print(f"EULER_RECONCILE_FAILED reason={err}", file=sys.stderr)
        return 1

    run_id = new_run_id()
    os.environ["EULER_RUN_ID"] = run_id

    # Defense in depth (also runs in `euler start` before spawn): fail closed
    # before entering the round loop. Not a mathematical verdict.
    pre = run_proving_preflight(wl)
    if not pre.ok:
        return _fail_precheck(pre, 0)

    write_status(wl, replace=True, state="running", round=0,
                 started_at=time.time(), run_id=run_id)
    rnd = 0
    consec_fail = 0
    try:
        while True:
            if wl.stop.exists():
                wl.stop.unlink(missing_ok=True)
                write_status(wl, state="stopped")
                break
            if _deadline_passed(project_dir):
                write_status(wl, state="deadline")
                break
            if max_rounds and rnd >= max_rounds:
                write_status(wl, state="max_rounds")
                break

            managed = _managed_loop_gate(wl, rnd)
            if managed is not None:
                return managed

            try:
                task_ctx = require_proving_task_context(wl)
            except TaskContextError as err:
                return _fail_task_context(err, rnd)

            stamp = stamp_env(wl, task_ctx, run_id)
            apply_stamp_env(stamp)
            scaffold.write_worker_configs(wl, role, extra_env=extra_mcp_env(stamp))

            # Immediately before EVERY run_round, including continuation rounds:
            # if verify/MCP/stores/context fail, do not launch the next
            # codex subprocess.
            pre = run_proving_preflight(wl, run_id=run_id)
            if not pre.ok:
                return _fail_precheck(pre, rnd)

            # Reconcile a request-sidecar/ledger crash gap before selecting the
            # next round. This also lets a restarted worker see a cluster that
            # was durable in memory before its previous process died.
            try:
                after_successful_close(
                    wl,
                    branch_id=task_ctx.branch_id or "",
                    subgoal_id=task_ctx.subgoal_id or "",
                )
                pending_synthesis = next_pending_request(wl)
            except FailureControlError as err:
                return _fail_failure_control(err, rnd)

            rnd += 1
            try:
                receipt_id = allocate_receipt_id(wl)
                start = prepare_round_start(wl, receipt_id)
            except RoundReceiptError as err:
                return _fail_receipt(err, rnd)
            from euler.control.project import is_euler_project
            managed_context = is_euler_project(wl.project_dir)
            digest_text = ""
            if not managed_context:
                digest_text = render_digest_prompt(
                    start.digest, str(start.path), start.digest_hash,
                    start.receipt_hash,
                )
            prompt = kickoff(
                project, worker,
                receipt_path=str(start.path),
                digest_hash=start.digest_hash,
                receipt_hash=start.receipt_hash,
                digest_text=digest_text,
                managed_context=managed_context,
                synthesis_text=(
                    render_synthesis_prompt(pending_synthesis)
                    if pending_synthesis else ""
                ),
            )
            # Use the restart-safe global receipt id, not the process-local
            # round counter. Otherwise restarting a worker would overwrite
            # logs/round_1.log even though its receipts correctly continue.
            log_path = wl.logs / f"round_{start.round_id}.log"
            write_status(
                wl, state="running", round=rnd, round_started_at=time.time(),
                receipt_path=str(start.path), digest_hash=start.digest_hash,
                receipt_hash=start.receipt_hash, receipt_id=start.round_id,
            )
            try:
                launch = persist_launch_receipt(wl, start, prompt, role)
            except RoundReceiptError as err:
                return _fail_receipt(err, rnd)
            try:
                from euler.control.hooks import ReconcileError, before_managed_round
                before_managed_round(wl, start, launch)
            except ReconcileError as err:
                return _fail_reconcile(err, rnd)
            started = time.monotonic()
            rc = run_round(wl, role, prompt, log_path, hard_timeout)
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            try:
                result_payload = persist_result_receipt(
                    wl, launch, rc, elapsed_ms, log_path=log_path,
                )
            except RoundReceiptError as err:
                write_status(
                    wl, state="RECEIPT_FAILED", round=rnd,
                    last_round_at=time.time(), last_rc=rc,
                    last_fact_id=_parse_last_fact_id(log_path),
                    receipt=err.to_status_dict(),
                )
                print(f"RECEIPT_FAILED phase={err.phase} reason={err.reason}",
                      file=sys.stderr)
                return 1
            if rc == 127:                    # selected provider missing — do not spin
                try:
                    from euler.control.hooks import ReconcileError, after_managed_round
                    from euler.control.project import is_euler_project
                    if is_euler_project(wl.project_dir):
                        end_path = finalize_round_end(wl, start)
                        after_managed_round(
                            wl,
                            start,
                            launch,
                            result_payload,
                            end_path,
                            auto_final_audit=True,
                        )
                except ReconcileError as err:
                    return _fail_reconcile(err, rnd)
                except RoundReceiptError as err:
                    return _fail_receipt(err, rnd)
                selected = provider_from_role(role)
                error = (
                    "codex binary not found"
                    if selected is ProviderName.CODEX
                    else f"{selected.value} provider unavailable"
                )
                write_status(wl, state="error", error=error,
                             round=rnd)
                return 127
            try:
                end_path = finalize_round_end(wl, start)
            except RoundReceiptError as err:
                write_status(
                    wl, state="RECEIPT_FAILED", round=rnd,
                    last_round_at=time.time(), last_rc=rc,
                    last_fact_id=_parse_last_fact_id(log_path),
                    receipt=err.to_status_dict(),
                )
                print(f"RECEIPT_FAILED phase={err.phase} reason={err.reason}",
                      file=sys.stderr)
                return 1
            try:
                from euler.control.hooks import ReconcileError, after_managed_round
                reconciled = after_managed_round(
                    wl,
                    start,
                    launch,
                    result_payload,
                    end_path,
                    auto_final_audit=True,
                )
            except ReconcileError as err:
                return _fail_reconcile(err, rnd)
            try:
                end_payload = json.loads(end_path.read_text(encoding="utf-8"))
                evaluate_round_close(
                    wl, end_payload.get("appended_ids") or {},
                    run_id=run_id,
                    branch_id=task_ctx.branch_id,
                    subgoal_id=task_ctx.subgoal_id,
                )
            except MemoryContractError as err:
                write_status(
                    wl, state="MEMORY_CONTRACT_FAILED", round=rnd,
                    last_round_at=time.time(), last_rc=rc,
                    last_fact_id=_parse_last_fact_id(log_path),
                    memory_contract=err.to_status_dict(),
                    receipt_path=str(start.path), receipt_end=str(end_path),
                )
                print(f"MEMORY_CONTRACT_FAILED reason={err.reason}", file=sys.stderr)
                return 1
            if pending_synthesis:
                try:
                    syn = evaluate_synthesis_close(
                        wl, end_payload.get("appended_ids") or {},
                        pending_synthesis,
                        run_id=run_id,
                        branch_id=task_ctx.branch_id,
                        subgoal_id=task_ctx.subgoal_id,
                    )
                    resolve_request(
                        wl.project_dir, pending_synthesis["cluster_id"],
                        kind=syn.get("kind") or "",
                        entry_id=syn.get("entry_id"),
                        worker=wl.name,
                    )
                except FailureControlError as err:
                    return _fail_failure_control(err, rnd)
                except FailureSynthesisError as err:
                    write_status(
                        wl, state="FAILURE_SYNTHESIS_FAILED", round=rnd,
                        last_round_at=time.time(), last_rc=rc,
                        last_fact_id=_parse_last_fact_id(log_path),
                        failure_synthesis=err.to_status_dict(),
                        receipt_path=str(start.path), receipt_end=str(end_path),
                    )
                    print(f"FAILURE_SYNTHESIS_FAILED reason={err.reason}",
                          file=sys.stderr)
                    return 1
            try:
                new_requests = after_successful_close(
                    wl,
                    branch_id=task_ctx.branch_id or "",
                    subgoal_id=task_ctx.subgoal_id or "",
                )
            except FailureControlError as err:
                return _fail_failure_control(err, rnd)
            from euler.control.project import is_euler_project
            if reconciled is not None and is_euler_project(wl.project_dir):
                write_status(
                    wl, state="waiting_dispatch", round=rnd,
                    last_round_at=time.time(), last_rc=rc,
                    last_fact_id=_parse_last_fact_id(log_path),
                    receipt_path=str(start.path), receipt_end=str(end_path),
                    digest_hash=start.digest_hash, receipt_hash=start.receipt_hash,
                    receipt_id=start.round_id, run_id=run_id,
                    failure_clusters=[r.get("cluster_id") for r in new_requests],
                    reconcile={
                        "ok": True,
                        "outcome_ref": reconciled.outcome_ref,
                        "reconciliation_id": reconciled.reconciliation_id,
                    },
                )
                return 0

            write_status(
                wl, state="idle", round=rnd, last_round_at=time.time(),
                last_rc=rc, last_fact_id=_parse_last_fact_id(log_path),
                receipt_path=str(start.path), receipt_end=str(end_path),
                digest_hash=start.digest_hash, receipt_hash=start.receipt_hash,
                receipt_id=start.round_id, run_id=run_id,
                failure_clusters=[r.get("cluster_id") for r in new_requests],
            )

            consec_fail = consec_fail + 1 if rc not in (0, 124) else 0
            if max_fail and consec_fail >= max_fail:
                write_status(wl, state="error", error=f"{consec_fail} consecutive failed rounds")
                return 1

            if beat > 0:
                time.sleep(beat)
    finally:
        # Keep the worker discoverable until its final writes have settled.
        # Removing .pid first lets status/stop (and a restart) race the snapshot.
        try:
            from euler.workspace.milestones import checkpoint_if_enabled
            checkpoint_if_enabled(wl.project_dir, settled_worker_pid=os.getpid())
        finally:
            _cleanup_pid(wl)
    return 0
