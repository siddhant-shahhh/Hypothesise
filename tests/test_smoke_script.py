"""Tests for scripts/ops/smoke.py — the Step-0 wire check.

Only the pure helpers and the early-exit paths. Driving the whole script means
launching real `npx` upstreams, which belongs in a manual run, not the suite.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ops" / "smoke.py"


def load_module():
    spec = importlib.util.spec_from_file_location("hypothesise_smoke", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


smoke = load_module()


def test_base_name_strips_whatever_namespacing_a_proxy_adds():
    """Tool names may or may not carry the upstream's name; match either."""
    assert smoke.base_name("list_tables") == "list_tables"
    assert smoke.base_name("db__list_tables") == "list_tables"
    assert smoke.base_name("db.list_tables") == "list_tables"
    assert smoke.base_name("db:list_tables") == "list_tables"
    assert smoke.base_name("db/list_tables") == "list_tables"


def test_fs_root_is_taken_from_the_filesystem_servers_last_argument():
    config = {"mcpServers": {
        "fs": {"command": "npx", "args": ["-y",
                                          "@modelcontextprotocol/server-filesystem",
                                          "/Volumes/Seagate/uris"]},
        "db": {"command": "npx", "args": ["-y", "mcp-server-sqlite-npx", "/x.db"]},
    }}
    assert smoke.fs_root_from(config) == "/Volumes/Seagate/uris"


def test_fs_root_is_none_when_there_is_no_filesystem_server():
    assert smoke.fs_root_from({"mcpServers": {
        "db": {"command": "npx", "args": ["-y", "mcp-server-sqlite-npx", "/x.db"]}}}) is None
    assert smoke.fs_root_from({}) is None


def test_no_call_in_the_safe_list_can_write():
    """These run against the user's real project database."""
    names = [n for n, _ in smoke.SAFE_CALLS]
    for forbidden in ("write_query", "create_table", "write_file", "edit_file",
                      "move_file", "append_insight"):
        assert forbidden not in names
    query = dict(smoke.SAFE_CALLS)["read_query"]["query"].upper()
    assert query.startswith("SELECT")


def test_missing_config_exits_nonzero(tmp_path, capsys):
    assert smoke.main(["--config", str(tmp_path / "nope.json")]) == 1
    assert "no such config" in capsys.readouterr().out


def test_unparseable_config_exits_nonzero(tmp_path, capsys):
    bad = tmp_path / "hypothesise.config.json"
    bad.write_text("{ not json")
    assert smoke.main(["--config", str(bad)]) == 1
    assert "not valid JSON" in capsys.readouterr().out


def test_unreachable_control_plane_is_reported_before_anything_is_launched(
        tmp_path, capsys):
    cfg = tmp_path / "hypothesise.config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    # Port 1 is reserved and never listening.
    assert smoke.main(["--config", str(cfg), "--api", "http://127.0.0.1:1"]) == 1
    out = capsys.readouterr().out
    assert "control plane unreachable" in out
    assert "hypothesise api" in out


def test_the_script_imports_nothing_from_hypothesise():
    """It launches the CLI as a subprocess, the way a client does — so it must
    not depend on importing the package itself."""
    for line in SCRIPT.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "hypothesise" not in stripped, line
