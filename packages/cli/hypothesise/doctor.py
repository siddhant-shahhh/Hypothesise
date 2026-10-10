#!/usr/bin/env python3
"""Environment check — run this when anything about the install looks wrong.

    python3 scripts/doctor.py

**This is the standalone copy, and it is the one that survives.** A second copy
ships inside the `hypothesise` package as `hypothesise doctor`, which is more convenient
but cannot help when `hypothesise` itself is the thing that is broken — the CLI has
to import the package to run. So nothing here imports hypothesise at module level.

It reports which interpreter is running, whether it is a virtualenv, whether it
is externally managed (PEP 668), where each package resolved from, whether
anything is shadowing this repo, and the exact command to fix what it finds.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

# Installed, so there may be no repo at all.
_parents = Path(__file__).resolve().parents
REPO = _parents[3] if len(_parents) > 3 else Path.cwd()
VENV = REPO / ".venv"

# `hypothesise` first: the top-level package is the one a stale or foreign
# install is most likely to shadow (ADR-031).
PKGS = ["hypothesise", "hypothesise_core", "hypothesise_store", "hypothesise_proxy", "hypothesise_api"]


def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def externally_managed() -> Path | None:
    """PEP 668 marker — pip will refuse to install here."""
    stdlib = sysconfig.get_path("stdlib")
    if not stdlib:
        return None
    marker = Path(stdlib) / "EXTERNALLY-MANAGED"
    return marker if marker.exists() else None


def venv_home(venv: Path) -> str | None:
    """The base interpreter a venv was built against, from pyvenv.cfg."""
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    try:
        lines = cfg.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == "home":
            return value.strip()
    return None


def venv_version(venv: Path) -> str | None:
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    try:
        lines = cfg.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            parts = value.strip().split(".")
            if len(parts) >= 2:
                return f"{parts[0]}.{parts[1]}"
    return None


def foreign_venv(venv: Path) -> str | None:
    """Why this venv is not usable from here, or None if it looks fine.

    A repo on a shared or external drive can carry a venv created by a
    different machine. Its bin/ entries are that machine's binaries and its
    pyvenv.cfg names that machine's interpreter, so nothing in it runs here —
    but the directory is present and looks ordinary. ADR-032.
    """
    home = venv_home(venv)
    if home and not Path(home).exists():
        return f"built against {home}, which is not on this machine"

    ver = venv_version(venv)
    if home and ver and not (Path(home) / f"python{ver}").exists():
        return f"built against Python {ver} in {home}, which is not there"

    if not (venv / "bin" / "hypothesise").exists():
        return "has no `hypothesise` in bin/ — nothing was installed into it here"

    return None


def site_dir_of(path: str) -> Path | None:
    p = Path(path)
    for parent in [p, *p.parents]:
        if parent.name == "site-packages":
            return parent
    return None


def main() -> int:
    print("=" * 70)
    print("HYPOTHESISE DOCTOR")
    print("=" * 70)
    print("\n  This copy ships inside the hypothesise package, so it cannot run when")
    print("  hypothesise itself is shadowed or broken. Use scripts/doctor.py for that.")

    print("\n[interpreter]")
    print(f"  running      {sys.executable}")
    print(f"  version      {sys.version.split()[0]}")
    print(f"  virtualenv   {'yes' if in_venv() else 'NO — base/system Python'}")
    if externally_managed():
        print("  PEP 668      EXTERNALLY MANAGED — pip will refuse to install here")

    which_py = shutil.which("python3")
    print(f"  PATH python3 {which_py or '<none>'}")
    if which_py and which_py != sys.executable:
        print(f"  !! this script is {sys.executable}")
        print(f"     but your shell's python3 is {which_py}")

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
        print(f"  .venv        {VENV}")
        # Compare PREFIXES: a venv's bin/python symlinks back to the base
        # interpreter, so resolve() would make any Python look like the venv's.
        active = Path(sys.prefix).resolve() == VENV.resolve()
        print(f"  in use       {'yes' if active else 'NO — running a different Python'}")
        why = foreign_venv(VENV)
        if why:
            print(f"  UNUSABLE     this .venv {why}")
            print("               Ignore the directory; do not point anything at")
            print("               .venv/bin/*. Use the interpreter below. ADR-032.")
        elif not active:
            print("               (fine — it exists and works, you are just not")
            print("               using it right now)")
    else:
        print("  .venv        not created yet")

    print("\n[for MCP client configs]")
    print("  A client launches its server with no PATH and no activated venv,")
    print("  so the config must name an absolute interpreter and use -m:")
    print(f'      "command": "{sys.executable}",')
    print('      "args": ["-m", "hypothesise", "proxy", "--config", "...", "--detect"]')
    print("  Write or check one without editing by hand:")
    print(f"      {sys.executable} scripts/ops/check-client-config.py <config> --fix")

    print("\n[packages]")
    missing: list[str] = []
    shadowed: list[tuple[str, str]] = []

    for name in PKGS:
        try:
            mod = importlib.import_module(name)
            where = getattr(mod, "__file__", "") or "?"
            ver = getattr(mod, "__version__", "")
            outside = str(REPO) not in where
            print(f"  {'WARN' if outside else 'OK  '}    {name:<14} {ver:<8} {where}")
            if outside:
                shadowed.append((name, where))
        except ImportError as exc:
            # "Installed but broken" and "not installed" need different fixes,
            # and a leftover third-party package looks like the former.
            spec = None
            # A broken package can raise anything at all from find_spec.
            with contextlib.suppress(Exception):
                spec = importlib.util.find_spec(name)
            if spec is not None and spec.origin:
                print(f"  BROKEN  {name:<14} found at {spec.origin}")
                print(f"          but failed to import: {type(exc).__name__}: {exc}")
                shadowed.append((name, spec.origin))
            else:
                print(f"  MISSING {name}")
            missing.append(name)

    outside_repo = [(n, w) for n, w in shadowed if str(REPO) not in w]

    if outside_repo:
        print("\n[shadowing]")
        print("  A package of the same name is installed elsewhere and takes")
        print("  precedence over this repo:")
        for name, where in outside_repo:
            print(f"    {name:<14} {where}")

        site = site_dir_of(outside_repo[0][1])
        print("\n  This is a stale or foreign copy of `hypothesise`. `pip uninstall`")
        print("  can remove its metadata but leave the directory, and a real")
        print("  directory in site-packages beats an editable install's path entry.")
        print("\n  FIX")
        if site:
            print(f"      rm -rf {site / 'hypothesise'} {site}/hypothesise-*.dist-info")
        print("      bash scripts/dev-setup.sh")

    print("\n" + "=" * 70)

    if not missing and not outside_repo:
        print("All good. Everything imports from this repo.")
        return 0

    if missing and not outside_repo:
        print("FIX\n")
        # Only recommend .venv if it is actually usable from this machine —
        # otherwise this line sends the reader straight back into ADR-032.
        if (VENV / "bin" / "python").exists() and not foreign_venv(VENV):
            print("  Packages are probably in .venv but you are not using it:\n")
            print(f"      source {VENV}/bin/activate\n")
            print("  Or run directly:\n")
            print(f"      {VENV / 'bin' / 'python'} -m hypothesise api --demo\n")
        else:
            print("  Install into the environment you are using right now:\n")
            print("      bash scripts/dev-setup.sh --here\n")
            print("  Or activate the one you want first, then drop --here.\n")
        if externally_managed() and not in_venv():
            print("  This Python is externally managed (PEP 668), so pip will not")
            print("  install into it. Do NOT use --break-system-packages on a")
            print("  Homebrew Python. Use the venv.\n")

    print("  Tests need none of this — pyproject.toml sets pythonpath, so")
    print("  `pytest` works from the repo with any interpreter.")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
