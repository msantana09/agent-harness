#!/usr/bin/env python3
"""Stop hook: a turn that answers a peer task must end with a SendMessage.

Usage: registered as a Stop command hook; Claude Code pipes the hook payload to stdin.

A cross-session task message is a user message containing `<cross-session-message ...>` with a
`reply-to:` or `done-signal:` line. The sending session marks a message as a task that wants an
answer by writing a `reply-to: <name>` or `done-signal: <what done looks like>` line in its body.
Only a `SendMessage` tool call answers it: text written to the local chat never reaches the
sender, who sees silence and sends the task again.

Rule: when the most recent human-authored user message is such a task message and no
`SendMessage` call has happened since it arrived, the stop is blocked. A message without a
`reply-to:` or `done-signal:` line is informational and needs no reply. A newer plain user
prompt ends the obligation.

Input: JSON with `transcript_path` and `stop_hook_active`.
Output: `{}` to allow the stop, or `{"decision": "block", "reason": ...}` naming the sender to
reply to. Exit code is 0 either way.
Fails open: unparseable stdin, a missing or unreadable transcript, and `stop_hook_active` (the
redo turn after a block) all allow the stop, so the hook can never trap a session.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_shared"))

from transcript_parser import load_transcript  # noqa: E402

PEER_TAG_RE = re.compile(r"<cross-session-message\b")
REPLY_FIELD_RE = re.compile(r"^\s*(reply-to|done-signal)\s*:", re.IGNORECASE | re.MULTILINE)
FROM_RE = re.compile(r'<cross-session-message[^>]*\bfrom="([^"]+)"')


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_tool_result_only(content: Any) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _tool_names(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        b.get("name", "") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def pending_peer_task(messages: list[dict[str, Any]]) -> str | None:
    """Return the sender id of an unanswered peer task, or None.

    Walks backwards to the latest human-authored user message. If it is a peer task message
    and no SendMessage call sits between it and the end, the task is unanswered.
    """
    send_seen = False
    for msg in reversed(messages):
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant":
            if "SendMessage" in _tool_names(content):
                send_seen = True
            continue
        if role != "user" or _is_tool_result_only(content):
            continue
        text = _text_of(content)
        if not PEER_TAG_RE.search(text) or not REPLY_FIELD_RE.search(text):
            return None
        if send_seen:
            return None
        m = FROM_RE.search(text)
        return m.group(1) if m else "<sender>"
    return None


def emit_allow() -> None:
    print(json.dumps({}))


def emit_block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        emit_allow()
        return

    if hook_input.get("stop_hook_active", False):
        emit_allow()
        return

    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path:
        emit_allow()
        return

    transcript = load_transcript(Path(transcript_path))
    if not transcript:
        emit_allow()
        return

    sender = pending_peer_task(transcript.get("messages", []))
    if sender is None:
        emit_allow()
        return

    emit_block(
        "This turn is answering a peer task message (it carries reply-to/done-signal) "
        "but no SendMessage was sent. Chat text never reaches the sender. "
        'Load SendMessage via ToolSearch(query="select:SendMessage") and reply to '
        f"`{sender}` (the message's from= id; a reply-to name may not resolve in this "
        "session), carrying only what the task asked."
    )


if __name__ == "__main__":
    main()
