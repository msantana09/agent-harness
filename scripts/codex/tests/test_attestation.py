"""Attestation: the snapshot taken before dispatch, and what a terminal state makes of it."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from codex_jobs import attestation

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, text: str = "x\n") -> None:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A feature branch one commit ahead of `origin/dev`, with `origin/HEAD` pointing at dev."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "dev", str(remote)], check=True, capture_output=True
    )
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(remote), str(work)], check=True, capture_output=True)
    git(work, "checkout", "-b", "dev")
    commit(work, "base.txt")
    git(work, "push", "-u", "origin", "dev")
    git(work, "remote", "set-head", "origin", "dev")
    git(work, "checkout", "-b", "feature")
    commit(work, "change.txt")
    return work


@pytest.fixture
def prompt(tmp_path) -> Path:
    path = tmp_path / "prompt.txt"
    path.write_text("review this\n")
    return path


def job(repo: Path) -> dict[str, Any]:
    return {
        "job_id": "cj-20990101-120000-abcd",
        "thread_id": "thr-1",
        "cwd": str(repo),
        "sandbox": "read-only",
        "model_used": "gpt-6.1-sol",
        "model_slug": "gpt-6.1-sol",
        "effort_used": "high",
        "effort_requested": "high",
    }


def test_snapshot_hashes_the_diff_a_gate_recomputes(repo, prompt):
    snap = attestation.snapshot(repo, prompt)
    merge_base = git(repo, "merge-base", "origin/dev", "HEAD")
    # The diff command of the attestation contract, run the way a gate would run it.
    diff = subprocess.run(
        ["git", "diff", "--no-color", "--no-ext-diff", merge_base, "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    assert snap.diff_sha256 == hashlib.sha256(diff).hexdigest()
    assert (snap.merge_base_sha, snap.head_sha) == (merge_base, git(repo, "rev-parse", "HEAD"))
    assert (snap.git_branch, snap.repo_root) == ("feature", str(repo.resolve()))
    assert snap.prompt_sha256 == hashlib.sha256(b"review this\n").hexdigest()


def test_completed_review_of_an_unmoved_head_is_ok_and_carries_the_job(repo, prompt):
    snap = attestation.snapshot(repo, prompt)
    result = attestation.complete(snap, job(repo), failure=None)
    assert result["exit_status"] == "ok"
    assert (result["head_sha"], result["diff_sha256"]) == (snap.head_sha, snap.diff_sha256)
    assert {k: result[k] for k in ("job_id", "thread_id", "cwd", "sandbox", "model", "effort")} == {
        "job_id": "cj-20990101-120000-abcd",
        "thread_id": "thr-1",
        "cwd": str(repo),
        "sandbox": "read-only",
        "model": "gpt-6.1-sol",
        "effort": "high",
    }
    assert set(result) == {
        "repo_root",
        "git_branch",
        "merge_base_sha",
        "head_sha",
        "diff_sha256",
        "prompt_sha256",
        "review_target",
        "target_merge_base_sha",
        "uncommitted_tracked",
        "job_id",
        "thread_id",
        "cwd",
        "sandbox",
        "model",
        "effort",
        "completed_at",
        "exit_status",
    }


def test_head_moved_is_not_ok(repo, prompt):
    snap = attestation.snapshot(repo, prompt)
    commit(repo, "late.txt")
    assert (
        attestation.complete(snap, job(repo), failure=None)["exit_status"]
        == "failure: head moved during review"
    )


def test_a_review_that_did_not_complete_names_why(repo, prompt):
    snap = attestation.snapshot(repo, prompt)
    assert (
        attestation.complete(snap, job(repo), failure="review blocked")["exit_status"]
        == "failure: review blocked"
    )


def test_a_job_that_ended_before_the_snapshot_is_never_ok(repo):
    result = attestation.complete(None, job(repo), failure=None)
    assert result["exit_status"].startswith("failure:")
    assert result["head_sha"] is None and result["diff_sha256"] is None


def test_the_snapshot_survives_into_another_process(repo, prompt, tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    attestation.save(job_dir, attestation.snapshot(repo, prompt))
    commit(repo, "late.txt")
    written = attestation.write(job_dir, job(repo), failure=None)
    assert json.loads((job_dir / "attestation.json").read_text()) == written
    assert written["exit_status"] == "failure: head moved during review"
    assert written["head_sha"] != git(repo, "rev-parse", "HEAD")


def test_write_without_a_saved_snapshot_records_the_failure(repo, tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    written = attestation.write(job_dir, job(repo), failure="final answer rejected: not JSON")
    assert written["exit_status"] == "failure: final answer rejected: not JSON"
    assert (job_dir / "attestation.json").exists()


def test_repository_without_origin_head_is_reviewed_but_never_ok(repo, prompt):
    git(repo, "remote", "set-head", "origin", "--delete")
    snap = attestation.snapshot(repo, prompt)
    assert (snap.head_sha, snap.merge_base_sha, snap.diff_sha256) == (
        git(repo, "rev-parse", "HEAD"),
        None,
        None,
    )
    status = attestation.complete(snap, job(repo), failure=None)["exit_status"]
    assert status.startswith("failure: not attestable") and "origin/HEAD" in status


def test_repository_before_its_first_commit_has_no_head_and_is_never_ok(tmp_path, prompt):
    unborn = tmp_path / "unborn"
    subprocess.run(["git", "init", "-q", "-b", "dev", str(unborn)], check=True)
    snap = attestation.snapshot(unborn, prompt)
    assert (snap.git_branch, snap.head_sha, snap.merge_base_sha, snap.diff_sha256) == (
        "dev",
        None,
        None,
        None,
    )
    status = attestation.complete(snap, job(unborn), failure=None)["exit_status"]
    assert status == "failure: not attestable: unborn HEAD: the repository has no commit yet"


def test_directory_outside_a_repository_has_no_git_state_and_is_never_ok(tmp_path, prompt):
    plain = tmp_path / "plain"
    plain.mkdir()
    snap = attestation.snapshot(plain, prompt)
    assert (snap.repo_root, snap.head_sha, snap.diff_sha256) == (None, None, None)
    status = attestation.complete(snap, job(plain), failure=None)["exit_status"]
    assert status == "failure: not attestable: not a git repository"


@pytest.mark.parametrize(
    "target, reason",
    [
        ({"type": "uncommittedChanges"}, "uncommittedChanges"),
        ({"type": "commit", "sha": "HEAD"}, "commit"),
        ({"type": "baseBranch", "branch": "feature"}, "feature"),
    ],
)
def test_a_native_review_narrower_than_the_branch_diff_is_never_ok(repo, target, reason):
    snap = attestation.snapshot(repo, None, review_target=target)
    status = attestation.complete(snap, job(repo), failure=None)["exit_status"]
    assert status.startswith("failure: not attestable") and reason in status


@pytest.mark.parametrize(
    "when, prefix",
    [("launch", "failure: not attestable"), ("completion", attestation.TREE_CHANGED)],
)
def test_a_native_review_with_uncommitted_tracked_changes_is_never_ok(repo, when, prefix):
    # Codex's base review diffs the working tree; with change.txt deleted there it sees no change
    # while the committed diff has one.
    if when == "launch":
        (repo / "change.txt").unlink()
    snap = attestation.snapshot(repo, None, review_target={"type": "baseBranch", "branch": "dev"})
    if when == "completion":
        (repo / "change.txt").unlink()
    status = attestation.complete(snap, job(repo), failure=None)["exit_status"]
    assert status.startswith(prefix) and "change.txt" in status


def test_a_repository_unreadable_at_completion_is_not_reported_as_a_moved_head(repo, prompt):
    snap = attestation.snapshot(repo, prompt)
    shutil.rmtree(repo)
    status = attestation.complete(snap, job(repo), failure=None)["exit_status"]
    assert status.startswith(attestation.UNREADABLE)


@pytest.mark.parametrize(
    "status, is_stale",
    [
        (attestation.HEAD_MOVED, True),
        (f"{attestation.TREE_CHANGED}:  D change.txt", True),
        (f"{attestation.UNREADABLE}: cannot run git", True),
        ("ok", False),
        ("failure: not attestable: not a git repository", False),
        ("failure: review blocked", False),
    ],
)
def test_only_a_state_changed_or_unreadable_since_launch_makes_a_review_stale(status, is_stale):
    assert attestation.stale(status) is is_stale


@pytest.mark.parametrize("branch", ["dev", "origin/dev"])
def test_a_native_review_against_the_attested_base_is_ok(repo, branch):
    snap = attestation.snapshot(repo, None, review_target={"type": "baseBranch", "branch": branch})
    result = attestation.complete(snap, job(repo), failure=None)
    assert result["exit_status"] == "ok"
    assert result["review_target"] == {"type": "baseBranch", "branch": branch}
