# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Immutable round launch/result provenance receipts.

Written by the orchestrator immediately before and after ``run_round``. Not a
mathematical verdict. Failure to persist or verify a launch receipt is
``RECEIPT_FAILED`` and must not exec codex. Old start-only dry receipts are
legacy artifacts; they are not valid launches.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from euler.core._util import utc_now
from euler.providers.usage import (
    COUNTERS,
    FORMATS,
    microusd_from_cost_usd,
    read_native_usage,
)

from . import layout as L
from .memory_digest import (
    RoundReceiptError,
    exclusive_write_json,
    hash_receipt_payload,
    StartReceipt,
)

SCHEMA_VERSION = 2


def launch_receipt_path(wl: L.WorkerLayout, round_id: int) -> Path:
    return wl.receipts / f"round_{int(round_id):04d}.launch.json"


def result_receipt_path(wl: L.WorkerLayout, round_id: int) -> Path:
    return wl.receipts / f"round_{int(round_id):04d}.result.json"


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def provider_identity(role: Mapping[str, Any] | None = None) -> str:
    """Worker provider name for launch receipts. Never an API-vendor alias."""
    from euler.providers.registry import provider_from_role

    if role:
        return provider_from_role(role).value
    return "unknown"


@dataclass
class LaunchReceipt:
    path: Path
    payload: Dict[str, Any]
    receipt_hash: str
    round_id: int


def _read_json(path: Path, phase: str) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise RoundReceiptError(phase, f"cannot read {phase} receipt {path}: {e}",
                                path=str(path)) from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RoundReceiptError(phase, f"{phase} receipt is not JSON: {e}",
                                path=str(path)) from e
    if not isinstance(payload, dict):
        raise RoundReceiptError(phase, f"{phase} receipt is not a JSON object",
                                path=str(path))
    return payload


def verify_launch_payload(payload: Mapping[str, Any]) -> str:
    stored = payload.get("receipt_hash")
    if not isinstance(stored, str) or not stored:
        raise RoundReceiptError("launch", "launch receipt missing receipt_hash")
    required = (
        "schema_version", "kind", "round_id", "run_id", "project_id", "worker_id",
        "branch_id", "subgoal_id", "start_receipt_hash", "model", "provider",
        "backend", "reasoning_effort", "prompt_sha256", "created_at",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise RoundReceiptError("launch", f"launch receipt missing fields {missing}")
    if payload.get("kind") != "launch":
        raise RoundReceiptError("launch", "launch receipt kind must be 'launch'")
    if payload.get("schema_version") not in {1, SCHEMA_VERSION}:
        raise RoundReceiptError(
            "launch",
            f"unsupported launch schema_version {payload.get('schema_version')}",
        )
    for key in (
        "run_id", "project_id", "worker_id", "branch_id", "subgoal_id",
        "model", "provider", "backend", "reasoning_effort", "created_at",
    ):
        if not isinstance(payload.get(key), str) or not payload.get(key).strip():
            raise RoundReceiptError("launch", f"launch receipt has invalid {key}")
    for key in ("start_receipt_hash", "prompt_sha256"):
        value = payload.get(key)
        if (not isinstance(value, str) or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)):
            raise RoundReceiptError("launch", f"launch receipt has invalid {key}")
    if not isinstance(payload.get("round_id"), int) or payload.get("round_id") < 1:
        raise RoundReceiptError("launch", "launch receipt has invalid round_id")
    output_format = payload.get("output_format")
    if payload.get("schema_version") == SCHEMA_VERSION:
        if output_format is None:
            raise RoundReceiptError("launch", "schema 2 launch receipt missing output_format")
        if not isinstance(output_format, str) or not output_format.strip():
            raise RoundReceiptError("launch", "launch receipt has invalid output_format")
    elif output_format is not None:
        if not isinstance(output_format, str) or not output_format.strip():
            raise RoundReceiptError("launch", "launch receipt has invalid output_format")
    recomputed = hash_receipt_payload(payload)
    if recomputed != stored:
        raise RoundReceiptError(
            "launch",
            "launch receipt_hash does not match canonical payload",
            expected=recomputed, stored=stored,
        )
    return stored


def verify_result_payload(payload: Mapping[str, Any]) -> str:
    stored = payload.get("receipt_hash")
    if not isinstance(stored, str) or not stored:
        raise RoundReceiptError("result", "result receipt missing receipt_hash")
    required = (
        "schema_version", "kind", "round_id", "run_id", "project_id", "worker_id",
        "launch_receipt_hash", "rc", "elapsed_ms", "completed_at",
        "usage_status", "tokens", "cost_usd",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise RoundReceiptError("result", f"result receipt missing fields {missing}")
    if payload.get("kind") != "result":
        raise RoundReceiptError("result", "result receipt kind must be 'result'")
    if payload.get("schema_version") not in {1, SCHEMA_VERSION}:
        raise RoundReceiptError(
            "result",
            f"unsupported result schema_version {payload.get('schema_version')}",
        )
    status = payload.get("usage_status")
    # V1 remains unreadable as accounting. V2 accepts only an explicitly
    # selected native protocol observation, retaining unknown fields as null.
    allowed = {"not_reported"} if payload.get("schema_version") == 1 else {"not_reported", "provider_reported"}
    if status not in allowed:
        raise RoundReceiptError("result", f"invalid usage_status {status!r}")
    if status == "not_reported" and (payload.get("tokens") is not None or payload.get("cost_usd") is not None):
        raise RoundReceiptError(
            "result", "usage_status=not_reported requires tokens and cost_usd null",
        )
    observation = payload.get("native_usage")
    if status == "not_reported" and isinstance(observation, dict) and observation.get("complete") is True:
        raise RoundReceiptError(
            "result", "usage_status=not_reported cannot carry a complete native observation"
        )
    if payload.get("schema_version") == 1 and observation is not None:
        raise RoundReceiptError("result", "schema 1 result cannot carry native_usage")
    if status == "provider_reported":
        _verify_native_usage_observation(observation)
        counters = observation["counters"]
        if payload.get("tokens") != counters:
            raise RoundReceiptError("result", "usage summary does not match native counters")
        claimed_usd = payload.get("cost_usd")
        native_micro = observation.get("cost_microusd")
        if claimed_usd is not None:
            converted = microusd_from_cost_usd(claimed_usd)
            if converted is None or native_micro is None or converted != native_micro:
                raise RoundReceiptError(
                    "result", "cost_usd does not match native cost_microusd"
                )
    for key in ("run_id", "project_id", "worker_id", "completed_at"):
        if not isinstance(payload.get(key), str) or not payload.get(key).strip():
            raise RoundReceiptError("result", f"result receipt has invalid {key}")
    launch_hash = payload.get("launch_receipt_hash")
    if (not isinstance(launch_hash, str) or len(launch_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in launch_hash)):
        raise RoundReceiptError("result", "result receipt has invalid launch_receipt_hash")
    if not isinstance(payload.get("round_id"), int) or payload.get("round_id") < 1:
        raise RoundReceiptError("result", "result receipt has invalid round_id")
    if isinstance(payload.get("rc"), bool) or not isinstance(payload.get("rc"), int):
        raise RoundReceiptError("result", "result receipt has invalid rc")
    if (isinstance(payload.get("elapsed_ms"), bool)
            or not isinstance(payload.get("elapsed_ms"), int)
            or payload.get("elapsed_ms") < 0):
        raise RoundReceiptError("result", "result receipt has invalid elapsed_ms")
    recomputed = hash_receipt_payload(payload)
    if recomputed != stored:
        raise RoundReceiptError(
            "result",
            "result receipt_hash does not match canonical payload",
            expected=recomputed, stored=stored,
        )
    return stored


def read_and_verify_launch(path: Path) -> Dict[str, Any]:
    payload = _read_json(path, "launch")
    verify_launch_payload(payload)
    return payload


def read_and_verify_result(path: Path) -> Dict[str, Any]:
    payload = _read_json(path, "result")
    verify_result_payload(payload)
    return payload


def persist_launch_receipt(
    wl: L.WorkerLayout,
    start: StartReceipt,
    prompt: str,
    role: Mapping[str, Any],
) -> LaunchReceipt:
    """O_EXCL write + re-read/verify. Fail closed before codex on any error."""
    task = start.task
    path = launch_receipt_path(wl, start.round_id)
    try:
        payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "launch",
            "output_format": FORMATS.get(provider_identity(role), "plain"),
            "round_id": start.round_id,
            "run_id": start.payload.get("run_id") or os.environ.get("EULER_RUN_ID") or "",
            "project_id": start.payload.get("project_id") or wl.project,
            "worker_id": wl.name,
            "branch_id": task.branch_id or "",
            "subgoal_id": task.subgoal_id or "",
            "start_receipt": str(start.path),
            "start_receipt_hash": start.receipt_hash,
            "model": str(role.get("MODEL") or ""),
            "provider": provider_identity(role),
            "backend": provider_identity(role),
            "reasoning_effort": str(role.get("REASONING_EFFORT") or ""),
            "prompt_sha256": prompt_sha256(prompt),
            "created_at": utc_now(),
        }
        payload["receipt_hash"] = hash_receipt_payload(payload)
        verify_launch_payload(payload)
        exclusive_write_json(path, payload)
        again = read_and_verify_launch(path)
        if again.get("receipt_hash") != payload["receipt_hash"]:
            raise RoundReceiptError("launch", "launch receipt hash mismatch after persist")
        if again.get("start_receipt_hash") != start.receipt_hash:
            raise RoundReceiptError("launch", "launch receipt not linked to start_receipt_hash")
        if again.get("prompt_sha256") != prompt_sha256(prompt):
            raise RoundReceiptError("launch", "launch prompt_sha256 mismatch after persist")
    except RoundReceiptError:
        raise
    except OSError as e:
        raise RoundReceiptError("launch", f"failed to persist launch receipt: {e}") from e
    except Exception as e:
        raise RoundReceiptError("launch", f"failed to build launch receipt: {e}") from e
    return LaunchReceipt(
        path=path, payload=payload, receipt_hash=payload["receipt_hash"],
        round_id=start.round_id,
    )


def _verify_native_usage_observation(observation: object) -> Dict[str, Any]:
    if not isinstance(observation, dict) or observation.get("complete") is not True:
        raise RoundReceiptError("result", "reported usage requires a complete native observation")
    if observation.get("schema_version") not in (None, 1):
        raise RoundReceiptError("result", "unsupported native_usage schema_version")
    if FORMATS.get(observation.get("provider")) != observation.get("output_format"):
        raise RoundReceiptError("result", "usage protocol/provider mismatch")
    convention = observation.get("input_convention")
    if not isinstance(convention, str) or not convention.strip():
        raise RoundReceiptError("result", "native_usage missing input_convention")
    counters = observation.get("counters")
    if not isinstance(counters, dict) or set(counters) != set(COUNTERS):
        raise RoundReceiptError("result", "invalid native counters")
    for value in (*counters.values(), observation.get("cost_microusd")):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise RoundReceiptError("result", "usage counter must be a nonnegative integer or null")
    digest = observation.get("source_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise RoundReceiptError("result", "usage source hash is invalid")
    return observation


def bind_result_usage_to_launch(
    result_payload: Mapping[str, Any], launch_payload: Mapping[str, Any]
) -> None:
    """Require native usage to name the launch-stamped provider and format.

    Schema 1 receipts have no protocol stamp and cannot carry settled usage.
    This is receipt binding, not cryptographic attestation of the CLI.
    """
    status = result_payload.get("usage_status") or "not_reported"
    observation = result_payload.get("native_usage")
    if result_payload.get("schema_version") == 1:
        if status != "not_reported":
            raise RoundReceiptError("result", "schema 1 result cannot report native usage")
        if observation is not None:
            raise RoundReceiptError("result", "schema 1 result cannot carry native_usage")
        return
    launch_provider = launch_payload.get("provider")
    launch_format = launch_payload.get("output_format") or "plain"
    if status == "not_reported":
        if isinstance(observation, dict) and observation.get("complete") is True:
            raise RoundReceiptError(
                "result", "not_reported result cannot carry a complete native observation"
            )
        return
    if status != "provider_reported":
        raise RoundReceiptError("result", f"invalid usage_status {status!r}")
    bound = _verify_native_usage_observation(observation)
    if bound.get("provider") != launch_provider:
        raise RoundReceiptError("result", "native_usage provider does not match launch receipt")
    if bound.get("output_format") != launch_format:
        raise RoundReceiptError("result", "native_usage output_format does not match launch receipt")
    if FORMATS.get(launch_provider) != launch_format:
        raise RoundReceiptError(
            "result", "launch receipt is not bound to a trusted native usage protocol"
        )


def observe_usage(log_path: Optional[Path], *, provider: str = "",
                  output_format: str = "plain") -> Dict[str, Any]:
    """Read a launch-selected native protocol; unknown/plain output stays null.

    ``cost_usd`` stays null. Provider-reported money is integer micro-USD on
    ``native_usage``; a float USD field would claim a billed amount.
    """
    unknown = {
        "usage_status": "not_reported", "tokens": None, "cost_usd": None,
    }
    if log_path is None or FORMATS.get(provider) != output_format:
        return unknown
    try:
        observed = read_native_usage(log_path, provider=provider, output_format=output_format)
    except OSError:
        return unknown
    if not observed.complete:
        return {**unknown, "native_usage": observed.to_dict()}
    return {
        "usage_status": "provider_reported",
        "tokens": observed.counters,
        "cost_usd": None,
        "native_usage": observed.to_dict(),
    }


def persist_result_receipt(
    wl: L.WorkerLayout,
    launch: LaunchReceipt,
    rc: int,
    elapsed_ms: int,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """O_EXCL write + re-read/verify the companion result receipt."""
    path = result_receipt_path(wl, launch.round_id)
    try:
        usage = observe_usage(log_path, provider=launch.payload.get("provider", ""),
                              output_format=launch.payload.get("output_format", "plain")) if rc == 0 else observe_usage(None)
        payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "result",
            "round_id": launch.round_id,
            "run_id": launch.payload.get("run_id") or "",
            "project_id": launch.payload.get("project_id") or wl.project,
            "worker_id": wl.name,
            "launch_receipt": str(launch.path),
            "launch_receipt_hash": launch.receipt_hash,
            "rc": int(rc),
            "elapsed_ms": int(elapsed_ms),
            "completed_at": utc_now(),
            "usage_status": usage["usage_status"],
            "tokens": usage["tokens"],
            "cost_usd": usage["cost_usd"],
        }
        if "native_usage" in usage:
            payload["native_usage"] = usage["native_usage"]
        payload["receipt_hash"] = hash_receipt_payload(payload)
        verify_result_payload(payload)
        exclusive_write_json(path, payload)
        again = read_and_verify_result(path)
        if again.get("receipt_hash") != payload["receipt_hash"]:
            raise RoundReceiptError("result", "result receipt hash mismatch after persist")
        if again.get("launch_receipt_hash") != launch.receipt_hash:
            raise RoundReceiptError("result", "result receipt not linked to launch hash")
        if again.get("usage_status") == "not_reported":
            if again.get("tokens") is not None or again.get("cost_usd") is not None:
                raise RoundReceiptError("result", "invented usage values")
    except RoundReceiptError:
        raise
    except OSError as e:
        raise RoundReceiptError("result", f"failed to persist result receipt: {e}") from e
    except Exception as e:
        raise RoundReceiptError("result", f"failed to build result receipt: {e}") from e
    return payload
