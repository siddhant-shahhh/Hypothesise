#!/usr/bin/env python3
"""Environment check — run this when an import fails.

Reports which interpreter is running, whether each weaver package imports and
from where, and whether the repo is on sys.path. Prints a concrete fix rather
than a diagnosis.

    python3 scripts/doctor.py
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKGS = ["weaver_core", "weaver_store", "weaver_proxy"]


def main() -> int:
    print("=" * 66)
    print("WEAVER DOCTOR")
    print("=" * 66)

    print("\n[interpreter]")
    print(f"  running     {sys.executable}")
    print(f"  version     {sys.version.split()[0]}")

    which_py = shutil.which("python3")
    which_pip = shutil.which("pip")
    print(f"  PATH python3 {which_py or '<none>'}")
    print(f"  PATH pip     {which_pip or '<none>'}")

    # The bug this script exists for: pip and python belonging to different Pythons.
    pip_py = None
    if which_pip:
        try:
            out = subprocess.run([which_pip, "-V"], capture_output=True, text=True, timeout=15)
            pip_py = out.stdout.strip()
            print(f"  pip -V      {pip_py}")
        except (OSError, subprocess.SubprocessError):
            pass

    mismatch = bool(which_py and which_py != sys.executable)
    if mismatch:
        print(f"\n  !! This script is running {sys.executable}")
        print(f"     but your shell's python3 is {which_py}")

    print("\n[packages]")
    missing = []
    for name in PKGS:
        try:
            mod = importlib.import_module(name)
            loc = getattr(mod, "__file__", "?")
            ver = getattr(mod, "__version__", "")
            print(f"  OK      {name:<14} {ver:<8} {loc}")
        except ImportError:
            print(f"  MISSING {name}")
            missing.append(name)

    print("\n[paths]")
    print(f"  repo        {REPO}")
    print(f"  cwd         {Path.cwd()}")
    on_path = any(str(REPO / "packages" / p) in sys.path for p in ("core", "store", "proxy"))
    print(f"  repo on sys.path: {on_path}")

    print("\n" + "=" * 66)
    if not missing:
        print("All good. Everything imports.")
        return 0

    print("FIX")
    print(f"""
  Install into THIS interpreter:

      {sys.executable} -m pip install -e packages/core -e packages/store -e packages/proxy

  or re-run setup pointed at it:

      PYTHON="{sys.executable}" bash scripts/dev-setup.sh

  Note: `pytest` works without any of this — pyproject.toml sets pythonpath.
""")
    if mismatch:
        print("  You have more than one python3. Install and run with the SAME one.")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
