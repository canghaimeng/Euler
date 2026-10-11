# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""A small proof-architecture overview projected from recorded structure only.

Displayed modules are curated reading-order nodes, or else the recorded
prerequisites of the main target and its routes. Edges between displayed
modules summarize recorded dependency chains (prerequisite -> dependent) and
report what remains open on the hidden intermediate records. Nothing here
creates a proof step, a status or a completion percentage.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .monitor import _epoch, _tail_jsonl, snapshot

DEPENDENCIES = {"requires", "depends_on", "REQUIRES", "BLOCKED_BY", "targets"}
MAX_MODULES = 8
MAX_ROUTES_IN_MAP = 6
MAX_OPEN_ITEMS = 6
MAX_CHAIN_ITEMS = 3
RECORD_LIMIT = 200000
ROUTE_RANK = {"ACTIVE": 0, "COMPLETED": 1, "PROPOSED": 2, "BLOCKED": 3, "PARKED": 4, "REFUTED": 5}
STATE_RANK = {"blocked": 0, "attempted": 1, "open": 2, "proposed": 3, "failed": 4, "unconfirmed": 5,
              "stale": 6, "parked": 7, "refuted": 8, "established": 9, "completed": 10}
EVIDENCE_CHECKS = {"KERNEL_CHECKED_PROOF": "kernel", "LLM_CHECKED_PROOF": "model",
                   "CERTIFICATE_CHECKED_RESULT": "certificate", "EXACT_FINITE_RESULT": "certificate",
                   "CHECKED_COUNTEREXAMPLE": "certificate", "HEURISTIC_OBSERVATION": "heuristic"}
BASIS = ["模块与状态来自控制事件、事实库与项目配置的公开记录；页面不重新验证证明。",
         "模块间连线概括已记录的依赖链（前提 → 结论），可能经过未单独展示的中间记录。",
         "中文标题为策展说明，只解释所记录的陈述，不代表验收结论。",
         "数量只描述记录条目，不代表主目标的完成比例。"]


def _compact(value, limit=280):
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def _state(node, blocked):
    kind, status = node.get("kind"), str(node.get("status") or "").upper()
    if node.get("validity") not in (None, "VALID"):
        return "stale"
    if status == "DOCUMENTED":
        return "documented"
    if kind == "fact":
        return {"ACCEPTED": "established", "REVOKED": "refuted"}.get(status, "stale")
    if kind == "route":
        return {"COMPLETED": "completed", "ACTIVE": "attempted", "PROPOSED": "proposed",
                "BLOCKED": "blocked", "PARKED": "parked", "REFUTED": "refuted"}.get(status, "open")
    if kind == "evidence":
        # Scope validity is only registration; a passed check needs an explicit PASS.
        # FAIL is a failed check, not a refutation; UNKNOWN or no checker is unconfirmed.
        return {"PASS": "established", "FAIL": "failed"}.get(str(node.get("checker_result") or "").upper(), "unconfirmed")
    if status == "DISCHARGED":
        return "established"
    if status in {"OPEN", "IN_PROGRESS"} and node["id"] in blocked:
        return "blocked"
    return {"IN_PROGRESS": "attempted", "REFUTED": "refuted", "PARKED": "parked"}.get(status, "open")


class _Graph:
    def __init__(self, graph, proofs=frozenset()):
        self.nodes = {node["id"]: node for node in graph.get("nodes", [])}
        self.proofs = proofs
        self.premises = defaultdict(list)
        self.consumers = defaultdict(list)
        self.evidence = defaultdict(list)
        self.direct = set()
        blocked = set()
        for edge in graph.get("edges", []):
            source, target = edge["source"], edge["target"]
            if source not in self.nodes or target not in self.nodes:
                continue
            if edge["type"] in DEPENDENCIES:
                self.premises[target].append(source)
                self.consumers[source].append(target)
                self.direct.add((source, target))
                if edge["type"] == "BLOCKED_BY" and edge.get("validity", "VALID") == "VALID":
                    blocked.add((source, target))
            elif edge["type"] == "evidence":
                self.evidence[target].append(source)
        self.state = {}
        for key, node in self.nodes.items():
            self.state[key] = _state(node, set())
        blocked_ids = {target for source, target in blocked if self.state[source] != "established"}
        for key, node in self.nodes.items():
            self.state[key] = _state(node, blocked_ids)

    def upstream(self, start, stop=frozenset()):
        seen, stack = {start}, [start]
        while stack:
            for item in self.premises[stack.pop()]:
                if item not in seen:
                    seen.add(item)
                    if item not in stop:
                        stack.append(item)
        return seen

    def downstream(self, start, stop=frozenset()):
        seen, stack = {start}, [start]
        while stack:
            for item in self.consumers[stack.pop()]:
                if item not in seen:
                    seen.add(item)
                    if item not in stop:
                        stack.append(item)
        return seen


def _card(graph, key, layer):
    node = graph.nodes[key]
    return {"id": key, "layer": layer, "kind": node.get("kind"),
            "title": node.get("display_title") or node.get("label") or key,
            "historical_order": node.get("historical_order"), "display_title_i18n": node.get("display_title_i18n"), "curated": bool(node.get("display_title")), "excerpt": _compact(node.get("statement")),
            "status": node.get("status"), "validity": node.get("validity", "VALID"),
            "state": graph.state[key], "depth": node.get("depth", 0),
            "evidence_kind": node.get("evidence_kind"), "checker_result": node.get("checker_result"),
            "has_proof": key in graph.proofs}


def _open_item(graph, key, layer):
    item = _card(graph, key, layer)
    item["frontier"] = all(graph.state[p] == "established" for p in graph.premises[key])
    return item


def _closure(graph, key, layer):
    """Records strictly below ``key`` in the recorded dependency order."""
    members = graph.upstream(key) - {key}
    obligations = Counter(graph.state[k] for k in members if graph.nodes[k].get("kind") == "obligation")
    facts = Counter(graph.state[k] for k in members if graph.nodes[k].get("kind") == "fact")
    checks = Counter()
    evidence = set()
    for member in members | {key}:
        for item in graph.evidence[member]:
            evidence.add(item)
    for item in evidence:
        code = graph.state[item]
        # Only explicit, scope-valid PASS receipts count as checks; others are reported as such.
        checks[EVIDENCE_CHECKS.get(graph.nodes[item].get("evidence_kind"), "other") if code == "established" else code] += 1
    unresolved = [k for k in members if graph.nodes[k].get("kind") in {"obligation", "fact"}
                  and graph.state[k] not in {"established", "parked", "documented"}]
    unresolved.sort(key=lambda k: (not all(graph.state[p] == "established" for p in graph.premises[k]),
                                   STATE_RANK.get(graph.state[k], 9), graph.nodes[k].get("depth", 0), k))
    return {"members": members | evidence | {key},
            "summary": {"obligations": dict(obligations), "facts": dict(facts), "evidence": dict(checks),
                        "routes": sum(graph.nodes[k].get("kind") == "route" for k in members)},
            "open_count": len(unresolved),
            "open_items": [_open_item(graph, k, layer) for k in unresolved[:MAX_OPEN_ITEMS]]}


def _route_card(graph, key, target_id):
    card = _card(graph, key, "research")
    targets = [k for k in graph.consumers[key] if (key, k) in graph.direct]
    requirements = [k for k in graph.premises[key]]
    card.update(targets_main=target_id in targets, target_ids=targets[:3],
                target_titles=[graph.nodes[k].get("display_title") or graph.nodes[k].get("label") or k for k in targets][:3],
                requires=len(requirements), requires_states=dict(Counter(graph.state[k] for k in requirements)),
                evidence=len(graph.evidence[key]))
    return card


def _research_modules(graph, target_id, reading_order):
    curated = [key for key in reading_order if key in graph.nodes and key != target_id
               and graph.nodes[key].get("kind") != "evidence"]
    if curated:
        return list(dict.fromkeys(curated)), [], "curated", []
    routes = [key for key in graph.premises[target_id] if graph.nodes[key].get("kind") == "route"]
    routes.sort(key=lambda k: (ROUTE_RANK.get(str(graph.nodes[k].get("status")), 9), k))
    candidates = [key for key in graph.premises[target_id] if graph.nodes[key].get("kind") != "route"]
    for route in routes:
        candidates += [key for key in graph.premises[route] if graph.nodes[key].get("kind") != "route"]
    candidates = [key for key in dict.fromkeys(candidates) if key != target_id]
    size = {key: len(graph.upstream(key)) for key in candidates}
    order = {key: index for index, key in enumerate(candidates)}
    candidates.sort(key=lambda k: (-size[k], STATE_RANK.get(graph.state[k], 9), order[k]))
    return candidates[:MAX_MODULES], routes[:MAX_ROUTES_IN_MAP], "recorded_premises", candidates[MAX_MODULES:]


def _layout(graph, shown, anchor, routes, ranks, layer):
    """Summarize dependency chains between shown nodes and assign sink-aligned tiers."""
    shown_set = set(shown)
    edges = {}
    for source in shown:
        below = graph.downstream(source, stop=shown_set)
        for target in below & (shown_set - {source}):
            direct = (source, target) in graph.direct
            between = set() if direct else (below & graph.upstream(target, stop=shown_set)) - shown_set
            unresolved = [k for k in between if graph.nodes[k].get("kind") in {"obligation", "fact"}
                          and graph.state[k] not in {"established", "parked", "documented"}]
            unresolved.sort(key=lambda k: (STATE_RANK.get(graph.state[k], 9), graph.nodes[k].get("depth", 0), k))
            edges[(source, target)] = {"source": source, "target": target, "direct": direct,
                                       "via": len(between), "via_open": len(unresolved),
                                       "via_items": [_open_item(graph, k, layer) for k in unresolved[:MAX_CHAIN_ITEMS]]}
    successors = defaultdict(set)
    for source, target in edges:
        successors[source].add(target)
    reach = {}
    for key in shown:
        seen, stack = set(), [key]
        while stack:
            for item in successors[stack.pop()]:
                if item not in seen:
                    seen.add(item)
                    stack.append(item)
        reach[key] = seen
    # Hide a summarized chain already shown through another displayed module.
    # Recorded direct edges stay visible, and routes never absorb a module edge.
    route_set = set(routes)
    for (source, target), edge in list(edges.items()):
        if not edge["direct"] and any(middle not in route_set and middle != target and middle in successors[source]
                                      and target in reach[middle] for middle in shown):
            del edges[(source, target)]
    successors = defaultdict(set)
    for source, target in edges:
        successors[source].add(target)
    height = {}

    def measure(key, trail):
        if key in height:
            return height[key]
        if key in trail:
            return 0
        value = 1 + max((measure(item, trail | {key}) for item in successors[key]), default=-1)
        height[key] = value
        return value

    for key in shown:
        measure(key, frozenset())
    connected = graph.upstream(anchor) if anchor else set(shown)
    top = max(height.values(), default=0)
    tiers = {key: top - height[key] for key in shown}
    if anchor in tiers:
        tiers[anchor] = top
    order = {}
    for tier in sorted(set(tiers.values()), reverse=True):
        members = [key for key in shown if tiers[key] == tier]

        def barycenter(key):
            positions = [order[item] for item in successors[key] if item in order]
            return sum(positions) / len(positions) if positions else len(order)
        members.sort(key=lambda k: (barycenter(k), ranks.get(k, 999), STATE_RANK.get(graph.state[k], 9), k))
        for index, key in enumerate(members):
            order[key] = index
    return list(edges.values()), tiers, order, connected


def _records(project, kind, warnings):
    return _tail_jsonl(project / "global_memory" / f"{kind}.jsonl", project, warnings, limit=RECORD_LIMIT)


def _ladder(project, workspace, research, facts, target_id, warnings):
    attempts = _records(project, "proof_attempt", warnings)
    results = Counter(row.get("result") if row.get("result") in {"success", "partial", "blocked", "abandoned"}
                      else "unspecified" for row in attempts)
    nodes = research.nodes.values()
    routes = Counter(str(node.get("status")) for node in nodes if node.get("kind") == "route")
    evidence = [node for node in nodes if node.get("kind") == "evidence"]
    evidence_states = Counter(research.state[node["id"]] for node in evidence)
    checks = Counter(EVIDENCE_CHECKS.get(node.get("evidence_kind"), "other") for node in evidence
                     if research.state[node["id"]] == "established")
    obligations = Counter(research.state[node["id"]] for node in nodes
                          if node.get("kind") == "obligation" and node["id"] != target_id)
    fact_states = Counter(str(node.get("status")) for node in facts.nodes.values())
    control, acceptance = workspace.get("control", {}), workspace.get("acceptance", {})
    return {
        "routes": {"total": sum(routes.values()), "by_status": dict(routes)},
        "self_reports": {"attempts": len(attempts), "by_result": dict(results),
                         "conclusions": len(_records(project, "conclusion", warnings)),
                         "obstacles": len(_records(project, "obstacle", warnings)),
                         "dead_ends": len(_records(project, "dead_end", warnings)),
                         "counterexamples": len(_records(project, "counterexample", warnings))},
        "accepted_evidence": {"registered": len(evidence),
                              "valid": len(evidence) - evidence_states.get("stale", 0),
                              "passed": evidence_states.get("established", 0),
                              "failed": evidence_states.get("failed", 0),
                              "unconfirmed": evidence_states.get("unconfirmed", 0),
                              "stale": evidence_states.get("stale", 0),
                              "facts_accepted": fact_states.get("accepted", 0),
                              "facts_revoked": fact_states.get("revoked", 0),
                              "facts_unavailable": fact_states.get("missing", 0) + fact_states.get("dependency_unavailable", 0)},
        "checked_components": {"obligations": dict(obligations), "checks": dict(checks)},
        "closure": {"target_status": control.get("target_status") if target_id else None,
                    "outcome": control.get("outcome"),
                    "formal_available": acceptance.get("available") is True,
                    "formal_status": acceptance.get("status"),
                    "formal_goal_achieved": acceptance.get("mathematical_goal_achieved") is True,
                    "formal_audit": (acceptance.get("independent_audit") or {}).get("verdict")},
    }


def _verdict(workspace, target_id):
    control, acceptance = workspace.get("control", {}), workspace.get("acceptance", {})
    formal = acceptance.get("status") == "passed" and acceptance.get("mathematical_goal_achieved") is True
    if control.get("outcome") == "SOLVED":
        code = "closed"
    elif control.get("outcome") == "REFUTED":
        code = "refuted"
    elif formal:
        code = "formal_passed"
    elif target_id:
        code = "open"
    else:
        code = "unregistered"
    if workspace.get("historical_case", {}).get("root_closed"):
        code = "evidence_closed"
    elif workspace.get("historical_case") and code in {"open", "unregistered"}:
        code = "historical"
    return {"code": code, "target_status": control.get("target_status") if target_id else None,
            "outcome": control.get("outcome"), "formal_passed": formal}


def _activity_index(workspace):
    latest = {}
    for entry in workspace.get("activity", []):
        key, when = entry.get("node_id"), _epoch(entry.get("timestamp"))
        if isinstance(key, str) and when is not None and when > latest.get(key, (0, ""))[0]:
            latest[key] = (when, entry.get("kind") or "")
    return latest


def build_architecture(project):
    data = snapshot(project)
    workspace = data["workspace"]
    # "Has proof" means readable source proof text, mirroring the reader's Fact linkage.
    fact_proofs = {key for key, fact in data["facts"].items() if (fact.get("proof") or "").strip()}
    research_proofs = {node["id"] for node in data["research_graph"]["nodes"]
                       if node.get("linked_fact_ids") and node["linked_fact_ids"][-1] in fact_proofs}
    research_proofs = set(research_proofs) | set(data.get("historical_documents", {}))
    research = _Graph(data["research_graph"], research_proofs)
    facts = _Graph(data["facts_graph"], fact_proofs)
    target_id = data["research_graph"].get("target_id")
    target_id = target_id if target_id in research.nodes else None
    reading_order = data["research_graph"].get("reading_order") or []
    warnings = []
    shown, routes, source, omitted, layer, graph = [], [], None, [], None, research
    if target_id:
        shown, routes, source, omitted = _research_modules(research, target_id, reading_order)
        layer = "research"
    if not shown and not routes and facts.nodes:
        # No recorded research structure: top-level Facts are entry points, not a target.
        layer, graph, source = "facts", facts, "top_facts"
        tops = [key for key in facts.nodes if not facts.consumers[key] and facts.nodes[key].get("status") == "accepted"]
        tops.sort(key=lambda k: (-facts.nodes[k].get("depth", 0), k))
        shown, omitted = tops[:MAX_MODULES], tops[MAX_MODULES:]
    anchor = target_id if layer == "research" else None
    members = ([anchor] if anchor else []) + shown + routes
    ranks = {key: index for index, key in enumerate(reading_order)}
    for index, key in enumerate(members):
        ranks.setdefault(key, len(reading_order) + index)
    edges, tiers, order, connected = (_layout(graph, members, anchor, routes, ranks, layer)
                                      if members else ([], {}, {}, set()))
    activity = _activity_index(workspace)
    nodes = []
    for key in members:
        closure = _closure(graph, key, layer)
        card = _card(graph, key, layer)
        latest = max((activity[k] for k in closure["members"] if k in activity), default=None)
        role = "target" if key == anchor else "route" if key in routes else "module"
        card.update(role=role, tier=tiers.get(key, 0), order=order.get(key, 0), connected=key in connected or key == anchor,
                    closure=closure["summary"], open_count=closure["open_count"], open_items=closure["open_items"],
                    direct_premises=len(graph.premises[key]),
                    last_activity_at=latest[0] if latest else None, last_activity_kind=latest[1] if latest else None)
        if role == "route":
            card.update(_route_card(graph, key, anchor))
            card.update(role=role, tier=tiers.get(key, 0), order=order.get(key, 0))
        nodes.append(card)
    route_cards = [_route_card(research, key, target_id) for key in research.nodes
                   if research.nodes[key].get("kind") == "route"]
    route_cards.sort(key=lambda r: (not r["targets_main"], ROUTE_RANK.get(str(r["status"]), 9), r["id"]))
    if workspace.get("historical_case"):
        route_cards.sort(key=lambda r: r.get("historical_order") if r.get("historical_order") is not None else -1)
    path = []
    for key in reading_order:
        if key in research.nodes:
            path.append(_card(research, key, "research"))
        elif key in facts.nodes:
            path.append(_card(facts, key, "facts"))
    target = _card(research, target_id, "research") if target_id else None
    if target:
        target["statement"] = research.nodes[target_id].get("statement", "")
    return {"available": bool(shown or routes), "layer": layer, "source": source,
            "target": target, "goal": workspace["project"].get("goal", ""),
            "nodes": nodes, "edges": edges, "tiers": (max(tiers.values()) + 1) if tiers else 0,
            "omitted": [_card(graph, key, layer) for key in omitted],
            "routes": route_cards, "reading_path": path,
            "ladder": _ladder(project, workspace, research, facts, target_id, warnings),
            "verdict": _verdict(workspace, target_id),
            "historical_case": workspace.get("historical_case"), "basis": BASIS, "warnings": warnings, "updated_at": workspace.get("updated_at")}
