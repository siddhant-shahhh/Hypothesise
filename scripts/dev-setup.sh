#!/usr/bin/env bash
# One-command dev setup. Installs every workspace package in editable mode.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> installing weaver packages (editable)"
pip install -q -e packages/core -e packages/store -e packages/proxy

echo "==> installing dev tools"
pip install -q pytest pytest-asyncio ruff mypy pyyaml sqlalchemy

echo "==> verifying"
python3 -c "import weaver_core, weaver_store, weaver_proxy; print('  weaver_core ', weaver_core.__version__); print('  weaver_proxy', weaver_proxy.__version__)"
echo "==> done. run: pytest"
