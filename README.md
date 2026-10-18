# Hypothesise

**A behavioural security layer for AI agents.** Hypothesise watches every tool
call your agents make — reading files, running commands, querying databases,
calling APIs — learns what normal looks like for each agent, and flags the
moment one starts behaving differently.

Permission systems ask *"is this agent allowed to do that?"* Hypothesise asks
*"is this normal for this agent?"* An agent allowed to read `.env` and allowed
to make web requests can do both in a row and leak your keys; every permission
check passes. Hypothesise sees the chain.

Runs entirely on your machine. No account, no cloud, nothing leaves your computer.

---

## Install

Needs Python 3.10+ on macOS, Windows or Linux. Install it as a command-line
tool, in its own environment:

```bash
uv tool install hypothesise        # recommended
# or
pipx install hypothesise
# or, inside a virtualenv you manage
pip install hypothesise
```

> Mind the spelling: **hypothesis**e. `hypothesis` (a testing library) and
> `hypothesize` (a statistics package) are unrelated projects.

## Upgrade

```bash
uv tool upgrade hypothesise     # or: pipx upgrade hypothesise · pip install -U hypothesise
hypothesise init                # repairs hooks for the new version; "nothing to change" if fine
```

Then fully restart your agent apps so they load the new code.

## Uninstall

```bash
hypothesise uninstall           # restore every config init changed (backed up first)
uv tool uninstall hypothesise   # or: pipx uninstall hypothesise · pip uninstall hypothesise
rm -rf ~/.hypothesise           # optional: also delete all recorded data
```

## Connect your agents

```bash
hypothesise init
```

`init` finds the agent apps on your machine, shows exactly what it will change,
and asks before changing anything:

| App | How it is connected | What it sees |
|---|---|---|
| **Claude Code** | a hook in `~/.claude/settings.json` | every tool: Bash, Read, Edit, WebFetch… *and* MCP tools |
| **Claude Desktop** | each MCP server wrapped in place | MCP tool calls |
| **Cursor** | each MCP server wrapped in place **+** a hook in `~/.cursor/hooks.json` | every tool: built-in read/edit/terminal *and* MCP |
| **Antigravity** | each MCP server wrapped in place (`~/.gemini/config/mcp_config.json`) | MCP tool calls — its built-in tools have no hooks yet |
| **VS Code (Copilot)** | each MCP server wrapped in place **+** a Copilot agent hook (`~/.copilot/hooks/`) | every tool: built-in read/edit/terminal *and* MCP |

Server names, tool names and settings stay exactly as they were. Every file is
backed up first (`~/.hypothesise/backups/`). Restart the apps afterwards.

**Prefer one entry that fronts several servers?** The original setup still
works and can sit beside `init`'s wrapped servers. Put your servers in a
`hypothesise.config.json` (same `mcpServers` shape as Claude/Cursor) and add one
entry to the app's MCP config:

```json
"hypothesise": {
  "type": "stdio",
  "command": "/absolute/path/to/python",
  "args": ["-m", "hypothesise", "proxy", "--config", "/absolute/path/hypothesise.config.json",
           "--detect", "--api", "http://127.0.0.1:8080"]
}
```

It ships live to the dashboard, catches chains across servers inline, and
spools if the dashboard is closed. An old `-m weaver proxy` entry is upgraded
to this form by `hypothesise init`.

Then use your agents as normal, and when you want to look (if port 8080 is
taken — common on Windows — it picks the next free one and tells you):

```bash
hypothesise api          # dashboard at http://127.0.0.1:8080
```

The dashboard does not need to be running: everything is recorded locally and
read in the next time you open it.

```bash
hypothesise status       # what is connected, what has been recorded
hypothesise uninstall    # put every config back exactly as it was
hypothesise api --demo   # just looking? sample attacks, in a separate demo database
```

## Observe and enforce

Everything starts in **observe** mode: every call is recorded and scored,
nothing is blocked, and the dashboard's *Shadow* tab shows what enforce mode
*would* have stopped. When that list is clean on your ordinary work:

```bash
hypothesise mode enforce    # every connected MCP server blocks dangerous calls — no restart
hypothesise mode observe    # back to safe
hypothesise mode            # show the current mode
```

The first time you enable enforce, Hypothesise asks a few questions — what
agents must never touch (passwords and keys, SSH and cloud logins, browser
data, mail and messages, photos, personal folders, bulk-delete commands) — and
turns the answers into block rules. A code folder inside Documents can be kept
open. Re-ask any time with `hypothesise mode enforce --setup`.

In enforce mode a blocked action raises a desktop popup saying which agent
tried what, and why (`--no-popup` turns it off). An action that needs approval
shows an Allow / Block dialog on screen; keep `hypothesise api` running so you
can also approve from the dashboard. Built-in IDE tools are blocked before they
run for protection rules and critical sequences; everything else there is
observed.

```bash
hypothesise reset               # delete recorded data, keep connections & settings
hypothesise release --all       # restore trust after testing attacks
```

## What it does — and does not — do

- **Observe mode by default.** A fresh install records and scores everything
  and blocks nothing. You see what it *would* have stopped before trusting it
  to stop anything.
- **Fails open.** If Hypothesise breaks, your tools keep working — a wrapped
  server falls back to running unwrapped.
- **Every alert is a sentence**, e.g. *"read file:///app/.env (secret) at step
  2, then external request to https://collector.example.net at step 3."*
- **What it cannot see:** Antigravity's *built-in* file, terminal and browser
  tools — it has no hooks, so only its MCP servers are watched. Claude Code,
  Copilot (needs `chat.useHooks`, on by default) and Cursor are covered by
  their hooks. Remote
  (HTTP) MCP servers are listed but not yet intercepted. Claude Code is
  covered completely through its hook.
- **Blocking is per server.** A chain that crosses two MCP servers ("read a
  secret through one, send it out through another") is caught in the
  dashboard, not stopped live.

## Where data lives

`~/.hypothesise/` (set `HYPOTHESISE_HOME` to move it): the event spool, the
dashboard database and config backups. Delete the folder to remove all data.

---

## Development

```bash
bash scripts/dev-setup.sh                 # Windows: scripts\dev-setup.ps1
source .venv/bin/activate
pytest
python scripts/replay.py --all --quiet    # scenario suite
python redteam/run.py                     # red team
python scripts/ops/drills.py              # safety guarantees
```

| Document | What's in it |
|---|---|
| [`docs/GUIDE.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/GUIDE.md) | everything end to end: every integration, the architecture, modes, troubleshooting |
| [`docs/EVIDENCE.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/EVIDENCE.md) | measured detection, false positives and latency vs a denylist and Invariant |
| [`docs/PRIMER.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/PRIMER.md) | the whole project in ten minutes |
| [`docs/HYPOTHESISE.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/HYPOTHESISE.md) | the complete guide |
| [`docs/ARCHITECTURE.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/ARCHITECTURE.md) | design, graph schema, risk math |
| [`docs/DECISIONS.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/DECISIONS.md) | why anything is the way it is (ADRs) |
| [`docs/OPERATIONS.md`](https://github.com/siddhant-shahhh/Hypothesise/blob/master/docs/OPERATIONS.md) | running it, releasing it |


## License

Licensed under the [Apache License 2.0](https://github.com/siddhant-shahhh/Hypothesise/blob/master/LICENSE). Copyright 2026 Siddhant Shah.
