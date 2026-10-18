"""Entry point:  python -m hypothesise_api"""

from __future__ import annotations

import argparse


def main() -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="hypothesise-api", description="Hypothesise control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=None, help="database URL (default: sqlite ./data/hypothesise.db)")
    ap.add_argument("--webhook", default=None, metavar="URL",
                    help="POST approval events here (Slack relay, PagerDuty, anything)")
    ap.add_argument("--demo", action="store_true",
                    help="load the scenario suite so the dashboard has something to show")
    args = ap.parse_args()

    from .app import create_app

    db_url = args.db
    if args.demo and not db_url:
        # load_demo() clears the database it is given. Pointed at the default,
        # that was the user's real history (ADR-042) — never again.
        from pathlib import Path

        from hypothesise_proxy.spool import home

        demo_db = home() / "demo.db"
        demo_db.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{demo_db}{suffix}").unlink(missing_ok=True)
        db_url = f"sqlite:///{demo_db}"
    app = create_app(db_url=db_url, webhook_url=args.webhook)

    if args.demo:
        from .demo import load_demo

        app.state.demo = True
        n = load_demo(app.state.repo, app.state.engine)
        print(f"hypothesise: loaded {n} demo events from the scenario suite")

    print(f"hypothesise: dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
