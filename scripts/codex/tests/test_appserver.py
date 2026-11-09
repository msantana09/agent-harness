"""Tests for codex_jobs.appserver: WebSocket framing, the JSON-RPC client and the launcher.

Everything runs against tests/fake_codex.py, which serves its own framing, so a framing bug in
the client is caught by interop and not mirrored. The real `gh` is never called.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_jobs import appserver
from codex_jobs.appserver import (
    AppServerClient,
    AppServerStartError,
    ConnectionClosed,
    HandshakeError,
    MessageTooLarge,
    RpcError,
    WebSocketConnection,
    stop_app_server,
)

THREAD_START = {
    "cwd": "/work",
    "model": "gpt-6-luna",
    "sandbox": "read-only",
    "approvalPolicy": "on-request",
    "approvalsReviewer": "user",
}
APPROVAL = {
    "method": "item/commandExecution/requestApproval",
    "params": {"command": "touch x", "cwd": "/work"},
}


def start_turn(client: AppServerClient) -> tuple[str, str]:
    """initialize + thread/start + turn/start with the real v2 keys; returns (thread id, turn
    id)."""
    client.initialize()
    thread_id = client.call("thread/start", THREAD_START)["thread"]["id"]
    turn = client.call(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "go"}],
            "effort": "low",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": True},
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
        },
    )["turn"]
    return thread_id, turn["id"]


def wait_for_notification(client: AppServerClient, method: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        note = client.next_notification(timeout=0.2)
        if note is not None and note["method"] == method:
            return note
    raise AssertionError(f"no {method} notification within {timeout}s")


def wait_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def test_handshake_and_initialize_round_trip(fake_codex):
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        result = client.initialize()
    assert result["platformFamily"] == "unix"
    fake_codex.wait_for(lambda r: r["kind"] == "recv" and r["msg"].get("method") == "initialized")
    sent = fake_codex.received()
    assert [m["method"] for m in sent] == ["initialize", "initialized"]
    assert all("jsonrpc" not in m for m in sent)
    assert sent[0]["params"] == {
        "clientInfo": {"name": "codex-jobs", "version": "0.1.0"},
        "capabilities": {"experimentalApi": True},
    }


@pytest.mark.parametrize("handshake", ["bad_accept", "refuse"])
def test_handshake_without_valid_101_is_refused(fake_codex, handshake):
    fake_codex.set_script({"handshake": handshake})
    server = fake_codex.launch()
    with pytest.raises(HandshakeError):
        WebSocketConnection.connect(server.socket_path)


@pytest.mark.parametrize("size", [200, 40000], ids=["16-bit length", "64-bit length"])
def test_message_over_125_and_65535_bytes_round_trips(fake_codex, size):
    server = fake_codex.launch()
    params = {"blob": "é" * size}  # 2 bytes each: 400 and 80000 bytes
    with AppServerClient(server.socket_path) as client:
        assert client.call("fake/echo", params) == params


@pytest.mark.parametrize("pings", [False, True], ids=["plain", "pings between fragments"])
def test_fragmented_server_message_is_reassembled(fake_codex, pings):
    fake_codex.set_script({"wire": {"fragment_parts": 3, "ping_before_each_message": pings}})
    server = fake_codex.launch()
    params = {"blob": "é" * 100}  # byte-split fragments land mid-character
    with AppServerClient(server.socket_path) as client:
        assert client.call("fake/echo", params) == params


def test_ping_is_answered_with_pong(fake_codex):
    fake_codex.set_script({"wire": {"ping_before_each_message": True}})
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        client.initialize()
    pong = fake_codex.wait_for(lambda r: r["kind"] == "pong")
    assert pong["payload"] == "hb"


def test_client_frames_are_masked(fake_codex):
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        client.initialize()
    assert fake_codex.records("violation") == []


def test_close_frame_raises_connection_closed(fake_codex):
    server = fake_codex.launch()
    conn = WebSocketConnection.connect(server.socket_path)
    conn.send_text(
        '{"id": 1, "method": "fake/close", "params": {"code": 1001, "reason": "going away"}}'
    )
    with pytest.raises(ConnectionClosed) as excinfo:
        conn.recv_text()
    assert (excinfo.value.code, excinfo.value.reason) == (1001, "going away")


def test_message_above_the_bound_is_refused(fake_codex):
    fake_codex.set_script({"responses": {"big/reply": {"blob": "x" * 5000}}})
    server = fake_codex.launch()
    conn = WebSocketConnection.connect(server.socket_path, max_message_bytes=1000)
    conn.send_text('{"id": 1, "method": "big/reply", "params": {}}')
    with pytest.raises(MessageTooLarge):
        conn.recv_text()


def test_unknown_method_raises_rpc_error_with_code_and_message(fake_codex):
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        client.initialize()
        with pytest.raises(RpcError) as excinfo:
            client.call("no/such/method")
    assert (excinfo.value.code, excinfo.value.message) == (-32601, "Method not found")


def test_call_times_out_and_the_client_stays_usable(fake_codex):
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        with pytest.raises(TimeoutError):
            client.call("fake/never", timeout=0.3)
        assert client.call("fake/echo", {"n": 1}) == {"n": 1}


def test_concurrent_calls_each_get_their_own_response(fake_codex):
    server = fake_codex.launch()
    answers: dict[int, object] = {}
    with AppServerClient(server.socket_path) as client:

        def worker(n: int) -> None:
            for i in range(10):
                answers[n * 100 + i] = client.call("fake/echo", {"n": n * 100 + i})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert answers == {k: {"n": k} for k in answers} and len(answers) == 80


def test_rate_limits_reply_is_surfaced_unchanged(fake_codex):
    # snake_case is the wire canary: the client must not rename or add keys
    for key in ("usedPercent", "used_percent"):
        reply = {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "primary": {key: 20, "windowDurationMins": 10080, "resetsAt": 1794410566}
            },
        }
        fake_codex.set_script({"responses": {"account/rateLimits/read": reply}})
        server = fake_codex.launch(name=f"job-{key}")
        with AppServerClient(server.socket_path) as client:
            client.initialize()
            result = client.call("account/rateLimits/read")
        assert result == reply
        assert set(result["rateLimits"]["primary"]) == {key, "windowDurationMins", "resetsAt"}


def test_connection_loss_wakes_pending_call_and_ends_notifications(fake_codex):
    server = fake_codex.launch()
    client = AppServerClient(server.socket_path)
    client.initialize()
    with pytest.raises(ConnectionClosed):
        client.call("fake/exit", timeout=10)
    assert client.closed
    with pytest.raises(ConnectionClosed):
        client.next_notification(timeout=1)
    assert list(client.iter_notifications()) == []
    with pytest.raises(ConnectionClosed):
        client.call("thread/start", THREAD_START)
    client.close()


def test_scripted_turn_streams_started_final_answer_and_completed(fake_codex):
    fake_codex.set_script({"turn": {"final_answer": "all good", "status": "completed"}})
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        thread_id, turn_id = start_turn(client)
        started = wait_for_notification(client, "turn/started")
        item = wait_for_notification(client, "item/completed")
        completed = wait_for_notification(client, "turn/completed")
    assert started["params"]["turn"]["id"] == turn_id
    assert (
        item["params"]["item"]["phase"] == "final_answer"
        and item["params"]["item"]["text"] == "all good"
    )
    assert item["params"]["threadId"] == thread_id
    assert completed["params"]["turn"]["status"] == "completed"
    turn_start = [m for m in fake_codex.received() if m.get("method") == "turn/start"][0]
    assert set(turn_start["params"]) == {
        "threadId",
        "input",
        "effort",
        "sandboxPolicy",
        "approvalPolicy",
        "approvalsReviewer",
    }


def test_second_client_steers_and_interrupts_while_first_sees_interrupted(fake_codex):
    fake_codex.set_script({"turn": {"hang": True}})
    server = fake_codex.launch()
    with (
        AppServerClient(server.socket_path) as first,
        AppServerClient(server.socket_path) as second,
    ):
        thread_id, turn_id = start_turn(first)
        second.initialize()
        steer = second.call(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": "stop at 10"}],
            },
        )
        interrupt = second.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        completed = wait_for_notification(first, "turn/completed")
        idle = wait_for_notification(second, "thread/status/changed")
        assert second.next_notification(timeout=0.3) is None
    assert steer == {"turnId": turn_id}
    assert interrupt == {}
    assert completed["params"]["turn"]["status"] == "interrupted"
    assert idle["params"]["status"] == {"type": "idle"}
    assert {m["method"] for m in fake_codex.received(client=2)} >= {"turn/steer", "turn/interrupt"}


@pytest.mark.parametrize("answers", [True, False], ids=["handler declines", "no handler"])
def test_server_request_reaches_handler_and_its_answer_goes_back(fake_codex, answers):
    fake_codex.set_script({"turn": {"approvals": [APPROVAL], "final_answer": "done"}})
    server = fake_codex.launch()
    seen: list[tuple[str, dict]] = []

    def decline(method: str, params: dict) -> dict:
        seen.append((method, params))
        return {"decision": "decline"}

    with AppServerClient(
        server.socket_path, request_handler=decline if answers else None
    ) as client:
        thread_id, turn_id = start_turn(client)
        wait_for_notification(client, "turn/completed")
    answer = fake_codex.wait_for(
        lambda r: r["kind"] == "recv" and "method" not in r["msg"] and r["msg"].get("id", 0) >= 1000
    )
    if answers:
        assert seen[0][0] == "item/commandExecution/requestApproval"
        assert seen[0][1]["command"] == "touch x" and seen[0][1]["threadId"] == thread_id
        assert answer["msg"] == {"id": answer["msg"]["id"], "result": {"decision": "decline"}}
    else:
        assert answer["msg"]["error"]["code"] == -32601


def test_turn_finishes_after_its_client_dies_and_a_new_client_reads_it(fake_codex):
    approval_unanswered = {
        "approvals": [APPROVAL],
        "approval_timeout": 0.3,
        "final_answer": "finished alone",
    }
    fake_codex.set_script({"turn": approval_unanswered})
    server = fake_codex.launch()
    first = AppServerClient(server.socket_path)
    thread_id, turn_id = start_turn(first)
    first.close()
    with AppServerClient(server.socket_path) as second:
        second.initialize()
        deadline = time.monotonic() + 5
        while True:
            turns = second.call("thread/read", {"threadId": thread_id, "includeTurns": True})[
                "thread"
            ]["turns"]
            if (turns and turns[0]["status"] != "inProgress") or time.monotonic() > deadline:
                break
            time.sleep(0.05)
    assert [t["id"] for t in turns] == [turn_id]
    assert turns[0]["status"] == "completed"
    assert turns[0]["items"][0]["text"] == "finished alone"


def test_thread_read_with_turns_sends_a_deprecation_notice_like_the_real_server(fake_codex):
    fake_codex.set_script({"turn": {"final_answer": "done"}})
    server = fake_codex.launch()
    with AppServerClient(server.socket_path) as client:
        thread_id, _ = start_turn(client)
        wait_for_notification(client, "thread/status/changed")
        client.call("thread/read", {"threadId": thread_id})
        assert client.next_notification(timeout=0.3) is None
        client.call("thread/read", {"threadId": thread_id, "includeTurns": True})
        assert (
            "Full-history hydration is deprecated"
            in wait_for_notification(client, "deprecationNotice")["params"]["summary"]
        )


def test_fake_exits_abruptly_at_a_named_point(fake_codex):
    fake_codex.set_script({"turn": {"exit_at": "after_turn_started", "final_answer": "never sent"}})
    server = fake_codex.launch()
    client = AppServerClient(server.socket_path)
    start_turn(client)
    assert [n["method"] for n in client.iter_notifications()] == [
        "thread/settings/updated",
        "turn/started",
    ]
    assert isinstance(client.close_reason, ConnectionClosed)
    client.close()
    deadline = time.monotonic() + 3
    while True:  # the socket file stays behind with nobody listening
        try:
            WebSocketConnection.connect(server.socket_path, timeout=1).close()
        except ConnectionRefusedError:
            break
        assert time.monotonic() < deadline, "connect still succeeds after the fake exited"
        time.sleep(0.05)


def test_launcher_runs_server_in_its_own_session_and_logs(fake_codex):
    server = fake_codex.launch(extra_config=["model_reasoning_summary=none"])
    assert server.pgid == server.pid != os.getpgrp()
    start = fake_codex.records("start")[0]
    assert start["pid"] == server.pid and start["pgid"] == server.pgid
    assert start["argv"] == [
        "app-server",
        "--listen",
        f"unix://{server.socket_path}",
        "-c",
        "allow_login_shell=false",
        "-c",
        "model_reasoning_summary=none",
    ]
    assert "listening" in (server.socket_path.parent / "app-server.log").read_text()


def test_build_app_server_env_is_minimal_and_keeps_the_callers_path(monkeypatch):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "HOME": "/home/u",
            "PATH": "/usr/bin",
            "USER": "u",
            "LANG": "C",
            "SECRET_SENTINEL_TOKEN": "x",
            "GH_TOKEN": "caller",
        },
    )
    env = appserver.build_app_server_env(None)
    assert env == {
        "HOME": "/home/u",
        "PATH": "/usr/bin",
        "USER": "u",
        "LANG": "C",
    }
    assert appserver.build_app_server_env("tok")["GH_TOKEN"] == "tok"


def test_server_sees_none_of_the_callers_other_variables(fake_codex, monkeypatch):
    monkeypatch.setenv("SECRET_SENTINEL_TOKEN", "sentinel-value")
    fake_codex.launch()
    start = fake_codex.records("start")[0]
    assert "SECRET_SENTINEL_TOKEN" not in start["env"]
    assert start["env"]["PATH"] == os.environ["PATH"]
    assert "GH_TOKEN" not in start["env"]
    assert "shell_environment_policy.ignore_default_excludes=true" not in start["argv"]


def test_gh_token_and_ignore_default_excludes_flag_only_when_resolved(fake_codex, tmp_path):
    resolved_for: list[Path] = []

    def resolver(repo_dir: Path) -> str:
        resolved_for.append(repo_dir)
        return "fake-token-123"

    fake_codex.launch(token_resolver=resolver)
    start = fake_codex.records("start")[0]
    assert resolved_for == [tmp_path]
    assert start["env"]["GH_TOKEN"] == "fake-token-123"
    assert start["argv"][5:7] == ["-c", "shell_environment_policy.ignore_default_excludes=true"]


def test_stop_app_server_ends_the_whole_process_group(fake_codex):
    fake_codex.set_script({"spawn_child": True})
    server = fake_codex.launch()
    child = fake_codex.wait_for(lambda r: r["kind"] == "child")["pid"]
    assert stop_app_server(server.pgid, grace=3.0) is True
    assert wait_gone(server.pid) and wait_gone(child)
    assert stop_app_server(server.pgid, grace=3.0) is True  # already gone


def test_stop_app_server_escalates_to_sigkill_after_the_grace(fake_codex):
    fake_codex.set_script({"ignore_sigterm": True})
    server = fake_codex.launch()
    started = time.monotonic()
    assert stop_app_server(server.pgid, grace=0.4) is True
    assert time.monotonic() - started >= 0.4
    assert wait_gone(server.pid)


def test_stop_app_server_refuses_the_callers_own_group():
    with pytest.raises(ValueError):
        stop_app_server(os.getpgrp())
    with pytest.raises(ValueError):
        stop_app_server(0)


def test_start_error_carries_log_tail_when_server_exits(fake_codex):
    fake_codex.set_script({"startup_fail": "boom: cannot bind"})
    with pytest.raises(AppServerStartError, match=r"exited with status 3(.|\n)*boom: cannot bind"):
        fake_codex.launch(startup_timeout=10)


def test_start_timeout_stops_the_server_and_carries_the_log(fake_codex):
    fake_codex.set_script({"startup_delay": 30})
    with pytest.raises(AppServerStartError, match="not accepting connections"):
        fake_codex.launch(startup_timeout=0.5)
    pid = fake_codex.records("start")[0]["pid"]
    assert wait_gone(pid)


def make_gh(tmp_path: Path, accounts: tuple[str, ...]) -> Path:
    """A fake `gh` in its own folder that prints `tok-<account>` for the accounts it knows."""
    gh = tmp_path / "gh-bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "auth token -u" ]; then\n'
        f"  for a in {' '.join(accounts)}; do "
        f'[ "$a" = "$4" ] && {{ echo "tok-$a"; exit 0; }}; done\n'
        '  echo "no account $4" >&2; exit 1\n'
        "fi\n"
        "exit 2\n"
    )
    gh.chmod(0o755)
    return gh


def make_repo(tmp_path: Path, origin: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    return repo


def gh_on_path(monkeypatch: pytest.MonkeyPatch, gh: Path) -> None:
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")


def make_token_command(tmp_path: Path, body: str) -> Path:
    command = tmp_path / "token-command"
    command.write_text(f"#!/bin/sh\n{body}\n")
    command.chmod(0o755)
    return command


def test_resolve_gh_token_uses_the_lowercased_origin_owner_account(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "git@github.com:Acme-Corp/tool.git")
    gh_on_path(monkeypatch, make_gh(tmp_path, ("acme-corp",)))
    assert appserver.resolve_gh_token(repo) == "tok-acme-corp"


def test_resolve_gh_token_is_none_for_an_owner_without_an_account(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "https://github.com/someone-else/tool.git")
    gh_on_path(monkeypatch, make_gh(tmp_path, ("acme-corp",)))
    assert appserver.resolve_gh_token(repo) is None


def test_resolve_gh_token_is_none_when_gh_is_not_on_path(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, "git@github.com:acme-corp/tool.git")
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    assert appserver.resolve_gh_token(repo) is None


def test_the_token_command_gets_the_repo_and_its_output_is_the_token(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    command = make_token_command(tmp_path, f'echo "$1" > {seen}; echo tok-from-command')
    monkeypatch.setenv(appserver.TOKEN_COMMAND_ENV, str(command))
    gh_on_path(monkeypatch, make_gh(tmp_path, ("acme-corp",)))
    repo = make_repo(tmp_path, "git@github.com:acme-corp/tool.git")
    assert appserver.resolve_gh_token(repo) == "tok-from-command"
    assert seen.read_text() == f"{repo}\n"


def test_empty_output_from_the_token_command_means_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv(appserver.TOKEN_COMMAND_ENV, str(make_token_command(tmp_path, "exit 0")))
    gh_on_path(monkeypatch, make_gh(tmp_path, ("acme-corp",)))
    repo = make_repo(tmp_path, "git@github.com:acme-corp/tool.git")
    assert appserver.resolve_gh_token(repo) is None


def test_a_token_command_that_exits_nonzero_refuses_with_its_stderr(tmp_path, monkeypatch):
    command = make_token_command(tmp_path, 'echo "no account for this owner" >&2; exit 3')
    monkeypatch.setenv(appserver.TOKEN_COMMAND_ENV, str(command))
    repo = make_repo(tmp_path, "git@github.com:acme-corp/tool.git")
    with pytest.raises(AppServerStartError, match="exit 3.*no account for this owner"):
        appserver.resolve_gh_token(repo)


def test_a_refusing_token_command_stops_the_job_before_the_server_starts(
    fake_codex, tmp_path, monkeypatch
):
    command = make_token_command(tmp_path, 'echo "refused" >&2; exit 1')
    monkeypatch.setenv(appserver.TOKEN_COMMAND_ENV, str(command))
    with pytest.raises(AppServerStartError, match="refused"):
        fake_codex.launch(token_resolver=appserver.resolve_gh_token)
    assert fake_codex.records("start") == []
