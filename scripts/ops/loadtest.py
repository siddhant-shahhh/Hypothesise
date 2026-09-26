#!/usr/bin/env python3
"""Load test the decision path.

Measures what actually matters: the latency the AGENT sees. The gate has a 50ms
budget and fails open on timeout, so a p99 anywhere near it means detection
silently stops under load — the worst failure mode this product has, because
everything keeps working and nothing is checked.

    python scripts/ops/loadtest.py                  # 10k decisions
    python scripts/ops/loadtest.py --n 100000
    python scripts/ops/loadtest.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for pkg in ("core", "store", "proxy"):
    sys.path.insert(0, str(ROOT / "packages" / pkg))

from weaver_core.classify import classify_action, classify_resource  # noqa: E402
from weaver_core.engine import Engine  # noqa: E402
from weaver_core.events import Event, Resource, Tool  # noqa: E402
from weaver_core.scenario import load_all  # noqa: E402
from weaver_proxy.emit import EventEmitter  # noqa: E402
from weaver_proxy.gate import Gate, GateConfig  # noqa: E402

TOOLS = [("github-mcp", "get_file_contents"), ("fs-mcp", "read_file"),
         ("fs-mcp", "write_file"), ("shell-mcp", "exec"),
         ("postgres-mcp", "query"), ("http-mcp", "fetch")]
URIS = ["github://acme/api/src/app.py", "file:///app/config/app.yaml",
        "file:///app/.env", "shell://pytest tests/", "postgres://prod-db/users",
        "https://api.internal/health", "https://pypi.org/simple/flask/",
        "file:///app/src/main.py", "shell://docker compose up -d"]


def synthetic(i: int, agent: str) -> Event:
    server, name = TOOLS[i % len(TOOLS)]
    uri = URIS[(i * 7) % len(URIS)]
    cls = classify_resource(uri, server, name)
    return Event(agent_id=agent, session_id=f"sess_{i // 40}", step=i % 40,
                 tool=Tool(server, name), resource=Resource(uri, cls),
                 action=classify_action(name, cls),
                 arg_keys=["path"], arg_bytes=random.randint(30, 300))


async def run(n: int, budget_ms: int) -> dict:
    engine = Engine()
    for _ in range(3):
        for sc in load_all(ROOT / "scenarios"):
            if not sc.should_fire:
                engine.train(sc.events)
                engine.reset_sessions()

    from weaver_proxy.evaluator import engine_evaluator

    gate = Gate(config=GateConfig(mode="observe", fail_mode="open",
                                  budget_ms=budget_ms),
                evaluator=engine_evaluator(engine))
    emitter = EventEmitter(maxsize=10_000)

    latencies: list[float] = []
    started = time.perf_counter()
    for i in range(n):
        ev = synthetic(i, f"agent-{i % 5}")
        t0 = time.perf_counter()
        decision = await gate.decide(ev, engine.snapshot)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        ev.decision = decision
        emitter.emit(ev)
    wall = time.perf_counter() - started

    latencies.sort()
    def pct(p: float) -> float:
        return latencies[min(int(len(latencies) * p), len(latencies) - 1)]

    return {
        "decisions": n,
        "wall_seconds": round(wall, 3),
        "throughput_per_sec": round(n / wall, 1),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3),
            "p50": round(pct(0.50), 3),
            "p95": round(pct(0.95), 3),
            "p99": round(pct(0.99), 3),
            "max": round(latencies[-1], 3),
        },
        "budget_ms": budget_ms,
        "gate": {"timeouts": gate.stats.timeouts, "errors": gate.stats.errors,
                 "would_block": gate.stats.observed_would_block},
        "emitter": {"emitted": emitter.stats.emitted,
                    "dropped": emitter.stats.dropped,
                    "drop_rate": round(emitter.stats.drop_rate, 5)},
        "graph": {"nodes": len(engine.snapshot.nodes),
                  "edges": len(engine.snapshot.edges)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Load test the Weaver decision path")
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--budget-ms", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = asyncio.run(run(args.n, args.budget_ms))
    if args.json:
        print(json.dumps(r, indent=2))
        return 0

    lat = r["latency_ms"]
    print(f"\n  decisions     {r['decisions']:,} in {r['wall_seconds']}s")
    print(f"  throughput    {r['throughput_per_sec']:,}/sec")
    print(f"  latency       mean {lat['mean']}ms  p50 {lat['p50']}ms  "
          f"p95 {lat['p95']}ms  p99 {lat['p99']}ms  max {lat['max']}ms")
    print(f"  budget        {r['budget_ms']}ms")
    print(f"  gate          {r['gate']['timeouts']} timeouts, "
          f"{r['gate']['errors']} errors")
    print(f"  emitter       {r['emitter']['dropped']} dropped "
          f"({r['emitter']['drop_rate'] * 100:.2f}%)")
    print(f"  graph         {r['graph']['nodes']} nodes, {r['graph']['edges']} edges")

    ok = True
    if lat["p99"] > r["budget_ms"] * 0.4:
        print(f"\n  WARN  p99 {lat['p99']}ms is close to the {r['budget_ms']}ms "
              f"budget — past it the gate fails open and detection stops silently")
        ok = False
    if r["gate"]["timeouts"]:
        print(f"\n  FAIL  {r['gate']['timeouts']} decisions exceeded the budget")
        ok = False
    if r["emitter"]["dropped"]:
        print(f"\n  NOTE  {r['emitter']['dropped']} events dropped — expected when "
              f"nothing is draining the queue")
    print(f"\n  {'PASS' if ok else 'REVIEW'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
