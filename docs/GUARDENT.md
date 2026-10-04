# Guardent — the complete guide

**Guardent** is a behavioural security layer for AI agents. It sits between an
agent and its tools, learns what that agent normally does, and scores every
tool call against that history.

This is the one document to read first. It covers what the product is, how it
is built, how to run it, how to test it, and what is honestly still missing.
Everything else in `docs/` goes deeper on one piece.

> **A note on the name.** The product is Guardent. The code, the CLI and the
> Python packages still say `weaver` — that was the working codename, and the
> rename is a separate mechanical change (see [Naming](#naming) at the end).
> So in this document: **Guardent** is the product, `weaver` is the command you
> type. Do not let that confuse you; it is the same thing.

---

## Contents

1. [The idea](#1-the-idea)
2. [What makes it different](#2-what-makes-it-different)
3. [Architecture](#3-architecture)
4. [How a decision gets made](#4-how-a-decision-gets-made)
5. [The detectors](#5-the-detectors)
6. [What stops it breaking your agents](#6-what-stops-it-breaking-your-agents)
7. [Repo layout](#7-repo-layout)
8. [Install](#8-install)
9. [Commands](#9-commands)
10. [Running it against a real project](#10-running-it-against-a-real-project)
11. [The dashboard](#11-the-dashboard)
12. [Testing](#12-testing)
13. [Where things stand](#13-where-things-stand)
14. [Honest gaps](#14-honest-gaps)
15. [Working on the code](#15-working-on-the-code)
16. [Naming](#naming)

---

## 1. The idea

AI agents call tools: read files, query databases, run shell commands, hit
APIs. Every existing security tool asks:

> **"Is this agent allowed to do that?"**

Necessary, and not enough. An agent that is *allowed* to read `.env` and
*allowed* to make HTTP requests can do both in sequence and exfiltrate your
credentials. Every permission check passes. Nothing looks wrong at any single
step.

Guardent asks a different question:

> **"Is the way this agent is behaving normal for this agent?"**

```
agent ──> [ Guardent ] ──> github-mcp / postgres-mcp / shell-mcp / ...
                │
                └──> observe · learn · score · allow / ask / block
```

The unit of analysis is the **sequence**, not the call.

---

## 2. What makes it different

Most agent-security products score the **agent**. Guardent scores the **graph**.

Risk is a property of nodes and edges — tools, resources, and the connections
between them — never a column on an agents table:

```
nodes: agent:coding-agent  tool:fs-mcp/read_file  res:file:///app/.env
       └─ risk, confidence ─┴─ risk, confidence ──┴─ risk, confidence

edges: agent ──read──> tool ──read──> resource
       └─ risk, confidence, count ─┘
```

Why it matters: if agent A gets caught somewhere, agent B inherits that
suspicion the next time it touches the same place. **Risk attaches to the
place, not just the visitor.** That is the stigmergy idea — ants leave a trail
in the environment, and other ants respond to the trail rather than to each
other.

Three *different* agents flagging the same resource counts for far more than
one agent flagging it three times:

```
weight × (1 + ln(distinct_reporters))
```

Keying on **distinct** reporters is deliberate. Otherwise one looping agent
manufactures its own corroboration.

Risk also decays. A resource that behaves normally cools off exponentially
toward a floor set by its class. A secrets store never cools below **0.30**,
because a secrets store is never "safe" — only "not currently suspicious".

| Class | Floor | Half-life |
|---|---|---|
| `secret` | 0.30 | ~87h |
| `shell` | 0.25 | ~58h |
| `network_egress` | 0.25 | ~58h |
| `database` | 0.15 | ~35h |
| `config` | 0.10 | ~23h |
| `communication` | 0.10 | ~23h |
| `unknown` | 0.05 | ~35h |
| `source_code` | 0.00 | ~14h |

> This is **ADR-004**, the decision everything else depends on. It costs nothing
> today and would cost roughly 30 days to retrofit. It is pinned by a test
> (`test_agents_table_has_no_risk_column`) so nobody can quietly undo it.

---

## 3. Architecture

### Two planes

```
        DATA PLANE                        CONTROL PLANE
  (must never be slow)                (may be slow, may be down)

   agent                                  ┌──────────────┐
     │                                    │  weaver api  │
     ▼                                    │   FastAPI    │
  ┌──────────────┐    events (async)      │              │
  │ weaver proxy │ ─────────────────────> │  ingest      │
  │   stdio      │                        │  persistence │
  │              │ <───── graph snapshot ─│  dashboard   │
  │  classify    │        trust, rules    │  approvals   │
  │  detect      │                        └──────┬───────┘
  │  score       │                               │
  │  gate 50ms   │                          SQLite / Postgres
  └──────┬───────┘
         ▼
   fs-mcp · db-mcp · shell-mcp · ...
```

The split is non-negotiable. The proxy is on the agent's critical path and must
survive the control plane being down, slow or absent. Events are queued and
shipped asynchronously; if the queue fills, events are **dropped and counted**.
Losing telemetry is the correct failure. Stalling an agent is not.

### Interception

Guardent speaks **MCP over stdio**. Your client launches the Guardent proxy
instead of your MCP servers; the proxy launches the real servers and forwards
everything. The agent sees the same tools under the same names.

```
Copilot / Cursor / Claude Code
      │  .vscode/mcp.json  ──>  <your python> -m weaver proxy
      ▼
  Guardent proxy ───> fs server   ──> your repo
      │          └──> sqlite srv  ──> your database
      │
      └──> events ──> weaver api ──> dashboard on :8080
```

**What it sees:** MCP tool calls, and only those.
**What it does not see:** the agent's built-in tools (VS Code's `readFile`,
`search`, `runInTerminal`), chat traffic, inline completions. This matters more
than it sounds — see [section 10](#10-running-it-against-a-real-project).

### Event shape

Everything downstream depends on this one structure:

```
Event
  event_id, session_id, step, ts
  agent_id
  tool     { server, name }
  resource { uri, class }        <- classify.py decides `class`
  args_digest, args_bytes
  status   { ok | error | denied }
  latency_ms
```

`resource.class` drives every risk floor, decay rate and sensitive-class
detector. Getting it wrong is worse than leaving it `unknown`, so the rules are
conservative and ordered most-sensitive-first: `.env` beats `config`, and a
`curl` command that reaches the network beats the `.env` it is reading.

---

## 4. How a decision gets made

```
tool call
    │
    ├─ classify        URI + tool name -> resource class
    ├─ detect          15 detectors over the session window + baseline
    ├─ score           saturating combination + graph risk + agent trust
    └─ verdict         ALLOW (<0.30) · ASK (<0.65) · BLOCK
```

The score:

```
total = 1.00 × tier1          (deterministic detectors — dominates)
      + 0.15 × node_risk      (graph prior, discounted by confidence)
      + 0.20 × trust_loss     (how much the agent has spent)
```

Detector weights **combine saturatingly**, not additively:

```
tier1 = 1 - ∏(1 - w)
```

Three MEDIUM detectors should raise concern without adding up to the same
certainty as one CRITICAL. Summation makes a pile of weak hints outrank a hard
fact, which is how anomaly products earn their reputation for noise.

Confidence is `n / (n + k)` with k=10 — one observation gives 0.09, ten give
0.50, a hundred give 0.91. A brand-new node cannot carry full weight.

### Explainability is enforced

Every verdict above ALLOW carries a sentence a non-engineer can act on:

> *Blocked: read `file:///app/.env` (secret) at step 2, then external request to
> `https://collector.unknown-host.net/ingest` at step 3.*

**If a decision cannot produce that sentence, it is not allowed to block.**
That rule is in code, not just in this document.

---

## 5. The detectors

15 in total. Three tiers, by the kind of claim they make.

### Tier 1 — deterministic, inline, near-zero false positive

They describe actions no legitimate workflow performs.

| Detector | Weight | Fires when |
|---|---|---|
| `retry_after_denial` | CRITICAL | Denied on a secret, then reaching it another way |
| `sensitive_then_egress` | CRITICAL | Secret read, then an external network call |
| `shell_exfil` | CRITICAL | One shell command that both touches a secret and reaches the network |
| `scope_widening` | HIGH | Sensitivity climbs monotonically, ending somewhere sensitive |
| `fan_out` | HIGH | Two or more never-before-invoked agents recruited |
| `novel_edge` | MEDIUM | First use of a tool, against a sensitive resource |
| `novel_sequence` | LOW | Unseen 3-gram — context, never evidence on its own |

The three CRITICAL detectors **short-circuit to BLOCK** regardless of score. No
amount of accumulated trust argues them down.

**`retry_after_denial` is the signature detector.** An agent gets blocked on
`.env`, then immediately tries `shell://cat /app/.env`. That is the agent
equivalent of port scanning, and per-call policy engines cannot see it, because
they have no memory of the denial.

### Tier 3 — graph-level

Only possible because risk lives on the graph.

| Detector | Weight | Fires when |
|---|---|---|
| `path_risk` | HIGH | 3+ distinct secrets in one session — no single step is odd, the accumulation is |
| `egress_after_broad_access` | HIGH | Outbound call after ranging across 3+ sensitive resources |
| `resource_reputation` | MEDIUM | This resource was flagged before, **possibly by another agent** |
| `blast_radius` | LOW | 5+ distinct sensitive resources reached this session |

`resource_reputation` is the stigmergy payoff: an agent with a clean record
inherits suspicion the moment it touches somewhere others got caught.

### Tier 2 — statistical, needs a baseline

They describe actions that are merely unusual. Welford accumulators, per agent.

| Detector | Weight | Fires when |
|---|---|---|
| `argument_anomaly` | HIGH/LOW | Payload far larger than this tool ever carries |
| `novel_arg_shape` | LOW | Parameters never used before on a sensitive resource |
| `velocity_anomaly` | LOW | Calls arriving far faster than normal, hitting something sensitive |
| `off_hours` | LOW | An hour this agent has never worked in |

### The corroboration floor

**If every detector that fired is LOW, the verdict stays ALLOW.** A pile of weak
hints is not evidence. This rule exists because `novel_sequence` was firing on
`pip install` while the scenario suite reported 0% false positives.

---

## 6. What stops it breaking your agents

A security layer that stalls a coding agent gets uninstalled, and then it
secures nothing. Five rules, all enforced in code and pinned by tests:

1. **Observe mode is the default.** A fresh install decides everything and
   blocks nothing. You look at what it *would* have done first.
2. **Fail open.** Engine crashes, hangs, or exceeds its 50ms budget → the call
   goes through. `failMode: closed` exists and is not the default.
3. **`emit()` never blocks.** Events go on a bounded queue; when it is full they
   are dropped and counted.
4. **No sentence, no block.** An unexplainable block is a bug.
5. **Unanswered approvals deny.** An ASK that nobody answers must not become an
   implicit allow (ADR-017).

### Adaptive trust

Every agent carries a trust score. Signals cost trust fast; clean operation
earns it back slowly. The asymmetry is the correct shape for security: cheap to
lose, expensive to regain.

| Zone | trust | Effect |
|---|---|---|
| 🟢 COLD | > 0.70 | normal operation |
| 🟡 WARM | 0.30–0.70 | sensitive actions need approval |
| 🔴 HOT | < 0.30 | sensitive actions blocked |
| ⛔ quarantined | 3 signals while already HOT | sensitive blocked, everything else needs approval |

Zones gate **sensitive classes only** — ordinary work is never touched by a
trust score (ADR-022). Trust survives a proxy restart (ADR-023). Quarantine is
cleared only by a human.

---

## 7. Repo layout

```
packages/core/weaver_core/     THE ENGINE — zero I/O, zero framework deps
  events.py                    the schema everything depends on
  classify.py                  URI -> resource class
  graph/model.py               nodes, edges, risk floors, decay rates
  risk/model.py                reinforcement, decay, corroboration, trust
  baseline/model.py            per-agent normal (Welford)
  detectors/tier1,2,3.py       the 15 detectors
  policy/score.py              scoring, thresholds, explanations
  policy/rules.py              block / ask / allow rules
  trust/model.py               the temperature state machine
  engine.py                    ties it together

packages/store/                SQLite now, Postgres-portable; versioned migrations
packages/proxy/                MCP interception — the data plane
packages/api/                  FastAPI + the dashboard (one HTML file, no build)
packages/cli/                  the `weaver` command

scenarios/                     the eval suite — attack/ must fire, benign/ must not
redteam/                       real proxy, real MCP servers, throwaway sandbox
scripts/                       dev-setup, doctor, replay
scripts/ops/                   smoke, check-client-config, loadtest, drills, release-check
docs/                          you are here
```

**`weaver_core` imports nothing.** Partly for testability, partly because
project ownership is still open (ADR-007) — a core that imports nothing is a
core that can be carved out.

---

## 8. Install

### The one rule

**Never `pip install weaver`.** That name belongs to an unrelated package on
PyPI. Installing it shadows this project and produces a traceback that mentions
neither (ADR-030, ADR-031).

### Setup

```bash
cd /Volumes/Seagate/AI_Layer
bash scripts/dev-setup.sh          # creates/uses a virtualenv, installs editable
```

PEP 668 makes a virtualenv mandatory on Homebrew and system Pythons. Do **not**
use `--break-system-packages`.

If a virtualenv is already active, the script installs into **that** one and
says so. Note what it prints — that is the interpreter every config below must
name.

```bash
bash scripts/dev-setup.sh --here                    # force the active env
PYTHON=/path/to/python3.12 bash scripts/dev-setup.sh
```

### When something looks wrong

```bash
python3 scripts/doctor.py
```

Reports which interpreter is running, whether it is a virtualenv, whether it is
externally managed, where each package resolved from, whether anything is
shadowing the repo, **and the exact `"command"` line your MCP client config
needs.**

> **On a shared or external drive**, `./.venv` may have been created by a
> different machine — its binaries will not run on yours and it may contain no
> `weaver` at all. `doctor` flags this as `UNUSABLE`. Do not point anything at
> `.venv/bin/*`; use the interpreter `doctor` prints. ADR-032.

---

## 9. Commands

All of these assume the environment from section 8 is active.

### Run it

```bash
weaver api                                  # control plane + dashboard on :8080
weaver api --demo                           # with demo data loaded
weaver proxy --config weaver.config.json --detect --api http://127.0.0.1:8080
weaver doctor                               # diagnose the environment
weaver migrate --check                      # schema state
weaver version
```

The database is `~/.weaver/weaver.db`, so it accumulates across restarts
(ADR-013 — SQLite cannot write on some removable volumes).

### Test it

```bash
pytest                                      # the unit suite
python3 scripts/replay.py --all --quiet     # the eval suite (exit 0 = criteria met)
python3 redteam/run.py                      # real proxy, real MCP servers, real attacks
python3 scripts/ops/drills.py               # failure drills
python3 scripts/ops/loadtest.py             # decisions/sec and latency
ruff check packages scripts tests
mypy packages/core/weaver_core
```

### Check a setup

```bash
python3 scripts/ops/smoke.py --config weaver.config.json
python3 scripts/ops/check-client-config.py <client-config.json>
python3 scripts/ops/check-client-config.py <client-config.json> --fix
python3 scripts/ops/release-check.sh
```

`smoke.py` is the one to remember — see the next section.

### Query it

```bash
curl -s localhost:8080/v1/stats     | python3 -m json.tool
curl -s localhost:8080/v1/sessions  | python3 -m json.tool
curl -s localhost:8080/v1/shadow    | python3 -m json.tool
curl -s localhost:8080/healthz
curl -s localhost:8080/metrics
```

`docs/COMMANDS.md` has the full reference — every flag, every endpoint, every
config key.

---

## 10. Running it against a real project

Two repos, two jobs. Nothing in the guarded project changes except one config
file; Guardent never modifies the project it watches.

| Repo | Role |
|---|---|
| `AI_Layer` | the guard — Guardent itself, the config, the dashboard |
| your project | the guarded — one file: `.vscode/mcp.json` |

### Step 1 — configure the upstreams

`AI_Layer/weaver.config.json` takes the same `mcpServers` block your client
already has. Adopting Guardent is moving a block of JSON, not learning a format.

```json
{
  "mcpServers": {
    "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem",
                                      "/path/to/your/project"]},
    "db": {"command": "npx", "args": ["-y", "mcp-server-sqlite-npx",
                                      "/path/to/your/project/app.db"]}
  },
  "weaver": {
    "mode": "observe",
    "failMode": "open",
    "budgetMs": 50,
    "apiUrl": "http://127.0.0.1:8080",
    "agentId": "copilot-yourproject"
  }
}
```

`agentId` is the baseline key. Keep it stable across sessions or the agent never
builds a baseline.

### Step 2 — point the client at Guardent

`.vscode/mcp.json` **in the guarded project** (VS Code uses `servers`; Cursor
and Claude Code use `mcpServers`):

```json
{"servers": {"weaver": {"type": "stdio",
  "command": "/absolute/path/to/your/env/bin/python",
  "args": ["-m", "weaver", "proxy",
           "--config", "/Volumes/Seagate/AI_Layer/weaver.config.json",
           "--detect", "--api", "http://127.0.0.1:8080"]}}}
```

Name the **interpreter**, not `bin/weaver`. One path to get wrong instead of
two, and no console-script shim that might be missing. MCP clients launch the
server with no PATH and no activated venv, so it must be absolute.

Do not hand-write it — generate and verify it:

```bash
python3 scripts/ops/check-client-config.py /path/to/project/.vscode/mcp.json --fix
python3 scripts/ops/check-client-config.py /path/to/project/.vscode/mcp.json
```

Add the file to that project's `.gitignore`. It contains machine-specific paths.

### Step 3 — prove the wire works, before involving the agent

```bash
weaver api                                   # terminal 1, leave running
python3 scripts/ops/smoke.py --config weaver.config.json   # terminal 2
```

This launches the proxy exactly as a client would, speaks JSON-RPC to it,
**prints the tool list your agent will see**, calls a few read-only tools, and
confirms `/v1/stats` went up.

`PASS` means interception, classification and shipping all work end to end. Do
this first; it separates *"Guardent is broken"* from *"the agent never called
it"*, which look identical from the dashboard.

### Step 4 — the agent

Reload the window, open an **agent** chat (not Ask), and confirm
`Output → MCP: weaver` shows all three lines:

```
weaver: mode=observe fail=open budget=50ms upstreams=2
weaver: detection engine enabled
weaver: shipping events to http://127.0.0.1:8080/v1/events
```

Missing the third means events die in memory and the dashboard stays empty.

### What Guardent can and cannot see

This is the single most common source of confusion.

| The agent does this | Through Guardent? |
|---|---|
| Queries the database — no built-in SQL tool exists | **Yes**, reliably |
| Reads a file through the MCP `fs` server | Sometimes |
| Reads a file with its own `readFile` / `codebase` / `search` | **No** |
| Edits a file, runs a terminal command | **No** — built-in tools |
| Chats, or completes code inline | **No** |

Copilot's built-in file tools are native VS Code, not MCP, and in agent mode it
usually prefers them. **A busy-looking session can produce zero MCP traffic and
an empty dashboard, with nothing wrong anywhere.** Phrase test questions to
require the database, and if you are reading a file, say "using the filesystem
tool".

`docs/TEST-URIS.md` has the full two-repo walkthrough with a day-1 script and a
troubleshooting table.

---

## 11. The dashboard

`http://127.0.0.1:8080` — twelve screens. It refreshes the current view every
five seconds and pauses on a hidden tab.

| Tab | Question | Healthy |
|---|---|---|
| **Overview** | anything happening? | events climbing, blocked 0 |
| **Sessions** | what did the agent do? | every call listed, classes right |
| **Agents** | how much trust is left? | COLD, trust 1.00 |
| **Approvals** | what is waiting on me? | empty |
| **Alerts** | what fired, and why? | empty during ordinary work |
| **Shadow** | what would enforce mode do? | `would_block: 0` |
| **Behavioural web** | what has the agent reached? | risk near the class floors; no dashed crimson edges |
| **Trust zones** | who has how much freedom? | everyone in Safe |
| **Threat signals** | what is the colony suspicious of? | few resources, low strength, decaying |
| **Policies** | what would block, and why? | the three short-circuit detectors, thresholds unchanged |
| **Audit log** | what did this agent do? | searchable; allows included |
| **Settings** | what can this install actually do? | detector errors 0, schema current |

**Agents is the fastest check.** If an agent drifts to WARM during normal work,
detectors are firing that should not be, and **Alerts** says which.

Reading a session timeline: each row is one tool call, with its resource class,
the verdict, the score, and the evidence. A score of `0.00` with an empty
evidence column on a benign read is correct, not a miss.

> **The API has no authentication** (ADR-009). It is local-first by design.
> Never expose port 8080.

---

## 12. Testing

Four layers, each answering a different question.

| Layer | Command | Answers |
|---|---|---|
| Unit | `pytest` | do the pieces behave? |
| Scenarios | `scripts/replay.py --all` | do the detectors fire on attacks and stay quiet on benign traffic? |
| Red team | `redteam/run.py` | does the **real proxy** catch real attacks over real MCP servers? |
| Drills | `scripts/ops/drills.py` | does it fail the way it promises to? |

### Why the red team exists separately

The scenario suite is YAML we wrote; a suite we wrote cannot surprise us. The
red team drives the real proxy through deterministic attack sequences over real
MCP subprocesses in a throwaway sandbox with obviously-fake secrets. It found
`novel_sequence` firing on `pip install`, and `retry_after_denial` being dead
code in production while still passing its tests.

**Do not try to get a coding agent to attack your project.** It often refuses,
and when it complies the sequence is whatever it improvised, so a pass tells you
nothing you can re-verify after a change. The red-team suite is the mechanism.

### When something fires on ordinary work

Capture it before changing any code:

```bash
cat > scenarios/benign/real_01.yaml <<'EOF'
name: real_01
description: Real traffic — <what you were doing>. <detector> fired; it shouldn't.
expect: silent
agent_id: copilot-yourproject
events:
  - {tool: {server: db, name: query}, resource: "<copy from the timeline>"}
EOF

python3 scripts/replay.py scenarios/benign/real_01.yaml   # should FAIL: it fires
# then fix the detector
python3 scripts/replay.py --all --quiet                   # whole suite still green
python3 redteam/run.py                                    # attacks still caught
```

Every false positive becomes a permanent regression test. That is how the suite
stops being something we invented.

---

## 13. Where things stand

Milestones **M0 through M5 are complete**: the engine, the store, the proxy, the
control plane and dashboard, detection tiers 1–3, enforcement with approvals and
policy rules, adaptive trust, packaging, migrations, health endpoints, container
files, load test, failure drills and a release gate.

Last measured:

```
321 tests passing · ruff clean · mypy clean
Scenarios:  8/8 detection · 8/8 on target · 0/12 false positives
Red team:   6/6 caught · 0/4 false positives · 3 known gaps
Drills:     8/8
Load:       10,435 decisions/sec · p99 0.11ms against a 50ms budget
```

Re-measure with the commands in section 9 rather than trusting these numbers.

---

## 14. Honest gaps

| Gap | Where |
|---|---|
| **No real agent traffic has ever been observed** — the big one | ADR-010 |
| No authentication on the API (local-first by design) | ADR-009 |
| Project ownership unresolved | ADR-007 |
| Postgres written but never run against a real server | ADR-028 |
| The wheel has never been built here | ADR-027 |
| Nothing published — the distribution name was blocked | ADR-030, and [Naming](#naming) |
| LangGraph adapter is advisory, cannot block | ADR-025 |
| 3 open red-team gaps: DNS exfil, destructive shell, config tamper | `redteam/README.md` |

### The caveat worth internalising

> The detectors do what they were designed to do. That is **not** the same as
> "detection works."

Three of the four serious bugs found so far were gaps **between** components,
invisible to tests that exercised each component alone:

- the proxy's graph snapshot never refreshed — tests run the engine in-process
- `retry_after_denial` could never fire — tests supply the status by hand
- the CLI package was never written — tests use `pythonpath`, not the install

Each time the suite was green and the thing was broken. The lesson, stated
plainly: **anything that only breaks across a process boundary the tests do not
cross needs an executable check, not a test.** Three now exist —
`scripts/doctor.py`, `scripts/ops/check-client-config.py`, and
`scripts/ops/smoke.py`.

### Before enabling enforce mode

Never switch `mode` to `enforce` until the **Shadow** tab has reported
`would_block: 0` on ordinary work for two clean weeks. Shadow mode is how you
find out what enforcement would have cost you, without paying it.

---

## 15. Working on the code

### Non-negotiables

- `weaver_core` imports nothing outside the standard library
- Risk lives on graph nodes and edges — never a column on `agents`
- Migrations are append-only; never edit a shipped one
- Observe mode stays the default; fail-open stays the default
- No LLM in the decision path (ADR-006)
- A decision that cannot explain itself cannot block

### The ADR record

`docs/DECISIONS.md` holds 33 Architecture Decision Records — every significant
choice, why it was made, and what it cost. Read it before changing anything
structural. If you make a decision that a future reader would otherwise have to
reverse-engineer, add one.

### Where to go next

| Question | File |
|---|---|
| Quick orientation | `docs/PRIMER.md` |
| How do I run it? | `docs/COMMANDS.md` |
| How does it work in detail? | `docs/ARCHITECTURE.md` |
| Why is it built this way? | `docs/DECISIONS.md` |
| What is the plan? | `docs/BUILD_PLAN.md` |
| Running it on my own agents | `docs/DOGFOOD.md` |
| Testing it on my own agents | `docs/TESTING.md` |
| Guarding another repo, end to end | `docs/TEST-URIS.md` |
| Running it for real | `docs/OPERATIONS.md` |
| Attacking it | `redteam/README.md` |

---

## Naming

The product is **Guardent**. The code is not yet.

ADR-030 recorded that `weaver` is taken on PyPI by an unrelated package —
blocking publication, and actively harmful to install (ADR-031: uninstalling the
stranger's package leaves a directory that shadows ours and produces a
traceback mentioning neither project).

`guardent` is **free on PyPI and on npm** (checked 2026-09-27). That unblocks
ADR-030.

**What has not happened yet:** the code still says `weaver` everywhere — the
`weaver` command, `weaver_core`, `weaver_store`, `weaver_proxy`, `weaver_api`,
`~/.weaver/`, and the `"weaver"` key in `weaver.config.json`. Renaming is
mechanical but it touches every client config, every venv with an editable
install, and every `~/.weaver` database path, so it is a deliberate step rather
than something to do mid-test.

Until it happens, read this document as: **Guardent** is the product, `weaver`
is what you type.
