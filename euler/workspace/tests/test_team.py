"""Whole-team persistence, native identity preservation and research boundaries."""
import platform
import copy,json,os,subprocess,sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from euler.workspace import team
from euler.workspace.milestones import Milestones,enable,public_projection,MilestoneError


@pytest.fixture
def workspace(tmp_path):
    p=tmp_path/'source';p.mkdir();(p/'project.json').write_text(json.dumps({'name':'source','workers':['lead','helper']}))
    members=[{'id':'lead','role':'lead','provider':'codex','model':'saved-codex','effort':'high','assignment':'Coordinate the proof'},
             {'id':'helper','role':'assistant','provider':'grok','model':'grok-4.6','effort':'high','assignment':'Check the construction'}]
    team.initialize(p,members,objective='Prove the exact original claim',max_turns=6,turn_seconds=60)
    private=tmp_path/'provider-original';private.mkdir()
    a=private/'lead.jsonl';a.write_text('opaque-session-A\nprior tool/context/compaction state\n')
    b=private/'grok';b.mkdir();(b/'updates.jsonl').write_text('opaque-session-B\n');(b/'plan.json').write_text('{"saved":"pending proof check"}')
    with team.edit(p) as s:
        for m,path in zip(s['members'],[a,b]):m.update(session_id='original-'+m['id'],archive=str(path),status='READY',recovery='NEW_SESSION')
    enable(p)
    return p,private


def checkpoint(p):return Milestones(p).capture('Both members at stage one',key='one',projection=lambda:public_projection(p))


def test_snapshot_restores_every_member_and_opaque_context_without_later_state(workspace):
    p,private=workspace
    team.queue_question(p,'helper','Review the candidate',key='pending')
    (p/'proof.txt').write_text('earlier proof')
    m=checkpoint(p)
    assert m['team']['complete'] and len(m['team']['members'])==2
    (private/'lead.jsonl').write_text('later private context MUST NOT enter restored team')
    (p/'proof.txt').write_text('later proof')
    r=Milestones(p).restore(m['commit'],key='restore-team')
    root=Path(r['project_dir']);s=team.load(root)
    assert s['instance_id']!=team.load(p)['instance_id']
    assert s['state']=='RESTORED_PAUSED' and s['messages'][0]['state']=='SUSPENDED'
    assert s['lead']=='lead' and [m['effort'] for m in s['members']]==['high','high']
    assert (root/'proof.txt').read_text()=='earlier proof'
    assert Path(s['members'][0]['archive']).read_text().startswith('opaque-session-A')
    assert (Path(s['members'][1]['archive'])/'plan.json').read_text()=='{"saved":"pending proof check"}'
    assert all(m['session_id'] is None and m['restored_from'].startswith('original-') for m in s['members'])
    assert 'opaque-session' not in json.dumps(team.public_state(root))
    assert (private/'lead.jsonl').read_text().startswith('later private')
    assert team.load(p)['members'][0]['session_id']=='original-lead'


def test_native_preparation_uses_all_frozen_archives_and_does_not_prompt(workspace,monkeypatch):
    from euler.workspace import team_provider
    p,_=workspace;r=Milestones(p).restore(checkpoint(p)['commit'],key='fork')
    root=Path(r['project_dir']);calls=[]
    def native(root,m,s):
        assert Path(m['archive']).exists();calls.append((m['id'],m['restored_from']))
        return {'session_id':'fork-'+m['id'],'recovery':'NATIVE_FORK','archive':m['archive']}
    monkeypatch.setattr(team_provider,'prepare_member',native)
    state=team.prepare(root);assert state['complete_native_recovery'] and len(calls)==2
    assert team.prepare(root)['complete_native_recovery'] and len(calls)==2
    assert all(m['session_id']=='fork-'+m['id'] for m in state['members'])
    assert not state['messages'] and state['turns_used']==0


def test_missing_one_session_blocks_full_recovery_and_running_capture(workspace):
    p,private=workspace;(private/'lead.jsonl').unlink()
    m=checkpoint(p);assert not m['team']['complete']
    root=Path(Milestones(p).restore(m['commit'],key='incomplete')['project_dir'])
    with pytest.raises(team.TeamError,match='no summary fallback'):team.queue_question(root,'helper','hello',key='x')
    with pytest.raises(team.TeamError,match='not captured'):team.prepare(root)
    with team.edit(p) as s:s['members'][1]['status']='RUNNING'
    with pytest.raises(MilestoneError,match='still running'):Milestones(p).capture('busy',key='busy')


def test_mailbox_idempotency_scope_and_controls_do_not_create_facts(workspace):
    p,_=workspace
    first=team.queue_question(p,'lead','Question',key='same')
    assert team.queue_question(p,'lead','Question',key='same')==first
    with pytest.raises(team.TeamError,match='different'):team.queue_question(p,'lead','Changed',key='same')
    with pytest.raises(team.TeamError,match='unknown'):team.queue_question(p,'foreign','Question',key='other')
    with pytest.raises(team.TeamError,match='absent'):team.queue_question(p,'lead','Question',key='bad-object',object_id='made-up')
    team.command(p,'pause');assert team.load(p)['messages'][0]['state']=='SUSPENDED'
    team.command(p,'resume');assert team.load(p)['messages'][0]['state']=='QUEUED'
    team.command(p,'stop')
    with pytest.raises(team.TeamError,match='resumed'):team.queue_question(p,'lead','Question',key='stopped')
    assert not (p/'fact_graph/facts').exists()


def test_selected_object_is_pinned_and_included_in_the_native_question(workspace, monkeypatch):
    from contextlib import contextmanager
    from euler.observability import monitor
    from euler.workspace import team_provider
    p,_=workspace
    record={'id':'selected-step','kind':'obligation','statement':'For every N > 0, some prime exceeds N.',
            'scope':{'assumptions':['N is a positive integer']},'status':'OPEN',
            'private_memory':'must never enter selected-object context'}
    graph={'facts_graph':{'nodes':[],'edges':[]},'research_graph':{'nodes':[record],'edges':[]}}
    monkeypatch.setattr(monitor,'snapshot',lambda _,**kwargs:graph)
    job=team.queue_question(p,'helper','Check the assumption.',key='node-question',object_id=record['id'])
    prompt=team.turn_question(job)
    assert record['statement'] in prompt and record['id'] in prompt and 'Check the assumption.' in prompt
    assert 'private_memory' not in prompt and 'must never enter' not in prompt
    record['statement']='A later, different statement'
    assert team.queue_question(p,'helper','Check the assumption.',key='node-question',object_id=record['id'])==job
    assert 'A later, different statement' not in team.turn_question(team.load(p)['messages'][0])
    received=[]
    @contextmanager
    def connect(*args,**kwargs):yield None,None,None
    def run(root,member,state,question,cancelled):
        received.append((member['id'],question))
        assert not cancelled()
        return {'reply':'A public discussion reply','native_session_id':member['session_id']}
    monkeypatch.setattr(team_provider,'connection',connect)
    monkeypatch.setattr(team_provider,'require_quota',lambda _:90)
    monkeypatch.setattr(team_provider,'run_turn',run)
    team.command(p,'resume');team._drain(p)
    assert received==[('helper',prompt)]
    assert team.load(p)['messages'][0]['state']=='ANSWERED'
    assert not (p/'fact_graph/facts').exists()


def test_branch_history_and_identity_exclude_later_parent_messages(workspace, monkeypatch, tmp_path):
    from euler.workspace import team_provider
    from euler.observability.team_api import branch_info
    from euler.observability.catalog import Project
    p,_=workspace
    before=team.queue_question(p,'lead','Before the milestone',key='before')
    saved=checkpoint(p)
    parent_later=team.queue_question(p,'lead','Parent after the milestone',key='parent-later')
    root=Path(Milestones(p).restore(saved['commit'],key='history-fork')['project_dir'])
    monkeypatch.setattr(team_provider,'prepare_member',lambda root,m,s:{
        'session_id':'fork-'+m['id'],'recovery':'NATIVE_FORK','archive':m['archive']})
    team.prepare(root)
    child=team.queue_question(root,'helper','Only in the branch',key='child')
    state=team.public_state(root)
    assert [(m['id'],m['inherited']) for m in state['messages']]==[(before['id'],True),(child['id'],False)]
    assert parent_later['id'] not in {m['id'] for m in state['messages']}
    assert all(not m['inherited'] for m in team.public_state(p)['messages'])
    # Older checkpoints have the same boundary in their exact captured team file.
    with team.edit(root) as s:s['source'].pop('inherited_message_ids')
    assert team.public_state(root)['messages'][0]['inherited']
    catalog=tmp_path/'branches.json';catalog.write_text(json.dumps({'projects':[
        {'id':'original','name':'My proof','project_dir':str(p)},
        {'id':'fork','name':'My proof · branch','project_dir':str(root)}]}))
    config=tmp_path/'dashboard.json';config.write_text(json.dumps({'project_catalog':str(catalog)}))
    monkeypatch.setenv('EULER_DASHBOARD_CONFIG',str(config))
    monkeypatch.delenv('EULER_MILESTONE_REGISTRY',raising=False)
    info=branch_info(Project('fork','My proof · branch',root,None),team.public_state(root))
    assert info['workspace_id']=='fork' and info['project_id']=='original'
    assert info['parent_workspace_id']=='original' and info['lineage_complete']
    assert info['instance_id']!=info['parent_instance_id']
    assert str(p) not in json.dumps(info) and str(root) not in json.dumps(info)


def test_native_quota_guard_refuses_unknown_low_and_paid_usage():
    from euler.workspace.team_provider import require_quota
    class Rpc:
        def __init__(self,d):self.data=d
        def request(self,m,p=None):return {'account':{'type':'chatgpt'}} if m=='account/read' else self.data
    healthy={'ordinaryUsageAllowed':True,'rateLimits':{'primary':{'usedPercent':80},'secondary':{'usedPercent':70}}}
    assert require_quota(Rpc(healthy),3)==20
    for changed in [{},dict(healthy,ordinaryUsageAllowed=False),dict(healthy,rateLimits={'primary':{'usedPercent':97}})]:
        with pytest.raises(team.TeamError,match='QUOTA'):require_quota(Rpc(changed),3)


@pytest.mark.skipif(platform.system() != 'Linux', reason='Linux Landlock test')
def test_linux_isolation_keeps_original_files_unreadable_and_untouched(tmp_path):
    original=tmp_path/'original';branch=tmp_path/'branch';original.mkdir();branch.mkdir()
    (original/'proof').write_text('old source')
    code='''from euler.workspace.isolation import restrict_filesystem
from pathlib import Path
import sys
old,new=map(Path,sys.argv[1:]);restrict_filesystem([new],[old]);(new/'new-proof').write_text('new result')
for operation in [lambda:(old/'proof').read_text(),lambda:(old/'proof').write_text('wrong')]:
 try:operation();raise AssertionError('source not isolated')
 except PermissionError:pass
'''
    subprocess.run([sys.executable,'-c',code,str(original),str(branch)],check=True)
    assert (original/'proof').read_text()=='old source' and (branch/'new-proof').read_text()=='new result'


def test_http_team_identity_auth_and_dry_recovery(workspace,tmp_path,monkeypatch):
    from euler.observability.app import app
    from euler.workspace import team_provider
    p,_=workspace
    config=tmp_path/'catalog.json';config.write_text(json.dumps({'projects':[{'id':'actual','project_dir':str(p)}]}))
    dashboard=tmp_path/'dashboard.json';dashboard.write_text(json.dumps({'project_catalog':str(config)}))
    policy=tmp_path/'team-config.json';policy.write_text(json.dumps({'projects':{'actual':{'allow_prepare':True,'allow_launch':False,'max_turns':6,'turn_seconds':60}}}))
    monkeypatch.setenv('EULER_DASHBOARD_CONFIG',str(dashboard));monkeypatch.setenv('EULER_DASHBOARD_PROJECT',str(p))
    monkeypatch.delenv('EULER_TEAM_CONFIG',raising=False);monkeypatch.delenv('EULER_CONTROL_TOKEN',raising=False)
    monkeypatch.delenv('EULER_DASHBOARD_TOKEN',raising=False)
    client=TestClient(app)
    status=client.get('/api/team?project=actual');assert status.status_code==200
    assert 'opaque-session' not in status.text and str(p) not in status.text
    assert client.post('/api/team/prepare?project=actual').status_code==403
    assert not status.json()['can_prepare']
    monkeypatch.setenv('EULER_TEAM_CONFIG',str(policy))
    assert client.get('/api/team?project=actual').json()['can_prepare']
    headers={}
    assert client.post('/api/team/prepare?project=actual',headers={**headers,'Origin':'http://foreign'}).status_code==403
    assert client.post('/api/team/prepare?project=actual',headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.post('/api/team/prepare?project=actual',headers=headers).status_code==200
    body={'member':'lead','question':'Start','object_id':None,'idempotency_key':'ask'}
    assert client.post('/api/team/ask?project=actual',headers=headers,json=body).status_code==403
    assert not team.load(p)['messages']


def test_grok_research_waits_for_completion_without_a_wall_deadline(monkeypatch,tmp_path):
    from contextlib import contextmanager
    from euler.workspace import team_provider as provider
    seen=[]
    class Rpc:
        def __init__(self,callback):self.callback=callback
        def request(self,method,params=None,timeout=45):
            if method=='session/load':return {}
            if method=='session/set_config_option':return {'configOptions':[{'id':params['configId'],'currentValue':params['value']}]}
            if method=='session/prompt':
                seen.append(timeout)
                self.callback({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Review complete.'}}}})
                return {'stopReason':'end_turn'}
            raise AssertionError(method)
    @contextmanager
    def connect(root,member,state,callback=None):yield Rpc(callback),tmp_path,{}
    monkeypatch.setattr(provider,'connection',connect)
    # Even a negative old timing allowance must not cancel a Grok research turn.
    member={'id':'grok','provider':'grok','role':'assistant','model':'grok-4.6','effort':'high','session_id':'saved','workdir':'workers/grok','goal':{}}
    result=provider.run_turn(tmp_path,member,{'objective':'Review','policy':{'turn_seconds':-1}},'Review',lambda:False)
    assert result['reply']=='Review complete.' and seen==[None]


@pytest.mark.skipif(platform.system() != 'Linux', reason='Linux Landlock test')
def test_isolation_allows_tool_package_imports_above_a_hidden_source(tmp_path):
    package=tmp_path/'package';(package/'toolpkg').mkdir(parents=True)
    (package/'toolpkg/__init__.py').write_text('VALUE=23\n')
    original=package/'private_source';branch=package/'branch';original.mkdir();branch.mkdir()
    (original/'proof').write_text('private source')
    (branch/'old-link').symlink_to(original/'proof')
    code='''from euler.workspace.isolation import restrict_filesystem
from pathlib import Path
import sys
package,old,new=map(Path,sys.argv[1:]);restrict_filesystem([new],[old]);sys.path.insert(0,str(package))
import toolpkg
assert toolpkg.VALUE==23
try:(new/'old-link').read_text();raise AssertionError('symlink crossed the old workspace boundary')
except PermissionError:pass
'''
    subprocess.run([sys.executable,'-c',code,str(package),str(original),str(branch)],check=True)


def test_restored_isolation_covers_registered_attachment_files(workspace,tmp_path):
    from euler.workspace.team_provider import isolation_policy
    p,_=workspace;proofs=tmp_path/'proofs';proofs.mkdir();(proofs/'lemma').write_text('original')
    enable(p,attachments={'proofs':str(proofs)})
    root=Path(Milestones(p).restore(checkpoint(p)['commit'],key='attachments')['project_dir'])
    policy=isolation_policy(root,team.load(root))
    assert str(root.parent/'proofs') in policy['writable']
    assert str(proofs) in policy['hidden'] and str(p) in policy['hidden']
    with team.edit(root) as s:s['source']['roots']=[]
    assert isolation_policy(root,team.load(root))==policy


@pytest.mark.skipif(platform.system() != 'Linux', reason='Linux Landlock test')
def test_isolation_rejects_a_write_grant_covering_the_source(tmp_path):
    old=tmp_path/'old';old.mkdir()
    code='''from euler.workspace.isolation import restrict_filesystem,IsolationError
import sys
try:restrict_filesystem([sys.argv[1]],[sys.argv[2]]);raise AssertionError('ancestor grant accepted')
except IsolationError:pass
'''
    subprocess.run([sys.executable,'-c',code,str(tmp_path),str(old)],check=True)


def test_late_assistant_reply_stays_held_after_pause(workspace, monkeypatch):
    from euler.workspace import team_provider
    p, _ = workspace
    def turn(root, member, state, question, cancelled):
        team.command(root, 'pause')
        return {'reply':'A late discussion reply.', 'native_session_id':member['session_id']}
    monkeypatch.setattr(team_provider, 'run_turn', turn)
    team.queue_question(p, 'helper', 'Review this step', key='delegation', sender='lead', parent='fixture')
    team.command(p, 'resume')
    team._drain(p)
    state = team.load(p)
    assert state['state'] == 'PAUSED'
    assert state['messages'][-1]['request']['sender'] == 'team-delivery'
    assert state['messages'][-1]['state'] == 'SUSPENDED'
    assert not any(m['state'] == 'QUEUED' for m in state['messages'])
