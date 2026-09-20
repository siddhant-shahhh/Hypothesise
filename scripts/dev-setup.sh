#!/usr/bin/env bash
# One-command dev setup.
#
# Uses `python -m pip` throughout rather than bare `pip`, so the interpreter
# that INSTALLS is always the interpreter that IMPORTS. A bare `pip` can belong
# to a different Python than `python3` — common with stacked conda/venv envs, or
# when this script runs under bash while your shell is zsh — and the install
# then silently lands somewhere your shell cannot see.
#
#   bash scripts/dev-setup.sh                      # uses python3 from PATH
#   PYTHON=/opt/homebrew/bin/python3.12 bash scripts/dev-setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

PY="${PYTHON:-python3}"

# `command -v python3` in zsh prints "alias python3=/path/to/python" when python3
# is an alias, not a path. Accept that form rather than choking on it.
case "$PY" in
  *alias*=*) PY="${PY#*=}" ;;
esac
PY="${PY%\'}"; PY="${PY#\'}"        # strip surrounding quotes if present

# Resolve to a real executable. `type -P` consults PATH only, ignoring aliases
# and shell functions.
if [ ! -x "$PY" ]; then
  RESOLVED="$(type -P "$PY" 2>/dev/null || true)"
  if [ -n "$RESOLVED" ]; then
    PY="$RESOLVED"
  else
    cat <<MSG
error: could not resolve '$PY' to a Python executable.

If python3 is a shell alias, \`command -v\` returns the alias text, not a path.
Get the real path from Python itself — this is alias-proof:

    PYTHON="\$(python3 -c 'import sys; print(sys.executable)')" bash scripts/dev-setup.sh

MSG
    exit 1
  fi
fi

echo "==> interpreter"
echo "    $PY -> $("$PY" -c 'import sys; print(sys.executable)')"
echo "    version $("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"

echo "==> installing weaver packages (editable)"
"$PY" -m pip install -q -e packages/core -e packages/store -e packages/proxy

echo "==> installing dev tools"
"$PY" -m pip install -q pytest pytest-asyncio ruff mypy pyyaml sqlalchemy

# Verify from OUTSIDE the repo: importing from the repo root can succeed for the
# wrong reason and hide a broken install.
echo "==> verifying (from a temp dir, not the repo)"
if (cd /tmp && "$PY" -c "
import weaver_core, weaver_store, weaver_proxy
print('    weaver_core ', weaver_core.__version__)
print('    weaver_store  ok')
print('    weaver_proxy', weaver_proxy.__version__)
") ; then
  echo "==> done."
else
  cat <<MSG

!! Install verification FAILED.
   Packages installed into: $("$PY" -c 'import sys; print(sys.executable)')
   Your shell's python3 is: $(command -v python3 2>/dev/null || echo '<none>')

   If those differ, re-run pointing at the Python you actually use:
       PYTHON="\$(command -v python3)" bash scripts/dev-setup.sh

   Tests do not need this install — pyproject.toml sets pythonpath, so
   \`pytest\` works either way.
MSG
  exit 1
fi

RESOLVED="$("$PY" -c 'import sys; print(sys.executable)')"
cat <<MSG

Next:
    $PY -m pytest
    $PY scripts/replay.py --all
    $PY -m weaver_proxy --config weaver.config.example.json

Installed into: $RESOLVED
If an import fails in your shell, run:  $PY scripts/doctor.py
MSG
