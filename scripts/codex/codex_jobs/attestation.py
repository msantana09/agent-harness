"""Attestation: binds a review to the exact git state it read, for a gate that checks it later.

`snapshot` records the repository state before the review turn is dispatched and `save` keeps it
in the job directory, so a process that settles the job later (an adopting runner, `reconcile`)
finds it. `write` runs at every terminal state: it recomputes the head and the diff, and the
attestation says `exit_status: "ok"` only when the review completed and both still match.

A native review (Codex's built-in reviewer) reads the working tree, not the commits, so it is
attested only when no tracked file has staged or unstaged changes, at launch and at completion.

The contract a gate recomputes, byte for byte: the diff hash is the sha256 of the stdout of
`git diff --no-color --no-ext-diff <merge-base> HEAD`, with the merge base taken against the
repository's own default branch (`origin/HEAD`), and the file is `attestation.json` under
`<repo>/.artifacts/codex/`, which is where an auto-created output directory lives.
"""

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SNAPSHOT_FILE = "attestation-snapshot.json"
ATTESTATION_FILE = "attestation.json"
HEAD_MOVED = "failure: head moved during review"
TREE_CHANGED = "failure: tracked files changed during review"
UNREADABLE = "failure: the repository could not be read at completion"

# The diff flags of the hash contract above; a gate that recomputes the hash uses the same ones.
DIFF_FLAGS = ("--no-color", "--no-ext-diff")


class AttestationError(Exception):
    """The repository state cannot be read, so the review cannot be attested."""


@dataclass(frozen=True)
class Snapshot:
    """The reviewed git state. Every git field is None when the directory is not in a git
    repository, and `head_sha`, `merge_base_sha` and `diff_sha256` are None before the
    repository's first commit."""

    repo_root: str | None
    git_branch: str | None
    merge_base_sha: str | None
    head_sha: str | None
    diff_sha256: str | None
    prompt_sha256: str | None
    review_target: dict[str, str] | None = (
        None  # a native review's `review/start` target; None for a prompted review
    )
    target_merge_base_sha: str | None = (
        None  # merge base of a `baseBranch` target's branch and HEAD
    )
    uncommitted_tracked: str | None = (
        None  # `git status --porcelain --untracked-files=no`; None when clean
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    except OSError as exc:
        raise AttestationError(f"cannot run git in {cwd}: {exc}") from exc


def _git_out(cwd: Path, *args: str) -> bytes:
    done = _git(cwd, *args)
    if done.returncode != 0:
        raise AttestationError(
            f"git {' '.join(args)} failed in {cwd}: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout


def _state(cwd: Path) -> dict[str, str | None]:
    """The git fields of a snapshot for `cwd`: all None outside a repository; the head, merge base
    and diff None in a repository whose HEAD names no commit yet (an unborn branch); and the merge
    base and diff None in a repository without `origin/HEAD`, which has no base branch to diff
    against."""
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return dict.fromkeys(
            (
                "repo_root",
                "git_branch",
                "merge_base_sha",
                "head_sha",
                "diff_sha256",
                "uncommitted_tracked",
            )
        )
    repo_root = Path(top.stdout.decode().strip())
    head = _git(repo_root, "rev-parse", "--verify", "--quiet", "HEAD")
    state: dict[str, str | None] = {
        "repo_root": str(repo_root),
        "git_branch": _git_out(repo_root, "branch", "--show-current").decode().strip() or None,
        "merge_base_sha": None,
        "head_sha": head.stdout.decode().strip() if head.returncode == 0 else None,
        "diff_sha256": None,
        "uncommitted_tracked": _git_out(repo_root, "status", "--porcelain", "--untracked-files=no")
        .decode()
        .rstrip()
        or None,
    }
    if state["head_sha"] is None:
        return state
    base = _git(repo_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if base.returncode == 0:
        merge_base = (
            _git_out(repo_root, "merge-base", base.stdout.decode().strip(), "HEAD").decode().strip()
        )
        diff = _git_out(repo_root, "diff", *DIFF_FLAGS, merge_base, "HEAD")
        state.update(merge_base_sha=merge_base, diff_sha256=hashlib.sha256(diff).hexdigest())
    return state


def _not_attestable(snap: Snapshot) -> str | None:
    """Why `snap` cannot bind a review to a diff, or None when it can."""
    if snap.repo_root is None:
        return "not a git repository"
    if snap.head_sha is None:
        return "unborn HEAD: the repository has no commit yet"
    if snap.diff_sha256 is None:
        return (
            "no origin/HEAD, so no base branch to diff against (`git remote set-head origin "
            "--auto` sets it)"
        )
    target = snap.review_target
    if target is not None and target["type"] != "baseBranch":
        return f"a native review of {target['type']} does not cover the branch's diff from its base"
    if target is not None and snap.target_merge_base_sha != snap.merge_base_sha:
        return (
            f"a native review against {target['branch']} did not review the diff from the "
            "attested base"
        )
    if target is not None and snap.uncommitted_tracked:
        return _uncommitted(snap.uncommitted_tracked)
    return None


def _uncommitted(porcelain: str) -> str:
    return (
        "a native review reads the working tree, and tracked files have uncommitted changes: "
        f"{'; '.join(porcelain.splitlines())}"
    )


def snapshot(
    cwd: Path, prompt_file: Path | None, *, review_target: dict[str, str] | None = None
) -> Snapshot:
    """The repository state at `cwd` and the hash of the prompt file (None for a review that has
    no prompt). Call before the review is dispatched.

    A native review passes its `review_target`: only a `baseBranch` target whose merge base with
    HEAD is the attested merge base reviewed the diff the gate checks.
    """
    prompt_sha256 = (
        hashlib.sha256(Path(prompt_file).read_bytes()).hexdigest()
        if prompt_file is not None
        else None
    )
    state = _state(Path(cwd))
    target_merge_base = None
    if (
        review_target is not None
        and review_target["type"] == "baseBranch"
        and state["repo_root"] is not None
    ):
        found = _git(Path(state["repo_root"]), "merge-base", review_target["branch"], "HEAD")
        target_merge_base = found.stdout.decode().strip() if found.returncode == 0 else None
    return Snapshot(
        **state,
        prompt_sha256=prompt_sha256,
        review_target=review_target,
        target_merge_base_sha=target_merge_base,
    )


def save(job_dir: Path, snap: Snapshot) -> None:
    (Path(job_dir) / SNAPSHOT_FILE).write_text(json.dumps(asdict(snap), indent=2) + "\n")


def load(job_dir: Path) -> Snapshot | None:
    """The snapshot saved for the job, or None if the job ended before one was taken."""
    path = Path(job_dir) / SNAPSHOT_FILE
    return Snapshot(**json.loads(path.read_text())) if path.exists() else None


def complete(snap: Snapshot | None, job: dict[str, Any], failure: str | None) -> dict[str, Any]:
    """The attestation for a job that has reached a terminal state.

    `failure` is why the review did not complete (None when it did). The git state is recomputed
    here; a review whose head or diff differs from the snapshot is `HEAD_MOVED`, a native review
    whose tracked files have uncommitted changes now but not at launch is `TREE_CHANGED`, and one
    whose state can no longer be read is `UNREADABLE`. With no snapshot the git fields are null
    and the status is a failure, and so is the status of a snapshot taken outside a repository,
    before its first commit, without a base, or of a native review with uncommitted changes at
    launch.
    """
    if failure is not None:
        exit_status = f"failure: {failure}"
    elif snap is None:
        exit_status = "failure: no snapshot was taken before dispatch"
    elif (reason := _not_attestable(snap)) is not None:
        exit_status = f"failure: not attestable: {reason}"
    else:
        try:
            now = _state(Path(job["cwd"]))
            if (now["head_sha"], now["diff_sha256"]) != (snap.head_sha, snap.diff_sha256):
                exit_status = HEAD_MOVED
            elif snap.review_target is not None and now["uncommitted_tracked"]:
                exit_status = (
                    f"{TREE_CHANGED}: {'; '.join(now['uncommitted_tracked'].splitlines())}"
                )
            else:
                exit_status = "ok"
        except AttestationError as exc:
            exit_status = f"{UNREADABLE}: {exc}"
    git_fields = asdict(snap) if snap is not None else dict.fromkeys(Snapshot.__dataclass_fields__)
    return {
        **git_fields,
        "job_id": job["job_id"],
        "thread_id": job["thread_id"],
        "cwd": job["cwd"],
        "sandbox": job["sandbox"],
        "model": job["model_used"] or job["model_slug"],
        "effort": job["effort_used"] or job["effort_requested"],
        "completed_at": datetime.now(UTC).isoformat(),
        "exit_status": exit_status,
    }


def stale(exit_status: str) -> bool:
    """Whether a completed review read a state the repository no longer has, or one that can no
    longer be checked: its findings do not describe the branch as it is now."""
    return exit_status == HEAD_MOVED or exit_status.startswith((TREE_CHANGED, UNREADABLE))


def write(job_dir: Path, job: dict[str, Any], failure: str | None) -> dict[str, Any]:
    """Build the job's attestation from its saved snapshot and write `attestation.json` in
    `job_dir`."""
    attestation = complete(load(job_dir), job, failure)
    (Path(job_dir) / ATTESTATION_FILE).write_text(json.dumps(attestation, indent=2) + "\n")
    return attestation
