# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Create a named research workspace from a user's first conversation message."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile

from . import team
from .milestones import _json, _lock, enable
from euler.control.contracts import MathematicalScope, ProblemContract, TargetKind, EvidenceKind
from euler.control.obligations import Obligation
from euler.control.policy import BudgetPolicy, DimensionLimit, RESOURCE_DIMENSIONS
from euler.control.project import initialize_project


def creation_settings():
    filename=os.environ.get('EULER_PROJECT_CREATION_CONFIG')
    if not filename:return None
    return json.loads(Path(filename).read_text())


def validate_request(body):
    if not isinstance(body,dict) or set(body)-{'name','question','lead','idempotency_key','target_kind'} or not {'name','question','lead','idempotency_key'}<=set(body):
        raise ValueError('Expected project name, first question, lead configuration and request identity')
    for key,limit in [('name',120),('question',16000),('idempotency_key',160)]:
        if not isinstance(body[key],str) or not body[key].strip() or len(body[key])>limit:raise ValueError(f'invalid {key}')
    lead=body['lead']
    if not isinstance(lead,dict) or set(lead)!={'provider','model','effort'}:raise ValueError('Expected lead provider, model and effort')
    lead=team.validate_member(dict(lead))
    target=TargetKind(body.get('target_kind','PROOF'))
    return {**body,'name':body['name'].strip(),'lead':lead,'target_kind':target.value}


def create_project(body, settings):
    """Persist and register first, then let the durable team runner start the chat."""
    body=validate_request(body)
    if not settings or not settings.get('allow_create'):raise ValueError('Project creation is not enabled')
    if body['lead']['provider'] not in settings.get('allowed_providers',sorted(team.PROVIDERS)):
        raise ValueError('This provider is not enabled for new projects')
    home=Path(settings['root']).expanduser().resolve();home.mkdir(parents=True,exist_ok=True)
    registry=Path(settings['registry']).resolve()
    identity=hashlib.sha256(body['idempotency_key'].encode()).hexdigest()[:24]
    identifier='research-'+identity
    fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    root=home/'projects'/identifier;record=home/'requests'/(identity+'.json')
    with _lock(home/'creation-lock'):
        if record.exists():
            previous=json.loads(record.read_text())
            if previous['fingerprint']!=fingerprint:raise ValueError('creation request identity has different content')
        else:_json(record,{'fingerprint':fingerprint,'request':body,'project':identifier})
        if root.exists():
            if root.is_symlink():raise ValueError('project destination must not be a symlink')
            marker=root/'CREATION.json'
            if not marker.is_file() or json.loads(marker.read_text()).get('fingerprint')!=fingerprint:
                raise ValueError('project destination is already occupied')
        else:
            root.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.creating-',dir=home) as tmp:
                stage=Path(tmp)/'project';stage.mkdir()
                _json(stage/'project.json',{'name':identifier,'title':body['name'],'workers':[],
                    'scope_status':'pending_clarification'})
                (stage/'PROBLEM.md').write_text('# '+body['name']+'\n\n'+body['question']+'\n\n'
                    '## Initial scope\n\nThis is the researcher’s initial request. Definitions, assumptions and quantifiers '
                    'must be clarified in the conversation; none are inferred by the project launcher.\n')
                scope=MathematicalScope.build(assumptions=[],quantifiers=[],objects=[],
                    parameter_domain='Not specified in the initial request; clarify with the researcher.',
                    definitions_hash=hashlib.sha256(('Initial user request: '+body['question']).encode()).hexdigest())
                contract=ProblemContract.build(problem_id=identifier,version=1,exact_statement=body['question'],scope=scope,
                    external_help_policy='Researcher-directed investigation; preserve provenance and clarify scope.',
                    target_kind=TargetKind(body['target_kind']),required_evidence_level={
                        'COUNTEREXAMPLE':EvidenceKind.CHECKED_COUNTEREXAMPLE,
                        'FORMALIZATION':EvidenceKind.KERNEL_CHECKED_PROOF}.get(body['target_kind'],EvidenceKind.LLM_CHECKED_PROOF),
                    completion_checks=['Clarify the exact mathematical target and scope with the researcher before final acceptance.',
                                       'Independent evidence and final target audit are required.'],budget_policy_ref='researcher-configured')
                obligation=Obligation.build(contract_id=contract.contract_id,exact_statement=contract.exact_statement,
                    scope=scope,desired_output='Clarify and investigate the researcher’s mathematical question.',created_by='human',version=1)
                budget=BudgetPolicy(**{k:DimensionLimit.unlimited() for k in RESOURCE_DIMENSIONS})
                with initialize_project(stage,contract,obligation,budget,'human:project-create',team.now()):pass
                enable(stage,store=home/'history'/identifier)
                lead={**body['lead'],'id':'lead','name':body['lead']['provider'],'role':'lead',
                      'assignment':'Clarify the researcher’s question, coordinate the investigation, and add configured teammates when needed.'}
                team.initialize(stage,[lead],objective=body['question'],turn_seconds=settings.get('turn_seconds',300),
                                quota_policy=settings.get('quota_policy'))
                _json(stage/'CREATION.json',{'fingerprint':fingerprint,'created_at':team.now(),'actor':'human:operator',
                    'target_obligation_id':obligation.obligation_id})
                os.replace(stage,root)
        milestone=enable(root,store=home/'history'/identifier)
        dashboard=home/'dashboards'/(identifier+'.json')
        _json(dashboard,{'name':body['name'],'description':'Research started from a conversation'})
        # Shares the registry lock with milestone restoration, avoiding lost
        # catalog or policy entries when users create and restore concurrently.
        with _lock(registry.parent/'registry-lock'):
            data=json.loads(registry.read_text()) if registry.exists() else {'projects':[]}
            if not any(p['id']==identifier for p in data['projects']):
                data['projects'].append({'id':identifier,'name':body['name'],'project_dir':str(root),
                    'config':str(dashboard),'description':'Research started from a conversation'})
                _json(registry,data)
            for env,policy in [('EULER_TEAM_CONFIG',{'allow_prepare':True,'allow_launch':True,'allow_add_members':True,
                                                   'max_turns':None,'turn_seconds':settings.get('turn_seconds',300)}),
                              ('EULER_MILESTONE_CONFIG',{'settings':milestone,'capture':True})]:
                path=Path(os.environ[env]);policies=json.loads(path.read_text()) if path.exists() else {'projects':{}}
                policies.setdefault('projects',{}).setdefault(identifier,policy);_json(path,policies)
                if env=='EULER_TEAM_CONFIG':launch_allowed=bool(policies['projects'][identifier].get('allow_launch'))
        target=json.loads((root/'CREATION.json').read_text()).get('target_obligation_id')
        message=team.queue_question(root,'lead',body['question'],key='project-first-question:'+identity,object_id=target)
    if message['state']=='QUEUED' and launch_allowed:team.start_runner(root)
    return {'project':identifier,'name':body['name'],'url':'/?project='+identifier+'#team',
            'message_id':message['id'],'state':message['state']}
