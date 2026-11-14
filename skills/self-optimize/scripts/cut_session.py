#!/usr/bin/env python3
"""Cut a session transcript just before one message the user typed, for an eval case that
resumes the session there and grades the agent's next reply.

    python3 cut_session.py --session <id> --before "<start of the message>" --out <path>
                           [--root DIR ...]

Inputs: the main transcript `<session id>.jsonl` under each `--root` (default
$CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects), and the start of the typed message to cut
before, as the transcripts skill prints it.
Outputs: the records before that message, written as JSONL to `--out`, for a case's
`context.history_file`. The message itself, whole, on stdout, for the case's prompt. On stderr,
how many records were kept and how many were dropped as standing context.

Exit codes: 0 success; 2 no transcript or more than one for the session, no typed message
starting with the text or more than one, or invalid arguments.

How the cut is made:

* Only messages the user typed can be cut before, a prompt sent during a running turn included;
  a tool result, skill body, command expansion or notice is never one. A match compares the start
  of the words the user typed, without a side agent's note, with the given text, runs of
  whitespace treated as one space. The message printed is the whole message, note included.
* Standing context is dropped: the instruction files Claude Code loaded (`CLAUDE.md`, rules,
  nested ones included), the memories it recalled, the saved system prompt, the memory and
  session context sections, the output style, the listings
  of skills, agents, MCP servers and deferred tools, and the logged API requests. With them kept,
  the resumed agent reads the old text of the file under test and never sees the text a case
  appends, so both versions of a case run the same; the listings name tools the eval sandbox
  does not have.
* Everything else stays as it was: tool results, skill and command bodies loaded during the
  session, and text hooks added.
* A dropped record's children are re-attached to its parent, and compaction boundaries stop
  naming it, so the conversation stays one unbroken chain for the resume to walk.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "transcripts" / "scripts"))
import transcripts  # noqa: E402

STANDING_ATTACHMENTS = {
    "instructions",
    "nested_memory",
    "relevant_memories",
    "prompt_snapshot",
    "context_sections",
    "session_context",
    "output_style_instructions",
    "skill_listing",
    "agent_listing_delta",
    "mcp_instructions_delta",
    "deferred_tools_delta",
    "deferred_tools_record",
}
CANDIDATES_SHOWN = 5

type Record = dict[str, Any]


def flat(text: str) -> str:
    return " ".join(text.split())


def typed_messages(records: list[Record]) -> list[tuple[int, str, str]]:
    """(index, the message whole, the words a person typed in it) for each typed message.

    A message Claude Code sent again after an API error is the same message, not a second one.
    """
    conversation = transcripts.Conversation()
    typed = []
    for index, raw in enumerate(records):
        record = transcripts.queued_prompt_as_user_record(raw)
        if record.get("type") == "assistant":
            conversation.see_assistant(record)
        if record.get("type") != "user" or record.get("toolUseResult") is not None:
            continue
        for _, words, reason in conversation.see_user(record):
            if reason is None:
                typed.append((index, transcripts.message_text(record), words))
    return typed


def standing(record: Record) -> bool:
    kind = str(record.get("type", ""))
    attachment = record.get("attachment") or {}
    return kind.startswith("api-request") or (
        kind == "attachment" and attachment.get("type") in STANDING_ATTACHMENTS
    )


def kept_uuid(uuid: Any, parent_of: dict[str, Any]) -> Any:
    """`uuid`, or the nearest kept record above it when it was dropped."""
    while uuid in parent_of:
        uuid = parent_of[uuid]
    return uuid


def forget_dropped(metadata: Record, parent_of: dict[str, Any]) -> None:
    """Take dropped records out of a compaction boundary's preserved messages and segment.

    The resume rebuilds the conversation kept across a compaction from these lists; one naming a
    record the history no longer holds breaks that rebuild and loses the preserved turns.
    """
    preserved = metadata.get("preservedMessages")
    if isinstance(preserved, dict):
        for key in ("uuids", "allUuids"):
            if isinstance(preserved.get(key), list):
                preserved[key] = [u for u in preserved[key] if u not in parent_of]
        if "anchorUuid" in preserved:
            preserved["anchorUuid"] = kept_uuid(preserved["anchorUuid"], parent_of)
    segment = metadata.get("preservedSegment")
    if isinstance(segment, dict):
        remaining = preserved.get("uuids") if isinstance(preserved, dict) else None
        if segment.get("headUuid") in parent_of and remaining:
            segment["headUuid"] = remaining[0]
        for key in ("headUuid", "anchorUuid", "tailUuid"):
            if key in segment:
                segment[key] = kept_uuid(segment[key], parent_of)


def cut(records: list[Record], before: str) -> tuple[list[Record], str, int]:
    """(the records kept, the matched message, how many were dropped as standing context).

    Raises ValueError naming the candidates when no typed message, or more than one, starts with
    `before`.
    """
    wanted = flat(before)
    typed = typed_messages(records)
    matches = [found for found in typed if flat(found[2]).startswith(wanted)]
    if len(matches) != 1:
        shown = [words for _, _, words in matches or typed]
        listing = "\n".join(f"  {flat(words)[:100]}" for words in shown[:CANDIDATES_SHOWN])
        problem = "more than one typed message starts" if matches else "no typed message starts"
        raise ValueError(f"{problem} with that text; candidates:\n{listing}")
    index, message, _ = matches[0]
    parent_of: dict[str, Any] = {}
    kept: list[Record] = []
    for record in records[:index]:
        if standing(record):
            if record.get("uuid"):
                parent_of[record["uuid"]] = record.get("parentUuid")
            continue
        kept.append(record)
    for record in kept:
        if "parentUuid" in record:
            record["parentUuid"] = kept_uuid(record["parentUuid"], parent_of)
        if isinstance(record.get("compactMetadata"), dict):
            forget_dropped(record["compactMetadata"], parent_of)
    return kept, message, index - len(kept)


def find_session(roots: list[Path], session: str) -> Path:
    found = sorted({path.resolve() for root in roots for path in root.glob(f"*/{session}.jsonl")})
    if len(found) != 1:
        problem = "no transcript" if not found else "more than one transcript"
        raise ValueError(f"{problem} for session {session}: {[str(p) for p in found]}")
    return found[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cut a session before one typed message.")
    parser.add_argument("--session", required=True, help="the session id")
    parser.add_argument("--before", required=True, help="start of the typed message to cut at")
    parser.add_argument("--out", required=True, type=Path, help="where to write the history")
    parser.add_argument("--root", action="append", help="a transcript folder (repeatable)")
    args = parser.parse_args(argv)
    stats = transcripts.Stats()
    try:
        path = find_session(transcripts.resolve_roots(args.root), args.session)
        kept, message, dropped = cut(list(transcripts.iter_records(path, stats)), args.before)
    except ValueError as error:
        print(f"cut_session: {error}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(record) + "\n" for record in kept))
    print(message)
    print(f"kept {len(kept)} records, dropped {dropped} as standing context", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
