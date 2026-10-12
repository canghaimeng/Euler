"""The public archive can only read exported data, including under hostile requests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

SOURCE = Path(__file__).resolve().parents[3] / 'scripts/public_snapshot.py'
spec = importlib.util.spec_from_file_location('public_snapshot', SOURCE)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


@pytest.fixture
def archive(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'static').mkdir()
    (tmp_path / 'index.html').write_text('<h1>Published proof</h1>')
    (tmp_path / 'static/app.js').write_text('/* viewer */')
    # Even if a sensitive file is accidentally present, it has no public route.
    (tmp_path / 'private.txt').write_text('DO NOT SERVE')
    (tmp_path / 'static/leak.txt').symlink_to(tmp_path / 'private.txt')
    routes = {}
    for path, params, data in [('/api/projects', {}, {'projects': ['example']}),
                               ('/api/graph', {'project': 'example', 'layer': 'research'}, {'nodes': [{'id': 'N1'}]})]:
        key = snapshot.route_key(path, params)
        name = hashlib.sha256(key.encode()).hexdigest() + '.json'
        (tmp_path / 'data' / name).write_text(json.dumps(data))
        routes[key] = name
    (tmp_path / 'routes.json').write_text(json.dumps({
        'prefix': '/euler/cases', 'default_project': 'example', 'routes': routes,
        'static_files': ['static/app.js', 'static/leak.txt']}))
    server = ThreadingHTTPServer(('127.0.0.1', 0), snapshot.make_handler(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))

    def request(path, method='GET'):
        req = Request(f'http://127.0.0.1:{server.server_port}' + path, method=method,
                      data=b'{}' if method not in ('GET', 'HEAD') else None)
        try:
            response = opener.open(req)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    yield request
    server.shutdown()
    server.server_close()
    thread.join()


def test_snapshot_reads_only_exported_routes(archive):
    status, body, _ = archive('/euler/cases/api/graph?layer=research&project=example')
    assert status == 200 and json.loads(body)['nodes'][0]['id'] == 'N1'
    assert archive('/api/graph')[0] == 200
    assert archive('/euler/cases/static/app.js')[0] == 200
    assert archive('/euler/cases/api/graph?project=private')[0] == 404
    assert archive('/api/graph?project=example&project=private')[0] == 400
    assert archive('/api/graph?project=example&path=/etc/passwd')[0] == 404


@pytest.mark.parametrize('path', ['/api/team/ask', '/api/team/prepare', '/api/team/control',
    '/api/projects', '/api/human/interventions', '/api/milestones/123/restore'])
def test_no_control_route_can_execute(archive, path):
    for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        status, _, headers = archive('/euler/cases' + path, method)
        assert status == 405
        assert headers['Allow'] == 'GET, HEAD'
    if path != '/api/projects':
        assert archive(path)[0] == 404


@pytest.mark.parametrize('path', ['/private.txt', '/routes.json', '/server.py',
    '/static/leak.txt', '/static/../private.txt', '/static/%2e%2e/private.txt',
    '/static/human.js', '/static/team.js', '/api/team', '/api/human/state',
    '/api/projects/create-capabilities', '/api/milestones'])
def test_private_files_and_runtime_apis_are_inaccessible(archive, path):
    assert archive(path)[0] == 404


def test_head_and_browser_policy(archive):
    status, body, headers = archive('/', 'HEAD')
    assert status == 200 and not body
    assert "form-action 'none'" in headers['Content-Security-Policy']


def test_scrub_operational_metadata_preserves_mathematics():
    data = {'statement': 'For every n ≥ 1, x_n > 0.', 'scope': {'n': 'ℕ'},
            'credentials': {'api_key': 'private'}, 'thread_id': 'private',
            'source': '/home/researcher/Documents/AI4math/proof/Main.lean'}
    clean = snapshot.sanitize(data)
    assert clean['statement'] == data['statement'] and clean['scope'] == data['scope']
    assert 'credentials' not in clean and 'thread_id' not in clean
    assert clean['source'] == '[workspace]/proof/Main.lean'
