#!/usr/bin/env python3
"""Read past Claude Code sessions: tool calls, messages, and the context around one message.

    python3 read.py messages --typed --search 'flag*' --skip-session 3f2a9c1b

Inputs: the JSONL transcripts under each --root (default <config dir>/projects), laid out as
<project>/<session>.jsonl, with subagent files under <project>/<session>/subagents/ and workflow
agents under <project>/<session>/subagents/workflows/<run>/. Workflow journal.jsonl files are not
transcripts and are skipped.
Outputs: readable text on stdout, or with --json one JSON object per row. Rows come from a local
index (index.py) that each query brings up to date first.
Exit codes: 0 done; 1 the message a context command names was not found, the search index is
unavailable, or the index turned out corrupt partway through; 2 a usage error or a bad --search,
or no transcript folder or transcript at all.

Commands:

* `tools`: one row per tool call. `--results` adds each call's result and whether it was an
  error. `--tool` is a full match on the tool name ("Bash" does not match "BashOutput"), `--match`
  a regex searched in the input's JSON, `--search` full-text words in the input, and in the
  result with `--results`.
* `messages`: one row per user or assistant record that has text, plus one user row per prompt a
  person sent while a turn ran. A send holding a side agent's note and typed words gives a row
  for each. `injected` says why a user row was not typed by a person (meta, sidechain,
  side-agent-note, origin-...); `--typed` keeps only what a person typed.
* `around`, `since-reply`, `lead-up`: the session around one message, named by a quote of its
  text or by its uuid. A turn is a typed user message or an assistant reply.
* `index`: bring every transcript into the index ahead of a wide first query.

How rows are chosen and counted:

* A row is in the window by its own timestamp, so a session left running for days gives only its
  rows inside the window; a row with no timestamp is always in it. A file last modified before
  the window starts is not read.
* `--search` takes SQLite full-text syntax: whole words, case-insensitive; `flag*` finds flag,
  flags and flagged; a phrase or a term with punctuation goes in double quotes, the star outside;
  AND, OR and NOT are operators only in capitals. Sessions print most matching rows first and
  each session shows how many of its rows the search matched before `--tool` and `--match`.
  `--match` reads every row in the window and takes any Python regex. Every matching row prints;
  there are no caps or samples.
* A forked, resumed or backgrounded session's file repeats records of the session it came from.
  A row that files of two selected sessions both hold (same record uuid, same tool call, same
  injected label) prints once: under the file that is not a copy (a record carrying
  `forkedFrom` and a file linked in from another session are copies), else the one created
  first, else by path. Only selected files compete, so `--session` naming a fork prints all its
  rows, inherited ones too. `--skip-session` leaves a session out of the output but keeps it
  competing, so its words never print under a fork of it.
* `cwd` is the directory Claude Code recorded with the record. Tool calls sent together in one
  message all carry the directory from before the first ran, so a call after a sibling's cd,
  pushd, popd, chdir, EnterWorktree or ExitWorktree shows the old one and is marked
  `cwd_uncertain`.
* The context commands find the message among the selected sessions' messages in the window by
  its uuid, or by a quote of a turn: the quote's words in order, whitespace ignored, case kept.
  A compaction summary or skill text that repeats the quote is not the message. Every match
  prints, each with its context, and the context is read from the file whatever the window.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

import index
import transcripts

KINDS = ("main", "subagent", "workflow")
KIND_GLOBS = {
    "main": "*/*.jsonl",
    "subagent": "*/*/subagents/*.jsonl",
    "workflow": "*/*/subagents/workflows/*/*.jsonl",
}
JOURNAL = "journal.jsonl"
COLUMNS = (
    "id, source, ts, cwd, role, injected, tool, call_id, cwd_uncertain, is_error, uuid, body,"
    " result"
)
WINDOW = (
    "source = :source AND (epoch IS NULL OR epoch >= :start)"
    " AND (:end IS NULL OR epoch IS NULL OR epoch < :end)"
)
# A turn is a typed user message or an assistant reply.
TURN = "(role = 'assistant' OR (role = 'user' AND injected IS NULL))"
CONTEXT_TURNS = 3
NOT_FOUND = 1
UNUSABLE_INDEX = 1

type Row = dict[str, Any]
type Who = dict[str, str | None]


def regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"bad regex {pattern!r}: {e}") from e


def kinds(value: str) -> list[str]:
    chosen = value.split(",")
    bad = [k for k in chosen if k not in KINDS]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown kind {','.join(bad)}; choose from {','.join(KINDS)}"
        )
    return [k for k in KINDS if k in chosen]


def turns(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        count = -1
    if count < 0:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number of turns")
    return count


def transcript_files(
    roots: list[Path], start: float, args: argparse.Namespace
) -> list[tuple[Path, Who]]:
    """(path, who) for each transcript file passing the shared filters, --skip-session aside."""
    found = []
    for kind in args.kind or KINDS:
        for root, path in sorted((r, p) for r in roots for p in r.glob(KIND_GLOBS[kind])):
            if path.name == JOURNAL:
                continue
            parts = path.relative_to(root).parts
            project, session = parts[0], parts[1].removesuffix(".jsonl")
            if args.project and args.project not in project:
                continue
            if args.session and not session.startswith(args.session):
                continue
            try:
                # A file last written before the window holds no rows inside it.
                if path.stat().st_mtime < start:
                    continue
            except OSError:
                continue
            agent = None if kind == "main" else path.stem
            who = {"project": project, "session": session, "agent": agent, "kind": kind}
            found.append((path, {**who, "file": str(path)}))
    return found


def skipped(who: Who, args: argparse.Namespace) -> bool:
    return any(str(who["session"]).startswith(prefix) for prefix in args.skip_session or ())


def row_filter(args: argparse.Namespace) -> str:
    """The SQL that keeps the messages a `messages` query asks for: a role, or typed words."""
    if getattr(args, "typed", False):
        return " AND role = 'user' AND injected IS NULL"
    return " AND role = :role" if getattr(args, "role", None) else ""


def to_print(args: argparse.Namespace) -> str:
    hits = " AND id IN (SELECT id FROM temp.hits WHERE file_id = :file)" if args.search else ""
    return (
        f"file_id = :file AND {WINDOW}{row_filter(args)}{hits}"
        " AND id NOT IN (SELECT id FROM temp.repeats)"
    )


def as_row(who: Who, stored: tuple[Any, ...], results: bool) -> Row:
    """One stored row as printed. Tool rows carry their result only when `results` is set."""
    (_, source, ts, cwd, role, injected, tool, call_id, uncertain, is_error, uuid, body, result) = (
        stored
    )
    if source == "message":
        return {
            "ts": ts,
            **who,
            "cwd": cwd,
            "role": role,
            "text": body,
            "injected": injected,
            "uuid": uuid,
        }
    try:
        tool_input = json.loads(body)
    except json.JSONDecodeError as e:
        raise index.DamagedRow(f"a stored tool input is not JSON: {e}") from e
    row = {
        "ts": ts,
        **who,
        "cwd": cwd,
        "cwd_uncertain": bool(uncertain),
        "tool": tool,
        "id": call_id,
        "input": tool_input,
        "uuid": uuid,
    }
    if results:
        row["result"] = result
        row["is_error"] = None if is_error is None else bool(is_error)
    return row


def listed_rows(
    conn: sqlite3.Connection, file_id: int, who: Who, args: argparse.Namespace, span: Row
) -> Iterator[Row]:
    source = "tool" if args.command == "tools" else "message"
    params = {**span, "file": file_id, "source": source, "role": getattr(args, "role", None)}
    for stored in conn.execute(
        f"SELECT {COLUMNS} FROM rows WHERE {to_print(args)} ORDER BY id", params
    ):
        if source == "tool" and args.tool and not args.tool.fullmatch(stored[6]):
            continue
        if args.match and not args.match.search(stored[11]):
            continue
        yield as_row(who, stored, results=bool(getattr(args, "results", False)))


def search_files(conn: sqlite3.Connection, args: argparse.Namespace) -> set[int]:
    """Fill temp.hits with the rows matching --search and return the files holding them."""
    columns = "body result" if args.command == "tools" and args.results else "body"
    conn.execute(
        "CREATE TEMP TABLE hits (file_id INTEGER, id INTEGER, PRIMARY KEY (file_id, id))"
        " WITHOUT ROWID"
    )
    conn.execute(
        "INSERT INTO temp.hits SELECT rows.file_id, rows.id FROM rows_text"
        " JOIN rows ON rows.id = rows_text.rowid WHERE rows_text MATCH ?",
        (f"{{{columns}}} : ({args.search})",),
    )
    return {file_id for (file_id,) in conn.execute("SELECT DISTINCT file_id FROM temp.hits")}


def mark_repeats(
    conn: sqlite3.Connection,
    competing: list[tuple[Path, Who]],
    ids: dict[str, int],
    span: Row,
    searched: bool,
) -> None:
    """Fill temp.repeats with the rows to leave out: those another competing session holds in a
    file that is not a copy, or was created earlier, or sorts first. Only rows the query would
    print compete, so a row is left out only for one printing in its place. One record gives
    several rows, told apart by call_id and injected; a tool_use id alone is not unique, as test
    runs reuse ids across sessions."""
    conn.execute("CREATE TEMP TABLE repeats (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TEMP TABLE chosen (file_id INTEGER PRIMARY KEY, session TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO temp.chosen VALUES (?, ?)",
        ((ids[str(path)], who["session"]) for path, who in competing if str(path) in ids),
    )
    hits = " AND rows.id IN (SELECT id FROM temp.hits)" if searched else ""
    conn.execute(
        f"""
        INSERT INTO temp.repeats SELECT id FROM (
            SELECT rows.id, session, first_value(session) OVER (
                PARTITION BY uuid, call_id, injected ORDER BY copied, born, path
            ) AS first_session
            FROM rows JOIN temp.chosen USING (file_id) JOIN files ON files.id = file_id
            WHERE uuid IN (
                SELECT uuid FROM rows WHERE uuid IS NOT NULL
                GROUP BY uuid HAVING min(file_id) < max(file_id)
            ) AND {WINDOW}{hits}
        ) WHERE session != first_session
        """,
        span,
    )


def session_matches(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    span: Row,
    selected: list[tuple[Path, Who]],
    ids: dict[str, int],
) -> dict[str, int]:
    """How many rows the search matched per session, among the rows this query reads, --tool
    and --match aside."""
    per_file = dict(
        conn.execute(
            "SELECT rows.file_id, count(*) FROM rows JOIN temp.hits USING (id)"
            f" WHERE {WINDOW}{row_filter(args)} AND id NOT IN (SELECT id FROM temp.repeats)"
            " GROUP BY rows.file_id",
            {**span, "role": getattr(args, "role", None)},
        )
    )
    matches: dict[str, int] = {}
    for path, who in selected:
        session = str(who["session"])
        matches[session] = matches.get(session, 0) + per_file.get(ids.get(str(path)), 0)
    return matches


def most_matches_first(
    selected: list[tuple[Path, Who]], matches: dict[str, int]
) -> list[tuple[Path, Who]]:
    first_file: dict[str, int] = {}
    for i, (_, who) in enumerate(selected):
        first_file.setdefault(str(who["session"]), i)
    return sorted(
        selected, key=lambda f: (-matches[str(f[1]["session"])], first_file[str(f[1]["session"])])
    )


class CutShort(Exception):
    """The index turned out corrupt after rows were printed, so a retry would print them twice."""


class Selection:
    """The files a query reads and the window it reads them in."""

    def __init__(self, args: argparse.Namespace, roots: list[Path]) -> None:
        window = transcripts.make_window(args.days, args.since, args.until)
        self.span: Row = {"start": window.start, "end": window.end}
        self.competing = transcript_files(roots, window.start, args)
        self.selected = [(p, who) for p, who in self.competing if not skipped(who, args)]


def open_snapshot(
    conn: sqlite3.Connection, db: Path, selection: Selection, source: str
) -> dict[str, int]:
    """Bring the files up to date, mark the repeats and return {path: file id}."""
    # One snapshot for the whole read: another session indexing a file again gives its rows new
    # ids, and a search that matched the old ids would print nothing for that file.
    index.read_snapshot(conn, db, [path for path, _ in selection.competing])
    ids = dict(conn.execute("SELECT path, id FROM files"))
    selection.span["source"] = source
    return ids


def listing(args: argparse.Namespace, roots: list[Path], db: Path, out: Printer) -> int:
    """Print the tools or messages rows the query selects."""
    selection = Selection(args, roots)
    source = "tool" if args.command == "tools" else "message"
    with closing(index.connect(db)) as conn:
        ids = open_snapshot(conn, db, selection, source)
        try:
            with_hits = search_files(conn, args) if args.search else None
        except sqlite3.OperationalError as e:
            print(
                f"bad --search query {args.search!r}: {e}. Put a phrase or a term with "
                "punctuation (hyphen, dot, apostrophe, #) in double quotes.",
                file=sys.stderr,
            )
            return 2
        mark_repeats(conn, selection.competing, ids, selection.span, bool(args.search))
        selected = selection.selected
        matches = None
        if args.search:
            matches = session_matches(conn, args, selection.span, selected, ids)
            selected = most_matches_first(selected, matches)

        def rows() -> Iterator[Row]:
            for path, who in selected:
                file_id = ids.get(str(path))
                if file_id is None or (with_hits is not None and file_id not in with_hits):
                    continue
                count = {} if matches is None else {"matches": matches[str(who["session"])]}
                for row in listed_rows(conn, file_id, who, args, selection.span):
                    yield {**row, **count}

        printed = False
        try:
            for row in rows():
                out.row(row)
                printed = True
            out.flush()
        except sqlite3.DatabaseError as e:
            if printed and index.is_corrupt(e):
                raise CutShort from e
            raise
    return 0


def normalized(text: str) -> str:
    return " ".join(text.split())


def context(args: argparse.Namespace, roots: list[Path], db: Path, out: Printer) -> int:
    """Print the context a context command asks for around each message that matches."""
    selection = Selection(args, roots)
    needle = normalized(args.message)
    with closing(index.connect(db)) as conn:
        ids = open_snapshot(conn, db, selection, "message")
        mark_repeats(conn, selection.competing, ids, selection.span, searched=False)
        anchors: list[tuple[int, Who, int]] = []
        for path, who in selection.selected:
            file_id = ids.get(str(path))
            if file_id is None:
                continue
            for row_id, uuid, body, turn in conn.execute(
                f"SELECT id, uuid, body, {TURN} FROM rows WHERE file_id = :file AND {WINDOW}"
                " AND id NOT IN (SELECT id FROM temp.repeats) ORDER BY id",
                {**selection.span, "file": file_id},
            ):
                # A quote names a turn: a summary or skill text that repeats it is not the message.
                if uuid == args.message or (turn and needle and needle in normalized(body)):
                    anchors.append((file_id, who, row_id))
        if not anchors:
            print(f"no message in the window matches {args.message!r}", file=sys.stderr)
            return NOT_FOUND
        for number, (file_id, who, anchor) in enumerate(anchors, 1):
            timeline = [
                as_row(who, stored, results=True) | {"_id": stored[0]}
                for stored in conn.execute(
                    f"SELECT {COLUMNS} FROM rows WHERE file_id = ?"
                    " AND source IN ('message', 'tool') ORDER BY id",
                    (file_id,),
                )
            ]
            at = next(i for i, row in enumerate(timeline) if row["_id"] == anchor)
            first, last, tools = span_of(args, timeline, at)
            out.context(number, len(anchors), timeline[first : last + 1], at - first, tools)
        out.flush()
    return 0


def is_turn(row: Row) -> bool:
    return row.get("role") == "assistant" or (row.get("role") == "user" and not row["injected"])


def span_of(args: argparse.Namespace, timeline: list[Row], at: int) -> tuple[int, int, bool]:
    """(first, last, whether tool calls print) for the rows a context command shows."""
    if args.command == "lead-up":
        return 0, at, args.tools
    if args.command == "since-reply":
        replies = [i for i in range(at) if timeline[i].get("role") == "assistant"]
        return (replies[-1] if replies else 0), at, True
    first = at
    counted = 0
    while first > 0 and counted < args.before:
        first -= 1
        counted += is_turn(timeline[first])
    last = at
    counted = 0
    while last + 1 < len(timeline):
        if is_turn(timeline[last + 1]):
            if counted == args.after:
                break
            counted += 1
        last += 1
    return first, last, args.tools


def build_index(roots: list[Path], db: Path) -> int:
    every = argparse.Namespace(kind=None, project=None, session=None)
    with closing(index.connect(db)) as conn:
        index.read_snapshot(conn, db, [path for path, _ in transcript_files(roots, 0.0, every)])
        files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        rows = conn.execute("SELECT count(*) FROM rows").fetchone()[0]
    print(f"files={files} rows={rows} megabytes={db.stat().st_size // 10**6} path={db}")
    return 0


class Printer:
    """Rows as JSON Lines, or as text an agent can read without parsing it."""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self.file: str | None = None
        self.cwd: str | None = None

    def write(self, line: str) -> None:
        sys.stdout.write(line + "\n")

    def flush(self) -> None:
        sys.stdout.flush()

    def row(self, row: Row) -> None:
        if self.as_json:
            self.write(json.dumps(row, ensure_ascii=False))
            return
        if row["file"] != self.file:
            if self.file is not None:
                self.write("")
            self.file, self.cwd = row["file"], None
            self.write(self.session_line(row))
        self.text_row(row)

    def session_line(self, row: Row) -> str:
        agent = f" {row['agent']}" if row["agent"] else ""
        matches = f", {row['matches']} matching rows" if "matches" in row else ""
        return (
            f"== session {row['session']}, {row['kind']}{agent}, project {row['project']}"
            f"{matches}\n   file {row['file']}"
        )

    def text_row(self, row: Row, note: str = "") -> None:
        cwd = ""
        if row["cwd"] != self.cwd:
            self.cwd = row["cwd"]
            cwd = f"  cwd={row['cwd']}"
        if "text" in row:
            who = row["role"] + (f" ({row['injected']})" if row["injected"] else "")
            self.write(f"{row['ts']}  {who}  uuid={row['uuid']}{cwd}{note}")
            self.write(indented(row["text"], 2))
            return
        uncertain = "  cwd_uncertain" if row["cwd_uncertain"] else ""
        self.write(f"{row['ts']}  {row['tool']}  id={row['id']}{cwd}{uncertain}{note}")
        self.write(input_lines(row["input"]))
        if "result" in row:
            if row["result"] is None:
                self.write("  result: none recorded")
            else:
                self.write("  result (error):" if row["is_error"] else "  result:")
                self.write(indented(row["result"], 4))

    def context(self, number: int, total: int, rows: list[Row], anchor: int, tools: bool) -> None:
        if self.as_json:
            for i, row in enumerate(rows):
                if tools or "text" in row:
                    printed = {k: v for k, v in row.items() if k != "_id"}
                    self.write(json.dumps({**printed, "anchor": i == anchor}, ensure_ascii=False))
            return
        first = rows[anchor]
        self.cwd = None
        if number > 1:
            self.write("")
        self.write(f"== match {number} of {total}: {self.session_line(first)[3:]}")
        hidden: list[str] = []
        for i, row in enumerate(rows):
            if "text" not in row and not tools:
                hidden.append(row["tool"])
                continue
            if hidden:
                self.write(hidden_calls(hidden))
                hidden = []
            self.text_row(row, "  <- this message" if i == anchor else "")
        if hidden:
            self.write(hidden_calls(hidden))


def hidden_calls(tools: list[str]) -> str:
    """One line standing for the tool calls a context command leaves out without --tools."""
    calls = "tool call" if len(tools) == 1 else "tool calls"
    return f"  [{len(tools)} {calls}: {', '.join(tools)}; --tools shows them]"


def indented(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.split("\n"))


def input_lines(tool_input: Any) -> str:
    """A tool input as `key: value` lines, a multi-line string value below its key."""
    if not isinstance(tool_input, dict):
        return indented(json.dumps(tool_input, ensure_ascii=False), 2)
    lines = []
    for key, value in tool_input.items():
        if isinstance(value, str) and "\n" in value:
            lines.append(f"  {key}:\n{indented(value, 4)}")
        elif isinstance(value, str):
            lines.append(f"  {key}: {value}")
        else:
            lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) if lines else "  (no input)"


def build_parser() -> argparse.ArgumentParser:
    roots = argparse.ArgumentParser(add_help=False)
    roots.add_argument(
        "--root",
        action="append",
        metavar="DIR",
        help="transcript folder holding <project>/<session>.jsonl; repeat to combine folders "
        "(default: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects)",
    )
    shared = argparse.ArgumentParser(add_help=False)
    span = shared.add_mutually_exclusive_group()
    span.add_argument(
        "--days",
        type=transcripts.positive_days,
        default=7,
        help="look back this many days (default 7)",
    )
    span.add_argument(
        "--since",
        type=transcripts.day_argument,
        metavar="YYYY-MM-DD",
        help="look back to local midnight of this date",
    )
    shared.add_argument(
        "--until",
        type=transcripts.day_argument,
        metavar="YYYY-MM-DD",
        help="stop at the end of this date (default: now)",
    )
    shared.add_argument("--project", help="substring of the project folder name")
    shared.add_argument("--session", help="prefix of the session id")
    shared.add_argument(
        "--skip-session",
        action="append",
        metavar="PREFIX",
        help="leave out sessions whose id starts with this; repeat for more",
    )
    shared.add_argument("--kind", type=kinds, help="comma list of main,subagent,workflow")
    shared.add_argument("--json", action="store_true", help="print JSON Lines rows for scripts")

    parser = argparse.ArgumentParser(
        prog="read.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    tools = sub.add_parser("tools", parents=[shared, roots], help="one row per tool call")
    tools.add_argument("--tool", type=regex, help="full-match regex on the tool name")
    tools.add_argument("--match", type=regex, help="regex searched in the JSON of the input")
    tools.add_argument("--results", action="store_true", help="add result and is_error")
    tools.add_argument(
        "--search", help="full-text query over the input, and the result with --results"
    )
    messages = sub.add_parser(
        "messages", parents=[shared, roots], help="one row per user or assistant text"
    )
    who = messages.add_mutually_exclusive_group()
    who.add_argument("--role", choices=("user", "assistant"))
    who.add_argument("--typed", action="store_true", help="only messages a person typed")
    messages.add_argument("--match", type=regex, help="regex searched in the text")
    messages.add_argument("--search", help="full-text query over the text")

    message_help = "a quote of the message's text, or its uuid"
    around = sub.add_parser(
        "around", parents=[shared, roots], help="the turns before and after a message"
    )
    around.add_argument("message", help=message_help)
    around.add_argument("--before", type=turns, default=CONTEXT_TURNS, metavar="N")
    around.add_argument("--after", type=turns, default=CONTEXT_TURNS, metavar="N")
    around.add_argument("--tools", action="store_true", help="print tool calls with results")
    since = sub.add_parser(
        "since-reply",
        parents=[shared, roots],
        help="the agent's last reply before a message, then every tool call and result",
    )
    since.add_argument("message", help=message_help)
    lead = sub.add_parser(
        "lead-up", parents=[shared, roots], help="a session from its start to a message"
    )
    lead.add_argument("message", help=message_help)
    lead.add_argument("--tools", action="store_true", help="print tool calls with results")
    sub.add_parser("index", parents=[roots], help="bring every transcript into the index")
    return parser


def run_surviving_a_reset(db: Path, run: Callable[[], int]) -> int:
    seen = index.generation(db)
    try:
        return index.retrying_after_reset(db, run)
    except CutShort:
        index.reset(db, seen)
        print(
            "The transcript index turned out corrupt partway through, so these rows are "
            "incomplete. It was reset: run the query again.",
            file=sys.stderr,
        )
        return 1


def usable_roots(given: list[str] | None) -> list[Path] | None:
    """The transcript folders, or None after one stderr line when one is missing or none holds a
    transcript."""
    roots = list(dict.fromkeys(transcripts.resolve_roots(given)))  # a root given twice is read once
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        print(f"read.py: no transcript folder at {', '.join(missing)}", file=sys.stderr)
        return None
    if not any(next(r.glob(glob), None) for r in roots for glob in KIND_GLOBS.values()):
        where = ", ".join(str(r) for r in roots)
        print(f"read.py: no transcripts found under {where}", file=sys.stderr)
        return None
    return roots


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "since", None) and args.until and args.until < args.since:
        parser.error(f"--until {args.until} is before --since {args.since}")
    roots = usable_roots(args.root)
    if roots is None:
        return transcripts.NO_TRANSCRIPTS
    if not index.fts5_available():
        print(
            "read.py: this Python's SQLite lacks FTS5 with contentless delete (SQLite 3.43 or "
            "newer), which the index needs",
            file=sys.stderr,
        )
        return UNUSABLE_INDEX
    db = index.db_path()
    try:
        if args.command == "index":
            return run_surviving_a_reset(db, partial(build_index, roots, db))
        command = listing if args.command in ("tools", "messages") else context
        return run_surviving_a_reset(db, partial(command, args, roots, db, Printer(args.json)))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
