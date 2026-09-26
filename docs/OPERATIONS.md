# Operations

Running Weaver beyond a laptop. Everything here is verified except where it
says otherwise.

---

## Install

> **Nothing is published yet, and the name `weaver` is already taken on PyPI**
> (ADR-030). `pip install weaver` installs an unrelated third-party package —
> do not run it. Install from the repo:

```bash
git clone <repo> && cd AI_Layer
bash scripts/dev-setup.sh          # creates .venv; PEP 668 makes this mandatory
source .venv/bin/activate
weaver --help
```

Once a distribution name is chosen and published, this becomes
`pip install <name>` (and `pip install '<name>[postgres]'` for Postgres). The
packaging is built and statically validated; `scripts/ops/release-check.sh`
builds the wheel and proves a clean install works.

One command, five subcommands:

```bash
weaver api [--port 8080] [--demo] [--db URL] [--webhook URL]
weaver proxy --config FILE [--detect] [--api URL]
weaver doctor
weaver migrate [--check] [--db URL]
weaver version
```

## Database and migrations

Default location `~/.weaver/weaver.db`, overridable with `WEAVER_HOME` or
`--db`. Deliberately outside your project (ADR-013).

```bash
weaver migrate            # bring the schema up to date
weaver migrate --check    # exit 1 if migrations are pending — use as a deploy gate
```

`weaver api` migrates on startup, so you rarely run this by hand. It matters
when you upgrade: **`create_all` does not add columns to existing tables**, and
M4 added two. Any database from before then needs this (ADR-026).

### Adding a migration

Append to `MIGRATIONS` in `packages/store/weaver_store/migrate.py`.

- append, never edit — a shipped migration has already run on someone's machine
- each step must be safe to re-run
- SQL must work on SQLite **and** Postgres, or branch on the dialect

## Health and metrics

| Endpoint | Purpose |
|---|---|
| `/healthz` | Liveness. Touches nothing — a probe that queries the database turns a slow query into an outage. |
| `/readyz` | Readiness. 503 when the database is unreachable or the schema is stale. |
| `/metrics` | Prometheus text format. |

```bash
curl -s localhost:8080/metrics
```

```
weaver_events_total 4812
weaver_decisions_total{verdict="allow"} 4799
weaver_decisions_total{verdict="ask"} 11
weaver_decisions_total{verdict="block"} 2
weaver_agents{zone="cold"} 5
weaver_agents_quarantined 0
weaver_approvals_pending 0
weaver_detector_errors_total 0
weaver_signals_total{detector="novel_edge"} 14
```

### What to alert on

| Signal | Why it matters |
|---|---|
| `weaver_detector_errors_total > 0` | A detector is crashing and therefore silent. This is how `scope_widening` was broken for days. |
| `weaver_decisions_total{verdict="block"}` rising on normal work | False positives. Someone is about to uninstall this. |
| `weaver_approvals_pending > 0` for minutes | An agent is stalled. Unanswered approvals **deny** after 120s (ADR-017). |
| `weaver_agents_quarantined > 0` | Needs a human — quarantine is never cleared by time. |
| `/readyz` returning 503 | Stale schema or unreachable database. |

## Containers

```bash
docker compose up -d          # control plane, SQLite in a named volume
docker compose --profile postgres up -d    # + Postgres (untested)
```

**The proxy is not containerised** (ADR-029). It runs as a subprocess of your
MCP client and spawns the upstream MCP servers as its own children over stdio;
in a container it is cut off from both. Install it from the repo on whatever
machine the agent runs on (see Install above).

So deployment is asymmetric, on purpose: dashboard in a container if you like,
proxy next to the agent.

The image binds `127.0.0.1` and runs as a non-root user. **There is no
authentication yet** (ADR-009 made this local-first) — do not expose port 8080
to a network you do not control.

## Load

```bash
python scripts/ops/loadtest.py --n 20000
```

Measures the latency the **agent** sees. Measured on a laptop-class machine:

```
decisions     20,000 in 5.7s
throughput    3,485/sec
latency       mean 0.21ms  p50 0.13ms  p95 0.50ms  p99 1.36ms
gate          0 timeouts, 0 errors
```

p99 of 1.36ms against a 50ms budget. **The number to watch is p99 against the
budget** — past it the gate fails open and detection stops silently while
everything appears to keep working. The script warns above 40% of budget.

## Failure drills

```bash
python scripts/ops/drills.py
```

Breaks things on purpose and asserts the agent survives. Every safety claim in
`ARCHITECTURE.md` §9, against real components:

```
PASS  observe mode never blocks
PASS  engine hang -> fail open
PASS  engine crash -> fail open
PASS  full queue never blocks emit
PASS  dead control plane drops, does not grow
PASS  crashing detector is contained and counted
PASS  block with no explanation is suppressed
PASS  dead upstream does not kill the proxy
```

Run this after touching the gate, the emitter, the shipper or the proxy. A
failure here means a safety guarantee is broken, which matters more than any
detection result.

## Before a release

```bash
bash scripts/ops/release-check.sh
```

Rebuilds `.venv` from scratch, runs everything, builds the wheel, checks its
contents, installs it into a throwaway virtualenv and runs the CLI from there.

The scratch rebuild is the point: **both undeclared dependencies this project
has shipped** (`pyyaml`, `httpx`) were invisible until something installed into
an empty environment.

## Backup and restore

```bash
sqlite3 ~/.weaver/weaver.db ".backup '/backup/weaver-$(date +%F).db'"   # SQLite
pg_dump "$WEAVER_DB_URL" > weaver.sql                                   # Postgres
```

What is lost if you lose the database: event history, the behavioural graph and
its accumulated risk, agent trust scores, approval history. **Baselines rebuild
themselves** from replayed traffic, but resource reputation is genuinely gone —
it is the accumulated product of everything that ever happened.

Restore, then `weaver migrate`.

## Known limitations

| Limitation | Where |
|---|---|
| No authentication on the API | ADR-009 — local-first; do not expose it |
| Postgres untested against a real server | ADR-028 |
| The wheel has never been built in this environment | ADR-027 — `release-check.sh` verifies it |
| No real agent traffic has ever been observed | ADR-010 — the largest one |
| LangGraph adapter is advisory, cannot block | ADR-025 |
| 3 known red-team gaps: DNS exfil, destructive shell, config tamper | `redteam/README.md` |
