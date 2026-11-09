"""Breadth lifecycle tests against the fake Codex: fork permissions, resume's recorded cwd and
model."""

import importlib.util
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from codex_jobs import permissions, records, worktrees

_spec = importlib.util.spec_from_file_location(
    "cli_harness", Path(__file__).with_name("test_cli.py")
)
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
ANSWER, TASK, run_in = _harness.ANSWER, _harness.TASK, _harness.run_in
env, repo = _harness.env, _harness.repo


def finished_run(env, repo, branch, *extra):
    """A finished `run` job on a new branch; returns its record."""
    env.fake.set_script({"turn": {"final_answer": TASK}})
    assert run_in(env, repo, "--branch", branch, *extra)[0] == 0
    (job,) = [job for job in env.jobs() if job["branch"] == branch]
    return job


def fork(env, repo, source, *extra, answer=ANSWER):
    """Fork `source`; the fake answers the fork's turn with `answer` (a consult answer unless the
    fork writes)."""
    env.fake.set_script({"turn": {"final_answer": answer}})
    return env.run(
        "fork",
        source,
        str(env.prompt),
        "--project-dir",
        str(repo),
        "--codex-bin",
        str(env.fake.bin),
        *extra,
    )


def fork_records(env, parent_id):
    return [job for job in env.jobs() if job["parent_job_id"] == parent_id]


def test_fork_of_a_run_job_without_a_worktree_is_read_only_in_the_parents_directory(env, repo):
    parent = finished_run(env, repo, "codex/fork-parent")
    assert fork(env, repo, parent["job_id"])[0] == 0
    (forked,) = fork_records(env, parent["job_id"])
    assert (forked["kind"], forked["sandbox"], forked["worktree"], forked["allow_commit"]) == (
        "consult",
        "read-only",
        None,
        False,
    )
    (opened,) = env.sent("thread/fork")
    assert (opened["params"]["cwd"], opened["params"]["sandbox"]) == (
        parent["worktree"],
        "read-only",
    )
    fork_turn = env.sent("turn/start")[-1]
    assert fork_turn["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": True}


@pytest.mark.parametrize("allow_commit", [False, True])
def test_fork_of_a_run_job_with_its_own_branch_writes_only_in_its_own_worktree(
    env, repo, allow_commit
):
    parent = finished_run(
        env, repo, "codex/fork-parent", *(["--allow-commit"] if allow_commit else [])
    )
    code, lines = fork(env, repo, parent["job_id"], "--branch", "codex/fork-child", answer=TASK)
    assert code == 0, lines
    (forked,) = fork_records(env, parent["job_id"])
    child_tree = str(repo / ".claude" / "worktrees" / "fork-child")
    assert (forked["kind"], forked["sandbox"], forked["worktree"], forked["branch"]) == (
        "run",
        "workspace-write",
        child_tree,
        "codex/fork-child",
    )
    assert forked["allow_commit"] is allow_commit and child_tree != parent["worktree"]
    (opened,) = env.sent("thread/fork")
    assert (opened["params"]["cwd"], opened["params"]["sandbox"]) == (child_tree, "workspace-write")
    fork_turn = env.sent("turn/start")[-1]
    permission_class = permissions.WRITE_COMMIT if allow_commit else permissions.WRITE
    assert (
        fork_turn["params"]["sandboxPolicy"]
        == permissions.policy_for(permission_class, child_tree)[1]
    )
    assert fork_turn["params"]["sandboxPolicy"]["writableRoots"][0] == child_tree
    assert ("You may commit your work" in fork_turn["params"]["input"][0]["text"]) is allow_commit
    assert worktrees.find_lease(env.root, forked["job_id"]) is not None


def test_fork_with_a_branch_named_like_the_parents_is_refused_not_downgraded(env, repo):
    parent = finished_run(env, repo, "codex/fork-parent")
    code, lines = fork(env, repo, parent["job_id"], "--branch", "other/fork-parent")
    assert code == 1 and lines == [
        f"FAILURE: {parent['worktree']} already exists; pick another branch name"
    ]
    assert fork_records(env, parent["job_id"]) == []


def test_fork_of_a_run_job_in_another_clean_worktree_keeps_the_parents_class(env, repo):
    parent = finished_run(env, repo, "codex/fork-parent")
    other = repo / ".claude" / "worktrees" / "mine"
    _harness.git(repo, "worktree", "add", "-q", "-b", "codex/mine", str(other), "trunk")
    assert fork(env, repo, parent["job_id"], "--worktree", str(other), answer=TASK)[0] == 0
    (forked,) = fork_records(env, parent["job_id"])
    assert (forked["kind"], forked["worktree"]) == ("run", str(other.resolve()))
    policy = env.sent("turn/start")[-1]["params"]["sandboxPolicy"]
    assert policy == {
        "type": "workspaceWrite",
        "writableRoots": [str(other.resolve())],
        "networkAccess": True,
    }


def test_fork_of_a_run_job_pointed_at_the_parents_worktree_runs_read_only_without_a_lease(
    env, repo
):
    parent = finished_run(env, repo, "codex/fork-parent")
    assert fork(env, repo, parent["job_id"], "--worktree", parent["worktree"])[0] == 0
    (forked,) = fork_records(env, parent["job_id"])
    assert (forked["kind"], forked["sandbox"], forked["worktree"], forked["cwd"]) == (
        "consult",
        "read-only",
        None,
        parent["worktree"],
    )
    assert env.sent("turn/start")[-1]["params"]["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": True,
    }
    assert worktrees.find_lease(env.root, forked["job_id"]) is None
    assert worktrees.find_lease(env.root, parent["job_id"]) is not None


def test_fork_of_a_consult_job_or_a_thread_id_refuses_a_worktree(env, repo):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    assert env.consult()[0] == 0
    (consult,) = env.jobs()
    for source in (consult["job_id"], consult["thread_id"]):
        code, lines = fork(env, repo, source, "--branch", "codex/nope")
        assert code == 1 and "apply to a fork of a run job" in lines[0]
    assert len(env.jobs()) == 1 and not (repo / ".claude").exists()


def test_resume_sends_the_source_jobs_cwd_and_model_and_an_explicit_model_wins(env, tmp_path):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    assert env.consult("--model", "luna", "--effort", "low")[0] == 0
    (first,) = env.jobs()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def resume(*extra):
        return env.run(
            "resume",
            first["job_id"],
            str(env.prompt),
            "--project-dir",
            str(elsewhere),
            "--codex-bin",
            str(env.fake.bin),
            *extra,
        )

    assert resume()[0] == 0
    opened = env.sent("thread/resume")[-1]
    assert (opened["params"]["threadId"], opened["params"]["cwd"], opened["params"]["model"]) == (
        first["thread_id"],
        str(env.project),
        "gpt-6-luna",
    )
    assert env.sent("turn/start")[-1]["params"]["effort"] == "low"
    (resumed,) = [job for job in env.jobs() if job["job_id"] != first["job_id"]]
    assert (resumed["cwd"], resumed["project_dir"], resumed["model_slug"]) == (
        str(env.project),
        str(elsewhere),
        "gpt-6-luna",
    )

    assert resume("--model", "sol", "--effort", "high")[0] == 0
    explicit = env.sent("thread/resume")[-1]
    assert (explicit["params"]["cwd"], explicit["params"]["model"]) == (
        str(env.project),
        "slug-sol",
    )


def test_resume_of_a_thread_id_uses_the_default_model_in_the_given_project(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    assert (
        env.run(
            "resume",
            "thr-elsewhere",
            str(env.prompt),
            "--project-dir",
            str(env.project),
            "--codex-bin",
            str(env.fake.bin),
        )[0]
        == 0
    )
    opened = env.sent("thread/resume")[-1]
    assert (opened["params"]["cwd"], opened["params"]["model"]) == (str(env.project), "slug-sol")


def resume(env, repo, source, *extra, answer=TASK):
    """Resume `source`; the fake answers the resumed turn with `answer`."""
    env.fake.set_script({"turn": {"final_answer": answer}})
    return env.run(
        "resume",
        source,
        str(env.prompt),
        "--project-dir",
        str(repo),
        "--codex-bin",
        str(env.fake.bin),
        *extra,
    )


def test_resume_of_a_finished_run_job_continues_in_its_worktree_and_takes_over_the_lease(env, repo):
    parent = finished_run(env, repo, "codex/resume-parent", "--allow-commit")
    tree = parent["worktree"]
    (Path(tree) / "a.txt").write_text("parent's commit\n")
    _harness.git(tree, "commit", "-q", "-am", "parent work")
    parent_commit = _harness.git(tree, "rev-parse", "HEAD").strip()
    (Path(tree) / "b.txt").write_text("the resumed job's file\n")
    code, lines = resume(env, repo, parent["job_id"])
    assert code == 0, lines
    (resumed,) = fork_records(env, parent["job_id"])
    assert (
        resumed["kind"],
        resumed["sandbox"],
        resumed["source_mode"],
        resumed["allow_commit"],
    ) == ("run", "workspace-write", "resume", True)
    assert (resumed["worktree"], resumed["cwd"], resumed["branch"], resumed["base_commit"]) == (
        tree,
        tree,
        parent["branch"],
        parent["base_commit"],
    )
    (opened,) = env.sent("thread/resume")
    assert (opened["params"]["cwd"], opened["params"]["sandbox"], opened["params"]["threadId"]) == (
        tree,
        "workspace-write",
        parent["thread_id"],
    )
    turn = env.sent("turn/start")[-1]["params"]
    assert turn["sandboxPolicy"] == permissions.policy_for(permissions.WRITE_COMMIT, tree)[1]
    assert turn["sandboxPolicy"]["writableRoots"][0] == tree
    assert "You may commit your work" in turn["input"][0]["text"]
    result = json.loads((env.root / resumed["job_id"] / "result.json").read_text())
    assert (
        result["commits"] == [parent_commit]
        and result["changed_files"] == ["b.txt"]
        and result["worktree"] == tree
    )
    assert worktrees.find_lease(env.root, parent["job_id"]) is None
    _path, lease = worktrees.find_lease(env.root, resumed["job_id"])
    assert (lease["worktree"], lease["created"]) == (tree, True)


def test_resume_of_a_run_job_is_refused_while_it_is_still_running(env, repo):
    env.fake.set_script({"turn": {"hang": True}})
    assert run_in(env, repo, "--branch", "codex/resume-live", "--detach")[0] == 0
    (parent,) = env.jobs()
    env.wait_for_turn(parent["job_id"])
    code, lines = resume(env, repo, parent["job_id"])
    assert (
        code == 1
        and lines[-1].startswith(f"FAILURE: job {parent['job_id']} is ")
        and "before resuming it" in lines[-1]
    )
    assert worktrees.find_lease(env.root, parent["job_id"]) is not None
    env.run("cancel", parent["job_id"])


def test_resume_right_after_a_run_finishes_waits_for_its_app_server_to_stop(env, repo):
    parent = finished_run(env, repo, "codex/resume-settling")
    # The runner marks the job finished just before it stops the app-server: stand in a group that
    # exits shortly.
    server = subprocess.Popen(["sleep", "0.5"], start_new_session=True)
    threading.Thread(target=server.wait, daemon=True).start()
    records.update_job(env.root, parent["job_id"], pgid=server.pid, appserver_stopped_at=None)
    code, lines = resume(env, repo, parent["job_id"])
    assert code == 0, lines
    (resumed,) = [job for job in env.jobs() if job["parent_job_id"] == parent["job_id"]]
    assert worktrees.find_lease(env.root, resumed["job_id"])[1]["worktree"] == parent["worktree"]


def test_resume_of_a_run_job_is_refused_after_release_or_removal(env, repo):
    parent = finished_run(env, repo, "codex/resume-gone")
    assert env.run("release", parent["job_id"])[0] == 0
    code, lines = resume(env, repo, parent["job_id"])
    assert (
        code == 1
        and lines[-1]
        == f"FAILURE: job {parent['job_id']} released its worktree {parent['worktree']}; "
        "start a new run instead"
    )
    other = finished_run(env, repo, "codex/resume-removed")
    shutil.rmtree(other["worktree"])
    code, lines = resume(env, repo, other["job_id"])
    assert (
        code == 1 and lines[-1] == f"FAILURE: {other['worktree']} is gone; start a new run instead"
    )


@pytest.mark.parametrize(
    "switched_to, refusal",
    [("dev", "branch dev is protected"), ("codex/elsewhere", "is on codex/elsewhere")],
)
def test_resume_of_a_run_job_is_refused_once_its_worktree_left_the_recorded_branch(
    env, repo, switched_to, refusal
):
    parent = finished_run(env, repo, "codex/resume-switched")
    _harness.git(parent["worktree"], "checkout", "-q", "-b", switched_to)
    code, lines = resume(env, repo, parent["job_id"])
    assert code == 1 and refusal in lines[-1], lines
    assert worktrees.find_lease(env.root, parent["job_id"]) is not None


def test_resume_of_a_run_job_is_refused_once_another_job_holds_its_lease(env, repo):
    parent = finished_run(env, repo, "codex/resume-taken")
    env.fake.set_script({"turn": {"final_answer": TASK}})
    assert run_in(env, repo, "--worktree", parent["worktree"])[0] == 0
    (taker,) = [job for job in env.jobs() if job["job_id"] != parent["job_id"]]
    code, lines = resume(env, repo, parent["job_id"])
    assert (
        code == 1
        and lines[-1]
        == f"FAILURE: {parent['worktree']} is now leased by {taker['job_id']}; "
        "resume that job instead"
    )
    assert worktrees.find_lease(env.root, taker["job_id"]) is not None


def test_a_resent_resume_key_returns_the_first_job_even_when_the_source_directory_is_gone(
    env, tmp_path
):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    assert env.consult()[0] == 0
    (first,) = env.jobs()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    args = (
        "resume",
        first["job_id"],
        str(env.prompt),
        "--project-dir",
        str(elsewhere),
        "--codex-bin",
        str(env.fake.bin),
        "--idempotency-key",
        "k-resume",
    )
    code, lines = env.run(*args)
    (resumed,) = [job for job in env.jobs() if job["job_id"] != first["job_id"]]
    assert (code, lines[-1]) == (0, f"SUCCESS: {resumed['output_dir']}")
    shutil.rmtree(env.project)
    assert not env.project.exists()
    assert env.run(*args) == (0, [f"JOB: {resumed['job_id']}"])
    assert len(env.jobs()) == 2
