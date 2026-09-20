"""Emitter tests — the 'never block the agent' guarantee."""

import asyncio

import pytest
from weaver_core.events import Event, Resource, Tool
from weaver_proxy.emit import EventEmitter


def _ev(step: int = 0) -> Event:
    return Event(agent_id="a", session_id="s", step=step,
                 tool=Tool("fs-mcp", "read_file"), resource=Resource("file:///x"))


@pytest.mark.asyncio
async def test_emit_never_blocks_when_full():
    """The core safety property: a full queue drops events, it does not stall.

    If this ever blocks, a slow control plane stalls the customer's agent and
    the product gets uninstalled.
    """
    em = EventEmitter(maxsize=4)
    for i in range(50):
        em.emit(_ev(i))        # no await anywhere — must not hang
    assert em.stats.emitted == 4
    assert em.stats.dropped == 46
    assert em.qsize() == 4


@pytest.mark.asyncio
async def test_drop_rate_and_pressure():
    em = EventEmitter(maxsize=10)
    for i in range(10):
        em.emit(_ev(i))
    assert em.pressure == 1.0
    assert em.stats.drop_rate == 0.0
    em.emit(_ev(99))
    assert em.stats.drop_rate == pytest.approx(1 / 11)


@pytest.mark.asyncio
async def test_drain_batches():
    em = EventEmitter(maxsize=100)
    for i in range(10):
        em.emit(_ev(i))
    batch = await em.drain(max_batch=4)
    assert len(batch) == 4
    assert em.qsize() == 6


@pytest.mark.asyncio
async def test_drain_waits_for_first_event():
    em = EventEmitter(maxsize=10)

    async def later():
        await asyncio.sleep(0.01)
        em.emit(_ev(1))

    asyncio.create_task(later())
    batch = await asyncio.wait_for(em.drain(), timeout=1.0)
    assert len(batch) == 1
