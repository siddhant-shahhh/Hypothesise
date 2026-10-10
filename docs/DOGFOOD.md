# Running Hypothesise against your own agents

There is no production agent traffic to learn from (ADR-010), and this is the
cheapest fix. Point your own Claude Code / Cursor / Copilot at Hypothesise and work
normally. Real traffic, available today, costs nothing.

**Observe mode only.** Nothing is blocked. Ever. Don't change that until M3.

---

## 1. Start the control plane

```bash
cd /Volumes/Seagate/AI_Layer
source .venv/bin/activate
hypothesise-api                       # http://127.0.0.1:8080
```

Leave it running. The database lives in `~/.hypothesise/hypothesise.db`, so it survives
restarts and accumulates across sessions.

## 2. Move your MCP servers behind Hypothesise

Take whatever `mcpServers` block your client already has, and put it in
`hypothesise.config.json` at the repo root:

```json
{
  "mcpServers": {
    "github":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/code"]}
  },
  "hypothesise": {
    "mode": "observe",
    "failMode": "open",
    "budgetMs": 50,
    "apiUrl": "http://127.0.0.1:8080"
  }
}
```

## 3. Point the client at Hypothesise instead

Same shape for all three — one server replacing the list.

**Claude Code** — `~/.claude.json` (or `claude mcp add`):

```json
{"mcpServers": {"hypothesise": {
  "command": "/absolute/path/to/your/env/bin/python3",
  "args": ["-m", "hypothesise", "proxy",
           "--config", "/Volumes/Seagate/AI_Layer/hypothesise.config.json", "--detect"]
}}}
```

**Cursor** — `~/.cursor/mcp.json` or `.cursor/mcp.json` in the project: same block.

**VS Code / Copilot** — `.vscode/mcp.json`, under `servers` rather than
`mcpServers`; the value is identical.

Use an **absolute path**, because the client does not inherit your shell's
activated venv — and point it at the **interpreter** (`python3 -m hypothesise`)
rather than a console script in `bin/`. One path to get wrong instead of two,
and no shim to be missing. ADR-032.

Then check it before you rely on it, from the environment Hypothesise is installed
in:

```bash
python3 scripts/ops/check-client-config.py <that config> --fix
python3 scripts/ops/check-client-config.py <that config>
```

## 4. Work normally

Your agent sees the same tools under the same names. Nothing changes for you.
Every tool call gets classified, scored and shipped to the dashboard.

---

## What to look for

Open `http://127.0.0.1:8080` and go to **Alerts**.

> **Every alert here is a false positive.**
>
> You are not attacking yourself. Anything above ALLOW on your own normal work
> is the detector being wrong.

That is the number the scenario suite cannot produce, and the one that decides
whether anybody keeps this installed.

### Target after a week of real use

| Metric | Target | Where |
|---|---|---|
| `verdicts.ask` | near 0 | `/v1/stats` |
| `verdicts.block` | **0** | `/v1/stats` |
| events | thousands | `/v1/stats` |

```bash
curl -s localhost:8080/v1/stats | python -m json.tool
```

### Suspects, in order

1. **`novel_sequence`** — fires most often, and the scenario suite is too small
   to stress it. Your real edit-test loops are the test.
2. **`novel_edge`** — every new tool you adopt looks like a first contact.
3. **`argument_anomaly`** — one genuinely large file write could trip it.

If a detector fires repeatedly on ordinary work, **write the case down as a
benign scenario** before changing anything:

```bash
cat > scenarios/benign/my_real_case.yaml <<'EOF'
name: my_real_case
description: Real traffic from <what you were doing>. novel_sequence fired; it shouldn't.
expect: silent
agent_id: coding-agent-prod
events:
  - {tool: {server: ..., name: ...}, resource: "..."}
EOF
python scripts/replay.py scenarios/benign/my_real_case.yaml
```

Then fix the detector and confirm the whole suite still passes. That way every
false positive you hit becomes a permanent regression test, and the suite stops
being something we invented.

---

## Health checks

```bash
curl -s localhost:8080/v1/detectors    # anything in "errors" is broken and silent
curl -s localhost:8080/v1/agents       # your agents, as Hypothesise sees them
```

If events aren't arriving: the proxy prints `hypothesise: shipping events to ...` on
stderr at startup. No line means `apiUrl` isn't set. The shipper retries three
times with backoff, then drops the batch and counts it — it never blocks your
agent and never grows without bound.

## Turning it off

Put your original `mcpServers` block back. Nothing else to undo. The database
in `~/.hypothesise` stays until you delete it.
