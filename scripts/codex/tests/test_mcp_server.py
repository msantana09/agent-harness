"""codex-jobs MCP server against the fake Codex: handshakes, tool-to-command mapping and tasks."""

import io
import json
import os
import signal
import threading
import time

import pytest
from codex_jobs import appserver, mcp_server, records, runner

ANSWER = json.dumps({"status": "completed", "answer": "the answer", "open_questions": []})
EFFORTS = ["low", "medium", "high", "xhigh"]
CATALOG = {
    "fetched_at": "2026-11-08T00:00:00Z",
    "models": [
        {"slug": slug, "supported_reasoning_levels": [{"effort": e} for e in EFFORTS]}
        for slug in ("slug-sol", "slug-astra", "slug-terra", "gpt-6-luna")
    ],
}
MODELS_SH = (
    "CODEX_MODEL_SOL=slug-sol\nCODEX_MODEL_ASTRA=slug-astra\n"
    "CODEX_MODEL_TERRA=slug-terra\nCODEX_MODEL_LUNA=gpt-6-luna\n"
)
TASKS_META = {
    "_meta": {mcp_server.META_CAPABILITIES: {"extensions": {mcp_server.TASKS_EXTENSION: {}}}}
}
SUBAGENT_META = {"_meta": {mcp_server.META_CAPABILITIES: {"roots": {"listChanged": True}}}}
EXPECTED_TOOLS = {
    "codex_capabilities", "codex_review", "codex_consult", "codex_run", "codex_status",
    "codex_result", "codex_wait", "codex_cancel", "codex_resume", "codex_fork", "codex_steer",
    "codex_interrupt",
}  # fmt: skip


class McpEnv:
    def __init__(self, tmp_path, fake, server):
        self.fake = fake
        self.server = server
        self.root = tmp_path / "jobs"
        self.project = tmp_path / "project"
        self.project.mkdir()
        self._next_id = 0

    def rpc(self, method, params=None):
        self._next_id += 1
        response = self.server.dispatch(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        )
        assert response["id"] == self._next_id
        return response

    def call(self, name, arguments=None, *, meta=None):
        response = self.rpc(
            "tools/call", {"name": name, "arguments": arguments or {}, **(meta or {})}
        )
        return response["result"]

    @staticmethod
    def text(result):
        return result["content"][0]["text"]

    def consult(self, *, meta=None, **arguments):
        arguments = {"prompt": "Review the thing.", "project_dir": str(self.project), **arguments}
        return self.call("codex_consult", arguments, meta=meta)

    def submit(self, **arguments):
        result = self.consult(**arguments)
        assert result["isError"] is False, result
        return json.loads(self.text(result))["job_id"]

    def wait_for_turn(self, job_id, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = records.load_job(self.root, job_id)
            if job["turn_id"]:
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} never dispatched a turn")


@pytest.fixture
def env(tmp_path, fake_codex, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps(CATALOG))
    (tmp_path / "models.sh").write_text(MODELS_SH)
    monkeypatch.setenv("CODEX_JOBS_BIN", str(fake_codex.bin))
    server = mcp_server.Server(
        root=tmp_path / "jobs",
        catalog_path=tmp_path / "models_cache.json",
        models_sh_path=tmp_path / "models.sh",
        sessions_dir=tmp_path / "sessions",
    )
    mcp_env = McpEnv(tmp_path, fake_codex, server)
    with server:
        yield mcp_env
    for job in records.list_jobs(mcp_env.root):
        if job["runner_pid"]:
            try:
                os.kill(job["runner_pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        if job["pgid"] and job["pgid"] > 1:
            appserver.stop_app_server(job["pgid"], grace=1.0)


def test_discover_handshake_advertises_the_tasks_extension(env):
    result = env.rpc("server/discover", TASKS_META)["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert result["capabilities"]["extensions"] == {"io.modelcontextprotocol/tasks": {}}


def test_classic_initialize_echoes_the_version_and_mirrors_the_declared_extension(env):
    plain = env.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert plain["protocolVersion"] == "2025-06-18" and plain["capabilities"] == {"tools": {}}
    declared = env.rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"extensions": {mcp_server.TASKS_EXTENSION: {}}},
        },
    )["result"]
    assert mcp_server.TASKS_EXTENSION in declared["capabilities"]["extensions"]


def test_tools_list_names_and_no_task_support_flag(env):
    tools = env.rpc("tools/list")["result"]["tools"]
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS
    assert all("execution" not in tool for tool in tools)


def test_consult_status_result_and_wait_map_to_the_job(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    job_id = env.submit(model="luna", effort="low")
    waited = env.call("codex_wait", {"job_id": job_id, "max_seconds": 30})
    envelope = json.loads(env.text(waited))
    assert (
        waited["isError"] is False
        and envelope["job_id"] == job_id
        and envelope["state"] == "succeeded"
    )
    assert json.loads(env.text(env.call("codex_result", {"job_id": job_id}))) == envelope
    assert json.loads(env.text(env.call("codex_status", {"job_id": job_id}))) == records.load_job(
        env.root, job_id
    )
    (row,) = json.loads(env.text(env.call("codex_status")))
    assert (row["job_id"], row["kind"], row["state"]) == (job_id, "consult", "succeeded")
    (start,) = [m for m in env.fake.received() if m.get("method") == "turn/start"]
    assert start["params"]["effort"] == "low"


def test_wait_on_a_blocked_task_is_an_error_result_carrying_the_envelope(env):
    blocked = json.dumps({"status": "blocked", "answer": "no access", "open_questions": []})
    env.fake.set_script({"turn": {"final_answer": blocked}})
    job_id = env.submit(model="luna", effort="low")
    waited = env.call("codex_wait", {"job_id": job_id, "max_seconds": 30})
    envelope = json.loads(env.text(waited))
    assert (envelope["state"], envelope["task_succeeded"]) == ("succeeded", False)
    assert waited["isError"] is True


def test_prompt_file_idempotency_key_and_images_reach_the_command(env, tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("From a file.")
    (env.project / "shot.png").write_bytes(b"\x89PNG")
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    args = {"prompt_file": str(prompt), "project_dir": str(env.project), "idempotency_key": "k1"}
    first = json.loads(env.text(env.call("codex_consult", {**args, "images": ["shot.png"]})))[
        "job_id"
    ]
    again = json.loads(env.text(env.call("codex_consult", args)))["job_id"]
    assert again == first
    job = records.load_job(env.root, first)
    assert (env.root / first / "prompt.txt").read_text() == "From a file."
    assert job["images"] == [str(env.project / "shot.png")]


def test_cancel_steer_and_interrupt_map_to_their_commands(env):
    env.fake.set_script({"turn": {"hang": True}})
    steered = env.submit()
    job = env.wait_for_turn(steered)
    result = env.call("codex_steer", {"job_id": steered, "text": "focus on the tests"})
    assert result["isError"] is False and env.text(result).startswith(f"STATE: {steered} ")
    (steer,) = [m for m in env.fake.received() if m.get("method") == "turn/steer"]
    assert steer["params"]["expectedTurnId"] == job["turn_id"]
    assert steer["params"]["input"] == [{"type": "text", "text": "focus on the tests"}]

    assert env.call("codex_interrupt", {"job_id": steered})["isError"] is False
    assert (
        json.loads(env.text(env.call("codex_wait", {"job_id": steered, "max_seconds": 30})))[
            "state"
        ]
        == "interrupted"
    )

    cancelled = env.submit()
    env.wait_for_turn(cancelled)
    result = env.call("codex_cancel", {"job_id": cancelled})
    assert (result["isError"], env.text(result)) == (False, f"STATE: {cancelled} cancelled")


def test_dash_prefixed_steer_text_reaches_the_turn_as_text(env):
    env.fake.set_script({"turn": {"hang": True}})
    job_id = env.submit()
    env.wait_for_turn(job_id)
    for text in ("--help", "--fix"):
        result = env.call("codex_steer", {"job_id": job_id, "text": text})
        assert result["isError"] is False and env.text(result).startswith(f"STATE: {job_id} "), (
            result
        )
    steers = [m["params"]["input"] for m in env.fake.received() if m.get("method") == "turn/steer"]
    assert steers == [[{"type": "text", "text": "--help"}], [{"type": "text", "text": "--fix"}]]


def test_dash_prefixed_idempotency_key_reaches_the_record(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    job_id = env.submit(idempotency_key="--k1")
    assert records.load_job(env.root, job_id)["idempotency_key"] == "--k1"
    assert env.submit(idempotency_key="--k1") == job_id


def test_a_finished_runner_is_reaped_by_the_next_tool_call(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    job_id = env.submit()
    env.call("codex_wait", {"job_id": job_id, "max_seconds": 30})
    pid = records.load_job(env.root, job_id)["runner_pid"]
    exited = os.WEXITED | os.WNOHANG | os.WNOWAIT
    deadline = time.monotonic() + 15
    # WNOWAIT leaves the exited child for the server to reap
    while os.waitid(os.P_PID, pid, exited) is None:
        assert time.monotonic() < deadline, "the runner never exited"
        time.sleep(0.05)
    env.call("codex_status", {"job_id": job_id})
    assert pid not in runner._spawned
    with pytest.raises(ChildProcessError):
        os.waitid(os.P_PID, pid, exited)


def test_resume_and_fork_start_jobs_from_a_source(env):
    env.fake.set_script(
        {
            "turn": {"final_answer": ANSWER},
            "responses": {
                "thread/resume": {"thread": {"id": "thr-fake-1"}},
                "thread/fork": {"thread": {"id": "thr-fake-2"}},
            },
        }
    )
    first = env.submit()
    env.call("codex_wait", {"job_id": first, "max_seconds": 30})
    thread_id = records.load_job(env.root, first)["thread_id"]
    resumed = json.loads(
        env.text(
            env.call(
                "codex_resume",
                {"source": first, "prompt": "go on", "project_dir": str(env.project)},
            )
        )
    )["job_id"]
    forked = json.loads(
        env.text(
            env.call(
                "codex_fork",
                {"source": thread_id, "prompt": "branch", "project_dir": str(env.project)},
            )
        )
    )["job_id"]
    assert records.load_job(env.root, resumed)["source_mode"] == "resume"
    assert records.load_job(env.root, resumed)["parent_job_id"] == first
    assert (
        records.load_job(env.root, forked)["source_mode"],
        records.load_job(env.root, forked)["source_thread_id"],
    ) == ("fork", thread_id)


def test_capabilities_tool_returns_the_command_json(env):
    caps = json.loads(env.text(env.call("codex_capabilities")))
    assert caps["tiers"]["luna"] == "gpt-6-luna" and "mcp" in caps["subcommands"]


def test_review_submits_a_read_only_review_job(env):
    result = env.call("codex_review", {"prompt": "x", "project_dir": str(env.project)})
    assert result["isError"] is False
    job = records.load_job(env.root, json.loads(env.text(result))["job_id"])
    assert (job["kind"], job["sandbox"]) == ("review", "read-only")


@pytest.mark.parametrize(
    "placement, flags",
    [
        ({"branch": "codex/x", "allow_commit": True}, ["--branch=codex/x", "--allow-commit"]),
        ({"worktree": "/w/tree"}, ["--worktree=/w/tree"]),
    ],
)
def test_run_passes_its_worktree_flags_to_the_command(env, monkeypatch, placement, flags):
    seen = []
    monkeypatch.setattr(
        env.server, "_run_cli", lambda argv: (seen.append(argv), (2, ["FAILURE: stopped here"]))[1]
    )
    result = env.call(
        "codex_run",
        {
            "prompt": "x",
            "project_dir": str(env.project),
            "model": "luna",
            "effort": "low",
            **placement,
        },
    )
    assert (result["isError"], env.text(result)) == (True, "FAILURE: stopped here")
    (argv,) = seen
    operands = argv.index("--")
    assert argv[0] == "run" and argv[operands - len(flags) : operands] == flags
    assert "--allow-commit" not in argv or placement.get("allow_commit")


@pytest.mark.parametrize(
    "placement, flags",
    [
        ({"branch": "codex/x"}, ["--branch=codex/x"]),
        ({"worktree": "/w/tree"}, ["--worktree=/w/tree"]),
    ],
)
def test_fork_passes_its_worktree_flags_to_the_command(env, monkeypatch, placement, flags):
    seen = []
    monkeypatch.setattr(
        env.server, "_run_cli", lambda argv: (seen.append(argv), (2, ["FAILURE: stopped here"]))[1]
    )
    result = env.call(
        "codex_fork",
        {"source": "thr-1", "prompt": "x", "project_dir": str(env.project), **placement},
    )
    assert (result["isError"], env.text(result)) == (True, "FAILURE: stopped here")
    (argv,) = seen
    operands = argv.index("--")
    assert (
        argv[0] == "fork"
        and argv[operands - len(flags) : operands] == flags
        and argv[operands + 1] == "thr-1"
    )


def test_fork_and_resume_take_only_the_flags_that_apply(env):
    for name, flag in (("codex_resume", "branch"), ("codex_fork", "allow_commit")):
        result = env.call(
            name, {"source": "thr-1", "prompt": "x", flag: "codex/x" if flag == "branch" else True}
        )
        assert result["isError"] is True and flag in env.text(result)


def test_wait_with_the_tasks_capability_returns_a_task_then_tasks_get_returns_the_envelope(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    job_id = env.submit()
    task = env.call("codex_wait", {"job_id": job_id}, meta=TASKS_META)
    assert task["resultType"] == "task" and task["taskId"] == job_id
    assert {"status", "createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs"} <= set(task)
    deadline = time.monotonic() + 30
    while True:
        view = env.rpc("tasks/get", {"taskId": job_id, **TASKS_META})["result"]
        if view["status"] != "working" or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert view["status"] == "completed" and view["result"]["isError"] is False
    assert json.loads(env.text(view["result"]))["state"] == "succeeded"
    # the measured completed-task result is {content, isError}
    assert "resultType" not in view["result"]
    # a task for a job that already ended carries its result
    again = env.call("codex_wait", {"job_id": job_id}, meta=TASKS_META)
    assert (
        again["resultType"] == "task"
        and again["status"] == "completed"
        and again["result"] == view["result"]
    )


def test_wait_without_the_tasks_capability_returns_the_envelope_with_no_task(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    job_id = env.submit()
    for meta in (None, SUBAGENT_META):
        result = env.call("codex_wait", {"job_id": job_id, "max_seconds": 30}, meta=meta)
        assert "taskId" not in result and result["resultType"] == "complete"
        assert json.loads(env.text(result))["state"] == "succeeded"


def test_wait_on_a_running_job_returns_the_running_state_after_max_seconds(env):
    env.fake.set_script({"turn": {"hang": True}})
    job_id = env.submit()
    result = env.call("codex_wait", {"job_id": job_id, "max_seconds": 1})
    body = json.loads(env.text(result))
    assert "taskId" not in result and result["isError"] is False
    assert body["job_id"] == job_id and body["terminal"] is False


def test_request_capabilities_beat_the_ones_initialize_declared(env):
    env.rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"extensions": {mcp_server.TASKS_EXTENSION: {}}},
        },
    )
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    job_id = env.submit()
    assert env.call("codex_wait", {"job_id": job_id}, meta=None)["resultType"] == "task"
    assert (
        env.call("codex_wait", {"job_id": job_id, "max_seconds": 30}, meta=SUBAGENT_META)[
            "resultType"
        ]
        == "complete"
    )


def test_submit_with_wait_true_follows_the_job(env):
    env.fake.set_script({"turn": {"final_answer": ANSWER}})
    sync = env.consult(wait=True)
    assert "taskId" not in sync and json.loads(env.text(sync))["state"] == "succeeded"
    task = env.consult(wait=True, idempotency_key="again", meta=TASKS_META)
    assert task["resultType"] == "task" and records.JOB_ID_RE.match(task["taskId"])


def test_bad_arguments_and_unknown_names_are_reported(env):
    # no prompt
    assert env.call("codex_consult", {"project_dir": str(env.project)})["isError"] is True
    assert env.call("codex_consult", {"prompt": "x", "prompt_file": "y"})["isError"] is True
    assert env.call("codex_wait", {"job_id": "../etc"})["isError"] is True
    assert env.call("codex_consult", {"prompt": "x", "bogus": 1})["isError"] is True
    assert env.rpc("tools/call", {"name": "nope"})["error"]["code"] == mcp_server.ERR_INVALID_PARAMS
    assert env.rpc("nope/method")["error"]["code"] == mcp_server.ERR_METHOD_NOT_FOUND
    assert (
        env.rpc("tasks/get", {"taskId": "cj-20990101-000000-abcd"})["error"]["code"]
        == mcp_server.ERR_INVALID_PARAMS
    )


def test_stdio_loop_frames_lines_and_serves_requests_while_a_wait_blocks(env):
    env.fake.set_script({"turn": {"hang": True}})
    job_id = env.submit()
    stdin_read, stdin_write = os.pipe()
    stdout_buffer = io.StringIO()
    out_lock = threading.Lock()

    class Out:
        def write(self, text):
            with out_lock:
                stdout_buffer.write(text)

        def flush(self):
            pass

    # A daemon thread: a failed assertion before stdin closes must not leave the interpreter
    # waiting on a loop that blocks reading it.
    loop = threading.Thread(
        target=mcp_server.serve_stdio, args=(env.server, os.fdopen(stdin_read), Out()), daemon=True
    )
    loop.start()

    def send(text):
        os.write(stdin_write, (text + "\n").encode())

    def replies():
        with out_lock:
            return [json.loads(line) for line in stdout_buffer.getvalue().splitlines()]

    def wait_for(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            found = [r for r in replies() if predicate(r)]
            if found:
                return found[0]
            time.sleep(0.02)
        raise AssertionError(replies())

    send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "codex_wait", "arguments": {"job_id": job_id, "max_seconds": 3}},
            }
        )
    )
    send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    send("{not json")
    send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}))
    assert wait_for(lambda r: r.get("id") == 2)["result"] == {"resultType": "complete"}
    assert wait_for(lambda r: r.get("id") is None)["error"]["code"] == mcp_server.ERR_PARSE
    assert not [r for r in replies() if r.get("id") == 1]  # the wait is still blocked
    assert (
        json.loads(wait_for(lambda r: r.get("id") == 1)["result"]["content"][0]["text"])["terminal"]
        is False
    )
    os.close(stdin_write)
    loop.join(10)
    assert not loop.is_alive() and len(replies()) == 3  # the notification got no reply
