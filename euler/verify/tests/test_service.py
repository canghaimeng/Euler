# SPDX-License-Identifier: Apache-2.0
# Adapted from Danus/Rethlas; modified for Euler Agent by canghaimeng.
"""Offline HTTP-contract tests for euler.verify.service + __main__ entry.

Exercises the FastAPI app via TestClient with the launcher's agent run
MONKEYPATCHED to a fake (no subprocess and no model spend), asserting the
POST /verify + GET /health contract and every error status mapping. The
``python -m euler.verify`` entrypoint is exercised via runpy with uvicorn.run
mocked so no server ever binds.

HTTP contract under test:
  POST /verify {statement, proof, force_fresh?} -> 200 {verification_report, verdict, repair_hints}
  * 400 on a vacuous / precheck-failing input (before any codex run)
  * 422 on a schema-invalid body (missing/empty field, or non-bool force_fresh)
  * 504 on codex timeout, 500 on exit / missing-output / bad-json (launcher raises)
  GET /health -> 200 {status: "ok", pid: int, euler_root: str}
  /health is transport identity only — never a mathematical verdict.

Runs standalone (``python -m euler.verify.tests.test_service``) and under pytest.
"""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.testclient import TestClient

from euler.verify import service

_STMT = "For every integer n, n + 0 equals n."
_PROOF = (
    "Zero is the additive identity of the integers, so adding zero to any integer n "
    "leaves the value unchanged. Hence n + 0 = n for every integer n, as required."
)

_CANNED_OK = {
    "verification_report": {"summary": "fake accept", "critical_errors": [], "gaps": []},
    "verdict": "correct",
    "repair_hints": "",
}


@contextmanager
def _fake_run(fn):
    """Replace the configured agent run (imported into service) with a fake."""
    orig_run = service.run_verification
    orig_alloc = service._allocate_run_id
    orig_find = service._find_cached_result
    orig_manifest = service._write_request_manifest
    service.run_verification = fn
    service._allocate_run_id = lambda statement: "RID-fake"
    service._find_cached_result = lambda statement, proof: None
    service._write_request_manifest = lambda run_id, statement, proof: None
    try:
        yield
    finally:
        service.run_verification = orig_run
        service._allocate_run_id = orig_alloc
        service._find_cached_result = orig_find
        service._write_request_manifest = orig_manifest


def _client():
    return TestClient(service.app)


# --------------------------------------------------------------------------- #
# /health                                                                     #
# --------------------------------------------------------------------------- #

def test_health_ok():
    resp = _client().get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # /health self-identifies with the serving process pid (callers match it
    # against runtime/run/verify.pid to distinguish OUR verify from a foreign
    # deployment holding the same port on a shared host).
    assert isinstance(body["pid"], int) and body["pid"] > 0
    # euler_root pins the responder to this checkout (not merely any :8091).
    assert isinstance(body.get("euler_root"), str) and body["euler_root"]
    from pathlib import Path
    assert Path(body["euler_root"]).is_dir()


# --------------------------------------------------------------------------- #
# /verify — happy path                                                        #
# --------------------------------------------------------------------------- #

def test_verify_accept_contract():
    def fake(run_id, statement, proof):
        assert run_id == "RID-fake"  # allocator was used
        return _CANNED_OK

    with _fake_run(fake):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "correct"
    assert body["verification_report"]["critical_errors"] == []
    assert "repair_hints" in body


def test_verify_reject_verdict_still_200():
    # a "wrong" verdict is a normal 200 response (the verdict is the payload).
    canned = dict(_CANNED_OK, verdict="wrong", repair_hints="fix the gap")
    with _fake_run(lambda run_id, statement, proof: canned):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
    assert resp.status_code == 200 and resp.json()["verdict"] == "wrong"


def test_verify_exact_cache_hit_skips_cold_start():
    orig_find = service._find_cached_result
    orig_run = service.run_verification
    service._find_cached_result = lambda statement, proof: _CANNED_OK
    service.run_verification = _must_not_run
    try:
        omitted = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
        explicit = _client().post(
            "/verify",
            json={"statement": _STMT, "proof": _PROOF, "force_fresh": False},
        )
    finally:
        service._find_cached_result = orig_find
        service.run_verification = orig_run
    assert omitted.status_code == 200
    assert omitted.json()["verdict"] == "correct"
    assert explicit.status_code == 200
    assert explicit.json()["verdict"] == "correct"


def test_verify_force_fresh_skips_cache_and_runs_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_results_root", lambda: tmp_path)
    calls = []

    def fake(run_id, statement, proof):
        calls.append(run_id)
        dest = tmp_path / run_id / "verification.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(_CANNED_OK), encoding="utf-8")
        return _CANNED_OK

    orig_run = service.run_verification
    orig_alloc = service._allocate_run_id
    service.run_verification = fake
    service._allocate_run_id = lambda statement: f"RID-{len(calls)+1}"
    try:
        first = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
        cached = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
        fresh = _client().post(
            "/verify",
            json={"statement": _STMT, "proof": _PROOF, "force_fresh": True},
        )
    finally:
        service.run_verification = orig_run
        service._allocate_run_id = orig_alloc
    assert first.status_code == 200 and first.json()["verdict"] == "correct"
    assert cached.status_code == 200
    assert cached.json() == first.json()
    assert fresh.status_code == 200
    assert fresh.json()["verdict"] == "correct"
    assert calls == ["RID-1", "RID-2"]
    assert (tmp_path / "RID-1" / service._REQUEST_MANIFEST).is_file()
    assert (tmp_path / "RID-2" / service._REQUEST_MANIFEST).is_file()


def test_request_fingerprint_includes_proof():
    key = service._request_fingerprint(_STMT, _PROOF)
    assert key == service._request_fingerprint(_STMT, _PROOF)
    assert key != service._request_fingerprint(_STMT, _PROOF + " extra")
    assert key != service._request_fingerprint(_STMT + " extra", _PROOF)


def test_cache_manifest_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_results_root", lambda: tmp_path)
    run_id = "RID-cache"
    (tmp_path / run_id).mkdir()
    service._write_request_manifest(run_id, _STMT, _PROOF)
    manifest = json.loads(
        (tmp_path / run_id / service._REQUEST_MANIFEST).read_text(encoding="utf-8")
    )
    assert manifest["fingerprint"] == service._request_fingerprint(_STMT, _PROOF)
    assert len(manifest["statement_sha256"]) == 64
    assert len(manifest["proof_sha256"]) == 64


def test_cache_reuses_only_exact_valid_result(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_results_root", lambda: tmp_path)
    run = tmp_path / "20260901T000000Z_deadbeef"
    run.mkdir()
    service._write_request_manifest(run.name, _STMT, _PROOF)
    (run / "verification.json").write_text(json.dumps(_CANNED_OK), encoding="utf-8")
    assert service._find_cached_result(_STMT, _PROOF) == _CANNED_OK
    assert service._find_cached_result(_STMT, _PROOF + " changed") is None


# --------------------------------------------------------------------------- #
# /verify — precheck rejections happen BEFORE any agent run (400)             #
# --------------------------------------------------------------------------- #

def _must_not_run(*a, **k):
    raise AssertionError("verifier agent must not run when a precheck rejects")


def test_verify_vacuous_proof_400():
    with _fake_run(_must_not_run):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": "QED"})
    assert resp.status_code == 400 and "vacuous proof" in resp.json()["detail"]


def test_verify_p1_precheck_400():
    bad = ("The result holds as declared in problem.md, which lists it as a verified "
           "building block, so we are done with the argument here.")
    with _fake_run(_must_not_run):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": bad})
    assert resp.status_code == 400 and "[P1 on proof]" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# /verify — launcher error mappings surface as the raised status              #
# --------------------------------------------------------------------------- #

def _raiser(status, detail):
    def fn(run_id, statement, proof):
        raise HTTPException(status_code=status, detail=detail)
    return fn


def test_verify_timeout_504():
    with _fake_run(_raiser(504, "codex exec timed out after 900s")):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
    assert resp.status_code == 504 and "timed out" in resp.json()["detail"]


def test_verify_exit_500():
    with _fake_run(_raiser(500, "codex exec failed with exit code 7")):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
    assert resp.status_code == 500 and "exit code" in resp.json()["detail"]


def test_verify_missing_output_500():
    with _fake_run(_raiser(500, "verification output was not found")):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
    assert resp.status_code == 500 and "was not found" in resp.json()["detail"]


def test_verify_bad_json_500():
    with _fake_run(_raiser(500, "verification output ... is not valid JSON")):
        resp = _client().post("/verify", json={"statement": _STMT, "proof": _PROOF})
    assert resp.status_code == 500 and "not valid JSON" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# /verify — schema validation (pydantic, 422) before prechecks               #
# --------------------------------------------------------------------------- #

def test_verify_empty_field_422():
    with _fake_run(_must_not_run):
        resp = _client().post("/verify", json={"statement": "", "proof": _PROOF})
    assert resp.status_code == 422


def test_verify_missing_field_422():
    with _fake_run(_must_not_run):
        resp = _client().post("/verify", json={"statement": _STMT})
    assert resp.status_code == 422


def test_verify_force_fresh_non_bool_422():
    with _fake_run(_must_not_run):
        as_string = _client().post(
            "/verify",
            json={"statement": _STMT, "proof": _PROOF, "force_fresh": "true"},
        )
        as_int = _client().post(
            "/verify",
            json={"statement": _STMT, "proof": _PROOF, "force_fresh": 1},
        )
    assert as_string.status_code == 422
    assert as_int.status_code == 422


# --------------------------------------------------------------------------- #
# `python -m euler.verify` entry — uvicorn mocked, no bind                    #
# --------------------------------------------------------------------------- #

def test_main_entry_runs_uvicorn(monkeypatch):
    import os
    import runpy

    calls = {}
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(app, host, port):  # noqa: ANN001
        calls["app"] = app
        calls["host"] = host
        calls["port"] = port

    fake_uvicorn.run = fake_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("VERIFY_HOST", "127.0.0.1")
    monkeypatch.setenv("VERIFY_PORT", "8199")
    monkeypatch.delenv("CODEX_TIMEOUT_SECONDS", raising=False)

    runpy.run_module("euler.verify", run_name="__main__")

    assert calls["host"] == "127.0.0.1" and calls["port"] == 8199
    assert calls["app"] is not None
    # the entrypoint sets a bounded default per-verification timeout
    assert os.environ.get("CODEX_TIMEOUT_SECONDS") == "900"


def main() -> None:
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                print(f"  [skip standalone] {name} (needs pytest fixture)")
                continue
            fn()
            print(f"  [ok] {name}")
    print("ALL SERVICE TESTS PASSED")


if __name__ == "__main__":
    main()
