"""Tests for the stop_gate PreToolUse hook: how it reads the flag the stop_flag hook leaves."""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).resolve().parents[1] / "main.py"
SESSION = "11111111-aaaa-4bbb-8ccc-000000000001"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stop_gate_main", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def flag_file(home: Path, session: str = SESSION) -> Path:
    path = home / ".claude" / "state" / "stop" / session
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def set_flag(home: Path, session: str = SESSION) -> None:
    flag_file(home, session).write_text(json.dumps({"session_id": session, "at": 1.0}))


def call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tool: str = "Read",
    session: str = SESSION,
) -> dict | None:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": {},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    load_hook().main()
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def assert_denied(out: dict | None) -> None:
    assert out is not None
    hook_output = out["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny"
    assert "The user typed stop" in hook_output["permissionDecisionReason"]


def test_allows_when_no_flag_exists(home, monkeypatch, capsys):
    assert call(monkeypatch, capsys) is None


def test_denies_while_the_session_flag_is_readable(home, monkeypatch, capsys):
    set_flag(home)
    assert_denied(call(monkeypatch, capsys))


def test_a_flag_stops_only_its_own_session(home, monkeypatch, capsys):
    set_flag(home)
    assert call(monkeypatch, capsys, session="22222222-aaaa-4bbb-8ccc-000000000002") is None


@pytest.mark.parametrize("tool", ["SubagentHandback", "StructuredOutput"])
def test_a_stopped_agent_can_still_return_its_result(tool, home, monkeypatch, capsys):
    set_flag(home)
    assert call(monkeypatch, capsys, tool=tool) is None


@pytest.mark.parametrize(
    "content", ["", "not json", "[]", '{"session_id": "someone-else"}', "\xff"]
)
def test_malformed_flag_allows(content, home, monkeypatch, capsys):
    flag_file(home).write_text(content, encoding="latin-1")
    assert call(monkeypatch, capsys) is None


def test_unreadable_flag_allows(home, monkeypatch, capsys):
    path = flag_file(home)
    path.write_text(json.dumps({"session_id": SESSION}))
    path.chmod(0)
    try:
        assert call(monkeypatch, capsys) is None
    finally:
        path.chmod(0o600)


def test_flag_that_is_a_directory_allows(home, monkeypatch, capsys):
    flag_file(home).mkdir()
    assert call(monkeypatch, capsys) is None


def test_a_session_id_that_is_not_a_filename_allows(home, monkeypatch, capsys):
    set_flag(home)
    assert call(monkeypatch, capsys, session="../../escape") is None


def test_the_script_run_as_a_process_denies_a_stopped_session_and_allows_otherwise(tmp_path):
    def launch(stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "HOME": str(tmp_path)},
        )

    payload = json.dumps({"session_id": SESSION, "tool_name": "Read", "tool_input": {}})

    allowed = launch(payload)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == ""

    set_flag(tmp_path)
    blocked = launch(payload)
    assert blocked.returncode == 0, blocked.stderr
    assert_denied(json.loads(blocked.stdout))

    for unusable in ("", "not json", "[]", '{"session_id": 7}'):
        result = launch(unusable or " ")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""
