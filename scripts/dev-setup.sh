#!/usr/bin/env bash
# One-command setup.
#
# Creates and uses a virtual environment by default. Modern Pythons (Homebrew,
# Debian/Ubuntu, Fedora) mark themselves "externally managed" under PEP 668 and
# refuse `pip install` outright. The documented escape hatch,
# --break-system-packages, can damage a Homebrew install, so this script does
# not use it. A venv is the correct answer and costs one command.
#
#   bash scripts/dev-setup.sh                 # create/use ./.venv
#   bash scripts/dev-setup.sh --here          # install into the ACTIVE env instead
#   PYTHON=/path/to/python3.12 bash scripts/dev-setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
VENV="$REPO/.venv"

USE_ACTIVE=0
[ "${1:-}" = "--here" ] && USE_ACTIVE=1

# ---- resolve a base interpreter -------------------------------------------
PY="${PYTHON:-python3}"

# `command -v python3` in zsh prints "alias python3=/path/..." when python3 is
# an alias, not a path. Accept that form rather than choking on it.
case "$PY" in *alias*=*) PY="${PY#*=}" ;; esac
PY="${PY%\'}"; PY="${PY#\'}"

if [ ! -x "$PY" ]; then
  RESOLVED="$(type -P "$PY" 2>/dev/null || true)"
  if [ -n "$RESOLVED" ]; then PY="$RESOLVED"; else
    echo "error: could not resolve '$PY' to a Python executable."
    echo "If python3 is a shell alias, get the real path from Python itself:"
    echo "    PYTHON=\"\$(python3 -c 'import sys; print(sys.executable)')\" bash scripts/dev-setup.sh"
    exit 1
  fi
fi

echo "==> base interpreter"
echo "    $($PY -c 'import sys; print(sys.executable)')  ($($PY -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])'))"

# ---- pick the target environment ------------------------------------------
if [ "$USE_ACTIVE" = "1" ]; then
  TARGET="$PY"
  echo "==> installing into the ACTIVE environment (--here)"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
  TARGET="$VIRTUAL_ENV/bin/python"
  echo "==> using the already-active virtualenv"
  echo "    $VIRTUAL_ENV"
else
  if [ ! -x "$VENV/bin/python" ]; then
    echo "==> creating virtualenv at .venv"
    "$PY" -m venv "$VENV" || {
      echo
      echo "error: could not create a virtualenv."
      echo "On Debian/Ubuntu you may need:  sudo apt install python3-venv"
      exit 1
    }
  else
    echo "==> reusing existing .venv"
  fi
  TARGET="$VENV/bin/python"
fi

echo "==> target: $("$TARGET" -c 'import sys; print(sys.executable)')"

# ---- install ---------------------------------------------------------------
"$TARGET" -m pip install -q --upgrade pip >/dev/null 2>&1 || true

echo "==> installing weaver packages (editable)"
if ! "$TARGET" -m pip install -q \
      -e packages/core -e packages/store -e packages/proxy -e packages/api; then
  cat <<'MSG'

error: pip refused to install.

If the message mentions "externally-managed-environment" (PEP 668), you are
installing into a system Python. Re-run without --here so the script creates a
virtualenv:

    bash scripts/dev-setup.sh

MSG
  exit 1
fi

echo "==> installing dev tools"
# httpx: required by starlette's TestClient. pyyaml: the scenario loader.
"$TARGET" -m pip install -q pytest pytest-asyncio httpx ruff mypy pyyaml

# ---- verify from OUTSIDE the repo -----------------------------------------
# Importing from the repo root can succeed for the wrong reason and hide a
# broken install.
echo "==> verifying"
if ! (cd /tmp && "$TARGET" -c "
import weaver_core, weaver_store, weaver_proxy, weaver_api
print('    weaver_core ', weaver_core.__version__)
print('    weaver_store  ok')
print('    weaver_proxy', weaver_proxy.__version__)
print('    weaver_api    ok')
"); then
  echo "!! verification failed. Run: $TARGET scripts/doctor.py"
  exit 1
fi

# ---- tell the user exactly what to run ------------------------------------
VENV_BIN="$(dirname "$TARGET")"
cat <<MSG

==> done.

Activate the environment:

    source ${VENV_BIN}/activate

Then:

    pytest
    python -m weaver_api --demo
    python scripts/replay.py --all

Or run without activating, using the full path:

    ${TARGET} -m pytest
    ${TARGET} -m weaver_api --demo

MSG
