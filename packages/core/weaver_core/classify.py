"""Resource and action classification — rules only, no model call.

`resource.class` is the field the whole risk model hangs off: risk floors,
decay rates and the sensitive-class detectors all key on it. Getting it wrong
is worse than leaving it UNKNOWN, so the rules are deliberately conservative
and ordered most-sensitive first.

ADR-006: no LLM in the decision path. A learned classifier may assist offline
during baseline building; it never runs inline.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .events import Action, ResourceClass

# Order matters. First match wins, and the most dangerous classes are checked
# first so that e.g. `.env` is SECRET rather than CONFIG.
_URI_RULES: list[tuple[ResourceClass, re.Pattern[str]]] = [
    (
        ResourceClass.SECRET,
        re.compile(
            r"(^secret://|^env://|^vault://|/\.env\b|\.env$|/\.aws/|/\.ssh/"
            r"|id_rsa|\.pem$|\.p12$|\.pfx$|\.key$|credential|secrets?\.(ya?ml|json|toml)"
            r"|api[_-]?key|access[_-]?token|private[_-]?key)",
            re.I,
        ),
    ),
    (
        ResourceClass.SHELL,
        re.compile(r"(^shell://|^exec://|^cmd://|^proc://|/bin/|/usr/bin/|\.sh$)", re.I),
    ),
    (
        ResourceClass.DATABASE,
        re.compile(
            r"(^postgres(ql)?://|^mysql://|^mongodb(\+srv)?://|^redis://"
            r"|^mssql://|^oracle://|^sqlite://|^clickhouse://|^snowflake://)",
            re.I,
        ),
    ),
    (
        ResourceClass.COMMUNICATION,
        re.compile(
            r"(^slack://|^mailto:|^smtp://|^discord://|^teams://"
            r"|slack\.com|discord\.com|sendgrid|mailgun)",
            re.I,
        ),
    ),
    (
        ResourceClass.CONFIG,
        re.compile(
            r"(\.ya?ml$|\.toml$|\.ini$|\.cfg$|\.conf$|\.properties$"
            r"|(^|/)config(/|\.)|dockerfile|docker-compose|\.tf$|\.tfvars$)",
            re.I,
        ),
    ),
    (
        ResourceClass.SOURCE_CODE,
        re.compile(
            r"(^github://|^gitlab://|^git://"
            r"|\.(py|js|ts|tsx|jsx|go|java|rb|rs|c|cc|cpp|h|hpp|cs|php|swift|kt|scala|sql|sh)$)",
            re.I,
        ),
    ),
]

# Hosts treated as internal. Anything else over http(s) is external egress.
_INTERNAL_HOST = re.compile(
    r"^(localhost|127\.0\.0\.1|::1|0\.0\.0\.0"
    r"|10\.\d+\.\d+\.\d+"
    r"|192\.168\.\d+\.\d+"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
    r"|.*\.internal|.*\.local|.*\.svc\.cluster\.local)$",
    re.I,
)

_SHELL_TOOL = re.compile(r"(shell|bash|exec|command|terminal|subprocess|run_script)", re.I)
_EGRESS_TOOL = re.compile(r"(fetch|curl|http_request|web_request|post_url|webhook|upload|send)", re.I)
_DB_TOOL = re.compile(r"(query|sql|execute_sql|select|find_documents)", re.I)

_READ_TOOL = re.compile(r"^(get|read|list|search|fetch_file|cat|show|describe|head|stat)", re.I)
_WRITE_TOOL = re.compile(r"^(write|create|update|put|patch|delete|remove|push|commit|merge|insert|drop)", re.I)
_EXEC_TOOL = re.compile(r"^(exec|run|shell|bash|invoke_shell|spawn)", re.I)
_INVOKE_TOOL = re.compile(r"^(invoke_agent|call_agent|delegate|handoff|spawn_agent)", re.I)


def classify_resource(uri: str, tool_server: str = "", tool_name: str = "") -> ResourceClass:
    """Map a resource URI (plus tool hints) to a class."""
    if not uri:
        # No resource URI — fall back to what the tool itself implies.
        blob = f"{tool_server} {tool_name}"
        if _SHELL_TOOL.search(blob):
            return ResourceClass.SHELL
        if _EGRESS_TOOL.search(blob):
            return ResourceClass.NETWORK_EGRESS
        if _DB_TOOL.search(blob):
            return ResourceClass.DATABASE
        return ResourceClass.UNKNOWN

    for cls, pattern in _URI_RULES:
        if pattern.search(uri):
            return cls

    # http(s) is egress only when it leaves the network.
    if uri.lower().startswith(("http://", "https://")):
        host = (urlparse(uri).hostname or "").lower()
        if host and not _INTERNAL_HOST.match(host):
            return ResourceClass.NETWORK_EGRESS
        return ResourceClass.UNKNOWN

    blob = f"{tool_server} {tool_name}"
    if _SHELL_TOOL.search(blob):
        return ResourceClass.SHELL
    if _DB_TOOL.search(blob):
        return ResourceClass.DATABASE

    return ResourceClass.UNKNOWN


def classify_action(tool_name: str, resource_cls: ResourceClass = ResourceClass.UNKNOWN) -> Action:
    """Infer the verb from the tool name, with the resource class as a tiebreak."""
    if _INVOKE_TOOL.match(tool_name):
        return Action.INVOKE
    if _EXEC_TOOL.match(tool_name) or resource_cls is ResourceClass.SHELL:
        return Action.EXECUTE
    if resource_cls is ResourceClass.NETWORK_EGRESS:
        return Action.EGRESS
    if _WRITE_TOOL.match(tool_name):
        return Action.WRITE
    if _READ_TOOL.match(tool_name):
        return Action.READ
    if _EGRESS_TOOL.search(tool_name):
        return Action.EGRESS
    return Action.UNKNOWN
