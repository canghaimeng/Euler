"""Multi-project isolation and source-grounded exploration/derivation contracts."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest
from fastapi.testclient import TestClient
from euler.observability import app
from euler.observability import monitor
from euler.observability.derivation import proof_sections
from .test_monitor import _write, _fact, seed_research_events


@pytest.fixture
def projects(tmp_path, monkeypatch):
    roots = [tmp_path / 'one', tmp_path / 'two']
    configs = []
    for i, root in enumerate(roots):
        _write(root / 'project.json', {'name': root.name, 'workers': []})
        _fact(root, 'same-id', 'tester')
        p = root / 'fact_graph/facts/same-id.md'
        p.write_text(p.read_text().replace('Lemma same-id', f'Only project {root.name}'))
        config = tmp_path / f'{root.name}.json'
        _write(config, {'name': f'Display {root.name}'})
        configs.append(config)
    registry = tmp_path / 'catalog.json'
    _write(registry, {'projects': [{'id': root.name, 'name': root.name,
            'project_dir': str(root), 'config': str(config)} for root, config in zip(roots, configs)],
            'discover_project_roots': [str(tmp_path / 'new-projects')]})
    config = json.loads(configs[0].read_text()); config['project_catalog'] = str(registry); _write(configs[0], config)
    monkeypatch.setenv('EULER_DASHBOARD_PROJECT', str(roots[0]))
    monkeypatch.setenv('EULER_DASHBOARD_CONFIG', str(configs[0]))
    monkeypatch.delenv('EULER_DASHBOARD_TOKEN', raising=False)
    return TestClient(app), roots, registry


def test_every_endpoint_selects_the_requested_project(projects):
    client, roots, _ = projects
    for ident in ['one', 'two']:
        assert client.get('/api/workspace', params={'project': ident}).json()['project']['name'] == f'Display {ident}'
        fact = client.get('/api/facts/same-id', params={'project': ident}).json()
        assert f'Only project {ident}' in fact['statement']
        proof = client.get('/api/derivation', params={'project': ident, 'layer': 'facts', 'node_id': 'same-id'}).json()
        assert f'Only project {ident}' in proof['node']['statement']
        assert proof['sections'][0]['text'] == 'By identity.'
    assert 'project_dir' not in client.get('/api/projects').text
    for endpoint in ['/api/workspace', '/api/graph', '/api/channels', '/api/exploration']:
        assert client.get(endpoint, params={'project': '../../private'}).status_code == 404


def test_parallel_project_requests_do_not_mix_configs_or_proofs(projects):
    client, _, _ = projects
    def read(ident):
        w = client.get('/api/workspace', params={'project': ident}).json()
        f = client.get('/api/facts/same-id', params={'project': ident}).json()
        return ident, w['project']['name'], f['statement']
    with ThreadPoolExecutor(max_workers=6) as pool:
        result = list(pool.map(read, ['one', 'two'] * 8))
    assert all(name == 'Display ' + ident and 'Only project ' + ident in statement for ident, name, statement in result)


def test_live_discovery_and_exploration_work_without_paper_or_facts(projects, tmp_path):
    client, _, _ = projects
    assert 'fresh' not in {p['id'] for p in client.get('/api/projects').json()['projects']}
    root = tmp_path / 'new-projects/fresh'
    _write(root / 'project.json', {'name': 'Fresh research problem', 'workers': ['explorer']})
    _write(root / 'workers/explorer/TASK.md', '# Try an extremal counterexample\nNo paper exists yet.')
    _write(root / 'workers/explorer/.status.json', {'state': 'created', 'updated_at': time.time()})
    _write(root / 'global_memory/plan.jsonl', json.dumps({'id': 'p1', 'claim': 'Search small instances', 'author': 'explorer', 'timestamp_utc': '2026-10-05T10:00:00Z'})+'\n')
    assert 'fresh' in {p['id'] for p in client.get('/api/projects').json()['projects']}
    view = client.get('/api/exploration?project=fresh').json()
    assert view['facts'] == [] and view['branches'] == []
    assert view['workers'][0]['task_summary'] == 'Try an extremal counterexample'
    assert view['signals'][0]['kind'] == 'plan'
    with (root / 'global_memory/obstacle.jsonl').open('w') as f:
        f.write(json.dumps({'id': 'o1', 'claim': 'The greedy route has a counterexample', 'timestamp_utc': '2026-10-05T11:00:00Z'})+'\n')
    updated = client.get('/api/exploration?project=fresh').json()
    assert updated['signals'][0]['id'] == 'o1'
    _write(root / 'workers/explorer/local_memory/private.md', 'NEVER-PUBLISH')
    assert 'NEVER-PUBLISH' not in client.get('/api/exploration?project=fresh').text


def test_requires_direction_and_recorded_claim_are_preserved(projects):
    from euler.control.event_store import EventStore
    from euler.control.obligations import Relation, RelationType
    from euler.control.schema import EventEnvelope
    from euler.control.tests.test_obligation_views import _contract
    client, roots, _ = projects
    root = roots[0]; ids = seed_research_events(root); contract = _contract(problem_id=root.name)
    relation = Relation.build(relation_type=RelationType.REQUIRES, source=ids['target'], target=ids['lemma'],
                              exact_claim='Use the base identity to justify the target.', scope=contract.scope,
                              contract_id=contract.contract_id, definitions_hash=contract.scope.definitions_hash)
    with EventStore(root/'control/control.sqlite3') as store:
        store.append(EventEnvelope.build(project_id=root.name, actor='test', event_type='relation.created',
            idempotency_key='requires', timestamp_utc='2026-01-02T00:00:00Z', payload={'relation': relation.to_dict()}))
    view = client.get('/api/derivation', params={'project': 'one', 'layer': 'research', 'node_id': ids['target']}).json()
    assert view['premises'][0]['id'] == ids['lemma']
    assert view['premises'][0]['rationale'] == relation.exact_claim
    assert view['routes'][0]['premises'][0]['id'] == ids['lemma']
    assert view['sections'] == [] and view['gap']  # A dependency is not a synthesized proof.
    assert view['source_kind'] == 'recorded_relations'


def test_proof_sections_are_exact_source_slices():
    proof = 'Overview\n\n## First identity\nA = B.\n```lean\n#check Nat\n```\n\n## Second identity\nB = C.\n'
    sections = proof_sections(proof)
    assert ''.join(s['text'] for s in sections) == proof
    assert len(sections) == 3
    assert all(s['text'] == proof[s['start']:s['end']] for s in sections)
    assert proof_sections('') == []


def test_discovery_does_not_follow_symlinks(projects, tmp_path):
    client, roots, _ = projects
    container = tmp_path/'new-projects'; container.mkdir()
    (container/'private-shortcut').symlink_to(roots[0], target_is_directory=True)
    assert len(client.get('/api/projects').json()['projects']) == 2


def test_selective_quota_display_does_not_pause_native_roles(projects, tmp_path):
    client, roots, _ = projects
    config = tmp_path/'one.json'; cfg = json.loads(config.read_text())
    quota = tmp_path/'quota.json'; cfg['quota_file'] = str(quota); _write(config, cfg)
    _write(quota, {'checked_at': time.time(), 'permit': True, 'remaining_percent': 55,
                  'threshold_percent': 10, 'latched': False, 'status': 'healthy',
                  'cursor_pause_scope': 'cursor_roles', 'cursor_threshold_percent': 3, 'cursor_permit': False,
                  'providers': {'codex': {'remaining_percent': 55}, 'cursor': {'remaining_percent': 2.9, 'on_demand_enabled': False}}})
    data = client.get('/api/quota?project=one').json()
    assert data['permit'] is True and data['cursor_permit'] is False
    assert data['remaining_percent'] == 55 and data['cursor_threshold_percent'] == 3


def test_recorded_fact_link_restores_proof_only_when_hashes_match(projects):
    import hashlib
    from euler.control.event_store import EventStore
    from euler.control.schema import EventEnvelope
    client, roots, _ = projects
    root = roots[0]; ids = seed_research_events(root)
    # Use an exact matching accepted Fact and its original immutable admission hash.
    from euler.control.tests.test_obligation_views import _contract
    statement = _contract(problem_id=root.name).exact_statement
    p = root/'fact_graph/facts/source-proof.md'
    text = f'---\nfact_id: source-proof\nauthor: alpha\npredecessors: []\n---\n## statement\n{statement}\n## proof\n### Step 1\nUse the original identity.\n### Step 2\nApply the bound.\n'
    _write(p, text)
    with EventStore(root/'control/control.sqlite3') as store:
        store.append(EventEnvelope.build(project_id=root.name, actor='test', event_type='euler.fact_linked',
            idempotency_key='source-link', timestamp_utc='2026-01-03T00:00:00Z', payload={
                'fact_id': 'source-proof', 'obligation_id': ids['target'], 'evidence_id': ids['evidence'],
                'dependency_hashes': {'source-proof': hashlib.sha256(text.encode()).hexdigest()}}))
    response = client.get('/api/derivation', params={'project': 'one', 'node_id': ids['target']}).json()
    assert response['proof_source_fact_id'] == 'source-proof' and len(response['sections']) == 2
    p.write_text(text.replace('original identity', 'unreviewed replacement'))
    monitor._CACHE.clear()
    changed = client.get('/api/derivation', params={'project': 'one', 'node_id': ids['target']}).json()
    assert changed['sections'] == [] and changed['proof_source_fact_id'] is None


def test_workspace_discovery_covers_default_and_isolated_new_tasks(projects, tmp_path):
    client, _, registry = projects
    workspace = tmp_path/'workspace'
    config = json.loads(registry.read_text());config['discover_workspaces'] = [str(workspace)];_write(registry, config)
    for relative, name in [('runtime/projects/direct', 'Direct task'), ('experiment/runtime/projects/isolated', 'Isolated task')]:
        _write(workspace/relative/'project.json', {'name': name, 'workers': [], 'goal': 'Explore an unsolved problem'})
    data = client.get('/api/projects').json()
    assert {'direct', 'isolated'} <= {p['id'] for p in data['projects']}
    response = client.get('/api/exploration?project=direct').json()
    assert response['project']['name'] == 'Direct task'
    assert response['project']['goal'] == 'Explore an unsolved problem'
    assert response['facts'] == [] and response['phase'] == 'exploring'


def test_managed_running_task_exposes_real_output_goal_not_empty_heading(projects):
    client, roots, _ = projects
    root = roots[0]
    _write(root/'project.json', {'name': 'one', 'workers': ['alpha']})
    _write(root/'workers/alpha/.status.json', {'state': 'created'})
    _write(root/'workers/alpha/TASK.md', '---\nproject_id: one\n---\n# Euler dispatch\noutput_contract: Find a minimal counterexample or prove the proposed inequality.\npacket_hash: abc\n\n## ContextPacket\n===== CONTRACT =====\n{}\n')
    view = client.get('/api/exploration?project=one').json()
    assert view['workers'][0]['task_summary'] == '目标：Find a minimal counterexample or prove the proposed inequality.'


def test_attempt_outcomes_and_reasons_remain_unverified_records(projects):
    client, roots, _ = projects
    _write(roots[0]/'global_memory/proof_attempt.jsonl', json.dumps({'id':'attempt1','claim':'Try an exchange argument','result':'blocked','reason':'The exchange loses the feasibility condition','author':'alpha','timestamp_utc':'2026-10-05T12:00:00Z'})+'\n')
    response = client.get('/api/exploration?project=one').json()
    row = next(x for x in response['signals'] if x['id']=='attempt1')
    assert row['attempt_result']=='blocked' and 'feasibility' in row['reason']
    assert row['verification']=='public_research_record'
    assert response['phase']=='exploring'
