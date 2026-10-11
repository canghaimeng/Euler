# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Historical curation never becomes an admission or arbitrary-file reader."""
import hashlib
import json
import pytest
from fastapi.testclient import TestClient
from euler.observability import app
from euler.observability.case_study import get_case, get_source, list_cases

@pytest.fixture
def records(tmp_path, monkeypatch):
    source = tmp_path / 'excerpt.md'; source.write_text('An original shared record.\n')
    manifest = {'schema_version': 1, 'edition': 'test', 'sources': {'s': {
        'id': 's', 'label': 'Original', 'file': 'excerpt.md',
        'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'locator': 'record x'}},
        'cases': [{'id': 'one', 'title': 'One', 'nodes': [{'id': 'o', 'sources': ['s']}],
                   'stages': [{'id': 'h', 'sources': ['s']}], 'outcome_sources': []},
                  {'id': 'two', 'title': 'Two', 'nodes': [], 'stages': []}]}
    path = tmp_path / 'cases.json';path.write_text(json.dumps(manifest))
    monkeypatch.setenv('EULER_CASE_STUDIES_MANIFEST', str(path))
    monkeypatch.delenv('EULER_DASHBOARD_TOKEN', raising=False)
    return source, path, manifest

def test_historical_projection_is_explicit_and_read_only(records):
    source,path,manifest=records
    before={p.name:p.read_bytes() for p in path.parent.iterdir()}
    result=get_case('one')
    assert result['projection']=='editorial_source_map'
    assert result['source_integrity']=='matched'
    assert 'text' not in result['sources']['s'] and 'file' not in result['sources']['s']
    assert get_source('one','s')['text']==source.read_text()
    assert before=={p.name:p.read_bytes() for p in path.parent.iterdir()}

def test_modified_source_withdraws_map_and_never_returns_changed_text(records):
    source,_,_=records;source.write_text('different proof')
    assert get_case('one')['source_integrity']=='withdrawn'
    result=get_source('one','s')
    assert result['integrity']=='changed' and 'text' not in result

def test_source_must_belong_to_selected_project(records):
    assert get_source('two','s') is None
    client=TestClient(app)
    assert client.get('/api/case-studies/two/sources/s').status_code==404
    assert client.get('/api/case-studies/missing').status_code==404
    assert client.get('/api/case-studies/one').json()['source_integrity']=='matched'

@pytest.mark.parametrize('kind',['traversal','absolute','symlink'])
def test_sources_cannot_escape_server_owned_bundle(records,kind,tmp_path):
    source,path,manifest=records
    secret=tmp_path.parent/(tmp_path.name+'-secret');secret.write_text('private')
    if kind=='symlink':
        source.unlink();source.symlink_to(secret);value='excerpt.md'
    elif kind=='absolute':value=str(secret)
    else:value='../'+secret.name
    manifest['sources']['s'].update(file=value,sha256=hashlib.sha256(b'private').hexdigest())
    path.write_text(json.dumps(manifest))
    assert get_case('one')['source_integrity']=='withdrawn'
    assert 'text' not in get_source('one','s')

def test_existing_auth_boundary_covers_historical_records(records,monkeypatch):
    monkeypatch.setenv('EULER_DASHBOARD_TOKEN','test-access')
    client=TestClient(app)
    assert client.get('/api/case-studies').status_code==401
    assert client.get('/api/case-studies',headers={'Authorization':'Bearer test-access'}).status_code==200

def test_absent_configuration_has_no_builtin_private_cases(monkeypatch):
    monkeypatch.delenv('EULER_CASE_STUDIES_MANIFEST',raising=False)
    assert list_cases()['cases']==[] and get_case('one') is None


def test_native_projection_preserves_controller_and_never_admits_evidence(records):
    from euler.observability.historical_projection import attach, derivation
    source,path,manifest=records
    def pair(t):return {'en':t,'zh':t}
    manifest['cases'][0].update(question=pair('Original question'),scope=pair('Original scope'),
        result=pair('Reported result'),verification=pair('Source review'),frontier=pair('Recorded boundary'),
        edges=[],stages=[],nodes=[{'id':'result','kind':'target','title':pair('Target'),
            'summary':pair('Original statement'),'detail':pair('Reading explanation'),
            'sources':['s'],'column':0}])
    path.write_text(json.dumps(manifest))
    original={'target_id':'actual-root','nodes':[{'id':'actual-root','status':'OPEN'}],'edges':[]}
    control={'target_status':'OPEN','outcome':None}
    state={'workspace':{'control':control,'warnings':[]},'facts':{},'research_graph':original}
    projected=attach(state,{'case_study_id':'one'})
    assert projected['workspace']['control']=={'target_status':'OPEN','outcome':None}
    assert projected['original_research_graph']==original
    graph=projected['research_graph']
    assert graph['projection']=='historical_source'
    assert all(n['status']=='DOCUMENTED' for n in graph['nodes'])
    assert all(n.get('checker_result') is None for n in graph['nodes'])
    read=derivation(projected,graph['target_id'])
    assert source.read_text().strip() in read['proof']
    assert read['process']['status']=='editorial'
    assert read['projection']=='historical_source'
    source.write_text('changed')
    withdrawn=attach({'workspace':{'control':control,'warnings':[]},'facts':{},'research_graph':original}, {'case_study_id':'one'})
    assert withdrawn['research_graph']==original
    assert withdrawn['workspace']['historical_case']['source_integrity']=='withdrawn'


def test_verified_fact_bindings_close_only_matching_active_statements(records, tmp_path):
    from euler.observability.historical_projection import attach
    source,path,manifest=records
    pair=lambda t:{'en':t,'zh':t}
    case=manifest['cases'][0]
    case.update(fact_target_id='f',question=pair('Q'),scope=pair('S'),result=pair('R'),verification=pair('V'),frontier=pair('F'),
        edges=[],stages=[],outcome_sources=['s'],nodes=[{'id':'result','kind':'target','title':pair('Target'),
        'summary':pair('Claim'),'detail':pair('Proof component'),'sources':['s'],'column':1,
        'verification_binding':{'kind':'accepted_facts','fact_ids':['f'], 'fact_sha256':{'f':'expected'}}}])
    path.write_text(json.dumps(manifest))
    project=tmp_path/'project';(project/'global_memory').mkdir(parents=True)
    checks=project/'global_memory/verification.jsonl'
    checks.write_text(json.dumps({'id':'v','fact_id':'f','verdict':'correct','claim':'Claim'})+'\n')
    def state(hash='expected',status='accepted'):
        return {'workspace':{'control':{'target_status':'OPEN'},'warnings':[]},
            'facts':{'f':{'status':status,'source_sha256':hash}},
            'facts_graph':{'nodes':[{'id':'f','kind':'fact','status':status,'depth':0,'predecessors':[]}],'edges':[]},
            'research_graph':{'nodes':[],'edges':[]}}
    good=attach(state(),{'case_study_id':'one'},project=project)
    target=next(n for n in good['research_graph']['nodes'] if n['id']==good['research_graph']['target_id'])
    assert good['facts_graph']['target_id']=='f'
    assert good['facts_graph']['reading_order']==['f']
    assert target['status']=='DISCHARGED'
    assert target['state_basis']['verification_record_ids']==['v']
    assert good['workspace']['control']['target_status']=='OPEN'
    assert any(e['type']=='targets' and e['target']==target['id'] for e in good['research_graph']['edges'])
    for snapshot in [state(hash='changed'),state(status='revoked')]:
        bad=attach(snapshot,{'case_study_id':'one'},project=project)
        assert not bad['workspace']['historical_case']['root_closed']
    checks.write_text(json.dumps({'id':'v','fact_id':'f','verdict':'wrong'})+'\n')
    bad=attach(state(),{'case_study_id':'one'},project=project)
    assert not bad['workspace']['historical_case']['root_closed']
    check=next(n for n in bad['research_graph']['nodes'] if ':verification:' in n['id'])
    assert check['checker_result']=='FAIL'
