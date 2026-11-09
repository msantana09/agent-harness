"""Job records: the per-job directory, its state machine, journal, idempotency index and result.

Layout under a caller-supplied `root`:

    <root>/<job-id>/job.json      current record, replaced atomically
    <root>/<job-id>/events.jsonl  append-only journal, one JSON object per line
    <root>/<job-id>/job.lock      flock target serialising every read-modify-write of job.json
    <root>/by-key/<sha256(key)>   idempotency index: the job id that owns the key

`job.json` holds a lifecycle: one transition function checks the allowed sources and writes
inside one lock, final states are one constant, and no caller assigns `state` through any other
path.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

JOB_ID_RE = re.compile(r"^cj-\d{8}-\d{6}-[0-9a-f]{4}$")
KINDS = ("review", "consult", "run")
SOURCE_MODES = ("resume", "fork")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

QUEUED, ADMITTED, STARTING, DISPATCHED, RUNNING = (
    "queued",
    "admitted",
    "starting",
    "dispatched",
    "running",
)
SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, TIMED_OUT, OUTCOME_UNKNOWN = (
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "timed_out",
    "outcome_unknown",
)

ACTIVE_STATES = frozenset({QUEUED, ADMITTED, STARTING, DISPATCHED, RUNNING})
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, TIMED_OUT, OUTCOME_UNKNOWN})
STATES = ACTIVE_STATES | TERMINAL_STATES

# The single transition table: current state -> states it may move to. A job that has
# reached `dispatched` may jump straight to `succeeded` because reconcile recovers a finished
# turn for a runner that died before journaling `running`. A job may time out from `starting`,
# before its turn is dispatched, when startup outlasts the timeout.
_ABORTS = frozenset({FAILED, CANCELLED, INTERRUPTED, OUTCOME_UNKNOWN})
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({ADMITTED}) | _ABORTS,
    ADMITTED: frozenset({STARTING}) | _ABORTS,
    STARTING: frozenset({DISPATCHED, TIMED_OUT}) | _ABORTS,
    DISPATCHED: frozenset({RUNNING, SUCCEEDED, TIMED_OUT}) | _ABORTS,
    RUNNING: frozenset({SUCCEEDED, TIMED_OUT}) | _ABORTS,
    **{state: frozenset() for state in TERMINAL_STATES},
}

# job.json fields other than the `state` and the two stamps, in file order.
JOB_FIELDS = (
    "job_id",
    "kind",
    "state",
    "created_at",
    "updated_at",
    "cwd",
    "project_dir",
    "output_dir",
    "model_requested",
    "model_slug",
    "catalog_fetched_at",
    "model_used",
    "effort_requested",
    "effort_used",
    "sandbox",
    "allow_commit",
    "worktree",
    "branch",
    "base_commit",
    "idempotency_key",
    "parent_job_id",
    "codex_bin",
    "images",
    "source_mode",
    "source_thread_id",
    "review_target",
    "runner_pid",
    "appserver_pid",
    "pgid",
    "appserver_stopped_at",
    "thread_id",
    "turn_id",
    "timeout_seconds",
    "admission",
    "cancel_requested_at",
    "result_path",
    "error",
    "codex_error_info",
    "recovered_via",
)
_MUTABLE_FIELDS = frozenset(JOB_FIELDS) - {"job_id", "kind", "state", "created_at", "updated_at"}

RESULT_FIELDS = (
    "job_id",
    "kind",
    "state",
    "output_dir",
    "ran",
    "task_succeeded",
    "review_approved",
    "model_requested",
    "model_used",
    "catalog_fetched_at",
    "effort_requested",
    "effort_used",
    "worktree",
    "branch",
    "commits",
    "changed_files",
    "tests_run",
    "usage",
    "recovery",
    "completed_at",
)

_KEY_READ_ATTEMPTS = 100
_KEY_READ_PAUSE_SECONDS = 0.02


class RecordError(Exception):
    """Base class for job-record failures."""


class JobNotFound(RecordError):
    pass


class IllegalTransition(RecordError):
    def __init__(self, job_id: str, current: str, target: str):
        self.job_id, self.current, self.target = job_id, current, target
        if current in TERMINAL_STATES:
            reason = f"{current} is final"
        else:
            reason = f"allowed from {current}: {sorted(TRANSITIONS[current])}"
        super().__init__(f"job {job_id}: cannot move {current} -> {target} ({reason})")


class InvalidResult(RecordError):
    pass


def now_stamp() -> str:
    return datetime.now(UTC).strftime(STAMP_FORMAT)


def parse_stamp(stamp: str) -> datetime:
    """The UTC time of a stamp written by `now_stamp` (job.json and journal timestamps)."""
    return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)


def _job_dir(root: Path, job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id):
        raise JobNotFound(f"not a job id: {job_id!r}")
    return Path(root) / job_id


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write `payload` to `path` through a temp file in the same directory and `os.replace`."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


@contextmanager
def _locked(job_dir: Path) -> Generator[None]:
    fd = os.open(job_dir / "job.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_record(job_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((job_dir / "job.json").read_text())
    except FileNotFoundError:
        raise JobNotFound(f"no job record at {job_dir / 'job.json'}") from None


def append_event(job_dir: Path, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append one journal line and return it. A single O_APPEND write keeps lines whole."""
    event = {"ts": now_stamp(), "kind": kind, "data": data or {}}
    line = (json.dumps(event) + "\n").encode()
    fd = os.open(Path(job_dir) / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return event


def read_events(job_dir: Path) -> list[dict[str, Any]]:
    """Return the journal in order. An unterminated final line (a crash mid-append) is dropped."""
    path = Path(job_dir) / "events.jsonl"
    if not path.exists():
        return []
    text = path.read_text()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    elif lines:
        lines.pop()
    return [json.loads(line) for line in lines]


def key_path(root: Path, key: str) -> Path:
    return Path(root) / "by-key" / hashlib.sha256(key.encode()).hexdigest()


def _key_owner(path: Path) -> str:
    """The job id in the index entry at `path`; the winner writes it straight after creating the file, so an empty entry is read again briefly."""
    for _ in range(_KEY_READ_ATTEMPTS):
        owner = path.read_text().strip()
        if owner:
            return owner
        time.sleep(_KEY_READ_PAUSE_SECONDS)
    raise RecordError(f"idempotency entry {path} stayed empty")


def job_for_key(root: Path, key: str) -> str | None:
    """The id of the job that owns `key`, None when no job does."""
    path = key_path(root, key)
    return _key_owner(path) if path.exists() else None


def claim_idempotency_key(root: Path, key: str, job_id: str) -> str:
    """Bind `key` to `job_id` unless taken; return the job id that owns the key.

    The index entry is created with O_CREAT|O_EXCL, so of any number of concurrent claims
    exactly one creates it. A loser reads the winner's id.
    """
    path = key_path(root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _key_owner(path)
    try:
        os.write(fd, job_id.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return job_id


def _new_job_id(now: datetime) -> str:
    return f"cj-{now:%Y%m%d}-{now:%H%M%S}-{secrets.token_hex(2)}"


def create_job(
    root: Path,
    *,
    kind: str,
    cwd: str,
    project_dir: str,
    model_requested: str,
    model_slug: str,
    effort_requested: str,
    sandbox: str,
    timeout_seconds: int,
    allow_commit: bool = False,
    output_dir: str | None = None,
    idempotency_key: str | None = None,
    parent_job_id: str | None = None,
    codex_bin: str,
    images: list[str] | None = None,
    source_mode: str | None = None,
    source_thread_id: str | None = None,
    review_target: dict[str, Any] | None = None,
    catalog_fetched_at: str | None = None,
    runner_pid: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a `queued` job and return `(record, created)`.

    `model_requested` is what the caller asked for (a tier or a slug); `model_slug` is the model id
    it resolved to, which the runner sends to Codex without resolving again, and
    `catalog_fetched_at` is the model catalog's `fetched_at` at that resolution. `codex_bin` is the
    resolved Codex executable the runner launches. `images` are absolute
    paths sent with the prompt. `allow_commit` lets a `run` job commit to its branch; `worktree`,
    `branch` and `base_commit` are set (with `cwd`) once the job holds its worktree. `source_mode` (`resume` or `fork`) and `source_thread_id` go
    together: the job continues or branches that thread instead of starting a new one.
    `review_target` (a `review/start` target object) makes a review job run Codex's built-in reviewer on it.
    `runner_pid` is the live process that owns the job from the first write: the submitting CLI,
    until the runner it spawns records its own pid.

    With an idempotency key already owned by another job, no job is created: the owner's
    record is returned with `created=False`.
    """
    if kind not in KINDS:
        raise RecordError(f"kind must be one of {list(KINDS)}, got {kind!r}")
    images = list(images or [])
    relative = [path for path in images if not os.path.isabs(path)]
    if relative:
        raise RecordError(f"image paths must be absolute, got {relative}")
    if source_mode is not None and source_mode not in SOURCE_MODES:
        raise RecordError(
            f"source_mode must be one of {list(SOURCE_MODES)} or None, got {source_mode!r}"
        )
    if (source_mode is None) != (source_thread_id is None):
        raise RecordError("source_mode and source_thread_id are set together or not at all")
    if review_target is not None and kind != "review":
        raise RecordError(f"review_target belongs to review jobs, not {kind!r}")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    while True:
        now = datetime.now(UTC)
        job_id = _new_job_id(now)
        job_dir = root / job_id
        try:
            job_dir.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    job_dir.chmod(0o700)

    stamp = now_stamp()
    record: dict[str, Any] = dict.fromkeys(JOB_FIELDS)
    record.update(
        job_id=job_id,
        kind=kind,
        state=QUEUED,
        created_at=stamp,
        updated_at=stamp,
        cwd=cwd,
        project_dir=project_dir,
        output_dir=output_dir,
        model_requested=model_requested,
        model_slug=model_slug,
        catalog_fetched_at=catalog_fetched_at,
        effort_requested=effort_requested,
        sandbox=sandbox,
        allow_commit=allow_commit,
        idempotency_key=idempotency_key,
        parent_job_id=parent_job_id,
        codex_bin=codex_bin,
        images=images,
        source_mode=source_mode,
        source_thread_id=source_thread_id,
        review_target=review_target,
        timeout_seconds=timeout_seconds,
        runner_pid=runner_pid,
    )
    _write_json_atomic(job_dir / "job.json", record)
    append_event(job_dir, "created", {"state": QUEUED, "kind": kind})

    if idempotency_key is not None:
        owner = claim_idempotency_key(root, idempotency_key, job_id)
        if owner != job_id:
            shutil.rmtree(job_dir)
            return load_job(root, owner), False
    return record, True


def load_job(root: Path, job_id: str) -> dict[str, Any]:
    return _read_record(_job_dir(root, job_id))


def list_jobs(root: Path) -> list[dict[str, Any]]:
    """Every job under `root`, oldest first. A directory without `job.json` is mid-creation."""
    root = Path(root)
    if not root.is_dir():
        return []
    jobs = []
    for entry in sorted(root.iterdir()):
        if JOB_ID_RE.match(entry.name) and (entry / "job.json").exists():
            jobs.append(_read_record(entry))
    return jobs


def transition(
    root: Path, job_id: str, to_state: str, *, data: dict[str, Any] | None = None, **fields: Any
) -> dict[str, Any]:
    """Move a job to `to_state` if the table allows it, merging `fields` in the same write.

    The read, the check, the write and the journal line happen under the job's lock, so two
    movers cannot both start from the same state. Raises `IllegalTransition` otherwise.
    """
    _check_fields(fields)
    job_dir = _job_dir(root, job_id)
    with _locked(job_dir):
        record = _read_record(job_dir)
        current = record["state"]
        if to_state not in STATES or to_state not in TRANSITIONS[current]:
            raise IllegalTransition(job_id, current, to_state)
        record.update(fields)
        record["state"] = to_state
        record["updated_at"] = now_stamp()
        _write_json_atomic(job_dir / "job.json", record)
        append_event(
            job_dir, "transition", {"from": current, "to": to_state, **(data or {}), **fields}
        )
    return record


def update_job(root: Path, job_id: str, **fields: Any) -> dict[str, Any]:
    """Merge non-state fields into the current record inside the lock (never `state`)."""
    _check_fields(fields)
    job_dir = _job_dir(root, job_id)
    with _locked(job_dir):
        record = _read_record(job_dir)
        record.update(fields)
        record["updated_at"] = now_stamp()
        _write_json_atomic(job_dir / "job.json", record)
    return record


def _check_fields(fields: dict[str, Any]) -> None:
    unknown = sorted(set(fields) - _MUTABLE_FIELDS)
    if unknown:
        raise RecordError(f"fields not writable on a job record: {unknown}")


def build_result(
    job: dict[str, Any],
    *,
    ran: bool | None,
    task_succeeded: bool | None,
    review_approved: bool | None,
    worktree: str | None,
    branch: str | None,
    commits: list[str],
    changed_files: list[str],
    tests_run: list[Any],
    usage: dict[str, Any] | None,
    recovery: dict[str, Any] | None,
    catalog_fetched_at: str | None = None,
) -> dict[str, Any]:
    """Assemble the `result.json` envelope for a job.

    `ran`, `task_succeeded` and `review_approved` are separate on purpose: a review that ran
    and found problems is `ran=True, review_approved=False`. `ran` is null only on an
    `outcome_unknown` job whose turn may or may not have started. `changed_files` and `commits`
    come from the caller's git inspection, never from anything the model reported.
    """
    result = {
        "job_id": job["job_id"],
        "kind": job["kind"],
        "state": job["state"],
        "output_dir": job["output_dir"],
        "ran": ran,
        "task_succeeded": task_succeeded,
        "review_approved": review_approved,
        "model_requested": job["model_requested"],
        "model_used": job["model_used"],
        "catalog_fetched_at": catalog_fetched_at,
        "effort_requested": job["effort_requested"],
        "effort_used": job["effort_used"],
        "worktree": worktree,
        "branch": branch,
        "commits": list(commits),
        "changed_files": list(changed_files),
        "tests_run": list(tests_run),
        "usage": usage,
        "recovery": recovery,
        "completed_at": now_stamp(),
    }
    validate_result(result)
    return result


def validate_result(result: dict[str, Any]) -> None:
    """Raise `InvalidResult` listing every way `result` departs from the envelope."""
    problems: list[str] = []
    missing = [name for name in RESULT_FIELDS if name not in result]
    extra = sorted(set(result) - set(RESULT_FIELDS))
    if missing:
        problems.append(f"missing fields: {missing}")
    if extra:
        problems.append(f"unknown fields: {extra}")

    def check(name: str, ok: bool, expected: str) -> None:
        if name in result and not ok:
            problems.append(f"{name} must be {expected}, got {result[name]!r}")

    def is_bool(value: Any) -> bool:
        return isinstance(value, bool)

    def is_str_list(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    def is_opt(kind: type, value: Any) -> bool:
        return value is None or isinstance(value, kind)

    check("kind", result.get("kind") in KINDS, f"one of {list(KINDS)}")
    check(
        "state",
        result.get("state") in TERMINAL_STATES,
        f"a terminal state {sorted(TERMINAL_STATES)}",
    )
    check(
        "ran",
        is_bool(result.get("ran"))
        or (result.get("ran") is None and result.get("state") == OUTCOME_UNKNOWN),
        "a boolean, or null only with outcome_unknown",
    )
    check(
        "task_succeeded",
        result.get("task_succeeded") is None or is_bool(result.get("task_succeeded")),
        "true, false or null",
    )
    check(
        "review_approved",
        result.get("review_approved") is None or is_bool(result.get("review_approved")),
        "true, false or null",
    )
    for name in (
        "output_dir",
        "model_requested",
        "model_used",
        "effort_requested",
        "effort_used",
        "worktree",
        "branch",
        "catalog_fetched_at",
    ):
        check(name, is_opt(str, result.get(name)), "a string or null")
    for name in ("commits", "changed_files"):
        check(name, is_str_list(result.get(name)), "a list of strings")
    check("tests_run", isinstance(result.get("tests_run"), list), "a list")
    check("usage", is_opt(dict, result.get("usage")), "an object or null")
    check("recovery", is_opt(dict, result.get("recovery")), "an object or null")
    if result.get("ran") is False and result.get("task_succeeded") is True:
        problems.append("task_succeeded cannot be true when ran is false")
    if result.get("review_approved") is not None and result.get("kind") != "review":
        problems.append("review_approved is only set on review jobs")
    if problems:
        raise InvalidResult("; ".join(problems))
