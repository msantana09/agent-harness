"""Settle jobs whose runner died, from the job's app-server or Codex's rollout file.

A job is acted on only when its runner process is gone and it is not terminal or still has an
app-server to stop. Reconcile never sends `turn/start`: a turn that did not start is failed, never
retried. Routes, in order:

1. no `turn_id` recorded and no `dispatching` event: the runner stopped before sending
   `turn/start`, and the job failed.
   No `turn_id` but a `dispatching` event (`turn/start` was sent, its reply not recorded): the
   app-server is asked for the thread's turns. Exactly one turn not among the thread's earlier
   turns is the job's: its id is recorded with the `dispatched` transition and the job goes on
   through the routes below. No new turn on an idle thread: the job failed, never dispatched.
   Anything else (the server gone, several new turns, an active thread listing none) is
   `outcome_unknown`, with `ran` null in the result.
2. the app-server answers on `<job_dir>/app.sock`: `thread/turns/list` shows the turn. Still
   running: start an adopt runner. Finished: finalize from the thread
   (`recovered_via = "thread/read"`).
3. the app-server is gone: Codex's rollout file for the thread has the turn's end
   (`recovered_via = "rollout"`), else the job is `outcome_unknown`. A native review's turn ends in
   that file with no message; its text is the last message of the review sub-thread's own rollout.

Every route that ends the job also stops the app-server process group. A terminal job whose
runner is gone with its app-server group recorded but not stamped stopped (the runner died
between settling the job and stopping the server) only has that group stopped; its result stays
as it is.
"""

import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import records, review, rollout, runner
from .appserver import AppServerClient, ConnectionClosed, ProtocolError, RpcError
from .runner import TurnOutcome

PROBE_TIMEOUT = 10.0
# OSError covers refused, missing and timed-out sockets
_SERVER_DOWN = (OSError, ConnectionClosed, ProtocolError, RpcError)


@contextmanager
def _exclusive(job_dir: Path) -> Generator[None]:
    """Serialise reconcilers of one job; a second caller waits and then finds the job settled."""
    fd = os.open(job_dir / "reconcile.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _probe_server(job_dir: Path, job: dict[str, Any]) -> TurnOutcome | str | None:
    """Ask the job's app-server about its turn: `"running"`, a finished turn's outcome, or None
    (server gone or turn unknown)."""
    thread_id = job["thread_id"]
    try:
        with AppServerClient(job_dir / "app.sock", connect_timeout=PROBE_TIMEOUT) as client:
            client.initialize(timeout=PROBE_TIMEOUT)
            turn = runner.read_turn(client, thread_id, job["turn_id"])
            if turn is None:
                return "running" if runner.thread_is_active(client, thread_id) else None
            if turn["status"] == "inProgress":
                return "running"
            return runner.settled_outcome(
                client, thread_id, turn, native=job["review_target"] is not None
            )
    except _SERVER_DOWN:
        return None


def _native_review_outcome(job: dict[str, Any], sessions_dir: Path, parent: Path) -> TurnOutcome:
    """A native review's text from the rollout of its review sub-agent thread, which started after
    `parent` did.

    The parent's turn ends with no message; the sub-thread's last message is the structured review,
    rendered to prose. Anything but exactly one such sub-thread is `unknown`.
    """
    thread_id = job["thread_id"]
    found = rollout.review_subthreads(sessions_dir, thread_id, parent)
    if len(found) != 1:
        count = (
            "no review sub-thread rollout"
            if not found
            else f"{len(found)} review sub-thread rollouts"
        )
        return TurnOutcome(
            "unknown",
            error=f"{count} found for thread {thread_id}; cannot tell which one is the review",
        )
    ends = [
        row["payload"]
        for row in rollout.rows(found[0])
        if row.get("type") == "event_msg"
        and (row.get("payload") or {}).get("type") == "task_complete"
    ]
    try:
        return TurnOutcome(
            "completed",
            final_text=review.render_review_output(json.loads(ends[-1]["last_agent_message"])),
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return TurnOutcome(
            "unknown",
            error=f"the review sub-thread rollout {found[0].name} does not end in a review output",
        )


def _outcome_from_rollout(job: dict[str, Any], sessions_dir: Path) -> TurnOutcome | None:
    """The end of the job's turn as Codex wrote it to `rollout-*-<thread id>.jsonl` (or its
    `.jsonl.zst`), or None if it never ended there."""
    for path in rollout.thread_rollouts(sessions_dir, job["thread_id"]):
        for row in rollout.rows(path):
            payload = row.get("payload") or {}
            if row.get("type") != "event_msg" or payload.get("turn_id") != job["turn_id"]:
                continue
            if payload.get("type") == "task_complete":
                if job["review_target"] is not None and payload.get("last_agent_message") is None:
                    return _native_review_outcome(job, sessions_dir, path)
                return TurnOutcome("completed", final_text=payload.get("last_agent_message"))
            if payload.get("type") == "turn_aborted":
                return TurnOutcome("interrupted")
    return None


def _settle(
    job_dir: Path, outcome: TurnOutcome, recovered_via: str | None, sessions_dir: Path
) -> dict[str, Any]:
    runner.finalize(job_dir, outcome, recovered_via=recovered_via, sessions_dir=sessions_dir)
    runner.stop_server(job_dir)
    return records.load_job(job_dir.parent, job_dir.name)


def _probe_dispatch(job_dir: Path, sent: dict[str, Any]) -> tuple[str, str | None]:
    """What the app-server shows after an unrecorded `turn/start`: `("started", turn id)`,
    `("none", None)` or `("unknown", None)`.

    `started` is exactly one turn not among the thread's earlier turns; `none` is no such turn on
    an idle thread; anything else, the server being gone included, is `unknown`.
    """
    thread_id = sent["thread_id"]
    try:
        with AppServerClient(job_dir / "app.sock", connect_timeout=PROBE_TIMEOUT) as client:
            client.initialize(timeout=PROBE_TIMEOUT)
            new = [
                turn_id
                for turn_id in runner.list_turn_ids(client, thread_id)
                if turn_id not in sent["prior_turn_ids"]
            ]
            if len(new) == 1:
                return "started", new[0]
            if not new and not runner.thread_is_active(client, thread_id):
                return "none", None
            return "unknown", None
    except _SERVER_DOWN:
        return "unknown", None


def _reconcile_undispatched(
    job_dir: Path, job: dict[str, Any], sessions_dir: Path
) -> dict[str, Any]:
    """Settle a job with no recorded turn id, finding its turn on the server when `turn/start` was
    sent."""
    sent = runner.unconfirmed_dispatch(job_dir)
    if sent is None:
        records.append_event(job_dir, "reconciled", {"route": "never-dispatched"})
        return _settle(
            job_dir,
            TurnOutcome(
                "failed", error="never dispatched: the runner stopped before its turn was accepted"
            ),
            None,
            sessions_dir,
        )
    verdict, turn_id = _probe_dispatch(job_dir, sent)
    if verdict == "started":
        records.append_event(job_dir, "reconciled", {"route": "dispatch-found", "turn_id": turn_id})
        records.transition(
            job_dir.parent,
            job_dir.name,
            records.DISPATCHED,
            thread_id=sent["thread_id"],
            turn_id=turn_id,
            # as the runner records it: a native review runs at the effort open_thread checked
            effort_used=job["effort_requested"] if job["review_target"] is not None else None,
        )
        return _reconcile(job_dir, records.load_job(job_dir.parent, job_dir.name), sessions_dir)
    if verdict == "none":
        records.append_event(
            job_dir, "reconciled", {"route": "never-dispatched", "confirmed_by": "app-server"}
        )
        return _settle(
            job_dir,
            TurnOutcome(
                "failed",
                error="never dispatched: turn/start was sent but the app-server shows no new turn",
            ),
            None,
            sessions_dir,
        )
    records.append_event(job_dir, "reconciled", {"route": "dispatch-unknown"})
    return _settle(
        job_dir,
        TurnOutcome(
            "unknown",
            error=(
                "turn/start was sent but its acceptance was not recorded, and the app-server "
                "cannot show whether a turn started"
            ),
        ),
        None,
        sessions_dir,
    )


def _reconcile(job_dir: Path, job: dict[str, Any], sessions_dir: Path) -> dict[str, Any]:
    if job["turn_id"] is None:
        return _reconcile_undispatched(job_dir, job, sessions_dir)
    probe = _probe_server(job_dir, job)
    if probe == "running":
        records.append_event(job_dir, "reconciled", {"route": "adopt"})
        runner.spawn_runner(job_dir, adopt=True)
        return records.load_job(job_dir.parent, job_dir.name)
    if isinstance(probe, TurnOutcome):
        records.append_event(job_dir, "reconciled", {"route": "thread/read"})
        return _settle(job_dir, probe, "thread/read", sessions_dir)
    outcome = _outcome_from_rollout(job, sessions_dir)
    if outcome is not None:
        records.append_event(job_dir, "reconciled", {"route": "rollout"})
        return _settle(job_dir, outcome, "rollout", sessions_dir)
    records.append_event(job_dir, "reconciled", {"route": "outcome-unknown"})
    return _settle(
        job_dir,
        TurnOutcome(
            "unknown",
            error="the app-server is gone and Codex's rollout does not show the turn's end",
        ),
        None,
        sessions_dir,
    )


def _server_left_running(job: dict[str, Any]) -> bool:
    """A terminal job whose app-server group was recorded but never stamped as stopped."""
    return (
        job["state"] in records.TERMINAL_STATES
        and job["pgid"] is not None
        and job["appserver_stopped_at"] is None
    )


def _needs_reconcile(job: dict[str, Any]) -> bool:
    return job["state"] not in records.TERMINAL_STATES or _server_left_running(job)


def reconcile_job(root: Path, job_id: str, *, sessions_dir: Path) -> dict[str, Any]:
    """Settle `job_id` if its runner is dead, or stop the app-server a dead runner left behind;
    return the record."""
    root = Path(root)
    job = records.load_job(root, job_id)
    if not _needs_reconcile(job) or runner.runner_alive(job):
        return job
    job_dir = root / job_id
    with _exclusive(job_dir):
        job = records.load_job(root, job_id)
        if not _needs_reconcile(job) or runner.runner_alive(job):
            return job
        if _server_left_running(job):
            records.append_event(job_dir, "reconciled", {"route": "stop-server"})
            runner.stop_server(job_dir)
            return records.load_job(root, job_id)
        return _reconcile(job_dir, job, Path(sessions_dir))


def reconcile_all(root: Path, *, sessions_dir: Path) -> list[dict[str, Any]]:
    """Reconcile every job that was not terminal or still has an app-server to stop; return their
    records afterwards."""
    return [
        reconcile_job(root, job["job_id"], sessions_dir=sessions_dir)
        for job in records.list_jobs(root)
        if _needs_reconcile(job)
    ]
