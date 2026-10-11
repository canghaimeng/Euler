# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Browser project creation: server-owned roots and existing personal controls."""
from fastapi import HTTPException, Request
from euler.workspace.project_creation import creation_settings, create_project
from euler.workspace.team_acp import BINARIES, binary_for
from .human_api import _authorize


def install_project_routes(app):
    @app.get('/api/projects/create-capabilities')
    def capabilities():
        settings=creation_settings() or {};providers=[]
        for name,binary in BINARIES.items():
            try:binary_for(name);available=True
            except ValueError:available=False
            providers.append({'id':name,'command':binary,'available':available,
                              'enabled':name in settings.get('allowed_providers',list(BINARIES)),
                              **settings.get('defaults',{}).get(name,{})})
        return {'enabled':bool(settings.get('allow_create')),'providers':providers}

    @app.post('/api/projects')
    def create(body: dict,request: Request):
        settings=creation_settings()
        _authorize(request,settings)
        if not settings.get('allow_create'):raise HTTPException(403,'Project creation is not enabled')
        try:return create_project(body,settings)
        except (ValueError,KeyError,TypeError) as exc:raise HTTPException(409,str(exc)) from exc
        except OSError as exc:raise HTTPException(503,'Project creation unavailable; retry with the same request identity') from exc
