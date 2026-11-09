"""Setup every test gets: UTC dates, and a Claude Code config folder and cache folder that are not
the real ones."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Pin TZ=UTC and point CLAUDE_CONFIG_DIR and XDG_CACHE_HOME at empty temp folders; yields the
    config folder.

    No real skill, transcript, setting or index on the machine running the tests can reach a test.
    """
    folder = tmp_path / "config"
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    yield folder
    monkeypatch.undo()
    time.tzset()
