"""Entry point:  python -m weaver_api"""

from __future__ import annotations

import argparse


def main() -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="weaver-api", description="Weaver control plane")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=None, help="database URL (default: sqlite ./data/weaver.db)")
    ap.add_argument("--webhook", default=None, metavar="URL",
                    help="POST approval events here (Slack relay, PagerDuty, anything)")
    ap.add_argument("--demo", action="store_true",
                    help="load the scenario suite so the dashboard has something to show")
    args = ap.parse_args()

    from .app import create_app

    app = create_app(db_url=args.db, webhook_url=args.webhook)

    if args.demo:
        from .demo import load_demo

        n = load_demo(app.state.repo, app.state.engine)
        print(f"weaver: loaded {n} demo events from the scenario suite")

    print(f"weaver: dashboard on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
