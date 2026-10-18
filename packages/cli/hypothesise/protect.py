"""Personal-data protection: a few questions, turned into block rules (ADR-042).

Behavioural detection catches sequences nobody wrote a rule for. What it
cannot know is which of *your* folders are private — that a coding agent has
no business in your photos, your browser's saved passwords or your mail. So
`hypothesise mode enforce` asks, once, and writes the answers as ordinary
block rules into ~/.hypothesise/settings.json, where every wrapped server,
proxy entry and blocking hook applies them.

The patterns are globs over the resource an agent touches (a file path, a URL,
or `shell://<command>`), matched case-insensitively with Windows backslashes
read as slashes, so one list covers macOS, Linux and Windows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

YES = ("y", "yes")


@dataclass(frozen=True)
class Protection:
    key: str
    question: str
    reason: str                 # what the popup and the agent are told
    patterns: tuple[str, ...] = ()
    resource_class: str = ""
    default: bool = True        # default when asked; also used by --yes
    default_unattended: bool = True
    note: str = ""
    extra_match: dict = field(default_factory=dict)


# Every pattern starts with "*" so it matches a bare path, a ~/ path, a
# file:// URI and a path inside a shell command alike.
CATALOGUE: tuple[Protection, ...] = (
    Protection(
        key="secrets",
        question="Passwords, API keys and .env files?",
        reason="AI agents may not read or change your passwords, API keys or .env files",
        resource_class="secret",
    ),
    Protection(
        key="credentials",
        question="SSH keys and cloud logins (~/.ssh, ~/.aws, ~/.kube, gcloud, Azure, keychains)?",
        reason="AI agents may not touch your SSH keys or cloud logins",
        patterns=(
            "*/.ssh/*", "*/.aws/*", "*/.kube/*", "*/.config/gcloud/*", "*/.azure/*",
            "*/.gnupg/*", "*/.docker/config.json", "*/.netrc", "*/.git-credentials",
            "*/.npmrc", "*/.pypirc", "*/library/keychains/*",
        ),
    ),
    Protection(
        key="browser",
        question="Browser data (saved passwords, cookies, history)?",
        reason="AI agents may not read your browser's saved passwords, cookies or history",
        patterns=(
            "*/library/application support/google/chrome/*",
            "*/library/application support/bravesoftware/*",
            "*/library/application support/microsoft edge/*",
            "*/library/application support/arc/*",
            "*/library/application support/firefox/*",
            "*/library/safari/*", "*/library/cookies/*",
            "*/.config/google-chrome/*", "*/.config/chromium/*",
            "*/.config/bravesoftware/*", "*/.mozilla/*",
            "*/appdata/local/google/chrome/user data/*",
            "*/appdata/local/microsoft/edge/user data/*",
            "*/appdata/local/bravesoftware/*", "*/appdata/roaming/mozilla/*",
        ),
    ),
    Protection(
        key="messages",
        question="Email, messages, notes, contacts and calendars?",
        reason="AI agents may not read your email, messages, notes, contacts or calendars",
        patterns=(
            "*/library/mail/*", "*/library/messages/*",
            "*/library/group containers/group.com.apple.notes/*",
            "*/library/containers/com.apple.notes/*",
            "*/library/application support/addressbook/*", "*/library/calendars/*",
            "*/appdata/local/microsoft/outlook/*", "*/.thunderbird/*",
            "*/appdata/roaming/thunderbird/*",
            "*/.local/share/evolution/*",
        ),
    ),
    Protection(
        key="media",
        question="Photos, videos and music (Pictures, Movies/Videos, Music)?",
        reason="AI agents may not open your photos, videos or music",
        # Anchored to the home folder: a project with its own `music/` or
        # `documents/` directory must not trip a personal-data rule.
        patterns=("{home}/Pictures/*", "{home}/Movies/*", "{home}/Videos/*",
                  "{home}/Music/*", "*.photoslibrary/*"),
    ),
    Protection(
        key="documents",
        question="Personal folders (Documents, Desktop, Downloads)?",
        reason="AI agents may not open files in your personal folders",
        patterns=("{home}/Documents/*", "{home}/Desktop/*", "{home}/Downloads/*"),
        default_unattended=False,
        note="Code folders inside these can be kept open in the next question.",
    ),
    Protection(
        key="bulk_delete",
        question="Commands that delete files in bulk (rm -rf, rmdir /s, del /s)?",
        reason="AI agents may not run commands that delete files in bulk",
        patterns=("shell://*rm -rf*", "shell://*rm -fr*", "shell://*rm -r -f*",
                  "shell://*rmdir /s*", "shell://*del /s*", "shell://*remove-item*-recurse*"),
        default=False, default_unattended=False,
        note="This also blocks build clean-ups such as `rm -rf dist`.",
    ),
)

BY_KEY = {p.key: p for p in CATALOGUE}
PERSONAL_DIRS = ("Documents", "Desktop", "Downloads")


def _glob_for_dir(path: str) -> str:
    p = str(Path(os.path.expanduser(path.strip())).resolve()).replace("\\", "/").rstrip("/")
    return f"*{p}/*"


def _expand(patterns: tuple[str, ...]) -> list[str]:
    """`{home}/X/*` -> the real home path, plus the `~/X/*` spelling agents
    use in shell commands."""
    home = str(Path.home().resolve()).replace("\\", "/").rstrip("/")
    out: list[str] = []
    for pat in patterns:
        if pat.startswith("{home}"):
            rest = pat[len("{home}"):]
            out += [f"*{home}{rest}", f"*~{rest}"]
        else:
            out.append(pat)
    return out


def build_rules(chosen: list[str], code_dirs: list[str] | None = None,
                private_dirs: list[str] | None = None) -> list[dict[str, Any]]:
    """Answers -> settings.json rules. Pure, so it is tested without a terminal."""
    code = [_glob_for_dir(d) for d in (code_dirs or []) if d.strip()]
    rules: list[dict[str, Any]] = []
    for key in chosen:
        p = BY_KEY.get(key)
        if p is None:
            continue
        match: dict[str, Any] = {}
        if p.resource_class:
            match["resource_class"] = p.resource_class
        if p.patterns:
            match["resource_uri"] = _expand(p.patterns)
        rule: dict[str, Any] = {"name": f"protect: {p.key}", "action": "block",
                                "match": match, "reason": p.reason, "source": "hypothesise mode"}
        if code and p.key in ("documents", "media"):
            rule["except"] = code
        rules.append(rule)
    private = [_glob_for_dir(d) for d in (private_dirs or []) if d.strip()]
    if private:
        rules.append({"name": "protect: your folders", "action": "block",
                      "match": {"resource_uri": private},
                      "reason": "AI agents may not open folders you marked private",
                      "source": "hypothesise mode"})
    return rules


def _guess_code_dir() -> str:
    """The current folder, when it sits inside a personal folder — the most
    likely place someone runs `hypothesise` from is a project they work on."""
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    for d in PERSONAL_DIRS:
        base = home / d
        if base in cwd.parents or cwd == base:
            return str(cwd)
    return ""


def _ask_yes(prompt: str, default: bool, inp) -> bool:
    hint = "Y/n" if default else "y/N"
    ans = inp(f"  {prompt} [{hint}] ").strip().lower()
    return default if not ans else ans in YES


def ask(inp=input, out=print, previous: dict | None = None) -> dict[str, Any]:
    """The questions. Returns the answers as stored in settings.json."""
    prev = previous or {}
    prev_chosen = set(prev.get("chosen") or [])
    out("\nWhat should AI agents NEVER be allowed to touch on this computer?")
    out("Hypothesise blocks these and shows a popup when an agent tries.\n")
    chosen: list[str] = []
    for p in CATALOGUE:
        default = (p.key in prev_chosen) if prev else p.default
        if p.note:
            out(f"  ({p.note})")
        if _ask_yes(p.question, default, inp):
            chosen.append(p.key)

    code_dirs: list[str] = list(prev.get("code_dirs") or [])
    if "documents" in chosen or "media" in chosen:
        guess = ", ".join(code_dirs) or _guess_code_dir()
        hint = f" [{guess}]" if guess else ""
        ans = inp(f"  Code folders agents may still use inside those (comma-separated){hint}: ").strip()
        code_dirs = [d.strip() for d in (ans or guess).split(",") if d.strip()]

    prev_private = ", ".join(prev.get("private_dirs") or [])
    hint = f" [{prev_private}]" if prev_private else ""
    ans = inp(f"  Any other folders agents must never open? (comma-separated, Enter for none){hint}: ").strip()
    private_dirs = [d.strip() for d in (ans or prev_private).split(",") if d.strip()]
    return {"chosen": chosen, "code_dirs": code_dirs, "private_dirs": private_dirs}


def unattended() -> dict[str, Any]:
    return {"chosen": [p.key for p in CATALOGUE if p.default_unattended],
            "code_dirs": [], "private_dirs": []}


def summary(answers: dict[str, Any]) -> list[str]:
    def first_up(t: str) -> str:
        return t[:1].upper() + t[1:]
    lines = [f"  - {first_up(BY_KEY[k].reason.replace('AI agents may not ', ''))}"
             for k in answers.get("chosen") or [] if k in BY_KEY]
    for d in answers.get("private_dirs") or []:
        lines.append(f"  - {d} (your private folder)")
    if answers.get("code_dirs"):
        lines.append(f"  Still open for coding: {', '.join(answers['code_dirs'])}")
    return lines
