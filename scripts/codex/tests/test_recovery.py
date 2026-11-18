"""Tests for codex_jobs.runner and codex_jobs.reconcile: a consult job end to end and its crash
recovery.

Runners are real detached subprocesses (`spawn_runner`) against tests/fake_codex.py, so a
`--crash-at` point really ends the process and the fake app-server really outlives it.
"""

import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import admission, permissions, reconcile, records, review, runner
from codex_jobs.appserver import AppServerClient

ANSWER = {"status": "completed", "answer": "The answer.", "open_questions": []}
TURN = {"final_answer": json.dumps(ANSWER)}
COMMAND_ITEM = {
    "method": "item/started",
    "params": {"item": {"type": "commandExecution", "id": "cmd-1", "command": "ls"}},
}
TOKEN_USAGE = {
    "last": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
    "total": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7},
    "modelContextWindow": 1000,
}
COMMAND_APPROVAL = {
    "method": "item/commandExecution/requestApproval",
    "params": {"command": "touch x"},
}
PERMISSIONS_APPROVAL = {
    "method": "item/permissions/requestApproval",
    "params": {"reason": "network"},
}
ELICITATION = {
    "method": "mcpServer/elicitation/request",
    "params": {"serverName": "docs", "message": "Sign in?"},
}
TASK = {
    "status": "completed",
    "summary": "Edited the file.",
    "changed_files": ["edit.txt"],
    "tests_run": ["pytest -q"],
    "blockers": ["no network"],
}
REVIEW_FINDING = {
    "severity": "high",
    "title": "Loader skips tenant check",
    "body": "The loader reads rows without the tenant filter.",
    "recommendation": "Filter by tenant before reading.",
    "file": "change.txt",
    "line_start": 1,
    "line_end": 1,
    "confidence": 0.8,
    "category": "security",
}
REVIEW = {
    "status": "completed",
    "review": "## Summary\nOne problem.\n\n## Review Findings\n- tenant check\n",
    "verdict": "needs-attention",
    "summary": "One high finding.",
    "findings": [REVIEW_FINDING],
}
REVIEW_TURN = {"final_answer": json.dumps(REVIEW)}
SCHEMAS = Path(runner.SCHEMAS_DIR)
GIT_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def wait_until(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} not reached within {timeout}s")
        time.sleep(0.02)


def group_gone(pgid):
    """Whether no process is left in the group; a killed group of unreaped zombies answers EPERM."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class Lab:
    """A job root, a project, an output dir and a sessions dir under tmp_path, driving the fake
    codex."""

    def __init__(self, tmp_path, fake):
        self.fake = fake
        self.root = tmp_path / "jobs"
        self.sessions = tmp_path / "sessions"
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.output = tmp_path / "out"

    def job(self, **overrides):
        args = dict(
            kind="consult",
            cwd=str(self.project),
            project_dir=str(self.project),
            model_requested="luna",
            model_slug="gpt-6-luna",
            effort_requested="low",
            sandbox="read-only",
            timeout_seconds=60,
            output_dir=str(self.output),
            codex_bin=str(self.fake.bin),
        )
        job, _ = records.create_job(self.root, **{**args, **overrides})
        job_dir = self.root / job["job_id"]
        (job_dir / "prompt.txt").write_text("Question for Codex")
        return job_dir

    def load(self, job_dir):
        return records.load_job(self.root, job_dir.name)

    def events(self, job_dir, kind):
        return [e["data"] for e in records.read_events(job_dir) if e["kind"] == kind]

    def run(self, job_dir, **kwargs):
        """Run the runner to its end; returns its exit status."""
        return runner.spawn_runner(job_dir, **kwargs).wait(timeout=60)

    def crash(self, job_dir, point):
        assert self.run(job_dir, crash_at=point) == 1
        return self.load(job_dir)

    def reconcile(self, job_dir):
        return reconcile.reconcile_job(self.root, job_dir.name, sessions_dir=self.sessions)

    def settled(self, job_dir, timeout=30.0):
        return wait_until(
            lambda: (job := self.load(job_dir))["state"] in records.TERMINAL_STATES and job,
            timeout,
            "a terminal state",
        )

    def requests(self, method):
        return [m for m in self.fake.received() if m.get("method") == method]

    def turn_starts(self):
        return len(self.requests("turn/start"))


@pytest.fixture
def lab(tmp_path, fake_codex):
    yield Lab(tmp_path, fake_codex)
    for process in list(runner._spawned.values()):
        if process.poll() is None:
            process.kill()
            process.wait()
    runner._spawned.clear()


@pytest.mark.parametrize(
    "point, script, state, files, starts",
    [
        ("before-dispatch", {}, "failed", ["result.json"], 0),
        ("after-dispatch-reply", {"turn": TURN}, "succeeded", ["answer.md", "result.json"], 1),
        ("after-accept", {"turn": TURN}, "succeeded", ["answer.md", "result.json"], 1),
        (
            "mid-command",
            {"turn": {"events": [COMMAND_ITEM], **TURN}},
            "succeeded",
            ["answer.md", "result.json"],
            1,
        ),
        ("after-complete", {"turn": TURN}, "succeeded", ["answer.md", "result.json"], 1),
        ("before-deliver", {"turn": TURN}, "succeeded", ["answer.md", "result.json"], 1),
    ],
)
def test_each_crash_point_settles_without_a_second_turn(lab, point, script, state, files, starts):
    lab.fake.set_script(script)
    job_dir = lab.job()
    crashed = lab.crash(job_dir, point)
    assert crashed["state"] not in records.TERMINAL_STATES
    lab.reconcile(job_dir)
    job = lab.settled(job_dir)
    assert job["state"] == state
    assert job["recovered_via"] == ("thread/read" if starts else None)
    assert lab.turn_starts() == starts
    assert sorted(os.listdir(lab.output)) == files
    assert len(lab.events(job_dir, "delivered")) == 1
    assert json.loads((lab.output / "result.json").read_text())["state"] == state
    assert job["appserver_stopped_at"] is not None
    assert job["error"] == (
        "never dispatched: the runner stopped before its turn was accepted" if not starts else None
    )


@pytest.mark.parametrize(
    "server, state, ran, error",
    [
        (
            "shows no new turn",
            "failed",
            False,
            "never dispatched: turn/start was sent but the app-server shows no new turn",
        ),
        (
            "is gone",
            "outcome_unknown",
            None,
            "turn/start was sent but its acceptance was not recorded",
        ),
    ],
)
def test_unrecorded_dispatch_is_settled_from_what_the_app_server_shows(
    lab, server, state, ran, error
):
    ghost_reply = {
        "responses": {"turn/start": {"turn": {"id": "turn-ghost", "status": "inProgress"}}}
    }
    lab.fake.set_script(ghost_reply if server == "shows no new turn" else {"turn": TURN})
    job_dir = lab.job()
    crashed = lab.crash(job_dir, "after-dispatch-reply")
    assert crashed["turn_id"] is None
    if server == "is gone":
        os.killpg(crashed["pgid"], signal.SIGKILL)
        wait_until(lambda: group_gone(crashed["pgid"]), 10, "the fake app-server to die")
    job = lab.reconcile(job_dir)
    assert (job["state"], job["turn_id"]) == (state, None)
    assert error in job["error"]
    assert json.loads((job_dir / "result.json").read_text())["ran"] is ran
    assert job["appserver_stopped_at"] is not None


def test_unrecorded_dispatch_on_a_resumed_thread_skips_its_earlier_turns(lab):
    lab.fake.set_script({"history": {"thr-src": ["turn-earlier"]}, "turn": TURN})
    job_dir = lab.job(source_mode="resume", source_thread_id="thr-src")
    lab.crash(job_dir, "after-dispatch-reply")
    lab.reconcile(job_dir)
    job = lab.settled(job_dir)
    # the runner died before journaling the server's applied effort, so none is recorded
    assert (job["state"], job["turn_id"], job["effort_used"]) == ("succeeded", "turn-fake-1", None)
    assert lab.events(job_dir, "dispatching") == [
        {"thread_id": "thr-src", "prior_turn_ids": ["turn-earlier"]}
    ]
    assert lab.turn_starts() == 1


@pytest.mark.parametrize(
    "server, state, ran",
    [("dies before replying", "outcome_unknown", None), ("refuses", "failed", False)],
)
def test_turn_start_without_a_reply_is_outcome_unknown_but_a_refusal_fails(lab, server, state, ran):
    if server == "refuses":
        lab.fake.set_script(
            {"errors": {"turn/start": {"code": -32600, "message": "model unavailable"}}}
        )
    else:
        lab.fake.set_script({"delays": {"turn/start": 2}, "turn": TURN})
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir)
    if server == "dies before replying":
        lab.fake.wait_for(lambda r: r["kind"] == "recv" and r["msg"].get("method") == "turn/start")
        os.killpg(lab.load(job_dir)["pgid"], signal.SIGKILL)
    assert process.wait(timeout=60) == 1
    job = lab.reconcile(job_dir)
    assert (job["state"], job["turn_id"]) == (state, None)
    assert json.loads((job_dir / "result.json").read_text())["ran"] is ran


def test_a_lost_turn_start_reply_leaves_the_turn_for_reconcile_to_find(lab):
    lab.fake.set_script({"drop_reply": ["turn/start"], "turn": TURN})
    job_dir = lab.job()
    assert lab.run(job_dir) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["turn_id"], job["appserver_stopped_at"]) == ("starting", None, None)
    assert not group_gone(job["pgid"])
    lab.reconcile(job_dir)
    job = lab.settled(job_dir)
    assert (job["state"], job["turn_id"], job["recovered_via"]) == (
        "succeeded",
        "turn-fake-1",
        "thread/read",
    )
    assert lab.events(job_dir, "reconciled")[0] == {
        "route": "dispatch-found",
        "turn_id": "turn-fake-1",
    }
    assert (lab.output / "answer.md").read_text() == "The answer."
    assert lab.turn_starts() == 1
    assert job["appserver_stopped_at"] is not None


def test_a_runner_killed_during_app_server_startup_leaves_no_server_behind(lab):
    lab.fake.set_script({"startup_delay": 30, "turn": TURN})
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir)
    started = lab.fake.wait_for(lambda r: r["kind"] == "start", timeout=10)
    wait_until(
        lambda: lab.load(job_dir)["pgid"] is not None, 5, "the app-server's group to be recorded"
    )
    process.kill()
    process.wait(timeout=10)
    job = lab.reconcile(job_dir)
    assert (job["state"], job["pgid"]) == ("failed", started["pgid"])
    assert job["appserver_stopped_at"] is not None
    assert group_gone(started["pgid"])


def test_a_runner_that_dies_after_its_timeout_interrupt_is_recovered_as_timed_out(lab):
    lab.fake.set_script({"turn": {"hang": True}})
    job_dir = lab.job(timeout_seconds=3)  # room for startup, which the timeout also counts
    lab.crash(job_dir, "after-complete")
    assert len(lab.events(job_dir, "timeout")) == 1
    job = lab.reconcile(job_dir)
    assert (job["state"], job["error"], job["recovered_via"]) == (
        "timed_out",
        "timed out after 3s",
        "thread/read",
    )
    assert json.loads((job_dir / "result.json").read_text())["task_succeeded"] is False


def test_reconcile_adopts_a_turn_that_is_still_running(lab):
    lab.fake.set_script({"turn": {"final_delay": 4, **TURN}})
    job_dir = lab.job()
    crashed = lab.crash(job_dir, "after-accept")
    adopting = lab.reconcile(job_dir)
    assert adopting["state"] not in records.TERMINAL_STATES
    assert lab.events(job_dir, "reconciled") == [{"route": "adopt"}]
    job = lab.settled(job_dir)
    assert (job["state"], job["recovered_via"]) == ("succeeded", "thread/read")
    assert job["runner_pid"] != crashed["runner_pid"]
    assert lab.turn_starts() == 1


def test_adopt_reads_on_a_fixed_cadence_when_every_read_makes_the_server_notify(lab):
    notice = {
        "method": "deprecationNotice",
        "params": {"summary": "hydration is deprecated", "details": None},
    }
    lab.fake.set_script(
        {
            "notify_on": {"thread/read": notice, "thread/turns/list": notice},
            "turn": {"final_delay": 3, **TURN},
        }
    )
    job_dir = lab.job()
    lab.crash(job_dir, "after-accept")
    started = time.monotonic()
    assert lab.run(job_dir, adopt=True) == 0
    elapsed = time.monotonic() - started
    reads = [
        m
        for m in lab.fake.received(client=2)
        if m.get("method") in ("thread/read", "thread/turns/list")
    ]
    assert 2 <= len(reads) <= math.ceil(elapsed / runner.POLL_SECONDS) + 2
    assert lab.load(job_dir)["state"] == "succeeded"
    assert any(n["method"] == "deprecationNotice" for n in lab.events(job_dir, "notification"))


def test_a_turn_and_its_final_answer_are_found_on_a_later_page(lab):
    lab.fake.set_script({"page_limit": 1, "turn": TURN})
    server = lab.fake.launch()
    with AppServerClient(server.socket_path) as client:
        client.initialize()
        thread_id = client.call("thread/start", {})["thread"]["id"]
        turn_ids = []
        for _ in range(2):
            turn_ids.append(
                client.call("turn/start", {"threadId": thread_id, "input": []})["turn"]["id"]
            )
            while (note := client.next_notification(timeout=5)) is None or note[
                "method"
            ] != "turn/completed":
                pass
        turn = runner.read_turn(client, thread_id, turn_ids[0])
        assert (turn["id"], turn["status"], turn["items"]) == (turn_ids[0], "completed", [])
        assert len(lab.requests("thread/turns/list")) == 2  # the older turn is on the second page
        assert runner.read_final_text(client, thread_id, turn_ids[0]) == TURN["final_answer"]
        assert runner.read_turn(client, thread_id, "turn-nobody") is None


@pytest.mark.parametrize("with_rollout", [True, False], ids=["rollout present", "both routes cut"])
def test_dead_socket_recovers_from_the_rollout_or_is_outcome_unknown(lab, with_rollout):
    lab.fake.set_script({"turn": {"hang": True}})
    job_dir = lab.job()
    crashed = lab.crash(job_dir, "after-accept")
    os.killpg(crashed["pgid"], signal.SIGKILL)
    wait_until(lambda: group_gone(crashed["pgid"]), 10, "the fake app-server to die")
    if with_rollout:
        day = lab.sessions / "2026" / "11" / "08"
        day.mkdir(parents=True)
        rows = [
            {"type": "session_meta", "payload": {"id": crashed["thread_id"]}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-earlier",
                    "last_agent_message": "{}",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": crashed["turn_id"]},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": crashed["turn_id"],
                    "last_agent_message": TURN["final_answer"],
                },
            },
        ]
        (day / f"rollout-2026-11-08T12-00-00-{crashed['thread_id']}.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + '\n{"type": "event_ms'
        )
    job = lab.reconcile(job_dir)
    result = json.loads((job_dir / "result.json").read_text())
    assert job["appserver_stopped_at"] is not None
    assert lab.turn_starts() == 1
    if with_rollout:
        assert (job["state"], job["recovered_via"]) == ("succeeded", "rollout")
        assert (job_dir / "answer.md").read_text() == "The answer."
        assert result["recovery"] == {"recovered_via": "rollout"}
    else:
        assert (job["state"], job["recovered_via"]) == ("outcome_unknown", None)
        assert (result["ran"], result["task_succeeded"]) == (True, None)


REFUSED = [{"type": "input_text", "text": "touch: x: Operation not permitted\n"}]
TOUCH = "await tools.exec_command({cmd: 'touch x'})"
STARTED = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-fake-1"}}
COMPLETE = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-fake-1"}}


def test_the_turns_code_mode_calls_are_journaled_once_from_the_rollout(
    lab, write_rollout, exec_call
):
    # the server sends no item for a command the sandbox refused inside a code-mode script; a
    # script still running when `exec` returns hands the rest of its output to a `wait` call
    write_rollout(
        "12-00-00",
        "thr-fake-1",
        [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-earlier"}},
            *exec_call("c0", "await tools.exec_command({cmd: 'ls'})", "earlier turn"),
            STARTED,
            *exec_call("c1", TOUCH, "Script running with cell ID 7"),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait",
                    "call_id": "w1",
                    "arguments": '{"cell_id":"7"}',
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "w1", "output": REFUSED},
            },
            *exec_call("c2", "*** Begin Patch", "applied", name="apply_patch"),
            COMPLETE,
        ],
    )
    lab.fake.set_script({"turn": TURN})
    job_dir = lab.job()
    lab.crash(job_dir, "before-deliver")
    assert lab.reconcile(job_dir)["state"] == "succeeded"  # settles the job a second time
    assert lab.events(job_dir, "code_mode_calls") == [
        {
            "turn_id": "turn-fake-1",
            "calls": [
                {
                    "thread_id": "thr-fake-1",
                    "call_id": "c1",
                    "tool": "exec",
                    "code": TOUCH,
                    "output": "Script running with cell ID 7",
                },
                {
                    "thread_id": "thr-fake-1",
                    "call_id": "w1",
                    "tool": "wait",
                    "code": '{"cell_id":"7"}',
                    "output": REFUSED,
                },
            ],
        }
    ]


def test_a_turn_without_a_rollout_journals_that_its_code_mode_calls_are_unknown(lab):
    lab.fake.set_script({"turn": TURN})
    job_dir = lab.job()
    assert lab.run(job_dir) == 0
    assert lab.events(job_dir, "code_mode_calls") == [
        {
            "turn_id": "turn-fake-1",
            "calls": [],
            "error": f"no rollout for thread thr-fake-1 in {lab.sessions}",
        }
    ]


def test_a_rollout_without_the_turns_end_journals_its_calls_as_possibly_incomplete(
    lab, write_rollout, exec_call
):
    write_rollout("12-00-00", "thr-fake-1", [STARTED, *exec_call("c1", TOUCH, REFUSED)])
    lab.fake.set_script({"turn": TURN})
    job_dir = lab.job()
    assert lab.run(job_dir) == 0
    (entry,) = lab.events(job_dir, "code_mode_calls")
    assert [call["call_id"] for call in entry["calls"]] == ["c1"]
    assert entry["error"] == (
        "the rollout does not show the end of turn turn-fake-1; calls may be missing"
    )


def test_an_unreadable_rollout_is_journaled_and_the_job_still_settles(lab, write_rollout):
    path = write_rollout("12-00-00", "thr-fake-1", [STARTED, COMPLETE])
    path.write_bytes(path.read_bytes() + b"\xff\xfe\n")  # cut inside a multibyte character
    lab.fake.set_script({"turn": TURN})
    job_dir = lab.job()
    assert lab.run(job_dir) == 0
    assert lab.load(job_dir)["state"] == "succeeded"
    (entry,) = lab.events(job_dir, "code_mode_calls")
    assert (entry["calls"], entry["error"].startswith("rollout unreadable: ")) == ([], True)


@pytest.mark.parametrize("runner_pid", [None, os.getpid()], ids=["runner starting", "runner alive"])
def test_reconcile_leaves_a_job_alone_while_its_runner_may_be_alive(lab, runner_pid):
    job_dir = lab.job()
    if runner_pid:
        records.update_job(lab.root, job_dir.name, runner_pid=runner_pid)
    before = lab.load(job_dir)
    assert lab.reconcile(job_dir) == before
    assert [e["kind"] for e in records.read_events(job_dir)] == ["created"]


def test_appserver_stopped_at_is_set_only_after_the_process_group_is_gone(lab):
    lab.fake.set_script({"ignore_sigterm": True, "spawn_child": True, "turn": TURN})
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir)
    job = wait_until(
        lambda: (j := lab.load(job_dir))["state"] == "succeeded" and j, what="succeeded"
    )
    assert job["appserver_stopped_at"] is None
    os.killpg(job["pgid"], 0)  # still alive: the fake ignores SIGTERM until the runner escalates
    assert process.wait(timeout=30) == 0
    assert lab.load(job_dir)["appserver_stopped_at"] is not None
    assert group_gone(job["pgid"])


def test_reconcile_stops_the_app_server_of_a_job_that_settled_before_stopping_it(lab):
    lab.fake.set_script({"turn": TURN})
    job_dir = lab.job()
    crashed = lab.crash(job_dir, "before-stop")
    assert (crashed["state"], crashed["appserver_stopped_at"]) == ("succeeded", None)
    assert not group_gone(crashed["pgid"])
    result = (job_dir / "result.json").read_text()
    (job,) = reconcile.reconcile_all(lab.root, sessions_dir=lab.sessions)
    assert job["appserver_stopped_at"] is not None and group_gone(crashed["pgid"])
    assert (job["state"], (job_dir / "result.json").read_text()) == ("succeeded", result)
    assert lab.events(job_dir, "reconciled") == [{"route": "stop-server"}]


def test_stop_server_leaves_a_reused_process_group_alone(lab):
    unrelated = lab.fake.launch(name="someone-else")  # its socket is not the job's
    job_dir = lab.job()
    records.update_job(lab.root, job_dir.name, pgid=unrelated.pgid)
    assert runner.stop_server(job_dir) is True
    assert not group_gone(unrelated.pgid)
    assert lab.load(job_dir)["appserver_stopped_at"] is not None
    (reused,) = lab.events(job_dir, "appserver_pgid_reused")
    assert reused["pgid"] == unrelated.pgid


def test_stop_server_does_not_trust_a_stop_that_leaves_the_group_alive(lab, monkeypatch):
    job_dir = lab.job()
    server = lab.fake.launch(name=f"jobs/{job_dir.name}")
    records.update_job(lab.root, job_dir.name, pgid=server.pgid)
    monkeypatch.setattr(runner.appserver, "stop_app_server", lambda pgid, grace=0: True)
    assert runner.stop_server(job_dir) is False
    assert lab.load(job_dir)["appserver_stopped_at"] is None
    monkeypatch.undo()
    assert runner.stop_server(job_dir, grace=1.0) is True
    assert lab.load(job_dir)["appserver_stopped_at"] is not None


def test_consult_job_succeeds_delivers_and_journals_every_notification(lab):
    lab.fake.set_script(
        {
            "turn": {
                "events": [
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thr-fake-1",
                            "turnId": "turn-fake-1",
                            "tokenUsage": TOKEN_USAGE,
                        },
                    }
                ],
                **TURN,
            }
        }
    )
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir)
    assert process.wait(timeout=60) == 0
    job = lab.load(job_dir)
    assert (job["state"], job["error"], job["runner_pid"]) == ("succeeded", None, process.pid)
    assert (job["thread_id"], job["turn_id"], job["effort_used"]) == (
        "thr-fake-1",
        "turn-fake-1",
        "low",
    )
    assert job["model_used"] == "gpt-6-luna"
    assert job["admission"] == {
        "allowed": True,
        "ordinaryUsageAllowed": True,
        "usedPercent": 12.0,
        "windowDurationMins": 10080,
        "resetsAt": 1794800000,
        "rateLimitReachedType": None,
        "reserve_percent": 15,
    }
    assert job["result_path"] == str(job_dir / "result.json")
    assert job["appserver_stopped_at"] is not None and group_gone(job["pgid"])

    result = json.loads((job_dir / "result.json").read_text())
    records.validate_result(result)
    assert (result["state"], result["ran"], result["task_succeeded"], result["recovery"]) == (
        "succeeded",
        True,
        True,
        None,
    )
    # where the deliverable is, for callers that get only result.json
    assert result["output_dir"] == str(lab.output)
    snapshot = job["admission"]
    assert result["usage"] == {
        "tokens": TOKEN_USAGE,
        "rate_limits_before": snapshot,
        "rate_limits_after": snapshot,
        "rate_limit_reached_type": None,
    }
    assert sorted(os.listdir(lab.output)) == ["answer.md", "result.json"]
    assert (lab.output / "answer.md").read_text() == "The answer."
    assert json.loads((lab.output / "result.json").read_text()) == result

    sent = [
        r["msg"]["method"]
        for r in lab.fake.records("send")
        if r["client"] == 1 and "method" in r["msg"]
    ]
    assert [n["method"] for n in lab.events(job_dir, "notification")] == sent[
        : sent.index("turn/completed") + 1
    ]
    moves = [t["to"] for t in lab.events(job_dir, "transition")]
    assert moves == ["admitted", "starting", "dispatched", "running", "succeeded"]


def test_runner_ignores_modules_in_the_callers_folder(lab, tmp_path, monkeypatch):
    lab.fake.set_script({"turn": TURN})
    folder = tmp_path / "callers-folder"
    folder.mkdir()
    (folder / "json.py").write_text("raise SystemExit(7)\n")
    monkeypatch.chdir(folder)
    assert lab.run(lab.job()) == 0


def test_requests_carry_model_sandbox_schema_prompt_and_images(lab, tmp_path):
    lab.fake.set_script({"turn": TURN})
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    job_dir = lab.job(model_slug="gpt-9-resolved", images=[str(image)])
    assert lab.run(job_dir) == 0
    (thread_start,) = lab.requests("thread/start")
    expected = {
        "cwd": str(lab.project),
        "model": "gpt-9-resolved",
        "sandbox": "read-only",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
    }
    assert thread_start["params"] == expected
    (turn_start,) = lab.requests("turn/start")
    params = turn_start["params"]
    assert (params["effort"], params["approvalPolicy"], params["approvalsReviewer"]) == (
        "low",
        "on-request",
        "user",
    )
    assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": True}
    assert params["outputSchema"] == json.loads((SCHEMAS / "consult.schema.json").read_text())
    text, picture = params["input"]
    assert text["text"].startswith("Question for Codex\n\nOUTPUT CONTRACT:")
    assert picture == {"type": "localImage", "path": str(image)}
    methods = [m.get("method") for m in lab.fake.received()]
    assert (
        methods.index("account/rateLimits/read")
        < methods.index("thread/start")
        < methods.index("turn/start")
    )


def test_approval_requests_are_declined_and_journaled(lab):
    lab.fake.set_script(
        {"turn": {"approvals": [COMMAND_APPROVAL, PERMISSIONS_APPROVAL, ELICITATION], **TURN}}
    )
    job_dir = lab.job()
    assert lab.run(job_dir) == 0
    answers = [m["result"] for m in lab.fake.received() if "result" in m and "method" not in m]
    assert answers == [{"decision": "decline"}, {"permissions": {}}, {"action": "decline"}]
    declined = [COMMAND_APPROVAL["method"], PERMISSIONS_APPROVAL["method"], ELICITATION["method"]]
    assert [e["method"] for e in lab.events(job_dir, "approval_declined")] == declined


def test_timeout_interrupts_the_turn_and_never_starts_another(lab):
    lab.fake.set_script({"turn": {"hang": True}})
    job_dir = lab.job(timeout_seconds=3)  # room for startup, which the timeout also counts
    assert lab.run(job_dir) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["error"]) == ("timed_out", "timed out after 3s")
    assert lab.turn_starts() == 1
    (interrupt,) = lab.requests("turn/interrupt")
    assert interrupt["params"] == {"threadId": job["thread_id"], "turnId": job["turn_id"]}
    assert json.loads((job_dir / "result.json").read_text())["task_succeeded"] is False


def test_a_startup_slower_than_the_timeout_times_out_without_dispatching(lab):
    lab.fake.set_script({"delays": {"thread/start": 2}, "turn": TURN})
    job_dir = lab.job(timeout_seconds=1)
    assert lab.run(job_dir) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["error"], job["turn_id"]) == ("timed_out", "timed out after 1s", None)
    assert lab.turn_starts() == 0
    assert json.loads((job_dir / "result.json").read_text())["ran"] is False
    assert job["appserver_stopped_at"] is not None and group_gone(job["pgid"])


@pytest.mark.parametrize("cancel_requested, state", [(True, "cancelled"), (False, "interrupted")])
def test_interrupted_turn_is_cancelled_only_when_a_cancel_was_requested(
    lab, cancel_requested, state
):
    lab.fake.set_script({"turn": {"hang": True}})
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir)
    assert os.getsid(process.pid) == process.pid  # detached into its own session
    job = wait_until(lambda: (j := lab.load(job_dir))["state"] == "running" and j, what="running")
    if cancel_requested:
        records.update_job(lab.root, job_dir.name, cancel_requested_at=records.now_stamp())
    with AppServerClient(job_dir / "app.sock") as client:
        client.initialize()
        client.call("turn/interrupt", {"threadId": job["thread_id"], "turnId": job["turn_id"]})
    assert process.wait(timeout=60) == 1
    assert lab.load(job_dir)["state"] == state
    assert lab.turn_starts() == 1


def test_cancel_during_a_slow_thread_start_ends_the_job_without_dispatching(lab):
    lab.fake.set_script({"delays": {"thread/start": 2}, "turn": TURN})
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir)
    lab.fake.wait_for(lambda r: r["kind"] == "recv" and r["msg"].get("method") == "thread/start")
    records.update_job(lab.root, job_dir.name, cancel_requested_at=records.now_stamp())
    assert process.wait(timeout=60) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["error"], job["turn_id"]) == ("cancelled", None, None)
    assert lab.turn_starts() == 0
    assert job["appserver_stopped_at"] is not None and group_gone(job["pgid"])
    assert json.loads((job_dir / "result.json").read_text())["ran"] is False


@pytest.mark.parametrize(
    "allow_commit, line",
    [
        (False, "Do not commit or push; the caller commits."),
        (True, "You may commit your work to the current branch."),
    ],
)
def test_run_job_writes_in_its_worktree_and_reports_what_git_saw(lab, tmp_path, allow_commit, line):
    tree = tmp_path / "tree"

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tree), *GIT_IDENTITY, *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "work", str(tree)], check=True)
    (tree / "edit.txt").write_text("v1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    base = git("rev-parse", "HEAD")
    (tree / "edit.txt").write_text("v2\n")
    (tree / "new.txt").write_text("new\n")
    lab.fake.set_script({"turn": {"final_answer": json.dumps(TASK)}})
    job_dir = lab.job(
        kind="run", sandbox="workspace-write", allow_commit=allow_commit, cwd=str(tree)
    )
    records.update_job(lab.root, job_dir.name, worktree=str(tree), branch="work", base_commit=base)
    assert lab.run(job_dir) == 0
    (thread_start,) = lab.requests("thread/start")
    assert (thread_start["params"]["cwd"], thread_start["params"]["sandbox"]) == (
        str(tree),
        "workspace-write",
    )
    (turn_start,) = lab.requests("turn/start")
    permission_class = permissions.WRITE_COMMIT if allow_commit else permissions.WRITE
    assert (
        turn_start["params"]["sandboxPolicy"] == permissions.policy_for(permission_class, tree)[1]
    )
    assert turn_start["params"]["sandboxPolicy"]["writableRoots"][0] == str(tree.resolve())
    assert turn_start["params"]["outputSchema"] == json.loads(
        (SCHEMAS / "task.schema.json").read_text()
    )
    text = turn_start["params"]["input"][0]["text"]
    assert text.startswith(f"Question for Codex\n\n{line}") and "\n\nOUTPUT CONTRACT:" in text
    result = json.loads((job_dir / "result.json").read_text())
    records.validate_result(result)
    assert (result["worktree"], result["branch"], result["commits"]) == (str(tree), "work", [])
    assert result["changed_files"] == ["edit.txt", "new.txt"]
    assert result["tests_run"] == ["pytest -q"] and result["task_succeeded"] is True
    assert (
        lab.output / "answer.md"
    ).read_text() == "Edited the file.\n\n## Blockers\n\n- no network\n"


def test_run_job_whose_worktree_is_gone_at_the_end_settles_failed(lab, tmp_path):
    gone = tmp_path / "deleted-worktree"
    lab.fake.set_script({"turn": {"final_answer": json.dumps(TASK)}})
    job_dir = lab.job(kind="run", sandbox="workspace-write", allow_commit=False, cwd=str(tmp_path))
    records.update_job(
        lab.root, job_dir.name, worktree=str(gone), branch="work", base_commit="0" * 40
    )
    lab.run(job_dir)
    job = lab.load(job_dir)
    assert job["state"] == "failed" and job["error"].startswith(
        "the worktree could not be read: git "
    )
    result = json.loads((job_dir / "result.json").read_text())
    assert (result["state"], result["task_succeeded"], result["changed_files"]) == (
        "failed",
        False,
        [],
    )


@pytest.mark.parametrize(
    "mode, method, thread_id",
    [("resume", "thread/resume", "thr-src"), ("fork", "thread/fork", "thr-src-fork")],
)
def test_resume_and_fork_open_the_source_thread(lab, mode, method, thread_id):
    lab.fake.set_script({"turn": TURN})
    job_dir = lab.job(source_mode=mode, source_thread_id="thr-src")
    assert lab.run(job_dir) == 0
    (opened,) = lab.requests(method)
    assert opened["params"]["threadId"] == "thr-src"
    assert (opened["params"]["cwd"], opened["params"]["sandbox"]) == (str(lab.project), "read-only")
    assert lab.requests("thread/start") == []
    (turn_start,) = lab.requests("turn/start")
    assert turn_start["params"]["threadId"] == thread_id
    assert lab.load(job_dir)["thread_id"] == thread_id


@pytest.mark.parametrize(
    "turn, state, task_succeeded, error",
    [
        (
            {
                "final_answer": json.dumps(
                    {**ANSWER, "status": "blocked", "open_questions": ["Which repo?"]}
                )
            },
            "succeeded",
            False,
            None,
        ),
        ({"final_answer": "not json"}, "failed", False, "final answer rejected: not JSON"),
        (
            {"final_answer": json.dumps({"status": "completed"})},
            "failed",
            False,
            "$.answer is missing",
        ),
        (
            {"final_answer": json.dumps({**ANSWER, "extra": 1})},
            "failed",
            False,
            "$.extra is not allowed",
        ),
        (
            {"status": "failed", "error": {"message": "model refused"}},
            "failed",
            False,
            "model refused",
        ),
    ],
    ids=["blocked answer", "not json", "missing field", "extra field", "failed turn"],
)
def test_turn_outcomes_map_to_job_states(lab, turn, state, task_succeeded, error):
    lab.fake.set_script({"turn": turn})
    job_dir = lab.job()
    assert lab.run(job_dir) == (0 if state == "succeeded" else 1)
    job = lab.load(job_dir)
    assert job["state"] == state
    assert error is None or error in job["error"]
    assert json.loads((job_dir / "result.json").read_text())["task_succeeded"] is task_succeeded
    if turn.get("final_answer", "").startswith('{"status": "blocked"'):
        assert (
            lab.output / "answer.md"
        ).read_text() == "The answer.\n\n## Open questions\n\n- Which repo?\n"


def review_job(lab, repo, **overrides):
    return lab.job(kind="review", cwd=str(repo), project_dir=str(repo), **overrides)


def attestation_of(directory):
    return json.loads((directory / "attestation.json").read_text())


def test_review_job_delivers_its_files_and_attests_the_state_it_read(lab, review_repo):
    lab.fake.set_script({"turn": REVIEW_TURN})
    job_dir = review_job(lab, review_repo)
    assert lab.run(job_dir) == 0
    (turn_start,) = lab.requests("turn/start")
    assert turn_start["params"]["outputSchema"] == review.load_schema()
    assert turn_start["params"]["sandboxPolicy"]["type"] == "readOnly"
    assert turn_start["params"]["input"][0]["text"].endswith(review.REVIEW_CONTRACT)
    assert sorted(os.listdir(lab.output)) == [
        "attestation.json",
        "findings.json",
        "findings.md",
        "result.json",
        "review.md",
    ]
    result = json.loads((lab.output / "result.json").read_text())
    records.validate_result(result)
    assert (result["kind"], result["task_succeeded"], result["review_approved"]) == (
        "review",
        True,
        False,
    )
    assert [
        f["id"] for f in json.loads((lab.output / "findings.json").read_text())["findings"]
    ] == ["H1"]
    attestation = attestation_of(lab.output)
    head = subprocess.run(
        ["git", "-C", str(review_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        attestation["exit_status"],
        attestation["head_sha"],
        attestation["git_branch"],
        attestation["job_id"],
    ) == ("ok", head, "feature", job_dir.name)
    assert attestation == attestation_of(job_dir)


def test_a_review_whose_head_moved_while_it_ran_is_not_ok_and_did_not_succeed(
    lab, review_repo, commit_file
):
    lab.fake.set_script({"turn": {"final_delay": 3, **REVIEW_TURN}})
    job_dir = review_job(lab, review_repo)
    process = runner.spawn_runner(job_dir)
    wait_until(lambda: lab.load(job_dir)["turn_id"], what="a dispatched turn")
    commit_file(review_repo, "late.txt")
    assert process.wait(timeout=60) == 0
    assert attestation_of(lab.output)["exit_status"] == "failure: head moved during review"
    result = json.loads((lab.output / "result.json").read_text())
    records.validate_result(result)
    assert (result["state"], result["task_succeeded"], result["review_approved"]) == (
        "succeeded",
        False,
        None,
    )


@pytest.mark.parametrize(
    "turn, overrides, state, failure",
    [
        ({"hang": True}, {"timeout_seconds": 1}, "timed_out", "failure: timed out after 1s"),
        (
            {"status": "failed", "error": {"message": "model refused"}},
            {},
            "failed",
            "failure: model refused",
        ),
        (
            {"final_answer": json.dumps({**REVIEW, "status": "blocked", "findings": []})},
            {},
            "succeeded",
            "failure: review blocked",
        ),
    ],
    ids=["timed out", "failed turn", "blocked review"],
)
def test_review_that_does_not_complete_still_writes_a_failed_attestation(
    lab, review_repo, turn, overrides, state, failure
):
    lab.fake.set_script({"turn": turn})
    job_dir = review_job(lab, review_repo, **overrides)
    lab.run(job_dir)
    assert lab.load(job_dir)["state"] == state
    attestation = attestation_of(lab.output)
    assert attestation["exit_status"] == failure
    assert attestation["head_sha"] is not None  # the snapshot taken before dispatch
    assert json.loads((lab.output / "result.json").read_text())["review_approved"] is None
    assert (lab.output / "review.md").exists() is (state == "succeeded")


def test_a_review_before_the_first_commit_runs_and_is_never_attested_ok(lab, tmp_path):
    unborn = tmp_path / "unborn"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(unborn)], check=True)
    lab.fake.set_script({"turn": REVIEW_TURN})
    job_dir = review_job(lab, unborn.resolve())
    assert lab.run(job_dir) == 0
    assert lab.turn_starts() == 1
    attestation = attestation_of(lab.output)
    assert (
        attestation["exit_status"]
        == "failure: not attestable: unborn HEAD: the repository has no commit yet"
    )
    assert (attestation["head_sha"], attestation["git_branch"]) == (None, "trunk")


def test_a_rerun_into_an_earlier_output_dir_leaves_only_its_own_files(lab, review_repo):
    approved = {**REVIEW, "verdict": "approve", "findings": []}
    lab.fake.set_script({"turn": {"final_answer": json.dumps(approved)}})
    assert lab.run(review_job(lab, review_repo)) == 0
    lab.fake.set_script({"turn": {"hang": True}})
    rerun = review_job(lab, review_repo, timeout_seconds=1)
    lab.run(rerun)
    assert sorted(os.listdir(lab.output)) == ["attestation.json", "result.json"]
    result = json.loads((lab.output / "result.json").read_text())
    assert (result["job_id"], result["state"], result["task_succeeded"]) == (
        rerun.name,
        "timed_out",
        False,
    )


@pytest.mark.parametrize(
    "point, delay",
    [("after-complete", 0), ("after-accept", 3)],
    ids=["reconcile from thread", "adopt"],
)
def test_a_review_settled_after_its_runner_died_is_attested_from_the_saved_snapshot(
    lab, review_repo, point, delay
):
    lab.fake.set_script({"turn": {"final_delay": delay, **REVIEW_TURN}})
    job_dir = review_job(lab, review_repo)
    lab.crash(job_dir, point)
    assert not (lab.output / "attestation.json").exists()
    lab.reconcile(job_dir)
    assert lab.settled(job_dir)["state"] == "succeeded"
    assert attestation_of(lab.output)["exit_status"] == "ok"
    assert (lab.output / "findings.md").exists() and lab.turn_starts() == 1


@pytest.mark.parametrize("exit_code, state", [(0, "succeeded"), (1, "failed")])
def test_code_mode_host_is_probed_before_the_app_server_starts(lab, exit_code, state):
    lab.fake.set_script({"turn": TURN})
    host = lab.fake.bin_dir / "codex-code-mode-host"
    host.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    host.chmod(0o755)
    job_dir = lab.job()
    lab.run(job_dir)
    job = lab.load(job_dir)
    assert job["state"] == state
    if state == "failed":
        assert "code-mode host startup failed" in job["error"]
        assert lab.fake.records("start") == []
        assert "Host startup failed" in (job_dir / "startup.log").read_text()


@pytest.mark.parametrize("name", ["consult", "task"])
def test_output_schemas_are_strict_for_structured_output(name):
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def limits(*, used=20, allowed=True, primary="window", reached=None):
    window = {"usedPercent": used, "windowDurationMins": 10080, "resetsAt": 1794800000}
    return {
        "ordinaryUsageAllowed": allowed,
        "rateLimits": {
            "primary": window if primary == "window" else primary,
            "rateLimitReachedType": reached,
        },
    }


def test_fifth_job_waits_queued_until_a_slot_frees(lab, hold_slots):
    lab.fake.set_script({"turn": TURN})
    holders = hold_slots(lab.root, 4)
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir, recheck_seconds=0.2)
    wait_until(lambda: lab.events(job_dir, "queued_behind"), what="queued_behind")
    assert lab.load(job_dir)["state"] == "queued"
    assert lab.fake.records("start") == []
    holders[0].kill()
    holders[0].wait()
    assert process.wait(timeout=60) == 0
    assert lab.load(job_dir)["state"] == "succeeded"
    assert [t["to"] for t in lab.events(job_dir, "transition")][:2] == ["admitted", "starting"]


def test_cancel_while_queued_ends_the_job_without_an_app_server(lab, hold_slots):
    hold_slots(lab.root, 4)
    job_dir = lab.job()
    process = runner.spawn_runner(job_dir, recheck_seconds=60)
    wait_until(lambda: lab.events(job_dir, "queued_behind"), what="queued_behind")
    records.update_job(lab.root, job_dir.name, cancel_requested_at=records.now_stamp())
    assert process.wait(timeout=30) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["error"], job["pgid"]) == ("cancelled", None, None)
    assert lab.fake.records("start") == []


def test_usage_over_the_reserve_fails_the_job_before_any_thread(lab):
    lab.fake.set_script(
        {"responses": {"account/rateLimits/read": limits(used=90, reached="primary")}, "turn": TURN}
    )
    job_dir = lab.job()
    assert lab.run(job_dir) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["error"]) == (
        "failed",
        "usage reserve reached: 90% used, 15% reserved",
    )
    assert job["admission"]["usedPercent"] == 90 and admission.refused(job["admission"])
    assert lab.requests("thread/start") == [] and lab.turn_starts() == 0
    assert json.loads((job_dir / "result.json").read_text())["usage"] == {
        "tokens": None,
        "rate_limits_before": job["admission"],
        "rate_limits_after": None,
        "rate_limit_reached_type": "primary",
    }
    assert job["appserver_stopped_at"] is not None and group_gone(job["pgid"])


def test_effort_used_is_the_effort_the_server_applied_not_the_one_requested(lab):
    lab.fake.set_script({"turn": {**TURN, "applied_effort": "medium"}})
    job_dir = lab.job()
    assert lab.run(job_dir) == 0
    assert lab.load(job_dir)["effort_used"] == "medium"
    assert json.loads((job_dir / "result.json").read_text())["effort_used"] == "medium"


def test_a_usage_limit_hit_mid_turn_is_recorded_with_both_snapshots(lab):
    error = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
    lab.fake.set_script(
        {
            "responses": {"account/rateLimits/read": limits(used=50, reached="primary")},
            "turn": {**TURN, "status": "failed", "error": error},
        }
    )
    job_dir = lab.job()
    assert lab.run(job_dir) == 1
    job = lab.load(job_dir)
    assert (job["state"], job["error"], job["codex_error_info"]) == (
        "failed",
        error["message"],
        "usageLimitExceeded",
    )
    usage = json.loads((job_dir / "result.json").read_text())["usage"]
    assert usage == {
        "tokens": None,
        "rate_limits_before": job["admission"],
        "rate_limits_after": job["admission"],
        "rate_limit_reached_type": "primary",
    }


@pytest.mark.parametrize(
    "reply, allowed",
    [(limits(allowed=None), "unknown"), (limits(primary=None), True)],
    ids=["ordinaryUsageAllowed-null", "primary-null"],
)
def test_null_rate_limits_are_recorded_and_do_not_block(lab, reply, allowed):
    lab.fake.set_script({"responses": {"account/rateLimits/read": reply}, "turn": TURN})
    job_dir = lab.job()
    assert lab.run(job_dir) == 0
    assert lab.load(job_dir)["admission"]["allowed"] == allowed


def test_snake_case_rate_limits_fail_the_job_instead_of_reading_headroom(lab):
    reply = {
        "ordinaryUsageAllowed": True,
        "rateLimits": {"primary": {"used_percent": 99, "resetsAt": 1}},
    }
    lab.fake.set_script({"responses": {"account/rateLimits/read": reply}, "turn": TURN})
    job_dir = lab.job()
    assert lab.run(job_dir) == 1
    job = lab.load(job_dir)
    assert job["state"] == "failed" and "usedPercent" in job["error"]
    assert lab.requests("thread/start") == [] and lab.turn_starts() == 0


@pytest.mark.parametrize("state", ["queued", "admitted"])
def test_a_job_whose_runner_died_before_its_turn_is_failed_and_frees_its_slot(lab, state):
    job_dir = lab.job()
    dead = subprocess.Popen(["true"])
    dead.wait()
    records.update_job(lab.root, job_dir.name, runner_pid=dead.pid)
    if state == "admitted":
        records.transition(lab.root, job_dir.name, "admitted")
    job = lab.reconcile(job_dir)
    assert job["state"] == "failed" and job["turn_id"] is None
    assert admission.slot_holders(lab.root) == []
