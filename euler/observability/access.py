# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright 2026 Qiuyu Chen (canghaimeng).
"""Optional access control for the read-only project dashboard."""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def install_access_control(app: FastAPI) -> None:
    """Protect data APIs when an operator configures a dashboard access token.

    The static application remains available so it can present its login form.
    No credentials are embedded in HTML or accepted in logged URL parameters.
    """

    @app.middleware("http")
    async def dashboard_access(request: Request, call_next):
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        token = os.environ.get("EULER_DASHBOARD_TOKEN", "")
        authorization = request.headers.get("authorization", "")
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if is_api and token and not hmac.compare_digest(supplied.encode(), token.encode()):
            response = JSONResponse(
                {"detail": "Dashboard access token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            response = await call_next(request)
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response
