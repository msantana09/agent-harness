"""Tests for codex_jobs.admission: usage rules, the concurrency cap, and the config loader.

Runner pids are real `sleep` child processes so liveness is the real `os.kill(pid, 0)`; the
rate-limits round trip goes through the real AppServerClient to tests/fake_codex.py.
"""

import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import admission, records
from codex_jobs.admission import AdmissionConfig, AdmissionConfigError, MalformedRateLimits
from codex_jobs.appserver import AppServerClient

CONFIG = AdmissionConfig(max_concurrent_jobs=4, reserve_percent=15)
TRACKED_CONFIG = Path(__file__).resolve().parent.parent / "admission.json"


def limits(used=20, *, allowed=True, key="usedPercent", resets=1794410566, reached=None):
    return {
        "ordinaryUsageAllowed": allowed,
        "rateLimits": {
            "primary": {key: used, "windowDurationMins": 10080, "resetsAt": resets},
            "rateLimitReachedType": reached,
        },
    }


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen]]:
    spawned: list[subprocess.Popen] = []
    yield spawned
    for proc in spawned:
        proc.kill()
        proc.wait()


def spawn_runner(children) -> subprocess.Popen:
    proc = subprocess.Popen(["sleep", "300"])
    children.append(proc)
    return proc


def make_job(root: Path, pid: int | None, state: str = records.RUNNING) -> dict:
    job, _ = records.create_job(
        root,
        kind="consult",
        cwd="/work",
        project_dir="/work",
        model_requested="gpt-6-luna",
        model_slug="gpt-6-luna",
        effort_requested="low",
        sandbox="read-only",
        timeout_seconds=60,
        codex_bin="codex",
    )
    job_id = job["job_id"]
    records.update_job(root, job_id, runner_pid=pid)
    for step in (records.ADMITTED, records.STARTING, records.DISPATCHED, records.RUNNING):
        if state == records.QUEUED:
            break
        records.transition(root, job_id, step)
        if step == state:
            break
    return records.load_job(root, job_id)


def queued_behind_events(root: Path, job_id: str) -> list[list[str]]:
    return [
        e["data"]["queued_behind"]
        for e in records.read_events(root / job_id)
        if e["kind"] == "queued_behind"
    ]


def test_ordinary_usage_false_is_refused_with_reason():
    decision = admission.check_usage(limits(10, allowed=False), CONFIG)
    assert decision.allowed is False
    assert decision.reason == "usage not allowed"
    assert decision.record["allowed"] is False
    assert decision.resets_at == "2026-11-11T15:22:46Z"


def test_ordinary_usage_null_records_unknown_and_percent_rule_decides():
    ok = admission.check_usage(limits(20, allowed=None), CONFIG)
    assert ok.allowed is True and ok.record["allowed"] == "unknown"
    refused = admission.check_usage(limits(90, allowed=None), CONFIG)
    assert refused.allowed is False and refused.record["allowed"] == "unknown"


def test_used_percent_above_reserve_refused_with_resets_at():
    decision = admission.check_usage(limits(86, reached="primary"), CONFIG)
    assert decision.allowed is False
    assert decision.resets_at == "2026-11-11T15:22:46Z"
    assert "86%" in decision.reason and "15%" in decision.reason
    assert decision.record["usedPercent"] == 86
    assert decision.record["rateLimitReachedType"] == "primary"


def test_used_percent_at_reserve_boundary_allowed():
    decision = admission.check_usage(limits(85), CONFIG)
    assert decision.allowed is True and decision.reason is None
    assert decision.record == {
        "allowed": True,
        "ordinaryUsageAllowed": True,
        "usedPercent": 85,
        "windowDurationMins": 10080,
        "resetsAt": 1794410566,
        "rateLimitReachedType": None,
        "reserve_percent": 15,
    }


@pytest.mark.parametrize("allowed, recorded", [(True, True), (None, "unknown")])
def test_null_primary_records_unknown_usage_and_does_not_refuse(allowed, recorded):
    decision = admission.check_usage(
        {"ordinaryUsageAllowed": allowed, "rateLimits": {"primary": None}}, CONFIG
    )
    assert decision.allowed is True and decision.reason is None and decision.resets_at is None
    assert decision.record["usedPercent"] == "unknown" and decision.record["allowed"] == recorded


def test_null_primary_with_ordinary_usage_false_is_still_refused():
    decision = admission.check_usage(
        {"ordinaryUsageAllowed": False, "rateLimits": {"primary": None}}, CONFIG
    )
    assert decision.allowed is False and decision.reason == "usage not allowed"


@pytest.mark.parametrize(
    "reply",
    [
        limits(20, key="used_percent"),
        {"ordinaryUsageAllowed": True, "rateLimits": {}},
        {"ordinaryUsageAllowed": True},
    ],
    ids=["snake_case", "no-primary", "no-rateLimits"],
)
def test_malformed_rate_limits_raise_instead_of_allowing(reply):
    with pytest.raises(MalformedRateLimits):
        admission.check_usage(reply, CONFIG)


def test_cap_reached_holds_then_frees_and_journals_queued_behind(tmp_path, children):
    holders = [make_job(tmp_path, spawn_runner(children).pid) for _ in range(4)]
    waiter = make_job(tmp_path, spawn_runner(children).pid, state=records.QUEUED)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert records.load_job(tmp_path, waiter["job_id"])["state"] == records.QUEUED
        if len(sleeps) == 3:  # the third recheck sees one holder gone
            children[0].terminate()
            children[0].wait()

    admitted = admission.admit(
        tmp_path,
        waiter["job_id"],
        CONFIG,
        is_cancelled=lambda: False,
        sleep=sleep,
        recheck_seconds=15,
    )

    assert admitted is True
    assert sleeps == [15, 15, 15]
    assert records.load_job(tmp_path, waiter["job_id"])["state"] == records.ADMITTED
    # journaled once for the three identical checks, naming the four holders oldest first
    assert queued_behind_events(tmp_path, waiter["job_id"]) == [[j["job_id"] for j in holders]]


def test_slot_holders_ignore_queued_dead_pid_missing_pid_and_terminal_jobs(tmp_path, children):
    live = make_job(tmp_path, spawn_runner(children).pid)
    gone = subprocess.Popen(["true"])
    gone.wait()
    make_job(tmp_path, gone.pid)  # slot state, dead runner
    make_job(tmp_path, None, state=records.ADMITTED)  # slot state, no runner recorded
    make_job(tmp_path, spawn_runner(children).pid, state=records.QUEUED)  # live but holds no slot
    finished = make_job(tmp_path, spawn_runner(children).pid)
    records.transition(tmp_path, finished["job_id"], records.SUCCEEDED)

    assert [j["job_id"] for j in admission.slot_holders(tmp_path)] == [live["job_id"]]


def test_dead_runner_with_live_appserver_group_holds_its_slot_until_the_group_is_gone(
    tmp_path, children
):
    for _ in range(3):
        make_job(tmp_path, spawn_runner(children).pid)
    orphan = make_job(tmp_path, 2**22 + 1)  # runner pid that is not alive
    appserver = subprocess.Popen(["sleep", "300"], start_new_session=True)
    children.append(appserver)
    records.update_job(tmp_path, orphan["job_id"], pgid=appserver.pid)
    waiter = make_job(tmp_path, spawn_runner(children).pid, state=records.QUEUED)

    assert orphan["job_id"] in [j["job_id"] for j in admission.slot_holders(tmp_path)]
    assert admission._try_admit(tmp_path, waiter["job_id"], CONFIG) != []
    assert records.load_job(tmp_path, waiter["job_id"])["state"] == records.QUEUED

    appserver.kill()
    appserver.wait()

    assert orphan["job_id"] not in [j["job_id"] for j in admission.slot_holders(tmp_path)]
    assert admission._try_admit(tmp_path, waiter["job_id"], CONFIG) == []


@pytest.mark.parametrize("state", [records.OUTCOME_UNKNOWN, records.TIMED_OUT])
def test_terminal_job_holds_its_slot_while_its_appserver_group_is_alive(tmp_path, children, state):
    for _ in range(3):
        make_job(tmp_path, spawn_runner(children).pid)
    ended = make_job(tmp_path, spawn_runner(children).pid)
    appserver = subprocess.Popen(["sleep", "300"], start_new_session=True)
    children.append(appserver)
    records.update_job(tmp_path, ended["job_id"], pgid=appserver.pid)
    records.transition(tmp_path, ended["job_id"], state)
    waiter = make_job(tmp_path, spawn_runner(children).pid, state=records.QUEUED)

    assert admission._try_admit(tmp_path, waiter["job_id"], CONFIG) != []

    appserver.kill()
    appserver.wait()

    assert admission._try_admit(tmp_path, waiter["job_id"], CONFIG) == []


def test_concurrent_admits_never_exceed_the_cap_and_the_rest_follow_as_slots_free(
    tmp_path, children
):
    jobs = [make_job(tmp_path, spawn_runner(children).pid, state=records.QUEUED) for _ in range(6)]
    results: dict[str, bool] = {}

    def run(job_id: str) -> None:
        results[job_id] = admission.admit(
            tmp_path, job_id, CONFIG, is_cancelled=lambda: False, recheck_seconds=0.01
        )

    def admitted_ids() -> list[str]:
        return [j["job_id"] for j in records.list_jobs(tmp_path) if j["state"] == records.ADMITTED]

    threads = [threading.Thread(target=run, args=(job["job_id"],)) for job in jobs]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 5
        while len(admitted_ids()) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)  # long enough for the two waiters to retry many times
        first = admitted_ids()
        assert len(first) == 4 and not results.keys() - set(first)

        # two of the admitted jobs' runners exit: the two waiters take their slots
        by_id = {job["job_id"]: child for job, child in zip(jobs, children, strict=True)}
        for job_id in first[:2]:
            by_id[job_id].terminate()
            by_id[job_id].wait()
    finally:
        for thread in threads:
            thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert results == {job["job_id"]: True for job in jobs}
    assert len(admission.slot_holders(tmp_path)) == 4


def test_cancel_while_queued_returns_without_a_slot(tmp_path, children):
    for _ in range(4):
        make_job(tmp_path, spawn_runner(children).pid)
    waiter = make_job(tmp_path, spawn_runner(children).pid, state=records.QUEUED)
    checks = iter([False, False, True])

    admitted = admission.admit(
        tmp_path,
        waiter["job_id"],
        CONFIG,
        is_cancelled=lambda: next(checks),
        sleep=lambda seconds: None,
    )

    assert admitted is False
    assert records.load_job(tmp_path, waiter["job_id"])["state"] == records.QUEUED
    assert len(queued_behind_events(tmp_path, waiter["job_id"])) == 1


def test_missing_config_file_raises_with_path(tmp_path):
    path = tmp_path / "admission.json"
    with pytest.raises(AdmissionConfigError) as caught:
        admission.load_config(path)
    assert caught.value.path == path and str(path) in str(caught.value)


@pytest.mark.parametrize("missing", ["max_concurrent_jobs", "reserve_percent"])
def test_missing_config_key_raises_with_path_and_key(tmp_path, missing):
    values = {"max_concurrent_jobs": 4, "reserve_percent": 15}
    del values[missing]
    path = tmp_path / "admission.json"
    path.write_text(json.dumps(values))
    with pytest.raises(AdmissionConfigError) as caught:
        admission.load_config(path)
    assert (caught.value.path, caught.value.key) == (path, missing)
    assert missing in str(caught.value)


def test_tracked_config_holds_the_agreed_values():
    assert admission.load_config(TRACKED_CONFIG) == AdmissionConfig(
        max_concurrent_jobs=4, reserve_percent=15
    )


def test_read_usage_round_trips_through_the_app_server_client(fake_codex):
    fake_codex.set_script({"responses": {"account/rateLimits/read": limits(86)}})
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        client.initialize()
        decision = admission.read_usage(client, CONFIG)
    assert decision.allowed is False and decision.record["usedPercent"] == 86
    assert [
        m["method"] for m in fake_codex.received() if m.get("method") == "account/rateLimits/read"
    ] == ["account/rateLimits/read"]
