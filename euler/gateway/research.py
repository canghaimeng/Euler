# SPDX-License-Identifier: Apache-2.0 AND PolyForm-Noncommercial-1.0.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
# Upstream portions: Apache-2.0; Euler modifications: PolyForm-Noncommercial-1.0.0.
"""Kind-specific payload validation + non-forgeable context stamp for gm_add.

Infrastructure only: does not judge whether a mathematical dead end is true.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from euler.core.global_memory import GlobalMemory
from euler.core.schema import GLOBAL_KINDS

STAMP_KEYS = (
    "project_id",
    "worker_id",
    "run_id",
    "branch_id",
    "subgoal_id",
    "provenance",
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def valid_context_id(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


# Worker/main gm_add research kinds. ``verification`` is reserved to internal
# fact_submit persistence and is rejected by gm_add / validate_and_build_extra.
RESEARCH_KINDS = frozenset({
    "proof_attempt",
    "obstacle",
    "dead_end",
    "counterexample",
})

PROOF_ATTEMPT_RESULTS = frozenset({"success", "partial", "blocked", "abandoned"})
EVIDENCE_CLASSES = frozenset({
    "explicit_counterexample",
    "logical_contradiction",
    "unavoidable_missing_assumption",
    "independent_agreement",
    "failure_synthesis",
})
DEAD_END_STATUSES = frozenset({"candidate", "confirmed"})


def stamped_context(*, project_name: str = "") -> Dict[str, str]:
    project = (
        os.environ.get("EULER_PROJECT_ID")
        or project_name
        or ""
    )
    worker = os.environ.get("EULER_WORKER_ID") or os.environ.get("EULER_AUTHOR") or ""
    return {
        "project_id": project,
        "worker_id": worker,
        "run_id": os.environ.get("EULER_RUN_ID") or "",
        "branch_id": os.environ.get("EULER_BRANCH_ID") or "",
        "subgoal_id": os.environ.get("EULER_SUBGOAL_ID") or "",
        "provenance": os.environ.get("EULER_PROVENANCE") or "worker_mcp",
    }


def require_stamped_context(*, project_name: str = "") -> Dict[str, str]:
    ctx = stamped_context(project_name=project_name)
    missing = [k for k in STAMP_KEYS if not ctx.get(k)]
    if missing:
        raise ValueError(
            f"cannot stamp research record: missing context {missing} "
            f"(MCP env EULER_BRANCH_ID/EULER_SUBGOAL_ID/EULER_RUN_ID)"
        )
    for k in ("project_id", "worker_id", "run_id", "branch_id", "subgoal_id"):
        if not valid_context_id(ctx[k]):
            raise ValueError(f"stamped {k} is malformed: {ctx[k]!r}")
    return ctx


def _need_str(meta: Mapping[str, Any], key: str) -> str:
    val = meta.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"{key} is required and must be a nonempty string")
    return val.strip()


def _need_present(meta: Mapping[str, Any], key: str) -> Any:
    if key not in meta or meta[key] is None:
        raise ValueError(f"{key} is required")
    return meta[key]


def _resolve_evidence_refs(
    refs: Any,
    *,
    global_memory: Optional[GlobalMemory],
) -> List[Tuple[str, Mapping[str, Any]]]:
    """Resolve record IDs against one project's global-memory store.

    Evidence references are deliberately record IDs, not attempt IDs, fact IDs,
    or free-form labels.  Reading only through the ``GlobalMemory`` instance
    supplied by the gateway keeps resolution project-local.
    """
    if not isinstance(refs, list) or not refs:
        raise ValueError("confirmed dead_end requires nonempty evidence_refs")
    if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("confirmed dead_end evidence_refs must be nonempty record ID strings")
    normalized = [ref.strip() for ref in refs]
    if len(normalized) != len(set(normalized)):
        raise ValueError("confirmed dead_end evidence_refs must not contain duplicate record IDs")
    if global_memory is None:
        raise ValueError("confirmed dead_end evidence_refs require project global memory")

    wanted = set(normalized)
    found: Dict[str, List[Tuple[str, Mapping[str, Any]]]] = {}
    for kind in GLOBAL_KINDS:
        for record in global_memory.read(kind):
            record_id = record.get("id")
            if record_id in wanted:
                found.setdefault(record_id, []).append((kind, record))

    missing = [ref for ref in normalized if ref not in found]
    if missing:
        raise ValueError(
            "confirmed dead_end evidence_refs contain unknown project global-memory "
            f"record IDs: {missing}"
        )
    ambiguous = [ref for ref in normalized if len(found[ref]) != 1]
    if ambiguous:
        raise ValueError(
            "confirmed dead_end evidence_refs are ambiguous project global-memory "
            f"record IDs: {ambiguous}"
        )
    return [found[ref][0] for ref in normalized]


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_supporting_record(kind: str, record: Mapping[str, Any]) -> bool:
    """Conservative admissibility rule for non-special evidence classes."""
    if kind == "verification":
        return (
            record.get("transport_status") == "ok"
            and record.get("verdict") in ("correct", "wrong")
        )
    if kind == "obstacle":
        return _has_text(record.get("claim")) or _has_text(record.get("evidence"))
    if kind == "proof_attempt" or record.get("verifiable") is True:
        return _has_text(record.get("evidence"))
    return False


def _validate_confirmed_dead_end_evidence(
    evidence_class: str,
    refs: Any,
    *,
    global_memory: Optional[GlobalMemory],
) -> List[str]:
    resolved = _resolve_evidence_refs(refs, global_memory=global_memory)

    if evidence_class == "explicit_counterexample":
        if not any(
            kind == "counterexample"
            and record.get("verifiable") is True
            and _has_text(record.get("evidence"))
            for kind, record in resolved
        ):
            raise ValueError(
                "explicit_counterexample requires at least one counterexample record ref"
            )
    elif evidence_class == "failure_synthesis":
        if not any(
            record.get("record_type") == "key_failures_summary"
            and (
                kind == "obstacle"
                or (
                    kind == "dead_end"
                    and record.get("dead_end_status") == "candidate"
                )
            )
            for kind, record in resolved
        ):
            raise ValueError(
                "failure_synthesis requires a key_failures_summary obstacle or "
                "candidate dead_end record ref"
            )
    elif evidence_class == "independent_agreement":
        workers = {
            record.get("worker_id")
            for kind, record in resolved
            if _is_supporting_record(kind, record)
            and _has_text(record.get("worker_id"))
        }
        if len(workers) < 2:
            raise ValueError(
                "independent_agreement requires supporting record refs from at "
                "least two distinct worker_id values"
            )
    elif not any(_is_supporting_record(kind, record) for kind, record in resolved):
        raise ValueError(
            f"{evidence_class} requires at least one verifiable, proof_attempt, "
            "obstacle, or successful-transport mathematical verification record ref"
        )

    return [str(ref).strip() for ref in refs]


def validate_and_build_extra(
    kind: str,
    metadata: Optional[Mapping[str, Any]],
    *,
    evidence: str = "",
    links: Optional[Mapping[str, Any]] = None,
    project_name: str = "",
    global_memory: Optional[GlobalMemory] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (extra_fields, links) for GlobalMemory.append.

    Stamped context always overwrites metadata for STAMP_KEYS (non-forgeable).
    Non-research kinds pass metadata through without stamping.
    """
    if kind == "verification":
        raise ValueError(
            "kind 'verification' is reserved to fact_submit persistence; "
            "gm_add cannot write verification rows"
        )

    meta = dict(metadata or {})
    out_links = dict(links or {})
    extra: Dict[str, Any] = {}

    if kind not in RESEARCH_KINDS:
        # do not stamp; drop any attempt to forge research context
        for k in STAMP_KEYS:
            meta.pop(k, None)
        extra.update(meta)
        return extra, out_links

    stamp = require_stamped_context(project_name=project_name)
    # metadata cannot override stamp
    for k in STAMP_KEYS:
        extra[k] = stamp[k]
        meta.pop(k, None)

    if kind == "proof_attempt":
        extra["attempt_id"] = _need_str(meta, "attempt_id")
        extra["approach"] = _need_str(meta, "approach")
        result = _need_str(meta, "result")
        if result not in PROOF_ATTEMPT_RESULTS:
            raise ValueError(
                f"proof_attempt.result must be one of {sorted(PROOF_ATTEMPT_RESULTS)}"
            )
        extra["result"] = result
        extra["reason"] = _need_str(meta, "reason")
        extra["salvage"] = _need_str(meta, "salvage")
        extra["inputs"] = _need_present(meta, "inputs")
        extra["related_memory"] = _need_present(meta, "related_memory")
        out_links["attempt_id"] = extra["attempt_id"]
    elif kind == "obstacle":
        extra["attempt_id"] = _need_str(meta, "attempt_id")
        out_links["attempt_id"] = extra["attempt_id"]
    elif kind == "dead_end":
        status = meta.get("dead_end_status") or meta.get("status") or "candidate"
        if status not in DEAD_END_STATUSES:
            raise ValueError("dead_end_status must be 'candidate' or 'confirmed'")
        extra["dead_end_status"] = status
        extra["attempt_id"] = _need_str(meta, "attempt_id")
        out_links["attempt_id"] = extra["attempt_id"]
        if status == "confirmed":
            eclass = _need_str(meta, "evidence_class")
            if eclass not in EVIDENCE_CLASSES:
                raise ValueError(
                    f"confirmed dead_end evidence_class must be one of "
                    f"{sorted(EVIDENCE_CLASSES)}"
                )
            refs = _validate_confirmed_dead_end_evidence(
                eclass,
                meta.get("evidence_refs"),
                global_memory=global_memory,
            )
            extra["evidence_class"] = eclass
            extra["evidence_refs"] = refs
        # never auto-upgrade candidate -> confirmed
    elif kind == "counterexample":
        extra["attempt_id"] = _need_str(meta, "attempt_id")
        extra["target"] = _need_str(meta, "target")
        if not (evidence or "").strip():
            raise ValueError("counterexample requires nonempty evidence")
        out_links["attempt_id"] = extra["attempt_id"]
        out_links["target"] = extra["target"]

    # leftover metadata (not stamp keys) may be kept except forge attempts
    for k, v in meta.items():
        if k in STAMP_KEYS or k in extra:
            continue
        extra[k] = v
    return extra, out_links
