"""Per-session stop flag, shared by the stop_flag and stop_gate hooks.

A bare "stop" from the user leaves one file per session under ~/.claude/state/stop/; the
PreToolUse stop_gate hook denies every tool call of that session, except the one a subagent or
workflow stage uses to hand back its result, until the next human prompt removes it. Stdlib
only: stop_gate imports this on every tool call in every session.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

DENY_REASON = (
    "The user typed stop. Make no tool calls. Acknowledge in one line and wait for their "
    "next message."
)


def flag_path(session_id: object) -> Path | None:
    """The flag file of a session, or None when the id cannot be a filename."""
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    return Path.home() / ".claude" / "state" / "stop" / session_id


def flag_is_set(path: Path, session_id: str) -> bool:
    """True only for a readable flag naming this session; anything else fails open."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("session_id") == session_id
