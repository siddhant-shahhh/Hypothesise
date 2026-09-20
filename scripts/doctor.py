#!/usr/bin/env python3
"""Environment check — run this when an import fails.

Reports which interpreter is running, whether it is a virtualenv, whether it
is externally managed (PEP 668), where each weaver package resolved from, and
the exact command to fix whatever is wrong.

    python3 scripts/doctor.py
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv"
PKGS = ["weaver_core", "weaver_store", "weaver_proxy", "weaver_api"]


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def externally_managed() -> Path | None:
    """PEP 668 marker — pip will refuse to install here."""
    stdlib = sysconfig.get_path("stdlib")
    if not stdlib:
        return None
    marker = Path(stdlib) / "EXTERNALLY-MANAGED"
    return marker if marker.exists() else None


def main() -> int:
    print("=" * 68)
    print("WEAVER DOCTOR")
    print("=" * 68)

    print("\n[interpreter]")
    print(f"  running      {sys.executable}")
    print(f"  version      {sys.version.split()[0]}")
    print(f"  virtualenv   {'yes' if in_venv() else 'NO — this is a base/system Python'}")

    em = externally_managed()
    if em:
        print("  PEP 668      EXTERNALLY MANAGED — pip will refuse to install here")

    which_py = shutil.which("python3")
    print(f"  PATH python3 {which_py or '<none>'}")
    if which_py and which_py != sys.executable:
        print(f"  !! this script is {sys.executable}, but your shell's python3 is {which_py}")

    which_pip = shutil.which("pip")
    if which_pip:
        try:
            out = subprocess.run([which_pip, "-V"], capture_output=True,
                                 text=True, timeout=15)
            print(f"  pip -V       {out.stdout.strip()}")
        except (OSError, subprocess.SubprocessError):
            pass

    print("\n[repo venv]")
    if (VENV / "bin" / "python").exists():
        print(f"  .venv exists {VENV}")
        # Compare PREFIXES, not executables. A venv's bin/python is a symlink
        # back to the base interpreter, so resolve() makes any Python look like
        # the venv's — the check would always pass.
        active = Path(sys.prefix).resolve() == VENV.resolve()
        print(f"  in use       {'yes' if active else 'NO — you are running a different Python'}")
    else:
        print("  .venv        not created yet")

    print("\n[packages]")
    missing = []
    for name in PKGS:
        try:
            mod = importlib.import_module(name)
            ver = getattr(mod, "__version__", "")
            print(f"  OK      {name:<14} {ver:<8} {getattr(mod, '__file__', '?')}")
        except ImportError:
            print(f"  MISSING {name}")
            missing.append(name)

    print("\n" + "=" * 68)
    if not missing:
        print("All good. Everything imports.")
        return 0

    print("FIX\n")
    venv_py = VENV / "bin" / "python"
    if venv_py.exists():
        print("  The packages are probably installed in .venv, but you are not using it:\n")
        print(f"      source {VENV}/bin/activate")
        print("      python -m weaver_api --demo\n")
        print("  Or run it directly without activating:\n")
        print(f"      {venv_py} -m weaver_api --demo\n")
    else:
        print("  Create the environment and install:\n")
        print("      bash scripts/dev-setup.sh\n")
        print(f"  Then:  source {VENV}/bin/activate\n")

    if em and not in_venv():
        print("  Note: this Python is externally managed (PEP 668), so pip will not")
        print("  install into it. Do NOT use --break-system-packages on a Homebrew")
        print("  Python — it can break your Homebrew install. Use the venv.\n")
    print("  Tests need none of this — pyproject.toml sets pythonpath, so")
    print("  `pytest` works from the repo with any interpreter.")
    print("=" * 68)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
