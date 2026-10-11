# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Three elementary human-intervention protocol demos with mock models.

Fresh, isolated projects; never edits historical campaigns. All mathematical
answers and verifier decisions are deterministic fixtures, not new results.
The scheduler, context compiler, subprocess worker, HTTP verifier gateway,
Fact admission, leases and reconciliation use the actual implementation.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

from euler.demo import (_FAKE_VERIFIER, _FAKE_WORKER, _free_loopback_port,
                        _wait_http, _unlimited_policy, child_pythonpath, package_root, _kill)
from euler.control.contracts import MathematicalScope, ProblemContract, TargetKind, EvidenceKind
from euler.control.obligations import Obligation
from euler.control.project import initialize_project, open_project
from euler.control.campaign import CampaignControl, campaign_snapshot, now
from euler.control.human_objects import resolve_object, digest
from euler.control.policy import ResourceVector
from euler.control.intervention import HumanInterventions
from euler.execution import scaffold, layout as L

CASES = {'generalize': {'name': 'Domain extension · MOCK',
                'intent': 'EXPLORE_ASSUMPTION',
                'statement': 'For every natural number n, n + 0 = n.',
                'assumptions': ['n is a natural number'],
                'question': 'Does the identity also hold for integers?',
                'derived': 'For every integer n, n + 0 = n.',
                'domain': 'Integers',
                'source': 'Built-in elementary protocol fixture; no historical research data'},
 'audit': {'name': 'Identity audit · MOCK',
           'intent': 'VERIFY_OR_AUDIT',
           'statement': 'For every integer n, n + 0 = n.',
           'assumptions': ['n is an integer'],
           'question': 'Audit the identity at the stated domain.',
           'derived': 'Audit the integer additive identity.',
           'domain': 'Integers',
           'source': 'Built-in elementary protocol fixture; no historical research data'},
 'alternative': {'name': 'Alternative argument · MOCK',
                 'intent': 'EXPLORE_ALTERNATIVE',
                 'statement': 'For every integer n, n + 0 = n.',
                 'assumptions': ['n is an integer'],
                 'question': 'Explore an alternative presentation of the additive identity.',
                 'derived': 'Develop another argument for the integer additive identity.',
                 'domain': 'Integers',
                 'source': 'Built-in elementary protocol fixture; no historical research data'}}


def prepare(root: Path, *, reuse: bool = False):
    if reuse and not (root/'requests.json').is_file():
        raise ValueError('reuse requires an existing demo workspace')
    root.mkdir(parents=True,exist_ok=reuse)
    for d in ['skills','runtime/run','verify-results','verifier-home/.agents/skills','traces']:(root/d).mkdir(parents=True,exist_ok=True)
    (root/'worker.md').write_text('# Isolated deterministic mock worker; no new mathematical result.\n')
    (root/'verifier-home/AGENTS.md').write_text('# Deterministic mock verifier for protocol testing only.\n')
    shebang=f'#!{sys.executable}\n'
    verifier=root/'fake-verifier.py';verifier.write_text(shebang+_FAKE_VERIFIER);verifier.chmod(0o755)
    # Read the actual scoped child target produced by the human API, not a
    # hardcoded theorem. Deliberately mocked proof/check; visibly labeled.
    worker_source=_FAKE_WORKER.replace('statement = os.environ.get("EULER_DEMO_STATEMENT") or ""', '''from euler.control.project import open_project
    from euler.control.dispatch import load_dispatch
    project_dir = Path.cwd().parent.parent
    with open_project(project_dir) as project:
        binding = load_dispatch(project_dir, Path.cwd().name)['binding']
        statement = project.research_view().get_obligation(binding['obligation_id']).exact_statement''').replace('proof = os.environ.get("EULER_DEMO_PROOF") or ""', 'proof = "MOCK protocol fixture only: this is not a mathematical proof or a new case-study result."')
    worker=root/'fake-worker.py';worker.write_text(shebang+worker_source);worker.chmod(0o755)
    port=_free_loopback_port()
    os.environ.pop('EULER_WORKER_LOOP_LAUNCHER',None)
    os.environ.update({'PYTHONPATH':child_pythonpath(),'EULER_ROOT':str(package_root()),
                       'EULER_RUNTIME':str(root/'runtime'),'EULER_AGENTS_ROOT':str(root/'projects'),
                       'EULER_WORKER_CONTRACT':str(root/'worker.md'),'EULER_WORKER_SKILLS':str(root/'skills'),
                       'EULER_CODEX_BIN':str(worker),'EULER_VERIFY_URL':f'http://127.0.0.1:{port}/verify',
                       'EULER_MAX_ROUNDS':'1'})
    env=os.environ.copy();env.update({'EULER_CODEX_BIN':str(verifier),'EULER_VERIFY_BACKEND':'codex',
        'VERIFIER_RESULTS_DIR':str(root/'verify-results'),'VERIFY_AGENT_HOME':str(root/'verifier-home'),
        'EULER_DEMO_VERIFY_LOG':str(root/'verifier-invocations.jsonl'),'EULER_VERIFY_TIMEOUT':'30'})
    log=(root/'verify-service.log').open('w')
    proc=subprocess.Popen([sys.executable,'-m','uvicorn','euler.verify.service:app','--host','127.0.0.1','--port',str(port),'--log-level','warning'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    (root/'runtime/run/verify.pid').write_text(str(proc.pid))
    _wait_http(f'http://127.0.0.1:{port}/health',15)
    cap=ResourceVector(cost_microusd=10000,input_tokens=10000,output_tokens=10000,reasoning_tokens=10000,cached_tokens=0,wall_ms=60000,tool_cpu_ms=60000,tool_gpu_ms=0)
    catalog=[];config={'projects':{}};requests={}
    for id,case in CASES.items():
        if not (root/'projects'/id/'project.json').is_file():
            scaffold.do_new(id,roles='codex:1',profile='codex',model='gpt-5.5')
        path=L.project_dir(id)
        scope=MathematicalScope.build(assumptions=case['assumptions'],quantifiers=(),objects=(),
            parameter_domain='Natural numbers' if id=='generalize' else case['domain'],definitions_hash=digest({'source':case['source']}))
        contract=ProblemContract.build(problem_id=id,version=1,exact_statement=case['statement'],scope=scope,
            external_help_policy='Human-led integration demonstration; MOCK models, not discovery evidence',
            target_kind=TargetKind.PROOF,required_evidence_level=EvidenceKind.LLM_CHECKED_PROOF,
            completion_checks=('Independent original-target audit required',),budget_policy_ref='demo-bounded')
        obligation=Obligation.build(contract_id=contract.contract_id,exact_statement=contract.exact_statement,
            scope=scope,desired_output='Original mathematical target (not completed by this demo)',created_by='demo-fixture',version=1)
        with initialize_project(path,contract,obligation,_unlimited_policy(),'demo-fixture',now()) as p:
            target=resolve_object(p,'obligation',obligation.obligation_id)['target']
            desired_scope=scope.to_dict()
            if id=='generalize':
                desired_scope={**desired_scope,'parameter_domain':case['domain'],'assumptions':['n is an integer']}
            requests[id]={'target':target,'intent':case['intent'],'user_question':case['question'],
                          'requested_statement':case['derived'],'requested_scope':desired_scope,'budget':cap.to_dict()}
        view=root/f'{id}-viewer.json';view.write_text(json.dumps({'name':case['name'],'description':'Contemporary protocol demonstration; deterministic MOCK worker/verifier. Historical result is not replayed or re-proved.','demo_mode':True}))
        catalog.append({'id':id,'name':case['name'],'project_dir':str(path),'config':str(view),'description':'MOCK protocol demonstration; human request to scoped task and checked fixture'})
        config['projects'][id]={'worker':'codex','model':'gpt-5.5','effort':'high','maximum':cap.to_dict(),'allow_launch':True}
    (root/'catalog.json').write_text(json.dumps({'projects':catalog},indent=2))
    (root/'dashboard.json').write_text(json.dumps({'project_catalog':str(root/'catalog.json')}))
    (root/'human-config.json').write_text(json.dumps(config,indent=2))
    (root/'requests.json').write_text(json.dumps(requests,indent=2))
    os.environ.update({'EULER_DASHBOARD_PROJECT':str(root/'projects/generalize'),
                       'EULER_DASHBOARD_CONFIG':str(root/'dashboard.json'),
                       'EULER_HUMAN_CONFIG':str(root/'human-config.json'),
                       'EULER_CONTROL_ACTOR':'human:demo-researcher'})
    return proc,log,requests,cap


def run_traces(root,requests,cap):
    # The exact same HTTP handlers used by the browser, then real subprocesses.
    from fastapi.testclient import TestClient
    from .app import app
    import logging
    logging.getLogger('httpx').setLevel(logging.WARNING)
    client=TestClient(app);headers={}
    report={'mock_models':True,'mock_verifier':True,'real_components':['HTTP human API','EventStore','scheduler','context compiler','native worker subprocess','verifier HTTP','worker Fact gateway','lease receipts','Reconcile'],'cases':{}}
    for id,request in requests.items():
        prefix='?project='+id
        before=client.get('/api/human/state'+prefix).json()
        pause=client.post('/api/human/control'+prefix,headers=headers,json={'command':'pause','mode':'drain','idempotency_key':'demo-pause','revision':before['revision'],'contract_id':before['contract_id']});pause.raise_for_status()
        preview=client.post('/api/human/preview'+prefix,headers=headers,json=request);preview.raise_for_status()
        submitted=client.post('/api/human/interventions'+prefix,headers=headers,json={'request':request,'preview_hash':preview.json()['preview_hash'],'idempotency_key':'demo-request'});submitted.raise_for_status();iid=submitted.json()['intervention_id']
        start=client.post('/api/human/interventions/'+iid+'/start'+prefix,headers=headers,json={'idempotency_key':'demo-start','launch':True});start.raise_for_status()
        deadline=time.monotonic()+80
        while time.monotonic()<deadline:
            result=client.get('/api/human/interventions/'+iid+prefix).json()
            if result['state'] in {'SETTLED','FAILED','CANCELLED'}:break
            time.sleep(.25)
        assert result['state']=='SETTLED',result['state']
        assert result['mathematical_status']=='DISCHARGED',result['mathematical_status']
        assert result['campaign']['state']=='PAUSED'
        with open_project(root/'projects'/id) as p:
            assert p.research_view().get_obligation(p.manifest.target_obligation_id).status.value=='OPEN'
            events=[{'seq':seq,**e.to_dict()} for seq,e in p.event_store.iter_project(p.project_id)]
        (root/'traces'/f'{id}-events.json').write_text(json.dumps(events,indent=2))
        (root/'traces'/f'{id}-outcome.json').write_text(json.dumps(result,indent=2))
        report['cases'][id]={'intervention_id':iid,'before':before['state'],'after':result['campaign']['state'],'child':result['mathematical_status'],'parent':'OPEN','events':len(events),'mock_acceptance_only':True}
        print(id,report['cases'][id],flush=True)
    (root/'traces/validation.json').write_text(json.dumps(report,indent=2))
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);parser.add_argument('--run-traces',action='store_true');parser.add_argument('--serve',action='store_true');parser.add_argument('--reuse',action='store_true');parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8105);args=parser.parse_args()
    if args.reuse and (not args.output or args.run_traces):
        parser.error('--reuse requires --output and cannot be combined with --run-traces; use a fresh workspace for measured traces')
    root=(args.output or Path(tempfile.gettempdir())/('euler-human-demo-'+secrets.token_hex(6))).resolve()
    proc=log=None
    try:
        proc,log,requests,cap=prepare(root,reuse=args.reuse)
        print(json.dumps({'directory':str(root),'url':f'http://{args.host}:{args.port}/','mode':'MOCK protocol demonstration'}),flush=True)
        if args.run_traces:run_traces(root,requests,cap)
        if args.serve:
            import uvicorn
            uvicorn.run('euler.observability.app:app',host=args.host,port=args.port,log_level='warning')
    finally:
        _kill(proc)
        if log:log.close()

if __name__=='__main__':main()
