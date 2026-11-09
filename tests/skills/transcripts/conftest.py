"""The transcripts scripts, loaded by file path under names of their own."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "skills" / "transcripts" / "scripts"
# Each script after the ones it imports.
LOAD_ORDER = ("transcripts",)


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
