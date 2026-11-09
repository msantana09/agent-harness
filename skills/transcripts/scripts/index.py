"""The SQLite index behind read.py: one row per tool call, message or recorded git branch of each
transcript file, plus full-text search over them.

    import index
    with closing(index.connect(index.db_path())) as conn:
        index.read_snapshot(conn, index.db_path(), paths)

Inputs: the transcript files a query names. Output: the database at
`$XDG_CACHE_HOME/agent-harness/transcripts/index.db` (`~/.cache` when the variable is unset); it
holds full transcript text, so its folder is private to the user (0700) and so is the file (0600).
It prints nothing to stdout; a reset after corruption says so on stderr.

How the index stays right:

* It is a derived cache. Deleting the file loses nothing, and a query rebuilds what it needs.
* A query first brings its own files up to date, so an answer never lags the transcripts. A file
  whose size or modification time changed is indexed again whole, in one transaction and by one
  process at a time, so concurrent sessions see the old rows or the new ones and never older rows
  after newer ones. A file that no longer exists leaves the index on the next query. A corrupt
  index is emptied in place and rebuilt by the query that found it.
* Every file keeps every row it holds, including those it repeats from another session's file, so
  a fork still shows the context it started from. Each row stores its record's uuid, whether it
  is a copy (the record carries `forkedFrom`, or the file is a link to another session's file),
  and its file's creation time, so a query can leave the repeats out. Creation time is the file
  system's where it records one (macOS, BSD) and the file's first record time elsewhere.
* VERSION hashes this file and transcripts.py, and a file indexed under another version is
  indexed again when next read.
"""

import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import zlib
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from itertools import repeat
from pathlib import Path
from typing import Any

import transcripts

VERSION = hashlib.sha256(
    Path(__file__).read_bytes() + Path(transcripts.__file__).read_bytes()
).hexdigest()
SCHEMA_VERSION = 1
# Waiting out another session's write, including one still building the index.
BUSY_TIMEOUT_SECONDS = 300
# Below this many stale files, starting worker processes costs more than it saves.
POOL_THRESHOLD = 32
WORKERS = 8

# AUTOINCREMENT: a session holding an id read earlier must never reach another file through it.
SCHEMA = """
CREATE TABLE files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL, version TEXT NOT NULL, born REAL NOT NULL
);
CREATE TABLE rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL, source TEXT NOT NULL,
    epoch REAL, ts TEXT, cwd TEXT, role TEXT, injected TEXT, tool TEXT, call_id TEXT,
    cwd_uncertain INTEGER, is_error INTEGER, uuid TEXT, copied INTEGER NOT NULL,
    body TEXT NOT NULL, result TEXT
);
CREATE INDEX rows_by_file ON rows (file_id, source, id);
CREATE INDEX rows_by_uuid ON rows (uuid, file_id);
CREATE VIRTUAL TABLE rows_text USING fts5(body, result, content='', contentless_delete=1);
"""

type Row = dict[str, Any]

# Tools that move the session's directory themselves.
WORKTREE_TOOLS = {"EnterWorktree", "ExitWorktree"}
DIRECTORY_CHANGERS = {"cd", "pushd", "popd", "chdir"}
# Words that come before a command's own name: compound-command keywords, and commands that run
# the command after them (their options are skipped; timeout also takes a duration).
KEYWORDS = {"!", "{", "}", "do", "then", "else", "elif", "if", "while", "until"}
WRAPPERS = {"builtin", "command", "exec", "nohup", "time", "env", "sudo"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
OPERATOR_CHARS = ";&|()\n"
# Stands in for an operator character the shell reads as data (quoted or escaped); a private-use
# character, so it never matches a command name.
LITERAL_OPERATOR = ""
# A redirection word: an optional file descriptor, then >, >>, <, <>, &>, &>>, >&, <& or >|, then
# its target when written in the same word (`2>/dev/null`, `2>&1`).
REDIRECTION = re.compile(r"\d*(?:&>>?|>>?|<>|<|>&|<&|>\|)")
# A heredoc operator and its delimiter word, quoted or not (`<<<`, a here-string, is not one).
HEREDOC = re.compile(r"<<(?!<)-?\s*(['\"]?)([\w.-]+)\1")
# An ANSI-C quoted string, `$'...'`, and the escapes the shell decodes inside one.
ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.DOTALL)
ANSI_C_ESCAPE = re.compile(
    r"\\(x[0-9A-Fa-f]{1,2}|u[0-9A-Fa-f]{1,4}|U[0-9A-Fa-f]{1,8}|[0-7]{1,3}|c.|.)", re.DOTALL
)
ANSI_C_LETTERS = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n"}
ANSI_C_LETTERS |= {"r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
# The fallback for a command the parser cannot read (a substitution, unbalanced quotes): a cd,
# pushd, popd or chdir after any command separator or then/do/else, behind assignments, builtin
# or command, quoted or not.
CD = re.compile(
    r"(?:^|[;&|({!\n]|\b(?:then|do|else)\s)\s*(?:\w+=\S*\s+)*(?:(?:builtin|command)\s+)?"
    r"(?:cd|pushd|popd|chdir)(?=$|[\s;&|)])"
)


def db_path() -> Path:
    """The index in the user's cache folder: `$XDG_CACHE_HOME`, which defaults to `~/.cache`."""
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "agent-harness" / "transcripts" / "index.db"


def fts5_available() -> bool:
    """Whether this Python's SQLite can build the search table (FTS5, SQLite 3.43 or newer)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(a, content='', contentless_delete=1)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


@contextmanager
def setup_lock(db: Path, mode: int) -> Iterator[None]:
    """Exclusive for setup and for a reset, shared to wait out either. Never take it around a
    connect or a catch_up, which take it exclusively on another descriptor and would wait on this
    one."""
    with open(os.open(f"{db}.lock", os.O_CREAT | os.O_RDWR, 0o600)) as lock:
        fcntl.flock(lock, mode)
        yield


def prepare(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("rows_text", "rows", "files"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in SCHEMA.split(";\n"):
            if statement.strip():
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("COMMIT")


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Setup runs one session at a time: SQLite's busy timeout does not cover the switch to WAL,
    # so sessions creating the index together would otherwise fail with "database is locked".
    with setup_lock(db, fcntl.LOCK_EX):
        # Only to create it private: closing any descriptor of a file drops every POSIX lock this
        # process holds on it, including those of its open connections, which corrupts the index.
        if not db.exists():
            os.close(os.open(db, os.O_CREAT | os.O_RDWR, 0o600))
        conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            prepare(conn)
        except BaseException:
            # Damage found mid-upgrade leaves the write lock held here, and the reset that
            # follows would wait on it for the whole busy timeout and fail.
            conn.close()
            raise
    return conn


class DamagedRow(sqlite3.DatabaseError):
    """A stored row that indexing could not have written."""


def is_corrupt(error: sqlite3.Error) -> bool:
    # Python raises a failed decode itself, with no SQLite code. Indexing stores only valid text
    # (storable), so stored text that will not decode was damaged.
    if isinstance(error, DamagedRow) or str(error).startswith("Could not decode to UTF-8"):
        return True
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    return code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


def generation(db: Path) -> int | None:
    """Changes when a reset finishes: read it before the work that may find damage."""
    try:
        return os.stat(f"{db}.lock").st_mtime_ns
    except FileNotFoundError:
        return None


def was_reset_since(db: Path, seen: int | None) -> bool:
    with setup_lock(db, fcntl.LOCK_SH):
        return generation(db) != seen


def retrying_after_reset[T](db: Path, run: Callable[[], T]) -> T:
    """Run a read of the index at `db`. If it finds the index corrupt, reset it and run once
    more."""
    seen = generation(db)
    try:
        return run()
    except sqlite3.DatabaseError as e:
        # Any error during another session's reset is that reset's doing, so it only needs a retry.
        if not was_reset_since(db, seen):
            if not is_corrupt(e):
                raise
            reset(db, seen)
            print(
                f"The transcript index was corrupt ({e}). It was reset and is rebuilding.",
                file=sys.stderr,
            )
    return run()


def reset(db: Path, seen: int | None) -> None:
    """Empty a corrupt index in place and recreate its tables; queries refill them.

    Never remove the file for this: a session still connected to it deletes the write-ahead log by
    name when it closes, even once that name belongs to the new file. `seen` is the generation
    from before the damage was found, so sessions that found it together reset once and no session
    empties what another just rebuilt.
    """
    with setup_lock(db, fcntl.LOCK_EX):
        if generation(db) != seen:
            return
        conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            # Reading the schema first keeps WAL mode, which lets the VACUUM run beside other
            # sessions' idle connections. Damage may fail the read; the reset works regardless.
            try:
                conn.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
            except sqlite3.DatabaseError:
                pass
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_RESET_DATABASE, True)
            conn.execute("VACUUM")
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_RESET_DATABASE, False)
            prepare(conn)
        finally:
            conn.close()
        os.utime(f"{db}.lock")


def records(path: Path) -> list[transcripts.Record]:
    out = []
    with path.open(errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                out.append(record)
    return out


def blocks(record: transcripts.Record) -> list[dict[str, Any]]:
    content = transcripts.message_content(record)
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def ansi_c_text(body: str) -> str:
    """The text an ANSI-C quoted string `$'body'` stands for, its escapes decoded."""

    def decode(escape: re.Match[str]) -> str:
        code = escape.group(1)
        if code[0] in "xuU":
            return chr(min(int(code[1:], 16), 0x10FFFF))
        if code[0] in "01234567":
            return chr(int(code, 8))
        if code[0] == "c" and len(code) == 2:
            return chr(ord(code[1]) & 0x1F)
        return ANSI_C_LETTERS.get(code, "\\" + code)  # the shell keeps an unknown escape as written

    return ANSI_C_ESCAPE.sub(decode, body)


def runnable_text(command: str) -> tuple[str, bool]:
    """(`command` without its comments and heredoc bodies, whether it runs a substitution).

    A comment is a # that starts a word outside quotes, through the end of its line; shlex would
    also start one at a # inside a word. A heredoc body is data for the command that reads it,
    from the line after its `<<` outside quotes through the delimiter line. A substitution is
    `$(` or a backtick outside single quotes, or `<(` or `>(` outside any quotes, none of them
    escaped: it runs commands its words do not show. A `;`, `&`, `|`, parenthesis or newline inside
    quotes or escaped is data, not an operator: it becomes LITERAL_OPERATOR, so no word of the
    split reads as a separator. An ANSI-C quoted string outside quotes (`$'c\\x64'`) becomes the
    single-quoted text it decodes to, which shlex cannot read in its `$'...'` form.
    """
    out: list[str] = []
    quote = None
    substitutes = False
    delimiters: list[str] = []
    i = 0
    while i < len(command):
        char = command[i]
        pair = command[i : i + 2]
        heredoc = None
        if quote is None and pair == "<<" and command[i - 1 : i] != "<":
            heredoc = HEREDOC.match(command, i)
        ansi_c = ANSI_C.match(command, i) if quote is None and pair == "$'" else None
        if ansi_c:
            text = ansi_c_text(ansi_c.group(1))
            text = "".join(LITERAL_OPERATOR if c in OPERATOR_CHARS else c for c in text)
            out.append("'" + text.replace("'", "'\\''") + "'")
            i = ansi_c.end()
            continue
        if char == "\\" and quote != "'":
            escaped = command[i + 1 : i + 2]
            if quote is None and escaped == "\n":
                pass  # a line continuation
            elif escaped and escaped in OPERATOR_CHARS:
                out.append(LITERAL_OPERATOR)
            else:
                out.append(pair)
            i += 2
            continue
        if quote == "'":
            quote = None if char == "'" else quote
        elif pair == "$(" or char == "`":
            substitutes = True
        elif quote == '"':
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif heredoc:
            delimiters.append(heredoc.group(2))
            out.append(heredoc.group())
            i = heredoc.end()
            continue
        elif pair in ("<(", ">("):
            substitutes = True
        elif char == "#" and (not out or out[-1][-1:] in " \t\n;&|()"):
            end = command.find("\n", i)
            i = len(command) if end == -1 else end
            continue
        elif char == "\n" and delimiters:
            i += 1
            for delimiter in delimiters:
                while i < len(command):
                    end = command.find("\n", i)
                    end = len(command) if end == -1 else end
                    line, i = command[i:end], end + 1
                    if line.strip() == delimiter:
                        break
            delimiters = []
            out.append("\n")
            continue
        out.append(LITERAL_OPERATOR if quote and char in OPERATOR_CHARS else char)
        i += 1
    return "".join(out), substitutes


def simple_commands(command: str) -> list[list[str]] | None:
    """The words of each simple command `command` runs, or None when shlex cannot split it
    (unbalanced quotes) or a substitution runs commands its words do not show."""
    text, substitutes = runnable_text(command)
    if substitutes:
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=OPERATOR_CHARS)
    lexer.whitespace = " \t\r"  # a newline ends a command, so it is an operator here
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        words = commands[-1]
        after = tokens[i + 1] if i + 1 < len(tokens) else ""
        # The splitter cuts a redirection at its & or |: rejoin `2>` `&` `1`, `&` `>log`, `>` `|`.
        if token in ("&", "|") and words and words[-1].endswith((">", "<")):
            words[-1] += token + (after if token == "&" else "")
            i += 2 if token == "&" else 1
        elif token == "&" and after.startswith(">"):
            words.append(token + after)
            i += 2
        elif set(token) <= set(OPERATOR_CHARS):
            commands.append([])
            i += 1
        else:
            words.append(token)
            i += 1
    return [words for words in commands if words]


def command_name(words: list[str]) -> str | None:
    """The name of the command `words` run, past keywords, assignments, redirections and
    wrappers."""
    i = 0
    while i < len(words):
        word = words[i]
        redirection = REDIRECTION.match(word)
        if redirection:
            # The target is the next word when the operator stands alone (`2> /dev/null`).
            i += 2 if redirection.end() == len(word) else 1
            continue
        if word in KEYWORDS or ASSIGNMENT.match(word):
            i += 1
        elif word in WRAPPERS or word == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += word == "timeout"
        else:
            return Path(word).name
    return None


def moves_directory(name: str, tool_input: Any) -> bool:
    """Whether the call may change the directory the session's later calls run in: a worktree
    tool, or a Bash command running cd, pushd, popd or chdir (a subshell's cd counts too)."""
    if name in WORKTREE_TOOLS:
        return True
    if name != "Bash" or not isinstance(tool_input, dict):
        return False
    command = str(tool_input.get("command") or "")
    commands = simple_commands(command)
    if commands is None:
        return bool(CD.search(command))
    return any(command_name(words) in DIRECTORY_CHANGERS for words in commands)


def storable(text: str | None) -> str | None:
    """Text SQLite can hold: a lone surrogate (a tool output cut mid-character) becomes U+FFFD."""
    if text is None:
        return None
    try:
        text.encode()
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def leaf_strings(value: Any) -> Iterator[str]:
    """The keys and string values of a tool input, decoded. Searching its JSON instead would read
    the newline in "a\\nb" as the word "nb"."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from leaf_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from leaf_strings(item)
    elif isinstance(value, str):
        yield value


def extract(path: Path) -> Iterator[tuple[Row, str]]:
    """Every row of one file in file order, with no filter applied, each with the text full-text
    search reads. A record's text comes before its tool calls."""
    recs = records(path)
    results = {}
    for rec in recs:
        if rec.get("type") == "user":
            for b in blocks(rec):
                if b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = b
    message_id, cd_seen, branches = None, False, set()
    for rec in map(transcripts.queued_prompt_as_user_record, recs):
        role = rec.get("type")
        common = {
            "epoch": transcripts.record_epoch(rec),
            "ts": rec.get("timestamp"),
            "cwd": rec.get("cwd"),
            "uuid": rec.get("uuid"),
            "copied": transcripts.FORK_MARKER in rec,
        }
        branch = rec.get("gitBranch")
        if branch and branch not in branches:
            branches.add(branch)
            yield {**common, "source": "branch", "body": branch}, branch
        if role in ("user", "assistant") and isinstance(rec.get("message"), dict):
            text = transcripts.message_text(rec)
            injected = transcripts.injected_reason(rec) if role == "user" else None
            parts = [(injected, text)]
            if role == "user" and injected is None and transcripts.SIDE_AGENT_NOTE.search(text):
                # Typed words and a note in one send are a row each, so a search over typed
                # messages reads only the person's words and the note keeps its own label.
                typed = transcripts.SIDE_AGENT_NOTE.sub("", text).strip()
                parts = [(None, typed), ("side-agent-note", transcripts.side_agent_notes(text))]
            for reason, body in parts:
                if body:
                    row = {**common, "source": "message", "role": role, "injected": reason}
                    yield {**row, "body": body}, body
        if role == "assistant":
            # Each tool_use of one message is its own record sharing message.id.
            message = rec.get("message")
            this_id = message.get("id") if isinstance(message, dict) else None
            if this_id != message_id:
                message_id, cd_seen = this_id, False
            for b in blocks(rec):
                if b.get("type") != "tool_use":
                    continue
                name = b.get("name") or ""
                tool_input = b.get("input")
                paired = results.get(b.get("id"))
                row = {
                    **common,
                    "source": "tool",
                    "tool": name,
                    "call_id": b.get("id"),
                    "cwd_uncertain": int(cd_seen),
                    "body": json.dumps(tool_input, ensure_ascii=False),
                    "result": result_text(paired.get("content")) if paired else None,
                    "is_error": int(bool(paired.get("is_error"))) if paired else None,
                }
                cd_seen = cd_seen or moves_directory(name, tool_input)
                yield row, "\n".join(leaf_strings(tool_input))


def read_snapshot(conn: sqlite3.Connection, db: Path, paths: Iterable[Path]) -> None:
    """Bring `paths` up to date and open one read snapshot holding them, catching up again if
    another session reset the index in between, which would leave a successful query with rows
    missing. The shared lock waits out a reset in progress, so a generation read here means no
    reset is running."""
    paths = list(paths)
    while True:
        with setup_lock(db, fcntl.LOCK_SH):
            seen = generation(db)
        catch_up(conn, db, paths)
        with setup_lock(db, fcntl.LOCK_SH):
            if generation(db) == seen:
                conn.execute("BEGIN")
                conn.execute("SELECT 1 FROM files LIMIT 1").fetchone()
                return


def born(path: Path) -> float:
    """When the file (a link itself, not its target) was created, where the file system records
    it; elsewhere its first record time."""
    stat = path.lstat()
    created = getattr(stat, "st_birthtime", None)
    return created if created is not None else transcripts.first_epoch(path)


def index_file(path: Path, db: Path) -> None:
    """Replace one file's rows in one transaction, one process per file from stat to commit, so a
    session that read older contents cannot replace a later session's rows. The stat comes first,
    so a write landing during the read leaves a size that differs and the next query indexes the
    file again. A file that cannot be read stores nothing, so the next query tries it again."""
    with open(os.open(f"{db}.files", os.O_CREAT | os.O_RDWR, 0o600)) as locks:
        fcntl.lockf(locks, fcntl.LOCK_EX, 1, zlib.crc32(os.fsencode(path)))
        conn = connect(db)
        try:
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime_ns, VERSION)
            stored = conn.execute(
                "SELECT size, mtime_ns, version FROM files WHERE path = ?", (str(path),)
            ).fetchone()
            if stored != current:
                rows = list(extract(path))
                replace_rows(conn, path, current, born(path), path.is_symlink(), rows)
        except OSError:
            return
        finally:
            conn.close()


def replace_rows(
    conn: sqlite3.Connection,
    path: Path,
    current: tuple[int, int, str],
    created: float,
    linked: bool,
    rows: list[tuple[Row, str]],
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    file_id = conn.execute(
        "INSERT INTO files (path, size, mtime_ns, version, born) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (path) DO UPDATE SET size = excluded.size, mtime_ns = excluded.mtime_ns, "
        "version = excluded.version, born = excluded.born RETURNING id",
        (str(path), *current, created),
    ).fetchone()[0]
    delete_rows(conn, file_id)
    for row, search_text in rows:
        body, result = storable(row["body"]), storable(row.get("result"))
        row_id = conn.execute(
            "INSERT INTO rows (file_id, source, epoch, ts, cwd, role, injected, tool, call_id, "
            "cwd_uncertain, is_error, uuid, copied, body, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                row["source"],
                row["epoch"],
                storable(row["ts"]),
                storable(row["cwd"]),
                row.get("role"),
                row.get("injected"),
                storable(row.get("tool")),
                storable(row.get("call_id")),
                row.get("cwd_uncertain"),
                row.get("is_error"),
                storable(row["uuid"]),
                int(row["copied"] or linked),
                body,
                result,
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO rows_text (rowid, body, result) VALUES (?, ?, ?)",
            (row_id, storable(search_text), result),
        )
    conn.execute("COMMIT")


def delete_rows(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute(
        "DELETE FROM rows_text WHERE rowid IN (SELECT id FROM rows WHERE file_id = ?)", (file_id,)
    )
    conn.execute("DELETE FROM rows WHERE file_id = ?", (file_id,))


def catch_up(conn: sqlite3.Connection, db: Path, paths: Iterable[Path]) -> None:
    """Bring `paths` up to date and drop files that no longer exist."""
    known = {
        path: (file_id, size, mtime_ns, version)
        for file_id, path, size, mtime_ns, version in conn.execute(
            "SELECT id, path, size, mtime_ns, version FROM files"
        )
    }
    stale = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if known.get(str(path), (None,))[1:] != (stat.st_size, stat.st_mtime_ns, VERSION):
            stale.append(path)
    if len(stale) >= POOL_THRESHOLD:
        with ProcessPoolExecutor(WORKERS) as pool:
            list(pool.map(index_file, stale, repeat(db), chunksize=4))
    else:
        for path in stale:
            index_file(path, db)
    for path, (file_id, *_) in known.items():
        if not os.path.exists(path):
            conn.execute("BEGIN IMMEDIATE")
            # Checked again holding the write lock: another session may have recreated it.
            if not os.path.exists(path):
                delete_rows(conn, file_id)
                conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            conn.execute("COMMIT")
