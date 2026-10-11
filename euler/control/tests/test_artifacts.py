"""Offline tests for the research-control content-addressed artifact store.

No network, no runtime projects, no Fact Graph / gateway / verify imports.
Covers bytes/text roundtrip, reopen/retry recovery, duplicate and concurrent
put, tamper rejection, malformed hashes, symlink escape, leftover temp cleanup,
and the truth-boundary static guard.

Runs standalone (``python -m euler.control.tests.test_artifacts``)
and under pytest.
"""

from __future__ import annotations

import ast
import os
import tempfile
import threading
from pathlib import Path

from euler.control.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFound,
    ArtifactRef,
    ArtifactStore,
    ArtifactStoreError,
)
from euler.control.ids import sha256_hex

_ROOT = Path(__file__).resolve().parents[1]


def _raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return
    raise AssertionError(
        f"expected {exc.__name__} from {getattr(fn, '__name__', fn)}"
    )


def _tmp_store():
    return tempfile.TemporaryDirectory()


def _store(path: Path) -> ArtifactStore:
    return ArtifactStore(path / "artifacts")


def _tmp_files(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    for item in root.rglob("*"):
        if item.is_file() and (item.suffix == ".tmp" or item.name.startswith(".")):
            found.append(item)
    return found


# --------------------------------------------------------------------------- #
# ArtifactRef                                                                 #
# --------------------------------------------------------------------------- #


def test_artifact_ref_validates_hash_size_and_media_type():
    digest = "ab" * 32
    ref = ArtifactRef(content_hash=digest, size_bytes=0)
    assert ref.content_hash == digest
    assert ref.size_bytes == 0
    assert ref.media_type is None
    typed = ArtifactRef(
        content_hash=digest, size_bytes=3, media_type="text/plain"
    )
    assert typed.media_type == "text/plain"
    _raises(ArtifactStoreError, ArtifactRef, content_hash="AB" * 32, size_bytes=0)
    _raises(ArtifactStoreError, ArtifactRef, content_hash="ab" * 31, size_bytes=0)
    _raises(ArtifactStoreError, ArtifactRef, content_hash=digest, size_bytes=-1)
    _raises(ArtifactStoreError, ArtifactRef, content_hash=digest, size_bytes=True)
    _raises(ArtifactStoreError, ArtifactRef, content_hash=digest, size_bytes=1.0)
    _raises(
        ArtifactStoreError,
        ArtifactRef,
        content_hash=digest,
        size_bytes=1,
        media_type="",
    )


# --------------------------------------------------------------------------- #
# roundtrip / layout / reopen                                                 #
# --------------------------------------------------------------------------- #


def test_bytes_and_text_roundtrip():
    with _tmp_store() as d:
        store = _store(Path(d))
        payload = b"\x00hello\xff"
        ref = store.put_bytes(payload, media_type="application/octet-stream")
        assert ref.content_hash == sha256_hex(payload)
        assert ref.size_bytes == len(payload)
        assert ref.media_type == "application/octet-stream"
        assert store.get_bytes(ref.content_hash) == payload
        assert store.exists(ref.content_hash) is True
        verified = store.verify(ref.content_hash)
        assert verified.content_hash == ref.content_hash
        assert verified.size_bytes == len(payload)

        blob = store.path_for(ref.content_hash)
        assert blob.name == f"{ref.content_hash}.blob"
        assert blob.parent.name == ref.content_hash[:2]
        assert blob.parent.parent.name == "sha256"
        assert blob.is_file()
        assert not blob.is_symlink()

        text_ref = store.put_text("命题", media_type="text/plain; charset=utf-8")
        assert store.get_text(text_ref.content_hash) == "命题"
        assert store.get_bytes(text_ref.content_hash) == "命题".encode("utf-8")
        empty = store.put_bytes(b"")
        assert empty.size_bytes == 0
        assert store.get_bytes(empty.content_hash) == b""
        _raises(ArtifactNotFound, store.get_bytes, "cd" * 32)
        assert store.exists("cd" * 32) is False
        _raises(ArtifactStoreError, store.put_bytes, "not-bytes")
        _raises(ArtifactStoreError, store.put_text, b"not-text")


def test_reopen_and_retry_recovery():
    with _tmp_store() as d:
        root = Path(d) / "artifacts"
        first = ArtifactStore(root)
        payload = b"retry-me"
        ref = first.put_bytes(payload)
        second = ArtifactStore(root)
        assert second.get_bytes(ref.content_hash) == payload
        assert second.exists(ref.content_hash) is True
        replay = second.put_bytes(payload, media_type="text/plain")
        assert replay.content_hash == ref.content_hash
        assert replay.size_bytes == len(payload)
        assert second.get_text(ref.content_hash, encoding="utf-8") == "retry-me"
        assert _tmp_files(root) == []


# --------------------------------------------------------------------------- #
# idempotent / concurrent put                                                 #
# --------------------------------------------------------------------------- #


def test_duplicate_put_is_idempotent_and_does_not_overwrite():
    with _tmp_store() as d:
        store = _store(Path(d))
        payload = b"same-bytes"
        first = store.put_bytes(payload)
        blob = store.path_for(first.content_hash)
        inode = blob.stat().st_ino
        second = store.put_bytes(payload)
        assert second.content_hash == first.content_hash
        assert blob.stat().st_ino == inode
        assert store.get_bytes(first.content_hash) == payload
        assert _tmp_files(Path(d)) == []


def test_concurrent_same_content_put():
    with _tmp_store() as d:
        root = Path(d) / "artifacts"
        ArtifactStore(root)
        payload = b"concurrent-payload"
        barrier = threading.Barrier(2)
        results: list[ArtifactRef] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                store = ArtifactStore(root)
                barrier.wait(timeout=5)
                ref = store.put_bytes(payload)
                with lock:
                    results.append(ref)
            except BaseException as exc:  # pragma: no cover - diagnostic
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            assert not thread.is_alive()
        assert errors == []
        assert len(results) == 2
        assert results[0].content_hash == results[1].content_hash == sha256_hex(payload)
        store = ArtifactStore(root)
        assert store.get_bytes(results[0].content_hash) == payload
        blob = store.path_for(results[0].content_hash)
        assert blob.is_file()
        assert _tmp_files(root) == []


# --------------------------------------------------------------------------- #
# tamper / malformed / symlink / leftover temps                               #
# --------------------------------------------------------------------------- #


def test_tamper_is_rejected_and_never_overwritten():
    with _tmp_store() as d:
        store = _store(Path(d))
        original = b"authentic-bytes"
        ref = store.put_bytes(original)
        blob = store.path_for(ref.content_hash)
        tampered = b"tampered-bytes-not-the-original"
        blob.write_bytes(tampered)
        _raises(ArtifactIntegrityError, store.get_bytes, ref.content_hash)
        _raises(ArtifactIntegrityError, store.exists, ref.content_hash)
        _raises(ArtifactIntegrityError, store.verify, ref.content_hash)
        _raises(ArtifactIntegrityError, store.put_bytes, original)
        assert blob.read_bytes() == tampered


def test_malformed_hash_is_rejected():
    with _tmp_store() as d:
        store = _store(Path(d))
        ref = store.put_bytes(b"x")
        bad = [
            "",
            "abc",
            "AB" * 32,
            ref.content_hash.upper(),
            ref.content_hash + "a",
            ref.content_hash[:-1],
            "../" + "a" * 61,
            "a" * 63 + "/",
            "g" * 64,
        ]
        for value in bad:
            _raises(ArtifactStoreError, store.get_bytes, value)
            _raises(ArtifactStoreError, store.get_text, value)
            _raises(ArtifactStoreError, store.exists, value)
            _raises(ArtifactStoreError, store.verify, value)
            _raises(ArtifactStoreError, store.path_for, value)


def test_symlink_escape_is_rejected():
    with _tmp_store() as d:
        base = Path(d)
        root = base / "store"
        outside = base / "outside"
        outside.mkdir()
        leaked = outside / "leaked.blob"
        leaked.write_bytes(b"not-in-store")
        store = ArtifactStore(root)
        payload = b"escape-payload"
        digest = sha256_hex(payload)
        sha_dir = root.resolve() / "sha256"
        sha_dir.mkdir(parents=True, exist_ok=True)
        prefix = sha_dir / digest[:2]
        prefix.symlink_to(outside)
        _raises(ArtifactStoreError, store.put_bytes, payload)
        _raises(ArtifactStoreError, store.get_bytes, digest)
        _raises(ArtifactStoreError, store.exists, digest)
        _raises(ArtifactStoreError, store.path_for, digest)
        assert list(outside.glob("*.tmp")) == []

        ok = store.put_bytes(b"inside")
        blob = store.path_for(ok.content_hash)
        os.unlink(blob)
        blob.symlink_to(leaked)
        _raises(ArtifactStoreError, store.get_bytes, ok.content_hash)
        _raises(ArtifactStoreError, store.exists, ok.content_hash)
        _raises(ArtifactStoreError, store.path_for, ok.content_hash)


def test_no_partial_temp_files_after_failure():
    with _tmp_store() as d:
        root = Path(d) / "artifacts"
        store = ArtifactStore(root)
        real_fsync = os.fsync

        def boom(_fd: int) -> None:
            raise OSError("injected fsync failure")

        os.fsync = boom  # type: ignore[assignment]
        try:
            _raises(ArtifactStoreError, store.put_bytes, b"payload-for-failure")
        finally:
            os.fsync = real_fsync
        assert _tmp_files(root) == []
        assert store.exists(sha256_hex(b"payload-for-failure")) is False


def test_production_module_does_not_import_truth_stores():
    forbidden_modules = {
        "euler.core.factgraph",
        "euler.core.FactGraph",
        "euler.core.global_memory",
        "euler.gateway",
        "euler.gateway.server",
        "euler.verify",
        "euler.verify.service",
        "euler.execution.launch_receipt",
        "euler.execution.memory_contract_audit",
        "euler.execution.sol_guidance",
    }
    forbidden_names = {"FactGraph", "fact_submit", "GlobalMemory"}
    path = _ROOT / "artifacts.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported: set[str] = set()
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
                named.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                named.add(alias.name)
    assert imported.isdisjoint(forbidden_modules)
    assert named.isdisjoint(forbidden_names)
    assert "fact_graph/" not in source
    assert "global_memory/" not in source
    assert "FactGraph.add" not in source
    assert "fact_submit" not in source


def main() -> None:
    tests = [
        test_artifact_ref_validates_hash_size_and_media_type,
        test_bytes_and_text_roundtrip,
        test_reopen_and_retry_recovery,
        test_duplicate_put_is_idempotent_and_does_not_overwrite,
        test_concurrent_same_content_put,
        test_tamper_is_rejected_and_never_overwritten,
        test_malformed_hash_is_rejected,
        test_symlink_escape_is_rejected,
        test_no_partial_temp_files_after_failure,
        test_production_module_does_not_import_truth_stores,
    ]
    for test in tests:
        test()
        print(f"  [ok] {test.__name__}")
    print("ALL ARTIFACT TESTS PASSED")


if __name__ == "__main__":
    main()
