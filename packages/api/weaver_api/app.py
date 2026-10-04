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
from fastapi.responses import HTMLResponse, JSONResponse, Response
from weaver_core.engine import Engine
from weaver_core.events import Event
from weaver_store.db import get_engine, init_db
from weaver_store.repo import Repo

STATIC = Path(__file__).parent / "static"


def create_app(db_url: str | None = None, engine: Engine | None = None,
               webhook_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Weaver", version="0.1.0",
                  description="Behavioural security layer for AI agents")

    db = get_engine(db_url or os.environ.get("WEAVER_DB_URL"))
    init_db(db)
    repo = Repo(engine=db)
    eng = engine or Engine()

    app.state.repo = repo
    app.state.engine = eng
    app.state.webhook = webhook_url or os.environ.get("WEAVER_WEBHOOK", "")

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

    @app.get("/v1/signals")
    def threat_signals(limit: int = Query(50, le=500)) -> list[dict]:
        """Corroborated suspicion per resource — the stigmergy view."""
        return repo.threat_signals(limit=limit)

    @app.get("/v1/decisions")
    def decision_log(limit: int = Query(200, le=2000), q: str = "") -> list[dict]:
        """Every decision, allows included. `/v1/alerts` is non-ALLOW only."""
        return repo.decision_log(limit=limit, q=q)

    @app.get("/v1/agents/{agent_id}/behavior")
    def agent_behavior(agent_id: str) -> dict:
        row = repo.agent_behavior(agent_id)
        if row is None:
            raise HTTPException(404, f"no such agent: {agent_id}")
        return row

    @app.get("/v1/policy")
    def policy() -> dict:
        """The policy actually in force in this process.

        Per-rule config (`weaver.config.json`) is loaded by the PROXY, not here,
        so the control plane cannot list those rules. What it can state exactly
        is the part that is compiled in: the thresholds, and the detectors that
        block on their own regardless of score.
        """
        from weaver_core.detectors.base import (
            detector_errors,
            tier1_names,
            tier2_names,
        )
        from weaver_core.policy.score import (
            ASK_THRESHOLD,
            BLOCK_THRESHOLD,
            SHORT_CIRCUIT,
            W_EDGE,
            W_NODE,
            W_TIER1,
            W_TRUST,
        )
        from weaver_core.trust.model import (
            HOT_BELOW,
            QUARANTINE_AFTER,
            WARM_BELOW,
        )

        return {
            "thresholds": {"ask": ASK_THRESHOLD, "block": BLOCK_THRESHOLD},
            "trust_zones": {"warm_below": WARM_BELOW, "hot_below": HOT_BELOW,
                            "quarantine_after": QUARANTINE_AFTER},
            "weights": {"tier1": W_TIER1, "node": W_NODE, "edge": W_EDGE,
                        "trust": W_TRUST},
            "short_circuit": sorted(SHORT_CIRCUIT),
            "detectors": {"tier1": tier1_names(), "tier2": tier2_names()},
            "errors": detector_errors(),
            "rules_source": "weaver.config.json, loaded by the proxy",
        }

    @app.post("/v1/agents/{agent_id}/restrict")
    def restrict(agent_id: str) -> dict:
        """Quarantine an agent by hand. The mirror of /release."""
        row = repo.restrict_agent(agent_id)
        if row is None:
            raise HTTPException(404, f"no such agent: {agent_id}")
        _notify(app.state.webhook, {"type": "agent_restricted", **row})
        return row

    # -- approvals ----------------------------------------------------------

    @app.post("/v1/approvals")
    def create_approval(body: dict[str, Any] = Body(...)) -> dict:
        """Open an approval request. Called by the proxy on an ASK verdict."""
        event_id = body.get("event_id")
        if not event_id:
            raise HTTPException(400, "event_id is required")
        row = repo.create_approval(
            event_id=event_id,
            ttl_seconds=int(body.get("ttl_seconds", 120)),
            reason=body.get("reason", ""),
            context={
                "agent_id": body.get("agent_id", ""),
                "tool": body.get("tool", ""),
                "resource": body.get("resource", ""),
                "class": body.get("resource_class", ""),
            },
        )
        _notify(app.state.webhook, {
            "type": "approval_requested",
            "approval_id": row["id"],
            "event_id": event_id,
            "reason": body.get("reason", ""),
            "agent_id": body.get("agent_id", ""),
            "tool": body.get("tool", ""),
            "resource": body.get("resource", ""),
            "expires_in_seconds": int(body.get("ttl_seconds", 120)),
        })
        return row

    @app.get("/v1/approvals")
    def list_approvals(limit: int = Query(50, le=500)) -> list[dict]:
        return repo.pending_approvals(limit=limit)

    @app.get("/v1/approvals/{approval_id}")
    def get_approval(approval_id: str) -> dict:
        row = repo.get_approval(approval_id)
        if row is None:
            raise HTTPException(404, "no such approval")
        return row

    @app.post("/v1/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, body: dict[str, Any] = Body(...)) -> dict:
        row = repo.decide_approval(
            approval_id,
            approve=bool(body.get("approve")),
            who=body.get("who", "dashboard"),
        )
        if row is None:
            raise HTTPException(404, "no such approval")
        _notify(app.state.webhook, {"type": "approval_decided", **row})
        return row

    # -- trust --------------------------------------------------------------

    @app.get("/v1/trust")
    def trust_state() -> list[dict]:
        """Trust for every known agent. The proxy reads this on startup."""
        return repo.trust_rows()

    @app.post("/v1/trust")
    def save_trust(body: list[dict[str, Any]] = Body(...)) -> dict:
        """Proxy pushes trust state here as it changes."""
        return {"saved": repo.save_trust(body)}

    @app.post("/v1/agents/{agent_id}/release")
    def release(agent_id: str) -> dict:
        """Clear an agent's quarantine. Deliberately a human-only action."""
        row = repo.release_agent(agent_id)
        if row is None:
            raise HTTPException(404, f"no such agent: {agent_id}")
        _notify(app.state.webhook, {"type": "agent_released", **row})
        return row

    @app.get("/v1/shadow")
    def shadow(limit: int = Query(200, le=2000)) -> dict:
        """What enforce mode WOULD have done. Run this before flipping the switch."""
        return repo.shadow_report(limit=limit)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness. Deliberately touches nothing — it answers 'is the process
        up', and a health check that queries the database turns a slow query
        into an outage."""
        return JSONResponse({"ok": True, "version": "0.1.0"})

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness: the database answers and the schema is current."""
        from weaver_store.migrate import status as schema_status

        try:
            st = schema_status(db)
            ok = st["up_to_date"]
            return JSONResponse(
                {"ok": ok, "schema": st},
                status_code=200 if ok else 503)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    @app.get("/metrics")
    def metrics() -> Response:
        """Prometheus text format.

        Hand-rolled rather than pulling in a client library — it is a dozen
        counters, and ADR-009 makes every dependency an install risk.
        """
        from weaver_core.detectors.base import detector_errors

        st = repo.stats()
        agents_by_zone: dict[str, int] = {}
        quarantined = 0
        for a in repo.agent_list():
            agents_by_zone[a.get("zone") or "cold"] = (
                agents_by_zone.get(a.get("zone") or "cold", 0) + 1)
            if a.get("quarantined"):
                quarantined += 1

        lines = [
            "# HELP weaver_events_total Events ingested.",
            "# TYPE weaver_events_total counter",
            f"weaver_events_total {st['events']}",
            "# HELP weaver_decisions_total Decisions by verdict.",
            "# TYPE weaver_decisions_total counter",
        ]
        for verdict in ("allow", "ask", "block"):
            lines.append(
                f'weaver_decisions_total{{verdict="{verdict}"}} '
                f'{st["verdicts"].get(verdict, 0)}')

        lines += [
            "# HELP weaver_agents Agents by trust zone.",
            "# TYPE weaver_agents gauge",
        ]
        for zone in ("cold", "warm", "hot"):
            lines.append(f'weaver_agents{{zone="{zone}"}} {agents_by_zone.get(zone, 0)}')

        lat = st.get("latency_ms") or {}
        lines += [
            "# HELP weaver_decision_latency_ms Mean decision latency the agent sees.",
            "# TYPE weaver_decision_latency_ms gauge",
            f"weaver_decision_latency_ms {lat.get('mean', 0)}",
            "# HELP weaver_decision_latency_max_ms Worst decision latency seen.",
            "# TYPE weaver_decision_latency_max_ms gauge",
            f"weaver_decision_latency_max_ms {lat.get('max', 0)}",
            "# HELP weaver_decisions_near_budget_total Decisions within 10ms of the budget.",
            "# TYPE weaver_decisions_near_budget_total counter",
            f"weaver_decisions_near_budget_total {lat.get('near_budget', 0)}",
            "# HELP weaver_decisions_failed_open_total Decisions allowed without evaluation.",
            "# TYPE weaver_decisions_failed_open_total counter",
            f"weaver_decisions_failed_open_total {lat.get('failed_open', 0)}",
            "# HELP weaver_agents_quarantined Quarantined agents.",
            "# TYPE weaver_agents_quarantined gauge",
            f"weaver_agents_quarantined {quarantined}",
            "# HELP weaver_approvals_pending Approvals awaiting a human.",
            "# TYPE weaver_approvals_pending gauge",
            f"weaver_approvals_pending {len(repo.pending_approvals(limit=500))}",
            "# HELP weaver_detector_errors_total Detectors raising exceptions.",
            "# TYPE weaver_detector_errors_total counter",
            f"weaver_detector_errors_total {sum(detector_errors().values())}",
            "# HELP weaver_signals_total Detector firings.",
            "# TYPE weaver_signals_total counter",
        ]
        for d in st["detectors"]:
            lines.append(
                f'weaver_signals_total{{detector="{d["detector"]}"}} {d["count"]}')

        return Response("\n".join(lines) + "\n", media_type="text/plain")

    # -- dashboard ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        index = STATIC / "index.html"
        if not index.exists():
            return HTMLResponse(
                "<h1>GUARDENT</h1><p>dashboard file missing from the install</p>",
                status_code=200)
        return HTMLResponse(index.read_text(encoding="utf-8"))

    return app


def _notify(url: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget webhook.

    Deliberately best-effort and never awaited: an approval must not fail
    because somebody's Slack relay is down, and the proxy is holding a real
    agent's tool call open while this runs.
    """
    if not url:
        return
    import json as _json
    import threading
    import urllib.request

    def _send() -> None:
        try:
            req = urllib.request.Request(
                url, data=_json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:  # noqa: BLE001 — a dead webhook is not an outage
            pass

    threading.Thread(target=_send, daemon=True).start()


app = create_app()
