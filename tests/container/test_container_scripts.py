import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check  # noqa: E402
import check_clone  # noqa: E402
import fixtures  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

SESSION = "run-1"


def plugin_of(config: Path) -> Path:
    """The folder the run's plugin is installed in, beside its config folder."""
    return config.parent / "agent-harness"


def run_with(config: Path, *commands: str, evals: bool = True, reads: dict | None = None) -> str:
    """A run's output naming the plugin's folder, its transcript whose Bash calls are *commands* and
    whose Read calls are *reads* (file to whether it was found), and an eval suite when *evals*.
    Returns the run output's path."""
    (plugin_of(config) / "skills" / "self-optimize" / "references").mkdir(parents=True)
    folder = config / "projects" / "-home-dev-shop"
    folder.mkdir(parents=True)
    blocks = [{"type": "tool_use", "name": "Bash", "input": {"command": c}} for c in commands]
    results = []
    for n, (file, found) in enumerate((reads or {}).items()):
        blocks.append(
            {"type": "tool_use", "id": f"r{n}", "name": "Read", "input": {"file_path": file}}
        )
        content = "1\tledger" if found else "File does not exist."
        results.append(
            {
                "type": "tool_result",
                "tool_use_id": f"r{n}",
                "is_error": not found,
                "content": content,
            }
        )
    records = [
        {"type": "assistant", "message": {"role": "assistant", "content": blocks}},
        {"type": "user", "message": {"role": "user", "content": results}},
    ]
    (folder / f"{SESSION}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    if evals:
        (config / "self-optimize" / "2099-01-01-evals").mkdir(parents=True)
        (config / "self-optimize" / "2099-01-01-evals" / "cases.md").write_text("x")
    plugin = {"name": "a", "path": str(plugin_of(config))}
    init = {"type": "system", "subtype": "init", "plugins": [plugin]}
    output = config.parent / "run.jsonl"
    output.write_text(
        json.dumps({"type": "system", "subtype": "hook"}) + "\n" + json.dumps(init) + "\n"
    )
    return str(output)


def test_a_run_inside_the_install_that_wrote_its_evals_passes(tmp_path, capsys) -> None:
    config = tmp_path / ".claude"
    output = run_with(
        config,
        f"cd {plugin_of(config)}/skills/self-optimize && ls references/ {config}/rules",
        f"cat {config}/self-optimize/ledger.md 2>/dev/null || echo first run",
        f"ls {config}/CLAUDE.md {config}/settings.json {config}/projects/*/memory/*.md",
        "ls ~/.cache/agent-harness/transcripts https://example.com/a/b and/or */x.md [ab]/y",
        "python3 -c 'p = dst+\"/prompt.md\"' && python3 -c \"q = dst + '/x.md'\"",
        "cd /home/dev && cd ~ && ls",
    )
    assert check.main([str(config), SESSION, output]) == 0
    assert "reached step 7: yes" in capsys.readouterr().out


def test_a_path_outside_the_install_fails_the_run(tmp_path, capsys) -> None:
    config = tmp_path / ".claude"
    output = run_with(
        config,
        "python3 /home/dev/agent-harness-copy/x.py",
        "cat /Users/someone/notes",
        "cat /home/dev/notes.md",
    )
    assert check.main([str(config), SESSION, output]) == 1
    out = capsys.readouterr().out
    assert "Bash: /home/dev/agent-harness-copy/x.py" in out
    assert "Bash: /Users/someone/notes" in out
    assert "Bash: /home/dev/notes.md" in out


def test_a_path_under_a_stand_in_name_is_outside_only_in_a_bash_command(tmp_path) -> None:
    config = tmp_path / ".claude"
    transcript = tmp_path / "session.jsonl"
    calls = [
        {"type": "tool_use", "name": "Agent", "input": {"prompt": "write <case-name>/prompt.md"}},
        {"type": "tool_use", "name": "Agent", "input": {"prompt": "<tmp>/home/dev/y</tmp>"}},
        {"type": "tool_use", "name": "Bash", "input": {"command": "sort <names>/home/dev/x"}},
    ]
    record = {"type": "assistant", "message": {"content": calls}}
    transcript.write_text(json.dumps(record) + "\n")
    found = check.outside(config, [], [transcript])
    assert found == [("Agent", "/home/dev/y"), ("Bash", "/home/dev/x")]


def test_a_sed_script_and_one_missing_name_at_the_disk_root_are_not_paths(tmp_path) -> None:
    config = tmp_path / ".claude"
    transcript = tmp_path / "session.jsonl"
    commands = [
        "sed -i 's/Three (a)/Two (b)/' /home/dev/report.md",
        "sed -n '/^# Review/,/^## /p' /home/dev/notes.md",
        "sed -n -f '/home/dev/edit.sed' shop.md",
        "sed -n '/needle/p' shop.md",
        "sed -n '1r /home/dev/more.md' shop.md",
        "printf '<p>received</p>' > page.html",
        "cat <names>/prompt.md",
        "cat /etc",
        "ls -d /et[c]",
    ]
    calls = [{"type": "tool_use", "name": "Bash", "input": {"command": c}} for c in commands]
    record = {"type": "assistant", "message": {"content": calls}}
    transcript.write_text(json.dumps(record) + "\n")
    found = check.outside(config, [], [transcript])
    names = ("report.md", "notes.md", "edit.sed", "more.md")
    names = (*(f"/home/dev/{name}" for name in names), "/etc", "/et[c]")
    assert found == [("Bash", name) for name in names]


def test_only_the_backslash_of_an_escaped_delimiter_around_a_path_is_dropped(tmp_path) -> None:
    config = tmp_path / ".claude"
    transcript = tmp_path / "session.jsonl"
    readme = tmp_path / "plugin" / "README.md"
    readme.parent.mkdir()
    readme.write_text("x")

    def checked(command: str) -> tuple[list[tuple[str, str]], list[str]]:
        call = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [call]}}))
        return (
            check.outside(config, [readme.parent], [transcript]),
            check.absent(config, [readme.parent], [transcript]),
        )

    # Prose wrapping an allowed folder or an existing file in escaped backticks or quotes.
    assert checked("cat > p.md <<EOF\nRepo (\\`/home/dev/shop\\`, an app)\nEOF") == ([], [])
    assert checked('echo "run \\"/home/dev/shop\\" first"') == ([], [])
    assert checked(f"echo \\`{readme}\\`") == ([], [])
    # The same wrapping around a path outside the install.
    assert checked("echo \\`/home/dev/notes.md\\`")[0] == [("Bash", "/home/dev/notes.md\\")]
    # A backslash that is part of the name: an escaped space, quote or backtick, a literal one.
    for tail in ("\\ notes.md", '\\"', "\\`", "\\'"):
        assert checked(f"cat /home/dev/shop{tail}")[0] == [("Bash", "/home/dev/shop\\")]
        assert checked(f"cat {readme}{tail}")[1] == [f"{readme}\\"]
    assert checked("cat '/home/dev/shop\\'")[0] == [("Bash", "/home/dev/shop\\")]


def test_only_a_names_only_listing_of_the_home_folders_folders_is_inside(tmp_path) -> None:
    config = tmp_path / ".claude"
    transcript = tmp_path / "session.jsonl"

    def outside(command: str) -> list[str]:
        call = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [call]}}))
        return [word for _, word in check.outside(config, [], [transcript])]

    assert outside("git ls-files; ls -d /home/dev/*/ 2>&1") == []
    assert outside("ls -a /home/dev/shop; ls /home/dev/*/ -d") == []
    assert outside("ls -ld ~/*/ $HOME/* | head") == []
    # Anything that opens what the glob expands to, or a path below it.
    assert outside("ls /home/dev/*/") == ["/home/dev/*/"]
    assert outside("ls -dR ~/*/") == ["~/*/"]
    assert outside("cat ~/*") == ["~/*"]
    assert outside("ls -I -d ~/*/") == ["~/*/"]
    assert outside("ls --ignore -d ~/*/") == ["~/*/"]
    assert outside("ls -d `cat ~/*`") == ["~/*"]
    assert outside("ls -d $(cat ~/*)") == ["~/*"]
    assert outside("ls -d /tmp; grep -R needle $HOME/*/") == ["$HOME/*/"]
    assert outside("ls -d /home/dev/*/src ~/*/notes.md /home/*/") == [
        "/home/dev/*/src",
        "~/*/notes.md",
        "/home/*/",
    ]


def test_a_preview_cut_short_and_a_word_joined_to_a_code_span_are_not_paths(tmp_path) -> None:
    config = tmp_path / ".claude"
    transcript = tmp_path / "session.jsonl"

    def found(tool: str, tool_input: dict) -> list[str]:
        call = {"type": "tool_use", "name": tool, "input": tool_input}
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [call]}}))
        stray = [word for _, word in check.outside(config, [], [transcript])]
        return stray + check.absent(config, [], [transcript])

    # A tool's own preview of a longer field of the same call, cut inside a path.
    full = f"The edited file is at {config}/self-optimize/evals/EDITED.md. Copy it."
    preview = f"The edited file is at {config}/…"
    assert found("SendMessage", {"message": full, "content": preview}) == []
    # Cut-short text with no longer field beside it is still judged.
    assert found("SendMessage", {"message": f"See {config}/…"}) == [f"{config}/…"] * 2
    twice = {"message": f"See {config}/…", "content": f"See {config}/……"}
    assert f"{config}/…" in found("SendMessage", twice)

    def bash(command: str) -> list[str]:
        return found("Bash", {"command": command})

    # A word joined to a closed code span that names nothing on disk.
    assert bash("cat > /tmp/note.md <<'EOF'\nThe `make test`/912/4-min line\nEOF") == []
    assert bash('echo "The \\`make test\\`/912/4-min line"') == []
    # A file that exists, or a path inside an open span, is still judged.
    assert bash("cat `true`/etc/passwd") == ["/etc/passwd"]
    assert bash("echo `/opt/elsewhere/notes.md`") == ["/opt/elsewhere/notes.md"]
    # A command substitution closes with a backtick too, and what follows it is a path.
    assert bash("cat `git rev-parse --show-toplevel`/../other/a.md") == ["/../other/a.md"]
    assert bash("cat > /tmp/n.md <<EOF\n`pwd`/../other/notes.md\nEOF") == ["/../other/notes.md"]
    assert bash("grep -c '`' a.md; V=`/opt/other/bin/tool -v`") == ["/opt/other/bin/tool"]
    # Outside a shell there is no substitution: a file's text is prose.
    note = {"file_path": "/tmp/n.md", "content": "The `make test`/912/4-min line"}
    assert found("Write", note) == []


def test_the_disk_root_given_to_a_command_that_walks_folders_is_outside(tmp_path) -> None:
    config = tmp_path / ".claude"
    transcript = tmp_path / "session.jsonl"

    def outside(command: str, **fields: str) -> list[str]:
        tool_input = {"command": command} | fields
        call = {"type": "tool_use", "name": "Bash", "input": tool_input}
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [call]}}))
        return [word for _, word in check.outside(config, [], [transcript])]

    assert outside("find / -name test_orders.py 2>/dev/null | head") == ["/"]
    assert outside("git ls-files; find -L / -name 'CLAUDE.md' -not -path '*/x/*'") == ["/"]
    assert outside("cd /tmp && sudo du -sh / | tail -1") == ["/"]
    assert outside("grep -rl needle /") == ["/"]
    # However the shell spells the root, and wherever the walker binary lives.
    assert outside('find "/" -name test_orders.py') == ["/"]
    assert outside("du -sh '/'") == ["/"]
    assert outside("find \\\n  / -name test_orders.py") == ["/"]
    assert outside("/usr/bin/find / -name test_orders.py") == ["/"]
    assert outside("rg -l --hidden needle /") == ["/"]
    assert outside("fd test_orders.py /") == ["/"]
    assert outside("x=$(find / -name test_orders.py)") == ["/"]
    # A slash that divides, or sits in text a command writes, is no argument to a walk.
    assert outside("echo $((total / 2)); python3 -c 'print(1 - pct / 100)'") == []
    assert outside("echo 'run 1 / run 2' > /tmp/note.md") == []
    assert outside("cat > /tmp/n.md <<'EOF'\nfind / is slow, search the project\nEOF") == []
    assert outside("cat > /tmp/n.md <<EOF\nfind / is slow, search the project\nEOF") == []
    assert outside("find . -name '*.py' | wc -l") == []
    assert outside('printf "%s" "Never run find / on this machine"') == []
    assert outside("git status  # never find / here") == []
    # A slash a walker reads as its pattern or a test, not as where to start.
    assert outside("grep -r / tests") == []
    assert outside("rg -n / tests") == []
    assert outside("find . -name /") == []
    assert outside("fd / tests") == []
    # A field the shell never runs.
    assert outside("pwd", description="Avoid find / when searching") == []


def test_a_file_of_the_authors_own_layout_in_the_config_folder_fails_the_run(
    tmp_path, capsys, monkeypatch
):
    # The config folder sits under an allowed system root, as it does under /tmp on Linux CI.
    monkeypatch.setattr(check, "SYSTEM", (str(tmp_path),))
    config = tmp_path / ".claude"
    output = run_with(config, f"cat {config}/memory/feedback_tests.md", f"ls {config}")
    assert check.main([str(config), SESSION, output]) == 1
    assert f"Bash: {config}/memory/feedback_tests.md" in capsys.readouterr().out


def test_a_path_named_in_the_config_folder_or_the_plugin_that_does_not_exist_fails_the_run(
    tmp_path, capsys
):
    config = tmp_path / ".claude"
    missing = f"{plugin_of(config)}/skills/review/references/missing.md"
    output = run_with(config, f"cat {config}/rules/core-rules.md 2>/dev/null; cat {missing}")
    assert check.main([str(config), SESSION, output]) == 1
    out = capsys.readouterr().out
    assert f"  {config}/rules/core-rules.md\n" in out
    assert f"  {missing}\n" in out


def test_a_read_that_finds_no_file_fails_the_run_except_a_first_runs_ledger(tmp_path, capsys):
    config = tmp_path / ".claude"
    ledger, memory = config / "self-optimize" / "ledger.md", config / "projects" / "p" / "memory.md"
    output = run_with(config, reads={str(ledger): False, str(memory): False})
    assert check.main([str(config), SESSION, output]) == 1
    out = capsys.readouterr().out
    assert f"  {memory}\n" in out
    assert str(ledger) not in out


def test_a_run_that_wrote_no_evals_fails(tmp_path, capsys) -> None:
    config = tmp_path / ".claude"
    output = run_with(config, "true", evals=False)
    assert check.main([str(config), SESSION, output]) == 1
    assert "reached step 7: no" in capsys.readouterr().out


def test_the_invented_sessions_fall_inside_a_first_runs_window(tmp_path) -> None:
    now = datetime(2099, 3, 1, 9, 30, tzinfo=UTC)
    today = now.replace(hour=0, minute=0)
    written = fixtures.write(tmp_path, now)
    assert len(written) == len(fixtures.SESSIONS)
    for path in written:
        for line in path.read_text().splitlines():
            moment = datetime.fromisoformat(json.loads(line)["timestamp"].replace("Z", "+00:00"))
            assert today - timedelta(days=30) <= moment < today


def test_the_project_holds_every_test_file_an_invented_session_names(tmp_path) -> None:
    kept = tmp_path / "tests" / "test_orders.py"
    kept.parent.mkdir()
    kept.write_text("# the project's own\n")
    written = fixtures.write_project(tmp_path)
    said = " ".join(text for session in fixtures.SESSIONS for text in session[1:])
    named = set(re.findall(r"tests/test_\w+\.py", said))
    assert len(named) >= 5
    for name in named:
        assert (tmp_path / name).is_file()
    assert kept.read_text() == "# the project's own\n" and kept not in written
    compile((tmp_path / "tests" / "test_cart.py").read_text(), "test_cart.py", "exec")


def probe(tmp_path: Path, *blocks: dict) -> Path:
    """A probe session's output holding *blocks*, tool calls from the assistant and the rest
    from the user."""
    records = [
        {
            "type": "assistant" if block["type"] == "tool_use" else "user",
            "message": {"content": [block]},
        }
        for block in blocks
    ]
    output = tmp_path / "probe.jsonl"
    output.write_text("".join(json.dumps(r) + "\n" for r in records))
    return output


def write_call() -> dict:
    content = "Done ✅\n"
    return {
        "type": "tool_use",
        "id": "t1",
        "name": "Write",
        "input": {"file_path": "notes.md", "content": content},
    }


def result(text: str, is_error: bool = True) -> dict:
    return {"type": "tool_result", "tool_use_id": "t1", "is_error": is_error, "content": text}


def any_input(_: dict) -> bool:
    return True


def test_a_call_refused_with_the_hooks_own_reason_passes(tmp_path) -> None:
    said = f"PreToolUse:Write hook error: Blocked: {check_clone.EMOJI_REASON} / ..."
    output = probe(tmp_path, write_call(), result(said))
    verdict = check_clone.refused(output, "Write", any_input, check_clone.EMOJI_REASON)
    assert verdict == (check_clone.PASSED, "")


def test_a_call_blocked_because_its_hook_crashed_fails(tmp_path) -> None:
    said = "PreToolUse:Write hook error: SyntaxError: invalid syntax"
    output = probe(tmp_path, write_call(), result(said))
    verdict, why = check_clone.refused(output, "Write", any_input, check_clone.EMOJI_REASON)
    assert verdict == check_clone.FAILED
    assert "SyntaxError" in why


def test_a_call_that_ran_fails(tmp_path) -> None:
    output = probe(tmp_path, write_call(), result("File created", is_error=False))
    verdict, _ = check_clone.refused(output, "Write", any_input, check_clone.EMOJI_REASON)
    assert verdict == check_clone.FAILED


def test_a_session_that_never_made_the_call_is_not_attempted(tmp_path) -> None:
    output = probe(tmp_path, {"type": "text", "text": "I would rather not write that."})
    verdict, _ = check_clone.refused(output, "Write", any_input, check_clone.EMOJI_REASON)
    assert verdict == check_clone.SKIPPED


def test_a_prose_question_passes_only_when_it_was_sent_back(tmp_path) -> None:
    question = {"type": "text", "text": "Which color do you prefer?"}
    feedback = {"type": "text", "text": f"Stop hook feedback:\n{check_clone.PROSE_FEEDBACK}."}
    sent_back = check_clone.sent_back(
        probe(tmp_path, question, feedback), check_clone.PROSE_FEEDBACK
    )
    assert sent_back == (check_clone.PASSED, "")
    let_through, _ = check_clone.sent_back(probe(tmp_path, question), check_clone.PROSE_FEEDBACK)
    assert let_through == check_clone.FAILED


def test_the_stop_flag_passes_only_when_set_kept_then_removed(tmp_path) -> None:
    for step, listed in (("stop", "s1\n"), ("machine_prompt", "s1\n"), ("human_prompt", "")):
        (tmp_path / f"flag_after_{step}.txt").write_text(listed)
    assert check_clone.stop_flag(tmp_path, "s1") == (check_clone.PASSED, "")
    (tmp_path / "flag_after_human_prompt.txt").write_text("s1\n")
    assert check_clone.stop_flag(tmp_path, "s1")[0] == check_clone.FAILED
    (tmp_path / "flag_after_stop.txt").write_text("")
    assert check_clone.stop_flag(tmp_path, "s1")[0] == check_clone.FAILED


def test_a_commit_passes_only_as_one_typed_commit_on_a_clean_tree_with_the_skill_loaded(
    tmp_path,
) -> None:
    config, out = tmp_path / ".claude", tmp_path / "out"
    transcript = config / "projects" / "-home-dev-shop" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    out.mkdir()
    (out / "commit.jsonl").write_text(json.dumps({"type": "system", "session_id": "s1"}) + "\n")
    transcript.write_text(f"{check_clone.SKILL_LOADED}{config}/skills/commit\n")
    (out / "git_status.txt").write_text("")
    (out / "git_log.txt").write_text("fix(shipping): raise the fee\nfeat: add the fee\n")
    assert check_clone.committed(config, out) == (check_clone.PASSED, "")
    (out / "git_log.txt").write_text("feat: add the fee\n")
    assert check_clone.committed(config, out)[0] == check_clone.FAILED
    (out / "git_log.txt").write_text("raise the fee\nfeat: add the fee\n")
    assert check_clone.committed(config, out)[0] == check_clone.FAILED
    (out / "git_log.txt").write_text("fix(shipping): raise the fee\nfeat: add the fee\n")
    transcript.write_text("/commit\n")
    verdict, why = check_clone.committed(config, out)
    assert verdict == check_clone.FAILED
    assert "skills/commit" in why


def test_a_script_passes_only_when_its_call_came_back_with_the_line_it_prints(tmp_path) -> None:
    command = "if [ -x ~/.claude/scripts/triage_run.sh ]; then ~/.claude/scripts/triage_run.sh"
    call = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}
    printed = result("FAILED (exit 1, 14 lines) full output: /tmp/run.txt", is_error=False)
    ran = check_clone.ran_script(
        probe(tmp_path, call, printed), "triage_run.sh", check_clone.TRIAGE_LINE
    )
    assert ran == (check_clone.PASSED, "")
    absent = result("triage_run.sh: No such file or directory")
    for blocks in ((call, absent), (call,), ()):
        verdict, _ = check_clone.ran_script(
            probe(tmp_path, *blocks), "triage_run.sh", check_clone.TRIAGE_LINE
        )
        assert verdict == check_clone.FAILED
    state = result("branch=main head=2de12a8 no-upstream dirty=1 main-tree=/x", is_error=False)
    assert check_clone.STATE_LINE.search(state["content"])


def test_the_installed_hook_commands_judge_a_payload(tmp_path, monkeypatch) -> None:
    """The two hooks no session can trip, run as settings.json registers them, from a copy of the
    hooks in a home folder of their own."""
    config = tmp_path / ".claude"
    shutil.copytree(REPO / "hooks", config / "hooks")
    shutil.copy(REPO / "settings.json", config / "settings.json")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert check_clone.trailing_space(config) == (check_clone.PASSED, "")
    assert check_clone.peer_reply(config) == (check_clone.PASSED, "")
    (config / "hooks" / "pre_tool_use" / "edit_trailing_space_guard" / "main.py").unlink()
    assert check_clone.trailing_space(config)[0] == check_clone.FAILED


def test_the_clone_install_runs_the_commands_the_readme_gives() -> None:
    readme = {line.strip() for line in (REPO / "README.md").read_text().splitlines()}
    script = (REPO / "tests" / "container" / "clone_install.sh").read_text().splitlines()
    commands = [line for line in script if line and not line.startswith(("#", "set "))]
    assert commands
    assert [line for line in commands if line not in readme] == []


def test_a_session_that_ended_in_an_error_result_did_not_end_normally(tmp_path) -> None:
    good = {"type": "result", "subtype": "success", "is_error": False, "result": "Done."}
    for name in check_clone.SESSIONS:
        (tmp_path / f"{name}.jsonl").write_text(json.dumps(good) + "\n")
    assert check_clone.ended(tmp_path) == (check_clone.PASSED, "")
    failed = {**good, "is_error": True, "result": "Failed to authenticate"}
    (tmp_path / "emoji.jsonl").write_text(json.dumps(failed) + "\n")
    verdict, why = check_clone.ended(tmp_path)
    assert verdict == check_clone.FAILED
    assert "emoji: Failed to authenticate" in why


def test_self_optimize_passes_only_with_a_written_eval_suite(tmp_path) -> None:
    assert check_clone.self_optimized(tmp_path)[0] == check_clone.FAILED
    suite = tmp_path / "self-optimize" / "2026-01-05-evals"
    suite.mkdir(parents=True)
    assert check_clone.self_optimized(tmp_path)[0] == check_clone.FAILED
    (suite / "case.md").write_text("case\n")
    assert check_clone.self_optimized(tmp_path) == (check_clone.PASSED, "")


def test_the_path_check_reads_the_probe_sessions_and_not_the_invented_ones(tmp_path) -> None:
    config, out = tmp_path / ".claude", tmp_path / "out"
    folder = config / "projects" / "-home-dev-shop"
    folder.mkdir(parents=True)
    out.mkdir()
    for name in check_clone.SESSIONS:
        (out / f"{name}.jsonl").write_text(json.dumps({"session_id": "probe"}) + "\n")

    def transcript(session: str, command: str) -> None:
        call = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
        record = {"type": "assistant", "message": {"content": [call]}}
        (folder / f"{session}.jsonl").write_text(json.dumps(record) + "\n")

    transcript("probe", "ls /home/dev/shop")
    transcript("invented", "cat /Users/someone/notes")
    assert check_clone.paths(config, out) == (check_clone.PASSED, "")
    transcript("probe", "cat /Users/someone/notes")
    verdict, why = check_clone.paths(config, out)
    assert verdict == check_clone.FAILED
    assert "/Users/someone/notes" in why
    (out / "selfoptimize.jsonl").write_text(json.dumps({"session_id": "unsaved"}) + "\n")
    verdict, why = check_clone.paths(config, out)
    assert (verdict, why) == (check_clone.FAILED, "no transcript for ['selfoptimize']")
