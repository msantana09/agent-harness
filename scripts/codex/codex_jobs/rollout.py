"""Read Codex's rollout files: `<sessions>/YYYY/MM/DD/rollout-<stamp>-<thread id>.jsonl`, one JSON
row per line, which Codex writes for every thread it runs.

Reconcile reads a turn's end from them when the app-server is gone. The runner copies a turn's
code-mode calls from them into the job's journal: a command Codex runs from a code-mode script
(`tools.exec_command` inside the `exec` tool) and the sandbox refuses emits no item on the
app-server stream, so the rollout is the only record of it.
"""

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

# the filename prefix that orders rollouts in time
STAMP_LENGTH = len("rollout-") + len("2026-11-08T12-00-00")
# The custom tool that runs a code-mode script, and the function that collects the rest of the
# output of a script that was still running when `exec` returned ("Script running with cell ID").
CODE_MODE_TOOL = "exec"
CODE_MODE_WAIT = "wait"
TURN_ENDS = ("task_complete", "turn_aborted")


def rows(path: Path) -> Iterator[dict[str, Any]]:
    for line in path.read_text().splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:  # a line cut off by a crash
            continue


def _session_meta(path: Path) -> dict[str, Any]:
    """The payload of the rollout's first row when it is `session_meta`, else {}; reads only
    that line."""
    with path.open() as handle:
        try:
            first = json.loads(handle.readline())
        except json.JSONDecodeError:
            return {}
    return (first.get("type") == "session_meta" and first.get("payload")) or {}


def thread_rollouts(sessions_dir: Path, thread_id: str) -> list[Path]:
    """The thread's rollout files, oldest first."""
    return sorted(Path(sessions_dir).rglob(f"rollout-*-{thread_id}.jsonl"))


def review_subthreads(sessions_dir: Path, thread_id: str, parent: Path) -> list[Path]:
    """The rollouts of review sub-agent threads of `thread_id` that started after `parent`, the
    thread's own rollout, did."""
    found = []
    for path in sorted(Path(sessions_dir).rglob("rollout-*.jsonl")):
        if path.name[:STAMP_LENGTH] < parent.name[:STAMP_LENGTH]:
            continue
        meta = _session_meta(path)
        if (
            meta.get("source") == {"subagent": "review"}
            and meta.get("parent_thread_id") == thread_id
        ):
            found.append(path)
    return found


def _turn_rows(items: Iterable[dict[str, Any]], turn_id: str) -> list[dict[str, Any]]:
    """The rows from the turn's `task_started` up to the next turn's."""
    current, kept = None, []
    for row in items:
        payload = row.get("payload") or {}
        if row.get("type") == "event_msg" and payload.get("type") == "task_started":
            current = payload.get("turn_id")
        if current == turn_id:
            kept.append(row)
    return kept


def _ended(items: list[dict[str, Any]], turn_id: str | None) -> bool:
    """Whether a turn end row is among `items`: the one for `turn_id`, or any when it is None."""
    return any(
        row.get("type") == "event_msg"
        and (row.get("payload") or {}).get("type") in TURN_ENDS
        and turn_id in (None, row["payload"].get("turn_id"))
        for row in items
    )


def _code_mode_calls(thread_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each code-mode script and `wait` in `items`, in order, with its output (null while it had
    none)."""
    calls: dict[str, dict[str, Any]] = {}
    for row in items:
        payload = row.get("payload") or {}
        kind, call_id = payload.get("type"), payload.get("call_id")
        if call_id is None:
            continue
        if (kind, payload.get("name")) in (
            ("custom_tool_call", CODE_MODE_TOOL),
            ("function_call", CODE_MODE_WAIT),
        ):
            calls[call_id] = {
                "thread_id": thread_id,
                "call_id": call_id,
                "tool": payload["name"],
                "code": payload.get("input", payload.get("arguments")),
                "output": None,
            }
        elif kind in ("custom_tool_call_output", "function_call_output") and call_id in calls:
            calls[call_id]["output"] = payload.get("output")
    return list(calls.values())


def turn_code_mode_calls(
    sessions_dir: Path, thread_id: str, turn_id: str, *, native_review: bool
) -> dict[str, Any]:
    """The `code_mode_calls` journal entry for a turn: `{"turn_id", "calls"}`, plus `"error"` when
    the calls may be incomplete: no rollout, not exactly one review sub-thread, or a rollout that
    does not show the turn's end yet.

    A native review runs its commands in its review sub-thread, which has its own rollout; the
    parent's rollout holds none of them.
    """
    entry: dict[str, Any] = {"turn_id": turn_id, "calls": []}
    found = thread_rollouts(sessions_dir, thread_id)
    if not found:
        return {**entry, "error": f"no rollout for thread {thread_id} in {sessions_dir}"}
    if native_review:
        subthreads = review_subthreads(sessions_dir, thread_id, found[0])
        if len(subthreads) != 1:
            return {
                **entry,
                "error": f"{len(subthreads)} review sub-thread rollouts for thread {thread_id}",
            }
        items = list(rows(subthreads[0]))
        entry["calls"] = _code_mode_calls(_session_meta(subthreads[0])["id"], items)
        ended = _ended(items, None)
    else:
        items = [row for path in found for row in _turn_rows(rows(path), turn_id)]
        entry["calls"] = _code_mode_calls(thread_id, items)
        ended = _ended(items, turn_id)
    if not ended:
        entry["error"] = (
            f"the rollout does not show the end of turn {turn_id}; calls may be missing"
        )
    return entry
