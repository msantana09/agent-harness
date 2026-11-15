"""Tests for the stop_flag UserPromptSubmit hook: when a prompt sets, keeps or clears the flag."""

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
OTHER_SESSION = "22222222-aaaa-4bbb-8ccc-000000000002"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stop_flag_main", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


def flag(config: Path, session: str = SESSION) -> Path:
    return config / "state" / "stop" / session


def submit(
    text: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    session: str = SESSION,
) -> dict | None:
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": text}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    load_hook().main()
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


@pytest.mark.parametrize(
    "text", ["stop", "Stop", "STOP!", "stop.", "  stop \n", "stop stop", "stop, stop!!", "STOP?"]
)
def test_a_bare_stop_sets_the_flag_and_tells_the_model_to_stop(text, config, monkeypatch, capsys):
    out = submit(text, monkeypatch, capsys)
    assert out is not None
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "make no tool calls" in out["hookSpecificOutput"]["additionalContext"].lower()
    assert json.loads(flag(config).read_text())["session_id"] == SESSION


@pytest.mark.parametrize(
    "text",
    ["stop the monitor", "don't stop", "please stop", "stopwatch", "stopstop", "stop it now", ""],
)
def test_a_prompt_that_only_contains_stop_sets_nothing(text, config, monkeypatch, capsys):
    assert submit(text, monkeypatch, capsys) is None
    assert not flag(config).exists()


def test_the_next_human_prompt_clears_the_flag(config, monkeypatch, capsys):
    submit("stop", monkeypatch, capsys)
    assert flag(config).exists()
    assert submit("ok, go ahead and run the tests", monkeypatch, capsys) is None
    assert not flag(config).exists()


@pytest.mark.parametrize(
    "text",
    [
        "<task-notification>\n<task-id>task1</task-id>\n<output-file>",
        '<agent-message from="agent1">\n[Subagent hand-back] The text below is the final report.',
        '<cross-session-message from="uds:/tmp/sockets/1.sock" from-name="worker" '
        'from-mode="prompting">\nhi\n</cross-session-message>',
        "The resume plugin sent a message:\nYour last turn was cut off by a dropped connection.",
        "Your last turn was cut off by a dropped connection. Continue the work.",
        "Your last turn stopped at the usage limit, which has now reset. Continue the work.",
        "You can continue now. Continue the task you were working on when the limit was reached.",
        "Your claude.ai usage limit has reset. Continue the task.",
        "[Cross-session idle notice] No idle signal from the recipient.",
        "[Artifact comment sent to Claude] thread=1",
        "  \n<task-notification>",
    ],
)
def test_a_machine_submitted_prompt_does_not_clear_the_flag(text, config, monkeypatch, capsys):
    submit("stop", monkeypatch, capsys)
    assert submit(text, monkeypatch, capsys) is None
    assert flag(config).exists()


def test_flags_are_per_session(config, monkeypatch, capsys):
    submit("stop", monkeypatch, capsys, session=SESSION)
    submit("carry on", monkeypatch, capsys, session=OTHER_SESSION)
    assert flag(config, SESSION).exists()
    assert not flag(config, OTHER_SESSION).exists()


def test_a_session_id_that_is_not_a_filename_sets_nothing(config, monkeypatch, capsys):
    assert submit("stop", monkeypatch, capsys, session="../../escape") is None
    assert not (config / "state").exists()


def test_a_payload_without_a_text_prompt_is_ignored(config, monkeypatch, capsys):
    submit("stop", monkeypatch, capsys)
    payload = {"session_id": SESSION, "prompt": ["stop"]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    load_hook().main()
    assert capsys.readouterr().out == ""
    assert flag(config).exists()


def test_the_script_run_as_a_process_sets_the_flag_on_stop_and_clears_it_on_the_next_prompt(
    tmp_path,
):
    def launch(text: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt": text}
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=stdin if stdin is not None else json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path)},
        )

    stopped = launch("stop")
    assert stopped.returncode == 0, stopped.stderr
    context = json.loads(stopped.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "The user typed stop" in context
    assert flag(tmp_path).is_file()

    resumed = launch("continue with the tests")
    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout.strip() == ""
    assert not flag(tmp_path).exists()

    broken = launch("", stdin="not json")
    assert broken.returncode == 0
    assert broken.stdout.strip() == ""
    assert "stop_flag:" in broken.stderr
