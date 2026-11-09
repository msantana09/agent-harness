"""Tests for codex_jobs.permissions: policy per class (checked on the wire), forks, envelope and git facts."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import permissions
from codex_jobs.appserver import AppServerClient
from codex_jobs.permissions import READ, WRITE, WRITE_COMMIT
from codex_jobs.worktrees import WorktreeError

GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *GIT_IDENTITY, *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def work(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(path)], check=True)
    for name in ("keep.txt", "edit.txt", "gone.txt", "old-name.txt"):
        (path / name).write_text(f"{name}\n")
    run_git(path, "add", ".")
    run_git(path, "commit", "-q", "-m", "init")
    return path.resolve()


def test_policy_per_class(work, tmp_path):
    linked = tmp_path / "linked"
    run_git(work, "worktree", "add", "-q", "-b", "codex/linked", str(linked), "trunk")
    linked = linked.resolve()
    assert permissions.policy_for(READ) == (
        "read-only",
        {"type": "readOnly", "networkAccess": True},
    )
    write_policy = {"type": "workspaceWrite", "writableRoots": [str(linked)], "networkAccess": True}
    assert permissions.policy_for(WRITE, linked) == ("workspace-write", write_policy)
    git_dir = work / ".git"
    commit_roots = [
        linked,
        git_dir / "worktrees" / "linked",
        git_dir / "objects",
        git_dir / "refs" / "heads",
        git_dir / "logs" / "refs" / "heads",
    ]
    commit_policy = {**write_policy, "writableRoots": [str(root) for root in commit_roots]}
    assert permissions.policy_for(WRITE_COMMIT, linked) == ("workspace-write", commit_policy)
    with pytest.raises(WorktreeError, match="needs a worktree"):
        permissions.policy_for(WRITE)
    with pytest.raises(ValueError, match="permission class"):
        permissions.policy_for("admin", work)


def test_sandbox_for_a_job_record(work):
    job = {"kind": "run", "cwd": str(work), "allow_commit": False}
    assert permissions.sandbox_for(job) == permissions.policy_for(WRITE, work)
    assert permissions.sandbox_for({**job, "allow_commit": True}) == permissions.policy_for(
        WRITE_COMMIT, work
    )
    assert permissions.sandbox_for(
        {"kind": "consult", "cwd": "/anywhere"}
    ) == permissions.policy_for(READ)
    assert permissions.sandbox_for(
        {"kind": "review", "cwd": "/anywhere"}
    ) == permissions.policy_for(READ)


@pytest.mark.parametrize("permission_class", [READ, WRITE, WRITE_COMMIT])
def test_each_class_reaches_the_server_with_the_schema_key_names(
    fake_codex, work, permission_class
):
    thread_sandbox, policy = permissions.policy_for(permission_class, work)
    fake_codex.set_script({"turn": {"final_answer": "done"}})
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        client.initialize()
        thread_id = client.call(
            "thread/start",
            {
                "cwd": str(work),
                "model": "gpt-6-luna",
                "sandbox": thread_sandbox,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
        )["thread"]["id"]
        client.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "go"}],
                "effort": "low",
                "sandboxPolicy": policy,
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            },
        )
    sent = {m["method"]: m["params"] for m in fake_codex.received() if "method" in m}
    assert sent["thread/start"]["sandbox"] == thread_sandbox
    assert sent["turn/start"]["sandboxPolicy"] == policy
    expected_keys = {
        READ: {"type", "networkAccess"},
        WRITE: {"type", "writableRoots", "networkAccess"},
        WRITE_COMMIT: {"type", "writableRoots", "networkAccess"},
    }
    assert set(sent["turn/start"]["sandboxPolicy"]) == expected_keys[permission_class]


def test_fork_never_inherits_write_in_the_parents_worktree(work, tmp_path):
    other = tmp_path / "other"
    for parent in (WRITE, WRITE_COMMIT):
        assert (
            permissions.fork_permission_class(parent, fork_worktree=None, parent_worktree=work)
            == READ
        )
        assert (
            permissions.fork_permission_class(parent, fork_worktree=work, parent_worktree=work)
            == READ
        )
        assert (
            permissions.fork_permission_class(parent, fork_worktree=other, parent_worktree=work)
            == parent
        )
    assert (
        permissions.fork_permission_class(READ, fork_worktree=other, parent_worktree=work) == READ
    )


def test_commit_line_with_and_without_allow_commit():
    assert permissions.commit_line(False) == "Do not commit or push; the caller commits."
    assert "may commit" in permissions.commit_line(True)
    assert "commit or push" not in permissions.commit_line(True)


def test_changed_files_equals_git_status_porcelain_paths(work):
    (work / "edit.txt").write_text("changed\n")
    (work / "gone.txt").unlink()
    run_git(work, "mv", "old-name.txt", "new-name.txt")
    (work / "fresh").mkdir()
    (work / "fresh" / "a.txt").write_text("a")
    (work / "fresh" / "b.txt").write_text("b")
    (work / "staged.txt").write_text("s")
    run_git(work, "add", "staged.txt")
    porcelain = run_git(work, "status", "--porcelain", "--untracked-files=all").splitlines()
    expected = sorted({path for line in porcelain for path in line[3:].split(" -> ")})
    assert sorted(permissions.changed_files(work)) == expected
    assert "keep.txt" not in permissions.changed_files(work)
    assert permissions.changed_files(work).count("new-name.txt") == 1


def test_changed_files_is_empty_for_a_clean_tree(work):
    assert permissions.changed_files(work) == []


def test_commits_lists_only_commits_after_the_base(work):
    base = run_git(work, "rev-parse", "HEAD").strip()
    assert permissions.commits(work, base) == []
    for n in (1, 2):
        (work / "edit.txt").write_text(f"v{n}\n")
        run_git(work, "commit", "-q", "-am", f"c{n}")
    head = run_git(work, "rev-parse", "HEAD").strip()
    listed = permissions.commits(work, base)
    assert len(listed) == 2 and listed[0] == head and base not in listed
