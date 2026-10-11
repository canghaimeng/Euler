"""Prepare pinned lake packages into an isolated kernel workspace.

Reads lake-manifest.json and existing ``.lake/packages`` caches. Materializes
each package only after a ``cp --reflink=always`` probe succeeds; then
``cp -a --reflink=auto`` clones large extents. If the probe fails, only trees
at most 8 GiB may use an ordinary copy under the same request deadline.
Never writes, chmods, or hardlinks
the original cache.

Isolated copies keep a sanitized ``.git`` so Lake can see HEAD == pin and skip
fetch/reclone, which is required to reuse ``.lake/build`` oleans. Nested
``.lake/packages`` symlinks (live shared trees) are stripped. Credential files
and extra git config are removed from the destination only. Published evidence
records pin identity (name/rev/url/input_rev/inherited), not copy method,
``.git`` contents, or local paths.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from euler.control.ids import canonical_json, sha256_hex

_GIT_DIR = ".git"
_NESTED_PACKAGES = (".lake", "packages")
# A pinned Mathlib cache is several GiB. Permit independent copies on ext4,
# while retaining both this per-package size cap and the request deadline.
_MAX_ORDINARY_COPY_BYTES = 8 * 1024 * 1024 * 1024
_SKIP_COPY_NAMES = frozenset({".git-credentials", ".netrc"})
_GIT_CREDENTIAL_FILES = frozenset(
    {
        "credentials",
        ".git-credentials",
        "credentials.store",
        "credential",
    }
)
_SAFE_PART_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_DEFAULT_PACKAGES_DIR = ".lake/packages"

__all__ = [
    "KernelDependencyError",
    "KernelPackagePin",
    "PreparedDependencies",
    "inspect_kernel_dependencies",
    "prepare_kernel_dependencies",
    "read_lake_packages",
]


class KernelDependencyError(ValueError):
    """Declared lake dependencies cannot be prepared from the local cache."""


@dataclass(frozen=True, slots=True, kw_only=True)
class KernelPackagePin:
    name: str
    rev: str
    url: str
    input_rev: str
    inherited: bool
    copy_method: str

    def identity_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rev": self.rev,
            "url": self.url,
            "input_rev": self.input_rev,
            "inherited": self.inherited,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.identity_dict()
        payload["copy_method"] = self.copy_method
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedDependencies:
    packages: tuple[KernelPackagePin, ...]
    fingerprint: str
    method: str
    packages_dir: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [item.to_dict() for item in self.packages],
            "fingerprint": self.fingerprint,
            "method": self.method,
            "packages_dir": self.packages_dir,
        }


def read_lake_packages(manifest_path: Path) -> tuple[str, tuple[dict[str, Any], ...]]:
    if not isinstance(manifest_path, Path):
        raise KernelDependencyError("dependency preparation: manifest path is invalid")
    if not manifest_path.is_file():
        return _DEFAULT_PACKAGES_DIR, ()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KernelDependencyError(
            f"dependency preparation: cannot read lake-manifest.json: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise KernelDependencyError("dependency preparation: lake-manifest.json is not an object")
    raw_dir = payload.get("packagesDir", _DEFAULT_PACKAGES_DIR)
    if raw_dir is None:
        packages_dir = _DEFAULT_PACKAGES_DIR
    elif not isinstance(raw_dir, str):
        raise KernelDependencyError("dependency preparation: packagesDir must be a string")
    else:
        packages_dir = raw_dir.strip() or _DEFAULT_PACKAGES_DIR
    _validate_packages_dir_text(packages_dir)
    raw = payload.get("packages") or []
    if not isinstance(raw, list):
        raise KernelDependencyError("dependency preparation: lake-manifest packages must be a list")
    packages: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            packages.append(dict(item))
    return packages_dir, tuple(packages)


def inspect_kernel_dependencies(
    source_root: Path | str,
    *,
    deadline: float,
) -> PreparedDependencies:
    """Verify declared pins on the live project without copying or writing."""
    source = _require_workspace(source_root, "source_root")
    _remaining(deadline, "dependency inspection")
    packages_dir, declared = _manifest_packages(source, source)
    if not declared:
        return PreparedDependencies(
            packages=(),
            fingerprint=_fingerprint(()),
            method="none",
            packages_dir=packages_dir,
        )
    _require_packages_dir_location(source, packages_dir, create=False)
    pins = _collect_git_pins(source, packages_dir, declared, deadline=deadline, copy_method="inspect")
    return PreparedDependencies(
        packages=tuple(pins),
        fingerprint=_fingerprint(tuple(pin.identity_dict() for pin in pins)),
        method="inspect",
        packages_dir=packages_dir,
    )


def prepare_kernel_dependencies(
    source_root: Path | str,
    isolated_root: Path | str,
    *,
    deadline: float,
) -> PreparedDependencies:
    """Materialize declared packages into ``isolated_root`` before lake runs."""
    source = _require_workspace(source_root, "source_root")
    isolated = _require_workspace(isolated_root, "isolated_root")
    _reject_overlapping_roots(source, isolated)
    _remaining(deadline, "dependency preparation")
    packages_dir, declared = _manifest_packages(isolated, source)
    if not declared:
        return PreparedDependencies(
            packages=(),
            fingerprint=_fingerprint(()),
            method="none",
            packages_dir=packages_dir,
        )
    _require_packages_dir_location(source, packages_dir, create=False)
    dest_root = _require_packages_dir_location(isolated, packages_dir, create=True)
    pins: list[KernelPackagePin] = []
    methods: list[str] = []
    for identity, cache in _iter_verified_caches(source, packages_dir, declared, deadline=deadline):
        dest = dest_root / identity["name"]
        _reject_unsafe_dest(dest, isolated)
        method = _materialize_package(
            cache, dest, isolated_root=isolated, deadline=deadline, origin_url=identity["url"]
        )
        methods.append(method)
        pins.append(
            KernelPackagePin(
                name=identity["name"],
                rev=identity["rev"],
                url=identity["url"],
                input_rev=identity["input_rev"],
                inherited=identity["inherited"],
                copy_method=method,
            )
        )
    fingerprint = _fingerprint(tuple(pin.identity_dict() for pin in pins))
    method = "cp-reflink" if methods and all(item == "cp-reflink" for item in methods) else (
        methods[0] if len(set(methods)) == 1 else "mixed"
    )
    return PreparedDependencies(
        packages=tuple(pins),
        fingerprint=fingerprint,
        method=method or "none",
        packages_dir=packages_dir,
    )


def _manifest_packages(primary: Path, fallback: Path) -> tuple[str, tuple[dict[str, Any], ...]]:
    manifest_path = primary / "lake-manifest.json"
    if not manifest_path.is_file():
        manifest_path = fallback / "lake-manifest.json"
    if not manifest_path.is_file():
        toml_path = fallback / "lakefile.toml"
        lean_path = fallback / "lakefile.lean"
        requires = False
        if toml_path.is_file():
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib
            try:
                requires = bool(tomllib.loads(toml_path.read_text(encoding="utf-8")).get("require"))
            except (ValueError, OSError) as exc:
                raise KernelDependencyError("dependency preparation: unreadable lakefile.toml") from exc
        if lean_path.is_file():
            requires = requires or bool(re.search(
                r"(?m)^\s*require\b", lean_path.read_text(encoding="utf-8")
            ))
        if requires:
            raise KernelDependencyError(
                "dependency preparation: declared packages require a pinned lake-manifest.json"
            )
    return read_lake_packages(manifest_path)


def _collect_git_pins(
    source_root: Path,
    packages_dir: str,
    declared: Sequence[Mapping[str, Any]],
    *,
    deadline: float,
    copy_method: str,
) -> list[KernelPackagePin]:
    pins: list[KernelPackagePin] = []
    for identity, _cache in _iter_verified_caches(
        source_root, packages_dir, declared, deadline=deadline
    ):
        pins.append(
            KernelPackagePin(
                name=identity["name"],
                rev=identity["rev"],
                url=identity["url"],
                input_rev=identity["input_rev"],
                inherited=identity["inherited"],
                copy_method=copy_method,
            )
        )
    return pins


def _iter_verified_caches(
    source_root: Path,
    packages_dir: str,
    declared: Sequence[Mapping[str, Any]],
    *,
    deadline: float,
) -> list[tuple[dict[str, Any], Path]]:
    found: list[tuple[dict[str, Any], Path]] = []
    for entry in declared:
        _remaining(deadline, "dependency verification")
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        _validate_package_name(name)
        ptype = str(entry.get("type") or "git").strip()
        if ptype == "path":
            _reject_external_path_dep(source_root, entry)
            continue
        if ptype != "git":
            raise KernelDependencyError(
                f"dependency preparation: unsupported package type {ptype!r} for {name}"
            )
        rev = str(entry.get("rev") or "").strip()
        if not rev:
            raise KernelDependencyError(
                f"dependency preparation: package {name} has no pinned revision"
            )
        url = str(entry.get("url") or "").strip()
        cache = _locate_package_cache(source_root, packages_dir, name)
        _verify_git_pin(cache, name=name, expected_rev=rev, expected_url=url, deadline=deadline)
        found.append(
            (
                {
                    "name": name,
                    "rev": rev,
                    "url": url,
                    "input_rev": str(entry.get("inputRev") or ""),
                    "inherited": bool(entry.get("inherited")),
                },
                cache,
            )
        )
    return found


def _validate_packages_dir_text(packages_dir: str) -> None:
    if not isinstance(packages_dir, str) or not packages_dir.strip():
        raise KernelDependencyError("dependency preparation: packagesDir is empty")
    text = packages_dir.strip()
    if text.startswith("/") or text.startswith("\\") or (len(text) >= 2 and text[1] == ":"):
        raise KernelDependencyError("dependency preparation: packagesDir must be project-relative")
    path = Path(text)
    if path.is_absolute():
        raise KernelDependencyError("dependency preparation: packagesDir must be project-relative")
    parts = path.parts
    if not parts:
        raise KernelDependencyError("dependency preparation: packagesDir is empty")
    for part in parts:
        if part in {"", ".", ".."}:
            raise KernelDependencyError(
                "dependency preparation: packagesDir must not contain parent traversal"
            )
        if any(ch not in _SAFE_PART_CHARS for ch in part):
            raise KernelDependencyError("dependency preparation: packagesDir has invalid path parts")


def _validate_package_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or ".." in name:
        raise KernelDependencyError("dependency preparation: invalid package name")
    if any(ch not in _SAFE_PART_CHARS for ch in name):
        raise KernelDependencyError("dependency preparation: invalid package name")


def _require_workspace(value: Path | str, label: str) -> Path:
    if isinstance(value, bool) or value is None:
        raise KernelDependencyError(f"dependency preparation: {label} is invalid")
    if not isinstance(value, (str, Path)):
        raise KernelDependencyError(f"dependency preparation: {label} is invalid")
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise KernelDependencyError(f"dependency preparation: {label} is a symlink")
    try:
        resolved = raw.resolve(strict=False)
    except OSError as exc:
        raise KernelDependencyError(
            f"dependency preparation: cannot resolve {label}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise KernelDependencyError(f"dependency preparation: {label} is not a directory")
    return resolved


def _reject_overlapping_roots(source: Path, isolated: Path) -> None:
    if source == isolated or isolated.is_relative_to(source) or source.is_relative_to(isolated):
        raise KernelDependencyError(
            "dependency preparation: source and isolated roots overlap"
        )


def _require_packages_dir_location(root: Path, packages_dir: str, *, create: bool) -> Path:
    _validate_packages_dir_text(packages_dir)
    cursor = root
    for part in Path(packages_dir).parts:
        nxt = cursor / part
        if nxt.is_symlink():
            raise KernelDependencyError(
                "dependency preparation: packagesDir destination is a symlink"
            )
        if nxt.exists() and not nxt.is_dir():
            raise KernelDependencyError(
                "dependency preparation: packagesDir is not a directory"
            )
        if not nxt.exists():
            if not create:
                return nxt
            nxt.mkdir(mode=0o755)
        cursor = nxt
        try:
            cursor.resolve().relative_to(root)
        except ValueError as exc:
            raise KernelDependencyError(
                "dependency preparation: packagesDir escapes the project"
            ) from exc
    try:
        cursor.resolve().relative_to(root)
    except ValueError as exc:
        raise KernelDependencyError(
            "dependency preparation: packagesDir escapes the project"
        ) from exc
    return cursor


def _reject_unsafe_dest(dest: Path, isolated_root: Path) -> None:
    if dest.is_symlink():
        raise KernelDependencyError(
            "dependency preparation: package destination is a symlink"
        )
    cursor = dest
    while True:
        if cursor.is_symlink():
            raise KernelDependencyError(
                "dependency preparation: packagesDir destination is a symlink"
            )
        if cursor == isolated_root:
            break
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    try:
        dest.resolve().relative_to(isolated_root)
    except ValueError as exc:
        raise KernelDependencyError(
            "dependency preparation: package destination escapes the isolated workspace"
        ) from exc


def _locate_package_cache(source_root: Path, packages_dir: str, name: str) -> Path:
    link = source_root.joinpath(*Path(packages_dir).parts) / name
    if not link.exists() and not link.is_symlink():
        raise KernelDependencyError(
            f"dependency preparation: package {name} is not in the local cache at {packages_dir}"
        )
    if link.is_symlink() or link.is_dir():
        try:
            resolved = link.resolve()
        except OSError as exc:
            raise KernelDependencyError(
                f"dependency preparation: cannot resolve package {name}: {exc}"
            ) from exc
        if not resolved.is_dir():
            raise KernelDependencyError(
                f"dependency preparation: package {name} cache is not a directory"
            )
        return resolved
    raise KernelDependencyError(
        f"dependency preparation: package {name} cache is not a directory"
    )


def _verify_git_pin(
    cache: Path,
    *,
    name: str,
    expected_rev: str,
    expected_url: str,
    deadline: float,
) -> None:
    git_dir = cache / _GIT_DIR
    if git_dir.is_symlink():
        raise KernelDependencyError(
            f"dependency preparation: package {name} .git is a symlink"
        )
    if not git_dir.exists():
        raise KernelDependencyError(
            f"dependency preparation: package {name} cache has no git metadata to verify the pin"
        )
    head = _git(cache, "rev-parse", "HEAD", deadline=deadline)
    if head != expected_rev:
        raise KernelDependencyError(
            f"dependency preparation: package {name} is revision {head}, expected {expected_rev}"
        )
    dirty = _git(cache, "status", "--porcelain", deadline=deadline)
    if dirty.strip():
        raise KernelDependencyError(
            f"dependency preparation: package {name} cache is dirty"
        )
    if expected_url:
        remote = _git(cache, "remote", "get-url", "origin", deadline=deadline)
        if remote and _normalize_git_url(remote) != _normalize_git_url(expected_url):
            raise KernelDependencyError(
                f"dependency preparation: package {name} origin {remote} does not match {expected_url}"
            )


def _reject_external_path_dep(source_root: Path, entry: Mapping[str, Any]) -> None:
    rel = str(entry.get("dir") or entry.get("path") or "").strip()
    name = str(entry.get("name") or "path-dep")
    if not rel:
        return
    try:
        _validate_packages_dir_text(rel)
    except KernelDependencyError as exc:
        raise KernelDependencyError(
            f"dependency preparation: path package {name} escapes the project"
        ) from exc
    _require_packages_dir_location(source_root, rel, create=False)


def _materialize_package(
    src: Path,
    dest: Path,
    *,
    isolated_root: Path,
    deadline: float,
    origin_url: str,
) -> str:
    _remaining(deadline, "dependency copy")
    if dest.is_symlink():
        raise KernelDependencyError(
            "dependency preparation: package destination is a symlink"
        )
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    method = _copy_tree(src, dest, deadline=deadline)
    _reject_metadata_symlinks(dest)
    _strip_unpublished(dest, origin_url=origin_url)
    _reject_live_write_through(src, dest, deadline=deadline)
    try:
        dest.resolve().relative_to(isolated_root)
    except ValueError as exc:
        raise KernelDependencyError(
            "dependency preparation: package destination escapes the isolated workspace"
        ) from exc
    return method


def _copy_tree(src: Path, dest: Path, *, deadline: float) -> str:
    _remove_path(dest)
    can_reflink, probe_detail = _probe_reflink(src, dest.parent, deadline=deadline)
    if can_reflink:
        # --reflink=always fails on ext4 inline/small files even when large
        # extents clone. A successful large-file probe plus --reflink=auto
        # clones oleans; it is not a silent full copy on a non-COW filesystem.
        remaining = _remaining(deadline, "dependency copy")
        try:
            completed = subprocess.run(
                ["cp", "-a", "--reflink=auto", str(src), str(dest)],
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _remove_path(dest)
            raise KernelDependencyError(
                "dependency preparation: timed out while copying a package cache"
            ) from exc
        except FileNotFoundError as exc:
            raise KernelDependencyError(
                "dependency preparation: cp is not available to materialize packages"
            ) from exc
        if completed.returncode == 0:
            return "cp-reflink"
        _remove_path(dest)
        probe_detail = completed.stderr.strip() or probe_detail
    size = _approx_size(src, deadline=deadline)
    if size > _MAX_ORDINARY_COPY_BYTES:
        raise KernelDependencyError(
            "dependency preparation: reflink copy failed and the package is too large "
            f"for an ordinary copy ({size} bytes): {probe_detail}"
        )
    remaining = _remaining(deadline, "ordinary package copy")
    try:
        fallback = subprocess.run(
            ["cp", "-a", "--reflink=never", str(src), str(dest)],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _remove_path(dest)
        raise KernelDependencyError(
            "dependency preparation: timed out during ordinary package copy"
        ) from exc
    except FileNotFoundError as exc:
        raise KernelDependencyError(
            "dependency preparation: cp is not available to materialize packages"
        ) from exc
    if fallback.returncode != 0:
        _remove_path(dest)
        raise KernelDependencyError(
            f"dependency preparation: ordinary copy failed: {fallback.stderr.strip()}"
        )
    return "cp-ordinary"


def _probe_reflink(src: Path, dest_parent: Path, *, deadline: float) -> tuple[bool, str]:
    remaining = _remaining(deadline, "reflink probe")
    sample = _large_regular_file(src, deadline=deadline)
    if sample is None:
        return False, "no file large enough to probe reflink"
    dest_parent.mkdir(parents=True, exist_ok=True)
    probe = dest_parent / ".euler-reflink-probe"
    _remove_path(probe)
    try:
        completed = subprocess.run(
            ["cp", "--reflink=always", str(sample), str(probe)],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _remove_path(probe)
        raise KernelDependencyError(
            "dependency preparation: timed out while probing reflink"
        ) from exc
    except FileNotFoundError:
        _remove_path(probe)
        return False, "cp missing"
    ok = completed.returncode == 0 and probe.is_file()
    detail = (completed.stderr or "").strip() or "reflink probe failed"
    _remove_path(probe)
    return ok, detail


def _large_regular_file(root: Path, *, deadline: float) -> Path | None:
    remaining = _remaining(deadline, "reflink probe")
    try:
        completed = subprocess.run(
            [
                "find",
                "-P",
                str(root),
                "(",
                "-path",
                "*/.git",
                "-o",
                "-path",
                "*/.git/*",
                ")",
                "-prune",
                "-o",
                "-type",
                "f",
                "-size",
                "+128k",
                "-print",
                "-quit",
            ],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    line = (completed.stdout or "").splitlines()
    if not line:
        return None
    path = Path(line[0])
    if path.is_file() and not path.is_symlink():
        return path
    return None


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=False)


def _reject_metadata_symlinks(dest: Path) -> None:
    lake = dest / ".lake"
    git_dir = dest / _GIT_DIR
    if lake.is_symlink():
        raise KernelDependencyError(
            "dependency preparation: isolated package .lake is a symlink"
        )
    if git_dir.is_symlink():
        raise KernelDependencyError(
            "dependency preparation: isolated .git is a symlink"
        )
    nested = dest.joinpath(*_NESTED_PACKAGES)
    if nested.is_symlink():
        nested.unlink()


def _strip_unpublished(dest: Path, *, origin_url: str) -> None:
    nested = dest.joinpath(*_NESTED_PACKAGES)
    if nested.is_symlink() or nested.is_file():
        nested.unlink()
    elif nested.is_dir():
        shutil.rmtree(nested)
    for skip in _SKIP_COPY_NAMES:
        extra = dest / skip
        if extra.is_symlink() or extra.is_file():
            extra.unlink(missing_ok=True)
        elif extra.is_dir():
            shutil.rmtree(extra)
    _sanitize_dest_git(dest / _GIT_DIR, origin_url=origin_url)


def _sanitize_dest_git(git_dir: Path, *, origin_url: str) -> None:
    """Rewrite destination git metadata only. Never touches the live cache."""
    if git_dir.is_symlink():
        raise KernelDependencyError(
            "dependency preparation: isolated .git is a symlink"
        )
    if not git_dir.exists():
        return
    if git_dir.is_file():
        git_dir.unlink()
        return
    for name in _GIT_CREDENTIAL_FILES:
        extra = git_dir / name
        if extra.is_symlink() or extra.is_file():
            extra.unlink(missing_ok=True)
        elif extra.is_dir():
            shutil.rmtree(extra)
    hooks = git_dir / "hooks"
    if hooks.is_symlink() or hooks.is_file():
        hooks.unlink(missing_ok=True)
    elif hooks.is_dir():
        shutil.rmtree(hooks)
    config = git_dir / "config"
    if config.is_dir() and not config.is_symlink():
        shutil.rmtree(config)
    elif config.exists() or config.is_symlink():
        config.unlink()
    lines = [
        "[core]\n",
        "\trepositoryformatversion = 0\n",
        "\tfilemode = true\n",
        "\tbare = false\n",
    ]
    if origin_url:
        lines.extend(
            [
                '[remote "origin"]\n',
                f"\turl = {origin_url}\n",
            ]
        )
    config.write_text("".join(lines), encoding="utf-8")


def _reject_live_write_through(src: Path, dest: Path, *, deadline: float) -> None:
    dest_res = dest.resolve()
    src_res = src.resolve()
    sample = _first_regular_file(dest_res, deadline=deadline)
    origin = _corresponding_source_file(src_res, dest_res, sample) if sample else None
    if sample is not None and origin is not None and origin.exists():
        s_stat = origin.stat()
        d_stat = sample.stat()
        if s_stat.st_ino == d_stat.st_ino and s_stat.st_dev == d_stat.st_dev:
            raise KernelDependencyError(
                "dependency preparation: refused a hardlink onto the live package cache"
            )
    for path in _package_symlinks(dest_res, deadline=deadline):
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise KernelDependencyError(
                f"dependency preparation: dangling symlink in isolated package: {path}"
            ) from exc
        try:
            resolved.relative_to(dest_res)
        except ValueError as exc:
            raise KernelDependencyError(
                f"dependency preparation: isolated package still links outside itself: {path}"
            ) from exc


def _package_symlinks(root: Path, *, deadline: float) -> list[Path]:
    remaining = _remaining(deadline, "symlink scan")
    try:
        completed = subprocess.run(
            ["find", "-P", str(root), "-type", "l"],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KernelDependencyError(
            "dependency preparation: timed out during symlink scan"
        ) from exc
    except OSError as exc:
        raise KernelDependencyError(
            f"dependency preparation: cannot scan isolated package symlinks: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise KernelDependencyError(
            "dependency preparation: cannot scan isolated package symlinks: "
            + (completed.stderr or completed.stdout or "").strip()
        )
    return [Path(line) for line in completed.stdout.splitlines() if line]


def _first_regular_file(root: Path, *, deadline: float) -> Path | None:
    remaining = _remaining(deadline, "inode check")
    try:
        completed = subprocess.run(
            [
                "find",
                "-P",
                str(root),
                "(",
                "-path",
                "*/.git",
                "-o",
                "-path",
                "*/.git/*",
                ")",
                "-prune",
                "-o",
                "-type",
                "f",
                "-print",
                "-quit",
            ],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    line = (completed.stdout or "").splitlines()
    if not line:
        return None
    path = Path(line[0])
    if path.is_file() and not path.is_symlink():
        return path
    return None


def _corresponding_source_file(src: Path, dest: Path, sample: Path) -> Path | None:
    try:
        rel = sample.relative_to(dest)
    except ValueError:
        return None
    return src / rel


def _approx_size(root: Path, *, deadline: float) -> int:
    remaining = _remaining(deadline, "size check")
    try:
        completed = subprocess.run(
            ["du", "-sb", str(root)],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise KernelDependencyError(
            "dependency preparation: timed out while measuring a package cache"
        ) from exc
    except OSError as exc:
        raise KernelDependencyError(
            f"dependency preparation: cannot measure package size: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise KernelDependencyError(
            "dependency preparation: cannot measure package size: "
            + (completed.stderr or completed.stdout or "").strip()
        )
    token = (completed.stdout or "").split()
    if not token:
        raise KernelDependencyError("dependency preparation: cannot measure package size")
    try:
        return int(token[0])
    except ValueError as exc:
        raise KernelDependencyError(
            "dependency preparation: cannot measure package size"
        ) from exc


def _git(cwd: Path, *args: str, deadline: float) -> str:
    remaining = _remaining(deadline, "git probe")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "--no-optional-locks", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=remaining,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise KernelDependencyError(
            "dependency preparation: timed out during git probe"
        ) from exc
    except OSError as exc:
        raise KernelDependencyError(
            f"dependency preparation: git probe failed in {cwd}: {exc}"
        ) from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise KernelDependencyError(
            f"dependency preparation: git {' '.join(args)} failed: {err}"
        )
    return (completed.stdout or "").strip()


def _normalize_git_url(url: str) -> str:
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text.lower()


def _fingerprint(pins: Sequence[Mapping[str, Any]]) -> str:
    return sha256_hex(canonical_json(list(pins)).encode("utf-8"))


def _remaining(deadline: float, stage: str) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise KernelDependencyError("dependency preparation: deadline must be a monotonic timestamp")
    left = float(deadline) - time.monotonic()
    if left <= 0:
        raise KernelDependencyError(f"dependency preparation: timed out during {stage}")
    return left
