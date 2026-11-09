"""The per-job runner: drives one Codex turn through a private app-server and records the outcome.

`python3 -m codex_jobs.runner <job_dir> [--adopt] [--crash-at POINT]` is started detached by
`spawn_runner`. It owns one job from `queued` to a terminal state: slot admission, app-server
launch, usage check, thread, turn, journal, result, delivery, app-server shutdown. `--adopt` is for
a job whose
runner died while its turn still ran: the new runner waits for that turn instead of starting one.

A runner never sends a second `turn/start`. Just before sending it the runner journals a
`dispatching` event naming the thread and the turns it already held, so a turn whose acceptance
was never recorded can still be found. A native review's `review/start` is sent the same way,
and everything said here of `turn/start` holds for it. Once the turn is sent, anything that kills or
disconnects the runner leaves the job non-terminal for `reconcile` to settle.

`finalize` and `stop_server` are shared with `reconcile`, which settles jobs without a runner.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import admission, appserver, attestation, permissions, records, review
from .appserver import AppServerClient, RpcError
from .worktrees import WorktreeError

PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1]
)  # scripts/codex, the PYTHONPATH of a spawned runner
SCHEMAS_DIR = PACKAGE_ROOT / "schemas"

CRASH_POINTS = (
    "before-dispatch",
    "after-dispatch-reply",
    "after-accept",
    "mid-command",
    "after-complete",
    "before-deliver",
    "before-stop",
)
POLL_SECONDS = 1.0
INTERRUPT_GRACE_SECONDS = 30.0
NEW_JOB_GRACE_SECONDS = 60.0  # a job created with no owning pid counts as starting, for this long
USAGE_LIMIT_ERROR = (
    "usageLimitExceeded"  # the `codexErrorInfo` of a turn that ran out of the account's usage
)

# Every file a job can deliver to its `output_dir`.
DELIVERED_FILES = ("result.json", "answer.md", *review.DELIVERABLES, attestation.ATTESTATION_FILE)

_CONSULT_CONTRACT = (
    "OUTPUT CONTRACT: Return the JSON object required by the output schema. Set status to "
    "completed only if you performed the requested work. Set status to blocked if tool access or "
    "missing inputs prevent it, or failed for another execution failure. Put the complete Markdown "
    "answer in answer, following the prompt's response format. List questions the caller must "
    "resolve in open_questions (an empty array if none). This envelope takes precedence over "
    "instructions about final-message formatting in the prompt. Do not create, modify, or delete "
    "repository files or change git state."
)

_RUN_CONTRACT = (
    "OUTPUT CONTRACT: Return the JSON object required by the output schema. Set status to "
    "completed only if you performed the requested work. Set status to blocked if tool access or "
    "missing inputs prevent it, or failed for another execution failure. Put a Markdown summary of "
    "what you did in summary, list the files you changed in changed_files and the commands you ran "
    "to check the work in tests_run, and list anything that stopped you in blockers (an empty "
    "array if none). This envelope takes precedence over instructions about final-message "
    "formatting in the prompt. Work only inside the current working directory."
)

# Codex may ask to run a command outside the sandbox. Every request goes to this runner, which
# declines
# and journals it: never to Codex's own auto-reviewer (a user's `approvals_reviewer =
# "auto_review"`),
# which could grant a read-only job write access.
APPROVAL_POLICY = "on-request"
APPROVALS_REVIEWER = "user"

# Server requests a headless run answers by refusing, and the response shape that refuses each.
_REFUSALS: dict[str, dict[str, Any]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
}

_spawned: dict[int, subprocess.Popen[bytes]] = {}


class RunnerError(Exception):
    """The job cannot run as configured."""


class InvalidAnswer(Exception):
    """The model's final message is not a valid answer for the job's kind."""


@dataclass
class TurnOutcome:
    """How a turn ended. `status` is a turn status (`completed`, `failed`, `interrupted`) or
    `unknown`.

    `usage` is the turn's last `thread/tokenUsage/updated` token counts; `error_info` the failed
    turn's `codexErrorInfo`; `rate_limits_after` the account's limits read once the turn ended.
    """

    status: str
    final_text: str | None = None
    error: str | None = None
    usage: dict[str, Any] | None = None
    timed_out: bool = False
    error_info: Any = None
    rate_limits_after: dict[str, Any] | None = None


def spawn_runner(
    job_dir: Path,
    *,
    adopt: bool = False,
    crash_at: str | None = None,
    recheck_seconds: float | None = None,
) -> subprocess.Popen[bytes]:
    """Start the runner for `job_dir` detached in its own session and record its pid on the job.

    stdout and stderr append to `<job_dir>/runner.log`. The returned `Popen` lets a caller that
    waits on the job `poll()` it, which reaps the child; `reap_spawned` reaps the ones nobody waits
    on.
    """
    reap_spawned()
    job_dir = Path(job_dir)
    argv = [sys.executable, "-m", "codex_jobs.runner", str(job_dir)]
    if adopt:
        argv.append("--adopt")
    if crash_at is not None:
        argv += ["--crash-at", crash_at]
    if recheck_seconds is not None:
        argv += ["--recheck-seconds", str(recheck_seconds)]
    existing = os.environ.get("PYTHONPATH")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(PACKAGE_ROOT), *([existing] if existing else [])]),
    }
    with open(job_dir / "runner.log", "ab") as log:
        process = subprocess.Popen(
            argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
        )
    _spawned[process.pid] = process
    records.update_job(job_dir.parent, job_dir.name, runner_pid=process.pid)
    return process


def reap_spawned() -> None:
    """Reap every runner this process spawned that has exited, whatever its job's state.

    A long-lived process (the MCP server) spawns a runner per job and stops looking at a job once
    it is terminal, so an exited runner would otherwise stay a zombie. Running runners are left
    alone.
    """
    for pid, process in list(_spawned.items()):
        if process.poll() is not None:
            _spawned.pop(pid, None)


def spawned_runner(pid: int) -> subprocess.Popen[bytes] | None:
    """The `Popen` of a runner this process spawned with `pid`, or None for any other process."""
    return _spawned.get(pid)


def runner_alive(job: dict[str, Any]) -> bool:
    """Whether the process that owns the job exists: its runner, or the CLI that submitted it while
    that CLI prepares the job (worktree, output directory) and before the runner records its pid.
    A job with no pid at all is alive while new."""
    pid = job["runner_pid"]
    if pid is None:
        age = datetime.now(UTC) - records.parse_stamp(job["created_at"])
        return age.total_seconds() < NEW_JOB_GRACE_SECONDS
    process = _spawned.get(pid)
    if process is not None:
        if process.poll() is not None:
            _spawned.pop(pid, None)
            return False
        return True
    try:
        os.kill(pid, 0)
    except (
        ProcessLookupError,
        PermissionError,
    ):  # gone, or another user's process now owns the pid
        return False
    return True


def _kind_spec(job: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """`(output schema, output contract text)` for the job's kind."""
    if job["kind"] == "review":
        return review.load_schema(), review.REVIEW_CONTRACT
    if job["kind"] == "run":
        return json.loads((SCHEMAS_DIR / "task.schema.json").read_text()), _RUN_CONTRACT
    return json.loads((SCHEMAS_DIR / "consult.schema.json").read_text()), _CONSULT_CONTRACT


def preflight_code_mode_host(codex_bin: str, log_path: Path) -> None:
    """Run the `codex-code-mode-host` next to `codex_bin`, if any, so a quarantined host fails
    before inference."""
    host = Path(codex_bin).with_name("codex-code-mode-host")
    with open(log_path, "wb") as log:
        if not host.is_file():
            log.write(b"No adjacent native host; package-specific startup is handled by Codex.\n")
            return
        try:
            subprocess.run(
                [str(host), "--help"],
                check=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.write(f"Host startup failed: {host}: {exc}\n".encode())
            raise RunnerError(
                f"code-mode host startup failed ({host}: {exc}); check macOS launch approval or "
                f"select another codex binary; see {log_path}"
            ) from exc


def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Problems with `value` against the JSON Schema subset the job schemas use.

    Supported: `type` (object, array, string, boolean, integer, number, null), `enum`,
    `properties`, `required`, `additionalProperties: false`, and `items`.
    """
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "integer": int,
        "number": (int, float),
        "null": type(None),
    }
    expected = schema.get("type")
    if expected is not None:
        ok = isinstance(value, types[expected]) and not (
            expected in ("integer", "number") and isinstance(value, bool)
        )
        if not ok:
            return [f"{path} must be {expected}"]
    problems: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path} must be one of {schema['enum']}, got {value!r}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems += [
            f"{path}.{name} is missing" for name in schema.get("required", []) if name not in value
        ]
        if schema.get("additionalProperties") is False:
            problems += [
                f"{path}.{name} is not allowed" for name in value if name not in properties
            ]
        for name, sub in properties.items():
            if name in value:
                problems += validate_instance(value[name], sub, f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            problems += validate_instance(item, schema["items"], f"{path}[{index}]")
    return problems


def parse_answer(job: dict[str, Any], text: str | None) -> dict[str, Any]:
    """The model's final message as JSON that satisfies the job kind's schema, else
    `InvalidAnswer`."""
    if text is None:
        raise InvalidAnswer("the turn ended without a final answer")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidAnswer(f"not JSON ({exc})") from exc
    schema, _ = _kind_spec(job)
    problems = validate_instance(value, schema)
    if problems:
        raise InvalidAnswer("; ".join(problems))
    return value


def _pages(
    client: AppServerClient, method: str, params: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """The entries of a paginated list method, following `nextCursor` until it is null."""
    cursor: str | None = None
    while True:
        page = client.call(method, {**params, **({"cursor": cursor} if cursor else {})}, timeout=60)
        yield from page["data"]
        cursor = page.get("nextCursor")
        if not cursor:
            return


def _turns(client: AppServerClient, thread_id: str) -> Iterator[dict[str, Any]]:
    """The thread's turns from `thread/turns/list`, newest first, without their items."""
    return _pages(client, "thread/turns/list", {"threadId": thread_id, "itemsView": "notLoaded"})


def read_turn(client: AppServerClient, thread_id: str, turn_id: str) -> dict[str, Any] | None:
    """The turn's status and error from `thread/turns/list`, or None if the thread has no such
    turn."""
    return next((turn for turn in _turns(client, thread_id) if turn["id"] == turn_id), None)


def list_turn_ids(client: AppServerClient, thread_id: str) -> list[str]:
    """The ids of every turn on the thread, newest first."""
    return [turn["id"] for turn in _turns(client, thread_id)]


def thread_is_active(client: AppServerClient, thread_id: str) -> bool:
    """Whether the server is running a turn on the thread, from a metadata-only `thread/read`."""
    return (
        client.call("thread/read", {"threadId": thread_id}, timeout=60)["thread"]
        .get("status", {})
        .get("type")
        == "active"
    )


def is_final_message(item: dict[str, Any], *, native: bool = False) -> bool:
    """Whether `item` is the agent message that ends a turn: phase `final_answer`, or for a native
    review (whose message has no phase) any phase-less one."""
    if item.get("type") != "agentMessage":
        return False
    return item.get("phase") == "final_answer" or (native and item.get("phase") is None)


def read_final_text(
    client: AppServerClient, thread_id: str, turn_id: str, *, native: bool = False
) -> str | None:
    """The turn's last final agent message from `thread/items/list`, or None if it has none."""
    entries = _pages(
        client,
        "thread/items/list",
        {"threadId": thread_id, "turnId": turn_id, "sortDirection": "desc"},
    )
    return next(
        (e["item"]["text"] for e in entries if is_final_message(e["item"], native=native)), None
    )


def final_text_of(turn: dict[str, Any], *, native: bool = False) -> str | None:
    """The final answer among the items a `turn/completed` notification carries."""
    final = [i for i in turn.get("items", []) if is_final_message(i, native=native)]
    return final[-1]["text"] if final else None


def outcome_of_turn(turn: dict[str, Any], *, native: bool = False) -> TurnOutcome:
    error = turn.get("error")
    return TurnOutcome(
        status=turn["status"],
        final_text=final_text_of(turn, native=native),
        error=error.get("message") if isinstance(error, dict) else error,
        error_info=error.get("codexErrorInfo") if isinstance(error, dict) else None,
    )


def settled_outcome(
    client: AppServerClient, thread_id: str, turn: dict[str, Any], *, native: bool = False
) -> TurnOutcome:
    """The outcome of a finished turn; a completed turn whose payload lacks its final answer gets it
    from `thread/items/list`."""
    outcome = outcome_of_turn(turn, native=native)
    if outcome.status == "completed" and outcome.final_text is None:
        outcome.final_text = read_final_text(client, thread_id, turn["id"], native=native)
    return outcome


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _terminal_for(
    job: dict[str, Any], outcome: TurnOutcome
) -> tuple[str, str | None, dict[str, Any] | None]:
    """`(terminal state, error, parsed answer)` for how the job's turn ended."""
    if outcome.timed_out:
        return records.TIMED_OUT, f"timed out after {job['timeout_seconds']}s", None
    if outcome.status == "interrupted":
        if job["cancel_requested_at"]:
            return records.CANCELLED, None, None
        return records.INTERRUPTED, "the turn was interrupted by another client", None
    if outcome.status == "failed":
        return records.FAILED, outcome.error or "the turn failed", None
    if outcome.status == "completed" and job["review_target"] is not None:
        if not (outcome.final_text or "").strip():
            return records.FAILED, "the review ended without a message", None
        return records.SUCCEEDED, None, {"review": outcome.final_text}
    if outcome.status == "completed":
        try:
            return records.SUCCEEDED, None, parse_answer(job, outcome.final_text)
        except InvalidAnswer as exc:
            return records.FAILED, f"final answer rejected: {exc}", None
    return (
        records.OUTCOME_UNKNOWN,
        outcome.error or "the turn's outcome could not be determined",
        None,
    )


def _answer_markdown(job: dict[str, Any], answer: dict[str, Any]) -> str:
    text, heading, items = (
        (answer["summary"], "Blockers", answer["blockers"])
        if job["kind"] == "run"
        else (answer["answer"], "Open questions", answer["open_questions"])
    )
    if items:
        text = (
            text.rstrip("\n")
            + f"\n\n## {heading}\n\n"
            + "\n".join(f"- {item}" for item in items)
            + "\n"
        )
    return text


def _deliver(job_dir: Path, output_dir: Path) -> None:
    """Copy the job's files into `output_dir`, first removing every file name the service delivers,
    so a directory reused across jobs holds only this job's files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in DELIVERED_FILES:
        (output_dir / name).unlink(missing_ok=True)
    for name in DELIVERED_FILES:
        if (job_dir / name).exists():
            _write_atomic(output_dir / name, (job_dir / name).read_text())
    records.append_event(job_dir, "delivered", {"output_dir": str(output_dir)})


def unconfirmed_dispatch(job_dir: Path) -> dict[str, Any] | None:
    """The `dispatching` event's data when `turn/start` was sent and the server did not refuse it,
    else None.

    The data names the thread and the ids of the turns it held before the send (`prior_turn_ids`).
    Callers check `turn_id` themselves: a recorded turn id confirms the dispatch.
    """
    sent = None
    for event in records.read_events(job_dir):
        if event["kind"] == "dispatching":
            sent = event["data"]
        elif event["kind"] == "dispatch_refused":
            sent = None
    return sent


def _timeout_journaled(job_dir: Path, turn_id: str | None) -> bool:
    """Whether a runner journaled a `timeout` (and asked the server to interrupt) for `turn_id`."""
    return turn_id is not None and any(
        e["kind"] == "timeout" and e["data"].get("turn_id") == turn_id
        for e in records.read_events(job_dir)
    )


def finalize(
    job_dir: Path, outcome: TurnOutcome, *, recovered_via: str | None, crash_at: str | None = None
) -> dict[str, Any]:
    """Write the job's result, deliver it to `output_dir`, then move the job to its terminal state.

    The terminal state comes last so that a crash anywhere before it leaves the job non-terminal
    and recoverable: reconcile runs this again and rewrites the same files. The result's `ran` is
    true once a turn id is recorded, null for `outcome_unknown` after a `turn/start` whose
    acceptance was never recorded, and false otherwise. An interrupted turn that a runner had
    journaled a `timeout` for is timed out, whichever runner or recovery route reads its end.
    """
    root, job_id = job_dir.parent, job_dir.name
    job = records.load_job(root, job_id)
    job["effort_used"] = applied_effort(job_dir, job["thread_id"]) or job["effort_used"]
    if outcome.status == "interrupted" and _timeout_journaled(job_dir, job["turn_id"]):
        outcome = replace(outcome, timed_out=True)
    state, error, answer = _terminal_for(job, outcome)
    writing = job["kind"] == "run"
    reviewing = job["kind"] == "review"
    commits: list[str] = []
    changed_files: list[str] = []
    if writing:
        try:
            commits = permissions.commits(job["worktree"], job["base_commit"])
            changed_files = permissions.changed_files(job["worktree"])
        except WorktreeError as exc:
            state, error, answer = records.FAILED, f"the worktree could not be read: {exc}", None
    review_approved: bool | None = None
    failure: str | None = None  # why a review did not complete; None when it did
    if reviewing:
        for name in review.DELIVERABLES:
            (job_dir / name).unlink(missing_ok=True)
    if state == records.SUCCEEDED and reviewing:
        verdict = (
            review.write_native_deliverable(job_dir, answer["review"])
            if job["review_target"] is not None
            else review.write_deliverables(job_dir, answer)
        )
        task_succeeded: bool | None = verdict.task_succeeded
        review_approved = verdict.review_approved
        failure = "; ".join(verdict.problems) or None
    elif state == records.SUCCEEDED:
        task_succeeded = answer["status"] == "completed"
    else:
        task_succeeded = None if state == records.OUTCOME_UNKNOWN else False
        failure = error or state
    if job["turn_id"] is not None:
        ran: bool | None = True
    elif state == records.OUTCOME_UNKNOWN and unconfirmed_dispatch(job_dir) is not None:
        ran = None
    else:
        ran = False
    result = records.build_result(
        {**job, "state": state},
        ran=ran,
        task_succeeded=task_succeeded,
        review_approved=review_approved,
        worktree=job["worktree"],
        branch=job["branch"],
        commits=commits,
        changed_files=changed_files,
        tests_run=answer["tests_run"] if writing and answer is not None else [],
        usage=_usage_block(job, outcome),
        recovery={"recovered_via": recovered_via} if recovered_via else None,
        catalog_fetched_at=job["catalog_fetched_at"],
    )
    _write_atomic(job_dir / "result.json", json.dumps(result, indent=2) + "\n")
    if answer is not None and not reviewing:
        _write_atomic(job_dir / "answer.md", _answer_markdown(job, answer))
    else:
        (job_dir / "answer.md").unlink(missing_ok=True)
    if reviewing:
        attestation.write(job_dir, job, failure)
    _crash_point(crash_at, "before-deliver")
    if job["output_dir"]:
        _deliver(job_dir, Path(job["output_dir"]))
    return records.transition(
        root,
        job_id,
        state,
        error=error,
        codex_error_info=outcome.error_info,
        effort_used=job["effort_used"],
        recovered_via=recovered_via,
        result_path=str(job_dir / "result.json"),
    )


def applied_effort(job_dir: Path, thread_id: str | None) -> str | None:
    """The effort the server last reported for the thread's turn (`thread/settings/updated`), from
    the journal; None if it never did.

    The server sends it after `turn/start`, not after `review/start`: a native review's effort is
    the one
    its `thread/start` reply reported.
    """
    effort = None
    for event in records.read_events(job_dir):
        data = event.get("data") or {}
        params = data.get("params") or {}
        if (
            event["kind"] == "notification"
            and data.get("method") == "thread/settings/updated"
            and params.get("threadId") == thread_id
        ):
            effort = params["threadSettings"].get("effort")
    return effort


def _usage_block(job: dict[str, Any], outcome: TurnOutcome) -> dict[str, Any]:
    """The result's `usage`: token counts, the limits read before and after the turn, and the limit
    a failure hit.

    `rate_limit_reached_type` is set only when the job failed on a usage limit (refused at
    admission, or the turn ended `usageLimitExceeded`), from the latest snapshot read.
    """
    before, after = job["admission"], outcome.rate_limits_after
    on_limit = (
        isinstance(before, dict) and admission.refused(before)
    ) or outcome.error_info == USAGE_LIMIT_ERROR
    latest = after if after is not None else before
    return {
        "tokens": outcome.usage,
        "rate_limits_before": before,
        "rate_limits_after": after,
        "rate_limit_reached_type": latest["rateLimitReachedType"]
        if on_limit and isinstance(latest, dict)
        else None,
    }


def _rate_limits_after(job_dir: Path, client: AppServerClient) -> dict[str, Any] | None:
    """The account's limits once the turn ended; a failed read is journaled and left out, since the
    turn's result does not depend on it."""
    config = admission.load_config(admission.DEFAULT_CONFIG_PATH)
    try:
        return admission.check_usage(client.call(admission.RATE_LIMITS_METHOD), config).record
    except (
        TimeoutError,
        appserver.ConnectionClosed,
        RpcError,
        appserver.ProtocolError,
        admission.MalformedRateLimits,
    ) as exc:
        records.append_event(job_dir, "rate_limits_after_unreadable", {"error": repr(exc)})
        return None


def _crash_point(crash_at: str | None, name: str) -> None:
    """The one test seam: end the process without cleanup when `--crash-at` names this point."""
    if crash_at == name:
        os._exit(1)


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _leader_status(pgid: int) -> tuple[str, str] | None:
    """`(ps state, command line)` of process `pgid`, or None when there is no such process."""
    listing = subprocess.run(
        ["ps", "-ww", "-o", "stat=,command=", "-p", str(pgid)],
        capture_output=True,
        text=True,
        check=False,
    )
    line = listing.stdout.strip()
    if listing.returncode != 0 or not line:
        return None
    stat, _, command = line.partition(" ")
    return stat, command.strip()


def _group_reused(job_dir: Path, pgid: int) -> str | None:
    """The leader's command line when process group `pgid` now belongs to another program, else
    None.

    The group is the job's while its leader runs with the launch argument
    `unix://<job_dir>/app.sock`,
    or while the leader is gone or a zombie (a pid is not reused while its group still has members).
    """
    leader = _leader_status(pgid)
    if leader is None or leader[0].startswith("Z"):
        return None
    command = leader[1]
    return None if f"unix://{Path(os.path.abspath(job_dir)) / 'app.sock'}" in command else command


def stop_server(job_dir: Path, *, grace: float = appserver.DEFAULT_STOP_GRACE) -> bool:
    """Stop the job's app-server process group and stamp `appserver_stopped_at` once the group is
    gone.

    The stamp rests on `killpg(pgid, 0)` raising, not on what the stop call reports. A job with no
    recorded group has nothing to stop and is not stamped. A group whose leader is now another
    program (the pgid was reused after the app-server ended) is not signalled: it is stamped and
    journaled as `appserver_pgid_reused`.
    """
    root, job_id = job_dir.parent, job_dir.name
    pgid = records.load_job(root, job_id)["pgid"]
    if pgid is None:
        return False
    if not _group_gone(pgid):
        command = _group_reused(job_dir, pgid)
        if command is not None:
            records.update_job(root, job_id, appserver_stopped_at=records.now_stamp())
            records.append_event(
                job_dir, "appserver_pgid_reused", {"pgid": pgid, "command": command}
            )
            return True
    appserver.stop_app_server(pgid, grace=grace)
    if not _group_gone(pgid):
        records.append_event(job_dir, "appserver_still_running", {"pgid": pgid})
        return False
    records.update_job(root, job_id, appserver_stopped_at=records.now_stamp())
    records.append_event(job_dir, "appserver_stopped", {"pgid": pgid})
    return True


class _Run:
    def __init__(self, job_dir: Path, crash_at: str | None, recheck_seconds: float):
        self.job_dir = job_dir
        self.root = job_dir.parent
        self.job_id = job_dir.name
        self.crash_at = crash_at
        self.recheck_seconds = recheck_seconds

    def job(self) -> dict[str, Any]:
        return records.load_job(self.root, self.job_id)

    def crash_point(self, name: str) -> None:
        _crash_point(self.crash_at, name)

    def cancel_requested(self) -> bool:
        return bool(self.job()["cancel_requested_at"])

    def check_not_cancelled(self) -> None:
        """Stop before the turn is dispatched when a cancel was recorded; the handler settles the
        job as cancelled."""
        if self.cancel_requested():
            raise RunnerError("cancel requested before the turn was dispatched")

    def nap(self, seconds: float) -> None:
        """Sleep `seconds` between slot checks, waking early once a cancel is recorded."""
        end = time.monotonic() + seconds
        while not self.cancel_requested() and (left := end - time.monotonic()) > 0:
            time.sleep(min(POLL_SECONDS, left))

    def refuse_server_request(self, method: str, params: dict[str, Any]) -> Any:
        if method in _REFUSALS:
            records.append_event(
                self.job_dir, "approval_declined", {"method": method, "params": params}
            )
            return _REFUSALS[method]
        records.append_event(
            self.job_dir, "server_request_refused", {"method": method, "params": params}
        )
        raise RpcError(-32601, f"codex-jobs cannot answer {method}")

    def deadline(self, job: dict[str, Any]) -> float:
        """The monotonic time the job times out: `timeout_seconds` after its `starting` transition.

        It is checked just before `turn/start` (an expired job times out without a turn) and then
        while the turn runs.
        """
        starts = [
            e
            for e in records.read_events(self.job_dir)
            if e["kind"] == "transition" and e["data"].get("to") == records.STARTING
        ]
        if not starts:
            raise RunnerError("job has no starting transition")
        elapsed = (datetime.now(UTC) - records.parse_stamp(starts[0]["ts"])).total_seconds()
        return time.monotonic() + job["timeout_seconds"] - elapsed

    def connect(self, socket_path: Path) -> AppServerClient:
        return AppServerClient(socket_path, request_handler=self.refuse_server_request)

    def interrupt(self, client: AppServerClient, thread_id: str, turn_id: str) -> None:
        records.append_event(self.job_dir, "timeout", {"turn_id": turn_id})
        try:
            client.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=15)
        except RpcError as exc:  # the turn may have ended in the meantime
            records.append_event(self.job_dir, "interrupt_refused", {"error": str(exc)})

    def journal(self, note: dict[str, Any]) -> None:
        records.append_event(self.job_dir, "notification", note)


def open_thread(client: AppServerClient, job: dict[str, Any]) -> tuple[str, str | None]:
    """`(thread id, model the server reports)` from `thread/start`, or `thread/resume` /
    `thread/fork` of the source thread."""
    sandbox, _ = permissions.sandbox_for(job)
    params = {
        "cwd": job["cwd"],
        "model": job["model_slug"],
        "sandbox": sandbox,
        "approvalPolicy": APPROVAL_POLICY,
        "approvalsReviewer": APPROVALS_REVIEWER,
    }
    if job["review_target"] is not None:
        params["config"] = {"model_reasoning_effort": job["effort_requested"]}
    if job["source_mode"] is None:
        reply = client.call("thread/start", params)
    else:
        reply = client.call(
            f"thread/{job['source_mode']}",
            {"threadId": job["source_thread_id"], "excludeTurns": True, **params},
            timeout=120,
        )
    if job["review_target"] is not None and reply.get("reasoningEffort") != job["effort_requested"]:
        raise RunnerError(
            f"the server runs the review at effort {reply.get('reasoningEffort')!r}, "
            f"not the requested {job['effort_requested']!r}"
        )
    return reply["thread"]["id"], reply.get("model")


def start_native_review(client: AppServerClient, job: dict[str, Any], thread_id: str) -> str:
    """Start Codex's built-in reviewer on the job's thread with `review/start`; the id of the review
    turn.

    The method takes no prompt, effort, sandbox or output schema: effort is set on the thread,
    and the reviewer answers in prose.
    """
    reply = client.call("review/start", {"threadId": thread_id, "target": job["review_target"]})
    return reply["turn"]["id"]


def start_turn(client: AppServerClient, job: dict[str, Any], thread_id: str, prompt: str) -> str:
    schema, contract = _kind_spec(job)
    _, policy = permissions.sandbox_for(job)
    envelope = (
        [permissions.commit_line(job["allow_commit"]), contract]
        if job["kind"] == "run"
        else [contract]
    )
    items = [{"type": "text", "text": "\n\n".join([prompt, *envelope])}]
    items += [{"type": "localImage", "path": path} for path in job["images"]]
    reply = client.call(
        "turn/start",
        {
            "threadId": thread_id,
            "input": items,
            "effort": job["effort_requested"],
            "sandboxPolicy": policy,
            "outputSchema": schema,
            "approvalPolicy": APPROVAL_POLICY,
            "approvalsReviewer": APPROVALS_REVIEWER,
        },
    )
    return reply["turn"]["id"]


def _follow_turn(
    run: _Run, client: AppServerClient, thread_id: str, turn_id: str, deadline: float
) -> TurnOutcome:
    """Read the turn's notifications until `turn/completed`; interrupt it when the deadline
    passes."""
    native = run.job()["review_target"] is not None
    final_text: str | None = None
    usage: dict[str, Any] | None = None
    running = False
    timed_out = False
    limit = deadline
    while True:
        now = time.monotonic()
        if now >= limit:
            if timed_out:
                return TurnOutcome("interrupted", usage=usage, timed_out=True)
            run.interrupt(client, thread_id, turn_id)
            timed_out = True
            limit = now + INTERRUPT_GRACE_SECONDS
            continue
        note = client.next_notification(timeout=min(POLL_SECONDS, limit - now))
        if note is None:
            continue
        run.journal(note)
        method, params = note["method"], note.get("params") or {}
        if not running and (method == "turn/started" or method.startswith("item/")):
            records.transition(run.root, run.job_id, records.RUNNING)
            running = True
        item = params.get("item") or {}
        if method == "item/started" and item.get("type") == "commandExecution":
            run.crash_point("mid-command")
        elif method == "item/completed" and is_final_message(item, native=native):
            final_text = item["text"]
        elif method == "thread/tokenUsage/updated" and params.get("turnId") == turn_id:
            usage = params["tokenUsage"]
        elif method == "turn/completed" and params["turn"]["id"] == turn_id:
            run.crash_point("after-complete")
            outcome = outcome_of_turn(params["turn"], native=native)
            outcome.final_text = outcome.final_text or final_text
            if outcome.status == "completed" and outcome.final_text is None:
                outcome.final_text = read_final_text(client, thread_id, turn_id, native=native)
            outcome.usage = usage
            outcome.timed_out = timed_out and outcome.status == "interrupted"
            return outcome


def _run_new(run: _Run) -> None:
    job = run.job()
    native = job["review_target"] is not None
    prompt_path = run.job_dir / "prompt.txt"
    config = admission.load_config(admission.DEFAULT_CONFIG_PATH)
    # The slot is taken while the job is still `queued`, so a waiting job costs no app-server
    # process.
    if not admission.admit(
        run.root,
        run.job_id,
        config,
        is_cancelled=run.cancel_requested,
        recheck_seconds=run.recheck_seconds,
        sleep=run.nap,
    ):
        raise RunnerError("cancel requested while queued")
    records.transition(run.root, run.job_id, records.STARTING)
    run.check_not_cancelled()
    deadline = run.deadline(job)
    preflight_code_mode_host(job["codex_bin"], run.job_dir / "startup.log")
    server = appserver.launch_app_server(
        run.job_dir,
        job["codex_bin"],
        Path(job["cwd"]),
        on_spawn=lambda pid, pgid: records.update_job(
            run.root, run.job_id, appserver_pid=pid, pgid=pgid
        ),
    )
    with run.connect(server.socket_path) as client:
        client.initialize()
        run.check_not_cancelled()
        # Read at the turn's own start, after any wait for a slot, so the number is not stale.
        decision = admission.read_usage(client, config)
        records.update_job(run.root, run.job_id, admission=decision.record)
        if not decision.allowed:
            finalize(run.job_dir, TurnOutcome("failed", error=decision.reason), recovered_via=None)
            return
        thread_id, model_used = open_thread(client, job)
        records.update_job(
            run.root,
            run.job_id,
            thread_id=thread_id,
            **({"model_used": model_used} if model_used else {}),
        )
        run.check_not_cancelled()
        if job["kind"] == "review":
            attestation.save(
                run.job_dir,
                attestation.snapshot(
                    Path(job["cwd"]),
                    None if native else prompt_path,
                    review_target=job["review_target"],
                ),
            )
        run.crash_point("before-dispatch")
        prior_turn_ids = [] if job["source_mode"] is None else list_turn_ids(client, thread_id)
        if time.monotonic() >= deadline:  # startup used the whole timeout: no turn is started
            finalize(
                run.job_dir,
                TurnOutcome("interrupted", timed_out=True),
                recovered_via=None,
                crash_at=run.crash_at,
            )
            return
        records.append_event(
            run.job_dir, "dispatching", {"thread_id": thread_id, "prior_turn_ids": prior_turn_ids}
        )
        try:
            turn_id = (
                start_native_review(client, job, thread_id)
                if native
                else start_turn(client, job, thread_id, prompt_path.read_text())
            )
        except RpcError as exc:  # an error reply: the server did not start the turn
            records.append_event(run.job_dir, "dispatch_refused", {"error": str(exc)})
            raise
        run.crash_point("after-dispatch-reply")
        records.transition(
            run.root,
            run.job_id,
            records.DISPATCHED,
            thread_id=thread_id,
            turn_id=turn_id,
            # a native review runs at its thread's effort, which open_thread checked against the
            # request
            effort_used=job["effort_requested"] if native else None,
        )
        run.crash_point("after-accept")
        outcome = _follow_turn(run, client, thread_id, turn_id, deadline)
        outcome.rate_limits_after = _rate_limits_after(run.job_dir, client)
    finalize(run.job_dir, outcome, recovered_via=None, crash_at=run.crash_at)


def _adopt(run: _Run) -> None:
    """Wait for the dispatched turn of a job whose runner died, by listing its turns (a second
    client gets no turn stream).

    The turn is read once per `POLL_SECONDS`. Notifications that arrive in between are journaled
    but do not bring the next read forward: the server answers some reads with a notification of
    its own, and waking on that would make the loop read as fast as the server answers.
    """
    job = run.job()
    if job["turn_id"] is None:
        raise RunnerError("nothing to adopt: the job never dispatched a turn")
    thread_id, turn_id = job["thread_id"], job["turn_id"]
    deadline = run.deadline(job)
    interrupted = False
    limit = deadline
    with run.connect(run.job_dir / "app.sock") as client:
        client.initialize()
        while True:
            turn = read_turn(client, thread_id, turn_id)
            if turn is None and not thread_is_active(client, thread_id):
                raise RunnerError(f"turn {turn_id} is not on the server and its thread is idle")
            if turn is not None and turn["status"] != "inProgress":
                outcome = settled_outcome(
                    client, thread_id, turn, native=job["review_target"] is not None
                )
                outcome.timed_out = interrupted and outcome.status == "interrupted"
                break
            now = time.monotonic()
            if now >= limit:
                if interrupted:
                    outcome = TurnOutcome("interrupted", timed_out=True)
                    break
                run.interrupt(client, thread_id, turn_id)
                interrupted = True
                limit = now + INTERRUPT_GRACE_SECONDS
            next_read = time.monotonic() + POLL_SECONDS
            while (wait := next_read - time.monotonic()) > 0:
                note = client.next_notification(timeout=wait)
                if note is not None:
                    run.journal(note)
        outcome.rate_limits_after = _rate_limits_after(run.job_dir, client)
    run.crash_point("after-complete")
    finalize(run.job_dir, outcome, recovered_via="thread/read", crash_at=run.crash_at)


def _log(job_id: str, message: str) -> None:
    print(f"runner {job_id}: {message}", file=sys.stderr, flush=True)


def run_job(
    job_dir: Path,
    *,
    adopt: bool = False,
    crash_at: str | None = None,
    recheck_seconds: float = admission.RECHECK_SECONDS,
) -> int:
    """Run (or adopt) the job in `job_dir` to a terminal state; the exit status is 0 only for
    `succeeded`.

    A failure before `turn/start` was sent, or one the server answered with an error, fails the job,
    or cancels it when a cancel was recorded (the cancel is what interrupted the runner's wait), and
    the settled job has its app-server stopped. A failure once `turn/start` was sent and not
    refused,
    whether or not its reply arrived, is journaled as `runner_error` and leaves the job and its
    app-server as they are: reconcile finds the turn on the server (or learns that none started)
    and settles the job from the server or the rollout once this runner has exited.
    """
    run = _Run(Path(job_dir), crash_at, recheck_seconds)
    records.update_job(run.root, run.job_id, runner_pid=os.getpid())
    try:
        (_adopt if adopt else _run_new)(run)
    except Exception as exc:
        job = run.job()
        message = str(exc) or type(exc).__name__
        if job["turn_id"] is None and unconfirmed_dispatch(run.job_dir) is None:
            cancelled = job["cancel_requested_at"] is not None
            if not cancelled:
                traceback.print_exc()
            outcome = (
                TurnOutcome("interrupted") if cancelled else TurnOutcome("failed", error=message)
            )
            finalize(run.job_dir, outcome, recovered_via=None)
            stop_server(run.job_dir)
        else:
            traceback.print_exc()
            records.append_event(run.job_dir, "runner_error", {"error": message})
        return 1
    run.crash_point("before-stop")
    stop_server(run.job_dir)
    state = run.job()["state"]
    _log(run.job_id, state)
    return 0 if state == records.SUCCEEDED else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m codex_jobs.runner")
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--crash-at", choices=CRASH_POINTS)
    parser.add_argument("--recheck-seconds", type=float, default=admission.RECHECK_SECONDS)
    args = parser.parse_args(argv)
    return run_job(
        Path(os.path.abspath(args.job_dir)),
        adopt=args.adopt,
        crash_at=args.crash_at,
        recheck_seconds=args.recheck_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
