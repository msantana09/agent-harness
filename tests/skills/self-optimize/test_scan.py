"""Tests for scan.py and the transcript rules it reports on, on the invented transcripts in
fixtures/ beside this file."""

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

BASIC = "aaaa1111-0000-0000-0000-000000000000"
OTHER = "bbbb2222-0000-0000-0000-000000000000"

type Place = Callable[..., Path]
type Result = tuple[int, dict[str, Any], str, str]


def run_scan(
    scan: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path, *args: str
) -> Result:
    """Run scan.main; return (exit code, the --json numbers, stdout, stderr)."""
    out = tmp_path / "scan.json"
    code = scan.main([*args, "--json", str(out)])
    captured = capsys.readouterr()
    numbers = json.loads(out.read_text()) if out.exists() else {}
    return code, numbers, captured.out, captured.err


def test_a_touched_old_file_is_not_a_session(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl", OTHER: "old.jsonl"})  # both files were modified just now
    code, numbers, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root), "--days", "30")
    assert code == 0
    assert numbers["activity"]["sessions"] == 1
    assert "1 session with activity" in out


def test_the_window_starts_at_the_since_date(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl", OTHER: "injected.jsonl"})  # 2099-06-01 and 2099-06-02
    _, numbers, _, _ = run_scan(
        scan, capsys, tmp_path, "--root", str(root), "--since", "2099-06-02"
    )
    assert numbers["activity"]["sessions"] == 1
    assert numbers["activity"]["first_day"] == "2099-06-02"


def test_the_window_ends_with_the_until_date(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl", OTHER: "injected.jsonl"})  # 2099-06-01 and 2099-06-02
    _, numbers, _, _ = run_scan(
        scan,
        capsys,
        tmp_path,
        "--root",
        str(root),
        "--since",
        "2099-05-01",
        "--until",
        "2099-06-01",
    )
    assert numbers["activity"]["sessions"] == 1
    assert numbers["activity"]["last_day"] == "2099-06-01"
    assert numbers["window"]["label"] == "2099-05-01 through 2099-06-01"


def test_until_before_since_is_refused(
    scan: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        scan.main(["--since", "2099-06-02", "--until", "2099-06-01"])
    assert stopped.value.code == 2
    assert "--until 2099-06-01 is before --since 2099-06-02" in capsys.readouterr().err


FORK_NAMES = {
    "source sorts first": ("aaaa1111-source", "bbbb2222-fork"),
    "fork sorts first": ("bbbb2222-source", "aaaa1111-fork"),
}


def place_fork(place: Place, names: tuple[str, str], marked: bool) -> Path:
    """The source and its fork, which starts at the same instant; `marked` keeps forkedFrom."""
    source, fork = names
    root = place({source: "fork_source.jsonl", fork: "fork_copy.jsonl"})
    if not marked:
        path = root / "-home-ada-demo" / f"{fork}.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        unmarked = [{k: v for k, v in r.items() if k != "forkedFrom"} for r in records]
        path.write_text("".join(json.dumps(r) + "\n" for r in unmarked))
    return root


@pytest.mark.parametrize("order", FORK_NAMES)
def test_a_marked_fork_leaves_the_repeated_messages_with_its_source(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    order: str,
) -> None:
    root = place_fork(place, FORK_NAMES[order], marked=True)
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 3  # two in the source, one only in the fork
    assert numbers["activity"]["sessions"] == 2


@pytest.mark.parametrize("order", FORK_NAMES)
def test_an_unmarked_copy_counts_each_message_once_under_the_file_read_first(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    order: str,
) -> None:
    root = place_fork(place, FORK_NAMES[order], marked=False)
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 3
    # Both files start at the same instant, so the path decides which one is read first. When
    # the fork is, it takes the shared records and the source has nothing left of its own.
    assert numbers["activity"]["sessions"] == (2 if order == "source sorts first" else 1)


def test_turns_the_harness_injected_are_not_typed_messages(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "injected.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    # Only the words typed beside a side agent's note, and the plain message.
    assert numbers["activity"]["typed_messages"] == 2


def test_a_resend_queued_behind_an_api_error_is_not_typed(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "after_api_error.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 2  # the first request and the later correction


def test_a_resend_is_not_typed_when_the_message_it_repeats_is_before_the_window(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "resend_across_midnight.jsonl"})  # sent 23:59:58, resent 00:00:01
    _, numbers, _, _ = run_scan(
        scan, capsys, tmp_path, "--root", str(root), "--since", "2099-06-03"
    )
    assert numbers["activity"]["typed_messages"] == 1  # only the later correction


def test_a_new_message_right_after_an_api_error_is_typed(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "new_after_api_error.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 2


def test_words_typed_into_a_tool_result_count_but_picking_an_option_does_not(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "answers.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 2  # a free-text answer and rejection feedback


def test_a_note_on_an_answer_is_typed_but_the_option_it_annotates_is_not(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "answer_note.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 1


def test_a_prompt_queued_by_a_person_during_a_turn_is_typed_but_a_task_notice_is_not(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "queued.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 2


def test_interrupted_turns_and_comment_free_rejections_are_counted(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["interrupts"]["interrupted"] == 1
    assert numbers["interrupts"]["rejected_without_comment"] == 1


def test_sessions_are_ranked_by_how_many_turns_they_stopped(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({OTHER: "one_interrupt.jsonl", BASIC: "basic.jsonl"})
    _, _, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert "Most in: aaaa1111 demo (2), bbbb2222 demo (1)" in out


def test_hook_blocks_are_counted_per_event_and_message(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert {h["hook"]: h["count"] for h in numbers["hook_blocks"]} == {
        "Stop: Run the tests first": 1,
        "PreToolUse:Bash: blocked by the test policy": 1,
    }


def test_a_hook_block_is_named_by_its_message_whatever_command_ran_it(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "hook_messages.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert {h["hook"]: h["count"] for h in numbers["hook_blocks"]} == {
        "Stop: Question asked in prose. Ask it with the question tool.": 2,
        "Stop: Blockquote in the reply. Use bullets instead.": 1,
    }


def test_each_hook_block_names_the_session_with_the_most_counting_its_subagents(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place(
        {
            BASIC: "basic.jsonl",
            OTHER: "one_interrupt.jsonl",
            f"{BASIC}/subagents/agent-1": "subagent.jsonl",
            f"{BASIC}/subagents/workflows/wf-1/agent-2": "subagent.jsonl",
            f"{OTHER}/subagents/agent-1": "subagent.jsonl",
        }
    )
    _, numbers, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    worst = {h["hook"]: h["worst_session"] for h in numbers["hook_blocks"]}
    assert worst["PreToolUse:Grep: use the search tool"] == {
        "session": "aaaa1111",
        "project": "demo",
        "count": 2,
    }
    assert "- 3x  PreToolUse:Grep: use the search tool  (most in aaaa1111 demo, 2)" in out


@pytest.mark.parametrize(
    ("one", "other"),
    [
        (
            "Working directory is '/home/ada/demo', not project root. Run it from the root.",
            "Working directory is '/tmp/x/.claude/worktrees/y', not project root. Run it from "
            "the root.",
        ),
        (
            "Blocked: `/tmp/x/parser.py` is 620 lines and this Read has no range.",
            "Blocked: `/home/ada/demo/notes.md` is 620 lines and this Read has no range.",
        ),
        (
            "Rerun the failing test command: `cd /home/ada/demo && pytest -q`\nThen report.",
            "Rerun the failing test command: `cd /tmp/x && pytest -q`\nThen report.",
        ),
        (
            "Edit outside the repo (/home/ada/demo/a.py), refused.",
            "Edit outside the repo (/tmp/x/b.py), refused.",
        ),
    ],
)
def test_a_hook_block_keeps_its_name_when_only_a_path_in_it_changes(
    scripts: dict[str, ModuleType], one: str, other: str
) -> None:
    name = scripts["transcripts"].hook_block_name
    assert "/" not in name("Stop", one)
    assert name("Stop", one) == name("Stop", other)


def test_a_hook_block_keeps_its_name_when_only_a_number_in_it_changes(
    scripts: dict[str, ModuleType],
) -> None:
    name = scripts["transcripts"].hook_block_name
    one = name("PreToolUse:Read", "Blocked: `notes.md` is 483 lines and this Read has no range.")
    other = name(
        "PreToolUse:Read", "Blocked: `parser.py` is 1207 lines and this Read has no range."
    )
    assert one == other
    assert one.startswith("PreToolUse:Read: Blocked: … is … lines and this Read")


def test_a_hook_name_cut_at_a_space_ends_without_it(scripts: dict[str, ModuleType]) -> None:
    transcripts = scripts["transcripts"]
    words = "x" * (transcripts.HOOK_MESSAGE_CHARS - 1)
    assert transcripts.hook_block_name("Stop", words + " and more") == "Stop: " + words


def test_a_hook_block_keeps_its_name_whatever_its_quoted_spans_hold(
    scripts: dict[str, ModuleType],
) -> None:
    name = scripts["transcripts"].hook_block_name
    one = name("PreToolUse:Bash", "Use the venv. Pattern '^python3\\s' detected.")
    other = name("PreToolUse:Bash", 'Use the venv. Pattern "pip" detected.')
    assert one == other == "PreToolUse:Bash: Use the venv. Pattern … detected."


def test_a_long_quoted_value_is_shown_by_its_start_and_end(scan: ModuleType) -> None:
    value = "/home/ada/" + "x" * 80 + "/notes.md"
    shown = scan.shorten(value)
    assert len(shown) <= scan.HOOK_VALUE_CHARS
    assert shown.startswith("/home/ada/") and shown.endswith("/notes.md") and "…" in shown


def test_an_apostrophe_inside_a_word_does_not_start_a_quoted_span(
    scripts: dict[str, ModuleType],
) -> None:
    transcripts = scripts["transcripts"]
    message = "Use the project's venv, not the user's. Pattern 'pip' detected."
    assert transcripts.hook_block_name("Stop", message) == (
        "Stop: Use the project's venv, not the user's. Pattern … detected."
    )
    assert transcripts.quoted_parts(message) == ("pip",)


def test_a_long_hook_message_is_cut_to_its_start(scripts: dict[str, ModuleType]) -> None:
    transcripts = scripts["transcripts"]
    record = {
        "attachment": {
            "type": "hook_blocking_error",
            "hookEvent": "Stop",
            "blockingError": {"blockingError": "x" * 200, "command": "command hook 1"},
        }
    }
    assert transcripts.blocking_hooks(record) == [
        ("Stop: " + "x" * transcripts.HOOK_MESSAGE_CHARS, ())
    ]


def test_each_hook_block_counts_every_quoted_part_of_its_messages(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # One hook, its pattern on the second line: DROP DATABASE twice, eval once, and `Monitor`
    # in every message.
    root = place({BASIC: "hook_values.jsonl"})
    _, numbers, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    [block] = numbers["hook_blocks"]
    assert block["hook"] == "PreToolUse:Bash: Dangerous command pattern detected."
    assert block["count"] == 3
    assert block["values"] == [
        {"value": "Monitor", "count": 3},
        {"value": "DROP\\s+DATABASE", "count": 2},
        {"value": "\\beval\\b", "count": 1},
    ]
    # A part every block quotes is the rule's own wording, so the report leaves it out.
    assert "  - quoted: `DROP\\s+DATABASE` 2, `\\beval\\b` 1\n" in out


EDIT_BEFORE_READ = "Edit: File has not been read yet. Read it first before writing to it."


def test_tool_errors_are_counted_by_name_leaving_out_hook_blocks_and_rejections(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "tool_errors.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert {e["error"]: e["count"] for e in numbers["tool_errors"]} == {
        EDIT_BEFORE_READ: 2,
        "Bash exit 1: (eval):…: no matches found: …": 1,
        "Bash exit 1: ModuleNotFoundError: No module named …": 1,
    }


@pytest.mark.parametrize(
    ("tool", "text", "name"),
    [
        (
            "Edit",
            "<tool_use_error>File has been modified since read.</tool_use_error>",
            "Edit: File has been modified since read.",
        ),
        (
            "Bash",
            "Exit code 127\nzsh: command not found: rg",
            "Bash exit 127: zsh: command not found: rg",
        ),
        ("Bash", "Exit code 1", "Bash exit 1"),
        (
            "Bash",
            "Exit code 1\nTraceback (most recent call last):\n"
            "  File \"x.py\", line 9\nKeyError: 'id'",
            "Bash exit 1: KeyError: …",
        ),
        (
            "Read",
            "File content (31337 tokens) exceeds maximum allowed tokens (25000).",
            "Read: File content (… tokens) exceeds maximum allowed tokens (…).",
        ),
    ],
)
def test_a_tool_error_is_named_by_the_line_that_names_the_failure(
    scripts: dict[str, ModuleType], tool: str, text: str, name: str
) -> None:
    assert scripts["transcripts"].tool_error_name(tool, text) == name


def test_each_tool_error_names_its_sessions_worst_session_and_subagent_count(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place(
        {
            BASIC: "tool_errors.jsonl",
            OTHER: "one_interrupt.jsonl",
            f"{OTHER}/subagents/agent-1": "tool_errors_subagent.jsonl",
        }
    )
    _, numbers, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["tool_errors"][0] == {
        "error": EDIT_BEFORE_READ,
        "count": 3,
        "sessions": 2,
        "in_subagents": 1,
        "worst_session": {"session": "aaaa1111", "project": "demo", "count": 2},
    }
    line = f"- 3x in 2 sessions  {EDIT_BEFORE_READ}  (most in aaaa1111 demo, 2; 1 in subagents)"
    assert line in out


def test_tool_errors_are_ranked_by_sessions_touched_before_count(
    scan: ModuleType, scripts: dict[str, ModuleType]
) -> None:
    transcript = scripts["transcripts"].Transcript
    loop = transcript(Path("a.jsonl"), "aaaa1111", tool_errors=Counter({"Bash exit 1": 9}))
    spread = [
        transcript(Path(f"{s}.jsonl"), s, tool_errors=Counter({EDIT_BEFORE_READ: 1}))
        for s in ("bbbb2222", "cccc3333")
    ]
    rows = scan.errors_summary([loop, *spread], [])
    assert [r["error"] for r in rows] == [EDIT_BEFORE_READ, "Bash exit 1"]


def test_the_report_lists_the_first_tool_errors_and_counts_the_rest(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan, "TOOL_ERRORS_SHOWN", 1)
    root = place({BASIC: "tool_errors.jsonl"})
    _, numbers, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert len(numbers["tool_errors"]) == 3
    assert "- 2 other errors not listed, 2 in all; --json has them" in out


def install_skills(config_dir: Path, *names: str) -> None:
    for name in names:
        manifest = config_dir / "skills" / name / "SKILL.md"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f"---\nname: {name}\n---\n")


def test_skill_loads_are_counted_by_how_they_loaded(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    config_dir: Path,
) -> None:
    install_skills(config_dir, "review", "pdf-tools")
    root = place({BASIC: "basic.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert {s["skill"]: s["how"] for s in numbers["skills"]["loads"]} == {
        "pdf-tools": {"tool": 1},
        "review": {"typed": 1},
    }


def test_a_skill_an_agent_definition_preloads_counts_as_preloaded(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "preloaded.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert {s["skill"]: s["how"] for s in numbers["skills"]["loads"]} == {
        "style-guide": {"preloaded": 1}
    }
    assert numbers["activity"]["typed_messages"] == 1


def test_a_subagent_file_adds_hook_blocks_skill_loads_and_spend_but_no_session(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl", f"{BASIC}/subagents/agent-1": "subagent.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["sessions"] == 1
    assert numbers["activity"]["typed_messages"] == 2
    assert {h["hook"] for h in numbers["hook_blocks"]} >= {"PreToolUse:Grep: use the search tool"}
    assert {s["skill"]: s["how"] for s in numbers["skills"]["loads"]}["pdf-tools"] == {"tool": 2}
    sonnet = next(m for m in numbers["spend"]["models"] if m["model"] == "claude-sonnet-5-5")
    assert sonnet["calls"] == 2


def test_installed_skills_that_never_loaded_are_listed(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    config_dir: Path,
) -> None:
    install_skills(config_dir, "review", "pdf-tools", "unused-skill")
    root = place({BASIC: "basic.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["skills"]["never_loaded"] == ["unused-skill"]


def test_spend_prices_each_request_once_from_its_last_line(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    sonnet = next(m for m in numbers["spend"]["models"] if m["model"] == "claude-sonnet-5-5")
    assert sonnet["calls"] == 1 and sonnet["output"] == 300
    # input 1000 at $2, 1000 five-minute and 1000 one-hour cache writes at 1.25x and 2x,
    # 10000 cache reads at 0.05x, 300 output tokens at $10 (per million tokens)
    expected = (1000 * 2 + 1000 * 2.5 + 1000 * 4 + 10000 * 0.1 + 300 * 10) / 1e6
    assert sonnet["cost_usd"] == pytest.approx(expected)
    assert sonnet["cache_read_share"] == pytest.approx(10000 / 13000)


def test_an_advisor_call_counts_once_under_its_own_model(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "advisor.jsonl"})
    _, numbers, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    models = {m["model"]: m for m in numbers["spend"]["models"]}
    assert models["claude-sonnet-5-5"]["calls"] == 1 and models["claude-sonnet-5-5"]["output"] == 50
    fable = models["claude-fable-5-1"]
    assert (fable["calls"], fable["input"], fable["cache_write"], fable["cache_read"]) == (
        1,
        1000,
        400,
        4000,
    )
    assert fable["output"] == 200
    # input 1000 at $10, 400 one-hour cache writes at 2x, 4000 cache reads at 0.025x,
    # 200 output tokens at $50 (per million tokens)
    assert fable["cost_usd"] == pytest.approx((1000 * 10 + 400 * 20 + 4000 * 0.25 + 200 * 50) / 1e6)
    assert numbers["spend"]["calls"] == 2
    assert "| claude-fable-5-1 | 1 |" in out


def test_a_model_without_a_price_is_named_in_the_report(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl"})
    _, _, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert "Not priced" in out and "claude-test-9" in out


def headings(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("## ")]


def test_sections_appear_in_order(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    config_dir: Path,
) -> None:
    install_skills(config_dir, "review")
    root = place({BASIC: "basic.jsonl", OTHER: "tool_errors.jsonl"})
    _, _, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert headings(out) == [
        "## Activity",
        "## Interrupted turns",
        "## Hook blocks",
        "## Tool errors",
        "## Skills",
        "## Spend",
    ]


def test_sections_with_nothing_to_show_are_omitted(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({OTHER: "answers.jsonl"})
    _, _, out, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert headings(out) == ["## Activity"]


def test_the_default_root_is_the_config_dir_projects_folder(
    scan: ModuleType,
    place: Place,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    config_dir: Path,
) -> None:
    place({BASIC: "basic.jsonl"}, root=config_dir / "projects")
    code, numbers, _, _ = run_scan(scan, capsys, tmp_path)
    assert code == 0 and numbers["activity"]["sessions"] == 1


def test_repeated_root_combines_folders(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    first = place({BASIC: "basic.jsonl"}, root=tmp_path / "copied-folder")
    second = place({OTHER: "injected.jsonl"}, root=tmp_path / "second-folder")
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(first), "--root", str(second))
    assert numbers["activity"]["sessions"] == 2


def test_no_transcripts_exits_2_with_one_line_and_no_report(
    scan: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    code, _, out, err = run_scan(scan, capsys, tmp_path, "--root", str(empty))
    assert code == 2
    assert out == ""
    assert len(err.strip().splitlines()) == 1


def test_a_root_that_does_not_exist_is_refused_by_name(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl"})
    missing = tmp_path / "no-such-folder"
    code, _, out, err = run_scan(
        scan, capsys, tmp_path, "--root", str(root), "--root", str(missing)
    )
    assert code == 2 and out == ""
    assert str(missing) in err


def test_a_pasted_message_is_typed_without_its_paste_markers(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "pasted.jsonl"})
    _, numbers, _, _ = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert numbers["activity"]["typed_messages"] == 1


def test_unusable_records_are_reported_on_stderr(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "malformed.jsonl"})
    code, _, _, err = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert code == 0
    assert "skipped 2 lines that were not valid JSON and 1 user or assistant record" in err


def test_active_sessions_without_a_typed_message_raise_a_warning(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "no_typed.jsonl"})
    _, _, _, err = run_scan(scan, capsys, tmp_path, "--root", str(root))
    assert "had activity but no typed messages" in err


@pytest.mark.parametrize("days", ["0", "-3"])
def test_days_must_be_a_positive_number(
    scan: ModuleType, capsys: pytest.CaptureFixture[str], days: str
) -> None:
    with pytest.raises(SystemExit) as stopped:
        scan.main(["--days", days])
    assert stopped.value.code == 2
    assert "above 0" in capsys.readouterr().err


@pytest.mark.parametrize("since", ["20990602", "2099-06-02T10:00", "2099-6-2"])
def test_since_takes_only_a_yyyy_mm_dd_date(
    scan: ModuleType, capsys: pytest.CaptureFixture[str], since: str
) -> None:
    with pytest.raises(SystemExit) as stopped:
        scan.main(["--since", since])
    assert stopped.value.code == 2
    assert "is not a YYYY-MM-DD date" in capsys.readouterr().err


def test_the_json_window_start_carries_its_utc_offset(
    scan: ModuleType, place: Place, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = place({BASIC: "basic.jsonl"})
    _, numbers, _, _ = run_scan(
        scan, capsys, tmp_path, "--root", str(root), "--since", "2099-06-02"
    )
    assert numbers["window"]["start"] == "2099-06-02T00:00:00+00:00"
