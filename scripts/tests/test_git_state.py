"""Tests for scripts/git_state.sh: the one-line branch, drift, dirty and worktree summary.

Each test builds a throwaway repo (and a clone or worktree where the case needs one) under
tmp_path and reads the script's single output line.
"""

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "git_state.sh"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=GIT_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)


def new_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, "first")
    return path


def state(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args], env=GIT_ENV, capture_output=True, text=True, check=False
    )


def test_a_repo_without_upstream_reports_branch_head_dirty_count_and_main_tree(
    tmp_path: Path,
) -> None:
    repo = new_repo(tmp_path / "repo")
    (repo / "untracked").write_text("x")
    head = git(repo, "rev-parse", "--short", "HEAD")
    result = state(str(repo))
    assert result.returncode == 0
    assert result.stdout == (
        f"branch=main head={head} no-upstream dirty=1 main-tree={repo.resolve()}\n"
    )


def test_drift_counts_commits_ahead_of_and_behind_the_upstream(tmp_path: Path) -> None:
    origin = new_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    commit(origin, "theirs")
    git(clone, "fetch", "-q")
    commit(clone, "mine-1")
    commit(clone, "mine-2")
    assert " ahead=2 behind=1 dirty=0 " in state(str(clone)).stdout


def test_a_base_ref_adds_the_commits_head_lacks(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "repo")
    git(repo, "branch", "feature")
    commit(repo, "on-main-1")
    commit(repo, "on-main-2")
    git(repo, "switch", "-q", "feature")
    assert " no-upstream behind_base=2 " in state(str(repo), "main").stdout


def test_a_linked_worktree_is_named_as_a_worktree(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "repo")
    tree = tmp_path / "tree"
    git(repo, "worktree", "add", "-q", "-b", "side", str(tree))
    out = state(str(tree)).stdout
    assert out.startswith("branch=side ")
    assert out.endswith(f" worktree={tree.resolve()}\n")


def test_a_detached_head_reads_detached(tmp_path: Path) -> None:
    repo = new_repo(tmp_path / "repo")
    git(repo, "switch", "-q", "--detach")
    assert state(str(repo)).stdout.startswith("branch=DETACHED ")


def test_a_folder_outside_git_or_an_unknown_base_ref_exits_1(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    outside = state(str(plain))
    assert (outside.returncode, outside.stdout) == (1, f"not a git repo: {plain}\n")
    repo = new_repo(tmp_path / "repo")
    unknown = state(str(repo), "origin/nope")
    assert (unknown.returncode, unknown.stderr) == (1, "unknown base-ref: origin/nope\n")


def test_a_worktree_listing_longer_than_a_pipe_buffer_still_prints_the_summary(
    tmp_path: Path,
) -> None:
    repo = new_repo(tmp_path / "repo")
    real_git = shutil.which("git")
    assert real_git
    # A stand-in git whose worktree listing is the real one followed by 150 KB of records, more
    # than a pipe holds, so a reader that stops after the first line leaves the writer blocked.
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$3" = worktree ]; then\n'
        f'  "{real_git}" "$@"\n'
        "  i=0\n"
        "  while [ $i -lt 5000 ]; do\n"
        '    printf "worktree /elsewhere/%s\\nHEAD 0\\n\\n" $i\n'
        "    i=$((i + 1))\n"
        "  done\n"
        "  exit 0\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    fake.chmod(0o755)
    env = {**GIT_ENV, "PATH": f"{fake.parent}{os.pathsep}{GIT_ENV['PATH']}"}
    result = subprocess.run(
        [str(SCRIPT), str(repo)], env=env, capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stdout.split()[-1]) == (0, f"main-tree={repo}")
