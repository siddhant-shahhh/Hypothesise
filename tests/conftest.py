"""Every test gets its own data folder.

Code under test writes to HYPOTHESISE_HOME (~/.hypothesise by default): the
spool, settings, the database. Without this, a test run on a developer's
machine would mix fixture events into their real dashboard. Tests that need a
specific folder still set their own — monkeypatch.setenv in the test wins.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HYPOTHESISE_HOME", str(tmp_path_factory.mktemp("hyphome")))
    for var in ("HYPOTHESISE_MODE", "HYPOTHESISE_API_URL", "HYPOTHESISE_NOTIFY"):
        monkeypatch.delenv(var, raising=False)
