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

from hypothesise_core.events import Event, ResourceClass, Verdict, new_id, utcnow
from hypothesise_core.graph.model import (
    edge_key,
    edges_for_event,
    lambda_for,
    nodes_for_event,
)
from hypothesise_core.risk.model import (
    Signal,
    confidence_from_observations,
    corroborate,
    decay,
    hours_between,
    reinforce,
)
from sqlalchemy import Engine, case, delete, func, insert, select, update

from .schema import (
    agents,
    approvals,
    decisions,
    edges,
    event_args,
    events,
    nodes,
    signals,
)

CORROBORATION_WINDOW_HOURS = 24 * 7


def _expired(expires_at: str | None) -> bool:
    from datetime import datetime, timezone

    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False



def _would(sentence: str | None) -> str:
    """The verdict an observe-mode sentence says enforce mode would have given."""
    return "block" if "would have block" in (sentence or "") else "ask"


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    i = max(0, min(len(sorted_vals) - 1, int(round(q * len(sorted_vals) + 0.5)) - 1))
    return sorted_vals[i]


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
                elapsed_ms=float(ev.decision.elapsed_ms or 0.0),
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
                # Without the floor the dashboard drew every secret ever
                # touched as a "reinforced" path, flagged or not (ADR-042).
                "risk_floor": round(float(r.risk_floor or 0.0), 4),
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
                           (decisions.c.verdict != Verdict.ALLOW.value)
                           | decisions.c.sentence.like("[observe]%"))
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
                .where((decisions.c.verdict != Verdict.ALLOW.value)
                       | decisions.c.sentence.like("[observe]%"))
                .order_by(decisions.c.ts.desc()).limit(limit)
            ).fetchall()
        # Observe-mode interventions are alerts too, but they must never read
        # as blocks that happened: `enforced` says which is which, and
        # `verdict` for a would-be one is what enforce mode WOULD have done.
        return [{
            "event_id": r.event_id, "ts": r.ts,
            "verdict": r.verdict if r.verdict != Verdict.ALLOW.value
            else _would(r.sentence),
            "enforced": r.verdict != Verdict.ALLOW.value,
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
            # Decision latency is what the AGENT feels. Past the gate's budget
            # it fails open and detection stops silently, so this is the number
            # that says whether the tool is still doing anything under load.
            times = sorted(
                float(r[0]) for r in conn.execute(
                    select(decisions.c.elapsed_ms).where(decisions.c.elapsed_ms > 0)
                ).fetchall())
            slow = conn.execute(
                select(func.count()).select_from(decisions)
                .where(decisions.c.elapsed_ms >= 40)
            ).scalar() or 0
            would = dict(conn.execute(
                select(case((decisions.c.sentence.like("[observe] would have block%"),
                             "block"), else_="ask"), func.count())
                .where(decisions.c.sentence.like("[observe]%"))
                .group_by(case((decisions.c.sentence.like("[observe] would have block%"),
                                "block"), else_="ask"))
            ).fetchall())
            today_prefix = utcnow()[:10]
            today = conn.execute(
                select(func.count()).select_from(events)
                .where(events.c.ts.like(f"{today_prefix}%"))
            ).scalar() or 0
            activity = self._activity(conn)
            failed_open = conn.execute(
                select(func.count()).select_from(decisions)
                .where(decisions.c.fail_open == 1)
            ).scalar() or 0
        return {
            "events": total, "agents": n_agents, "sessions": n_sessions,
            "events_today": today,
            "verdicts": by_verdict,
            "would": {"block": int(would.get("block", 0)), "ask": int(would.get("ask", 0))},
            "activity": activity,
            "detectors": [{"detector": d, "count": n} for d, n in top],
            # Only decisions that carry a measurement count; rows written
            # before ADR-042 have none and would drag every figure to zero.
            "latency_ms": {
                "mean": round(sum(times) / len(times), 4) if times else None,
                "p50": round(_pct(times, 0.50), 4) if times else None,
                "p95": round(_pct(times, 0.95), 4) if times else None,
                "max": round(times[-1], 4) if times else None,
                "measured": len(times),
                "near_budget": slow,        # >= 40ms of a 50ms budget
                "failed_open": failed_open,  # decisions made without evaluating
            },
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

    def _activity(self, conn, hours: int = 120) -> dict:
        """Calls per hour for the last `hours` hours (UTC), with how many were
        held, blocked, or would have been. Drives the Overview heatmap, which
        used to be a fixed pseudo-random pattern (ADR-042)."""
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=hours - 1)
        rows = conn.execute(
            select(events.c.ts, decisions.c.verdict, decisions.c.sentence)
            .join(decisions, decisions.c.event_id == events.c.event_id, isouter=True)
            .where(events.c.ts >= start.strftime("%Y-%m-%dT%H"))
        ).fetchall()
        buckets: dict[str, dict] = {}
        for ts, verdict, sentence in rows:
            key = str(ts)[:13]
            b = buckets.setdefault(key, {"calls": 0, "ask": 0, "block": 0, "would": 0})
            b["calls"] += 1
            if verdict == "block":
                b["block"] += 1
            elif verdict == "ask":
                b["ask"] += 1
            elif (sentence or "").startswith("[observe]"):
                b["would"] += 1
        cells = []
        for i in range(hours):
            t = start + timedelta(hours=i)
            key = t.strftime("%Y-%m-%dT%H")
            cells.append({"hour": key, **buckets.get(
                key, {"calls": 0, "ask": 0, "block": 0, "would": 0})})
        return {"hours": hours, "cells": cells}

    def agent_list(self) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(agents)).fetchall()
        return [{"agent_id": r.agent_id, "trust": r.trust, "zone": r.zone,
                 "hot_signals": r.hot_signals, "quarantined": bool(r.quarantined),
                 "baseline_state": r.baseline_state, "events": r.event_count,
                 "last_seen": r.last_seen,
                 "trust_updated": r.trust_updated} for r in rows]

    def clear(self) -> None:
        """Wipe everything. Test and demo helper."""
        with self.engine.begin() as conn:
            for t in (signals, decisions, event_args, events, edges, nodes, agents):
                conn.execute(delete(t))

    # -- approvals ----------------------------------------------------------

    def create_approval(self, event_id: str, ttl_seconds: int = 120,
                        reason: str = "", context: dict | None = None) -> dict:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        row_id = new_id("apr")
        with self.engine.begin() as conn:
            conn.execute(insert(approvals).values(
                id=row_id, tenant_id=self.tenant, event_id=event_id,
                created_at=now.isoformat(timespec="milliseconds"),
                expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(
                    timespec="milliseconds"),
                state="pending", reason=reason,
                context=json.dumps(context or {}),
            ))
        return {"id": row_id, "state": "pending", "event_id": event_id}

    def get_approval(self, approval_id: str) -> dict | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(approvals).where(approvals.c.id == approval_id)
            ).first()
        if row is None:
            return None
        state = row.state
        # Expiry is evaluated on read rather than by a sweep job, so a pending
        # row can never be "still pending" past its deadline just because
        # nothing happened to run.
        if state == "pending" and _expired(row.expires_at):
            state = "expired"
        return {"id": row.id, "event_id": row.event_id, "state": state,
                "created_at": row.created_at, "expires_at": row.expires_at,
                "decided_at": row.decided_at, "decided_by": row.decided_by,
                "reason": row.reason}

    def decide_approval(self, approval_id: str, approve: bool,
                        who: str = "dashboard") -> dict | None:
        current = self.get_approval(approval_id)
        if current is None:
            return None
        if current["state"] != "pending":
            return current          # already decided or expired — never flip it

        with self.engine.begin() as conn:
            conn.execute(update(approvals).where(approvals.c.id == approval_id).values(
                state="approved" if approve else "denied",
                decided_at=utcnow(), decided_by=who,
            ))
        return self.get_approval(approval_id)

    def pending_approvals(self, limit: int = 50) -> list[dict]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(approvals, events.c.agent_id, events.c.tool_server,
                       events.c.tool_name, events.c.resource_uri,
                       events.c.resource_class, events.c.step,
                       decisions.c.sentence, decisions.c.score,
                       decisions.c.detectors)
                .join(events, events.c.event_id == approvals.c.event_id, isouter=True)
                .join(decisions, decisions.c.event_id == approvals.c.event_id,
                      isouter=True)
                .where(approvals.c.state == "pending")
                .order_by(approvals.c.created_at.desc()).limit(limit)
            ).fetchall()
        out = []
        for r in rows:
            if _expired(r.expires_at):
                continue
            # Prefer the event row once it has arrived; fall back to the
            # context the proxy sent with the request.
            ctx = {}
            if r.context:
                try:
                    ctx = json.loads(r.context)
                except (TypeError, ValueError):
                    ctx = {}
            out.append({
                "id": r.id, "event_id": r.event_id, "created_at": r.created_at,
                "expires_at": r.expires_at,
                "agent_id": r.agent_id or ctx.get("agent_id") or "unknown",
                "tool": (f"{r.tool_server}/{r.tool_name}" if r.tool_server
                         else ctx.get("tool") or ""),
                "resource": r.resource_uri or ctx.get("resource") or "",
                "class": r.resource_class or ctx.get("class") or "unknown",
                "step": r.step, "sentence": r.sentence or r.reason or "",
                "score": r.score or 0.0,
                "detectors": json.loads(r.detectors) if r.detectors else [],
            })
        return out

    # -- trust --------------------------------------------------------------

    def trust_rows(self) -> list[dict]:
        """Everything the proxy needs to restore trust state on startup."""
        with self.engine.begin() as conn:
            rows = conn.execute(select(agents)).fetchall()
        return [{"agent_id": r.agent_id, "trust": r.trust, "zone": r.zone,
                 "hot_signals": r.hot_signals,
                 "quarantined": bool(r.quarantined),
                 "trust_updated": r.trust_updated} for r in rows]

    def save_trust(self, states: list[dict]) -> int:
        """Persist trust state. Called by the proxy as it changes."""
        now = utcnow()
        saved = 0
        with self.engine.begin() as conn:
            for st in states:
                aid = st.get("agent_id")
                if not aid:
                    continue
                existing = conn.execute(
                    select(agents).where(agents.c.agent_id == aid)).first()
                values = {
                    "trust": float(st.get("trust", 1.0)),
                    "zone": st.get("zone", "cold"),
                    "hot_signals": int(st.get("hot_signals", 0)),
                    "quarantined": int(bool(st.get("quarantined", False))),
                    "trust_updated": now,
                }
                if existing is None:
                    conn.execute(insert(agents).values(
                        agent_id=aid, tenant_id=self.tenant,
                        baseline_state="learning", event_count=0,
                        first_seen=now, last_seen=now, **values))
                else:
                    conn.execute(update(agents)
                                 .where(agents.c.agent_id == aid).values(**values))
                saved += 1
        return saved

    def release_agent(self, agent_id: str) -> dict | None:
        """Clear a quarantine. Only ever done by a human."""
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agents).where(agents.c.agent_id == agent_id)).first()
            if row is None:
                return None
            # 0.70 is the Safe-zone floor; the stored zone must say what the
            # engine will do with that trust (it said "warm" while the engine
            # treated the agent as cold — ADR-042).
            from hypothesise_core.trust.model import WARM_BELOW, zone_for

            trust = max(float(row.trust), WARM_BELOW)
            conn.execute(update(agents).where(agents.c.agent_id == agent_id).values(
                quarantined=0, hot_signals=0,
                trust=trust, zone=zone_for(trust),
                trust_updated=utcnow()))
        return self.agent(agent_id)

    def agent(self, agent_id: str) -> dict | None:
        with self.engine.begin() as conn:
            r = conn.execute(
                select(agents).where(agents.c.agent_id == agent_id)).first()
        if r is None:
            return None
        return {"agent_id": r.agent_id, "trust": r.trust, "zone": r.zone,
                "hot_signals": r.hot_signals, "quarantined": bool(r.quarantined),
                "baseline_state": r.baseline_state, "events": r.event_count,
                "first_seen": r.first_seen, "last_seen": r.last_seen,
                "trust_updated": r.trust_updated}

    def shadow_report(self, limit: int = 200) -> dict:
        """What enforce mode WOULD have done, from decisions made in observe mode."""
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(decisions, events.c.agent_id, events.c.tool_server,
                       events.c.tool_name, events.c.resource_uri, events.c.ts)
                .join(events, events.c.event_id == decisions.c.event_id)
                .where(decisions.c.mode == "observe",
                       decisions.c.sentence.like("[observe]%"))
                .order_by(decisions.c.ts.desc()).limit(limit)
            ).fetchall()
            total = conn.execute(select(func.count()).select_from(events)).scalar() or 0

        items = [{
            "event_id": r.event_id, "ts": r.ts, "agent_id": r.agent_id,
            "tool": f"{r.tool_server}/{r.tool_name}", "resource": r.resource_uri,
            "score": r.score, "sentence": r.sentence,
            "detectors": json.loads(r.detectors) if r.detectors else [],
        } for r in rows]
        would_block = sum(1 for i in items if "would have block" in i["sentence"])
        return {
            "total_events": total,
            "would_intervene": len(items),
            "would_block": would_block,
            "would_ask": len(items) - would_block,
            "rate": round(len(items) / total, 5) if total else 0.0,
            "items": items,
        }

    # -- views the dashboard needs -----------------------------------------

    def threat_signals(self, limit: int = 50) -> list[dict]:
        """Resources ranked by corroborated suspicion.

        The `ix_signals_node_agent_ts` index was created for exactly this query
        and nothing read it until now. What makes a signal strong is the number
        of DISTINCT agents that reported the node, not the number of reports —
        one looping agent must not be able to corroborate itself.

        `trend` is derived, not stored: a node whose decayed risk still sits
        near its stored value was reinforced recently and is rising; one that
        has visibly cooled since `risk_updated` is decaying.
        """
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(
                    signals.c.node_id,
                    func.count(func.distinct(signals.c.agent_id)).label("agents"),
                    func.count().label("reports"),
                    func.max(signals.c.ts).label("last_ts"),
                )
                .where(signals.c.node_id.isnot(None))
                .group_by(signals.c.node_id)
                .order_by(func.count(func.distinct(signals.c.agent_id)).desc(),
                          func.count().desc())
                .limit(limit)
            ).fetchall()
            if not rows:
                return []
            node_rows = conn.execute(
                select(nodes).where(nodes.c.id.in_([r.node_id for r in rows]))
            ).fetchall()
            detail = conn.execute(
                select(signals.c.node_id, signals.c.detector,
                       func.count().label("n"))
                .where(signals.c.node_id.in_([r.node_id for r in rows]))
                .group_by(signals.c.node_id, signals.c.detector)
            ).fetchall()

        by_id = {n.id: n for n in node_rows}
        dets: dict[str, list[dict]] = {}
        for d in detail:
            dets.setdefault(d.node_id, []).append(
                {"detector": d.detector, "count": d.n})

        out = []
        for r in rows:
            n = by_id.get(r.node_id)
            if n is None:
                continue
            now = self._decayed(n)
            stored = float(n.risk)
            floor = float(n.risk_floor)
            headroom = stored - floor
            if headroom <= 1e-9:
                trend = "stable"
            elif now >= stored - 0.02 * max(headroom, 0.01):
                trend = "rising"
            elif now <= floor + 0.5 * headroom:
                trend = "decaying"
            else:
                trend = "stable"
            out.append({
                "node_id": n.id,
                "label": n.label or n.id,
                "kind": n.kind,
                "class": n._mapping["class"],
                "agents": int(r.agents),
                "reports": int(r.reports),
                "risk": round(now, 4),
                "risk_floor": round(floor, 4),
                "confidence": round(float(n.confidence), 4),
                "observations": int(n.observations),
                "trend": trend,
                "last_signal": r.last_ts,
                "detectors": sorted(dets.get(n.id, []),
                                    key=lambda d: -d["count"]),
            })
        return out

    def decision_log(self, limit: int = 200, q: str = "") -> list[dict]:
        """Every decision, allows included — the audit view.

        `alerts()` deliberately returns only non-ALLOW rows, which makes it the
        wrong source for an audit log: the question an auditor asks is "what did
        this agent do", and the answer has to include everything it was let
        through on.
        """
        stmt = (
            select(decisions, events.c.agent_id, events.c.session_id,
                   events.c.step, events.c.tool_server, events.c.tool_name,
                   events.c.action, events.c.resource_uri,
                   events.c.resource_class, events.c.status)
            .join(events, events.c.event_id == decisions.c.event_id)
        )
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                events.c.agent_id.like(like)
                | events.c.tool_name.like(like)
                | events.c.tool_server.like(like)
                | events.c.resource_uri.like(like)
                | decisions.c.sentence.like(like)
                | decisions.c.verdict.like(like)
            )
        with self.engine.begin() as conn:
            rows = conn.execute(
                stmt.order_by(decisions.c.ts.desc()).limit(limit)).fetchall()
        return [{
            "event_id": r.event_id, "ts": r.ts, "agent_id": r.agent_id,
            "session_id": r.session_id, "step": r.step,
            "tool": f"{r.tool_server}/{r.tool_name}", "action": r.action,
            "resource": r.resource_uri, "class": r.resource_class,
            "status": r.status, "verdict": r.verdict,
            "score": r.score, "sentence": r.sentence or "",
            "detectors": json.loads(r.detectors) if r.detectors else [],
        } for r in rows]

    def agent_behavior(self, agent_id: str, limit: int = 10) -> dict | None:
        """What this agent normally does, and what fell outside it.

        Built from events rather than the proxy's baseline: the baseline lives
        in the proxy process and is never shipped, so the control plane's only
        honest answer to "what is normal for this agent" is the distribution of
        what it has actually been seen doing.
        """
        base = self.agent(agent_id)
        if base is None:
            return None
        with self.engine.begin() as conn:
            classes = conn.execute(
                select(events.c.resource_class, func.count().label("n"))
                .where(events.c.agent_id == agent_id)
                .group_by(events.c.resource_class)
                .order_by(func.count().desc())
            ).fetchall()
            tools = conn.execute(
                select(events.c.tool_server, events.c.tool_name,
                       func.count().label("n"))
                .where(events.c.agent_id == agent_id)
                .group_by(events.c.tool_server, events.c.tool_name)
                .order_by(func.count().desc()).limit(limit)
            ).fetchall()
            outside = conn.execute(
                select(decisions.c.verdict, decisions.c.score,
                       decisions.c.sentence, decisions.c.ts,
                       events.c.resource_uri, events.c.resource_class,
                       events.c.tool_server, events.c.tool_name)
                .join(events, events.c.event_id == decisions.c.event_id)
                .where(events.c.agent_id == agent_id,
                       decisions.c.verdict != Verdict.ALLOW.value)
                .order_by(decisions.c.ts.desc()).limit(limit)
            ).fetchall()

        total = sum(c.n for c in classes) or 1
        return {
            **base,
            "classes": [{"class": c.resource_class or "unknown", "events": c.n,
                         "pct": round(100.0 * c.n / total, 1)} for c in classes],
            "tools": [{"tool": f"{t.tool_server}/{t.tool_name}", "events": t.n}
                      for t in tools],
            "outside": [{
                "tool": f"{o.tool_server}/{o.tool_name}",
                "resource": o.resource_uri, "class": o.resource_class,
                "verdict": o.verdict, "score": o.score,
                "sentence": o.sentence or "", "ts": o.ts,
            } for o in outside],
        }

    def restrict_agent(self, agent_id: str) -> dict | None:
        """Quarantine by hand — the mirror of `release_agent`.

        The dashboard could already let a human clear a quarantine but not start
        one, so an operator who saw something alarming had no action available
        except editing the database.
        """
        with self.engine.begin() as conn:
            row = conn.execute(
                select(agents).where(agents.c.agent_id == agent_id)).first()
            if row is None:
                return None
            conn.execute(update(agents).where(agents.c.agent_id == agent_id).values(
                quarantined=1, zone="hot",
                trust=min(float(row.trust), 0.20),
                trust_updated=utcnow()))
        return self.agent(agent_id)
