# Build Plan — Agent Immune System (Weaver)

Solo, full-time. **90 working days of build, 22 weeks calendar** (the gap is client
review cycles, scope changes, and detection tuning, which always overruns).

Read `ARCHITECTURE.md` first. This file is the order of operations.

---

## Milestone map

| Phase | Days | Weeks | Question it answers | Payment |
|---|---|---|---|---|
| **M0** Foundations | 1–5 | 1 | "Is the skeleton right?" | advance ₹2.0L |
| **M1** Visibility | 6–20 | 2–4 | "What did my agent actually do?" | ₹2.5L @ wk 4 |
| **M2** Detection | 21–40 | 5–8 | "Is this behaviour unusual?" | — |
| **M3** Enforcement | 41–55 | 9–11 | "What do we do about it?" | ₹4.0L @ wk 12 |
| **M4** Trust + graph | 56–70 | 12–14 | "How much freedom right now?" | — |
| **M5** Hardening | 71–90 | 15–18 | "Can a real team run this?" | ₹3.5L @ wk 22 |
| Buffer | — | 19–22 | tuning, UAT, handover | — |

**Out of scope for v1, say so in the contract:** SOC 2, penetration testing, 24/7
SLA, cross-customer threat sharing, more than one agent framework beyond the
LangGraph adapter, more than 3 bundled MCP integrations.

---

## M0 — Foundations · Days 1–5

**Goal:** the skeleton compiles, the contract between layers is fixed, and the
thing that measures success exists before the thing being measured.

- [ ] `uv` workspace across `packages/*`; ruff + mypy + pytest wired
- [ ] Define the **event schema** as a Pydantic model in `weaver_core` — this is
      the contract every other package depends on. Freeze it early.
- [ ] `weaver_store`: SQLAlchemy Core models for `nodes`, `edges`, `paths`,
      `events`, `decisions`, `signals`, `approvals`. Migration runner.
      **`tenant_id` on every table from day one.**
- [ ] Resource classifier v0 — rules only. URI pattern → class. ~60 lines.
- [ ] **`scenarios/` suite skeleton.** A scenario is a YAML file: an ordered list
      of synthetic events plus `expect: fire | silent`. Start with 6 attack and
      10 benign. This is the target you tune against — it must exist first.
- [ ] `scripts/replay.py` — feed a scenario through the engine, print verdicts.

**Exit criteria:** `pytest` green. `python scripts/replay.py scenarios/attack/exfil_basic.yaml`
runs end to end and prints "allow" for everything (no detectors yet — that is correct).

---

## M1 — Visibility · Days 6–20

**Goal:** the client can see what their agent did. This is the first thing you
demo and the first payment gate.

### Days 6–12 — the MCP proxy

- [ ] `weaver_proxy`: stdio MCP server that forwards to configured upstream servers
- [ ] Tool aggregation — present the union of upstream tools to the client
- [ ] Session and agent identity from the MCP `initialize` handshake
- [ ] Event emission onto a **bounded, non-blocking** queue (drop + count on full)
- [ ] HTTP/SSE transport as well as stdio
- [ ] Decision budget timer + fail-open wrapper (no detectors yet, always allows)

> ⚠️ The riskiest week of the project. MCP transport edge cases — notifications,
> progress tokens, cancellation, server-initiated messages — will eat time. Budget
> 2 spare days here and take them from M4 if unused.

### Days 13–16 — ingest and graph build

- [ ] `weaver_api`: FastAPI, `POST /v1/events` (batch), API-key auth
- [ ] Ingest worker: event → upsert nodes, upsert edges, extract n-grams → `paths`
- [ ] Lazy decay on read, implemented once, in `weaver_core/risk`
- [ ] `GET /v1/graph`, `GET /v1/sessions`, `GET /v1/events` with filters

### Days 17–20 — dashboard v1

- [ ] Next.js app, API-key auth, no user accounts yet
- [ ] **Session timeline** — the step-by-step trace of one agent run. This is the
      screen that sells the product. Build it well.
- [ ] **Graph view** — Cytoscape.js, force layout, node colour by risk, edge
      thickness by count. Click a node → its events.
- [ ] Event table with filters

**Exit criteria (demo script):**
1. Point a real Claude Code or Cursor session at the proxy
2. Let it do actual work against github-mcp and a filesystem MCP server
3. Open the dashboard, show the full session timeline and the graph that built itself
4. No measurable latency complaint from the agent operator

---

## M2 — Detection · Days 21–40

**Goal:** the system can say "this is unusual" and be right.

### Days 21–26 — baselines

- [ ] Per-agent baseline: tool frequency, n-gram set (n=3,4,5), active-hours
      histogram, resource-class distribution, per-tool arg centroid
- [ ] Baseline lifecycle: `learning` (first N events or T hours) → `active`.
      **Nothing fires while an agent is in `learning`.**
- [ ] Baseline persistence + incremental update on ingest
- [ ] Cold-start policy: a brand-new agent inherits the fleet-median baseline,
      not an empty one

### Days 27–34 — Tier 1 detectors

Build in this order. Each one ships with scenarios before code.

- [ ] **`retry_after_denial`** ← build this first, it is the signature detector
- [ ] **`sensitive_then_egress`** ← the canonical exfil pattern
- [ ] `novel_edge`
- [ ] `novel_sequence`
- [ ] `scope_widening`

Tier 1 runs **inline** in the proxy against the session ring buffer + graph snapshot.

### Days 35–40 — Tier 2 + scoring

- [ ] `velocity_anomaly`, `off_hours`, `argument_drift`, `fan_out` (async)
- [ ] Risk reinforcement + decay + corroboration boost wired into ingest
- [ ] Scoring function and the three thresholds
- [ ] **Explanation renderer** — evidence list → English sentence
- [ ] Alerts view in the dashboard, with the sentence front and centre

**Exit criteria — numeric, this is what the contract should reference:**

- ≥ 80% of `scenarios/attack/*` fire at HIGH or CRITICAL
- ≤ 5% of `scenarios/benign/*` fire at all
- Tier-1 inline decision p99 < 15 ms
- Every alert renders a sentence that a non-engineer can read

---

## M3 — Enforcement · Days 41–55

**Goal:** decisions become actions. This is where the product can break the
customer, so it is the most conservative phase in the plan.

- [ ] Verdict enforcement in the proxy: ALLOW / ASK / BLOCK
- [ ] **`WEAVER_MODE=observe` remains the default.** Enforce is opt-in, per agent,
      and the dashboard shows a banner while it is on.
- [ ] Blocked-call response: a structured MCP error the agent can reason about —
      it should be able to tell its operator *why*, not just fail
- [ ] **Approval workflow:** ASK suspends the call, creates an approval row,
      notifies (webhook + Slack), and blocks the proxy call up to a timeout
      (default 120 s → deny). Approve/deny from the dashboard.
- [ ] Policy engine: YAML rules layered *on top of* behavioural scoring —
      `never allow shell-mcp to touch secret://*`, `always ask on write to prod-db`
- [ ] Shadow mode: run enforce logic, log what *would* have happened, block nothing.
      **Every customer runs shadow for 2 weeks before enforce.**
- [ ] Alerting: webhook, Slack, email

**Exit criteria:**
- A live attack scenario is blocked end to end against a real MCP server
- An ASK verdict pauses a real agent, pings Slack, and resumes on approval
- Killing the API process does not stop agent traffic (fail-open verified under load)
- Shadow mode produces a diff report: "in the last 7 days, enforce would have
  blocked 4 actions, here they are"

---

## M4 — Adaptive trust + graph intelligence · Days 56–70

**Goal:** the differentiating layer. Risk on the graph starts paying off.

- [ ] Trust state machine: COLD/WARM/HOT, asymmetric up/down movement
- [ ] Zone-conditional policy — the same action gets a different verdict depending
      on the agent's current zone
- [ ] Session quarantine on repeated HOT signals
- [ ] Tier 3 detectors: `path_risk`, `resource_reputation`, `blast_radius`
- [ ] **Resource reputation surfaced in the UI** — "this MCP server has been
      flagged by 3 of your agents in the last week". This is the stigmergy demo.
- [ ] Graph view v2: risk heat, attack-path highlighting, time scrubber
- [ ] **LangGraph adapter** — `BaseCallbackHandler` emitting the same event shape
- [ ] Trust timeline chart per agent

**Exit criteria:**
- An agent visibly drops COLD → WARM → HOT across a scenario and recovers over time
- A resource flagged by agent A changes the verdict for agent B on its next touch
- The LangGraph adapter produces events indistinguishable from proxy events

---

## M5 — Hardening and deploy · Days 71–90

**Goal:** a real team can install, run and trust it. This phase is unglamorous and
it is the phase that separates "demo" from "product". Do not compress it.

- [ ] **Packaging: one-command install** (`pip install weaver` / `npx weaver init`)
      — ADR-009 makes this the single most important item in M5
- [ ] Zero-config defaults: works with no `.env`, sensible fallbacks everywhere
- [ ] `weaver --help` that explains the product, not just the flags
- [ ] Local dashboard on 127.0.0.1, no accounts (ADR-009 supersedes auth/roles)
- [ ] Secrets handling: arg redaction rules, retention policy, purge job
- [ ] Audit log — immutable, covers every decision and every config change
- [ ] Postgres migration path + a switch; keep SQLite for single-node installs
- [ ] Docker Compose install, `.env` driven, one-command bring-up
- [ ] Health, readiness, Prometheus metrics; queue-depth and drop-count alarms
- [ ] Load test: 1,000 tool calls/min sustained, verify p99 and zero agent errors
- [ ] Failure drills: kill DB, kill API, fill the queue, corrupt the cache
- [ ] Backup/restore runbook
- [ ] Docs: install guide, integration guide, detector reference, tuning guide,
      "what to do when it blocks something it shouldn't"
- [ ] Test coverage ≥ 70% on `weaver_core`

**Exit criteria:**
- Fresh machine → working install in under 30 minutes following the written guide
- Survives every failure drill without dropping agent traffic
- The client's own engineer completes an install unaided

---

## Weeks 19–22 — buffer

Not padding. This is where the real cost lands:

- Tuning against the client's actual traffic (their false positives, not yours)
- UAT and the change requests that come out of it
- Handover, training, and the first production incident

---

## Build order rules

1. **Scenarios before detectors, always.** Write the YAML that should fire, watch
   it not fire, then build the detector. Anything else is guessing.
2. **`weaver_core` imports nothing.** No FastAPI, no SQLAlchemy, no MCP. If a
   detector needs data, it arrives as an argument.
3. **Never let the proxy block on the engine.** Every path through the data plane
   has a timeout and a fail-open fallback. Test this before you need it.
4. **Observe mode is the default in every environment**, including yours.
5. **Ship the timeline view early.** It is the screen that makes the client
   believe the rest is real, and it costs almost nothing to build.

---

## Top risks

| Risk | Why it hurts | Mitigation |
|---|---|---|
| MCP transport edge cases | Silently breaks the client's agents | 2 spare days in M1; test against ≥3 real MCP servers |
| False positives on real traffic | One bad block and the product is uninstalled | Shadow mode mandatory; observe-mode default; numeric exit criteria |
| Detection tuning overruns | The classic solo-dev schedule killer | Scenario suite from day 5; milestone-based billing past M2, not fixed-price |
| Scope creep into "collective intelligence" | It is a research project, not a deliverable | Explicitly out of scope in the contract; schema supports it, v1 does not ship it |
| Ownership undecided | Could hand over the moat by accident | `weaver_core` stays dependency-free and separable; settle IP before M1 payment |
