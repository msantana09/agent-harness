"""assert_linear's verdict, on a clock each test scripts: run takes what the test says it takes."""

import sys
import time

import pytest


def scripted_run(monkeypatch, costs):
    """A run that spends the next of *costs* seconds on each call, and the texts it was given."""
    now = [0.0]
    seen = []
    monkeypatch.setattr(time, "process_time", lambda: now[0])
    remaining = iter(costs)

    def run(text: str) -> None:
        seen.append(text)
        now[0] += next(remaining)

    return run, seen


def test_a_linear_run_passes_after_one_large_run(assert_linear, monkeypatch):
    run, seen = scripted_run(monkeypatch, [1.0, 1.0, 1.0, 4.0])
    assert_linear(sys.modules[__name__], run, lambda size: "x" * size, 40)
    assert [len(text) for text in seen] == [10, 10, 10, 40]


def test_a_quadratic_run_fails_after_three_large_runs(assert_linear, monkeypatch):
    run, seen = scripted_run(monkeypatch, [1.0, 1.0, 1.0, 16.0, 16.0, 16.0])
    with pytest.raises(AssertionError):
        assert_linear(sys.modules[__name__], run, lambda size: "x" * size, 40)
    assert len(seen) == 6


def test_a_noisy_large_run_gets_its_retries(assert_linear, monkeypatch):
    run, seen = scripted_run(monkeypatch, [1.0, 1.0, 1.0, 30.0, 4.0])
    assert_linear(sys.modules[__name__], run, lambda size: "x" * size, 40)
    assert len(seen) == 5


def test_the_best_small_run_sets_the_bound(assert_linear, monkeypatch):
    run, _ = scripted_run(monkeypatch, [9.0, 1.0, 9.0, 16.0, 16.0, 16.0])
    with pytest.raises(AssertionError):
        assert_linear(sys.modules[__name__], run, lambda size: "x" * size, 40)
