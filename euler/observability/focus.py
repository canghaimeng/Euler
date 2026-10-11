# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Read-only relation-map focus: the main line of a goal and its newest substantive results.

Only currently valid recorded support edges (premise -> dependent) form highlighted
paths, and parked, refuted or stale records are never traversed. "Substantive" means
a recorded status transition of an obligation into DISCHARGED/REFUTED, a Fact
admission, or a public research record (proof attempt, conclusion, obstacle,
counterexample, dead end) that has not been accepted. Heartbeats, leases, budgets and
evidence registration never count. A focus is a reading aid over recorded structure,
not a newly proved implication.
"""
from __future__ import annotations

from collections import defaultdict, deque

from .architecture import _Graph
from .monitor import _epoch, _iso, _tail_jsonl, _text, snapshot

SUPPORT = {"requires", "depends_on", "REQUIRES", "targets"}
INACTIVE = {"parked", "refuted", "stale"}
FRONTIER_LIMIT = 6
READY_LIMIT = 4
RECORD_LIMIT = 6
OPEN_MAINLINE_LIMIT = 40
RECORD_KINDS = ("conclusion", "proof_attempt", "counterexample", "obstacle", "dead_end")
# Only these structured fields name a graph record; free text is never matched.
LINK_KEYS = {"obligation_id", "obligation_ids", "obligation", "route_id", "fact_id", "fact_ids",
             "related_fact", "related_facts", "predecessor_fact", "predecessor_facts", "target", "target_id"}
ATTEMPT_RESULTS = {"success", "partial", "blocked", "abandoned"}


def edge_key(edge: dict) -> str:
    return edge.get("id") or f"{edge['source']}|{edge['type']}|{edge['target']}"


class _Map:
    def __init__(self, graph: dict):
        self.graph = _Graph(graph)
        self.nodes, self.state = self.graph.nodes, self.graph.state
        self.recorded_in = defaultdict(list)
        self.support_in, self.support_out, self.evidence_in = defaultdict(list), defaultdict(list), defaultdict(list)
        for edge in graph.get("edges", []):
            if edge["source"] not in self.nodes or edge["target"] not in self.nodes:
                continue
            self.recorded_in[edge["target"]].append(edge)
            if edge.get("validity", "VALID") != "VALID":
                continue
            if edge["type"] in SUPPORT:
                self.support_in[edge["target"]].append(edge)
                self.support_out[edge["source"]].append(edge)
            elif edge["type"] == "evidence":
                self.evidence_in[edge["target"]].append(edge)

    def active(self, key: str) -> bool:
        return self.state[key] not in INACTIVE and self.nodes[key].get("kind") != "evidence"

    def supports(self, key: str):
        """Recorded supports of ``key``; a closed node keeps only routes that closed it."""
        closed = self.state[key] == "established"
        for edge in self.support_in[key]:
            source = edge["source"]
            if not self.active(source):
                continue
            if edge["type"] == "targets" and closed and self.state[source] != "completed":
                continue
            yield edge

    def premises_closed(self, key: str) -> list[str] | None:
        """Recorded premises when every one is currently closed, else ``None``.

        At least one premise must be recorded, every recorded premise edge must be
        valid, every premise must be established now and no valid blocker may be open.
        Zero recorded premises never counts as "all closed".
        """
        recorded = [edge for edge in self.recorded_in[key] if edge["type"] in SUPPORT - {"targets"}]
        if not recorded or any(edge.get("validity", "VALID") != "VALID" for edge in recorded):
            return None
        if any(self.state[edge["source"]] != "established" for edge in recorded):
            return None
        if any(edge["type"] == "BLOCKED_BY" and edge.get("validity", "VALID") == "VALID"
               and self.state[edge["source"]] != "established" for edge in self.recorded_in[key]):
            return None
        return list(dict.fromkeys(edge["source"] for edge in recorded))

    def path_to(self, start: str, goal: str | None):
        """Shortest valid recorded path start -> goal through active records."""
        if start not in self.nodes or goal not in self.nodes:
            return None
        if not self.active(start) or not self.active(goal):
            return None
        if start == goal:
            return ([start], [])
        previous = {start: None}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for edge in self.support_out[current]:
                target = edge["target"]
                if target in previous or not (self.active(target) or target == goal):
                    continue
                previous[target] = edge
                if target == goal:
                    nodes, edges, cursor = [goal], [], goal
                    while previous[cursor] is not None:
                        edges.append(edge_key(previous[cursor]))
                        cursor = previous[cursor]["source"]
                        nodes.append(cursor)
                    return nodes[::-1], edges[::-1]
                queue.append(target)
        return None


def _time(node: dict):
    """Recorded time of the node's current status and where that time comes from."""
    if node.get("status_changed_at"):
        return node["status_changed_at"], "status_event"
    if node.get("kind") == "fact" and node.get("created_at"):
        return node["created_at"], "fact_file"
    return node.get("created_at"), "created"


def mainline(graph: dict, anchor: str | None) -> dict:
    view = _Map(graph)
    if anchor not in view.nodes:
        return {"anchor": None, "nodes": [], "edges": [], "basis": "none", "routes": [], "evidence": []}
    closed_goal = view.state[anchor] in {"established", "refuted"}
    completed = [edge for edge in view.support_in[anchor] if edge["type"] == "targets"
                 and view.state[edge["source"]] == "completed"]
    nodes, edges, order = {anchor}, set(), [anchor]
    queue = deque([(anchor, 0)])
    limit = None if closed_goal or graph.get("projection") == "historical_source" else 2
    if completed:
        basis = "completed_route"
    elif view.nodes[anchor].get("kind") == "fact":
        basis = "main_fact"
    else:
        basis = "goal_structure"
    while queue:
        current, depth = queue.popleft()
        if limit is not None and depth >= limit:
            continue
        for edge in view.supports(current):
            if completed and current == anchor and edge["type"] == "targets" and edge not in completed:
                continue
            source = edge["source"]
            if source not in nodes:
                if not closed_goal and len(nodes) >= OPEN_MAINLINE_LIMIT:
                    continue
                nodes.add(source)
                order.append(source)
                queue.append((source, depth + 1))
            edges.add(edge_key(edge))
    # Curated reading-order records stay on the main line with their recorded link to the goal.
    for key in graph.get("reading_order") or []:
        if key in view.nodes and key not in nodes and view.active(key):
            path = view.path_to(key, anchor)
            if path:
                for item in path[0]:
                    if item not in nodes:
                        nodes.add(item)
                        order.append(item)
                edges.update(path[1])
    evidence = []
    for key in [anchor] + [edge["source"] for edge in completed]:
        for edge in view.evidence_in[key]:
            if view.state[edge["source"]] == "established":
                evidence.append(edge["source"])
                edges.add(edge_key(edge))
    return {"anchor": anchor, "nodes": order + [key for key in evidence if key not in nodes], "edges": sorted(edges),
            "basis": basis, "routes": [edge["source"] for edge in completed], "evidence": evidence,
            "limited": not closed_goal and len(nodes) >= OPEN_MAINLINE_LIMIT}


def _links(row: dict, ids: dict[str, set]) -> list[dict]:
    values = [("fact_id", row.get("fact_id"))]
    links = row.get("links")
    if isinstance(links, dict):
        values += [(key, value) for key, value in links.items() if key in LINK_KEYS]
    found, seen = [], set()
    for key, value in values:
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str) or item in seen:
                continue
            layer = "research" if item in ids["research"] else "facts" if item in ids["facts"] else None
            if layer:
                seen.add(item)
                found.append({"id": item, "layer": layer, "field": key})
    return found


def public_records(project, ids: dict[str, set], limit: int = RECORD_LIMIT) -> list[dict]:
    """Newest unaccepted public research records with their structured links only."""
    warnings: list[str] = []
    rows = []
    for kind in RECORD_KINDS:
        for row in _tail_jsonl(project / "global_memory" / f"{kind}.jsonl", project, warnings, limit=80):
            claim = _text(row.get("claim"), 600)
            if not claim:
                continue
            rows.append({"id": _text(row.get("id"), 160), "kind": kind, "claim": claim,
                         "detail": _text(row.get("evidence"), 8000), "reason": _text(row.get("reason"), 2000),
                         "result": row.get("result") if row.get("result") in ATTEMPT_RESULTS else None,
                         "reported_status": _text(row.get("status"), 40), "author": _text(row.get("author")),
                         "timestamp": _iso(row.get("timestamp_utc")), "links": _links(row, ids),
                         "verification": "unaccepted_public_record"})
    rows.sort(key=lambda row: (_epoch(row["timestamp"]) is None, -(_epoch(row["timestamp"]) or 0), row["kind"], row["id"]))
    return rows[:limit]


def frontier(graph: dict, anchor: str | None) -> dict:
    view = _Map(graph)
    items = []
    results = [key for key, node in view.nodes.items()
               if (node.get("kind") == "obligation" and view.state[key] in {"established", "refuted"} and key != anchor)
               or (node.get("kind") == "fact" and view.state[key] == "established" and key != anchor)]
    results = [key for key in results if _epoch(_time(view.nodes[key])[0]) is not None]
    results.sort(key=lambda key: (-_epoch(_time(view.nodes[key])[0]), key))
    for key in results[:FRONTIER_LIMIT]:
        at, source = _time(view.nodes[key])
        kind = "admitted" if view.nodes[key].get("kind") == "fact" else "refuted" if view.state[key] == "refuted" else "closed"
        items.append({"id": key, "kind": kind, "at": at, "time_source": source})
    ready = []
    for key, node in view.nodes.items():
        if key == anchor or node.get("kind") not in {"obligation", "route"}:
            continue
        if view.state[key] not in {"open", "attempted", "proposed"}:
            continue
        premises = view.premises_closed(key)
        if premises:
            latest = max(premises, key=lambda item: _epoch(_time(view.nodes[item])[0]) or 0)
            ready.append((key, _epoch(_time(view.nodes[latest])[0]), latest, premises))
    ready.sort(key=lambda item: (view.path_to(item[0], anchor) is None, -(item[1] or 0), item[0]))
    for key, latest, premise, premises in ready[:READY_LIMIT]:
        items.append({"id": key, "kind": "ready", "at": _time(view.nodes[premise])[0] if latest else None,
                      "time_source": "premises_closed", "premises": premises})
    nodes, edges = ([anchor] if anchor in view.nodes else []), set()
    for item in items:
        path = view.path_to(item["id"], anchor) if anchor in view.nodes else None
        item["connected"] = path is not None
        item["path"] = path[0] if path else [item["id"]]
        for key in item["path"] + item.get("premises", []):
            if key not in nodes:
                nodes.append(key)
        if path:
            edges.update(path[1])
        for edge in view.recorded_in[item["id"]] if item["kind"] == "ready" else []:
            if edge["source"] in item["premises"]:
                edges.add(edge_key(edge))
    return {"anchor": anchor if anchor in view.nodes else None, "items": items, "nodes": nodes, "edges": sorted(edges)}


def alternatives(graph: dict, anchor: str | None) -> list[str]:
    view = _Map(graph)
    if anchor not in view.nodes:
        return []
    return [edge["source"] for edge in graph.get("edges", []) if edge["target"] == anchor and edge["type"] == "targets"
            and edge["source"] in view.nodes and view.state[edge["source"]] in {"parked", "refuted"}]


def phase(research: dict, workspace: dict) -> dict:
    """Completion follows the target's current valid state, not a historical outcome event.

    Controller closure and formal acceptance are reported as separate records. A
    target reopened after a SOLVED event or a formal report is in progress again.
    A later formal report can select completed reading mode for a legacy open
    controller target; the two recorded statuses remain separate.
    """
    control, acceptance = workspace.get("control", {}), workspace.get("acceptance", {})
    target = research.get("target_id")
    view = _Map(research)
    state = view.state.get(target) if target in view.nodes else None
    formal = acceptance.get("status") == "passed" and acceptance.get("mathematical_goal_achieved") is True
    closed = state in {"established", "refuted"}
    formal_current = formal
    if target in view.nodes:
        record = view.nodes[target]
        changed = max((_epoch(record.get(key)) or 0 for key in ("created_at", "status_changed_at")), default=0)
        checked = _epoch(acceptance.get("checked_at"))
        formal_current = formal and state != "stale" and checked is not None and changed > 0 and checked >= changed
    if research.get("projection") == "historical_source":
        original_closed = control.get("target_status") in {"DISCHARGED", "REFUTED"}
        return {"complete":closed or formal_current,"archive_closed":closed,
                "controller_closed":original_closed,"target_state":state,"target_status":control.get("target_status"),
                "recorded_outcome":control.get("outcome"),"formal_acceptance":acceptance.get("status"),
                "formal_available":acceptance.get("available") is True,"formal_passed":formal,"formal_current":formal_current}
    return {"complete": closed or formal_current, "controller_closed": closed,
            "target_state": state, "target_status": control.get("target_status"),
            "recorded_outcome": control.get("outcome"), "formal_acceptance": acceptance.get("status") or None,
            "formal_available": acceptance.get("available") is True,
            "formal_passed": formal, "formal_current": formal_current}


def focus(graph: dict, anchor: str | None, complete: bool, records: list[dict] | None = None) -> dict:
    main, front = mainline(graph, anchor), frontier(graph, anchor)
    front["records"] = records or []
    if complete:
        default = "mainline" if len(main["nodes"]) > 1 else "all"
    else:
        default = "frontier" if front["items"] or front["records"] else "mainline" if len(main["nodes"]) > 1 else "all"
    return {"anchor": main["anchor"] or front["anchor"], "complete": complete, "default_view": default,
            "mainline": main, "frontier": front, "alternatives": alternatives(graph, anchor)}


def build_map_focus(project, layer: str = "research") -> dict:
    data = snapshot(project)
    workspace, research = data["workspace"], data["research_graph"]
    graph = data[layer + "_graph"]
    status = phase(research, workspace)
    if layer == "research":
        anchor, complete = graph.get("target_id"), status["complete"]
    else:
        # A Fact is a main conclusion only when the project's reading order says so.
        order = research.get("reading_order") or []
        ids = {node["id"] for node in graph.get("nodes", []) if node.get("status") == "accepted"}
        anchor = research.get("fact_target_id") if research.get("fact_target_id") in ids else next((key for key in order if key in ids), None)
        complete = status["controller_closed"] or status["formal_passed"]
    ids = {name: {node["id"] for node in data[name + "_graph"].get("nodes", [])} for name in ("research", "facts")}
    result = focus(dict(graph, reading_order=research.get("reading_order") or []), anchor, complete,
                   public_records(project, ids))
    if layer == "research" and graph.get("projection") == "historical_source":
        result["default_view"] = "mainline"
        status["historical_source"] = True
    result.update(layer=layer, phase=status, updated_at=workspace.get("updated_at"))
    return result
