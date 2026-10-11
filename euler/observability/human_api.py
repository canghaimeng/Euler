# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Single-user research controls bound to configured projects and execution limits.

The personal workbench needs no separate browser control credential. Project
policies, same-origin requests and the existing evidence gates still apply.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import HTTPException, Request

from euler.control.campaign import CampaignControl, campaign_snapshot
from euler.control.human_objects import resolve_object
from euler.control.intervention import HumanInterventions
from euler.control.policy import ResourceVector
from euler.control.project import is_euler_project, open_project


def _settings(identifier):
    filename = os.environ.get('EULER_HUMAN_CONFIG')
    if not filename:
        return None
    p = Path(filename)
    if p.stat().st_size > 128000:
        raise ValueError('human configuration too large')
    data = json.loads(p.read_text())
    return data.get('projects', {}).get(identifier)


def _authorize(request, config):
    if config is None:
        raise HTTPException(403, 'This project is read-only in this deployment')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Cross-origin control requests are not permitted')
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Cross-origin control requests are not permitted')
    return os.environ.get('EULER_CONTROL_ACTOR', 'human:operator')


def install_human_routes(app, read_project):
    def execute(identifier, fn, *, request=None, write=False):
        def selected(entry):
            config = _settings(entry.id)
            actor = _authorize(request, config) if write else os.environ.get('EULER_CONTROL_ACTOR', 'human:operator')
            if not is_euler_project(entry.path):
                raise HTTPException(409, 'Historical/read-only project has no native control identity')
            with open_project(entry.path) as project:
                return fn(project, config, actor)
        try:
            return read_project(identifier, selected, with_entry=True)
        except HTTPException:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(503, 'Control state temporarily unavailable') from exc

    def service(p, config, actor):
        if config is None:
            raise HTTPException(403, 'No focused execution policy configured')
        return HumanInterventions(p, actor=actor, worker=config['worker'],
            model=config['model'], effort=config['effort'],
            maximum=ResourceVector.from_dict(config['maximum']))

    @app.get('/api/human/capabilities')
    def capabilities(project: str | None = None):
        def selected(entry):
            config = _settings(entry.id)
            managed = is_euler_project(entry.path)
            return {'project': entry.id, 'native_identity': managed,
                    'enabled': bool(config and managed),
                    'allow_launch': bool(config and config.get('allow_launch')),
                    'maximum': config.get('maximum') if config else None,
                    'reason': '' if managed and config else 'Historical or unconfigured project: inspect original records only'}
        return read_project(project, selected, with_entry=True)

    @app.get('/api/human/object')
    def object_view(object_type: str, object_id: str, project: str | None = None):
        return execute(project, lambda p,c,a: resolve_object(p,object_type,object_id))

    @app.get('/api/human/state')
    def state(project: str | None = None):
        return execute(project, lambda p,c,a: campaign_snapshot(p))

    @app.post('/api/human/control')
    def control(body: dict, request: Request, project: str | None = None):
        if set(body) != {'command','mode','idempotency_key','revision','contract_id'}:
            raise HTTPException(422, 'Unexpected or missing control fields')
        return execute(project, lambda p,c,a: CampaignControl(p,actor=a).command(
            body['command'],mode=body['mode'],key=body['idempotency_key'],
            expected_revision=body['revision'],contract_id=body['contract_id']),request=request,write=True)

    @app.post('/api/human/preview')
    def preview(body: dict, request: Request, project: str | None = None):
        return execute(project,lambda p,c,a:service(p,c,a).preview(body),request=request,write=True)

    @app.post('/api/human/interventions')
    def submit(body: dict, request: Request, project: str | None = None):
        if set(body) != {'request','preview_hash','idempotency_key'}:
            raise HTTPException(422, 'Unexpected or missing intervention fields')
        return execute(project,lambda p,c,a:service(p,c,a).submit(body['request'],
            preview_hash=body['preview_hash'],key=body['idempotency_key']),request=request,write=True)

    @app.post('/api/human/interventions/{iid}/start')
    def start(iid: str, body: dict, request: Request, project: str | None = None):
        if set(body) != {'idempotency_key','launch'} or not isinstance(body['launch'],bool):
            raise HTTPException(422, 'Explicit start fields required')
        def action(p,c,a):
            if body['launch'] and not c.get('allow_launch',False):
                raise HTTPException(403, 'Provider launching is not enabled by the operator')
            # The local provider adapter and managed worker preflight still enforce
            # the deployment's quota/authorization checks before inference.
            return service(p,c,a).start(iid,key=body['idempotency_key'],launch=body['launch'])
        return execute(project,action,request=request,write=True)

    @app.get('/api/human/interventions')
    def investigations(project: str | None = None, object_id: str | None = None):
        return execute(project,lambda p,c,a:service(p,c,a).list(object_id=object_id))

    @app.get('/api/human/changes')
    def changes(project: str | None = None, since: int = 0):
        if since < 0:
            raise HTTPException(422, 'sequence must be nonnegative')
        def read(p,c,a):
            kinds = {'obligation.status_changed':'obligation_status', 'obligation.reopened':'reopened',
                     'evidence.invalidated':'evidence_invalidated', 'route.status_changed':'route_status',
                     'problem.contract.created':'contract_registered',
                     'euler.human_requested':'human_request', 'euler.human_outcome_reconciled':'investigation_outcome'}
            events = list(p.event_store.iter_project(p.project_id))
            items = [{'seq':seq,'event_id':e.event_id,'kind':kinds[e.event_type],
                      'timestamp':e.timestamp_utc,'payload':dict(e.payload)}
                     for seq,e in events if seq > since and e.event_type in kinds]
            return {'latest_sequence':max((seq for seq,_ in events),default=0),
                    'items':items[-50:], 'omitted':max(0,len(items)-50),
                    'note':'Recorded changes; requests, scope registration and execution settlement are not mathematical acceptance'}
        return execute(project,read)

    @app.get('/api/human/compare')
    def compare(left: str, right: str, project: str | None = None):
        return execute(project,lambda p,c,a:{'left':resolve_object(p,'route',left),
                                            'right':resolve_object(p,'route',right),
                                            'note':'Recorded route scopes and dependencies; no new equivalence or implication inferred'})

    @app.get('/api/human/interventions/{iid}')
    def investigation(iid: str, project: str | None = None):
        return execute(project,lambda p,c,a:service(p,c,a).get(iid))
