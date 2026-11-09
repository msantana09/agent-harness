"""Tests for read.py's context commands (around, since-reply, lead-up) and the typed-message
search, on the invented transcripts in fixtures/ beside this file."""

import json
from collections.abc import Callable
from pathlib import Path

MAIN = "aaaa1111"
CORRECTION = "No, tests go in tests/, not src/."

type Place = Callable[..., Path]
type Run = Callable[..., tuple[int, str, str]]


def texts(out: str) -> list[str]:
    """The body lines of the plain text output, without the headers and file lines."""
    return [
        line.strip()
        for line in out.splitlines()
        if line.startswith("  ") and not line.startswith("   file ")
    ]


def test_around_prints_the_turns_before_and_after_a_quoted_message(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    code, out, _ = run("around", "tests go in   tests/", "--before", "1", "--after", "1")
    assert code == 0
    assert texts(out) == [
        "I put the test in src/parser_test.py.",
        "[1 tool call: Write; --tools shows them]",
        CORRECTION,
        "Base directory for this skill: /skills/testing",
        "",
        "Put tests beside the code.",
        "Moving it to tests/parser_test.py.",
    ]
    assert "uuid=u-2  <- this message" in out


def test_around_with_tools_prints_each_call_and_its_result(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    _, out, _ = run("around", "u-2", "--before", "1", "--after", "0", "--tools")
    assert "Write  id=c-3" in out
    assert "  result:\n    File created." in out


def test_since_reply_prints_the_calls_and_results_after_the_agents_last_reply(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    code, out, _ = run("since-reply", CORRECTION)
    assert code == 0
    assert texts(out) == [
        "I put the test in src/parser_test.py.",
        "file_path: src/parser_test.py",
        "content:",
        "def test_parse():",
        "assert parse('1') == 1",
        "",
        "result:",
        "File created.",
        CORRECTION,
    ]


def test_lead_up_prints_the_session_from_its_start_to_the_message(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    code, out, _ = run("lead-up", CORRECTION)
    assert code == 0
    assert texts(out) == [
        "Add a test for the parser.",
        "I will read the parser first.",
        "[2 tool calls: Bash, Read; --tools shows them]",
        "I put the test in src/parser_test.py.",
        "[1 tool call: Write; --tools shows them]",
        CORRECTION,
    ]


def test_a_quote_found_in_two_sessions_prints_the_context_of_each(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    place("bbbb2222", "retry.jsonl")
    code, out, _ = run("lead-up", CORRECTION)
    assert code == 0
    assert "== match 1 of 2: session aaaa1111" in out
    assert "== match 2 of 2: session bbbb2222" in out


def test_a_context_command_marks_the_message_in_its_json_rows(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    _, out, _ = run("lead-up", "u-2", "--json")
    found = [json.loads(line) for line in out.splitlines()]
    assert [r["uuid"] for r in found if r["anchor"]] == ["u-2"]
    assert found[0]["text"] == "Add a test for the parser."


def test_a_message_nothing_matches_exits_1(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    code, out, err = run("around", "words nobody wrote")
    assert (code, out) == (1, "")
    assert "no message in the window matches" in err


def test_a_quote_names_a_turn_not_skill_text_that_repeats_it(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    assert run("around", "Put tests beside the code")[0] == 1
    code, out, _ = run("around", "u-3", "--before", "0", "--after", "0")
    assert code == 0
    assert "user (meta)  uuid=u-3  cwd=/home/ada/demo  <- this message" in out


def test_a_typed_search_skips_the_named_sessions(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    place("bbbb2222", "retry.jsonl")
    place("cccc3333", "other.jsonl")
    code, out, _ = run("messages", "--typed", "--search", "test*", "--skip-session", "bbbb")
    assert code == 0
    assert "session bbbb2222" not in out
    assert texts(out) == ["Add a test for the parser.", CORRECTION]
