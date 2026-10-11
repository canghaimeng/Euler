# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Personal persistent-team recovery and member conversation endpoints."""
import json,os
from pathlib import Path
from fastapi import HTTPException,Request
from .human_api import _authorize
from euler.workspace import team
from .catalog import catalog


def policy(identifier):
    f=os.environ.get('EULER_TEAM_CONFIG')
    return json.loads(Path(f).read_text()).get('projects',{}).get(identifier) if f else None


def branch_info(entry, state):
    """Describe the selected catalog workspace and its exact native parent identity."""
    source = state.get('source') or {}
    origin_path = entry.path / 'RESTORE_ORIGIN.json'
    origin = json.loads(origin_path.read_text()) if origin_path.is_file() and not origin_path.is_symlink() else {}
    parent = source.get('instance_id')
    by_instance = {}
    ambiguous = set()
    for candidate in catalog(entry.path)[0]:
        try:
            saved = team.load(candidate.path)
            identifier = saved.get('instance_id') if isinstance(saved, dict) else None
            if not identifier:
                continue
            if identifier in by_instance and by_instance[identifier][0].path != candidate.path:
                ambiguous.add(identifier)
            else:
                by_instance.setdefault(identifier, (candidate, saved.get('source') or {}))
        except (OSError, ValueError, TypeError):
            continue
    for identifier in ambiguous:
        by_instance.pop(identifier, None)
    ancestor = parent
    root = entry
    visited = set()
    while ancestor in by_instance and ancestor not in visited:
        visited.add(ancestor)
        root, parent_source = by_instance[ancestor]
        ancestor = parent_source.get('instance_id')
    return {'workspace_id':entry.id, 'workspace_name':entry.name,
            'instance_id':state.get('instance_id'),
            'kind':'restored' if source or origin else 'original',
            'project_id':root.id, 'project_name':root.name,
            'lineage_complete':not ancestor and (not origin or bool(parent)),
            'parent_workspace_id':by_instance[parent][0].id if parent in by_instance else None,
            'parent_instance_id':parent, 'source_commit':source.get('commit') or origin.get('source_commit'),
            'source_label':origin.get('source_label'), 'restored_at':origin.get('created_at')}


def install_team_routes(app,read_project):
    def execute(identifier,fn,request=None):
        def selected(entry):
            config=policy(entry.id)
            if request:_authorize(request,config)
            return fn(entry,config)
        try:return read_project(identifier,selected,with_entry=True)
        except (ValueError,KeyError,TypeError) as exc:raise HTTPException(409,str(exc)) from exc
        except OSError as exc:raise HTTPException(503,'Team state temporarily unavailable') from exc

    @app.get('/api/team')
    def status(project: str | None = None):
        def read(e,c):
            state = team.public_state(e.path)
            return {**state, 'branch':branch_info(e,state),
                    'can_prepare':bool(c and c.get('allow_prepare')),
                    'can_add_members':bool(c and c.get('allow_add_members',c.get('allow_launch')) and state.get('available')),
                    'can_launch':bool(c and c.get('allow_launch'))}
        return execute(project,read)

    @app.post('/api/team/prepare')
    def prepare(request: Request,project: str | None = None):
        def act(e,c):
            if not c.get('allow_prepare'):raise HTTPException(403,'Native team recovery is disabled')
            return team.prepare(e.path)
        return execute(project,act,request)

    @app.post('/api/team/members')
    def member(body: dict,request: Request,project: str | None = None):
        if set(body)!={'member','idempotency_key'}:raise HTTPException(422,'Expected member configuration and request identity')
        def act(e,c):
            if not c.get('allow_add_members',c.get('allow_launch')):raise HTTPException(403,'Adding members is not enabled')
            result=team.add_member(e.path,body['member'],key=body['idempotency_key'],actor='human:operator')
            from euler.workspace.milestones import checkpoint_if_enabled
            checkpoint_if_enabled(e.path)
            return result
        return execute(project,act,request)

    @app.post('/api/team/ask')
    def ask(body: dict,request: Request,project: str | None = None):
        if set(body)!={'member','question','object_id','idempotency_key'}:raise HTTPException(422,'Expected exact member, question, object and request identity')
        def act(e,c):
            if not c.get('allow_launch'):raise HTTPException(403,'Team inference is not enabled')
            s=team.load(e.path)
            limit=c.get('max_turns');declared=s['policy'].get('max_turns')
            if (limit is not None and (declared is None or declared>limit)) or s['policy']['turn_seconds']>c['turn_seconds']:
                raise HTTPException(403,'Team allowance exceeds the configured execution policy')
            result=team.queue_question(e.path,body['member'],body['question'],key=body['idempotency_key'],object_id=body['object_id'])
            if result['state']=='QUEUED':team.start_runner(e.path)
            return result
        return execute(project,act,request)

    @app.post('/api/team/control')
    def control(body: dict,request: Request,project: str | None = None):
        if set(body)!={'command'}:raise HTTPException(422,'Expected a team command')
        def act(e,c):
            if body['command']=='resume' and not c.get('allow_launch'):raise HTTPException(403,'Team inference is disabled')
            result=team.command(e.path,body['command'])
            if body['command']=='resume':
                team.start_runner(e.path)
            return result
        return execute(project,act,request)
