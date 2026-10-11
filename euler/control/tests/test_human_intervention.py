"""Canonical identity, scoped branches, lifecycle and immutable evidence boundary."""
import copy
import threading
from pathlib import Path

import pytest

from euler.control.tests.test_controller_dispatch import _managed, _candidate
from euler.control.campaign import CampaignControl, CampaignError, campaign_snapshot
from euler.control.human_objects import resolve_object, ObjectConflict
from euler.control.intervention import HumanInterventions, InterventionError
from euler.control.controller import EulerController, EulerControllerError
from euler.control.policy import ResourceVector
from euler.control.project import open_project
from euler.control.research_service import ResearchService


@pytest.fixture
def setup(tmp_path):
    p,c,o=_managed(tmp_path)
    cap=ResourceVector(cost_microusd=100,input_tokens=2000,output_tokens=2000,reasoning_tokens=2000,
                       cached_tokens=0,wall_ms=60000,tool_cpu_ms=60000,tool_gpu_ms=0)
    h=HumanInterventions(p,actor='human:researcher',worker='w1',model='test-model',effort='high',maximum=cap)
    request={'target':resolve_object(p,'obligation',o.obligation_id)['target'],
             'intent':'VERIFY_OR_AUDIT','user_question':'Audit the exact hypothesis boundary.',
             'requested_statement':'An independent scope audit of the original claim is available.',
             'requested_scope':o.scope.to_dict(),'budget':cap.to_dict()}
    yield p,c,o,h,request
    p.close()


def command(p, action, key='control', mode='drain'):
    return CampaignControl(p,actor='human:researcher').command(action,mode=mode,key=key,
        expected_revision=campaign_snapshot(p)['revision'],contract_id=p.manifest.contract_id)


def test_preview_is_readonly_and_idempotent_admission_survives_restart(setup):
    p,c,o,h,r=setup
    before=list(p.event_store.iter_project(p.project_id))
    preview=h.preview(r)
    assert before==list(p.event_store.iter_project(p.project_id))
    result=h.submit(r,preview_hash=preview['preview_hash'],key='request')
    assert h.submit(r,preview_hash=preview['preview_hash'],key='request')['intervention_id']==result['intervention_id']
    assert result['state']=='AWAITING_START'
    assert p.research_view().get_obligation(o.obligation_id).status.value=='OPEN'
    assert not (p.project_dir/'fact_graph/facts').exists()
    with open_project(p.project_dir) as again:
        assert again.research_view().get_obligation(result['obligation_id'])
        assert again.research_view().get_contract(result['child_contract_id']).scope==o.scope
        assert again.event_store.verify_integrity()>3
    bad=copy.deepcopy(r);bad['user_question']='I declare PASS'
    with pytest.raises(InterventionError,match='idempotency'):
        h.submit(bad,preview_hash=preview['preview_hash'],key='request')


def test_stale_object_and_presentation_alias_rejected(setup):
    p,c,o,h,r=setup
    r['target']['statement_hash']='0'*64
    with pytest.raises(ObjectConflict):h.preview(r)
    with pytest.raises(ObjectConflict):resolve_object(p,'obligation','history:nikoghosyan:proof:result')
    with pytest.raises(ObjectConflict):resolve_object(p,'obligation',o.exact_statement)


def test_scope_change_requires_explicit_child_contract(setup):
    p,c,o,h,r=setup
    r['requested_scope']['parameter_domain']='all real numbers'
    with pytest.raises(InterventionError,match='scope'):h.preview(r)
    r['intent']='EXPLORE_ASSUMPTION';r['requested_statement']='Does the result extend to real numbers?'
    v=h.preview(r);branch=h.submit(r,preview_hash=v['preview_hash'],key='generalize')
    assert p.research_view().get_contract(c.contract_id).scope.parameter_domain=='Z'
    assert p.research_view().get_contract(branch['child_contract_id']).scope.parameter_domain=='all real numbers'


def test_paused_parent_only_allows_explicit_focused_dispatch(setup):
    p,c,o,h,r=setup
    ctl=EulerController(p);ctl.propose(_candidate(c,o,'ordinary'),'w2');ctl.schedule()
    assert command(p,'pause')['state']=='PAUSED'
    with pytest.raises(EulerControllerError,match='paused'):ctl.dispatch('ordinary')
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='focused')
    assert not ctl.schedule().selected_candidate_ids
    started=h.start(branch['intervention_id'],key='start')
    assert started['state']=='READY'
    assert campaign_snapshot(p)['state']=='PAUSED'
    assert 'ordinary' not in ctl.current_plan().selected_candidate_ids
    packet=p.artifacts.get_bytes(started['timeline'][-1]['payload']['context_hash']).decode()
    assert o.exact_statement in packet and r['user_question'] in packet
    assert branch['child_contract_id'] in packet
    assert command(p,'stop',key='stop')['state']=='STOPPED'
    assert h.get(branch['intervention_id'])['state']=='CANCELLED'


def test_pause_replay_and_resume_do_not_resurrect_issued_lease(setup):
    p,c,o,h,r=setup
    ctl=EulerController(p);ctl.propose(_candidate(c,o,'ordinary'),'w2');ctl.schedule();b=ctl.dispatch('ordinary')
    rev=campaign_snapshot(p)['revision'];control=CampaignControl(p,actor='human:researcher')
    args=dict(mode='drain',key='pause',expected_revision=rev,contract_id=p.manifest.contract_id)
    assert control.command('pause',**args)['state']=='PAUSED'
    assert control.command('pause',**args)['state']=='PAUSED'
    assert command(p,'resume',key='resume')['state']=='RUNNING'
    assert next(l for l in p.runtime_snapshot().leases if l.lease_id==b.lease_id).status.value=='CANCELLED'
    assert 'ordinary' in ctl.schedule().selected_candidate_ids
    retry=ctl.dispatch('ordinary')
    assert retry.lease_id!=b.lease_id
    assert len(p.runtime_snapshot().leases)==2


def test_running_cancellation_is_pending_until_real_settlement(setup):
    p,c,o,h,r=setup
    ctl=EulerController(p);ctl.propose(_candidate(c,o,'ordinary'),'w2');ctl.schedule();b=ctl.dispatch('ordinary')
    p.runtime.mark_running(p.project_id,b.lease_id)
    result=command(p,'stop',mode='cancel')
    assert result['state']=='STOP_REQUESTED' and not result['remote_stop_confirmed']
    assert p.runtime_snapshot().leases[0].status.value=='RUNNING'
    with pytest.raises(CampaignError,match='settle'):command(p,'resume',key='resume')
    p.runtime.cancel(p.project_id,b.lease_id)
    assert campaign_snapshot(p)['state']=='STOPPED'


def test_budget_and_scope_fields_cannot_be_smuggled(setup):
    p,c,o,h,r=setup
    r['budget']['wall_ms']+=1
    with pytest.raises(InterventionError,match='allowance'):h.preview(r)
    r['budget']['wall_ms']-=1;r['verified']=True
    with pytest.raises(InterventionError,match='fields'):h.preview(r)


def test_pause_and_dispatch_race_has_consistent_boundary(setup):
    p,c,o,h,r=setup
    ctl=EulerController(p);ctl.propose(_candidate(c,o,'race'),'w2');ctl.schedule()
    barrier=threading.Barrier(2);results=[]
    def run(which):
        with open_project(p.project_dir) as q:
            barrier.wait()
            try:results.append(command(q,'pause') if which else EulerController(q).dispatch('race'))
            except EulerControllerError:results.append('blocked')
    a=threading.Thread(target=run,args=(0,));b=threading.Thread(target=run,args=(1,));a.start();b.start();a.join();b.join()
    assert len(results)==2
    assert campaign_snapshot(p)['state']=='PAUSED'
    assert all(l.status.value=='CANCELLED' for l in p.runtime_snapshot().leases)


def test_http_control_is_opt_in_scoped_and_has_no_truth_write_surface(setup, monkeypatch, tmp_path):
    import json
    from fastapi.testclient import TestClient
    from euler.observability.app import app
    p,c,o,h,r=setup
    monkeypatch.setenv('EULER_DASHBOARD_PROJECT',str(p.project_dir))
    monkeypatch.delenv('EULER_DASHBOARD_CONFIG',raising=False)
    monkeypatch.delenv('EULER_DASHBOARD_TOKEN',raising=False)
    monkeypatch.delenv('EULER_HUMAN_CONFIG',raising=False)
    client=TestClient(app)
    assert not client.get('/api/human/capabilities').json()['enabled']
    assert client.post('/api/human/preview',json=r).status_code==403
    config=tmp_path/'human.json'
    config.write_text(json.dumps({'projects':{'demo':{'worker':'w1','model':'test-model','effort':'high',
                                           'maximum':h.maximum.to_dict(),'allow_launch':False}}}))
    monkeypatch.setenv('EULER_HUMAN_CONFIG',str(config));monkeypatch.delenv('EULER_CONTROL_TOKEN',raising=False)
    assert client.get('/api/human/capabilities').json()['enabled']
    headers={}
    assert client.post('/api/human/preview',json=r,headers={**headers,'Origin':'https://unrelated.example'}).status_code==403
    assert client.post('/api/human/preview',json=r,headers={'Sec-Fetch-Site':'cross-site'}).status_code==403
    assert client.post('/api/human/preview?project=../../secrets',json=r,headers=headers).status_code==404
    preview=client.post('/api/human/preview',json=r,headers=headers)
    assert preview.status_code==200,preview.text
    response=client.post('/api/human/interventions',json={'request':r,'preview_hash':preview.json()['preview_hash'],
                                                        'idempotency_key':'http-request'},headers=headers)
    assert response.status_code==200,response.text
    iid=response.json()['intervention_id']
    assert client.post(f'/api/human/interventions/{iid}/start',json={'idempotency_key':'start','launch':True},headers=headers).status_code==403
    assert client.post('/api/human/fact_submit',json={'verdict':'PASS'},headers=headers).status_code==404
    assert p.research_view().get_obligation(o.obligation_id).status.value=='OPEN'


def test_focused_result_reconciles_only_child_and_late_drain_result_is_valid(setup):
    from euler.control.tests.test_reconcile import _receipts, _write_fact, _write_admission
    from euler.control.hooks import before_managed_round, after_managed_round
    from euler.execution.layout import WorkerLayout
    p,c,o,h,r=setup
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='checked')
    h.start(branch['intervention_id'],key='start')
    wl=WorkerLayout(p.project_dir/'workers/w1')
    start,launch,result,end=_receipts(wl)
    before_managed_round(wl,start,launch)
    command(p,'pause')
    # Test-only fixtures emulate the existing verifier's admitted output.
    _write_fact(p.project_dir,'a'*16,r['requested_statement'])
    _write_admission(p.project_dir,'a'*16,r['requested_statement'])
    rec=after_managed_round(wl,start,launch,result,end)
    assert rec.outcome_ref=='OBLIGATION_DISCHARGED'
    assert after_managed_round(wl,start,launch,result,end)==rec
    done=h.get(branch['intervention_id'])
    assert done['state']=='SETTLED' and done['mathematical_status']=='DISCHARGED'
    assert len([e for e in done['timeline'] if e['type']=='euler.human_outcome_reconciled'])==1
    assert p.research_view().get_obligation(o.obligation_id).status.value=='OPEN'
    assert campaign_snapshot(p)['state']=='PAUSED'
    assert p.research_view().get_evidence(done['evidence_refs'][0]).contract_id==branch['child_contract_id']


def test_cancelled_result_is_operational_not_mathematical_failure(setup):
    from euler.control.tests.test_reconcile import _receipts
    from euler.control.hooks import before_managed_round, after_managed_round
    from euler.execution.layout import WorkerLayout
    p,c,o,h,r=setup
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='cancel')
    h.start(branch['intervention_id'],key='start')
    wl=WorkerLayout(p.project_dir/'workers/w1')
    start,launch,result,end=_receipts(wl,rc=130)
    before_managed_round(wl,start,launch)
    assert command(p,'stop')['state']=='STOP_REQUESTED'
    after_managed_round(wl,start,launch,result,end)
    done=h.get(branch['intervention_id'])
    assert done['state']=='CANCELLED' and done['mathematical_status']=='OPEN'
    assert not done['evidence_refs']
    assert campaign_snapshot(p)['state']=='STOPPED'


def test_human_credentials_are_not_inherited_by_workers():
    from euler.providers.base import isolate_provider_env,ProviderName
    env=isolate_provider_env(ProviderName.CODEX,{'EULER_CONTROL_TOKEN':'secret','PATH':'/bin'},drop_consult=True)
    assert 'EULER_CONTROL_TOKEN' not in env


def test_real_local_worker_cancellation_observed_without_remote_claim(setup, tmp_path):
    import subprocess,sys
    from euler.execution.human_control import wait_with_control
    p,c,o,h,r=setup
    ctl=EulerController(p);ctl.propose(_candidate(c,o,'cancel-process'),'w2');ctl.schedule();b=ctl.dispatch('cancel-process')
    p.runtime.mark_running(p.project_id,b.lease_id)
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
    try:
        assert command(p,'stop')['state']=='STOP_REQUESTED'
        with (tmp_path/'cancel.log').open('w') as log:
            assert wait_with_control(child,p.project_dir,'w2',b.lease_id,20,log)==130
        assert child.poll() is not None
        # Physical termination must still be followed by authenticated receipt settlement.
        assert campaign_snapshot(p)['state']=='STOP_REQUESTED'
        assert not campaign_snapshot(p)['remote_stop_confirmed']
    finally:
        if child.poll() is None:child.kill();child.wait()


def test_observed_scope_and_revoked_fact_binding_invalidate_request(setup):
    from euler.control.tests.test_reconcile import _write_fact,_write_admission
    from euler.control.research_service import ResearchService,_digest
    p,c,o,h,r=setup
    _write_fact(p.project_dir,'b'*16,o.exact_statement);_write_admission(p.project_dir,'b'*16,o.exact_statement)
    service=ResearchService(p,actor='reviewer')
    service.admit_fact('b'*16,o.obligation_id,confirmed_scope_hash=_digest(o.scope.to_dict()),alignment_note='Explicit test alignment')
    obj=resolve_object(p,'fact','b'*16);assert obj['writable'];assert obj['evidence'][0]['checker_result']=='PASS'
    source=p.project_dir/'fact_graph/facts'/('b'*16+'.md');source.unlink()
    with pytest.raises(ObjectConflict,match='revoked'):resolve_object(p,'fact','b'*16)


def test_unknown_scope_is_reported_and_same_label_does_not_resolve(setup):
    p,c,o,h,r=setup
    obj=resolve_object(p,'obligation',o.obligation_id)
    assert 'objects' in obj['unknown_scope_fields']
    assumption=resolve_object(p,'assumption',o.obligation_id+':0')
    assert assumption['statement']==o.scope.assumptions[0]
    with pytest.raises(ObjectConflict):resolve_object(p,'assumption',o.obligation_id+':-1')


def test_reaudit_existing_fact_needs_fresh_attempt_bound_verification(setup):
    import json
    from euler.control.tests.test_reconcile import _receipts,_write_fact,_write_admission
    from euler.control.hooks import before_managed_round,after_managed_round
    from euler.execution.layout import WorkerLayout
    p,c,o,h,r=setup
    _write_fact(p.project_dir,'c'*16,r['requested_statement']);_write_admission(p.project_dir,'c'*16,r['requested_statement'])
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='reaudit')
    ready=h.start(branch['intervention_id'],key='start')
    wl=WorkerLayout(p.project_dir/'workers/w1');start,launch,result,end=_receipts(wl)
    before_managed_round(wl,start,launch)
    path=p.project_dir/'global_memory/verification.jsonl'
    with path.open('a') as f:
        f.write(json.dumps({'id':'fresh-audit','author':'w1','kind':'verification','provenance':'fact_submit',
            'transport_status':'ok','verdict':'correct','fact_id':'c'*16,'claim':r['requested_statement'],
            'attempt_id':ready['leases'][0]['attempt_id'],'write_error':None})+'\n')
    rec=after_managed_round(wl,start,launch,result,end)
    assert rec.outcome_ref=='OBLIGATION_DISCHARGED'
    assert p.research_view().get_obligation(o.obligation_id).status.value=='OPEN'


def test_reinitialize_after_child_contracts_preserves_original_project(setup):
    from euler.control.project import initialize_project
    from euler.control.campaign import now
    p,c,o,h,r=setup
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='restart-with-child')
    before=len(list(p.event_store.iter_project(p.project_id)))
    with initialize_project(p.project_dir,c,o,p.policy,'operator',now()) as reopened:
        assert reopened.manifest.contract_id==c.contract_id
        assert reopened.research_view().get_contract(branch['child_contract_id'])
        assert len(list(reopened.event_store.iter_project(reopened.project_id)))==before


def test_challenge_cannot_accept_a_proof_of_the_original_positive_claim(setup):
    p,c,o,h,r=setup
    r['intent']='CHALLENGE_OR_COUNTEREXAMPLE';r['requested_statement']=o.exact_statement
    with pytest.raises(InterventionError,match='refutation target'):h.preview(r)
    r['requested_statement']='There exists an even integer greater than 2 that is not a sum of two primes.'
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='challenge')
    child=p.research_view().get_contract(branch['child_contract_id'])
    assert child.target_kind.value=='COUNTEREXAMPLE'
    assert child.required_evidence_level.value=='CHECKED_COUNTEREXAMPLE'
    assert p.research_view().get_obligation(o.obligation_id).status.value=='OPEN'


def test_changed_execution_policy_requires_new_authorization(setup):
    p,c,o,h,r=setup
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='policy-pinned')
    h.model='different-model'
    with pytest.raises(InterventionError,match='policy'):h.start(branch['intervention_id'],key='start')
    assert not p.runtime_snapshot().leases


def test_identical_named_projects_cannot_cross_bind_object_requests(setup, tmp_path):
    p,c,o,h,r=setup
    other,_,other_root=_managed(tmp_path/'separate-instance')
    try:
        obj=resolve_object(other,'obligation',other_root.obligation_id)
        assert obj['target']['project_id']==r['target']['project_id']
        assert obj['target']['object_id']==r['target']['object_id']
        assert obj['target']['project_instance_hash']!=r['target']['project_instance_hash']
        from euler.control.human_objects import assert_same_target
        with pytest.raises(ObjectConflict):assert_same_target(obj,r['target'])
    finally:other.close()


def test_prerequisite_scope_and_revocation_are_in_source_context(setup):
    from euler.control.obligations import Obligation,Route
    from euler.control.tests.test_reconcile import _write_fact,_write_admission
    from euler.control.research_service import ResearchService,_digest
    p,c,o,h,r=setup
    premise=Obligation.build(contract_id=c.contract_id,exact_statement='A required local premise',scope=c.scope,
                             desired_output='proof',created_by='researcher',version=1)
    route=Route.build(target_obligation_id=o.obligation_id,mechanism='Use the prerequisite',required_obligation_ids=[premise.obligation_id])
    service=ResearchService(p,actor='researcher');service.apply([
        {'event_type':'obligation.created','payload':{'obligation':premise.to_dict()}},
        {'event_type':'route.created','payload':{'route':route.to_dict()}}])
    _write_fact(p.project_dir,'d'*16,premise.exact_statement);_write_admission(p.project_dir,'d'*16,premise.exact_statement)
    service.admit_fact('d'*16,premise.obligation_id,confirmed_scope_hash=_digest(c.scope.to_dict()),alignment_note='Reviewed exact prerequisite')
    before=resolve_object(p,'obligation',o.obligation_id)
    assert before['premises'][0]['exact_statement']==premise.exact_statement
    assert before['source_fact_ids']==['d'*16]
    (p.project_dir/'fact_graph/facts'/('d'*16+'.md')).unlink()
    assert not resolve_object(p,'obligation',o.obligation_id)['writable']


def test_coordinator_cannot_extend_a_focused_budget_with_an_unapproved_candidate(setup):
    p,c,o,h,r=setup
    branch=h.submit(r,preview_hash=h.preview(r)['preview_hash'],key='sealed-budget')
    child=p.research_view().get_contract(branch['child_contract_id'])
    obligation=p.research_view().get_obligation(branch['obligation_id'])
    ctl=EulerController(p);ctl.propose(_candidate(child,obligation,'unapproved-copy'),'w2')
    assert campaign_snapshot(p)['state']=='RUNNING'
    assert 'unapproved-copy' not in ctl.schedule().selected_candidate_ids
    with pytest.raises(EulerControllerError,match='explicitly authorized'):ctl.dispatch('unapproved-copy')
    assert not p.runtime_snapshot().leases
