# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Mandatory orchestrator memory retrieval, bounded digest, and round receipts.

Before every ``run_round`` (after proving preflight) the loop — not the model —
retrieves a bounded slice of shared global memory, writes an immutable start
receipt, verifies it, and injects the digest + path/hash into the kickoff
prompt. After the round, a companion end receipt records the end watermark and
IDs appended during the round (including sibling writes). The start payload and
``digest_hash`` are never rewritten.

This is transport/wiring, not a mathematical verdict. Failure to create, persist,
or verify a start receipt is ``RECEIPT_FAILED`` and must not launch codex.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from euler.core import GlobalMemory
from euler.core._util import utc_now

from . import layout as L

SCHEMA_VERSION = 2

RETRIEVAL_KINDS: Tuple[str, ...] = (
    "proof_attempt",
    "obstacle",
    "dead_end",
    "counterexample",
    "verification",
)

DEFAULT_TOP_PER_KIND = 5
DEFAULT_TOTAL_CAP = 16
CLAIM_SNIPPET = 200
QUERY_LIMIT = 2000
DEFAULT_PROMPT_CHAR_CAP = 8000
DEFAULT_PROMPT_BYTE_CAP = 12000
_HASH_PLACEHOLDER = "0" * 64
_RECEIPT_NAME_RE = re.compile(r"^round_(\d+)\.(start|end)\.json$")

_PROVENANCE_RANK = {"exact_subgoal": 0, "exact_branch": 1, "bm25": 2}

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z",
    re.DOTALL,
)

_META_ALIASES = {
    "project_id": "project_id",
    "project": "project_id",
    "branch_id": "branch_id",
    "branch": "branch_id",
    "subgoal_id": "subgoal_id",
    "subgoal": "subgoal_id",
}


class RoundReceiptError(Exception):
    """Start, launch, result, or end receipt could not be created, persisted, or verified."""

    def __init__(self, phase: str, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.phase = phase
        self.reason = reason
        self.details = details

    def to_status_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "ok": False,
            **self.details,
        }


@dataclass(frozen=True)
class TaskContext:
    """Parsed TASK.md. IDs are optional in stage 2; plain files stay valid."""

    project_id: Optional[str] = None
    branch_id: Optional[str] = None
    subgoal_id: Optional[str] = None
    body: str = ""
    raw: str = ""
    source: str = "plain"  # plain | frontmatter

    def as_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch_id": self.branch_id,
            "subgoal_id": self.subgoal_id,
            "source": self.source,
        }


@dataclass
class StartReceipt:
    path: Path
    payload: Dict[str, Any]
    digest: Dict[str, Any]
    digest_hash: str
    receipt_hash: str
    round_id: int
    task: TaskContext = field(default_factory=TaskContext)


def top_per_kind(override: Optional[int] = None) -> int:
    if override is not None:
        return max(1, int(override))
    raw = os.environ.get("EULER_MEMORY_TOP_PER_KIND")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_TOP_PER_KIND


def total_cap(override: Optional[int] = None) -> int:
    if override is not None:
        return max(1, int(override))
    raw = os.environ.get("EULER_MEMORY_TOTAL_CAP")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_TOTAL_CAP


def prompt_char_cap(override: Optional[int] = None) -> int:
    if override is not None:
        return max(200, int(override))
    raw = os.environ.get("EULER_MEMORY_PROMPT_CHAR_CAP")
    if raw:
        try:
            return max(200, int(raw))
        except ValueError:
            pass
    return DEFAULT_PROMPT_CHAR_CAP


def prompt_byte_cap(override: Optional[int] = None) -> int:
    if override is not None:
        return max(200, int(override))
    raw = os.environ.get("EULER_MEMORY_PROMPT_BYTE_CAP")
    if raw:
        try:
            return max(200, int(raw))
        except ValueError:
            pass
    return DEFAULT_PROMPT_BYTE_CAP


# --------------------------------------------------------------------------- #
# TASK.md metadata (optional; does not break plain assignments)                #
# --------------------------------------------------------------------------- #

def parse_task_text(text: str) -> TaskContext:
    raw = text or ""
    m = _FRONTMATTER_RE.match(raw)
    meta: Dict[str, Optional[str]] = {
        "project_id": None, "branch_id": None, "subgoal_id": None,
    }
    if not m:
        return TaskContext(body=raw.strip("\n"), raw=raw, source="plain")
    fm, body = m.group(1), m.group(2)
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        mapped = _META_ALIASES.get(key.strip())
        if mapped is None:
            continue
        cleaned = val.strip().strip("'").strip('"')
        meta[mapped] = cleaned or None
    return TaskContext(
        project_id=meta["project_id"],
        branch_id=meta["branch_id"],
        subgoal_id=meta["subgoal_id"],
        body=body.strip("\n"),
        raw=raw,
        source="frontmatter",
    )


def load_task_context(wl: L.WorkerLayout) -> TaskContext:
    if not wl.task.is_file():
        return TaskContext(project_id=wl.project, source="plain")
    try:
        text = wl.task.read_text(encoding="utf-8")
    except OSError:
        return TaskContext(project_id=wl.project, source="plain")
    ctx = parse_task_text(text)
    if not ctx.project_id:
        return TaskContext(
            project_id=wl.project,
            branch_id=ctx.branch_id,
            subgoal_id=ctx.subgoal_id,
            body=ctx.body,
            raw=ctx.raw,
            source=ctx.source,
        )
    return ctx


# --------------------------------------------------------------------------- #
# watermarks (append-only: counts + last id + file digest)                     #
# --------------------------------------------------------------------------- #

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_raw(path: Path) -> bytes:
    if not path.is_file():
        return b""
    return path.read_bytes()


def _ids_from_jsonl_bytes(raw: bytes) -> List[str]:
    ids: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            ids.append(payload["id"])
    return ids


def _ids_sha256(ids: Sequence[str]) -> str:
    return _sha256_bytes(json.dumps(list(ids), separators=(",", ":")).encode("utf-8"))


def memory_watermark(project_dir: Path, kinds: Sequence[str] = RETRIEVAL_KINDS) -> Dict[str, Any]:
    """Append-only snapshot: byte_size, full/prefix digest, ID count/hash. Not a global integer."""
    gm = GlobalMemory(project_dir)
    kinds_out: Dict[str, Any] = {}
    for kind in kinds:
        path = gm._path(kind)
        raw = _read_raw(path)
        ids = _ids_from_jsonl_bytes(raw)
        digest = _sha256_bytes(raw)
        kinds_out[kind] = {
            "count": len(ids),
            "last_id": ids[-1] if ids else None,
            "byte_size": len(raw),
            "file_sha256": digest,
            "prefix_sha256": digest,  # at start, prefix == whole file
            "ids_sha256": _ids_sha256(ids),
        }
    return {"kinds": kinds_out}


# --------------------------------------------------------------------------- #
# retrieval                                                                    #
# --------------------------------------------------------------------------- #

def _link_val(entry: Mapping[str, Any], *keys: str) -> Optional[str]:
    links = entry.get("links") if isinstance(entry.get("links"), dict) else {}
    for key in keys:
        for src in (links, entry):
            val = src.get(key) if isinstance(src, dict) else None
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _entry_branch(entry: Mapping[str, Any]) -> Optional[str]:
    return _link_val(entry, "branch_id", "branch")


def _entry_subgoal(entry: Mapping[str, Any]) -> Optional[str]:
    return _link_val(entry, "subgoal_id", "subgoal")


def _snippet(claim: Any) -> str:
    text = claim if isinstance(claim, str) else ""
    text = " ".join(text.split())
    if len(text) <= CLAIM_SNIPPET:
        return text
    return text[: CLAIM_SNIPPET - 1] + "…"


def _hit(entry: Mapping[str, Any], kind: str, provenance: str, score: Optional[float]) -> Dict[str, Any]:
    rounded: Optional[float]
    if score is None:
        rounded = None
    else:
        rounded = round(float(score), 6)
    return {
        "id": entry.get("id"),
        "kind": kind,
        "score": rounded,
        "provenance": provenance,
        "claim_snippet": _snippet(entry.get("claim")),
        "author": entry.get("author"),
        "timestamp_utc": entry.get("timestamp_utc"),
    }


def _exact_hits(entries: Sequence[Mapping[str, Any]], kind: str, task: TaskContext,
                limit: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set = set()
    if task.subgoal_id:
        for e in entries:
            eid = e.get("id")
            if not isinstance(eid, str) or eid in seen:
                continue
            if _entry_subgoal(e) == task.subgoal_id:
                out.append(_hit(e, kind, "exact_subgoal", None))
                seen.add(eid)
                if len(out) >= limit:
                    return out
    if task.branch_id and len(out) < limit:
        for e in entries:
            eid = e.get("id")
            if not isinstance(eid, str) or eid in seen:
                continue
            if _entry_branch(e) == task.branch_id:
                out.append(_hit(e, kind, "exact_branch", None))
                seen.add(eid)
                if len(out) >= limit:
                    return out
    return out


def _apply_total_cap(hits_by_kind: Dict[str, List[Dict[str, Any]]], cap: int
                     ) -> Dict[str, List[Dict[str, Any]]]:
    flat: List[Dict[str, Any]] = []
    for kind in RETRIEVAL_KINDS:
        flat.extend(hits_by_kind.get(kind) or [])
    if len(flat) <= cap:
        return hits_by_kind

    def sort_key(h: Dict[str, Any]) -> Tuple:
        prov = _PROVENANCE_RANK.get(str(h.get("provenance")), 9)
        score = h.get("score")
        score_v = float("inf") if score is None else float(score)
        kind = str(h.get("kind") or "")
        kind_i = RETRIEVAL_KINDS.index(kind) if kind in RETRIEVAL_KINDS else 99
        return (prov, -score_v, kind_i, str(h.get("id") or ""))

    kept_ids = {h["id"] for h in sorted(flat, key=sort_key)[:cap]}
    return {
        kind: [h for h in (hits_by_kind.get(kind) or []) if h.get("id") in kept_ids]
        for kind in RETRIEVAL_KINDS
    }


def retrieve_bounded(
    project_dir: Path,
    task: TaskContext,
    *,
    per_kind: Optional[int] = None,
    cap: Optional[int] = None,
) -> Dict[str, Any]:
    """Level-1 exact branch/subgoal, then BM25 fill. Dedupe. Never full history."""
    limit = top_per_kind(per_kind)
    cap_n = total_cap(cap)
    gm = GlobalMemory(project_dir)
    query = (task.body or task.raw or "").strip()
    if len(query) > QUERY_LIMIT:
        query = query[:QUERY_LIMIT]
    hits_by_kind: Dict[str, List[Dict[str, Any]]] = {}
    queries: Dict[str, Any] = {}
    for kind in RETRIEVAL_KINDS:
        entries = gm.read(kind)
        selected = _exact_hits(entries, kind, task, limit)
        seen = {h["id"] for h in selected}
        if len(selected) < limit and query:
            ranked = gm.search(query, kinds=[kind], limit_per_kind=limit * 3)
            bucket = (ranked.get("results_by_kind") or {}).get(kind) or {}
            for row in bucket.get("results") or []:
                entry = row.get("entry") or {}
                eid = entry.get("id")
                if not isinstance(eid, str) or eid in seen:
                    continue
                score = row.get("score")
                if not isinstance(score, (int, float)) or score <= 0:
                    continue
                selected.append(_hit(entry, kind, "bm25", float(score)))
                seen.add(eid)
                if len(selected) >= limit:
                    break
        hits_by_kind[kind] = selected
        queries[kind] = {
            "exact_branch_id": task.branch_id,
            "exact_subgoal_id": task.subgoal_id,
            "bm25_query": query,
        }
    hits_by_kind = _apply_total_cap(hits_by_kind, cap_n)
    return {
        "hits_by_kind": hits_by_kind,
        "queries": queries,
        "top_per_kind": limit,
        "total_cap": cap_n,
        "query": query,
    }


def build_digest(
    *,
    project_id: str,
    worker_id: str,
    round_id: int,
    task: TaskContext,
    retrieval: Mapping[str, Any],
    run_id: str = "",
) -> Dict[str, Any]:
    kinds_out: Dict[str, List[Dict[str, Any]]] = {}
    for kind in RETRIEVAL_KINDS:
        kinds_out[kind] = [
            {
                "id": h.get("id"),
                "score": h.get("score"),
                "provenance": h.get("provenance"),
                "claim_snippet": h.get("claim_snippet"),
                "author": h.get("author"),
            }
            for h in (retrieval.get("hits_by_kind") or {}).get(kind) or []
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "worker_id": worker_id,
        "round_id": round_id,
        "run_id": run_id,
        "task": task.as_dict(),
        "top_per_kind": retrieval.get("top_per_kind"),
        "total_cap": retrieval.get("total_cap"),
        "kinds": kinds_out,
    }


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_digest(digest: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json(digest).encode("utf-8"))


def _contains_evidence_key(obj: Any) -> bool:
    if isinstance(obj, dict):
        if "evidence" in obj:
            return True
        return any(_contains_evidence_key(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_evidence_key(v) for v in obj)
    return False


def render_digest_prompt(digest: Mapping[str, Any], receipt_path: str, digest_hash: str,
                         receipt_hash: str = "") -> str:
    """Bounded rendering for the kickoff prompt. Never dumps evidence/history."""
    lines = [
        "ORCHESTRATOR MEMORY DIGEST (mandatory; already retrieved — do not skip, do not re-search as a substitute):",
        f"receipt_path: {receipt_path}",
        f"digest_hash: {digest_hash}",
    ]
    if receipt_hash:
        lines.append(f"receipt_hash: {receipt_hash}")
    lines.extend([
        f"schema_version: {digest.get('schema_version')}",
        f"round_id: {digest.get('round_id')}",
        f"branch_id: {(digest.get('task') or {}).get('branch_id')}",
        f"subgoal_id: {(digest.get('task') or {}).get('subgoal_id')}",
        "hits:",
    ])
    any_hit = False
    for kind in RETRIEVAL_KINDS:
        for h in (digest.get("kinds") or {}).get(kind) or []:
            any_hit = True
            score = h.get("score")
            score_s = "exact" if score is None else f"{score}"
            claim = h.get("claim_snippet") or ""
            lines.append(
                f"  - [{kind}] ({h.get('provenance')}, {score_s}) "
                f"id={h.get('id')} {claim}"
            )
    if not any_hit:
        lines.append("  (none — bounded retrieval returned no hits; do not assume empty history beyond this digest)")
    lines.append(
        "Use only these hits plus TASK.md / the fact graph. This digest is not the full memory log."
    )
    return "\n".join(lines)


def _prompt_over_cap(text: str, char_cap: int, byte_cap: int) -> bool:
    return len(text) > char_cap or len(text.encode("utf-8")) > byte_cap


def _assert_prompt_cap(text: str, char_cap: int, byte_cap: int) -> None:
    n_chars, n_bytes = len(text), len(text.encode("utf-8"))
    if n_chars > char_cap or n_bytes > byte_cap:
        raise RoundReceiptError(
            "start",
            f"injected prompt exceeds cap ({n_chars} chars/{n_bytes} bytes; "
            f"limits {char_cap}/{byte_cap})",
        )


def _flat_hits(digest: Mapping[str, Any]) -> List[Tuple[str, int, Dict[str, Any]]]:
    out: List[Tuple[str, int, Dict[str, Any]]] = []
    for kind in RETRIEVAL_KINDS:
        for i, h in enumerate((digest.get("kinds") or {}).get(kind) or []):
            out.append((kind, i, h))
    return out


def _drop_key(item: Tuple[str, int, Dict[str, Any]]) -> Tuple:
    kind, _i, h = item
    prov = _PROVENANCE_RANK.get(str(h.get("provenance")), 9)
    score = h.get("score")
    sv = float(score) if isinstance(score, (int, float)) else 1e18
    kind_i = RETRIEVAL_KINDS.index(kind) if kind in RETRIEVAL_KINDS else 99
    return (prov, -sv, kind_i, str(h.get("id") or ""))


def prune_digest_for_prompt(
    digest: Mapping[str, Any],
    receipt_path: str,
    *,
    char_cap: Optional[int] = None,
    byte_cap: Optional[int] = None,
) -> Dict[str, Any]:
    """Drop lowest-priority hits / shrink snippets until render fits char+byte caps."""
    pruned = copy.deepcopy(dict(digest))
    if "kinds" not in pruned or not isinstance(pruned["kinds"], dict):
        pruned["kinds"] = {k: [] for k in RETRIEVAL_KINDS}
    c_cap, b_cap = prompt_char_cap(char_cap), prompt_byte_cap(byte_cap)

    def rendered() -> str:
        return render_digest_prompt(pruned, receipt_path, _HASH_PLACEHOLDER, _HASH_PLACEHOLDER)

    while _prompt_over_cap(rendered(), c_cap, b_cap):
        flat = _flat_hits(pruned)
        if not flat:
            break
        kind, idx, _h = max(flat, key=_drop_key)
        pruned["kinds"][kind].pop(idx)

    for snip_len in (80, 40, 16, 0):
        if not _prompt_over_cap(rendered(), c_cap, b_cap):
            break
        for kind in RETRIEVAL_KINDS:
            for h in pruned["kinds"].get(kind) or []:
                s = h.get("claim_snippet") or ""
                if snip_len <= 0:
                    h["claim_snippet"] = ""
                elif len(s) > snip_len:
                    h["claim_snippet"] = s[: max(0, snip_len - 1)] + "…"

    _assert_prompt_cap(rendered(), c_cap, b_cap)
    return pruned


# --------------------------------------------------------------------------- #
# receipts                                                                     #
# --------------------------------------------------------------------------- #

def start_receipt_path(wl: L.WorkerLayout, round_id: int) -> Path:
    return wl.receipts / f"round_{int(round_id):04d}.start.json"


def end_receipt_path(wl: L.WorkerLayout, round_id: int) -> Path:
    return wl.receipts / f"round_{int(round_id):04d}.end.json"


def scan_max_receipt_id(wl: L.WorkerLayout) -> int:
    folder = wl.receipts
    if not folder.is_dir():
        return 0
    mx = 0
    for p in folder.iterdir():
        m = _RECEIPT_NAME_RE.match(p.name)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def allocate_receipt_id(wl: L.WorkerLayout) -> int:
    """Per-worker monotonic ID. Survives process restart; flock serializes concurrent alloc."""
    folder = wl.receipts
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = folder / ".allocator.lock"
    seq_path = folder / ".next_id"
    with open(lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            stored = 1
            if seq_path.is_file():
                try:
                    stored = max(1, int(seq_path.read_text(encoding="utf-8").strip() or "1"))
                except ValueError:
                    stored = 1
            nxt = max(stored, scan_max_receipt_id(wl) + 1)
            fd = os.open(str(seq_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
            try:
                os.write(fd, f"{nxt + 1}\n".encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(folder)
            return nxt
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _receipt_phase_from_path(path: Path) -> str:
    """Map an immutable receipt filename to its phase. Never treat all non-start as end."""
    name = Path(path).name
    for kind in ("start", "end", "launch", "result"):
        if name.endswith(f".{kind}.json"):
            return kind
    return "start"


def exclusive_write_json(path: Path, obj: Mapping[str, Any]) -> None:
    """Create ``path`` with O_CREAT|O_EXCL. Pre-existing path is a failure, never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError as e:
        raise RoundReceiptError(
            _receipt_phase_from_path(path),
            f"receipt already exists (immutable, will not overwrite): {path}",
            path=str(path),
        ) from e
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.close(fd)
    _fsync_dir(path.parent)


# Back-compat name used by older tests; still exclusive (never os.replace).
atomic_write_json = exclusive_write_json


def hash_receipt_payload(payload: Mapping[str, Any]) -> str:
    """Hash the complete receipt object excluding only ``receipt_hash`` itself."""
    body = {k: v for k, v in payload.items() if k != "receipt_hash"}
    return _sha256_bytes(canonical_json(body).encode("utf-8"))


def verify_start_payload(payload: Mapping[str, Any]) -> str:
    """Return digest_hash if the payload is self-consistent; raise otherwise."""
    digest = payload.get("digest")
    stored_digest = payload.get("digest_hash")
    stored_receipt = payload.get("receipt_hash")
    if not isinstance(digest, dict) or not isinstance(stored_digest, str) or not stored_digest:
        raise RoundReceiptError("start", "receipt missing digest or digest_hash")
    if not isinstance(stored_receipt, str) or not stored_receipt:
        raise RoundReceiptError("start", "receipt missing receipt_hash")
    required = ("schema_version", "project_id", "worker_id", "round_id", "run_id",
                "task", "queries", "hits", "memory_watermark_start", "digest",
                "digest_hash", "created_at")
    missing = [k for k in required if k not in payload]
    if missing:
        raise RoundReceiptError("start", f"receipt missing fields {missing}")
    recomputed_digest = hash_digest(digest)
    if recomputed_digest != stored_digest:
        raise RoundReceiptError(
            "start",
            "digest_hash does not match canonical digest (tamper or persist failure)",
            expected=recomputed_digest, stored=stored_digest,
        )
    recomputed_receipt = hash_receipt_payload(payload)
    if recomputed_receipt != stored_receipt:
        raise RoundReceiptError(
            "start",
            "receipt_hash does not match canonical start payload (tamper or persist failure)",
            expected=recomputed_receipt, stored=stored_receipt,
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RoundReceiptError(
            "start",
            f"unsupported receipt schema_version {payload.get('schema_version')}",
        )
    return stored_digest


def verify_end_payload(payload: Mapping[str, Any]) -> str:
    stored = payload.get("receipt_hash")
    if not isinstance(stored, str) or not stored:
        raise RoundReceiptError("end", "end receipt missing receipt_hash")
    required = ("schema_version", "project_id", "worker_id", "round_id",
                "start_receipt", "start_digest_hash", "start_receipt_hash",
                "memory_watermark_end", "appended_ids", "finalized_at")
    missing = [k for k in required if k not in payload]
    if missing:
        raise RoundReceiptError("end", f"end receipt missing fields {missing}")
    recomputed = hash_receipt_payload(payload)
    if recomputed != stored:
        raise RoundReceiptError(
            "end",
            "end receipt_hash does not match canonical payload (tamper or persist failure)",
            expected=recomputed, stored=stored,
        )
    return stored


def read_and_verify_start(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RoundReceiptError("start", f"cannot read start receipt {path}: {e}",
                                path=str(path)) from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RoundReceiptError("start", f"start receipt is not JSON: {e}",
                                path=str(path)) from e
    if not isinstance(payload, dict):
        raise RoundReceiptError("start", "start receipt is not a JSON object",
                                path=str(path))
    verify_start_payload(payload)
    return payload


def read_and_verify_end(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RoundReceiptError("end", f"cannot read end receipt {path}: {e}",
                                path=str(path)) from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RoundReceiptError("end", f"end receipt is not JSON: {e}",
                                path=str(path)) from e
    if not isinstance(payload, dict):
        raise RoundReceiptError("end", "end receipt is not a JSON object", path=str(path))
    verify_end_payload(payload)
    return payload


def prepare_round_start(
    wl: L.WorkerLayout,
    round_id: Optional[int] = None,
    *,
    per_kind: Optional[int] = None,
    cap: Optional[int] = None,
    char_cap: Optional[int] = None,
    byte_cap: Optional[int] = None,
) -> StartReceipt:
    """Retrieve, write immutable start receipt, re-read and verify. Never launches codex."""
    if round_id is None:
        round_id = allocate_receipt_id(wl)
    task = load_task_context(wl)
    path = start_receipt_path(wl, round_id)
    try:
        retrieval = retrieve_bounded(wl.project_dir, task, per_kind=per_kind, cap=cap)
        watermark = memory_watermark(wl.project_dir)
        run_id = os.environ.get("EULER_RUN_ID") or ""
        digest = build_digest(
            project_id=task.project_id or wl.project,
            worker_id=wl.name,
            round_id=round_id,
            task=task,
            retrieval=retrieval,
            run_id=run_id,
        )
        c_cap, b_cap = prompt_char_cap(char_cap), prompt_byte_cap(byte_cap)
        digest = prune_digest_for_prompt(digest, str(path), char_cap=c_cap, byte_cap=b_cap)
        digest_h = hash_digest(digest)
        hits_meta = {
            kind: [
                {"id": h.get("id"), "score": h.get("score"), "provenance": h.get("provenance")}
                for h in (digest.get("kinds") or {}).get(kind) or []
            ]
            for kind in RETRIEVAL_KINDS
        }
        payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "project_id": task.project_id or wl.project,
            "worker_id": wl.name,
            "round_id": round_id,
            "run_id": run_id,
            "task": task.as_dict(),
            "queries": retrieval.get("queries"),
            "hits": hits_meta,
            "memory_watermark_start": watermark,
            "digest": digest,
            "digest_hash": digest_h,
            "created_at": utc_now(),
        }
        payload["receipt_hash"] = hash_receipt_payload(payload)
        exclusive_write_json(path, payload)
        read_and_verify_start(path)
        rendered = render_digest_prompt(
            digest, str(path), digest_h, payload["receipt_hash"],
        )
        _assert_prompt_cap(rendered, c_cap, b_cap)
        if _contains_evidence_key(digest):
            raise RoundReceiptError("start", "digest leaked evidence/full history")
    except RoundReceiptError:
        raise
    except OSError as e:
        raise RoundReceiptError("start", f"failed to persist start receipt: {e}") from e
    except Exception as e:
        raise RoundReceiptError("start", f"failed to build start receipt: {e}") from e
    return StartReceipt(
        path=path, payload=payload, digest=digest,
        digest_hash=digest_h, receipt_hash=payload["receipt_hash"],
        round_id=round_id, task=task,
    )


def appended_ids_since(project_dir: Path, start_watermark: Mapping[str, Any],
                       kinds: Sequence[str] = RETRIEVAL_KINDS) -> Dict[str, List[str]]:
    """Fail-closed suffix of append-only JSONL. Truncation/rewrite is not a suffix."""
    gm = GlobalMemory(project_dir)
    start_kinds = (start_watermark or {}).get("kinds") or {}
    out: Dict[str, List[str]] = {}
    for kind in kinds:
        path = gm._path(kind)
        raw = _read_raw(path)
        meta = start_kinds.get(kind) or {}
        start_size = int(meta.get("byte_size") or 0)
        start_count = int(meta.get("count") or 0)
        start_prefix = meta.get("prefix_sha256") or _sha256_bytes(b"")
        start_ids_hash = meta.get("ids_sha256") or _ids_sha256([])
        if len(raw) < start_size:
            raise RoundReceiptError(
                "end",
                f"{kind} JSONL truncated ({len(raw)} < start byte_size {start_size})",
                kind=kind,
            )
        prefix = raw[:start_size]
        if _sha256_bytes(prefix) != start_prefix:
            raise RoundReceiptError(
                "end",
                f"{kind} JSONL prefix bytes mutated (not append-only)",
                kind=kind,
            )
        ids = _ids_from_jsonl_bytes(raw)
        if _ids_sha256(ids[:start_count]) != start_ids_hash:
            raise RoundReceiptError(
                "end",
                f"{kind} JSONL ID prefix hash mismatch (rewrite, not append)",
                kind=kind,
            )
        out[kind] = ids[start_count:]
    return out


def finalize_round_end(wl: L.WorkerLayout, start: StartReceipt) -> Path:
    """Write companion end receipt. Never mutates the start receipt/hash."""
    try:
        on_disk = read_and_verify_start(start.path)
        if on_disk.get("digest_hash") != start.digest_hash:
            raise RoundReceiptError(
                "end",
                "start receipt digest_hash changed before finalization",
                path=str(start.path),
            )
        if on_disk.get("receipt_hash") != start.receipt_hash:
            raise RoundReceiptError(
                "end",
                "start receipt_hash changed before finalization",
                path=str(start.path),
            )
        watermark_end = memory_watermark(wl.project_dir)
        appended = appended_ids_since(
            wl.project_dir, start.payload.get("memory_watermark_start") or {},
        )
        payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "project_id": start.payload.get("project_id"),
            "worker_id": start.payload.get("worker_id"),
            "round_id": start.round_id,
            "start_receipt": str(start.path),
            "start_digest_hash": start.digest_hash,
            "start_receipt_hash": start.receipt_hash,
            "memory_watermark_end": watermark_end,
            "appended_ids": appended,
            "finalized_at": utc_now(),
        }
        payload["receipt_hash"] = hash_receipt_payload(payload)
        path = end_receipt_path(wl, start.round_id)
        exclusive_write_json(path, payload)
        again = read_and_verify_start(start.path)
        if again.get("receipt_hash") != start.receipt_hash:
            raise RoundReceiptError("end", "start receipt was rewritten during finalization")
        read_and_verify_end(path)
        return path
    except RoundReceiptError:
        raise
    except OSError as e:
        raise RoundReceiptError("end", f"failed to persist end receipt: {e}") from e
    except Exception as e:
        raise RoundReceiptError("end", f"failed to finalize end receipt: {e}") from e
