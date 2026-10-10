"""The `hypothesise` command.

    hypothesise init                            connect the agent apps on this machine
    hypothesise status                          what is connected, what was recorded
    hypothesise api [--port 8080] [--demo]     dashboard (alias: dashboard)
    hypothesise uninstall                       restore every config init changed
    hypothesise proxy --config FILE [--detect] MCP interception from a config file
    hypothesise wrap --client C --server S -- CMD...   one wrapped server (written by init)
    hypothesise hook claude-code|vscode         agent hooks (written by init)
    hypothesise doctor                          diagnose the environment
    hypothesise migrate [--check]               bring the schema up to date
    hypothesise version

Subcommands rather than separate executables: a stranger who has just run
`pip install hypothesise` has one thing to discover, and `hypothesise --help` tells them
the rest. ADR-009 makes the first five minutes the thing this product is judged
on.
"""

from __future__ import annotations

import argparse
import sys

CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode")


def _cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    # Line-buffered so the startup lines reach a log file before uvicorn takes over.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    from hypothesise_api.app import create_app

    app = create_app(db_url=args.db, webhook_url=args.webhook)
    if args.demo:
        from hypothesise_api.demo import load_demo

        n = load_demo(app.state.repo, app.state.engine)
        print(f"hypothesise: loaded {n} demo events from the scenario suite")
    # Say WHICH index.html is being served. A non-editable install leaves a
    # real copy in site-packages that shadows the repo, so "I changed the
    # dashboard and nothing changed" is a path problem with no other symptom.
    from hypothesise_api.app import STATIC

    index = STATIC / "index.html"
    print(f"hypothesise: dashboard on http://{args.host}:{args.port}")
    if index.exists():
        from datetime import datetime, timezone

        when = datetime.fromtimestamp(
            index.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"hypothesise: serving {index}  (modified {when} UTC)")
    else:
        print(f"hypothesise: no dashboard file at {index}", file=sys.stderr)
    # Whatever proxies and hooks recorded while the dashboard was closed.
    from hypothesise_api.app import ingest_spool_once, start_spool_ingest

    caught_up = ingest_spool_once(app)
    if caught_up:
        print(f"hypothesise: read {caught_up} events recorded while the dashboard was closed")
    start_spool_ingest(app)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _session_for(client: str) -> str:
    """One session per running app, shared by all of its wrapped servers.

    The app starts every server as a direct child, so the parent pid is the
    same for all of them — that is what lets the dashboard see "read a secret
    via one server, sent it out via another" as a single chain. The date
    guards against pid reuse.
    """
    import hashlib
    import os
    import time

    key = f"{client}:{os.getppid()}:{time.strftime('%Y%m%d')}"
    return "sess-" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _passthrough(cmd: list[str]) -> int:
    """Run the original server untouched. The fallback when wrapping fails:
    a broken security layer must cost the user its protection, not their tool."""
    import os
    import subprocess

    from hypothesise_proxy.upstream import resolve_command

    exe = resolve_command(cmd[0], dict(os.environ))
    if os.name != "nt":
        os.execv(exe, [exe, *cmd[1:]])
    return subprocess.call([exe, *cmd[1:]])


def _cmd_wrap(args: argparse.Namespace) -> int:
    import os

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("hypothesise wrap: no server command given after --", file=sys.stderr)
        return 2
    try:
        import asyncio

        from hypothesise_core.engine import Engine
        from hypothesise_proxy.config import ProxyConfig
        from hypothesise_proxy.gate import GateConfig
        from hypothesise_proxy.server import HypothesiseProxy
        from hypothesise_proxy.upstream import UpstreamConfig

        config = ProxyConfig(
            upstreams=[UpstreamConfig(name=args.server, command=cmd[0], args=cmd[1:])],
            gate=GateConfig.from_env(),
            agent_id=args.client,
            api_url=os.environ.get("HYPOTHESISE_API_URL", ""),
            session_id=_session_for(args.client),
        )
        if config.gate.enforcing and not config.api_url:
            # Refusing to start would take the user's server down with us.
            print("hypothesise: enforce mode needs the dashboard for approvals "
                  "(set HYPOTHESISE_API_URL); running in observe mode", file=sys.stderr)
            config.gate.mode = "observe"
        proxy = HypothesiseProxy(config, engine=Engine())
    except Exception as exc:  # noqa: BLE001
        print(f"hypothesise: could not start ({exc}); running {cmd[0]} unwrapped",
              file=sys.stderr)
        return _passthrough(cmd)
    print(f"hypothesise: guarding {args.server} for {args.client} "
          f"(mode={config.gate.mode})", file=sys.stderr)
    asyncio.run(proxy.run())
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    from hypothesise.hook import run

    return run(args.client)


def _cmd_init(args: argparse.Namespace) -> int:
    from hypothesise.clients import cmd_init

    return cmd_init(args)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from hypothesise.clients import cmd_uninstall

    return cmd_uninstall(args)


def _cmd_status(args: argparse.Namespace) -> int:
    from hypothesise.clients import cmd_status

    return cmd_status(args)


def _cmd_proxy(args: argparse.Namespace) -> int:
    import asyncio

    from hypothesise_proxy.config import ProxyConfig
    from hypothesise_proxy.server import HypothesiseProxy

    try:
        config = ProxyConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"hypothesise: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 1

    if args.api:
        config.api_url = args.api

    # An ASK verdict has to reach a human. With no control plane there is
    # nobody to ask, and both remaining options are bad enough to refuse at
    # startup rather than discover in production.
    if config.gate.enforcing and not config.api_url:
        print("hypothesise: enforce mode needs a control plane for approvals.\n"
              "        run `hypothesise api`, then pass --api http://127.0.0.1:8080",
              file=sys.stderr)
        return 2

    engine = None
    if args.detect:
        from hypothesise_core.engine import Engine
        from hypothesise_core.policy.rules import RuleSet

        engine = Engine(rules=RuleSet.from_config(config.rules))
        extra = f" ({len(engine.rules.rules)} policy rules)" if engine.rules.rules else ""
        print(f"hypothesise: detection engine enabled{extra}", file=sys.stderr)

    print(f"hypothesise: mode={config.gate.mode} fail={config.gate.fail_mode} "
          f"budget={config.gate.budget_ms}ms upstreams={len(config.upstreams)}",
          file=sys.stderr)
    asyncio.run(HypothesiseProxy(config, engine=engine).run())
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from hypothesise.doctor import main as doctor_main

    return doctor_main()


def _cmd_migrate(args: argparse.Namespace) -> int:
    from hypothesise_store.db import get_engine
    from hypothesise_store.migrate import migrate, status

    engine = get_engine(args.db)
    st = status(engine)
    if args.check:
        print(f"schema {st['current']} / {st['target']}"
              f"{'  up to date' if st['up_to_date'] else ''}")
        for p in st["pending"]:
            print(f"  pending  {p}")
        return 0 if st["up_to_date"] else 1

    applied = migrate(engine)
    if applied:
        for a in applied:
            print(f"applied  {a}")
    else:
        print(f"schema already at {st['target']}, nothing to do")
    return 0


def _cmd_version(_args: argparse.Namespace) -> int:
    from hypothesise import __version__

    print(f"hypothesise {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hypothesise",
        description="Behavioural security layer for AI agents. "
                    "Learns what each agent normally does, flags deviations, "
                    "and can block them.",
        epilog="Start with:  hypothesise init   (connects your agent apps)\n"
               "then:        hypothesise api    and open http://127.0.0.1:8080\n"
               "Just looking? hypothesise api --demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def client_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--client", action="append", choices=CLIENTS, metavar="APP",
                        help=f"only this app (repeatable): {', '.join(CLIENTS)}")
        sp.add_argument("--project", default=None, metavar="DIR",
                        help="also check DIR/.cursor and DIR/.vscode (default: current dir)")
        sp.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
        sp.add_argument("-y", "--yes", action="store_true", help="apply without asking")

    i = sub.add_parser("init", help="connect the agent apps on this machine")
    client_opts(i)
    i.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    i.set_defaults(fn=_cmd_init)

    st = sub.add_parser("status", help="what is connected, what has been recorded")
    st.add_argument("--project", default=None, metavar="DIR")
    st.set_defaults(fn=_cmd_status)

    u = sub.add_parser("uninstall", help="restore every config that init changed")
    client_opts(u)
    u.set_defaults(fn=_cmd_uninstall)

    a = sub.add_parser("api", aliases=["dashboard"], help="run the dashboard (alias: dashboard)")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8080)
    a.add_argument("--db", default=None, help="database URL (default: ~/.hypothesise/hypothesise.db)")
    a.add_argument("--webhook", default=None, metavar="URL",
                   help="POST approval events here")
    a.add_argument("--demo", action="store_true",
                   help="preload the scenario suite so the dashboard has content")
    a.set_defaults(fn=_cmd_api)

    p = sub.add_parser("proxy", help="run the MCP interception layer")
    p.add_argument("--config", default="hypothesise.config.json")
    p.add_argument("--detect", action="store_true",
                   help="enable the detection engine (default: record only)")
    p.add_argument("--api", default=None, metavar="URL",
                   help="control plane to ship events and approvals to")
    p.set_defaults(fn=_cmd_proxy)

    w = sub.add_parser("wrap", help="run one MCP server behind Hypothesise (written by init)")
    w.add_argument("--client", required=True)
    w.add_argument("--server", required=True)
    w.add_argument("cmd", nargs=argparse.REMAINDER, help="-- the original server command")
    w.set_defaults(fn=_cmd_wrap)

    h = sub.add_parser("hook", help="agent hook receiver (written by init)")
    h.add_argument("client", choices=["claude-code", "vscode"])
    h.set_defaults(fn=_cmd_hook)

    d = sub.add_parser("doctor", help="diagnose the environment")
    d.set_defaults(fn=_cmd_doctor)

    m = sub.add_parser("migrate", help="bring the database schema up to date")
    m.add_argument("--db", default=None)
    m.add_argument("--check", action="store_true",
                   help="report status and exit non-zero if migrations are pending")
    m.set_defaults(fn=_cmd_migrate)

    v = sub.add_parser("version", help="print the version")
    v.set_defaults(fn=_cmd_version)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
