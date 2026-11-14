"""Tests for grader_check.py. A fake `claude` on PATH grades each reply by the words in it.

In the fixture round the eval's grader puts the edited text at 5 against the current text's 3.
"""

import json
import os
import shutil
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

RESULTS = Path(__file__).resolve().parent / "fixtures" / "eval_results"
# Fails a reply that holds any word listed in fail.json, passes the rest, and keeps what it was
# given. The calls run in parallel, so each one writes its own stdin file.
FAKE_CLAUDE = """#!{python}
import json, os, pathlib, re, sys
state = pathlib.Path({state!r})
text = sys.stdin.read()
(state / f"stdin-{{os.getpid()}}.txt").write_text(text)
if (state / "broken").exists():
    sys.exit(1)
data = json.loads(re.search(r"=== BEGIN DATA \\w+ ===\\n(.*)\\n=== END DATA", text, re.S)[1])
fail = json.loads((state / "fail.json").read_text())
verdict = "FAIL" if any(word in data["reply"] for word in fail) else "PASS"
reply = {{"is_error": False, "total_cost_usd": 0.001,
          "structured_output": {{"verdict": verdict, "reason": "checked the file name"}}}}
sys.stdout.write(json.dumps(reply))
"""


def install_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: list[str], broken: bool = False
) -> Path:
    state = tmp_path / "fake-claude"
    state.mkdir()
    (state / "fail.json").write_text(json.dumps(fail))
    if broken:
        (state / "broken").touch()
    script = state / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, state=str(state)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{state}{os.pathsep}{os.environ['PATH']}")
    return state


def suite(tmp_path: Path, name: str, fixture: str) -> Path:
    folder = tmp_path / name
    results = folder / "results" / "2000-01-01T00-00-00-000Z"
    results.mkdir(parents=True)
    shutil.copy(RESULTS / fixture, results / "aggregate-result.json")
    return folder


def run(
    grader_check: ModuleType, capsys: pytest.CaptureFixture[str], *args: str
) -> tuple[int, str, str]:
    code = grader_check.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_verdict_stands_when_the_second_grader_agrees(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_claude(tmp_path, monkeypatch, fail=["bad"])
    code, out, _ = run(grader_check, capsys, str(suite(tmp_path, "evals", "round.json")))
    assert code == 0
    assert "## edited vs current: edited higher" in out
    assert "Cases, eval grader: 1 won, 1 tied, 0 lost" in out


def test_a_verdict_the_second_grader_does_not_share_is_too_close_to_call(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_claude(tmp_path, monkeypatch, fail=["bad", "borderline"])
    code, out, _ = run(grader_check, capsys, str(suite(tmp_path, "evals", "round.json")))
    assert code == 0
    assert "## edited vs current: too close to call" in out
    assert "sonnet: 3 of 6 vs 3 of 6, cannot separate" in out
    assert "tone-edited run 1: eval PASS, sonnet FAIL" in out


def test_an_edit_in_a_second_suite_folder_is_compared_with_the_current_cases(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_claude(tmp_path, monkeypatch, fail=["bad"])
    first = suite(tmp_path, "evals", "round.json")
    retry = suite(tmp_path, "evals-retry", "retry.json")
    code, out, _ = run(grader_check, capsys, str(first), str(retry))
    assert code == 0
    assert "## edited2 vs current: edited higher" in out
    assert "Eval grader: 3 of 6 vs 6 of 6, edited higher" in out


def test_more_runs_with_a_lower_pass_rate_score_lower(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_claude(tmp_path, monkeypatch, fail=["bad"])
    first = suite(tmp_path, "evals", "round.json")
    retry = suite(tmp_path, "evals-retry", "retry.json")
    results = next(retry.glob("results/*/aggregate-result.json"))
    data = json.loads(results.read_text())
    for case in data["cases"]:
        good = case["arms"]["with"][0]
        grader = {**good["graders"][0], "passed": False, "evidence": "bad: nothing named"}
        bad = {**good, "score": 0, "passed": False, "graders": [grader]}
        case["arms"]["with"] = [good] * 4 + [bad] * 6
    results.write_text(json.dumps(data))
    code, out, _ = run(grader_check, capsys, str(first), str(retry))
    assert code == 0
    assert "## edited2 vs current: edited lower" in out
    assert "Eval grader: 3 of 6 vs 8 of 20, edited lower" in out


def test_the_second_grader_sees_only_the_criteria_and_the_reply(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = install_claude(tmp_path, monkeypatch, fail=["bad"])
    run(grader_check, capsys, str(suite(tmp_path, "evals", "round.json")))
    for given in state.glob("stdin-*.txt"):
        text = given.read_text()
        assert "tone" not in text and "scope" not in text
        assert "current" not in text and "edited" not in text
        assert "judge votes" not in text


def test_a_run_the_eval_did_not_grade_is_not_sent_to_the_second_grader(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = install_claude(tmp_path, monkeypatch, fail=["bad"])
    code, out, _ = run(grader_check, capsys, str(suite(tmp_path, "evals", "round.json")))
    assert code == 0
    assert len(list(state.glob("stdin-*.txt"))) == 11
    assert "1 runs the eval did not grade" in out


def test_a_results_file_of_another_schema_version_exits_2(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_claude(tmp_path, monkeypatch, fail=[])
    folder = suite(tmp_path, "evals", "round.json")
    results = next(folder.glob("results/*/aggregate-result.json"))
    data = json.loads(results.read_text())
    data["schemaVersion"] = 2
    results.write_text(json.dumps(data))
    code, out, err = run(grader_check, capsys, str(folder))
    assert code == 2 and out == ""
    assert "schemaVersion 2" in err


def test_a_failed_model_call_exits_1_with_no_verdict(
    grader_check: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_claude(tmp_path, monkeypatch, fail=[], broken=True)
    code, out, err = run(grader_check, capsys, str(suite(tmp_path, "evals", "round.json")))
    assert code == 1 and out == ""
    assert "a model call failed" in err
