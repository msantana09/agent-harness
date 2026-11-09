import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check  # noqa: E402
import fixtures  # noqa: E402

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
    )
    assert check.main([str(config), SESSION, output]) == 0
    assert "reached step 7: yes" in capsys.readouterr().out


def test_a_path_outside_the_install_fails_the_run(tmp_path, capsys) -> None:
    config = tmp_path / ".claude"
    output = run_with(
        config, "python3 /home/dev/agent-harness-copy/x.py", "cat /Users/someone/notes"
    )
    assert check.main([str(config), SESSION, output]) == 1
    out = capsys.readouterr().out
    assert "Bash: /home/dev/agent-harness-copy/x.py" in out
    assert "Bash: /Users/someone/notes" in out


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
