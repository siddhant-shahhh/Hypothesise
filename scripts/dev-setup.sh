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

echo "==> checking for a shadowing package"
# `weaver` is a real package on PyPI (ADR-030). `pip uninstall weaver` removes
# its metadata but can leave site-packages/weaver/ behind, and a real directory
# there beats an editable install's path entry — so `import weaver` silently
# resolves to the stranger's code and fails on ITS imports.
SITE="$("$TARGET" -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null || echo "")"
if [ -n "$SITE" ] && [ -d "$SITE/weaver" ] && [ ! -e "$SITE/weaver/__main__.py" ]; then
  cat <<MSG
error: a different package named 'weaver' is installed and will shadow this one.

    $SITE/weaver

That is almost certainly the unrelated 'weaver' package from PyPI. Remove it:

    rm -rf "$SITE/weaver" "$SITE"/weaver-*.dist-info
    bash scripts/dev-setup.sh

MSG
  exit 1
fi
echo "    none found"

echo "==> installing weaver packages (editable)"
# Every member must have a pyproject.toml, or pip fails with a message that
# looks nothing like the real cause. Check first and say so plainly.
MISSING=""
for m in packages/core packages/store packages/proxy packages/api packages/cli \
         packages/adapters/langgraph; do
  [ -f "$m/pyproject.toml" ] || MISSING="$MISSING $m"
done
if [ -n "$MISSING" ]; then
  echo "error: these packages have no pyproject.toml and cannot be installed:"
  for m in $MISSING; do echo "    $m"; done
  echo
  echo "That is a repo bug, not an environment problem. pytest passes anyway"
  echo "because it resolves imports via pythonpath — see tests/test_packaging.py."
  exit 1
fi

if ! "$TARGET" -m pip install -e packages/core -e packages/store -e packages/proxy \
      -e packages/api -e packages/cli -e packages/adapters/langgraph; then
  cat <<'MSG'

error: pip refused to install. The output above is pip's own message — read it
before assuming a cause.

Two common ones:
  * "externally-managed-environment" (PEP 668) — you are installing into a
    system Python. Re-run without --here so a virtualenv is created.
  * "does not appear to be a Python project" — a package is missing its
    pyproject.toml. That is a repo bug; run pytest tests/test_packaging.py.

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
import weaver, weaver_core, weaver_store, weaver_proxy, weaver_api
print('    weaver      ', weaver.__version__)
print('    weaver_core ', weaver_core.__version__)
print('    weaver_store  ok')
print('    weaver_proxy', weaver_proxy.__version__)
print('    weaver_api    ok')
"); then
  echo "!! verification failed. Run: $TARGET scripts/doctor.py"
  echo "   (use the standalone script, not \`weaver doctor\` — if the weaver"
  echo "    package itself is broken, the CLI cannot load to diagnose it)"
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
