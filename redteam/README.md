# Red-team suite

Drives the **real proxy** over **real MCP server subprocesses** in a throwaway
sandbox, and reports what got through.

```bash
source .venv/bin/activate
python redteam/run.py                # everything
python redteam/run.py --only exfil   # by tag
python redteam/run.py --json         # for CI
```

Exit code is 0 only when every non-gap attack is caught and no benign case
fires.

## Why this exists separately from `scenarios/`

The scenario suite is synthetic: hand-written events with hand-written statuses.
It is fast, it runs in CI, and it cannot surprise us — it only contains attacks
we thought of, shaped the way we assumed.

This suite runs the actual code path. In its first two runs it found two things
the scenario suite had been reporting as fine for weeks:

- **`novel_sequence` fired on three of four benign cases** — `pip install`, an
  app reading its own `.env`, internal health checks. The scenario suite said
  0% false positives (ADR-021).
- **`retry_after_denial` could never fire at all.** Nothing set `Status.DENIED`,
  because MCP has no denial status. The scenario YAML supplies it by hand, so
  every test passed while the product's headline detector was dead code
  (ADR-020).

Neither was findable without running the real thing.

## Safety

`sandbox.py` builds a temp directory with **fake** secrets — obvious nonsense
strings, never anything that resembles a real key. Nothing outside the temp
directory is touched, no real credentials exist anywhere in scope, and the
whole tree is deleted afterwards.

The "external" hosts are `example-evil.net` and similar, which do not resolve.
No traffic leaves the machine.

## Known gaps

Attacks marked `known_gap=True` are ones we miss **and know we miss**. They
report loudly and do not fail the build.

| Gap | Why it gets through | Where it's handled |
|---|---|---|
| `slow_harvest` | No individual window is anomalous | Tier-3 `path_risk`, M4 |
| `dns_exfil` | DNS is not HTTP egress | needs a DNS-aware classifier |
| `destructive_shell` | `rm -rf` is not exfiltration; no detector covers destruction | open |
| `config_tamper` | Writing config is ordinary; intent is invisible | open |

A gap written down is a backlog item. A gap not written down is a surprise
during a customer's incident.

## Adding an attack

Add an `Attack` to `catalogue()` in `attacks.py`. Write it the way an attacker
would act, not the way a detector expects — several existing entries
deliberately try to evade detectors we have, which is the point.

If it gets through and you aren't fixing it today, mark `known_gap=True` with a
comment saying why.

**Add a benign counterpart too.** A red-team suite without a control group only
measures how loud you are.
