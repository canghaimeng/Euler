# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Read-only projections of public project stores for the browser dashboard.

Never instantiates the execution/control stores. SQLite is copied, including its
WAL, into an external temporary directory before opening the copy: even SQLite's
``mode=ro`` can create WAL sidecars beside an otherwise read-only source file.
Worker scratch memory, provider sessions and logs are outside this reader.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import hashlib
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_EVENTS = 50000
MAX_FACTS = 20000
ACTIVITY_LIMIT = 80
MONITOR_STALE_SECONDS = 900
QUOTA_STALE_SECONDS = 45
QUOTA_THRESHOLD_PERCENT = 3.0
_RESEARCH_EVENTS = {
    "problem.contract.created", "evidence.registered", "evidence.invalidated",
    "obligation.created", "obligation.status_changed", "obligation.reopened",
    "route.created", "route.status_changed", "relation.created", "failure.recorded",
}
_CHANNELS = ("conclusion", "example", "counterexample", "proof_attempt", "plan",
             "direction", "obstacle", "dead_end", "verification", "elaboration", "master_guidance")
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_BUILD_LOCKS: dict[str, Any] = {}


def _text(value: Any, limit: int = 500) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _epoch(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc).timestamp() if dt.tzinfo is None else dt.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def _iso(value: Any) -> str | None:
    epoch = _epoch(value)
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _contained(root: Path, path: Path) -> bool:
    try:
        root = root.resolve()
        relative = path.absolute().relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            # Public store names cannot be redirected to worker scratch data,
            # even if the symlink target is inside the same project directory.
            if current.is_symlink():
                return False
        path.resolve().relative_to(root)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _read_text(path: Path, *, root: Path | None = None, limit: int = MAX_FILE_BYTES) -> str:
    if root is not None and not _contained(root, path):
        raise ValueError("source escapes configured project")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("source exceeds dashboard read limit")
    return data.decode("utf-8")


def _json(path: Path, warnings: list[str], *, root: Path | None = None,
          label: str = "metadata", optional: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(_read_text(path, root=root))
        if not isinstance(value, dict):
            raise ValueError("expected JSON object")
        return value
    except FileNotFoundError:
        if not optional:
            warnings.append(f"{label}: source is missing.")
    except (OSError, ValueError, UnicodeError):
        warnings.append(f"{label}: source is unreadable or malformed.")
    return {}


def _tail_jsonl(path: Path, root: Path, warnings: list[str], limit: int = 200) -> list[dict]:
    """Read only a bounded tail; tolerate an appender's incomplete final line."""
    if not _contained(root, path):
        warnings.append("Shared activity source escapes configured project.")
        return []
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            offset = max(0, size - MAX_FILE_BYTES)
            stream.seek(offset)
            if offset:
                stream.readline()
            lines = stream.read(MAX_FILE_BYTES).decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        warnings.append(f"Shared activity {path.stem}: source unavailable.")
        return []
    rows = []
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            continue
    return rows


from contextlib import contextmanager
from contextvars import ContextVar

_ENV_CONFIG = object()
_REQUEST_CONFIG = ContextVar("dashboard_request_config", default=_ENV_CONFIG)


def _config_value():
    value = _REQUEST_CONFIG.get()
    return os.environ.get("EULER_DASHBOARD_CONFIG") if value is _ENV_CONFIG else value


@contextmanager
def dashboard_config(path):
    """Request-local config, including explicit no-config; safe across projects/threads."""
    token = _REQUEST_CONFIG.set(str(path) if path else None)
    try:
        yield
    finally:
        _REQUEST_CONFIG.reset(token)


def _config(warnings: list[str]) -> tuple[dict, Path | None]:
    value = _config_value()
    if not value:
        return {}, None
    path = Path(value).expanduser().resolve()
    return _json(path, warnings, label="Dashboard configuration", optional=False), path.parent


def _external_file(config: dict, directory: Path | None, key: str) -> Path | None:
    raw = config.get(key)
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (directory or Path.cwd()) / path


def _depths(nodes: dict[str, dict], edges: list[dict]) -> tuple[dict[str, int], bool]:
    """Iterative topological layers; cycles remain visible without recursion."""
    incoming = {key: 0 for key in nodes}
    children: dict[str, list[str]] = {key: [] for key in nodes}
    depth = dict.fromkeys(nodes, 0)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        if source in nodes and target in nodes:
            incoming[target] += 1
            children[source].append(target)
    ready = deque(key for key, count in incoming.items() if not count)
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in children[source]:
            depth[target] = max(depth[target], depth[source] + 1)
            incoming[target] -= 1
            if not incoming[target]:
                ready.append(target)
    return depth, visited != len(nodes)


def _facts(project: Path, warnings: list[str]) -> tuple[dict[str, dict], dict]:
    from .app import _parse_fact

    facts: dict[str, dict] = {}
    nodes: dict[str, dict] = {}
    for dirname, status in (("facts", "accepted"), ("_revoked", "revoked")):
        directory = project / "fact_graph" / dirname
        if not _contained(project, directory):
            warnings.append("Fact source escapes configured project.")
            continue
        try:
            paths = sorted(directory.glob("*.md"))
        except OSError:
            warnings.append("Fact store is unavailable.")
            continue
        if len(paths) > MAX_FACTS:
            warnings.append(f"Fact view limited to {MAX_FACTS} files per store.")
        for path in paths[:MAX_FACTS]:
            try:
                raw = _read_text(path, root=project)
                fact = _parse_fact(raw)
                fact["source_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
                fid = _text(fact.get("fact_id"), 160) or path.stem
                predecessors = fact.get("predecessors")
                fact["predecessors"] = list(dict.fromkeys(
                    x for x in predecessors if isinstance(x, str) and x
                )) if isinstance(predecessors, list) else []
                statement = _text(fact.get("statement"), MAX_FILE_BYTES)
                fact.update(id=fid, fact_id=fid, status=status, author=_text(fact.get("author")),
                            created_at=_iso(path.stat().st_mtime))
                facts[fid] = fact
                nodes[fid] = {"id": fid, "label": re.sub(r"\s+", " ", statement)[:110] or fid,
                              "statement": statement[:1600], "status": status,
                              "author": _text(fact.get("author")), "team_id": None,
                              "depth": 0, "created_at": fact["created_at"],
                              "predecessors": fact["predecessors"], "kind": "fact"}
            except (OSError, ValueError, UnicodeError):
                warnings.append(f"Fact {path.stem[:80]}: source unavailable or malformed.")
    edges = []
    for node in list(nodes.values()):
        for pred in node["predecessors"]:
            if pred not in nodes:
                nodes[pred] = {"id": pred, "label": f"Missing predecessor · {pred[:12]}",
                               "statement": "This referenced predecessor is absent from the fact store.",
                               "status": "missing", "author": "", "team_id": None,
                               "depth": 0, "created_at": None, "predecessors": [], "kind": "fact"}
            edges.append({"source": pred, "target": node["id"], "type": "requires"})
    missing = sum(n["status"] == "missing" for n in nodes.values())
    if missing:
        warnings.append(f"{missing} referenced fact predecessor(s) are missing; placeholder nodes are shown.")
    invalid = {key for key, node in nodes.items() if node["status"] in {"revoked", "missing"}}
    # An active descendant of a missing/revoked predecessor must not look accepted.
    changed = True
    while changed:
        changed = False
        for edge in edges:
            if edge["source"] in invalid and edge["target"] not in invalid:
                invalid.add(edge["target"])
                nodes[edge["target"]]["status"] = "dependency_unavailable"
                if edge["target"] in facts:
                    facts[edge["target"]]["status"] = "dependency_unavailable"
                changed = True
    depth, cycle = _depths(nodes, edges)
    if cycle:
        warnings.append("Fact dependencies contain a cycle; the displayed layers are provisional.")
    for key, node in nodes.items():
        node["depth"] = depth[key]
    return facts, {"nodes": list(nodes.values()), "edges": _edge_ids(edges),
                   "max_depth": max(depth.values(), default=0),
                   "available": bool(nodes), "warnings": list(warnings)}


def _edge_ids(edges: list[dict]) -> list[dict]:
    """Give each edge an identity that survives refreshes and reordering.

    A recorded relation keeps its own ``relation_id``. Other edges are identified by
    their content; exact duplicates are told apart by recorded occurrence order.
    """
    seen: dict[str, int] = defaultdict(int)
    for edge in edges:
        if edge.get("relation_id"):
            edge["id"] = "rel:" + str(edge["relation_id"])
            continue
        content = "\x1f".join((edge["source"], edge["type"], edge["target"], edge.get("exact_claim", "")))
        key = "e:" + hashlib.sha256(content.encode()).hexdigest()[:16]
        seen[key] += 1
        edge["id"] = key if seen[key] == 1 else f"{key}~{seen[key]}"
    return edges


def _signature(path: Path) -> tuple | None:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def _events(project: Path, warnings: list[str]) -> list[dict] | None:
    source = project / "control" / "control.sqlite3"
    if not source.is_file():
        return None
    wal = source.with_name(source.name + "-wal")
    if not all(_contained(project, item) for item in (source, wal)):
        warnings.append("Control store escapes configured project.")
        return None
    try:
        for attempt in range(3):
            before = (_signature(source), _signature(wal))
            if any(sig and sig[1] > MAX_DATABASE_BYTES for sig in before):
                raise ValueError("control snapshot exceeds read limit")
            with tempfile.TemporaryDirectory(prefix="euler-dashboard-") as temporary:
                target = Path(temporary) / "control.sqlite3"
                shutil.copyfile(source, target)
                if before[1]:
                    shutil.copyfile(wal, target.with_name(target.name + "-wal"))
                if before != (_signature(source), _signature(wal)):
                    continue
                with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA query_only = ON")
                    rows = connection.execute(
                        "SELECT seq, event_id, schema_version, project_id, timestamp_utc, actor, "
                        "event_type, idempotency_key, payload, payload_hash, causation_id, correlation_id "
                        "FROM events ORDER BY seq LIMIT ?", (MAX_EVENTS + 1,)
                    ).fetchall()
                if len(rows) > MAX_EVENTS:
                    raise ValueError("control event stream exceeds read limit")
                result = []
                project_ids = set()
                for row in rows:
                    event = dict(row)
                    project_ids.add(event["project_id"])
                    event["payload"] = json.loads(event["payload"])
                    if not isinstance(event["payload"], dict):
                        raise ValueError("event payload is not an object")
                    result.append(event)
                if len(project_ids) > 1:
                    raise ValueError("multiple project identities in one control store")
                return result
        warnings.append("Control store changed during snapshot; retry on the next refresh.")
    except (OSError, ValueError, sqlite3.Error):
        warnings.append("Control events are unavailable or malformed; no mathematical status inferred.")
    return None


def _research(project: Path, events: list[dict] | None, warnings: list[str]) -> tuple[dict, dict]:
    graph = {"nodes": [], "edges": [], "max_depth": 0, "available": False, "warnings": []}
    control = {"available": False, "target_status": "UNKNOWN", "obligations_count": 0,
               "outcome": None}
    if events is None:
        graph["warnings"] = ["No readable research control event stream is available."]
        return graph, control
    # These are pure schema/projection classes; no runtime store is opened.
    from euler.control.acceptance import AcceptanceRecord
    from euler.control.schema import EventEnvelope
    from euler.control.views import ResearchView

    view = ResearchView()
    created: dict[str, str] = {}
    changed: dict[str, str] = {}
    accepted: list[dict] = []
    try:
        for row in events:
            envelope = EventEnvelope.from_dict({key: value for key, value in row.items() if key != "seq"})
            if envelope.event_type in _RESEARCH_EVENTS:
                view.apply(row["seq"], envelope)
                for key in ("obligation", "route", "evidence"):
                    value = row["payload"].get(key)
                    if isinstance(value, dict):
                        identifier = value.get(f"{key}_id") or value.get("artifact_id")
                        if identifier:
                            created.setdefault(identifier, row["timestamp_utc"])
                # Recorded time of the latest status transition (not of heartbeats or receipts).
                if envelope.event_type in {"obligation.status_changed", "obligation.reopened", "route.status_changed"}:
                    identifier = row["payload"].get("obligation_id") or row["payload"].get("route_id")
                    if isinstance(identifier, str):
                        changed[identifier] = row["timestamp_utc"]
            elif envelope.event_type == "euler.acceptance_recorded":
                acceptance = row["payload"].get("acceptance")
                # PASS is meaningful only on a validated acceptance record,
                # including its mandatory final-audit receipt and content ID.
                accepted.append(AcceptanceRecord.from_dict(acceptance).to_dict())
            elif not envelope.event_type.startswith(("euler.", "runtime.", "budget.")):
                raise ValueError("unknown research event")
    except Exception as exc:
        # An invalid replay must never leave a partially replayed claim looking final.
        warning = f"Research event replay failed ({type(exc).__name__}); graph and target state are unavailable."
        warnings.append(warning)
        graph["warnings"] = [warning]
        return graph, control
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(identifier: str, statement: str, status: str, kind: str, author: str = "",
                 validity: str = "VALID") -> None:
        nodes[identifier] = {"id": identifier, "label": re.sub(r"\s+", " ", statement)[:110],
                             "statement": statement, "status": status, "kind": kind,
                             "author": author, "team_id": None, "depth": 0,
                             "created_at": created.get(identifier), "predecessors": [],
                             "validity": validity, "status_changed_at": _iso(changed.get(identifier))}

    def add_edge(source: str, target: str, kind: str, validity: str = "VALID",
                 exact_claim: str = "") -> None:
        edge = {"source": source, "target": target, "type": kind, "validity": validity}
        if exact_claim:
            edge["exact_claim"] = exact_claim
        edges.append(edge)

    for obligation in view.obligations():
        add_node(obligation.obligation_id, obligation.exact_statement, obligation.status.value,
                 "obligation", obligation.created_by, obligation.validity.value)
        for evidence in obligation.evidence_refs:
            add_edge(evidence, obligation.obligation_id, "evidence")
    for route in view.routes():
        add_node(route.route_id, route.mechanism, route.status.value, "route", validity=route.validity.value)
        for requirement in route.required_obligation_ids:
            add_edge(requirement, route.route_id, "requires")
        for dependency in route.dependencies:
            add_edge(dependency, route.route_id, "depends_on")
        for evidence in route.evidence_refs:
            add_edge(evidence, route.route_id, "evidence")
        add_edge(route.route_id, route.target_obligation_id, "targets")
    for evidence in view.evidence():
        validity = view.evidence_validity(evidence.artifact_id)
        add_node(evidence.artifact_id, evidence.exact_claim, validity.value if validity else "UNKNOWN",
                 "evidence", evidence.producer, validity.value if validity else "UNKNOWN")
        nodes[evidence.artifact_id]["evidence_kind"] = evidence.evidence_kind.value
        nodes[evidence.artifact_id]["checker_result"] = evidence.checker_result.value if evidence.checker_result else None
    for relation in view.relations():
        # Stored A REQUIRES B means B is A's prerequisite. Display all such
        # edges prerequisite -> consumer, matching Fact and route edges.
        reverse = relation.relation_type.value in {"REQUIRES", "BLOCKED_BY"}
        add_edge(relation.target if reverse else relation.source,
                 relation.source if reverse else relation.target, relation.relation_type.value,
                 relation.validity.value, relation.exact_claim)
        edges[-1].update(recorded_source=relation.source, recorded_target=relation.target,
                         relation_id=relation.relation_id)
    dangling = [edge for edge in edges if edge["source"] not in nodes or edge["target"] not in nodes]
    if dangling:
        graph["warnings"].append(f"{len(dangling)} research relationship(s) reference absent nodes.")
    edges = _edge_ids([edge for edge in edges if edge not in dangling])
    for edge in edges:
        nodes[edge["target"]]["predecessors"].append(edge["source"])
    depth, cycle = _depths(nodes, edges)
    if cycle:
        graph["warnings"].append("Research relationships contain a cycle; this layer is not a fact DAG.")
    for identifier, node in nodes.items():
        node["depth"] = depth[identifier]
    manifest = _json(project / "control" / "manifest.json", warnings, root=project, label="Control manifest")
    target_id = manifest.get("target_obligation_id")
    target = view.get_obligation(target_id) if isinstance(target_id, str) else None
    if target is None and len(view.obligations()) == 1:
        target = view.obligations()[0]
    control.update(available=True, obligations_count=len(view.obligations()),
                   target_status=target.status.value if target else "UNKNOWN")
    if target:
        control["target_id"] = target.obligation_id
        control["goal"] = target.exact_statement
        for record in reversed(accepted):
            if record.get("obligation_id") != target.obligation_id:
                continue
            if (record.get("verdict") == "PASS" and record.get("contract_id") == target.contract_id
                    and (record.get("project_outcome"), target.status.value) in {
                        ("SOLVED", "DISCHARGED"), ("REFUTED", "REFUTED")}
                    and target.validity.value == "VALID"
                    ):
                control["outcome"] = record["project_outcome"]
            # A newer failed or mismatched acceptance supersedes an older PASS.
            break
    graph.update(nodes=list(nodes.values()), edges=edges, available=bool(nodes), target_id=control.get("target_id"),
                 max_depth=max(depth.values(), default=0))
    return graph, control


def _pid_alive(worker: Path, pid: Any, started_at: Any) -> bool | None:
    """Validate PID identity against the execution argv and process start time.

    A PID that exists alone is insufficient: stopped runs leave old PID values.
    Platforms without procfs report unknown instead of fabricating liveness.
    """
    if os.environ.get("EULER_DASHBOARD_PID_MODE") == "metadata":
        return None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    proc = Path("/proc") / str(pid)
    if not Path("/proc").is_dir():
        return None
    try:
        argv = (proc / "cmdline").read_bytes().split(b"\0")
        args = [value.decode("utf-8", errors="replace") for value in argv if value]
        if not any(args[index:index + 2] == ["-m", "euler.execution"] for index in range(len(args) - 1)):
            return False
        if not any(Path(arg).is_absolute() and Path(arg).resolve() == worker.resolve() for arg in args):
            return False
        # Linux field 22 is seconds after boot in clock ticks. Check against status epoch.
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = int(stat[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        process_started = time.time() - uptime + start_ticks / os.sysconf("SC_CLK_TCK")
        recorded = _epoch(started_at)
        return recorded is not None and abs(process_started - recorded) < 30
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        return None


def _workers(project: Path, meta: dict, facts: dict, warnings: list[str]) -> list[dict]:
    configs = {item["name"]: item for item in meta.get("worker_configs", [])
               if isinstance(item, dict) and isinstance(item.get("name"), str)} if isinstance(meta.get("worker_configs"), list) else {}
    directory = project / "workers"
    if not directory.is_dir() or not _contained(project, directory):
        return []
    accepted = Counter(fact.get("author") for fact in facts.values() if fact.get("status") == "accepted")
    result = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir() or not _contained(project, path):
            continue
        if isinstance(meta.get("workers"), list) and path.name not in meta["workers"]:
            continue
        status = _json(path / ".status.json", warnings, root=project, label=f"Worker {path.name}")
        role = {}
        try:
            for line in _read_text(path / ".role", root=project, limit=65536).splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"PROVIDER", "MODEL", "REASONING_EFFORT", "ROLE"}:
                    role[key] = value.strip().strip("\"'")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError):
            warnings.append(f"Worker {path.name}: role metadata unavailable.")
        config = configs.get(path.name, {})
        pid = status.get("pid")
        alive = _pid_alive(path, pid, status.get("started_at"))
        state = _text(status.get("state")) or "unknown"
        if state in {"running", "idle"} and alive is False:
            state = "offline"
        task = ""
        try:
            content = _read_text(path / "TASK.md", root=project, limit=256 * 1024)
            content = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)
            lines = content.splitlines()
            headings = [re.sub(r"^#{1,6}\s+", "", line.strip()) for line in lines if re.match(r"^#{1,6}\s+", line.strip())]
            task = next((heading for heading in headings if heading.lower() not in {
                "euler dispatch", "contextpacket", "context packet", "task context", "control", "task",
            }), "")[:300]
            output = re.search(r"(?m)^output_contract:[ \t]*([^\n]+)", content)
            if output:
                task = "目标：" + output.group(1).strip()[:300]
            if not task and not headings:
                task = next((line.strip() for line in lines if line.strip()
                             and not line.startswith(("<!--", "---", "===", "{", "["))
                             and not re.match(r"^[a-z_]+:", line)), "")[:300]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError):
            warnings.append(f"Worker {path.name}: task summary unavailable.")
        round_value = status.get("receipt_id", status.get("round", 0))
        result.append({"id": path.name, "name": path.name,
                       "role": role.get("ROLE") or _text(config.get("role")),
                       "provider": role.get("PROVIDER") or _text(config.get("provider")) or "unknown",
                       "model": role.get("MODEL") or _text(config.get("model")) or "unknown",
                       "effort": role.get("REASONING_EFFORT") or _text(config.get("effort")),
                       "status": state, "alive": alive,
                       "liveness_source": "metadata" if alive is None else "local_process",
                       "round": int(round_value) if _number(round_value) is not None else 0,
                       "last_activity_at": _iso(status.get("updated_at")),
                       "task_summary": task, "accepted_facts": accepted[path.name]})
    return result


def _team_status(workers: list[dict]) -> str:
    if any(worker["alive"] for worker in workers):
        return "running"
    if any(worker["alive"] is None and worker["status"] in {"running", "idle"} for worker in workers):
        return "unverified"
    if any(_blocked(worker["status"]) for worker in workers):
        return "blocked"
    if workers and all(worker["status"] in {"stopped", "terminated", "deadline", "max_rounds"} for worker in workers):
        return "stopped"
    return "idle" if workers else "unknown"


def _blocked(status: str) -> bool:
    return status in {"error", "offline", "blocked"} or status.endswith("_FAILED")


def _teams(workers: list[dict], config: dict, meta: dict, monitor: dict,
           warnings: list[str]) -> list[dict]:
    by_id = {worker["id"]: worker for worker in workers}
    teams, assigned, team_ids = [], set(), set()
    configured = config.get("teams")
    if not isinstance(configured, list) or not configured:
        configured = [{"id": "default", "name": "Research team", "workers": list(by_id),
                       "coordinator": {"name": _text(meta.get("coordinator")) or "Coordinator"}}]
    for index, item in enumerate(configured):
        if not isinstance(item, dict):
            warnings.append("Invalid dashboard team configuration skipped.")
            continue
        team_id = _text(item.get("id")) or f"team-{index + 1}"
        if team_id in team_ids:
            warnings.append(f"Duplicate team id {team_id}: later entry skipped.")
            continue
        team_ids.add(team_id)
        names = item.get("workers", [])
        members = []
        if not isinstance(names, list):
            names = []
            warnings.append(f"Team {team_id}: workers must be a list.")
        for name in names:
            if not isinstance(name, str) or name not in by_id:
                warnings.append(f"Team {team_id}: configured worker is unavailable.")
            elif name in assigned:
                warnings.append(f"Worker {name}: duplicate team membership skipped.")
            else:
                members.append(by_id[name])
                assigned.add(name)
        team = {"id": team_id, "name": _text(item.get("name")) or team_id,
                "description": _text(item.get("description")), "status": _team_status(members),
                "workers": members}
        coordinator = item.get("coordinator")
        if isinstance(coordinator, dict):
            team["coordinator"] = {"name": _text(coordinator.get("name")) or "Coordinator",
                                   "model": _text(coordinator.get("model")),
                                   "effort": _text(coordinator.get("effort")),
                                   "role": _text(coordinator.get("role")) or "Research lead",
                                   "status": (_text(monitor.get("coordinator_ui"))
                                              if len(configured) == 1 or coordinator.get("name") == meta.get("coordinator")
                                              else "") or "unknown"}
            if len(configured) == 1 or coordinator.get("name") == meta.get("coordinator"):
                team["coordinator"]["goal_status"] = _text(monitor.get("goal_status")) or "unknown"
                team["coordinator"]["checked_at"] = _iso(monitor.get("checked_at_utc"))
                team["coordinator"]["task_summary"] = (_text(monitor.get("coordinator_task"), 700)
                                                       or _text(coordinator.get("task_summary"), 700))
        teams.append(team)
    remaining = [worker for worker in workers if worker["id"] not in assigned]
    if remaining:
        identifier = "unassigned"
        while identifier in team_ids:
            identifier += "-workers"
        teams.append({"id": identifier, "name": "Unassigned workers", "description": "",
                      "status": _team_status(remaining), "workers": remaining})
    return teams


def _percent(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _quota(config: dict, directory: Path | None, warnings: list[str]) -> dict:
    """Expose only public quota percentages; unknown or stale data never permits.

    This is a read-only view of the external guard's decision, not a guard or a
    provider client. Account identifiers, credentials, balances and arbitrary
    provider error strings are deliberately absent from its allowlisted shape.
    """
    path = _external_file(config, directory, "quota_file")
    if path is None:
        return {"available": False}
    record = _json(path, warnings, label="Quota monitor", optional=False)
    now = time.time()
    checked = _epoch(record.get("checked_at"))
    max_age = _number(config.get("quota_max_age_seconds"))
    max_age = max_age if max_age is not None and 0 < max_age <= 3600 else QUOTA_STALE_SECONDS
    threshold = _percent(record.get("threshold_percent"))
    threshold = max(QUOTA_THRESHOLD_PERCENT, threshold) if threshold is not None else QUOTA_THRESHOLD_PERCENT
    providers = {}
    raw_providers = record.get("providers")
    raw_providers = raw_providers if isinstance(raw_providers, dict) else {}
    for name in ("codex", "cursor"):
        provider = raw_providers.get(name)
        provider = provider if isinstance(provider, dict) else {}
        windows = []
        raw_windows = provider.get("windows")
        if isinstance(raw_windows, dict):
            raw_windows = [dict(value, name=key) for key, value in raw_windows.items() if isinstance(value, dict)]
        for index, window in enumerate(raw_windows[:8] if isinstance(raw_windows, list) else []):
            if not isinstance(window, dict):
                continue
            # Labels are generated, rather than echoing arbitrary account keys.
            minutes = _number(window.get("window_minutes", window.get("limit_window_minutes")))
            windows.append({"label": f"Window {index + 1}",
                            "remaining_percent": _percent(window.get("remaining_percent")),
                            "used_percent": _percent(window.get("used_percent")),
                            "window_minutes": minutes if minutes is not None and 0 < minutes <= 525600 else None,
                            "resets_at": _iso(window.get("resets_at", window.get("reset_at")))})
        remaining = _percent(provider.get("remaining_percent"))
        values = [window["remaining_percent"] for window in windows if window["remaining_percent"] is not None]
        if remaining is not None and values:
            remaining = min(remaining, *values)
        providers[name] = {"remaining_percent": remaining, "windows": windows}
        if name == "cursor":
            value = provider.get("on_demand_enabled")
            providers[name]["on_demand_enabled"] = value if isinstance(value, bool) else None
    remaining = _percent(record.get("remaining_percent"))
    selective = record.get("cursor_pause_scope") == "cursor_roles"
    amounts = [providers["codex"]["remaining_percent"]] if selective else [provider["remaining_percent"] for provider in providers.values()]
    if remaining is not None and all(value is not None for value in amounts):
        remaining = min(remaining, *amounts)
    else:
        remaining = None
    latched = record.get("latched") is True
    reason_code = "allowed"
    reason = "额度充足，高于暂停阈值。"
    status = "healthy"
    if checked is None or checked <= 0 or checked > now + 30:
        status, reason_code, reason = "unknown", "invalid_timestamp", "额度检查时间未确认，保持保护性暂停。"
    elif now - checked > max_age:
        status, reason_code, reason = "stale", "stale", "额度记录已过期，保持保护性暂停。"
    elif remaining is None:
        status, reason_code, reason = "unknown", "unknown_quota", "额度尚未确认，保持保护性暂停。"
    elif remaining <= threshold:
        status, reason_code, reason = "paused", "below_threshold", "剩余额度已达到保护阈值，禁止发起新的模型调用。"
    elif not selective and providers["cursor"]["on_demand_enabled"] is not False:
        if providers["cursor"]["on_demand_enabled"] is True:
            status, reason_code, reason = "paused", "on_demand_enabled", "Cursor 按量使用已开启，额度保护器保持暂停。"
        else:
            status, reason_code, reason = "unknown", "unknown_on_demand", "Cursor 按量使用状态未确认，保持保护性暂停。"
    elif latched:
        status, reason_code, reason = "paused", "latched", "额度保护暂停已锁定，额度恢复后仍保持暂停。"
    elif record.get("permit") is not True:
        status, reason_code, reason = "paused", "guard_denied", "额度保护器未允许执行，保持暂停。"
        reasons = {"codex_unavailable": "Codex 额度暂时不可读取，保持保护性暂停。",
                   "cursor_unavailable": "Cursor 额度暂时不可读取，保持保护性暂停。",
                   "on_demand_enabled": "Cursor 按量使用已开启，额度保护器保持暂停。",
                   "stale": "额度记录已过期，保持保护性暂停。",
                   "paused": "额度保护器已暂停新调用。"}
        if _text(record.get("reason")) in reasons:
            reason_code = _text(record["reason"])
            reason = reasons[reason_code]
    elif ((record.get("status") is not None and
           (not isinstance(record["status"], str) or record["status"] not in {"healthy", "ok", "allowed"}))
          or not isinstance(record.get("latched"), bool)):
        status, reason_code, reason = "unknown", "guard_status_unconfirmed", "额度保护器状态未确认，保持保护性暂停。"
    cursor_threshold = _percent(record.get("cursor_threshold_percent")) if selective else threshold
    cursor_remaining = providers["cursor"]["remaining_percent"]
    cursor_permit = (status == "healthy" and cursor_threshold is not None and cursor_remaining is not None
                     and cursor_remaining > cursor_threshold and providers["cursor"]["on_demand_enabled"] is False
                     and (not selective or record.get("cursor_permit") is True))
    return {"available": True, "checked_at": checked, "age_seconds": max(0, now - checked) if checked else None,
            "max_age_seconds": max_age, "permit": status == "healthy", "status": status,
            "reason_code": reason_code, "reason": reason, "remaining_percent": remaining,
            "threshold_percent": threshold, "latched": latched, "providers": providers,
            "cursor_pause_scope": "cursor_roles" if selective else "all_roles",
            "cursor_threshold_percent": cursor_threshold, "cursor_permit": cursor_permit}


def _acceptance(config: dict, directory: Path | None, warnings: list[str]) -> dict:
    output = {"available": False, "status": "unknown"}
    path = _external_file(config, directory, "acceptance_file")
    if path is None:
        return output
    record = _json(path, warnings, label="Formal acceptance report", optional=False)
    if not record:
        return output
    requirements = record.get("requirements")
    if not isinstance(requirements, dict):
        warnings.append("Formal acceptance report: requirements are missing.")
        return output
    audit = requirements.get("fresh_independent_final_audit", {})
    if not isinstance(audit, dict):
        audit = {}
    builds = []
    for key, label in (("default_whole_lake_build", "Whole project"), ("all_prerequisite_build", "All prerequisites")):
        value = requirements.get(key)
        if isinstance(value, dict):
            builds.append({"label": label, "exit_code": value.get("exit_code") if _number(value.get("exit_code")) is not None else None,
                           "jobs": value.get("jobs") if _number(value.get("jobs")) is not None else None})
    passed = (record.get("mathematical_goal_achieved") is True
              and requirements.get("exact_target_kernel_closed") is True
              and audit.get("verdict") == "PASS" and _number(audit.get("critical_errors")) == 0
              and _number(audit.get("gaps")) == 0 and len(builds) == 2
              and all(build["exit_code"] == 0 for build in builds))
    paper = requirements.get("complete_paper_map", {})
    hashes = requirements.get("source_hashes_unchanged_after_audit", {})
    output.update(available=True, status="passed" if passed else "pending",
                  checked_at=_iso(record.get("checked_at")),
                  main_declaration=_text(record.get("kernel_main_declaration")),
                  main_source=_text(record.get("main_source")), main_sha256=_text(record.get("main_sha256"), 64),
                  mathematical_goal_achieved=record.get("mathematical_goal_achieved") is True,
                  independent_audit={"verdict": _text(audit.get("verdict")) or "UNKNOWN",
                                     **{key: audit.get(key) if _number(audit.get(key)) is not None else None
                                        for key in ("checks", "critical_errors", "gaps")}},
                  builds=builds, paper_labels=_number(paper.get("labels")) if isinstance(paper, dict) else None,
                  source_count=_number(hashes.get("project_lean_files")) if isinstance(hashes, dict) else None,
                  fact_admission_claimed=record.get("euler_main_fact_admission_claimed") is True,
                  source_label=path.name,
                  verification_basis="Reported acceptance artifact; dashboard does not rerun the Lean kernel.")
    return output


def _verifier(monitor: dict) -> dict:
    record = monitor.get("verifier")
    if not isinstance(record, dict):
        return {"available": False}
    return {"available": True, "backend": _text(record.get("backend")),
            "model": _text(record.get("model")), "effort": _text(record.get("effort")),
            "status": "stale" if monitor.get("coordinator_ui") == "stale" else _text(record.get("state")) or "unknown",
            "checked_at": _iso(monitor.get("checked_at_utc")), "source": "runtime_monitor"}


def _activity(project: Path, events: list[dict] | None, warnings: list[str], nodes=None) -> list[dict]:
    activity = []
    for kind in _CHANNELS:
        for index, row in enumerate(_tail_jsonl(project / "global_memory" / f"{kind}.jsonl", project, warnings)):
            timestamp = _iso(row.get("timestamp_utc"))
            activity.append({"id": _text(row.get("id")) or f"{kind}-{index}-{timestamp}",
                             "kind": kind, "title": _text(row.get("claim"), 220) or _text(row.get("verdict")) or kind,
                             "author": _text(row.get("author")), "timestamp": timestamp,
                             "detail": _text(row.get("evidence"), 1200)})
    for row in (events or [])[-ACTIVITY_LIMIT:]:
        payload = row["payload"]
        binding = payload.get("binding", {})
        author = binding.get("worker") if isinstance(binding, dict) else None
        identifier = payload.get("obligation_id") or payload.get("route_id") or payload.get("artifact_id")
        for field in ("evidence", "obligation", "route"):
            value = payload.get(field)
            if isinstance(value, dict):
                identifier = value.get("artifact_id") or value.get("obligation_id") or value.get("route_id") or identifier
        related = (nodes or {}).get(identifier, {}) if isinstance(identifier, str) else {}
        title = _text(related.get("display_title") or related.get("label"), 220)
        activity.append({"id": _text(row.get("event_id")), "kind": _text(row.get("event_type")),
                         "title": title or _text(row.get("event_type")).replace(".", " · ").replace("_", " "),
                         "author": _text(author) or _text(row.get("actor")), "timestamp": _iso(row.get("timestamp_utc")),
                         "detail": _text(related.get("statement"), 1200), "node_id": related.get("id")})
    activity.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    return activity[:ACTIVITY_LIMIT]


def _snapshot(project: Path) -> dict:
    warnings: list[str] = []
    config, directory = _config(warnings)
    meta = _json(project / "project.json", warnings, root=project, label="Project metadata")
    facts, graph = _facts(project, warnings)
    events = _events(project, warnings)
    research, control = _research(project, events, warnings)
    # Admission already records exact Fact -> obligation/evidence links. Reuse
    # those links to expose existing proofs, never guess from similar wording.
    research_nodes = {item["id"]: item for item in research["nodes"]}
    for event in events or []:
        if event.get("event_type") != "euler.fact_linked":
            continue
        link = event.get("payload", {})
        fact = facts.get(link.get("fact_id"))
        if not fact or fact.get("status") != "accepted":
            continue
        dependencies = link.get("dependency_hashes", {})
        if not isinstance(dependencies, dict) or link.get("fact_id") not in dependencies:
            continue
        if any(fid not in facts or facts[fid].get("status") != "accepted"
               or facts[fid].get("source_sha256") != digest for fid, digest in dependencies.items()):
            continue
        for key in ("obligation_id", "evidence_id"):
            item = research_nodes.get(link.get(key))
            if item and item.get("validity") == "VALID" and item.get("statement") == fact.get("statement"):
                item.setdefault("linked_fact_ids", [])
                if fact["id"] not in item["linked_fact_ids"]:
                    item["linked_fact_ids"].append(fact["id"])
    labels = config.get("node_labels", {})
    if isinstance(labels, dict):
        for item in graph["nodes"] + research["nodes"]:
            if isinstance(labels.get(item["id"]), str):
                item["display_title"] = labels[item["id"]][:160]
    research["reading_order"] = [i for i in config.get("reading_order", [])
                                 if isinstance(i, str)] if isinstance(config.get("reading_order"), list) else []
    workers = _workers(project, meta, facts, warnings)
    # Operator corrections affect the display only; runtime roles and receipts stay intact.
    hidden = config.get("hidden_workers", [])
    hidden = {name for name in hidden if isinstance(name, str)} if isinstance(hidden, list) else set()
    workers = [worker for worker in workers if worker["id"] not in hidden]
    display_overrides = config.get("worker_display_overrides", {})
    if isinstance(display_overrides, dict):
        for worker in workers:
            override = display_overrides.get(worker["id"], {})
            if isinstance(override, dict):
                for field in ("name", "role", "provider", "model", "effort"):
                    if isinstance(override.get(field), str):
                        worker[field] = _text(override[field], 160)
    worker_labels = config.get("worker_labels", {})
    if isinstance(worker_labels, dict):
        for worker in workers:
            if isinstance(worker_labels.get(worker["id"]), str):
                worker["name"] = worker_labels[worker["id"]][:100]
    monitor: dict = {}
    monitor_path = _external_file(config, directory, "monitor_file")
    if monitor_path:
        monitor = _json(monitor_path, warnings, label="Runtime monitor", optional=False)
        if monitor.get("project") is not None and _text(monitor.get("project")) not in {project.name, _text(meta.get("name"))}:
            warnings.append("Runtime monitor belongs to a different project; ignored.")
            monitor = {}
        checked = _epoch(monitor.get("checked_at_utc"))
        max_age = _number(config.get("monitor_max_age_seconds")) or MONITOR_STALE_SECONDS
        if monitor and (checked is None or time.time() - checked > max_age):
            warnings.append("Runtime monitor snapshot is stale; coordinator status is not live.")
            monitor = {**monitor, "coordinator_ui": "stale"}
    teams = _teams(workers, config, meta, monitor, warnings)
    memberships = {worker["id"]: team["id"] for team in teams for worker in team["workers"]}
    for layer in (graph, research):
        for node in layer["nodes"]:
            node["team_id"] = memberships.get(node["author"])
    acceptance = _acceptance(config, directory, warnings)
    quota = _quota(config, directory, warnings)
    activity = _activity(project, events, warnings, research_nodes)
    goal = _text(control.get("goal") or meta.get("goal"), 3000)
    if not goal:
        try:
            goal = _read_text(project / "PROBLEM.md", root=project, limit=MAX_FILE_BYTES)[:3000]
        except (OSError, ValueError, UnicodeError):
            pass
    workspace = {
        "project": {"id": project.name, "name": _text(config.get("name")) or _text(meta.get("name")) or project.name,
                    "description": _text(config.get("description"), 1500),
                    "independent": config.get("independent_project") is True,
                    "state": control.get("outcome") or control["target_status"], "goal": goal},
        "metrics": {"facts": sum(n["status"] == "accepted" for n in graph["nodes"]),
                    "teams": len(teams), "workers": len(workers),
                    "active_workers": (None if any(worker["alive"] is None and worker["status"] in {"running", "idle"}
                                                   for worker in workers) else sum(worker["alive"] is True for worker in workers)),
                    "blocked_workers": sum(_blocked(worker["status"]) for worker in workers),
                    "graph_edges": len(graph["edges"])},
        "teams": teams, "activity": activity, "control": control, "acceptance": acceptance, "quota": quota,
        "verifier": _verifier(monitor),
        "updated_at": time.time(), "warnings": list(dict.fromkeys(warnings)),
    }
    from .historical_projection import attach
    return attach({"workspace": workspace, "facts": facts, "facts_graph": graph, "research_graph": research}, config, project=project)


def snapshot(project: Path, *, use_cache: bool = True) -> dict:
    project = project.resolve()
    config, directory = _config([])
    frozen = _external_file(config, directory, 'frozen_snapshot')
    if frozen and (project / 'RESTORE_ORIGIN.json').is_file():
        # A historical view is pinned to the same Git tree as the restored files.
        # It is not rebuilt from today's external case-study sources.
        return json.loads(frozen.read_text())
    key = str(project) + "|" + str(_config_value() or "")
    with _CACHE_LOCK:
        build_lock = _BUILD_LOCKS.setdefault(key, threading.Lock())
    with build_lock:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if use_cache and cached and time.monotonic() - cached[0] < 2:
                return copy.deepcopy(cached[1])
        result = _snapshot(project)
        with _CACHE_LOCK:
            if len(_CACHE) >= 8:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[key] = (time.monotonic(), result)
    return copy.deepcopy(result)


def build_workspace(project: Path, *, use_cache: bool = True) -> dict:
    return snapshot(project, use_cache=use_cache)["workspace"]


def build_graph(project: Path, layer: str = "facts", *, use_cache: bool = True) -> dict:
    if layer not in {"facts", "research"}:
        raise ValueError("unknown graph layer")
    return snapshot(project, use_cache=use_cache)[layer + "_graph"]


def build_fact_detail(project: Path, fact_id: str) -> dict | None:
    fact = snapshot(project)["facts"].get(fact_id)
    if fact is None:
        return None
    return {key: fact.get(key) for key in ("id", "statement", "proof", "intuition", "predecessors", "author", "status", "created_at")}
