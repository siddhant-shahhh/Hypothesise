# Hypothesise — evidence package

*Generated 2026-10-18T09:36 UTC · reproduce with `python scripts/evidence.py --invariant-python <venv>/bin/python`*

Straiker, Lakera and NeuralTrust publish accuracy and latency claims. This is
ours, on a small suite, measured rather than asserted. Every number here is
reproducible from this repository.

## Method

Baselines trained on a held-out half of synthetic benign coding traffic; false positives measured on the other half plus the benign scenario suites; attacks from the scenario and red-team suites (known gaps excluded).

- **Trained on** 76 benign sessions (ordinary coding work + the benign scenario and red-team sessions, which give every agent id a baseline).
- **Tested on** 49 held-out sessions never used in training: 14 attacks and 30 benign coding sessions (5 written-down gaps excluded — see below).
- Every system sees the **same information per call**: tool, server, and the target (path, URL or shell command).

The four systems:

| System | What it is |
|---|---|
| **hypothesise** | this engine, trained on representative benign traffic |
| **denylist** | the per-call rule list most teams start with: block secret paths (`.env`, `.ssh`, `id_rsa`…) and obvious exfil commands (`curl`, `wget`, `scp`…). Sees one call at a time. |
| **invariant** | [Invariant Guardrails](https://github.com/invariantlabs-ai/invariant) (now Snyk), open source, run locally with a rule set written the way its docs show: secret-then-network flows and one-line read-and-send. |

## Headline

| System | Detection | False positives | Mean ms | p95 ms | Max ms |
|---|---|---|---|---|---|
| hypothesise | 14/14 (100.0%) | 0/30 (0.0%) | 0.0518 | 0.0747 | 0.1571 |
| denylist | 12/14 (85.7%) | 12/30 (40.0%) | 0.0014 | 0.0028 | 0.0216 |
| invariant | 6/14 (42.9%) | 0/30 (0.0%) | 24.6239 | 136.0227 | 301.6094 |

- **Detection** = share of real attacks caught (any non-allow verdict in the session).
- **False positives** = benign sessions that would have been blocked or held. *A false positive is worse than a miss* — it is what gets a security tool uninstalled.
- **Latency** = time to decide one call, measured over every call in the run.

## What the comparison shows

**Hypothesise catches sequences a per-call denylist cannot see.** Chains caught here that the denylist missed: **bulk_exfil_payload, novel_tool_combination**. These are attacks with no secret-looking path and no `curl`/`wget` — a large-payload fetch-then-send, and a never-before-seen tool combination. A rule that matches one call at a time has nothing to match.

**A naive denylist is noisy.** It fired on 12 of 30 benign coding sessions (40.0%), because it blocks every `.env` read — which normal apps do at boot. That noise is why teams turn these rules off.

**Invariant is precise but shallow on sequences.** With flow rules it catches the canonical secret-then-egress shapes at 0.0% false positives, but misses retry-after-denial, scope-widening, fan-out and slow credential harvests — and runs ~475× slower per call than Hypothesise.

## Hypothesise by suite

| Suite | Detection | False positives |
|---|---|---|
| Attack scenarios | 8/8 | 0/0 |
| Red-team catalogue | 6/6 | 0/0 |
| Held-out coding | —/— | 0/30 |

## Decision latency

Hypothesise decides in **0.0518 ms on average** (0.0747 ms at the 95th percentile, 0.1571 ms worst case) against a 50 ms budget that fails open. There is no model in the decision path, so this holds on a laptop with no GPU and cannot be prompt-injected.

## Honest limitations

- **Synthetic benign traffic.** The coding sessions are generated, not captured from a real developer. The real-world false-positive rate still needs dogfooding on live traffic — that number is the one still worth getting, and the dashboard now records exactly it.
- **Cold start.** A brand-new agent with no baseline relies only on the tier-1 critical sequence detectors (secret→egress, shell exfiltration, retry-after-denial) and your protection rules. Novelty- and volume-based detectors (`fan_out`, `argument_anomaly`) stay quiet until the agent has history. Measured here by training every agent first, as a real deployment does.
- **Known gaps, excluded from the scores and tracked openly:** DNS-based exfiltration, destructive shell commands with no secret involved (`rm -rf`), and quiet config tampering. The red-team runner reports these separately rather than hiding them.

## Reproduce

```bash
python scripts/evidence.py                                   # hypothesise vs denylist
python scripts/evidence.py --invariant-python <venv>/bin/python   # add Invariant/Snyk
python scripts/evidence.py --json evidence.json              # machine-readable
python redteam/run.py          # the attack catalogue against the real proxy
python scripts/replay.py --all # the scenario suite
```
