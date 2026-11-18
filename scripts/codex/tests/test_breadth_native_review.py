"""Native review (`review --native`): Codex's built-in reviewer through `review/start`."""

import json
import os
import signal
import time
from pathlib import Path

import pytest
from codex_jobs import appserver, cli, mcp_server, reconcile, records, review, runner
from codex_jobs import rollout as rollouts

NATIVE_REVIEW = "The patch makes `add` subtract.\n\n- [P1] Keep addition in `add`"
TARGET = {"type": "baseBranch", "branch": "origin/trunk"}
CATALOG = {
    "fetched_at": "2026-11-08T00:00:00Z",
    "models": [
        {
            "slug": "slug-sol",
            "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high")],
        }
    ],
}


class Env:
    def __init__(self, tmp_path, fake, capsys):
        self.fake = fake
        self.capsys = capsys
        self.root = tmp_path / "jobs"
        self.sessions = tmp_path / "sessions"
        self.catalog = tmp_path / "models_cache.json"
        self.catalog.write_text(json.dumps(CATALOG))
        self.models_sh = tmp_path / "models.sh"
        self.models_sh.write_text(
            "CODEX_MODEL_SOL=slug-sol\nCODEX_MODEL_ASTRA=slug-sol\nCODEX_MODEL_TERRA=slug-sol\nCODEX_MODEL_LUNA=slug-sol\n"
        )
        self.prompt = tmp_path / "prompt.md"
        self.prompt.write_text("Review the thing.")

    def run(self, *argv):
        code = cli.main(
            list(argv),
            root=self.root,
            catalog_path=self.catalog,
            models_sh_path=self.models_sh,
            sessions_dir=self.sessions,
        )
        return code, self.capsys.readouterr().out.splitlines()

    def script(self, **turn):
        self.fake.set_script({"turn": turn})

    def sent(self, method):
        return [m for m in self.fake.received() if m.get("method") == method]

    def jobs(self):
        return records.list_jobs(self.root)

    def native_job(self, repo, **overrides):
        """A queued native review job in `repo`, with the runner not yet started."""
        job, _ = records.create_job(
            self.root,
            kind="review",
            cwd=str(repo),
            project_dir=str(repo),
            model_requested="luna",
            model_slug="gpt-6-luna",
            effort_requested="low",
            sandbox="read-only",
            timeout_seconds=60,
            output_dir=str(self.root.parent / "out"),
            codex_bin=str(self.fake.bin),
            review_target=TARGET,
            **overrides,
        )
        return self.root / job["job_id"]


@pytest.fixture
def env(tmp_path, fake_codex, capsys):
    cli_env = Env(tmp_path, fake_codex, capsys)
    yield cli_env
    for job in cli_env.jobs():
        if job["runner_pid"]:
            try:
                os.kill(job["runner_pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        if job["pgid"] and job["pgid"] > 1:
            appserver.stop_app_server(job["pgid"], grace=1.0)


def native(env, repo, *extra):
    return env.run(
        "review", "--native", "--project-dir", str(repo), "--codex-bin", str(env.fake.bin), *extra
    )


def test_native_review_leaves_the_message_as_review_md_and_no_findings(env, review_repo):
    env.script(final_answer=NATIVE_REVIEW)
    code, lines = native(env, review_repo, "--target", "base:origin/trunk")
    assert (code, lines[:-1]) == (0, []), lines
    (job,) = env.jobs()
    assert lines == [f"SUCCESS: {job['output_dir']}"]
    output = Path(job["output_dir"])
    assert (output / "review.md").read_text() == NATIVE_REVIEW + "\n"
    assert sorted(p.name for p in output.iterdir() if p.name != ".gitignore") == [
        "attestation.json",
        "result.json",
        "review.md",
    ]
    assert not (env.root / job["job_id"] / "findings.json").exists()
    result = json.loads((output / "result.json").read_text())
    assert (result["kind"], result["task_succeeded"], result["review_approved"]) == (
        "review",
        True,
        None,
    )
    # the effort the thread/start reply reported
    assert result["effort_used"] == job["effort_requested"]
    attestation = json.loads((output / "attestation.json").read_text())
    assert (attestation["exit_status"], attestation["prompt_sha256"]) == ("ok", None)
    sent = [
        e["data"]
        for e in records.read_events(env.root / job["job_id"])
        if e["kind"] == "dispatching"
    ]
    # review/start is journaled like turn/start
    assert sent == [{"thread_id": job["thread_id"], "prior_turn_ids": []}]


def test_native_review_journals_the_code_mode_calls_of_its_review_sub_thread(
    env, review_repo, write_rollout, exec_call
):
    # the reviewer runs its commands in a sub-thread whose rollout is its own file; the parent's
    # rollout starts the sub-thread's turn and ends its own, as Codex writes them
    write_rollout(
        "12-00-00",
        "thr-fake-1",
        [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-review"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-fake-1"}},
        ],
    )
    write_rollout(
        "12-00-01",
        "thr-review",
        [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-review"}},
            *exec_call("c1", "await tools.exec_command({cmd: 'git diff'})", "diff --git"),
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-review"}},
        ],
        source={"subagent": "review"},
        parent_thread_id="thr-fake-1",
    )
    env.script(final_answer=NATIVE_REVIEW)
    assert native(env, review_repo, "--target", "uncommitted")[0] == 0
    (job,) = env.jobs()
    journaled = [
        e["data"]
        for e in records.read_events(env.root / job["job_id"])
        if e["kind"] == "code_mode_calls"
    ]
    assert journaled == [
        {
            "turn_id": "turn-fake-1",
            "calls": [
                {
                    "thread_id": "thr-review",
                    "call_id": "c1",
                    "tool": "exec",
                    "code": "await tools.exec_command({cmd: 'git diff'})",
                    "output": "diff --git",
                }
            ],
        }
    ]


def test_a_rollout_archived_while_sub_threads_are_scanned_is_skipped(
    tmp_path, write_rollout, monkeypatch
):
    # Codex archives a rollout by moving it, so a file listed a moment ago can be gone
    parent = write_rollout("12-00-00", "thr-fake-1", [])
    archived = write_rollout("12-00-00", "thr-other", [])
    sub = write_rollout(
        "12-00-01", "thr-review", [], source={"subagent": "review"}, parent_thread_id="thr-fake-1"
    )
    read_meta = rollouts._session_meta

    def archived_meanwhile(path):
        if path == archived:
            path.unlink()
        return read_meta(path)

    monkeypatch.setattr(rollouts, "_session_meta", archived_meanwhile)
    assert rollouts.review_subthreads(tmp_path / "sessions", "thr-fake-1", parent) == [sub]


def test_a_rollout_cut_inside_a_character_does_not_hide_the_review_sub_thread(
    tmp_path, write_rollout
):
    # a crash can cut a rollout's last row inside a multi-byte character
    parent = write_rollout("12-00-00", "thr-fake-1", [])
    cut = write_rollout("12-00-00", "thr-other", [])
    cut.write_bytes(cut.read_bytes() + "é".encode()[:1] + b"\n")
    sub = write_rollout(
        "12-00-01", "thr-review", [], source={"subagent": "review"}, parent_thread_id="thr-fake-1"
    )
    assert rollouts.review_subthreads(tmp_path / "sessions", "thr-fake-1", parent) == [sub]


def test_a_review_sub_thread_archived_once_read_keeps_its_calls(
    tmp_path, write_rollout, exec_call, monkeypatch
):
    write_rollout("12-00-00", "thr-fake-1", [])
    sub = write_rollout(
        "12-00-01",
        "thr-review",
        [
            *exec_call("c1", "await tools.exec_command({cmd: 'git diff'})", "diff --git"),
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-review"}},
        ],
        source={"subagent": "review"},
        parent_thread_id="thr-fake-1",
    )
    read_rows = rollouts.rows

    def archived_once_read(path):
        yield from read_rows(path)
        if path == sub:
            path.unlink()  # Codex archives a rollout by moving it

    monkeypatch.setattr(rollouts, "rows", archived_once_read)
    entry = rollouts.turn_code_mode_calls(
        tmp_path / "sessions", "thr-fake-1", "turn-fake-1", native_review=True
    )
    calls = [(call["thread_id"], call["call_id"]) for call in entry["calls"]]
    assert (calls, "error" in entry) == ([("thr-review", "c1")], False)


@pytest.mark.parametrize(
    "target, wire",
    [
        ("uncommitted", {"type": "uncommittedChanges"}),
        ("base:origin/trunk", {"type": "baseBranch", "branch": "origin/trunk"}),
        ("commit:abc1234", {"type": "commit", "sha": "abc1234"}),
    ],
)
def test_native_review_sends_review_start_with_the_wire_target_and_no_turn_start(
    env, review_repo, target, wire
):
    env.script(final_answer=NATIVE_REVIEW)
    assert native(env, review_repo, "--target", target, "--effort", "low")[0] == 0
    (request,) = env.sent("review/start")
    assert request["params"] == {"threadId": "thr-fake-1", "target": wire}
    assert env.sent("turn/start") == []
    assert env.sent("thread/start")[0]["params"]["config"] == {"model_reasoning_effort": "low"}


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--native"], "--native needs --target"),
        (["--native", "--target", "bogus"], "unrecognized review target 'bogus'"),
        (["--native", "--target", "base:"], "unrecognized review target 'base:'"),
        (
            ["--native", "--target", "commit:not-a-sha"],
            "unrecognized review target 'commit:not-a-sha'",
        ),
        (["--target", "uncommitted"], "--target needs --native"),
        ([], "review needs a prompt file"),
    ],
)
def test_native_review_flags_that_do_not_fit_are_refused_before_a_job_exists(
    env, review_repo, argv, message
):
    code, lines = env.run(
        "review", "--project-dir", str(review_repo), "--codex-bin", str(env.fake.bin), *argv
    )
    assert (
        code == 1 and len(lines) == 1 and lines[0].startswith("FAILURE: ") and message in lines[0]
    )
    assert env.jobs() == []


def test_native_review_takes_no_prompt_file(env, review_repo):
    code, lines = native(env, review_repo, "--target", "uncommitted", str(env.prompt))
    assert code == 1 and "takes no prompt file" in lines[0]
    assert env.jobs() == []


def test_codex_review_tool_passes_the_native_flags_and_no_prompt(tmp_path, monkeypatch):
    server = mcp_server.Server(
        root=tmp_path / "jobs",
        catalog_path=tmp_path / "cache.json",
        models_sh_path=tmp_path / "models.sh",
        sessions_dir=tmp_path / "s",
    )
    seen = []
    monkeypatch.setattr(
        server, "_run_cli", lambda argv: (seen.append(argv), (2, ["FAILURE: stopped here"]))[1]
    )
    arguments = {
        "native": True,
        "target": "base:main",
        "project_dir": str(tmp_path),
        "model": "luna",
        "effort": "low",
    }
    with server:
        refused = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "codex_review", "arguments": {**arguments, "prompt": "x"}},
            }
        )
        assert (
            seen == []
            and refused["result"]["isError"] is True
            and "no prompt" in refused["result"]["content"][0]["text"]
        )
        server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "codex_review", "arguments": arguments},
            }
        )
    (argv,) = seen
    assert (
        argv[:3] == ["review", "--detach", "--native"]
        and "--target=base:main" in argv
        and argv[-1] == "--"
    )


def test_a_native_review_of_uncommitted_changes_succeeds_but_never_satisfies_the_gate(
    env, review_repo
):
    env.script(final_answer=NATIVE_REVIEW)
    assert native(env, review_repo, "--target", "uncommitted")[0] == 0
    (job,) = env.jobs()
    status = json.loads((Path(job["output_dir"]) / "attestation.json").read_text())["exit_status"]
    assert status.startswith("failure: not attestable: a native review of uncommittedChanges")


def test_a_native_review_settled_after_its_runner_died_is_recovered_from_the_thread(
    env, review_repo
):
    env.script(final_answer=NATIVE_REVIEW)
    job_dir = env.native_job(review_repo)
    assert runner.spawn_runner(job_dir, crash_at="after-complete").wait(timeout=60) == 1
    reconcile.reconcile_job(env.root, job_dir.name, sessions_dir=env.sessions)
    deadline = time.monotonic() + 30
    while records.load_job(env.root, job_dir.name)["state"] not in records.TERMINAL_STATES:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    output = env.root.parent / "out"
    assert records.load_job(env.root, job_dir.name)["state"] == records.SUCCEEDED
    assert (output / "review.md").read_text() == NATIVE_REVIEW + "\n"
    assert not (output / "findings.json").exists()
    assert json.loads((output / "attestation.json").read_text())["exit_status"] == "ok"
    assert env.sent("turn/start") == [] and len(env.sent("review/start")) == 1


def test_a_native_review_that_ends_without_a_message_fails(env, review_repo):
    env.fake.set_script({"turn": {}})
    job_dir = env.native_job(review_repo)
    assert runner.spawn_runner(job_dir).wait(timeout=60) == 1
    job = records.load_job(env.root, job_dir.name)
    output = env.root.parent / "out"
    assert (job["state"], job["error"]) == (records.FAILED, "the review ended without a message")
    assert not (output / "review.md").exists()
    assert (
        json.loads((output / "attestation.json").read_text())["exit_status"]
        == "failure: the review ended without a message"
    )


# The review sub-thread's last message and the text the server delivered for the same review,
# from codex 0.167.
REVIEW_OUTPUT = {
    "findings": [
        {
            "title": "[P1] Preserve addition semantics in `add`",
            "body": (
                "For any caller that uses `add(a, b)`, this change now subtracts `b`.\n"
                "Restore addition."
            ),
            "confidence_score": 1.0,
            "priority": 1,
            "code_location": {
                "absolute_file_path": "/r/calc.py",
                "line_range": {"start": 2, "end": 2},
            },
        },
        {
            "title": "[P1] Pass commands safely to the shell",
            "body": "Concatenating `cmd` lets shell syntax execute.",
            "confidence_score": 0.99,
            "priority": 1,
            "code_location": {
                "absolute_file_path": "/r/util.py",
                "line_range": {"start": 4, "end": 6},
            },
        },
    ],
    "overall_correctness": "patch is incorrect",
    "overall_explanation": "Both issues are visible in the changed code.",
    "overall_confidence_score": 0.99,
}
REVIEW_PROSE = (
    "Both issues are visible in the changed code.\n\nFull review comments:\n\n"
    "- [P1] Preserve addition semantics in `add` \u2014 /r/calc.py:2-2\n"
    "  For any caller that uses `add(a, b)`, this change now subtracts `b`.\n"
    "  Restore addition.\n\n"
    "- [P1] Pass commands safely to the shell \u2014 /r/util.py:4-6\n"
    "  Concatenating `cmd` lets shell syntax execute."
)
CLEAN_OUTPUT = {
    "findings": [],
    "overall_correctness": "patch is correct",
    "overall_explanation": "Only a comment changed.",
    "overall_confidence_score": 1.0,
}


def test_review_output_renders_as_the_prose_the_server_delivers():
    one = {
        **REVIEW_OUTPUT,
        "findings": REVIEW_OUTPUT["findings"][:1],
        "overall_explanation": "One issue.",
    }
    assert review.render_review_output(REVIEW_OUTPUT) == REVIEW_PROSE
    assert review.render_review_output(one).startswith(
        "One issue.\n\nReview comment:\n\n- [P1] Preserve"
    )
    assert review.render_review_output(CLEAN_OUTPUT) == "Only a comment changed."


def rollout(day, thread_id, rows, *, source=None, parent=None):
    meta = {"id": thread_id, **({"source": source, "parent_thread_id": parent} if source else {})}
    rows = [{"type": "session_meta", "payload": meta}, *rows]
    (day / f"rollout-2026-11-08T12-00-00-{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )


def complete(turn_id, message):
    return {
        "type": "event_msg",
        "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": message},
    }


@pytest.mark.parametrize(
    "subthreads, state, expected",
    [
        (1, "succeeded", None),
        (0, "outcome_unknown", "no review sub-thread rollout"),
        (2, "outcome_unknown", "2 review sub-thread rollouts"),
    ],
)
def test_a_native_review_with_no_server_is_recovered_from_its_sub_thread_rollout(
    env, review_repo, subthreads, state, expected
):
    env.script(hang=True)
    job_dir = env.native_job(review_repo)
    assert runner.spawn_runner(job_dir, crash_at="after-accept").wait(timeout=60) == 1
    crashed = records.load_job(env.root, job_dir.name)
    os.killpg(crashed["pgid"], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while True:
        try:
            os.killpg(crashed["pgid"], 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pass
        assert time.monotonic() < deadline
        time.sleep(0.02)
    day = env.sessions / "2026" / "11" / "08"
    day.mkdir(parents=True)
    rollout(day, crashed["thread_id"], [complete(crashed["turn_id"], None)])
    for n in range(subthreads):
        rollout(
            day,
            f"sub-{n}",
            [complete(f"inner-{n}", json.dumps(REVIEW_OUTPUT))],
            source={"subagent": "review"},
            parent=crashed["thread_id"],
        )
    rollout(
        day,
        "other-sub",
        [complete("x", "{}")],
        source={"subagent": "review"},
        parent="some-other-thread",
    )
    job = reconcile.reconcile_job(env.root, job_dir.name, sessions_dir=env.sessions)
    output = env.root.parent / "out"
    assert job["state"] == state
    if expected is None:
        assert job["recovered_via"] == "rollout"
        assert (output / "review.md").read_text() == REVIEW_PROSE + "\n"
        assert not (output / "findings.json").exists()
    else:
        assert expected in job["error"] and crashed["thread_id"] in job["error"]
        assert not (output / "review.md").exists()
