"""`hypothesise mode [observe|enforce]` — one switch for every connected agent.

    hypothesise mode                 show the current mode
    hypothesise mode enforce         block dangerous actions (popup on each block)
    hypothesise mode observe         record and score only — the safe default

Writes ~/.hypothesise/settings.json. Running wrapped servers and proxies re-read
it every two seconds, so the change applies to the next tool call with no
restart (ADR-041). Agent hooks (Claude Code, Copilot, Cursor) are observe-only
by design and are not affected.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

from hypothesise_proxy import settings


def _get(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    except Exception:  # noqa: BLE001
        return None


def _show(s: dict) -> None:
    eff = settings.mode("observe")
    print(f"mode       {eff}" + ("   (from HYPOTHESISE_MODE)" if os.environ.get("HYPOTHESISE_MODE") else ""))
    if eff == "enforce":
        print(f"approvals  {s.get('apiUrl') or settings.DEFAULT_API}")
        print(f"popups     {'on' if settings.notify_enabled() else 'off'}")
    print(f"settings   {settings.path()}")


def cmd_mode(args: argparse.Namespace) -> int:
    s = settings.load()
    if not args.value:
        _show(s)
        return 0

    s["mode"] = args.value
    if args.api:
        s["apiUrl"] = args.api
    if args.popup is not None:
        s["notify"] = args.popup
    if args.value == "enforce":
        s.setdefault("apiUrl", settings.DEFAULT_API)
        s.setdefault("notify", True)
    settings.save(s)

    if os.environ.get("HYPOTHESISE_MODE"):
        print(f"note: HYPOTHESISE_MODE={os.environ['HYPOTHESISE_MODE']} is set in this "
              "environment and overrides this setting wherever it is set")

    if args.value == "observe":
        print("hypothesise: observe mode — everything is recorded and scored, nothing is blocked.")
        print("  Connected agents switch within ~2 seconds; no restart needed.")
        return 0

    api = s["apiUrl"].rstrip("/")
    print("hypothesise: ENFORCE mode — dangerous actions by connected agents will be blocked.")
    on = s.get("notify", True)
    print(f"  Popups on block: {'on' if on else 'off'}   (turn "
          + ("off: hypothesise mode enforce --no-popup)" if on
             else "on: hypothesise mode enforce --popup)"))
    print("  Connected MCP servers switch within ~2 seconds; no restart needed.")
    print("  Agent hooks (Claude Code, Copilot, Cursor built-in tools) stay observe-only.")

    if _get(f"{api}/healthz") is None:
        print(f"\n  ! The dashboard is not running at {api}. Actions that need a human")
        print("    decision (ASK) will be DENIED until it is. Start it:  hypothesise api")
    else:
        shadow = _get(f"{api}/v1/shadow") or {}
        wb = shadow.get("would_block", shadow.get("would_block_total"))
        if isinstance(wb, int) and wb:
            print(f"\n  ! In observe mode, {wb} recorded action(s) would have been blocked.")
            print(f"    Review the Shadow tab first ({api}/#shadow): if those were your own")
            print("    normal work, enforce mode will block it too.")
    print("\n  Back to safe at any time:  hypothesise mode observe")
    return 0
