#!/usr/bin/env python3
"""
Shared transcript parsing utilities for Claude Code hooks.

Provides reusable functions for parsing conversation transcripts,
which can be used by any hook that needs to analyze conversation history.
"""

import json
from pathlib import Path
from typing import Any


def load_transcript(transcript_path: Path) -> dict[str, Any] | None:
    """
    Load and parse a conversation transcript JSONL file.

    Claude Code transcripts are JSONL format (one JSON object per line).
    This function reads all lines and combines them into a messages array.

    The JSONL contains several event types:
    - {"type": "assistant", "message": {...}} - Assistant messages with content
    - {"type": "user", "message": {...}} - User messages
    - {"type": "tool_use", "name": "...", "input": {...}} - Standalone tool calls
    - {"type": "tool_result", "tool_use_id": "...", "content": "..."} - Tool results
    - {"type": "api-request-blob", "message": {...}} - Repeats earlier messages
      (same tool_use ids) without their `timestamp` or `cwd`; skipped

    Tool events are wrapped in synthetic messages for downstream compatibility.
    An event's `timestamp` (ISO 8601, written by Claude Code on every line) is
    copied onto the message appended for it, so callers can place messages on
    a real-time timeline. Its `cwd` (the shell's directory when the line was
    written, before the line's own command ran) is copied the same way, so
    callers can place a Bash call in the directory it started in.

    Args:
        transcript_path: Path to conversation JSONL file

    Returns:
        Parsed transcript dictionary with 'messages' key, or None if loading fails

    Examples:
        >>> transcript = load_transcript(Path('/path/to/transcript.jsonl'))
        >>> messages = transcript.get('messages', [])
    """
    if not transcript_path or not transcript_path.exists():
        return None

    try:
        messages = []
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)

                if event.get("type") == "api-request-blob":
                    continue

                # JSONL events have message nested: {"message": {...}, "type": "assistant"}
                # Extract the actual message if present
                if "message" in event and isinstance(event["message"], dict):
                    message = event["message"]
                elif event.get("role") in ("user", "assistant"):
                    # Fallback for direct message format
                    message = event
                # Handle standalone tool_use events (not nested in assistant message)
                # Wrap as synthetic assistant message for downstream compatibility
                elif event.get("type") == "tool_use":
                    message = {"role": "assistant", "content": [event]}
                # Handle standalone tool_result events
                # Wrap as synthetic user message (tool results come from system)
                elif event.get("type") == "tool_result":
                    message = {"role": "user", "content": [event]}
                else:
                    continue

                if "timestamp" in event:
                    message["timestamp"] = event["timestamp"]
                if "cwd" in event:
                    message["cwd"] = event["cwd"]
                messages.append(message)

        return {"messages": messages}
    except (OSError, json.JSONDecodeError, ValueError):
        return None
