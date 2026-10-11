"""Product-namespace scan: current Euler surfaces must not keep Danus identity.

Runs standalone (``python -m euler.tests.test_package_namespace``) and under pytest.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "euler"
_THIS_FILE = Path(__file__).resolve()

_SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "runtime",
    "node_modules",
    "dist",
    "build",
}

_HISTORICAL_DOC_PARTS: set[str] = set()

_SKIP_FILES = {
    _THIS_FILE,
    _REPO_ROOT / "LICENSE",
}

_FORBIDDEN_TOKENS = (
    "DANUS_",
    "import danus",
    "from danus",
    "python -m danus",
    "python3 -m danus",
    "bin/danus",
    "mcp_servers.danus",
    "danus_root",
    "euler.research_control",
    "euler/research_control",
)

_TEXT_SUFFIXES = {
    ".py",
    ".sh",
    ".toml",
    ".md",
    ".example",
    ".json",
    ".html",
    ".js",
    ".css",
    ".cff",
}


def _is_production_py(path: Path) -> bool:
    try:
        path.relative_to(_PACKAGE_ROOT)
    except ValueError:
        return False
    if "tests" in path.parts or path.name.startswith("test_"):
        return False
    return path.suffix == ".py"


def _is_current_surface(path: Path) -> bool:
    if path in _SKIP_FILES or not path.is_file():
        return False
    if any(part in _SKIP_DIR_NAMES or part.endswith(".egg-info") for part in path.parts):
        return False
    if any(part in _HISTORICAL_DOC_PARTS for part in path.parts):
        return False
    if path.suffix == ".py":
        return _is_production_py(path)
    if path.parent == _REPO_ROOT / "bin":
        return True
    if path.parent == _REPO_ROOT / "scripts" and path.suffix == ".sh":
        return True
    if path.parent == _REPO_ROOT / "config" and path.suffix == ".example":
        return True
    if path.suffix in _TEXT_SUFFIXES:
        rel = path.relative_to(_REPO_ROOT)
        top = rel.parts[0]
        if top in {
            "README.md",
            "ARCHITECTURE.md",
            "NOTICE",
            "OPERATOR.md",
            "EULER_DISTRIBUTION.md",
            "CITATION.cff",
            "docs",
            "agents",
            "examples",
        }:
            return True
        if top == "euler" and path.name in {
            "README.md",
            "INTERFACES.md",
            "DATA_MODEL.md",
        } or (
            top == "euler" and path.name.startswith("INTERFACES")
        ):
            return True
    return False


def _iter_scan_files() -> list[Path]:
    files: list[Path] = []
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if _is_current_surface(path):
            files.append(path)
    return sorted(files)


def test_production_python_has_no_danus_imports() -> None:
    offenders: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if not _is_production_py(path):
            continue
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(_PACKAGE_ROOT)
        if "import danus" in source or "from danus" in source:
            offenders.append(f"{rel}: source token import/from danus")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "danus" or alias.name.startswith("danus."):
                        offenders.append(f"{rel}: import {alias.name}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
            ):
                if node.module == "danus" or node.module.startswith("danus."):
                    offenders.append(f"{rel}: from {node.module}")
    assert not offenders, "production still imports danus:\n" + "\n".join(offenders)


def test_current_product_surfaces_have_no_danus_identity() -> None:
    offenders: list[str] = []
    for path in _iter_scan_files():
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(_REPO_ROOT)
        for token in _FORBIDDEN_TOKENS:
            if token in source:
                offenders.append(f"{rel}: {token!r}")
    assert not offenders, "product identity leftovers:\n" + "\n".join(offenders)


def test_release_root_has_no_client_persona_scaffolding() -> None:
    forbidden = (
        "AGENTS.md",
        "CLAUDE.md",
        "GROK.md",
        "CURSOR.md",
        ".agents",
        ".claude",
        ".codex",
        ".grok",
    )
    present = [name for name in forbidden if (_REPO_ROOT / name).exists()]
    assert not present, f"client-specific root scaffolding is present: {present}"


def test_release_attribution_is_canonical() -> None:
    citation = (_REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    package = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    license_text = (_REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (_REPO_ROOT / "NOTICE").read_text(encoding="utf-8")

    assert "family-names: Chen" in citation
    assert "given-names: Qiuyu" in citation
    assert citation.count("affiliation: Shanghai Jiao Tong University") == 2
    assert 'authors = [{ name = "Qiuyu Chen" }]' in package
    assert 'maintainers = [{ name = "canghaimeng" }]' in package
    assert "**By canghaimeng**" in readme
    assert "Copyright 2026 Qiuyu Chen (canghaimeng)" in license_text
    assert "PolyForm Noncommercial License 1.0.0" in license_text
    assert "PolyForm-Noncommercial-1.0.0" in citation
    assert "OSI Approved :: Apache" not in package
    upstream_license = (_REPO_ROOT / "LICENSES" / "Apache-2.0.txt").read_text()
    assert "Apache License" in upstream_license and "Version 2.0" in upstream_license
    assert (_REPO_ROOT / "LICENSING.md").is_file()
    assert "By canghaimeng" in notice
    assert "Affiliation: Shanghai Jiao Tong University" in notice
    assert "Danus" in notice and "Rethlas" in notice
    assert "FrenzyMath" in notice
    assert "Bin Wu" not in citation
    assert "FrenzyMath" not in citation
    assert "Bin Wu" not in package
    assert "FrenzyMath" not in package

    credit_re = re.compile(
        r"(?i)(?:authored|written|created|implemented|developed|built|"
        r"completed|generated)\s+by\s+(?:codex|grok|claude|cursor)|"
        r"co-authored-by"
    )
    offenders: list[str] = []
    for path in _iter_scan_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(_REPO_ROOT)
        if credit_re.search(text):
            offenders.append(f"{rel}: model/provider authorship credit")
    assert not offenders, "attribution leftovers:\n" + "\n".join(offenders)


def test_upstream_adaptation_headers_are_consistent() -> None:
    roots = (
        _REPO_ROOT / "euler",
        _REPO_ROOT / "scripts",
        _REPO_ROOT / "bin",
        _REPO_ROOT / "agents",
        _REPO_ROOT / "examples",
        _REPO_ROOT / ".github",
    )
    candidates = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and path != _THIS_FILE
        and not (set(path.relative_to(_REPO_ROOT).parts) & (_SKIP_DIR_NAMES | {".elan"}))
        and (_REPO_ROOT / "euler" / "verify" / "agent") not in path.parents
        and (path.suffix in _TEXT_SUFFIXES | {".yml", ".yaml", ".txt"} or not path.suffix)
    ] + [_REPO_ROOT / "pyproject.toml"]
    adapted = []
    inconsistent = []
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="ignore")
        has_spdx = ("SPDX-License-Identifier: Apache-2.0" in text
                    or "SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0" in text)
        has_origin = (
            "Adapted from Danus/Rethlas; modified for Euler Agent by "
            "canghaimeng."
        ) in text
        if has_spdx or has_origin:
            adapted.append(path)
        has_original_copyright = "Copyright 2026 Qiuyu Chen (canghaimeng)." in text
        if has_spdx != (has_origin or has_original_copyright):
            inconsistent.append(path.relative_to(_REPO_ROOT).as_posix())
    assert adapted, "expected the retained upstream/original copyright notices"
    assert not inconsistent, f"incomplete adaptation headers: {inconsistent}"

    for rel in (
        "euler/core/factgraph.py",
        "euler/execution/loop.py",
        "euler/gateway/server.py",
        "euler/verify/service.py",
        "euler/write_paper/server.py",
        "scripts/bootstrap.sh",
        "bin/euler",
    ):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "SPDX-License-Identifier: Apache-2.0" in text, rel
    for rel in (
        "euler/control/controller.py",
        "euler/providers/grok.py",
        "euler/demo.py",
    ):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "Adapted from Danus/Rethlas" not in text, rel


def main() -> None:
    test_production_python_has_no_danus_imports()
    test_current_product_surfaces_have_no_danus_identity()
    test_release_root_has_no_client_persona_scaffolding()
    test_release_attribution_is_canonical()
    test_upstream_adaptation_headers_are_consistent()
    print("ALL PACKAGE NAMESPACE TESTS PASSED")


if __name__ == "__main__":
    main()
