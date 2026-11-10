#!/usr/bin/env python3
"""Emoji guard for the Edit, MultiEdit, Write and NotebookEdit tools.

Usage: PreToolUse hook, matcher `Edit|MultiEdit|Write|NotebookEdit`, timeout 2 s, onFailure
"block"; Claude Code runs `python3 main.py` with the tool call as JSON on stdin. Codex does
not run this hook.

Enforces the emoji rule: emoji are prohibited in docs and comments, and allowed in code only
when explicitly requested. Only the text being written is checked (`new_string`, `content`, a
MultiEdit edit's `new_string`, a notebook's `new_source`), so deleting an emoji from a file is
never blocked.

An emoji is a character the renderer draws as a picture:
- a code point with Unicode `Emoji_Presentation=Yes` (EMOJI_PRESENTATION_RANGES; the regional
  indicators U+1F1E6..U+1F1FF are in it),
- any character followed by U+FE0F (the emoji variation selector).
Text-style symbols stay allowed: CJK, Hangul, box drawing, arrows, the check and ballot marks
U+2713 and U+2717, U+25CB, full-width brackets. U+2705, U+1F525, U+1F4A1, U+1F44D and U+1F916
are blocked.

Output on stdout, always with exit code 0:
- the text goes to a doc or text file (DOC_EXTENSIONS, or a markdown notebook cell): a
  `permissionDecision` of "deny";
- the text goes anywhere else, code and unknown file types alike: "ask", since the rule allows
  emoji in code when requested and a prompt is the cheap way to find out;
- nothing at all (the call is allowed) when the text holds no emoji, and when the guard fails
  open: stdin is not JSON or not a JSON object, the tool is not one of the four, `tool_input`
  is not an object, or the call carries no string text to check.

The reason names up to MAX_LISTED distinct emoji by code point, in order of appearance.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

# Emoji_Presentation=Yes as inclusive (low, high) code point ranges, generated from Unicode 17.0:
# every code point outside the surrogates tested with JavaScript's /\p{Emoji_Presentation}/u,
# consecutive hits merged. Python's unicodedata has no emoji properties, so regenerate this
# table the same way when the Unicode version moves.
# fmt: off
EMOJI_PRESENTATION_RANGES = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3), (0x25FD, 0x25FE),
    (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693), (0x26A1, 0x26A1),
    (0x26AA, 0x26AB), (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4),
    (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C), (0x274E, 0x274E),
    (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x1F004, 0x1F004),
    (0x1F0CF, 0x1F0CF), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1E6, 0x1F1FF),
    (0x1F201, 0x1F201), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F236),
    (0x1F238, 0x1F23A), (0x1F250, 0x1F251), (0x1F300, 0x1F320), (0x1F32D, 0x1F335),
    (0x1F337, 0x1F37C), (0x1F37E, 0x1F393), (0x1F3A0, 0x1F3CA), (0x1F3CF, 0x1F3D3),
    (0x1F3E0, 0x1F3F0), (0x1F3F4, 0x1F3F4), (0x1F3F8, 0x1F43E), (0x1F440, 0x1F440),
    (0x1F442, 0x1F4FC), (0x1F4FF, 0x1F53D), (0x1F54B, 0x1F54E), (0x1F550, 0x1F567),
    (0x1F57A, 0x1F57A), (0x1F595, 0x1F596), (0x1F5A4, 0x1F5A4), (0x1F5FB, 0x1F64F),
    (0x1F680, 0x1F6C5), (0x1F6CC, 0x1F6CC), (0x1F6D0, 0x1F6D2), (0x1F6D5, 0x1F6D8),
    (0x1F6DC, 0x1F6DF), (0x1F6EB, 0x1F6EC), (0x1F6F4, 0x1F6FC), (0x1F7E0, 0x1F7EB),
    (0x1F7F0, 0x1F7F0), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1F9FF),
    (0x1FA70, 0x1FA7C), (0x1FA80, 0x1FA8A), (0x1FA8E, 0x1FAC6), (0x1FAC8, 0x1FAC8),
    (0x1FACD, 0x1FADC), (0x1FADF, 0x1FAEA), (0x1FAEF, 0x1FAF8),
)
# fmt: on

DOC_EXTENSIONS = frozenset(
    {".md", ".mdx", ".markdown", ".txt", ".text", ".rst", ".adoc", ".org", ".tex", ".log"}
)

WRITING_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
MAX_LISTED = 5

_EMOJI = re.compile(
    ".\\uFE0F|["
    + "".join(
        f"\\U{low:08X}" if low == high else f"\\U{low:08X}-\\U{high:08X}"
        for low, high in EMOJI_PRESENTATION_RANGES
    )
    + "]",
    re.DOTALL,
)


def emit(decision: str, reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def written_texts(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """The text a tool call writes, one string per edit."""
    if tool_name == "Edit":
        keys = [tool_input.get("new_string")]
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            keys = [edit.get("new_string") for edit in edits if isinstance(edit, dict)]
        else:
            keys = []
    elif tool_name == "Write":
        keys = [tool_input.get("content")]
    else:
        keys = [tool_input.get("new_source")]
    return [text for text in keys if isinstance(text, str)]


def is_doc(tool_name: str, tool_input: dict[str, Any]) -> bool:
    if tool_name == "NotebookEdit":
        return tool_input.get("cell_type") == "markdown"
    path = tool_input.get("file_path")
    return isinstance(path, str) and Path(path).suffix.lower() in DOC_EXTENSIONS


def emoji_found(texts: list[str]) -> list[str]:
    """Distinct emoji in the texts, each as its code points (`U+1F525`), in order of appearance.

    Stops one past MAX_LISTED: the message names MAX_LISTED and says there are more, so a text
    full of distinct emoji costs one pass, not a scan of the growing list per match.
    """
    found: list[str] = []
    for text in texts:
        for match in _EMOJI.finditer(text):
            label = " ".join(f"U+{ord(char):04X}" for char in match.group())
            if label not in found:
                found.append(label)
                if len(found) > MAX_LISTED:
                    return found
    return found


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)
    if not isinstance(hook_input, dict):
        sys.exit(0)

    tool_name = hook_input.get("tool_name")
    tool_input = hook_input.get("tool_input")
    if tool_name not in WRITING_TOOLS or not isinstance(tool_input, dict):
        sys.exit(0)

    found = emoji_found(written_texts(tool_name, tool_input))
    if not found:
        sys.exit(0)

    listed = ", ".join(found[:MAX_LISTED]) + (", ..." if len(found) > MAX_LISTED else "")
    if is_doc(tool_name, tool_input):
        emit(
            "deny",
            f"Blocked: this write puts emoji in a doc or text file ({listed}). Emoji are "
            "prohibited in docs and comments. Say it in words instead; check "
            "and ballot marks (U+2713, U+2717), arrows and box drawing are fine. To mention an "
            "emoji, write its code point.",
        )
    emit(
        "ask",
        f"This write puts emoji in a code or unrecognized file ({listed}). Emoji are "
        "prohibited in comments and allowed in code only when explicitly requested. "
        "Approve only if the request asked for them.",
    )


if __name__ == "__main__":
    main()
