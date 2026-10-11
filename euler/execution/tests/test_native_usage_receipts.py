"""Schema 2 native usage receipts bound to the launch protocol stamp."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from euler.execution import layout as L
from euler.execution.launch_receipt import (
    bind_result_usage_to_launch,
    persist_launch_receipt,
    persist_result_receipt,
    read_and_verify_launch,
    read_and_verify_result,
    verify_launch_payload,
    verify_result_payload,
)
from euler.execution.memory_digest import RoundReceiptError, hash_receipt_payload, prepare_round_start
from euler.execution.scaffold import write_codex_config
from euler.orchestration import cli
from euler.providers.usage import parse_native_usage


@contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _project_env(tmp: Path):
    contract = tmp / "worker.md"
    contract.write_text("# worker contract (stub)\n", encoding="utf-8")
    skills = tmp / "skills"
    skills.mkdir(exist_ok=True)
    runtime = tmp / "runtime"
    (runtime / "run").mkdir(parents=True, exist_ok=True)
    env = {
        "EULER_AGENTS_ROOT": str(tmp / "agents"),
        "EULER_WORKER_CONTRACT": str(contract),
        "EULER_WORKER_SKILLS": str(skills),
        "EULER_RUNTIME": str(runtime),
        "EULER_RUN_ID": "run-usage-aaaa-bbbb-cccc",
        "EULER_CODEX_MODEL": "gpt-5.5",
        "EULER_CODEX_EFFORT": "high",
    }
    with _env(**env):
        yield


def _prep(tmp: Path, project: str = "U") -> L.WorkerLayout:
    cli.do_new(project, roles="high:1")
    cli.do_assign(f"{project}/high", "prove it", branch_id="b1", subgoal_id="s1")
    wl = L.WorkerLayout(L.worker_dir(project, "high"))
    write_codex_config(wl)
    os.environ["EULER_PROJECT_DIR"] = str(wl.project_dir)
    os.environ["EULER_PROJECT_ID"] = project
    os.environ["EULER_WORKER_ID"] = wl.name
    os.environ["EULER_AUTHOR"] = wl.name
    os.environ["EULER_BRANCH_ID"] = "b1"
    os.environ["EULER_SUBGOAL_ID"] = "s1"
    return wl


def _launch(wl: L.WorkerLayout, *, provider: str = "codex"):
    start = prepare_round_start(wl, 1)
    role = {
        "MODEL": "m",
        "REASONING_EFFORT": "high",
        "PROVIDER": provider,
    }
    return start, persist_launch_receipt(wl, start, "kickoff", role)


def _stream(*events) -> bytes:
    return ("\n".join(json.dumps(item) for item in events) + "\n").encode()


def _write_log(wl: L.WorkerLayout, data: bytes) -> Path:
    wl.logs.mkdir(parents=True, exist_ok=True)
    path = wl.logs / "round_1.log"
    path.write_bytes(data)
    return path


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def test_schema2_launch_stamps_output_format(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "stamp")
        _start, launch = _launch(wl, provider="codex")
        rec = read_and_verify_launch(launch.path)
        assert rec["schema_version"] == 2
        assert rec["provider"] == "codex"
        assert rec["output_format"] == "codex-json"


def test_codex_json_log_becomes_provider_reported_without_inventing_usd(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "codexlog")
        _start, launch = _launch(wl, provider="codex")
        log = _write_log(wl, _stream({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 60,
            },
        }))
        result = persist_result_receipt(wl, launch, 0, 12, log_path=log)
        assert result["usage_status"] == "provider_reported"
        assert result["tokens"]["input_tokens"] == 100
        assert result["tokens"]["cached_tokens"] == 60
        assert result["tokens"]["reasoning_tokens"] is None
        assert result["cost_usd"] is None
        assert result["native_usage"]["cost_microusd"] is None
        assert result["native_usage"]["complete"] is True
        again = read_and_verify_result(wl.receipts / "round_0001.result.json")
        bind_result_usage_to_launch(again, rec := read_and_verify_launch(launch.path))
        assert rec["output_format"] == again["native_usage"]["output_format"]


def test_incomplete_stream_stays_not_reported(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "incomplete")
        _start, launch = _launch(wl, provider="grok")
        log = _write_log(wl, _stream({
            "type": "usage",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }))
        result = persist_result_receipt(wl, launch, 0, 9, log_path=log)
        assert result["usage_status"] == "not_reported"
        assert result["tokens"] is None
        assert result["native_usage"]["complete"] is False


def test_schema1_result_still_verifies_without_native_fields():
    payload = {
        "schema_version": 1,
        "kind": "result",
        "round_id": 1,
        "run_id": "run-1",
        "project_id": "demo",
        "worker_id": "w1",
        "launch_receipt_hash": "a" * 64,
        "rc": 0,
        "elapsed_ms": 4,
        "completed_at": "2026-01-01T00:00:00Z",
        "usage_status": "not_reported",
        "tokens": None,
        "cost_usd": None,
    }
    payload["receipt_hash"] = hash_receipt_payload(payload)
    verify_result_payload(payload)
    launch = {
        "schema_version": 1,
        "kind": "launch",
        "round_id": 1,
        "run_id": "run-1",
        "project_id": "demo",
        "worker_id": "w1",
        "branch_id": "b1",
        "subgoal_id": "s1",
        "start_receipt_hash": "b" * 64,
        "model": "m",
        "provider": "codex",
        "backend": "codex",
        "reasoning_effort": "high",
        "prompt_sha256": "c" * 64,
        "created_at": "2026-01-01T00:00:00Z",
    }
    launch["receipt_hash"] = hash_receipt_payload(launch)
    verify_launch_payload(launch)
    bind_result_usage_to_launch(payload, launch)


def test_schema1_hash_does_not_accept_schema2_native_fields():
    payload = {
        "schema_version": 1,
        "kind": "result",
        "round_id": 1,
        "run_id": "run-1",
        "project_id": "demo",
        "worker_id": "w1",
        "launch_receipt_hash": "a" * 64,
        "rc": 0,
        "elapsed_ms": 4,
        "completed_at": "2026-01-01T00:00:00Z",
        "usage_status": "not_reported",
        "tokens": None,
        "cost_usd": None,
        "native_usage": {"complete": False},
    }
    payload["receipt_hash"] = hash_receipt_payload(payload)
    _raises(RoundReceiptError, verify_result_payload, payload)


def test_tampered_counters_and_source_hash_fail(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "tamper")
        _start, launch = _launch(wl, provider="codex")
        log = _write_log(wl, _stream({
            "type": "turn.completed",
            "usage": {"input_tokens": 11, "output_tokens": 2},
        }))
        persisted = persist_result_receipt(wl, launch, 0, 5, log_path=log)
        assert persisted["usage_status"] == "provider_reported"
        path = wl.receipts / "round_0001.result.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["tokens"] = dict(payload["tokens"])
        payload["tokens"]["input_tokens"] = 99
        payload["receipt_hash"] = hash_receipt_payload(payload)
        _raises(RoundReceiptError, verify_result_payload, payload)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["native_usage"] = dict(payload["native_usage"])
        payload["native_usage"]["source_sha256"] = "0" * 64
        _raises(RoundReceiptError, verify_result_payload, payload)


def test_mismatched_launch_provider_or_format_is_rejected(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "mismatch")
        _start, launch = _launch(wl, provider="codex")
        log = _write_log(wl, _stream({
            "type": "turn.completed",
            "usage": {"input_tokens": 8, "output_tokens": 1},
        }))
        result = persist_result_receipt(wl, launch, 0, 3, log_path=log)
        forged = dict(launch.payload)
        forged["provider"] = "grok"
        forged["output_format"] = "grok-streaming-json"
        _raises(RoundReceiptError, bind_result_usage_to_launch, result, forged)


def test_plain_log_text_cannot_become_native_usage(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "plain")
        _start, launch = _launch(wl, provider="codex")
        log = _write_log(
            wl,
            b"usage: input_tokens=99 cost_usd=1.5\n"
            + json.dumps({"type": "agent_message", "text": json.dumps({
                "type": "turn.completed", "usage": {"input_tokens": 99},
            })}).encode()
            + b"\n",
        )
        result = persist_result_receipt(wl, launch, 0, 2, log_path=log)
        assert result["usage_status"] == "not_reported"
        assert result["tokens"] is None


def test_source_sha256_matches_raw_log_bytes(tmp: Path):
    with _project_env(tmp):
        wl = _prep(tmp, "hash")
        _start, launch = _launch(wl, provider="codex")
        data = _stream({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}})
        log = _write_log(wl, data)
        result = persist_result_receipt(wl, launch, 0, 1, log_path=log)
        assert result["native_usage"]["source_sha256"] == hashlib.sha256(data).hexdigest()
        parsed = parse_native_usage(data, provider="codex", output_format="codex-json")
        assert parsed.source_sha256 == result["native_usage"]["source_sha256"]
