"""Source-grounded proof reading, without synthesis or inferred proof edges."""
import re
from .monitor import snapshot
from .proof_guide import digest, guide_for

DEPENDENCIES = {"requires", "depends_on", "REQUIRES", "BLOCKED_BY", "targets"}
RELATIONS = {"requires": "需要这些前提", "REQUIRES": "依赖前提", "depends_on": "依赖",
             "BLOCKED_BY": "受此问题阻塞", "targets": "用于完成此目标", "evidence": "验收证据",
             "REDUCES_TO": "归约关系", "EQUIVALENT_UNDER": "条件等价",
             "SUSPECTED_SAME": "待确认的相似关系", "REFUTED_BY": "反驳记录", "DESCENDS_BY": "递降关系"}


def compact(value, limit=220):
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def proof_sections(proof):
    """Slice original proof text, preserving offsets and fenced code/math blocks."""
    if not proof.strip():
        return []
    starts = [0]
    fence = False
    for match in re.finditer(r"(?m)^.*(?:\n|$)", proof):
        line = match.group()
        if re.match(r"^\s*(```|~~~)", line):
            fence = not fence
        if not fence and re.match(r"^#{1,6}\s+\S", line) and match.start() > 0:
            starts.append(match.start())
    # Without headings, individual paragraphs are reading units, not new lemmas.
    if len(starts) == 1 and len(proof) > 1400:
        fence = False
        math = False
        offset = 0
        for line in proof.splitlines(keepends=True):
            if re.match(r"^\s*(```|~~~)", line):
                fence = not fence
            if not fence and line.strip() in {"$$", r"\[", r"\]"}:
                math = not math
            if not fence and not math and not line.strip() and offset - starts[-1] > 300:
                starts.append(offset + len(line))
            offset += len(line)
    starts = sorted(set(x for x in starts if x < len(proof)))
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(proof)
        source = proof[start:end]
        if not source.strip():
            continue
        heading = re.match(r"^\s*#{1,6}\s+([^\n]+)", source)
        # "heading" is the exact source heading; untitled units get only a display ordinal.
        sections.append({"index": len(sections), "title": heading.group(1) if heading else f"原文第 {len(sections)+1} 段",
                         "heading": heading.group(1) if heading else None, "ordinal": len(sections) + 1,
                         "text": source, "start": start, "end": end})
    return sections


def build_derivation(project, layer, node_id, project_id=None):
    data = snapshot(project)
    if layer == "research" and node_id in data.get("historical_documents", {}):
        from .historical_projection import derivation
        return derivation(data, node_id)
    graph = data[layer + "_graph"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    if node_id not in nodes:
        return None
    selected = nodes[node_id]
    def card(identifier):
        n = nodes[identifier]
        return {"id": identifier, "title": n.get("display_title") or n.get("label") or identifier,
                "summary": compact(n.get("statement")), "kind": n.get("kind"),
                "status": n.get("status"), "validity": n.get("validity", "VALID"),
                "evidence_kind": n.get("evidence_kind"), "checker_result": n.get("checker_result")}
    incoming = [e for e in graph["edges"] if e["target"] == node_id]
    outgoing = [e for e in graph["edges"] if e["source"] == node_id]
    routes = []
    for edge in incoming:
        if edge["type"] == "targets" and nodes[edge["source"]].get("kind") == "route":
            r = card(edge["source"])
            r["premises"] = [card(e["source"]) for e in graph["edges"]
                             if e["target"] == r["id"] and e["type"] in DEPENDENCIES]
            r["mechanism"] = nodes[r["id"]].get("statement", "")
            routes.append(r)
    fact = data["facts"].get(node_id, {}) if layer == "facts" or selected.get("kind") == "fact" else {}
    linked = selected.get("linked_fact_ids", [])
    if not fact and linked:
        fact = data["facts"].get(linked[-1], {})
    proof = fact.get("proof", "")
    premises = [dict(card(e["source"]), relation=e["type"], relation_label=RELATIONS.get(e["type"], e["type"]),
                     rationale=e.get("exact_claim", ""), relation_validity=e.get("validity", "VALID"))
                for e in incoming if e["type"] in DEPENDENCIES and e["type"] != "targets"]
    evidence = [card(e["source"]) for e in incoming if e["type"] == "evidence"]
    related = [dict(card(e["target"] if e["source"] == node_id else e["source"]),
                    relation=e["type"], relation_label=RELATIONS.get(e["type"], e["type"]),
                    rationale=e.get("exact_claim", "")) for e in incoming + outgoing
               if e["type"] not in DEPENDENCIES | {"evidence"}]
    sections = proof_sections(proof)
    return {"node": dict(card(node_id), statement=fact.get("statement") or selected.get("statement", "")),
            "premises": premises, "routes": routes, "evidence": evidence, "related": related,
            "next": [card(e["target"]) for e in outgoing if e["type"] in DEPENDENCIES],
            "sections": sections, "proof": proof, "intuition": fact.get("intuition", ""),
            "mechanism": selected.get("statement", "") if selected.get("kind") == "route" else "",
            "source_kind": "fact_proof" if proof else "recorded_relations", "proof_source_fact_id": fact.get("id"),
            "proof_sha256": digest(proof) if proof else None,
            "process": guide_for(project_id, fact.get("id"), proof, sections) if proof else None,
            "gap_code": None if proof else "no_proof_text",
            "gap": None if proof else "该节点未附逐步证明正文；下面展示已记录的依赖、路线和证据。"}
