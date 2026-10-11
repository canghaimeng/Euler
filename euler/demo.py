"""Offline integration demo of the live Euler agent loop.

This is deterministic plumbing mathematics, not a benchmark. It starts a real
verify HTTP process, scaffolds one Codex worker, lease-bound dispatches, and
launches ``python -m euler.execution``. The fake worker model calls the real
gateway ``fact_submit``; reconciliation and independent final audit run in the
managed hooks. No Fact Graph, verification JSONL, or acceptance objects are
hand-built here.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from euler.control.cli import do_init, do_propose, do_schedule
from euler.control.context import ContextLimits
from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import canonical_json, sha256_hex
from euler.control.live_records import (
    EVENT_ACCEPTANCE_RECORDED,
    EVENT_CHECKPOINT_CREATED,
    EVENT_DISPATCH_READY,
    EVENT_ROUND_RECONCILED,
)
from euler.control.policy import BudgetClass, BudgetPolicy, DimensionLimit, ResourceVector
from euler.control.project import open_project
from euler.control.recurrence import RecurrenceRecommendation
from euler.control.runtime import LeaseStatus
from euler.control.scheduler import (
    ActionKind,
    CandidateAction,
    DependencyValidity,
    PriorityClass,
    build_action_context_pair,
    slot_kind_for_action,
)
from euler.execution import layout as L
from euler.execution import scaffold
from euler.orchestration import cli as orch_cli

_PROJECT = "DEMO"
_WORKER = "codex"
_STATEMENT = "For every integer n, n + 0 equals n."
_PROOF = (
    "Zero is the additive identity of the integers, so adding zero to any integer n "
    "leaves the value unchanged. Hence n + 0 = n for every integer n, as required."
)
_TIMEOUT_S = 90.0

_FAKE_VERIFIER = r'''"""Temporary verifier backend for the offline demo. Not a mathematical judge."""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path

def main() -> int:
    log = os.environ.get("EULER_DEMO_VERIFY_LOG")
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": sys.argv[1:], "ts": time.time()}) + "\n")
    if len(sys.argv) < 2:
        sys.stderr.write("demo verifier: no prompt\n")
        return 2
    prompt = sys.argv[-1]
    match = re.search(r"this exact path:\s*(\S+)", prompt)
    if not match:
        sys.stderr.write("demo verifier: missing output path\n")
        return 3
    out = Path(match.group(1).rstrip("."))
    payload = {
        "verification_report": {
            "summary": "offline demo verifier accept (plumbing, not a benchmark)",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "correct",
        "repair_hints": "",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    sys.stdout.write(f"demo verifier wrote {out}\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''

_FAKE_WORKER = r'''"""Temporary Codex worker model for the offline demo."""
from __future__ import annotations
import os, re, sys
from pathlib import Path

def main() -> int:
    task = Path("TASK.md")
    if not task.is_file():
        sys.stderr.write("demo worker: TASK.md missing\n")
        return 2
    text = task.read_text(encoding="utf-8")
    match = re.search(r"^attempt_id:\s*([0-9a-f]{64})\s*$", text, re.M)
    if not match:
        sys.stderr.write("demo worker: TASK.md has no attempt_id\n")
        return 3
    attempt_id = match.group(1)
    statement = os.environ.get("EULER_DEMO_STATEMENT") or ""
    proof = os.environ.get("EULER_DEMO_PROOF") or ""
    if not statement or not proof:
        sys.stderr.write("demo worker: missing statement/proof env\n")
        return 4
    from euler.gateway import server
    added = server.gm_add(
        "proof_attempt",
        claim=statement,
        evidence=proof,
        metadata={
            "attempt_id": attempt_id,
            "approach": "offline demo identity proof",
            "result": "success",
            "reason": "additive identity of the integers",
            "salvage": "none",
            "inputs": [],
            "related_memory": [],
        },
        links={"attempt_id": attempt_id},
    )
    if not added.get("ok"):
        sys.stderr.write(f"demo worker gm_add failed: {added}\n")
        return 5
    submitted = server.fact_submit(statement, proof, attempt_id=attempt_id)
    fact_id = submitted.get("fact_id") or ""
    if not submitted.get("accepted") or len(str(fact_id)) != 16:
        sys.stderr.write(f"demo worker fact_submit failed: {submitted}\n")
        return 6
    sys.stdout.write(f"demo worker accepted fact {fact_id}\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


@dataclass
class DemoReport:
    """Result of one offline demo run. ``ok`` is false on any incomplete arrow."""

    ok: bool
    kind: str = "offline_integration_demo"
    note: str = (
        "Deterministic plumbing mathematics. Offline integration demo, not a benchmark."
    )
    project: str = _PROJECT
    worker: str = _WORKER
    elapsed_s: float = 0.0
    missing_optional: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ids: dict[str, Any] = field(default_factory=dict)
    events: dict[str, Any] = field(default_factory=dict)
    receipts: dict[str, Any] = field(default_factory=dict)
    verifier_invocations: int = 0
    workdir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "note": self.note,
            "project": self.project,
            "worker": self.worker,
            "elapsed_s": round(self.elapsed_s, 3),
            "missing_optional": list(self.missing_optional),
            "errors": list(self.errors),
            "ids": dict(self.ids),
            "events": dict(self.events),
            "receipts": dict(self.receipts),
            "verifier_invocations": self.verifier_invocations,
            "workdir": self.workdir,
        }

    def format_text(self) -> str:
        lines = [
            "== Euler offline integration demo (not a benchmark) ==",
            f"ok={self.ok}  elapsed_s={self.elapsed_s:.2f}",
            self.note,
        ]
        if self.missing_optional:
            lines.append("missing optional: " + ", ".join(self.missing_optional))
        for key, value in self.ids.items():
            lines.append(f"  {key}: {value}")
        for key, value in self.events.items():
            lines.append(f"  event {key}: {value}")
        lines.append(f"  verifier_invocations: {self.verifier_invocations}")
        for err in self.errors:
            lines.append(f"ERROR: {err}")
        return "\n".join(lines)


def package_root() -> Path:
    import euler

    return Path(euler.__file__).resolve().parent.parent


def child_pythonpath() -> str:
    roots = [str(package_root())]
    try:
        import site

        for item in site.getsitepackages():
            if item and item not in roots:
                roots.append(item)
    except Exception:
        pass
    existing = os.environ.get("PYTHONPATH") or ""
    if existing:
        roots.append(existing)
    return os.pathsep.join(roots)


def missing_optional_dependencies() -> list[str]:
    missing: list[str] = []
    for name in ("uvicorn", "fastapi"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    try:
        import mcp  # noqa: F401
    except ImportError:
        missing.append("mcp")
    return missing


def _free_loopback_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _unlimited_policy() -> BudgetPolicy:
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
        max_reasoning_slots=2,
        max_compute_slots=1,
        max_retrieval_slots=1,
        max_verifier_slots=2,
        final_audit_reserve_bp=1000,
        exploration_reserve_bp=0,
    )


def _contract() -> ProblemContract:
    scope = MathematicalScope.build(
        assumptions=("n is an integer",),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain="Z"),
        ),
        objects=(),
        parameter_domain="Z",
        definitions_hash=sha256_hex("demo-defs"),
    )
    return ProblemContract.build(
        problem_id="demo-identity",
        version=1,
        exact_statement=_STATEMENT,
        scope=scope,
        permitted_tools=("reasoning",),
        external_help_policy="none",
        target_kind=TargetKind.PROOF,
        required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
        completion_checks=("fact admission", "independent final audit"),
        budget_policy_ref="demo-policy",
    )


def _candidate(contract: ProblemContract, obligation_id: str) -> CandidateAction:
    spec, request, seed = build_action_context_pair(
        contract_id=contract.contract_id,
        obligation_id=obligation_id,
        concrete_gap="prove n + 0 = n",
        output_contract="return a KERNEL_CHECKED_PROOF",
        tool_environment_hash=sha256_hex("demo-tool"),
        acceptance_policy_hash=sha256_hex("demo-accept"),
        reference_versions_hash=sha256_hex("demo-refs"),
        limits=ContextLimits.unlimited(),
        action_type="prove",
        mechanism="identity",
        model_id="gpt-5.5",
        effort="high",
        task_mode="worker",
        parameters_json=canonical_json({"demo": True}),
        dependency_hash=sha256_hex("demo-dep"),
        definitions_hash=contract.scope.definitions_hash,
    )
    return CandidateAction(
        candidate_id="prove-identity",
        action_spec=spec,
        context_request=request,
        context_seed=seed,
        obligation_id=obligation_id,
        mechanism_family="identity",
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


def _wait_http(url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
                last = f"HTTP {resp.status}"
        except Exception as exc:
            last = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"verify HTTP did not become ready at {url}: {last}")


def _count_log(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def run_offline_demo(*, timeout_s: float = _TIMEOUT_S, retain_workdir: bool = False) -> DemoReport:
    """Run the live product path once. Cleans subprocesses and temp state."""
    started = time.monotonic()
    report = DemoReport(ok=False)
    missing = missing_optional_dependencies()
    report.missing_optional = missing
    if missing:
        report.errors.append("missing optional dependencies: " + ", ".join(missing))
        report.elapsed_s = time.monotonic() - started
        return report

    workdir = Path(tempfile.mkdtemp(prefix="euler-demo-"))
    report.workdir = str(workdir)
    verify_proc: subprocess.Popen | None = None
    verify_log_file = None
    old_env = dict(os.environ)
    try:
        # A local mock demo must not invoke deployment-owned process launchers.
        os.environ.pop("EULER_WORKER_LOOP_LAUNCHER", None)
        agents = workdir / "projects"
        runtime = workdir / "runtime"
        results = workdir / "verify-results"
        skills = workdir / "skills"
        skills.mkdir(parents=True)
        verifier_home = workdir / "verifier-home"
        (verifier_home / ".agents" / "skills").mkdir(parents=True)
        (verifier_home / "AGENTS.md").write_text(
            "# Offline mock verifier\nDeterministic protocol fixture; not a mathematical judge.\n",
            encoding="utf-8",
        )
        (workdir / "worker.md").write_text("# demo worker contract\n", encoding="utf-8")
        runtime.joinpath("run").mkdir(parents=True)
        results.mkdir()
        fake_verifier = workdir / "fake_verifier.py"
        fake_worker = workdir / "fake_worker.py"
        shebang = f"#!{sys.executable}\n"
        fake_verifier.write_text(shebang + _FAKE_VERIFIER, encoding="utf-8")
        fake_worker.write_text(shebang + _FAKE_WORKER, encoding="utf-8")
        fake_verifier.chmod(0o755)
        fake_worker.chmod(0o755)
        verify_log = workdir / "verifier-invocations.jsonl"
        pythonpath = child_pythonpath()
        port = _free_loopback_port()
        verify_url = f"http://127.0.0.1:{port}/verify"
        health_url = f"http://127.0.0.1:{port}/health"
        repo = package_root()

        verify_env = os.environ.copy()
        verify_env.update(
            {
                "PYTHONPATH": pythonpath,
                "EULER_ROOT": str(repo),
                "EULER_RUNTIME": str(runtime),
                "EULER_VERIFY_BACKEND": "codex",
                "EULER_CODEX_BIN": str(fake_verifier),
                "VERIFIER_RESULTS_DIR": str(results),
                "EULER_DEMO_VERIFY_LOG": str(verify_log),
                "EULER_VERIFY_TIMEOUT": "60",
                "VERIFY_AGENT_HOME": str(verifier_home),
            }
        )
        verify_log_file = (workdir / "uvicorn.log").open("w", encoding="utf-8")
        verify_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "euler.verify.service:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=verify_env,
            stdout=verify_log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        (runtime / "run" / "verify.pid").write_text(str(verify_proc.pid), encoding="utf-8")
        _wait_http(health_url, 15)

        os.environ.update(
            {
                "PYTHONPATH": pythonpath,
                "EULER_ROOT": str(repo),
                "EULER_RUNTIME": str(runtime),
                "EULER_AGENTS_ROOT": str(agents),
                "EULER_WORKER_CONTRACT": str(workdir / "worker.md"),
                "EULER_WORKER_SKILLS": str(skills),
                "EULER_VERIFY_URL": verify_url,
                "EULER_CODEX_BIN": str(fake_worker),
                "EULER_MAX_ROUNDS": "1",
                "EULER_DEMO_STATEMENT": _STATEMENT,
                "EULER_DEMO_PROOF": _PROOF,
                "EULER_DEMO_VERIFY_LOG": str(verify_log),
            }
        )
        scaffold.do_new(_PROJECT, roles="codex:1", profile="codex", model="gpt-5.5")
        contract = _contract()
        init = do_init(
            _PROJECT,
            contract=contract.to_dict(),
            policy=_unlimited_policy().to_dict(),
        )
        cand = _candidate(contract, init["target_obligation_id"])
        proposed = do_propose(f"{_PROJECT}/{_WORKER}", cand.to_dict())
        scheduled = do_schedule(_PROJECT, dispatch=True, launch=True)
        if not scheduled.get("dispatched"):
            report.errors.append("scheduler did not dispatch the candidate")
        pdir = L.project_dir(_PROJECT)
        wl = L.WorkerLayout(L.worker_dir(_PROJECT, _WORKER))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _demo_complete(pdir, verify_log):
                break
            time.sleep(0.25)
        try:
            orch_cli.do_stop(f"{_PROJECT}/{_WORKER}", force=True)
        except SystemExit:
            pass
        report.verifier_invocations = _count_log(verify_log)
        report.ids, report.events, report.receipts, inspect_errors = _inspect(
            pdir, wl, init, proposed, scheduled
        )
        report.errors.extend(inspect_errors)
        if report.verifier_invocations < 2:
            report.errors.append(
                f"expected >=2 verifier invocations, got {report.verifier_invocations}"
            )
        report.ok = not report.errors
        if not report.ok:
            loop_log = wl.logs / "loop.log"
            if loop_log.is_file():
                report.errors.append("loop.log tail: " + loop_log.read_text(encoding="utf-8")[-2000:])
    except Exception as exc:
        report.errors.append(f"{type(exc).__name__}: {exc}")
        report.ok = False
    finally:
        _kill(verify_proc)
        try:
            orch_cli.do_stop(f"{_PROJECT}/{_WORKER}", force=True)
        except Exception:
            pass
        if verify_log_file is not None:
            try:
                verify_log_file.close()
            except Exception:
                pass
        for key, value in old_env.items():
            os.environ[key] = value
        for key in list(os.environ):
            if key not in old_env:
                os.environ.pop(key, None)
        if not retain_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        report.elapsed_s = time.monotonic() - started
    return report


def _demo_complete(pdir: Path, verify_log: Path) -> bool:
    try:
        project = open_project(pdir)
    except Exception:
        return False
    try:
        types = [
            envelope.event_type
            for _seq, envelope in project.event_store.iter_project(project.project_id)
        ]
        return (
            EVENT_CHECKPOINT_CREATED in types
            and EVENT_ACCEPTANCE_RECORDED in types
            and _count_log(verify_log) >= 2
        )
    finally:
        project.close()


def _inspect(
    pdir: Path,
    wl: L.WorkerLayout,
    init: dict[str, Any],
    proposed: dict[str, Any],
    scheduled: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    errors: list[str] = []
    ids = {
        "contract_id": init.get("contract_id"),
        "obligation_id": init.get("target_obligation_id"),
        "candidate_id": proposed.get("candidate_id"),
        "proposal_id": proposed.get("proposal_id"),
    }
    dispatched = (scheduled.get("dispatched") or [None])[0] or {}
    ids["lease_id"] = dispatched.get("lease_id")
    ids["attempt_id"] = dispatched.get("attempt_id")
    ids["packet_hash"] = dispatched.get("packet_hash")
    ids["task_hash"] = dispatched.get("task_hash")
    receipts = {}
    for kind in ("start", "launch", "result", "end"):
        matches = sorted(wl.receipts.glob(f"round_*.{kind}.json")) if wl.receipts.is_dir() else []
        receipts[kind] = str(matches[-1]) if matches else ""
        if not matches:
            errors.append(f"missing {kind} receipt")
    sidecar = pdir / "control" / "dispatch" / f"{_WORKER}.json"
    task = wl.task
    if sidecar.is_file() and task.is_file() and dispatched:
        side = json.loads(sidecar.read_text(encoding="utf-8"))
        task_text = task.read_text(encoding="utf-8")
        if dispatched.get("packet_hash") and dispatched["packet_hash"] not in task_text:
            errors.append("TASK.md missing packet_hash")
        if side.get("packet_artifact_hash") != dispatched.get("packet_hash"):
            errors.append("sidecar packet hash mismatch")
        if side.get("state") != "settled":
            errors.append("completed managed action sidecar is not settled")
        if (side.get("binding") or {}).get("ready_id") != dispatched.get("ready_id"):
            errors.append("settled sidecar lost the original ready binding")
    else:
        errors.append("missing TASK.md or dispatch sidecar")
    facts = list((pdir / "fact_graph" / "facts").glob("*.md")) if (pdir / "fact_graph" / "facts").is_dir() else []
    if len(facts) != 1:
        errors.append(f"expected one active Fact, found {len(facts)}")
    else:
        ids["fact_id"] = facts[0].stem
        if len(facts[0].stem) != 16:
            errors.append("fact_id is not 16-hex")
    ver_path = pdir / "global_memory" / "verification.jsonl"
    if not ver_path.is_file():
        errors.append("missing verification.jsonl from fact_submit")
    else:
        rows = [
            json.loads(line)
            for line in ver_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not any(
            row.get("provenance") == "fact_submit"
            and row.get("verdict") == "correct"
            and row.get("fact_id")
            for row in rows
        ):
            errors.append("no accepted fact_submit verification row")
    events: dict[str, Any] = {}
    try:
        project = open_project(pdir)
    except Exception as exc:
        errors.append(f"cannot open project: {exc}")
        return ids, events, receipts, errors
    try:
        types = [
            envelope.event_type
            for _seq, envelope in project.event_store.iter_project(project.project_id)
        ]
        for name in (
            EVENT_DISPATCH_READY,
            EVENT_ROUND_RECONCILED,
            EVENT_ACCEPTANCE_RECORDED,
            EVENT_CHECKPOINT_CREATED,
            "evidence.registered",
            "obligation.status_changed",
        ):
            events[name] = types.count(name)
        if events.get(EVENT_DISPATCH_READY, 0) < 1:
            errors.append("missing euler.dispatch_ready")
        if events.get(EVENT_ROUND_RECONCILED, 0) != 1:
            errors.append("expected exactly one euler.round_reconciled")
        if events.get("evidence.registered", 0) < 1:
            errors.append("missing evidence.registered")
        if events.get("obligation.status_changed", 0) < 1:
            errors.append("missing obligation.status_changed")
        if events.get(EVENT_ACCEPTANCE_RECORDED, 0) < 1:
            errors.append("missing euler.acceptance_recorded")
        if events.get(EVENT_CHECKPOINT_CREATED, 0) < 1:
            errors.append("missing euler.checkpoint_created")
        for _seq, envelope in project.event_store.iter_project(project.project_id):
            if envelope.event_type == EVENT_DISPATCH_READY:
                binding = dict(envelope.payload).get("binding") or {}
                if binding.get("packet_hash") != dispatched.get("packet_hash"):
                    errors.append("ready event packet_hash mismatch")
            if envelope.event_type == EVENT_ACCEPTANCE_RECORDED:
                acc = dict(envelope.payload).get("acceptance") or {}
                ids["acceptance_id"] = acc.get("record_id")
                ids["outcome"] = acc.get("project_outcome")
                if acc.get("verdict") != "PASS" or acc.get("project_outcome") != "SOLVED":
                    errors.append(f"acceptance is {acc.get('verdict')}/{acc.get('project_outcome')}")
            if envelope.event_type == EVENT_CHECKPOINT_CREATED:
                ck = dict(envelope.payload).get("checkpoint") or {}
                ids["checkpoint_id"] = ck.get("report_id")
                if ck.get("outcome") != "SOLVED":
                    errors.append(f"checkpoint outcome {ck.get('outcome')}")
            if envelope.event_type == "obligation.status_changed":
                if dict(envelope.payload).get("status") != "DISCHARGED":
                    errors.append("obligation status is not DISCHARGED")
        snap = project.runtime_snapshot()
        worker_leases = [
            item for item in snap.leases if item.lease_id == dispatched.get("lease_id")
        ]
        if not worker_leases or worker_leases[0].status is not LeaseStatus.COMPLETED:
            errors.append("worker lease is not COMPLETED")
        elif worker_leases[0].running_at is None:
            errors.append("worker lease never entered RUNNING")
        for _seq, envelope in project.event_store.iter_project(project.project_id):
            if envelope.event_type == EVENT_ROUND_RECONCILED:
                rec = dict(envelope.payload).get("reconciliation") or {}
                if rec.get("outcome_ref") == "OBLIGATION_DISCHARGED" and not rec.get("progress_id"):
                    errors.append("round_reconciled missing ProgressReceipt id")
        completed = [item for item in snap.leases if item.status is LeaseStatus.COMPLETED]
        if len(completed) < 2:
            errors.append("expected worker lease and final-audit lease COMPLETED")
        else:
            ids["final_audit_lease_id"] = next(
                (
                    item.lease_id
                    for item in completed
                    if item.lease_id != dispatched.get("lease_id")
                ),
                None,
            )
    finally:
        project.close()
    return ids, events, receipts, errors
