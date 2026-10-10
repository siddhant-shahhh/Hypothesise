"""Scenario loading — the eval harness.

BUILD_PLAN rule 1: scenarios before detectors, always. Write the YAML that
should fire, watch it not fire, then build the detector. Anything else is
guessing, and guessing is how detection tuning eats a schedule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .classify import classify_action, classify_resource
from .events import Event, Outcome, Resource, Status, Tool, arg_shape, digest_args, new_id


@dataclass
class Scenario:
    name: str
    description: str
    expect: str                    # "fire" | "silent"
    agent_id: str
    events: list[Event]
    severity: str = ""
    target_detector: str = ""
    path: str = ""

    @property
    def should_fire(self) -> bool:
        return self.expect == "fire"


def load_scenario(path: str | Path, base_ts: datetime | None = None) -> Scenario:
    try:
        # Local import: the engine's hot path never touches YAML, so this stays
        # out of hypothesise_core's hard dependencies (ADR-005).
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Loading scenarios needs a YAML parser, which is an optional extra.\n"
            "    pip install 'hypothesise-core[scenarios]'   (or: pip install pyyaml)"
        ) from exc

    p = Path(path)
    raw: dict[str, Any] = yaml.safe_load(p.read_text())

    agent_id = raw.get("agent_id", "unknown-agent")
    session_id = new_id("sess")
    t0 = base_ts or datetime.now(timezone.utc)

    events: list[Event] = []
    for i, e in enumerate(raw.get("events", [])):
        tool = Tool(server=e["tool"]["server"], name=e["tool"]["name"])
        uri = e.get("resource", "") or ""
        args = e.get("args") or {}
        keys, nbytes = arg_shape(args) if args else ([], 0)
        cls = classify_resource(uri, tool.server, tool.name)
        action = classify_action(tool.name, cls)
        events.append(
            Event(
                agent_id=agent_id,
                session_id=session_id,
                step=i,
                tool=tool,
                resource=Resource(uri=uri, cls=cls),
                action=action,
                ts=(t0 + timedelta(seconds=i * 3)).isoformat(timespec="milliseconds"),
                arg_keys=keys,
                arg_bytes=int(e.get("arg_bytes", nbytes)),
                args_digest=digest_args(args) if args else "",
                outcome=Outcome(status=Status(e.get("status", "ok"))),
            )
        )

    return Scenario(
        name=raw.get("name", p.stem),
        description=(raw.get("description") or "").strip(),
        expect=raw.get("expect", "silent"),
        agent_id=agent_id,
        events=events,
        severity=raw.get("severity", ""),
        target_detector=raw.get("target_detector", ""),
        path=str(p),
    )


def load_all(root: str | Path = "scenarios") -> list[Scenario]:
    root = Path(root)
    return [load_scenario(p) for p in sorted(root.rglob("*.yaml"))]
