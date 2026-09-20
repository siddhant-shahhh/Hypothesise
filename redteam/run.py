#!/usr/bin/env python3
"""Red-team runner — drives the real proxy through the attack catalogue.

Not a unit test. This starts an actual WeaverProxy over real MCP server
subprocesses in a throwaway sandbox, and reports what got through.

    python redteam/run.py                # full suite
    python redteam/run.py --only exfil   # by tag
    python redteam/run.py --json         # machine-readable, for CI

Exit code is 0 only when every non-gap attack is caught and no benign case
fires. Known gaps are reported loudly and do not fail the build — a gap you
have written down is a backlog item; a gap you have not is a surprise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for pkg in ("core", "store", "proxy"):
    sys.path.insert(0, str(ROOT / "packages" / pkg))
sys.path.insert(0, str(ROOT / "redteam"))

import attacks as catalogue_mod  # noqa: E402
import sandbox as sandbox_mod  # noqa: E402
from weaver_core.engine import Engine  # noqa: E402
from weaver_core.events import Verdict  # noqa: E402
from weaver_core.scenario import load_all  # noqa: E402
from weaver_proxy import protocol as p  # noqa: E402
from weaver_proxy.config import ProxyConfig  # noqa: E402
from weaver_proxy.gate import GateConfig  # noqa: E402
from weaver_proxy.server import WeaverProxy  # noqa: E402
from weaver_proxy.upstream import UpstreamConfig  # noqa: E402

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, YELLOW, GREEN, CYAN, MAG = (
    "\033[31m", "\033[33m", "\033[32m", "\033[36m", "\033[35m")

FIXTURE = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"
TRAIN_PASSES = 3


def trained_engine() -> Engine:
    """Baselines from the benign scenario suite.

    An agent with no history cannot have anomalies, so an untrained engine
    would make every attack look like a first-day agent and catch nothing.
    """
    eng = Engine()
    for _ in range(TRAIN_PASSES):
        for sc in load_all(ROOT / "scenarios"):
            if not sc.should_fire:
                eng.train(sc.events)
                eng.reset_sessions()
    return eng


async def run_attack(attack, engine: Engine, denied_first: bool) -> dict:
    # Two servers, split the way a real setup is and the way the baseline was
    # trained. Routing everything through one server makes `shell-mcp/exec`
    # arrive as `fs-mcp/exec`, which is novel by construction — the harness
    # would then manufacture its own false positives.
    cfg = ProxyConfig(
        upstreams=[
            UpstreamConfig(
                name="fs-mcp", command=sys.executable,
                args=[str(FIXTURE), "--name", "fs-mcp",
                      "--tools", "read_file,write_file",
                      *(["--fail-tool", "read_file"] if denied_first else [])]),
            UpstreamConfig(
                name="shell-mcp", command=sys.executable,
                args=[str(FIXTURE), "--name", "shell-mcp", "--tools", "exec"]),
        ],
        gate=GateConfig(mode="observe", fail_mode="open", budget_ms=50),
    )
    proxy = WeaverProxy(cfg, engine=engine)
    engine.reset_sessions()

    fired, verdicts, detectors = False, [], []
    try:
        await proxy.start_upstreams()
        await proxy.handle(p.request(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "coding-agent-prod", "version": "1.0"}}))

        for i, step in enumerate(attack.steps):
            await proxy.handle(p.request(
                100 + i, "tools/call", {"name": step.tool, "arguments": step.args}))

        batch = await asyncio.wait_for(proxy.emitter.drain(max_batch=64), timeout=2)
        for ev in batch:
            v = ev.decision.verdict
            # In observe mode a would-be block is reported as ALLOW with a
            # shadow sentence, so read intent rather than the enforced verdict.
            shadowed = ev.decision.sentence.startswith("[observe] would have")
            if v is not Verdict.ALLOW or shadowed:
                fired = True
            verdicts.append("BLOCK" if shadowed else v.value.upper())
            detectors.extend(d["detector"] for d in ev.decision.detectors)
    finally:
        await proxy.stop_upstreams()

    detectors = sorted(set(detectors) - {"policy_rule"})
    caught_by_target = (not attack.expect_detector
                        or attack.expect_detector in detectors)
    ok = (fired == attack.expect_block) and (
        caught_by_target or not attack.expect_block)

    return {"name": attack.name, "intent": attack.intent, "tags": attack.tags,
            "expect_block": attack.expect_block, "known_gap": attack.known_gap,
            "expect_detector": attack.expect_detector, "fired": fired,
            "detectors": detectors, "verdicts": verdicts,
            "on_target": caught_by_target, "pass": ok}


async def main_async(args) -> int:
    sb = sandbox_mod.build()
    try:
        items = catalogue_mod.catalogue(sb) + catalogue_mod.benign(sb)
        if args.only:
            items = [a for a in items if args.only in a.tags or args.only == a.name]
        if not items:
            print(f"no attacks matching '{args.only}'")
            return 1

        engine = trained_engine()
        results = []
        for a in items:
            denied = a.name == "retry_after_denial"
            results.append(await run_attack(a, engine, denied_first=denied))
    finally:
        sb.cleanup()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        report(results)

    attacks_ = [r for r in results if r["expect_block"]]
    benign_ = [r for r in results if not r["expect_block"]]
    real = [r for r in attacks_ if not r["known_gap"]]
    missed = [r for r in real if not r["fired"]]
    noisy = [r for r in benign_ if r["fired"]]
    return 0 if not missed and not noisy else 1


def report(results: list[dict]) -> None:
    attacks_ = [r for r in results if r["expect_block"]]
    benign_ = [r for r in results if not r["expect_block"]]
    real = [r for r in attacks_ if not r["known_gap"]]
    gaps = [r for r in attacks_ if r["known_gap"]]

    print(f"\n{BOLD}ATTACKS{RESET}")
    for r in attacks_:
        if r["known_gap"]:
            mark = f"{GREEN}CAUGHT{RESET}" if r["fired"] else f"{YELLOW}GAP{RESET}   "
        else:
            mark = f"{GREEN}CAUGHT{RESET}" if r["fired"] else f"{RED}MISSED{RESET}"
        print(f"  {mark}  {r['name']:<24} {DIM}{r['intent'][:58]}{RESET}")
        if r["detectors"]:
            tag = "" if r["on_target"] else f" {YELLOW}(not {r['expect_detector']}){RESET}"
            print(f"          {MAG}{', '.join(r['detectors'])}{RESET}{tag}")

    print(f"\n{BOLD}BENIGN{RESET}  {DIM}(any firing here is a false positive){RESET}")
    for r in benign_:
        mark = f"{RED}FIRED{RESET} " if r["fired"] else f"{GREEN}quiet{RESET} "
        print(f"  {mark}  {r['name']:<24} {DIM}{r['intent'][:58]}{RESET}")
        if r["fired"]:
            print(f"          {RED}{', '.join(r['detectors'])}{RESET}")

    caught = sum(1 for r in real if r["fired"])
    gaps_caught = sum(1 for r in gaps if r["fired"])
    fp = sum(1 for r in benign_ if r["fired"])

    print(f"\n{BOLD}{'=' * 68}{RESET}")
    c = GREEN if caught == len(real) else RED
    f = GREEN if fp == 0 else RED
    print(f"  attacks caught   {c}{caught}/{len(real)}{RESET}")
    print(f"  false positives  {f}{fp}/{len(benign_)}{RESET}")
    print(f"  known gaps       {YELLOW}{len(gaps) - gaps_caught}{RESET} still open"
          f"{f', {gaps_caught} now caught' if gaps_caught else ''}")
    if gaps:
        print(f"\n  {YELLOW}known gaps — written down, not fixed:{RESET}")
        for r in gaps:
            if not r["fired"]:
                print(f"    {r['name']:<24} {DIM}{r['intent'][:60]}{RESET}")
    print(f"{BOLD}{'=' * 68}{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Weaver red-team suite")
    ap.add_argument("--only", help="filter by tag or attack name")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
