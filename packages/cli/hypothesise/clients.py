"""Connect Hypothesise to the agent apps already on this machine.

    hypothesise init         find agent apps, show the plan, apply it
    hypothesise uninstall    put every file back the way it was
    hypothesise status       what is connected, what is waiting to be read

Two mechanisms, chosen per app:

* **Claude Desktop, Cursor, VS Code** — each stdio MCP server entry is wrapped
  *in place*: the same key, the same env, the same everything, except that the
  command becomes `python -m hypothesise wrap ... -- <original command>`. Tool
  names and server names are unchanged, so the user's prompts, allowlists and
  per-server toggles keep working. The original command is carried verbatim
  after `--`, so unwrapping needs no state at all — a lost backup cannot strand
  anyone.
* **Claude Code** — a PostToolUse hook in ~/.claude/settings.json. The hook
  sees built-in tools *and* MCP tools under Claude's own session id, which is
  strictly more than wrapping its servers would, so its MCP config is left
  untouched.

Every file is backed up before it is written, and written atomically. A file
that is not plain JSON (VS Code allows comments) is reported and left alone
rather than rewritten without its comments.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WRAP = ["-m", "hypothesise", "wrap"]
HOOK_MARK = "-m hypothesise hook"
CLAUDE_CODE = "claude-code"
CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "antigravity")


# -- where things live ------------------------------------------------------

def home() -> Path:
    return Path(os.environ.get("HYPOTHESISE_HOME", "~/.hypothesise")).expanduser()


def _windows() -> bool:
    return os.name == "nt"


def user_config_base() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if _windows():
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


@dataclass
class McpFile:
    client: str      # agent id the baseline is keyed on
    label: str
    path: Path
    key: str         # "mcpServers" (Claude/Cursor) or "servers" (VS Code)


def mcp_files(project: Path | None = None) -> list[McpFile]:
    base = user_config_base()
    files = [
        McpFile("claude-desktop", "Claude Desktop",
                base / "Claude" / "claude_desktop_config.json", "mcpServers"),
        McpFile("cursor", "Cursor", Path.home() / ".cursor" / "mcp.json", "mcpServers"),
        McpFile("vscode", "VS Code", base / "Code" / "User" / "mcp.json", "servers"),
        McpFile("vscode", "VS Code Insiders",
                base / "Code - Insiders" / "User" / "mcp.json", "servers"),
        # Antigravity (Google). The shared config feeds both its IDE and CLI;
        # earlier releases used the per-app path. Same mcpServers shape as
        # Claude/Cursor. Its built-in tools have no hooks, so only MCP is seen.
        McpFile("antigravity", "Antigravity",
                Path.home() / ".gemini" / "config" / "mcp_config.json", "mcpServers"),
        McpFile("antigravity", "Antigravity (older layout)",
                Path.home() / ".gemini" / "antigravity" / "mcp_config.json", "mcpServers"),
    ]
    if project is not None:
        files += [
            McpFile("cursor", "Cursor (this project)",
                    project / ".cursor" / "mcp.json", "mcpServers"),
            McpFile("vscode", "VS Code (this workspace)",
                    project / ".vscode" / "mcp.json", "servers"),
        ]
    return files


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def claude_settings() -> Path:
    return claude_dir() / "settings.json"


def copilot_hook_file() -> Path:
    """User-level Copilot agent hook (VS Code agent hooks, ADR-038)."""
    return Path.home() / ".copilot" / "hooks" / "hypothesise.json"


def vscode_present() -> bool:
    base = user_config_base()
    return ((base / "Code").is_dir() or (base / "Code - Insiders").is_dir()
            or shutil.which("code") is not None)


def shell_safe_exe(exe: str) -> str:
    """The interpreter path written so every shell runs it the same way.

    Agents run hook commands through whatever shell they like — PowerShell on
    Windows for VS Code, Git Bash for Claude Code, sh elsewhere. A quoted path
    followed by arguments works in cmd and sh but is a *parse error* in
    PowerShell, and a hook that fails is silent by design — so the first
    Windows install recorded nothing at all. An unquoted path with no spaces
    means the same thing to every shell: use it, via the 8.3 short name on
    Windows when the real path has spaces. Quote only as a last resort.
    """
    path = Path(exe).as_posix()
    if " " not in path:
        return path
    if _windows():
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(exe, buf, 1024):  # type: ignore[attr-defined]
                short = Path(buf.value).as_posix()
                if short and " " not in short:
                    return short
        except (AttributeError, OSError):
            pass
    return f'"{path}"'


def cursor_hooks_file() -> Path:
    """User-level Cursor hooks (cursor.com/docs/hooks), shared with the user's own."""
    return Path.home() / ".cursor" / "hooks.json"


def cursor_present() -> bool:
    return (Path.home() / ".cursor").is_dir() or shutil.which("cursor") is not None


def cursor_hook_command(exe: str) -> str:
    return f"{shell_safe_exe(exe)} {HOOK_MARK} cursor"


def copilot_hook_command(exe: str) -> str:
    return f"{shell_safe_exe(exe)} {HOOK_MARK} vscode"


def claude_code_present() -> bool:
    return claude_dir().is_dir() or shutil.which("claude") is not None


# -- the interpreter every wrapped entry will name --------------------------

def interpreter() -> str:
    exe = sys.executable
    if _windows():
        # pythonw has no console streams; MCP needs stdio.
        exe = exe.replace("pythonw.exe", "python.exe")
    return exe


def ephemeral_interpreter(exe: str) -> bool:
    """True for interpreters that will not exist tomorrow (uvx, pipx run).

    Writing one of those into a config would break every wrapped server the
    next time the cache is cleaned.
    """
    p = exe.replace("\\", "/").lower()
    return any(m in p for m in ("/archive-v0/", "/uv/cache/", "/pipx/.cache/", "/.cache/pipx/"))


def hook_command(exe: str) -> str:
    # Forward slashes work in cmd, PowerShell and the Git Bash Claude Code
    # uses on Windows; see shell_safe_exe for why the path is not quoted.
    return f"{shell_safe_exe(exe)} {HOOK_MARK} {CLAUDE_CODE}"


# -- entries ----------------------------------------------------------------

def is_wrapped(entry: Any) -> bool:
    return isinstance(entry, dict) and list(entry.get("args") or [])[:3] == WRAP


# A hand-written proxy entry — the original setup, still fully supported:
#     python -m hypothesise proxy --config hypothesise.config.json --detect --api URL
# One entry fronting several servers, shipping live to the dashboard. `init`
# leaves these alone (wrapping one would put the proxy behind itself) and
# upgrades the pre-rename `-m weaver` form in place (ADR-037).
_CURRENT_MODULES = {"hypothesise": "hypothesise", "hypothesise_proxy": "hypothesise_proxy"}
_LEGACY_MODULES = {"weaver": "hypothesise", "weaver_proxy": "hypothesise_proxy"}
_LEGACY_SCRIPTS = {"weaver": ["-m", "hypothesise"], "weaver-proxy": ["-m", "hypothesise_proxy"]}
_CURRENT_SCRIPTS = {"hypothesise", "hypothesise-proxy"}


def _module_arg(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "-m" and i + 1 < len(args):
            return args[i + 1]
    return None


def _script_name(entry: dict) -> str:
    name = Path(str(entry.get("command", "")).replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def is_proxy_entry(entry: Any) -> bool:
    """Current-name proxy entry: connected already, by hand."""
    if not isinstance(entry, dict) or is_wrapped(entry):
        return False
    args = [str(a) for a in entry.get("args") or []]
    return _script_name(entry) in _CURRENT_SCRIPTS or _module_arg(args) in _CURRENT_MODULES


def is_legacy_proxy(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    args = [str(a) for a in entry.get("args") or []]
    return _script_name(entry) in _LEGACY_SCRIPTS or _module_arg(args) in _LEGACY_MODULES


def is_own_proxy(entry: Any) -> bool:
    return is_proxy_entry(entry) or is_legacy_proxy(entry)


def _can_import(python: str) -> bool:
    import subprocess

    try:
        return subprocess.run([python, "-c", "import hypothesise"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _renamed_config(path: str) -> str:
    """weaver.config.json -> hypothesise.config.json, when the new file exists."""
    p = Path(path)
    if p.name != "weaver.config.json":
        return path
    new = p.with_name("hypothesise.config.json")
    return str(new) if new.is_file() or not p.is_file() else path


def upgrade_legacy_entry(entry: dict, exe: str, can_import: Any = None) -> dict:
    """Rewrite a pre-rename proxy entry to the current names, keeping its shape.

    The interpreter is kept when it can import hypothesise (it is usually the
    developer's own environment, ADR-032) and replaced with this one otherwise.
    """
    can_import = can_import or _can_import
    new = dict(entry)
    args = [str(a) for a in entry.get("args") or []]
    script = _script_name(entry)
    if script in _LEGACY_SCRIPTS:
        new["command"] = exe
        args = [*_LEGACY_SCRIPTS[script], *args]
    else:
        for i, a in enumerate(args[:-1]):
            if a == "-m" and args[i + 1] in _LEGACY_MODULES:
                args[i + 1] = _LEGACY_MODULES[args[i + 1]]
                break
        if not can_import(str(entry.get("command", ""))):
            new["command"] = exe
    for i, a in enumerate(args[:-1]):
        if a == "--config":
            args[i + 1] = _renamed_config(args[i + 1])
    new["args"] = args
    return new


def is_stdio(entry: Any) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("command"), str)
            and bool(entry["command"]) and not entry.get("url")
            and entry.get("type", "stdio") == "stdio")


def wrap_entry(entry: dict, client: str, name: str, exe: str) -> dict:
    new = dict(entry)
    new["command"] = exe
    new["args"] = [*WRAP, "--client", client, "--server", name, "--",
                   entry["command"], *[str(a) for a in entry.get("args") or []]]
    return new


def unwrap_entry(entry: dict) -> dict | None:
    args = list(entry.get("args") or [])
    try:
        i = args.index("--")
    except ValueError:
        return None
    rest = args[i + 1:]
    if not rest:
        return None
    new = dict(entry)
    new["command"] = rest[0]
    new["args"] = rest[1:]
    return new


# -- json files -------------------------------------------------------------

def read_json(path: Path) -> tuple[dict | None, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read: {exc}"
    if not text.strip():
        return {}, ""
    try:
        data = json.loads(text)
    except ValueError:
        return None, "not plain JSON (comments?) — left untouched, edit it by hand"
    if not isinstance(data, dict):
        return None, "unexpected shape — left untouched"
    return data, ""


def backup(path: Path, client: str) -> Path | None:
    if not path.exists():
        return None
    d = home() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{client}-{path.name}"
    shutil.copy2(path, dest)
    return dest


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.hypothesise.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# -- planning ---------------------------------------------------------------

@dataclass
class Change:
    label: str
    client: str
    path: Path
    kind: str                                   # "wrap" | "unwrap" | "hook" | "unhook"
    servers: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    data: dict | None = None                    # the new file contents
    error: str = ""

    @property
    def writes(self) -> bool:
        if self.kind == "remove":
            return not self.error
        return self.data is not None and not self.error and (
            bool(self.servers) or self.kind in ("hook", "unhook"))


def _wanted(client: str, only: list[str] | None) -> bool:
    return not only or client in only


def plan_wrap(f: McpFile, exe: str) -> Change | None:
    if not f.path.is_file():
        return None
    ch = Change(f.label, f.client, f.path, "wrap")
    data, err = read_json(f.path)
    if data is None:
        ch.error = err
        return ch
    servers = data.get(f.key)
    if not isinstance(servers, dict) or not servers:
        ch.error = "no MCP servers configured"
        return ch
    new_servers = {}
    for name, entry in servers.items():
        if is_wrapped(entry):
            ch.skipped.append((name, "already connected"))
            new_servers[name] = entry
        elif is_proxy_entry(entry):
            ch.skipped.append((name, "already connected (hypothesise proxy entry)"))
            new_servers[name] = entry
        elif is_legacy_proxy(entry):
            new_servers[name] = upgrade_legacy_entry(entry, exe)
            ch.servers.append(f"{name}  (upgrade old `weaver` proxy entry to hypothesise)")
        elif not is_stdio(entry):
            ch.skipped.append((name, "remote (http/sse) server — not interceptable yet"))
            new_servers[name] = entry
        else:
            new_servers[name] = wrap_entry(entry, f.client, name, exe)
            ch.servers.append(name)
    ch.data = {**data, f.key: new_servers}
    return ch


def plan_unwrap(f: McpFile) -> Change | None:
    if not f.path.is_file():
        return None
    ch = Change(f.label, f.client, f.path, "unwrap")
    data, err = read_json(f.path)
    if data is None:
        ch.error = err
        return ch
    servers = data.get(f.key)
    if not isinstance(servers, dict):
        return None
    new_servers = {}
    for name, entry in servers.items():
        if is_wrapped(entry):
            orig = unwrap_entry(entry)
            if orig is None:
                ch.skipped.append((name, "wrapped entry has no original command"))
                new_servers[name] = entry
            else:
                new_servers[name] = orig
                ch.servers.append(name)
        else:
            new_servers[name] = entry
    if not ch.servers and not ch.skipped:
        return None
    ch.data = {**data, f.key: new_servers}
    return ch


def _hook_groups(settings: dict) -> list:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    post = hooks.get("PostToolUse")
    return post if isinstance(post, list) else []


def has_hook(settings: dict) -> bool:
    for group in _hook_groups(settings):
        for h in (group or {}).get("hooks") or []:
            if HOOK_MARK in str((h or {}).get("command", "")):
                return True
    return False


def plan_hook(exe: str) -> Change | None:
    if not claude_code_present():
        return None
    path = claude_settings()
    ch = Change("Claude Code", CLAUDE_CODE, path, "hook")
    data, err = read_json(path) if path.exists() else ({}, "")
    if data is None:
        ch.error = err
        return ch
    if has_hook(data):
        want = hook_command(exe)
        new = json.loads(json.dumps(data))
        stale = False
        for group in _hook_groups(new):
            for h in (group or {}).get("hooks") or []:
                if HOOK_MARK in str((h or {}).get("command", "")) and h.get("command") != want:
                    h["command"] = want
                    stale = True
        if not stale:
            ch.skipped.append(("all tools", "already connected"))
            return ch
        ch.data = new
        ch.servers.append("all tools (repair outdated hook command)")
        return ch
    new = json.loads(json.dumps(data))
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        ch.error = "'hooks' in settings.json is not an object — left untouched"
        return ch
    post = hooks.setdefault("PostToolUse", [])
    post.append({"matcher": "*", "hooks": [
        {"type": "command", "command": hook_command(exe), "timeout": 10}]})
    ch.data = new
    ch.servers.append("all tools (built-in + MCP)")
    return ch


def plan_copilot_hook(exe: str) -> Change | None:
    """Copilot's built-in tools (view, read_file, bash…) cannot be switched off
    and never touch MCP. A PostToolUse agent hook is the only way to see them."""
    if not vscode_present():
        return None
    path = copilot_hook_file()
    ch = Change("VS Code Copilot (built-in tools)", "vscode", path, "hook")
    want = {"hooks": {"PostToolUse": [
        {"type": "command", "command": copilot_hook_command(exe), "timeout": 10}]}}
    if path.is_file():
        data, _ = read_json(path)
        if data == want:
            ch.skipped.append(("all tools", "already connected"))
            return ch
        if data is not None and HOOK_MARK in json.dumps(data):
            ch.data = want
            ch.servers.append("all tools (repair outdated hook command)")
            return ch
    ch.data = want
    ch.servers.append("all tools (built-in; MCP stays on the wrapped servers)")
    return ch


def plan_copilot_unhook() -> Change | None:
    path = copilot_hook_file()
    if not path.is_file():
        return None
    data, _ = read_json(path)
    if data is None or HOOK_MARK not in json.dumps(data):
        return None
    ch = Change("VS Code Copilot (built-in tools)", "vscode", path, "remove")
    ch.servers.append("all tools hook")
    return ch


def _cursor_entries(data: dict) -> list:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    post = hooks.get("postToolUse")
    return post if isinstance(post, list) else []


def plan_cursor_hook(exe: str) -> Change | None:
    """Cursor's built-in tools (read, edit, terminal) bypass MCP; its
    postToolUse hook reports every one (ADR-040). The user's own hooks in the
    same file are kept untouched."""
    if not cursor_present():
        return None
    path = cursor_hooks_file()
    ch = Change("Cursor (built-in tools)", "cursor", path, "hook")
    data, err = read_json(path) if path.exists() else ({}, "")
    if data is None:
        ch.error = err
        return ch
    want = cursor_hook_command(exe)
    new = json.loads(json.dumps(data))
    new.setdefault("version", 1)
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        ch.error = "'hooks' in hooks.json is not an object — left untouched"
        return ch
    post = hooks.setdefault("postToolUse", [])
    ours = [h for h in post if isinstance(h, dict) and HOOK_MARK in str(h.get("command", ""))]
    if ours and all(h.get("command") == want for h in ours):
        ch.skipped.append(("all tools", "already connected"))
        return ch
    if ours:
        for h in ours:
            h["command"] = want
        ch.servers.append("all tools (repair outdated hook command)")
    else:
        post.append({"command": want, "timeout": 10})
        ch.servers.append("all tools (built-in; MCP stays on the wrapped servers)")
    ch.data = new
    return ch


def plan_cursor_unhook() -> Change | None:
    path = cursor_hooks_file()
    if not path.is_file():
        return None
    data, _ = read_json(path)
    if data is None or not any(HOOK_MARK in str((h or {}).get("command", ""))
                               for h in _cursor_entries(data)):
        return None
    new = json.loads(json.dumps(data))
    kept = [h for h in _cursor_entries(new) if HOOK_MARK not in str((h or {}).get("command", ""))]
    if kept:
        new["hooks"]["postToolUse"] = kept
    else:
        new["hooks"].pop("postToolUse", None)
    if new.get("hooks") == {} and set(new) <= {"version", "hooks"}:
        ch = Change("Cursor (built-in tools)", "cursor", path, "remove")
    else:
        ch = Change("Cursor (built-in tools)", "cursor", path, "unhook", data=new)
    ch.servers.append("all tools hook")
    return ch


def plan_unhook() -> Change | None:
    path = claude_settings()
    if not path.is_file():
        return None
    data, err = read_json(path)
    if data is None or not has_hook(data):
        return None
    new = json.loads(json.dumps(data))
    groups = []
    for group in _hook_groups(new):
        kept = [h for h in (group or {}).get("hooks") or []
                if HOOK_MARK not in str((h or {}).get("command", ""))]
        if kept:
            groups.append({**group, "hooks": kept})
    if groups:
        new["hooks"]["PostToolUse"] = groups
    else:
        new["hooks"].pop("PostToolUse", None)
        if not new["hooks"]:
            new.pop("hooks")
    ch = Change("Claude Code", CLAUDE_CODE, path, "unhook", data=new)
    ch.servers.append("all tools hook")
    return ch


# -- commands ---------------------------------------------------------------

def _print_plan(changes: list[Change], verb: str) -> None:
    for ch in changes:
        print(f"\n  {ch.label}  ({ch.path})")
        if ch.error:
            print(f"    skipped: {ch.error}")
            continue
        for s in ch.servers:
            print(f"    {verb:<9} {s}")
        for name, why in ch.skipped:
            print(f"    {'skip':<9} {name} — {why}")


def _confirm(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("\nnot a terminal: re-run with --yes to apply")
        return False
    try:
        return input("\nApply these changes? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _apply(changes: list[Change]) -> int:
    n = 0
    for ch in changes:
        if ch.kind == "remove" and ch.writes:
            try:
                ch.path.unlink()
                n += 1
                print(f"  removed {ch.path}")
            except OSError as exc:
                print(f"  FAILED  {ch.path}: {exc}", file=sys.stderr)
            continue
        if not ch.writes or ch.data is None:
            continue
        try:
            saved = backup(ch.path, ch.client)
            write_json(ch.path, ch.data)
            n += 1
            print(f"  updated {ch.path}" + (f"  (backup: {saved})" if saved else ""))
        except OSError as exc:
            print(f"  FAILED  {ch.path}: {exc}", file=sys.stderr)
    return n


def cmd_init(args: argparse.Namespace) -> int:
    exe = interpreter()
    if ephemeral_interpreter(exe) and not args.force:
        print("hypothesise: this copy is running from a temporary cache "
              f"({exe}).\nInstall it permanently first, then run init:\n\n"
              "    uv tool install hypothesise      (or: pipx install hypothesise)\n"
              "    hypothesise init\n", file=sys.stderr)
        return 2

    only = args.client or None
    project = Path(args.project).resolve() if args.project else Path.cwd()
    changes: list[Change] = []
    if _wanted(CLAUDE_CODE, only):
        ch = plan_hook(exe)
        if ch:
            changes.append(ch)
    if _wanted("vscode", only):
        ch = plan_copilot_hook(exe)
        if ch:
            changes.append(ch)
    if _wanted("cursor", only):
        ch = plan_cursor_hook(exe)
        if ch:
            changes.append(ch)
    seen: set[Path] = set()
    for f in mcp_files(project):
        if not _wanted(f.client, only) or f.path in seen:
            continue
        seen.add(f.path)
        ch = plan_wrap(f, exe)
        if ch:
            changes.append(ch)

    if not changes:
        print("hypothesise: no supported agent apps found on this machine.")
        print("  looked for: Claude Code (~/.claude), Claude Desktop, Cursor, VS Code, Antigravity")
        print("  add an MCP server to one of them, then run `hypothesise init` again.")
        return 1

    print("hypothesise init — observe mode: nothing will be blocked\n")
    print(f"  interpreter: {exe}")
    _print_plan(changes, "connect")
    if not any(c.writes for c in changes):
        print("\nnothing to change.")
        return 0
    if args.dry_run:
        print("\n--dry-run: no files were changed.")
        return 0
    if not _confirm(args.yes):
        print("no changes made.")
        return 1

    home().mkdir(parents=True, exist_ok=True)
    print()
    _apply(changes)
    print("\nNext:")
    print("  1. Restart the apps above (fully quit Claude Desktop / Cursor / VS Code).")
    if any(c.kind == "hook" and c.client == "vscode" and c.writes for c in changes):
        print("     VS Code: check Settings → `chat.useHooks` is on (it is by default), and the")
        print("     Copilot panel's Hooks page lists hypothesise.json.")
    print("  2. Use your agents normally. Events are recorded to "
          f"{home() / 'spool'} even while the dashboard is closed.")
    print("  3. hypothesise api     → open http://127.0.0.1:8080 to see them")
    print("  Undo everything any time:  hypothesise uninstall")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    only = args.client or None
    project = Path(args.project).resolve() if args.project else Path.cwd()
    changes: list[Change] = []
    if _wanted(CLAUDE_CODE, only):
        ch = plan_unhook()
        if ch:
            changes.append(ch)
    if _wanted("vscode", only):
        ch = plan_copilot_unhook()
        if ch:
            changes.append(ch)
    if _wanted("cursor", only):
        ch = plan_cursor_unhook()
        if ch:
            changes.append(ch)
    seen: set[Path] = set()
    for f in mcp_files(project):
        if not _wanted(f.client, only) or f.path in seen:
            continue
        seen.add(f.path)
        ch = plan_unwrap(f)
        if ch:
            changes.append(ch)
    if not any(c.writes for c in changes):
        print("hypothesise: nothing is connected — nothing to undo.")
        return 0
    print("hypothesise uninstall — restoring original configs")
    _print_plan(changes, "restore")
    if args.dry_run:
        print("\n--dry-run: no files were changed.")
        return 0
    if not _confirm(args.yes):
        print("no changes made.")
        return 1
    print()
    _apply(changes)
    print("\nRestart the apps above. Your data in "
          f"{home()} is kept; delete that folder to remove it too.")
    return 0


def _db_events() -> int | None:
    import sqlite3

    db = home() / "hypothesise.db"
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1) as conn:
            return int(conn.execute("select count(*) from events").fetchone()[0])
    except sqlite3.Error:
        return None


def cmd_status(args: argparse.Namespace) -> int:
    from hypothesise_proxy.spool import SpoolReader, spool_dir

    project = Path(args.project).resolve() if args.project else Path.cwd()
    print("hypothesise status\n")
    found = False
    if claude_code_present():
        found = True
        data, _ = read_json(claude_settings()) if claude_settings().exists() else ({}, "")
        state = "connected (all tools)" if data and has_hook(data) else "not connected"
        print(f"  {'Claude Code':<26} {state}")
    if vscode_present():
        found = True
        f = copilot_hook_file()
        data, _ = read_json(f) if f.is_file() else (None, "")
        on = data is not None and HOOK_MARK in json.dumps(data)
        print(f"  {'VS Code Copilot hook':<26} "
              f"{'connected (built-in tools)' if on else 'not connected'}")
    if cursor_present():
        found = True
        f = cursor_hooks_file()
        data, _ = read_json(f) if f.is_file() else (None, "")
        on = data is not None and any(HOOK_MARK in str((h or {}).get("command", ""))
                                      for h in _cursor_entries(data))
        print(f"  {'Cursor hook':<26} "
              f"{'connected (built-in tools)' if on else 'not connected'}")
    seen: set[Path] = set()
    for f in mcp_files(project):
        if f.path in seen or not f.path.is_file():
            continue
        seen.add(f.path)
        found = True
        data, err = read_json(f.path)
        servers = (data or {}).get(f.key) or {}
        if err or not isinstance(servers, dict):
            print(f"  {f.label:<26} {err or 'no servers'}")
            continue
        wrapped = [n for n, e in servers.items() if is_wrapped(e)]
        proxies = [n for n, e in servers.items() if is_proxy_entry(e)]
        legacy = [n for n, e in servers.items() if is_legacy_proxy(e)]
        open_ = [n for n, e in servers.items()
                 if is_stdio(e) and not is_wrapped(e) and not is_own_proxy(e)]
        line = f"{len(wrapped)}/{len(wrapped) + len(open_)} MCP servers connected"
        if proxies:
            line += f"  + proxy entry: {', '.join(proxies)}"
        if open_:
            line += f"  — not yet: {', '.join(open_)} (run `hypothesise init`)"
        if legacy:
            line += f"  — OLD weaver entry: {', '.join(legacy)} (run `hypothesise init` to upgrade)"
        print(f"  {f.label:<26} {line}")
    if not found:
        print("  no supported agent apps found")

    pending = SpoolReader().pending_bytes()
    print(f"\n  data folder   {home()}")
    print(f"  waiting       {pending / 1024:.1f} KB in {spool_dir()} "
          "(read when the dashboard runs)")
    n = _db_events()
    print(f"  recorded      {n if n is not None else 0} events in the dashboard database")
    return 0
