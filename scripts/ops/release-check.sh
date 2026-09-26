#!/usr/bin/env bash
# Pre-release gate. Run before tagging or publishing anything.
#
# Rebuilds the environment from scratch on purpose: both undeclared
# dependencies this project has shipped (pyyaml, httpx) were invisible until
# something installed into an empty environment.
set -uo pipefail
cd "$(dirname "$0")/../.."
FAIL=0

step()  { printf "\n\033[1m==> %s\033[0m\n" "$1"; }
check() { if "$@" >/dev/null 2>&1; then echo "    PASS  $*"; else echo "    FAIL  $*"; FAIL=1; fi; }

step "clean environment (catches undeclared dependencies)"
rm -rf .venv
bash scripts/dev-setup.sh >/dev/null 2>&1 || { echo "    FAIL  dev-setup"; exit 1; }
# shellcheck disable=SC1091
source .venv/bin/activate
echo "    PASS  rebuilt .venv"

step "correctness"
check python -m pytest -q
check python -m ruff check packages scripts tests redteam
check python -m mypy packages/core/weaver_core

step "detection quality"
check python scripts/replay.py --all --quiet    # >=80% detection, <=5% false positives
check python redteam/run.py                     # nothing gets through that shouldn't

step "operational safety"
check python scripts/ops/drills.py              # every ARCHITECTURE section 9 guarantee
check python scripts/ops/loadtest.py --n 20000  # p99 well inside the decision budget

step "packaging"
python -m pip install -q build 2>/dev/null
rm -rf dist
if python -m build --wheel >/dev/null 2>&1; then
  echo "    PASS  wheel built"
  python - <<'PY'
import glob, sys, zipfile
w = sorted(glob.glob("dist/*.whl"))
if not w:
    print("    FAIL  no wheel"); sys.exit(1)
names = zipfile.ZipFile(w[-1]).namelist()
want = {"weaver", "weaver_core", "weaver_store", "weaver_proxy",
        "weaver_api", "weaver_langgraph"}
missing = want - {n.split("/")[0] for n in names if "/" in n}
if missing:
    print("    FAIL  missing from wheel: %s" % sorted(missing)); sys.exit(1)
if not any(n.endswith("entry_points.txt") for n in names):
    print("    FAIL  no console entry point"); sys.exit(1)
print("    PASS  wheel contains every package and the entry point")
PY
  [ $? -ne 0 ] && FAIL=1
else
  echo "    FAIL  wheel build"; FAIL=1
fi

step "a stranger's install actually works"
TMP="$(mktemp -d)"
if python -m venv "$TMP/v" >/dev/null 2>&1 \
   && "$TMP/v/bin/pip" install -q "$(ls dist/*.whl | head -1)" >/dev/null 2>&1; then
  check "$TMP/v/bin/weaver" version
  check "$TMP/v/bin/weaver" migrate --db "sqlite:///$TMP/x.db"
  check "$TMP/v/bin/weaver" --help
else
  echo "    FAIL  could not install the wheel into a clean venv"; FAIL=1
fi
rm -rf "$TMP"

printf "\n\033[1m%s\033[0m\n" \
  "$([ $FAIL -eq 0 ] && echo 'RELEASE CHECK PASSED' || echo 'RELEASE CHECK FAILED')"
exit $FAIL
