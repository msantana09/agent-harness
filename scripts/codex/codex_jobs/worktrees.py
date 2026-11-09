"""Worktrees for writing jobs: creation through the repo's own script, refusals, leases and release.

A writing (`run`) job never runs in a main checkout and never on `dev`/`uat`/`main` or the repo's
default branch, and two writers never hold one worktree.

A repo whose worktrees need more than `git worktree add` (linked dependency folders, local env
files) carries two scripts, which this module runs from the repo's main checkout:

    scripts/git/worktree-add.sh <branch> <base ref>   create `<branch>` from `<base ref>` at
                                                      `<main>/.claude/worktrees/<last segment of
                                                      branch>`
    scripts/git/worktree-remove.sh <path>             remove the worktree at `<path>`

The base ref is always passed: `origin/<default branch>`, freshly fetched. A repo without the add
script gets `git worktree add` and `git worktree remove` at the same path.

A worktree is held through a lease:

    <root>/leases/<sha256(resolved worktree path)>       JSON, created with O_EXCL
    <root>/lease-locks/<sha256(resolved worktree path)>  flock target, never deleted

    {"job_id", "worktree", "main", "branch", "created"}

The lease file is the source of truth for "who holds this worktree"; the holder's job record says
whether that holder is still live. `created` is true when the service made the worktree (release
then removes it) and false for a caller-supplied one (release only drops the lease).

Every change to a lease (a claim, the replacement of a finished holder's lease, a release with its
removal, the cleanup after a failed creation) runs under the worktree's lock, re-reads the lease
there, and deletes a lease or removes a worktree only while the lease still names the job acting.
"""

import fcntl
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_jobs import records

PROTECTED_BRANCHES = frozenset({"dev", "uat", "main"})
REPO_ADD_SCRIPT = "scripts/git/worktree-add.sh"
REPO_REMOVE_SCRIPT = "scripts/git/worktree-remove.sh"
WORKTREE_DIR = ".claude/worktrees"

_LEASE_READ_ATTEMPTS = 100
_LEASE_READ_PAUSE_SECONDS = 0.02


class WorktreeError(Exception):
    """A refusal or a failed git step; the message names what blocked and where."""


@dataclass(frozen=True)
class Worktree:
    path: str
    main: str
    branch: str
    base_commit: str
    created: bool


def _git_env() -> dict[str, str]:
    # Hooks export GIT_DIR and friends; they would point every git call below at the hook's repo.
    return {
        k: v
        for k, v in os.environ.items()
        if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }


def git(cwd: str | os.PathLike[str], *args: str) -> str:
    """Run `git -C cwd <args>` and return stdout.

    A nonzero exit raises `WorktreeError` with stderr.
    """
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_git_env(),
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def main_checkout(path: str | os.PathLike[str]) -> Path:
    """The main checkout of the repo `path` belongs to.

    It is the first entry of `git worktree list --porcelain`.
    """
    for line in git(path, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree ")).resolve()
    raise WorktreeError(f"git worktree list printed no worktree for {path}")


def worktree_toplevel(path: str | os.PathLike[str]) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip()).resolve()


def refuse_main_checkout(cwd: str | os.PathLike[str]) -> Path:
    """Return the top level of the checkout `cwd` is in.

    Refuse when that is the repo's main checkout.
    """
    top = worktree_toplevel(Path(cwd).resolve())
    main = main_checkout(top)
    if top == main:
        raise WorktreeError(
            f"{top} is a main checkout; a writing job runs only in a linked worktree"
        )
    return top


def default_branch(main: str | os.PathLike[str]) -> str:
    try:
        ref = git(main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    except WorktreeError as exc:
        raise WorktreeError(
            f"cannot tell the default branch of {main}: origin/HEAD is not set "
            "(git remote set-head origin -a)"
        ) from exc
    return ref.removeprefix("origin/")


def refuse_protected_branch(main: str | os.PathLike[str], branch: str) -> None:
    if branch in PROTECTED_BRANCHES:
        raise WorktreeError(f"branch {branch} is protected; a writing job needs its own branch")
    default = default_branch(main)
    if branch == default:
        raise WorktreeError(
            f"branch {branch} is the default branch of {main}; a writing job needs its own branch"
        )


def current_branch(worktree: str | os.PathLike[str]) -> str:
    return git(worktree, "rev-parse", "--abbrev-ref", "HEAD").strip()


def repo_has_worktree_scripts(main: str | os.PathLike[str]) -> bool:
    return (Path(main) / REPO_ADD_SCRIPT).is_file()


def _leases_dir(root: Path) -> Path:
    return Path(root) / "leases"


def _digest(worktree: str | os.PathLike[str]) -> str:
    return hashlib.sha256(str(Path(worktree).resolve()).encode()).hexdigest()


def lease_path(root: Path, worktree: str | os.PathLike[str]) -> Path:
    return _leases_dir(root) / _digest(worktree)


@contextmanager
def _lease_lock(root: Path, worktree: str | os.PathLike[str]) -> Generator[None]:
    """Hold `worktree`'s lease lock.

    The file sits outside `leases/` so `find_lease` never reads it.
    """
    path = Path(root) / "lease-locks" / _digest(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_lease(path: Path) -> dict[str, Any] | None:
    """The lease at `path`, None if there is none. A file seen empty is mid-write: read it again."""
    for _ in range(_LEASE_READ_ATTEMPTS):
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        if text:
            return json.loads(text)
        time.sleep(_LEASE_READ_PAUSE_SECONDS)
    raise WorktreeError(f"lease file {path} stayed empty")


def appserver_running(job: dict[str, Any]) -> bool:
    """True while the job's app-server process group may still be alive.

    `appserver_stopped_at` is set only once `killpg(pgid, 0)` raised, so a stamp means gone;
    without one the group is probed. A job that never launched an app-server has no `pgid`.
    """
    pgid = job["pgid"]
    if pgid is None or job["appserver_stopped_at"]:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _holder_live(root: Path, lease: dict[str, Any]) -> bool:
    try:
        job = records.load_job(root, lease["job_id"])
    except records.JobNotFound:
        return False
    return job["state"] in records.ACTIVE_STATES or appserver_running(job)


def lease_holder(root: Path, worktree: str | os.PathLike[str]) -> str | None:
    """The id of the live job holding `worktree`, None when it is free or only a finished job's."""
    lease = _read_lease(lease_path(root, worktree))
    if lease is not None and _holder_live(root, lease):
        return lease["job_id"]
    return None


def _refuse_if_held(root: Path, worktree: str | os.PathLike[str]) -> None:
    """Raise `leased by <job-id>` when a live job holds `worktree`.

    The caller holds the lease lock.
    """
    existing = _read_lease(lease_path(root, worktree))
    if existing is not None and _holder_live(root, existing):
        raise WorktreeError(f"{Path(worktree).resolve()} is leased by {existing['job_id']}")


def _write_lease(
    root: Path,
    job_id: str,
    worktree: str | os.PathLike[str],
    *,
    main: str,
    branch: str,
    created: bool,
) -> None:
    """Replace any lease on `worktree` with one for `job_id`.

    The caller holds the lease lock and has run `_refuse_if_held`.
    """
    path = lease_path(root, worktree)
    payload = json.dumps(
        {
            "job_id": job_id,
            "worktree": str(Path(worktree).resolve()),
            "main": main,
            "branch": branch,
            "created": created,
        }
    )
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL creates the file before the payload lands; readers outside the lock re-read an
    # empty one.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)


def claim_lease(
    root: Path,
    job_id: str,
    worktree: str | os.PathLike[str],
    *,
    main: str,
    branch: str,
    created: bool,
) -> None:
    """Take the lease on `worktree` for `job_id`.

    Refuse with `leased by <job-id>` when a live job holds it.
    A lease left by a finished job whose app-server is gone is replaced.
    """
    with _lease_lock(root, worktree):
        _refuse_if_held(root, worktree)
        _write_lease(root, job_id, worktree, main=main, branch=branch, created=created)


def find_lease(root: Path, job_id: str) -> tuple[Path, dict[str, Any]] | None:
    leases = _leases_dir(root)
    if not leases.is_dir():
        return None
    for entry in sorted(leases.iterdir()):
        lease = _read_lease(entry)
        if lease is not None and lease["job_id"] == job_id:
            return entry, lease
    return None


def _run_script(main: Path, argv: list[str]) -> None:
    proc = subprocess.run(
        argv, cwd=main, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=_git_env()
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"{argv[0]} {' '.join(argv[1:])} failed in {main}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )


def planned_worktree_path(main: str | os.PathLike[str], branch: str) -> Path:
    """Where a service-created worktree for `branch` lands.

    The path is `<main>/.claude/worktrees/<last segment of branch>`.
    """
    return Path(main) / WORKTREE_DIR / branch.rsplit("/", 1)[-1]


def create_worktree(
    root: Path, job_id: str, project_dir: str | os.PathLike[str], branch: str
) -> Worktree:
    """Create the worktree for a writing job on a new `branch` and lease it to `job_id`.

    The base is a freshly fetched `origin/<default branch>`. A repo that carries
    `scripts/git/worktree-add.sh` builds the worktree with it, given the branch and that base;
    any other repo gets `git worktree add -b`. The worktree lands at
    `<main>/.claude/worktrees/<last segment of branch>`.
    """
    main = main_checkout(project_dir)
    git(main, "check-ref-format", "--branch", branch)
    refuse_protected_branch(main, branch)
    path = planned_worktree_path(main, branch)
    if path.exists():
        raise WorktreeError(f"{path} already exists")
    claim_lease(root, job_id, path, main=str(main), branch=branch, created=True)
    try:
        default = default_branch(main)
        git(main, "fetch", "-q", "origin", default)
        base = f"origin/{default}"
        if repo_has_worktree_scripts(main):
            _run_script(main, ["./" + REPO_ADD_SCRIPT, branch, base])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            git(main, "worktree", "add", "--no-track", "-b", branch, str(path), base)
        if not path.is_dir():
            raise WorktreeError(f"worktree creation for {branch} did not produce {path}")
        return Worktree(
            path=str(path.resolve()),
            main=str(main),
            branch=current_branch(path),
            base_commit=git(path, "rev-parse", "HEAD").strip(),
            created=True,
        )
    except BaseException:
        with _lease_lock(root, path):
            lease = _read_lease(lease_path(root, path))
            if lease is not None and lease["job_id"] == job_id:
                lease_path(root, path).unlink()
        raise


def use_worktree(root: Path, job_id: str, path: str | os.PathLike[str]) -> Worktree:
    """Lease a caller-supplied worktree to `job_id`.

    Refuse a main checkout, a protected branch, a leased or a dirty tree.
    """
    top = refuse_main_checkout(path)
    if top != Path(path).resolve():
        raise WorktreeError(
            f"{path} is inside the worktree {top}; pass the worktree's own directory"
        )
    main = main_checkout(top)
    branch = current_branch(top)
    refuse_protected_branch(main, branch)
    with _lease_lock(root, top):
        _refuse_if_held(
            root, top
        )  # a live job's edits make the tree dirty; name the holder, not the edits
        dirty = git(top, "status", "--porcelain").strip()
        if dirty:
            raise WorktreeError(f"worktree {top} is dirty; commit or discard first:\n{dirty}")
        _write_lease(root, job_id, top, main=str(main), branch=branch, created=False)
    return Worktree(
        path=str(top),
        main=str(main),
        branch=branch,
        base_commit=git(top, "rev-parse", "HEAD").strip(),
        created=False,
    )


def _refuse_unfinished(job: dict[str, Any], action: str) -> None:
    """Refuse while the job is not finished or its app-server process group may still be alive."""
    if job["state"] in records.ACTIVE_STATES:
        raise WorktreeError(
            f"job {job['job_id']} is {job['state']}; "
            f"cancel it or wait for it to finish before {action}"
        )
    if appserver_running(job):
        raise WorktreeError(
            f"job {job['job_id']}: app-server process group {job['pgid']} is still alive; "
            "stop it first"
        )


def resume_worktree(root: Path, job_id: str, parent: dict[str, Any]) -> Worktree:
    """Hand `parent`'s worktree lease to `job_id`, which continues the parent's thread there.

    The parent must be finished and hold the lease; a released or removed worktree, or one another
    job has since taken, is refused. So is a directory that is no longer a linked worktree of the
    parent's repo, and a worktree whose current branch is protected or is not the parent's branch;
    uncommitted edits stay, since the resumed job continues them. The new lease keeps the parent's
    `main`, `branch` and `created`, and the Worktree keeps the parent's `base_commit`, so the
    resumed job's commits are still measured from where the parent started.
    """
    _refuse_unfinished(parent, "resuming it")
    top = Path(parent["worktree"])
    lease = _read_lease(lease_path(root, top))
    if lease is None:
        raise WorktreeError(
            f"job {parent['job_id']} released its worktree {top}; start a new run instead"
        )
    if lease["job_id"] != parent["job_id"]:
        raise WorktreeError(f"{top} is now leased by {lease['job_id']}; resume that job instead")
    if not top.is_dir():
        raise WorktreeError(f"{top} is gone; start a new run instead")
    if (
        refuse_main_checkout(top) != top.resolve()
        or main_checkout(top) != Path(lease["main"]).resolve()
    ):
        raise WorktreeError(
            f"{top} is no longer a linked worktree of {lease['main']}; start a new run instead"
        )
    branch = current_branch(top)
    refuse_protected_branch(lease["main"], branch)
    if branch != parent["branch"]:
        raise WorktreeError(
            f"{top} is on {branch}, not job {parent['job_id']}'s branch {parent['branch']}; "
            "switch it back first"
        )
    claim_lease(
        root, job_id, top, main=lease["main"], branch=lease["branch"], created=lease["created"]
    )
    return Worktree(
        path=str(top.resolve()),
        main=lease["main"],
        branch=parent["branch"],
        base_commit=parent["base_commit"],
        created=lease["created"],
    )


def release_worktree(root: Path, job_id: str) -> dict[str, Any]:
    """Drop `job_id`'s lease, and remove the worktree when the service created it.

    Refused while the job is not finished or while its app-server process group may still be
    alive, and when another job has taken the worktree over since. The branch is left in place;
    a dirty worktree is not forced.
    """
    found = find_lease(root, job_id)
    if found is None:
        raise WorktreeError(f"job {job_id} holds no worktree lease")
    _, lease = found
    _refuse_unfinished(records.load_job(root, job_id), "releasing")
    with _lease_lock(root, lease["worktree"]):
        current = _read_lease(lease_path(root, lease["worktree"]))
        if current is None or current["job_id"] != job_id:
            holder = f" (now leased by {current['job_id']})" if current else ""
            raise WorktreeError(f"job {job_id} no longer holds {lease['worktree']}{holder}")
        if current["created"]:
            main, worktree = Path(current["main"]), Path(current["worktree"])
            if repo_has_worktree_scripts(main):
                _run_script(main, ["./" + REPO_REMOVE_SCRIPT, str(worktree)])
            else:
                git(main, "worktree", "remove", str(worktree))
            if worktree.exists():
                raise WorktreeError(f"{worktree} still exists after removal")
        lease_path(root, current["worktree"]).unlink()
    return current
