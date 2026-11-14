"""The self-optimize scripts, loaded by file path, and a helper that lays fixtures out as a
transcript root."""

import importlib.util
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SKILLS = Path(__file__).resolve().parents[3] / "skills"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT = "-home-ada-demo"
MODIFIED_AFTER_EVERY_RECORD = 4102444800  # 2100-01-01
# Each script after the ones it imports; the transcript reader is its own skill.
LOAD_ORDER = {
    "pricing": SKILLS / "self-optimize" / "scripts" / "pricing.py",
    "transcripts": SKILLS / "transcripts" / "scripts" / "transcripts.py",
    "scan": SKILLS / "self-optimize" / "scripts" / "scan.py",
    "corrections": SKILLS / "self-optimize" / "scripts" / "corrections.py",
    "cut_session": SKILLS / "self-optimize" / "scripts" / "cut_session.py",
    "grader_check": SKILLS / "self-optimize" / "scripts" / "grader_check.py",
}

type Place = Callable[..., Path]


def load_scripts() -> dict[str, ModuleType]:
    """Each script as a module named self_optimize_<name>, with its sibling imports bound to
    these same copies.

    The scripts import each other by bare name (`import transcripts`), as they run from their
    folder. Bare names would collide with another skill's script of the same name, so each one is
    visible under its bare name only while the scripts load, and the previous entry is restored.
    """
    modules: dict[str, ModuleType] = {}
    previous = {name: sys.modules.get(name) for name in LOAD_ORDER}
    try:
        for name, path in LOAD_ORDER.items():
            spec = importlib.util.spec_from_file_location(f"self_optimize_{name}", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module  # dataclasses look their module up here
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules[name] = module
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return modules


@pytest.fixture(scope="session")
def scripts() -> dict[str, ModuleType]:
    return load_scripts()


@pytest.fixture(scope="session")
def scan(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["scan"]


@pytest.fixture(scope="session")
def corrections(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["corrections"]


@pytest.fixture(scope="session")
def cut_session(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["cut_session"]


@pytest.fixture(scope="session")
def grader_check(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["grader_check"]


@pytest.fixture(scope="session")
def pricing(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["pricing"]


@pytest.fixture
def place(tmp_path: Path) -> Place:
    """Copy fixtures into a transcript root: place({"<session id>": "<fixture file>"}) -> root.

    A key holding a slash places a subagent file: "<session id>/subagents/<agent>". Every file is
    modified after all of its records (and after every window the tests use), so its
    modification time says nothing about when its records were written.
    """

    def _place(sessions: dict[str, str], root: Path | None = None) -> Path:
        root = root or tmp_path / "projects"
        for session, fixture in sessions.items():
            destination = root / PROJECT / f"{session}.jsonl"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURES / fixture, destination)
            os.utime(destination, (MODIFIED_AFTER_EVERY_RECORD, MODIFIED_AFTER_EVERY_RECORD))
        return root

    return _place
