"""LangGraph / LangChain callback handler — the second front door.

The MCP proxy (ADR-001) covers every MCP-speaking client, which is where the
dangerous surface usually lives: filesystem, shell, database, secrets. It
cannot see a tool call that never leaves the Python process — a function the
graph calls directly.

This handler closes that. It emits the *same* event shape into the *same*
engine, so a decision made here and one made in the proxy are identical.

**Advisory by default.** A LangChain callback cannot veto a tool call — by the
time `on_tool_start` runs, the framework is committed. Setting
`raise_on_block=True` makes a BLOCK raise `ToolBlocked`, which surfaces to the
graph as a tool error; that is enforcement of a sort, but the call may already
have had side effects. Honest framing: this front door is for visibility, and
the proxy is where blocking actually works.

Imports nothing from langchain, so `hypothesise_langgraph` stays installable and
testable without it.
"""

from __future__ import annotations

import contextlib
from typing import Any
from uuid import UUID

from hypothesise_core.classify import classify_action, classify_resource
from hypothesise_core.engine import Engine
from hypothesise_core.events import (
    Event,
    Outcome,
    Resource,
    Status,
    Tool,
    Verdict,
    arg_shape,
    digest_args,
    new_id,
)


class ToolBlocked(Exception):
    """Raised when raise_on_block is set and the engine returns BLOCK."""

    def __init__(self, sentence: str, event_id: str = "") -> None:
        super().__init__(sentence)
        self.sentence = sentence
        self.event_id = event_id


def _resource_from(tool_name: str, payload: Any) -> str:
    """Best-effort resource URI out of a LangChain tool input.

    Inputs are free-form, so this is the same heuristic the proxy uses. An
    unrecognised shape yields "" and classifies UNKNOWN, which is the correct
    failure: guessing wrong attaches risk to a resource never touched.
    """
    if isinstance(payload, dict):
        for key in ("uri", "url", "path", "file_path", "filepath", "file",
                    "resource", "target", "repo", "query", "command", "cmd"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                if key in ("command", "cmd"):
                    return f"shell://{val}"
                if key == "query":
                    return f"query://{val[:120]}"
                return val
        return ""
    if isinstance(payload, str) and payload:
        looks_like_uri = "://" in payload or payload.startswith("/")
        return payload if looks_like_uri else ""
    return ""


def _spool_event(event: Event) -> None:
    from hypothesise_proxy.spool import append

    append([event.to_dict()])


class HypothesiseCallbackHandler:
    """Duck-typed LangChain callback handler.

    Subclass `langchain_core.callbacks.BaseCallbackHandler` in your own code if
    you want strict typing; the method names are what LangChain dispatches on.
    """

    def __init__(self, engine: Engine | None = None, agent_id: str = "langgraph-agent",
                 session_id: str | None = None, raise_on_block: bool = False,
                 on_event: Any = "spool") -> None:
        self.engine = engine or Engine()
        self.agent_id = agent_id
        self.session_id = session_id or new_id("sess")
        self.raise_on_block = raise_on_block
        # Default: record to the local spool, so a graph shows up in the
        # dashboard with no extra wiring (ADR-041). Pass a callable to route
        # events elsewhere, or None to record nothing.
        if on_event == "spool":
            on_event = _spool_event
        self.on_event = on_event
        self.step = 0
        self._inflight: dict[str, Event] = {}

    # -- LangChain hooks ----------------------------------------------------

    def on_tool_start(self, serialized: dict[str, Any], input_str: Any,
                      *, run_id: UUID | None = None, **kwargs: Any) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "unknown_tool"
        payload = kwargs.get("inputs", input_str)

        uri = _resource_from(name, payload)
        cls = classify_resource(uri, "langgraph", name)
        keys, nbytes = arg_shape(payload) if isinstance(payload, dict) else ([], 0)

        event = Event(
            agent_id=self.agent_id, session_id=self.session_id, step=self.step,
            tool=Tool(server="langgraph", name=name),
            resource=Resource(uri=uri, cls=cls),
            action=classify_action(name, cls),
            args_digest=digest_args(payload), arg_keys=keys, arg_bytes=nbytes,
        )
        self.step += 1

        decision, _signals = self.engine.evaluate(event)
        event.decision = decision

        if decision.verdict is Verdict.BLOCK:
            event.outcome = Outcome(status=Status.BLOCKED)
            self._emit(event)
            if self.raise_on_block:
                raise ToolBlocked(decision.sentence, event.event_id)
            return

        self._inflight[str(run_id)] = event

    def on_tool_end(self, output: Any, *, run_id: UUID | None = None,
                    **kwargs: Any) -> None:
        event = self._inflight.pop(str(run_id), None)
        if event is None:
            return
        event.outcome = Outcome(status=Status.OK, bytes=len(str(output or "")))
        self._emit(event)

    def on_tool_error(self, error: BaseException, *, run_id: UUID | None = None,
                      **kwargs: Any) -> None:
        event = self._inflight.pop(str(run_id), None)
        if event is None:
            return
        from hypothesise_core.classify import looks_denied

        text = str(error)
        event.outcome = Outcome(
            status=Status.DENIED if looks_denied(text) else Status.ERROR,
            error=text[:500])
        self._emit(event)

    # -- plumbing -----------------------------------------------------------

    def _emit(self, event: Event) -> None:
        if self.on_event is not None:
            # Telemetry must never break somebody's graph.
            with contextlib.suppress(Exception):
                self.on_event(event)

    @property
    def pending(self) -> int:
        return len(self._inflight)
