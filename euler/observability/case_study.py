# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Read-only, source-bound editorial maps of historical research artifacts.

These maps are not Facts, control events, or inferred verification results.
Only a server-owned manifest can choose the frozen source files served here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MAX_MANIFEST = 2 * 1024 * 1024
MAX_SOURCE = 2 * 1024 * 1024


def _read(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("invalid case-study source")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("case-study source is too large")
    return data


def _manifest() -> tuple[dict, Path] | None:
    configured = os.environ.get("EULER_CASE_STUDIES_MANIFEST")
    if not configured:
        return None
    path = Path(configured).expanduser().absolute()
    data = json.loads(_read(path, MAX_MANIFEST))
    if data.get("schema_version") != 1 or not isinstance(data.get("cases"), list):
        raise ValueError("invalid case-study manifest")
    return data, path.parent.resolve()


def _source(source: dict, root: Path, *, include_text: bool = False) -> dict:
    public = {k: source.get(k) for k in ("id", "label", "sha256", "original_sha256", "locator", "format")}
    public["integrity"] = "unavailable"
    try:
        rel = Path(source["file"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("source must be contained")
        path = root / rel
        if any((root.joinpath(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts) + 1)):
            raise ValueError("symlink source")
        path.resolve().relative_to(root)
        blob = _read(path, MAX_SOURCE)
        if hashlib.sha256(blob).hexdigest() != source.get("sha256"):
            public["integrity"] = "changed"
        else:
            public["integrity"] = "matched"
            if include_text:
                public["text"] = blob.decode("utf-8")
    except (OSError, ValueError, KeyError, UnicodeError):
        pass
    return public


def list_cases() -> dict:
    loaded = _manifest()
    if loaded is None:
        return {"edition": None, "cases": []}
    data, _root = loaded
    return {"edition": data.get("edition"), "provenance": data.get("provenance"),
            "cases": [{k: case.get(k) for k in ("id", "title", "field", "headline")}
                      for case in data["cases"]]}


def get_case(identifier: str) -> dict | None:
    loaded = _manifest()
    if loaded is None:
        return None
    data, root = loaded
    case = next((c for c in data["cases"] if c.get("id") == identifier), None)
    if case is None:
        return None
    sources = data.get("sources", {})
    used = set(case.get("outcome_sources", []))
    for item in case.get("nodes", []) + case.get("edges", []) + case.get("story", []) + case.get("stages", []):
        used.update(item.get("sources", []))
        used.update(item.get("verification_binding", {}).get("sources", []))
    records = {sid: _source(sources[sid], root) for sid in used if sid in sources}
    valid = len(records) == len(used) and all(s["integrity"] == "matched" for s in records.values())
    return {**case, "edition": data.get("edition"), "projection": "editorial_source_map",
            "source_integrity": "matched" if valid else "withdrawn",
            "sources": records}


def get_source(case_id: str, source_id: str) -> dict | None:
    loaded = _manifest()
    if loaded is None:
        return None
    data, root = loaded
    case = next((c for c in data["cases"] if c.get("id") == case_id), None)
    if case is None:
        return None
    used = set(case.get("outcome_sources", []))
    for item in case.get("nodes", []) + case.get("edges", []) + case.get("story", []) + case.get("stages", []):
        used.update(item.get("sources", []))
        used.update(item.get("verification_binding", {}).get("sources", []))
    if source_id not in used or source_id not in data.get("sources", {}):
        return None
    return _source(data["sources"][source_id], root, include_text=True)
