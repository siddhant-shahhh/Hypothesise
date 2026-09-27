# Guarding `uris` with Weaver — two-repo setup

Two codebases, two jobs:

| Repo | Role | What lives here |
|---|---|---|
| **AI_Layer** | the guard | Weaver itself, the config, the dashboard |
| **uris** | the guarded | one file: `.vscode/mcp.json` pointing Copilot at Weaver |

Nothing in `uris` changes except that one config file. Weaver never modifies the
project it watches.

```
Copilot (in uris)
      │  .vscode/mcp.json  ──>  <your python> -m weaver proxy
      ▼
  Weaver proxy ───> fs server   ──> /Volumes/Seagate/uris
      │        └──> sqlite srv  ──> uris/backend/uris.db
      │
      └──> events ──> weaver api ──> dashboard on :8080
```

---

## In AI_Layer — start the guard

```bash
cd /Volumes/Seagate/AI_Layer
source /path/to/your/env/bin/activate   # the env you installed into
weaver doctor                           # confirms it is the right one
weaver api                              # leave running; 127.0.0.1:8080
```

> **Not `.venv`.** This repo lives on a shared drive, and `AI_Layer/.venv` was
> created by a Linux VM: its binaries cannot execute on macOS and it contains no
> `weaver` at all. `dev-setup.sh` on the Mac installed into whichever
> virtualenv was already active instead — `weaver doctor` prints which. Use
> that one everywhere below. ADR-032.

`weaver.config.json` is already written and points at `uris`:

- **fs** — filesystem server scoped to `/Volumes/Seagate/uris`
- **db** — SQLite server on `uris/backend/uris.db`
- `agentId: copilot-uris` — the baseline key; keep it stable across sessions
- `mode: observe` — nothing is ever blocked

## In uris — point Copilot at the guard

Already written: `uris/.vscode/mcp.json`. VS Code does not inherit an activated
virtualenv, so the entry has to name an absolute path — and it names the
**interpreter**, not a console script:

```json
{"servers": {"weaver": {"type": "stdio",
  "command": "/Users/<you>/<your-env>/bin/python3",
  "args": ["-m", "weaver", "proxy",
           "--config", "/Volumes/Seagate/AI_Layer/weaver.config.json",
           "--detect", "--api", "http://127.0.0.1:8080"]}}}
```

`python3 -m weaver` rather than `bin/weaver` because there is exactly one path
to get wrong instead of two, and `-m` needs no console-script shim.

**Make it exactly right rather than editing it by hand.** From the environment
that has Weaver installed:

```bash
python3 scripts/ops/check-client-config.py /Volumes/Seagate/uris/.vscode/mcp.json --fix
python3 scripts/ops/check-client-config.py /Volumes/Seagate/uris/.vscode/mcp.json
```

`--fix` rewrites the entry to use the interpreter running it, which is by
definition the one that can import `weaver`. The second run verifies: the
command exists, is executable, is built for this platform, actually answers
`weaver version`, the `--config` file parses, and something is listening on
`:8080`. Do this before touching VS Code — a wrong path shows up there only as
*"server exited"* with no other detail.

Added to `uris/.gitignore` — it contains machine-specific paths and must not be
committed to a shared repo.

Then in VS Code, open **`uris`** as the workspace and reload the window
(`Cmd+Shift+P` → *Developer: Reload Window*). Open a Copilot **agent** chat; it
should list tools from a server called `weaver`.

### Check the wiring before doing anything else

VS Code **Output → MCP** should show:

```
weaver: mode=observe fail=open budget=50ms upstreams=2
weaver: detection engine enabled
weaver: shipping events to http://127.0.0.1:8080/v1/events
```

All three lines matter. Missing the third means events are dying in memory and
the dashboard will stay empty.

---

## Step 0 — prove the wire works, without Copilot

Do this **before** asking Copilot anything. It takes a minute, and it separates
the two failures that look identical from the dashboard:

- Weaver is broken, or
- Weaver is fine and Copilot simply never called it

```bash
cd /Volumes/Seagate/AI_Layer
python3 scripts/ops/smoke.py --config weaver.config.json
```

It launches the proxy exactly as VS Code would, speaks JSON-RPC to it over
stdio, prints the tool list your agent will see, calls a few read-only tools,
and checks that `/v1/stats` went up.

`PASS` means interception, classification and shipping all work end to end —
any emptiness after that is Copilot's choice, not a bug. `FAIL` prints the
proxy's stderr and names the stage that broke.

---

## What Weaver can and cannot see

Read this before reading the dashboard. It explains most "nothing happened"
moments.

Weaver sits on **one wire**: MCP tool calls from Copilot to the `fs` and `db`
servers. That is all it sees.

| Copilot does this | Through Weaver? |
|---|---|
| Queries the database — it has no built-in SQL tool | **Yes**, reliably |
| Reads a file through the MCP `fs` server | Sometimes |
| Reads a file with its own `readFile` / `codebase` / `search` | **No** |
| Edits a file, runs a terminal command | **No** — built-in tools |
| Chats, or completes code inline | **No** |

Copilot's built-in file tools are native VS Code, not MCP, and in agent mode it
usually prefers them. So a busy-looking session can produce zero MCP traffic and
an empty dashboard with nothing wrong anywhere.

**The database is the reliable test.** There is no built-in SQL tool, so a
question that genuinely needs data has to go through the `db` server — and
therefore through Weaver.

---

## Day 1 — can it see? (15 minutes)

Be in a Copilot **agent** chat (not Ask), with the `weaver` server started.

### These must produce events

```
1. Query the database and tell me what tables exist and how many rows each has.

2. Run a query against the database to show the schema of the users table.

3. Query the database for the five most recent rows in any table that has a
   timestamp column.
```

Saying *"query the database"* matters. Asked "what tables are there?", Copilot
will often answer from a models file it already has open, and never touch the
database at all.

### These may or may not — and that is fine

```
4. Using the filesystem tool, read backend/.env.example and list the variables.

5. Using the filesystem tool, list the files under backend/app.
```

Naming the tool pushes Copilot towards MCP, but it is a hint, not a guarantee.
Nothing appearing here is not a Weaver failure.

Then **Sessions** → click the session.

| Check | Pass |
|---|---|
| Every call Copilot made is listed | `step` 0,1,2… no gaps |
| Agent | `copilot-uris` |
| Database queries | class **`database`** |
| `.env.example`, if it appears | class **`secret`** — it matches the secret pattern, which is correct and worth confirming |
| `main.py`, route files | class **`source_code`** |
| Verdicts | all **ALLOW** |

```bash
curl -s localhost:8080/v1/stats | python3 -m json.tool
```

`latency_ms.measured > 0` is the proof real traffic reached the gate. It stays 0
for demo data.

**If anything shows class `unknown`** — that is the most likely first finding,
and it has two causes. Either the resource URI was never extracted from that
server's argument shape (the RESOURCE column is empty or wrong) — a fix in
`extract_resource_uri` — or the URI is right but no rule matches it — a fix in
`classify_resource`. The first real run turned up the second kind:
`.vscode/mcp.json` read as `unknown`, because the CONFIG rule had no JSON in it
at all. Copy the resource from the timeline and add a case to
`tests/test_classify.py` before touching the pattern.

### Still nothing in the dashboard

Work down this list. It is ordered by how often each one turns out to be the
answer.

| Check | How | If wrong |
|---|---|---|
| Did `weaver` actually start? | `.vscode/mcp.json` shows `Running`, not `▷ Start` | Click **Start**. A config change needs the server restarted, not just a window reload |
| Is it Copilot you are typing into? | Other chat extensions ignore `.vscode/mcp.json` entirely | Open Copilot Chat (`Ctrl+Cmd+I`) |
| Is the chat in **Agent** mode? | Mode dropdown in the chat box | Ask mode never calls tools |
| Are the weaver tools ticked? | 🛠 tools icon in the chat box | Enable the `weaver` group |
| Is `weaver api` running? | `curl -s localhost:8080/healthz` | Start it — without it every event is dropped |
| Does the wire work at all? | `python3 scripts/ops/smoke.py` | Step 0 above |
| Did Copilot just use its own tools? | The chat shows which tool ran at each step | Expected. Ask a database question instead |

---

## Week 1 — is it quiet? (the actual point)

**Work on `uris` exactly as you normally would.** Ask Copilot to explain code,
fix a failing test, add an endpoint, query the database while debugging. Do not
try to be interesting.

Once a day:

```bash
curl -s localhost:8080/v1/stats \
  | python -c "import json,sys; d=json.load(sys.stdin); print(d['events'], d['verdicts'], d['latency_ms'])"
```

| Metric | Target |
|---|---|
| `verdicts.block` | **0** |
| `verdicts.ask` | 0, or very close |
| `events` | hundreds, then thousands |
| `latency_ms.failed_open` | **0** |

**Shadow** tab: every row there is a false positive, because you are not
attacking your own project.

This is the number no suite we wrote can produce. It reported 0% false positives
for weeks while `novel_sequence` was firing on `pip install`.

### When something fires on ordinary work

Back in **AI_Layer**, capture it before changing any code:

```bash
cd /Volumes/Seagate/AI_Layer
cat > scenarios/benign/uris_real_01.yaml <<'EOF'
name: uris_real_01
description: Real Copilot traffic on uris — <what you were doing>. <detector> fired; it shouldn't.
expect: silent
agent_id: copilot-uris
events:
  - {tool: {server: db, name: query}, resource: "<copy from the timeline>"}
EOF

python scripts/replay.py scenarios/benign/uris_real_01.yaml   # should FAIL: it fires
# then fix the detector
python scripts/replay.py --all --quiet                        # whole suite still green
python redteam/run.py                                         # attacks still caught
```

That way every false positive becomes a permanent regression test, and the suite
stops being something we invented.

---

## Attack coverage — stays in AI_Layer

```bash
cd /Volumes/Seagate/AI_Layer
python redteam/run.py
```

Do **not** try to get Copilot to attack `uris`. Two reasons: it often refuses, and
when it complies the sequence is whatever it improvised, so a pass tells you
nothing you can re-verify after a change.

The red-team suite drives the real proxy through 12 deterministic attack
sequences over real MCP subprocesses in a throwaway sandbox with fake secrets.
It is what found `novel_sequence` firing on `pip install` and
`retry_after_denial` being dead code in production.

**Division of labour:** the red team covers attacks, repeatably. The `uris` run
covers false positives, which is the thing we cannot invent — ordinary traffic
contains sequences nobody thought of.

---

## Where to look, by tab

| Tab | Question | Healthy |
|---|---|---|
| **Overview** | anything happening? | events climbing, `blocked` 0 |
| **Sessions** | what did Copilot do? | every call, classes right |
| **Agents** | how much trust is left? | `copilot-uris` COLD, trust 1.00 |
| **Alerts** | what fired, and why? | empty |
| **Shadow** | what would enforce do? | `would_block: 0` |
| **Graph** | what has it reached? | uris file + db nodes, risk near floors |

**Agents** is the fastest check. If `copilot-uris` drifts to WARM during normal
work, detectors are firing that shouldn't, and **Alerts** says which.

Weekly:

```bash
curl -s localhost:8080/v1/detectors | python -m json.tool
```

`errors` must stay `{}`. Non-empty means a detector is crashing and therefore
matching nothing — how `scope_widening` stayed broken for days while every test
passed.

---

## Two things about this drive

**SQLite writes fail on `/Volumes/Seagate`.** Reads are fine, so Copilot's
queries against `uris.db` work. A write may fail with `disk I/O error` — that is
the drive, not Weaver. Arguably convenient: no risk of the test damaging your dev
database.

**`uris` has no real `.env`,** only `.env.example`, and `uris.db` holds 1 user and
a few notification rows. Nothing sensitive is in scope. If you later add a real
`.env`, remember the fs server is scoped to the whole repo and Copilot will be
able to read it — that is true with or without Weaver.

## Do not

- Set `mode: enforce` until Shadow has been clean for two weeks
- Commit `uris/.vscode/mcp.json` (gitignored) or `AI_Layer/weaver.config.json`
- Expose port 8080 — there is no authentication yet (ADR-009)

## After a week, send me

```bash
curl -s localhost:8080/v1/stats  > /tmp/uris-stats.json
curl -s localhost:8080/v1/shadow > /tmp/uris-shadow.json
```

Those two, plus a sentence about what you were doing when anything fired.

| Result | Meaning |
|---|---|
| `would_block: 0`, `would_ask: 0` | usable. Try `enforce` with one narrow rule. |
| A few `would_ask` | tune those detectors — benign scenario first. |
| Any `would_block` on your own work | **do not enable enforce.** This is the finding worth having. |

## Teardown

```bash
rm -f /Volumes/Seagate/uris/.vscode/mcp.json     # in uris
rm -f /Volumes/Seagate/AI_Layer/weaver.config.json
rm -rf ~/.weaver                                  # also deletes accumulated history
```
