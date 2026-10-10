#!/usr/bin/env python3
"""Validate (and optionally fix) an MCP client config that points at Hypothesise.

    python3 scripts/ops/check-client-config.py ~/proj/.vscode/mcp.json
    python3 scripts/ops/check-client-config.py ~/proj/.vscode/mcp.json --fix

Run it with the interpreter you actually installed Hypothesise into. That is the
whole point: the config names an absolute path to an executable, the client
launches it with none of your shell's environment, and if that path is wrong
the client reports "server exited" and nothing else. Every install failure in
this project so far has been an interpreter-identity problem (ADR-014,
ADR-031, ADR-032), and this is the one place a wrong interpreter is invisible
until an agent session fails.

`--fix` rewrites each Hypothesise server entry to `sys.executable -m hypothesise`, which
is correct by construction: the interpreter running this script is the one that
can import hypothesise, and `-m` needs no console-script shim on PATH.

Imports nothing from hypothesise — it has to work when hypothesise is the broken thing.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"

# Mach-O thin (both endians), Mach-O universal, ELF.
_MACHO = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
          b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe"}
_ELF = b"\x7fELF"


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.warned = False

    def line(self, level: str, msg: str) -> None:
        print(f"  {level}    {msg}")
        if level == FAIL:
            self.failed = True
        elif level == WARN:
            self.warned = True

    def note(self, msg: str) -> None:
        print(f"          {msg}")


def servers_of(doc: dict[str, Any]) -> dict[str, Any]:
    """VS Code uses `servers`; Claude Code and Cursor use `mcpServers`."""
    for key in ("servers", "mcpServers"):
        block = doc.get(key)
        if isinstance(block, dict):
            return block
    return {}


def is_hypothesise_entry(name: str, entry: dict[str, Any]) -> bool:
    if "hypothesise" in name.lower():
        return True
    blob = " ".join([str(entry.get("command", "")), *map(str, entry.get("args", []))])
    return "hypothesise" in blob.lower()


def shebang_of(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    first = head.split(b"\n", 1)[0][2:].strip()
    parts = first.decode("utf-8", "replace").split()
    if not parts:
        return None
    # `#!/usr/bin/env python3` — the interpreter is the argument.
    if parts[0].endswith("/env") and len(parts) > 1:
        return parts[1]
    return parts[0]


def platform_of(path: Path) -> str | None:
    """'macho', 'elf', 'script' or None. A config that survived a move between
    machines usually still points at a binary for the machine it left."""
    try:
        with path.open("rb") as fh:
            magic = fh.read(4)
    except OSError:
        return None
    if magic.startswith(b"#!"):
        return "script"
    if magic == _ELF:
        return "elf"
    if magic in _MACHO:
        return "macho"
    return None


def native_format() -> str:
    return "macho" if sys.platform == "darwin" else "elf"


def check_command(rep: Report, entry: dict[str, Any]) -> Path | None:
    raw = entry.get("command")
    if not isinstance(raw, str) or not raw:
        rep.line(FAIL, "no `command` in this server entry")
        return None

    cmd = Path(os.path.expanduser(raw))
    if not cmd.is_absolute():
        rep.line(WARN, f"command `{raw}` is not absolute")
        rep.note("MCP clients do not inherit your shell PATH or activated venv.")
        found = None
        for d in os.environ.get("PATH", "").split(os.pathsep):
            cand = Path(d) / raw
            if cand.exists():
                found = cand
                break
        if found is None:
            rep.line(FAIL, f"and `{raw}` is not on this shell's PATH either")
            return None
        cmd = found

    if not cmd.exists():
        rep.line(FAIL, f"command does not exist: {cmd}")
        rep.note("This is what a client reports as 'server exited' with no log.")
        return None
    if not os.access(cmd, os.X_OK):
        rep.line(FAIL, f"command is not executable: {cmd}")
        return None
    rep.line(OK, f"command exists and is executable: {cmd}")

    fmt = platform_of(cmd)
    if fmt in ("macho", "elf") and fmt != native_format():
        rep.line(FAIL, f"command is a {fmt} binary; this host needs {native_format()}")
        rep.note("A venv created on another machine — even on a shared drive —")
        rep.note("cannot run here. Recreate it on this host.")
        return None

    interp = shebang_of(cmd)
    if interp:
        ipath = Path(interp)
        if not ipath.exists():
            rep.line(FAIL, f"its shebang points at a missing interpreter: {interp}")
            return None
        ifmt = platform_of(ipath)
        if ifmt in ("macho", "elf") and ifmt != native_format():
            rep.line(FAIL, f"its interpreter is a {ifmt} binary; this host "
                           f"needs {native_format()}")
            return None
        rep.line(OK, f"shebang interpreter exists: {interp}")

    mine = Path(sys.prefix).resolve()
    theirs = cmd.resolve().parent.parent
    if theirs != mine:
        rep.line(WARN, "this config uses a different environment than the "
                       "interpreter running this check")
        rep.note(f"config  {theirs}")
        rep.note(f"me      {mine}")
        rep.note("Not wrong by itself, but only one of them has hypothesise installed.")

    return cmd


def probe_version(rep: Report, cmd: Path, args: list[str]) -> None:
    """Actually run it. Everything above is inference; this is the answer."""
    argv = [str(cmd)]
    if args[:1] == ["-m"] and len(args) > 1:
        argv += args[:2]
    argv.append("version")
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        rep.line(FAIL, f"could not run it: {type(exc).__name__}: {exc}")
        return
    if out.returncode != 0:
        rep.line(FAIL, f"`{' '.join(argv)}` exited {out.returncode}")
        for stream in (out.stderr, out.stdout):
            tail = [ln for ln in stream.strip().splitlines() if ln][-4:]
            for ln in tail:
                rep.note(ln)
        return
    rep.line(OK, f"it runs: {out.stdout.strip() or '(no output)'}")


def check_config_arg(rep: Report, args: list[str]) -> None:
    if "--config" not in args:
        rep.line(WARN, "no `--config` argument; the proxy has no upstreams to run")
        return
    idx = args.index("--config")
    if idx + 1 >= len(args):
        rep.line(FAIL, "`--config` has no value")
        return
    path = Path(os.path.expanduser(args[idx + 1]))
    if not path.exists():
        rep.line(FAIL, f"--config file does not exist: {path}")
        return
    try:
        json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        rep.line(FAIL, f"--config is not readable JSON: {exc}")
        return
    rep.line(OK, f"--config exists and parses: {path}")


def check_api_arg(rep: Report, args: list[str]) -> None:
    if "--api" not in args:
        rep.line(WARN, "no `--api`; events stay in memory and the dashboard "
                       "stays empty")
        return
    idx = args.index("--api")
    if idx + 1 >= len(args):
        rep.line(FAIL, "`--api` has no value")
        return
    url = urlparse(args[idx + 1])
    host, port = url.hostname, url.port or (443 if url.scheme == "https" else 80)
    if not host:
        rep.line(FAIL, f"--api is not a URL: {args[idx + 1]}")
        return
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError:
        rep.line(WARN, f"nothing listening on {host}:{port} right now")
        rep.note("Start it with `hypothesise api` before the agent session.")
        return
    rep.line(OK, f"control plane answers on {host}:{port}")


def fixed_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Rewrite to `<this interpreter> -m hypothesise ...`, dropping any existing
    `-m hypothesise` prefix so --fix is idempotent."""
    args = [str(a) for a in entry.get("args", [])]
    if args[:2] == ["-m", "hypothesise"]:
        args = args[2:]
    new = dict(entry)
    new["command"] = sys.executable
    new["args"] = ["-m", "hypothesise", *args]
    return new


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("path", help="mcp.json / .cursor/mcp.json / ~/.claude.json")
    ap.add_argument("--fix", action="store_true",
                    help="rewrite hypothesise entries to use this interpreter")
    args = ap.parse_args(argv)

    path = Path(os.path.expanduser(args.path))
    print("=" * 70)
    print("CLIENT CONFIG CHECK")
    print("=" * 70)
    print(f"\n  config       {path}")
    print(f"  checking as  {sys.executable}")

    if not path.exists():
        print(f"\n  FAIL    no such file: {path}")
        return 1
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"\n  FAIL    not readable JSON: {exc}")
        return 1
    if not isinstance(doc, dict):
        print("\n  FAIL    top level is not an object")
        return 1

    block = servers_of(doc)
    if not block:
        print("\n  FAIL    no `servers` or `mcpServers` object")
        print("          VS Code uses `servers`; Cursor and Claude Code use")
        print("          `mcpServers`. The wrong key is silently ignored.")
        return 1

    targets = {n: e for n, e in block.items()
               if isinstance(e, dict) and is_hypothesise_entry(n, e)}
    if not targets:
        print(f"\n  FAIL    none of {sorted(block)} looks like a hypothesise entry")
        return 1

    rep = Report()
    for name, entry in targets.items():
        print(f"\n[{name}]")
        eargs = [str(a) for a in entry.get("args", [])]
        cmd = check_command(rep, entry)
        if cmd is not None:
            probe_version(rep, cmd, eargs)
        check_config_arg(rep, eargs)
        check_api_arg(rep, eargs)

    if args.fix:
        for name in targets:
            block[name] = fixed_entry(block[name])
        path.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"\n  rewrote {len(targets)} entr{'y' if len(targets) == 1 else 'ies'} "
              f"to use {sys.executable} -m hypothesise")
        print("  Reload the client window, then run this again without --fix.")
        return 0

    print("\n" + "=" * 70)
    if rep.failed:
        print("Not usable as written. Fix the FAIL lines, or run again with")
        print("--fix using the interpreter that has hypothesise installed:\n")
        print(f"    {sys.executable} {Path(__file__).name} {path} --fix")
        print("=" * 70)
        return 1
    if rep.warned:
        print("Usable, with warnings above.")
        print("=" * 70)
        return 0
    print("Good. The client can launch this.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
