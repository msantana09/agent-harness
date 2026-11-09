#!/usr/bin/env python3
"""Tests for transcript parser utilities."""

import json
import sys
import tempfile
from pathlib import Path

# Add paths for imports
hooks_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(hooks_root))

from lib_shared.transcript_parser import load_transcript  # noqa: E402


class TestLoadTranscript:
    """Tests for load_transcript function."""

    def test_load_nonexistent_file(self):
        """Test that nonexistent file returns None."""
        result = load_transcript(Path("/nonexistent/path.jsonl"))
        assert result is None

    def test_load_empty_path(self):
        """Test that empty path returns None."""
        result = load_transcript(None)
        assert result is None

    def test_load_nested_message_format(self):
        """Test loading JSONL with nested message format."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            # Standard Claude Code format: {"type": "assistant", "message": {...}}
            event = {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                },
            }
            f.write(json.dumps(event) + "\n")
            f.flush()

            result = load_transcript(Path(f.name))
            assert result is not None
            assert len(result["messages"]) == 1
            assert result["messages"][0]["role"] == "assistant"
            assert result["messages"][0]["content"][0]["text"] == "Hello"

    def test_load_standalone_tool_use_events(self):
        """Test that standalone tool_use events are captured as synthetic messages."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            # Standalone tool_use event (not nested in assistant message)
            tool_use_event = {
                "type": "tool_use",
                "id": "tool_1",
                "name": "Bash",
                "input": {"command": "pytest tests/"},
            }
            f.write(json.dumps(tool_use_event) + "\n")
            f.flush()

            result = load_transcript(Path(f.name))
            assert result is not None
            assert len(result["messages"]) == 1

            # Should be wrapped as synthetic assistant message
            msg = result["messages"][0]
            assert msg["role"] == "assistant"
            assert len(msg["content"]) == 1
            assert msg["content"][0]["type"] == "tool_use"
            assert msg["content"][0]["name"] == "Bash"
            assert msg["content"][0]["input"]["command"] == "pytest tests/"

    def test_load_standalone_tool_result_events(self):
        """Test that standalone tool_result events are captured as synthetic messages."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            # Standalone tool_result event
            tool_result_event = {
                "type": "tool_result",
                "tool_use_id": "tool_1",
                "content": "10 passed in 2.3s",
            }
            f.write(json.dumps(tool_result_event) + "\n")
            f.flush()

            result = load_transcript(Path(f.name))
            assert result is not None
            assert len(result["messages"]) == 1

            # Should be wrapped as synthetic user message
            msg = result["messages"][0]
            assert msg["role"] == "user"
            assert len(msg["content"]) == 1
            assert msg["content"][0]["type"] == "tool_result"
            assert msg["content"][0]["tool_use_id"] == "tool_1"
            assert msg["content"][0]["content"] == "10 passed in 2.3s"

    def test_load_mixed_event_types(self):
        """Test loading transcript with all event types in sequence."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            # Sequence: assistant text -> tool_use -> tool_result -> assistant text
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Running tests..."}],
                    },
                },
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Bash",
                    "input": {"command": "pytest"},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "5 passed",
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Tests passed!"}],
                    },
                },
            ]
            for event in events:
                f.write(json.dumps(event) + "\n")
            f.flush()

            result = load_transcript(Path(f.name))
            assert result is not None
            assert len(result["messages"]) == 4

            # Verify order and types
            assert result["messages"][0]["role"] == "assistant"
            assert result["messages"][0]["content"][0]["type"] == "text"

            assert result["messages"][1]["role"] == "assistant"
            assert result["messages"][1]["content"][0]["type"] == "tool_use"

            assert result["messages"][2]["role"] == "user"
            assert result["messages"][2]["content"][0]["type"] == "tool_result"

            assert result["messages"][3]["role"] == "assistant"
            assert result["messages"][3]["content"][0]["type"] == "text"

    def test_load_carries_event_timestamps_onto_messages(self):
        """Every branch that appends a message copies the event's timestamp;
        an event without one leaves its message without one."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            events = [
                {
                    "type": "assistant",
                    "timestamp": "2026-09-23T10:00:00.000Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
                },
                {"role": "user", "content": "direct", "timestamp": "2026-09-23T10:00:01.000Z"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {},
                    "timestamp": "2026-09-23T10:00:02.000Z",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "ok",
                    "timestamp": "2026-09-23T10:00:03.000Z",
                },
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
                },
            ]
            f.write("\n".join(json.dumps(e) for e in events))
            f.flush()

            messages = load_transcript(Path(f.name))["messages"]

        assert [m.get("timestamp") for m in messages] == [
            "2026-09-23T10:00:00.000Z",
            "2026-09-23T10:00:01.000Z",
            "2026-09-23T10:00:02.000Z",
            "2026-09-23T10:00:03.000Z",
            None,
        ]

    def test_load_carries_event_cwd_onto_messages(self):
        """The shell's directory when the event was written, copied like the
        timestamp; an event without one leaves its message without one."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            events = [
                {
                    "type": "assistant",
                    "cwd": "/p/apps/web/api",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
                },
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}, "cwd": "/p"},
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
                },
            ]
            f.write("\n".join(json.dumps(e) for e in events))
            f.flush()

            messages = load_transcript(Path(f.name))["messages"]

        assert [m.get("cwd") for m in messages] == ["/p/apps/web/api", "/p", None]

    def test_load_skips_api_request_blob_events(self):
        """An `api-request-blob` event nests a message that repeats an earlier one
        (same tool_use ids) without its timestamp or cwd; it is not a message."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            message = {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            }
            events = [
                {
                    "type": "assistant",
                    "timestamp": "2026-09-23T10:00:00.000Z",
                    "cwd": "/p",
                    "message": message,
                },
                {"type": "api-request-blob", "hash": "h1", "message": message},
            ]
            f.write("\n".join(json.dumps(e) for e in events))
            f.flush()

            messages = load_transcript(Path(f.name))["messages"]

        assert messages == [{**message, "timestamp": "2026-09-23T10:00:00.000Z", "cwd": "/p"}]

    def test_load_preserves_tool_use_fields(self):
        """Test that all tool_use fields are preserved when wrapped."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            tool_use_event = {
                "type": "tool_use",
                "id": "toolu_abc123",
                "name": "Edit",
                "input": {
                    "file_path": "/path/to/file.py",
                    "old_string": "foo",
                    "new_string": "bar",
                },
            }
            f.write(json.dumps(tool_use_event) + "\n")
            f.flush()

            result = load_transcript(Path(f.name))
            content = result["messages"][0]["content"][0]

            # All original fields should be preserved
            assert content["type"] == "tool_use"
            assert content["id"] == "toolu_abc123"
            assert content["name"] == "Edit"
            assert content["input"]["file_path"] == "/path/to/file.py"
            assert content["input"]["old_string"] == "foo"
            assert content["input"]["new_string"] == "bar"
