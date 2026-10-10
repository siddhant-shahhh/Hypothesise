"""End-to-end proxy tests against REAL MCP server subprocesses.

Not mocks. The failure modes this phase is expected to produce — id collisions
between upstreams, handshake ordering, framing, deadlock — only appear when
actual processes exchange actual bytes.
"""

import sys
from pathlib import Path

import pytest
from hypothesise_core.events import ResourceClass, Verdict
from hypothesise_proxy import protocol as p
from hypothesise_proxy.config import ProxyConfig
from hypothesise_proxy.gate import GateConfig
from hypothesise_proxy.server import HypothesiseProxy, extract_resource_uri
from hypothesise_proxy.upstream import UpstreamConfig

FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _upstream(name: str, *extra: str) -> UpstreamConfig:
    return UpstreamConfig(name=name, command=sys.executable,
                          args=[FIXTURE, "--name", name, *extra])


def _config(*ups: UpstreamConfig, mode: str = "observe", budget_ms: int = 50) -> ProxyConfig:
    return ProxyConfig(upstreams=list(ups),
                       gate=GateConfig(mode=mode, fail_mode="open", budget_ms=budget_ms))


async def _boot(proxy: HypothesiseProxy) -> dict:
    await proxy.start_upstreams()
    return await proxy.handle(p.request(1, "initialize", {
        "protocolVersion": "2024-11-05",
        "clientInfo": {"name": "test-agent", "version": "1.0"},
    }))


@pytest.mark.asyncio
async def test_initialize_and_aggregate_tools():
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        init = await _boot(proxy)
        assert init["result"]["serverInfo"]["name"] == "hypothesise-proxy"
        tools = (await proxy.handle(p.request(2, "tools/list")))["result"]["tools"]
        assert {t["name"] for t in tools} == {"read_file", "write_file", "exec"}
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_agent_identity_from_client_handshake():
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        await _boot(proxy)
        assert proxy.agent_id == "test-agent"
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_two_upstreams_collide_and_get_namespaced():
    """Both fixtures expose read_file. Names must stay unique AND route correctly.

    This is the test that catches cross-upstream id collisions: two servers
    each numbering their responses from 1.
    """
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp"), _upstream("github-mcp")))
    try:
        await _boot(proxy)
        tools = (await proxy.handle(p.request(2, "tools/list")))["result"]["tools"]
        names = [t["name"] for t in tools]
        assert len(names) == len(set(names)), "tool names must be unique"
        assert "read_file" in names
        assert "github-mcp__read_file" in names

        a = await proxy.handle(p.request(3, "tools/call",
                               {"name": "read_file", "arguments": {"path": "/a"}}))
        b = await proxy.handle(p.request(4, "tools/call",
                               {"name": "github-mcp__read_file", "arguments": {"path": "/b"}}))
        assert "fs-mcp:read_file:ok" in str(a["result"])
        assert "github-mcp:read_file:ok" in str(b["result"])
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_tool_call_forwards_and_emits_event():
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        await _boot(proxy)
        resp = await proxy.handle(p.request(3, "tools/call", {
            "name": "read_file", "arguments": {"path": "file:///app/.env"}}))
        assert "result" in resp

        batch = await proxy.emitter.drain()
        ev = batch[0]
        assert ev.agent_id == "test-agent"
        assert ev.tool.server == "fs-mcp"
        assert ev.resource.cls is ResourceClass.SECRET     # classified in the hot path
        assert ev.args_digest.startswith("sha256:")
        assert ev.decision.verdict is Verdict.ALLOW
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_cleanly():
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        await _boot(proxy)
        resp = await proxy.handle(p.request(3, "tools/call",
                                  {"name": "nope", "arguments": {}}))
        assert resp["error"]["code"] == p.INVALID_PARAMS
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_upstream_error_propagates_and_is_recorded():
    proxy = HypothesiseProxy(_config(_upstream(
        "fs-mcp", "--fail-tool", "exec", "--fail-text", "command failed: exit 1")))
    try:
        await _boot(proxy)
        await proxy.handle(p.request(3, "tools/call",
                           {"name": "exec", "arguments": {"command": "rm -rf /"}}))
        ev = (await proxy.emitter.drain())[0]
        assert ev.outcome.status.value == "error"
        assert ev.outcome.error
        assert ev.resource.cls is ResourceClass.SHELL
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_dead_upstream_does_not_kill_the_proxy():
    """One broken server must not take the others down with it."""
    bad = UpstreamConfig(name="broken", command="/nonexistent/binary", args=[])
    proxy = HypothesiseProxy(_config(bad, _upstream("fs-mcp")))
    try:
        await _boot(proxy)
        tools = (await proxy.handle(p.request(2, "tools/list")))["result"]["tools"]
        assert len(tools) == 3          # fs-mcp's tools still available
        resp = await proxy.handle(p.request(3, "tools/call",
                                  {"name": "read_file", "arguments": {"path": "/x"}}))
        assert "result" in resp
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_unsupported_method_returns_method_not_found():
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        await _boot(proxy)
        resp = await proxy.handle(p.request(9, "resources/list", {}))
        assert resp["error"]["code"] == p.METHOD_NOT_FOUND
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_notifications_get_no_reply():
    """Replying to a notification is a protocol violation."""
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        await _boot(proxy)
        assert await proxy.handle(p.notification("notifications/initialized")) is None
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_steps_increment_across_a_session():
    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")))
    try:
        await _boot(proxy)
        for i in range(3):
            await proxy.handle(p.request(10 + i, "tools/call",
                               {"name": "read_file", "arguments": {"path": f"/f{i}"}}))
        batch = await proxy.emitter.drain()
        assert [e.step for e in batch] == [0, 1, 2]
        assert len({e.session_id for e in batch}) == 1
    finally:
        await proxy.stop_upstreams()


# -- resource extraction ----------------------------------------------------


def test_extract_resource_uri_from_common_shapes():
    assert extract_resource_uri("read_file", {"path": "/app/.env"}) == "/app/.env"
    assert extract_resource_uri("fetch", {"url": "https://x.com"}) == "https://x.com"
    assert extract_resource_uri("exec", {"command": "cat /etc/passwd"}) == "shell://cat /etc/passwd"
    assert extract_resource_uri("query", {"query": "SELECT 1"}).startswith("query://")


def test_extract_resource_uri_unknown_shape_yields_empty():
    """Guessing wrong attaches risk to a resource that was never touched.

    Empty -> UNKNOWN class -> low floor, fires nothing. That is the correct
    failure mode.
    """
    assert extract_resource_uri("mystery", {"foo": 1, "bar": [1, 2]}) == ""


@pytest.mark.asyncio
async def test_proxy_with_engine_records_decisions():
    """The engine plugged into the Gate, end to end through a real subprocess."""
    from hypothesise_core.engine import Engine

    proxy = HypothesiseProxy(_config(_upstream("fs-mcp")), engine=Engine())
    try:
        await _boot(proxy)
        await proxy.handle(p.request(3, "tools/call",
                           {"name": "read_file", "arguments": {"path": "file:///app/.env"}}))
        ev = (await proxy.emitter.drain())[0]
        assert ev.decision.verdict is Verdict.ALLOW      # observe mode
        assert ev.decision.budget_ms < 50                # inside the budget
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_engine_decisions_stay_within_budget():
    """Tier-1 detectors run inline. If they ever exceed the budget the Gate
    fails open and detection silently stops."""
    from hypothesise_core.engine import Engine

    proxy = HypothesiseProxy(_config(_upstream("fs-mcp"), budget_ms=50), engine=Engine())
    try:
        await _boot(proxy)
        for i in range(20):
            await proxy.handle(p.request(100 + i, "tools/call",
                               {"name": "read_file", "arguments": {"path": f"/f{i}"}}))
        assert proxy.gate.stats.timeouts == 0
        assert proxy.gate.stats.errors == 0
        assert proxy.gate.stats.avg_ms < 20
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_upstream_refusal_is_recorded_as_denied():
    """MCP has no distinct 'denied' status — a refusal and a missing file both
    come back as an error.

    Nothing set Status.DENIED for the whole of M1-M3, which meant
    retry_after_denial could never fire on real traffic while still passing
    its tests, because the scenario YAML sets the status by hand.
    """
    from hypothesise_core.events import Status

    proxy = HypothesiseProxy(_config(_upstream(
        "fs-mcp", "--fail-tool", "read_file", "--fail-text", "permission denied")))
    try:
        await _boot(proxy)
        await proxy.handle(p.request(3, "tools/call",
                           {"name": "read_file", "arguments": {"path": "/app/.env"}}))
        ev = (await proxy.emitter.drain())[0]
        assert ev.outcome.status is Status.DENIED
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_missing_file_is_an_error_not_a_denial():
    """Absence is not refusal. Agents probe for optional files constantly."""
    from hypothesise_core.events import Status

    proxy = HypothesiseProxy(_config(_upstream(
        "fs-mcp", "--fail-tool", "read_file", "--fail-text", "ENOENT: no such file")))
    try:
        await _boot(proxy)
        await proxy.handle(p.request(3, "tools/call",
                           {"name": "read_file", "arguments": {"path": "/app/.nvmrc"}}))
        ev = (await proxy.emitter.drain())[0]
        assert ev.outcome.status is Status.ERROR
    finally:
        await proxy.stop_upstreams()


@pytest.mark.asyncio
async def test_our_own_block_counts_as_a_denial_for_the_retry_detector():
    """An agent we just blocked, immediately probing a different route to the
    same target, is the clearest evasion signal there is."""
    from hypothesise_core.detectors.base import SessionWindow
    from hypothesise_core.events import Event, Outcome, Resource, ResourceClass, Status, Tool

    w = SessionWindow()
    w.push(Event(agent_id="a", session_id="s", step=0,
                 tool=Tool("fs-mcp", "read_file"),
                 resource=Resource("file:///app/.env", ResourceClass.SECRET),
                 outcome=Outcome(status=Status.BLOCKED)))
    assert w.last_denial() is not None
