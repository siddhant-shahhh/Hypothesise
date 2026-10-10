"""`hypothesise init / uninstall / status` against fake app configs.

The contract: init changes only the command line of stdio MCP servers (or adds
one hook for Claude Code), backs every file up first, is idempotent, and
uninstall returns each file to exactly what it was.
"""

import argparse
import json
import sys
from pathlib import Path

import pytest
from hypothesise import clients as c

ORIGINAL_DESKTOP = {
    "globalShortcut": "Ctrl+Space",
    "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                   "env": {"GITHUB_TOKEN": "x"}},
        "remote": {"type": "http", "url": "https://mcp.example.com"},
    },
}
ORIGINAL_CURSOR = {"mcpServers": {"pg": {"command": "uvx", "args": ["mcp-server-postgres"]}}}
ORIGINAL_VSCODE = {"inputs": [{"id": "tok", "type": "promptString"}],
                   "servers": {"fs": {"type": "stdio", "command": "npx",
                                      "args": ["-y", "server-fs", "/w"],
                                      "env": {"TOKEN": "${input:tok}"}}}}
ORIGINAL_CLAUDE = {"permissions": {"allow": ["Bash(ls:*)"]},
                   "hooks": {"PostToolUse": [{"matcher": "Edit",
                                              "hooks": [{"type": "command", "command": "fmt"}]}]}}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.setenv("HYPOTHESISE_HOME", str(h / ".hypothesise"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(c, "_windows", lambda: False)
    monkeypatch.setattr(c, "interpreter", lambda: "/opt/tools/hypothesise/bin/python")
    files = {
        "desktop": (h / ".config" / "Claude" / "claude_desktop_config.json", ORIGINAL_DESKTOP),
        "cursor": (h / ".cursor" / "mcp.json", ORIGINAL_CURSOR),
        "vscode": (h / ".config" / "Code" / "User" / "mcp.json", ORIGINAL_VSCODE),
        "claude": (h / ".claude" / "settings.json", ORIGINAL_CLAUDE),
    }
    for path, data in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=4))
    monkeypatch.chdir(tmp_path)
    return files


def _args(**kw):
    base = {"client": None, "project": None, "dry_run": False, "yes": True, "force": False}
    return argparse.Namespace(**{**base, **kw})


def _load(p):
    return json.loads(Path(p).read_text())


def test_init_wraps_stdio_servers_in_place(machine):
    assert c.cmd_init(_args()) == 0
    gh = _load(machine["desktop"][0])["mcpServers"]["github"]
    assert gh["command"] == "/opt/tools/hypothesise/bin/python"
    assert gh["args"] == ["-m", "hypothesise", "wrap", "--client", "claude-desktop",
                          "--server", "github", "--", "npx", "-y",
                          "@modelcontextprotocol/server-github"]
    assert gh["env"] == {"GITHUB_TOKEN": "x"}, "env stays where the app substitutes it"
    assert _load(machine["desktop"][0])["mcpServers"]["remote"] == \
        ORIGINAL_DESKTOP["mcpServers"]["remote"], "remote servers are left alone"
    assert _load(machine["desktop"][0])["globalShortcut"] == "Ctrl+Space"
    fs = _load(machine["vscode"][0])["servers"]["fs"]
    assert fs["env"]["TOKEN"] == "${input:tok}"
    assert fs["type"] == "stdio"
    assert "--client" in _load(machine["cursor"][0])["mcpServers"]["pg"]["args"]


def test_init_adds_one_claude_code_hook_and_keeps_existing_ones(machine):
    c.cmd_init(_args())
    s = _load(machine["claude"][0])
    groups = s["hooks"]["PostToolUse"]
    assert groups[0]["matcher"] == "Edit", "the user's own hook survives"
    ours = groups[-1]
    assert ours["matcher"] == "*"
    assert ours["hooks"][0]["command"] == \
        '"/opt/tools/hypothesise/bin/python" -m hypothesise hook claude-code'
    assert s["permissions"] == ORIGINAL_CLAUDE["permissions"]


def test_init_is_idempotent(machine):
    c.cmd_init(_args())
    snap = {k: _load(p) for k, (p, _) in machine.items()}
    c.cmd_init(_args())
    assert {k: _load(p) for k, (p, _) in machine.items()} == snap


def test_uninstall_restores_every_file_exactly(machine):
    c.cmd_init(_args())
    assert c.cmd_uninstall(_args()) == 0
    for _key, (path, original) in machine.items():
        assert _load(path) == original, path


def test_unwrap_needs_no_backup(machine):
    """The original command rides along after `--`. Losing ~/.hypothesise
    must not strand anyone with wrapped configs."""
    import shutil

    c.cmd_init(_args())
    shutil.rmtree(c.home())
    c.cmd_uninstall(_args())
    assert _load(machine["cursor"][0]) == ORIGINAL_CURSOR


def test_backups_are_written_before_changes(machine):
    c.cmd_init(_args())
    backups = list((c.home() / "backups").iterdir())
    assert len(backups) == 4
    desk = next(b for b in backups if "claude-desktop" in b.name)
    assert json.loads(desk.read_text()) == ORIGINAL_DESKTOP


def test_dry_run_changes_nothing(machine):
    before = {k: Path(p).read_text() for k, (p, _) in machine.items()}
    assert c.cmd_init(_args(dry_run=True)) == 0
    assert {k: Path(p).read_text() for k, (p, _) in machine.items()} == before


def test_client_filter(machine):
    c.cmd_init(_args(client=["cursor"]))
    assert _load(machine["desktop"][0]) == ORIGINAL_DESKTOP
    assert _load(machine["claude"][0]) == ORIGINAL_CLAUDE
    assert c.is_wrapped(_load(machine["cursor"][0])["mcpServers"]["pg"])


def test_jsonc_is_left_untouched(machine, capsys):
    path = machine["vscode"][0]
    text = '// my servers\n{"servers": {"fs": {"command": "npx"}}}\n'
    path.write_text(text)
    c.cmd_init(_args())
    assert path.read_text() == text
    assert "not plain JSON" in capsys.readouterr().out


def test_project_level_configs(machine, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".vscode").mkdir(parents=True)
    (proj / ".vscode" / "mcp.json").write_text(json.dumps(
        {"servers": {"db": {"command": "uvx", "args": ["mcp-sqlite"]}}}))
    c.cmd_init(_args(project=str(proj)))
    assert c.is_wrapped(_load(proj / ".vscode" / "mcp.json")["servers"]["db"])


def test_refuses_a_temporary_interpreter(machine, monkeypatch):
    monkeypatch.setattr(c, "interpreter",
                        lambda: "/home/u/.cache/uv/archive-v0/abc/bin/python")
    assert c.cmd_init(_args()) == 2
    assert _load(machine["cursor"][0]) == ORIGINAL_CURSOR


def test_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(c, "_windows", lambda: False)
    monkeypatch.setattr(c.shutil, "which", lambda _n: None)
    monkeypatch.chdir(tmp_path)
    assert c.cmd_init(_args()) == 1


def test_status_reports_connections(machine, capsys):
    c.cmd_init(_args(client=["cursor"]))
    capsys.readouterr()
    assert c.cmd_status(_args()) == 0
    out = capsys.readouterr().out
    assert "1/1 MCP servers connected" in out
    assert "not yet: github" in out


@pytest.mark.parametrize("platform,osname,expect", [
    ("darwin", "posix", "Library/Application Support"),
    ("win32", "nt", "AppData/Roaming"),
    ("linux", "posix", ".config"),
])
def test_config_locations_per_os(monkeypatch, tmp_path, platform, osname, expect):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(c, "_windows", lambda: osname == "nt")
    desktop = c.mcp_files()[0].path.as_posix()
    assert expect in desktop and desktop.endswith("Claude/claude_desktop_config.json")


def test_hook_command_quotes_paths_with_spaces():
    cmd = c.hook_command("/Users/Pratham Agarwal/.local/bin/python")
    assert cmd == '"/Users/Pratham Agarwal/.local/bin/python" -m hypothesise hook claude-code'


def test_hook_command_uses_forward_slashes_on_windows():
    from pathlib import PureWindowsPath

    assert PureWindowsPath("C:\\Users\\P A\\python.exe").as_posix() == "C:/Users/P A/python.exe"


def test_an_old_weaver_proxy_entry_is_upgraded_not_wrapped(machine, tmp_path, monkeypatch):
    """The original setup — one proxy entry fronting fs + db, shipping live to
    the dashboard — keeps working after the rename. init upgrades it in place."""
    monkeypatch.setattr(c, "_can_import", lambda _py: True)
    cfgdir = tmp_path / "AI_Layer"
    cfgdir.mkdir()
    (cfgdir / "hypothesise.config.json").write_text("{}")
    path = machine["cursor"][0]
    entry = {"type": "stdio", "command": "/u/sagar_env/bin/python",
             "args": ["-m", "weaver", "proxy", "--config", str(cfgdir / "weaver.config.json"),
                      "--detect", "--api", "http://127.0.0.1:8080"]}
    path.write_text(json.dumps({"mcpServers": {"weaver": entry}}))
    c.cmd_init(_args(client=["cursor"]))
    got = _load(path)["mcpServers"]["weaver"]
    assert got["command"] == "/u/sagar_env/bin/python", "a working interpreter is kept"
    assert got["args"] == ["-m", "hypothesise", "proxy", "--config",
                           str(cfgdir / "hypothesise.config.json"),
                           "--detect", "--api", "http://127.0.0.1:8080"]
    assert not c.is_wrapped(got)
    # Second run: recognised as connected, left alone.
    c.cmd_init(_args(client=["cursor"]))
    assert _load(path)["mcpServers"]["weaver"] == got


def test_upgrade_swaps_an_interpreter_that_cannot_import_hypothesise():
    entry = {"command": "/old/env/python", "args": ["-m", "weaver", "proxy"]}
    got = c.upgrade_legacy_entry(entry, "/new/python", can_import=lambda _p: False)
    assert got["command"] == "/new/python"
    assert got["args"] == ["-m", "hypothesise", "proxy"]


def test_upgrade_handles_the_old_console_scripts():
    got = c.upgrade_legacy_entry({"command": "/x/bin/weaver-proxy", "args": ["--config", "a.json"]},
                                 "/new/python", can_import=lambda _p: True)
    assert got == {"command": "/new/python",
                   "args": ["-m", "hypothesise_proxy", "--config", "a.json"]}


def test_a_current_proxy_entry_is_left_alone_and_counted(machine, capsys):
    path = machine["cursor"][0]
    entry = {"command": "/env/bin/python",
             "args": ["-m", "hypothesise", "proxy", "--config", "/x/hypothesise.config.json",
                      "--detect", "--api", "http://127.0.0.1:8080"]}
    path.write_text(json.dumps({"mcpServers": {"guard": entry, "pg": {"command": "uvx"}}}))
    c.cmd_init(_args(client=["cursor"]))
    servers = _load(path)["mcpServers"]
    assert servers["guard"] == entry
    assert c.is_wrapped(servers["pg"]), "both setups side by side in one file"
    capsys.readouterr()
    c.cmd_status(_args())
    assert "proxy entry: guard" in capsys.readouterr().out
    c.cmd_uninstall(_args(client=["cursor"]))
    assert _load(path)["mcpServers"]["guard"] == entry, "uninstall never removes a hand-written entry"


def test_init_adds_the_copilot_hook_and_uninstall_removes_it(machine, capsys):
    """Copilot's built-in tools are 'Always Available' and bypass MCP; the
    VS Code agent hook is the only way to see them (ADR-038)."""
    c.cmd_init(_args())
    hook = c.copilot_hook_file()
    data = _load(hook)
    (entry,) = data["hooks"]["PostToolUse"]
    assert entry["type"] == "command"
    assert entry["command"] == '"/opt/tools/hypothesise/bin/python" -m hypothesise hook vscode'
    capsys.readouterr()
    c.cmd_status(_args())
    assert "connected (built-in tools)" in capsys.readouterr().out
    c.cmd_init(_args())
    assert _load(hook) == data, "idempotent"
    c.cmd_uninstall(_args())
    assert not hook.exists()


def test_copilot_hook_is_not_offered_without_vscode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(c, "_windows", lambda: False)
    monkeypatch.setattr(c.shutil, "which", lambda _n: None)
    assert c.plan_copilot_hook("/x/python") is None
