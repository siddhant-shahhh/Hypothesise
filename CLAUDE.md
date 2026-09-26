# Weaver — notes for Claude Code

Behavioural security layer for AI agents. Intercepts tool calls at the MCP
layer, learns what each agent normally does, scores deviations, and can block.

**Read `docs/PRIMER.md` first** — ten minutes, covers the whole project.
Then `docs/DECISIONS.md` for why anything is the way it is (21 ADRs).

## Setup

```bash
bash scripts/dev-setup.sh      # creates .venv — PEP 668 makes this mandatory
source .venv/bin/activate
```

`python3` on this machine is a zsh alias to Homebrew 3.13, and Homebrew Python
refuses `pip install` under PEP 668. Never use `--break-system-packages` here;
it can damage the Homebrew install. If imports fail: `python scripts/doctor.py`.

## Checks — all three must pass

```bash
pytest                                  # 247
python scripts/replay.py --all --quiet  # scenarios, exit 0 = criteria met
python redteam/run.py                   # red team, exit 0 = nothing got through
python scripts/ops/drills.py            # every safety guarantee still holds
ruff check packages scripts tests redteam
mypy packages/core/weaver_core
```

Before any release: `bash scripts/ops/release-check.sh` — rebuilds .venv from
scratch, which is how both undeclared dependencies were caught.

## Rules that are load-bearing

**Risk lives on graph nodes and edges, never on an agents table** (ADR-004).
`test_agents_table_has_no_risk_column` fails if anyone adds `agents.risk_score`.
That column would quietly kill resource reputation; undoing it is ~30 days.

**`weaver_core` imports nothing** (ADR-005) — no FastAPI, no SQLAlchemy, no MCP.
Detectors are pure functions. Partly testability, partly that project ownership
is still open and a dependency-free core stays separable.

**The data plane never blocks the agent.** `emit()` is synchronous and
non-blocking; the gate has a 50ms budget and fails open; the shipper drops
batches rather than growing. A security layer that stalls someone's agent gets
uninstalled, and then secures nothing.

**No sentence, no block** (ARCHITECTURE §9). A block with no human-readable
explanation is suppressed in code, not just discouraged.

**Observe mode is the default everywhere**, including in development.

**Scenarios before detectors.** Write the YAML that should fire, watch it not
fire, then build the detector.

**A false positive is worse than a miss.** The benign suite is 12 cases against
8 attacks on purpose. A missed detection is a bug; a false positive is an
uninstall.

## Where things are

```
packages/core/weaver_core/   the engine — events, classify, graph, risk,
                             baseline, detectors, policy, engine.py
packages/store/              SQLite now, Postgres-portable (ADR-002)
packages/proxy/              MCP interception — the data plane
packages/api/                FastAPI + single-file dashboard (no build step)
scenarios/                   attack/ must fire · benign/ must not
redteam/                     real proxy, real MCP subprocesses, fake secrets
```

## Status

M0–M5 built. One installable distribution, one CLI: `weaver api`, `proxy`,
`doctor`, `migrate`, `version`. See `docs/OPERATIONS.md`.

**Nothing is published, and `weaver` is taken on PyPI** (ADR-030). Never write
`pip install weaver` in docs or tell anyone to run it — it installs an
unrelated package. Install with `bash scripts/dev-setup.sh`.

**Migrations are append-only** — never edit a shipped one, and every step must
be safe to re-run. `create_all` does not add columns to existing tables, which
is the bug ADR-026 exists for.

**The real gap is data, not features.** Everything so far runs against fixtures
and hand-written YAML. No real agent has ever connected — there is no
`weaver.config.json`, only the example pointing at a test stub.

Both serious bugs found so far came from running the real thing:

- `novel_sequence` fired on `pip install` while the scenario suite reported 0%
  false positives (ADR-021)
- `retry_after_denial` **could never fire in production** — nothing set
  `Status.DENIED` — while the scenario suite reported 100% detection (ADR-020)

`novel_edge`, `argument_anomaly` and `scope_widening` have never seen a real
tool call. Prefer dogfooding (`docs/DOGFOOD.md`) over building M4.

## Dogfooding from Claude Code

You are an MCP client. Point yourself at Weaver and this repo becomes its own
test subject:

```bash
weaver-api                     # terminal 1
# then add weaver-proxy to your MCP config — docs/DOGFOOD.md has the block
```

Observe mode blocks nothing. Every alert on ordinary work is a false positive,
and that number is the one nobody has yet.

## Open decisions

- **ADR-007, ownership** — unresolved, and must close before the M1 payment.
  `weaver_core` stays separable only until it ships.
- Which MCP servers the client actually runs is still unknown; GitHub,
  Postgres and shell are assumed.

## Conventions

- Comments explain *why*, never *what*. Load-bearing decisions cite their ADR.
- New detector → benign counterpart scenario first.
- Changed a dependency → `rm -rf .venv && bash scripts/dev-setup.sh` before
  trusting the suite. That is how both undeclared dependencies were caught.
- Working codename `weaver`; the product name is undecided **and now blocking**
  — the PyPI name is taken, so a distribution name must be chosen before
  anything ships (ADR-030).
