"""Event schema — the contract every other package depends on.

`weaver_core` has zero I/O and zero framework dependencies (ADR-005), so this
is stdlib dataclasses rather than Pydantic. Serialisation is explicit and the
wire format is plain JSON-compatible dicts.

Freeze this early. Changing it after M1 means touching the proxy, the store,
the API and the dashboard at once.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    """ISO-8601 UTC. Stored as TEXT so the schema stays Postgres-portable."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:22]}"


class Action(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    INVOKE = "invoke"
    EGRESS = "egress"
    UNKNOWN = "unknown"


class ResourceClass(str, Enum):
    SECRET = "secret"
    SHELL = "shell"
    NETWORK_EGRESS = "network_egress"
    DATABASE = "database"
    CONFIG = "config"
    SOURCE_CODE = "source_code"
    COMMUNICATION = "communication"
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    BLOCK = "block"


class Status(str, Enum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"    # upstream refused it (its own permissions) — NOT our block
    BLOCKED = "blocked"  # we blocked it
    PENDING = "pending"  # held for approval


# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    server: str
    name: str

    @property
    def qualified(self) -> str:
        return f"{self.server}/{self.name}"


@dataclass(frozen=True)
class Resource:
    uri: str
    cls: ResourceClass = ResourceClass.UNKNOWN


@dataclass(frozen=True)
class Outcome:
    status: Status = Status.OK
    latency_ms: int = 0
    bytes: int = 0
    error: str | None = None


@dataclass
class Decision:
    """Why we did what we did. Never a bare verdict.

    `sentence` is the human-readable explanation. A decision that cannot
    produce one must not block — see ARCHITECTURE section 6.
    """

    verdict: Verdict = Verdict.ALLOW
    score: float = 0.0
    detectors: list[dict[str, Any]] = field(default_factory=list)
    sentence: str = ""
    budget_ms: int = 0
    fail_open: bool = False  # true if we allowed because the engine failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "score": round(self.score, 4),
            "detectors": self.detectors,
            "sentence": self.sentence,
            "budget_ms": self.budget_ms,
            "fail_open": self.fail_open,
        }


@dataclass
class Event:
    """One intercepted tool call."""

    agent_id: str
    session_id: str
    step: int
    tool: Tool
    resource: Resource
    action: Action = Action.UNKNOWN
    event_id: str = field(default_factory=lambda: new_id("evt"))
    ts: str = field(default_factory=utcnow)
    tenant_id: str = "default"
    args_digest: str = ""
    # Argument SHAPE, not content. The digest identifies a payload but says
    # nothing about it; these let Tier-2 detectors reason about size and
    # structure without ever storing or transmitting the arguments themselves.
    arg_keys: list[str] = field(default_factory=list)
    arg_bytes: int = 0
    outcome: Outcome = field(default_factory=Outcome)
    decision: Decision = field(default_factory=Decision)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ts": self.ts,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "step": self.step,
            "tool": {"server": self.tool.server, "name": self.tool.name},
            "resource": {"uri": self.resource.uri, "class": self.resource.cls.value},
            "action": self.action.value,
            "args_digest": self.args_digest,
            "arg_keys": self.arg_keys,
            "arg_bytes": self.arg_bytes,
            "outcome": {
                "status": self.outcome.status.value,
                "latency_ms": self.outcome.latency_ms,
                "bytes": self.outcome.bytes,
                "error": self.outcome.error,
            },
            "decision": self.decision.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        dec_raw = d.get("decision") or {}
        out_raw = d.get("outcome") or {}
        return cls(
            agent_id=d["agent_id"],
            session_id=d["session_id"],
            step=int(d.get("step", 0)),
            tool=Tool(server=d["tool"]["server"], name=d["tool"]["name"]),
            resource=Resource(
                uri=d["resource"]["uri"],
                cls=ResourceClass(d["resource"].get("class", "unknown")),
            ),
            action=Action(d.get("action", "unknown")),
            event_id=d.get("event_id") or new_id("evt"),
            ts=d.get("ts") or utcnow(),
            tenant_id=d.get("tenant_id", "default"),
            args_digest=d.get("args_digest", ""),
            arg_keys=list(d.get("arg_keys") or []),
            arg_bytes=int(d.get("arg_bytes", 0)),
            outcome=Outcome(
                status=Status(out_raw.get("status", "ok")),
                latency_ms=int(out_raw.get("latency_ms", 0)),
                bytes=int(out_raw.get("bytes", 0)),
                error=out_raw.get("error"),
            ),
            decision=Decision(
                verdict=Verdict(dec_raw.get("verdict", "allow")),
                score=float(dec_raw.get("score", 0.0)),
                detectors=dec_raw.get("detectors", []),
                sentence=dec_raw.get("sentence", ""),
                budget_ms=int(dec_raw.get("budget_ms", 0)),
                fail_open=bool(dec_raw.get("fail_open", False)),
            ),
        )


def arg_shape(args: Any) -> tuple[list[str], int]:
    """(top-level keys, serialised size). Shape only — never the values."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    keys = sorted(args.keys()) if isinstance(args, dict) else []
    return keys, len(blob)


def digest_args(args: Any) -> str:
    """Hash of tool arguments.

    Full arguments contain secrets by definition, so only the digest travels
    inline. The full payload goes to a separately-redactable table with its own
    retention policy (ARCHITECTURE section 9, rule 5).
    """
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
