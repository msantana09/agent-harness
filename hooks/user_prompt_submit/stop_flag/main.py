#!/usr/bin/env python3
"""A bare "stop" from the user halts the session's tool calls until their next prompt.

A prompt that is only the word stop (any case, trailing punctuation or repeats) sets the
session's flag; the stop_gate PreToolUse hook then denies every tool call except the one a
subagent or workflow stage uses to hand back its result. The next human prompt clears it.
Machine-submitted prompts (below) never clear it.

The hook input carries no origin field on Claude Code 2.1.303 (keys are session_id,
transcript_path, cwd, prompt_id, permission_mode, hook_event_name, prompt; the `source` field in
the UserPromptSubmitHookInput types is absent from real payloads), so machine prompts are
recognised by the text shape their sender gives them. Known gap: a scheduled task, routine or
/loop wakeup (origin `scheduled-trigger`) submits its stored prompt verbatim, with no marker, so
it reads as human and clears the flag.

Usage: registered as a UserPromptSubmit command hook; Claude Code pipes the payload to stdin.
Input: JSON with `session_id` and `prompt`.
Output: nothing, or on a bare stop an `additionalContext` JSON object telling the model to make
no tool calls. The flag is a file under ~/.claude/state/stop/ named by the session id
(hooks/lib_shared/stop_flag.py).
Exit code: 0 always; a failure is written to stderr and the prompt goes through.
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_shared"))

from stop_flag import DENY_REASON, flag_path  # noqa: E402

BARE_STOP = re.compile(r"stop(?:[\s.!?,;:…]+stop)*[\s.!?,;:…]*", re.IGNORECASE)

# Prompts that reach UserPromptSubmit without the user typing them, by leading text.
MACHINE_PROMPT_PREFIXES = (
    # Background task, Monitor and relay events, artifact-watch (origin `task-notification`).
    "<task-notification>",
    # Subagent hand-backs and cross-session messages (origin `peer`). The transcript shows them
    # under an "Another Claude session sent a message:" frame, but the hook sees the bare tag:
    # Claude Code 2.1.303 delivers a hand-back as
    # `<agent-message from="...">\n[Subagent hand-back] ...` and a message from another session
    # as `<cross-session-message from="..." from-name="..." from-mode="prompting">`.
    "<agent-message ",
    "<cross-session-message ",
    # A resume plugin's own text. It is submitted without asUser, so PLUGIN_FRAME below normally
    # wraps it; whether the hook sees the frame is unconfirmed, so the bare text is listed too.
    "Your last turn was cut off by",
    "Your last turn stopped at the usage limit",
    # Claude Code's usage-limit auto-continuation (origin `auto-continuation`).
    "You can continue now. Continue the task you were working on",
    "Your claude.ai usage limit has reset.",
    # Cross-session delivery and idle notices.
    "[Cross-session ",
    # Artifact comment turns (Claude Docs MCP server instructions).
    "[Artifact comment sent to Claude]",
)
# A prompt a plugin submits without asUser is framed like this for the model. The transcript
# frame is added after the hook runs (the peer frame is absent at the hook, see above), so the
# plugin frame is probably absent too; it stays as a harmless second match.
PLUGIN_FRAME = re.compile(r"The \S+ plugin sent a message:")


def is_bare_stop(prompt: str) -> bool:
    return BARE_STOP.fullmatch(prompt.strip()) is not None


def is_machine_prompt(prompt: str) -> bool:
    text = prompt.lstrip()
    return text.startswith(MACHINE_PROMPT_PREFIXES) or PLUGIN_FRAME.match(text) is not None


def main() -> None:
    hook_input = json.loads(sys.stdin.read())
    session_id = hook_input.get("session_id")
    prompt = hook_input.get("prompt")
    path = flag_path(session_id)
    if path is None or not isinstance(prompt, str):
        return
    if is_bare_stop(prompt):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_id": session_id, "at": time.time()}), encoding="utf-8")
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": DENY_REASON,
                    }
                }
            )
        )
    elif not is_machine_prompt(prompt):
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"stop_flag: {error}", file=sys.stderr)
    sys.exit(0)
