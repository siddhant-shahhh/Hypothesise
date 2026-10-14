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


def _macos(body: str) -> None:
    esc = body.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'display notification "{esc}" with title "{TITLE}" '
              f'subtitle "{SUBTITLE}" sound name "Basso"')
    _spawn(["osascript", "-e", script])


_PS = r"""
$t = $env:HYP_TITLE; $b = $env:HYP_BODY
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  $x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $n = $x.GetElementsByTagName('text')
  $n.Item(0).AppendChild($x.CreateTextNode($t)) | Out-Null
  $n.Item(1).AppendChild($x.CreateTextNode($b)) | Out-Null
  $id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))
} catch {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show($b, $t, 'OK', 'Warning') | Out-Null
}
"""


def _windows(body: str) -> None:
    enc = base64.b64encode(_PS.encode("utf-16-le")).decode("ascii")
    env = {**os.environ, "HYP_TITLE": f"{TITLE} — {SUBTITLE}", "HYP_BODY": body}
    _spawn(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-EncodedCommand", enc], env=env)


def _linux(body: str) -> None:
    if shutil.which("notify-send"):
        _spawn(["notify-send", "-u", "critical", "-a", "Hypothesise", TITLE, body])


def popup_command(body: str) -> list[str]:
    """The argv that would be spawned on this platform (for tests and docs)."""
    if sys.platform == "darwin":
        return ["osascript", "-e", "display notification …"]
    if _is_windows():
        return ["powershell", "-EncodedCommand", "…"]
    return ["notify-send", TITLE, body]


def blocked(sentence: str, agent: str = "", tool: str = "", now: float | None = None) -> bool:
    """Show a popup for a block. Returns True if one was shown. Never raises."""
    global _last, _suppressed
    try:
        from .settings import notify_enabled

        if not notify_enabled():
            return False
        t = time.monotonic() if now is None else now
        if t - _last < MIN_INTERVAL:
            _suppressed += 1
            return False
        who = " → ".join(x for x in (agent, tool) if x)
        body = _clip(f"{who}: {sentence}" if who else sentence)
        if _suppressed:
            body = _clip(f"{body} (+{_suppressed} more blocked)")
        _last, _suppressed = t, 0
        if sys.platform == "darwin":
            _macos(body)
        elif _is_windows():
            _windows(body)
        else:
            _linux(body)
        return True
    except Exception:  # noqa: BLE001 — a popup must never cost the user their tool
        return False
