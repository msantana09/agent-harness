"""Read Claude Code session transcripts: which records count, who typed what, and when.

Holds the record and typed-message rules both scripts share, and one `load()` that finds the
files, reads the window and reports what it could not use. Imported by scan.py and
corrections.py; it prints nothing to stdout and makes no model calls.

    import transcripts
    loaded = transcripts.load(args, "scan.py", subagents=True)

Inputs: the JSONL transcripts under each root (default <config dir>/projects), laid out as
<project>/<session>.jsonl, with subagent files under <project>/<session>/subagents/.
Outputs: `Transcript` records, and on stderr one line for lines and records it could not read.
`load()` returns None when a given root is not a folder or no transcript exists under any root;
the caller exits `NO_TRANSCRIPTS` (2).

How the counts are made:

* A record is in the window by its own timestamp. A file's modification time only decides which
  files are worth opening, so a session left running for days adds just its recent records and
  a file that was merely touched adds nothing.
* A session is a main transcript file with at least one in-window user or assistant record of its
  own. Records without a timestamp (titles, bookkeeping) never make a session active.
* A forked, resumed or backgrounded session's file repeats records of the session it came from,
  and each repeated record counts once. A record carrying `forkedFrom` is a copy and counts under
  its source. An unmarked copy carries nothing that names its source (its `sessionId` is
  rewritten to the copying session), so it counts under the file read first: files are read by
  first timestamp (modification time when they have none), then by path. A copy that starts at
  the same instant as its source can take the shared records, and a source holding nothing else
  then shows no activity of its own.
* A typed message is a user turn a person typed. Skill bodies, command expansions, notices,
  compaction summaries, subagent prompts, messages from other sessions and plugins, and resends
  (a message right after an API error with the same words as the last one typed, even when that
  one is before the window) are not. Prompts a
  person queued during a running turn are, and so is text a person pasted into a message, with
  its paste markers taken off. Words typed into a tool result are too: feedback
  given when rejecting a tool call, and free-text answers and notes to a question the assistant
  asked (picking an offered option is not).
* A hook block is named by its event and the start of its message's first line
  (`Stop: Run the tests first`), so two rules checked by one script stay apart. Quoted spans,
  paths and numbers in the message are masked so one rule keeps one name; the message's quoted
  spans are kept beside the name, so the patterns one rule checks can still be counted apart. The
  message is the name because Claude Code often records a hook's command only as `command hook 1`.
* A window with `--until` ends at the midnight after that date. `--days N` counts back from the
  window's end: N whole days through `--until`, or N times 24 hours before now without it.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

NO_TRANSCRIPTS = 2
FORK_MARKER = "forkedFrom"
INTERRUPT_TEXT = "[Request interrupted by user]"
REJECTED_WITHOUT_COMMENT = "User rejected tool use"
# toolUseResult of a tool call the user rejected with feedback; without feedback the text ends
# "wait for the user to tell you how to proceed." instead.
REJECTION_WITH_FEEDBACK = re.compile(
    r"(?:Error: )?The user doesn't want to proceed with this tool use\..*?"
    r"To tell you how to proceed, the user said:\n(.*)",
    re.DOTALL,
)
# A side agent's note as it reaches the input box: this line, then its quoted lines. Anything
# else in the same send was typed by the person.
SIDE_AGENT_NOTE = re.compile(r"^Here is a note offered by a side agent:\n+(?:>.*(?:\n+|\Z))+", re.M)
COMMAND_NAME = re.compile(r"<command-name>/?([^<\s]+)</command-name>")
SKILL_FORMAT_TAG = "<skill-format>true</skill-format>"
# A hook that blocked a tool call shows up in the tool result as "PreToolUse:Bash hook error: ...".
TOOL_HOOK_ERROR = re.compile(r"(?:Error: )?(\w+:\S+) hook error:?(.*)", re.DOTALL)
HOOK_MESSAGE_CHARS = 60
# A quote mark inside a word (the apostrophe in "project's") neither opens nor closes a span.
QUOTED_SPAN = re.compile(r"`[^`\n]*`|(?<!\w)'[^'\n]*'(?!\w)|(?<!\w)\"[^\"\n]*\"(?!\w)")
BARE_PATH = re.compile(r"(?<!\S)([(\[{<'\"`]*)\S*/\S*?([,.:;)\]}>'\"`]*)(?!\S)")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
MASK = "…"
IMAGE_STUB = re.compile(r"\[Image[^\]]*\]")
# Claude Code wraps text a person pasted into a message in these markers.
PASTE_MARKER = re.compile(r'</?pasted_content id="\w+">')
# Text a person did not type that no record flag marks.
TEXT_NOISE = (
    ("interrupt-stub", re.compile(r"\[Request interrupted")),
    ("continuation-summary", re.compile(r"This session is being continued")),
    ("skill-load", re.compile(r"Base directory for this skill")),
    ("classifier-notice", re.compile(r"Your response above was stopped by a safety classifier")),
    ("slash-command", re.compile(r"/[\w:-]+(\s.*)?$", re.S)),
    ("tag-markup", re.compile(r"<")),
)
CALL_HEAD_CHARS = 300
DAY_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}")

type Record = dict[str, Any]


def message_content(record: Record) -> Any:
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def message_text(record: Record) -> str:
    """The text blocks of one record's message, joined and stripped ("" if none)."""
    content = message_content(record)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        blocks = (
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        return " ".join(blocks).strip()
    return ""


def typed_words(text: str) -> str:
    """`text` without the side agent's notes in it, and with the markers around text the person
    pasted taken off (the pasted text stays: the person chose to send it)."""
    return re.sub(r" {2,}", " ", PASTE_MARKER.sub("", SIDE_AGENT_NOTE.sub("", text))).strip()


def queued_prompt_as_user_record(record: Record) -> Record:
    """`record`, unless it holds a prompt a person sent during a running turn.

    Claude Code delivers such a prompt as a queued_command attachment and writes no user record
    for it, so it is rewritten here as one.
    """
    attachment = record.get("attachment") or {}
    origin = attachment.get("origin") or {}
    if attachment.get("type") != "queued_command" or origin.get("kind") != "human":
        return record
    return {
        **record,
        "type": "user",
        "origin": origin,
        "message": {"content": attachment.get("prompt")},
    }


def local_command_reply(record: Record) -> bool:
    """True for the "No response requested." row the harness appends after a local command.

    It shares the model name "<synthetic>" with API-error rows, which carry isApiErrorMessage.
    """
    message = record.get("message")
    return (
        record.get("type") == "assistant"
        and isinstance(message, dict)
        and message.get("model") == "<synthetic>"
        and not record.get("isApiErrorMessage")
    )


def injected_reason(record: Record) -> str | None:
    """Why this user record was not typed by a person, or None when it may have been.

    Skill bodies, command expansions and harness notices are isMeta records; the other flags
    cover compaction summaries, subagent prompts and traffic from SDK callers, other sessions and
    plugins. A side agent's note carries the flags of a typed prompt, so only its words tell it
    apart: a record holding nothing but notes is not typed, and one that also holds typed words is.
    """
    if record.get("isMeta"):
        return "meta"
    if record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
        return "transcript-only"
    if record.get("isSidechain"):
        return "sidechain"
    if record.get("promptSource") == "sdk":
        return "sdk-prompt"
    origin = (record.get("origin") or {}).get("kind")
    if origin not in (None, "human"):
        return f"origin-{origin}"
    text = message_text(record)
    if text and not typed_words(text):
        return "side-agent-note"
    return None


def untyped_reason(record: Record, words: str) -> str | None:
    """Why `words` of this user record were not typed by a person, or None when they were."""
    reason = injected_reason(record)
    if reason:
        return reason
    for reason, pattern in TEXT_NOISE:
        if pattern.match(words):
            return reason
    return None


def wordless_stop(record: Record) -> str | None:
    """ "rejection" or "interrupt" when the user stopped a turn without typing anything."""
    if record.get("type") != "user":
        return None
    if record.get("toolUseResult") == REJECTED_WITHOUT_COMMENT:
        return "rejection"
    return "interrupt" if message_text(record) == INTERRUPT_TEXT else None


def tool_calls(record: Record) -> dict[str, str]:
    """{tool_use id: "Name: command or path"} for the tool calls in one assistant record."""
    content = message_content(record)
    calls = {}
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            args = block.get("input") or {}
            head = args.get("command") or args.get("file_path") or json.dumps(args)
            calls[block.get("id") or ""] = f"{block.get('name')}: {head}"[:CALL_HEAD_CHARS]
    return calls


def user_answers(record: Record, calls: dict[str, str]) -> list[tuple[str, str, bool]]:
    """The user's own words inside one tool-result record, as (context, text, typed) triples.

    Rejection feedback is typed, with the rejected call from `calls` as context. An answer to a
    question comes with the question as context and is typed unless it only picks offered option
    labels; a note on an answer is typed, with "question -> answer" as context. Any other record
    gives [].
    """
    result = record.get("toolUseResult")
    if isinstance(result, str):
        rejected = REJECTION_WITH_FEEDBACK.match(result)
        feedback = rejected.group(1).strip() if rejected else ""
        if not feedback:
            return []
        content = message_content(record)
        blocks = content if isinstance(content, list) else []
        ids = [b.get("tool_use_id") or "" for b in blocks if isinstance(b, dict)]
        return [(calls.get(ids[0], "") if ids else "", feedback, True)]
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        return []
    labels = {
        q.get("question"): [o.get("label") or "" for o in q.get("options") or []]
        for q in result.get("questions") or []
    }
    notes = result.get("annotations") or {}
    triples = []
    for question, answer in result["answers"].items():
        rest = str(answer)
        for label in sorted(labels.get(question, []), key=len, reverse=True):
            rest = rest.replace(label, "") if label else rest
        triples.append((question, str(answer), bool(rest.strip(" ,\n"))))
        note = (notes.get(question) or {}).get("notes")
        if note:
            triples.append((f"{question} -> {answer}", note, True))
    return triples


def skill_load(record: Record) -> tuple[str, str] | None:
    """(skill name, how it loaded) when one user record loads a skill, else None.

    A Skill tool call leaves a tool result naming the skill ("tool"); a skill typed as a slash
    command leaves a command record ("typed"); a skill an agent definition preloads leaves a
    meta record tagged skill-format ("preloaded"). Built-in slash commands look like typed skills.
    """
    result = record.get("toolUseResult")
    if isinstance(result, dict) and result.get("commandName"):
        return (result["commandName"], "tool") if result.get("success") is not False else None
    text = message_text(record)
    name = COMMAND_NAME.search(text)
    if not name or not text.startswith("<command-"):
        return None
    if record.get("isMeta"):
        return (name.group(1), "preloaded") if SKILL_FORMAT_TAG in text else None
    return name.group(1), "typed"


def mask_variable_parts(text: str) -> str:
    """`text` with each quoted span, path and number replaced by `…`. A path is any
    whitespace-delimited word holding a slash (its surrounding brackets and punctuation kept)."""
    masked = BARE_PATH.sub(rf"\1{MASK}\2", QUOTED_SPAN.sub(MASK, text))
    return re.sub(r" {2,}", " ", NUMBER.sub(MASK, masked))


def first_line(message: str) -> str:
    lines = message.strip().splitlines()
    return lines[0].strip() if lines else ""


def hook_block_name(event: str, message: str) -> str:
    """`event` and the start of the first line of the hook's message, its quoted spans, paths
    and numbers masked."""
    first = mask_variable_parts(first_line(message))[:HOOK_MESSAGE_CHARS].rstrip()
    return f"{event}: {first}" if first else event


def quoted_parts(message: str) -> tuple[str, ...]:
    """Each distinct quoted span anywhere in the hook's message, without its quote marks: the
    patterns, files and commands that one block was about, and any the rule's own text quotes."""
    return tuple(sorted({found.group()[1:-1] for found in QUOTED_SPAN.finditer(message)}))


def blocking_hooks(record: Record) -> list[tuple[str, tuple[str, ...]]]:
    """(name, quoted parts) of each hook that blocked something, as this record shows it.

    Claude Code writes a hook_blocking_error attachment for a blocking hook such as a Stop hook,
    with its event and message. A blocked tool call instead returns an error tool result that
    starts "<Event>:<Tool> hook error: <message>".
    """
    attachment = record.get("attachment") or {}
    if attachment.get("type") == "hook_blocking_error":
        error = attachment.get("blockingError")
        message = (error.get("blockingError") if isinstance(error, dict) else error) or ""
        event = attachment.get("hookEvent") or attachment.get("hookName") or "hook"
        message = message if isinstance(message, str) else ""
        return [(hook_block_name(event, message), quoted_parts(message))]
    content = message_content(record) if record.get("type") == "user" else None
    blocks = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
            found = TOOL_HOOK_ERROR.match(tool_result_text(block))
            if found:
                event, message = found.group(1), found.group(2)
                blocks.append((hook_block_name(event, message), quoted_parts(message)))
    return blocks


def tool_result_text(block: Record) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


@dataclass(frozen=True)
class Window:
    start: float  # epoch seconds
    end: float | None  # epoch seconds, excluded; None for no end
    label: str

    def contains(self, epoch: float) -> bool:
        return epoch >= self.start and (self.end is None or epoch < self.end)


def make_window(days: int, since: str | None, until: str | None = None) -> Window:
    """Everything from local midnight of `since` (YYYY-MM-DD), or else the `days` days before the
    window's end. The end is the end of `until` (YYYY-MM-DD) when given, so `days` then counts
    whole days ending on that date, and otherwise now."""
    if until is None:
        start = datetime.fromisoformat(since).timestamp() if since else time.time() - days * 86400
        return Window(start, None, f"since {since}" if since else f"last {days} days")
    end_day = datetime.fromisoformat(until) + timedelta(days=1)
    if since:
        return Window(
            datetime.fromisoformat(since).timestamp(),
            end_day.timestamp(),
            f"{since} through {until}",
        )
    start = (end_day - timedelta(days=days)).timestamp()
    return Window(start, end_day.timestamp(), f"{days} days through {until}")


def record_epoch(record: Record) -> float | None:
    """The record's own timestamp as epoch seconds, None when it has no readable one."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def local_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).date().isoformat()


def config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def resolve_roots(given: list[str] | None) -> list[Path]:
    """The transcript folders to read: those given, else <config dir>/projects."""
    return [Path(r).expanduser() for r in given] if given else [config_dir() / "projects"]


def find_transcripts(roots: list[Path]) -> tuple[list[Path], list[Path]]:
    """(main session files, subagent files) under the roots, each file once.

    A file reachable twice (overlapping roots, a linked subagent file) comes once.
    """

    def unique(pattern: str) -> list[Path]:
        found = sorted(f for root in roots for f in root.glob(pattern))
        return sorted({f.resolve(): f for f in reversed(found)}.values())

    return unique("*/*.jsonl"), unique("*/*/subagents/**/*.jsonl")


def first_epoch(path: Path) -> float:
    """The file's first record timestamp; its modification time when no record has one."""
    with path.open(errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            epoch = record_epoch(record) if isinstance(record, dict) else None
            if epoch is not None:
                return epoch
    return path.stat().st_mtime


@dataclass
class Stats:
    """What the readers could not use, so a quiet report is never mistaken for a quiet window."""

    bad_lines: int = 0  # lines that were not valid JSON
    unreadable: int = 0  # user or assistant records with no usable timestamp or message
    dropped: Counter[str] = field(default_factory=Counter)  # untyped user turns, by reason


@dataclass
class Typed:
    text: str  # what the person typed
    context: str  # the assistant text, rejected call or question it answers
    epoch: float


@dataclass
class Request:
    model: str
    usage: dict[str, Any]
    in_window: bool

    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        """(model, usage) of each model call the request made.

        The top-level usage covers the request's own model, summed over its `message`
        iterations. An `advisor_message` iteration is a separate call to its own model, and its
        tokens are not in the top-level usage.
        """
        iterations = self.usage.get("iterations")
        if not isinstance(iterations, list):
            iterations = []
        advisors = [
            (item.get("model") or "?", item)
            for item in iterations
            if isinstance(item, dict) and item.get("type") == "advisor_message"
        ]
        return [(self.model, self.usage), *advisors]


@dataclass
class Transcript:
    path: Path
    session: str  # first 8 characters of the owning session's id (see session_of)
    cwd: str = ""
    first_epoch: float | None = None  # earliest and latest in-window message of its own
    last_epoch: float | None = None
    typed: list[Typed] = field(default_factory=list)
    stops: Counter[str] = field(default_factory=Counter)  # "interrupt" / "rejection"
    # (name, quoted parts) of each hook block
    hook_blocks: Counter[tuple[str, tuple[str, ...]]] = field(default_factory=Counter)
    skill_loads: list[tuple[str, str]] = field(default_factory=list)
    requests: dict[str, Request] = field(default_factory=dict)
    uuids: set[str] = field(default_factory=set)  # in-window records this file wrote itself

    @property
    def active(self) -> bool:
        return self.first_epoch is not None

    @property
    def project(self) -> str:
        return Path(self.cwd).name or self.path.parent.name

    def touch(self, epoch: float) -> None:
        self.first_epoch = epoch if self.first_epoch is None else min(self.first_epoch, epoch)
        self.last_epoch = epoch if self.last_epoch is None else max(self.last_epoch, epoch)


class Conversation:
    """What the assistant last said, which gives the next typed message its context, and what
    the person last typed, which tells a resend from a new message."""

    def __init__(self) -> None:
        self.previous_text = ""
        self.after_api_error = False  # the assistant's last turn was an API error
        self.last_typed = ""
        self.calls: dict[str, str] = {}

    def see_assistant(self, record: Record) -> None:
        self.calls.update(tool_calls(record))
        self.after_api_error = bool(record.get("isApiErrorMessage"))
        text = message_text(record)
        if text:
            self.previous_text = text

    def said(self, record: Record) -> list[tuple[str, str]]:
        """(context, words) for what the user said in one user record."""
        if record.get("toolUseResult") is not None:
            answers = user_answers(record, self.calls)
            return [(context, words) for context, words, typed in answers if typed]
        text = message_text(record)
        return [(self.previous_text, typed_words(text))] if text else []

    def see_user(self, record: Record) -> list[tuple[str, str, str | None]]:
        """(context, words, the reason it was not typed) for what one user record says. A typed
        message, whose reason is None, becomes the last thing typed, whether or not the record
        is counted, so a resend just inside a window is still known for one."""
        found = []
        for context, words in self.said(record):
            reason = untyped_reason(record, words)
            if reason is None and self.after_api_error and words == self.last_typed:
                reason = "queued-resend"
            if reason is None and not IMAGE_STUB.sub("", words).strip():
                reason = "image-only"
            if reason is None:
                self.last_typed = words
            found.append((context, words, reason))
        return found


def iter_records(path: Path, stats: Stats) -> Iterator[Record]:
    with path.open(errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                stats.bad_lines += bool(line.strip())
                continue
            if isinstance(record, dict):
                yield record
            else:
                stats.bad_lines += 1


def read_transcript(
    path: Path, window: Window, seen: set[str], stats: Stats, subagent: bool = False
) -> Transcript:
    """Read one file once. `seen` holds the uuids of in-window records of earlier files.

    Only the main transcript of a session has activity, interrupts and typed messages; a subagent
    file contributes hook blocks, skill loads and spend.
    """
    transcript = Transcript(path, session_of(path, subagent))
    talk = Conversation()
    for raw in iter_records(path, stats):
        record = queued_prompt_as_user_record(raw)
        kind = record.get("type")
        epoch = record_epoch(record)
        in_window = epoch is not None and window.contains(epoch)
        if kind in ("user", "assistant") and (
            epoch is None or (in_window and not isinstance(message_content(record), (str, list)))
        ):
            stats.unreadable += 1
        transcript.cwd = transcript.cwd or record.get("cwd") or ""
        if kind == "assistant":
            if FORK_MARKER not in record:
                note_request(transcript, record, in_window)
            if not local_command_reply(record):
                talk.see_assistant(record)
        stop = wordless_stop(record) if kind == "user" else None
        said = talk.see_user(record) if kind == "user" and not subagent and not stop else []
        uuid = record.get("uuid")
        if epoch is None or not in_window or FORK_MARKER in record or uuid in seen:
            continue
        if uuid:
            transcript.uuids.add(uuid)
        transcript.hook_blocks.update(blocking_hooks(record))
        if kind == "user":
            load = skill_load(record)
            if load:
                transcript.skill_loads.append(load)
        if subagent or kind not in ("user", "assistant"):
            continue
        transcript.touch(epoch)
        if stop:
            transcript.stops[stop] += 1
        for context, words, reason in said:
            if reason:
                stats.dropped[reason] += 1
            else:
                transcript.typed.append(Typed(IMAGE_STUB.sub("", words).strip(), context, epoch))
    return transcript


def session_of(path: Path, subagent: bool) -> str:
    """The first 8 characters of the session id that owns the file.

    A main file is named for its session. A subagent file sits under
    <project>/<session>/subagents/, so it belongs to that folder's session.
    """
    if subagent:
        folder = next((p for p in path.parents if p.name == "subagents"), None)
        if folder is not None:
            return folder.parent.name[:8]
    return path.stem[:8]


def note_request(transcript: Transcript, record: Record, in_window: bool) -> None:
    """Keep the model call's usage; a later line of the same request replaces an earlier one."""
    message = record.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(usage, dict):
        return
    if message.get("model") == "<synthetic>":
        return
    request_id = record.get("requestId") or message.get("id") or record.get("uuid")
    if request_id:
        transcript.requests[request_id] = Request(message.get("model") or "?", usage, in_window)


def read_transcripts(
    main_files: list[Path], subagent_files: list[Path], window: Window, stats: Stats
) -> tuple[list[Transcript], list[Transcript]]:
    """(main transcripts in reading order, subagent transcripts) that may hold window records."""

    def possible(files: list[Path]) -> list[Path]:
        return [f for f in files if f.stat().st_mtime >= window.start]

    seen: set[str] = set()
    mains = []
    for path in sorted(possible(main_files), key=lambda p: (first_epoch(p), str(p))):
        transcript = read_transcript(path, window, seen, stats)
        seen |= transcript.uuids
        mains.append(transcript)
    subs = [
        read_transcript(p, window, set(), stats, subagent=True) for p in possible(subagent_files)
    ]
    return mains, subs


def positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError:
        days = 0
    if days < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of days above 0")
    return days


def day_argument(value: str) -> str:
    # date.fromisoformat also takes the compact form (20990602), which the help does not offer.
    if DAY_FORMAT.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")


def add_window_arguments(parser: argparse.ArgumentParser) -> None:
    span = parser.add_mutually_exclusive_group()
    span.add_argument(
        "--days", type=positive_days, default=30, help="look back this many days (default 30)"
    )
    span.add_argument(
        "--since",
        type=day_argument,
        metavar="YYYY-MM-DD",
        help="look back to local midnight of this date",
    )
    parser.add_argument(
        "--until",
        type=day_argument,
        metavar="YYYY-MM-DD",
        help="stop at the end of this date (default: now)",
    )
    parser.add_argument(
        "--root",
        action="append",
        metavar="DIR",
        help="transcript folder holding <project>/<session>.jsonl; repeat to combine folders "
        "(default: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects)",
    )


def parse_window_arguments(
    parser: argparse.ArgumentParser, argv: list[str] | None
) -> argparse.Namespace:
    """`parser.parse_args(argv)`, refusing an --until date before the --since date."""
    args = parser.parse_args(argv)
    if args.since and args.until and args.until < args.since:
        parser.error(f"--until {args.until} is before --since {args.since}")
    return args


@dataclass
class Loaded:
    roots: list[Path]
    window: Window
    mains: list[Transcript]  # in reading order
    subs: list[Transcript]
    stats: Stats


def load(args: argparse.Namespace, program: str, subagents: bool) -> Loaded | None:
    """Read the window `args` names; None, after one stderr line, when a root is not a folder or
    no transcript exists.

    `args` comes from `parse_window_arguments`. Subagent files are read only when
    `subagents` is set. Lines and records that could not be read are counted on stderr.
    """
    roots = resolve_roots(args.root)
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        print(f"{program}: no transcript folder at {', '.join(missing)}", file=sys.stderr)
        return None
    main_files, subagent_files = find_transcripts(roots)
    if not main_files:
        where = ", ".join(str(r) for r in roots)
        print(f"{program}: no transcripts found under {where}", file=sys.stderr)
        return None
    window = make_window(args.days, args.since, args.until)
    stats = Stats()
    mains, subs = read_transcripts(main_files, subagent_files if subagents else [], window, stats)
    if stats.bad_lines or stats.unreadable:
        print(
            f"{program}: skipped {stats.bad_lines} lines that were not valid JSON and "
            f"{stats.unreadable} user or assistant records with no readable timestamp or message",
            file=sys.stderr,
        )
    return Loaded(roots, window, mains, subs, stats)
