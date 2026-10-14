# Hypothesise — the complete guide

Everything in one place: what Hypothesise does, how it plugs into each AI tool,
and how it works end to end, from an agent's tool call to a verdict, a popup and
a row on the dashboard.

> Package `hypothesise` on PyPI · Source `github.com/siddhant-shahhh/Hypothesise`
> · License Apache-2.0 · Runs entirely on your machine.

---

## Contents

1. [What it is](#1-what-it-is)
2. [Five-minute start](#2-five-minute-start)
3. [How it connects to each AI tool](#3-how-it-connects-to-each-ai-tool)
4. [Architecture, end to end](#4-architecture-end-to-end)
5. [The life of one tool call](#5-the-life-of-one-tool-call)
6. [Detection: how a verdict is reached](#6-detection-how-a-verdict-is-reached)
7. [Observe and enforce modes](#7-observe-and-enforce-modes)
8. [The dashboard](#8-the-dashboard)
9. [Data, privacy and storage](#9-data-privacy-and-storage)
10. [Safety guarantees](#10-safety-guarantees)
11. [Command reference](#11-command-reference)
12. [What it cannot see](#12-what-it-cannot-see)
13. [Troubleshooting](#13-troubleshooting)
14. [Code map and further reading](#14-code-map-and-further-reading)

---

## 1. What it is

AI agents act through tools: they read files, run shell commands, query
databases and call APIs. Permission systems ask **"is this agent allowed to do
that?"**. That question is necessary and not enough: an agent allowed to read
`.env` *and* allowed to make web requests can do both in a row and leak your
keys, and every permission check passes.

Hypothesise asks a different question:

> **Is this the way this agent normally behaves?**

It sits on the path between agents and their tools, learns each agent's normal
behaviour, scores every call against it, and — in enforce mode — blocks
dangerous sequences and tells you why, in one plain sentence.

What makes it different:

- **It judges sequences, not single calls.** "Read a secret, then send data out"
  is caught even though each step alone is allowed.
- **Risk lives on the graph, not on the agent.** Tools, resources and the links
  between them carry risk. If one agent misbehaves around a resource, every
  other agent touching it inherits that caution — like ants leaving a trail
  ("stigmergy").
- **No AI in the decision path.** Verdicts come from deterministic rules,
  statistics and graph math: fast (well under a millisecond), explainable,
  and impossible to prompt-inject.
- **Local-first.** No account, no cloud. Everything stays on your machine.

---

## 2. Five-minute start

```bash
uv tool install hypothesise        # or: pipx install hypothesise / pip install hypothesise
hypothesise init                   # connect the agent apps on this machine (asks first)
hypothesise api                    # dashboard → http://127.0.0.1:8080
```

Restart your agent apps, use them normally, watch the dashboard. Everything
starts in **observe mode** — recorded and scored, nothing blocked. When you
trust what you see:

```bash
hypothesise mode enforce           # block dangerous actions, popup on each block
hypothesise mode observe           # back to safe, any time
```

Undo everything: `hypothesise uninstall`.

---

## 3. How it connects to each AI tool

Hypothesise has **four ways in**, because AI tools expose their actions in four
different ways. `hypothesise init` picks the right one(s) per tool.

| Way in | What it is | Can block? | Sees |
|---|---|---|---|
| **MCP wrap** | each MCP server entry in the app's config is wrapped in place: `python -m hypothesise wrap … -- <original command>` | ✅ yes (enforce mode) | every call to that MCP server |
| **Proxy entry** | one hand-written MCP server (`hypothesise proxy --config …`) that fronts several real servers | ✅ yes, and catches cross-server chains *inline* | every call to the servers in its config |
| **Agent hook** | the agent itself reports each tool call after it runs (Claude Code, Copilot, Cursor) | ❌ observe only | **built-in tools too** — file reads/edits, terminal, web |
| **LangChain / LangGraph callback** | a callback handler added to your graph | advisory (can raise) | tools inside your Python process |

### Support matrix

| Tool | How `init` connects it | Built-in tools seen | MCP tools seen | Agent id on dashboard |
|---|---|---|---|---|
| **Claude Code** | PostToolUse hook in `~/.claude/settings.json` | ✅ all | ✅ (via hook) | `claude-code` |
| **VS Code + GitHub Copilot** | MCP wrap (`mcp.json`, user + workspace) **+** agent hook `~/.copilot/hooks/hypothesise.json` | ✅ (hook; needs `chat.useHooks`) | ✅ (wrap) | `copilot` (built-ins), `vscode` (MCP) |
| **Cursor** | MCP wrap (`~/.cursor/mcp.json`, project `.cursor/mcp.json`) **+** postToolUse hook in `~/.cursor/hooks.json` | ✅ (hook) | ✅ (wrap) | `cursor` |
| **Claude Desktop** | MCP wrap (`claude_desktop_config.json`) | — (no built-in tools that touch your files) | ✅ | `claude-desktop` |
| **Google Antigravity** | MCP wrap (`~/.gemini/config/mcp_config.json`) | ❌ no hooks exist yet | ✅ | `antigravity` |
| **Any other MCP client** (Windsurf, Zed, Continue, custom) | add a proxy entry by hand (below) | ❌ | ✅ | the `agentId` you choose |
| **LangChain / LangGraph apps** | `HypothesiseCallbackHandler` in code | ✅ (your Python tools) | — | your `agent_id` |

### 3.1 MCP wrap (Claude Desktop, Cursor, VS Code, Antigravity)

MCP (Model Context Protocol) is how agents call external tools: the app starts
each MCP server as a child process and talks JSON-RPC to it over stdin/stdout.
`init` rewrites only the **command line** of each stdio server entry:

```json
// before
"github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "…"} }

// after
"github": { "command": "/path/to/python",
            "args": ["-m", "hypothesise", "wrap", "--client", "cursor", "--server", "github",
                     "--", "npx", "-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "…"} }
```

- The server keeps its name, so tool names, prompts and allowlists still work.
- `env` stays in place, so the app still fills in its own variables.
- The original command rides along after `--`, so `uninstall` can restore it
  exactly even if every backup is lost.
- Every file is backed up first (`~/.hypothesise/backups/`) and written atomically.
- Remote (HTTP/SSE) servers are listed but not wrapped yet.
- All servers wrapped for one app share a session (keyed on the app's process),
  so the dashboard sees "secret via server A, upload via server B" as one chain.

### 3.2 Proxy entry (any MCP client; strongest blocking)

One MCP entry fronts several servers listed in `hypothesise.config.json` (same
`mcpServers` shape as Claude/Cursor):

```json
// in the app's MCP config
"hypothesise": { "type": "stdio", "command": "/absolute/path/to/python",
  "args": ["-m", "hypothesise", "proxy", "--config", "/abs/hypothesise.config.json",
           "--detect", "--api", "http://127.0.0.1:8080"] }
```

```json
// hypothesise.config.json
{ "mcpServers": { "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/work"]},
                  "db": {"command": "npx", "args": ["-y", "mcp-server-sqlite-npx", "/work/app.db"]} },
  "hypothesise": { "agentId": "copilot-myproject", "mode": "observe" },
  "rules": [ {"name": "no prod db writes", "action": "block",
              "match": {"resource_uri": "postgresql://prod*", "action": "write"},
              "reason": "production is read-only for agents"} ] }
```

One engine sees every upstream, so cross-server chains are blocked **live**.
This is also the only setup that supports operator **policy rules** today.
Works with any MCP client that can launch a stdio server.

### 3.3 Agent hooks (Claude Code, Copilot, Cursor)

Built-in tools never touch MCP, so wrapping can't see them. Three agents offer
hooks — a command they run after every tool call, with the call as JSON:

| Agent | Hook file | Event | Session field | Result field |
|---|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | `PostToolUse` | `session_id` | `tool_response` |
| VS Code Copilot | `~/.copilot/hooks/hypothesise.json` | `PostToolUse` | `session_id` | `tool_response` |
| Cursor | `~/.cursor/hooks.json` | `postToolUse` | `conversation_id` | `tool_output` |

The hook (`hypothesise hook <client>`) is deliberately tiny: it maps the tool
name to a verb (`Read`/`view`/`read_file` → read, `Bash`/`run_terminal_cmd` →
execute, `Edit`/`edit_file` → write, `WebFetch`/`fetch_webpage` → egress…),
classifies the target, appends one line to the local spool and exits 0 — about
55 ms, standard library only, never blocks and never fails the agent. Copilot's
and Cursor's MCP calls are skipped by their hooks because the wrapped servers
already record them. Hooks run *after* the tool, so they observe; they don't block.

### 3.4 LangChain / LangGraph

For agents you write yourself in Python, add the callback handler — no MCP needed:

```python
from hypothesise_langgraph import HypothesiseCallbackHandler, ToolBlocked

handler = HypothesiseCallbackHandler(agent_id="research-bot")   # records to the local spool
result = graph.invoke(inputs, config={"callbacks": [handler]})

# Optional: stop the graph when the engine says BLOCK
strict = HypothesiseCallbackHandler(agent_id="research-bot", raise_on_block=True)
try:
    graph.invoke(inputs, config={"callbacks": [strict]})
except ToolBlocked as e:
    print("blocked:", e.sentence)
```

Events land on the same dashboard as everything else. LangChain fires the
callback when the tool is already committed, so `raise_on_block` stops the
*graph* but the call may have run — use the MCP proxy where blocking must be
guaranteed. The handler imports nothing from LangChain, so it also works with
any framework that can call `on_tool_start` / `on_tool_end` / `on_tool_error`.

### 3.5 Anything else

- **CrewAI, AutoGen, OpenAI Agents SDK:** if their tools are MCP servers, use a
  proxy entry; if they're Python functions, call the callback handler's three
  methods around each tool call.
- **Remote MCP servers (HTTP/SSE):** not intercepted yet.

---

## 4. Architecture, end to end

```
 ┌──────────── AI tools ─────────────┐        ┌──────────── Hypothesise ─────────────────────────────┐
 │                                   │        │                                                      │
 │ Claude Desktop / Cursor / VS Code │──MCP──▶│ DATA PLANE  (one process per wrapped server / proxy) │
 │ Antigravity / any MCP client      │ stdio  │  wrap/proxy ─▶ classify ─▶ engine ─▶ gate (50 ms)    │──▶ real MCP server
 │                                   │        │     │                         │  allow/ask/block     │    (fs, db, github…)
 │ Claude Code / Copilot / Cursor    │─hook──▶│  hook (observe, ~55 ms) ──┐    │  popup on block      │
 │   built-in tools                  │        │                           ▼    ▼                      │
 │                                   │        │        bounded queue ─▶ shipper ──HTTP──┐             │
 │ LangChain / LangGraph app         │─cb────▶│  callback handler ─────▶ spool (JSONL) ◀┘ (fallback)  │
 └───────────────────────────────────┘        │                             │                        │
                                              │ CONTROL PLANE  `hypothesise api`  (127.0.0.1:8080)   │
                                              │  ingest (spool reader + POST /v1/events)             │
                                              │   └▶ same engine ▶ store (SQLite) ▶ REST ▶ dashboard │
                                              │  approvals (ASK) · graph/trust sync back to proxies   │
                                              └──────────────────────────────────────────────────────┘
```

### Two planes

| | Data plane | Control plane |
|---|---|---|
| Runs as | `hypothesise wrap` / `proxy` (child of the agent app), `hypothesise hook` | `hypothesise api` |
| Job | intercept, decide in ≤ 50 ms, forward or block, emit | ingest, persist, correlate, serve dashboard, approvals |
| If it dies | the agent's tool still works (fail open / passthrough) | events wait in the spool; nothing is lost |
| Needs network | no | localhost only |

### Packages

| Package | Role | Depends on |
|---|---|---|
| `hypothesise_core` | the engine: event schema, classifier, graph & risk math, baselines, detectors, scoring, trust, policy rules | **nothing** (pure Python, no I/O) |
| `hypothesise_proxy` | MCP data plane: JSON-RPC protocol, upstream servers, gate, emitter, shipper, spool, settings, popup | core |
| `hypothesise_store` | SQLAlchemy Core schema, migrations, repository, ingest | core, SQLAlchemy |
| `hypothesise_api` | FastAPI control plane + single-file dashboard (no build step) | core, store, proxy |
| `hypothesise_langgraph` | LangChain/LangGraph callback handler | core |
| `hypothesise` | the CLI: `init`, `mode`, `status`, `api`, `wrap`, `hook`, `proxy`, `uninstall`, `doctor`, `migrate` | all |

One install (`hypothesise` on PyPI) ships all six. Four runtime dependencies:
SQLAlchemy, PyYAML, FastAPI, Uvicorn.

### Event delivery: never block, never lose

Every intercepted call becomes an **event**. The proxy hands it to a bounded
in-memory queue with a non-blocking `emit()` (if the queue is ever full, the
event is dropped and counted — the agent is never made to wait). A background
task then delivers it:

- **With `--api` / enforce mode:** POSTed to the dashboard live. If the
  dashboard is down, the batch goes to the spool instead of being dropped.
- **Otherwise:** appended to the spool, `~/.hypothesise/spool/events-YYYYMMDD.jsonl`
  (one JSON line per event, under a short cross-process file lock; capped at 200 MB).

The dashboard reads the spool on start and every 2 seconds, through the same
`ingest()` as the live path — so an event is judged identically whichever road
it took. Offsets are committed only after a successful write (a crash re-reads,
never loses); duplicates are skipped by event id.

---

## 5. The life of one tool call

Example: Cursor's agent calls the `read_file` tool on a wrapped `fs` server.

1. **Cursor** sends `tools/call {name: "read_file", arguments: {path: "/app/.env"}}`
   to what it thinks is the `fs` server — actually `hypothesise wrap`.
2. **Extract & classify.** The resource URI is pulled from the arguments
   (`path`, `url`, `command`, `query`…): `/app/.env`. The classifier maps it to a
   class — here `secret` (rules: `.env`, `.aws/`, `id_rsa`, `*.pem`, `credential`…).
   The tool name gives the action — `read`. Only the arguments' *shape* (keys,
   size, a hash) is kept, never their content.
3. **Engine.** The event joins this session's window. Detectors run against the
   window, the agent's baseline and the graph (section 6). A score and verdict
   come out, with a plain-English sentence.
4. **Gate.** Runs the engine inside a 50 ms budget. Timeout or crash → **fail
   open** (allowed, flagged). In **observe** mode any non-ALLOW verdict is
   recorded as "would have blocked" and the call proceeds.
5. **Verdict in enforce mode.**
   - **ALLOW** → forwarded to the real server; the reply goes back unchanged.
   - **ASK** → held; a request appears on the dashboard's *Approvals* tab; no
     answer within 120 s means **deny**.
   - **BLOCK** → not forwarded. Cursor gets a structured error with the sentence
     (`code -32001`, *"Blocked: read /app/.env (secret) at step 2, then external
     request to … at step 3"*) and **a popup appears on screen**.
6. **Outcome.** The real server's reply (or error) is recorded: OK, ERROR, or
   DENIED (a refusal — which arms the `retry_after_denial` detector).
7. **Emit → deliver → ingest → store → dashboard**, as in section 4.
8. **Graph & trust update.** Nodes (agent, tool, resource) and edges gain
   observations; detectors that fired reinforce the risk of the resources involved
   and cost the agent trust. The proxy pulls the updated graph back every 30 s,
   so the next agent to touch that resource meets the raised risk.

---

## 6. Detection: how a verdict is reached

### 6.1 Detectors

**Tier 1 — deterministic sequences** (near-zero false positives):

| Detector | Fires when |
|---|---|
| `sensitive_then_egress` ⛔ | a secret is read, then an external network call follows |
| `retry_after_denial` ⛔ | access to a secret is refused, then the agent reaches it another way (e.g. `cat .env` after `read_file .env` was denied) |
| `shell_exfil` ⛔ | a shell command that both touches a secret and sends data out (`curl -d @.env …`) |
| `scope_widening` | sensitivity climbs step by step, ending somewhere sensitive |
| `fan_out` | two or more never-before-invoked agents are recruited |
| `novel_edge` | first use of a tool against a sensitive resource |
| `novel_sequence` | a never-seen 3-step pattern (stronger if it touches something sensitive) |

⛔ = **critical: always BLOCK**, regardless of score or trust; no allow-rule can override.

**Tier 2 — statistical, against the agent's own baseline:**
`argument_anomaly` (payload far larger than usual), `novel_arg_shape`
(new parameters on a sensitive resource), `velocity_anomaly` (calls arriving
much faster than normal), `off_hours` (an hour this agent has never worked).

**Tier 3 — graph-level** (only possible because risk lives on the graph):
`path_risk` (3+ distinct secrets in one session), `egress_after_broad_access`
(outbound call after ranging over 3+ sensitive resources), `resource_reputation`
(this resource was flagged before — possibly by *another* agent),
`blast_radius` (5+ sensitive resources reached in one session).

### 6.2 Score and verdict

```
score = 1.00 × tier-1 signal strength
      + 0.15 × riskiest node touched (decayed, confidence-weighted)
      + 0.20 × (1 − agent trust)                     capped at 1.0

critical detector fired  or  score ≥ 0.65  →  BLOCK
score ≥ 0.30                               →  ASK
otherwise                                  →  ALLOW
```

Every non-ALLOW verdict carries a sentence. **No sentence, no block** — a block
without an explanation is suppressed in code.

### 6.3 Risk on the graph

- **Floors:** each resource class has a minimum risk it never decays below —
  secret 0.30, shell 0.25, network egress 0.25, database 0.15, config 0.10,
  communication 0.10, unknown 0.05, source code 0.
- **Decay:** suspicion cools exponentially toward the floor when nothing bad
  happens — slowly for secrets (~87 h half-life), fast for source (~14 h).
- **Corroboration:** a signal's weight grows with the number of *distinct*
  agents reporting it: `weight × (1 + ln(distinct reporters))`. Three agents
  flagging a resource counts far more than one agent flagging it three times —
  one looping agent can't manufacture its own corroboration.

### 6.4 Adaptive trust (COLD → WARM → HOT)

Each agent has a trust score from 0 to 1. Signals cost trust fast (one critical
signal ≈ a quarter of it); clean operation earns it back slowly (≈ a day).

| Zone | Trust | Effect |
|---|---|---|
| COLD | ≥ 0.70 | normal |
| WARM | 0.30 – 0.70 | touching a secret, shell or database needs approval (ASK) |
| HOT | < 0.30 | sensitive access blocked; 3 more signals → session quarantined |

Zones gate sensitive resources; they never make detectors more sensitive across
the board. You can release or restrict an agent from the dashboard.

### 6.5 Policy rules (proxy entry)

Explicit operator intent on top of behaviour. Match fields: `agent_id`,
`tool_server`, `tool_name`, `tool`, `resource_uri`, `resource_class`, `action`,
`status` (glob patterns). `block`/`ask` rules can only raise a verdict; `allow`
rules can only lower one, and never past a critical detector.

---

## 7. Observe and enforce modes

| | Observe (default) | Enforce |
|---|---|---|
| Records & scores every call | ✅ | ✅ |
| Shows alerts / "would block" | ✅ (Shadow tab) | ✅ |
| Stops dangerous calls | ❌ | ✅ (MCP wrap, proxy entry) |
| ASK → human approval | ❌ | ✅ on the dashboard; unanswered = denied |
| Popup on block | — | ✅ (can be turned off) |
| Agent hooks | observe | still observe |

```bash
hypothesise mode                         # show current mode
hypothesise mode enforce                 # switch every connected MCP server, no restart
hypothesise mode enforce --no-popup      # enforce without popups
hypothesise mode enforce --api http://127.0.0.1:8765   # dashboard on another port
hypothesise mode observe                 # back to safe
```

How it works: `mode` writes `~/.hypothesise/settings.json`. Every running
wrapped server and proxy re-reads it every 2 seconds and switches on the next
tool call — nothing to restart. Precedence: `HYPOTHESISE_MODE` environment
variable > `hypothesise mode` > the proxy config file > observe.

**Before enforcing:** keep `hypothesise api` running (ASK needs it — the command
warns if it isn't), and check the **Shadow** tab: every "would block" there on
your own ordinary work is a block you'll get in enforce mode. The recommended
path is two clean weeks of observe first.

**The popup:** when enforce mode blocks a call (or a human denies one), a
notification appears — macOS Notification Center, a Windows toast (message-box
fallback), or `notify-send` on Linux — with the agent, the tool and the exact
reason. At most one every few seconds; extra blocks are counted into the next
one. It runs as a detached process, so it can never delay the agent.

---

## 8. The dashboard

`hypothesise api` → `http://127.0.0.1:8080` (if the port is taken — common on
Windows — it picks the next free one and says so). Works offline; everything is
bundled. Refreshes every 5 seconds.

| Tab | Shows |
|---|---|
| Overview | calls today, blocks, latency vs the 50 ms budget, detectors firing, riskiest resources |
| Sessions | every session; click one for its step-by-step timeline with verdicts and evidence |
| Agents | each agent's trust, zone and behaviour; release / restrict |
| Behavioural web | the live graph of agents, tools and resources, coloured by risk |
| Trust zones | COLD / WARM / HOT at a glance |
| Threat signals | corroborated suspicion per resource (how many agents reported it) |
| Alerts | every non-ALLOW decision with its sentence |
| Approvals | pending ASK requests — approve or deny |
| Shadow | what enforce mode *would* have done while in observe |
| Policies | thresholds, critical detectors, rules in force |
| Audit log | every decision, searchable |
| Settings | integrations, version, health |

`hypothesise api --demo` shows sample attacks in a separate demo database; your
real data is untouched.

---

## 9. Data, privacy and storage

- **Nothing leaves your machine.** No telemetry, no cloud, no account.
- **Per user, per machine.** Data lives in `~/.hypothesise/` (move it with
  `HYPOTHESISE_HOME`). Another person or another OS user has their own.
- **Arguments are not stored** — only their shape (keys, size, a hash), the
  resource URI and the verdict.

| Path | What |
|---|---|
| `~/.hypothesise/hypothesise.db` | the dashboard database (SQLite) |
| `~/.hypothesise/spool/` | events waiting for the dashboard |
| `~/.hypothesise/settings.json` | `hypothesise mode` choices |
| `~/.hypothesise/backups/` | every app config before `init`/`uninstall` changed it |
| `~/.hypothesise/demo.db` | `--demo` sample data |

**No login on the dashboard** — it listens on 127.0.0.1 only. Never expose
port 8080 to a network.

---

## 10. Safety guarantees

A security tool that breaks your agent gets uninstalled, and then protects
nothing. These hold in code and are exercised by failure drills on every release:

1. **Observe is the default** everywhere.
2. **Fail open:** engine slow (> 50 ms) or crashing → the call goes through, flagged.
3. **Passthrough:** if a wrapped server can't start Hypothesise, it runs the
   original server unwrapped.
4. **Non-blocking emit:** telemetry never makes the agent wait; a full queue drops.
5. **Bounded everything:** queue size, spool size, retries.
6. **No sentence, no block.**
7. **Critical detectors can't be allow-listed away.**
8. **Hooks always exit 0** and never print anything that changes the agent's behaviour.
9. **Reversible:** `uninstall` restores every config exactly.

---

## 11. Command reference

| Command | Does |
|---|---|
| `hypothesise init [--client APP] [--project DIR] [--dry-run] [--yes]` | connect agent apps (asks first, backs up, idempotent; re-run repairs outdated hooks) |
| `hypothesise status` | what's connected, events waiting, events recorded |
| `hypothesise mode [observe\|enforce] [--no-popup] [--api URL]` | show or switch the mode for every connected agent |
| `hypothesise api [--port N] [--demo]` | dashboard (alias `dashboard`) |
| `hypothesise uninstall [--client APP]` | restore every config `init` changed |
| `hypothesise proxy --config FILE [--detect] [--api URL]` | run a proxy entry (normally launched by the app) |
| `hypothesise wrap --client C --server S -- CMD…` | one wrapped server (written by `init`) |
| `hypothesise hook claude-code\|vscode\|cursor` | hook receiver (written by `init`) |
| `hypothesise doctor` | diagnose the Python environment |
| `hypothesise migrate [--check]` | bring the database schema up to date |
| `hypothesise version` | print the version |

Environment: `HYPOTHESISE_HOME`, `HYPOTHESISE_MODE`, `HYPOTHESISE_API_URL`,
`HYPOTHESISE_NOTIFY=0`, `HYPOTHESISE_DECISION_BUDGET_MS`, `HYPOTHESISE_FAIL_MODE`.

---

## 12. What it cannot see

- **Antigravity's built-in tools** (it has no hooks) — only its MCP servers.
- **Remote (HTTP/SSE) MCP servers** — listed by `init`, not intercepted yet.
- **Inline code completion and chat text** — only tool calls are actions.
- **Blocking built-in tools** — hooks run after the tool, so built-ins are
  observed, not stopped.
- **Cross-server chains are blocked live only with a proxy entry**; with wrapped
  servers they're caught on the dashboard.
- **Policy rules** apply in proxy entries only.

Known open red-team gaps: data leaving via DNS lookups, destructive shell
commands without a secret involved, quietly rewriting config files.

---

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| Nothing on the dashboard | `hypothesise status`: is the app connected? Run `hypothesise init`, then fully restart the app |
| Copilot's file reads missing | Settings → `chat.useHooks` on; Reload Window; `status` shows "VS Code Copilot hook connected" |
| Windows: hook installed but no events | upgrade (`pip install -U hypothesise`) and re-run `hypothesise init` — repairs the quoted command PowerShell rejected in 0.1.0a1 |
| "access to a socket … forbidden" / port busy | it now moves to a free port automatically; or `hypothesise api --port 9123` |
| Sample sessions (`coding-agent-prod`…) in your data | an old `--demo` run (before 0.1.0a2) — delete `~/.hypothesise/hypothesise.db` |
| Enforce mode not blocking | `hypothesise mode` shows `enforce`? Is the call an MCP call (hooks never block)? |
| Every ASK is denied | the dashboard isn't running — start `hypothesise api` |
| No popup | `hypothesise mode` shows popups on? macOS: allow notifications for Script Editor; Linux: install `notify-send` |
| Wrong Python in configs | `hypothesise doctor`; install with `uv tool install` / `pipx`, not `uvx` |

---

## 14. Code map and further reading

```
packages/core/hypothesise_core/   events · classify · graph · risk · baseline · detectors · policy · trust · engine
packages/proxy/hypothesise_proxy/ server · upstream · protocol · gate · emit · shipper · spool · settings · notify · sync · approval
packages/store/hypothesise_store/ schema · migrate · repo · ingest
packages/api/hypothesise_api/     app (REST) · static/index.html (dashboard) · demo
packages/adapters/langgraph/      HypothesiseCallbackHandler
packages/cli/hypothesise/         __main__ (CLI) · clients (init/uninstall/status) · hook · mode · doctor
scenarios/                        attack/ must fire · benign/ must not
redteam/                          real proxy, real MCP servers, fake secrets
```

| Document | For |
|---|---|
| [PRIMER](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/PRIMER.md) | the project in ten minutes |
| [ARCHITECTURE](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/ARCHITECTURE.md) | deep design: graph schema, risk math |
| [DECISIONS](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/DECISIONS.md) | why everything is the way it is (ADRs) |
| [OPERATIONS](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/OPERATIONS.md) | running and releasing |
| [TEST-URIS](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/TEST-URIS.md) | a real-project test walkthrough |
