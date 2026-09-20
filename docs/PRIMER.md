# Weaver in ten minutes

Read this first. It's the whole project — what it does, why it's built this
way, and what's actually true versus still aspirational.

---

## The idea

AI agents can call tools: read files, query databases, run shell commands, hit
APIs. Every existing security tool asks **"is this agent allowed to do that?"**

That question is necessary and insufficient. An agent that is *allowed* to read
`.env` and *allowed* to make HTTP requests can do both in sequence and
exfiltrate your credentials, and every permission check passes.

Weaver asks a different question:

> **Is the way this agent is behaving normal for this agent?**

It sits between the agent and its tools, learns what that agent normally does,
and scores each call against that history.

```
agent ──> [ Weaver ] ──> github-mcp / postgres-mcp / shell-mcp / ...
               │
               └──> observe · learn · score · allow / ask / block
```

---

## The one idea that makes it different

Most agent-security products score the **agent**. Weaver scores the **graph**.

Risk is a property of nodes and edges — tools, resources, and the connections
between them — not a column on an agents table:

```
nodes: agent:coding-agent  tool:fs-mcp/read_file  res:file:///app/.env
       └─ risk, confidence ─┴─ risk, confidence ──┴─ risk, confidence

edges: agent ──read──> tool ──read──> resource
       └─ risk, confidence, count ─┘
```

Why that matters: if agent A finds a resource suspicious, agent B inherits that
suspicion the next time it touches the same resource. Risk attaches to the
*place*, not just the visitor. That's the "ant colony" idea from the original
spec — ants leave a trail in the environment, and other ants respond to it.

Three different agents flagging the same resource counts for far more than one
agent flagging it three times:

```
weight × (1 + ln(distinct_reporters))
```

Keying on *distinct* reporters is deliberate — otherwise one looping agent
manufactures its own corroboration.

Risk also decays. A resource that behaves normally cools off, exponentially,
toward a floor set by its class. A secrets store never cools below 0.30,
because a secrets store is never "safe" — only "not currently suspicious".

> This is ADR-004, and it's the decision everything else depends on. It costs
> nothing today and would cost about 30 days to retrofit.

---

## How a decision gets made

```
tool call
    │
    ├─ classify        URI + tool name -> resource class (secret? shell? source?)
    ├─ detect          10 detectors over the session window + baseline
    ├─ score           saturating combination + graph risk + agent trust
    └─ verdict         ALLOW (<0.30) · ASK (<0.65) · BLOCK
```

Every verdict above ALLOW carries a sentence a non-engineer can act on:

> *Blocked: read file:///app/.env (secret) at step 2, then external request to
> https://collector.unknown-host.net/ingest at step 3.*

**If a decision can't produce that sentence, it isn't allowed to block.** That's
enforced in code, not just documented.

---

## The detectors

**Tier 1** — deterministic. Describe actions no legitimate workflow performs.

| Detector | Weight | Fires when |
|---|---|---|
| `retry_after_denial` | CRITICAL | Denied on a secret, then reaching it another way |
| `sensitive_then_egress` | CRITICAL | Secret read, then an external network call |
| `scope_widening` | HIGH | Sensitivity climbs monotonically, ending somewhere sensitive |
| `fan_out` | HIGH | Two or more never-before-invoked agents recruited |
| `novel_edge` | MEDIUM | First use of a tool, against a sensitive resource |
| `novel_sequence` | MEDIUM/LOW | Unseen 3-gram; MEDIUM only if it touches something sensitive |

**Tier 2** — statistical. Describe actions that are merely unusual.

| Detector | Weight | Fires when |
|---|---|---|
| `argument_anomaly` | HIGH/LOW | Payload far larger than this tool ever carries |
| `novel_arg_shape` | LOW | Parameters never used before on a sensitive resource |
| `velocity_anomaly` | LOW | Calls arriving far faster than normal, hitting something sensitive |
| `off_hours` | LOW | An hour this agent has never worked in |

The two CRITICAL detectors **short-circuit to BLOCK** regardless of score. No
amount of accumulated trust argues them down.

### The one to understand

**`retry_after_denial`** is the signature detector. An agent gets blocked on
`.env`, then immediately tries `shell://cat /app/.env`. That's the agent
equivalent of port scanning, and per-call policy engines cannot see it because
they have no memory of the denial.

---

## What keeps it from breaking your agents

A security layer that stalls a coding agent gets uninstalled, and then it
secures nothing. Four rules, all enforced in code and pinned by tests:

1. **Observe mode is the default.** A fresh install decides everything and
   blocks nothing. You look at what it *would* have done first.
2. **Fail open.** Engine crashes, hangs, or exceeds its 50ms budget → the call
   goes through. `fail_mode=closed` exists and is not the default.
3. **`emit()` never blocks.** Events go on a bounded queue; when it's full they
   are dropped and counted. Losing telemetry beats stalling an agent.
4. **No sentence, no block.** An unexplainable block is a bug.

---

## Layout

```
packages/core/weaver_core/     THE ENGINE — zero I/O, zero framework deps
  events.py                    the schema everything depends on
  classify.py                  URI -> resource class
  graph/model.py               nodes, edges, risk floors, decay rates
  risk/model.py                reinforcement, decay, corroboration, trust
  baseline/model.py            per-agent normal
  detectors/tier1.py, tier2.py the detectors
  policy/score.py              scoring, thresholds, explanations
  engine.py                    ties it together

packages/store/                SQLite now, Postgres-portable
packages/proxy/                MCP interception — the data plane
packages/api/                  FastAPI + the dashboard (single HTML file)
scenarios/                     attack/ must fire · benign/ must not
```

`weaver_core` imports nothing. Partly for testability, partly because project
ownership is still open — a core that imports nothing is a core that can be
carved out.

---

## Try it

```bash
bash scripts/dev-setup.sh
pytest                                   # 135 tests
python3 scripts/replay.py --all          # the eval suite
python3 -m weaver_api --demo             # dashboard on :8080
```

`docs/COMMANDS.md` has everything else.

---

## What is actually true right now

**Built and working:**

- MCP proxy intercepting real tool calls over stdio
- 10 detectors, 135 tests, ruff and mypy clean
- Scoring, explanations, risk on the graph with decay and corroboration
- Ingest API, persistence, dashboard with session timeline and graph view
- **7/7 attack scenarios caught, 0/11 false positives**

**Not built yet:**

- Enforcement (M3) — `ASK` doesn't actually pause anything, there's no approval
  workflow, and `observe` mode is still the only sane setting
- Adaptive trust (M4) — the COLD/WARM/HOT machine is designed, not wired
- Tier 3 graph detectors (M4) — `path_risk`, `blast_radius`
- LangGraph adapter (M4), auth, packaging, Postgres (M5)

**The honest caveat.** That 7/7 and 0/11 is against a scenario suite we wrote
ourselves, and a suite we wrote cannot surprise us. There is no real agent
traffic yet (ADR-010). Read it as *"the detectors do what they were designed to
do"*, not *"detection works"*.

The cheapest fix is to put Weaver in front of your own Claude Code or Cursor
session today — section 6 of `COMMANDS.md`. Real traffic, available now,
costs nothing.

---

## Where to go next

| Question | File |
|---|---|
| How do I run it? | `docs/COMMANDS.md` |
| How does it work in detail? | `docs/ARCHITECTURE.md` |
| Why is it built this way? | `docs/DECISIONS.md` |
| What's the plan? | `docs/BUILD_PLAN.md` |
