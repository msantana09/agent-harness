"""Read Codex's rollout files: `<sessions>/YYYY/MM/DD/rollout-<stamp>-<thread id>.jsonl`, one JSON
row per line, which Codex writes for every thread it runs. Codex compresses a rollout that has
been cold for a week to `.jsonl.zst`; both forms are read.

Reconcile reads a turn's end from them when the app-server is gone. The runner copies a turn's
code-mode calls from them into the job's journal: a command Codex runs from a code-mode script
(`tools.exec_command` inside the `exec` tool) and the sandbox refuses emits no item on the
app-server stream, so the rollout is the only record of it.
"""

import json
from collections.abc import Iterable, Iterator
from compression import zstd
from pathlib import Path
from typing import Any, TextIO

# the filename prefix that orders rollouts in time
STAMP_LENGTH = len("rollout-") + len("2026-11-08T12-00-00")
# The custom tool that runs a code-mode script, and the function that collects the rest of the
# output of a script that was still running when `exec` returned ("Script running with cell ID").
CODE_MODE_TOOL = "exec"
CODE_MODE_WAIT = "wait"
TURN_ENDS = ("task_complete", "turn_aborted")
# The rows of a tool call and of its output, paired by `call_id`.
CALL_ROWS = ("custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output")


def _row(line: str) -> dict[str, Any] | None:
    """The line as a row: an object whose `payload`, when present, is an object; else None."""
    try:
        row = json.loads(line)
    except json.JSONDecodeError:  # a line cut off by a crash
        return None
    if not isinstance(row, dict) or not isinstance(row.get("payload", {}), dict):
        return None
    return row


def _open(path: Path) -> TextIO:
    """The rollout for reading, decompressed when it is `.zst`. A rollout can change form after it
    was listed: Codex compresses a cold one and writes the plain file back when the thread resumes,
    each time removing the other form, so a listed form that is gone is read as the other one.
    A crash can cut a row inside a multi-byte character, so bytes that are not UTF-8 read as U+FFFD
    and leave only that row unparseable."""
    try:
        return _open_form(path)
    except FileNotFoundError:
        if path.suffix == ".zst":
            return _open_form(path.with_suffix(""))
        return _open_form(path.with_name(f"{path.name}.zst"))


def _open_form(path: Path) -> TextIO:
    """The file at `path` for reading as text, decompressed when it is `.zst`."""
    if path.suffix == ".zst":
        return zstd.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def _rollouts(sessions_dir: Path, stem: str) -> list[Path]:
    """The rollouts under `sessions_dir` whose name matches `<stem>.jsonl` or `<stem>.jsonl.zst`,
    sorted. A `.zst` is left out while its plain file is still there: Codex writes it beside the
    plain file before removing that one."""
    found = set(Path(sessions_dir).rglob(f"{stem}.jsonl*"))
    return sorted(
        path
        for path in found
        if path.suffix == ".jsonl"
        or (path.suffixes[-2:] == [".jsonl", ".zst"] and path.with_suffix("") not in found)
    )


def rows(path: Path) -> Iterator[dict[str, Any]]:
    """The file's rows, one per physical line: Codex writes a row's text unescaped, so a Unicode
    line separator inside a row does not end it. A file moved since it was listed has none."""
    try:
        handle = _open(path)
    except FileNotFoundError:  # Codex archives a rollout by moving it
        return
    with handle:
        for line in handle:
            if (row := _row(line)) is not None:
                yield row


def _session_meta(path: Path) -> dict[str, Any]:
    """The payload of the rollout's first row when it is `session_meta`, else {}; reads only
    that line."""
    with _open(path) as handle:
        return _meta(_row(handle.readline()))


def _meta(first: dict[str, Any] | None) -> dict[str, Any]:
    """The payload of a rollout's first row when it is `session_meta`, else {}."""
    first = first or {}
    return (first.get("type") == "session_meta" and first.get("payload")) or {}


def thread_rollouts(sessions_dir: Path, thread_id: str) -> list[Path]:
    """The thread's rollout files, oldest first: `<thread id>.jsonl`, and the
    `<thread id>_<rollout id>.jsonl` that replaces it when the thread is reverted; either as
    `.jsonl.zst` once Codex has compressed it."""
    return sorted(
        [
            *_rollouts(sessions_dir, f"rollout-*-{thread_id}"),
            *_rollouts(sessions_dir, f"rollout-*-{thread_id}_*"),
        ]
    )


def review_subthreads(sessions_dir: Path, thread_id: str, parent: Path) -> list[Path]:
    """The rollouts of review sub-agent threads of `thread_id` that started after `parent`, the
    thread's own rollout, did."""
    found = []
    for path in _rollouts(sessions_dir, "rollout-*"):
        if path.name[:STAMP_LENGTH] < parent.name[:STAMP_LENGTH]:
            continue
        try:
            meta = _session_meta(path)
        except FileNotFoundError:  # Codex archives a rollout by moving it
            continue
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


def _code_mode_calls(
    thread_id: str | None, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Each code-mode script and `wait` in `items`, in order, with its output (null while it had
    none); and how many tool call or output rows had no string call id, so could not be paired."""
    calls: dict[str, dict[str, Any]] = {}
    unusable = 0
    for row in items:
        payload = row.get("payload") or {}
        kind, call_id = payload.get("type"), payload.get("call_id")
        if kind not in CALL_ROWS:
            continue
        if not isinstance(call_id, str):
            unusable += 1
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
    return list(calls.values()), unusable


def turn_code_mode_calls(
    sessions_dir: Path, thread_id: str, turn_id: str, *, native_review: bool
) -> dict[str, Any]:
    """The `code_mode_calls` journal entry for a turn: `{"turn_id", "calls"}`, plus `"error"` when
    the calls may be incomplete: no rollout, not exactly one review sub-thread, tool call rows
    without a usable call id, or a rollout that does not show the turn's end yet.

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
        # the id comes from the rows read: Codex can archive the file the moment they are
        child_id = _meta(items[0] if items else None).get("id")
        entry["calls"], unusable = _code_mode_calls(child_id, items)
        ended = _ended(items, None)
    else:
        items = [row for path in found for row in _turn_rows(rows(path), turn_id)]
        entry["calls"], unusable = _code_mode_calls(thread_id, items)
        ended = _ended(items, turn_id)
    notes = []
    if unusable:
        notes.append(f"{unusable} rollout rows of the turn have no usable call id")
    if not ended:
        notes.append(f"the rollout does not show the end of turn {turn_id}")
    if notes:
        entry["error"] = "; ".join(notes) + "; calls may be missing"
    return entry
