"""The transcripts scripts, loaded by file path under names of their own, and helpers that lay
fixtures out as a transcript root and run read.py on it."""

import importlib.util
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "transcripts" / "scripts"
# Each script after the ones it imports.
LOAD_ORDER = ("transcripts", "index", "read")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROJECT = "-home-ada-demo"
MODIFIED_AFTER_EVERY_RECORD = 4102444800  # 2100-01-01

type Place = Callable[..., Path]
type Run = Callable[..., tuple[int, str, str]]


def load_scripts() -> dict[str, ModuleType]:
    """Each script as a module named transcripts_<name>, with its sibling imports bound to these
    same copies. Bare names are visible only while the scripts load, as in the self-optimize
    tests, and the previous entries are restored."""
    modules: dict[str, ModuleType] = {}
    previous = {name: sys.modules.get(name) for name in LOAD_ORDER}
    try:
        for name in LOAD_ORDER:
            spec = importlib.util.spec_from_file_location(
                f"transcripts_{name}", SCRIPTS / f"{name}.py"
            )
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
def transcripts(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["transcripts"]


@pytest.fixture(scope="session")
def index(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["index"]


@pytest.fixture(scope="session")
def read(scripts: dict[str, ModuleType]) -> ModuleType:
    return scripts["read"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def place(root: Path) -> Place:
    """Copy a fixture into the transcript root: place("<session>", "<fixture>") -> its path.

    A session holding a slash places a subagent or workflow file:
    "<session>/subagents/<agent>". Every file is modified after all of its records, so its
    modification time never keeps it out of a window the tests use.
    """

    def _place(session: str, fixture: str, project: str = PROJECT) -> Path:
        destination = root / project / f"{session}.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, destination)
        os.utime(destination, (MODIFIED_AFTER_EVERY_RECORD, MODIFIED_AFTER_EVERY_RECORD))
        return destination

    return _place


@pytest.fixture
def run(read: ModuleType, root: Path, capsys: pytest.CaptureFixture[str]) -> Run:
    """run(<command>, <args>...) -> (exit code, stdout, stderr) of read.py over the root, with
    a window that holds every fixture record."""

    def _run(command: str, *args: str) -> tuple[int, str, str]:
        window = [] if command == "index" or "--days" in args else ["--since", "2099-01-01"]
        code = read.main([command, "--root", str(root), *window, *args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run
