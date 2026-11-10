#!/usr/bin/env python3
"""Deny an Edit or MultiEdit whose new_string ends in a space or tab, outside Markdown files.

The Edit tool drops trailing spaces and tabs from new_string in any file but Markdown (`.md`,
`.mdx`), so a replacement whose boundary is a space silently deletes it:
`new_string="identifier: "` is written as `identifier:`, and the next token fuses on. A
`replace_all` that rewrites a prefix ending in a space corrupts every match.

Usage: PreToolUse hook, matcher `Edit|MultiEdit`; Claude Code passes the call as JSON on stdin.
Input: `tool_name` and `tool_input` with its `file_path`; for MultiEdit `tool_input.edits` is a
list of edits.
Output: nothing to allow, or a `permissionDecision: deny` JSON object on stdout.
Exit code: 0. Malformed JSON, other tools and unexpected tool_input shapes are allowed.

Decisions:
- file_path ends in `.md` or `.mdx`, in any case -> allow: the tool keeps the whitespace there.
- new_string ends in " " or "\\t" -> deny (for MultiEdit, any edit in the list).
- new_string ends in a newline or any other character, or is empty -> allow.
- old_string is not checked: the tool matches it exactly, so a trailing space there either
  matches or fails loudly ("String to replace not found").

Register with a short timeout and onFailure "block" so a hook that times out, crashes or cannot
start blocks the edit instead of letting it through.
"""

import json
import re
import sys
from typing import Any

TRAILING_WHITESPACE = (" ", "\t")
# The paths whose trailing whitespace the Edit tool keeps.
MARKDOWN = re.compile(r"\.(md|mdx)\Z", re.IGNORECASE)


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def edits_in(tool_name: str, tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """The individual edits a tool call carries."""
    if tool_name == "Edit":
        return [tool_input]
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        return [edit for edit in edits if isinstance(edit, dict)] if isinstance(edits, list) else []
    return []


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        sys.exit(0)
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and MARKDOWN.search(file_path):
        sys.exit(0)

    edits = edits_in(hook_input.get("tool_name", ""), tool_input)
    for index, edit in enumerate(edits, start=1):
        new_string = edit.get("new_string")
        if isinstance(new_string, str) and new_string.endswith(TRAILING_WHITESPACE):
            which = f"edit {index} of {len(edits)}'s" if len(edits) > 1 else "this edit's"
            deny(
                f"Blocked: {which} new_string ends in a space or tab, and outside Markdown "
                "files the Edit tool drops trailing spaces and tabs from new_string, so the "
                "replacement would silently "
                "lose it (`identifier: ` is written as `identifier:`, and the next token fuses "
                "on). Anchor the boundary on a non-whitespace token: include the next token in "
                "both old_string and new_string, or stop before the space. After a replace_all, "
                "grep the new text once to confirm its spacing."
            )
    sys.exit(0)


if __name__ == "__main__":
    main()
