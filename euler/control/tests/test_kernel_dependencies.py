"""Pinned lake-cache materialization for isolated kernel checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from euler.control.contracts import (
    EvidenceKind,
    MathematicalScope,
    ProblemContract,
    QuantifierBinding,
    QuantifierKind,
    TargetKind,
)
from euler.control.ids import sha256_hex
from euler.control.kernel_dependencies import (
    KernelDependencyError,
    inspect_kernel_dependencies,
    prepare_kernel_dependencies,
    read_lake_packages,
)
from euler.control.kernel_evidence import (
    KernelCheckRequest,
    KernelCheckStatus,
    LeanLakeTransport,
    TRUSTED_PROVENANCE,
    run_kernel_check,
)
from euler.control.obligations import Obligation

_TOOLCHAIN = os.environ.get("EULER_TEST_LEAN_TOOLCHAIN", "leanprover/lean4:v4.34.1")
_ORIGIN = "https://example.com/foo.git"


def _mathlib_project() -> Path | None:
    raw = os.environ.get("EULER_TEST_MATHLIB_PROJECT", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if (path / "lake-manifest.json").is_file():
            return path.resolve()
        return None
    repo_root = Path(__file__).resolve().parents[3]
    sibling = repo_root.parent / "LeanPlayground"
    if (sibling / "lake-manifest.json").is_file():
        return sibling.resolve()
    return None


def _mathlib_cache_ready(root: Path) -> bool:
    pkg = root / ".lake" / "packages" / "mathlib"
    if not pkg.exists() and not pkg.is_symlink():
        return False
    try:
        resolved = pkg.resolve()
    except OSError:
        return False
    return (resolved / ".git").exists()


def _hex(*parts: str) -> str:
    return sha256_hex("|".join(parts).encode("utf-8"))


def _deadline(seconds: float = 30.0) -> float:
    return time.monotonic() + seconds


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", str(cwd), "--no-optional-locks", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {(completed.stderr or completed.stdout).strip()}"
        )
    return (completed.stdout or "").strip()


def _init_git(pkg: Path, origin: str) -> str:
    _git(pkg, "init", "-q")
    _git(pkg, "config", "user.email", "dep@test.local")
    _git(pkg, "config", "user.name", "dep-test")
    _git(pkg, "add", "-A")
    _git(pkg, "commit", "-qm", "pin")
    _git(pkg, "remote", "add", "origin", origin)
    return _git(pkg, "rev-parse", "HEAD")


def _write_manifest(root: Path, packages: list[dict], packages_dir: str = ".lake/packages") -> None:
    payload = {
        "version": "1.2.0",
        "packagesDir": packages_dir,
        "packages": packages,
        "name": "tinydep",
        "lakeDir": ".lake",
    }
    (root / "lake-manifest.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _git_entry(name: str, rev: str, url: str = _ORIGIN, **extra: object) -> dict:
    payload = {
        "url": url,
        "type": "git",
        "rev": rev,
        "name": name,
        "inputRev": extra.get("inputRev", "main"),
        "inherited": extra.get("inherited", False),
    }
    payload.update(extra)
    return payload


def _mini_project(root: Path, *, name: str = "foo", rev: str | None = None, url: str = _ORIGIN) -> tuple[Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "lean-toolchain").write_text(_TOOLCHAIN + "\n", encoding="utf-8")
    (root / "lakefile.toml").write_text(
        'name = "tinydep"\nversion = "0.1.0"\n[[lean_lib]]\nname = "TinyDep"\n',
        encoding="utf-8",
    )
    (root / "TinyDep.lean").write_text("def unused : Nat := 0\n", encoding="utf-8")
    cache = root / ".lake" / "packages" / name
    cache.mkdir(parents=True)
    (cache / "lakefile.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    (cache / "README.md").write_text("pkg\n", encoding="utf-8")
    pin = rev or _init_git(cache, url)
    _write_manifest(root, [_git_entry(name, pin, url)])
    return root, cache, pin


def _listing_hash(root: Path) -> str:
    completed = subprocess.run(
        [
            "find",
            str(root),
            "(",
            "-name",
            ".git",
            "-prune",
            ")",
            "-o",
            "-printf",
            "%P\t%s\t%T@\t%y\t%m\n",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    return sha256_hex(completed.stdout.encode("utf-8"))


def _lstat_payload(path: Path) -> dict[str, object]:
    st = path.lstat()
    payload: dict[str, object] = {
        "ino": st.st_ino,
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "mode": st.st_mode,
        "is_symlink": path.is_symlink(),
    }
    if path.is_symlink():
        payload["target"] = os.readlink(path)
    return payload


def _live_snapshot(playground: Path) -> dict[str, object]:
    snap: dict[str, object] = {
        "playground_listing": _listing_hash(playground),
    }
    for rel in ("lean-toolchain", "lakefile.toml", "lake-manifest.json", "AI4Math.lean"):
        path = playground / rel
        if path.is_file():
            snap[f"playground_file:{rel}"] = {
                "sha256": sha256_hex(path.read_bytes()),
                **_lstat_payload(path),
            }
    pkg_dir = playground / ".lake" / "packages"
    if pkg_dir.is_dir():
        for name in sorted(os.listdir(pkg_dir)):
            link = pkg_dir / name
            snap[f"playground_link:{name}"] = _lstat_payload(link)
            if not link.exists() and not link.is_symlink():
                continue
            resolved = link.resolve()
            snap[f"cache:{name}:HEAD"] = _git(resolved, "rev-parse", "HEAD")
            snap[f"cache:{name}:dirty"] = _git(resolved, "status", "--porcelain")
            snap[f"cache:{name}:origin"] = _git(resolved, "remote", "get-url", "origin")
            config = resolved / ".git" / "config"
            if config.is_file():
                snap[f"cache:{name}:config"] = sha256_hex(config.read_bytes())
    return snap


def _scope() -> MathematicalScope:
    return MathematicalScope.build(
        assumptions=(),
        quantifiers=(
            QuantifierBinding(variable="n", kind=QuantifierKind.FORALL, domain="N"),
        ),
        objects=(),
        parameter_domain="N",
        definitions_hash=_hex("defs"),
    )


def _contract() -> ProblemContract:
    return ProblemContract.build(
        problem_id="two-plus-two",
        version=1,
        exact_statement="two plus two equals four",
        scope=_scope(),
        permitted_tools=("lean",),
        external_help_policy="none",
        target_kind=TargetKind.FORMALIZATION,
        required_evidence_level=EvidenceKind.KERNEL_CHECKED_PROOF,
        completion_checks=("kernel checked",),
        budget_policy_ref="default-budget",
    )


def _obligation(contract: ProblemContract) -> Obligation:
    return Obligation.build(
        contract_id=contract.contract_id,
        exact_statement=contract.exact_statement,
        scope=contract.scope,
        desired_output="KERNEL_CHECKED_PROOF",
        created_by="researcher",
        version=1,
    )


def _request(contract, obligation, project_dir: Path, **overrides) -> KernelCheckRequest:
    kwargs = {
        "project_dir": str(project_dir),
        "module_name": "TinyMath",
        "theorem_name": "two_add_two",
        "expected_type": "2 + 2 = 4",
        "contract_id": contract.contract_id,
        "obligation_id": obligation.obligation_id,
        "exact_claim": obligation.exact_statement,
        "timeout_ms": 60_000,
    }
    lake = os.environ.get("EULER_TEST_LAKE") or shutil.which("lake")
    if lake:
        kwargs["lake_bin"] = lake
    kwargs.update(overrides)
    return KernelCheckRequest(**kwargs)


def test_core_only_project_is_a_noop(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "lean-toolchain").write_text(_TOOLCHAIN + "\n", encoding="utf-8")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    (isolated / "lean-toolchain").write_text(_TOOLCHAIN + "\n", encoding="utf-8")
    started = time.monotonic()
    prepared = prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    elapsed = time.monotonic() - started
    assert prepared.packages == ()
    assert prepared.method == "none"
    assert len(prepared.fingerprint) == 64
    assert not (isolated / ".lake" / "packages").exists()
    assert elapsed < 1.0


def test_missing_declared_package_fails_without_creating_cache(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    _write_manifest(root, [_git_entry("mathlib", "d13f23b723b8a846827a245b89c10fc7d3f11612")])
    isolated = tmp_path / "iso"
    isolated.mkdir()
    (isolated / "lake-manifest.json").write_bytes((root / "lake-manifest.json").read_bytes())
    with pytest.raises(KernelDependencyError, match="not in the local cache"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert not (isolated / ".lake" / "packages" / "mathlib").exists()


def test_unpinned_revision_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    _write_manifest(root, [_git_entry("foo", "")])
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    with pytest.raises(KernelDependencyError, match="no pinned revision"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())


def test_wrong_revision_is_rejected(tmp_path: Path) -> None:
    root, cache, pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    _write_manifest(isolated, [_git_entry("foo", "0" * 40)])
    with pytest.raises(KernelDependencyError, match="revision"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert _git(cache, "rev-parse", "HEAD") == pin
    assert _git(cache, "status", "--porcelain") == ""


def test_dirty_cache_is_rejected(tmp_path: Path) -> None:
    root, cache, pin = _mini_project(tmp_path / "src")
    (cache / "dirty.txt").write_text("local change\n", encoding="utf-8")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    with pytest.raises(KernelDependencyError, match="dirty"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert _git(cache, "rev-parse", "HEAD") == pin
    assert "dirty.txt" in _git(cache, "status", "--porcelain")


def test_origin_mismatch_is_rejected(tmp_path: Path) -> None:
    root, cache, pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    _write_manifest(
        isolated,
        [_git_entry("foo", pin, "https://evil.example/foo.git")],
    )
    with pytest.raises(KernelDependencyError, match="origin"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert _git(cache, "remote", "get-url", "origin") == _ORIGIN


def test_path_dep_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    _write_manifest(
        root,
        [{"name": "escape", "type": "path", "dir": "../outside"}],
    )
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    with pytest.raises(KernelDependencyError, match="escapes"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())


def test_escaping_symlink_in_package_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("classified\n", encoding="utf-8")
    cache = root / ".lake" / "packages" / "foo"
    cache.mkdir(parents=True)
    (cache / "ok.txt").write_text("ok\n", encoding="utf-8")
    (cache / "leak").symlink_to(outside)
    pin = _init_git(cache, _ORIGIN)
    _write_manifest(root, [_git_entry("foo", pin)])
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    with pytest.raises(KernelDependencyError, match="links outside"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())


def test_materialize_uses_independent_inodes_and_sanitized_git(tmp_path: Path) -> None:
    root, cache, pin = _mini_project(tmp_path / "src")
    (cache / ".git" / "credentials").write_text("token=secret\n", encoding="utf-8")
    source_config = (cache / ".git" / "config").read_bytes()
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    prepared = prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert prepared.method in {"cp-reflink", "cp-ordinary"}
    assert prepared.packages[0].rev == pin
    dest = isolated / ".lake" / "packages" / "foo"
    src_file = cache / "README.md"
    dest_file = dest / "README.md"
    assert dest_file.is_file()
    s_stat = src_file.stat()
    d_stat = dest_file.stat()
    assert (s_stat.st_ino, s_stat.st_dev) != (d_stat.st_ino, d_stat.st_dev)
    assert not dest_file.is_symlink()
    dest_git = dest / ".git"
    assert dest_git.is_dir()
    assert not dest_git.is_symlink()
    config_text = (dest_git / "config").read_text(encoding="utf-8")
    assert "token" not in config_text
    assert "secret" not in config_text
    assert _ORIGIN in config_text
    assert not (dest_git / "credentials").exists()
    dumped = json.dumps(prepared.to_dict())
    assert str(cache) not in dumped
    assert "secret" not in dumped
    assert (cache / ".git" / "config").read_bytes() == source_config
    assert _git(cache, "status", "--porcelain") == ""


def test_hardlink_copy_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root, cache, _pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    orig = subprocess.run

    def _hardlink_cp(cmd, **kwargs):
        if cmd and cmd[0] == "cp" and "-a" in cmd:
            src, dest = cmd[-2], cmd[-1]
            # Inject the forbidden inode sharing on GNU and BSD hosts alike.
            shutil.copytree(src, dest, copy_function=os.link, symlinks=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return orig(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _hardlink_cp)
    with pytest.raises(KernelDependencyError, match="hardlink"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())


def test_ordinary_copy_rejected_when_package_is_large(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import euler.control.kernel_dependencies as deps

    root, _cache, _pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    orig = subprocess.run

    def _fail_reflink(cmd, **kwargs):
        if cmd and cmd[0] == "cp" and "--reflink=always" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "reflink unsupported")
        return orig(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fail_reflink)
    monkeypatch.setattr(
        deps, "_approx_size", lambda _root, **_kwargs: deps._MAX_ORDINARY_COPY_BYTES + 1
    )
    with pytest.raises(KernelDependencyError, match="too large"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())


def test_lake_transport_missing_dependency_is_preparation_fail(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lean-toolchain").write_text(_TOOLCHAIN + "\n", encoding="utf-8")
    (root / "lakefile.toml").write_text(
        'name = "tinydep"\nversion = "0.1.0"\n[[lean_lib]]\nname = "TinyMath"\n',
        encoding="utf-8",
    )
    (root / "TinyMath.lean").write_text(
        "theorem two_add_two : 2 + 2 = 4 := rfl\n", encoding="utf-8"
    )
    _write_manifest(
        root,
        [_git_entry("mathlib", "d13f23b723b8a846827a245b89c10fc7d3f11612")],
    )
    contract = _contract()
    obligation = _obligation(contract)
    started = time.monotonic()
    artifact = run_kernel_check(
        _request(contract, obligation, root, timeout_ms=15_000, module_name="TinyMath"),
        transport=LeanLakeTransport(),
    )
    elapsed = time.monotonic() - started
    assert artifact.status is KernelCheckStatus.FAIL
    assert "dependency preparation" in artifact.detail
    assert "not in the local cache" in artifact.detail
    assert "cloning" not in (artifact.stderr or "").lower()
    assert elapsed < 5.0


@pytest.mark.skipif(
    _mathlib_project() is None or not _mathlib_cache_ready(_mathlib_project()),
    reason="EULER_TEST_MATHLIB_PROJECT / sibling LeanPlayground cache is not present",
)
def test_mathlib_tiny_theorem_reuses_playground_cache_without_live_writes() -> None:
    playground = _mathlib_project()
    assert playground is not None
    before = _live_snapshot(playground)
    contract = _contract()
    obligation = _obligation(contract)
    with tempfile.TemporaryDirectory(prefix="euler-r5-mathlib-", dir="/tmp") as raw:
        proj = Path(raw) / "proj"
        proj.mkdir()
        for rel in ("lean-toolchain", "lake-manifest.json"):
            shutil.copyfile(playground / rel, proj / rel)
        (proj / "lakefile.toml").write_text(
            (
                'name = "tinymathlibcheck"\n'
                'version = "0.1.0"\n'
                'defaultTargets = ["TinyMath"]\n\n'
                "[[require]]\n"
                'name = "mathlib"\n'
                'git = "https://github.com/leanprover-community/mathlib4.git"\n'
                'rev = "v4.34.1"\n\n'
                "[[lean_lib]]\n"
                'name = "TinyMath"\n'
            ),
            encoding="utf-8",
        )
        (proj / "TinyMath.lean").write_text(
            "import Mathlib.Tactic.NormNum\n\n"
            "theorem two_add_two : 2 + 2 = 4 := by\n"
            "  norm_num\n",
            encoding="utf-8",
        )
        pkg_dir = proj / ".lake" / "packages"
        pkg_dir.mkdir(parents=True)
        for name in (playground / ".lake" / "packages").iterdir():
            (pkg_dir / name.name).symlink_to(name.resolve())

        started = time.monotonic()
        artifact = run_kernel_check(
            _request(
                contract,
                obligation,
                proj,
                module_name="TinyMath",
                timeout_ms=180_000,
            ),
            transport=LeanLakeTransport(),
        )
        elapsed = time.monotonic() - started
        blob = f"{artifact.stdout}\n{artifact.stderr}\n{artifact.detail}"
        timing = {
            "total_s": round(elapsed, 3),
            "status": artifact.status.value,
            "detail": artifact.detail,
            "cloned": "cloning" in blob.lower(),
            "url_changed": "URL has changed" in blob,
            "fingerprint": artifact.source_manifest.dependency_fingerprint,
            "build_status": artifact.build_status,
            "check_status": artifact.check_status,
        }
        print("MATHLIB_KERNEL_TIMING", json.dumps(timing), flush=True)
        assert "cloning" not in blob.lower()
        assert "URL has changed" not in blob
        assert artifact.status is KernelCheckStatus.PASS, artifact.detail
        assert artifact.provenance_kind == TRUSTED_PROVENANCE
        assert artifact.theorem_name == "two_add_two"
        assert artifact.theorem_type == "2 + 2 = 4"
        assert artifact.build_status == "PASS"
        assert artifact.check_status == "PASS"
        assert artifact.source_manifest.dependency_fingerprint
        assert len(artifact.source_manifest.dependency_fingerprint) == 64
        assert "[deps]" in artifact.stdout
        assert "mathlib@" in artifact.stdout
        assert elapsed < 185.0

    after = _live_snapshot(playground)
    assert after == before


def test_read_lake_packages_missing_manifest(tmp_path: Path) -> None:
    packages_dir, packages = read_lake_packages(tmp_path / "lake-manifest.json")
    assert packages_dir == ".lake/packages"
    assert packages == ()


@pytest.mark.parametrize("filename,source", [
    ("lakefile.toml", 'name = "tiny"\n[[require]]\nname = "foo"\ngit = "https://example.com/foo.git"\n'),
    ("lakefile.lean", 'import Lake\nopen Lake DSL\npackage tiny\nrequire foo from git "https://example.com/foo.git"\n'),
])
def test_declared_dependency_without_pin_manifest_fails_before_lake(tmp_path, filename, source):
    project = tmp_path / "source"
    isolated = tmp_path / "isolated"
    project.mkdir()
    isolated.mkdir()
    (project / filename).write_text(source)
    with pytest.raises(KernelDependencyError, match="pinned lake-manifest"):
        prepare_kernel_dependencies(project, isolated, deadline=_deadline())
    assert not (isolated / ".lake").exists()


def test_fingerprint_matches_inspect_and_ignores_copy_method(tmp_path: Path) -> None:
    root, _cache, pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    inspected = inspect_kernel_dependencies(root, deadline=_deadline())
    prepared = prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert inspected.fingerprint == prepared.fingerprint
    assert prepared.packages[0].rev == pin
    assert prepared.packages[0].copy_method in {"cp-reflink", "cp-ordinary"}
    assert inspected.packages[0].copy_method == "inspect"
    identities = [item.identity_dict() for item in prepared.packages]
    assert "copy_method" not in identities[0]


def test_absolute_packages_dir_is_rejected(tmp_path: Path) -> None:
    root, _cache, pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    evil = tmp_path / "evil-abs"
    _write_manifest(isolated, [_git_entry("foo", pin)], packages_dir=str(evil))
    with pytest.raises(KernelDependencyError, match="project-relative"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert not evil.exists()


def test_parent_packages_dir_is_rejected(tmp_path: Path) -> None:
    root, _cache, pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    _write_manifest(isolated, [_git_entry("foo", pin)], packages_dir="../escaped")
    sentinel = tmp_path / "escaped" / "foo"
    sentinel.mkdir(parents=True)
    marker = tmp_path / "escaped" / "KEEP.txt"
    marker.write_text("alive\n", encoding="utf-8")
    with pytest.raises(KernelDependencyError, match="parent traversal"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert marker.read_text(encoding="utf-8") == "alive\n"


def test_packages_dir_ancestor_symlink_does_not_delete_external(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "KEEP.txt"
    marker.write_text("alive\n", encoding="utf-8")
    root, _cache, pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    (isolated / ".lake").symlink_to(outside)
    with pytest.raises(KernelDependencyError, match="symlink"):
        prepare_kernel_dependencies(root, isolated, deadline=_deadline())
    assert marker.read_text(encoding="utf-8") == "alive\n"
    assert set(outside.iterdir()) == {marker}


def test_overlapping_source_and_isolated_roots_rejected(tmp_path: Path) -> None:
    root, _cache, _pin = _mini_project(tmp_path / "src")
    nested = root / "nested-iso"
    nested.mkdir()
    shutil.copyfile(root / "lake-manifest.json", nested / "lake-manifest.json")
    with pytest.raises(KernelDependencyError, match="overlap"):
        prepare_kernel_dependencies(root, nested, deadline=_deadline())


def test_expired_deadline_does_not_extend_git_budget(tmp_path: Path) -> None:
    root, _cache, _pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    shutil.copyfile(root / "lake-manifest.json", isolated / "lake-manifest.json")
    started = time.monotonic()
    with pytest.raises(KernelDependencyError, match="timed out"):
        prepare_kernel_dependencies(root, isolated, deadline=time.monotonic() - 1)
    assert time.monotonic() - started < 1.0


def test_deadline_type_is_validated(tmp_path: Path) -> None:
    root, _cache, _pin = _mini_project(tmp_path / "src")
    isolated = tmp_path / "iso"
    isolated.mkdir()
    with pytest.raises(KernelDependencyError, match="deadline"):
        prepare_kernel_dependencies(root, isolated, deadline=True)  # type: ignore[arg-type]
