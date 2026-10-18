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
import sys
import urllib.request

from hypothesise_proxy import settings


def _get(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "null")
    except Exception:  # noqa: BLE001
        return None


def _show(s: dict) -> None:
    from hypothesise import protect

    eff = settings.mode("observe")
    print(f"mode       {eff}" + ("   (from HYPOTHESISE_MODE)" if os.environ.get("HYPOTHESISE_MODE") else ""))
    if eff == "enforce":
        print(f"approvals  {s.get('apiUrl') or settings.DEFAULT_API}")
        print(f"popups     {'on' if settings.notify_enabled() else 'off'}")
    answers = s.get("protections")
    if answers:
        print("protected  (blocked in enforce mode, 'would block' in observe mode)")
        for line in protect.summary(answers):
            print(f"  {line.strip()}")
    print(f"settings   {settings.path()}")


def _blocking_hooks() -> tuple[list[str], list[str]]:
    """(hook clients that can block, hook clients still observe-only)."""
    try:
        from hypothesise import clients as c
    except Exception:  # noqa: BLE001
        return [], []
    ok, old = [], []
    for name, present in (("Claude Code", c.has_hook_file(c.claude_settings())),
                          ("VS Code Copilot", c.has_hook_file(c.copilot_hook_file())),
                          ("Cursor", c.has_hook_file(c.cursor_hooks_file()))):
        if present is None:
            continue
        (ok if present else old).append(name)
    return ok, old


def cmd_mode(args: argparse.Namespace) -> int:
    from hypothesise import protect

    s = settings.load()
    if not args.value:
        _show(s)
        return 0

    s["mode"] = args.value
    if args.api:
        s["apiUrl"] = args.api
    if args.popup is not None:
        s["notify"] = args.popup

    asked = False
    if args.value == "enforce":
        s.setdefault("apiUrl", settings.DEFAULT_API)
        s.setdefault("notify", True)
        interactive = sys.stdin.isatty() and not getattr(args, "yes", False)
        if getattr(args, "setup", False) or not s.get("protections"):
            if interactive:
                try:
                    s["protections"] = protect.ask(previous=s.get("protections"))
                    asked = True
                except (EOFError, KeyboardInterrupt):
                    print("\nhypothesise: cancelled — nothing changed.")
                    return 1
            elif not s.get("protections"):
                s["protections"] = protect.unattended()
        answers = s["protections"]
        s["rules"] = protect.build_rules(answers.get("chosen") or [],
                                         answers.get("code_dirs"), answers.get("private_dirs"))
    settings.save(s)

    if os.environ.get("HYPOTHESISE_MODE"):
        print(f"note: HYPOTHESISE_MODE={os.environ['HYPOTHESISE_MODE']} is set in this "
              "environment and overrides this setting wherever it is set")

    if args.value == "observe":
        print("hypothesise: observe mode — everything is recorded and scored, nothing is blocked.")
        print("  Connected agents switch within ~2 seconds; no restart needed.")
        if s.get("protections"):
            print("  Your protection rules are kept; matches show as 'would block' on the dashboard.")
        return 0

    api = s["apiUrl"].rstrip("/")
    print("\nhypothesise: ENFORCE mode is on.")
    print("  Blocked now: dangerous sequences (e.g. read a secret, then send data out),")
    print("  and anything you protected:")
    for line in protect.summary(s["protections"]) or ["  - (nothing extra — run: hypothesise mode enforce --setup)"]:
        print(f"  {line}")
    if not asked:
        print("  Change these any time:  hypothesise mode enforce --setup")
    on = s.get("notify", True)
    print(f"  Popup on every block: {'on' if on else 'off'}   (turn "
          + ("off: hypothesise mode enforce --no-popup)" if on
             else "on: hypothesise mode enforce --popup)"))
    print("  Applies within ~2 seconds to every connected agent — no restart needed.")

    ok, old = _blocking_hooks()
    if ok:
        print(f"  Built-in tools are blocked too in: {', '.join(ok)}.")
    if old:
        print(f"  ! {', '.join(old)}: built-in tools are only watched, not blocked.")
        print("    Run  hypothesise init  once to turn blocking on for them, then restart the app.")

    if _get(f"{api}/healthz") is None:
        print(f"\n  Note: the dashboard is not running ({api}). Blocks still happen and")
        print("  still pop up; actions that need your OK are refused until it runs.")
        print("  To see blocks and approve actions:  hypothesise api")
    else:
        agents = _get(f"{api}/v1/agents") or []
        wary = [a for a in agents if isinstance(a, dict) and a.get("zone") in ("warm", "hot")]
        if wary:
            names = ", ".join(f"{a['agent_id']} ({'Caution' if a['zone'] == 'warm' else 'Restricted'})"
                              for a in wary[:5])
            print(f"\n  Earlier activity lowered trust for: {names}.")
            print("  Their secret, shell and network actions will ask you first (a popup).")
            print("  If that earlier activity was you testing:  hypothesise release --all")
        shadow = _get(f"{api}/v1/shadow") or {}
        wb = shadow.get("would_block") if isinstance(shadow, dict) else None
        if isinstance(wb, int) and wb:
            print(f"\n  In observe mode, {wb} recorded action(s) would have been blocked.")
            print(f"  Check the Shadow tab ({api}/#shadow): if those were your own normal")
            print("  work, enforce mode will block that work too.")
    print("\n  Back to safe at any time:  hypothesise mode observe")
    return 0
