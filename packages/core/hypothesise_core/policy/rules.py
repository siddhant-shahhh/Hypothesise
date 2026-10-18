"""Policy rules — explicit operator intent, layered over behavioural scoring.

Behavioural detection answers "is this unusual for this agent". Rules answer
"do we allow this at all". Both are needed: detection cannot know that your
production database is special, and a rule cannot notice a novel attack.

Direction matters, and is asymmetric on purpose:

  * `block` and `ask` rules only ever RAISE a verdict.
  * `allow` rules only ever LOWER one, and **never** past a CRITICAL detector.

An allowlist that can silence `retry_after_denial` or `shell_exfil` is a hole
with a config file in front of it. Anyone writing "always allow shell-mcp" to
stop the noise would switch off the product's whole reason for existing, and
would not find out until afterwards.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from ..events import Event, Verdict

RANK = {Verdict.ALLOW: 0, Verdict.ASK: 1, Verdict.BLOCK: 2}


@dataclass
class Rule:
    name: str
    action: Verdict
    match: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    # Resource globs this rule never applies to — "block Documents, except my
    # code folder". An exception narrows one rule; it is not an allow rule, so
    # it cannot reopen what a different rule closed (ADR-042).
    except_uri: list[str] = field(default_factory=list)

    def matches(self, ev: Event) -> bool:
        """Every condition must hold. Values are glob patterns (or a list of
        them, any of which may match), case-insensitive, with Windows
        backslashes read as forward slashes so one pattern serves every OS."""
        for key, pattern in self.match.items():
            value = _field(ev, key)
            if value is None:
                return False
            if not _any_glob(str(value), pattern):
                return False
        if self.except_uri and _any_glob(ev.resource.uri, self.except_uri):
            return False
        return bool(self.match)          # an empty match block never matches


def _norm(s: str) -> str:
    return s.replace("\\", "/").lower()


def _any_glob(value: str, pattern: Any) -> bool:
    pats = pattern if isinstance(pattern, (list, tuple)) else [pattern]
    v = _norm(value)
    return any(fnmatch.fnmatchcase(v, _norm(str(p))) for p in pats if str(p))


def _field(ev: Event, key: str) -> Any:
    return {
        "agent_id": ev.agent_id,
        "tool_server": ev.tool.server,
        "tool_name": ev.tool.name,
        "tool": ev.tool.qualified,
        "resource_uri": ev.resource.uri,
        "resource_class": ev.resource.cls.value,
        "action": ev.action.value,
        "status": ev.outcome.status.value,
    }.get(key)


@dataclass
class RuleSet:
    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def from_config(cls, raw: list[dict[str, Any]] | None) -> RuleSet:
        out: list[Rule] = []
        for i, r in enumerate(raw or []):
            try:
                action = Verdict(str(r.get("action", "ask")).lower())
            except ValueError:
                continue
            exc = r.get("except") or []
            out.append(Rule(
                name=r.get("name") or f"rule-{i + 1}",
                action=action,
                match=dict(r.get("match") or {}),
                reason=r.get("reason", ""),
                except_uri=[exc] if isinstance(exc, str) else list(exc),
            ))
        return cls(rules=out)

    def apply(self, ev: Event, verdict: Verdict,
              has_critical: bool = False) -> tuple[Verdict, Rule | None]:
        """Returns the verdict after rules, and the rule that changed it."""
        applied: Rule | None = None
        result = verdict

        for rule in self.rules:
            if not rule.matches(ev):
                continue

            if rule.action in (Verdict.BLOCK, Verdict.ASK):
                if RANK[rule.action] > RANK[result]:
                    result, applied = rule.action, rule

            elif rule.action is Verdict.ALLOW:
                # An allow rule cannot override a CRITICAL detector. This is the
                # difference between an allowlist and a backdoor.
                if has_critical:
                    continue
                if RANK[rule.action] < RANK[result]:
                    result, applied = rule.action, rule

        return result, applied


def describe(rule: Rule) -> str:
    # Personal-data rules carry long pattern lists; the sentence a person
    # reads must stay one line, so lists are summarised.
    conditions = ", ".join(
        f"{k}={v}" if not isinstance(v, (list, tuple))
        else f"{k}={v[0]}" + (f" (+{len(v) - 1} more)" if len(v) > 1 else "")
        for k, v in rule.match.items())
    base = f"policy rule '{rule.name}' ({conditions})"
    return f"{base}: {rule.reason}" if rule.reason else base
