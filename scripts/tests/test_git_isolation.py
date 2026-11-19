"""The git fixtures under scripts/ act on their own temp repos even when the caller exports the
variables that point git at a repository (`git rebase -x` from a linked worktree exports
GIT_DIR, and so do git hooks).

The test starts a nested pytest run of two git-using tests with GIT_DIR pointing at a decoy repo
under tmp_path, which has no remote, and checks the decoy afterwards.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
GIT_USING_TESTS = [
    "scripts/tests/test_git_state.py::"
    "test_a_repo_without_upstream_reports_branch_head_dirty_count_and_main_tree",
    "scripts/codex/tests/test_breadth_native_review.py::"
    "test_native_review_leaves_the_message_as_review_md_and_no_findings",
]


def snapshot(repo: Path) -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
        ).stdout

    return {
        "head": git("symbolic-ref", "HEAD"),
        "refs": git("for-each-ref"),
        "config": git("config", "--local", "--list"),
        "files": git("ls-files", "--others", "--cached"),
    }


def test_tests_run_under_an_exported_git_dir_leave_that_repo_untouched(tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    no_global = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q", "-b", "main", str(decoy)], check=True, env=no_global)
    (decoy / "decoy.txt").write_text("decoy\n")
    subprocess.run(["git", "-C", str(decoy), "add", "decoy.txt"], check=True, env=no_global)
    subprocess.run(
        ["git", "-C", str(decoy), *GIT_IDENTITY, "commit", "-q", "-m", "decoy"],
        check=True,
        env=no_global,
    )
    before = snapshot(decoy)

    # The caller's PYTEST_ADDOPTS (a -k, --lf or --deselect) could deselect the nested targets.
    nested = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *GIT_USING_TESTS],
        cwd=ROOT,
        env={**nested, "GIT_DIR": str(decoy / ".git")},
        capture_output=True,
        text=True,
    )

    assert snapshot(decoy) == before
    assert run.returncode == 0, run.stdout + run.stderr
