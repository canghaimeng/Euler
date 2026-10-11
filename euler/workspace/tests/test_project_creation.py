"""Browser creation -> durable dialogue -> lead-created teammate -> checkpoint."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from fastapi.testclient import TestClient

from euler.workspace import team, team_provider, team_mcp
from euler.workspace.project_creation import create_project
from euler.workspace.milestones import Milestones, public_projection


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    original=tmp_path/'original';original.mkdir();(original/'project.json').write_text(json.dumps({'name':'original','workers':[]}))
    setup=tmp_path/'setup';setup.mkdir()
    config={'allow_create':True,'root':str(tmp_path/'research'),'registry':str(setup/'created.json'),
            'defaults':{'codex':{'model':'test-model','effort':'high'}}}
    for env,file,data in [('EULER_PROJECT_CREATION_CONFIG','creation.json',config),
                          ('EULER_TEAM_CONFIG','team.json',{'projects':{}}),
                          ('EULER_MILESTONE_CONFIG','milestones.json',{'projects':{}})]:
        path=setup/file;path.write_text(json.dumps(data));monkeypatch.setenv(env,str(path))
    monkeypatch.setenv('EULER_DASHBOARD_PROJECT',str(original))
    for key in ['EULER_DASHBOARD_CONFIG','EULER_MILESTONE_REGISTRY','EULER_DASHBOARD_TOKEN']:
        monkeypatch.delenv(key,raising=False)
    calls=[]
    monkeypatch.setattr(team,'start_runner',lambda p:calls.append(p))
    return config,calls


def request(key='test-create'):
    return {'name':'新的研究项目','question':'Prove that there are infinitely many primes.',
            'lead':{'provider':'codex','model':'test-model','effort':'high'},'idempotency_key':key}


def test_creation_is_idempotent_and_starts_in_the_selected_project(workspace):
    from euler.observability.app import app
    settings,calls=workspace;client=TestClient(app)
    result=client.post('/api/projects',json=request());assert result.status_code==200,result.text
    made=result.json();root=Path(settings['root'])/'projects'/made['project']
    assert made['url'].endswith('#team') and len(calls)==1
    assert client.post('/api/projects',json=request()).json()['project']==made['project']
    assert len(list(root.parent.iterdir()))==1
    assert client.post('/api/projects',json={**request(),'name':'Different'}).status_code==409
    catalog=client.get('/api/projects').json()['projects']
    assert next(p for p in catalog if p['id']==made['project'])['name']=='新的研究项目'
    state=client.get('/api/team',params={'project':made['project']}).json()
    assert state['ready_for_conversation'] and state['can_add_members']
    assert state['policy']['max_turns'] is None and len(state['messages'])==1
    assert state['messages'][0]['request']['question']==request()['question']
    graph=client.get('/api/graph',params={'project':made['project'],'layer':'research'}).json()
    assert len(graph['nodes'])==1 and graph['nodes'][0]['status']=='OPEN'
    assert not (root/'fact_graph').exists()


def test_create_and_add_member_require_personal_control_origin(workspace):
    from euler.observability.app import app
    client=TestClient(app)
    assert client.post('/api/projects',json=request(),headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.post('/api/projects',json={**request(),'project_dir':'/tmp/forbidden'}).status_code==409
    assert client.post('/api/projects',json={**request(),'lead':{**request()['lead'],'binary':'sh'}}).status_code==409


def test_first_dialogue_can_add_consult_and_checkpoint_a_real_roster(workspace,monkeypatch):
    from euler.workspace import team_acp
    monkeypatch.setattr(team_acp,'binary_for',lambda _: 'model-free-fixture')
    settings,_=workspace;made=create_project(request(),settings)
    root=Path(settings['root'])/'projects'/made['project'];prepared=[];turns=[]
    def prepare(project,member,state):
        prepared.append(member['id']);archive=project/member['workdir']/'native.jsonl';archive.write_text('opaque native fixture')
        return {'session_id':'test-'+member['id'],'archive':str(archive),'recovery':'NEW_SESSION'}
    def run(project,member,state,question,cancelled):
        assert not cancelled();turns.append(member['id'])
        if len(turns)==1:
            monkeypatch.setenv('EULER_TEAM_PROJECT',str(root));monkeypatch.setenv('EULER_TEAM_MEMBER',member['id'])
            added=team_mcp.euler_team_create_member('独立审查','grok','test-grok','high','Review the prime divisor step')
            team_mcp.euler_team_ask(added['id'],'Check the key step.')
        return {'reply':'Public test response','native_session_id':member['session_id']}
    monkeypatch.setattr(team_provider,'prepare_member',prepare);monkeypatch.setattr(team_provider,'run_turn',run)
    team.command(root,'resume');team.drain(root)
    state=team.load(root);helper=state['members'][1]
    assert turns==['lead',helper['id'],'lead'] and prepared==['lead',helper['id']]
    assert all(m['state']=='ANSWERED' for m in state['messages'])
    assert state['roster_events'][0]['actor']=='agent:lead'
    assert state['roster_events'][0]['parent_message_id']==state['messages'][0]['id']
    checkpoint=Milestones(root).capture('Expanded team',key='checkpoint',projection=lambda:public_projection(root))
    restored=Path(Milestones(root).restore(checkpoint['commit'],key='fork')['project_dir'])
    restored_state=team.load(restored)
    assert [m['name'] for m in restored_state['members']]==['codex','独立审查']
    assert restored_state['roster_events']==state['roster_events']
    assert len(restored_state['messages'])==3 and not team.ready_for_conversation(restored_state)
    assert not (root/'fact_graph').exists() and not (restored/'fact_graph').exists()


def test_human_member_addition_is_branch_local_and_concurrent_safe(workspace):
    from euler.observability.app import app
    settings,_=workspace;made=create_project(request(),settings)
    root=Path(settings['root'])/'projects'/made['project']
    member={'name':'Reviewer','provider':'grok','model':'test','effort':'high','role':'assistant','assignment':'Check the construction.'}
    client=TestClient(app)
    added=client.post('/api/team/members',params={'project':made['project']},json={'member':member,'idempotency_key':'one'})
    assert added.status_code==200,added.text
    assert client.post('/api/team/members',params={'project':made['project']},json={'member':member,'idempotency_key':'one'}).json()==added.json()
    with pytest.raises(team.TeamError,match='active lead'):
        team.add_member(root,{**member,'name':'Unauthorized'},key='bad',actor='agent:'+added.json()['id'])
    with pytest.raises(team.TeamError,match='lead is unchanged'):
        team.add_member(root,{**member,'name':'Second lead','role':'lead'},key='bad-role')
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda i:team.add_member(root,{**member,'name':'Helper '+str(i)},key='thread-'+str(i)),range(4)))
    assert len(team.load(root)['members'])==6
    checkpoint=Milestones(root).capture('Roster',key='saved',projection=lambda:public_projection(root))
    branch=Path(Milestones(root).restore(checkpoint['commit'],key='branch')['project_dir'])
    team.add_member(branch,{**member,'name':'Branch only'},key='branch-member')
    assert len(team.load(branch)['members'])==7 and len(team.load(root)['members'])==6


def test_retried_creation_does_not_reset_operator_policy(workspace):
    import os
    settings,calls=workspace;made=create_project(request(),settings)
    policy_path=Path(os.environ['EULER_TEAM_CONFIG']);policies=json.loads(policy_path.read_text())
    policies['projects'][made['project']]['allow_launch']=False
    policies['projects'][made['project']]['max_turns']=5
    policy_path.write_text(json.dumps(policies));previous=len(calls)
    create_project(request(),settings)
    assert len(calls)==previous
    assert json.loads(policy_path.read_text())['projects'][made['project']]['max_turns']==5
