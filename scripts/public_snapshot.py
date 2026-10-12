#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Export reviewed workbench views; serve frozen files without an agent runtime.

The export command reads only the named observation endpoints. Review the output
before publishing: mathematical records can contain author-supplied prose.
The serve command uses only the standard library and the exported manifest.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import shutil
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener


def route_key(path, params):
    return path + '?' + urlencode(sorted(params.items()))


def sanitize(value):
    """Remove operational metadata and local path prefixes, retaining proof text."""
    private = {'quota', 'credentials', 'access_token', 'api_key', 'session_id',
               'thread_id', 'pid', 'cwd', 'home', 'environment', 'command',
               'launch_command', 'monitor_file', 'config_path', 'project_dir'}
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items() if k not in private}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'/home/[^/\s]+/(?:文档|Documents)/AI4math/', '[workspace]/', value)
        value = re.sub(r'/home/[^/\s]+/', '[home]/', value)
        value = re.sub(r'/Users/[^/\s]+/', '[home]/', value)
        value = re.sub(r'(?:127\.0\.0\.1|100\.114\.255\.117):\d+', '[local service]', value)
    return value


def export(args):
    root = Path(args.output).resolve()
    if root.exists():
        raise ValueError('Choose a new output directory; existing snapshots are preserved')
    if not args.project or any(not re.fullmatch(r'[a-zA-Z0-9_-]+', p) for p in args.project):
        raise ValueError('Explicit project IDs are required')
    prefix = args.prefix.rstrip('/')
    if prefix and not re.fullmatch(r'(?:/[a-zA-Z0-9_-]+)+', prefix):
        raise ValueError('Invalid URL prefix')
    source = args.source.rstrip('/')
    opener = build_opener(ProxyHandler({}))

    def get(path, params=None):
        url = source + path + ('?' + urlencode(params) if params else '')
        with opener.open(url, timeout=90) as response:
            return json.load(response)

    catalog = get('/api/projects')
    entries = {p['id']: p for p in catalog['projects']}
    if any(p not in entries or not entries[p]['available'] for p in args.project):
        raise ValueError('A selected project is unavailable')
    (root / 'data').mkdir(parents=True)
    routes = {}
    counts = {}

    def save(path, params, data):
        key = route_key(path, params)
        name = hashlib.sha256(key.encode()).hexdigest() + '.json'
        (root / 'data' / name).write_text(json.dumps(sanitize(data), ensure_ascii=False))
        routes[key] = name

    save('/api/projects', {}, {'default_project': args.project[0], 'projects': [
        {k: entries[p][k] for k in ('id', 'name', 'description', 'available') if k in entries[p]}
        for p in args.project]})
    tasks = []
    for project in args.project:
        params = {'project': project}
        workspace = get('/api/workspace', params)
        workspace.update(teams=[], activity=[], control={}, quota={'available': False},
                         verifier={'available': False}, warnings=[])
        save('/api/workspace', params, workspace)
        save('/api/architecture', params, get('/api/architecture', params))
        exploration = get('/api/exploration', params)
        exploration.update(workers=[], coordinators=[], signals=[], control={}, warnings=[])
        save('/api/exploration', params, exploration)
        counts[project] = {}
        for layer in ('research', 'facts'):
            query = {**params, 'layer': layer}
            graph = get('/api/graph', query)
            save('/api/graph', query, graph)
            focus = get('/api/map-focus', query)
            if 'frontier' in focus:
                focus['frontier']['records'] = []
            save('/api/map-focus', query, focus)
            counts[project][layer] = {'nodes': len(graph['nodes']), 'edges': len(graph['edges'])}
            for node in graph['nodes']:
                tasks.append(('/api/derivation', {**query, 'node_id': node['id']}))
                if layer == 'facts':
                    tasks.append(('/api/facts/' + node['id'], params))
    with ThreadPoolExecutor(max_workers=4) as pool:
        for path, params, data in pool.map(lambda task: (*task, get(*task)), tasks):
            save(path, params, data)

    repo = Path(__file__).resolve().parents[1]
    assets = repo / 'euler/observability/static'
    static = root / 'static'
    static.mkdir()
    # Control clients, live help, team transcripts and project creation are excluded.
    for name in ('style.css', 'app.js', 'i18n.js', 'reader-entry.css'):
        shutil.copy2(assets / name, static / name)
    shutil.copytree(assets / 'vendor', static / 'vendor')
    for name in ('LICENSE', 'NOTICE', 'LICENSING.md'):
        shutil.copy2(repo / name, static / name)
    shutil.copytree(repo / 'LICENSES', static / 'LICENSES')
    js = (static / 'app.js').read_text()
    js = js.replace("const VIEWS = ['overview', 'reader', 'explore', 'map', 'ops', 'team'];",
                    "const VIEWS = ['overview', 'reader', 'explore', 'map'];")
    js = js.replace("teams: 'ops', activity: 'ops'", "teams: 'overview', activity: 'explore'")
    js = js.replace('live: true,', 'live: false,')
    js = js.replace("'/api", "'" + prefix + '/api').replace('"/api', '"' + prefix + '/api')
    (static / 'app.js').write_text(js)
    # Restrict project-specific labels to the published cases, without executing JS.
    i18n = (static / 'i18n.js').read_text()
    marker = 'const DISPLAY_LABELS = '
    start = i18n.index(marker) + len(marker)
    labels, length = json.JSONDecoder().raw_decode(i18n[start:])
    labels = {k: v for k, v in labels.items() if k in args.project}
    i18n = i18n[:start] + json.dumps(labels, ensure_ascii=False) + i18n[start + length:]
    i18n += "\nObject.assign(I18N.en, {'tab.explore':'Research history','footer.brand':'Published research archive','footer.note':'Read statements, dependencies and recorded evidence. Model calls are disabled.','publication.label':'Public case studies · read-only','publication.help':'How to read these cases'});\n"
    i18n += "Object.assign(I18N.zh, {'tab.explore':'研究历史','footer.brand':'公开研究档案','footer.note':'可阅读命题、依赖与验证记录；公开页面已禁用模型调用。','publication.label':'公开案例 · 只读展示','publication.help':'如何阅读这些案例'});\n"
    (static / 'i18n.js').write_text(i18n)
    html = (assets / 'index.html').read_text()
    html = re.sub(r'<script[^>]+src="/static/(?:human|milestones|team|creation|reader-entry)\.js[^>]*></script>', '', html)
    html = re.sub(r'<link[^>]+href="/static/human\.css[^>]*>', '', html)
    html = re.sub(r'<details class="euler-reader-entry".*?</details>', '', html, flags=re.S)
    html = html.replace('data-entry-page="help" data-entry-copy="help"', 'data-i18n="publication.help"')
    html = html.replace('</head>', '<link rel="stylesheet" href="/static/public.css" /></head>')
    generated = datetime.now(timezone.utc).isoformat()
    html = html.replace('<main id="main" tabindex="-1">', '<main id="main" tabindex="-1"><div class="public-snapshot"><span data-i18n="publication.label">Public case studies · read-only</span> · ' + generated[:10] + '</div>')
    html = html.replace('/static/', prefix + '/static/')
    (root / 'index.html').write_text(html)
    (static / 'public.css').write_text('''
#ops-view,#team-view,#overview-live,.verdict-live,#live-toggle,#refresh-button,
#refresh-note,#ops-alert,#explore-live-dot,[data-view="ops"],[data-view="team"]{display:none!important}
.public-snapshot{font-size:12px;color:#697d73;margin:0 0 18px}
.overview-split{grid-template-columns:1fr}
''')
    (static / 'help.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reading the Euler case studies</title><style>body{max-width:780px;margin:60px auto;padding:0 24px;background:#f6f4ef;color:#1c3a3f;font:17px/1.7 system-ui}h1,h2{font-family:Georgia,serif}a{color:inherit}li{margin:12px 0}</style><a href="../">← Euler</a><h1>Reading the case studies · 案例阅读指南</h1><p>Explore three mathematical investigations through their goals, proof dependencies, alternative routes and recorded evidence. These pages preserve a published snapshot of historical research.</p><ol><li><b>Overview:</b> read the exact question, result and main proof modules.</li><li><b>Proof reader:</b> open a step to inspect its premises, argument and source excerpts.</li><li><b>Relation map:</b> follow the main line, select a node to highlight its dependencies, and switch to all records to inspect alternative routes.</li><li><b>Research history:</b> inspect recorded branches, local obstacles and established facts.</li></ol><p>Use the project selector to switch cases and 中文 / EN to switch the interface language. Mathematical source statements remain in their original language. Recorded evidence labels describe the checks performed during the research; browsing does not rerun them.</p><h2>从目标到证据</h2><p>先在「概览」了解原问题、结论和证明模块，再到「证明阅读」逐步展开前提、论证及原始来源。在「关系图」选择节点，可以突出它的依赖路径；切换「全部记录」可查看其他路线。「研究历史」保留研究分支、局部障碍和已有结论。</p><p>顶部可切换三个项目和中英文界面。数学原文保持原语言。这里展示的是历史研究快照，读者可以自由浏览；团队聊天、创建项目、恢复研究和模型调用均已关闭。</p><p><a href="https://github.com/canghaimeng/Euler">Euler on GitHub</a> · <a href="NOTICE">Software notices</a> · <a href="LICENSING.md">Licenses</a></p></html>''')
    manifest = {'format': 1, 'projects': args.project, 'default_project': args.project[0],
                'generated_at': generated, 'prefix': prefix, 'routes': routes,
                'counts': counts, 'static_files': [str(p.relative_to(root)) for p in static.rglob('*') if p.is_file()]}
    (root / 'routes.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    shutil.copy2(__file__, root / 'server.py')
    print(json.dumps({'output': str(root), 'routes': len(routes), 'counts': counts}, ensure_ascii=False))


def make_handler(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'routes.json').read_text())
    static_files = set(manifest['static_files'])

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, mime='application/json; charset=utf-8', head=False):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if status == 405:
                self.send_header('Allow', 'GET, HEAD')
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def reject_write(self):
            self.close_connection = True
            self.reply(405, b'{"detail":"Published archive: model calls and research controls are disabled"}')

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject_write

        def do_HEAD(self):
            self.serve(head=True)

        def do_GET(self):
            self.serve()

        def serve(self, head=False):
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            prefix = manifest.get('prefix', '')
            if prefix and path.startswith(prefix + '/'):
                path = path[len(prefix):]
            filename = None
            if path in ('/', '/index.html'):
                filename = 'index.html'
            elif path.lstrip('/') in static_files and path.startswith('/static/'):
                filename = path.lstrip('/')
            elif path.startswith('/api/'):
                try:
                    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=8)
                except ValueError:
                    return self.reply(400, b'{"detail":"Invalid query"}', head=head)
                query = dict(pairs)
                if len(query) != len(pairs):
                    return self.reply(400, b'{"detail":"Duplicate query parameter"}', head=head)
                if path != '/api/projects':
                    query.setdefault('project', manifest['default_project'])
                    if path in ('/api/graph', '/api/map-focus', '/api/derivation'):
                        query.setdefault('layer', 'research')
                name = manifest['routes'].get(route_key(path, query))
                if name and re.fullmatch(r'[0-9a-f]{64}\.json', name):
                    filename = 'data/' + name
            if filename:
                candidate = root / filename
                if candidate.is_file() and not candidate.is_symlink() and root in candidate.resolve().parents:
                    return self.reply(200, candidate.read_bytes(), mimetypes.guess_type(filename)[0] or 'application/octet-stream', head)
            self.reply(404, b'{"detail":"Not part of this published archive"}', head=head)

        def log_message(self, fmt, *args):
            print(self.command, urlsplit(self.path).path, flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    publish = commands.add_parser('export')
    publish.add_argument('--source', required=True)
    publish.add_argument('--project', action='append', required=True)
    publish.add_argument('--output', required=True)
    publish.add_argument('--prefix', default='')
    serve = commands.add_parser('serve')
    serve.add_argument('--root', default=str(Path(__file__).resolve().parent))
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8165)
    args = parser.parse_args()
    if args.command == 'export':
        export(args)
    else:
        ThreadingHTTPServer((args.host, args.port), make_handler(args.root)).serve_forever()


if __name__ == '__main__':
    main()
