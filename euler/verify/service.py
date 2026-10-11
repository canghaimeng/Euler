# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Euler verify service — the sole write-gate's HTTP front.

    POST /verify {statement, proof, force_fresh?} -> {verification_report, verdict, repair_hints}
    GET  /health                    -> {status: "ok", pid: <int>, euler_root: <str>}

/health is **transport identity only** — it never reports a mathematical verdict.
Callers must match ``pid`` against ``runtime/run/verify.pid`` (and ``euler_root``
against this deployment) so a foreign process on the same port cannot look "up".

/verify runs the deterministic pre-checks (``prechecks.run_prechecks``) and then
reuses a completed, schema-valid result only when the exact length-delimited
``(statement, proof)`` fingerprint matches, unless ``force_fresh`` is true.
``force_fresh=false`` (the default, and the gateway ``fact_submit`` path) keeps
that retry cache.  ``force_fresh=true`` skips cache lookup and always allocates
a new verifier run, still writing the exact-input request manifest and result.
Otherwise it cold-starts a fresh configured verifier agent
(``launcher.run_verification``).  The durable exact-input cache lets an MCP
retry recover after a client timeout without accepting a verdict for changed
proof text.  The gateway's ``fact_submit`` uses the returned
verdict to decide whether a claim becomes a fact. The verifier is an LLM, NOT a
formal proof assistant, with no human in the loop by default — see the verifier
contract (``agents/contracts/verifier.md``).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StrictBool

from .launcher import (
    VERIFICATION_FILENAMES,
    _allocate_run_id,
    _results_root,
    _validate_payload,
    run_verification,
)
from .prechecks import run_prechecks


class VerifyRequest(BaseModel):
    statement: str = Field(..., min_length=1)
    proof: str = Field(..., min_length=1)
    force_fresh: StrictBool = False


app = FastAPI(title="Euler verify service", version="0.1.0")

_REQUEST_MANIFEST = "request.json"


def _request_fingerprint(statement: str, proof: str) -> str:
    """Content key for an exact verifier request.

    Both fields are length-delimited so distinct ``(statement, proof)`` pairs
    cannot collide through delimiter ambiguity.  The cache never keys on the
    statement alone: changing the proof must trigger a fresh verification.
    """
    h = hashlib.sha256()
    for value in (statement, proof):
        raw = value.encode("utf-8")
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return h.hexdigest()


def _request_manifest_path(run_id: str) -> Path:
    return _results_root() / run_id / _REQUEST_MANIFEST


def _write_request_manifest(run_id: str, statement: str, proof: str) -> None:
    """Persist the exact-input cache key before starting the verifier.

    The run directory is allocated atomically by ``_allocate_run_id``.  The
    manifest write is atomic as well, so an interrupted request is either
    absent or complete and can never masquerade as a cache hit.
    """
    path = _request_manifest_path(run_id)
    payload = {
        "schema_version": 1,
        "fingerprint": _request_fingerprint(statement, proof),
        "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
        "proof_sha256": hashlib.sha256(proof.encode("utf-8")).hexdigest(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _find_cached_result(statement: str, proof: str) -> Dict[str, Any] | None:
    """Return a completed exact-input verification, newest first.

    Only verifier-owned run directories containing both a matching request
    manifest and a schema-valid ``verification.json`` are reusable.  Partial,
    malformed, or legacy runs are ignored.  This makes an MCP retry after a
    client-side timeout fast without weakening the correctness gate.
    """
    root = _results_root()
    if not root.is_dir():
        return None
    wanted = _request_fingerprint(statement, proof)
    try:
        run_dirs = sorted(
            (path for path in root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return None
    for run_dir in run_dirs:
        manifest_path = run_dir / _REQUEST_MANIFEST
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(manifest, dict) or manifest.get("fingerprint") != wanted:
            continue
        for filename in VERIFICATION_FILENAMES:
            verification_path = run_dir / filename
            try:
                payload = json.loads(verification_path.read_text(encoding="utf-8"))
                return _validate_payload(payload, verification_path)
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError, HTTPException, TypeError):
                break
    return None


def _deployment_root() -> str:
    env = os.environ.get("EULER_ROOT")
    if env:
        return str(Path(env).resolve())
    # euler/verify/service.py → repo root (parents[2])
    return str(Path(__file__).resolve().parents[2])


@app.get("/health")
async def health() -> Dict[str, Any]:
    # async on purpose: /health must not queue behind sync /verify threadpool
    # calls, so it responds in ~microseconds regardless of in-flight verifications.
    # Transport identity only — not a mathematical verdict. `pid` + `euler_root`
    # self-identify this instance: a health probe alone cannot tell OUR verify
    # from another deployment's verify holding the same port on a shared host.
    # Callers match pid against runtime/run/verify.pid and euler_root against
    # this checkout.
    return {"status": "ok", "pid": os.getpid(), "euler_root": _deployment_root()}


@app.post("/verify")
def verify(request: VerifyRequest) -> Dict[str, Any]:
    rejected = run_prechecks(request.statement, request.proof)
    if rejected is not None:
        status_code, detail = rejected
        raise HTTPException(status_code=status_code, detail=detail)
    if not request.force_fresh:
        cached = _find_cached_result(request.statement, request.proof)
        if cached is not None:
            return cached
    run_id = _allocate_run_id(request.statement)
    _write_request_manifest(run_id, request.statement, request.proof)
    return run_verification(run_id=run_id, statement=request.statement, proof=request.proof)
