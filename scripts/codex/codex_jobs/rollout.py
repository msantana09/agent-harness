"""Read Codex's rollout files: `<sessions>/YYYY/MM/DD/rollout-<stamp>-<thread id>.jsonl`, one JSON
row per line, which Codex writes for every thread it runs.

Reconcile reads a turn's end from them when the app-server is gone. The runner copies a turn's
code-mode calls from them into the job's journal: a command Codex runs from a code-mode script
(`tools.exec_command` inside the `exec` tool) and the sandbox refuses emits no item on the
app-server stream, so the rollout is the only record of it.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# the filename prefix that orders rollouts in time
STAMP_LENGTH = len("rollout-") + len("2026-11-08T12-00-00")
# the custom tool that runs a code-mode script
CODE_MODE_TOOL = "exec"


def rows(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text().splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:  # a line cut off by a crash
            continue


def thread_rollouts(sessions_dir: Path, thread_id: str) -> list[Path]:
    """The thread's rollout files, oldest first."""
    return sorted(Path(sessions_dir).rglob(f"rollout-*-{thread_id}.jsonl"))


def _is_review_subthread(path: Path, parent_thread_id: str) -> bool:
    """Whether the rollout's first row says it is a review sub-agent thread of
    `parent_thread_id`."""
    first = next(rows(path), None)
    meta = (first or {}).get("payload") or {}
    return (
        (first or {}).get("type") == "session_meta"
        and meta.get("source") == {"subagent": "review"}
        and meta.get("parent_thread_id") == parent_thread_id
    )


def review_subthreads(sessions_dir: Path, thread_id: str, parent: Path) -> list[Path]:
    """The rollouts of review sub-agent threads of `thread_id` that started after `parent`, the
    thread's own rollout, did."""
    return [
        path
        for path in sorted(Path(sessions_dir).rglob("rollout-*.jsonl"))
        if path.name[:STAMP_LENGTH] >= parent.name[:STAMP_LENGTH]
        and _is_review_subthread(path, thread_id)
    ]


def _turn_rows(path: Path, turn_id: str) -> Iterator[dict[str, Any]]:
    """The rows from the turn's `task_started` up to the next turn's."""
    current = None
    for row in rows(path):
        payload = row.get("payload") or {}
        if row.get("type") == "event_msg" and payload.get("type") == "task_started":
            current = payload.get("turn_id")
        if current == turn_id:
            yield row


def _code_mode_calls(thread_id: str, items: Iterator[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each code-mode script in `items` with its output (null while it had none)."""
    calls: dict[str, dict[str, Any]] = {}
    for row in items:
        payload = row.get("payload") or {}
        if payload.get("type") == "custom_tool_call" and payload.get("name") == CODE_MODE_TOOL:
            calls[payload["call_id"]] = {
                "thread_id": thread_id,
                "call_id": payload["call_id"],
                "code": payload.get("input"),
                "output": None,
            }
        elif payload.get("type") == "custom_tool_call_output" and payload.get("call_id") in calls:
            calls[payload["call_id"]]["output"] = payload.get("output")
    return list(calls.values())


def turn_code_mode_calls(
    sessions_dir: Path, thread_id: str, turn_id: str, *, native_review: bool
) -> dict[str, Any]:
    """The `code_mode_calls` journal entry for a turn: `{"turn_id", "calls"}`, plus `"error"` when
    the thread has no rollout in `sessions_dir`. A native review's calls are its review
    sub-threads', which run in their own rollouts."""
    found = thread_rollouts(sessions_dir, thread_id)
    if not found:
        return {
            "turn_id": turn_id,
            "calls": [],
            "error": f"no rollout for thread {thread_id} in {sessions_dir}",
        }
    calls = [
        call for path in found for call in _code_mode_calls(thread_id, _turn_rows(path, turn_id))
    ]
    if native_review:
        for path in review_subthreads(sessions_dir, thread_id, found[0]):
            calls += _code_mode_calls(next(rows(path))["payload"]["id"], rows(path))
    return {"turn_id": turn_id, "calls": calls}
