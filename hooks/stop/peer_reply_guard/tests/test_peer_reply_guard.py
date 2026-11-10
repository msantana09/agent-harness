"""Tests for the peer_reply_guard Stop hook: the decision on a message list, and the hook run as
settings.json launches it, on the invented transcripts in fixtures/ beside this file."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HOOK = Path(__file__).resolve().parents[1] / "main.py"
FIXTURES = Path(__file__).parent / "fixtures"

SENDER = "relay:session_0example0sender"
TASK = (
    "Another Claude session sent a message:\n"
    f'<cross-session-message from="{SENDER}" from-name="Build queue" from-mode="prompting">\n'
    "Count the failing tests in the nightly run.\n"
    "done-signal: a one-line count.\n"
    "reply-to: worker-a\n"
    "</cross-session-message>"
)
FYI = (
    "Another Claude session sent a message:\n"
    f'<cross-session-message from="{SENDER}" from-name="Build queue" from-mode="prompting">\n'
    "Done: suite green.\nSha: abc\n</cross-session-message>"
)

type Message = dict[str, Any]


def load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("peer_reply_guard_main", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_hook()


def user(text: str) -> Message:
    return {"role": "user", "content": text}


def tool_result() -> Message:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
    }


def assistant(*tools: str, text: str = "Seven tests fail.") -> Message:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content += [
        {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}
        for i, name in enumerate(tools)
    ]
    return {"role": "assistant", "content": content}


def run_hook(payload: str) -> tuple[int, dict[str, Any]]:
    """Run main.py with the payload on stdin; return its exit code and the JSON it printed."""
    done = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, check=False
    )
    return done.returncode, json.loads(done.stdout)


def payload_for(transcript: Path, **extra: Any) -> str:
    return json.dumps({"transcript_path": str(transcript), **extra})


def test_task_answered_in_chat_only_is_pending() -> None:
    messages = [user(TASK), assistant("Bash"), tool_result(), assistant()]
    assert hook.pending_peer_task(messages) == SENDER


def test_task_answered_with_sendmessage_is_clear() -> None:
    messages = [
        user(TASK),
        assistant("Bash"),
        tool_result(),
        assistant("SendMessage"),
        tool_result(),
        assistant(),
    ]
    assert hook.pending_peer_task(messages) is None


def test_fyi_message_without_reply_fields_needs_no_reply() -> None:
    assert hook.pending_peer_task([user(FYI), assistant()]) is None


def test_plain_user_prompt_after_task_clears_obligation() -> None:
    messages = [user(TASK), assistant(), user("did you reply?"), assistant()]
    assert hook.pending_peer_task(messages) is None


def test_second_task_after_answered_first_is_pending() -> None:
    messages = [
        user(TASK),
        assistant("SendMessage"),
        tool_result(),
        user(TASK),
        assistant("Bash"),
        tool_result(),
        assistant(),
    ]
    assert hook.pending_peer_task(messages) == SENDER


def test_list_content_user_message_is_parsed() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": TASK}]}, assistant()]
    assert hook.pending_peer_task(messages) == SENDER


def test_a_task_without_a_from_attribute_names_a_placeholder_sender() -> None:
    task = "<cross-session-message>\nreply-to: worker-a\n</cross-session-message>"
    assert hook.pending_peer_task([user(task), assistant()]) == "<sender>"


def test_an_unanswered_task_blocks_the_stop_and_names_the_sender() -> None:
    code, output = run_hook(payload_for(FIXTURES / "unanswered_task.jsonl"))
    assert code == 0
    assert output["decision"] == "block"
    assert f"`{SENDER}`" in output["reason"]
    assert "SendMessage" in output["reason"]


def test_an_answered_task_allows_the_stop() -> None:
    code, output = run_hook(payload_for(FIXTURES / "answered_task.jsonl"))
    assert code == 0
    assert output == {}


def test_the_redo_turn_after_a_block_allows_the_stop() -> None:
    payload = payload_for(FIXTURES / "unanswered_task.jsonl", stop_hook_active=True)
    assert run_hook(payload) == (0, {})


@pytest.mark.parametrize("payload", ["not json", "{}", '{"transcript_path": "/nonexistent.jsonl"}'])
def test_unreadable_input_allows_the_stop(payload: str) -> None:
    assert run_hook(payload) == (0, {})
