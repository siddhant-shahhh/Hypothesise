# Architecture — Agent Immune System

> Working codename: **Weaver**. Package prefix `weaver_*`.
> Name is a placeholder — one find-replace to change it. Decide by end of M1.

---

## 0. The one-paragraph version

Weaver sits between an AI agent and the tools it calls. Every tool call is
intercepted, scored against what that agent normally does, and allowed, held for
human approval, or blocked. Risk is stored **on the graph** — on agents, on tools,
on resources, and on the edges between them — so that a resource which behaved
badly once makes every future agent that touches it more suspect.

The product is three things:

| Layer | Job | Latency budget |
|---|---|---|
| **Data plane** (proxy) | Intercept, decide, forward | < 50 ms, fail-open |
| **Control plane** (API + engine) | Baseline, score, update graph | async, seconds |
| **Presentation** (dashboard) | Show the web, alerts, approvals | human speed |

---

## 1. Two planes, and why the split is non-negotiable

The single biggest failure mode for this product is **breaking the customer's
agents**. A security layer that adds 400 ms to every tool call, or that halts the
pipeline when its own database is down, gets uninstalled in week one.

So the split is absolute:

```
            ┌──────────────────────── DATA PLANE (hot) ────────────────────────┐
            │                                                                   │
  Agent ──► MCP Proxy ──► [hot cache lookup + Tier-1 detectors] ──► MCP Server  │
            │      │                                                            │
            │      └──► emit(event) ──► in-proc queue (non-blocking, bounded)   │
            └────────────────────────────────┬──────────────────────────────────┘
                                             │
            ┌────────────────────────────────▼─────────── CONTROL PLANE (cold) ─┐
            │  Ingest ► Store ► Baseline ► Detectors T2/T3 ► Risk ► Graph update │
            │                                    │                               │
            │                                    └──► hot cache refresh          │
            └───────────────────────────────────┬───────────────────────────────┘
                                                │
                                    Dashboard / Approvals / Alerts
```

**Hard rules for the data plane:**

1. It never does a network call to decide. Everything it needs is in an in-memory
   snapshot of node/edge risk, refreshed from the control plane on a timer.
2. It never does a synchronous DB write. Events go onto a bounded queue; if the
   queue is full, events are dropped and a counter increments. Dropping telemetry
   is always better than blocking the agent.
3. It has a hard decision budget (`WEAVER_DECISION_BUDGET_MS`, default 50 ms). On
   timeout or any exception, it applies `WEAVER_FAIL_MODE` — default **open**.
4. `WEAVER_MODE=observe` is the default for a new install. Nothing is ever blocked
   until the operator flips to `enforce`. Every customer runs in observe mode for
   at least two weeks first.

---

## 2. Interception: MCP proxy first

We intercept at the **MCP protocol layer**, not inside a framework.

```
Claude Code ─┐
Cursor ──────┤
LangGraph ───┼──► Weaver MCP Proxy ──► github-mcp / postgres-mcp / shell-mcp / ...
CrewAI ──────┤
Custom ──────┘
```

The proxy is a **stdio and HTTP MCP server** that registers itself as an upstream
for the real MCP servers. To the agent it looks like one MCP server exposing the
union of all tools. To each real server it looks like a normal client.

What it sees on every call, for free:

- agent identity (from the session / client info handshake)
- tool name and the full argument object
- target server (the resource)
- the response, and whether it errored
- timing

**Why this over a LangGraph callback:** one integration covers every MCP-speaking
client, and MCP servers are where the genuinely dangerous surface lives —
filesystem, shell, database, secrets. A framework callback only ever covers that
framework.

**The LangGraph adapter (M4)** is a `BaseCallbackHandler` that emits the same
event shape into the same ingest endpoint. It catches in-process Python tool calls
that never cross MCP. Same engine, second front door.

### Event shape (the contract between everything)

```jsonc
{
  "event_id":   "evt_01J...",
  "ts":         "2026-09-19T10:31:22.418Z",
  "session_id": "sess_01J...",        // one agent run
  "agent_id":   "coding-agent-prod",  // stable across runs
  "step":       17,                   // monotonic within session
  "actor":      { "type": "agent", "id": "coding-agent-prod", "version": "1.4.2" },
  "tool":       { "server": "github-mcp", "name": "get_file_contents" },
  "resource":   { "uri": "github://acme/api/src/config.py", "class": "source_code" },
  "args_digest":"sha256:...",         // full args stored separately, redactable
  "outcome":    { "status": "ok", "bytes": 4210, "latency_ms": 88 },
  "decision":   { "verdict": "allow", "score": 0.11, "detectors": [], "budget_ms": 3 }
}
```

`resource.class` is the key field. It is assigned by a classifier (rules first,
learned later) into: `source_code`, `config`, `secret`, `database`, `shell`,
`network_egress`, `communication`, `unknown`. Risk floors attach to the class.

---

## 3. The graph — schema

This is the decision that must be right on day one. Risk lives on **nodes and
edges**, never as a column on an agents table. Get this wrong and stigmergy is a
rewrite later.

```sql
-- A thing in the ecosystem.
CREATE TABLE nodes (
  id            TEXT PRIMARY KEY,      -- "agent:coding-agent-prod", "res:github://..."
  kind          TEXT NOT NULL,         -- agent | tool | resource | identity | server
  class         TEXT,                  -- resource class, see above
  label         TEXT,
  risk          REAL NOT NULL DEFAULT 0.0,   -- 0..1, current
  risk_floor    REAL NOT NULL DEFAULT 0.0,   -- intrinsic floor by class
  confidence    REAL NOT NULL DEFAULT 0.0,   -- 0..1, how much evidence
  observations  INTEGER NOT NULL DEFAULT 0,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  risk_updated  TEXT NOT NULL,         -- for lazy decay
  attrs         TEXT                   -- JSON
);

-- A directed, typed relationship that has been observed.
CREATE TABLE edges (
  id            TEXT PRIMARY KEY,
  src           TEXT NOT NULL REFERENCES nodes(id),
  dst           TEXT NOT NULL REFERENCES nodes(id),
  action        TEXT NOT NULL,         -- read | write | execute | invoke | egress
  count         INTEGER NOT NULL DEFAULT 0,
  risk          REAL NOT NULL DEFAULT 0.0,
  confidence    REAL NOT NULL DEFAULT 0.0,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  risk_updated  TEXT NOT NULL,
  UNIQUE (src, dst, action)
);

-- An observed sequence of edges. This is the behavioural pattern store.
CREATE TABLE paths (
  seq_hash      TEXT PRIMARY KEY,      -- hash of the ordered (tool,action,class) tuple
  n             INTEGER NOT NULL,      -- length of the n-gram
  agent_id      TEXT NOT NULL,
  seq           TEXT NOT NULL,         -- JSON array, human-readable
  seen_count    INTEGER NOT NULL DEFAULT 0,
  risk          REAL NOT NULL DEFAULT 0.0,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

-- Raw event log. Append-only. The source of truth everything else derives from.
CREATE TABLE events (...);       -- the event shape above, flattened
CREATE TABLE decisions (...);    -- every verdict, with its full reason, for audit
CREATE TABLE approvals (...);    -- human-in-the-loop queue
CREATE TABLE signals (...);      -- detector firings, the input to risk updates
```

**Portability note:** SQLite now, Postgres later. Therefore: no SQLite-specific
SQL, use SQLAlchemy Core, ISO-8601 TEXT for timestamps, JSON as TEXT. Every table
carries a `tenant_id TEXT NOT NULL DEFAULT 'default'` from day one even though v1
is single-tenant — it costs nothing now and saves a migration later.

Path queries are 2–4 hops. Recursive CTEs handle that fine. No graph database.

---

## 4. Detectors — the behavioural layer

Detectors are pure functions: `(event, session_window, graph_snapshot) -> Signal | None`.
They never touch the DB directly. That makes them trivially testable and lets the
Tier-1 set run inline in the data plane.

### Tier 1 — deterministic, inline, near-zero false positive

Ships in M2. These run in the hot path because they need only the current
session's ring buffer and the in-memory graph snapshot.

| # | Detector | Fires when | Weight |
|---|---|---|---|
| 1 | `retry_after_denial` | A denied/blocked action is followed within N steps by a *different route to the same resource* | **critical** |
| 2 | `sensitive_then_egress` | Touched a `secret`/`config` class resource, then a `network_egress` action within the window | **critical** |
| 3 | `novel_edge` | This agent→tool or tool→resource edge has never been observed | medium |
| 4 | `novel_sequence` | The current n-gram (n=3,4,5) is absent from the agent's baseline | medium |
| 5 | `scope_widening` | Resource sensitivity increases monotonically across a sliding window | high |
| 6 | `fan_out` | Two or more agents invoked that this agent has never invoked | high |

**`retry_after_denial` is the product's signature detector.** It is the agent
equivalent of port scanning: an agent that gets blocked and immediately probes a
second path to the same target is doing something no legitimate workflow does.
Almost nothing on the market catches it, because per-call policy engines have no
memory of the denial. Build this first, demo this first.

### Tier 2 — statistical, needs a baseline

Built in M2. **These run inline too**, in the same pass as Tier 1.

> **Changed during M2.** The original design put Tier 2 in the control plane,
> async, because `argument_drift` was going to need embeddings. ADR-011
> replaced that with structural statistics, and every Tier-2 detector is now an
> O(1) arithmetic check over Welford accumulators already in memory. Running
> them inline means a decision uses all available evidence at the moment it is
> made, rather than catching up one event late.
>
> The tier distinction survives in **weight**, which is where it always
> mattered: Tier 1 describes actions nobody legitimate performs; Tier 2
> describes actions that are merely unusual. No Tier-2 detector
> short-circuits, and none exceeds HIGH.

| # | Detector | Weight | Method |
|---|---|---|---|
| 7 | `argument_anomaly` | HIGH / LOW | Payload size z-score vs. this tool's history. HIGH only when outbound. |
| 8 | `novel_arg_shape` | LOW | Parameter key-set never used before, on a sensitive resource |
| 9 | `velocity_anomaly` | LOW | Inter-call gap far below normal, landing on a sensitive resource |
| 10 | `off_hours` | LOW | An hour with no history, reaching a sensitive resource |

Each keeps a Welford accumulator (count, mean, M²) per tool rather than storing
samples: constant memory, and a baseline that cannot grow without bound. All
four return `None` until the baseline is active — statistics over eight samples
are not statistics.

### Tier 3 — graph-level

Ships in M4. These are the ones that only exist because risk lives on the graph.

| # | Detector | Method |
|---|---|---|
| 10 | `path_risk` | cumulative risk along the edges traversed this session |
| 11 | `resource_reputation` | inherited risk from the target node — **the stigmergy signal** |
| 12 | `blast_radius` | count of sensitive resources reachable within k hops from the agent's current position |

---

## 5. The risk model — reinforcement and decay

Every node and edge carries `risk ∈ [0,1]` and `confidence ∈ [0,1]`.

### Reinforcement (saturating — can approach 1, never exceed it)

```
risk ← risk + α · w · (1 − risk)
```

- `α` = `WEAVER_ALPHA`, default 0.35
- `w` = detector weight, normalised: low 0.1 / medium 0.3 / high 0.6 / critical 1.0

### Decay toward the class floor

Applied lazily on read, using `risk_updated`, so there is no background sweep job:

```
Δt   = hours since risk_updated
risk ← floor + (risk − floor) · e^(−λ · Δt)
```

- `λ` = `WEAVER_LAMBDA`, default 0.02/hr → roughly a 35-hour half-life
- `λ` is tuned **per node class**: a `secret` node decays slower than a `source_code` node
- `floor` is the intrinsic risk of the class, never decayed below:

  | class | floor |
  |---|---|
  | `secret` | 0.30 |
  | `shell` | 0.25 |
  | `network_egress` | 0.25 |
  | `database` | 0.15 |
  | `config` | 0.10 |
  | `source_code` | 0.00 |

### Independent corroboration — the ant-colony term

Three different agents independently finding the same resource suspicious is much
stronger evidence than one agent finding it suspicious three times. So the weight
is boosted by the number of *distinct* reporters, not the raw count:

```
distinct = count(distinct agent_id in signals for this node, last 7d)
w ← w · (1 + ln(max(distinct, 1)))
```

This is the whole stigmergy mechanic. Twenty lines of code. The value is not the
formula — it is that the schema can hold it.

### Confidence

```
confidence = n / (n + k)        k = 10
```

Derived from the observation count, not accumulated incrementally.

> **Corrected in M0.** The incremental form `c + (1−c)/(n+1)` was specified here
> first and is wrong: it saturates to 1.0 on the *first* observation, which lets
> a single sighting of a brand-new node carry full weight into the score —
> precisely the false positive the confidence term exists to prevent. Pinned by
> `test_single_observation_has_low_confidence`.

With k=10: 1 observation → 0.09, 10 → 0.50, 100 → 0.91. Low confidence damps the
risk term in the decision engine, so a brand-new node with one bad observation
does not start blocking production traffic.

---

## 6. The decision engine

```
score = w_seq   · sequence_anomaly
      + w_node  · max(node.risk · node.confidence   for nodes on this path)
      + w_edge  · mean(edge.risk · edge.confidence  for edges on this path)
      + w_trust · (1 − agent.trust)
      + w_t1    · Σ tier1_weights                  ← dominant term
```

Thresholds:

```
score < 0.30            → ALLOW
0.30 ≤ score < 0.65     → ASK   (hold for human approval)
score ≥ 0.65            → BLOCK
```

Signals combine **saturatingly**, not additively:

```
combined = 1 − Π(1 − wᵢ)
```

A sum lets a pile of weak hints outrank one hard fact, which is how anomaly
products earn their reputation for noise. Three MEDIUMs must raise concern
without reaching the certainty of one CRITICAL.

**Short-circuit:** any `critical` Tier-1 detector goes straight to BLOCK (in
enforce mode) regardless of score. `retry_after_denial` and `sensitive_then_egress`
are not negotiable against a trust score.

### Explainability is a first-class output, not a log line

Every decision row stores the full reason structure, and the API renders it to a
sentence:

```json
{
  "verdict": "block",
  "score": 0.81,
  "primary": "sensitive_then_egress",
  "evidence": [
    {"detector":"sensitive_then_egress","weight":1.0,
     "detail":"read secret://aws/prod-keys at step 12, then network_egress to api.unknown-host.net at step 14"},
    {"detector":"novel_edge","weight":0.3,
     "detail":"edge coding-agent-prod --egress--> api.unknown-host.net never observed in 4,102 prior events"}
  ],
  "sentence": "Blocked: this agent read production credentials and then made an
               external network request to a host it has never contacted before."
}
```

If a decision cannot produce that sentence, it must not block. That is a hard rule.

---

## 7. Adaptive trust — the temperature state machine

Per-agent `trust ∈ [0,1]`, and a zone derived from it:

| Zone | trust | Behaviour |
|---|---|---|
| 🟢 **COLD** | > 0.70 | Normal actions auto-approved. Verbose logging off. |
| 🟡 **WARM** | 0.30 – 0.70 | Sensitive-class actions require approval. Full arg capture on. |
| 🔴 **HOT** | < 0.30 | Sensitive actions blocked. After N further signals → session quarantined. |

**Trust moves asymmetrically — fast down, slow up.** This is correct for security
and it is a deliberate choice, not an accident:

```
on signal:      trust ← trust − β · w          (β = 0.25, immediate)
on clean hour:  trust ← trust + γ · (1 − trust) (γ = 0.02, gradual)
```

An agent can lose trust in one action and takes roughly a day of clean operation
to fully recover it.

---

## 8. Repo layout

```
AI_Layer/
├── docs/
│   ├── ARCHITECTURE.md        ← this file
│   ├── BUILD_PLAN.md          ← what to build, in order
│   └── DECISIONS.md           ← ADR log + open questions
├── packages/
│   ├── core/weaver_core/      ← THE ENGINE. No I/O, no framework deps.
│   │   ├── graph/             ← node/edge/path model + traversal
│   │   ├── baseline/          ← per-agent behavioural baselines
│   │   ├── detectors/         ← tier 1/2/3, pure functions
│   │   ├── risk/              ← reinforcement, decay, corroboration
│   │   ├── policy/            ← scoring + verdict + explanation
│   │   └── trust/             ← temperature state machine
│   ├── store/weaver_store/    ← persistence, SQLAlchemy, migrations
│   ├── proxy/weaver_proxy/    ← MCP interception (the data plane)
│   ├── api/weaver_api/        ← FastAPI control plane
│   └── adapters/langgraph/    ← second front door (M4)
├── apps/dashboard/            ← Next.js UI
├── scenarios/                 ← eval suite: benign/ and attack/
│   ├── benign/                ← must NOT fire (false-positive tests)
│   └── attack/                ← must fire (detection tests)
├── tests/
└── scripts/
```

**`weaver_core` has no I/O and no framework dependencies.** Not for elegance —
because ownership of this project is still undecided, and a core that imports
nothing is a core that can be licensed, carved out, or kept. Everything crossing
that boundary goes through explicit interfaces.

---

## 9. Non-negotiables

1. **Observe mode is the default.** A fresh install never blocks anything.
2. **Fail open.** Engine down = traffic flows. There is a `fail_mode=closed`
   setting for customers who want it; it is not the default.
3. **Every block has a human-readable sentence.** No sentence, no block.
4. **The scenario suite exists before the detectors do.** You cannot tune
   detection without a target. `scenarios/` is built in M1, not M5.
5. **Arguments are redactable.** Full tool arguments contain secrets by
   definition. Store the digest inline, full args in a separately-redactable
   table with a retention policy.
6. **No cross-tenant data sharing in v1.** When it comes, it shares hashes and
   patterns only — sequence hashes, server URL fingerprints, tool schema hashes.
   Never arguments, never payloads, never customer identifiers.
