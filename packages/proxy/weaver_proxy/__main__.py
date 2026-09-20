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
    args = ap.parse_args()

    try:
        config = ProxyConfig.load(args.config)
    except (OSError, ValueError) as exc:
        print(f"weaver: cannot load config {args.config}: {exc}", file=sys.stderr)
        return 1

    print(
        f"weaver: mode={config.gate.mode} fail={config.gate.fail_mode} "
        f"budget={config.gate.budget_ms}ms upstreams={len(config.upstreams)}",
        file=sys.stderr,
    )
    asyncio.run(WeaverProxy(config).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
