"""Real Git, whole-team file recovery and exact historical graph retention."""
import copy
import json
import os
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from euler.workspace.milestones import Milestones, MilestoneError, enable, auto_capture, public_projection
from euler.control.tests.test_controller_dispatch import _managed, _candidate
from euler.control.controller import EulerController
from euler.control.project import open_project
from euler.control.campaign import campaign_snapshot


@pytest.fixture
def team(tmp_path):
    p,c,o = _managed(tmp_path)
    root=p.project_dir
    for name,text in [('workers/w1/TASK.md','First assignment'),('workers/w2/TASK.md','Second assignment'),
        ('workers/w1/local_memory/opaque.dat','private fixture; never present in HTTP'),
        ('untracked/proof.lean','theorem identity : True := True.intro'),('.gitignore','untracked/\n'),
        ('.env','EXAMPLE_SECRET=not-for-snapshot')]:
        dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text)
    deps=tmp_path/'dependencies';deps.mkdir();(deps/'contract.md').write_text('Worker contract')
    (root/'workers/w1/AGENTS.md').symlink_to(deps/'contract.md')
    enable(root,attachments={'contracts':str(deps)})
    yield p,c,o,Milestones(root)
    p.close()


def capture(h,key='first'):
    return h.capture('Full research state',key=key,projection=lambda:public_projection(h.project))


def test_all_team_files_untracked_notes_database_and_links_round_trip(team):
    p,c,o,h=team
    result=capture(h); assert len(result['commit'])==40
    before=list(p.event_store.iter_project(p.project_id))
    (p.project_dir/'workers/w1/TASK.md').write_text('Later assignment')
    (p.project_dir/'untracked/proof.lean').unlink()
    restored=h.restore(result['commit'],key='question-1',question='Why does the prerequisite hold?',object_id=o.obligation_id)
    root=Path(restored['project_dir'])
    assert (root/'workers/w1/TASK.md').read_text()=='First assignment'
    assert (root/'workers/w2/TASK.md').read_text()=='Second assignment'
    assert (root/'untracked/proof.lean').exists()
    assert (root/'workers/w1/local_memory/opaque.dat').read_text().startswith('private fixture')
    assert (root/'workers/w1/AGENTS.md').read_text()=='Worker contract'
    assert str(root.parent.parent) in str((root/'workers/w1/AGENTS.md').resolve())
    assert not (root/'.env').exists()
    assert 'Why does' in (root/'FOLLOWUP.md').read_text()
    assert before==list(p.event_store.iter_project(p.project_id))
    with open_project(root) as new:
        assert new.research_view().get_obligation(o.obligation_id)==p.research_view().get_obligation(o.obligation_id)
        assert campaign_snapshot(new)['state']=='PAUSED'
        assert not campaign_snapshot(new)['active_leases']
    assert not restored['worker_started'] and not restored['provider_session_resumed']
    continuation = Milestones(root)
    assert continuation.store != h.store
    assert continuation.settings['roots']['project'] == str(root)
    followup = capture(continuation, 'new-branch')
    assert followup['commit'] != result['commit']
    assert len(h.list()) == 1


def test_snapshots_keep_deleted_files_and_idempotency_is_exact(team):
    p,c,o,h=team
    one=capture(h)
    assert capture(h)==one
    with pytest.raises(MilestoneError,match='different content'):
        h.capture('Different title',key='first')
    (p.project_dir/'untracked/proof.lean').unlink()
    two=capture(h,'second')
    assert two['commit']!=one['commit']
    a=h.restore(one['commit'],key='a'); b=h.restore(two['commit'],key='b')
    assert (Path(a['project_dir'])/'untracked/proof.lean').exists()
    assert not (Path(b['project_dir'])/'untracked/proof.lean').exists()
    assert h.restore(one['commit'],key='a')==a
    with pytest.raises(MilestoneError,match='different content'):
        h.restore(two['commit'],key='a')
    for ref in ['HEAD','../HEAD',one['commit']+'^{tree}']:
        with pytest.raises(MilestoneError):h.restore(ref,key='bad')


def test_git_export_attributes_cannot_drop_proof_files(team):
    p,c,o,h=team
    (p.project_dir/'.gitattributes').write_text('untracked/** export-ignore\n*.lean text eol=crlf\n')
    one=capture(h); restored=h.restore(one['commit'],key='git-attributes')
    assert (Path(restored['project_dir'])/'untracked/proof.lean').read_bytes()==(p.project_dir/'untracked/proof.lean').read_bytes()


def test_frozen_proof_reader_does_not_query_current_sources(monkeypatch):
    from euler.observability import historical_projection as hp
    def forbidden(*args):
        raise AssertionError('old milestone read a live source')
    monkeypatch.setattr(hp,'get_source',forbidden)
    data={'research_graph':{'case_id':'example','nodes':[{'id':'old','statement':'Original claim'}],'edges':[]},
          'historical_documents':{'old':{'sources':['s'],'summary':'Original argument','title':'Original'}},
          'historical_sources':{'s':{'integrity':'matched','text':'The original saved proof.','format':'md','label':'Proof','locator':'saved.md'}}}
    result=hp.derivation(data,'old')
    assert 'The original saved proof.' in result['proof']


def test_running_lease_live_process_and_capture_race_are_rejected(team):
    p,c,o,h=team
    ctl=EulerController(p);ctl.propose(_candidate(c,o,'running'),'w2');ctl.schedule();b=ctl.dispatch('running')
    with pytest.raises(MilestoneError,match='active leases'):capture(h)
    p.runtime.cancel(p.project_id,b.lease_id,actor='test')
    pid=p.project_dir/'workers/w1/.pid';pid.write_text(str(os.getpid()))
    with pytest.raises(MilestoneError,match='process'):capture(h)
    pid.unlink()
    def raced():
        (p.project_dir/'workers/w2/TASK.md').write_text('Concurrent change')
        return public_projection(p.project_dir)
    with pytest.raises(MilestoneError,match='changed during capture'):
        h.capture('Raced',key='race',projection=raced)
    assert h.list()==[]


def test_wal_database_backup_and_unknown_object_fail_closed(team):
    p,c,o,h=team
    db=sqlite3.connect(p.project_dir/'team.sqlite3');db.execute('PRAGMA journal_mode=WAL')
    db.execute('create table notes (text)');db.execute('insert into notes values (?)',('retained WAL state',));db.commit()
    one=capture(h)
    restored=h.restore(one['commit'],key='wal')
    with sqlite3.connect(Path(restored['project_dir'])/'team.sqlite3') as read:
        assert read.execute('select text from notes').fetchone()[0]=='retained WAL state'
    db.close()
    with pytest.raises(MilestoneError,match='does not exist'):
        h.restore(one['commit'],key='bad-object',object_id='same-name-but-wrong-ID')


def test_automatic_milestone_changes_only_when_research_changes(team):
    p,c,o,h=team
    first=auto_capture(p.project_dir)
    assert first and auto_capture(p.project_dir) is None
    (p.project_dir/'workers/w1/TASK.md').write_text('Operational update')
    assert auto_capture(p.project_dir) is None


def test_restored_workspace_supports_new_native_question_without_resetting_state(team):
    from euler.control.human_objects import resolve_object
    from euler.control.intervention import HumanInterventions
    from euler.control.policy import ResourceVector
    p,c,o,h=team
    one=capture(h); root=Path(h.restore(one['commit'],key='native-question')['project_dir'])
    with open_project(root) as restored:
        cap=ResourceVector(cost_microusd=100,input_tokens=2000,output_tokens=2000,reasoning_tokens=2000,
                           cached_tokens=0,wall_ms=60000,tool_cpu_ms=60000,tool_gpu_ms=0)
        service=HumanInterventions(restored,actor='human:researcher',worker='w1',model='test-model',effort='high',maximum=cap)
        target=resolve_object(restored,'obligation',o.obligation_id)['target']
        assert target != resolve_object(p,'obligation',o.obligation_id)['target']
        request={'target':target,'intent':'VERIFY_OR_AUDIT','user_question':'Audit this restored result.',
                 'requested_statement':'A scope audit is available.','requested_scope':o.scope.to_dict(),'budget':cap.to_dict()}
        focused=service.submit(request,preview_hash=service.preview(request)['preview_hash'],key='ask')
        started=service.start(focused['intervention_id'],key='start')
        assert started['state']=='READY'
        assert (root/'untracked/proof.lean').exists()
        assert restored.research_view().get_obligation(o.obligation_id)
        assert campaign_snapshot(restored)['state']=='PAUSED'
    assert not any(e.event_type=='euler.focused_created' for _,e in p.event_store.iter_project(p.project_id))


def test_http_restoration_keeps_full_frozen_graph_and_never_exposes_private_files(team,tmp_path,monkeypatch):
    from euler.observability.app import app
    p,c,o,h=team
    view=public_projection(p.project_dir)
    view['research_graph']['projection']='historical_source'
    # Three-node archive fixture; count/edge equality matters, not visual padding.
    view['research_graph']['nodes'] += [dict(view['research_graph']['nodes'][0],id='old-one'),dict(view['research_graph']['nodes'][0],id='old-two')]
    view['research_graph']['edges'] += [{'source':'old-one','target':'old-two','type':'requires'}]
    one=h.capture('Historical archive imported today',key='archive',projection=lambda:view,kind='archive_import')
    registry=tmp_path/'projects.json';registry.write_text(json.dumps({'projects':[{'id':'source','project_dir':str(p.project_dir)}]}))
    config=tmp_path/'dashboard.json';config.write_text(json.dumps({'project_catalog':str(registry)}))
    policy=tmp_path/'milestones.json';policy.write_text(json.dumps({'projects':{'source':{'settings':h.settings,'capture':True}}}))
    monkeypatch.setenv('EULER_DASHBOARD_CONFIG',str(config));monkeypatch.setenv('EULER_DASHBOARD_PROJECT',str(p.project_dir))
    monkeypatch.setenv('EULER_MILESTONE_CONFIG',str(policy));monkeypatch.setenv('EULER_MILESTONE_REGISTRY',str(tmp_path/'restored.json'))
    monkeypatch.delenv('EULER_CONTROL_TOKEN',raising=False);monkeypatch.delenv('EULER_DASHBOARD_TOKEN',raising=False)
    client=TestClient(app);body={'idempotency_key':'browser','question':'Inspect this step','object_id':'old-one','layer':'research'}
    route='/api/milestones/'+one['commit']+'/restore?project=source'
    monkeypatch.delenv('EULER_MILESTONE_CONFIG',raising=False)
    assert client.post(route,json=body).status_code==403
    monkeypatch.setenv('EULER_MILESTONE_CONFIG',str(policy))
    assert client.get('/api/milestones?project=source').json()['can_restore']
    headers={}
    assert client.post(route,json=body,headers={**headers,'Origin':'http://evil.test'}).status_code==403
    assert client.post(route,json=body,headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    result=client.post(route,json=body,headers=headers);assert result.status_code==200,result.text
    new=result.json()['project']
    assert client.get('/api/graph?layer=research&project='+new).json()==view['research_graph']
    for path in ['/api/milestones?project=source','/api/milestones?project='+new,'/api/projects']:
        response=client.get(path);assert response.status_code==200,response.text
        assert 'private fixture' not in response.text and str(h.store) not in response.text
    assert client.post(route,json=body,headers=headers).json()==result.json()
