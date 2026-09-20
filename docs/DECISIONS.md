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
