"""The assert_linear fixture: a linear-time check in CPU time for the shell readers' tests."""

import sys
import time
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2]


def _hook_modules(module):
    """*module* and every loaded module under hooks/, where the shell readers keep their
    caches."""
    loaded = [
        other
        for other in list(sys.modules.values())
        if getattr(other, "__file__", None)
        and Path(other.__file__).resolve().is_relative_to(HOOKS_DIR)
    ]
    return [module, *loaded]


@pytest.fixture
def assert_linear():
    """Return assert_linear(module, run, build, size) for a linear-time test.

    run's CPU time on build(size) grows no faster than linearly from
    build(size // 4): the ratio stays under 8 (4 for linear growth, 16 for
    quadratic) with 5 ms of headroom. Only the ratio is asserted: CPU time
    drifts far less than a wall clock when other processes load the machine,
    but it still grows, so an absolute bound fails a linear run. Each size gets
    three timed runs and the best one counts, with the per-text caches of *module*
    and of every other loaded module under hooks/ (the tokenizer and the heredoc
    reader) emptied before every run. The large size stops at its first run under
    the bound, since that run alone settles the best of three.
    """

    def seconds(modules, run, text):
        for owner in modules:
            for value in vars(owner).values():
                if callable(getattr(value, "cache_clear", None)):
                    value.cache_clear()
        start = time.process_time()
        run(text)
        return time.process_time() - start

    def check(module, run, build, size):
        modules = _hook_modules(module)
        small_text, large_text = build(size // 4), build(size)
        small = min(seconds(modules, run, small_text) for _ in range(3))
        bound = 8 * small + 0.005
        large = float("inf")
        for _ in range(3):
            large = min(large, seconds(modules, run, large_text))
            if large <= bound:
                return
        assert large <= bound, (small, large)

    return check
