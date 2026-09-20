#!/usr/bin/env python3
"""Replay a scenario through the engine and print what it decided.

M0 exit criterion: this runs end to end and prints ALLOW for everything.
There are no detectors yet — all-allow is the CORRECT output today. When the
Tier-1 detectors land in M2, the attack scenarios start turning red and the
benign ones must stay green.

    python3 scripts/replay.py scenarios/attack/exfil_basic.yaml
    python3 scripts/replay.py --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core"))
sys.path.insert(0, str(ROOT / "packages" / "store"))

from weaver_core.events import Verdict  # noqa: E402
from weaver_core.graph.model import (  # noqa: E402
    GraphSnapshot,
    edges_for_event,
    nodes_for_event,
    sequence_key,
)
from weaver_core.scenario import Scenario, load_all, load_scenario  # noqa: E402

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, YELLOW, GREEN, CYAN = "\033[31m", "\033[33m", "\033[32m", "\033[36m"

VERDICT_COLOR = {Verdict.ALLOW: GREEN, Verdict.ASK: YELLOW, Verdict.BLOCK: RED}


def replay(sc: Scenario, verbose: bool = True) -> dict:
    """Run one scenario. Returns a summary dict."""
    snapshot = GraphSnapshot()
    fired = False

    if verbose:
        print(f"\n{BOLD}{sc.name}{RESET}  {DIM}[{sc.expect}]{RESET}")
        if sc.description:
            print(f"{DIM}{sc.description}{RESET}")
        if sc.target_detector:
            print(f"{DIM}target: {sc.target_detector}{RESET}")
        print()
        print(f"  {'#':<3} {'TOOL':<28} {'ACTION':<8} {'CLASS':<16} {'STATUS':<8} VERDICT")
        print(f"  {'-'*3} {'-'*28} {'-'*8} {'-'*16} {'-'*8} {'-'*7}")

    for ev in sc.events:
        # --- what the engine will do here once detectors exist (M2) ---
        # signals  = run_tier1(ev, window, snapshot)
        # decision = score_and_decide(signals, snapshot, agent_trust)
        # For M0 the decision stays at its default: ALLOW, score 0.
        decision = ev.decision
        if decision.verdict is not Verdict.ALLOW:
            fired = True

        # Build the graph as we go, so the snapshot reflects history.
        for n in nodes_for_event(ev):
            snapshot.nodes.setdefault(n.id, n)
        for src, dst, action in edges_for_event(ev):
            from weaver_core.graph.model import Edge, edge_key

            k = edge_key(src, dst, action)
            if k not in snapshot.edges:
                snapshot.edges[k] = Edge(id=k, src=src, dst=dst, action=action)
            snapshot.edges[k].count += 1

        if verbose:
            c = VERDICT_COLOR[decision.verdict]
            print(
                f"  {ev.step:<3} {ev.tool.qualified:<28} {ev.action.value:<8} "
                f"{ev.resource.cls.value:<16} {ev.outcome.status.value:<8} "
                f"{c}{decision.verdict.value.upper()}{RESET}"
            )
            if decision.sentence:
                print(f"      {c}→ {decision.sentence}{RESET}")

    ok = fired == sc.should_fire
    if verbose:
        print()
        print(f"  graph built: {CYAN}{len(snapshot.nodes)} nodes, {len(snapshot.edges)} edges{RESET}")
        print(f"  n-gram (n={len(sc.events)}): {DIM}{' → '.join(sequence_key(sc.events))}{RESET}")
        status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  expected {sc.expect}, fired={fired}  {status}")

    return {"name": sc.name, "expect": sc.expect, "fired": fired, "pass": ok}


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay Weaver scenarios.")
    ap.add_argument("scenario", nargs="?", help="path to a scenario YAML")
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.all or not args.scenario:
        scenarios = load_all(ROOT / "scenarios")
    else:
        scenarios = [load_scenario(args.scenario)]

    results = [replay(sc, verbose=not args.quiet) for sc in scenarios]

    attack = [r for r in results if r["expect"] == "fire"]
    benign = [r for r in results if r["expect"] == "silent"]
    detected = sum(1 for r in attack if r["fired"])
    false_pos = sum(1 for r in benign if r["fired"])

    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}SUMMARY{RESET}  {len(results)} scenarios")
    print(f"  detection   {detected}/{len(attack)} attack scenarios fired"
          f"   {DIM}(M2 target: >= 80%){RESET}")
    print(f"  false pos   {false_pos}/{len(benign)} benign scenarios fired"
          f"   {DIM}(M2 target: <= 5%){RESET}")
    if detected == 0 and false_pos == 0:
        print(f"\n  {DIM}No detectors built yet — all-allow is the correct M0 output.{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
