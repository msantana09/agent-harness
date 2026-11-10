"""
Tests for the bare_git_stash_guard Bash hook.

A bare stash clobbers unrelated working-tree changes, and a bare pop or apply
reapplies stash@{0}, which may be another session's. Only a pathspec scopes a stash
and only an explicit ref scopes a pop or apply: a flag (`-q`, `-u`, `-m msg`), a
redirection, a pipe or a global option (`git -C dir`) changes neither.
"""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "main.py"
_spec = importlib.util.spec_from_file_location("bare_git_stash_guard_main", HOOK_PATH)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

# The shell readers' assert_linear fixture, loaded from its one definition.
_READERS_CONFTEST = HOOK_PATH.parents[2] / "lib_shared" / "tests" / "conftest.py"
_readers_spec = importlib.util.spec_from_file_location("shell_readers_conftest", _READERS_CONFTEST)
assert _readers_spec is not None and _readers_spec.loader is not None
_readers_conftest = importlib.util.module_from_spec(_readers_spec)
_readers_spec.loader.exec_module(_readers_conftest)
assert_linear = _readers_conftest.assert_linear


def blocks(command: str) -> bool:
    return guard.check_bare_git_stash(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        # a flag after `stash` is no pathspec
        "git stash -q",
        "git stash -q 2>/dev/null",
        "cd apps && git stash -q 2>/dev/null; echo;",
        "git stash -q && git pull",
        "git stash --quiet",
        "git stash -u",
        "git stash --include-untracked",
        "git stash -k -a",
        "git stash -m wip",
        "git stash -qm 'wip'",
        "git stash --message=wip",
        "git stash push",
        "git stash push -q",
        "git stash push -m wip",
        "git stash save -q",
        "git stash pop -q",
        "git stash pop --index",
        "git stash apply -q",
        # a flag that picks by kind of change is no pathspec: --staged takes the whole
        # index (another session's staged paths too), -p takes the hunks answered on stdin
        "git stash push --staged",
        "git stash -S",
        "git stash -p",
        "git stash push --patch",
        # git's own options before the subcommand
        "git -C worktree stash",
        "git -C worktree stash pop -q",
        "git -c user.name=x stash -q",
        "git --no-pager stash",
        # a redirection, a pipe, a background job or the next line ends the command
        "git stash 2>&1",
        "git stash -q | cat",
        "git stash &",
        "git stash\ngit status",
        # a command an ssh destination or a nested shell runs
        "ssh host 'git stash -q'",
        "ssh host git -C repo stash -q",
        "bash -c 'git stash -q'",
        # a message the hook blanks keeps its word: the next one is no pathspec
        'git stash push -m x -m "$(date)"',
        "git stash push -m x -m 'y'",
        "git stash push -mx -m 'y'",
        "X=1 git stash push --message=x -m 'y'",
    ],
)
def test_bare_stash_blocks(command):
    assert blocks(command)


@pytest.mark.parametrize(
    "command",
    [
        "git stash push -- a.py",
        "git stash push -q -- a.py b.py",
        "git stash push a.py -q",
        "git stash push -m wip -- a.py",
        "git stash push -um wip -- a.py",
        "git stash push --pathspec-from-file=paths.txt",
        "git stash push --staged -- a.py",
        "git stash -q -- a.py",
        "git stash pop stash@{0}",
        "git stash pop -q stash@{1}",
        "git stash apply --index stash@{0}",
        "git -C worktree stash push -q -- a.py",
        "git stash list",
        "git stash show -p",
        "git stash drop stash@{0}",
        "git stash branch topic",
        "git stash --help",
        "git stash push -h",
        "git status",
        # data: a quoted word, a commit message
        "grep -rn 'git stash -q' docs/",
        "echo 'git stash -q'",
        "git commit -m 'fix: never git stash -q'",
        "ssh host 'git stash push -- a.py'",
        "git stash list; echo it's",
        # a word of another command, a comment, a help request
        "echo git stash",
        "git status # git stash",
        "git stash save --help",
        # a line with an unclosed quote: the shell refuses it, so nothing runs
        "git stash; echo it's",
        # a message the hook blanks keeps its word: the word after it is the pathspec
        "git stash push -m x topic",
        "X=1 git stash push -m x topic",
        "eval 'git stash push -m x topic'",
        "git stash push -mx topic",
        "X=1 git stash push --message x topic",
    ],
)
def test_scoped_or_unrelated_stash_passes(command):
    assert not blocks(command)


def test_quoted_data_passes_where_the_same_text_run_blocks():
    assert not blocks('grep -rn "git stash" docs/')
    assert blocks("git stash")
    assert blocks("bash -c 'git stash'")


def test_the_check_runs_in_linear_time(assert_linear):
    unit = "git -C d stash push -q -m wip -u -- a b; "
    assert_linear(guard, guard.check_bare_git_stash, lambda size: unit * size, 1000)


def run_main(monkeypatch, capsys, stdin: str) -> str:
    """The hook's stdout for raw stdin text, run in-process."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    guard.main()
    return capsys.readouterr().out


def test_the_hook_blocks_a_stash_with_a_flag():
    hook_input = {"tool_name": "Bash", "tool_input": {"command": "git stash -q 2>/dev/null"}}
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(hook_input),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert payload["permissionDecision"] == "deny"
    assert "bare `git stash`" in payload["permissionDecisionReason"]


def test_a_line_continuation_is_joined_before_the_check(monkeypatch, capsys):
    hook_input = {"tool_name": "Bash", "tool_input": {"command": "git \\\nstash -q"}}
    out = run_main(monkeypatch, capsys, json.dumps(hook_input))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        "[]",
        json.dumps({"tool_name": "Edit", "tool_input": {"command": "git stash"}}),
        json.dumps({"tool_name": "Bash", "tool_input": {}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}}),
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git stash push -- a.py"}}),
    ],
)
def test_malformed_input_other_tools_and_scoped_stashes_print_nothing(monkeypatch, capsys, stdin):
    assert run_main(monkeypatch, capsys, stdin) == ""


def test_a_crash_in_the_check_allows_the_command(monkeypatch, capsys):
    def crash(command: str) -> str | None:
        raise RecursionError("too deep")

    monkeypatch.setattr(guard, "check_bare_git_stash", crash)
    hook_input = {"tool_name": "Bash", "tool_input": {"command": "git stash"}}
    assert run_main(monkeypatch, capsys, json.dumps(hook_input)) == ""
