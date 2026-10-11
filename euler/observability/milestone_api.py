# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Milestone browsing and isolated restoration; no private file download API."""
import json
import os
from pathlib import Path
import subprocess

from fastapi import HTTPException, Request

from euler.workspace.milestones import Milestones, MilestoneError, _json, _lock, configured, public_projection
from .human_api import _authorize


def _policy(entry):
    filename = os.environ.get('EULER_MILESTONE_CONFIG')
    data = json.loads(Path(filename).read_text()) if filename else {}
    return data.get('projects', {}).get(entry.id)


def install_milestone_routes(app, read_project):
    def execute(identifier, fn, request=None):
        def selected(entry):
            policy = _policy(entry)
            settings = policy.get('settings') if policy else configured(entry.path)
            if request:
                _authorize(request, policy)
            return fn(entry, settings, policy)
        try:
            return read_project(identifier, selected, with_entry=True)
        except (MilestoneError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            raise HTTPException(503, 'Milestone operation unavailable; inspect local service logs') from exc

    @app.get('/api/milestones')
    def milestones(project: str | None = None):
        def read(entry, settings, policy):
            origin = entry.path / 'RESTORE_ORIGIN.json'
            restored = json.loads(origin.read_text()) if origin.is_file() and not origin.is_symlink() else None
            if restored:
                restored = {k:restored.get(k) for k in ['source_commit','source_label','created_at','state','worker_started','provider_session_resumed','request','selected_object_kind','team']}
            return {'enabled': bool(settings), 'can_capture': bool(policy and policy.get('capture')),
                    'can_restore': bool(policy),
                    'restored_from': restored,
                    'items': Milestones(entry.path, settings=settings).list() if settings else []}
        return execute(project, read)

    @app.post('/api/milestones')
    def capture(body: dict, request: Request, project: str | None = None):
        if set(body) != {'label','idempotency_key'}:
            raise HTTPException(422, 'Expected label and idempotency_key')
        def action(entry, settings, policy):
            if not policy.get('capture'):
                raise HTTPException(403, 'Capturing this project is not authorized')
            return Milestones(entry.path, settings=settings).capture(body['label'], key=body['idempotency_key'],
                projection=lambda: public_projection(entry.path,entry.config))
        return execute(project, action, request)

    @app.post('/api/milestones/{commit}/restore')
    def restore(commit: str, body: dict, request: Request, project: str | None = None):
        if set(body) != {'idempotency_key','question','object_id','layer'}:
            raise HTTPException(422, 'Expected exact restore and question fields')
        def action(entry, settings, policy):
            registry = os.environ.get('EULER_MILESTONE_REGISTRY')
            if not registry:
                raise HTTPException(409, 'Operator has not configured restored workspace registry')
            result = Milestones(entry.path,settings=settings).restore(commit,key=body['idempotency_key'],
                question=body['question'], object_id=body['object_id'],layer=body['layer'])
            root = Path(result['project_dir'])
            identifier = 'restored-' + root.parents[1].name
            base = root.parents[1]
            view = base / 'view/snapshot.json'
            historical = view.is_file() and json.loads(view.read_text()).get('research_graph',{}).get('projection') == 'historical_source'
            config = {'name':entry.name+' · '+commit[:8], 'description':'Restored Git milestone · '+result['source_label']}
            if historical:
                config['frozen_snapshot'] = str(view)
            _json(base/'dashboard.json',config)
            continuation = configured(root)
            continuation['dashboard'] = str(base/'dashboard.json')
            _json(root/'.euler-milestones.json',continuation)
            registry = Path(registry)
            with _lock(registry.parent/'registry-lock'):
                data = json.loads(registry.read_text()) if registry.exists() else {'projects':[]}
                if not any(p['id']==identifier for p in data['projects']):
                    data['projects'].append({'id':identifier,'name':entry.name+' · '+commit[:8],
                        'project_dir':str(root),'config':str(base/'dashboard.json'),
                        'description':'Restored milestone; original campaign untouched'})
                    _json(registry,data)
                team_config = os.environ.get('EULER_TEAM_CONFIG')
                if team_config:
                    policies = json.loads(Path(team_config).read_text())
                    if entry.id in policies.get('projects', {}):
                        policies['projects'][identifier] = policies['projects'][entry.id]
                        _json(Path(team_config), policies)
                # Only previously authorized native execution policies carry over.
                from .human_api import _settings
                human = _settings(entry.id)
                if human:
                    filename = Path(os.environ['EULER_HUMAN_CONFIG'])
                    policies = json.loads(filename.read_text())
                    policies['projects'][identifier] = human
                    _json(filename,policies)
                filename = os.environ.get('EULER_MILESTONE_CONFIG')
                if filename:
                    policies = json.loads(Path(filename).read_text())
                    policies['projects'][identifier] = {'settings':continuation,'capture':True}
                    _json(Path(filename),policies)
            return {'project':identifier,'url':'/?project='+identifier+'#team',
                    **{k:result[k] for k in ['source_commit','state','file_count','excluded_count','external_links','worker_started','provider_session_resumed']}}
        return execute(project, action, request)
