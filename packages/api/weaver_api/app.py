"""Control plane — ingest, queries, and the dashboard.

Local-first by design (ADR-009): binds to 127.0.0.1, no accounts, no tenancy
UI. The dashboard is a single HTML file served from this app, so there is no
build step and nothing to install beyond the Python package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from weaver_core.engine import Engine
from weaver_core.events import Event
from weaver_store.db import get_engine, init_db
from weaver_store.repo import Repo

STATIC = Path(__file__).parent / "static"


def create_app(db_url: str | None = None, engine: Engine | None = None) -> FastAPI:
    app = FastAPI(title="Weaver", version="0.1.0",
                  description="Behavioural security layer for AI agents")

    db = get_engine(db_url or os.environ.get("WEAVER_DB_URL"))
    init_db(db)
    repo = Repo(engine=db)
    eng = engine or Engine()

    app.state.repo = repo
    app.state.engine = eng

    # -- ingest -------------------------------------------------------------

    @app.post("/v1/events")
    def ingest(batch: list[dict[str, Any]] = Body(...)) -> dict:
        """Accept a batch of events, evaluate them, persist the results.

        Evaluation happens here rather than in the proxy when the proxy runs
        without an engine — the same code path either way, so a decision made
        inline and a decision made on ingest are identical.
        """
        accepted, alerts = 0, 0
        for raw in batch:
            try:
                ev = Event.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            decision, sigs = eng.evaluate(ev)
            # A decision already made inline by the proxy wins — re-deciding
            # would overwrite what actually happened to the call.
            if raw.get("decision", {}).get("sentence") or raw.get("decision", {}).get("verdict") != "allow":
                decision = ev.decision
                sigs = []
            else:
                ev.decision = decision
            repo.record(ev, sigs)
            accepted += 1
            if decision.verdict.value != "allow":
                alerts += 1
        return {"accepted": accepted, "alerts": alerts}

    # -- queries ------------------------------------------------------------

    @app.get("/v1/stats")
    def stats() -> dict:
        return repo.stats()

    @app.get("/v1/sessions")
    def sessions(limit: int = Query(50, le=500)) -> list[dict]:
        return repo.sessions(limit=limit)

    @app.get("/v1/sessions/{session_id}")
    def timeline(session_id: str) -> list[dict]:
        rows = repo.timeline(session_id)
        if not rows:
            raise HTTPException(404, f"no such session: {session_id}")
        return rows

    @app.get("/v1/alerts")
    def alerts(limit: int = Query(100, le=1000)) -> list[dict]:
        return repo.alerts(limit=limit)

    @app.get("/v1/graph")
    def graph(limit: int = Query(500, le=5000)) -> dict:
        return repo.graph(limit=limit)

    @app.get("/v1/agents")
    def agent_list() -> list[dict]:
        return repo.agent_list()

    @app.get("/v1/detectors")
    def detectors() -> dict:
        from weaver_core.detectors.base import (
            detector_errors,
            tier1_names,
            tier2_names,
        )

        return {"tier1": tier1_names(), "tier2": tier2_names(),
                "registered": tier1_names() + tier2_names(),
                "errors": detector_errors()}

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "version": "0.1.0"})

    # -- dashboard ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        index = STATIC / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>Weaver</h1><p>dashboard not built</p>", status_code=200)
        return HTMLResponse(index.read_text())

    return app


app = create_app()
