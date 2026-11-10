"""Tests for the edit_trailing_space_guard Edit|MultiEdit hook."""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "main.py"
MARKER = "new_string ends in a space or tab"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("edit_trailing_space_guard_main", HOOK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook_raw(
    stdin_text: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict | None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    with pytest.raises(SystemExit) as exit_info:
        load_hook().main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def run_hook(
    tool_name: str,
    tool_input: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> dict | None:
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    return run_hook_raw(payload, monkeypatch, capsys)


def edit(old: str, new: str, **extra: Any) -> dict:
    return {"file_path": "/tmp/x.py", "old_string": old, "new_string": new, **extra}


def assert_denied(out: dict | None) -> str:
    assert out is not None
    hook_output = out["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny"
    assert MARKER in hook_output["permissionDecisionReason"]
    return hook_output["permissionDecisionReason"]


@pytest.mark.parametrize(
    "new_string",
    ["Invalid label identifier: ", "x = 1\t", "    ", "foo\n  ", "a b  "],
)
def test_denies_new_string_ending_in_space_or_tab(new_string, monkeypatch, capsys):
    assert_denied(run_hook("Edit", edit("old", new_string), monkeypatch, capsys))


def test_denies_a_replace_all_that_rewrites_a_prefix_ending_in_a_space(monkeypatch, capsys):
    reason = assert_denied(
        run_hook(
            "Edit",
            edit('"Invalid user identifier: ', '"Invalid account identifier: ', replace_all=True),
            monkeypatch,
            capsys,
        )
    )
    assert "non-whitespace token" in reason


@pytest.mark.parametrize(
    "new_string",
    ["", "identifier", "identifier:", "line one\n", "line one  \n", "line one\n\n", "\n"],
)
def test_allows_new_string_not_ending_in_space_or_tab(new_string, monkeypatch, capsys):
    assert run_hook("Edit", edit("old", new_string), monkeypatch, capsys) is None


def test_old_string_trailing_space_is_not_checked(monkeypatch, capsys):
    assert run_hook("Edit", edit("identifier: ", "identifier:"), monkeypatch, capsys) is None


def test_multiedit_denies_the_offending_edit_by_position(monkeypatch, capsys):
    reason = assert_denied(
        run_hook(
            "MultiEdit",
            {
                "file_path": "/tmp/x.py",
                "edits": [
                    {"old_string": "a", "new_string": "b"},
                    {"old_string": "c", "new_string": "d "},
                ],
            },
            monkeypatch,
            capsys,
        )
    )
    assert "edit 2 of 2" in reason


@pytest.mark.parametrize("name", ["notes.md", "page.mdx", "README.MD", "docs/a.b.Mdx"])
def test_allows_a_trailing_space_in_a_markdown_file_which_the_tool_keeps(name, monkeypatch, capsys):
    hard_break = {**edit("line one", "line one  "), "file_path": f"/tmp/{name}"}
    assert run_hook("Edit", hard_break, monkeypatch, capsys) is None
    several = {"file_path": f"/tmp/{name}", "edits": [{"old_string": "c", "new_string": "d "}]}
    assert run_hook("MultiEdit", several, monkeypatch, capsys) is None


@pytest.mark.parametrize("name", ["notes.md.txt", "md", "page.mdx.py", "notes.markdown", 7])
def test_denies_a_trailing_space_in_a_file_that_only_resembles_markdown(name, monkeypatch, capsys):
    path = f"/tmp/{name}" if isinstance(name, str) else name
    assert_denied(run_hook("Edit", {**edit("a", "b "), "file_path": path}, monkeypatch, capsys))


def test_multiedit_allows_clean_edits(monkeypatch, capsys):
    out = run_hook(
        "MultiEdit",
        {
            "file_path": "/tmp/x.py",
            "edits": [
                {"old_string": "a ", "new_string": "b"},
                {"old_string": "c", "new_string": "d\n"},
            ],
        },
        monkeypatch,
        capsys,
    )
    assert out is None


@pytest.mark.parametrize("tool_name", ["Write", "Bash", "Read", "NotebookEdit"])
def test_ignores_other_tools(tool_name, monkeypatch, capsys):
    tool_input = {"new_string": "ends in space ", "content": "x "}
    assert run_hook(tool_name, tool_input, monkeypatch, capsys) is None


def test_malformed_json_fails_open(monkeypatch, capsys):
    assert run_hook_raw("not json", monkeypatch, capsys) is None


@pytest.mark.parametrize(
    "tool_input", [None, "text", [], {"edits": "nope"}, {"edits": [None, 3]}, {"new_string": 7}]
)
def test_unexpected_input_shapes_fail_open(tool_input, monkeypatch, capsys):
    assert run_hook("MultiEdit", tool_input, monkeypatch, capsys) is None
    assert run_hook("Edit", tool_input, monkeypatch, capsys) is None


def test_the_script_run_as_a_process_denies_a_trailing_space_and_allows_a_clean_edit():
    def launch(new_string: str) -> subprocess.CompletedProcess[str]:
        payload = {"tool_name": "Edit", "tool_input": edit("old", new_string)}
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )

    blocked = launch("identifier: ")
    assert blocked.returncode == 0, blocked.stderr
    assert_denied(json.loads(blocked.stdout))

    allowed = launch("identifier:")
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == ""
