"""Tests for scripts/ops/check-client-config.py — the guard from ADR-032.

The bug this exists to prevent was a config naming an executable that was not
on this machine, and nothing in the suite could see it because pytest never
launches the installed console script. So these tests do the one thing the
suite was missing: build real files on disk and check the guard's verdict.

Loaded by path because the filename has a hyphen and `scripts/ops` is not a
package.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ops" / "check-client-config.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_client_config", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ccc = load_module()


def write_json(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc, indent=2))
    return path


def stub_executable(path: Path) -> Path:
    """A shell script that exits 0 whatever it is asked — stands in for a
    working `hypothesise` without needing one installed."""
    path.write_text(f"#!{sys.executable}\nimport sys\nprint('hypothesise 0.1.0')\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def vscode_config(command: str, *, api: str | None = None,
                  config: str | None = None) -> dict:
    args: list[str] = ["-m", "hypothesise", "proxy"]
    if config is not None:
        args += ["--config", config]
    args.append("--detect")
    if api is not None:
        args += ["--api", api]
    return {"servers": {"hypothesise": {"type": "stdio", "command": command,
                                   "args": args}}}


# --------------------------------------------------------------- key handling

def test_recognises_both_client_dialects():
    """VS Code says `servers`; Cursor and Claude Code say `mcpServers`. The
    wrong key is silently ignored by the client, so the checker reads both."""
    assert ccc.servers_of({"servers": {"a": {}}}) == {"a": {}}
    assert ccc.servers_of({"mcpServers": {"a": {}}}) == {"a": {}}
    assert ccc.servers_of({"somethingElse": {"a": {}}}) == {}


def test_identifies_a_hypothesise_entry_by_name_or_by_command():
    assert ccc.is_hypothesise_entry("hypothesise", {})
    assert ccc.is_hypothesise_entry("guard", {"command": "/x/bin/python",
                                         "args": ["-m", "hypothesise"]})
    assert not ccc.is_hypothesise_entry("fs", {"command": "npx",
                                          "args": ["-y", "server-filesystem"]})


# ------------------------------------------------------- binary/format checks

def test_elf_on_macos_is_named_as_the_problem(tmp_path):
    """The actual ADR-032 failure: a venv built by the Linux VM, sitting on the
    shared drive, pointed at from macOS."""
    binary = tmp_path / "hypothesise"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 60)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    assert ccc.platform_of(binary) == "elf"

    rep = ccc.Report()
    result = ccc.check_command(rep, {"command": str(binary)})
    if ccc.native_format() == "macho":
        assert result is None
        assert rep.failed
    else:
        assert result == binary


def test_shebang_is_followed_to_its_interpreter(tmp_path):
    script = tmp_path / "hypothesise"
    script.write_text("#!/nonexistent/python3\nprint(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    assert ccc.shebang_of(script) == "/nonexistent/python3"
    rep = ccc.Report()
    assert ccc.check_command(rep, {"command": str(script)}) is None
    assert rep.failed


def test_env_shebang_reads_the_interpreter_not_env():
    p = Path(os.devnull)
    assert ccc.shebang_of(p) is None  # not a script at all


# ------------------------------------------------------------- end-to-end main

def test_missing_command_fails(tmp_path, capsys):
    cfg = write_json(tmp_path / "mcp.json",
                     vscode_config(str(tmp_path / "does-not-exist")))
    assert ccc.main([str(cfg)]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_relative_command_not_on_path_fails(tmp_path, capsys):
    cfg = write_json(tmp_path / "mcp.json", vscode_config("hypothesise-not-a-real-cmd"))
    assert ccc.main([str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "not absolute" in out and "PATH" in out


def test_no_hypothesise_entry_fails(tmp_path):
    cfg = write_json(tmp_path / "mcp.json",
                     {"servers": {"fs": {"command": "npx", "args": ["-y", "x"]}}})
    assert ccc.main([str(cfg)]) == 1


def test_wrong_top_level_key_fails(tmp_path, capsys):
    cfg = write_json(tmp_path / "mcp.json", {"mcp": {"hypothesise": {}}})
    assert ccc.main([str(cfg)]) == 1
    assert "mcpServers" in capsys.readouterr().out


def test_absent_file_fails(tmp_path):
    assert ccc.main([str(tmp_path / "nope.json")]) == 1


def test_a_working_config_passes(tmp_path, capsys):
    """Everything present: a runnable command, a parseable --config. No --api,
    so this is the warn-but-usable path."""
    cmd = stub_executable(tmp_path / "hypothesise")
    upstream = write_json(tmp_path / "hypothesise.config.json", {"mcpServers": {}})
    cfg = write_json(tmp_path / "mcp.json",
                     vscode_config(str(cmd), config=str(upstream)))
    assert ccc.main([str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "it runs: hypothesise 0.1.0" in out
    assert "--config exists and parses" in out


def test_a_command_that_exits_nonzero_fails(tmp_path, capsys):
    cmd = tmp_path / "hypothesise"
    cmd.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    cmd.chmod(cmd.stat().st_mode | stat.S_IXUSR)
    cfg = write_json(tmp_path / "mcp.json", vscode_config(str(cmd)))
    assert ccc.main([str(cfg)]) == 1
    assert "exited 3" in capsys.readouterr().out


def test_unparseable_upstream_config_fails(tmp_path, capsys):
    cmd = stub_executable(tmp_path / "hypothesise")
    broken = tmp_path / "hypothesise.config.json"
    broken.write_text("{ not json")
    cfg = write_json(tmp_path / "mcp.json",
                     vscode_config(str(cmd), config=str(broken)))
    assert ccc.main([str(cfg)]) == 1
    assert "not readable JSON" in capsys.readouterr().out


def test_missing_api_warns_but_does_not_fail(tmp_path, capsys):
    """Events stay in memory and the dashboard stays empty — worth saying,
    not worth refusing."""
    cmd = stub_executable(tmp_path / "hypothesise")
    cfg = write_json(tmp_path / "mcp.json", vscode_config(str(cmd)))
    assert ccc.main([str(cfg)]) == 0
    assert "dashboard" in capsys.readouterr().out


def test_unreachable_api_warns_but_does_not_fail(tmp_path, capsys):
    cmd = stub_executable(tmp_path / "hypothesise")
    cfg = write_json(tmp_path / "mcp.json",
                     vscode_config(str(cmd), api="http://127.0.0.1:1"))
    assert ccc.main([str(cfg)]) == 0
    assert "nothing listening" in capsys.readouterr().out


# ------------------------------------------------------------------------ fix

def test_fix_rewrites_to_this_interpreter(tmp_path):
    cfg = write_json(tmp_path / "mcp.json",
                     vscode_config("/Volumes/Seagate/AI_Layer/.venv/bin/hypothesise"))
    ccc.main([str(cfg), "--fix"])
    entry = json.loads(cfg.read_text())["servers"]["hypothesise"]
    assert entry["command"] == sys.executable
    assert entry["args"][:2] == ["-m", "hypothesise"]


def test_fix_is_idempotent(tmp_path):
    """Run twice, and `-m hypothesise` must not appear twice."""
    cfg = write_json(tmp_path / "mcp.json", vscode_config("/old/bin/hypothesise"))
    ccc.main([str(cfg), "--fix"])
    first = cfg.read_text()
    ccc.main([str(cfg), "--fix"])
    assert cfg.read_text() == first
    args = json.loads(first)["servers"]["hypothesise"]["args"]
    assert args.count("hypothesise") == 1


def test_fix_preserves_the_proxy_arguments(tmp_path):
    cfg = write_json(tmp_path / "mcp.json",
                     vscode_config("/old/bin/hypothesise", config="/x/c.json",
                                   api="http://127.0.0.1:8080"))
    ccc.main([str(cfg), "--fix"])
    args = json.loads(cfg.read_text())["servers"]["hypothesise"]["args"]
    for expected in ("proxy", "--config", "/x/c.json", "--detect",
                     "--api", "http://127.0.0.1:8080"):
        assert expected in args


def test_fix_keeps_other_fields_and_other_servers(tmp_path):
    doc = vscode_config("/old/bin/hypothesise")
    doc["servers"]["fs"] = {"command": "npx", "args": ["-y", "server-fs"]}
    cfg = write_json(tmp_path / "mcp.json", doc)
    ccc.main([str(cfg), "--fix"])
    after = json.loads(cfg.read_text())["servers"]
    assert after["hypothesise"]["type"] == "stdio"
    assert after["fs"] == {"command": "npx", "args": ["-y", "server-fs"]}


def test_fixed_entry_uses_dash_m_so_no_console_script_is_needed():
    """The point of ADR-032: `-m hypothesise` needs no shim in bin/."""
    fixed = ccc.fixed_entry({"command": "/old/bin/hypothesise", "args": ["proxy"]})
    assert "bin/hypothesise" not in fixed["command"]
    assert fixed["args"] == ["-m", "hypothesise", "proxy"]


# ----------------------------------------------------- the guard's own wiring

def test_the_script_imports_nothing_from_hypothesise():
    """Like scripts/doctor.py: it has to work when hypothesise is the broken thing."""
    source = SCRIPT.read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "hypothesise" not in stripped, line


@pytest.mark.parametrize("doc_name", ["TEST-URIS.md", "TESTING.md", "DOGFOOD.md"])
def test_docs_no_longer_hardcode_a_venv_console_script(doc_name):
    """The docs are what put the wrong path in the config in the first place."""
    text = (REPO / "docs" / doc_name).read_text()
    assert ".venv/bin/hypothesise" not in text
