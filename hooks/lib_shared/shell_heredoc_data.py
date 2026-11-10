"""
Which heredoc bodies of a Bash command are data, and the command with them cut.

A body that only a program that takes it as text reads (a file written with `cat > f.md <<EOF`,
python's stdin, a commit message, a pull request body) runs nothing, so a command it names
(`terraform apply`) is a mention, not a call. A guard that matches the command line as written reads
`strip_data_heredoc_bodies(command)`: every body that may run, a shell's or one for a program the
reader does not know, stays in the text (see _data_heredocs for the rules). With `interpreted=True`,
python's or node's stdin and a source file written that a later command runs (`python3 x.py`,
`pytest x.py`) are code too; without it they are text, as shell patterns and simple-command readers
take them. The heredocs themselves come from shell_comments.shell_heredocs; the tokens from
shell_tokens.

_last_runs_of_files is the one tracker of which files a command runs (with _files_written and
_later_run, which files it writes and where a later command runs one): _data_heredocs, the readers
of scripts fed on stdin, _heredoc_runs and _run_file_paths all ask it whether a written file runs.
By default it takes every run that may happen, matching a file by its name; with `proven=True` it
keeps only the runs proven by a file's path word, with no shell input and no crontab.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from dataclasses import dataclass
from pathlib import Path

from shell_comments import shell_heredocs
from shell_tokens import (
    _FD_NUMBER_RE,
    _MAX_STDIN_LIST_DEPTH,
    _NESTED_SHELL_NAMES,
    _NO_FILE_TARGETS,
    _PIPE_TOKENS,
    _RESERVED_PREFIXES,
    _SEGMENT_BREAK_TOKENS,
    _STDIN_FREE_COMMANDS,
    _command_tokens,
    _command_words,
    _gh_path,
    _git_globals,
    _redirection,
    _segments_with_breaks,
    _shell_c_payload_indexes,
    _shell_script_indexes,
    _shell_tokens,
    _ssh_remote_words,
    _without_redirections,
    _writes_file,
)


@dataclass(frozen=True)
class _Heredoc:
    start: int  # the `<<`
    word_end: int  # the end of the delimiter word
    tag: str  # the delimiter word without its quotes
    # past the newline before the body's first line: the one that ends the line the
    # heredoc opens on, or, for a heredoc opened on the line of one before it, the one
    # that ends that one's closing line; one past `end` when no newline comes before
    # `end` (a body that ends in the backtick body it opens in, or the text ends first)
    body_start: int
    body_end: int  # the newline before the closing line, or the end of the text
    end: int  # the end of the tag that closes the body, or of the text

    @property
    def closed(self) -> bool:
        """Whether a line closes the body. The bare `<<TAG` that _cut_heredoc_bodies
        leaves where a body was has none, and reads as a body that runs to the end."""
        return self.end > self.body_end


def _heredocs(text: str) -> list[_Heredoc]:
    """Each heredoc in *text* (shell_heredocs), in order, from its `<<` to the
    tag on the line that closes its body (shell_comments.heredoc_closes), or to
    the end of the text, or of its backtick body, when no line does. Heredocs
    opened on one line (`cat <<A <<B`) read their bodies in turn, each after
    the closing line of the one before (_Heredoc.body_start), so such a later
    heredoc starts before the one ahead of it ends; their bodies never
    overlap."""
    return list(_cached_heredocs(text))


@functools.lru_cache(maxsize=256)
def _cached_heredocs(text: str) -> tuple[_Heredoc, ...]:
    found: list[_Heredoc] = []
    for start, word_end, tag, body_end, end in shell_heredocs(text):
        # a heredoc opened on the line of the one before it reads its body after that one's
        after = found[-1].end if found and start < found[-1].end else word_end
        newline = text.find("\n", after, end)
        body_start = newline + 1 if newline != -1 else end + 1
        found.append(_Heredoc(start, word_end, tag, body_start, body_end, end))
    return tuple(found)


def _cut_heredoc_bodies(text: str, heredocs: list[_Heredoc] | None = None) -> tuple[str, list[str]]:
    """*text* with each heredoc body cut (_heredocs, or the given *heredocs*),
    from the newline before it to its closing tag, and a `<<TAG` marker left
    for its opener, and the text after the first opener on each line. The shell
    reads a body from the lines after the one that opens it, so the rest of
    that line runs (`python3 <<EOF && pytest -q`) and stays, the other openers
    on it too (`cat <<A <<B`), each body after the one before."""
    heredocs = _heredocs(text) if heredocs is None else heredocs
    tails: list[str] = []
    for before, heredoc in zip([None, *heredocs], heredocs):  # noqa: B905
        if before is not None and heredoc.start < before.end:
            continue  # opened on the line of the one before: its tail holds this one's
        line_end = text.find("\n", heredoc.word_end, heredoc.end)
        if line_end == -1:
            line_end = heredoc.end  # a body that ends in the backtick body it opens in
        tail = text[heredoc.word_end : line_end]
        if tail.strip():
            tails.append(tail)
    parts: list[str] = []
    last = 0
    for start, end, marker in _heredoc_cuts(heredocs):
        parts += [text[last:start], marker]
        last = end
    parts.append(text[last:])
    return "".join(parts), tails


def _heredoc_cuts(heredocs: list[_Heredoc]) -> list[tuple[int, int, str]]:
    """The edits _cut_heredoc_bodies makes for *heredocs*, in the order of the
    text, as (start, end, what replaces the text between): each opener's
    delimiter word made a bare `<<TAG`, and each body cut from the newline
    before it to its closing tag. A body that opens no line has none to cut."""
    cuts: list[tuple[int, int, str]] = []
    for heredoc in heredocs:
        cuts.append((heredoc.start, heredoc.word_end, f"<<{heredoc.tag}"))
        if heredoc.body_start <= heredoc.end:
            cuts.append((heredoc.body_start - 1, heredoc.end, ""))
    return sorted(cuts)


def _expands_body(text: str, heredoc: _Heredoc) -> bool:
    """Whether the shell expands the body of *heredoc* in *text*: its delimiter
    word holds no quote or backslash (`<<EOF`, not `<<'EOF'`, `<<"EOF"`,
    `<<\\EOF`)."""
    return not set(text[heredoc.start : heredoc.word_end]) & {"'", '"', "\\"}


# The programs that take a heredoc body as text, never as commands, by printing it or
# writing it to a file (cat, tee, dd, sponge). _reads_text also takes python or node
# when stdin is the script, `git commit -F -`, `git apply`, `kubectl apply -f -` and the
# gh commands of _GH_STDIN_OPTIONS. A body for any other program, a shell, `sudo`, `.`,
# a client (`psql` runs a `\!` line in a shell), `crontab -` (cron runs each line), an
# interpreter given a script or one _INTERPRETER_RE does not know, may run, so a
# program is code until _reads_text takes it.
_TEXT_PRINTERS = {"cat", "tee", "dd", "sponge"}
_INTERPRETER_RE = re.compile(r"node|python[\d.]*t?")
# The options of an interpreter that leave its script on stdin: python's flags of one
# letter that take no value, node's `--` options but the ones that evaluate code.
_STDIN_SCRIPT_OPTION_RE = re.compile(
    r"-[BEIOPRSbdiqsuvx]+|--(?!eval|print|check|test|run)[\w-]+(?:=.*)?"
)
# The words that open and close a compound command where a command could start; a
# `{` or `(` group is told apart by its opening word (_enclosures).
_COMPOUND_OPENERS = {"for", "select", "while", "until", "if", "case"}
_COMPOUND_CLOSERS = {"done", "fi", "esac"}
# The files a data reader may write a body to: data and source of a kind no shell runs.
# A script, a startup file or a hook (no extension, or `.sh`) and SQL (`.sql`, which a
# client runs) are not here. A source file (_SOURCE_FILE_SUFFIXES) is code to an
# `interpreted` reading once a later command of the text names it.
_SOURCE_FILE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".tsx"})
_DATA_FILE_SUFFIXES = (
    frozenset(
        {
            ".md", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".csv", ".tsv",
            ".html", ".css", ".diff", ".patch",
        }
    )
    | _SOURCE_FILE_SUFFIXES
)  # fmt: skip
# The tag _data_heredocs gives each heredoc, with its number, while it reads the text.
_HEREDOC_MARK = "HEREDOC_BODY_"


@dataclass(frozen=True)
class _Enclosures:
    """The `(...)` and `{ ...; }` groups and the compound commands of a token list,
    each by its number in the order they open (_enclosures)."""

    kinds: list[str]  # its opening word
    parents: list[int | None]  # the one around it
    closes: list[int | None]  # the position of its closing word, None when it never closes
    innermost: list[int | None]  # for each token position, the innermost one open there


def _enclosures(tokens: list[str]) -> _Enclosures:
    """Each `(...)` or `{ ...; }` group and each compound command (`for`, `while`,
    `until`, `select`, `if`, `case` to its closing word) of *tokens*: the walk that
    the data heredoc reader (_data_heredocs) and shell_runs' fed-script reader
    both read groups from. A closing word closes the innermost one open, and only
    when that one is of its kind (`)` a `(`, `}` a `{`, `done`, `fi` or `esac` a
    compound command), so a case arm's `)` closes no group; a `{`, `}` or reserved
    word counts only where a command starts: at a segment break, or past the reserved
    words and the `time [-p]` that go before a command."""
    found = _Enclosures([], [], [], [])
    open_now: list[int] = []
    starts_command = True
    for pos, token in enumerate(tokens):
        top = open_now[-1] if open_now else None
        if token == "(" or (starts_command and (token == "{" or token in _COMPOUND_OPENERS)):
            found.kinds.append(token)
            found.parents.append(top)
            found.closes.append(None)
            open_now.append(len(found.kinds) - 1)
        elif top is not None and (
            (token == ")" and found.kinds[top] == "(")
            or (starts_command and token == "}" and found.kinds[top] == "{")
            or (
                starts_command
                and token in _COMPOUND_CLOSERS
                and found.kinds[top] in _COMPOUND_OPENERS
            )
        ):
            found.closes[top] = pos
            open_now.pop()
        found.innermost.append(open_now[-1] if open_now else None)
        starts_command = token in _SEGMENT_BREAK_TOKENS or (
            starts_command
            and (
                token in _RESERVED_PREFIXES
                or token == "time"
                or (token == "-p" and pos > 0 and tokens[pos - 1] == "time")
            )
        )
    return found


def _data_heredocs(text: str, *, interpreted: bool = False) -> list[_Heredoc]:
    """The heredocs of *text* whose body is data: only a program that takes it as text reads it
    (_reads_text: cat, tee, dd, sponge, python or node with stdin as the script, `git commit -F -`,
    which takes it as the message, `git apply`, `kubectl apply -f -`, `gh pr create --body-file -`),
    and nothing carries it on. A command named in one runs nothing. Every other body is code, or may
    be, and stays in the text a guard reads. With *interpreted*, python's or node's stdin is code
    too, and so is a source file written (_SOURCE_FILE_SUFFIXES) that a later command names
    (`python3 x.py`, `pytest x.py`, `node x.mjs`):

    - a program not listed, a shell included, or none the reader can name
      (`sudo -s`, `su`, `. /dev/stdin`, `$READER`, a loop's `done`), or python or
      node given a script, a module or code of its own (`python3 manage.py
      dbshell`, `python3 -c`), whose stdin is input to a program the reader cannot see;
    - the reader's output goes on: a pipe, a process substitution, a group or a
      substitution around it, a compound command around it whose closing word
      sends its output on (`done | bash`, `fi > run.sh`), a line continuation that
      moves the rest of its line below the opener;
    - a file written that a shell may run: one with no data extension
      (_DATA_FILE_SUFFIXES: `x.sh`, a hook, `~/.zshrc`), a target the reader
      cannot name (`"$F"`), or one a later command runs (_last_runs_of_files:
      `./x.sh`, `bash -e x.sh`, `. x.sh`, `ssh host bash x.md`, after a `cp`; a file
      given to a shell as input, `bash bench.sh in.txt`, is not);
    - an unquoted body that holds a `$(...)` or a backtick, which the shell runs;
    - any body in a text that cannot be tokenized."""
    return list(_cached_data_heredocs(text, interpreted))


@functools.lru_cache(maxsize=256)
def _cached_data_heredocs(text: str, interpreted: bool) -> tuple[_Heredoc, ...]:
    heredocs = _heredocs(text)
    marked = [
        dataclasses.replace(heredoc, tag=f"{_HEREDOC_MARK}{idx}")
        for idx, heredoc in enumerate(heredocs)
    ]
    tokens = _command_tokens(_FD_NUMBER_RE.sub("", _cut_heredoc_bodies(text, marked)[0]))
    if tokens is None:
        return ()
    # Each command of the cut text, past the reserved words that open it, with the
    # operator that ends it, and the command at each token position; for each heredoc
    # the command it feeds and the innermost group or compound command around it
    # (_enclosures).
    enclosures = _enclosures(tokens)
    segments: list[tuple[list[str], str | None]] = []
    segment_at: list[int] = []
    feeds: dict[int, tuple[int, int | None]] = {}
    current: list[str] = []
    for pos, token in enumerate(tokens):
        segment_at.append(len(segments))
        if token in _SEGMENT_BREAK_TOKENS:
            segments.append((_without_reserved_prefix(current), token))
            current = []
            continue
        if pos and tokens[pos - 1] == "<<" and token.startswith(_HEREDOC_MARK):
            number = token[len(_HEREDOC_MARK) :]
            if number.isdigit():
                feeds[int(number)] = (len(segments), enclosures.innermost[pos])
        current.append(token)
    segments.append((_without_reserved_prefix(current), None))
    # whether a group or compound command, or one around it, sends its output on: a
    # group, or a compound command that never closes or whose closing command pipes,
    # hands it to a process substitution or writes it to a file (`done | bash`,
    # `fi > run.sh`)
    sends_on: list[bool] = []
    for kind, around, close in zip(
        enclosures.kinds, enclosures.parents, enclosures.closes, strict=True
    ):
        closing = segments[segment_at[close]] if close is not None else None
        sends_on.append(
            (around is not None and sends_on[around])
            or kind in ("(", "{")
            or closing is None
            or _pipes_output(*closing)
            or bool(_written_files(closing[0], closing[0]))
        )
    runs_file: dict[tuple[str, bool], int] | None = None
    names_file: dict[str, int] | None = None

    def takes_text(index: int) -> bool:
        """Whether the command at *index* takes the bodies it is fed as text and
        nothing runs what it writes."""
        nonlocal runs_file, names_file
        words, operator = segments[index]
        if _pipes_output(words, operator):
            return False
        command = _command_words(_without_redirections(words))
        if not command or not _reads_text(command):
            return False
        if interpreted and _INTERPRETER_RE.fullmatch(Path(command[0]).name):
            return False
        files = _written_files(words, command)
        if any(Path(file).suffix.lower() not in _DATA_FILE_SUFFIXES for file in files):
            return False
        if files:
            if runs_file is None:
                runs_file = _last_runs_of_files(segments)
            if _later_run([(file, False) for file in files], index, runs_file):
                return False
        sources = [file for file in files if Path(file).suffix.lower() in _SOURCE_FILE_SUFFIXES]
        if interpreted and sources:
            if names_file is None:
                names_file = _last_mentions_of_files(segments)
            if any(names_file.get(Path(file).name, -1) > index for file in sources):
                return False
        return True

    # each command's reading, once: the heredocs opened on one line feed it in turn
    text_takers: dict[int, bool] = {}
    data: list[_Heredoc] = []
    for idx, heredoc in enumerate(heredocs):
        line_end = text.find("\n", heredoc.word_end, heredoc.end)
        if line_end == -1:
            data.append(heredoc)
            continue
        if idx not in feeds:
            continue
        # a continued opener line: the shell reads the rest of the logical line, which
        # _cut_heredoc_bodies reads as the body
        tail = text[heredoc.word_end : line_end]
        if (len(tail) - len(tail.rstrip("\\"))) % 2:
            continue
        index, innermost = feeds[idx]
        if innermost is not None and sends_on[innermost]:
            continue
        body = text[heredoc.body_start : heredoc.body_end]
        if _expands_body(text, heredoc) and ("$(" in body or "`" in body):
            continue
        if index not in text_takers:
            text_takers[index] = takes_text(index)
        if text_takers[index]:
            data.append(heredoc)
    return tuple(data)


def _without_reserved_prefix(words: list[str]) -> list[str]:
    """*words* past the reserved words that open a command (`do`, `then`, `if`, `{`:
    _RESERVED_PREFIXES): `do cat > f` is the command `cat > f`."""
    start = 0
    while start < len(words) and words[start] in _RESERVED_PREFIXES:
        start += 1
    return words[start:]


def _pipes_output(words: list[str], operator: str | None) -> bool:
    """Whether the command *words*, ended by *operator*, hands its output on: a pipe,
    or a process substitution (`> >(bash)`: the `(` ends the command after its
    redirect)."""
    return operator in _PIPE_TOKENS or bool(words and _redirection(words[-1]))


def _reads_text(command: list[str]) -> bool:
    """Whether the command *command* (its words from the program on) takes a heredoc
    body as text: _TEXT_PRINTERS, python or node when stdin is the script
    (_INTERPRETER_RE: `python3 -`, `node`, with no script, module or code of its own
    and none of the options _STDIN_SCRIPT_OPTION_RE leaves out; what follows a `-` is
    argv), `git commit -F -`, which takes it as the message, `git apply` and
    `kubectl apply -f -`, which take it as a patch or a manifest (_reads_applied_text),
    and a gh command given its stdin as the text it stores or sends (_reads_gh_text)."""
    program = Path(command[0]).name
    if program in _TEXT_PRINTERS:
        return True
    if _INTERPRETER_RE.fullmatch(program):
        for word in command[1:]:
            if word == "-":
                return True
            if not _STDIN_SCRIPT_OPTION_RE.fullmatch(word):
                return False
        return True
    return _reads_commit_message(command) or _reads_applied_text(command) or _reads_gh_text(command)


def _reads_applied_text(command: list[str]) -> bool:
    """Whether the command *command* applies its stdin as data: a `git apply` given no
    patch file (a word after it that is no option, the value of an option given as a
    word of its own included, reads as one), or a `kubectl apply` given `-f -`."""
    program, args = Path(command[0]).name, command[1:]
    if program == "git":
        args = args[_git_globals(args).subcommand :]
        return args[:1] == ["apply"] and all(word.startswith("-") for word in args[1:])
    if program == "kubectl" and args[:1] == ["apply"]:
        return any(
            word in ("-f-", "-f=-", "--filename=-")
            or (word in ("-f", "--filename") and value == "-")
            for word, value in zip(args, [*args[1:], ""])  # noqa: B905
        )
    return False


# How a gh command names stdin: a file option's value `-`, a typed field's value `name=@-`
# (`gh help api`), the presence of a flag, or an operand `-`.
_STDIN_FILE, _STDIN_FIELD, _STDIN_FLAG, _STDIN_OPERAND = "file", "field", "flag", "operand"
_GH_BODY_FILE = ("F", ("--body-file",), _STDIN_FILE)
_GH_NOTES_FILE = ("F", ("--notes-file",), _STDIN_FILE)
_GH_TASK_FILE = ("F", ("--from-file",), _STDIN_FILE)
_GH_FIELD = ("F", ("--field",), _STDIN_FIELD)
_GH_STDIN_OPERAND = (("", (), _STDIN_OPERAND),)
# How a gh command reads its stdin whole as the text it stores or sends (a body, release
# notes, a task description, a request body or its fields, workflow inputs, a token, a gist
# file), by the command's path with the aliases `gh help reference` lists (gh 2.92): the
# letters and long names of the option that names it, and how it does. `-F` is a body,
# notes or task file on most and a field on `api` and `workflow run`, so the table is by
# path. None runs the text: it goes to GitHub, or a token to gh's credential store. A
# command it does not list stays code: an extension runs a program of its own, `alias set`
# stores an expansion that gh, or a shell with `--shell`, runs later, and `gist create`
# with no operand, `secret set` and `variable set` with no `--body` read stdin with
# nothing in the command that names it.
_GH_STDIN_OPTIONS: dict[tuple[str, ...], tuple[tuple[str, tuple[str, ...], str], ...]] = {
    ("issue", "comment"): (_GH_BODY_FILE,),
    ("issue", "create"): (_GH_BODY_FILE,),
    ("issue", "new"): (_GH_BODY_FILE,),
    ("issue", "edit"): (_GH_BODY_FILE,),
    ("pr", "comment"): (_GH_BODY_FILE,),
    ("pr", "create"): (_GH_BODY_FILE,),
    ("pr", "new"): (_GH_BODY_FILE,),
    ("pr", "edit"): (_GH_BODY_FILE,),
    ("pr", "merge"): (_GH_BODY_FILE,),
    ("pr", "revert"): (_GH_BODY_FILE,),
    ("pr", "review"): (_GH_BODY_FILE,),
    ("release", "create"): (_GH_NOTES_FILE,),
    ("release", "new"): (_GH_NOTES_FILE,),
    ("release", "edit"): (_GH_NOTES_FILE,),
    ("agent-task", "create"): (_GH_TASK_FILE,),
    ("agent-tasks", "create"): (_GH_TASK_FILE,),
    ("agent", "create"): (_GH_TASK_FILE,),
    ("agents", "create"): (_GH_TASK_FILE,),
    ("api",): (("", ("--input",), _STDIN_FILE), _GH_FIELD),
    ("workflow", "run"): (_GH_FIELD, ("", ("--json",), _STDIN_FLAG)),
    ("auth", "login"): (("", ("--with-token",), _STDIN_FLAG),),
    ("gist", "create"): _GH_STDIN_OPERAND,
    ("gist", "new"): _GH_STDIN_OPERAND,
}


def _reads_gh_text(command: list[str]) -> bool:
    """Whether the command *command* (its words from the program on) is a gh command
    given an option that makes it read its stdin as the text it stores or sends
    (_GH_STDIN_OPTIONS): the entry of its path, the words that lead its arguments with
    gh's repo selector among them passed over (_gh_path). Not one whose path another
    option breaks (`gh --jq .x pr create`), an option in a cluster of short ones (`-dF
    -`), or a value that is no stdin (`--body-file b.md`, `/dev/stdin`): its stdin
    stays code."""
    if Path(command[0]).name != "gh":
        return False
    args = command[1:]
    path, ends = _gh_path(args, 3)
    for size in range(len(path), 0, -1):
        if (options := _GH_STDIN_OPTIONS.get(path[:size])) is not None:
            return any(_names_stdin(args[ends[size - 1] :], *option) for option in options)
    return False


def _names_stdin(args: list[str], letters: str, longs: tuple[str, ...], reads: str) -> bool:
    """Whether *args*, the options of a gh command, give an option of *letters* or
    *longs* the form that makes it read stdin (*reads*): `-F -`, `-F-`, `-F=-`,
    `--body-file -`, `--body-file=-`, `--field a=@-`, `--json`, a `-` operand."""
    if reads == _STDIN_OPERAND:
        return "-" in args
    for word, following in zip(args, [*args[1:], ""]):  # noqa: B905
        if word.startswith("--"):
            name, equals, value = word.partition("=")
            if name not in longs:
                continue
            if reads == _STDIN_FLAG:
                if not equals:
                    return True
                continue
            value = value if equals else following
        elif len(word) > 1 and word[0] == "-" and word[1] in letters:
            value = word[2:].removeprefix("=") or following
        else:
            continue
        if reads == _STDIN_FILE:
            if value == "-":
                return True
        elif value.partition("=")[2] == "@-":
            return True
    return False


def _commit_file_source(commit_line: str) -> str | None:
    """Where a commit reads its message from: the <src> of `-F <src>`, `-F<src>`,
    `--file <src>` or `--file=<src>` (`-` is stdin), else None.

    Read from shell tokens up to the end of the commit's own command, so a `-F` inside a
    quoted `-m` message or in a later chained command is not one. None as well when the
    line cannot be tokenized.
    """
    tokens = _shell_tokens(commit_line)
    if tokens is None:
        return None
    for idx, token in enumerate(tokens):
        if token in _SEGMENT_BREAK_TOKENS:
            return None
        if token in ("-F", "--file"):
            return tokens[idx + 1] if idx + 1 < len(tokens) else None
        if token.startswith("--file="):
            return token[len("--file=") :]
        if token.startswith("-F"):
            return token[2:]
    return None


def _reads_commit_message(command: list[str]) -> bool:
    """Whether the command *command* is a `git commit` that reads its message from
    stdin (`-F -`)."""
    if Path(command[0]).name != "git" or "commit" not in command:
        return False
    return _commit_file_source(" ".join(command[command.index("commit") :])) == "-"


def _written_files(words: list[str], command: list[str]) -> list[str]:
    """The files the command *words* writes: the target of each redirection that
    writes one (`> f`, `>> f`, `&> f`; not `2>&1` or `>/dev/null`), the operands of
    `tee` and `sponge`, and the `of=` file of `dd`. *command* is its words from the
    program on."""
    files = [target for word, target in zip(words, words[1:]) if _writes_file(word, target)]  # noqa: B905
    program = Path(command[0]).name
    if program in ("tee", "sponge"):
        files += [word for word in command[1:] if not word.startswith("-")]
    elif program == "dd":
        files += [word[3:] for word in command[1:] if word.startswith("of=")]
    return [file for file in files if file not in _NO_FILE_TARGETS]


def _nested_lists(command: list[str]) -> list[tuple[list[tuple[list[str], str | None]], bool]]:
    """(its commands, past the reserved words that open each, with the operator that
    ends each; whether ssh runs it on another machine) of each command list that the
    command *command* (its words from the program on) runs: a shell's `-c` list
    (_shell_c_payload_indexes) and an ssh remote command (_ssh_remote_words). A list
    that does not tokenize is left out. The writes and the runs of a text read the
    same commands' lists, so each is read once."""
    return _cached_nested_lists(tuple(command))


@functools.lru_cache(maxsize=1024)
def _cached_nested_lists(
    command: tuple[str, ...],
) -> list[tuple[list[tuple[list[str], str | None]], bool]]:
    name = Path(command[0]).name
    if name in _NESTED_SHELL_NAMES:
        texts = [command[idx] for idx in _shell_c_payload_indexes(command, 0)]
    elif name == "ssh":
        remote = _ssh_remote_words(command)
        texts = [" ".join(remote)] if remote else []
    else:
        return []
    lists = []
    for text in texts:
        tokens = _shell_tokens(text)
        if tokens is not None:
            segments = [
                (_without_reserved_prefix(words), op) for words, op in _segments_with_breaks(tokens)
            ]
            lists.append((segments, name == "ssh"))
    return lists


def _files_written(
    words: list[str], command: list[str], over_ssh: bool = False, depth: int = 0
) -> list[tuple[str, bool]]:
    """(file, written over ssh) for each file the command *words* writes
    (_written_files; *command* is its words from the program on) and each file a
    command writes in a command list it runs (_nested_lists: `sh -c 'cat > f'`, `ssh
    host 'cat > f'`), with the file a copy or a move in that list makes of one of
    them (_file_copy: `sh -c 'cat > r.tmp && mv r.tmp r.sh'`), read
    _MAX_STDIN_LIST_DEPTH lists deep."""
    files = [(file, over_ssh) for file in _written_files(words, command)]
    if depth < _MAX_STDIN_LIST_DEPTH:
        for segments, remote in _nested_lists(command):
            names = {Path(file).name for file, _ in files}
            for inner, _ in segments:
                inner_command = _command_words(_without_redirections(inner))
                if not inner_command:
                    continue
                written = _files_written(inner, inner_command, over_ssh or remote, depth + 1)
                copy = _file_copy(inner_command)
                if copy and any(Path(source).name in names for source in copy[0]):
                    written.append((copy[1], over_ssh or remote))
                files += written
                names.update(Path(file).name for file, _ in written)
    return files


# Commands that copy or move the file of their first operands to their last one.
_FILE_COPIES = {"cp", "mv", "install"}


def _file_copy(command: list[str]) -> tuple[list[str], str] | None:
    """(the files copied, where to) of the command *command* when it is a copy or a
    move (_FILE_COPIES: `cp a b`, `mv a b c/`), else None."""
    if Path(command[0]).name not in _FILE_COPIES:
        return None
    operands = [word for word in command[1:] if not word.startswith("-")]
    return (operands[:-1], operands[-1]) if len(operands) > 1 else None


def _file_name(word: str) -> str:
    """The name of the file the word *word* names (`/tmp/x.sh`: `x.sh`)."""
    return Path(word).name


def _path_word(word: str) -> str:
    """The word *word* that names a file without a leading `./` (`./x.sh`: `x.sh`),
    so two words that name one path as written compare equal."""
    return word.removeprefix("./")


def _last_runs_of_files(
    segments: list[tuple[list[str], str | None]],
    over_ssh: bool = False,
    depth: int = 0,
    *,
    proven: bool = False,
) -> dict[tuple[str, bool], int]:
    """For each file name that a command of *segments* runs, and whether it runs it
    over ssh, the last command that does: as the program (`./x.sh`, `x.sh`; a bare
    _STDIN_FREE_COMMANDS word is that command, `test -f test`), as the script of a
    shell (_shell_script_indexes: `bash -o pipefail x.sh`; not `bash -n x.sh`),
    `source` or `.` (`. ./x.sh`), as the input of a shell (`sh < x.sh`, `cat x.sh |
    sh`), as a crontab (`crontab x`, which cron runs), or in a command list it runs
    (_nested_lists: `sh -c 'bash x.sh'`, `ssh host bash x.sh`, over ssh), read
    _MAX_STDIN_LIST_DEPTH lists deep. A file copied or moved to one that runs later
    (`cp`, `mv`, `install`) runs then too. A file a shell is given as an argument of
    its script (`bash bench.sh in.txt`) is not run. *proven* reads only the runs
    that happen now, by each file's path word (_path_word) in place of its name: no
    shell input, which `bash -n < x.sh` and `printf x.sh | sh` share with a run,
    and no crontab."""
    key = _path_word if proven else _file_name
    last: dict[tuple[str, bool], int] = {}
    copies: list[tuple[int, list[str], str]] = []
    for index, (words, operator) in enumerate(segments):
        command = _command_words(_without_redirections(words))
        if not command:
            continue
        name = Path(command[0]).name
        if "/" in command[0] or name not in _STDIN_FREE_COMMANDS:
            last[(key(command[0]), over_ssh)] = index
        sources = command[0] in (".", "source")
        ran: list[str] = []
        if sources:
            ran = [word for word in command[1:] if not word.startswith("-")][:1]
        elif name in _NESTED_SHELL_NAMES:
            ran = [command[idx] for idx in _shell_script_indexes(command, 0)]
        elif name == "crontab" and not proven:
            ran = [word for word in command[1:] if not word.startswith("-")]
        elif copy := _file_copy(command):
            copies.append((index, *copy))
        if (sources or name in _NESTED_SHELL_NAMES) and not proven:
            ran += [target for word, target in zip(words, words[1:]) if word == "<"]  # noqa: B905
        last.update(((key(file), over_ssh), index) for file in ran)
        if depth < _MAX_STDIN_LIST_DEPTH:
            for inner, remote in _nested_lists(command):
                inner_runs = _last_runs_of_files(
                    inner, over_ssh or remote, depth + 1, proven=proven
                )
                last.update((file, index) for file in inner_runs)
        if operator in _PIPE_TOKENS and not proven and index + 1 < len(segments):
            into = _command_words(_without_redirections(segments[index + 1][0]))
            if into and Path(into[0]).name in _NESTED_SHELL_NAMES:
                last.update(((key(file), over_ssh), index) for file in command[1:])
    for index, copied, target in reversed(copies):
        for remote in (False, True):
            run = last.get((key(target), remote), -1)
            for source in copied:
                file = (key(source), remote)
                if run > index and run > last.get(file, -1):
                    last[file] = run
    return last


def _later_run(
    files: list[tuple[str, bool]], index: int, runs: dict[tuple[str, bool], int]
) -> str | None:
    """Where a command after command *index* runs one of *files* (each with whether
    it was written over ssh), from *runs* (_last_runs_of_files): "local" when a file
    written on this machine runs on it, "remote" when each such run, or the write
    before it, is over ssh, None when no command after *index* runs one."""
    where = {
        written_remote or run_remote
        for file, written_remote in files
        for run_remote in (False, True)
        if runs.get((Path(file).name, run_remote), -1) > index
    }
    if not where:
        return None
    return "remote" if False not in where else "local"


def _last_mentions_of_files(segments: list[tuple[list[str], str | None]]) -> dict[str, int]:
    """For each file name a command of *segments* names as one of its words, the last
    command that does: the source files an `interpreted` reading takes as run
    (`python3 x.py`, `pytest -q x.py`, `node x.mjs`)."""
    last: dict[str, int] = {}
    for index, (words, _) in enumerate(segments):
        last.update(
            (Path(word).name, index) for word in _command_words(_without_redirections(words))
        )
    return last


def _cut_segments(
    text: str, heredocs: list[_Heredoc] | None = None
) -> list[tuple[list[str], str | None]] | None:
    """The commands of *text* with its heredoc bodies cut (_cut_heredoc_bodies, with
    *heredocs* when given), past the reserved words that open each, with the operator
    that ends each; None where the cut text does not tokenize."""
    tokens = _command_tokens(_FD_NUMBER_RE.sub("", _cut_heredoc_bodies(text, heredocs)[0]))
    if tokens is None:
        return None
    return [
        (_without_reserved_prefix(words), operator)
        for words, operator in _segments_with_breaks(tokens)
    ]


# The programs that run text handed to them as shell commands: a heredoc body fed to them, or to a
# command whose output they read, and a quoted argument (`ssh host '...'`, `bash -c '...'`); and a
# heredoc marked in a word (a substitution: `"$(cat <<EOF)"`).
_BODY_SHELLS = _NESTED_SHELL_NAMES | {"ssh"}
_MARK_IN_WORD_RE = re.compile(rf"<<{_HEREDOC_MARK}(\d+)")


def _heredoc_runs(text: str) -> list[tuple[_Heredoc, bool, tuple[str, ...], bool]]:
    """(heredoc, whether a shell reads its body, the files its command writes, whether a later
    command runs one of them) for each heredoc of *text* (_heredocs), counting only the runs proven
    to happen, where _data_heredocs keeps every body that may run. A shell reads a body when a word
    of the command it feeds, or of a command that command pipes its output into, names a shell or
    ssh (`bash <<EOF`, `docker exec -i c bash <<EOF`, `cat <<EOF | tee x | bash`; for a heredoc in a
    substitution, of the command holding it: `bash -c "$(cat <<EOF ...)"`). The files are
    _written_files of the command it feeds, and the later runs _last_runs_of_files's proven ones
    (`bash -o pipefail x.sh` runs x.sh; `bash -n x.sh`, `test -f test`, `sh < x.sh` and `crontab
    x.sh` run none), with the script of a later python or node command (_last_interpreter_scripts):
    a run on this machine of the path word written (_path_word: `> r.sh`, then `./r.sh`), and not of
    a namesake (`> /tmp/a/r.sh`, then `bash /tmp/b/r.sh`). A text that does not tokenize runs no
    body: zsh, which runs the Bash tool's commands (`zsh -c`, then `eval`), parses the whole text
    first and runs none of it on a syntax error. bash would run the lines before the error."""
    return list(_cached_heredoc_runs(text))


@functools.lru_cache(maxsize=256)
def _cached_heredoc_runs(text: str) -> tuple[tuple[_Heredoc, bool, tuple[str, ...], bool], ...]:
    heredocs = _heredocs(text)
    if not heredocs:
        return ()
    marked = [
        dataclasses.replace(heredoc, tag=f"{_HEREDOC_MARK}{idx}")
        for idx, heredoc in enumerate(heredocs)
    ]
    segments = _cut_segments(text, marked)
    if segments is None:
        return tuple((heredoc, False, (), False) for heredoc in heredocs)
    # for each heredoc, the command it feeds, and whether it stands in a word of it
    feeds: dict[int, tuple[int, bool]] = {}
    for index, (words, _) in enumerate(segments):
        for pos, word in enumerate(words):
            number = word[len(_HEREDOC_MARK) :]
            if (
                pos
                and words[pos - 1] == "<<"
                and word.startswith(_HEREDOC_MARK)
                and number.isdigit()
            ):
                feeds[int(number)] = (index, False)
            else:
                feeds.update(
                    (int(found), (index, True)) for found in _MARK_IN_WORD_RE.findall(word)
                )
    # for each command, whether it or a command it pipes its output into, on to the
    # end of the pipeline, names a shell or ssh
    shell_reads = [False] * len(segments)
    for index in reversed(range(len(segments))):
        words, operator = segments[index]
        shell_reads[index] = any(
            Path(word).name in _BODY_SHELLS for word in _without_redirections(words)
        ) or (operator in _PIPE_TOKENS and index + 1 < len(segments) and shell_reads[index + 1])
    runs_file: dict[tuple[str, bool], int] | None = None
    scripts: dict[str, int] = {}
    # each command's files and their later runs, once: the heredocs opened on one
    # line feed it in turn
    written: dict[tuple[int, bool], tuple[tuple[str, ...], bool]] = {}
    uses: list[tuple[_Heredoc, bool, tuple[str, ...], bool]] = []
    for idx, heredoc in enumerate(heredocs):
        if idx not in feeds:
            uses.append((heredoc, False, (), False))
            continue
        index, in_word = feeds[idx]
        if (index, in_word) not in written:
            words = segments[index][0]
            command = _command_words(_without_redirections(words))
            files = () if in_word or not command else tuple(_written_files(words, command))
            later = False
            if files:
                if runs_file is None:
                    runs_file = _last_runs_of_files(segments, proven=True)
                    scripts = _last_interpreter_scripts(segments)
                later = any(
                    max(
                        runs_file.get((_path_word(file), False), -1),
                        scripts.get(_path_word(file), -1),
                    )
                    > index
                    for file in files
                )
            written[index, in_word] = (files, later)
        uses.append((heredoc, shell_reads[index], *written[index, in_word]))
    return tuple(uses)


def _last_interpreter_scripts(segments: list[tuple[list[str], str | None]]) -> dict[str, int]:
    """For each file a python or node command of *segments* runs as its script, by its path word
    (_path_word: `python3 -u ./x.py`, `node x.mjs`; not `python3 -m pytest x.py` or `python3 -c`),
    the last command that does. _last_runs_of_files leaves a python or node script out, so
    _heredoc_runs and _run_file_paths add these."""
    last: dict[str, int] = {}
    for index, (words, _) in enumerate(segments):
        command = _command_words(_without_redirections(words))
        if not command or not _INTERPRETER_RE.fullmatch(Path(command[0]).name):
            continue
        for word in command[1:]:
            if not _STDIN_SCRIPT_OPTION_RE.fullmatch(word):
                if not word.startswith("-"):
                    last[_path_word(word)] = index
                break
    return last


def _run_file_paths(text: str) -> set[str]:
    """The path word (_path_word) of each file a command of *text* runs now on this
    machine (_last_runs_of_files, proven; not over ssh), or runs as a python or node
    script (_last_interpreter_scripts), its heredoc bodies cut; none where *text*
    does not tokenize."""
    segments = _cut_segments(text)
    if segments is None:
        return set()
    runs = _last_runs_of_files(segments, proven=True)
    return {file for file, remote in runs if not remote} | set(_last_interpreter_scripts(segments))


def strip_data_heredoc_bodies(text: str, *, interpreted: bool = False) -> str:
    """*text* with each data heredoc body cut (_data_heredocs) and every other body,
    which may run, left in place: the text a guard that matches the command line
    itself reads. *interpreted* reads a body python or node runs as code
    (_data_heredocs). A text with no `<<` opens no heredoc and comes back as it is."""
    if "<<" not in text:
        return text
    return _cut_heredoc_bodies(text, _data_heredocs(text, interpreted=interpreted))[0]
