"""Tests for cut_session.py."""

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SESSION = "cccc3333-0000-0000-0000-000000000000"
SCRIPT = Path(__file__).resolve().parents[3] / "skills/self-optimize/scripts/cut_session.py"

type Place = Callable[..., Path]


def history(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_history_ends_before_the_message_and_prints_it_whole(
    cut_session: ModuleType, place: Place, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = place({SESSION: "cut_session.jsonl"})
    out = tmp_path / "case" / "history.jsonl"

    code = cut_session.main(
        ["--session", SESSION, "--before", "Why did you skip the", "--root", str(root)]
        + ["--out", str(out)]
    )

    assert code == 0
    assert [r.get("uuid") for r in history(out)] == ["u1", "r1", "d1", "m1"]
    assert capsys.readouterr().out == "Why did you skip\n  the tests?\n"


def test_standing_context_is_dropped_and_its_children_reattached(
    cut_session: ModuleType, place: Place, tmp_path: Path
) -> None:
    root = place({SESSION: "cut_session.jsonl"})
    out = tmp_path / "history.jsonl"

    cut_session.main(
        ["--session", SESSION, "--before", "Why did", "--root", str(root), "--out", str(out)]
    )

    parents = {r["uuid"]: r["parentUuid"] for r in history(out)}
    assert parents == {"u1": None, "r1": "u1", "d1": "r1", "m1": "d1"}
    assert "Old rule text" not in out.read_text()


def test_a_message_the_user_did_not_type_is_never_a_cut_point(
    cut_session: ModuleType, place: Place, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = place({SESSION: "cut_session.jsonl"})

    code = cut_session.main(
        ["--session", SESSION, "--before", "Fix the login page later", "--root", str(root)]
        + ["--out", str(tmp_path / "history.jsonl")]
    )

    assert code == 2
    assert "no typed message starts" in capsys.readouterr().err
    assert not (tmp_path / "history.jsonl").exists()


def test_a_session_with_no_transcript_is_refused(
    cut_session: ModuleType, place: Place, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = place({SESSION: "cut_session.jsonl"})

    code = cut_session.main(
        ["--session", "dddd4444-0000-0000-0000-000000000000", "--before", "Fix"]
        + ["--root", str(root), "--out", str(tmp_path / "history.jsonl")]
    )

    assert code == 2
    assert "no transcript for session" in capsys.readouterr().err


def test_a_start_shared_by_two_messages_is_refused_with_both_listed(place: Place) -> None:
    root = place({SESSION: "cut_session.jsonl"})

    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--session", SESSION, "--before", "Fix the login"]
        + ["--root", str(root), "--out", "unused.jsonl"],
        capture_output=True,
        text=True,
    )

    assert run.returncode == 2
    assert "Fix the login page" in run.stderr
    assert "Fix the login redirect too" in run.stderr
