"""codex-jobs CLI against the fake Codex: stdout lines, exit codes, parity rows and job control."""

import ast
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from codex_jobs import admission, appserver, breadth, cli, reconcile, records, runner, worktrees

CODEX_DIR = Path(__file__).resolve().parent.parent
ANSWER = json.dumps({"status": "completed", "answer": "the answer", "open_questions": []})
REVIEW = json.dumps(
    {
        "status": "completed",
        "review": "## Summary\nClean.\n\n## Review Findings\nNone.\n",
        "verdict": "approve",
        "summary": "No findings.",
        "findings": [],
    }
)
TASK = json.dumps(
    {"status": "completed", "summary": "done", "changed_files": [], "tests_run": [], "blockers": []}
)
GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
EFFORTS = ["low", "medium", "high", "xhigh"]
CATALOG = {
    "fetched_at": "2026-11-08T00:00:00Z",
    "models": [
        {"slug": slug, "supported_reasoning_levels": [{"effort": effort} for effort in efforts]}
        for slug, efforts in {
            "slug-sol": EFFORTS,
            "slug-astra": EFFORTS,
            "slug-terra": EFFORTS[:3],
            "gpt-6-luna": EFFORTS[:3],
        }.items()
    ],
}
MODELS_SH = (
    "# fixture\nCODEX_MODEL_SOL=slug-sol\nCODEX_MODEL_ASTRA=slug-astra\n"
    "CODEX_MODEL_TERRA=slug-terra\nCODEX_MODEL_LUNA=gpt-6-luna\n"
)


class CliEnv:
    def __init__(self, tmp_path, fake, capsys):
        self.fake = fake
        self.capsys = capsys
        self.root = tmp_path / "jobs"
        self.sessions = tmp_path / "sessions"
        self.catalog = tmp_path / "models_cache.json"
        self.catalog.write_text(json.dumps(CATALOG))
        self.models_sh = tmp_path / "models.sh"
        self.models_sh.write_text(MODELS_SH)
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.prompt = tmp_path / "prompt.md"
        self.prompt.write_text("Review the thing.")

    def run(self, *argv: str) -> tuple[int, list[str]]:
        code = cli.main(
            list(argv),
            root=self.root,
            catalog_path=self.catalog,
            models_sh_path=self.models_sh,
            sessions_dir=self.sessions,
        )
        return code, self.capsys.readouterr().out.splitlines()

    def consult(self, *extra: str, kind: str = "consult") -> tuple[int, list[str]]:
        return self.run(
            kind,
            str(self.prompt),
            "--project-dir",
            str(self.project),
            "--codex-bin",
            str(self.fake.bin),
            *extra,
        )

    def script(self, **turn) -> None:
        self.fake.set_script({"turn": turn})

    def start_detached(self, *extra: str) -> str:
        code, lines = self.consult("--detach", *extra)
        assert code == 0 and len(lines) == 1 and lines[0].startswith("JOB: "), lines
        return lines[0].removeprefix("JOB: ")

    def sent(self, method: str) -> list[dict]:
        return [m for m in self.fake.received() if m.get("method") == method]

    def jobs(self) -> list[dict]:
        return records.list_jobs(self.root)

    def wait_for_turn(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = records.load_job(self.root, job_id)
            if job["turn_id"]:
                return job
            time.sleep(0.05)
        raise AssertionError(
            f"job {job_id} never dispatched a turn: {records.load_job(self.root, job_id)}"
        )


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *GIT_IDENTITY, *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A main checkout on `trunk` whose bare origin has HEAD set."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "trunk", str(origin)], check=True)
    main = tmp_path / "proj"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(main)], check=True)
    (main / "a.txt").write_text("a\n")
    git(main, "add", "a.txt")
    git(main, "commit", "-q", "-m", "init")
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "-q", "origin", "trunk")
    git(main, "remote", "set-head", "origin", "trunk")
    return main.resolve()


@pytest.fixture
def env(tmp_path, fake_codex, capsys):
    cli_env = CliEnv(tmp_path, fake_codex, capsys)
    yield cli_env
    for job in cli_env.jobs():
        if job["runner_pid"]:
            try:
                os.kill(job["runner_pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        if job["pgid"] and job["pgid"] > 1:
            appserver.stop_app_server(job["pgid"], grace=1.0)


def test_consult_wait_prints_success_line_and_writes_answer(env):
    env.script(final_answer=ANSWER)
    code, lines = env.consult()
    (job,) = env.jobs()
    assert (code, lines) == (0, [f"SUCCESS: {job['output_dir']}"])
    output = Path(job["output_dir"])
    result = json.loads((output / "result.json").read_text())
    assert result["state"] == "succeeded"
    assert result["catalog_fetched_at"] == "2026-11-08T00:00:00Z"
    assert (output / "answer.md").read_text().strip() == "the answer"


def test_detach_prints_job_and_wait_reports_state(env):
    env.script(final_answer=ANSWER)
    job_id = env.start_detached()
    assert env.run("wait", job_id, "--max-seconds", "30") == (0, [f"STATE: {job_id} succeeded"])


def test_wait_fails_when_codex_reports_the_task_did_not_complete(env):
    env.script(
        final_answer=json.dumps({"status": "blocked", "answer": "no access", "open_questions": []})
    )
    job_id = env.start_detached()
    code, lines = env.run("wait", job_id, "--max-seconds", "30")
    (job,) = env.jobs()
    assert (code, lines) == (
        1,
        [
            f"STATE: {job_id} succeeded",
            f"FAILURE: codex reported the task did not complete (job {job_id}; see {job['output_dir']})",
        ],
    )


def test_wait_in_the_spawning_process_recovers_a_lost_turn_start_reply(env):
    # The runner is this process's child, so once it exits unreaped its pid still answers kill(0).
    env.fake.set_script({"drop_reply": ["turn/start"], "turn": {"final_answer": ANSWER}})
    job_id = env.start_detached()
    assert env.run("wait", job_id, "--max-seconds", "30") == (0, [f"STATE: {job_id} succeeded"])
    assert records.load_job(env.root, job_id)["recovered_via"] == "thread/read"


def test_wait_on_a_running_turn_prints_running_and_exits_75(env):
    env.script(hang=True)
    job_id = env.start_detached()
    code, lines = env.run("wait", job_id, "--max-seconds", "1")
    assert code == 75
    assert len(lines) == 1 and lines[0].startswith(f"RUNNING: {job_id} ")


def test_unlisted_effort_fails_before_any_job_exists(env):
    code, lines = env.consult("--model", "luna", "--effort", "ultra")
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("FAILURE:")
    assert "gpt-6-luna" in lines[0] and "low, medium, high" in lines[0]
    assert not env.root.exists()
    assert env.fake.records() == []


def test_idempotency_key_reattaches_without_a_second_runner(env):
    env.script(final_answer=ANSWER)
    first = env.start_detached("--idempotency-key", "k1")
    assert env.consult("--idempotency-key", "k1") == (0, [f"JOB: {first}"])
    assert len(env.jobs()) == 1
    assert env.run("wait", first, "--max-seconds", "30")[0] == 0
    assert len(env.fake.records("start")) == 1


def test_output_dir_outside_artifacts_is_refused(env, tmp_path):
    for target in (tmp_path / "elsewhere", env.project / ".artifacts"):
        code, lines = env.consult("--output-dir", str(target))
        assert code == 1 and lines[0].startswith("FAILURE: refusing --output-dir outside")
    assert not env.root.exists()


def test_output_dir_symlink_escape_is_refused(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (env.project / ".artifacts").mkdir()
    (env.project / ".artifacts" / "link").symlink_to(outside)
    code, lines = env.consult("--output-dir", str(env.project / ".artifacts" / "link" / "sub"))
    assert code == 1 and lines[0].startswith("FAILURE: refusing --output-dir outside")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("session_id, parent", [("abcdef123456", "abcdef12"), (None, None)])
def test_auto_output_dir_layout_and_gitignore(env, monkeypatch, session_id, parent):
    if session_id:
        monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    else:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    env.script(final_answer=ANSWER)
    assert env.consult()[0] == 0
    output = Path(env.jobs()[0]["output_dir"])
    base = env.project / ".artifacts" / "codex" / (parent or "")
    assert output.parent == base and output.name.startswith("analysis-")
    assert (output / ".gitignore").read_text().strip() == "*"


def test_explicit_output_dir_inside_artifacts_is_used(env):
    env.script(final_answer=ANSWER)
    target = env.project / ".artifacts" / "mine" / "run1"
    code, lines = env.consult("--output-dir", str(target))
    assert (code, lines) == (0, [f"SUCCESS: {target.resolve()}"])
    assert (target / "answer.md").exists() and (target / ".gitignore").read_text().strip() == "*"


def test_project_dir_defaults_to_git_root(env, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo / "sub")
    env.script(final_answer=ANSWER)
    code, lines = env.run("consult", str(env.prompt), "--codex-bin", str(env.fake.bin), "--detach")
    assert code == 0
    job = records.load_job(env.root, lines[0].removeprefix("JOB: "))
    assert Path(job["project_dir"]).resolve() == repo.resolve()


def test_non_git_cwd_is_the_threads_cwd(env, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    env.script(final_answer=ANSWER)
    code, _ = env.run("consult", str(env.prompt), "--codex-bin", str(env.fake.bin))
    assert code == 0
    (start,) = [m for m in env.fake.received() if m.get("method") == "thread/start"]
    assert Path(start["params"]["cwd"]).resolve() == plain.resolve()


def test_timeout_fails_the_job_and_sends_one_turn(env):
    env.script(hang=True)
    code, lines = env.consult("--timeout", "1")
    (job,) = env.jobs()
    assert code == 1 and lines[0].startswith("FAILURE: timeout after 1s")
    assert job["state"] == "timed_out"
    assert len([m for m in env.fake.received() if m.get("method") == "turn/start"]) == 1


def test_codex_bin_resolves_through_symlinks_and_env(env, tmp_path, monkeypatch):
    link = tmp_path / "linked-codex"
    link.symlink_to(env.fake.bin)
    monkeypatch.setenv("CODEX_JOBS_BIN", str(link))
    env.script(final_answer=ANSWER)
    code, lines = env.run("consult", str(env.prompt), "--project-dir", str(env.project), "--detach")
    assert code == 0
    assert records.load_job(env.root, lines[0].removeprefix("JOB: "))["codex_bin"] == str(
        env.fake.bin.resolve()
    )


def test_missing_codex_bin_and_missing_image_fail_before_submit(env, tmp_path):
    missing_bin = env.run(
        "consult",
        str(env.prompt),
        "--project-dir",
        str(env.project),
        "--codex-bin",
        str(tmp_path / "nope"),
    )
    missing_image = env.consult("--image", str(tmp_path / "nope.png"))
    assert missing_bin[0] == missing_image[0] == 1
    assert missing_bin[1][0].startswith("FAILURE: Codex executable not found")
    assert missing_image[1][0].startswith("FAILURE: image file not found")
    assert not env.root.exists()


def test_empty_image_value_is_refused_with_its_own_message(env):
    code, lines = env.consult("--image", "")
    assert code == 1
    assert lines[0] == "FAILURE: --image needs a file path, got an empty value"
    assert not env.root.exists()


def test_cancel_marks_the_job_cancelled(env):
    env.script(hang=True)
    job_id = env.start_detached()
    env.wait_for_turn(job_id)
    assert env.run("cancel", job_id) == (0, [f"STATE: {job_id} cancelled"])
    job = records.load_job(env.root, job_id)
    assert job["state"] == "cancelled" and job["cancel_requested_at"]


def test_cancel_leaves_a_reused_process_group_alone(env, monkeypatch):
    env.script(hang=True)
    job_id = env.start_detached()
    own_pgid = env.wait_for_turn(job_id)["pgid"]
    unrelated = env.fake.launch(name="someone-else")  # its socket is not the job's
    records.update_job(env.root, job_id, pgid=unrelated.pgid)
    monkeypatch.setattr(cli, "CANCEL_GRACE_SECONDS", 0)
    monkeypatch.setattr(cli, "CANCEL_SETTLE_SECONDS", 0)
    env.run("cancel", job_id)
    try:
        os.killpg(unrelated.pgid, 0)
    finally:
        records.update_job(env.root, job_id, pgid=own_pgid)


def test_steer_sends_turn_steer_with_the_expected_turn_id(env):
    env.script(hang=True)
    job_id = env.start_detached()
    job = env.wait_for_turn(job_id)
    code, lines = env.run("steer", job_id, "focus on the tests")
    assert code == 0 and lines[0].startswith(f"STATE: {job_id} ")
    (steer,) = [m for m in env.fake.received() if m.get("method") == "turn/steer"]
    assert steer["params"]["expectedTurnId"] == job["turn_id"]
    assert steer["params"]["threadId"] == job["thread_id"]
    assert steer["params"]["input"] == [{"type": "text", "text": "focus on the tests"}]
    assert "steered" in [e["kind"] for e in records.read_events(env.root / job_id)]


def test_interrupt_journals_and_ends_the_turn(env):
    env.script(hang=True)
    job_id = env.start_detached()
    env.wait_for_turn(job_id)
    assert env.run("interrupt", job_id)[0] == 0
    assert "interrupted_by_client" in [e["kind"] for e in records.read_events(env.root / job_id)]
    assert env.run("wait", job_id, "--max-seconds", "30") == (1, [f"STATE: {job_id} interrupted"])


def review_in(env, repo, *extra: str) -> tuple[int, list[str]]:
    return env.run(
        "review",
        str(env.prompt),
        "--project-dir",
        str(repo),
        "--codex-bin",
        str(env.fake.bin),
        *extra,
    )


def test_review_delivers_its_files_and_prints_the_output_dir(env, review_repo):
    env.script(final_answer=REVIEW)
    code, lines = review_in(env, review_repo)
    (job,) = env.jobs()
    assert (code, lines) == (0, [f"SUCCESS: {job['output_dir']}"])
    assert (job["kind"], job["sandbox"], job["model_requested"], job["effort_requested"]) == (
        "review",
        "read-only",
        "sol",
        "high",
    )
    output = Path(job["output_dir"])
    assert output.is_relative_to(
        review_repo / ".artifacts" / "codex"
    )  # where a gate looks for attestation.json
    assert sorted(p.name for p in output.iterdir() if p.name != ".gitignore") == [
        "attestation.json", "findings.json", "findings.md", "result.json", "review.md",
    ]  # fmt: skip
    assert json.loads((output / "attestation.json").read_text())["exit_status"] == "ok"
    assert json.loads((output / "result.json").read_text())["review_approved"] is True


def test_blocked_review_exits_nonzero_with_the_reason_and_keeps_its_files(env, review_repo):
    env.script(
        final_answer=json.dumps(
            {**json.loads(REVIEW), "status": "blocked", "verdict": "needs-attention"}
        )
    )
    code, lines = review_in(env, review_repo)
    (job,) = env.jobs()
    failure = f"FAILURE: review blocked (job {job['job_id']}; see {job['output_dir']})"
    assert (code, lines) == (1, [failure])
    assert (Path(job["output_dir"]) / "review.md").exists()
    assert env.run("wait", job["job_id"], "--max-seconds", "30") == (
        1,
        [f"STATE: {job['job_id']} succeeded", failure],
    )


def test_capabilities_lists_tiers_models_and_efforts(env):
    code, lines = env.run("capabilities", "--codex-bin", str(env.fake.bin))
    caps = json.loads("\n".join(lines))
    assert code == 0
    assert caps["kinds"] == ["review", "consult", "run"] and "kinds_not_enabled" not in caps
    assert caps["tiers"] == {
        "luna": "gpt-6-luna",
        "terra": "slug-terra",
        "sol": "slug-sol",
        "astra": "slug-astra",
    }
    assert caps["models"]["slug-sol"] == EFFORTS and caps["models"]["gpt-6-luna"] == EFFORTS[:3]
    assert {
        "consult",
        "run",
        "resume",
        "fork",
        "status",
        "result",
        "wait",
        "cancel",
        "steer",
        "interrupt",
        "release",
        "reconcile",
        "capabilities",
    } <= set(caps["subcommands"])


def test_status_lists_jobs_and_prints_one_record(env):
    assert env.run("status") == (0, [])
    env.script(final_answer=ANSWER)
    assert env.consult()[0] == 0
    (job,) = env.jobs()
    code, lines = env.run("status")
    assert code == 0 and len(lines) == 1
    assert lines[0].split("\t")[:4] == [job["job_id"], "consult", "succeeded", job["model_used"]]
    code, lines = env.run("status", job["job_id"])
    assert (code, json.loads("\n".join(lines))) == (0, records.load_job(env.root, job["job_id"]))


def test_result_prints_the_envelope_or_fails(env):
    env.script(final_answer=ANSWER)
    assert env.consult()[0] == 0
    (job,) = env.jobs()
    code, lines = env.run("result", job["job_id"])
    assert code == 0 and json.loads("\n".join(lines))["job_id"] == job["job_id"]
    code, lines = env.run("result", "cj-20990101-000000-abcd")
    assert code == 1 and lines[0].startswith("FAILURE: no job record")


def test_image_is_stored_absolute_and_sent_as_user_input(env):
    (env.project / "shot.png").write_bytes(b"\x89PNG")
    env.script(final_answer=ANSWER)
    assert env.consult("--image", "shot.png")[0] == 0
    (job,) = env.jobs()
    assert job["images"] == [str(env.project / "shot.png")]
    (start,) = env.sent("turn/start")
    assert {"type": "localImage", "path": job["images"][0]} in start["params"]["input"]


def test_resume_and_fork_record_their_source(env):
    env.fake.set_script(
        {
            "turn": {"final_answer": ANSWER},
            "responses": {
                "thread/resume": {"thread": {"id": "thr-fake-1"}},
                "thread/fork": {"thread": {"id": "thr-fake-2"}},
            },
        }
    )
    first = env.start_detached()
    env.run("wait", first, "--max-seconds", "30")
    thread_id = records.load_job(env.root, first)["thread_id"]
    code, lines = env.run(
        "resume",
        first,
        str(env.prompt),
        "--project-dir",
        str(env.project),
        "--codex-bin",
        str(env.fake.bin),
        "--detach",
    )
    resumed = records.load_job(env.root, lines[0].removeprefix("JOB: "))
    assert code == 0 and (
        resumed["source_mode"],
        resumed["source_thread_id"],
        resumed["parent_job_id"],
    ) == ("resume", thread_id, first)
    code, lines = env.run(
        "fork",
        thread_id,
        str(env.prompt),
        "--project-dir",
        str(env.project),
        "--codex-bin",
        str(env.fake.bin),
        "--detach",
    )
    forked = records.load_job(env.root, lines[0].removeprefix("JOB: "))
    assert code == 0 and (
        forked["source_mode"],
        forked["source_thread_id"],
        forked["parent_job_id"],
    ) == ("fork", thread_id, None)


def test_launcher_runs_capabilities_from_a_symlinked_path(tmp_path, fake_codex):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "models_cache.json").write_text(json.dumps(CATALOG))
    link = tmp_path / "bin" / "codex-jobs"
    link.parent.mkdir()
    link.symlink_to(CODEX_DIR / "codex-jobs")
    done = subprocess.run(
        [str(link), "capabilities", "--codex-bin", str(fake_codex.bin)],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    caps = json.loads(done.stdout)
    assert caps["kinds"] == ["review", "consult", "run"] and caps["models"]["slug-sol"] == EFFORTS


def test_jobs_live_in_the_user_data_folder(tmp_path, monkeypatch):
    from codex_jobs.__main__ import job_root

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert job_root() == tmp_path / "data" / "agent-harness" / "codex-jobs"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert job_root() == tmp_path / "home" / ".local" / "share" / "agent-harness" / "codex-jobs"


def run_in(env, repo, *extra: str) -> tuple[int, list[str]]:
    base = ["run", str(env.prompt), "--project-dir", str(repo), "--codex-bin", str(env.fake.bin)]
    return env.run(*base, "--model", "luna", "--effort", "low", *extra)


@pytest.mark.parametrize(
    "given, missing", [(["--effort", "low"], "--model"), (["--model", "luna"], "--effort")]
)
def test_run_requires_an_explicit_model_and_effort(env, repo, given, missing):
    code, lines = env.run(
        "run", str(env.prompt), "--project-dir", str(repo), "--branch", "codex/x", *given
    )
    assert code == 2 and lines == [f"FAILURE: the following arguments are required: {missing}"]
    assert not env.root.exists()


def test_run_needs_exactly_one_of_branch_and_worktree(env, repo):
    code, lines = run_in(env, repo)
    assert code == 2 and "one of the arguments --branch --worktree is required" in lines[0]
    code, lines = run_in(env, repo, "--branch", "codex/a", "--worktree", str(repo))
    assert code == 2 and "not allowed with argument" in lines[0]
    code, lines = env.run(
        "run", str(env.prompt), "--branch", "codex/a", "--model", "luna", "--effort", "low"
    )
    assert (code, lines) == (
        1,
        ["FAILURE: run --branch needs --project-dir: the repo to create the worktree in"],
    )
    assert not env.root.exists()


def test_run_in_a_service_made_worktree_reports_what_git_saw(env, repo):
    env.script(final_answer=TASK, final_delay=1.5)
    code, lines = run_in(env, repo, "--branch", "codex/s3-probe", "--detach")
    assert code == 0
    job_id = lines[0].removeprefix("JOB: ")
    job = env.wait_for_turn(job_id)
    worktree = repo / ".claude" / "worktrees" / "s3-probe"
    assert (job["kind"], job["worktree"], job["cwd"], job["branch"]) == (
        "run",
        str(worktree),
        str(worktree),
        "codex/s3-probe",
    )
    assert (
        job["base_commit"] == git(repo, "rev-parse", "origin/trunk").strip()
        and job["allow_commit"] is False
    )
    (worktree / "a.txt").write_text("edited\n")
    (worktree / "sub").mkdir()
    (worktree / "sub" / "new.txt").write_text("new\n")
    assert env.run("wait", job_id, "--max-seconds", "30") == (0, [f"STATE: {job_id} succeeded"])
    result = json.loads((env.root / job_id / "result.json").read_text())
    porcelain = [
        line[3:]
        for line in git(worktree, "status", "--porcelain", "--untracked-files=all").splitlines()
    ]
    assert result["changed_files"] == porcelain == ["a.txt", "sub/new.txt"]
    assert (result["worktree"], result["branch"], result["commits"]) == (
        str(worktree),
        "codex/s3-probe",
        [],
    )
    (start,) = env.sent("thread/start")
    assert (start["params"]["cwd"], start["params"]["sandbox"]) == (
        str(worktree),
        "workspace-write",
    )


def test_allow_commit_permits_commits_and_lists_them(env, repo):
    env.script(final_answer=TASK, final_delay=1.5)
    code, lines = run_in(env, repo, "--branch", "codex/commits", "--allow-commit", "--detach")
    job_id = lines[0].removeprefix("JOB: ")
    job = env.wait_for_turn(job_id)
    assert job["allow_commit"] is True
    (turn,) = env.sent("turn/start")
    assert (
        "\n\nYou may commit your work to the current branch." in turn["params"]["input"][0]["text"]
    )
    worktree = Path(job["worktree"])
    (worktree / "a.txt").write_text("committed\n")
    git(worktree, "commit", "-q", "-am", "work")
    assert env.run("wait", job_id, "--max-seconds", "30")[0] == 0
    result = json.loads((env.root / job_id / "result.json").read_text())
    assert (
        result["commits"] == [git(worktree, "rev-parse", "HEAD").strip()]
        and result["changed_files"] == []
    )


@pytest.mark.parametrize("branch", ["main", "trunk"])
def test_worktree_refusal_fails_the_job_before_any_runner_spawns(env, repo, branch):
    code, lines = run_in(env, repo, "--branch", branch)
    (job,) = env.jobs()
    assert code == 1 and lines == [f"FAILURE: {job['error']}"]
    assert "a writing job needs its own branch" in job["error"]
    assert (job["state"], job["runner_pid"], job["pgid"]) == ("failed", None, None)
    assert env.fake.records("start") == []
    assert not (repo / ".claude").exists()


def test_reconcile_during_a_preparation_past_the_grace_leaves_the_job_to_its_submitter(
    env, repo, monkeypatch
):
    monkeypatch.setattr(runner, "NEW_JOB_GRACE_SECONDS", 0.0)
    real_create = worktrees.create_worktree
    seen = []

    def slow_create(root, job_id, project_dir, branch):
        # Preparation outlasts the grace; a concurrent reconcile looks at the job meanwhile.
        seen.extend(
            job["state"] for job in reconcile.reconcile_all(env.root, sessions_dir=env.sessions)
        )
        return real_create(root, job_id, project_dir, branch)

    monkeypatch.setattr(worktrees, "create_worktree", slow_create)
    env.script(final_answer=TASK)
    code, lines = run_in(env, repo, "--branch", "codex/slow-prep")
    (job,) = env.jobs()
    assert seen == ["queued"]
    assert (code, lines) == (0, [f"SUCCESS: {job['output_dir']}"])


def test_a_job_settled_during_preparation_fails_the_submission_without_a_runner(
    env, repo, monkeypatch
):
    real_create = worktrees.create_worktree

    def create_then_settle(root, job_id, project_dir, branch):
        tree = real_create(root, job_id, project_dir, branch)
        records.transition(root, job_id, records.FAILED, error="settled elsewhere")
        return tree

    monkeypatch.setattr(worktrees, "create_worktree", create_then_settle)
    code, lines = run_in(env, repo, "--branch", "codex/settled", "--detach")
    (job,) = env.jobs()
    assert (code, lines) == (1, [f"FAILURE: job {job['job_id']} failed: settled elsewhere"])
    assert job["runner_pid"] is None and env.fake.records("start") == []


def test_second_run_on_a_leased_worktree_is_refused_and_release_ends_the_lease(env, repo):
    env.script(hang=True)
    code, lines = run_in(env, repo, "--branch", "codex/lease", "--detach")
    first = lines[0].removeprefix("JOB: ")
    worktree = Path(env.wait_for_turn(first)["worktree"])
    (worktree / "a.txt").write_text("the first job's edit\n")
    code, lines = run_in(env, repo, "--worktree", str(worktree), "--detach")
    assert code == 1 and lines == [f"FAILURE: {worktree} is leased by {first}"]
    assert len(env.fake.records("start")) == 1
    code, lines = env.run("release", first)
    assert code == 1 and lines == [
        f"FAILURE: job {first} is running; cancel it or wait for it to finish before releasing"
    ]
    assert env.run("cancel", first) == (0, [f"STATE: {first} cancelled"])
    git(worktree, "checkout", "-q", "--", "a.txt")
    assert env.run("release", first) == (0, [str(worktree)])
    assert not worktree.exists()
    assert env.run("release", first)[1] == [f"FAILURE: job {first} holds no worktree lease"]


def test_release_keeps_a_caller_worktree(env, repo):
    worktree = repo / ".claude" / "worktrees" / "mine"
    git(repo, "worktree", "add", "-q", "-b", "codex/mine", str(worktree), "trunk")
    env.script(final_answer=TASK)
    code, lines = run_in(env, repo, "--worktree", str(worktree))
    (job,) = env.jobs()
    assert (code, lines) == (0, [f"SUCCESS: {job['output_dir']}"])
    assert (job["worktree"], job["branch"]) == (str(worktree.resolve()), "codex/mine")
    assert env.run("release", job["job_id"]) == (0, [str(worktree.resolve())])
    assert worktree.is_dir()


def test_fork_of_a_run_job_is_read_only(env, repo):
    env.fake.set_script({"turn": {"final_answer": TASK}, "fork_thread_id": "thr-fake-2"})
    assert run_in(env, repo, "--branch", "codex/forked")[0] == 0
    (first,) = env.jobs()
    code, lines = env.run(
        "fork",
        first["job_id"],
        str(env.prompt),
        "--project-dir",
        str(repo),
        "--codex-bin",
        str(env.fake.bin),
        "--wait",
    )
    assert (
        code == 1
    )  # the fake answers the fork turn with the task envelope, which the consult schema rejects
    (forked,) = [job for job in env.jobs() if job["parent_job_id"] == first["job_id"]]
    assert (forked["kind"], forked["sandbox"], forked["worktree"]) == ("consult", "read-only", None)
    (opened,) = env.sent("thread/fork")
    assert opened["params"]["sandbox"] == "read-only"
    (_, fork_turn) = env.sent("turn/start")
    assert fork_turn["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": True}


def over_reserve_script() -> dict:
    window = {"usedPercent": 90, "windowDurationMins": 10080, "resetsAt": 1794800000}
    return {
        "responses": {
            "account/rateLimits/read": {
                "ordinaryUsageAllowed": True,
                "rateLimits": {"primary": window},
            }
        }
    }


def test_usage_refusal_prints_the_typed_failure_on_start_wait_and_result(env):
    env.fake.set_script(over_reserve_script())
    resets = admission.iso_utc(1794800000)
    failure = f"FAILURE: usage limit (resets {resets})"
    assert env.consult() == (1, [failure])
    (job,) = env.jobs()
    code, lines = env.run("wait", job["job_id"], "--max-seconds", "30")
    assert (code, lines) == (1, [f"STATE: {job['job_id']} failed", failure])
    code, lines = env.run("result", job["job_id"])
    assert code == 1 and lines[-1] == failure


def test_a_usage_limit_hit_mid_turn_prints_the_same_typed_failure(env):
    window = {"usedPercent": 50, "windowDurationMins": 10080, "resetsAt": 1794800000}
    error = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
    env.fake.set_script(
        {
            "responses": {
                "account/rateLimits/read": {
                    "ordinaryUsageAllowed": True,
                    "rateLimits": {"primary": window},
                }
            },
            "turn": {"status": "failed", "error": error},
        }
    )
    assert env.consult() == (1, [f"FAILURE: usage limit (resets {admission.iso_utc(1794800000)})"])


def test_detach_returns_the_job_at_once_while_it_queues_behind_four_holders(env, hold_slots):
    hold_slots(env.root, 4)
    started = time.monotonic()
    job_id = env.start_detached()
    assert time.monotonic() - started < 10
    assert records.load_job(env.root, job_id)["state"] == "queued"


@pytest.mark.parametrize(
    "script, message",
    [
        ({"startup_fail": "boom: cannot bind"}, "exited with status 3"),
        (
            {
                "responses": {
                    "account/rateLimits/read": {"ordinaryUsageAllowed": True, "rateLimits": {}}
                }
            },
            "has no 'primary' key",
        ),
    ],
)
def test_capabilities_reports_a_failed_usage_probe_as_failure(env, script, message):
    env.fake.set_script(script)
    code, lines = env.run("capabilities", "--codex-bin", str(env.fake.bin))
    assert code == 1
    assert lines[0].startswith("FAILURE: account/rateLimits/read failed") and message in "\n".join(
        lines
    )


def test_capabilities_reports_live_usage_and_the_cap(env, hold_slots):
    hold_slots(env.root, 2)
    code, lines = env.run("capabilities", "--codex-bin", str(env.fake.bin))
    assert code == 0
    assert json.loads("\n".join(lines))["admission"] == {
        "usedPercent": 12.0,
        "ordinaryUsageAllowed": True,
        "slot_holders": 2,
        "max_concurrent_jobs": 4,
    }
    assert [(item["name"], item["tests"]) for item in json.loads("\n".join(lines))["breadth"]] == [
        (item["name"], list(item["tests"])) for item in breadth.CAPABILITIES
    ]


def test_every_breadth_capability_names_tests_that_exist():
    tests_dir = Path(__file__).parent
    for item in breadth.CAPABILITIES:
        assert item["tests"] or item["name"] == "delegation", f"{item['name']} names no test"
        for node in item["tests"]:
            file_name, test_name = node.split("::")
            tree = ast.parse((tests_dir / file_name).read_text())
            defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            assert test_name in defined, f"{item['name']}: {node} does not exist"
