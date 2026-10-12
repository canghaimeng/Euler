#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Check public documentation pairs, local links, anchors and case languages."""
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def chinese(path):
    return path.name.endswith(('.zh.md', '.zh-CN.md'))


def prose(text):
    return re.sub(r'^```[^\n]*\n.*?^```[^\n]*$', '', text, flags=re.M | re.S)


def anchors(path):
    result = set()
    duplicates = {}
    for heading in re.findall(r'^#{1,6} (.+)$', prose(path.read_text()), re.M):
        heading = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', heading)
        heading = heading.replace('`', '').replace('*', '').lower()
        slug = ''.join(c for c in heading if c in '-_ ' or unicodedata.category(c)[0] in 'LN').replace(' ', '-')
        count = duplicates.get(slug, 0)
        duplicates[slug] = count + 1
        result.add(slug + (f'-{count}' if count else ''))
    result.update(re.findall(r'<a\s+(?:name|id)=["\']([^"\']+)', path.read_text()))
    return result


def main():
    files = [ROOT / name for name in ('README.md', 'ARCHITECTURE.md', 'CONTRIBUTING.md', 'LICENSING.md')]
    files += sorted(p for p in (ROOT / 'docs').glob('*.md') if not chinese(p))
    files += sorted(p for p in (ROOT / 'examples').rglob('*.md') if not chinese(p))
    errors = []
    checked = 0
    pairs = len(files)
    for path in list(files):
        partner = path.with_name('README.zh-CN.md' if path == ROOT / 'README.md' else path.stem + '.zh.md')
        if partner.exists():
            files.append(partner)
        else:
            errors.append(f'{path.relative_to(ROOT)}: missing Chinese version')
    for path in files:
        language = 'zh' if chinese(path) else 'en'
        text = prose(path.read_text())
        # Language selectors are the intentional exception to same-language links.
        for line in text.splitlines():
            switch = '[English](' in line and '[简体中文](' in line
            switch |= '**English**' in line or '**简体中文**' in line
            for raw in re.findall(r'\]\(([^\s)]+)\)', line):
                url = urlsplit(raw)
                if url.netloc == 'canghaimeng.site' and url.path.startswith('/euler/cases'):
                    if parse_qs(url.query).get('lang') != [language]:
                        errors.append(f'{path.relative_to(ROOT)}: case language mismatch: {raw}')
                if url.scheme or url.netloc:
                    continue
                target = (path.parent / unquote(url.path)).resolve() if url.path else path
                if target.is_dir():
                    target /= 'README.md'
                checked += 1
                if not target.exists():
                    errors.append(f'{path.relative_to(ROOT)}: missing target: {raw}')
                    continue
                if target.suffix == '.md':
                    if not switch and chinese(target) != chinese(path):
                        errors.append(f'{path.relative_to(ROOT)}: language mismatch: {raw}')
                    if url.fragment and unquote(url.fragment) not in anchors(target):
                        errors.append(f'{path.relative_to(ROOT)}: missing anchor: {raw}')
    for error in errors:
        print(error)
    print(f'{pairs} language pairs; {checked} local links checked; {len(errors)} errors')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
