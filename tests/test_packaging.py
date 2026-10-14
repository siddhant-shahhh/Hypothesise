"""Packaging integrity.

`packages/cli/pyproject.toml` was missing for a full day. The script that
should have written it aborted partway on an unrelated error, and nothing
noticed: the test suite resolves imports through pytest's `pythonpath`, so
every test passed while the package was not installable at all.

These tests check the packaging itself rather than the imports, so a gap
between "the code works" and "the code ships" fails the build.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _members() -> list[Path]:
    """Every directory that is meant to be an installable package."""
    out = [d for d in (ROOT / "packages").iterdir()
           if d.is_dir() and d.name != "adapters"]
    adapters = ROOT / "packages" / "adapters"
    if adapters.is_dir():
        out += [d for d in adapters.iterdir() if d.is_dir()]
    return sorted(out)


def _root_cfg() -> str:
    return (ROOT / "pyproject.toml").read_text()


@pytest.mark.parametrize("member", _members(), ids=lambda p: p.name)
def test_every_member_has_a_pyproject(member: Path):
    """The exact bug: dev-setup does `pip install -e packages/cli` and pip fails
    with 'does not appear to be a Python project'."""
    assert (member / "pyproject.toml").is_file(), (
        f"{member.relative_to(ROOT)} has no pyproject.toml — "
        f"`pip install -e` on it will fail")


@pytest.mark.parametrize("member", _members(), ids=lambda p: p.name)
def test_every_member_declares_a_name_and_build_backend(member: Path):
    cfg = (member / "pyproject.toml").read_text()
    assert re.search(r'^name = ".+"', cfg, re.M), f"{member.name}: no name"
    assert "[build-system]" in cfg, f"{member.name}: no build-system"


def test_root_wheel_lists_packages_that_exist():
    """A path in the wheel target that does not exist produces a wheel silently
    missing that package."""
    block = re.search(
        r'\[tool\.hatch\.build\.targets\.wheel\]\npackages = \[(.*?)\]',
        _root_cfg(), re.S)
    assert block, "root pyproject has no wheel packages list"

    listed = re.findall(r'"(.+?)"', block.group(1))
    assert listed, "wheel packages list is empty"
    for rel in listed:
        path = ROOT / rel
        assert path.is_dir(), f"wheel lists {rel}, which does not exist"
        assert (path / "__init__.py").is_file(), f"{rel} has no __init__.py"


def test_every_importable_package_is_in_the_wheel():
    """A package that tests can import but the wheel omits works in the repo
    and breaks for a user — the hardest kind of bug to notice."""
    block = re.search(
        r'\[tool\.hatch\.build\.targets\.wheel\]\npackages = \[(.*?)\]',
        _root_cfg(), re.S).group(1)
    in_wheel = {Path(p).name for p in re.findall(r'"(.+?)"', block)}

    on_path = set()
    pp = re.search(r'pythonpath = \[(.*?)\]', _root_cfg(), re.S)
    for rel in re.findall(r'"(.+?)"', pp.group(1)):
        d = ROOT / rel
        if not d.is_dir():
            continue
        on_path |= {c.name for c in d.iterdir()
                    if c.is_dir() and (c / "__init__.py").is_file()}

    missing = on_path - in_wheel
    assert not missing, f"importable in tests but absent from the wheel: {sorted(missing)}"


def test_the_cli_entry_point_is_declared():
    cfg = _root_cfg()
    assert "[project.scripts]" in cfg
    assert re.search(r'^\w[\w-]* = "hypothesise\.__main__:main"', cfg, re.M)


def test_dev_setup_installs_every_member():
    """Adding a package without adding it to dev-setup means a clean install
    silently lacks it."""
    setup = (ROOT / "scripts" / "dev-setup.sh").read_text()
    for member in _members():
        rel = str(member.relative_to(ROOT))
        assert rel in setup, f"dev-setup.sh does not install {rel}"


def test_readme_installs_it_as_a_cli_tool():
    """ADR-034: the product is a command, so the README leads with an isolated
    tool install (uv tool / pipx) — plain pip into a system Python is refused
    under PEP 668 on Homebrew and most Linux distributions."""
    readme = (ROOT / "README.md").read_text()
    assert "uv tool install hypothesise" in readme
    assert "pipx install hypothesise" in readme
    assert "hypothesise init" in readme


def test_readme_warns_about_the_lookalike_names():
    readme = (ROOT / "README.md").read_text()
    assert "`hypothesis`" in readme and "`hypothesize`" in readme


def test_wheel_ships_the_demo_scenarios():
    """`hypothesise api --demo` reads scenarios beside the package; an install
    without them shows an empty demo and looks broken."""
    cfg = _root_cfg()
    assert '"scenarios/attack" = "hypothesise_api/scenarios/attack"' in cfg
    assert '"scenarios/benign" = "hypothesise_api/scenarios/benign"' in cfg


def test_standalone_doctor_diagnoses_a_shadowing_package(tmp_path):
    """A third-party package with the same name shadows ours, and the traceback
    comes from ITS imports — telling the reader nothing about this project.

    A stale or foreign `hypothesise` directory left in site-packages (ADR-031
    recorded this happening with the old `weaver` name) beats an editable
    install's path entry.

    Note this runs `scripts/doctor.py`, not `hypothesise doctor`: the CLI's doctor
    lives *inside* the hypothesise package, so when hypothesise is shadowed the CLI cannot
    load at all. The standalone script is the entry point that survives.
    """
    import os
    import subprocess
    import sys

    fake_site = tmp_path / "site-packages"
    (fake_site / "hypothesise").mkdir(parents=True)
    # A foreign package whose import fails — what the old `weaver` collision
    # looked like (a Fabric 1.x import that no longer resolves).
    (fake_site / "hypothesise" / "__init__.py").write_text("from fabric.api import *\n")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_site)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "doctor.py")],
                       capture_output=True, text=True, timeout=60, cwd=ROOT, env=env)

    out = r.stdout + r.stderr
    assert r.returncode != 0, "doctor must exit non-zero when something is wrong"
    assert "hypothesise" in out
    assert "MISSING" in out or "BROKEN" in out or "shadow" in out.lower(), out
    assert "FIX" in out or "rm -rf" in out or "dev-setup" in out, (
        f"doctor must print an actionable fix, got:\n{out}")


def test_standalone_doctor_exists_and_imports_nothing_at_module_level():
    """It has to run when the package it diagnoses is broken, so it must not
    import hypothesise to start up."""
    src = (ROOT / "scripts" / "doctor.py").read_text()
    head = src.split("def ")[0]
    assert "import hypothesise" not in head, (
        "scripts/doctor.py imports hypothesise at module level — it will die on the "
        "exact failure it exists to diagnose")


def test_dev_setup_checks_for_shadowing_before_installing():
    setup = (ROOT / "scripts" / "dev-setup.sh").read_text()
    assert "shadowing" in setup
    assert "site-packages" in setup


def test_readme_links_work_on_pypi():
    """PyPI renders the README with no repository around it, so a relative
    link like docs/PRIMER.md is a dead link on the project page."""
    readme = (ROOT / "README.md").read_text()
    links = re.findall(r"\]\(([^)]+)\)", readme)
    relative = [x for x in links if not x.startswith(("http://", "https://", "#", "mailto:"))]
    assert not relative, f"relative links break on PyPI: {relative}"
