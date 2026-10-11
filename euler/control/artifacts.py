"""Content-addressed artifact store for research-control.

Stdlib only. Artifacts are raw bytes addressed by the full lowercase SHA-256
digest. Writes use a temp file, fsync, then an exclusive link so an existing
hash is never overwritten. Reads recompute digest and size. Every resolved
path must stay contained under the configured root.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from euler.control.ids import sha256_hex

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_BLOB_SUFFIX = ".blob"
_DIR_MODE = 0o755
_FILE_MODE = 0o644

__all__ = [
    "ArtifactStoreError",
    "ArtifactNotFound",
    "ArtifactIntegrityError",
    "ArtifactRef",
    "ArtifactStore",
]


class ArtifactStoreError(Exception):
    """Base error for artifact-store failures."""


class ArtifactNotFound(ArtifactStoreError):
    """No blob exists for the requested content hash."""


class ArtifactIntegrityError(ArtifactStoreError):
    """On-disk bytes do not match the content hash, or the blob is not a file."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactRef:
    """Address of persisted bytes. ``media_type`` is caller metadata, not stored."""

    content_hash: str
    size_bytes: int
    media_type: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "content_hash", _require_hash(self.content_hash)
        )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ArtifactStoreError("size_bytes must be a nonnegative int")
        if self.size_bytes < 0:
            raise ArtifactStoreError("size_bytes must be a nonnegative int")
        if self.media_type is not None:
            if not isinstance(self.media_type, str) or self.media_type == "":
                raise ArtifactStoreError("media_type must be a nonempty str")


class ArtifactStore:
    """CAS blob store rooted at ``root`` / ``sha256/<prefix>/<hash>.blob``."""

    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        try:
            path.mkdir(parents=True, exist_ok=True)
            resolved = path.resolve()
        except OSError as exc:
            raise ArtifactStoreError(f"cannot create artifact root: {exc}") from exc
        if not resolved.is_dir():
            raise ArtifactStoreError(f"artifact root is not a directory: {resolved}")
        self._root = resolved

    def put_bytes(
        self, data: bytes, *, media_type: str | None = None
    ) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise ArtifactStoreError(
                f"put_bytes requires bytes, got {type(data).__name__}"
            )
        content_hash = sha256_hex(data)
        ref = ArtifactRef(
            content_hash=content_hash,
            size_bytes=len(data),
            media_type=media_type,
        )
        dest = self._lexical_blob(content_hash)
        prefix_dir = self._prepare_prefix_dir(content_hash)
        if dest.exists() or dest.is_symlink():
            self._read_verified(dest, content_hash)
            return ref
        token = (
            f"{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(8)}"
        )
        temp = prefix_dir / f".{content_hash}.{token}.tmp"
        try:
            self._write_temp(temp, data)
            try:
                os.link(str(temp), str(dest))
            except FileExistsError:
                self._read_verified(dest, content_hash)
            else:
                if dest.is_symlink() or not dest.is_file():
                    raise ArtifactIntegrityError(
                        f"published artifact {content_hash} is not a regular file"
                    )
                self._contained(dest)
            self._fsync_directory(prefix_dir)
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to persist artifact {content_hash}: {exc}"
            ) from exc
        finally:
            _unlink_quietly(temp)
        return ref

    def put_text(
        self,
        text: str,
        *,
        encoding: str = "utf-8",
        media_type: str | None = None,
    ) -> ArtifactRef:
        if not isinstance(text, str):
            raise ArtifactStoreError(
                f"put_text requires str, got {type(text).__name__}"
            )
        encoding = _require_encoding(encoding)
        try:
            data = text.encode(encoding)
        except (LookupError, UnicodeEncodeError) as exc:
            raise ArtifactStoreError(f"cannot encode artifact text: {exc}") from exc
        return self.put_bytes(data, media_type=media_type)

    def get_bytes(self, content_hash: str) -> bytes:
        content_hash = _require_hash(content_hash)
        path = self._lexical_blob(content_hash)
        if not path.exists() and not path.is_symlink():
            self._assert_missing_path_contained(path)
            raise ArtifactNotFound(f"artifact {content_hash} not found")
        return self._read_verified(path, content_hash)

    def get_text(self, content_hash: str, *, encoding: str = "utf-8") -> str:
        encoding = _require_encoding(encoding)
        data = self.get_bytes(content_hash)
        try:
            return data.decode(encoding)
        except UnicodeDecodeError as exc:
            raise ArtifactStoreError(
                f"artifact {content_hash} is not valid {encoding}"
            ) from exc

    def exists(self, content_hash: str) -> bool:
        content_hash = _require_hash(content_hash)
        path = self._lexical_blob(content_hash)
        if not path.exists() and not path.is_symlink():
            self._assert_missing_path_contained(path)
            return False
        self._read_verified(path, content_hash)
        return True

    def verify(self, content_hash: str) -> ArtifactRef:
        content_hash = _require_hash(content_hash)
        data = self.get_bytes(content_hash)
        return ArtifactRef(content_hash=content_hash, size_bytes=len(data))

    def path_for(self, content_hash: str) -> Path:
        content_hash = _require_hash(content_hash)
        path = self._lexical_blob(content_hash)
        if path.exists() or path.is_symlink():
            self._reject_symlink_blob(path)
            return self._contained(path)
        self._assert_missing_path_contained(path)
        return path

    def _lexical_blob(self, content_hash: str) -> Path:
        return (
            self._root / "sha256" / content_hash[:2] / f"{content_hash}{_BLOB_SUFFIX}"
        )

    def _prepare_prefix_dir(self, content_hash: str) -> Path:
        sha_dir = self._root / "sha256"
        prefix_dir = sha_dir / content_hash[:2]
        try:
            sha_dir.mkdir(mode=_DIR_MODE, exist_ok=True)
            self._contained(sha_dir)
            if prefix_dir.is_symlink() or prefix_dir.exists():
                resolved = self._contained(prefix_dir)
                if not resolved.is_dir():
                    raise ArtifactStoreError(
                        f"artifact prefix path is not a directory: {prefix_dir}"
                    )
            prefix_dir.mkdir(mode=_DIR_MODE, exist_ok=True)
            self._contained(prefix_dir)
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError(
                f"cannot create artifact directory: {exc}"
            ) from exc
        return prefix_dir

    def _write_temp(self, temp: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(str(temp), flags, _FILE_MODE)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ArtifactStoreError("short write while persisting artifact")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_verified(self, path: Path, content_hash: str) -> bytes:
        self._reject_symlink_blob(path)
        if not path.exists():
            raise ArtifactNotFound(f"artifact {content_hash} not found")
        resolved = self._contained(path)
        if not resolved.is_file() or resolved.is_symlink():
            raise ArtifactIntegrityError(
                f"artifact {content_hash} is not a regular file"
            )
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError(
                f"cannot read artifact {content_hash}: {exc}"
            ) from exc
        digest = sha256_hex(data)
        if digest != content_hash:
            raise ArtifactIntegrityError(
                f"artifact {content_hash} failed hash verification"
            )
        return data

    def _reject_symlink_blob(self, path: Path) -> None:
        if path.is_symlink():
            raise ArtifactStoreError(
                f"artifact path is a symlink: {path}"
            )

    def _contained(self, path: Path) -> Path:
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise ArtifactStoreError(f"cannot resolve artifact path: {exc}") from exc
        root = self._root
        if resolved != root and root not in resolved.parents:
            raise ArtifactStoreError("artifact path escapes store root")
        return resolved

    def _assert_missing_path_contained(self, path: Path) -> None:
        parent = path.parent
        if parent.exists() or parent.is_symlink():
            self._contained(parent)
            return
        sha_dir = self._root / "sha256"
        if sha_dir.exists() or sha_dir.is_symlink():
            self._contained(sha_dir)
            return
        self._contained(self._root)

    def _fsync_directory(self, path: Path) -> None:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise ArtifactStoreError("content hash must be 64-char lowercase hex")
    return value


def _require_encoding(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise ArtifactStoreError("encoding must be a nonempty str")
    return value


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(str(path))
    except FileNotFoundError:
        return
    except OSError:
        return
