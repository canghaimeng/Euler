"""Read-only snapshot of an Euler project's facts, memory, and round receipts.

May import core read helpers/constants. Must not call mutation APIs, gateway,
verifier, execution control, or append research-control events.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from euler.core.factgraph import parse_frontmatter, statement_of
from euler.core.schema import GLOBAL_KINDS, clean_external_refs

from ..ids import canonical_json, sha256_hex

_FACTS_DIR = Path("fact_graph") / "facts"
_REVOKED_DIR = Path("fact_graph") / "_revoked"
_MEMORY_DIR = Path("global_memory")
_RECEIPTS_DIR = Path("receipts")
_REVOCATION_LOG = Path("fact_graph") / "revocation_log.jsonl"
_FRONTMATTER_FACT_ID_RE = re.compile(r"^fact_id:\s*([^\s]+)\s*$", re.MULTILINE)

__all__ = [
    "ProjectAdapterError",
    "FactSnapshot",
    "MemoryRecord",
    "ReceiptSnapshot",
    "ProjectSnapshot",
    "snapshot_project",
]


class ProjectAdapterError(ValueError):
    """Raised when an Euler project snapshot cannot be read strictly."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FactSnapshot:
    fact_id: str
    relative_path: str
    raw_markdown: str
    content_sha256: str
    statement: str
    predecessors: tuple[str, ...]
    external_refs: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryRecord:
    kind: str
    record_id: str
    relative_path: str
    canonical_json: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptSnapshot:
    relative_path: str
    canonical_json: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectSnapshot:
    active_facts: tuple[FactSnapshot, ...]
    revoked_fact_ids: tuple[str, ...]
    memory_records: tuple[MemoryRecord, ...]
    receipts: tuple[ReceiptSnapshot, ...]


def snapshot_project(project_dir: str | Path) -> ProjectSnapshot:
    """Return a deterministic read-only snapshot. Never writes or appends."""
    root = Path(project_dir)
    if not root.is_dir():
        raise ProjectAdapterError(f"project directory does not exist: {root}")
    root = root.resolve()
    active_facts = tuple(_read_active_facts(root))
    revoked_fact_ids = tuple(_read_revoked_ids(root))
    _validate_fact_graph_snapshot(active_facts, revoked_fact_ids)
    return ProjectSnapshot(
        active_facts=active_facts,
        revoked_fact_ids=revoked_fact_ids,
        memory_records=tuple(_read_memory(root)),
        receipts=tuple(_read_receipts(root)),
    )


def _read_active_facts(root: Path) -> list[FactSnapshot]:
    facts_dir = root / _FACTS_DIR
    if not facts_dir.is_dir():
        return []
    snapshots: list[FactSnapshot] = []
    for path in sorted(facts_dir.glob("*.md"), key=lambda p: p.name):
        rel = _project_relative(root, path)
        raw = _read_bytes(path)
        try:
            markdown = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectAdapterError(f"fact is not UTF-8: {rel}") from exc
        parsed = parse_frontmatter(markdown)
        match = _FRONTMATTER_FACT_ID_RE.search(markdown)
        if match is None or match.group(1) != path.stem:
            raise ProjectAdapterError(
                f"fact frontmatter id does not match filename: {rel}"
            )
        predecessors = tuple(str(item) for item in parsed.get("predecessors") or [])
        refs = tuple(
            _deep_freeze(ref) for ref in clean_external_refs(parsed.get("external_refs"))
        )
        snapshots.append(
            FactSnapshot(
                fact_id=path.stem,
                relative_path=rel,
                raw_markdown=markdown,
                content_sha256=sha256_hex(raw),
                statement=statement_of(markdown),
                predecessors=predecessors,
                external_refs=refs,
            )
        )
    snapshots.sort(key=lambda item: (item.fact_id, item.relative_path))
    return snapshots


def _read_revoked_ids(root: Path) -> list[str]:
    revoked_dir = root / _REVOKED_DIR
    ids: set[str] = set()
    if revoked_dir.is_dir():
        for path in revoked_dir.glob("*.md"):
            _project_relative(root, path)
            ids.add(path.stem)
    log = root / _REVOCATION_LOG
    if log.exists():
        if not log.is_file():
            raise ProjectAdapterError("revocation log is not a file")
        rel = _project_relative(root, log)
        for payload in _read_jsonl_objects(log, rel):
            fact_id = payload.get("fact_id")
            if not isinstance(fact_id, str) or not fact_id:
                raise ProjectAdapterError(
                    f"revocation record missing fact_id in {rel}"
                )
            ids.add(fact_id)
    return sorted(ids)


def _validate_fact_graph_snapshot(
    active_facts: tuple[FactSnapshot, ...],
    revoked_fact_ids: tuple[str, ...],
) -> None:
    active_ids = {fact.fact_id for fact in active_facts}
    revoked_ids = set(revoked_fact_ids)
    overlap = active_ids & revoked_ids
    if overlap:
        raise ProjectAdapterError(
            f"fact ids are both active and revoked: {sorted(overlap)}"
        )
    for fact in active_facts:
        missing = [pred for pred in fact.predecessors if pred not in active_ids]
        if missing:
            raise ProjectAdapterError(
                f"active fact {fact.fact_id} has inactive/dangling predecessors: "
                f"{missing}"
            )


def _read_memory(root: Path) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    for kind in sorted(GLOBAL_KINDS):
        path = root / _MEMORY_DIR / f"{kind}.jsonl"
        if not path.exists():
            continue
        if not path.is_file():
            raise ProjectAdapterError(f"global memory channel is not a file: {kind}")
        rel = _project_relative(root, path)
        for payload in _read_jsonl_objects(path, rel):
            record_id = payload.get("id")
            if not isinstance(record_id, str) or record_id == "":
                raise ProjectAdapterError(f"memory record missing id in {rel}")
            declared = payload.get("kind")
            if declared is not None and declared != kind:
                raise ProjectAdapterError(
                    f"memory record kind {declared!r} does not match channel {kind!r}"
                )
            try:
                encoded = canonical_json(payload)
            except (TypeError, ValueError) as exc:
                raise ProjectAdapterError(
                    f"memory record is not JSON-safe in {rel}"
                ) from exc
            records.append(
                MemoryRecord(
                    kind=kind,
                    record_id=record_id,
                    relative_path=rel,
                    canonical_json=encoded,
                )
            )
    records.sort(key=lambda item: (item.kind, item.record_id, item.canonical_json))
    return records


def _read_receipts(root: Path) -> list[ReceiptSnapshot]:
    receipts_root = root / _RECEIPTS_DIR
    if not receipts_root.is_dir():
        return []
    paths = (
        list(receipts_root.glob("*/*.start.json"))
        + list(receipts_root.glob("*/*.end.json"))
        + list(receipts_root.glob("*/*.launch.json"))
        + list(receipts_root.glob("*/*.result.json"))
    )
    snapshots: list[ReceiptSnapshot] = []
    for path in paths:
        if not path.is_file():
            continue
        rel = _project_relative(root, path)
        try:
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
        except UnicodeDecodeError as exc:
            raise ProjectAdapterError(f"receipt is not UTF-8: {rel}") from exc
        except json.JSONDecodeError as exc:
            raise ProjectAdapterError(f"malformed receipt JSON: {rel}") from exc
        if not isinstance(payload, dict):
            raise ProjectAdapterError(f"receipt JSON must be an object: {rel}")
        try:
            encoded = canonical_json(payload)
        except (TypeError, ValueError) as exc:
            raise ProjectAdapterError(f"receipt is not JSON-safe: {rel}") from exc
        snapshots.append(
            ReceiptSnapshot(relative_path=rel, canonical_json=encoded)
        )
    snapshots.sort(key=lambda item: item.relative_path)
    return snapshots


def _read_jsonl_objects(path: Path, rel: str) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectAdapterError(f"JSONL is not UTF-8: {rel}") from exc
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip() == "":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProjectAdapterError(
                f"malformed JSONL {rel}:{lineno}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProjectAdapterError(f"non-object JSONL {rel}:{lineno}")
        records.append(payload)
    return records


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProjectAdapterError(f"cannot read {path}: {exc}") from exc


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _project_relative(root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ProjectAdapterError(f"path escapes project: {path}") from exc
    return relative.as_posix()
