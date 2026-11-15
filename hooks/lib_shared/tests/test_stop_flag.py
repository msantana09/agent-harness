"""Tests for the per-session stop flag helpers."""

import json
import sys
from pathlib import Path

import pytest

hooks_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(hooks_root))

from lib_shared.stop_flag import DENY_REASON, flag_is_set, flag_path  # noqa: E402

SESSION = "11111111-aaaa-4bbb-8ccc-000000000001"


def test_flag_path_is_one_file_per_session_under_the_config_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    assert flag_path(SESSION) == tmp_path / "config" / "state" / "stop" / SESSION


@pytest.mark.parametrize("configured", [None, ""])
def test_flag_path_falls_back_to_the_home_folder_without_a_config_folder(
    monkeypatch, tmp_path, configured
):
    monkeypatch.setenv("HOME", str(tmp_path))
    if configured is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", configured)
    assert flag_path(SESSION) == tmp_path / ".claude" / "state" / "stop" / SESSION


@pytest.mark.parametrize("session_id", [None, 7, "", "../escape", "a/b", "a b", "x" * 129])
def test_flag_path_rejects_ids_that_cannot_be_a_filename(session_id):
    assert flag_path(session_id) is None


def test_flag_is_set_only_for_the_session_it_names(tmp_path):
    flag = tmp_path / "flag"
    flag.write_text(json.dumps({"session_id": SESSION, "at": 1.0}))
    assert flag_is_set(flag, SESSION)
    assert not flag_is_set(flag, "another-session")


@pytest.mark.parametrize("content", ["", "not json", "[]", '"text"', '{"at": 1.0}'])
def test_flag_is_set_fails_open_on_unreadable_content(tmp_path, content):
    flag = tmp_path / "flag"
    flag.write_text(content)
    assert not flag_is_set(flag, SESSION)


def test_flag_is_set_fails_open_when_the_file_is_missing(tmp_path):
    assert not flag_is_set(tmp_path / "missing", SESSION)


def test_deny_reason_tells_the_agent_to_stop_calling_tools():
    assert "Make no tool calls" in DENY_REASON
