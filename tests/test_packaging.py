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
    assert re.search(r'^\w[\w-]* = "weaver\.__main__:main"', cfg, re.M)


def test_dev_setup_installs_every_member():
    """Adding a package without adding it to dev-setup means a clean install
    silently lacks it."""
    setup = (ROOT / "scripts" / "dev-setup.sh").read_text()
    for member in _members():
        rel = str(member.relative_to(ROOT))
        assert rel in setup, f"dev-setup.sh does not install {rel}"


def test_no_document_tells_anyone_to_pip_install_a_taken_name():
    """ADR-030. `weaver` belongs to someone else on PyPI, so that command
    installs a stranger's package. Nothing is published yet either."""
    for doc in ("docs/OPERATIONS.md", "docs/PRIMER.md", "README.md"):
        path = ROOT / doc
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "pip install weaver" not in line:
                continue
            lowered = line.lower()
            warned = any(w in lowered for w in
                         ("do not", "don't", "unrelated", "stranger", "taken"))
            assert warned, f"{doc}:{i} instructs `pip install weaver`: {line.strip()}"
