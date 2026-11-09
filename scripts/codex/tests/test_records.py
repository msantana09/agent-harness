"""Tests for codex_jobs.records: state machine, journal, atomic write, idempotency, result
envelope."""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import records
from codex_jobs.records import IllegalTransition, InvalidResult, JobNotFound

CHAIN = ["admitted", "starting", "dispatched", "running"]
TERMINALS = ["succeeded", "failed", "cancelled", "interrupted", "timed_out", "outcome_unknown"]


def _create(root, **overrides):
    args = dict(
        kind="consult",
        cwd="/work",
        project_dir="/work",
        model_requested="luna",
        model_slug="gpt-6-luna",
        effort_requested="low",
        sandbox="read-only",
        timeout_seconds=1800,
        codex_bin="/opt/codex/bin/codex",
    )
    return records.create_job(root, **{**args, **overrides})


def test_create_job_id_dir_mode_and_initial_record(tmp_path):
    job, created = _create(tmp_path)
    assert created
    assert re.fullmatch(r"cj-\d{8}-\d{6}-[0-9a-f]{4}", job["job_id"])
    assert (tmp_path / job["job_id"]).stat().st_mode & 0o777 == 0o700
    assert list(records.load_job(tmp_path, job["job_id"])) == list(records.JOB_FIELDS)
    assert job["state"] == "queued"


@pytest.mark.parametrize("terminal", TERMINALS)
def test_legal_chain_reaches_each_terminal_and_journals_each_move(tmp_path, terminal):
    job, _ = _create(tmp_path)
    job_id = job["job_id"]
    path = {"timed_out": CHAIN[:3], "succeeded": CHAIN[:3]}.get(terminal, CHAIN[:2])
    for state in [*path, terminal]:
        records.transition(tmp_path, job_id, state)
    assert records.load_job(tmp_path, job_id)["state"] == terminal
    events = records.read_events(tmp_path / job_id)
    assert [e["kind"] for e in events] == ["created", *["transition"] * (len(path) + 1)]
    assert [e["data"]["to"] for e in events[1:]] == [*path, terminal]


def test_runner_fields_default_empty_and_source_thread_is_kept(tmp_path):
    plain, _ = _create(tmp_path)
    assert (
        plain["codex_bin"],
        plain["images"],
        plain["source_mode"],
        plain["source_thread_id"],
    ) == ("/opt/codex/bin/codex", [], None, None)
    assert (plain["model_requested"], plain["model_slug"]) == ("luna", "gpt-6-luna")
    assert [
        plain[name] for name in ("appserver_stopped_at", "cancel_requested_at", "result_path")
    ] == [None] * 3
    forked, _ = _create(
        tmp_path, images=["/img/a.png"], source_mode="fork", source_thread_id="thr-1"
    )
    loaded = records.load_job(tmp_path, forked["job_id"])
    assert (loaded["images"], loaded["source_mode"], loaded["source_thread_id"]) == (
        ["/img/a.png"],
        "fork",
        "thr-1",
    )
    records.update_job(
        tmp_path,
        forked["job_id"],
        cancel_requested_at="t",
        appserver_stopped_at="t",
        result_path="/r",
    )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"images": ["rel/a.png"]}, "absolute"),
        ({"source_mode": "clone", "source_thread_id": "thr-1"}, "source_mode"),
        ({"source_mode": "resume"}, "together"),
        ({"source_thread_id": "thr-1"}, "together"),
    ],
)
def test_create_job_rejects_bad_images_and_source_pairs(tmp_path, overrides, message):
    with pytest.raises(records.RecordError, match=message):
        _create(tmp_path, **overrides)
    assert not any(p.name.startswith("cj-") for p in tmp_path.iterdir())


def test_full_chain_through_running(tmp_path):
    job, _ = _create(tmp_path)
    for state in [*CHAIN, "succeeded"]:
        records.transition(tmp_path, job["job_id"], state)


def test_illegal_transition_raises_and_leaves_record_unchanged(tmp_path):
    job, _ = _create(tmp_path)
    with pytest.raises(IllegalTransition):
        records.transition(tmp_path, job["job_id"], "running")
    with pytest.raises(IllegalTransition):
        records.transition(tmp_path, job["job_id"], "bogus")
    assert records.load_job(tmp_path, job["job_id"])["state"] == "queued"
    assert len(records.read_events(tmp_path / job["job_id"])) == 1


@pytest.mark.parametrize("terminal", TERMINALS)
def test_terminal_state_is_final(tmp_path, terminal):
    job, _ = _create(tmp_path)
    for state in ["admitted", "starting", "dispatched", terminal]:
        records.transition(tmp_path, job["job_id"], state)
    for target in records.STATES:
        with pytest.raises(IllegalTransition, match="final"):
            records.transition(tmp_path, job["job_id"], target)


def test_transition_merges_fields_and_update_job_refuses_state(tmp_path):
    job, _ = _create(tmp_path)
    records.transition(tmp_path, job["job_id"], "admitted", admission={"allowed": True})
    records.update_job(tmp_path, job["job_id"], runner_pid=4242)
    loaded = records.load_job(tmp_path, job["job_id"])
    assert (loaded["admission"], loaded["runner_pid"]) == ({"allowed": True}, 4242)
    with pytest.raises(records.RecordError):
        records.update_job(tmp_path, job["job_id"], state="succeeded")


def test_crash_between_temp_write_and_replace_keeps_previous_job_json(tmp_path, monkeypatch):
    job, _ = _create(tmp_path)
    job_dir = tmp_path / job["job_id"]
    before = (job_dir / "job.json").read_text()

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(records.os, "replace", boom)
    with pytest.raises(OSError):
        records.transition(tmp_path, job["job_id"], "admitted")
    monkeypatch.undo()

    assert (job_dir / "job.json").read_text() == before
    assert json.loads(before)["state"] == "queued"
    assert sorted(p.name for p in job_dir.iterdir()) == ["events.jsonl", "job.json", "job.lock"]


def test_idempotency_key_reattaches(tmp_path):
    first, first_created = _create(tmp_path, kind="run", idempotency_key="run-7-stage-2")
    second, second_created = _create(tmp_path, kind="run", idempotency_key="run-7-stage-2")
    assert (first_created, second_created) == (True, False)
    assert second["job_id"] == first["job_id"]
    assert [p.name for p in tmp_path.iterdir() if p.name != "by-key"] == [first["job_id"]]


def test_concurrent_submissions_with_one_key_make_one_job(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _create(tmp_path, idempotency_key="same"), range(8)))
    assert len({job["job_id"] for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert len([p for p in tmp_path.iterdir() if p.name != "by-key"]) == 1


def test_keys_differ_and_missing_job_raises(tmp_path):
    a, _ = _create(tmp_path, idempotency_key="a")
    b, _ = _create(tmp_path, idempotency_key="b")
    assert a["job_id"] != b["job_id"]
    assert [j["job_id"] for j in records.list_jobs(tmp_path)] == sorted([a["job_id"], b["job_id"]])
    with pytest.raises(JobNotFound):
        records.load_job(tmp_path, "cj-20260101-000000-abcd")
    with pytest.raises(JobNotFound):
        records.load_job(tmp_path, "../etc")
    assert records.list_jobs(tmp_path / "nothing") == []


def test_read_events_drops_unterminated_final_line(tmp_path):
    records.append_event(tmp_path, "one", {"n": 1})
    with open(tmp_path / "events.jsonl", "a") as handle:
        handle.write('{"ts": "x", "kind": "tor')
    assert [e["kind"] for e in records.read_events(tmp_path)] == ["one"]


def _finished_job(tmp_path, kind="review"):
    job, _ = _create(tmp_path, kind=kind)
    for state in ["admitted", "starting", "dispatched", "running"]:
        records.transition(tmp_path, job["job_id"], state)
    return records.transition(
        tmp_path, job["job_id"], "succeeded", model_used="gpt-6-luna", effort_used="low"
    )


def test_result_envelope_has_required_fields_and_takes_changed_files_from_argument(tmp_path):
    job = _finished_job(tmp_path)
    result = records.build_result(
        job,
        ran=True,
        task_succeeded=None,
        review_approved=False,
        worktree="/wt",
        branch="feat/x",
        commits=["abc123"],
        changed_files=["a.py", "dir/b.py"],
        tests_run=[],
        usage={"input_tokens": 5},
        recovery=None,
        catalog_fetched_at="2026-11-09T00:20:39Z",
    )
    assert set(result) == set(records.RESULT_FIELDS)
    assert result["changed_files"] == ["a.py", "dir/b.py"]
    assert (result["ran"], result["review_approved"], result["task_succeeded"]) == (
        True,
        False,
        None,
    )
    assert (result["model_requested"], result["model_used"]) == ("luna", "gpt-6-luna")
    assert (result["effort_requested"], result["effort_used"]) == ("low", "low")
    records.validate_result(json.loads(json.dumps(result)))


def test_validate_result_rejects_missing_fields_and_inconsistent_flags(tmp_path):
    job = _finished_job(tmp_path, kind="consult")
    good = records.build_result(
        job,
        ran=True,
        task_succeeded=True,
        review_approved=None,
        worktree=None,
        branch=None,
        commits=[],
        changed_files=[],
        tests_run=[],
        usage=None,
        recovery=None,
    )
    broken = {k: v for k, v in good.items() if k != "changed_files"}
    with pytest.raises(InvalidResult, match="changed_files"):
        records.validate_result(broken)
    with pytest.raises(InvalidResult, match="review_approved"):
        records.validate_result({**good, "review_approved": True})
    with pytest.raises(InvalidResult, match="ran is false"):
        records.validate_result({**good, "ran": False})
    with pytest.raises(InvalidResult, match="null only with outcome_unknown"):
        records.validate_result({**good, "ran": None})
    records.validate_result(
        {**good, "state": "outcome_unknown", "ran": None, "task_succeeded": None}
    )
    with pytest.raises(InvalidResult, match="list of strings"):
        records.validate_result({**good, "changed_files": "a.py"})
