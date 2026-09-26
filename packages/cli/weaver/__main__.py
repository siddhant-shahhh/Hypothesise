"""The `weaver` command.

    weaver api [--port 8080] [--demo]     control plane and dashboard
    weaver proxy --config FILE [--detect] MCP interception
    weaver doctor                          diagnose the environment
    weaver migrate [--check]               bring the schema up to date
    weaver version

Subcommands rather than separate executables: a stranger who has just run
`pip install weaver` has one thing to discover, and `weaver --help` tells them
the rest. ADR-009 makes the first five minutes the thing this product is judged
on.
"""

from __future__ import annotations

import argparse
import sys


def _cmd_api(args: argparse.Namespace) -> int:
    import uvicorn
    from weaver_api.app import create_app

    app = create_app(db_url=args.db, webhook_url=args.webhook)
    if args.demo:
        from weaver_api.demo import load_demo

        n = load_demo(app.state.repo, app.state.engine)
        print(f"weaver: loaded {n} demo events from the scenario suite")
    print(f"weaver: dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_proxy(args: argparse.Namespace) -> int:
    import asyncio

    from weaver_proxy.config import ProxyConfig
    from weaver_proxy.server import WeaverProxy

    try:
        config = ProxyConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"weaver: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 1

    if args.api:
        config.api_url = args.api

    # An ASK verdict has to reach a human. With no control plane there is
    # nobody to ask, and both remaining options are bad enough to refuse at
    # startup rather than discover in production.
    if config.gate.enforcing and not config.api_url:
        print("weaver: enforce mode needs a control plane for approvals.\n"
              "        run `weaver api`, then pass --api http://127.0.0.1:8080",
              file=sys.stderr)
        return 2

    engine = None
    if args.detect:
        from weaver_core.engine import Engine
        from weaver_core.policy.rules import RuleSet

        engine = Engine(rules=RuleSet.from_config(config.rules))
        extra = f" ({len(engine.rules.rules)} policy rules)" if engine.rules.rules else ""
        print(f"weaver: detection engine enabled{extra}", file=sys.stderr)

    print(f"weaver: mode={config.gate.mode} fail={config.gate.fail_mode} "
          f"budget={config.gate.budget_ms}ms upstreams={len(config.upstreams)}",
          file=sys.stderr)
    asyncio.run(WeaverProxy(config, engine=engine).run())
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from weaver.doctor import main as doctor_main

    return doctor_main()


def _cmd_migrate(args: argparse.Namespace) -> int:
    from weaver_store.db import get_engine
    from weaver_store.migrate import migrate, status

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
    from weaver import __version__

    print(f"weaver {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="weaver",
        description="Behavioural security layer for AI agents. "
                    "Learns what each agent normally does, flags deviations, "
                    "and can block them.",
        epilog="Start with:  weaver api --demo   then open http://127.0.0.1:8080",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    a = sub.add_parser("api", help="run the control plane and dashboard")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8080)
    a.add_argument("--db", default=None, help="database URL (default: ~/.weaver/weaver.db)")
    a.add_argument("--webhook", default=None, metavar="URL",
                   help="POST approval events here")
    a.add_argument("--demo", action="store_true",
                   help="preload the scenario suite so the dashboard has content")
    a.set_defaults(fn=_cmd_api)

    p = sub.add_parser("proxy", help="run the MCP interception layer")
    p.add_argument("--config", default="weaver.config.json")
    p.add_argument("--detect", action="store_true",
                   help="enable the detection engine (default: record only)")
    p.add_argument("--api", default=None, metavar="URL",
                   help="control plane to ship events and approvals to")
    p.set_defaults(fn=_cmd_proxy)

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
