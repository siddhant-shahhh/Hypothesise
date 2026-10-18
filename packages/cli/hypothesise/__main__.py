"""The `hypothesise` command.

    hypothesise init                            connect the agent apps on this machine
    hypothesise status                          what is connected, what was recorded
    hypothesise mode [observe|enforce]          switch every connected agent at once
    hypothesise api [--port 8080] [--demo]     dashboard (alias: dashboard)
    hypothesise reset                           delete recorded data, start clean
    hypothesise release [AGENT|--all]           restore trust after testing
    hypothesise uninstall                       restore every config init changed
    hypothesise proxy --config FILE [--detect] MCP interception from a config file
    hypothesise wrap --client C --server S -- CMD...   one wrapped server (written by init)
    hypothesise hook claude-code|vscode|cursor  agent hooks (written by init)
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

CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "antigravity")


def _cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    # Line-buffered so the startup lines reach a log file before uvicorn takes over.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    from hypothesise_api.app import create_app

    db_url = args.db
    if args.demo and not db_url:
        # Sample sessions never go into the real database (ADR-040): mixed in,
        # they read as activity that never happened and cannot be told apart.
        from pathlib import Path

        from hypothesise_proxy.spool import home

        demo_db = home() / "demo.db"
        demo_db.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{demo_db}{suffix}").unlink(missing_ok=True)
        db_url = f"sqlite:///{demo_db}"
        print(f"hypothesise: demo mode — sample data in {demo_db}; your real data is untouched")

    port = _free_port(args.host, args.port)
    if port is None:
        print(f"hypothesise: cannot listen on {args.host}:{args.port} or the next 20 ports.\n"
              f"        Another program is using them, or (on Windows) Hyper-V/WSL/Docker\n"
              f"        reserved them. Try:  hypothesise api --port 9123", file=sys.stderr)
        return 1
    if port != args.port:
        print(f"hypothesise: port {args.port} is unavailable (in use, or reserved by the OS); "
              f"using {port} instead")
    args.port = port

    app = create_app(db_url=db_url, webhook_url=args.webhook)
    if args.demo:
        from hypothesise_api.demo import load_demo

        app.state.demo = True
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
    # Whatever proxies and hooks recorded while the dashboard was closed. Not in
    # demo mode: the spool would be drained into the throwaway demo database
    # and the real events lost from the real one.
    if not args.demo:
        from hypothesise_api.app import ingest_spool_once, start_spool_ingest

        caught_up = ingest_spool_once(app)
        if caught_up:
            print(f"hypothesise: read {caught_up} events recorded while the dashboard was closed")
        start_spool_ingest(app)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _free_port(host: str, port: int, tries: int = 21) -> int | None:
    """The first port from `port` that can actually be bound, or None.

    Windows reserves whole port ranges for Hyper-V, WSL and Docker, and 8080 is
    often inside one; the raw error ("an attempt was made to access a socket in
    a way forbidden by its access permissions") tells a user nothing useful.
    """
    import os
    import socket

    for candidate in range(port, port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # Same as uvicorn on POSIX, so a port still in TIME_WAIT after a
            # restart is not mistaken for taken. Never on Windows, where the
            # flag lets two processes share a port.
            if os.name != "nt":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return None


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

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("hypothesise wrap: no server command given after --", file=sys.stderr)
        return 2
    try:
        import asyncio

        from hypothesise_core.engine import Engine
        from hypothesise_proxy import settings as _settings
        from hypothesise_proxy.config import ProxyConfig
        from hypothesise_proxy.gate import GateConfig
        from hypothesise_proxy.server import HypothesiseProxy
        from hypothesise_proxy.upstream import UpstreamConfig

        config = ProxyConfig(
            upstreams=[UpstreamConfig(name=args.server, command=cmd[0], args=cmd[1:])],
            gate=GateConfig.from_env(),
            agent_id=args.client,
            api_url=_settings.api_url(""),
            session_id=_session_for(args.client),
        )
        if config.gate.enforcing and not config.api_url:
            # Refusing to start would take the user's server down with us.
            print("hypothesise: enforce mode needs the dashboard for approvals "
                  "(set HYPOTHESISE_API_URL); running in observe mode", file=sys.stderr)
            config.gate.mode = "observe"
        from hypothesise_core.policy.rules import RuleSet
        from hypothesise_proxy import baselines as _bl

        engine = Engine.live(rules=RuleSet.from_config(_settings.rules()))
        _bl.load_into(engine)
        proxy = HypothesiseProxy(config, engine=engine)
    except Exception as exc:  # noqa: BLE001
        print(f"hypothesise: could not start ({exc}); running {cmd[0]} unwrapped",
              file=sys.stderr)
        return _passthrough(cmd)
    print(f"hypothesise: guarding {args.server} for {args.client} "
          f"(mode={config.gate.mode})", file=sys.stderr)
    asyncio.run(proxy.run())
    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    from hypothesise.hook import run, run_pre

    return run_pre(args.client) if args.pre else run(args.client)


def _cmd_init(args: argparse.Namespace) -> int:
    from hypothesise.clients import cmd_init

    return cmd_init(args)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from hypothesise.clients import cmd_uninstall

    return cmd_uninstall(args)


def _cmd_release(args: argparse.Namespace) -> int:
    """Give agents their trust back (Safe zone) — e.g. after testing attacks."""
    import json
    import urllib.request

    api = (args.api or "http://127.0.0.1:8080").rstrip("/")

    def call(path: str, method: str = "GET"):
        req = urllib.request.Request(f"{api}{path}", method=method,
                                     data=b"" if method == "POST" else None)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8") or "null")

    try:
        agents = call("/v1/agents")
    except Exception:  # noqa: BLE001
        print(f"hypothesise: the dashboard is not running at {api} — start it with "
              "`hypothesise api`, then run this again.", file=sys.stderr)
        return 1
    names = [a["agent_id"] for a in agents
             if args.all and a.get("zone") in ("warm", "hot") or a["agent_id"] in (args.agent or [])]
    if not names:
        print("hypothesise: no agent needs releasing — all are in the Safe zone."
              if args.all else "hypothesise: name an agent, or use --all")
        return 0
    for n in names:
        row = call(f"/v1/agents/{urllib.request.quote(n, safe='')}/release", "POST")
        print(f"  released {n}: trust {row.get('trust', 0):.2f}, zone {row.get('zone')}")
    print("Connected proxies pick this up within about 30 seconds.")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    """Forget everything recorded; keep connections, mode and protections.

    For a machine that ran tests or demos into its real database before
    0.1.0a4, and for anyone who wants a clean start (ADR-042).
    """
    import json
    import urllib.request

    from hypothesise_proxy.spool import home, spool_dir

    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=1):
            print("hypothesise: the dashboard is running — stop it (Ctrl+C) first, "
                  "then run reset again.", file=sys.stderr)
            return 1
    except Exception:  # noqa: BLE001 — not running is what we want
        pass
    root = home()
    targets = [p for p in (root / "hypothesise.db", root / "hypothesise.db-wal",
                           root / "hypothesise.db-shm") if p.exists()]
    spooled = [p for p in spool_dir().glob("*") if p.is_file()] if spool_dir().is_dir() else []
    if not targets and not spooled:
        print("hypothesise: nothing recorded yet — already clean.")
        return 0
    print("hypothesise reset — deletes every recorded event, alert and trust score:")
    for p in targets:
        print(f"  {p}")
    if spooled:
        print(f"  {spool_dir()}  ({len(spooled)} file(s) waiting for the dashboard)")
    print("Kept: app connections, observe/enforce mode, protections, backups.")
    if not args.yes:
        try:
            if input("Delete? [y/N] ").strip().lower() not in ("y", "yes"):
                print("hypothesise: nothing deleted.")
                return 1
        except EOFError:
            print("hypothesise: nothing deleted (pass --yes to confirm).")
            return 1
    for p in [*targets, *spooled]:
        try:
            p.unlink()
        except OSError as exc:
            print(f"hypothesise: could not delete {p}: {exc}", file=sys.stderr)
            return 1
    print(json.dumps({"deleted": len(targets) + len(spooled)}) if args.json else
          "hypothesise: done. The dashboard starts empty and shows only what your agents do from now on.")
    return 0


def _cmd_mode(args: argparse.Namespace) -> int:
    from hypothesise.mode import cmd_mode

    return cmd_mode(args)


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
        # Refusing to start shows up in the agent app as a broken server, and
        # takes every tool behind it down with it. Blocks work without the
        # dashboard; only approvals need it (ADR-042).
        print("hypothesise: enforce mode without a dashboard — blocks still apply; "
              "actions that need approval cannot be approved until `hypothesise api` runs",
              file=sys.stderr)

    engine = None
    if args.detect:
        from hypothesise_core.engine import Engine
        from hypothesise_core.policy.rules import RuleSet

        # The proxy's own rules plus the personal-data rules from
        # `hypothesise mode enforce` (ADR-042).
        from hypothesise_proxy import baselines as _bl
        from hypothesise_proxy import settings as _settings

        engine = Engine.live(rules=RuleSet.from_config([*config.rules, *_settings.rules()]))
        _bl.load_into(engine)
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

    md = sub.add_parser("mode", help="show or switch observe / enforce for every connected agent")
    md.add_argument("value", nargs="?", choices=["observe", "enforce"])
    md.add_argument("--api", default=None, metavar="URL",
                    help="dashboard that approves ASK decisions (default http://127.0.0.1:8080)")
    pg = md.add_mutually_exclusive_group()
    pg.add_argument("--popup", dest="popup", action="store_true", default=None,
                    help="show an on-screen popup when an action is blocked (default)")
    pg.add_argument("--no-popup", dest="popup", action="store_false",
                    help="no popups; blocks still show on the dashboard")
    md.add_argument("--setup", action="store_true",
                    help="ask again which personal data agents may never touch")
    md.add_argument("-y", "--yes", action="store_true",
                    help="no questions: keep saved answers, or use the safe defaults")
    md.set_defaults(fn=_cmd_mode)

    u = sub.add_parser("uninstall", help="restore every config that init changed")
    client_opts(u)
    u.set_defaults(fn=_cmd_uninstall)

    rl = sub.add_parser("release", help="restore trust for agents (after testing attacks)")
    rl.add_argument("agent", nargs="*", help="agent id(s), as shown on the Agents tab")
    rl.add_argument("--all", action="store_true", help="every agent in Caution or Restricted")
    rl.add_argument("--api", default=None, metavar="URL")
    rl.set_defaults(fn=_cmd_release)

    r = sub.add_parser("reset", help="delete everything recorded (keeps connections and settings)")
    r.add_argument("-y", "--yes", action="store_true", help="do not ask")
    r.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(fn=_cmd_reset)

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
    h.add_argument("client", choices=["claude-code", "vscode", "cursor"])
    h.add_argument("--pre", action="store_true",
                   help="before the tool runs: block it in enforce mode (written by init)")
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
