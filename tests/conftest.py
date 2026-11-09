"""Setup every test gets: UTC dates and a Claude Code config folder that is not the real one."""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Pin TZ=UTC and point CLAUDE_CONFIG_DIR at an empty temp folder; yields that folder.

    No real skill, transcript or setting on the machine running the tests can reach a test.
    """
    folder = tmp_path / "config"
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    yield folder
    monkeypatch.undo()
    time.tzset()
