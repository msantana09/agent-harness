#!/usr/bin/env python3
"""Deny every tool call of a session the user has told to stop.

Usage: PreToolUse hook, matcher `*`, registered first; paired with user_prompt_submit/stop_flag,
which sets the flag on a bare "stop" and clears it on the next human prompt. Claude Code passes
the call as JSON on stdin.
Input: `tool_name` and `session_id`; the flag is the file named by the session id under
~/.claude/state/stop/ (hooks/lib_shared/stop_flag.py).
Output: nothing to allow, or a `permissionDecision: deny` JSON object on stdout.
Exit code: 0 always.

Subagents carry their parent's session_id, so they are denied too, except for the call that
returns their result: a stopped subagent or workflow stage that cannot hand back keeps retrying.

Runs on every tool call in every session: with no flag it costs one stat. Any failure allows the
call, so a bad flag can never lock a session; register it with no onFailure "block" for the same
reason, since that also blocks a hook that crashes or cannot start.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_shared"))

from stop_flag import DENY_REASON, flag_is_set, flag_path  # noqa: E402

RESULT_TOOLS = frozenset({"SubagentHandback", "StructuredOutput"})


def main() -> None:
    hook_input = json.loads(sys.stdin.read())
    if hook_input.get("tool_name") in RESULT_TOOLS:
        return
    session_id = hook_input.get("session_id")
    path = flag_path(session_id)
    if path is None or not path.is_file() or not flag_is_set(path, session_id):
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": DENY_REASON,
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
