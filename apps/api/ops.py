"""Ops dashboard API: a READ-ONLY window onto a running system's state directory.

A different process from the runner, serving what `services/supervisor/ops_state.py` reads. Same rule as the
status API (apps/api/main.py): there is no route that writes anything — no POST/PUT/DELETE/PATCH, no control of
the runner, the kill switch, the LIVE gate, intents or incidents (those stay CLI-only with a named operator).

Optional shared-secret token (QUANT_OPS_TOKEN): when set, every route needs `Authorization: Bearer <token>` or
`?token=<token>` (compared in constant time). The dashboard binds to loopback by default; to expose it beyond
the host, set a token AND put it behind TLS."""
from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services.supervisor.ops_state import OpsState

OPS_HTML = Path(__file__).resolve().parents[1] / "web" / "ops.html"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def create_ops_app(ops: OpsState, token: Optional[str] = None,
                   now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> FastAPI:
    app = FastAPI(title="Quant Trading Platform — Ops Dashboard API", version="0.1.0",
                  docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(request: Request) -> None:
        if not token:
            return
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(authorize)])
    def page() -> str:
        return OPS_HTML.read_text()

    @app.get("/api/status", dependencies=[Depends(authorize)])
    def status() -> dict[str, Any]:
        return ops.status(now())

    @app.get("/api/performance", dependencies=[Depends(authorize)])
    def performance() -> dict[str, Any]:
        return ops.performance()

    @app.get("/api/sessions", dependencies=[Depends(authorize)])
    def sessions(limit: int = Query(60, ge=1, le=500)) -> dict[str, Any]:
        return ops.sessions(limit)

    @app.get("/api/sessions/{session_date}", dependencies=[Depends(authorize)])
    def session(session_date: str) -> dict[str, Any]:
        if not _DATE.match(session_date):
            raise HTTPException(status_code=400, detail="session date must be YYYY-MM-DD")
        detail = ops.session_detail(session_date)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such session")
        return detail

    @app.get("/api/holdings", dependencies=[Depends(authorize)])
    def holdings() -> dict[str, Any]:
        return ops.holdings()

    @app.get("/api/observers", dependencies=[Depends(authorize)])
    def observers() -> dict[str, Any]:
        return ops.observers()

    @app.get("/api/incidents", dependencies=[Depends(authorize)])
    def incidents() -> dict[str, Any]:
        return ops.incidents()

    @app.get("/api/intents", dependencies=[Depends(authorize)])
    def intents() -> dict[str, Any]:
        return ops.intents()

    @app.get("/api/alerts", dependencies=[Depends(authorize)])
    def alerts(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return ops.alerts(limit)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
