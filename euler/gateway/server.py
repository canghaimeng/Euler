#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Euler gateway — the role-gated MCP server.

A thin MCP wrapper over ``euler.core`` (the truth stores) + one external
integration (``euler.integrations`` arXiv search). It exposes only the verbs an
LLM can't do reliably itself (content-addressed writes, cascade integrity, the
verifier-gated fact write, BM25) — reads / local memory / novelty judgment are
the agent's own file operations, deliberately NOT tools.

The permission model (which tools each role sees) lives in ``roles.py``. The
``fact_submit`` tool is the ONLY fact-write path: it runs the glossary-coverage
check, calls the verify service, writes the node IFF the verdict is ``correct``,
and ALWAYS traces the verdict to global memory (kind ``verification``) — accept,
reject, or accept-but-write-failed — so a verdict is never stored by nobody (the
verify service is stateless).

Config is read from the environment at CALL time (not import time) so the server
is testable and reconfigurable:
  EULER_PROJECT_DIR   the project dir a worker is pinned to (fallback for main)
  EULER_AGENTS_ROOT   root holding all projects (<root>/<project>); lets main
                      address any project by name via the ``project`` arg
  EULER_AUTHOR        this agent's id, for attribution
  EULER_ROLE          worker | main | verifier | all  (selects exposed tools;
                      unset falls back to the read-only verifier set — fail-closed)
  EULER_VERIFY_URL    verify-service endpoint for fact_submit
  EULER_PROBLEM_ID    problem id stamped on written facts (default: project name)
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from euler._mcp import FastMCP
from euler.core import FactGraph, GlobalMemory
from euler.integrations import search as _arxiv_search

from .research import require_stamped_context, validate_and_build_extra
from .roles import tools_for

_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ROUND_ENV_KEYS = (
    "EULER_WORKER_ID",
    "EULER_RUN_ID",
    "EULER_BRANCH_ID",
    "EULER_SUBGOAL_ID",
)
_ATTEMPT_CONTEXT_KEYS = (
    "project_id",
    "worker_id",
    "run_id",
    "branch_id",
    "subgoal_id",
)
_CONTEXT_UNSET = object()


# --------------------------------------------------------------------------- #
# config resolution (env read at call time — testable / reconfigurable)       #
# --------------------------------------------------------------------------- #

def _author() -> str:
    return os.environ.get("EULER_AUTHOR", "unknown")


def _role() -> str:
    # Fail-closed: an UNSET role gets the most-restrictive read-only set, same as
    # a mis-typed one (roles.tools_for). Dev use of the full set is explicit:
    # EULER_ROLE=all.
    return os.environ.get("EULER_ROLE", "verifier")


def _project(project: Optional[str] = None) -> Path:
    """Resolve the project dir to operate on.

    ``project`` (the main agent's per-call selector) wins: it names a project
    under ``EULER_AGENTS_ROOT`` (``<root>/<project>``), so one session can touch
    several projects. With no ``project`` we fall back to ``EULER_PROJECT_DIR``
    (a worker is always pinned this way). The name is validated to a single path
    segment — no ``/`` or ``..`` — so it can never escape the agents root."""
    agents_root = os.environ.get("EULER_AGENTS_ROOT", "")
    project_dir = os.environ.get("EULER_PROJECT_DIR", "")
    if project:
        if not agents_root:
            raise RuntimeError("EULER_AGENTS_ROOT is not set; cannot resolve a project by name")
        if not _PROJECT_NAME_RE.match(project):
            raise RuntimeError(f"invalid project name: {project!r}")
        pdir = Path(agents_root) / project
        if not pdir.is_dir():
            raise RuntimeError(f"no such project: {project!r} (under {agents_root})")
        return pdir
    if not project_dir:
        raise RuntimeError("EULER_PROJECT_DIR is not set and no project was given")
    return Path(project_dir)


def _gm(project: Optional[str] = None) -> GlobalMemory:
    return GlobalMemory(_project(project))


def _fg(project: Optional[str] = None) -> FactGraph:
    return FactGraph(_project(project))


def _verify(statement: str, proof: str) -> Dict[str, Any]:
    """POST {statement, proof} to the verify service; return its JSON."""
    verify_url = os.environ.get("EULER_VERIFY_URL", "")
    if not verify_url:
        raise RuntimeError("EULER_VERIFY_URL is not set (verify service not wired yet)")
    try:
        timeout = int(os.environ.get("EULER_VERIFY_TIMEOUT", "3600"))
    except ValueError:
        timeout = 3600
    data = json.dumps({"statement": statement, "proof": proof}).encode("utf-8")
    req = urllib.request.Request(
        verify_url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted local URL)
        return json.loads(resp.read().decode("utf-8"))


_PRECHECK_DETAIL_RE = re.compile(
    r"vacuous|\[p[135]\b|hard prohibition p[135]\b",
    re.IGNORECASE,
)


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Parse FastAPI/Starlette ``{"detail": ...}`` (or raw body) from HTTPError."""
    body = ""
    try:
        raw = exc.read()
        if isinstance(raw, bytes):
            body = raw.decode("utf-8", errors="replace")
        elif raw:
            body = str(raw)
    except Exception:
        body = ""
    body = (body or "").strip()
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and "detail" in parsed:
            detail = parsed["detail"]
            if isinstance(detail, str):
                return detail
            try:
                return json.dumps(detail, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(detail)
        return body
    reason = getattr(exc, "reason", "") or ""
    return f"HTTP {exc.code}: {reason}".strip()


def classify_http_verify_error(exc: urllib.error.HTTPError) -> Dict[str, Any]:
    """Classify a verify-service HTTPError.

    HTTP 400 with vacuous/P1/P3/P5 precheck detail is a successful transport
    and a mathematical ``wrong``. 422 is protocol; 500 is service/http; 504 is
    timeout. Never maps 500/504 to ``wrong``.
    """
    code = int(getattr(exc, "code", 0) or 0)
    detail = _http_error_detail(exc)
    if code == 400 and _PRECHECK_DETAIL_RE.search(detail or ""):
        return {
            "transport_status": "ok",
            "verdict": "wrong",
            "detail": detail,
            "repair_hints": detail,
        }
    if code == 422:
        status = "protocol_error"
    elif code == 504:
        status = "timeout"
    elif code == 500:
        status = "service_error"
    else:
        status = "http_error"
    return {
        "transport_status": status,
        "verdict": None,
        "detail": detail,
        "repair_hints": None,
    }


def classify_verify_transport_error(exc: BaseException) -> tuple:
    """Map infra failures. Never maps them to a mathematical verdict.

    HTTP 400 precheck rejections are classified as transport ``ok`` (the
    mathematical ``wrong`` is applied by ``fact_submit``, not here as a
    transport class).
    """
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return "timeout", str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        classified = classify_http_verify_error(exc)
        return classified["transport_status"], classified["detail"]
    if isinstance(exc, urllib.error.URLError):
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            return "timeout", reason
        return "connection_error", reason
    if isinstance(exc, json.JSONDecodeError):
        return "protocol_error", f"invalid JSON: {exc}"
    if isinstance(exc, (TypeError, ValueError, UnicodeDecodeError)):
        return "protocol_error", str(exc)
    return "service_error", str(exc)


# --------------------------------------------------------------------------- #
# global memory                                                               #
# --------------------------------------------------------------------------- #

def gm_add(
    kind: str,
    claim: str,
    evidence: str = "",
    verifiable: Optional[bool] = None,
    glossary: Optional[Dict[str, str]] = None,
    links: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish a finding to shared global memory (claim + evidence). Verifiable
    kinds (conclusion/example/counterexample/proof_attempt) require explicit
    evidence; judgments (plan/direction/obstacle/master_guidance/elaboration) do
    not. Define your symbols in ``glossary`` and reuse project terminology.

    Research kinds (proof_attempt/obstacle/dead_end/counterexample)
    take a structured ``metadata`` object. The gateway stamps non-forgeable
    project/worker/run/branch/subgoal/provenance from the current MCP env
    (metadata cannot override those fields). ``kind="verification"`` is rejected:
    verification rows are reserved to internal ``fact_submit`` persistence.
    Direct JSONL / Python fallbacks are prohibited.

    Main agent: pass ``project`` to target one of several projects by name;
    workers omit it (pinned to their own project)."""
    if kind == "verification":
        return {
            "ok": False,
            "error": (
                "kind 'verification' is reserved to fact_submit persistence; "
                "gm_add cannot write verification rows"
            ),
            "kind": kind,
        }
    try:
        pdir = _project(project)
        gm = GlobalMemory(pdir)
        extra, out_links = validate_and_build_extra(
            kind, metadata, evidence=evidence, links=links,
            project_name=pdir.name,
            global_memory=gm,
        )
        entry_id = gm.append(
            kind, claim=claim, evidence=evidence, author=_author(),
            verifiable=verifiable, glossary=glossary, links=out_links,
            **extra,
        )
    except ValueError as e:
        return {"ok": False, "error": str(e), "kind": kind}
    return {"ok": True, "id": entry_id, "kind": kind}


def gm_search(query: str, kinds: Optional[List[str]] = None, limit_per_kind: int = 10,
              project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 over shared global-memory findings. Use to reuse others' results,
    avoid duplicate work, and learn which paths already died. Main agent: pass
    ``project`` to search a specific project; workers omit it."""
    return _gm(project).search(query, kinds=kinds, limit_per_kind=limit_per_kind)


# --------------------------------------------------------------------------- #
# fact graph                                                                  #
# --------------------------------------------------------------------------- #

def _fact_submit_round_context(*, project_name: str) -> Optional[Dict[str, str]]:
    """Return the current worker-round context, or ``None`` for legacy calls.

    A legacy/dev/authoring invocation has none of the worker-round variables.
    Once any such variable is present, the call is attempting to run as a
    worker round and must have a complete, valid stamp (fail closed).
    """
    if not any(os.environ.get(key) for key in _ROUND_ENV_KEYS):
        return None
    return require_stamped_context(project_name=project_name)


def _matching_proof_attempt(
    gm: GlobalMemory,
    *,
    attempt_id: Optional[str],
    context: Dict[str, str],
) -> Dict[str, Any]:
    """Resolve an attempt in this project's GM and require an exact round match."""
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError(
            "attempt_id is required for fact_submit in a worker round"
        )
    aid = attempt_id.strip()
    candidates = [
        row for row in gm.read("proof_attempt")
        if row.get("attempt_id") == aid
    ]
    if not candidates:
        raise ValueError(
            f"unknown attempt_id {aid!r}: no proof_attempt exists in this project"
        )
    for row in reversed(candidates):
        if all(row.get(key) == context[key] for key in _ATTEMPT_CONTEXT_KEYS):
            return row
    mismatches = sorted({
        key
        for row in candidates
        for key in _ATTEMPT_CONTEXT_KEYS
        if row.get(key) != context[key]
    })
    raise ValueError(
        f"proof_attempt {aid!r} belongs to a different worker round context "
        f"(mismatched fields: {mismatches})"
    )


def _persist_verification(gm, *, claim: str, evidence: str, extra: Dict[str, Any],
                          links: Optional[Dict[str, Any]] = None,
                          context: Any = _CONTEXT_UNSET) -> None:
    """Internal fact_submit persistence; provenance is always ``fact_submit``.

    Worker rounds pass their already-validated context. Direct callers retain
    the stricter historical helper contract (a full stamp is required), while
    ``fact_submit`` may explicitly pass ``None`` for a legacy/dev invocation.
    """
    if context is _CONTEXT_UNSET:
        ctx: Optional[Dict[str, str]] = require_stamped_context(
            project_name=Path(_project()).name
        )
    else:
        ctx = context
    stamped = dict(extra)
    if ctx is not None:
        for k, v in ctx.items():
            stamped[k] = v
    stamped["provenance"] = "fact_submit"
    gm.append(
        "verification",
        claim=claim,
        evidence=evidence,
        author=_author(),
        verifiable=False,
        links=links or {},
        **stamped,
    )


def fact_submit(
    statement: str,
    proof: str,
    predecessors: Optional[List[str]] = None,
    glossary_introduces: Optional[Dict[str, str]] = None,
    intuition: str = "",
    source_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    external_refs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The only way to write a fact. Runs the glossary-coverage check, calls the
    verifier, and writes the node IFF accepted. On reject, returns repair hints
    and writes nothing. Cite the returned ``fact_id`` in downstream proofs.

    This function is the MCP ``fact_submit`` implementation. Calling it from a
    CLI, a shell fallback, or a direct ``from euler.gateway.server import
    fact_submit`` is **prohibited** in production (as is writing fact-graph JSONL
    / markdown by hand). Missing MCP tools are a preflight failure, not a reason
    to bypass the gateway.

    Once a verdict exists, the verification outcome is **always** recorded to
    global memory (kind ``verification``) — accept, reject, or accept-but-write-
    failed — so a verdict is never stored by nobody (the verifier is stateless;
    this worker tool persists it). ``source_id`` optionally links to the
    global-memory finding being promoted. In a stamped worker round,
    ``attempt_id`` is required and must identify a ``proof_attempt`` in this
    project with the same project/worker/run/branch/subgoal context; this check
    happens before the verifier or fact graph is touched. Legacy calls without
    worker-round context retain the optional ``attempt_id`` behavior.

    When your proof cites an external (published) result, pass it in
    ``external_refs`` as a structured entry — e.g.
    ``{"key": "HL26", "authors": ["Han", "Liu"], "title": "...",
    "arxiv": "2603.03817", "year": 2026, "cited_for": "Theorem 1.2"}`` (ground it
    with ``search_arxiv_theorems``). This is captured on the fact so the paper
    pipeline can cite it without re-deriving; it is mutable metadata and does not
    affect the ``fact_id``."""
    pdir = _project()
    try:
        round_context = _fact_submit_round_context(project_name=Path(pdir).name)
    except ValueError as e:
        return {
            "accepted": False,
            "verdict": None,
            "error": str(e),
            "context_error": True,
        }

    fg = FactGraph(pdir)
    gm = GlobalMemory(pdir)
    problem_id = os.environ.get("EULER_PROBLEM_ID", Path(pdir).name)

    proof_attempt = None
    if round_context is not None:
        try:
            proof_attempt = _matching_proof_attempt(
                gm, attempt_id=attempt_id, context=round_context,
            )
        except ValueError as e:
            return {
                "accepted": False,
                "verdict": None,
                "error": str(e),
                "attempt_error": True,
            }

    aid = attempt_id.strip() if isinstance(attempt_id, str) else (source_id or None)

    # A worker may retry an MCP call after losing the first response.  Once this
    # exact round/attempt has an accepted active fact, replay the result instead
    # of spending another verifier call and creating a second content-addressed
    # node from a cosmetically different proof body.  Reusing an accepted
    # attempt for a different statement is a contract error, not a new proof.
    if attempt_id and aid:
        accepted_rows = [
            row for row in gm.read("verification")
            if row.get("attempt_id") == aid
            and row.get("verdict") == "correct"
            and row.get("fact_id")
            and not row.get("write_error")
            and (
                round_context is None
                or all(row.get(key) == round_context[key]
                       for key in _ATTEMPT_CONTEXT_KEYS)
            )
        ]
        if accepted_rows:
            prior = accepted_rows[0]
            prior_fact_id = str(prior["fact_id"])
            if prior.get("claim") != statement:
                return {
                    "accepted": False,
                    "verdict": None,
                    "error": (
                        f"attempt_id {aid!r} already accepted a different "
                        "statement; create a new proof_attempt"
                    ),
                    "attempt_error": True,
                    "idempotent_replay": False,
                }
            if not fg.exists(prior_fact_id):
                return {
                    "accepted": False,
                    "verdict": None,
                    "error": (
                        f"attempt_id {aid!r} points to inactive fact "
                        f"{prior_fact_id}; create a new proof_attempt"
                    ),
                    "attempt_error": True,
                    "idempotent_replay": False,
                }
            return {
                "accepted": True,
                "fact_id": prior_fact_id,
                "transport_status": "not_called",
                "undefined_symbols": [],
                "idempotent_replay": True,
            }

    # glossary coverage is advisory — never let a heuristic bug block submission
    try:
        undefined = fg.undefined_symbols(
            statement=statement, proof=proof, intuition=intuition,
            predecessors=predecessors, glossary_introduces=glossary_introduces,
        )
    except Exception:
        undefined = []

    links = {
        "source_id": source_id,
        "predecessors": predecessors or [],
    }
    if aid:
        links["attempt_id"] = aid
    if proof_attempt is not None:
        links["proof_attempt_id"] = proof_attempt.get("id")

    # 1) Transport. Infra failures are persisted with verdict=null — never "wrong".
    # HTTP 400 precheck (vacuous/P1/P3/P5) is transport ok + mathematical wrong.
    result: Optional[Dict[str, Any]] = None
    try:
        result = _verify(statement, proof)
    except Exception as e:
        if isinstance(e, urllib.error.HTTPError):
            classified = classify_http_verify_error(e)
            if classified["verdict"] == "wrong":
                extra = {
                    "transport_status": "ok",
                    "verdict": "wrong",
                    "error": classified["detail"],
                    "detail": classified["detail"],
                    "repair_hints": classified["repair_hints"],
                    "attempt_id": aid,
                    "fact_id": None,
                }
                _persist_verification(
                    gm, claim=statement,
                    evidence=classified["repair_hints"] or classified["detail"]
                    or "verdict: wrong",
                    extra=extra, links=links, context=round_context,
                )
                return {
                    "accepted": False, "verdict": "wrong", "transport_status": "ok",
                    "repair_hints": classified["repair_hints"],
                    "detail": classified["detail"],
                    "undefined_symbols": undefined,
                }
            tstatus = classified["transport_status"]
            detail = classified["detail"]
        else:
            tstatus, detail = classify_verify_transport_error(e)
        extra = {
            "transport_status": tstatus,
            "verdict": None,
            "error": detail,
            "detail": detail,
            "attempt_id": aid,
            "fact_id": None,
        }
        _persist_verification(
            gm, claim=statement,
            evidence=f"transport_status={tstatus}; verdict=null",
            extra=extra, links=links, context=round_context,
        )
        return {
            "accepted": False, "verdict": None, "transport_status": tstatus,
            "error": detail, "undefined_symbols": undefined,
        }
    if not isinstance(result, dict):
        detail = f"verify service returned a non-dict body ({type(result).__name__})"
        extra = {
            "transport_status": "protocol_error", "verdict": None,
            "error": detail, "detail": detail, "attempt_id": aid, "fact_id": None,
        }
        _persist_verification(
            gm, claim=statement,
            evidence="transport_status=protocol_error; verdict=null",
            extra=extra, links=links, context=round_context,
        )
        return {
            "accepted": False, "verdict": None, "transport_status": "protocol_error",
            "error": detail, "undefined_symbols": undefined,
        }
    verdict = result.get("verdict")
    if verdict not in ("correct", "wrong"):
        detail = f"invalid verdict {verdict!r} (expected correct|wrong)"
        extra = {
            "transport_status": "protocol_error", "verdict": None,
            "error": detail, "detail": detail, "attempt_id": aid, "fact_id": None,
            "verification_report": result.get("verification_report"),
        }
        _persist_verification(
            gm, claim=statement,
            evidence="transport_status=protocol_error; verdict=null",
            extra=extra, links=links, context=round_context,
        )
        return {
            "accepted": False, "verdict": None, "transport_status": "protocol_error",
            "error": detail, "undefined_symbols": undefined,
        }
    accepted = verdict == "correct"

    # 2) Write the fact iff accepted. Catch write failures so they do NOT skip the trace.
    fact_id = None
    write_error = None
    if accepted:
        try:
            fact_id = fg.add(
                problem_id=problem_id, author=_author(), statement=statement, proof=proof,
                predecessors=predecessors, glossary_introduces=glossary_introduces,
                intuition=intuition, external_refs=external_refs,
            )
        except Exception as e:
            write_error = str(e)

    # 3) ALWAYS persist this invocation (transport ok).
    extra = {
        "transport_status": "ok",
        "verdict": verdict,
        "attempt_id": aid,
        "fact_id": fact_id,
        "write_error": write_error,
        "verification_report": result.get("verification_report"),
    }
    ev = "verdict: correct" if accepted else (result.get("repair_hints") or "verdict: wrong")
    _persist_verification(
        gm, claim=statement, evidence=ev, extra=extra, links=links,
        context=round_context,
    )

    # 4) Return.
    if not accepted:
        return {
            "accepted": False,
            "verdict": verdict,
            "transport_status": "ok",
            "repair_hints": result.get("repair_hints"),
            "verification_report": result.get("verification_report"),
            "undefined_symbols": undefined,
        }
    if write_error:
        return {"accepted": True, "fact_id": None, "write_error": write_error,
                "transport_status": "ok", "undefined_symbols": undefined}
    return {"accepted": True, "fact_id": fact_id, "transport_status": "ok",
            "undefined_symbols": undefined}


def fact_search(query: str, limit: int = 10, project: Optional[str] = None) -> Dict[str, Any]:
    """BM25 search over the verified fact graph (statement + proof + glossary),
    the derived fact index rebuilt on demand from the fact files — the fact graph
    stays the single source of truth. Use it **before proving** to check whether a
    fact like yours already exists, and to find the verified facts that bear on
    your subgoal so you can cite their ``fact_id``. Returns ranked ``{fact_id,
    score, statement}``. Main agent: pass ``project`` to search a specific
    project's graph; workers omit it."""
    return {"query": query, "results": _fg(project).search(query, limit=limit)}


def fact_revoke(fact_id: str, reason: str, project: Optional[str] = None) -> Dict[str, Any]:
    """Cascade-revoke a wrong fact and everything that depends on it. Destructive;
    operator / main-agent only. Main agent: pass ``project`` to target the project
    that owns the fact."""
    revoked = _fg(project).revoke(fact_id, reason=reason)
    return {"revoked": revoked}


# --------------------------------------------------------------------------- #
# arXiv theorem search (external integration)                                 #
# --------------------------------------------------------------------------- #

def search_arxiv_theorems(query: str, num_results: int = 10) -> Dict[str, Any]:
    """Semantic search over arXiv theorem statements (Matlas). Returns
    **verbatim, as-published** theorem / lemma / definition statements — statement
    fidelity matters for math reasoning and citation checking. Phrase the query as
    a *complete mathematical statement* when possible. Returns ranked results,
    each with ``title``, the full ``theorem`` text, ``arxiv_id``, and the in-paper
    ``theorem_id``. External HTTP, no auth; on outage returns an ``error`` and
    empty ``results`` (retry / fall back to built-in web search)."""
    return _arxiv_search(query, num_results=num_results)


# --------------------------------------------------------------------------- #
# role-based registration                                                     #
# --------------------------------------------------------------------------- #

_TOOLS = {
    "gm_add": gm_add,
    "gm_search": gm_search,
    "fact_submit": fact_submit,
    "fact_search": fact_search,
    "fact_revoke": fact_revoke,
    "search_arxiv_theorems": search_arxiv_theorems,
}


def build_app(role: Optional[str] = None) -> FastMCP:
    """Build the stdio MCP app exposing exactly the tools ``role`` may use.
    ``role`` defaults to ``EULER_ROLE`` (env); unset falls back to the read-only
    verifier set (fail-closed)."""
    app = FastMCP("euler-core")
    for name in tools_for(role if role is not None else _role()):
        app.tool(name=name)(_TOOLS[name])
    return app
