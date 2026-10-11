"""Live public exploration signals, including tasks without any accepted proof."""
from .monitor import snapshot, _tail_jsonl, _text, _iso

SIGNALS = ("plan", "direction", "proof_attempt", "obstacle", "dead_end", "counterexample", "conclusion", "example")


def build_exploration(project):
    data = snapshot(project)
    graph = data["research_graph"]
    warnings = []
    signals = []
    for kind in SIGNALS:
        for row in _tail_jsonl(project / "global_memory" / f"{kind}.jsonl", project, warnings, limit=80):
            signals.append({"id": _text(row.get("id")), "kind": kind,
                            "claim": _text(row.get("claim"), 400),
                            "detail": _text(row.get("evidence"), 10000),
                            "author": _text(row.get("author")), "timestamp": _iso(row.get("timestamp_utc")),
                            "verification": "public_research_record",
                            "attempt_result": row.get("result") if row.get("result") in ("success", "partial", "blocked", "abandoned") else None,
                            "reason": _text(row.get("reason"), 1200)})
    event_labels = {"runtime.attempt_started": "执行轮次开始", "runtime.attempt_finished": "执行轮次结束",
                    "runtime.lease_infra_failed": "执行遇到运行问题", "failure.recorded": "新问题已记录",
                    "obligation.status_changed": "证明义务状态更新", "evidence.registered": "新的验收证据已登记",
                    "route.created": "新研究路线已登记", "route.status_changed": "研究路线状态更新"}
    for entry in data["workspace"]["activity"]:
        if entry["kind"] in event_labels:
            # "event"/"subject" let a client localize the label; "claim" keeps the original combined text.
            signals.append({"id": entry["id"], "kind": "evidence" if entry["kind"] == "evidence.registered" else "execution",
                            "claim": event_labels[entry["kind"]] + (" · " + entry["title"] if entry.get("node_id") else ""),
                            "event": entry["kind"], "subject": entry["title"] if entry.get("node_id") else "",
                            "detail": entry.get("detail", ""), "author": entry["author"],
                            "timestamp": entry["timestamp"], "node_id": entry.get("node_id"), "verification": "public_control_event"})
    for fact in data["facts_graph"]["nodes"]:
        if fact["status"] == "accepted":
            signals.append({"id": "fact:" + fact["id"], "kind": "fact", "claim": fact.get("display_title") or fact["label"],
                            "detail": fact.get("statement", ""), "author": fact.get("author", ""),
                            "timestamp": fact.get("created_at"), "node_id": fact["id"], "verification": "fact_store"})
    for stage in reversed(data.get("historical_stages", [])):
        signals.append({"id":stage["node_id"],"node_id":stage["node_id"],"kind":"direction",
                        "claim":stage["title"]["en"],"claim_i18n":stage["title"],
                        "detail":stage["description"]["en"],"detail_i18n":stage["description"],
                        "author":"Historical source records","timestamp":None,"date_label":stage["date"],"verification":"historical_source"})
    signals.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    signals.sort(key=lambda x: x.get("verification") != "historical_source")
    branches = [n for n in graph["nodes"] if n.get("kind") == "route"]
    branches.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    branches.sort(key=lambda n: n["status"] not in {"ACTIVE", "PROPOSED", "BLOCKED"})
    blockers = [n for n in graph["nodes"] if n.get("kind") == "obligation" and n["status"] in {"BLOCKED", "OPEN"}]
    workers = [dict(w, team_name=t["name"]) for t in data["workspace"]["teams"] for w in t["workers"]]
    return {"phase": "complete" if data["workspace"]["control"].get("outcome") in {"SOLVED", "REFUTED"} or (data["workspace"]["acceptance"].get("status") == "passed" and data["workspace"]["acceptance"].get("mathematical_goal_achieved") is True) else "exploring",
            "project": data["workspace"]["project"], "control": data["workspace"]["control"],
            "workers": workers, "coordinators": [t["coordinator"] for t in data["workspace"]["teams"] if t.get("coordinator")], "branches": branches[:24], "branch_count": len(branches),
            "blockers": blockers[:30], "open_count": len(blockers), "signals": signals[:80],
            "facts": data["facts_graph"]["nodes"][-12:],
            "updated_at": data["workspace"]["updated_at"], "warnings": warnings,
            "basis": "公开的任务、研究记录与验收状态；未验证尝试不等于已证明结论。"}
