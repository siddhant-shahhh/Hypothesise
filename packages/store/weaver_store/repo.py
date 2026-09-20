"""Repository — the only place that talks to the database.

Risk decay is applied lazily, on read, using each row's `risk_updated`. There
is no background sweep job: one fewer moving part to deploy, monitor and get
wrong, and a value that is correct the moment it is read rather than correct
as of the last sweep.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, delete, func, insert, select, update
from weaver_core.events import Event, ResourceClass, Verdict, new_id, utcnow
from weaver_core.graph.model import (
    edge_key,
    edges_for_event,
    lambda_for,
    nodes_for_event,
)
from weaver_core.risk.model import (
    Signal,
    confidence_from_observations,
    corroborate,
    decay,
    hours_between,
    reinforce,
)

from .schema import agents, decisions, edges, event_args, events, nodes, signals

CORROBORATION_WINDOW_HOURS = 24 * 7


@dataclass
class Repo:
    engine: Engine
    tenant: str = "default"

    # -- writes -------------------------------------------------------------

    def record(self, ev: Event, sigs: list[Signal] | None = None,
               mode: str = "observe", args: Any = None) -> None:
        now = utcnow()
        sigs = sigs or []
        with self.engine.begin() as conn:
            conn.execute(insert(events).values(
                event_id=ev.event_id, tenant_id=self.tenant, ts=ev.ts,
                session_id=ev.session_id, agent_id=ev.agent_id, step=ev.step,
                tool_server=ev.tool.server, tool_name=ev.tool.name,
                resource_uri=ev.resource.uri, resource_class=ev.resource.cls.value,
                action=ev.action.value, args_digest=ev.args_digest,
                status=ev.outcome.status.value, latency_ms=ev.outcome.latency_ms,
                bytes=ev.outcome.bytes, error=ev.outcome.error,
            ))
            conn.execute(insert(decisions).values(
                event_id=ev.event_id, tenant_id=self.tenant, ts=now,
                verdict=ev.decision.verdict.value, score=ev.decision.score,
                detectors=json.dumps(ev.decision.detectors),
                sentence=ev.decision.sentence, budget_ms=ev.decision.budget_ms,
                fail_open=int(ev.decision.fail_open), mode=mode,
            ))
            if args is not None:
                conn.execute(insert(event_args).values(
                    event_id=ev.event_id, tenant_id=self.tenant,
                    args=json.dumps(args, default=str), redacted=0,
                ))
            for s in sigs:
                conn.execute(insert(signals).values(
                    id=new_id("sig"), tenant_id=self.tenant, ts=s.ts or now,
                    event_id=ev.event_id, detector=s.detector, weight=s.weight,
                    detail=s.detail, agent_id=ev.agent_id,
                    node_id=s.node_id, edge_id=s.edge_id,
                ))
            self._touch_graph(conn, ev, now)
            self._touch_agent(conn, ev, now)

        for s in sigs:
            if s.node_id:
                self.reinforce_node(s.node_id, s.weight, s.detector)

    def _touch_graph(self, conn, ev: Event, now: str) -> None:
        for n in nodes_for_event(ev):
            row = conn.execute(select(nodes).where(nodes.c.id == n.id)).first()
            if row is None:
                conn.execute(insert(nodes).values(
                    id=n.id, tenant_id=self.tenant, kind=n.kind,
                    **{"class": n.cls.value}, label=n.label,
                    risk=n.risk_floor, risk_floor=n.risk_floor,
                    confidence=0.0, observations=1,
                    first_seen=now, last_seen=now, risk_updated=now,
                ))
            else:
                obs = row.observations + 1
                conn.execute(update(nodes).where(nodes.c.id == n.id).values(
                    observations=obs, last_seen=now,
                    confidence=confidence_from_observations(obs),
                ))

        for src, dst, action in edges_for_event(ev):
            k = edge_key(src, dst, action)
            row = conn.execute(select(edges).where(edges.c.id == k)).first()
            if row is None:
                conn.execute(insert(edges).values(
                    id=k, tenant_id=self.tenant, src=src, dst=dst,
                    action=action.value, count=1, risk=0.0, confidence=0.0,
                    first_seen=now, last_seen=now, risk_updated=now,
                ))
            else:
                cnt = row.count + 1
                conn.execute(update(edges).where(edges.c.id == k).values(
                    count=cnt, last_seen=now,
                    confidence=confidence_from_observations(cnt),
                ))

    def _touch_agent(self, conn, ev: Event, now: str) -> None:
        row = conn.execute(
            select(agents).where(agents.c.agent_id == ev.agent_id)
        ).first()
        if row is None:
            conn.execute(insert(agents).values(
                agent_id=ev.agent_id, tenant_id=self.tenant, trust=1.0,
                zone="cold", baseline_state="learning", event_count=1,
                first_seen=now, last_seen=now, trust_updated=now,
            ))
        else:
            conn.execute(update(agents).where(agents.c.agent_id == ev.agent_id)
                         .values(event_count=row.event_count + 1, last_seen=now))

    def reinforce_node(self, node_id: str, weight: float, detector: str) -> None:
        """Raise a node's risk, boosted by how many DISTINCT agents reported it.

        The ant-colony term. Keyed on distinct reporters so a single looping
        agent cannot manufacture its own corroboration.
        """
        now = utcnow()
        with self.engine.begin() as conn:
            row = conn.execute(select(nodes).where(nodes.c.id == node_id)).first()
            if row is None:
                return
            distinct = conn.execute(
                select(func.count(func.distinct(signals.c.agent_id)))
                .where(signals.c.node_id == node_id)
            ).scalar() or 1

            cls = ResourceClass(row._mapping["class"] or "unknown")
            current = decay(row.risk, row.risk_floor,
                            hours_between(row.risk_updated, now), lambda_for(cls))
            new_risk = reinforce(current, corroborate(weight, distinct))
            conn.execute(update(nodes).where(nodes.c.id == node_id).values(
                risk=new_risk, risk_updated=now,
            ))

    # -- reads (decay applied here) -----------------------------------------

    def _decayed(self, row) -> float:
        cls = ResourceClass(row._mapping.get("class") or "unknown")
        return decay(row.risk, row.risk_floor,
                     hours_between(row.risk_updated, utcnow()), lambda_for(cls))

    def graph(self, limit: int = 500) -> dict[str, list[dict]]:
        with self.engine.begin() as conn:
            nrows = conn.execute(select(nodes).limit(limit)).fetchall()
            erows = conn.execute(select(edges).limit(limit * 3)).fetchall()
        return {
            "nodes": [{
                "id": r.id, "kind": r.kind, "class": r._mapping["class"],
                "label": r.label, "risk": round(self._decayed(r), 4),
                "confidence": round(r.confidence, 4),
                "observations": r.observations, "last_seen": r.last_seen,
            } for r in nrows],
            "edges": [{
                "id": r.id, "src": r.src, "dst": r.dst, "action": r.action,
                "count": r.count, "risk": round(r.risk, 4),
            } for r in erows],
        }

    def sessions(self, limit: int = 50) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    events.c.session_id, events.c.agent_id,
                    func.count().label("n"),
                    func.min(events.c.ts).label("started"),
                    func.max(events.c.ts).label("ended"),
                ).group_by(events.c.session_id, events.c.agent_id)
                .order_by(func.max(events.c.ts).desc()).limit(limit)
            ).fetchall()
            out = []
            for r in rows:
                alerts = conn.execute(
                    select(func.count()).select_from(decisions)
                    .join(events, events.c.event_id == decisions.c.event_id)
                    .where(events.c.session_id == r.session_id,
                           decisions.c.verdict != Verdict.ALLOW.value)
                ).scalar() or 0
                out.append({"session_id": r.session_id, "agent_id": r.agent_id,
                            "events": r.n, "started": r.started, "ended": r.ended,
                            "alerts": alerts})
        return out

    def timeline(self, session_id: str) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(events, decisions.c.verdict, decisions.c.score,
                       decisions.c.sentence, decisions.c.detectors)
                .join(decisions, decisions.c.event_id == events.c.event_id, isouter=True)
                .where(events.c.session_id == session_id)
                .order_by(events.c.step)
            ).fetchall()
        return [{
            "event_id": r.event_id, "step": r.step, "ts": r.ts,
            "tool": f"{r.tool_server}/{r.tool_name}", "action": r.action,
            "resource": r.resource_uri, "class": r.resource_class,
            "status": r.status, "latency_ms": r.latency_ms,
            "verdict": r.verdict or "allow", "score": r.score or 0.0,
            "sentence": r.sentence or "",
            "detectors": json.loads(r.detectors) if r.detectors else [],
        } for r in rows]

    def alerts(self, limit: int = 100) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(decisions, events.c.agent_id, events.c.session_id,
                       events.c.tool_server, events.c.tool_name,
                       events.c.resource_uri, events.c.step)
                .join(events, events.c.event_id == decisions.c.event_id)
                .where(decisions.c.verdict != Verdict.ALLOW.value)
                .order_by(decisions.c.ts.desc()).limit(limit)
            ).fetchall()
        return [{
            "event_id": r.event_id, "ts": r.ts, "verdict": r.verdict,
            "score": r.score, "sentence": r.sentence,
            "detectors": json.loads(r.detectors) if r.detectors else [],
            "agent_id": r.agent_id, "session_id": r.session_id, "step": r.step,
            "tool": f"{r.tool_server}/{r.tool_name}", "resource": r.resource_uri,
        } for r in rows]

    def stats(self) -> dict:
        with self.engine.begin() as conn:
            total = conn.execute(select(func.count()).select_from(events)).scalar() or 0
            by_verdict = {
                row[0]: row[1]
                for row in conn.execute(
                    select(decisions.c.verdict, func.count()).group_by(decisions.c.verdict)
                ).fetchall()
            }
            n_agents = conn.execute(select(func.count()).select_from(agents)).scalar() or 0
            n_sessions = conn.execute(
                select(func.count(func.distinct(events.c.session_id)))
            ).scalar() or 0
            top = conn.execute(
                select(signals.c.detector, func.count().label("n"))
                .group_by(signals.c.detector).order_by(func.count().desc()).limit(10)
            ).fetchall()
            risky = conn.execute(
                select(nodes).where(nodes.c.kind == "resource")
                .order_by(nodes.c.risk.desc()).limit(10)
            ).fetchall()
        return {
            "events": total, "agents": n_agents, "sessions": n_sessions,
            "verdicts": by_verdict,
            "detectors": [{"detector": d, "count": n} for d, n in top],
            "riskiest": [
                {
                    "id": r.id,
                    "label": r.label,
                    "class": r._mapping["class"],
                    "risk": round(self._decayed(r), 4),
                }
                for r in risky
            ],
        }

    def agent_list(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(agents)).fetchall()
        return [{"agent_id": r.agent_id, "trust": r.trust, "zone": r.zone,
                 "baseline_state": r.baseline_state, "events": r.event_count,
                 "last_seen": r.last_seen} for r in rows]

    def clear(self) -> None:
        """Wipe everything. Test and demo helper."""
        with self.engine.begin() as conn:
            for t in (signals, decisions, event_args, events, edges, nodes, agents):
                conn.execute(delete(t))
