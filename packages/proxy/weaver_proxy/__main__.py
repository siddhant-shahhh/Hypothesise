"""Entry point:  python -m weaver_proxy --config weaver.config.json"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import ProxyConfig
from .server import WeaverProxy


def main() -> int:
    ap = argparse.ArgumentParser(prog="weaver-proxy", description="Weaver MCP proxy")
    ap.add_argument("--config", default="weaver.config.json")
    ap.add_argument("--detect", action="store_true",
                    help="enable the detection engine (default: record only)")
    ap.add_argument("--api", default=None, metavar="URL",
                    help="control plane to ship events to, e.g. http://127.0.0.1:8080")
    args = ap.parse_args()

    try:
        config = ProxyConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"weaver: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 1

    if args.api:
        config.api_url = args.api

    # An ASK verdict has to reach a human. Without a control plane there is
    # nobody to ask, and the only honest options are "allow everything ASK-worthy"
    # or "block everything ASK-worthy" — both bad enough to refuse at startup
    # rather than discover in production.
    if config.gate.enforcing and not config.api_url:
        print("weaver: enforce mode needs a control plane for approvals.\n"
              "        start it with `weaver-api`, then pass --api http://127.0.0.1:8080\n"
              "        (or set weaver.apiUrl in the config file)", file=sys.stderr)
        return 2

    print(
        f"weaver: mode={config.gate.mode} fail={config.gate.fail_mode} "
        f"budget={config.gate.budget_ms}ms upstreams={len(config.upstreams)}",
        file=sys.stderr,
    )
    engine = None
    if args.detect:
        from weaver_core.engine import Engine
        from weaver_core.policy.rules import RuleSet

        engine = Engine(rules=RuleSet.from_config(config.rules))
        msg = "weaver: detection engine enabled"
        if engine.rules.rules:
            msg += f" ({len(engine.rules.rules)} policy rules)"
        print(msg, file=sys.stderr)

    asyncio.run(WeaverProxy(config, engine=engine).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
