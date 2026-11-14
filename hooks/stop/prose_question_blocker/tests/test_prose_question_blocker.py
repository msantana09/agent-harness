"""Tests for the prose_question_blocker Stop hook: what each check blocks, what it lets through,
and what the hook prints for a Stop payload."""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOK = Path(__file__).resolve().parents[1] / "main.py"
SESSION = "payload-test"


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prose_question_blocker_under_test", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_hook()


def stop_payload(message: object, stop_hook_active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": SESSION,
        "stop_hook_active": stop_hook_active,
        "last_assistant_message": message,
    }


def run_main(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hook.main()


def printed(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.strip())


def opt_out_project(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "prose-questions-ok").touch()
    return tmp_path


def set_stop_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, session_id: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    flag_dir = tmp_path / ".claude" / "state" / "stop"
    flag_dir.mkdir(parents=True)
    (flag_dir / session_id).write_text(json.dumps({"session_id": session_id}))


def test_a_question_at_the_end_of_a_response_is_blocked() -> None:
    assert hook.has_prose_question("Should I proceed with the migration?")


def test_a_question_mid_paragraph_is_blocked() -> None:
    assert hook.has_prose_question("First, is X true? Then we can move on.")


def test_text_without_a_question_is_allowed() -> None:
    assert not hook.has_prose_question("Done. Changelog updated.")


def test_a_question_mark_in_a_fenced_code_block_is_allowed() -> None:
    assert not hook.has_prose_question("```python\nx = foo()  # are we sure?\n```")


def test_a_question_mark_in_inline_code_is_allowed() -> None:
    assert not hook.has_prose_question("Run `psql -c 'select * from t where x?;'` to test.")


def test_the_bypass_marker_allows_a_question() -> None:
    assert not hook.has_prose_question("Quoted user back: did it work? <!-- ask-ok -->")


def test_a_question_mark_in_a_markdown_table_header_is_allowed() -> None:
    text = (
        "| Commit | Surface | Touches billing-service? |\n"
        "|--------|---------|---------------------------|\n"
        "| abc123 | UI | No |\n"
    )
    assert not hook.has_prose_question(text)


def test_an_indented_markdown_table_is_allowed() -> None:
    text = "  | col1 | col2? |\n  |------|-------|\n  | a    | b     |\n"
    assert not hook.has_prose_question(text)


def test_a_real_question_after_a_table_is_blocked() -> None:
    text = "| col1 | col2? |\n|------|-------|\n| a    | b     |\n\nShould I proceed?\n"
    assert hook.has_prose_question(text)


def test_a_pipe_in_prose_does_not_hide_a_question() -> None:
    assert hook.has_prose_question("The pipe character | is a separator. Should we change that?")


def test_say_the_word_is_a_handback_offer() -> None:
    assert hook.has_handback_offer("Done. Say the word and I'll ship it.")


def test_happy_to_do_x_if_is_a_handback_offer() -> None:
    assert hook.has_handback_offer("Happy to add more tests if you want deeper coverage.")


def test_ready_to_do_x_when_you_is_a_handback_offer() -> None:
    assert hook.has_handback_offer("Ready to merge this when you give the go-ahead.")


def test_i_can_also_do_x_if_is_a_handback_offer() -> None:
    assert hook.has_handback_offer("I can also update the docs if that helps.")


def test_let_me_know_if_youd_like_me_to_is_a_handback_offer() -> None:
    assert hook.has_handback_offer("Let me know if you'd like me to expand this.")


def test_if_you_want_i_can_is_a_handback_offer() -> None:
    assert hook.has_handback_offer("If you want, I can dig deeper into the logs.")


def test_a_status_report_without_an_offer_is_allowed() -> None:
    assert not hook.has_handback_offer("Done: fixed the bug. Tests pass.")


def test_a_handback_phrase_in_a_code_fence_is_allowed() -> None:
    assert not hook.has_handback_offer("```\nsay the word\n```")


def test_a_handback_phrase_on_a_next_line_of_the_report_template_is_allowed() -> None:
    text = (
        "Done: extended the hook.\n"
        "Flags: none.\n"
        "Next: happy to expand tests if you want more edge cases.\n"
    )
    assert not hook.has_handback_offer(text)


def test_a_handback_phrase_in_a_blockquote_is_allowed() -> None:
    text = 'Implemented the fix.\n\n> "Let me know if you\'d like me to help further."\n'
    assert not hook.has_handback_offer(text)


def test_an_offer_naming_a_human_gated_action_is_allowed() -> None:
    assert not hook.has_handback_offer("I can also run terraform apply if you want it deployed.")


def test_a_double_quoted_handback_phrase_is_allowed() -> None:
    text = 'Done: the hook now blocks phrases like "say the word" and "I can also X".'
    assert not hook.has_handback_offer(text)


def test_a_single_quoted_handback_phrase_is_allowed() -> None:
    text = "Done: added a check for 'happy to help if needed' phrasing."
    assert not hook.has_handback_offer(text)


def test_a_curly_quoted_handback_phrase_is_allowed() -> None:
    assert not hook.has_handback_offer("Done: the memo warns against “say the word” endings.")


def test_two_contractions_in_a_line_are_not_read_as_a_quoted_span() -> None:
    # The apostrophes of "can't" and "don't" must not swallow "say the word" between them.
    assert hook.has_handback_offer("I can't say the word here, but I don't want to worry you.")


def test_a_bulleted_next_line_of_the_report_template_is_allowed() -> None:
    text = "Done: extended the hook.\n- Next: happy to add tests if you want more.\n"
    assert not hook.has_handback_offer(text)


def test_a_bulleted_bold_flags_line_of_the_report_template_is_allowed() -> None:
    text = "Done: extended the hook.\n- **Flags:** say the word to apply the change.\n"
    assert not hook.has_handback_offer(text)


def test_a_numbered_next_line_of_the_report_template_is_allowed() -> None:
    text = "Done: extended the hook.\n1. Next: say the word if you want it merged.\n"
    assert not hook.has_handback_offer(text)


def test_i_can_also_without_an_if_clause_is_a_statement_of_fact() -> None:
    assert not hook.has_handback_offer("I can also confirm the uat run passed.")


def test_ready_to_go_when_you_are_is_a_sign_off_not_an_offer() -> None:
    assert not hook.has_handback_offer("PR is ready to go when you are.")


def test_the_bypass_marker_allows_a_handback_offer() -> None:
    assert not hook.has_handback_offer("Done. Say the word and I'll ship it. <!-- ask-ok -->")


def test_a_handback_phrase_in_an_unclosed_fence_is_allowed() -> None:
    assert not hook.has_handback_offer("Explanation.\n\n```\nsay the word\nno closing fence")


@pytest.mark.parametrize(
    "message",
    [
        "The fix is drafted. I'll change it once you say go.",
        "I'm holding here until you say how to proceed.",
        "Nothing else changes until you say so.",
        "Waiting for your go before making it.",
        "The spec update waits on your go.",
        "Say the word and I'll merge it.",
        "I'll make that change on your go.",
        "Waiting on your go.",
        "Run this, then tell me to go and I'll open the PR.",
        "Tell me to go ahead and I'll ship it as a small PR.",
        "The fix waits for your go.",
        "I'll start whenever you say go.",
        "I'll ship it once you give the go.",
        "I'll do it if you say so.",
    ],
)
def test_waiting_for_a_go_is_a_handback_offer(message: str) -> None:
    assert hook.has_handback_offer(message)


@pytest.mark.parametrize(
    "message",
    [
        "Stopped. Nothing runs until you say otherwise.",
        "I'll merge once you say go.",
        "The branch stays unmerged until you say go.",
        "The data migration waits on your go.",
        "I'll migrate the database once you say go.",
        "It's a dry run. Nothing is deleted until you say go.",
        "I'll drop the table once you say go.",
        "Nothing is migrated until you say go.",
        "The deletion waits on your go.",
        "The destruction waits on your go.",
        "Next: waiting for your go on the rollout.",
    ],
)
def test_waiting_for_a_go_on_a_gated_action_or_a_reported_stop_is_allowed(message: str) -> None:
    assert not hook.has_handback_offer(message)


def test_blockquote_lines_that_are_not_a_quotation_are_blocked() -> None:
    text = "Here are the options:\n\n> Option A costs more\n> Option B is slower\n"
    assert hook.has_blockquote_bar(text)


def test_a_bulleted_list_is_not_a_blockquote_bar() -> None:
    text = "Here are the options:\n\n- Option A costs more\n- Option B is slower\n"
    assert not hook.has_blockquote_bar(text)


def test_a_blockquote_marker_in_a_code_fence_is_allowed() -> None:
    text = "```\n> not a real blockquote, just example text\n```"
    assert not hook.has_blockquote_bar(text)


def test_a_blockquote_that_opens_with_a_quote_mark_is_allowed() -> None:
    assert not hook.has_blockquote_bar('> "This is what the user actually said."')


def test_a_multiline_quotation_is_allowed() -> None:
    assert not hook.has_blockquote_bar('> "Line one of the quote\n> continues on line two."')


def test_a_blockquote_formatted_like_a_quotation_but_without_a_quote_mark_is_blocked() -> None:
    assert hook.has_blockquote_bar("> This is not a quotation\n> just formatted like one.\n")


def test_a_greater_than_percentage_is_not_a_blockquote_bar() -> None:
    assert not hook.has_blockquote_bar("raise an alert when the disk is >90% used")


def test_a_greater_or_equal_comparison_at_line_start_is_not_a_blockquote_bar() -> None:
    assert not hook.has_blockquote_bar("only flag rows where the count is\n>= 5 rows")


def test_a_spaced_greater_than_comparison_at_line_start_is_not_a_blockquote_bar() -> None:
    assert not hook.has_blockquote_bar("drop the batch if\n> 5 rows fail validation")


def test_a_blockquote_marker_in_a_tilde_fence_is_allowed() -> None:
    text = "~~~\n> not a real blockquote, just example text\n~~~"
    assert not hook.has_blockquote_bar(text)


def test_a_blockquote_marker_in_an_unclosed_fence_is_allowed() -> None:
    text = "Explanation.\n\n```python\nx = 1\n> not markdown, just code\n"
    assert not hook.has_blockquote_bar(text)


def test_the_bypass_marker_allows_a_blockquote_bar() -> None:
    text = "> This is not a quotation\n> just formatted like one.\n<!-- ask-ok -->"
    assert not hook.has_blockquote_bar(text)


def test_the_status_template_trips_none_of_the_checks() -> None:
    text = (
        "Done: extended the hook.\n"
        "Found: no other instances.\n"
        "Flags: none.\n"
        "Next: run the hook test suite.\n"
    )
    assert not hook.has_handback_offer(text)
    assert not hook.has_blockquote_bar(text)
    assert not hook.has_prose_question(text)


def test_a_last_line_ending_in_a_colon_is_an_unlaunched_announcement() -> None:
    text = "Pushed the fix.\n\nRunning the same narrow review on just this commit:"
    assert hook.has_unlaunched_announcement(text)


def test_trailing_blank_lines_do_not_hide_an_announcement() -> None:
    assert hook.has_unlaunched_announcement("Running the suite now:\n\n  \n")


def test_a_colon_line_in_the_middle_of_the_message_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("Results:\n\nAll 12 tests pass.")


def test_a_last_line_without_a_colon_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("Done. Changelog updated.")


@pytest.mark.parametrize(
    "label_line",
    ["Done:", "Found:", "Flags:", "Next:", "- Next:", "- **Flags:**", "1. Done:", "**Found:**"],
)
def test_a_status_template_label_as_the_last_line_is_allowed(label_line: str) -> None:
    assert not hook.has_unlaunched_announcement(f"Pushed the fix.\n{label_line}")


def test_a_closing_code_fence_as_the_last_line_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("Verify with:\n```\nmake test-unit\n```")


def test_a_closing_tilde_fence_as_the_last_line_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("Verify with:\n~~~\nls\n~~~")


def test_a_colon_line_inside_an_unclosed_fence_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("Output:\n```\nhead of the log:")


def test_a_colon_line_after_a_closed_fence_is_still_an_announcement() -> None:
    assert hook.has_unlaunched_announcement("```\nls\n```\n\nRunning it now:")


def test_a_table_row_as_the_last_line_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("| Check | Result |\n|---|---|\n| lint: | ok: |")


def test_a_list_lead_in_followed_by_its_items_is_allowed() -> None:
    assert not hook.has_unlaunched_announcement("Three things changed:\n- the hook\n- its tests")


def test_the_bypass_marker_allows_an_announcement() -> None:
    assert not hook.has_unlaunched_announcement("Draft subject line: <!-- ask-ok -->")


def test_main_blocks_a_prose_question_in_the_payloads_last_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Done. Want me to push this now?"))

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "Prose question" in result["reason"]


def test_main_allows_a_clean_last_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Done: pushed. Next: nothing."))

    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert captured.err == ""


def test_main_allows_an_empty_last_message_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload(""))

    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert captured.err == ""


@pytest.mark.parametrize("value", [None, 7])
def test_main_allows_a_missing_last_message_and_says_so_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: object
) -> None:
    payload = stop_payload("")
    if value is None:
        del payload["last_assistant_message"]
    else:
        payload["last_assistant_message"] = value
    run_main(monkeypatch, payload)

    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "last-assistant-message-missing" in captured.err


def test_main_allows_a_payload_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    hook.main()

    assert printed(capsys) == {}


def test_main_blocks_a_handback_offer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Done. Say the word and I'll ship it."))

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "Hand-back offer" in result["reason"]


def test_main_blocks_an_unlaunched_announcement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Pushed.\n\nRunning the review now:"))

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "Unlaunched announcement" in result["reason"]


def test_main_blocks_a_blockquote_bar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Options:\n\n> A is faster\n> B is cheaper"))

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "blockquote" in result["reason"].lower()


@pytest.mark.parametrize(
    "message",
    [
        "Should I proceed?",
        "Done. Say the word and I'll ship it.",
        "Pushed.\n\nRunning the review now:",
    ],
)
def test_block_reasons_do_not_cite_a_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], message: str
) -> None:
    run_main(monkeypatch, stop_payload(message))

    assert "PR #" not in printed(capsys)["reason"]


def test_main_joins_the_reasons_of_every_failed_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Should I proceed?\n\nSay the word and I'll ship it."))

    reason = printed(capsys)["reason"]
    assert "Prose question" in reason
    assert "Hand-back offer" in reason


def test_the_redo_turn_skips_the_announcement_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Running the review now:", stop_hook_active=True))

    assert printed(capsys) == {}


def test_the_redo_turn_skips_the_handback_and_blockquote_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = "Say the word.\n\n> still formatted like a quote bar"
    run_main(monkeypatch, stop_payload(message, stop_hook_active=True))

    assert printed(capsys) == {}


def test_the_redo_turn_still_blocks_a_prose_question(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("Should I proceed?", stop_hook_active=True))

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "Prose question" in result["reason"]


def test_the_redo_turn_allows_wait_wording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("I'm holding here.", stop_hook_active=True))

    assert printed(capsys) == {}


@pytest.mark.parametrize(
    "message",
    [
        "Draft reply: how are you?",
        "The draft ends on this line:",
        "> A pull quote from the draft",
    ],
)
def test_an_opted_out_project_may_carry_drafted_copy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    message: str,
) -> None:
    payload = stop_payload(message) | {"cwd": str(opt_out_project(tmp_path))}
    run_main(monkeypatch, payload)

    assert printed(capsys) == {}


def test_an_opted_out_project_still_gets_its_handback_offers_blocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    payload = stop_payload("Draft is saved. Say the word and I'll send it.") | {
        "cwd": str(opt_out_project(tmp_path))
    }
    run_main(monkeypatch, payload)

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "Hand-back offer" in result["reason"]


DRAFTED_QUESTION = "Draft reply: how are you?"


@pytest.fixture
def folders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """An opted-out project holding the temp folder and the default config folder (`~/.claude`,
    with HOME at the project), so a call there reads as inside the project unless skipped, and
    another project beside it.

    tmp_path itself sits under a system temp folder, so the fixed temp roots are cleared and
    TMPDIR points at the project's temp folder instead.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    opt_out_project(docs)
    paths = {"docs": docs, "other": tmp_path / "other", "scratch": docs / "scratch"}
    paths["config"] = docs / ".claude"
    paths["other"].mkdir()
    paths["scratch"].mkdir()
    monkeypatch.setattr(hook, "SYSTEM_TEMP_DIRS", ())
    monkeypatch.setenv("TMPDIR", str(paths["scratch"]))
    monkeypatch.setenv("HOME", str(docs))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return paths


def tool_call(
    name: str, sidechain: bool = False, shell_folder: Path | None = None, **tool_input: str
) -> str:
    content = [{"type": "tool_use", "id": "call", "name": name, "input": tool_input}]
    record = {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"role": "assistant", "content": content},
    }
    if shell_folder is not None:
        record["cwd"] = str(shell_folder)
    return json.dumps(record)


def opted_out_run(monkeypatch: pytest.MonkeyPatch, folders: dict[str, Path], *lines: str) -> None:
    transcript = folders["scratch"] / "session.jsonl"
    transcript.write_text("\n".join(lines) + "\n")
    payload = stop_payload(DRAFTED_QUESTION) | {
        "cwd": str(folders["docs"]),
        "transcript_path": str(transcript),
    }
    run_main(monkeypatch, payload)


def test_the_opt_out_applies_when_the_transcript_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], folders: dict[str, Path]
) -> None:
    payload = stop_payload(DRAFTED_QUESTION) | {
        "cwd": str(folders["docs"]),
        "transcript_path": str(folders["scratch"] / "missing.jsonl"),
    }
    run_main(monkeypatch, payload)

    assert printed(capsys) == {}


def test_the_opt_out_applies_when_the_last_located_call_is_in_the_project(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], folders: dict[str, Path]
) -> None:
    opted_out_run(
        monkeypatch,
        folders,
        tool_call("Edit", file_path=str(folders["other"] / "main.py")),
        tool_call("Bash", command=f"cd {folders['docs']} && git status"),
        tool_call("Read", file_path=str(folders["other"] / "main.py")),
    )

    assert printed(capsys) == {}


@pytest.mark.parametrize(
    "call",
    [
        lambda other: tool_call("Edit", file_path=str(other / "main.py")),
        lambda other: tool_call("Bash", command=f"cd {other} && git status"),
        lambda other: tool_call("Bash", command=f"git -C '{other}' log --oneline -3"),
        lambda other: tool_call("Bash", command="(cd ../other && npm test)"),
    ],
    ids=["edit", "cd", "git-C", "relative-cd"],
)
def test_the_opt_out_does_not_apply_when_the_work_is_in_another_project(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    folders: dict[str, Path],
    call,
) -> None:
    opted_out_run(
        monkeypatch,
        folders,
        tool_call("Edit", file_path=str(folders["docs"] / "guide.md")),
        call(folders["other"]),
    )

    result = printed(capsys)
    assert result["decision"] == "block"
    assert "Prose question" in result["reason"]


def test_a_relative_path_resolves_against_the_folder_the_shell_was_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], folders: dict[str, Path]
) -> None:
    opted_out_run(
        monkeypatch,
        folders,
        tool_call("Bash", command=f"cd {folders['other']}"),
        tool_call("Bash", shell_folder=folders["other"], command="git -C src status"),
    )

    assert printed(capsys)["decision"] == "block"


@pytest.mark.parametrize(
    "last_line",
    [
        lambda paths: tool_call("Write", file_path=str(paths["scratch"] / "notes.md")),
        lambda paths: tool_call("Bash", command=f"cd {paths['config']} && ls"),
        lambda paths: tool_call("Edit", file_path="~/.claude/memory/notes.md"),
        lambda paths: '{"type": "assistant", "message": {"content": [{"type": "tool_use"',
        lambda paths: tool_call("Edit", sidechain=True, file_path=str(paths["docs"] / "a.md")),
    ],
    ids=["temp-write", "config-cd", "home-config-edit", "malformed-line", "sidechain-call"],
)
def test_calls_that_say_nothing_about_the_project_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    folders: dict[str, Path],
    last_line,
) -> None:
    opted_out_run(
        monkeypatch,
        folders,
        tool_call("Edit", file_path=str(folders["other"] / "main.py")),
        last_line(folders),
    )

    assert printed(capsys)["decision"] == "block"


def test_a_project_without_the_opt_out_file_is_not_opted_out(tmp_path: Path) -> None:
    assert not hook.project_opted_out(str(tmp_path))
    assert not hook.project_opted_out("")


def test_a_hold_after_a_bare_stop_is_not_a_handback_offer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    set_stop_flag(monkeypatch, tmp_path, SESSION)

    run_main(monkeypatch, stop_payload("Stopped. Nothing runs until you say go."))

    assert printed(capsys) == {}


def test_another_sessions_stop_flag_does_not_excuse_a_handback_offer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    set_stop_flag(monkeypatch, tmp_path, "another-session")

    run_main(monkeypatch, stop_payload("Nothing runs until you say go."))

    assert printed(capsys)["decision"] == "block"


def test_the_handback_reason_names_the_ask_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(monkeypatch, stop_payload("I'm holding here until you say how to proceed."))

    assert "AskUserQuestion" in printed(capsys)["reason"]


def run_script(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_the_script_exits_zero_and_allows_a_clean_reply(tmp_path: Path) -> None:
    result = run_script(tmp_path, stop_payload("Done: pushed. Next: nothing."))

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert result.stderr == ""


def test_the_script_exits_zero_and_blocks_a_prose_question(tmp_path: Path) -> None:
    result = run_script(tmp_path, stop_payload("Done. Want me to push this now?"))

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["decision"] == "block"
    assert "AskUserQuestion" in output["reason"]


def test_the_script_flags_a_payload_without_a_last_message_on_stderr(tmp_path: Path) -> None:
    payload = stop_payload("")
    del payload["last_assistant_message"]

    result = run_script(tmp_path, payload)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
    assert "last-assistant-message-missing" in result.stderr
