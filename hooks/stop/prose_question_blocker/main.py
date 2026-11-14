#!/usr/bin/env python3
"""Stop hook: block a final reply that asks the user a prose question, offers to do an in-scope
action instead of doing it, announces an action that never launched, or uses blockquote bars.

Usage: registered under "Stop" in settings.json; Claude Code pipes the Stop payload to stdin.

Input: the Stop payload JSON. Fields read: `last_assistant_message` (the turn's final reply),
`stop_hook_active` (true on the redo turn this hook caused), `session_id`, `cwd` and
`transcript_path` (read only when `cwd` carries the per-project opt-out below).

Output: `{}` on stdout to allow the stop, or `{"decision": "block", "reason": ...}` with one
reason per failed check. Exit code is 0 for any JSON-object payload; a JSON value that is not an
object raises (exit 1). A payload that is not JSON is allowed. A payload whose
`last_assistant_message` is absent or not a string is allowed too, with a
`last-assistant-message-missing` line on stderr, so a renamed payload field is visible instead
of failing open silently.

Questions to the user go through the AskUserQuestion tool, never prose. The prose-question check
looks for a `?` followed by whitespace or end of text, after stripping code fences, inline code
and markdown table rows, so a `?` inside an identifier, a query string or example code never
counts. The final reply is read from the payload only: the transcript is written asynchronously
and often lacks the final reply at Stop time.

Bypass: the marker `<!-- ask-ok -->` anywhere in the message bypasses all four checks.

Per-project opt-out: a file named `.claude/prose-questions-ok` in the session's cwd turns off the
prose-question, announcement and blockquote checks for that project (documentation repos where
drafted copy legitimately contains questions, trailing colons or blockquote bars), while the
session's work is in that project. The hand-back check stays on: an offer at the end of the reply
is the agent's own, and drafted copy can carry the marker.

Where the work is: the session's most recent tool call that says, found by reading its transcript
and its subagents' transcripts backwards from their ends. An Edit or Write `file_path` or a
NotebookEdit `notebook_path` says so wherever it points. A Bash command says so only when it ran
in, or names, a folder outside cwd: the folder the transcript records for the call, or a
`cd <path>`, `pushd <path>`, `git -C <path>`, `make -C <path>`, `npm --prefix <path>`,
`npx --prefix <path>` or `uv --directory <path>` at command position. A command run inside cwd
does not bring the opt-out back; an edit there does. Relative paths resolve against the folder
recorded for the call (cwd when none is), or against the directory an earlier `cd` in the same
command moved to. A Bash path the shell would have to expand (a variable, a command
substitution, a glob, `cd -`) names nothing, and neither does text in a heredoc body. Paths under
the system temp folders (/tmp, /private/tmp, TMPDIR) or the Claude config folder
(CLAUDE_CONFIG_DIR, else ~/.claude) say nothing about the project, so those calls are passed
over, as are lines that are not valid JSON. The last located call inside cwd keeps the opt-out;
one outside it runs every check. No transcript, one that cannot be read, or no located call in
the last 32 MiB of each keeps the opt-out, since the marker's project is then the only evidence.

Loop protection: on the redo turn (`stop_hook_active` true) the hand-back, announcement and
blockquote checks do not block again, since the agent already complied once. The prose-question
check re-checks every turn.

Three further checks share this hook (same payload field, same block plumbing):

- Hand-back offers: the final paragraph offers to do something ("say the word", "happy to X if",
  "I can also X if") or waits for a go ("once you say go", "I'm holding here") instead of doing
  it. Skipped after a bare "stop" (the session's stop flag, read through lib_shared/stop_flag.py),
  where holding is what the user asked for. Exempt: code fences, quoted spans and blockquoted text
  (straight and curly quotes, so a phrase discussed in prose never counts as the agent's own
  offer), the `Flags:`/`Next:` report template lines (plain or bulleted/numbered), and lines
  naming a genuinely human-gated action (terraform apply/destroy/import, prod/uat deploys,
  promotion-PR merges, spending money). Wait wording is also exempt on a line naming a merge, a
  migration or a delete, which are the user's calls.
- Unlaunched announcements: the last non-empty line of the raw message is prose ending in `:`
  ("Running the check on this commit:"). A Stop means no tool call followed it, so the announced
  action never started, yet a recap and the user read the line as work in flight. Exempt: a last
  line that is inside or closes a code fence ("Verify with:" plus a fenced command is the correct
  ending), a table row, or a `Done:`/`Found:`/`Flags:`/`Next:` template line (plain or
  bulleted/numbered/bold). A list lead-in followed by its items is never the last line, so it
  needs no exemption.
- Blockquote bars: any markdown blockquote line (`>` at line start, outside code fences) that is
  not a real quotation (content starting with a quote mark right after `> `).
"""

import json
import os
import re
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_shared"))

from stop_flag import flag_is_set, flag_path  # noqa: E402

BYPASS_MARKER = "<!-- ask-ok -->"
PROJECT_OPT_OUT = Path(".claude") / "prose-questions-ok"


def project_opted_out(cwd: str) -> bool:
    return bool(cwd) and (Path(cwd) / PROJECT_OPT_OUT).is_file()


# Each transcript is read backwards from its end, at most this many bytes; a transcript whose
# last located call is further back counts as having none.
TRANSCRIPT_SCAN_BYTES = 32 * 1024 * 1024
TRANSCRIPT_CHUNK_BYTES = 1024 * 1024
# Fixed system temp folders; TMPDIR is read at call time beside them.
SYSTEM_TEMP_DIRS: tuple[str, ...] = ("/tmp", "/private/tmp")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
TOOL_USE_BYTES_RE = re.compile(rb'"type":\s*"tool_use"')
# What can go wrong reading a record nobody promised the shape of.
UNREADABLE = (OSError, ValueError, TypeError, RecursionError)

# A heredoc with its body: the body is text the command passes on, not commands it runs.
HEREDOC_RE = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)

# A directory a Bash command moves to or names, each command at command position (start of the
# command, after `&&`, `||`, `;`, `|`, `(`, `{`, a newline or a keyword such as `if` or `then`,
# with any `NAME=value` settings in front): `cd <path>` or `pushd <path>`, `git -C <path>` (among
# git's own options, before the subcommand, so `git grep -C 3` is not one), `make -C <path>`,
# `npm --prefix <path>` or `npx --prefix <path>`, and `uv --directory <path>`.
_COMMAND_START = r"(?:^|[;&|({\n]|\b(?:if|then|elif|else|while|until|do)\b)[ \t]*(?:\w+=\S*[ \t]+)*"
_WORD = r"[^\s;&|()]+"
_TOKEN = r"\"[^\"]*\"|'[^']*'|" + _WORD
_PATH_TOKEN = "(" + _TOKEN + ")"
_FLAG_VALUE = r"(?:=|[ \t]+)"
MOVE_RE = re.compile(_COMMAND_START + r"(?:cd|pushd)[ \t]+(?:--[ \t]+)?" + _PATH_TOKEN)
DIRECTORY_FLAG_RES = [
    re.compile(
        _COMMAND_START
        + r"git(?:[ \t]+(?:-c[ \t]+(?:"
        + _TOKEN
        + r")|-(?!C\b)"
        + _WORD
        + r"))*[ \t]+-C[ \t]*"
        + _PATH_TOKEN
    ),
    re.compile(_COMMAND_START + r"make\b[^;&|\n]*?[ \t]-C[ \t]*" + _PATH_TOKEN),
    re.compile(_COMMAND_START + r"np[mx]\b[^;&|\n]*?[ \t]--prefix" + _FLAG_VALUE + _PATH_TOKEN),
    re.compile(_COMMAND_START + r"uv\b[^;&|\n]*?[ \t]--directory" + _FLAG_VALUE + _PATH_TOKEN),
]


def _non_work_roots() -> list[Path]:
    """Folders whose files say nothing about which project the work is in: the system temp
    folders and the Claude config folder (CLAUDE_CONFIG_DIR, else ~/.claude), each as written
    and as resolved, since a config folder can be a link into a repository."""
    roots = list(SYSTEM_TEMP_DIRS)
    if os.environ.get("TMPDIR"):
        roots.append(os.environ["TMPDIR"])
    roots.append(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))
    written = [Path(os.path.normpath(os.path.expanduser(root))) for root in roots]
    return written + [Path(os.path.realpath(root)) for root in roots]


def _resolve(path: str, base: str) -> str:
    """Absolute normalised path for a path as written."""
    return os.path.normpath(os.path.join(base, os.path.expanduser(path)))


def _resolve_shell_word(raw: str, base: str) -> str | None:
    """A Bash path argument resolved, or None when it cannot be known without a shell (variables,
    command substitution, globs, `cd -`) or is an option rather than a path."""
    path = raw.strip("\"'")
    if not path or path.startswith("-") or any(char in path for char in "$`*?"):
        return None
    return _resolve(path, base)


def _bash_locations(command: str, base: str) -> list[str]:
    """Directories a Bash command runs in or names, in command order: the folder the shell was
    in, then each one the command names. A `cd` moves the base that later relative paths in the
    same command resolve against."""
    command = HEREDOC_RE.sub(lambda heredoc: heredoc.group(0).split("\n", 1)[0], command)
    found = [(match.start(1), True, match.group(1)) for match in MOVE_RE.finditer(command)]
    for pattern in DIRECTORY_FLAG_RES:
        found += [(match.start(1), False, match.group(1)) for match in pattern.finditer(command)]
    locations = [base]
    for _, is_move, raw in sorted(found):
        path = _resolve_shell_word(raw, base)
        if path is None:
            continue
        locations.append(path)
        if is_move:
            base = path
    return locations


def _call_locations(block: dict, base: str) -> list[str]:
    name = block.get("name")
    tool_input = block.get("input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        return []
    if name in EDIT_TOOLS:
        raw = tool_input.get(EDIT_TOOLS[name])
        return [_resolve(raw, base)] if isinstance(raw, str) and raw else []
    command = tool_input.get("command")
    if name == "Bash" and isinstance(command, str):
        return _bash_locations(command, base)
    return []


def _lines_from_end(path: Path) -> Iterator[bytes]:
    """Yield the transcript's lines last first, reading at most TRANSCRIPT_SCAN_BYTES."""
    with path.open("rb") as transcript:
        position = transcript.seek(0, os.SEEK_END)
        floor = max(0, position - TRANSCRIPT_SCAN_BYTES)
        carry = b""
        while position > floor:
            size = min(TRANSCRIPT_CHUNK_BYTES, position - floor)
            position -= size
            transcript.seek(position)
            lines = (transcript.read(size) + carry).split(b"\n")
            carry = lines[0]
            yield from reversed(lines[1:])
        if floor == 0:
            yield carry


def _tool_calls(event: dict, subagent: bool) -> list[dict]:
    """The tool calls in one transcript record, last first. A session's own transcript counts
    only its own records and a subagent's transcript only the subagent's; request blobs, which
    repeat earlier calls, carry none."""
    if (event.get("isSidechain") is True) is not subagent:
        return []
    if event.get("type") == "tool_use":
        return [event]
    message = event.get("message")
    if event.get("type") != "assistant" or not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    return blocks[::-1]


def _inside(location: str, folder: str) -> bool:
    return Path(os.path.realpath(location)).is_relative_to(os.path.realpath(folder))


def _last_located(transcript: Path, cwd: str, subagent: bool) -> tuple[str, str] | None:
    """The time and place of the most recent call in one transcript that says where the work is:
    an edit anywhere, or a Bash command run in or naming a folder outside `cwd`. Folders that
    say nothing about the project are passed over."""
    skipped = _non_work_roots()
    for line in _lines_from_end(transcript):
        if not TOOL_USE_BYTES_RE.search(line):
            continue  # cheap filter before parsing: no tool call on this line
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        shell_folder = event.get("cwd")
        base = shell_folder if isinstance(shell_folder, str) and shell_folder else cwd
        timestamp = event.get("timestamp")
        for block in _tool_calls(event, subagent):
            for location in reversed(_call_locations(block, base)):
                forms = {Path(location), Path(os.path.realpath(location))}
                if any(form.is_relative_to(root) for form in forms for root in skipped):
                    continue
                if block.get("name") in EDIT_TOOLS or not _inside(location, cwd):
                    return (timestamp if isinstance(timestamp, str) else "", location)
    return None


def _written_after(transcript: Path, timestamp: str) -> bool:
    """True when the file changed after the record time `timestamp` (or the time is unknown)."""
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return transcript.stat().st_mtime > moment


def last_work_location(transcript_path: str, cwd: str) -> str | None:
    """Where the session's work is: the latest located call across the session's transcript and
    its subagents' transcripts (`<transcript without .jsonl>/subagents/**/agent-*.jsonl`), or
    None when no call says."""
    if not transcript_path:
        return None
    session = Path(transcript_path)
    latest = _last_located(session, cwd, subagent=False)
    for transcript in sorted(session.with_suffix("").glob("subagents/**/agent-*.jsonl")):
        if latest is not None and not _written_after(transcript, latest[0]):
            continue
        located = _last_located(transcript, cwd, subagent=True)
        if located is not None and (latest is None or located[0] > latest[0]):
            latest = located
    return latest[1] if latest is not None else None


def work_in_project(cwd: str, transcript_path: str) -> bool:
    """True unless the session's most recent located call is outside `cwd`. A transcript that
    cannot be read keeps the answer True: the marker's project is then the only evidence."""
    try:
        location = last_work_location(transcript_path, cwd)
        return location is None or _inside(location, cwd)
    except UNREADABLE:
        return True


# Paired code fences, either ``` or ~~~ (a fence closes only with its own marker, per CommonMark).
FENCED_CODE_RE = re.compile(r"(```|~~~).*?\1", re.DOTALL)
# An opening fence with no matching close before end-of-text: everything from the marker to the
# end of the message is code (or at least not prose to scan), so it is exempt the same as a
# properly closed fence.
UNCLOSED_FENCE_RE = re.compile(r"(```|~~~).*\Z", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# Markdown table rows: any line starting (after optional whitespace) with `|`. A `?` inside column
# headers or cells is labeling, not a question to the user.
MD_TABLE_ROW_RE = re.compile(r"^[ \t]*\|.*$", re.MULTILINE)
PROSE_QUESTION_RE = re.compile(r"\?(?=\s|$)")


def strip_code(text: str) -> str:
    text = FENCED_CODE_RE.sub("", text)
    text = UNCLOSED_FENCE_RE.sub("", text)
    text = INLINE_CODE_RE.sub("", text)
    return text


def strip_tables(text: str) -> str:
    return MD_TABLE_ROW_RE.sub("", text)


def has_prose_question(text: str) -> bool:
    if BYPASS_MARKER in text:
        return False
    stripped = strip_tables(strip_code(text))
    return bool(PROSE_QUESTION_RE.search(stripped))


HANDBACK_PATTERNS = [
    re.compile(r"\bsay the word\b", re.IGNORECASE),
    re.compile(r"\bif you want,?\s+i can\b", re.IGNORECASE),
    re.compile(r"\bhappy to \w+(?:\s+\w+){0,3} if\b", re.IGNORECASE),
    # Excludes the "ready to go when you are" idiom (a sign-off, not an offer); everything else
    # of the shape "ready to <verb...> when you" still matches.
    re.compile(
        r"\bready to (?!go\s+when\s+you\s+are\b)\w+(?:\s+\w+){0,3} when you\b",
        re.IGNORECASE,
    ),
    re.compile(r"\blet me know if you['’]d like me to\b", re.IGNORECASE),
    # Requires a trailing "if..." clause: bare "I can also confirm X passed" is a statement of
    # fact, not an offer, and matching it trips precision-over-recall.
    re.compile(r"\bi can also \w+(?:\s+\w+){0,6}\s+if\b", re.IGNORECASE),
]

# Waiting for a go is the same hand-back in other words. "Until you say otherwise" is left out:
# it reports a stop the user already asked for.
WAIT_FOR_GO_PATTERNS = [
    re.compile(r"\b(?:once|after|when|whenever) you (?:say|give) (?:the )?go\b", re.IGNORECASE),
    re.compile(r"\bif you say so\b", re.IGNORECASE),
    re.compile(r"\buntil you say\b(?!\s+otherwise\b)", re.IGNORECASE),
    re.compile(r"\bi['’]?m holding here\b", re.IGNORECASE),
    re.compile(r"\b(?:wait|waits|waiting) (?:for|on) your go\b", re.IGNORECASE),
    re.compile(r"\bon your go\b", re.IGNORECASE),
    re.compile(r"\btell me to go\b", re.IGNORECASE),
]

# Quoted spans (someone's own words, or a phrase being discussed) are not the agent's own offer
# even when the quoted text contains a hand-back phrase. Straight and curly quote pairs; a
# straight single-quote pair requires a non-word char on both sides, a curly one after its close,
# so contractions ("can't ... don't") are never treated as a span.
QUOTED_SPAN_RE = re.compile(
    r'"[^"\n]*"'
    r"|(?<![A-Za-z0-9])'[^'\n]*'(?![A-Za-z0-9])"
    r"|“[^”\n]*”"
    r"|‘[^’\n]*’(?![A-Za-z0-9])"
)


def strip_quotes(text: str) -> str:
    return QUOTED_SPAN_RE.sub("", text)


# Lines naming a genuinely human-gated action are exempt even if they match a hand-back phrase
# (core-rules ACT BY DEFAULT: human-authority actions, spend, promotions).
GATED_ACTION_RE = re.compile(
    r"terraform\s+(apply|destroy|import)"
    r"|prod(?:uction)?\s+deploy"
    r"|uat\s+deploy"
    r"|promotion[- ]pr"
    r"|merge\s+the\s+promotion"
    r"|spend(?:ing)?\s+money",
    re.IGNORECASE,
)

# A wait on a merge, a migration or a delete is a real gate. This narrows the wait wording only:
# an offer like "say the word and I'll merge" still goes through HANDBACK_PATTERNS.
GATED_WAIT_RE = re.compile(
    r"\b(?:un)?merg(?:e|es|ed|ing)\b"
    r"|\bmigrat(?:e|es|ed|ing|ion|ions)\b"
    r"|\bdelet(?:e|es|ed|ing|ion|ions)\b"
    r"|\bdrop(?:s|ped|ping)?\b"
    r"|\bdestr(?:oy|oys|oyed|oying|uction)\b",
    re.IGNORECASE,
)

# The `Done:`/`Found:`/`Flags:`/`Next:` execution-status template (communication.md, Default Output
# Budget) is the legitimate place to hand back genuinely gated items. Also matches the
# bulleted/numbered form ("- **Flags:**", "1. Next:"): bullets are kept for multi-item answers, so
# that is the common shape the template takes.
FLAGS_NEXT_LINE_RE = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d+\.[ \t]+)?\**(Flags|Next)\**:")


def _last_paragraph(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text.strip())
    return paragraphs[-1] if paragraphs else text


def has_handback_offer(text: str) -> bool:
    if BYPASS_MARKER in text:
        return False
    tail = strip_quotes(_last_paragraph(strip_code(text)))
    candidate_lines = []
    for line in tail.split("\n"):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith(">"):
            continue  # quoted text
        if FLAGS_NEXT_LINE_RE.match(line):
            continue  # Flags:/Next: report template
        if GATED_ACTION_RE.search(line):
            continue  # names a human-gated action
        candidate_lines.append(line)
    candidate = "\n".join(candidate_lines)
    if any(pattern.search(candidate) for pattern in HANDBACK_PATTERNS):
        return True
    wait_candidate = "\n".join(line for line in candidate_lines if not GATED_WAIT_RE.search(line))
    return any(pattern.search(wait_candidate) for pattern in WAIT_FOR_GO_PATTERNS)


# The four `Done:`/`Found:`/`Flags:`/`Next:` template labels, in the same plain or
# bulleted/numbered/bold shapes as FLAGS_NEXT_LINE_RE. Kept separate: that regex is the hand-back
# exemption and must not widen to Done/Found.
STATUS_TEMPLATE_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+|\d+\.[ \t]+)?\**(Done|Found|Flags|Next)\**:"
)


def has_unlaunched_announcement(text: str) -> bool:
    if BYPASS_MARKER in text:
        return False
    body = text.rstrip()
    if not body:
        return False
    # Judge the raw last line, before strip_code: stripping a trailing fence would expose the
    # "Verify with:" line above it as the message's end, and a fenced command after the colon is
    # the correct ending, not an announcement.
    masked = FENCED_CODE_RE.sub(lambda match: " " * len(match.group()), body)
    if len(masked.rstrip()) < len(body):
        return False  # a closed fence reaches the end of the message
    if UNCLOSED_FENCE_RE.search(masked):
        return False  # an unclosed fence runs to the end of the message
    last_line = body.split("\n")[-1]
    if not last_line.endswith(":"):
        return False
    if MD_TABLE_ROW_RE.match(last_line):
        return False
    return not STATUS_TEMPLATE_LINE_RE.match(last_line)


# A `>` at line start is only a blockquote marker when it isn't a comparison operator: `>90%
# used`, `>= 5 rows`, `> 5 rows` all read as "greater than", not a quote bar, so exclude `>`/`>=`
# immediately followed by (optional whitespace then) a digit.
BLOCKQUOTE_LINE_RE = re.compile(r"^[ \t]*>(?!=?[ \t]*\d)[ \t]?(.*)$")
QUOTE_MARK_RE = re.compile(r"^[\"'“‘]")


def _blockquote_runs(text: str) -> Iterator[list[str]]:
    """Yield contiguous runs of blockquote-line content, one run per `>` block."""
    run: list[str] = []
    for line in text.split("\n"):
        match = BLOCKQUOTE_LINE_RE.match(line)
        if match:
            run.append(match.group(1))
            continue
        if run:
            yield run
        run = []
    if run:
        yield run


def has_blockquote_bar(text: str) -> bool:
    if BYPASS_MARKER in text:
        return False
    stripped = strip_code(text)
    for run in _blockquote_runs(stripped):
        first_content = run[0].lstrip()
        if not QUOTE_MARK_RE.match(first_content):
            return True
    return False


def emit_allow() -> None:
    print(json.dumps({}))


def emit_block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


PROSE_QUESTION_REASON = (
    "Prose question detected in assistant turn. Every question to the user MUST go through the "
    "AskUserQuestion tool, never prose (core-rules ACT BY DEFAULT). Redo the turn: call "
    "AskUserQuestion with the questions you intended to ask. If the `?` was a genuine "
    "non-question (e.g. quoting the user back), include the literal marker "
    f"`{BYPASS_MARKER}` in the message to bypass."
)

HANDBACK_OFFER_REASON = (
    "Hand-back offer detected at the end of the assistant turn (e.g. 'say the word', "
    "'happy to X if', 'ready to X when you', 'I can also X', 'once you say go', 'I'm "
    "holding here', 'waiting for your go'). A clean, reversible, in-scope next step gets "
    "executed, not offered (core-rules ACT BY DEFAULT). Redo the turn: do it now, ask through "
    "AskUserQuestion if the call is really the user's, or drop the offer. Hand back only "
    "actions that need the user's authority: terraform apply/destroy/import, prod or uat "
    "deploys, promotion-PR merges, spending money, or another never-decide-alone call. If the "
    "user told you to stop or hold, stay stopped: run nothing and end the turn on that. If the "
    "phrase is drafted copy rather than your own offer, include the literal marker "
    f"`{BYPASS_MARKER}` in the message to bypass."
)

UNLAUNCHED_ANNOUNCEMENT_REASON = (
    "Unlaunched announcement detected: the last line of the assistant turn ends with ':' "
    "and no tool call follows it (a Stop means none did). The announced action never "
    'started, but a recap and the user read a line like "Running the check on this '
    'commit:" as work in flight. Redo the turn: make the launching tool call '
    "(background Bash, Agent, or Workflow) in this turn, or rewrite the last line as a "
    'statement or "Next: X". If the trailing colon is not an announcement, include the '
    f"literal marker `{BYPASS_MARKER}` in the message to bypass."
)

BLOCKQUOTE_BAR_REASON = (
    "Markdown blockquote bar detected in assistant turn. Plain output uses bullets or fenced "
    "blocks, never `>` blockquotes: the left bar is noise and `/copy` carries the `>` into "
    "pasted drafts. Redo the turn: re-send that content as bullets or a fenced block. "
    "A real quotation of someone's words may keep the `>` if the quoted line starts "
    "with a quote mark right after `> `."
)


def stop_flag_set(session_id: object) -> bool:
    """True when the user's bare "stop" still holds this session; a bad flag reads as unset."""
    path = flag_path(session_id)
    return path is not None and path.is_file() and flag_is_set(path, session_id)


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        emit_allow()
        return

    cwd = hook_input.get("cwd", "")
    transcript_path = hook_input.get("transcript_path")
    opted_out = project_opted_out(cwd) and work_in_project(
        cwd, transcript_path if isinstance(transcript_path, str) else ""
    )

    # The payload field is the only source of the final reply: no fallback to a transcript read.
    # A present-but-empty field is a turn with no final text.
    text = hook_input.get("last_assistant_message")
    if not isinstance(text, str):
        sys.stderr.write(
            "last-assistant-message-missing: Stop payload has no string "
            "last_assistant_message; allowing (fail-open)\n"
        )
        emit_allow()
        return
    if not text:
        emit_allow()
        return

    stop_hook_active = bool(hook_input.get("stop_hook_active", False))

    reasons = []
    if not opted_out and has_prose_question(text):
        reasons.append(PROSE_QUESTION_REASON)
    if not stop_hook_active:
        if has_handback_offer(text) and not stop_flag_set(hook_input.get("session_id")):
            reasons.append(HANDBACK_OFFER_REASON)
        if not opted_out and has_unlaunched_announcement(text):
            reasons.append(UNLAUNCHED_ANNOUNCEMENT_REASON)
        if not opted_out and has_blockquote_bar(text):
            reasons.append(BLOCKQUOTE_BAR_REASON)

    if not reasons:
        emit_allow()
        return

    emit_block("\n\n".join(reasons))


if __name__ == "__main__":
    main()
