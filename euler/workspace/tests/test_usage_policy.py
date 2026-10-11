"""Release usage defaults and opt-in local guards; no provider inference."""
from contextlib import contextmanager
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from euler.workspace import team, team_provider


def registered(root, **options):
    root.mkdir(exist_ok=True)
    team.initialize(root, [
        {'id':'lead','provider':'codex','role':'lead','model':'test','effort':'high'},
        {'id':'helper','provider':'grok','role':'assistant','model':'test','effort':'high','max_turns':1},
    ], objective='Usage policy test', **options)
    with team.edit(root) as state:
        for member in state['members']:
            member.update(recovery='NEW_SESSION',status='READY',session_id=member['id'])
        state['state']='READY'
    return root


def test_release_defaults_do_not_check_account_quota_and_member_caps_are_independent(tmp_path,monkeypatch):
    root=registered(tmp_path/'project')
    state=team.load(root)
    assert state['policy']['max_turns'] is None
    assert team.quota_threshold(state) is None
    assert 'codex_stop_percent' not in state['policy']
    def forbidden(*a,**kw):raise AssertionError('default execution must not start an account quota observer')
    monkeypatch.setattr(team_provider,'connection',forbidden)
    monkeypatch.setattr(team_provider,'require_quota',forbidden)
    called=[]
    def turn(project,member,state,question,cancelled):
        assert not cancelled();called.append(member['id'])
        return {'reply':'Public reply','native_session_id':member['session_id']}
    monkeypatch.setattr(team_provider,'run_turn',turn)
    first=team.queue_question(root,'helper','One',key='one')
    team.queue_question(root,'helper','Two',key='two')
    team.queue_question(root,'lead','Lead continues',key='lead')
    team._drain(root)
    state=team.load(root)
    assert called==['helper','lead']
    assert [m['state'] for m in state['messages']]==['ANSWERED','SUSPENDED','ANSWERED']
    assert state['state']=='READY' and state['turns_used']==2
    assert team.queue_question(root,'helper','One',key='one')['id']==first['id']
    with pytest.raises(team.TeamError,match='member turn allowance exhausted'):
        team.queue_question(root,'helper','Three',key='three')
    assert team.queue_question(root,'lead','More',key='more')['state']=='QUEUED'


def test_existing_and_explicit_local_guards_keep_their_threshold(tmp_path,monkeypatch):
    assert team.quota_threshold({'policy':{'codex_stop_percent':3}})==3
    assert team.quota_threshold({'policy':{'quota_monitor':'codex_included','codex_stop_percent':3}})==3
    root=registered(tmp_path/'guarded',quota_policy={'codex_stop_percent':8})
    seen=[]
    @contextmanager
    def connect(*args,**kw):yield object(),None,None
    def quota(rpc,threshold):
        seen.append(threshold)
        raise team.TeamError('QUOTA: below the configured threshold')
    monkeypatch.setattr(team_provider,'connection',connect)
    monkeypatch.setattr(team_provider,'require_quota',quota)
    monkeypatch.setattr(team_provider,'run_turn',lambda *a:pytest.fail('guarded work must not start'))
    team.queue_question(root,'helper','Check',key='one');team._drain(root)
    assert seen==[8] and team.load(root)['state']=='QUOTA_PAUSED'


def test_codex_turn_has_no_hidden_default_account_gate(tmp_path,monkeypatch):
    calls=[]
    class Rpc:
        def __init__(self,callback):self.callback=callback
        def request(self,method,params=None,**kwargs):
            calls.append(method)
            assert not method.startswith('account/')
            if method=='turn/start':
                self.callback({'method':'item/completed','params':{'item':{'type':'agentMessage','text':'Done'}}})
                self.callback({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
                return {'turn':{'id':'turn'}}
            return {}
    @contextmanager
    def connect(root,member,state,callback=None):yield Rpc(callback),tmp_path,{}
    monkeypatch.setattr(team_provider,'connection',connect)
    member={'id':'lead','provider':'codex','model':'test','effort':'high','role':'lead','workdir':'workers/lead','session_id':'saved','goal':{}}
    state={'objective':'Test','policy':{'turn_seconds':30,'quota_monitor':'provider_managed'}}
    result=team_provider.run_turn(tmp_path,member,state,'Question',lambda:False)
    assert result['reply']=='Done' and result['remaining_percent'] is None
    assert calls==['thread/resume','turn/start','thread/goal/get']


def test_explicit_codex_api_credentials_only_reach_unguarded_codex(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','test-openai-key')
    monkeypatch.setenv('GEMINI_API_KEY','must-not-reach-codex')
    home=tmp_path/'runtime';home.mkdir()
    member={'id':'lead','provider':'codex'}
    env=team_provider.team_environment(tmp_path,member,home,{'policy':{'quota_monitor':'provider_managed'}})
    assert env['OPENAI_API_KEY']=='test-openai-key' and 'GEMINI_API_KEY' not in env
    guarded=team_provider.team_environment(tmp_path,member,home,{'policy':{'codex_stop_percent':3}})
    assert 'OPENAI_API_KEY' not in guarded


@pytest.mark.parametrize('declared,allowed,status',[(None,None,200),(12,None,200),(None,12,403),(13,12,403)])
def test_http_policy_accepts_optional_limits_without_bypassing_deployment_cap(tmp_path,monkeypatch,declared,allowed,status):
    from euler.observability.app import app
    from euler.observability import monitor
    root=registered(tmp_path/'project',max_turns=declared)
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps({'projects':[{'id':'test','project_dir':str(root)}]}))
    dashboard=tmp_path/'dashboard.json';dashboard.write_text(json.dumps({'project_catalog':str(catalog)}))
    policy=tmp_path/'policy.json';policy.write_text(json.dumps({'projects':{'test':{'allow_launch':True,'max_turns':allowed,'turn_seconds':180}}}))
    monkeypatch.setenv('EULER_DASHBOARD_PROJECT',str(root));monkeypatch.setenv('EULER_DASHBOARD_CONFIG',str(dashboard));monkeypatch.setenv('EULER_TEAM_CONFIG',str(policy))
    monkeypatch.delenv('EULER_MILESTONE_REGISTRY',raising=False);monkeypatch.delenv('EULER_DASHBOARD_TOKEN',raising=False)
    monkeypatch.setattr(team,'start_runner',lambda _:None);monitor._CACHE.clear()
    result=TestClient(app).post('/api/team/ask?project=test',json={'member':'lead','question':'Question','object_id':None,'idempotency_key':'one'})
    assert result.status_code==status,result.text
    assert len(team.load(root)['messages'])==(1 if status==200 else 0)
