# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Euler personal workbench — observation and configured research controls for a
project's mathematical state. Dedicated control modules own writes.

A single FastAPI app serving locally bundled browser assets without a build
step at deployment. Compatibility parsers below read the public on-disk stores
under a project dir and NEVER write:

  <project>/fact_graph/facts/*.md         the verified-fact DAG
  <project>/global_memory/<kind>.jsonl    categorized findings (the 11 kinds)
  <project>/spend/consult.jsonl       pro-consult cost ledger (optional)

The richer team/research/evidence projections live in ``monitor.py`` and do not
instantiate mutable runtime stores. Re-sync ``CHANNELS`` when the core channel
schema changes.

Config is read at CALL time from args / env (never at import): project dir from
``--project`` / ``EULER_DASHBOARD_PROJECT`` / ``EULER_PROJECT_DIR``; bind
127.0.0.1:8099 (loopback only) by default.

Run:
    python -m euler.observability --project /path/to/project [--port 8099]
    EULER_PROJECT_DIR=/path/to/project python -m euler.observability
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .access import install_access_control

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

# ------------------------------------------------------------------------- #
# channels — global-memory kinds in display order, each with a semantic role  #
# tag the client uses to color/group them. Mirrors euler.core GLOBAL_KINDS    #
# (kept as data on purpose — the dashboard imports no core runtime module).   #
# ------------------------------------------------------------------------- #
CHANNELS = [
    ("conclusion", "result"), ("example", "result"), ("counterexample", "result"),
    ("proof_attempt", "result"), ("plan", "judgment"), ("direction", "judgment"),
    ("obstacle", "deadend"), ("dead_end", "deadend"), ("verification", "verify"),
    ("elaboration", "strategy"), ("master_guidance", "strategy"),
]
_CHANNEL_KINDS = {k for k, _ in CHANNELS}


# ------------------------------------------------------------------------- #
# config — resolved at CALL time (never at import)                           #
# ------------------------------------------------------------------------- #

def _project_dir() -> Path:
    p = os.environ.get("EULER_DASHBOARD_PROJECT") or os.environ.get("EULER_PROJECT_DIR")
    if not p:
        raise RuntimeError("no project dir — set --project / EULER_PROJECT_DIR")
    return Path(p)


# on-disk layout, centralized so a layout change touches one spot.
def _facts_dir(project: Path) -> Path:
    return project / "fact_graph" / "facts"


def _channel_file(project: Path, kind: str) -> Path:
    return project / "global_memory" / f"{kind}.jsonl"


def _spend_file(project: Path) -> Path:
    return project / "spend" / "consult.jsonl"


# ------------------------------------------------------------------------- #
# parsing (self-contained; never writes; tolerant of partial/malformed data) #
# ------------------------------------------------------------------------- #

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_fact(text: str) -> Dict[str, Any]:
    """fact md = YAML-ish frontmatter (fact_id / problem_id / author /
    predecessors) + ``## statement`` / ``## proof`` / ``## intuition`` sections."""
    m = _FM.match(text)
    fm: Dict[str, Any] = {}
    body = text
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                fm[k] = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()] if inner else []
            else:
                fm[k] = v
    secs = {"statement": "", "proof": "", "intuition": ""}
    cur: Optional[str] = None
    fence = None
    for line in body.splitlines():
        stripped = line.strip()
        if fence is None and stripped.startswith(("```", "~~~")):
            char = stripped[0]
            fence = (char, len(stripped) - len(stripped.lstrip(char)))
        elif fence is not None:
            char, length = fence
            if stripped and set(stripped) == {char} and len(stripped) >= length:
                fence = None
        else:
            heading = stripped.lower()
            # Metadata sections are ordered. A quoted lemma's headings inside
            # the proof must never overwrite the Fact's original statement.
            if cur is None and heading == '## statement':
                cur = 'statement'
                continue
            if cur in {None, 'statement'} and heading == '## proof':
                cur = 'proof'
                continue
            if cur == 'proof' and heading == '## intuition':
                cur = 'intuition'
                continue
        if cur:
            secs[cur] += line + "\n"
    return {
        "fact_id": fm.get("fact_id", "") if isinstance(fm.get("fact_id", ""), str) else "",
        "problem_id": fm.get("problem_id", "") if isinstance(fm.get("problem_id", ""), str) else "",
        "author": fm.get("author", "") if isinstance(fm.get("author", ""), str) else "",
        "predecessors": fm.get("predecessors", []) if isinstance(fm.get("predecessors", []), list) else [],
        "statement": secs["statement"].strip(),
        "proof": secs["proof"].strip(),
        "intuition": secs["intuition"].strip(),
    }


def _load_facts(project: Path) -> List[Dict[str, Any]]:
    d = _facts_dir(project)
    if not d.is_dir() or not _public_source(project, d):
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(d.glob("*.md")):
        if not _public_source(project, f):
            continue
        try:
            fact = _parse_fact(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if not fact["fact_id"]:
            fact["fact_id"] = f.stem
        out.append(fact)
    return out


def _public_source(project: Path, path: Path) -> bool:
    """A public-store path cannot redirect into private memory through a symlink."""
    try:
        relative = path.absolute().relative_to(project.absolute())
        current = project.absolute()
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        path.resolve().relative_to(project.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read a .jsonl line-by-line, skipping blank/malformed lines. Missing file
    -> empty. Never raises on bad data (stores are appended while we read)."""
    if not path.is_file() or path.is_symlink():
        return []
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _load_channel(project: Path, kind: str) -> List[Dict[str, Any]]:
    path = _channel_file(project, kind)
    return _load_jsonl(path) if _public_source(project, path) else []


def _load_spend(project: Path) -> List[Dict[str, Any]]:
    path = _spend_file(project)
    return _load_jsonl(path) if _public_source(project, path) else []


def _depths(deps: Dict[str, List[str]]) -> Dict[str, int]:
    """depth = longest path from a leaf (no-predecessor) node up to this one —
    how many dependency layers a fact is built on. Leaves = 0. Cycle-guarded (a
    content-addressed DAG shouldn't cycle, but the store may be mid-write)."""
    depth: Dict[str, int] = {}

    def get(fid: str, stack: frozenset) -> int:
        if fid in depth:
            return depth[fid]
        if fid in stack:  # cycle guard
            return 0
        ps = deps.get(fid, [])
        d = 0 if not ps else 1 + max(get(p, stack | {fid}) for p in ps)
        depth[fid] = d
        return d

    for fid in deps:
        get(fid, frozenset())
    return depth


# ------------------------------------------------------------------------- #
# typed response models (the four /api/* payloads)                           #
# ------------------------------------------------------------------------- #

class Overview(BaseModel):
    project: str
    facts: int
    facts_with_predecessors: int
    facts_by_author: Dict[str, int]
    channel_counts: Dict[str, int]
    verdicts: Dict[str, int]
    consult_count: int
    consult_cost_usd: float
    updated_at: float


class GraphNode(BaseModel):
    id: str
    author: str
    problem_id: str
    statement: str
    proof: str
    intuition: str
    predecessors: List[str]
    depth: int


class GraphEdge(BaseModel):
    source: str
    target: str


class FactGraphResp(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    max_depth: int


class ChannelInfo(BaseModel):
    kind: str
    role: str
    count: int


class ChannelsResp(BaseModel):
    channels: List[ChannelInfo]


class ChannelResp(BaseModel):
    kind: str
    count: int
    entries: List[Dict[str, Any]]


# ------------------------------------------------------------------------- #
# route implementations (pure functions — testable offline without a client) #
# ------------------------------------------------------------------------- #

def build_overview(project: Optional[Path] = None) -> Dict[str, Any]:
    project = project or _project_dir()
    facts = _load_facts(project)
    counts = {k: len(_load_channel(project, k)) for k, _ in CHANNELS}
    verdicts: Dict[str, int] = {}
    for e in _load_channel(project, "verification"):
        v = e.get("verdict", "?")
        verdicts[v] = verdicts.get(v, 0) + 1
    spend = _load_spend(project)
    total_cost = round(sum(float(s.get("cost_usd", 0.0) or 0.0) for s in spend), 2)
    by_author: Dict[str, int] = {}
    for f in facts:
        by_author[f["author"]] = by_author.get(f["author"], 0) + 1
    leaves = sum(1 for f in facts if not f["predecessors"])
    return {
        "project": project.name,
        "facts": len(facts),
        "facts_with_predecessors": len(facts) - leaves,
        "facts_by_author": by_author,
        "channel_counts": counts,
        "verdicts": verdicts,
        "consult_count": len(spend),
        "consult_cost_usd": total_cost,
        "updated_at": time.time(),
    }


def build_factgraph(project: Optional[Path] = None) -> Dict[str, Any]:
    project = project or _project_dir()
    facts = _load_facts(project)
    ids = {f["fact_id"] for f in facts}
    deps = {f["fact_id"]: [p for p in f["predecessors"] if p in ids] for f in facts}
    depth = _depths(deps)
    nodes = [{
        "id": f["fact_id"], "author": f["author"], "problem_id": f["problem_id"],
        "statement": f["statement"], "proof": f["proof"], "intuition": f["intuition"],
        "predecessors": deps[f["fact_id"]],
        "depth": depth.get(f["fact_id"], 0),
    } for f in facts]
    edges = [{"source": p, "target": f["fact_id"]} for f in facts for p in deps[f["fact_id"]]]
    return {"nodes": nodes, "edges": edges, "max_depth": max(depth.values(), default=0)}


def build_channels(project: Optional[Path] = None) -> Dict[str, Any]:
    project = project or _project_dir()
    return {"channels": [{"kind": k, "role": r, "count": len(_load_channel(project, k))}
                         for k, r in CHANNELS]}


def build_channel(kind: str, project: Optional[Path] = None) -> Dict[str, Any]:
    if kind not in _CHANNEL_KINDS:
        raise KeyError(kind)
    project = project or _project_dir()
    entries = _load_channel(project, kind)
    entries.sort(key=lambda e: e.get("timestamp_utc", ""), reverse=True)
    return {"kind": kind, "count": len(entries), "entries": entries}


# ------------------------------------------------------------------------- #
# app                                                                        #
# ------------------------------------------------------------------------- #

app = FastAPI(title="euler-observability", version="0.1.0")
install_access_control(app)


def _read_project(identifier, reader, *, with_entry=False):
    from .catalog import resolve
    from .monitor import dashboard_config
    try:
        entry = resolve(_project_dir(), identifier)
    except KeyError:
        raise HTTPException(404, "unknown project")
    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(503, "project catalog unavailable")
    if not entry.path.is_dir():
        raise HTTPException(503, "project temporarily unavailable")
    with dashboard_config(entry.config):
        return reader(entry) if with_entry else reader(entry.path)


@app.get("/api/case-studies")
def case_studies() -> JSONResponse:
    from .case_study import list_cases
    try:
        return JSONResponse(list_cases())
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "case-study catalog unavailable")


@app.get("/api/case-studies/{case_id}")
def case_study(case_id: str) -> JSONResponse:
    from .case_study import get_case
    try:
        result = get_case(case_id)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "case-study catalog unavailable")
    if result is None:
        raise HTTPException(404, "unknown case study")
    return JSONResponse(result)


@app.get("/api/case-studies/{case_id}/sources/{source_id}")
def case_study_source(case_id: str, source_id: str) -> JSONResponse:
    from .case_study import get_source
    try:
        result = get_source(case_id, source_id)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "case-study source unavailable")
    if result is None:
        raise HTTPException(404, "unknown case-study source")
    return JSONResponse(result)


@app.get("/api/projects")
def projects() -> JSONResponse:
    from .catalog import catalog
    try:
        entries, selected = catalog(_project_dir())
    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(503, "project catalog unavailable")
    return JSONResponse({"default_project": selected, "projects": [
        {"id": p.id, "name": p.name, "description": p.description, "available": p.path.is_dir()}
        for p in entries]})


@app.get("/api/workspace")
def workspace(project: str | None = None) -> JSONResponse:
    from .monitor import build_workspace
    return JSONResponse(_read_project(project, build_workspace))


@app.get("/api/graph")
def graph(layer: str = "facts", project: str | None = None) -> JSONResponse:
    from .monitor import build_graph
    if layer not in {"facts", "research"}:
        raise HTTPException(400, "unknown graph layer")
    return JSONResponse(_read_project(project, lambda p: build_graph(p, layer)))


@app.get("/api/derivation")
def derivation(node_id: str, layer: str = "research", project: str | None = None) -> JSONResponse:
    from .derivation import build_derivation
    if layer not in {"facts", "research"}:
        raise HTTPException(400, "unknown graph layer")
    result = _read_project(project, lambda entry: build_derivation(entry.path, layer, node_id, entry.id), with_entry=True)
    if result is None:
        raise HTTPException(404, "unknown node")
    return JSONResponse(result)


@app.get("/api/exploration")
def exploration(project: str | None = None) -> JSONResponse:
    from .exploration import build_exploration
    return JSONResponse(_read_project(project, build_exploration))


@app.get("/api/map-focus")
def map_focus(layer: str = "research", project: str | None = None) -> JSONResponse:
    from .focus import build_map_focus
    if layer not in {"facts", "research"}:
        raise HTTPException(400, "unknown graph layer")
    return JSONResponse(_read_project(project, lambda p: build_map_focus(p, layer)))


@app.get("/api/architecture")
def architecture(project: str | None = None) -> JSONResponse:
    from .architecture import build_architecture
    return JSONResponse(_read_project(project, build_architecture))


@app.get("/api/facts/{fact_id}")
def fact_detail(fact_id: str, project: str | None = None) -> JSONResponse:
    from .monitor import build_fact_detail
    result = _read_project(project, lambda p: build_fact_detail(p, fact_id))
    if result is None:
        raise HTTPException(404, "unknown fact")
    return JSONResponse(result)


@app.get("/api/evidence/acceptance")
def acceptance_evidence(project: str | None = None) -> JSONResponse:
    from .monitor import build_workspace
    return JSONResponse(_read_project(project, lambda p: build_workspace(p)["acceptance"]))


@app.get("/api/quota")
def quota_monitor(project: str | None = None) -> JSONResponse:
    from .monitor import build_workspace
    return JSONResponse(_read_project(project, lambda p: build_workspace(p)["quota"]))


@app.get("/api/overview", response_model=Overview)
def overview(project: str | None = None) -> JSONResponse:
    return JSONResponse(_read_project(project, build_overview))


@app.get("/api/factgraph", response_model=FactGraphResp)
def factgraph(project: str | None = None) -> JSONResponse:
    return JSONResponse(_read_project(project, build_factgraph))


@app.get("/api/channels", response_model=ChannelsResp)
def channels(project: str | None = None) -> JSONResponse:
    return JSONResponse(_read_project(project, build_channels))


@app.get("/api/channel/{kind}", response_model=ChannelResp)
def channel(kind: str, project: str | None = None) -> JSONResponse:
    try:
        return JSONResponse(_read_project(project, lambda p: build_channel(kind, p)))
    except KeyError:
        raise HTTPException(404, f"unknown channel {kind}")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


from .human_api import install_human_routes
install_human_routes(app, _read_project)
from .milestone_api import install_milestone_routes
install_milestone_routes(app, _read_project)
from .team_api import install_team_routes
install_team_routes(app, _read_project)
from .project_api import install_project_routes
install_project_routes(app)

app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    ap = argparse.ArgumentParser(description="Euler project workbench: teams, proof graphs and evidence.")
    ap.add_argument("--project", help="project dir (or set EULER_PROJECT_DIR)")
    ap.add_argument("--config", help="optional dashboard JSON: team grouping and public evidence sources")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    if args.project:
        os.environ["EULER_DASHBOARD_PROJECT"] = args.project
    config = args.config or os.environ.get("EULER_DASHBOARD_CONFIG")
    if config:
        path = Path(config).expanduser().resolve()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("configuration must be a JSON object")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid dashboard config: {path}: {exc}") from exc
        os.environ["EULER_DASHBOARD_CONFIG"] = str(path)
    project = _project_dir()  # fail fast if unset
    if not project.is_dir():
        raise SystemExit(f"project dir not found: {project}")
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    import uvicorn
    print(f"euler dashboard: http://{args.host}:{args.port}/  (project: {project})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
