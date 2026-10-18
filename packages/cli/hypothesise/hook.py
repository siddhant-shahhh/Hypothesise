"""`hypothesise hook claude-code|vscode` — sees the tools MCP interception cannot.

Built-in agent tools — Claude Code's Bash/Read/Edit/WebFetch, Copilot's
view/read_file/bash/run_in_terminal — never touch an MCP server, so the proxy
is blind to them, and they are most of what a coding agent does. Both agents
run a PostToolUse hook after every tool call with the call as JSON on stdin
(the same fields in both: session_id, tool_name, tool_input, tool_response),
so one receiver serves both (ADR-038).

Constraints, all deliberate:

* **Observe only after the tool.** The PostToolUse receiver runs after the
  tool, decides nothing, prints nothing, always exits 0. A hook that errors
  is a broken agent.
* **Blocking only before the tool, only in enforce mode** (`--pre`, ADR-042).
  In observe mode the pre-tool hook returns at once without loading the
  engine. In enforce mode it blocks only on what cannot be a false alarm of
  learned behaviour: your own protection rules, and the critical sequences
  (secret then egress, shell exfiltration, retry after denial).
* **Fast.** Standard library plus the pure engine's classifier; no database,
  no HTTP. The record goes to the local spool and the dashboard evaluates it
  later, with every other event from the same Claude session.
* **One session.** Claude's own `session_id` keys the record, so a secret read
  by `Read` and an upload by an MCP `fetch` land in one window — the
  correlation the per-server proxies cannot make.
"""

from __future__ import annotations

import json
import sys
from typing import Any

AGENT = "claude-code"

# client -> (agent id, session prefix). Copilot keeps its own agent id: it is a
# different agent with a different baseline from the wrapped MCP servers that
# record under "vscode".
CLIENTS = {"claude-code": ("claude-code", "cc"), "vscode": ("copilot", "vs"),
           "cursor": ("cursor", "cu")}

# Clients whose MCP calls already pass through wrapped servers (`init`), so the
# hook must skip them or every MCP call is counted twice.
_WRAPPED_MCP = {"vscode", "cursor"}

# Built-in tool -> the verb the classifier understands. Without this, `Edit`
# and `Grep` classify as UNKNOWN and `Task` (spawning a sub-agent) is not seen
# as delegation, which is exactly what `fan_out` exists to notice.
_VERB = {
    "Edit": "write_file", "MultiEdit": "write_file", "NotebookEdit": "write_file",
    "Write": "write_file", "Read": "read_file", "NotebookRead": "read_file",
    "Glob": "search_files", "Grep": "search_files", "LS": "list_directory",
    "Bash": "bash", "BashOutput": "read_output", "KillShell": "bash",
    "WebFetch": "fetch", "WebSearch": "search_web",
    "Task": "invoke_agent", "Agent": "invoke_agent",
    # Copilot (VS Code) built-ins — both the current and the older names.
    "view": "read_file", "read_file": "read_file", "readFile": "read_file",
    "list_dir": "list_directory", "listDirectory": "list_directory",
    "file_search": "search_files", "grep_search": "search_files",
    "semantic_search": "search_files", "codebase": "search_files", "search": "search_files",
    "create": "write_file", "create_file": "write_file", "edit": "write_file",
    "apply_patch": "write_file", "replace_string_in_file": "write_file",
    "insert_edit_into_file": "write_file", "edit_file": "write_file",
    "bash": "bash", "powershell": "bash", "run_in_terminal": "bash",
    "write_bash": "bash", "write_powershell": "bash",
    "read_bash": "read_output", "read_powershell": "read_output",
    "get_terminal_output": "read_output",
    "fetch_webpage": "fetch", "fetch": "fetch", "open_simple_browser": "fetch",
    "runSubagent": "invoke_agent", "run_subagent": "invoke_agent",
    # Cursor built-ins.
    "search_replace": "write_file", "write": "write_file", "delete_file": "write_file",
    "run_terminal_cmd": "bash", "grep": "search_files", "glob_file_search": "search_files",
    "web_search": "search_web", "read_lints": "read_file",
}

_SHELL_TOOLS = {"Bash", "bash", "powershell", "run_in_terminal", "write_bash",
                "write_powershell", "KillShell", "run_terminal_cmd"}

# Built-in tool -> the argument that names what it touches.
_TARGET_KEYS = ("file_path", "filePath", "target_file", "notebook_path", "path",
                "dirPath", "target_directory", "url", "urls", "command", "input",
                "pattern", "query", "search_term")


def _target(tool: str, args: dict[str, Any]) -> str:
    if tool in _SHELL_TOOLS:
        cmd = args.get("command") or args.get("input")
        return f"shell://{cmd}" if isinstance(cmd, str) and cmd else ""
    if tool == "WebSearch":
        q = args.get("query")
        return f"query://{str(q)[:120]}" if q else ""
    for key in _TARGET_KEYS:
        val = args.get(key)
        if isinstance(val, list) and val and isinstance(val[0], str):
            val = val[0]
        if isinstance(val, str) and val:
            if key in ("command",):
                return f"shell://{val}"
            if key == "query":
                return f"query://{val[:120]}"
            return val
    return ""


def _outcome(response: Any) -> tuple[str, str | None]:
    """(status, error text) from a tool_response of any shape."""
    from hypothesise_core.classify import looks_denied

    text = None
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError") or response.get("error"):
            text = str(response.get("error") or response.get("content") or "")[:500]
    elif isinstance(response, str) and response.lower().startswith(("error", "permission denied")):
        text = response[:500]
    if text is None:
        return "ok", None
    return ("denied" if looks_denied(text) else "error"), text


def _session(payload: dict[str, Any]) -> str:
    # Cursor identifies the chat as conversation_id.
    return str(payload.get("session_id") or payload.get("conversation_id") or "unknown")


def _is_mcp(tool_name: str) -> bool:
    return tool_name.startswith("mcp__") or tool_name.startswith("mcp_")


def build_event(payload: dict[str, Any], step: int,
                client: str = AGENT) -> dict[str, Any] | None:
    from hypothesise_core.classify import classify_action, classify_resource, extract_resource_uri
    from hypothesise_core.events import Event, Outcome, Resource, Status, Tool, digest_args

    agent, prefix = CLIENTS.get(client, CLIENTS[AGENT])
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name:
        return None
    # Copilot's and Cursor's MCP calls already go through the wrapped servers
    # (`init`), so recording them here too would count every one twice.
    if client in _WRAPPED_MCP and _is_mcp(tool_name):
        return None
    args = payload.get("tool_input") or {}
    if not isinstance(args, dict):
        args = {"input": args}

    # MCP tools arrive as mcp__<server>__<tool>; keep the real server name so
    # graph nodes match what the proxy would have recorded for the same call.
    if tool_name.startswith("mcp__") and tool_name.count("__") >= 2:
        _, server, name = tool_name.split("__", 2)
        uri = extract_resource_uri(name, args)
    else:
        server, name = agent, tool_name
        uri = _target(tool_name, args)

    verb = _VERB.get(name, name)
    cls = classify_resource(uri, server, verb)
    # Claude Code and VS Code say tool_response; Cursor says tool_output.
    status, error = _outcome(payload.get("tool_response", payload.get("tool_output")))
    blob = json.dumps(args, default=str)
    ev = Event(
        agent_id=agent,
        session_id=f"{prefix}-{_session(payload)}",
        step=step,
        tool=Tool(server=server, name=name),
        resource=Resource(uri=uri, cls=cls),
        action=classify_action(verb, cls),
        args_digest=digest_args(args),
        arg_keys=sorted(args)[:32],
        arg_bytes=len(blob.encode("utf-8")),
        outcome=Outcome(status=Status(status), error=error),
    )
    return ev.to_dict()


# Window of earlier calls the pre-tool hook reads back from the spool, so a
# sequence (read .env, then curl) is visible to a process that lives for one
# tool call. Bounded: the tail of today's spool file only.
_HISTORY_BYTES = 512 * 1024
_HISTORY_EVENTS = 50


def _history(session: str) -> list[dict[str, Any]]:
    from hypothesise_proxy.spool import spool_files

    files = spool_files()
    if not files:
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(files[-1], "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _HISTORY_BYTES))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    for line in tail[1:] if len(tail) > 1 else tail:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("session_id") == session:
            out.append(rec)
    return out[-_HISTORY_EVENTS:]


def _deny(client: str, sentence: str) -> int:
    """Refuse the tool call in the shape each agent reads, and exit 2, which
    all three treat as "block" even if they ignore the JSON."""
    if client == "cursor":
        print(json.dumps({"permission": "deny", "user_message": sentence,
                          "agent_message": sentence}))
    else:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": sentence}}))
    print(sentence, file=sys.stderr)
    return 2


def decide_pre(payload: dict[str, Any], client: str) -> tuple[dict[str, Any], str] | None:
    """(event record, sentence) when this call must be blocked, else None."""
    from hypothesise_core.engine import Engine
    from hypothesise_core.events import Event, Outcome, Status, Verdict
    from hypothesise_core.policy.rules import RuleSet
    from hypothesise_core.policy.score import SHORT_CIRCUIT
    from hypothesise_proxy import settings
    from hypothesise_proxy.spool import next_step

    _, prefix = CLIENTS.get(client, CLIENTS[AGENT])
    session = f"{prefix}-{_session(payload)}"
    record = build_event({**payload, "tool_response": None}, 0, client)
    if record is None:
        return None
    engine = Engine(rules=RuleSet.from_config(settings.rules()))
    window = engine.window_for(session)
    for rec in _history(session):
        try:
            window.push(Event.from_dict(rec))
        except (KeyError, TypeError, ValueError):
            continue
    ev = Event.from_dict(record)
    import time

    started = time.perf_counter()
    decision, _ = engine.evaluate(ev)
    decision.elapsed_ms = (time.perf_counter() - started) * 1000.0
    if decision.verdict is not Verdict.BLOCK:
        return None
    fired = {d.get("detector") for d in decision.detectors}
    # A fresh process has no baseline and no trust history, so only verdicts
    # that need neither may block here: an explicit rule, or a critical
    # sequence. Anything else would be a guess.
    if "policy_rule" not in fired and not (fired & set(SHORT_CIRCUIT)):
        return None
    ev.step = next_step(session)
    ev.outcome = Outcome(status=Status.BLOCKED)
    ev.decision = decision
    return ev.to_dict(), decision.sentence


def run_pre(client: str = AGENT, stdin: Any = None) -> int:
    """PreToolUse. Observe mode: say nothing, cost nothing. Enforce: block."""
    try:
        raw = (stdin or sys.stdin).read()
        from hypothesise_proxy import settings

        if settings.mode("observe") != "enforce":
            if client == "cursor":
                print("{}")
            return 0
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        out = decide_pre(payload, client)
        if out is None:
            if client == "cursor":
                print("{}")
            return 0
        record, sentence = out
        from hypothesise_proxy import notify
        from hypothesise_proxy.spool import append

        append([record])
        notify.blocked(sentence, record.get("agent_id", ""),
                       f"{record['tool']['server']}/{record['tool']['name']}")
        return _deny(client, sentence)
    except Exception:  # noqa: BLE001 — fail open: a broken hook must not break the agent
        if client == "cursor":
            print("{}")
        return 0


def run(client: str = AGENT, stdin: Any = None) -> int:
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        from hypothesise_proxy.spool import append, next_step

        _, prefix = CLIENTS.get(client, CLIENTS[AGENT])
        if not (client in _WRAPPED_MCP and _is_mcp(str(payload.get("tool_name") or ""))):
            session = f"{prefix}-{_session(payload)}"
            record = build_event(payload, next_step(session), client)
            if record is not None:
                append([record])
    except Exception:  # noqa: BLE001 — a hook must never break the agent
        pass
    if client == "cursor":
        # Cursor reads a JSON reply from every hook; an empty object changes nothing.
        print("{}")
    return 0
