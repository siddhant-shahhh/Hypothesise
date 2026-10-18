"""On-screen popup when enforce mode blocks an agent's action (ADR-041).

A block that only shows up in a dashboard nobody is looking at reads, to the
user, as the agent "just failing". The popup says what happened and why, in the
same sentence the block carries (no sentence, no block — ARCHITECTURE §9).

Rules, in the spirit of the data plane:

* **Never blocks the agent.** The popup is a detached child process; this
  function returns immediately and swallows every error.
* **Never floods the screen.** At most one popup every few seconds per proxy;
  blocks in between are counted into the next one.
* **Off switch.** `hypothesise mode enforce --no-popup`, or HYPOTHESISE_NOTIFY=0.

macOS: Notification Center banner (osascript). Windows: a toast, falling back
to a message box. Linux: notify-send when installed. Nothing else is required.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import time

MIN_INTERVAL = 4.0
TITLE = "Hypothesise blocked an action"
SUBTITLE = "Blocked for security reasons"

_last = 0.0
_suppressed = 0


def _is_windows() -> bool:
    return os.name == "nt"


def _clip(text: str, n: int = 220) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _spawn(argv: list[str], env: dict | None = None) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if _is_windows():
        kwargs["creationflags"] = 0x08000000 | 0x00000200   # NO_WINDOW | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, env=env, **kwargs)  # noqa: S603 — fixed argv, no shell


# A banner (display notification) is silently dropped when the user never
# allowed notifications for Script Editor, which is the default — the first
# real test showed nothing. An alert needs no permission and stays on screen
# until clicked, or gives up by itself so it never piles up (ADR-042).
ALERT_SECONDS = 30


def _macos(body: str) -> None:
    def q(x: str) -> str:
        return x.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display alert "{q(TITLE)}" message "{q(SUBTITLE)}.\\n\\n{q(body)}" '
              f'as critical buttons {{"OK"}} default button "OK" '
              f'giving up after {ALERT_SECONDS}')
    _spawn(["osascript", "-e", script])


# WScript.Shell.Popup: present on every Windows, needs no app registration (a
# toast does), and closes itself. 16 = stop icon, 4096 = stay on top.
_PS = r"""
$t = $env:HYP_TITLE; $b = $env:HYP_BODY
try {
  (New-Object -ComObject WScript.Shell).Popup($b, [int]$env:HYP_SECONDS, $t, 4112) | Out-Null
} catch {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show($b, $t, 'OK', 'Stop') | Out-Null
}
"""


def _windows(body: str) -> None:
    enc = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
    env = {**os.environ, "HYP_TITLE": f"{TITLE} — {SUBTITLE}", "HYP_BODY": body,
           "HYP_SECONDS": str(ALERT_SECONDS)}
    _spawn(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-EncodedCommand", enc], env=env)


def _linux(body: str) -> None:
    if shutil.which("zenity"):
        _spawn(["zenity", "--error", f"--title={TITLE}", f"--timeout={ALERT_SECONDS}",
                "--no-wrap", f"--text={SUBTITLE}.\n\n{body}"])
    elif shutil.which("notify-send"):
        _spawn(["notify-send", "-u", "critical", "-a", "Hypothesise", TITLE, body])


def popup_command(body: str) -> list[str]:
    """The argv that would be spawned on this platform (for tests and docs)."""
    if sys.platform == "darwin":
        return ["osascript", "-e", "display alert …"]
    if _is_windows():
        return ["powershell", "-EncodedCommand", "…"]
    return ["zenity", "--error", body] if shutil.which("zenity") else ["notify-send", TITLE, body]


def _shared_last() -> float:
    """Last popup time across processes. Blocking hooks are a new process per
    tool call, so an in-memory limit alone would let a loop flood the screen."""
    try:
        from .spool import home
        return float((home() / ".last-popup").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _mark_shown(t: float) -> None:
    try:
        from .spool import home
        f = home() / ".last-popup"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(t))
    except OSError:
        pass


def blocked(sentence: str, agent: str = "", tool: str = "", now: float | None = None) -> bool:
    """Show a popup for a block. Returns True if one was shown. Never raises."""
    global _last, _suppressed
    try:
        from .settings import notify_enabled

        if not notify_enabled():
            return False
        t = time.time() if now is None else now
        if t - max(_last, _shared_last() if now is None else 0.0) < MIN_INTERVAL:
            _suppressed += 1
            return False
        who = " → ".join(x for x in (agent, tool) if x)
        body = _clip(f"{who}: {sentence}" if who else sentence)
        if _suppressed:
            body = _clip(f"{body} (+{_suppressed} more blocked)")
        _last, _suppressed = t, 0
        if now is None:
            _mark_shown(t)
        if sys.platform == "darwin":
            _macos(body)
        elif _is_windows():
            _windows(body)
        else:
            _linux(body)
        return True
    except Exception:  # noqa: BLE001 — a popup must never cost the user their tool
        return False


# -- on-screen approval (ADR-042) -------------------------------------------
#
# An ASK verdict used to wait for someone to click Approve on the dashboard —
# a page nobody had open — so in the first real enforce run an ordinary
# `pytest` call hung for two minutes and failed. The question now also comes
# to the screen, with Allow and Block buttons; whichever answer arrives first,
# here or on the dashboard, wins. Blocking call: run it in an executor.

ASK_TITLE = "Hypothesise: allow this action?"


def ask(sentence: str, agent: str = "", tool: str = "", timeout: float = 60.0) -> bool | None:
    """True = allowed, False = blocked or timed out, None = no way to ask here."""
    try:
        if os.environ.get("HYPOTHESISE_NOTIFY", "").strip().lower() in ("0", "false", "no", "off"):
            return None
        who = " → ".join(x for x in (agent, tool) if x)
        body = _clip(f"{who}: {sentence}" if who else sentence, 400)
        secs = max(5, int(timeout))
        if sys.platform == "darwin":
            def q(x: str) -> str:
                return x.replace("\\", "\\\\").replace('"', '\\"')
            script = (f'display dialog "{q(body)}\\n\\nAllow it? (No answer in {secs}s = Block)" '
                      f'with title "{q(ASK_TITLE)}" buttons {{"Block", "Allow"}} '
                      f'default button "Block" cancel button "Block" with icon caution '
                      f'giving up after {secs}')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                               timeout=secs + 10)
            return "button returned:Allow" in r.stdout and "gave up:true" not in r.stdout
        if _is_windows():
            ps = ("$r = (New-Object -ComObject WScript.Shell).Popup($env:HYP_BODY, "
                  "[int]$env:HYP_SECONDS, $env:HYP_TITLE, 4132); exit $r")
            enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
            env = {**os.environ, "HYP_TITLE": ASK_TITLE,
                   "HYP_BODY": f"{body}\n\nAllow it? (No answer in {secs}s = Block)",
                   "HYP_SECONDS": str(secs)}
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                "Bypass", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
                               env=env, capture_output=True, timeout=secs + 15,
                               creationflags=0x08000000)
            return r.returncode == 6          # 6 = Yes; 7 = No; -1 = timed out
        if shutil.which("zenity") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            r = subprocess.run(["zenity", "--question", f"--title={ASK_TITLE}",
                                "--ok-label=Allow", "--cancel-label=Block", f"--timeout={secs}",
                                "--no-wrap", f"--text={body}\n\nAllow it?"],
                               capture_output=True, timeout=secs + 10)
            return r.returncode == 0
        return None
    except Exception:  # noqa: BLE001 — failing to ask is "could not ask", never "allowed"
        return None

