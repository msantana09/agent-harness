"""Tests for codex_jobs.worktrees against real temporary git repos (a bare `origin` plus a
clone)."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import records, worktrees
from codex_jobs.worktrees import WorktreeError

GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
DEFAULT_BRANCH = "trunk"


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *GIT_IDENTITY, *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A main checkout on `trunk` with a bare origin whose HEAD is set, as a clone of a hosted repo
    has."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", DEFAULT_BRANCH, str(origin)], check=True)
    main = tmp_path / "proj"
    subprocess.run(["git", "init", "-q", "-b", DEFAULT_BRANCH, str(main)], check=True)
    (main / "a.txt").write_text("a\n")
    run_git(main, "add", "a.txt")
    run_git(main, "commit", "-q", "-m", "init")
    run_git(main, "remote", "add", "origin", str(origin))
    run_git(main, "push", "-q", "origin", DEFAULT_BRANCH)
    run_git(main, "remote", "set-head", "origin", DEFAULT_BRANCH)
    return main.resolve()


def new_job(root: Path, **overrides) -> dict:
    args = dict(
        kind="run",
        cwd="/work",
        project_dir="/work",
        model_requested="luna",
        model_slug="gpt-6-luna",
        effort_requested="low",
        sandbox="workspace-write",
        timeout_seconds=60,
        codex_bin="/bin/true",
    )
    job, _ = records.create_job(root, **{**args, **overrides})
    return job


def finish(root: Path, job_id: str, **fields) -> None:
    records.transition(root, job_id, "failed", **fields)


def add_worktree(repo: Path, name: str, branch: str | None = None) -> Path:
    path = repo / ".claude" / "worktrees" / name
    run_git(
        repo, "worktree", "add", "-q", "-b", branch or f"feat/{name}", str(path), DEFAULT_BRANCH
    )
    return path.resolve()


def test_main_checkout_is_refused_and_a_linked_worktree_passes(repo):
    with pytest.raises(WorktreeError, match="main checkout"):
        worktrees.refuse_main_checkout(repo)
    (repo / "sub").mkdir()
    with pytest.raises(WorktreeError, match="main checkout"):
        worktrees.refuse_main_checkout(repo / "sub")
    linked = add_worktree(repo, "w1")
    assert worktrees.refuse_main_checkout(linked) == linked


@pytest.mark.parametrize("branch", ["dev", "uat", "main", DEFAULT_BRANCH])
def test_protected_and_default_branches_are_refused_before_anything_is_created(
    repo, tmp_path, branch
):
    root = tmp_path / "jobs"
    job = new_job(root)
    with pytest.raises(WorktreeError, match="protected|default branch"):
        worktrees.create_worktree(root, job["job_id"], repo, branch)
    assert not (repo / ".claude").exists()
    assert not (root / "leases").exists()


def test_caller_worktree_on_a_protected_branch_is_refused(repo, tmp_path):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "on-dev", branch="dev")
    with pytest.raises(WorktreeError, match="protected"):
        worktrees.use_worktree(root, new_job(root)["job_id"], linked)


def test_dirty_caller_worktree_is_refused(repo, tmp_path):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "dirty")
    (linked / "scratch.txt").write_text("x")
    with pytest.raises(WorktreeError, match="dirty"):
        worktrees.use_worktree(root, new_job(root)["job_id"], linked)
    assert not (root / "leases").exists()


def test_main_checkout_as_caller_worktree_is_refused(repo, tmp_path):
    root = tmp_path / "jobs"
    with pytest.raises(WorktreeError, match="main checkout"):
        worktrees.use_worktree(root, new_job(root)["job_id"], repo)


def test_second_claim_names_the_holder(repo, tmp_path):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "shared")
    first, second = new_job(root), new_job(root)
    worktrees.use_worktree(root, first["job_id"], linked)
    lease = worktrees.lease_path(root, linked)
    assert lease.is_file()
    assert worktrees.lease_holder(root, linked) == first["job_id"]
    with pytest.raises(WorktreeError, match=f"leased by {first['job_id']}"):
        worktrees.use_worktree(root, second["job_id"], linked)


def test_lease_of_a_finished_job_is_taken_over(repo, tmp_path):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "reuse")
    first, second = new_job(root), new_job(root)
    worktrees.use_worktree(root, first["job_id"], linked)
    finish(root, first["job_id"])
    assert worktrees.lease_holder(root, linked) is None
    worktrees.use_worktree(root, second["job_id"], linked)
    assert worktrees.lease_holder(root, linked) == second["job_id"]


def test_two_takeovers_of_a_finished_lease_leave_exactly_one_holder(repo, tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "contested")
    old, first, second = new_job(root), new_job(root), new_job(root)
    worktrees.use_worktree(root, old["job_id"], linked)
    finish(root, old["job_id"])
    lease = worktrees.lease_path(root, linked)
    outcomes: dict[str, str] = {}

    def claim(job_id: str) -> None:
        try:
            worktrees.claim_lease(
                root, job_id, linked, main=str(repo), branch="feat/contested", created=False
            )
            outcomes[job_id] = "won"
        except WorktreeError as exc:
            outcomes[job_id] = str(exc)

    rival = threading.Thread(target=claim, args=(second["job_id"],))
    real_unlink = Path.unlink
    armed = [True]

    def unlink(self, *args, **kwargs):
        # The first claimant, about to drop the finished lease, lets the second one try in between.
        if self == lease and armed[0]:
            armed[0] = False
            rival.start()
            rival.join(timeout=1.0)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    claim(first["job_id"])
    rival.join()
    assert outcomes == {
        first["job_id"]: "won",
        second["job_id"]: f"{linked} is leased by {first['job_id']}",
    }
    assert worktrees.lease_holder(root, linked) == first["job_id"]


def test_release_by_an_old_holder_after_a_takeover_keeps_the_new_lease_and_worktree(
    repo, tmp_path, monkeypatch
):
    root = tmp_path / "jobs"
    old, new = new_job(root), new_job(root)
    wt = worktrees.create_worktree(root, old["job_id"], repo, "feat/taken")
    finish(root, old["job_id"])
    real_load_job = records.load_job
    armed = [True]

    def load_job(root_arg, job_id):
        # The old holder's release has found its lease; the new job takes the worktree over before
        # it acts.
        if armed[0] and job_id == old["job_id"]:
            armed[0] = False
            worktrees.use_worktree(root, new["job_id"], wt.path)
        return real_load_job(root_arg, job_id)

    monkeypatch.setattr(records, "load_job", load_job)
    with pytest.raises(WorktreeError, match=f"job {old['job_id']} no longer holds"):
        worktrees.release_worktree(root, old["job_id"])
    assert Path(wt.path).is_dir()
    assert worktrees.lease_holder(root, wt.path) == new["job_id"]


def test_release_is_blocked_while_the_process_group_lives(repo, tmp_path):
    root = tmp_path / "jobs"
    job = new_job(root)
    wt = worktrees.create_worktree(root, job["job_id"], repo, "feat/held")
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        records.update_job(root, job["job_id"], pgid=os.getpgid(proc.pid))
        with pytest.raises(WorktreeError, match="is queued"):
            worktrees.release_worktree(root, job["job_id"])
        finish(root, job["job_id"])
        with pytest.raises(WorktreeError, match="process group .* still alive"):
            worktrees.release_worktree(root, job["job_id"])
        assert Path(wt.path).is_dir()
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        worktrees.release_worktree(root, job["job_id"])
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert not Path(wt.path).exists()
    assert worktrees.find_lease(root, job["job_id"]) is None


def test_a_stopped_stamp_means_the_process_group_is_gone_whatever_the_probe_says(repo, tmp_path):
    root = tmp_path / "jobs"
    job = new_job(root)
    wt = worktrees.create_worktree(root, job["job_id"], repo, "feat/stamped")
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        finish(root, job["job_id"], pgid=os.getpgid(proc.pid))
        with pytest.raises(WorktreeError, match="still alive"):
            worktrees.release_worktree(root, job["job_id"])
        records.update_job(root, job["job_id"], appserver_stopped_at=records.now_stamp())
        worktrees.release_worktree(root, job["job_id"])
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    assert not Path(wt.path).exists()


def test_second_caller_worktree_claim_is_refused_as_leased_even_when_the_holder_dirtied_it(
    repo, tmp_path
):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "busy")
    first, second = new_job(root), new_job(root)
    worktrees.use_worktree(root, first["job_id"], linked)
    (linked / "edit.txt").write_text("the holder's work")
    with pytest.raises(WorktreeError, match=f"leased by {first['job_id']}"):
        worktrees.use_worktree(root, second["job_id"], linked)


def test_a_rival_that_dirties_and_finishes_between_the_clean_check_and_the_claim_is_not_taken_over(
    repo, tmp_path, monkeypatch
):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "raced")
    first, rival_job = new_job(root), new_job(root)
    outcomes: dict[str, str] = {}

    def rival_run() -> None:
        try:
            worktrees.use_worktree(root, rival_job["job_id"], linked)
        except WorktreeError as exc:
            outcomes["rival"] = str(exc)
            return
        (linked / "edit.txt").write_text("the rival's work")
        finish(root, rival_job["job_id"])
        outcomes["rival"] = "won"

    rival = threading.Thread(target=rival_run)
    real_git = worktrees.git
    armed = [True]

    def git(cwd, *args):
        out = real_git(cwd, *args)
        # The first submission has just read the status; the rival tries to take the worktree before
        # it claims.
        if args[:1] == ("status",) and armed[0]:
            armed[0] = False
            rival.start()
            rival.join(timeout=1.0)
        return out

    monkeypatch.setattr(worktrees, "git", git)
    try:
        worktrees.use_worktree(root, first["job_id"], linked)
        first_outcome = "won"
    except WorktreeError as exc:
        first_outcome = str(exc)
    rival.join()
    assert real_git(linked, "status", "--porcelain").strip() == ""
    assert (first_outcome, outcomes["rival"]) == ("won", f"{linked} is leased by {first['job_id']}")


def test_release_of_a_caller_worktree_drops_the_lease_and_keeps_the_directory(repo, tmp_path):
    root = tmp_path / "jobs"
    linked = add_worktree(repo, "mine")
    job = new_job(root)
    worktrees.use_worktree(root, job["job_id"], linked)
    finish(root, job["job_id"])
    worktrees.release_worktree(root, job["job_id"])
    assert linked.is_dir()
    assert worktrees.find_lease(root, job["job_id"]) is None


def test_a_repo_without_worktree_scripts_creates_through_git_worktree_add(repo, tmp_path):
    root = tmp_path / "jobs"
    job = new_job(root)
    wt = worktrees.create_worktree(root, job["job_id"], repo, "feat/new-thing")
    assert wt.path == str(repo / ".claude" / "worktrees" / "new-thing")
    assert wt.branch == "feat/new-thing" and wt.created
    assert wt.base_commit == run_git(repo, "rev-parse", f"origin/{DEFAULT_BRANCH}").strip()
    assert str(Path(wt.path)) in run_git(repo, "worktree", "list", "--porcelain")
    assert worktrees.lease_holder(root, wt.path) == job["job_id"]
    assert worktrees.main_checkout(wt.path) == repo


def add_worktree_scripts(repo: Path, log: Path) -> None:
    """The repo's own worktree scripts: add requires both the branch and the base ref (`set -u`)."""
    scripts = repo / "scripts" / "git"
    scripts.mkdir(parents=True)
    (scripts / "worktree-add.sh").write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        f'echo "add $PWD $*" >> {log}\n'
        "branch=$1; base=$2\n"
        'git worktree add -q -b "$branch" ".claude/worktrees/${branch##*/}" "$base"\n'
    )
    (scripts / "worktree-remove.sh").write_text(
        f'#!/usr/bin/env bash\nset -e\necho "remove $PWD $*" >> {log}\ngit worktree remove "$1"\n'
    )
    for script in scripts.iterdir():
        script.chmod(0o755)


def test_worktree_scripts_are_detected_by_path_and_called_with_the_branch_base_and_path(
    repo, tmp_path
):
    log = tmp_path / "argv.log"
    add_worktree_scripts(repo, log)
    root = tmp_path / "jobs"
    job = new_job(root)
    wt = worktrees.create_worktree(root, job["job_id"], repo, "feat/via-script")
    assert Path(wt.path).is_dir() and wt.branch == "feat/via-script"
    finish(root, job["job_id"])
    worktrees.release_worktree(root, job["job_id"])
    assert log.read_text().splitlines() == [
        f"add {repo} feat/via-script origin/{DEFAULT_BRANCH}",
        f"remove {repo} {wt.path}",
    ]
    assert not Path(wt.path).exists()


def test_the_add_script_builds_from_the_fetched_default_branch_not_the_local_head(repo, tmp_path):
    add_worktree_scripts(repo, tmp_path / "argv.log")
    (repo / "local.txt").write_text("not pushed\n")
    run_git(repo, "add", "local.txt")
    run_git(repo, "commit", "-q", "-m", "local only")
    root = tmp_path / "jobs"
    job = new_job(root)
    wt = worktrees.create_worktree(root, job["job_id"], repo, "feat/from-origin")
    origin_head = run_git(repo, "rev-parse", f"origin/{DEFAULT_BRANCH}").strip()
    assert wt.base_commit == origin_head != run_git(repo, "rev-parse", "HEAD").strip()
