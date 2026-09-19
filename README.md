# AI_Layer — Agent Immune System

> **Working codename:** Weaver · **Status:** M0, scaffolding

A behavioural security layer for AI agents. It sits between an agent and the tools
it calls, learns how that agent normally operates, detects dangerous deviations,
and dynamically controls how much autonomy the agent has.

**What makes it different from an agent firewall:** risk is attached to the
*graph* — to tools, resources and the edges between them — not to the agent. A
resource that behaved badly once makes every future agent that touches it more
suspect.

---

## Start here

| Document | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, graph schema, detector tiers, risk math, trust machine |
| [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md) | M0–M5, day by day, with exit criteria |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADR log and the open questions |

---

## Layout

```
packages/core/      weaver_core   — the engine. no I/O, no framework deps.
packages/store/     persistence (SQLite now, Postgres-portable)
packages/proxy/     MCP interception — the data plane
packages/api/       FastAPI control plane
packages/adapters/  LangGraph callback handler (M4)
apps/dashboard/     Next.js UI
scenarios/          eval suite — attack/ must fire, benign/ must not
```

---

## Locked decisions

- Intercept at the **MCP layer** first; LangGraph adapter second
- **SQLite** now, Postgres-portable schema, `tenant_id` everywhere from day one
- **Self-hosted, single-tenant** — no telemetry leaves the client's network
- **Risk lives on nodes and edges**, never as a column on an agents table
- **No LLM in the decision path**

## Non-negotiables

1. Observe mode is the default. A fresh install never blocks anything.
2. Fail open. Engine down means traffic flows.
3. Every block renders a human-readable sentence. No sentence, no block.
4. The scenario suite exists before the detectors do.

---

## Dev setup

```bash
uv sync
cp .env.example .env
pytest
python scripts/replay.py scenarios/attack/exfil_basic.yaml
```
