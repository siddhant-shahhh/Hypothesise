"""Shipper tests — against a REAL HTTP server, not a mock.

The failure this component exists to survive is a control plane that is slow,
broken or absent. Mocking the transport would test everything except that.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from hypothesise_core.events import Event, Resource, Tool
from hypothesise_proxy.emit import EventEmitter
from hypothesise_proxy.shipper import Shipper


class _Collector(BaseHTTPRequestHandler):
    received: list = []
    status = 200
    delay = 0.0

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        body = self.rfile.read(n)
        if _Collector.delay:
            import time
            time.sleep(_Collector.delay)
        if _Collector.status < 400:
            _Collector.received.extend(json.loads(body))
        self.send_response(_Collector.status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":1}')

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def collector():
    _Collector.received = []
    _Collector.status = 200
    _Collector.delay = 0.0
    srv = HTTPServer(("127.0.0.1", 0), _Collector)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", _Collector
    srv.shutdown()


def _ev(step=0, agent="test-agent"):
    return Event(agent_id=agent, session_id="s1", step=step,
                 tool=Tool("fs-mcp", "read_file"),
                 resource=Resource("file:///app/main.py"))


@pytest.mark.asyncio
async def test_ships_events(collector):
    url, sink = collector
    em = EventEmitter(maxsize=100)
    sh = Shipper(api_url=url, emitter=em, interval=0.1, batch_size=10)
    sh.start()
    for i in range(5):
        em.emit(_ev(i))
    await asyncio.sleep(0.6)
    await sh.stop()

    assert sh.stats.shipped == 5
    assert len(sink.received) == 5
    assert sink.received[0]["agent_id"] == "test-agent"
    assert sink.received[0]["tool"]["server"] == "fs-mcp"


@pytest.mark.asyncio
async def test_endpoint_is_built_correctly():
    em = EventEmitter()
    assert Shipper("http://x:8080", em).endpoint == "http://x:8080/v1/events"
    assert Shipper("http://x:8080/", em).endpoint == "http://x:8080/v1/events"


@pytest.mark.asyncio
async def test_dead_control_plane_drops_rather_than_growing(collector):
    """A control plane that never comes back must not become unbounded memory.

    Retries are finite; after that the batch is dropped and counted. Losing
    telemetry is the correct failure for a security layer. An OOM is not.
    """
    _, _sink = collector
    em = EventEmitter(maxsize=100)
    # Port nothing is listening on.
    sh = Shipper(api_url="http://127.0.0.1:1", emitter=em,
                 interval=0.05, batch_size=5, timeout=0.2)
    sh.start()
    for i in range(5):
        em.emit(_ev(i))
    await asyncio.sleep(3.0)
    await sh.stop()

    assert sh.stats.shipped == 0
    assert sh.stats.dropped == 5
    assert sh.stats.offline is True
    assert sh.stats.last_error


@pytest.mark.asyncio
async def test_server_errors_are_retried_then_dropped(collector):
    url, sink = collector
    sink.status = 500
    em = EventEmitter(maxsize=100)
    sh = Shipper(api_url=url, emitter=em, interval=0.05, batch_size=5, timeout=1.0)
    sh.start()
    em.emit(_ev(0))
    await asyncio.sleep(3.0)
    await sh.stop()

    assert sh.stats.failed_batches >= 1
    assert sh.stats.shipped == 0


@pytest.mark.asyncio
async def test_recovers_when_the_control_plane_comes_back(collector):
    url, sink = collector
    sink.status = 500
    em = EventEmitter(maxsize=100)
    sh = Shipper(api_url=url, emitter=em, interval=0.05, batch_size=5, timeout=1.0)
    sh.start()
    em.emit(_ev(0))
    await asyncio.sleep(2.5)
    assert sh.stats.offline is True

    sink.status = 200
    em.emit(_ev(1))
    await asyncio.sleep(1.5)
    await sh.stop()

    assert sh.stats.offline is False
    assert sh.stats.shipped >= 1


@pytest.mark.asyncio
async def test_shipping_never_blocks_emit(collector):
    """The core guarantee. emit() is called on the agent's request path.

    The collector here is deliberately slow; emit() must not notice.
    """
    url, sink = collector
    sink.delay = 0.5
    em = EventEmitter(maxsize=50)
    sh = Shipper(api_url=url, emitter=em, interval=0.05, batch_size=5, timeout=5.0)
    sh.start()

    import time
    start = time.perf_counter()
    for i in range(50):
        em.emit(_ev(i))
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05, f"emit() took {elapsed:.3f}s — it must never block"
    await sh.stop()


@pytest.mark.asyncio
async def test_no_shipper_when_no_api_configured():
    """Local-only is the default. Nothing leaves the machine unless asked."""
    from hypothesise_proxy.config import ProxyConfig
    from hypothesise_proxy.server import HypothesiseProxy

    assert HypothesiseProxy(ProxyConfig()).shipper is None
    assert HypothesiseProxy(ProxyConfig(api_url="http://127.0.0.1:8080")).shipper is not None
