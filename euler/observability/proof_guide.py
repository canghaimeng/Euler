# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Editorial reading guides for existing proofs, pinned to the exact source text.

A guide summarizes the order of an already-written argument and cites exact
source offsets. It is served only while the SHA-256 of the source proof and every
cited anchor still match; any change withdraws it until it is re-read and
re-pinned. A guide is a reading aid: never evidence, review or certification.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

GUIDES_PATH = Path(__file__).with_name("proof_guides.json")
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[tuple, list]] = {}
# "setup" and "argument" steps form the proof line; "scope" and "audit" steps describe
# review scope or bookkeeping written in the same source and are kept off the main line.
ROLES = {"setup", "argument", "scope", "audit"}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_guides(path: Path | None = None) -> list[dict]:
    path = Path(path or os.environ.get("EULER_DASHBOARD_PROOF_GUIDES") or GUIDES_PATH)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return []
    key = (stat.st_mtime_ns, stat.st_size)
    with _LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        guides = value.get("guides", []) if isinstance(value, dict) else []
    except (OSError, ValueError):
        guides = []
    guides = [guide for guide in guides if isinstance(guide, dict)]
    with _LOCK:
        _CACHE[str(path)] = (key, guides)
    return guides


def _cut_ok(proof: str, offset: int, section: dict) -> bool:
    """A cited boundary may not split a fenced block, display math or inline math."""
    if offset in (section["start"], section["end"]):
        return True
    if not (proof[offset - 1].isspace() or proof[offset].isspace()):
        return False
    before = proof[section["start"]:offset]
    if before.count("```") % 2 or before.count("~~~") % 2 or before.count("$$") % 2:
        return False
    paragraph = before[before.rfind("\n\n") + 2:] if "\n\n" in before else before
    return paragraph.replace("$$", "").count("$") % 2 == 0


def _text(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {lang: value[lang] for lang in ("zh", "en") if isinstance(value.get(lang), str) and value[lang].strip()}
    return result if len(result) == 2 else None


def validate(guide: dict, proof: str, sections: list[dict]) -> dict:
    """Return the guide bound to this exact source, or a withdrawal notice."""
    source = digest(proof)
    base = {"project": guide.get("project"), "fact_id": guide.get("fact_id"), "source_sha256": source,
            "guide_sha256": guide.get("proof_sha256")}
    if guide.get("proof_sha256") != source:
        return {**base, "status": "withdrawn", "reason": "source_changed"}
    steps = []
    for step in guide.get("steps", []):
        text = _text(step.get("text"))
        cites = []
        for cite in step.get("cite", []) if isinstance(step.get("cite"), list) else []:
            start, end, quote = cite.get("start"), cite.get("end"), cite.get("quote")
            if not (isinstance(start, int) and isinstance(end, int) and isinstance(quote, str) and quote
                    and 0 <= start < end <= len(proof) and proof[start:start + len(quote)] == quote
                    and len(quote) <= end - start):
                return {**base, "status": "withdrawn", "reason": "anchor_mismatch"}
            section = next((s for s in sections if s["start"] <= start < s["end"]), None)
            if section is None or end > section["end"] or not (_cut_ok(proof, start, section) and _cut_ok(proof, end, section)):
                return {**base, "status": "withdrawn", "reason": "anchor_mismatch"}
            cites.append({"section": section["index"], "start": start, "end": end, "quote": quote,
                          "whole": start == section["start"] and end == section["end"]})
        if text is None or not cites:
            return {**base, "status": "withdrawn", "reason": "anchor_mismatch"}
        refs = [ref for ref in step.get("refs", []) if isinstance(ref, str) and ref in proof]
        role = step.get("role") if step.get("role") in ROLES else "argument"
        # A short bilingual heading for the step; editorial like the text, never a source quotation.
        steps.append({"text": text, "title": _text(step.get("title")), "cites": cites, "refs": refs, "role": role})
    if not steps:
        return {**base, "status": "withdrawn", "reason": "anchor_mismatch"}
    return {**base, "status": "editorial", "summary": _text(guide.get("summary")), "steps": steps,
            "authored": guide.get("authored")}


def guide_for(project_id: str | None, fact_id: str | None, proof: str, sections: list[dict],
              guides: list[dict] | None = None) -> dict | None:
    if not (project_id and fact_id and proof.strip()):
        return None
    for guide in load_guides() if guides is None else guides:
        if guide.get("project") == project_id and guide.get("fact_id") == fact_id:
            return validate(guide, proof, sections)
    return None
