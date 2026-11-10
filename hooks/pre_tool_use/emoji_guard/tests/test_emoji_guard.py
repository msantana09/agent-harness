"""Tests for the emoji_guard Edit|MultiEdit|Write|NotebookEdit hook.

Every non-ASCII character is written as an escape so this file is itself emoji-free.
"""

import importlib.util
import io
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).resolve().parent.parent / "main.py"
MARKER = "prohibited"

FIRE = "\U0001f525"
BULB = "\U0001f4a1"
THUMBS_UP = "\U0001f44d"
CHECK_BOX = "\u2705"
ROBOT = "\U0001f916"
HEART = "\u2764"
VARIATION_SELECTOR = "\ufe0f"
FLAG_US = "\U0001f1fa\U0001f1f8"

spec = importlib.util.spec_from_file_location("emoji_guard_main", HOOK_PATH)
emoji_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emoji_guard)


@pytest.fixture
def run_stdin(monkeypatch, capsys) -> Callable[[str], dict | None]:
    """Run the hook in-process on raw stdin text: its JSON output, or None when it prints
    nothing. Every path must exit 0."""

    def run(stdin: str) -> dict | None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        with pytest.raises(SystemExit) as exit_info:
            emoji_guard.main()
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        return json.loads(out) if out.strip() else None

    return run


@pytest.fixture
def run_hook(run_stdin) -> Callable[[str, object], dict | None]:
    def run(tool_name: str, tool_input: object) -> dict | None:
        return run_stdin(json.dumps({"tool_name": tool_name, "tool_input": tool_input}))

    return run


@pytest.fixture
def write(run_hook) -> Callable[[str, str], dict | None]:
    def run(path: str, content: str) -> dict | None:
        return run_hook("Write", {"file_path": path, "content": content})

    return run


def decision_of(out: dict | None) -> tuple[str, str]:
    assert out is not None
    hook_output = out["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert MARKER in hook_output["permissionDecisionReason"]
    return hook_output["permissionDecision"], hook_output["permissionDecisionReason"]


def listed_in(reason: str) -> str:
    """The parenthesised emoji list of a deny or ask message."""
    match = re.search(r"file \((.*?)\)\.", reason)
    assert match
    return match.group(1)


@pytest.mark.parametrize(
    ("emoji", "label"),
    [
        (FIRE, "U+1F525"),
        (BULB, "U+1F4A1"),
        (THUMBS_UP, "U+1F44D"),
        (CHECK_BOX, "U+2705"),
        (ROBOT, "U+1F916"),
        (FLAG_US, "U+1F1FA, U+1F1F8"),
        (HEART + VARIATION_SELECTOR, "U+2764 U+FE0F"),
        ("1" + VARIATION_SELECTOR + "\u20e3", "U+0031 U+FE0F"),
    ],
)
def test_an_emoji_in_a_doc_is_denied_and_named_by_code_point(write, emoji, label):
    decision, reason = decision_of(write("/scratch/notes.md", f"Status {emoji} done"))
    assert decision == "deny"
    assert label in reason


@pytest.mark.parametrize(
    "name", ["a.md", "A.MD", "pr-body.md", "commit-msg.txt", "x.rst", "page.mdx"]
)
def test_every_doc_extension_is_denied(write, name):
    assert decision_of(write(f"/scratch/{name}", ROBOT))[0] == "deny"


@pytest.mark.parametrize(
    "name", ["a.py", "a.ts", "run.sh", "cfg.json", "page.html", "Makefile", "x.unknown"]
)
def test_code_and_unknown_files_ask(write, name):
    assert decision_of(write(f"/repo/{name}", f"# {FIRE}"))[0] == "ask"


@pytest.mark.parametrize(
    "text",
    [
        "\u4e2d\u6587",
        "\ud55c\uae00",
        "\u2500\u2502\u250c\u2514",
        "\u2190\u2191\u2192\u2193",
        "\u2713 \u2717",
        "\u25cb",
        "\uff08\uff09\u3010\u3011",
        "\u2714 \u26a0 \u00a9 \u2122",
        "plain ascii",
        "",
    ],
)
def test_text_symbols_and_plain_text_are_allowed(write, text):
    assert write("/scratch/notes.md", text) is None
    assert write("/repo/a.py", text) is None


def test_a_variation_selector_after_a_text_symbol_is_what_makes_it_emoji(write):
    assert write("/scratch/notes.md", "\u2714") is None
    assert decision_of(write("/scratch/notes.md", "\u2714" + VARIATION_SELECTOR))[0] == "deny"


def test_at_most_five_distinct_emoji_are_listed(write):
    emoji = [chr(code) for code in range(0x1F600, 0x1F608)]
    _, reason = decision_of(write("/scratch/notes.md", FIRE + FIRE + "".join(emoji)))
    assert listed_in(reason) == "U+1F525, U+1F600, U+1F601, U+1F602, U+1F603, ..."


def cpu_seconds(run: Callable[[str], object], text: str) -> float:
    start = time.process_time()
    run(text)
    return time.process_time() - start


@pytest.mark.parametrize(
    "construct",
    [
        "plain text line\n",
        FIRE,
        "x" + VARIATION_SELECTOR,
        "\u4e2d\u6587 \u2500\u2713 ",
        VARIATION_SELECTOR * 3,
    ],
)
def test_large_texts_are_scanned_in_linear_time(construct):
    """CPU time on 4x the text stays under 8x the small run's (4 for linear, 16 for
    quadratic) plus 5 ms of headroom; each size takes the best of three runs."""

    def run(text: str) -> object:
        return emoji_guard.emoji_found([text])

    size = 20000
    small_text, large_text = construct * (size // 4), construct * size
    small = min(cpu_seconds(run, small_text) for _ in range(3))
    large = min(cpu_seconds(run, large_text) for _ in range(3))
    assert large <= 8 * small + 0.005, (small, large)


def test_distinct_emoji_in_a_big_write_are_listed_once_and_capped(write):
    emoji = "".join(chr(code) + VARIATION_SELECTOR for code in range(0x4E00, 0x4E00 + 3000))
    _, reason = decision_of(write("/scratch/notes.md", emoji))
    assert listed_in(reason).count("U+FE0F") == 5
    assert listed_in(reason).endswith(", ...")


def test_edit_checks_the_new_string_only(run_hook):
    added = {"file_path": "/scratch/n.md", "old_string": "a", "new_string": FIRE}
    removed = {"file_path": "/scratch/n.md", "old_string": FIRE, "new_string": "done"}
    assert decision_of(run_hook("Edit", added))[0] == "deny"
    assert run_hook("Edit", removed) is None


def test_multiedit_checks_every_edit(run_hook):
    edits = [
        {"old_string": "a", "new_string": "b"},
        {"old_string": "c", "new_string": f"d {FIRE}"},
    ]
    doc = run_hook("MultiEdit", {"file_path": "/scratch/n.md", "edits": edits})
    code = run_hook("MultiEdit", {"file_path": "/repo/a.py", "edits": edits})
    assert decision_of(doc)[0] == "deny"
    assert decision_of(code)[0] == "ask"


def test_a_notebook_markdown_cell_is_a_doc_and_a_code_cell_asks(run_hook):
    cell = {"notebook_path": "/repo/n.ipynb", "new_source": f"title {FIRE}"}
    assert decision_of(run_hook("NotebookEdit", {**cell, "cell_type": "markdown"}))[0] == "deny"
    assert decision_of(run_hook("NotebookEdit", {**cell, "cell_type": "code"}))[0] == "ask"
    clean = {"notebook_path": "/repo/n.ipynb", "new_source": "title"}
    assert run_hook("NotebookEdit", clean) is None


@pytest.mark.parametrize("tool_name", ["Bash", "Read", "Grep"])
def test_other_tools_are_ignored(run_hook, tool_name):
    assert run_hook(tool_name, {"command": FIRE, "content": FIRE, "new_string": FIRE}) is None


def test_malformed_json_fails_open(run_stdin):
    assert run_stdin("not json") is None


@pytest.mark.parametrize(
    "tool_input",
    [
        None,
        "text",
        [],
        {"edits": "nope"},
        {"edits": [None, 3]},
        {"new_string": 7, "content": None},
        {"file_path": 3},
    ],
)
def test_unexpected_input_shapes_fail_open(run_hook, tool_input):
    for tool_name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        assert run_hook(tool_name, tool_input) is None


@pytest.mark.parametrize("payload", ["[]", "3", "null", '"text"'])
def test_non_object_payloads_fail_open(run_stdin, payload):
    assert run_stdin(payload) is None


def run_as_launched(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_launched_as_a_command_it_denies_an_emoji_doc_and_allows_a_clean_one():
    blocked = run_as_launched(
        {"tool_name": "Write", "tool_input": {"file_path": "/scratch/n.md", "content": FIRE}}
    )
    assert blocked.returncode == 0, blocked.stderr
    assert decision_of(json.loads(blocked.stdout))[0] == "deny"

    allowed = run_as_launched(
        {"tool_name": "Write", "tool_input": {"file_path": "/scratch/n.md", "content": "done"}}
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == ""
