"""Tests for read.py's tools, messages and index commands and the index behind them, on the invented
transcripts in fixtures/ beside this file."""

import ast
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

READ = Path(__file__).resolve().parents[3] / "skills" / "transcripts" / "scripts" / "read.py"
MAIN = "aaaa1111"
PARENT = "pppp0000"
FORK = "ffff0000"

type Place = Callable[..., Path]
type Run = Callable[..., tuple[int, str, str]]


def rows(run: Run, command: str, *args: str) -> list[dict[str, Any]]:
    code, out, err = run(command, "--json", *args)
    assert code == 0, err
    return [json.loads(line) for line in out.splitlines()]


def test_each_tool_call_is_a_row_with_its_result_and_whether_it_failed(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    found = {r["id"]: r for r in rows(run, "tools", "--results")}
    assert list(found) == ["c-1", "c-2", "c-3", "c-4"]
    assert found["c-1"]["input"] == {"command": "cd src && ls", "description": "List the sources"}
    assert (found["c-1"]["result"], found["c-1"]["is_error"]) == ("parser.py\nlexer.py", False)
    assert found["c-2"]["is_error"] is True
    assert (found["c-4"]["result"], found["c-4"]["is_error"]) == (None, None)
    assert "result" not in rows(run, "tools")[0]


def test_messages_label_what_a_person_did_not_type_and_keep_a_queued_prompt(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    users = [(r["text"], r["injected"]) for r in rows(run, "messages", "--role", "user")]
    assert users == [
        ("Add a test for the parser.", None),
        ("No, tests go in tests/, not src/.", None),
        ("Base directory for this skill: /skills/testing\n\nPut tests beside the code.", "meta"),
        ("Also run the suite.", None),
        ("Keep it short.", None),
        (
            "Here is a note offered by a side agent:\n\n> The suite takes a minute.",
            "side-agent-note",
        ),
    ]
    assert {r["role"] for r in rows(run, "messages", "--role", "assistant")} == {"assistant"}


def test_typed_keeps_only_the_words_a_person_typed(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    assert [r["text"] for r in rows(run, "messages", "--typed")] == [
        "Add a test for the parser.",
        "No, tests go in tests/, not src/.",
        "Also run the suite.",
        "Keep it short.",
    ]


def test_tool_is_a_full_match_on_the_name_and_match_a_regex_on_the_input(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    assert [r["id"] for r in rows(run, "tools", "--tool", "Bas")] == []
    assert [r["id"] for r in rows(run, "tools", "--tool", "Bash|Read")] == ["c-1", "c-2", "c-4"]
    assert [r["id"] for r in rows(run, "tools", "--match", r"parse\w+_test")] == ["c-3"]


def test_search_takes_prefixes_and_prints_the_session_with_most_matches_first(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    place("bbbb2222", "other.jsonl")
    found = rows(run, "messages", "--search", "renam*")
    assert [(r["session"], r["matches"]) for r in found] == [("bbbb2222", 2)] * 2
    found = rows(run, "messages", "--search", "test* OR renam*")
    assert [(r["session"], r["matches"]) for r in found][:1] == [(MAIN, 6)]
    assert [r["session"] for r in found][-2:] == ["bbbb2222", "bbbb2222"]
    assert rows(run, "messages", "--search", "renam") == []


def test_search_reads_results_only_when_they_are_asked_for(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    assert rows(run, "tools", "--search", "lexer") == []
    assert [r["id"] for r in rows(run, "tools", "--results", "--search", "lexer")] == ["c-1"]


def test_a_bad_search_is_a_usage_error_that_names_the_fix(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")
    code, _, err = run("messages", "--search", "run-health")
    assert code == 2
    assert "in double quotes" in err


def stamp(days_ago: float, text: str, uuid: str) -> str:
    when = datetime.now(UTC) - timedelta(days=days_ago)
    record = {
        "type": "user",
        "uuid": uuid,
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {"content": text},
    }
    return json.dumps(record) + "\n"


def test_the_window_is_the_last_seven_days_by_each_rows_own_time(root: Path, run: Run) -> None:
    session = root / "-home-ada-demo" / f"{MAIN}.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text(stamp(8, "eight days ago", "x-1") + stamp(1, "yesterday", "x-2"))
    assert [r["text"] for r in rows(run, "messages", "--days", "7")] == ["yesterday"]
    assert [r["text"] for r in rows(run, "messages", "--days", "9")] == [
        "eight days ago",
        "yesterday",
    ]
    old = time.time() - 10 * 86400
    os.utime(session, (old, old))
    assert rows(run, "messages", "--days", "7") == []


def test_since_and_until_bound_the_window_by_whole_days(place: Place, run: Run) -> None:
    place(MAIN, "correction.jsonl")  # 2099-06-01
    assert rows(run, "messages", "--until", "2099-05-31") == []
    assert len(rows(run, "messages", "--until", "2099-06-01")) == 10
    with pytest.raises(SystemExit) as refused:
        run("messages", "--until", "2098-12-31")  # before the --since the helper passes
    assert refused.value.code == 2


def test_project_session_and_kind_pick_files_and_a_subagent_row_names_its_session(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    place("bbbb2222", "other.jsonl", project="-home-ada-docs")
    place(f"{MAIN}/subagents/agent-a1", "agent.jsonl")
    place(f"{MAIN}/subagents/workflows/run-1/agent-w1", "agent.jsonl")
    place(f"{MAIN}/subagents/workflows/run-1/journal", "other.jsonl")
    assert {r["session"] for r in rows(run, "messages", "--project", "docs")} == {"bbbb2222"}
    assert {r["session"] for r in rows(run, "messages", "--session", "bbbb")} == {"bbbb2222"}
    agents = rows(run, "tools", "--kind", "subagent,workflow")
    assert [(r["session"], r["kind"], r["agent"]) for r in agents] == [
        (MAIN, "subagent", "agent-a1"),
        (MAIN, "workflow", "agent-w1"),
    ]


def test_each_row_carries_its_cwd_and_a_call_after_a_sibling_cd_is_uncertain(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    found = {r["id"]: r for r in rows(run, "tools")}
    assert {i: r["cwd_uncertain"] for i, r in found.items()} == {
        "c-1": False,
        "c-2": True,
        "c-3": False,
        "c-4": False,
    }
    assert {r["cwd"] for r in found.values()} == {"/home/ada/demo"}


@pytest.mark.parametrize(
    ("command", "moves"),
    [
        ("git fetch && cd sub && ls", True),
        ("echo a\ncd x", True),
        ("(cd x && make)", True),
        ("if true; then cd /tmp; fi", True),
        ("HOME=/tmp builtin cd", True),
        ("command pushd /tmp", True),
        ("chdir /tmp", True),
        ("echo $(pwd); cd x", True),
        ('cd; echo "$(pwd)"', True),
        ("ssh host 'cd ~/x; pwd'", False),
        ("grep -n 'cd ' notes.txt", False),
        ('grep -n "cd " f; echo "$(pwd)"', False),
        ("echo done # then cd back", False),
        ("cat > run.sh <<'EOF'\ncd /srv\nEOF\nls", False),
        ("python3 - <<PY\nimport os\nPY\ncd x", True),
        ("echo \\`cd x\\`", False),
        ("2>/dev/null cd /tmp", True),
        ("FOO=1 2>&1 cd /x", True),
        (">log cd x && ls", True),
        ("make 2>&1 | tee log; cd out", True),
        ("echo ';' cd /tmp", False),
        ('echo "&&" cd /tmp', False),
        ("find . -name x -exec rm {} \\; cd y", False),
        ("G='Read(a/**)'; (cd sub && ls)", True),
        ("ls ~/Library/Application\\ Support/x; cd /p", True),
        ('s=\'for f in $(cd "$d" && ls); do :; done\'; echo "$s"', False),
        ("$'cd' /tmp", True),
        ("$'c\\x64' /tmp", True),
        ("builtin $'cd' /tmp", True),
        ("c$'d' /tmp", True),
        ("printf $'a;b\\n' cd; echo $'cd'", False),
    ],
)
def test_a_command_moves_the_directory_only_when_the_shell_runs_a_cd(
    index: ModuleType, command: str, moves: bool
) -> None:
    assert index.moves_directory("Bash", {"command": command}) is moves


def test_a_worktree_tool_moves_the_directory(index: ModuleType) -> None:
    assert index.moves_directory("EnterWorktree", {"name": "w"})
    assert index.moves_directory("ExitWorktree", {})


def test_a_record_a_fork_repeats_prints_once_under_the_session_it_came_from(
    place: Place, run: Run
) -> None:
    place(PARENT, "parent.jsonl")
    place(FORK, "fork.jsonl")
    found = [(r["session"], r["text"]) for r in rows(run, "messages", "--role", "user")]
    assert sorted(found) == [
        (FORK, "The fork goes on."),
        (PARENT, "Before the fork: rename the flag."),
        (PARENT, "The parent goes on."),
    ]
    fork_only = rows(run, "messages", "--role", "user", "--session", FORK)
    assert [r["text"] for r in fork_only] == [
        "Before the fork: rename the flag.",
        "The fork goes on.",
    ]


def test_an_unmarked_copy_gives_its_repeats_to_the_file_created_first(
    place: Place, run: Run
) -> None:
    place(PARENT, "parent.jsonl")
    place("zzzz9999", "copy.jsonl")
    found = [(r["session"], r["text"]) for r in rows(run, "messages", "--role", "user")]
    assert sorted(found) == [
        (PARENT, "Before the fork: rename the flag."),
        (PARENT, "The parent goes on."),
        ("zzzz9999", "The copy goes on."),
    ]


def test_a_file_linked_into_a_resuming_session_prints_under_the_session_that_made_it(
    place: Place, run: Run, root: Path
) -> None:
    made = place("zzzz9999/subagents/agent-a1", "agent.jsonl")
    link = root / "-home-ada-demo" / "aaaa0000" / "subagents" / "agent-a1.jsonl"
    link.parent.mkdir(parents=True)
    link.symlink_to(made)
    assert [r["session"] for r in rows(run, "tools")] == ["zzzz9999"]
    assert [r["session"] for r in rows(run, "tools", "--session", "aaaa0000")] == ["aaaa0000"]


def test_skip_session_leaves_a_session_out_and_its_words_out_of_its_forks(
    place: Place, run: Run
) -> None:
    place(PARENT, "parent.jsonl")
    place(FORK, "fork.jsonl")
    found = rows(run, "messages", "--typed", "--skip-session", "pppp")
    assert [(r["session"], r["text"]) for r in found] == [(FORK, "The fork goes on.")]
    assert rows(run, "messages", "--skip-session", "pppp", "--skip-session", "ffff") == []


def test_repeated_root_reads_every_folder(place: Place, run: Run, tmp_path: Path) -> None:
    place(MAIN, "correction.jsonl")
    second = tmp_path / "second-login" / "projects"
    (second / "-home-ada-docs").mkdir(parents=True)
    place("bbbb2222", "other.jsonl").rename(second / "-home-ada-docs" / "bbbb2222.jsonl")
    found = rows(run, "messages", "--root", str(second))
    assert {r["session"] for r in found} == {MAIN, "bbbb2222"}


def test_the_default_root_is_the_config_folders_projects(
    place: Place, read: ModuleType, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (config_dir / "projects").mkdir(parents=True)
    place("bbbb2222", "other.jsonl").parent.rename(config_dir / "projects" / "-home-ada-docs")
    assert read.main(["messages", "--since", "2099-01-01", "--json"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_no_transcripts_exits_2_with_one_line(run: Run, root: Path) -> None:
    root.mkdir()
    code, out, err = run("messages")
    assert (code, out) == (2, "")
    assert "no transcripts found" in err


def test_every_row_prints_whole_with_no_cap(root: Path, run: Run) -> None:
    session = root / "-home-ada-demo" / f"{MAIN}.jsonl"
    session.parent.mkdir(parents=True)
    long_text = "word " * 20_000
    lines = [stamp(1, f"message {n}", f"n-{n}") for n in range(600)]
    session.write_text("".join(lines) + stamp(1, long_text, "long"))
    found = rows(run, "messages", "--days", "7")
    assert len(found) == 601
    assert found[-1]["text"] == long_text.strip()
    _, out, _ = run("messages", "--days", "7")
    assert long_text.strip() in out


def test_plain_text_names_the_session_once_and_shows_each_row_in_full(
    place: Place, run: Run
) -> None:
    place(MAIN, "correction.jsonl")
    _, out, _ = run("tools", "--results", "--tool", "Write|Read")
    assert out.count("== session aaaa1111, main, project -home-ada-demo") == 1
    assert (
        "  Read  id=c-2  cwd=/home/ada/demo  cwd_uncertain\n  file_path: parser.py\n"
        "  result (error):\n    File does not exist.\n"
    ) in out
    assert "  content:\n    def test_parse():\n        assert parse('1') == 1\n" in out
    _, out, _ = run("messages", "--typed", "--match", "tests/")
    assert "user  uuid=u-2  cwd=/home/ada/demo\n  No, tests go in tests/, not src/.\n" in out


def test_the_index_command_indexes_every_transcript_through_worker_processes(
    tmp_path: Path, config_dir: Path, index: ModuleType
) -> None:
    try:
        os.sysconf("SC_SEM_NSEMS_MAX")  # a process pool queries this limit before it starts
    except PermissionError:
        pytest.skip("this sandbox refuses the semaphore limit query a process pool starts with")
    root = tmp_path / "projects" / "-home-ada-demo"
    root.mkdir(parents=True)
    for n in range(index.POOL_THRESHOLD + 1):
        (root / f"s{n:03d}.jsonl").write_text(stamp(1, f"message {n}", f"u-{n}"))
    script = Path(index.__file__).with_name("read.py")
    done = subprocess.run(
        [sys.executable, str(script), "index", "--root", str(tmp_path / "projects")],
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(
        f"files={index.POOL_THRESHOLD + 1} rows={index.POOL_THRESHOLD + 1} "
    )


def test_a_file_that_grew_is_read_again_and_keeps_one_copy_of_each_row(
    place: Place, run: Run
) -> None:
    session = place("bbbb2222", "other.jsonl")
    assert len(rows(run, "messages")) == 2
    with session.open("a") as more:
        more.write(
            json.dumps({"type": "user", "uuid": "o-3", "message": {"content": "more"}}) + "\n"
        )
    os.utime(session, (4102444801, 4102444801))  # still after the window starts
    assert [r["text"] for r in rows(run, "messages")][-1] == "more"
    assert len(rows(run, "messages")) == 3


def test_a_deleted_transcript_leaves_the_index(place: Place, run: Run, index: ModuleType) -> None:
    kept = place(MAIN, "correction.jsonl")
    place("bbbb2222", "other.jsonl")
    rows(run, "messages")
    (kept.parent / "bbbb2222.jsonl").unlink()
    rows(run, "messages")
    conn = sqlite3.connect(index.db_path())
    assert [path for (path,) in conn.execute("SELECT path FROM files")] == [str(kept)]
    conn.close()


def test_search_finds_a_tool_inputs_words_and_forgets_them_when_the_file_changes_or_goes(
    root: Path, run: Run, index: ModuleType
) -> None:
    session = root / "-home-ada-demo" / "cccc3333.jsonl"
    session.parent.mkdir(parents=True)

    def write(command: str, mtime: int) -> None:
        call = {"type": "tool_use", "id": "c-9", "name": "Bash", "input": {"command": command}}
        record = {
            "type": "assistant",
            "uuid": "a-9",
            "timestamp": "2099-06-01T10:00:00.000Z",
            "cwd": "/home/ada/demo",
            "message": {"id": "m-9", "role": "assistant", "content": [call]},
        }
        session.write_text(json.dumps(record) + "\n")
        os.utime(session, (mtime, mtime))

    def found(word: str) -> list[str]:
        return [r["id"] for r in rows(run, "tools", "--search", word)]

    # A newline in the input is an escape in its stored JSON, which must not join the words.
    write("cd src\nmake build", 4102444800)
    assert found("make") == ["c-9"]
    write("pytest -q", 4102444801)
    assert (found("make"), found("pytest")) == ([], ["c-9"])
    conn = sqlite3.connect(index.db_path())
    conn.execute("INSERT INTO rows_text (rows_text, rank) VALUES ('integrity-check', 1)")
    conn.close()
    session.unlink()
    kept = root / "-home-ada-demo" / "dddd4444.jsonl"
    kept.write_text("")
    assert found("pytest") == []


def test_the_index_is_private_to_the_user(place: Place, run: Run, index: ModuleType) -> None:
    place(MAIN, "correction.jsonl")
    rows(run, "messages")
    assert index.db_path().stat().st_mode & 0o777 == 0o600
    assert index.db_path().parent.stat().st_mode & 0o777 == 0o700


def test_a_corrupt_index_is_reset_and_the_query_still_answers(
    place: Place, run: Run, index: ModuleType
) -> None:
    place("bbbb2222", "other.jsonl")
    rows(run, "messages")
    with index.db_path().open("r+b") as handle:
        handle.write(b"\0" * 4096)
    code, out, err = run("messages", "--json")
    assert code == 0
    assert len(out.splitlines()) == 2
    assert "corrupt" in err


def test_an_older_python_is_told_the_version_read_needs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ast.parse(READ.read_text(), feature_version=(3, 9))
    monkeypatch.setattr(sys, "version_info", (3, 13, 5, "final", 0))
    monkeypatch.setattr(sys, "version", "3.13.5 (main)")
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(READ), run_name="read_on_an_older_python")
    assert exited.value.code == 2
    assert capsys.readouterr().err == "read.py needs Python 3.14 or newer; this is 3.13.5.\n"
