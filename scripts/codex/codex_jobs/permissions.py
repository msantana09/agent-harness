"""Permission classes: which sandbox a job's thread and turns run under, and what git says it
changed.

    read          review/consult: nothing writable, network on (gh and git ls-remote keep working)
    write         run: the job's worktree is writable, the task envelope forbids commit and push
    write-commit  run with --allow-commit: the worktree plus what a commit from it writes, and the
                  envelope permits commits

A linked worktree keeps its git data in the main repo's `.git`, so write-commit also opens the
worktree's own git dir (`<common>/worktrees/<name>`: index, HEAD reflog) and, in the shared git
dir, `objects/`, `refs/heads/` and `logs/refs/heads/`. That lets the job move any local branch of
the repo, not only its own. Tags, remote-tracking refs, `packed-refs`, `config` and the main
checkout's working tree stay read-only; git prints a harmless `Unable to create
.../packed-refs.lock` on each commit and still updates the branch.

`thread/start` takes the sandbox as a mode string and `turn/start` as a `sandboxPolicy` object
(key names from the generated app-server schema: `readOnly`, `workspaceWrite`, `writableRoots`,
`networkAccess`).
"""

import os
from pathlib import Path
from typing import Any

from codex_jobs.worktrees import WorktreeError, git

READ, WRITE, WRITE_COMMIT = "read", "write", "write-commit"
CLASSES = (READ, WRITE, WRITE_COMMIT)

NO_COMMIT_LINE = "Do not commit or push; the caller commits."
COMMIT_LINE = "You may commit your work to the current branch. Do not push; the caller pushes."


def policy_for(
    permission_class: str, worktree: str | os.PathLike[str] | None = None
) -> tuple[str, dict[str, Any]]:
    """`(thread_sandbox, sandbox_policy)` for a class; the writing classes need the worktree."""
    if permission_class == READ:
        return "read-only", {"type": "readOnly", "networkAccess": True}
    if permission_class in (WRITE, WRITE_COMMIT):
        if worktree is None:
            raise WorktreeError(f"permission class {permission_class} needs a worktree")
        roots = [Path(worktree).resolve()]
        if permission_class == WRITE_COMMIT:
            roots += _commit_roots(roots[0])
        return "workspace-write", {
            "type": "workspaceWrite",
            "writableRoots": [str(root) for root in roots],
            "networkAccess": True,
        }
    raise ValueError(f"permission class must be one of {list(CLASSES)}, got {permission_class!r}")


def _commit_roots(worktree: Path) -> list[Path]:
    """The git paths outside `worktree` that `git add` and `git commit` in it write."""
    git_dir, common = (
        Path(line).resolve()
        for line in git(
            worktree, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"
        ).splitlines()
    )
    return [
        git_dir,
        common / "objects",
        common / "refs" / "heads",
        common / "logs" / "refs" / "heads",
    ]


def permission_class_for(job: dict[str, Any]) -> str:
    """`run` jobs write (and commit when the record says `allow_commit`); every other kind reads."""
    if job["kind"] != "run":
        return READ
    return WRITE_COMMIT if job["allow_commit"] else WRITE


def sandbox_for(job: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The sandbox a job's runner sends: its class from `permission_class_for`, its `cwd` as the
    worktree."""
    return policy_for(permission_class_for(job), job["cwd"])


def fork_permission_class(
    parent_class: str,
    *,
    fork_worktree: str | os.PathLike[str] | None,
    parent_worktree: str | os.PathLike[str] | None,
) -> str:
    """The class a fork starts with: its parent's only inside a worktree of its own, otherwise
    `read`."""
    if parent_class == READ or fork_worktree is None:
        return READ
    if (
        parent_worktree is not None
        and Path(fork_worktree).resolve() == Path(parent_worktree).resolve()
    ):
        return READ
    return parent_class


def commit_line(allow_commit: bool) -> str:
    """The envelope sentence about committing, appended to a writing job's prompt."""
    return COMMIT_LINE if allow_commit else NO_COMMIT_LINE


def changed_files(worktree: str | os.PathLike[str]) -> list[str]:
    """Paths `git status --porcelain` reports (untracked files listed one by one); a rename lists
    both paths."""
    entries = git(worktree, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries) and entries[i]:
        code, path = entries[i][:2], entries[i][3:]
        paths.append(path)
        if "R" in code or "C" in code:
            i += 1
            paths.append(entries[i])
        i += 1
    return paths


def commits(worktree: str | os.PathLike[str], base: str) -> list[str]:
    """Commit ids from `git rev-list <base>..HEAD`, newest first."""
    return git(worktree, "rev-list", f"{base}..HEAD").split()
