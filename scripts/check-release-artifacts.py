#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Validate the contents of built distributions before uploading them."""
import argparse
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PAPERS = {f'examples/{case}/paper.pdf' for case in ('jnw', 'lewis-overton', 'nikoghosyan')}
PRIVATE_DIRS = {'.git', '.venv', 'runtime', 'snapshots', 'reports', 'node_modules',
                '.euler-team', '.euler-history', '.codex', '.grok', '.cursor'}


def check_private_paths(names):
    for name in names:
        path = PurePosixPath(name)
        assert not (set(path.parts) & PRIVATE_DIRS), f'Private directory in artifact: {name}'
        assert not path.is_absolute() and '..' not in path.parts, f'Unsafe artifact path: {name}'
        assert path.name not in ('.env', 'id_rsa', 'id_ed25519'), f'Credential filename: {name}'
        assert path.suffix not in ('.pem', '.p12', '.pfx', '.key', '.env'), f'Local configuration: {name}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    wheels = list(args.dist.glob('*.whl'))
    sources = list(args.dist.glob('*.tar.gz'))
    assert len(wheels) == len(sources) == 1, 'Expect one wheel and one source distribution'
    with tarfile.open(sources[0]) as archive:
        members = archive.getmembers()
        assert all(m.isfile() or m.isdir() for m in members), 'Unexpected source archive links'
        names = {str(PurePosixPath(m.name).relative_to(PurePosixPath(m.name).parts[0]))
                 for m in members if m.isfile()}
    check_private_paths(names)
    required = {'README.md', 'README.zh-CN.md', 'CITATION.cff', 'uv.lock', 'LICENSE',
                'LICENSING.md', 'NOTICE', 'LICENSES/Apache-2.0.txt',
                'agents/contracts/main_agent.md', 'scripts/check-doc-links.py',
                'scripts/public_snapshot.py', 'examples/personal-research/create_workspace.py',
                'docs/assets/euler-logo.svg', 'docs/assets/euler-frontier-loop.png'}
    localized = [*ROOT.glob('*.zh.md'), ROOT / 'README.zh-CN.md',
                 *(ROOT / 'docs').glob('*.zh.md'), *(ROOT / 'examples').rglob('*.zh.md')]
    required.update(p.relative_to(ROOT).as_posix() for p in localized)
    assert required <= names, f'Source distribution missing: {sorted(required - names)}'
    assert {n for n in names if n.endswith('.pdf')} == PAPERS, 'Unexpected or missing case papers'
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = set(archive.namelist())
    check_private_paths(wheel_names)
    for asset in ('index.html', 'app.js', 'help.html', 'help.en.md', 'help.zh.md',
                  'help-redirect.js', 'vendor/LICENSE.cytoscape', 'vendor/LICENSE.katex',
                  'vendor/LICENSE.markdown-it'):
        assert 'euler/observability/static/' + asset in wheel_names, f'Missing viewer asset: {asset}'
    assert not any(n.endswith(('.pdf', '.lean')) for n in wheel_names), 'Research artifact in wheel'
    print(f'Artifacts verified: {len(names)} source files; {len(wheel_names)} wheel entries; '
          f'{len(localized)} Chinese documents; exactly three selected case papers')


if __name__ == '__main__':
    main()
