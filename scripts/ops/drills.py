#!/usr/bin/env python3
"""Failure drills — break things on purpose and check the agent survives.

Every safety claim in ARCHITECTURE §9 is asserted here against the real
components, not mocked. The claims:

  1. observe mode never blocks
  2. the engine failing means traffic flows (fail-open)
  3. emit() never blocks, even when the queue is full
  4. a dead control plane drops telemetry rather than growing
  5. a crashing detector does not break a decision
  6. a block with no explanation is suppressed
  7. a dead upstream does not take the proxy down

    python scripts/ops/drills.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for pkg in ("core", "store", "proxy"):
    sys.path.insert(0, str(ROOT / "packages" / pkg))

from weaver_core.events import (  # noqa: E402
    Decision,
    Event,
    Resource,
    ResourceClass,
    Tool,
    Verdict,
)
from weaver_core.graph.model import GraphSnapshot  # noqa: E402
from weaver_proxy import protocol as p  # noqa: E402
from weaver_proxy.config import ProxyConfig  # noqa: E402
from weaver_proxy.emit import EventEmitter  # noqa: E402
from weaver_proxy.gate import Gate, GateConfig  # noqa: E402
from weaver_proxy.server import WeaverProxy  # noqa: E402
from weaver_proxy.shipper import Shipper  # noqa: E402
from weaver_proxy.upstream import UpstreamConfig  # noqa: E402

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW = "\033[31m", "\033[32m", "\033[33m"

FIXTURE = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def _ev() -> Event:
    return Event(agent_id="drill", session_id="s", step=0,
                 tool=Tool("fs-mcp", "read_file"),
                 resource=Resource("file:///app/.env", ResourceClass.SECRET))


async def drill_observe_never_blocks() -> tuple[bool, str]:
    async def always_block(_e, _s):
        return Decision(verdict=Verdict.BLOCK, score=0.99, sentence="exfiltration")

    g = Gate(config=GateConfig(mode="observe"), evaluator=always_block)
    d = await g.decide(_ev(), GraphSnapshot())
    ok = d.verdict is Verdict.ALLOW and g.stats.observed_would_block == 1
    return ok, f"verdict={d.verdict.value}, would_block={g.stats.observed_would_block}"


async def drill_engine_hang_fails_open() -> tuple[bool, str]:
    async def hang(_e, _s):
        await asyncio.sleep(10)
        return Decision(verdict=Verdict.BLOCK)

    g = Gate(config=GateConfig(mode="enforce", fail_mode="open", budget_ms=30),
             evaluator=hang)
    t0 = time.perf_counter()
    d = await g.decide(_ev(), GraphSnapshot())
    elapsed = (time.perf_counter() - t0) * 1000
    ok = d.verdict is Verdict.ALLOW and d.fail_open and elapsed < 500
    return ok, f"allowed in {elapsed:.0f}ms, fail_open={d.fail_open}"


async def drill_engine_crash_fails_open() -> tuple[bool, str]:
    async def boom(_e, _s):
        raise MemoryError("engine died")

    g = Gate(config=GateConfig(mode="enforce", fail_mode="open"), evaluator=boom)
    d = await g.decide(_ev(), GraphSnapshot())
    return d.verdict is Verdict.ALLOW and d.fail_open, f"verdict={d.verdict.value}"


async def drill_full_queue_never_blocks() -> tuple[bool, str]:
    em = EventEmitter(maxsize=100)
    t0 = time.perf_counter()
    for _ in range(50_000):
        em.emit(_ev())
    elapsed = (time.perf_counter() - t0) * 1000
    ok = em.stats.dropped > 0 and elapsed < 2000
    return ok, (f"50k emits in {elapsed:.0f}ms, {em.stats.dropped:,} dropped, "
                f"queue capped at {em.qsize()}")


async def drill_dead_control_plane_drops() -> tuple[bool, str]:
    em = EventEmitter(maxsize=200)
    sh = Shipper(api_url="http://127.0.0.1:1", emitter=em,
                 interval=0.05, batch_size=20, timeout=0.2)
    sh.start()
    for _ in range(20):
        em.emit(_ev())
    await asyncio.sleep(3.5)
    await sh.stop()
    ok = sh.stats.dropped > 0 and sh.stats.shipped == 0 and sh.stats.offline
    return ok, (f"{sh.stats.dropped} dropped, {sh.stats.failed_batches} failed "
                f"batches, offline={sh.stats.offline}")


async def drill_crashing_detector_is_contained() -> tuple[bool, str]:
    from weaver_core.detectors.base import (
        _TIER1,
        DetectorContext,
        SessionWindow,
        detector_errors,
        reset_detector_errors,
        run_tier1,
    )

    def explode(_ctx):
        raise ZeroDivisionError("bad detector")

    reset_detector_errors()
    _TIER1.append(("drill_explode", explode))
    try:
        from weaver_core.baseline.model import Baseline

        w = SessionWindow()
        ev = _ev()
        w.push(ev)
        signals = run_tier1(DetectorContext(
            ev, w, Baseline(agent_id="drill", state="active", event_count=99),
            GraphSnapshot()))
        counted = detector_errors().get("drill_explode", 0)
    finally:
        _TIER1[:] = [(n, f) for n, f in _TIER1 if n != "drill_explode"]
        reset_detector_errors()
    return counted == 1 and isinstance(signals, list), (
        f"decision survived, error counted={counted}")


async def drill_unexplained_block_is_suppressed() -> tuple[bool, str]:
    async def mute(_e, _s):
        return Decision(verdict=Verdict.BLOCK, score=0.99, sentence="   ")

    g = Gate(config=GateConfig(mode="enforce"), evaluator=mute)
    d = await g.decide(_ev(), GraphSnapshot())
    ok = d.verdict is Verdict.ALLOW and "suppressed" in d.sentence
    return ok, f"verdict={d.verdict.value}"


async def drill_dead_upstream_survives() -> tuple[bool, str]:
    cfg = ProxyConfig(
        upstreams=[
            UpstreamConfig(name="broken", command="/nonexistent/binary"),
            UpstreamConfig(name="fs-mcp", command=sys.executable,
                           args=[str(FIXTURE), "--name", "fs-mcp"]),
        ],
        gate=GateConfig(mode="observe"),
    )
    proxy = WeaverProxy(cfg)
    try:
        await proxy.start_upstreams()
        await proxy.handle(p.request(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "drill", "version": "1"}}))
        tools = (await proxy.handle(p.request(2, "tools/list")))["result"]["tools"]
        resp = await proxy.handle(p.request(3, "tools/call", {
            "name": "read_file", "arguments": {"path": "/x"}}))
        ok = len(tools) > 0 and "result" in resp
        return ok, f"{len(tools)} tools still available, call succeeded"
    finally:
        await proxy.stop_upstreams()


DRILLS = [
    ("observe mode never blocks", drill_observe_never_blocks),
    ("engine hang -> fail open", drill_engine_hang_fails_open),
    ("engine crash -> fail open", drill_engine_crash_fails_open),
    ("full queue never blocks emit", drill_full_queue_never_blocks),
    ("dead control plane drops, does not grow", drill_dead_control_plane_drops),
    ("crashing detector is contained and counted", drill_crashing_detector_is_contained),
    ("block with no explanation is suppressed", drill_unexplained_block_is_suppressed),
    ("dead upstream does not kill the proxy", drill_dead_upstream_survives),
]


async def main_async() -> int:
    print(f"\n{BOLD}FAILURE DRILLS{RESET}  {DIM}breaking things on purpose{RESET}\n")
    failures = 0
    for name, fn in DRILLS:
        try:
            ok, detail = await fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"drill itself raised: {type(exc).__name__}: {exc}"
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {name}")
        print(f"        {DIM}{detail}{RESET}")
        failures += 0 if ok else 1

    print(f"\n{BOLD}{'=' * 66}{RESET}")
    if failures:
        print(f"  {RED}{failures} of {len(DRILLS)} drills FAILED{RESET} — "
              f"a safety guarantee is broken")
    else:
        print(f"  {GREEN}all {len(DRILLS)} drills passed{RESET} — "
              f"the agent survives every failure mode tested")
    print(f"{BOLD}{'=' * 66}{RESET}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
