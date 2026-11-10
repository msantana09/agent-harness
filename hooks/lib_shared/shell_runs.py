"""
The commands a Bash command line would run.

Reads a command line the way the shell reads it: its simple commands (_read_runs, _any_run), the
shells nested in it (`bash -c`, `eval`, the command an ssh destination runs: _nested_payloads), its
text with quoted data masked (_mask_quoted_text), with heredoc bodies cut (_strip_heredoc_bodies)
and with commit and gh message text blanked (strip_commit_message), and its line continuations
joined (_join_line_continuations).
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import itertools
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shell_comments import shell_comment_spans, shell_continuations, shell_quotes
from shell_heredoc_data import (
    _COMPOUND_CLOSERS,
    _COMPOUND_OPENERS,
    _HEREDOC_MARK,
    _MARK_IN_WORD_RE,
    _TEXT_PRINTERS,
    _commit_file_source,
    _cut_heredoc_bodies,
    _data_heredocs,
    _enclosures,
    _expands_body,
    _files_written,
    _Heredoc,
    _heredocs,
    _last_runs_of_files,
    _later_run,
    _reads_text,
    _without_reserved_prefix,
)
from shell_tokens import (
    _ANSI_C_SIMPLE_ESCAPES,
    _COMMAND_WRAPPERS,
    _FD_NUMBER_RE,
    _MAX_STDIN_LIST_DEPTH,
    _NESTED_SHELL_NAMES,
    _PIPE_TOKENS,
    _SEGMENT_BREAK_TOKENS,
    _STDIN_FREE_COMMANDS,
    ENV_ASSIGNMENT_RE,
    _command_tokens,
    _command_words,
    _decode_ansi_c,
    _decode_ansi_c_quotes,
    _env_split_string,
    _escaped_char,
    _gh_path,
    _git_globals,
    _linear_split,
    _redirection,
    _segments_with_breaks,
    _shell_c_payload_indexes,
    _shell_option_readings,
    _shell_tokens,
    _ssh_remote_words,
    _strip_shell_comments,
    _without_redirections,
    _wrapper_option_takes_next_word,
)

# A python interpreter's file name: versioned (`python3.14`) or free-threaded
# (`python3.14t`, the build uv and python.org install beside the default one).
PYTHON_NAME = r"python[\d.]*t?"


def _xargs_replace(wrappers: list[str]) -> str | None:
    """For the wrapper words *wrappers* ahead of a command: None without xargs;
    else the string xargs replaces with each input line (-I, -J, -i,
    --replace), or "" when it appends the lines as operands."""
    idx = next((i for i, word in enumerate(wrappers) if Path(word).name == "xargs"), None)
    if idx is None:
        return None
    replace = ""
    rest = wrappers[idx + 1 :]
    for pos, word in enumerate(rest):
        if word in ("-I", "-J") and pos + 1 < len(rest):
            replace = rest[pos + 1]
        elif word[:2] in ("-I", "-J") and len(word) > 2:
            replace = word[2:]
        elif word == "-i" or word == "--replace":
            replace = "{}"
        elif word.startswith(("-i", "--replace=")):
            replace = word.partition("=")[2] if word.startswith("--") else word[2:]
    return replace


# Text with no shell name followed by an option runs no nested shell. It is
# matched with quotes and backslashes removed, since the shell joins
# `"bash"`, `b\ash` and `ba''sh` into `bash`.
_NESTED_SHELL_MARKER_RE = re.compile(r"\b(?:ba|z|da|k)?sh\s+[-+]")
_SHELL_QUOTING_CHARS = str.maketrans("", "", "'\"\\")


def _quote_removed(text: str) -> str:
    """*text* with its ANSI-C quotes decoded, then every quote and backslash
    removed: the word the shell joins from `"bash"`, `b\\ash`, `ap''ply` or
    `$'apply'`. For text matched as a whole, not tokenized."""
    return (_decode_ansi_c_quotes(text) or text).translate(_SHELL_QUOTING_CHARS)


# A nested shell anywhere in the text, inside another quoted word too
# (`"$(bash -c '...')"`, an ssh command string):
#   bash -c "..."   sh -c '...'   zsh -lc $'...'   bash -o 'pipefail' -c '...'
_NESTED_SHELL_NAME_RE = re.compile(r"\b(?:ba|z|da|k)?sh(?=\s)")
# One part of a shell word: a quoted string (ANSI-C, single, double; group 1,
# 2 or 3 holds its text) or one unquoted character (group 4) that does not end
# the word. `\"` and `\\` inside double quotes, and every backslash escape
# inside `$'...'`, are escapes, as in the shell.
_SHELL_WORD_PART_RE = re.compile(
    r"""\$'((?:[^'\\]|\\[\s\S])*)'|'([^']*)'|\$?"((?:[^"\\]|\\[\s\S])*)"|"""
    r"""([^\s'"`;&|()<>])"""
)
# One word after that shell name: its adjacent parts, which the shell joins
# into one word (`$'git push '"main"`). A bare variable (`$o`, `${o}`) is a
# word too, one an option can take as its value; `$(` reads as the word `$`,
# and the walk stops at the parenthesis. A word never fails once a part
# matched, so nothing backtracks and a long run of words is read in linear time.
_SHELL_ARG_WORD_RE = re.compile(rf"\s+(?:{_SHELL_WORD_PART_RE.pattern})+")
_DOUBLE_QUOTE_ESCAPE_RE = re.compile(r'\\([\\"])')
# What precedes a shell name that stands as a word of its own (a path's `/` too).
_SHELL_WORD_START = " \t\n;&|()/"


@functools.lru_cache(maxsize=256)
def _comment_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Each comment of *text* (shell_comment_spans, heredoc bodies read past), as (where its `#` is,
    where it ends). Read once per text, since several checks ask for the same command's quoted
    positions and commit end. A text with a `<<TAG` no line closes has its bodies cut (the bare
    marker _strip_heredoc_bodies leaves): none is read past, which would hide the comments after the
    first marker."""
    all_closed = all(heredoc.closed for heredoc in _heredocs(text))
    return tuple(shell_comment_spans(text, read_heredocs=all_closed))


def _quoted_positions(text: str) -> set[int]:
    """Positions in *text* inside a quoted word or a backtick substitution.

    Each quote runs to its next twin, and a backslash escapes the character
    after it everywhere but inside single quotes; an ANSI-C `$'...'` word
    honours backslash escapes too. A comment (_comment_spans) and a heredoc body
    (_heredocs) are read past: a quote or backtick in either (`# don't`) opens
    nothing. Only a body that a closing line ends is (_Heredoc.closed): the bare
    `<<TAG` that _strip_heredoc_bodies leaves where a body was holds none.
    """
    quoted: set[int] = set()
    skipped = dict(_comment_spans(text))
    for heredoc in _heredocs(text):
        if heredoc.closed:
            skipped[heredoc.body_start] = heredoc.end
    i, n = 0, len(text)
    while i < n:
        if i in skipped:
            i = skipped[i]
            continue
        char = text[i]
        if char == "\\":
            i += 2
            continue
        ansi_c = char == "$" and text[i + 1 : i + 2] == "'"
        if ansi_c:
            i += 1
            char = "'"
        elif char not in "'\"`":
            i += 1
            continue
        j = i + 1
        while j < n and text[j] != char:
            j += 2 if (char != "'" or ansi_c) and text[j] == "\\" else 1
        quoted.update(range(i + 1, j))
        i = j + 1
    return quoted


# A backslash escape inside a double-quoted word: the shell drops the backslash
# before `\`, `"`, `$` and a backtick, drops a backslash-newline whole, and keeps
# every other backslash (`"\x"` is `\x`).
_DOUBLE_QUOTED_WORD_ESCAPE_RE = re.compile(r"\\([\s\S])")


def _double_quoted_runs(
    text: str, unquoted: list[tuple[int, int]] | None = None
) -> dict[int, list[tuple[int, int]]]:
    """Each double-quoted word in *text*, by where its opening quote is: the
    runs of it that sit directly inside its quotes, as (start, end), in order.

    A `$(...)` or backtick body in the word holds quotes of its own, as do the
    words inside that body, so it ends one run and the next starts after it.
    Single quotes, comments (_comment_spans) and closed heredoc bodies outside
    double quotes are read past, as in _quoted_positions. A word no quote
    closes runs to the end of *text*. *unquoted* receives the (start, end) of
    the body of each closed `$(...)`, backtick and process (`<(...)`, `>(...)`)
    substitution outside double quotes, at any depth.
    """
    words: dict[int, list[tuple[int, int]]] = {}
    skipped = dict(_comment_spans(text))
    for heredoc in _heredocs(text):
        if heredoc.closed:
            skipped[heredoc.body_start] = heredoc.end
    # Each open frame: its kind (`"`, `$(`, `(` or a backtick), for a `"` where
    # its opening quote is and where its current run starts, and for a
    # substitution where its body starts and whether it is outside double quotes.
    stack: list[list[Any]] = []
    i, n = 0, len(text)
    while i < n:
        kind = stack[-1][0] if stack else ""
        if kind not in ('"', "`") and i in skipped:
            i = skipped[i]
            continue
        char = text[i]
        if char == "\\":
            i += 2
            continue
        body = "$(" if text.startswith("$(", i) else "`" if char == "`" and kind != "`" else ""
        if not body and kind not in ('"', "`") and text.startswith(("<(", ">("), i):
            body = "$("  # a process substitution, read as a `$(...)` is
        if kind == '"' and (body or char == '"'):
            words.setdefault(stack[-1][1], []).append((stack[-1][2], i))
        if kind == "`" or (kind == '"' and not body):
            if char == kind:
                frame = stack.pop()
                if kind == "`" and frame[2] and unquoted is not None:
                    unquoted.append((frame[1], i))
                if stack and stack[-1][0] == '"':
                    stack[-1][2] = i + 1
            i += 1
        elif body:
            stack.append([body, i + len(body), kind != '"'])
            i += len(body)
        elif char == '"':
            stack.append(['"', i, i + 1])
            i += 1
        elif char == "'" or text.startswith("$'", i):
            i = _quote_end(text, i) + 1
        elif char == "(" and kind in ("$(", "("):
            stack.append(["("])
            i += 1
        elif char == ")" and kind in ("$(", "("):
            frame = stack.pop()
            if kind == "$(" and frame[2] and unquoted is not None:
                unquoted.append((frame[1], i))
            if stack and stack[-1][0] == '"':
                stack[-1][2] = i + 1
            i += 1
        else:
            i += 1
    for frame in stack:
        if frame[0] == '"':
            words.setdefault(frame[1], []).append((frame[2], n))
    return words


def _decode_double_quoted_word(
    text: str, runs: list[tuple[int, int]]
) -> tuple[str, list[int], list[int]]:
    """The text of the double-quoted word whose runs (_double_quoted_runs) are
    *runs*, as the shell hands it on: the escapes of each run decoded
    (_DOUBLE_QUOTED_WORD_ESCAPE_RE), the substitution bodies between runs as
    written. Also where it maps onto *text*: each decoded position at which the
    offset to *text* changes, and the position in *text* it maps to."""
    out: list[str] = []
    decoded_at, text_at = [0], [runs[0][0]]
    length = 0
    for index, (start, end) in enumerate(runs):
        last = start
        for escape in _DOUBLE_QUOTED_WORD_ESCAPE_RE.finditer(text, start, end):
            char = escape.group(1)
            kept = char if char in '\\"$`' else "" if char == "\n" else escape.group()
            out += [text[last : escape.start()], kept]
            length += escape.start() - last + len(kept)
            last = escape.end()
            decoded_at.append(length)
            text_at.append(last)
        following = runs[index + 1][0] if index + 1 < len(runs) else end
        out.append(text[last:following])
        length += following - last
    return "".join(out), decoded_at, text_at


def _shell_payloads_in(text: str, names: set[str] = _NESTED_SHELL_NAMES) -> list[str]:
    """The command lines that nested shells in *text* run, one wrapper deep:
    the shells of *names*, all of them by default.

    Shell tokens read a payload whole, the way the shell does: an escaped
    quote (`bash -c "git push \\"x\\""`) or a `'\\''` splice stays inside it,
    and the shell may be path-qualified or sit behind `env`, `sudo` or
    `docker exec`. The regex walk (_regex_shell_payloads) adds the invocations
    that tokens cannot see, those inside another quoted word (read as that
    word's escapes decode them) or a backtick substitution, and is all there
    is for text shlex cannot tokenize. A walk
    from a shell word that stands unquoted on its own is one the tokens
    already read, perhaps cut short at a quote, and is dropped.
    """
    if not _NESTED_SHELL_MARKER_RE.search(_quote_removed(text)):
        return []
    tokens = _shell_tokens(text)
    walked = [
        (start, payload)
        for start, payload in _regex_shell_payloads(text)
        if _NESTED_SHELL_NAME_RE.match(text, start).group() in names
    ]
    if tokens is None:
        return [payload for _, payload in walked]
    quoted = _quoted_positions(text)
    payloads: list[str] = []
    for idx, token in enumerate(tokens):
        if Path(token).name in names:
            payloads.extend(tokens[i] for i in _shell_c_payload_indexes(tokens, idx))
    return payloads + [
        payload
        for start, payload in walked
        if start in quoted or (start and text[start - 1] not in _SHELL_WORD_START)
    ]


def _regex_shell_payloads(text: str) -> list[tuple[int, str]]:
    """Each quoted word in *text* that a shell named before it runs with
    `-c`, as (where the shell name starts, the word's text).

    From each shell name the words after it are read one at a time, an
    option's value (quoted, or a bare variable) as a word of its own, until no
    option can still take the next word as its value;
    _shell_c_payload_indexes then reads them as it reads tokens. Each word is
    tested as an option with its quotes removed and its ANSI-C escapes decoded,
    as the shell reads `'-c'`. Only a quoted payload is taken. A shell name
    that sits directly inside a double-quoted word has its words read from that
    word's text as the shell hands it on (_decode_double_quoted_word: `\\$(`
    and `\\"` decoded), up to its closing quote; one inside a `$(...)` or
    backtick body in that word reads its quotes afresh. The search for
    the next shell name resumes after the last word read, so each word is read
    once, each double-quoted word is decoded once, and a long run of words is
    read in linear time.
    """
    found: list[tuple[int, str]] = []
    words = _double_quoted_runs(text)
    runs = sorted((start, end, quote) for quote, spans in words.items() for start, end in spans)
    decoded: dict[int, tuple[str, list[int], list[int]]] = {}
    pos = 0
    while name := _NESTED_SHELL_NAME_RE.search(text, pos):
        pos = name.end()
        source = text
        run = runs[bisect.bisect_right(runs, (name.start() + 1,)) - 1] if runs else None
        if run and run[0] <= name.start() < run[1]:
            if run[2] not in decoded:
                decoded[run[2]] = _decode_double_quoted_word(text, words[run[2]])
            source, decoded_at, text_at = decoded[run[2]]
            at = bisect.bisect_right(text_at, pos) - 1
            pos = decoded_at[at] + pos - text_at[at]
        args: list[str] = []
        texts: list[tuple[str, bool]] = []
        values_left = 0
        while word := _SHELL_ARG_WORD_RE.match(source, pos):
            pos = word.end()
            value, quoted = _shell_word_text(word.group().lstrip())
            texts.append((value, quoted))
            args.append(value)
            if value.startswith(("-", "+")):
                values_left += max(consumed for consumed, _ in _shell_option_readings(value))
            elif values_left:
                values_left -= 1
            else:
                break
        if source is not text:
            at = bisect.bisect_right(decoded_at, pos) - 1
            pos = text_at[at] + pos - decoded_at[at]
        indexes = _shell_c_payload_indexes(["sh", *args], 0)
        found.extend((name.start(), texts[i - 1][0]) for i in indexes if texts[i - 1][1])
    return found


def _shell_word_text(word: str) -> tuple[str, bool]:
    """The text of the _SHELL_ARG_WORD_RE word *word*, its parts unquoted and
    joined, and whether any part is quoted."""
    out: list[str] = []
    quoted = escaped = False
    for part in _SHELL_WORD_PART_RE.finditer(word):
        ansi_c, single, double, char = part.groups()
        if char is not None:
            if char == "\\" and not escaped:
                escaped = True
                continue
            out.append(char)
        elif ansi_c is not None:
            out.append(_decode_ansi_c(ansi_c))
        elif single is not None:
            out.append(single)
        else:
            out.append(_DOUBLE_QUOTE_ESCAPE_RE.sub(r"\1", double))
        quoted = quoted or char is None
        escaped = False
    return "".join(out), quoted


def _shell_args_run_next_word(args: str) -> bool:
    """True when a shell given the words in *args* runs the word after them
    with `-c`, rather than taking it as an option's value or an argument."""
    words = args.split()
    return len(words) + 1 in _shell_c_payload_indexes(["sh", *words, "''"], 0)


# Script operands that name a shell's own stdin.
_STDIN_PATHS = {"/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
# A printf conversion (`%s`, `%-5d`, `%.4s`; group 1 the precision, group 2
# the conversion letter) or a literal `%%`.
_PRINTF_DIRECTIVE_RE = re.compile(r"%(?:%|[-+ #0-9]*(?:\.([0-9]*))?([a-zA-Z]))")
# The backslash escapes that `echo -e`, zsh's plain `echo` and printf `%b`
# decode: `\0nnn` octal, `\nnn` octal (printf `%b` in bash only), hex,
# Unicode, `\c` (no further output) and single letters.
_ECHO_ESCAPE_RE = re.compile(
    r"\\(?:0([0-7]{0,3})|([1-7][0-7]{0,2})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})"
    r"|U([0-9A-Fa-f]{1,8})|(c)|([abeEfnrtv\\]))"
)


def _shell_reads_stdin(words: list[str]) -> bool:
    """True when the command *words* is a shell that reads its script from
    stdin: no option sets `-c`, and it has no script operand, its script
    operand names stdin (`/dev/stdin`), or `-s` makes its operands positional
    parameters. Every reading of an ambiguous option cluster
    (_shell_option_readings) counts."""
    if not words or Path(words[0]).name not in _NESTED_SHELL_NAMES:
        return False
    pending = [(1, False)]
    seen: set[tuple[int, bool]] = set()
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        i, stdin_flag = state
        if i >= len(words):
            return True
        word = words[i]
        if word in ("-", "--"):
            if stdin_flag or i + 1 == len(words) or words[i + 1] in _STDIN_PATHS:
                return True
            continue
        if not word.startswith(("-", "+")):
            if stdin_flag or word in _STDIN_PATHS:
                return True
            continue
        sets_s = word.startswith("-") and not word.startswith("--") and "s" in word
        for consumed, sets_c in _shell_option_readings(word):
            if not sets_c:
                pending.append((i + 1 + consumed, stdin_flag or sets_s))
    return False


def _stdin_runner(words: list[str], depth: int = 0, closed: bool = False) -> str | None:
    """Where the command *words* runs the script it reads on stdin (the first
    of _stdin_state)."""
    return _stdin_state(words, depth, closed)[0]


def _stdin_state(words: list[str], depth: int, closed: bool) -> tuple[str | None, bool]:
    """(where the command *words* runs the script it reads on stdin, whether
    it takes all the text fed to it). The runner is "local" for a shell that
    reads one (_shell_reads_stdin) or whose `-c` command list holds one that
    does (_list_stdin_state), "remote" for ssh with no remote command or with
    a list as its remote command that runs it (read *closed*), else None.
    *depth* counts the command lists this one sits in; a list past
    _MAX_STDIN_LIST_DEPTH is not read and is taken to run its stdin.

    In a *closed* list a command runs its stdin unless it takes it as text or
    reads none (_reads_text, _STDIN_FREE_COMMANDS): a program the reader does
    not list may be a shell (`sudo -i`, `su -`, `$SHELL`, `docker exec -i c
    sh`), and so may a wrapper left with no command, a shell given a script file
    or a `-c` list that does not tokenize. The runner of such a command is
    "remote". The text is taken by a reader of it to the end (_takes_all_stdin)
    and by a `-c` list that holds one (_list_stdin_state); only a closed list
    reads that."""
    stripped = _without_redirections(words)
    command = _command_words(stripped)
    if not command:
        # a wrapper with no command after it (`sudo -i`) runs a shell of its own
        has_command = not all(ENV_ASSIGNMENT_RE.match(word) for word in stripped)
        return ("remote" if closed and has_command else None), False
    name = Path(command[0]).name
    if name in _NESTED_SHELL_NAMES:
        if _shell_reads_stdin(command):
            return "local", False
        indexes = _shell_c_payload_indexes(command, 0)
        takes = closed and bool(indexes)
        for idx in indexes:
            if depth >= _MAX_STDIN_LIST_DEPTH:
                return "local", False
            payload_tokens = _shell_tokens(command[idx])
            if payload_tokens is None:
                runner, inner = ("remote" if closed else None), False
            else:
                runner, inner = _list_stdin_state(payload_tokens, depth + 1, closed)
            if runner:
                return runner, False
            takes = takes and inner
        return ("remote" if closed and not indexes else None), takes
    if name == "ssh":
        remote = _ssh_remote_words(command)
        if remote == [] or (remote and depth >= _MAX_STDIN_LIST_DEPTH):
            return "remote", False
        remote_tokens = _shell_tokens(" ".join(remote)) if remote else None
        if remote is not None and (
            remote_tokens is None or _list_stdin_state(remote_tokens, depth + 1, closed=True)[0]
        ):
            return "remote", False
        return None, False
    if not closed:
        return None, False
    if name in _STDIN_FREE_COMMANDS:
        return None, False
    if not _reads_text(command):
        return "remote", False
    return None, _takes_all_stdin(words, command)


def _takes_all_stdin(words: list[str], command: list[str]) -> bool:
    """Whether the text reader *command* (_reads_text; *words* is the same
    command with its redirections) reads the text fed to it to the end: its
    input is stdin, not an `<` redirection or a file operand (`cat < f`, `cat
    /dev/null`, `dd if=f`; `cat - f` reads both and is left out), and it reads
    stdin to the end (`dd count=1` reads one block). A quoted `'<'` is a file
    name, not a redirection (_redirection)."""
    if any(_redirection(word) and "<" in word for word in words):
        return False
    program = Path(command[0]).name
    if program == "cat":
        return all(word == "-" for word in command[1:] if word == "-" or not word.startswith("-"))
    if program == "dd":
        return not any(word.startswith(("if=", "count=")) for word in command[1:])
    return True


def _list_stdin_runner(tokens: list[str], depth: int = 0, closed: bool = False) -> str | None:
    """Where the command list *tokens* runs the script it reads on stdin (the
    first of _list_stdin_state)."""
    return _list_stdin_state(tokens, depth, closed)[0]


def _list_stdin_state(tokens: list[str], depth: int, closed: bool) -> tuple[str | None, bool]:
    """(where the command list *tokens* runs the script it reads on stdin,
    whether it takes all of the text: _stdin_state of each command in it,
    *depth* lists deep, *closed* or not). Every command reads the list's
    stdin, a command a pipe feeds too, as each stage before it passes the
    text on (`cat | bash`, `cat | sudo -i`, as _pipe_runners reads a
    pipeline), except past an `echo` or `printf`, which writes text of its
    own, and in a closed list past a command that reads no stdin (`cd /srv |
    bash`): the stages after either get its output (`echo x | tee y | bash`).
    The reserved words that open a command (`{`, `if`, `then`, `do`:
    _without_reserved_prefix) are skipped; a closing `fi`, `done` or `esac`
    and a `for` header read no stdin, unless a header word holds a backtick or
    `$(` substitution, which tokens do not split out. "local" wins when the
    commands disagree.

    A command that copies the text it reads to a file that a later command
    of the list runs (_run_writers: `cat > x.sh && bash x.sh`, `tee x.sh; .
    ./x.sh`, `cat > f; cat f | sh`) runs it: "remote" in a closed list, else
    where that command runs it.

    In a closed list a reader that takes all the text (_takes_all_stdin) and
    ends with `&&`, `;` or a newline leaves none to the commands after it, so
    they are read open: only a shell that reads stdin runs the text (`cat > f
    && sudo -i` passes, `cat > f && bash -s` does not), and a stage a pipe
    feeds reads only the pipe (`cat > f; ls | bash -s` passes). Two readers leave the
    text where it was: one a `||` came before in its line, which may be skipped
    (`test -f f || cat > f; sudo -i`), and one a pipe feeds, which reads the
    pipe. A list with a `&` takes none: a background job gets /dev/null, and
    so does the list it ends. A reader that `||` ends has run when its line
    goes on past a `;` (`cat > f || exit 1; ls`), but the command after the
    `||` is read closed (`cat > f || sudo -i`: a failed redirection reads
    none). A failure before the reader (`cd /srv && cat > f; sudo -i`) or in
    its redirection is not read, and neither is a reader inside a compound
    command (_enclosures), which may not run (`if [ -d d ]; then cat > f; fi;
    sudo -i`)."""
    runners: set[str] = set()
    piped = False  # this stage's stdin is a pipe from a command that writes its own text
    raw_segments = _segments_with_breaks(tokens)
    segments = [
        (_without_command_header(_without_reserved_prefix(words)), operator)
        for words, operator in raw_segments
    ]
    writers = _run_writers(segments)
    # a background job gets /dev/null, and so does the list it ends: a list with a `&` takes none
    may_take = closed and all(operator != "&" for _, operator in segments)
    # for each token position, whether a compound command around it may not run its commands
    conditional: list[bool] = []
    if may_take:
        enclosures = _enclosures(tokens)
        compound: list[bool] = []
        for kind, parent in zip(enclosures.kinds, enclosures.parents, strict=True):
            compound.append(kind in _COMPOUND_OPENERS or (parent is not None and compound[parent]))
        conditional = [group is not None and compound[group] for group in enclosures.innermost]
    taken = False  # an earlier command took all the text
    or_ended = False  # a reader that `||` ends: it took the text if its line goes on to a `;`
    after_or = False  # a `||` came since the last `;`
    start = 0  # the token position of this segment
    for index, (words, operator) in enumerate(segments):
        read_closed = closed and not taken
        runner, takes = (None, False) if piped else _stdin_state(words, depth, read_closed)
        if index in writers and not (piped or taken or _echoed_texts(words)):
            command = _command_words(_without_redirections(words))
            if _takes_all_stdin(words, command):
                runner = runner or ("remote" if closed else writers[index])
        if runner:
            runners.add(runner)
        if conditional and start < len(conditional) and conditional[start]:
            takes = False
        start += len(raw_segments[index][0]) + 1
        if may_take and takes and not after_or:
            if operator in ("&&", ";", ")", None):
                taken = True
            elif operator == "||":
                or_ended = True
        if operator == "||":
            after_or = True
        elif operator == ";":
            taken = taken or or_ended
            after_or = or_ended = False
        # a `(` group keeps the stdin of the command it stands for
        if operator not in ("(", ")"):
            # once a reader took all the text, a pipe carries only what its writer prints
            piped = operator in _PIPE_TOKENS and (
                piped
                or taken
                or bool(_echoed_texts(words))
                or (read_closed and _reads_no_stdin(words))
            )
    return ("local" if "local" in runners else next(iter(runners), None)), taken


def _reads_no_stdin(words: list[str]) -> bool:
    """True when the command *words* reads no stdin (_STDIN_FREE_COMMANDS), so
    a pipe from it feeds its command only its output."""
    command = _command_words(_without_redirections(words))
    return bool(command) and Path(command[0]).name in _STDIN_FREE_COMMANDS


def _without_command_header(words: list[str]) -> list[str]:
    """*words* past the `fi`, `done` or `esac` that close a compound command, and
    none for a `for` header (`for f in a b`): neither reads stdin. A header word
    that holds a backtick or a `$(` substitution (`for x in "$(sudo -i)"`), whose
    commands tokens do not split out, is left in place."""
    while words and words[0] in _COMPOUND_CLOSERS:
        words = words[1:]
    if words[:1] == ["for"] and not any("`" in word or "$(" in word for word in words):
        return []
    return words


# The commands that copy the text they are given: a text printer reads it on stdin
# (_TEXT_PRINTERS), `echo` and `printf` take it as words, and a shell or ssh hands it
# to the command lists it runs (_files_written reads what those write).
_TEXT_COPIERS = _TEXT_PRINTERS | _NESTED_SHELL_NAMES | {"echo", "printf", "ssh"}


def _run_writers(segments: list[tuple[list[str], str | None]]) -> dict[int, str]:
    """For the index of each command of *segments* (past the reserved words that
    open each) that copies its text (_TEXT_COPIERS) to a file a later command
    runs, where that command runs it (_later_run): `cat > x.sh <<EOF && bash
    x.sh`, `sh -c 'cat > x.sh' <<EOF ... EOF; sudo bash x.sh`, `echo ls > x.sh;
    . ./x.sh`, "remote" for `cat > x.sh; ssh host bash x.sh`. The files are read
    by _files_written, and the runs by the one tracker the data heredoc reader
    also asks (_last_runs_of_files, _later_run)."""
    written: list[tuple[int, list[tuple[str, bool]]]] = []
    for index, (words, _) in enumerate(segments):
        command = _command_words(_without_redirections(words))
        if command and Path(command[0]).name in _TEXT_COPIERS:
            files = _files_written(words, command)
            if files:
                written.append((index, files))
    if not written:
        return {}
    runs = _last_runs_of_files(segments)
    writers: dict[int, str] = {}
    for index, files in written:
        if runner := _later_run(files, index, runs):
            writers[index] = runner
    return writers


def _decode_echo_escapes(text: str, bare_octal: bool) -> tuple[str, bool]:
    """*text* with the escapes `echo -e` decodes (_ECHO_ESCAPE_RE), and whether
    a `\\c` ended the output there. *bare_octal* decodes `\\nnn` with no
    leading 0 too, as bash's printf `%b` does. A NUL is dropped and a code
    that is no character kept as written (_escaped_char)."""
    out: list[str] = []
    pos = 0
    for match in _ECHO_ESCAPE_RE.finditer(text):
        octal, bare, hex_byte, short_code, long_code, stop, simple = match.groups()
        if bare is not None and not bare_octal:
            continue
        out.append(text[pos : match.start()])
        pos = match.end()
        if stop:
            return "".join(out), True
        if simple is not None:
            out.append(_ANSI_C_SIMPLE_ESCAPES[simple])
            continue
        if octal is not None or bare is not None:
            code = int(octal or bare or "0", 8)
        else:
            code = int(hex_byte or short_code or long_code, 16)
        out.append(_escaped_char(code, match.group(0)) if code else "")
    out.append(text[pos:])
    return "".join(out), False


def _printf_output(args: list[str]) -> str | None:
    """What `printf` writes given *args*: the format with its escapes decoded
    and each conversion filled by the next argument, the format reused while
    arguments remain. `%b` decodes its argument's escapes (a `\\c` there ends
    all output), `%c` takes its first character and a precision (`%.4s`)
    cuts it short. None for `printf -v`, which writes nothing."""
    if args and args[0] == "--":
        args = args[1:]
    if not args or args[0] == "-v":
        return None
    fmt, rest = _decode_ansi_c(args[0]), args[1:]
    out: list[str] = []
    while True:
        used = pos = 0
        for match in _PRINTF_DIRECTIVE_RE.finditer(fmt):
            out.append(fmt[pos : match.start()])
            pos = match.end()
            if match.group(0) == "%%":
                out.append("%")
                continue
            value = rest[used] if used < len(rest) else ""
            used += 1
            precision, conversion = match.group(1, 2)
            stopped = False
            if conversion == "b":
                value, stopped = _decode_echo_escapes(value, bare_octal=True)
            if conversion == "c":
                value = value[:1]
            elif precision is not None and conversion in "sb":
                value = value[: int(precision or 0)]
            out.append(value)
            if stopped:
                return "".join(out)
        out.append(fmt[pos:])
        rest = rest[used:]
        if not rest or not used:
            return "".join(out)


def _echoed_texts(words: list[str]) -> list[str]:
    """Each reading of what the `echo` or `printf` command *words* writes:
    for echo its arguments as written (bash) and with their escapes decoded
    (zsh, which runs Claude Code's commands, and `echo -e`); for printf its
    output. Empty for any other command. The reserved words that open a
    command (`then echo ...`: _without_reserved_prefix) are skipped."""
    words = _command_words(_without_redirections(_without_reserved_prefix(words)))
    if not words:
        return []
    name, args = Path(words[0]).name, words[1:]
    if name == "echo":
        while args and re.fullmatch(r"-[neE]+", args[0]):
            args = args[1:]
        written = " ".join(args)
        decoded, _ = _decode_echo_escapes(written, bare_octal=False)
        return [written] if decoded == written else [written, decoded]
    if name == "printf":
        output = _printf_output(args)
        return [] if output is None else [output]
    return []


# What decides whether a backtick opens or closes a substitution: an escaped character, an
# ANSI-C or single-quoted string, a double quote, a backtick; in double quotes only an
# escaped character, the closing quote and a backtick
_BACKTICK_CONTEXT_RE = re.compile(r"\\.|\$'(?:\\.|[^'\\])*'?|'[^']*'?|[\"`]", re.DOTALL)
_DOUBLE_QUOTED_BACKTICK_CONTEXT_RE = re.compile(r"\\.|[\"`]", re.DOTALL)
# A backtick substitution's body to the backtick that closes it, the first one not
# escaped, quotes or not: the shell finds that end before it reads the body
_BACKTICK_BODY_RE = re.compile(r"(?:\\.|[^`\\])*`", re.DOTALL)


def _substitution_backticks(text: str) -> list[int]:
    """Where each backtick of *text*, read from outside every quote, opens or
    closes a command substitution: not one that is escaped or in a
    single-quoted or ANSI-C string (`'x`y'`, a quoted delimiter `<<'A`B'`).
    One left open runs to the end of *text*."""
    found: list[int] = []
    pos, quoted = 0, False
    while match := (_DOUBLE_QUOTED_BACKTICK_CONTEXT_RE if quoted else _BACKTICK_CONTEXT_RE).search(
        text, pos
    ):
        pos = match.end()
        if match.group() == '"':
            quoted = not quoted
        elif match.group() == "`":
            found.append(match.start())
            body = _BACKTICK_BODY_RE.match(text, pos)
            if body is None:
                break
            found.append(body.end() - 1)
            pos = body.end()
    return found


def _opener_tokens(text: str, before: bool) -> list[str] | None:
    """The tokens around a `<<` heredoc operator, from *text*: the command
    text before the operator (*before*), or the line after a delimiter, the
    rest of its command and what follows it. A backtick of a substitution
    starts or ends a command (_substitution_backticks), so the text is cut at
    the last one before the operator, or the first one after it. Text that
    still cannot be tokenized (the heredoc opens inside a quoted `bash -c
    '...'` or `"$(...)"`) is cut at the last quote or parenthesis before the
    operator, or the first one after it."""
    text = _FD_NUMBER_RE.sub("", text)
    if ticks := _substitution_backticks(text):
        text = text[ticks[-1] + 1 :] if before else text[: ticks[0]]
    tokens = _shell_tokens(text)
    if tokens is None:
        text = re.split(r"[\"'(]", text)[-1] if before else re.split(r"[\"')]", text)[0]
        tokens = _shell_tokens(text)
    return tokens


def _closed_group(tokens: list[str], end: int) -> list[str] | None:
    """The tokens inside the `(...)` or `{ ...; }` group that *tokens* up to
    *end* end with, or None when they end in no group or its opening word is
    not among them."""
    if not end or tokens[end - 1] not in (")", "}"):
        return None
    closer = tokens[end - 1]
    opener = "(" if closer == ")" else "{"
    depth = 0
    for idx in range(end - 1, -1, -1):
        depth += (tokens[idx] == closer) - (tokens[idx] == opener)
        if depth == 0:
            return tokens[idx + 1 : end - 1]
    return None


def _opens_above(text: str, start: int, end: int) -> bool | None:
    """Whether the command that ends at *end* in *text* began before *start*,
    a line start: the line above ends in a line continuation, or the text
    from *start* closes a `(...)` or `{ ...; }` group it does not open. None
    when that text cannot be tokenized."""
    above_end = max(start - 1, 0)
    line_above = text[text.rfind("\n", 0, above_end) + 1 : above_end]
    if (len(line_above) - len(line_above.rstrip("\\"))) % 2:
        return True
    tokens = _shell_tokens(text[start:end])
    if tokens is None:
        return None
    return tokens.count(")") > tokens.count("(") or tokens.count("}") > tokens.count("{")


def _command_start(text: str, end: int, quote_line: Callable[[int], int | None]) -> int:
    """Where the command that ends at *end* in *text* begins: the start of
    its line, or of a span twice as long each time the span opens above
    (_opens_above), so a command is read in time linear in its length. A span
    that cannot be tokenized from a line start inside a quoted word closes
    that word, and the command is read on from the start of the line that
    opens it (*quote_line*); from a line start outside every quote, the span
    opens a quote it does not close (the `"$(` of `git commit -m "$(cat
    <<'EOF'`), and no line above holds the start of its command."""
    start = text.rfind("\n", 0, end) + 1
    size = max(end - start, 1)
    while start:
        opens = _opens_above(text, start, end)
        if opens is None:
            opened = quote_line(start)
            if opened is None:
                break
            start, size = opened, max(end - opened, 1)
            continue
        if not opens:
            break
        size *= 2
        start = text.rfind("\n", 0, max(end - size, 0)) + 1
    return start


# The tabs that open each line of a `<<-` body, which the shell cuts before it reads the line.
_LEADING_TABS_RE = re.compile(r"^\t+", re.MULTILINE)


def _heredoc_scripts(text: str) -> list[tuple[_Heredoc, str, str]]:
    """(heredoc, body, runner) of each heredoc in *text* whose command, or a
    command in the group it feeds, reads it as a script, or whose command's
    output a pipeline runs (_output_runners: `cat <<EOF | bash`, `cat <<EOF
    | tee x | bash`, a pipe that goes on at the line after the body, `{ cat
    <<EOF; } | bash`, `for ...; do cat <<EOF; done | sh`) or a later command
    runs from the file it copies the body to (_run_writers: `cat > x.sh <<EOF
    && bash x.sh`, `sh -c 'cat > x.sh' <<EOF ... EOF; sudo bash x.sh`), runner
    being "local" or "remote" (_list_stdin_runner). The body is the one the shell
    reads: a `<<-` body without the tabs that open its lines. The command it feeds is
    read as _heredoc_feeds reads it. Where the output goes is read from the
    whole text, every body cut (_heredoc_output_runners)."""
    return list(_cached_heredoc_scripts(text))


@functools.lru_cache(maxsize=64)
def _cached_heredoc_scripts(text: str) -> tuple[tuple[_Heredoc, str, str], ...]:
    scripts: list[tuple[_Heredoc, str, str]] = []
    heredocs = _heredocs(text)
    output_runners: dict[int, str] | None = None  # read once, for the first heredoc that needs it
    fed_runners: dict[int, str | None] = {}  # by feed, once: heredocs on one command share theirs
    for idx, (heredoc, feed) in enumerate(zip(heredocs, _heredoc_feeds(text), strict=True)):
        line_end = text.find("\n", heredoc.word_end, heredoc.end)
        if line_end == -1:
            continue
        runner = None
        if feed is not None:
            if id(feed) not in fed_runners:
                command, _, piped = feed
                fed_runners[id(feed)] = _list_stdin_runner(command) or piped
            runner = fed_runners[id(feed)]
        if not runner:
            if output_runners is None:
                output_runners = _heredoc_output_runners(text, heredocs)
            runner = output_runners.get(idx)
        if runner:
            body = text[heredoc.body_start : heredoc.body_end]
            if text.startswith("<<-", heredoc.start):
                body = _LEADING_TABS_RE.sub("", body)
            scripts.append((heredoc, body, runner))
    return tuple(scripts)


# The command a heredoc feeds (_heredoc_feeds): its words, the operator that ends
# it, and where the pipeline that operator opens runs the command's output.
_HeredocFeed = tuple[list[str], str | None, str | None]
_NEWLINE_RE = re.compile("\n")


@functools.lru_cache(maxsize=64)
def _heredoc_feeds(text: str) -> tuple[_HeredocFeed | None, ...]:
    """For each heredoc of *text* (_heredocs), the command it feeds: the words
    of the command whose redirections it is among, the operator after them
    and, when that is a pipe, where its pipeline runs the command's output
    (_pipe_runners); None when the words cannot be read or the body opens no
    line. The heredocs a line opens are read together, once
    (_opener_line_feeds), from where the command of the last of them starts
    (_command_start), which can be on a line above: that text is read with
    earlier heredoc bodies cut and continued lines joined, or from the start
    of the opener line when it cannot be tokenized. Whether a line start is
    inside a quoted word is read from *text* once (shell_quotes), when a line
    first cannot be tokenized. The text goes on after the last body of the
    line (`cat <<A <<B` then the line after B)."""
    feeds: list[_HeredocFeed | None] = []
    above = ""  # *text* up to the operator, earlier bodies cut, fd numbers dropped
    # the span of each piece of *above* and that of *text* it was cut from
    pieces: list[tuple[int, int, int, int]] = []
    # each newline of *above*, and where it is in *text*: the line after it is the
    # line after that newline of *text*; listed from *pieces* when a line first
    # cannot be tokenized (quote_line), up to the piece *indexed*
    above_newlines: list[int] = []
    text_newlines: list[int] = []
    indexed = 0
    quotes: tuple[list[int], list[int]] | None = None  # the outermost quoted words of *text*

    def extend(begin: int, stop: int) -> None:
        nonlocal above
        piece = _FD_NUMBER_RE.sub("", text[begin:stop])
        pieces.append((len(above), len(above) + len(piece), begin, stop))
        above += piece

    def quote_line(start: int) -> int | None:
        """The start of the line of *above* that opens the quoted word its line
        start *start* is inside, or None when it is inside none."""
        nonlocal quotes, indexed
        if quotes is None:
            quotes = ([], [])
            for quote_start, quote_end, _ in shell_quotes(text, read_heredocs=True):
                if not quotes[1] or quote_start >= quotes[1][-1]:
                    quotes[0].append(quote_start)
                    quotes[1].append(quote_end)
        for first, last, begin, stop in pieces[indexed:]:
            above_newlines.extend(
                newline.start() for newline in _NEWLINE_RE.finditer(above, first, last)
            )
            text_newlines.extend(
                newline.start() for newline in _NEWLINE_RE.finditer(text, begin, stop)
            )
        indexed = len(pieces)
        at = text_newlines[bisect.bisect_left(above_newlines, start - 1)] + 1
        idx = bisect.bisect_left(quotes[0], at) - 1
        if idx < 0 or at >= quotes[1][idx]:
            return None
        # a quote is never in a cut body, so the newline before it is one of *above*
        newline = bisect.bisect_left(text_newlines, quotes[0][idx]) - 1
        return above_newlines[newline] + 1 if newline >= 0 else 0

    last = 0
    heredocs = _heredocs(text)
    idx = 0
    while idx < len(heredocs):
        heredoc = heredocs[idx]
        line_end = text.find("\n", heredoc.word_end, heredoc.end)
        # cut with the operator still there, so an fd number before it (`0<<`) goes
        extend(last, heredoc.start + 2)
        last = heredoc.start + 2
        if line_end == -1:
            feeds.append(None)
            idx += 1
            continue
        first, end = idx, len(above) - 2
        idx += 1
        while idx < len(heredocs) and heredocs[idx].start < line_end:  # the others the line opens
            extend(last, heredocs[idx].start + 2)
            last = heredocs[idx].start + 2
            idx += 1
        # the command starts where that of the last heredoc on the line starts: the text
        # up to it holds every bracket the line closes
        start = _command_start(above, len(above) - 2, quote_line)
        opened = heredocs[first:idx]
        line_feeds = _opener_line_feeds(
            _join_line_continuations(above[start:end]),
            above[above.rfind("\n", 0, end) + 1 : end],
            text[heredoc.start : line_end],
            [(other.start - heredoc.start, other.word_end - heredoc.start) for other in opened],
        )
        feeds += [
            feed if line_end < other.end else None
            for other, feed in zip(opened, line_feeds, strict=True)
        ]
        # the text goes on past the bodies of the line, after the last of them
        extend(last, line_end)
        last = max(other.end for other in opened)
    return tuple(feeds)


def _opener_line_feeds(
    before: str, line_before: str, rest: str, spans: list[tuple[int, int]]
) -> list[_HeredocFeed | None]:
    """The feed (_heredoc_feeds) of each heredoc opened on one line, read from
    the command text *before* the first `<<` (from where its command starts,
    or from the start of its line, *line_before*, when that text cannot be
    tokenized) and the line from that `<<` on (*rest*), where each heredoc's
    operator starts and its delimiter ends (*spans*). A heredoc feeds the
    command whose redirections it is among (_fed_commands): its words, without
    the operators and delimiter words of the line's heredocs (redirections,
    which change no reader of stdin). A backtick of a substitution starts or
    ends a command (_substitution_backticks: not one in single quotes or
    escaped), so the line is read in the stretches between those backticks,
    each once (_stretch_feeds); a stretch after one starts its command there."""
    feeds: list[_HeredocFeed | None] = []
    idx = 0
    ticks = [-1, *_substitution_backticks(rest), len(rest)]
    for left, end in itertools.pairwise(ticks):
        start, first = left + 1, idx
        stretch = rest[start:end]
        while idx < len(spans) and spans[idx][0] < end:
            idx += 1
        if idx > first:
            opener = spans[first][0] - start
            local = [
                (at - start - opener, word_end - start - opener)
                for at, word_end in spans[first:idx]
            ]
            if first:
                before = line_before = stretch[:opener]
            feeds += _stretch_feeds(before, line_before, stretch[opener:], local)
    return feeds


def _stretch_feeds(
    before: str, line_before: str, rest: str, spans: list[tuple[int, int]]
) -> list[_HeredocFeed | None]:
    """The feeds of the heredocs of *spans* (_opener_line_feeds) on a stretch
    of an opener line that holds no substitution backtick, read as one run of tokens
    (_line_words). When the stretch does not read as one, because the text
    between a delimiter and the next operator opens a quote or parenthesis it
    does not close, and every quote and parenthesis of the stretch is in such
    text, each heredoc's command is read as _opener_tokens cuts it, in runs of
    one pass each: it ends before the first quote of the first such text after
    its delimiter, and starts after the last quote of the last one before its
    operator. Any other stretch leaves each heredoc to read the text up to its
    own operator and from its own delimiter on (_heredoc_by_heredoc_feeds)."""
    head = _opener_tokens(before, before=True)
    if head is None:
        head = _opener_tokens(line_before, before=True)
    if head is None:
        return _heredoc_by_heredoc_feeds(before, line_before, rest, spans)
    read = _line_words(head, rest, len(spans))
    if read is not None:
        return list(_fed_commands(*read))
    # (the last heredoc before a cut, where the command before it ends, where the one after starts)
    cuts: list[tuple[int, int, int | None]] = []
    quotes = 0  # the quotes and parentheses of the text that cannot be tokenized
    for idx, (_, word_end) in enumerate(spans):
        stop = spans[idx + 1][0] if idx + 1 < len(spans) else len(rest)
        between = rest[word_end:stop]
        if _shell_tokens(_FD_NUMBER_RE.sub("", between)) is None:
            first = min((pos for pos in map(between.find, "\"')") if pos != -1), default=-1)
            last = max(map(between.rfind, "\"'("))
            if first == -1 or last == -1:
                return _heredoc_by_heredoc_feeds(before, line_before, rest, spans)
            cuts.append((idx, word_end + first, word_end + last + 1))
            quotes += sum(map(between.count, "\"'()"))
    if not cuts or quotes != sum(map(rest.count, "\"'()")):
        return _heredoc_by_heredoc_feeds(before, line_before, rest, spans)
    feeds: list[_HeredocFeed | None] = []
    first = 0  # the first heredoc of the run read next
    for last, end, resume in [*cuts, (len(spans) - 1, len(rest), None)]:
        if first <= last:
            opener = spans[first][0]
            read = None if head is None else _line_words(head, rest[opener:end], last + 1 - first)
            if read is None:
                return _heredoc_by_heredoc_feeds(before, line_before, rest, spans)
            feeds += _fed_commands(*read)
        first = last + 1
        if resume is not None and first < len(spans):
            head = _opener_tokens(rest[resume : spans[first][0]], before=True)
    return feeds


def _line_words(head: list[str], rest: str, count: int) -> tuple[list[str], list[int]] | None:
    """The words of the command text whose tokens before the first of *count*
    heredoc operators are *head* and whose text from that operator on is
    *rest*, without the operators and their delimiter words, and where each
    heredoc stood in them; None unless *rest* tokenizes with one `<<` per
    heredoc, the first at its start."""
    tail = _shell_tokens(_FD_NUMBER_RE.sub("", rest))
    if tail is None:
        return None
    operators = [pos for pos, token in enumerate(tail) if token == "<<"]
    if len(operators) != count or operators[0] != 0 or operators[-1] == len(tail) - 1:
        return None
    words, cuts, last = list(head), [], 0
    for pos in operators:
        words += tail[last:pos]
        cuts.append(len(words))
        last = pos + 2
    words += tail[last:]
    return words, cuts


def _heredoc_by_heredoc_feeds(
    before: str, line_before: str, rest: str, spans: list[tuple[int, int]]
) -> list[_HeredocFeed | None]:
    """The feeds of the heredocs of *spans* (_stretch_feeds), each read from
    the text up to its own operator and from its own delimiter on
    (_opener_tokens), the cuts made there."""
    feeds: list[_HeredocFeed | None] = []
    for idx, (start, word_end) in enumerate(spans):
        prefix = _opener_tokens(before + rest[:start], before=True)
        if prefix is None:
            prefix = _opener_tokens(line_before + rest[:start], before=True)
        suffix = _opener_tokens(rest[word_end:], before=False)
        if prefix is None or suffix is None:
            feeds.append(None)
            continue
        # the line's heredocs before this one are the last of the prefix, those after it
        # all of the suffix
        prefix = _without_heredoc_operators(prefix, idx)
        feeds += _fed_commands(prefix + _without_heredoc_operators(suffix, None), [len(prefix)])
    return feeds


def _without_heredoc_operators(tokens: list[str], count: int | None) -> list[str]:
    """*tokens* without the last *count* `<<` operators (every one when None)
    and the delimiter word after each."""
    if count == 0:
        return tokens
    cut = [pos for pos, token in enumerate(tokens[:-1]) if token == "<<"]
    if count is not None:
        cut = cut[-count:]
    kept, last = [], 0
    for pos in cut:
        kept += tokens[last:pos]
        last = pos + 2
    return kept + tokens[last:]


def _fed_commands(words: list[str], cuts: list[int]) -> Iterator[_HeredocFeed]:
    """The feed (_heredoc_feeds) of the heredoc whose redirection stood at each
    of *cuts* in *words*, the command text with those redirections cut out:
    the command whose redirections it is among, the operator that ends it and
    where a pipeline it opens runs its output (_pipe_runners), the pipes read
    once for all of them. After a `(...)` or `{ ...; }` group the heredoc is
    the group's stdin, and its words are the group's command list. Heredocs
    that feed one command share one feed (`cat <<A <<B`, `cat <<A x <<B`)."""
    segments = _segments_with_breaks(words)
    piped = (
        _pipe_runners(words) if any(operator in _PIPE_TOKENS for _, operator in segments) else {}
    )
    # where the operator that ends each segment stands in *words*
    ends = list(itertools.accumulate((len(command) + 1 for command, _ in segments), initial=-1))[1:]
    by_cut: dict[int, _HeredocFeed] = {}  # the feed of each place a heredoc stands, read once
    by_segment: dict[int, _HeredocFeed] = {}  # the feed of each command, read once
    segment = 0
    for cut in cuts:
        while cut > ends[segment]:
            segment += 1
        if cut not in by_cut:
            command, operator = segments[segment]
            group = _closed_group(words, cut)
            if group is not None:
                by_cut[cut] = (
                    group + words[cut : ends[segment]],
                    operator,
                    piped.get(ends[segment]),
                )
            else:
                if segment not in by_segment:
                    by_segment[segment] = (command, operator, piped.get(ends[segment]))
                by_cut[cut] = by_segment[segment]
        yield by_cut[cut]


@functools.lru_cache(maxsize=64)
def _heredoc_text_starts(text: str) -> frozenset[int]:
    """The start of each heredoc of *text* whose body only a text reader takes:
    no reading of _heredoc_scripts runs it, no pipe carries the output of the
    command it feeds (_heredoc_feeds) on, and that command, read closed as an
    ssh remote command is (_list_stdin_state), runs no stdin (`sh -c 'cat >
    f'`, `sudo sh -c 'mkdir -p d && cat > d/f'`, `sh -c 'python3 -'`). A
    program the closed reading does not list may run it (`bash x.sh <<EOF`,
    `sh -c 'sudo -i'`), and so may a command it cannot read."""
    run = {heredoc.start for heredoc, _, _ in _heredoc_scripts(text)}
    starts: set[int] = set()
    text_only: dict[int, bool] = {}  # by feed, once: heredocs on one command share theirs
    for heredoc, feed in zip(_heredocs(text), _heredoc_feeds(text), strict=True):
        if feed is None or heredoc.start in run:
            continue
        if id(feed) not in text_only:
            command, operator, _ = feed
            text_only[id(feed)] = operator not in _PIPE_TOKENS and not _list_stdin_runner(
                command, closed=True
            )
        if text_only[id(feed)]:
            starts.add(heredoc.start)
    return frozenset(starts)


def _heredoc_output_runners(text: str, heredocs: list[_Heredoc]) -> dict[int, str]:
    """For the index of each of *heredocs* (those of *text*) whose command's
    output a pipeline runs as a script, where it runs it (_output_runners),
    read from *text* with every body cut and a numbered tag left for each
    (_HEREDOC_MARK), as _data_heredocs reads it. When that text cannot be
    tokenized, each line of it that cannot be on its own is blanked, so a line
    the tokens misread (`echo "$(echo 'it"s')"`, which the shell runs) hides
    no group or pipe on the others; empty when the text still cannot be."""
    marked = [
        dataclasses.replace(heredoc, tag=f"{_HEREDOC_MARK}{idx}")
        for idx, heredoc in enumerate(heredocs)
    ]
    cut = _FD_NUMBER_RE.sub("", _cut_heredoc_bodies(text, marked)[0])
    tokens = _command_tokens(cut)
    if tokens is None:
        lines = cut.split("\n")
        tokens = _command_tokens(
            "\n".join(line if _command_tokens(line) is not None else "" for line in lines)
        )
    if tokens is None:
        return {}
    runners = _output_runners(tokens)
    found: dict[int, str] = {}
    for pos, token in enumerate(tokens):
        if pos and tokens[pos - 1] == "<<" and token.startswith(_HEREDOC_MARK):
            number = token[len(_HEREDOC_MARK) :]
            if number.isdigit() and (runner := runners[pos]):
                found[int(number)] = runner
    return found


def _body_substitutions(text: str, heredoc: _Heredoc) -> list[str]:
    """The body of each command substitution in the body of *heredoc* in
    *text* when the shell expands it (_expands_body): it runs each one as it
    expands the body, whatever command reads it, a data reader or a file
    write too (_missed_substitutions). An arithmetic `$((...))` runs nothing."""
    body = text[heredoc.body_start : heredoc.body_end]
    if not _expands_body(text, heredoc):
        return []
    if "$(" not in body and "`" not in body:
        return []
    return [
        inner
        for inner in _missed_substitutions(body, False, heredoc=True)
        if not inner.startswith("(")
    ]


def _opened_group(tokens: list[str]) -> list[str] | None:
    """The tokens inside the `(...)` or `{ ...; }` group that *tokens* start
    with, or None when they start with no group or its closing word is not
    among them."""
    if not tokens or tokens[0] not in ("(", "{"):
        return None
    opener = tokens[0]
    closer = ")" if opener == "(" else "}"
    depth = 0
    for idx, token in enumerate(tokens):
        depth += (token == opener) - (token == closer)
        if depth == 0:
            return tokens[1:idx]
    return None


def _pipe_runners(tokens: list[str]) -> dict[int, str]:
    """For the position in *tokens* of each `|` or `|&` whose pipeline runs
    what is piped in as a script, where it runs it, "local" or "remote": a
    later command of the pipeline reads its stdin as a script (_stdin_runner),
    or a command in a `(...)` or `{ ...; }` group of it reads the group's
    (_list_stdin_runner). Each command between is taken to pass the text on,
    as cat, tee and a filter do. Read from the last pipe back, so each
    command is read once."""
    runners: dict[int, str] = {}
    next_break = len(tokens)
    for pos in range(len(tokens) - 1, -1, -1):
        token = tokens[pos]
        if token in _PIPE_TOKENS:
            opens = tokens[pos + 1 : pos + 2] in (["("], ["{"])
            group = _opened_group(tokens[pos + 1 :]) if opens else None
            if group is not None:
                runner = _list_stdin_runner(group)
                after = pos + len(group) + 3  # the token after the group's closing word
            else:
                runner = _stdin_runner(tokens[pos + 1 : next_break])
                after = next_break
            runner = runner or runners.get(after)
            if runner:
                runners[pos] = runner
        if token in _SEGMENT_BREAK_TOKENS:
            next_break = pos
    return runners


def _output_runners(tokens: list[str]) -> list[str | None]:
    """For each position in *tokens*, where the output of the command there
    is run as a script, "local", "remote" or None: the pipe that ends the
    command feeds a pipeline that runs it (_pipe_runners), or the output
    leaves a `(...)` or `{ ...; }` group or a compound command around it
    (`for`, `while`, `until`, `select`, `if`, `case` to its closing word) and
    the pipe after that group, or after one around it, does (`{ cat <<EOF; }
    | bash`, `for ...; done | sh`). The groups and compound commands are read
    as the data heredoc reader reads them (_enclosures). The output of a
    command that copies its text to a file a later command runs is run there
    too (_run_writers: `cat > x.sh <<EOF && bash x.sh`, `echo ls > x.sh; .
    ./x.sh`)."""
    pipe_runners = _pipe_runners(tokens)
    # the first segment break at or after each position
    breaks = [len(tokens)] * (len(tokens) + 1)
    for pos in range(len(tokens) - 1, -1, -1):
        breaks[pos] = pos if tokens[pos] in _SEGMENT_BREAK_TOKENS else breaks[pos + 1]
    # the segment break after each command whose text a file written and run carries
    segments = _segments_with_breaks(tokens)
    ends = list(itertools.accumulate(len(words) + 1 for words, _ in segments))
    written = {
        ends[index] - 1: runner
        for index, runner in _run_writers(
            [(_without_reserved_prefix(words), operator) for words, operator in segments]
        ).items()
    }
    enclosures = _enclosures(tokens)
    group_runners: list[str | None] = []
    for parent, close in zip(enclosures.parents, enclosures.closes, strict=True):
        runner = pipe_runners.get(breaks[close + 1]) if close is not None else None
        group_runners.append(runner or (group_runners[parent] if parent is not None else None))
    return [
        pipe_runners.get(breaks[pos])
        or written.get(breaks[pos])
        or (group_runners[group] if group is not None else None)
        for pos, group in enumerate(enclosures.innermost)
    ]


def _token_fed_scripts(tokens: list[str]) -> list[tuple[str, str]]:
    """(script, runner) for each here-string (`bash <<< '...'`) and each
    `echo`/`printf` whose output a pipeline reads as a script
    (_output_runners: `echo x | tee y | bash`, `{ echo x; } | bash`, `cat
    <<< x | bash`), runner being "local" or "remote". When the here-string
    follows a `(...)` or `{ ...; }` group, a command in the group that reads
    the group's stdin counts (_list_stdin_runner)."""
    scripts: list[tuple[str, str]] = []
    segments = _segments_with_breaks(tokens)
    output_runners = _output_runners(tokens)
    start = 0
    for segment, _ in segments:
        for pos, token in enumerate(segment[:-1]):
            if token == "<<<":
                group = _closed_group(tokens, start + pos)
                runner = _list_stdin_runner(group) if group is not None else _stdin_runner(segment)
                runner = runner or output_runners[start + pos]
                if runner:
                    scripts.append((segment[pos + 1], runner))
        runner = output_runners[start] if segment else None
        if runner:
            scripts.extend((echoed, runner) for echoed in _echoed_texts(segment))
        start += len(segment) + 1
    return scripts


def _missed_substitutions(
    text: str, tokenized: bool, heredoc: bool = False, spans: list[tuple[int, int]] | None = None
) -> list[str]:
    """The body of each command substitution in *text* whose commands its
    tokens miss, at any depth. Tokens split an unquoted `$(...)` at its
    parentheses and read its commands; they miss a backtick substitution, a
    `$(...)` inside a double-quoted word, and every substitution of text that
    cannot be tokenized (*tokenized* False). A body comes back without the
    substitutions inside it that its own tokens miss (each comes back on its
    own), so each character is tokenized once. A substitution inside single
    quotes or a comment is text, and an unterminated one is not taken. With
    *heredoc*, *text* is the body of a heredoc whose delimiter is unquoted:
    the shell runs each substitution in it, and its quotes and `#` are plain
    characters. *spans* receives the (start, end) in *text* of each whole body
    that no other missed substitution holds.
    """
    bodies: list[str] = []
    # Each missed substitution: where its body starts, and the spans of the
    # missed ones inside it, cut from its body.
    missed: list[tuple[int, list[tuple[int, int]]]] = []
    # Each open frame: (kind, where its body starts, the index in *missed* of
    # the substitution whose tokens read it (-1 for *text* itself), whether it
    # opened that substitution). Kind is "top", "<<" (*text* is a heredoc
    # body), "$(", "(" (a group inside a `$(...)`), "`" or '"'.
    stack = [("<<" if heredoc else "top", 0, -1, False)]
    comments = dict(shell_comment_spans(text, heredoc_body=heredoc))
    i, n = 0, len(text)
    while i < n:
        kind, start, owner, opens = stack[-1]
        char = text[i]
        if char == "\\":
            i += 2
            continue
        sub = "$(" if text.startswith("$(", i) else "`" if char == "`" and kind != "`" else ""
        if sub:
            body_start = i + len(sub)
            if sub == "`" or kind in ('"', "<<") or (kind == "top" and not tokenized):
                missed.append((body_start, []))
                stack.append((sub, body_start, len(missed) - 1, True))
            else:
                stack.append((sub, body_start, owner, False))
            i = body_start
            continue
        closes = (kind == "`" and char == "`") or (kind in ("$(", "(") and char == ")")
        if closes or (kind == '"' and char == '"'):
            stack.pop()
            if closes and opens:
                body_start, cuts = missed[owner]
                kept, pos = [], body_start
                for cut_start, cut_end in cuts:
                    kept.append(text[pos:cut_start])
                    pos = cut_end
                bodies.append("".join(kept) + text[pos:i])
                parent = stack[-1][2]
                if parent >= 0:
                    missed[parent][1].append((start, i))
                elif spans is not None:
                    spans.append((start, i))
        elif kind in ('"', "<<"):
            pass
        elif char == "(" and kind in ("$(", "("):
            stack.append(("(", i, owner, False))
        elif char == '"':
            stack.append(('"', i + 1, owner, False))
        elif char == "'" or text.startswith("$'", i):
            ansi_c = char == "$"
            j = i + 2 if ansi_c else i + 1
            while j < n and text[j] != "'":
                j += 2 if ansi_c and text[j] == "\\" else 1
            if j >= n:
                break
            i = j
        elif i in comments:
            i = comments[i]
            continue
        i += 1
    return bodies


def _substitution_fed_scripts(text: str, tokenized: bool, over_ssh: bool) -> list[tuple[str, str]]:
    """(script, runner) for each script fed to a shell (_token_fed_scripts)
    inside a command substitution of *text* that its tokens miss
    (_missed_substitutions; *tokenized* False when *text* cannot be
    tokenized). With *over_ssh*, the command an ssh destination runs in one is
    a script too, runner "remote"."""
    scripts: list[tuple[str, str]] = []
    for body in _missed_substitutions(text, tokenized):
        tokens = _shell_tokens(_FD_NUMBER_RE.sub("", body))
        if tokens is not None:
            scripts += _token_fed_scripts(tokens)
            if over_ssh:
                scripts += [(command, "remote") for command in _ssh_remote_commands(tokens)]
    return scripts


def _ssh_remote_commands(tokens: list[str]) -> list[str]:
    """The remote command of each ssh command in *tokens* that names one. Each
    simple command is read as the shell reader reads it (_run_words): past the
    reserved words, group openers and wrappers (xargs, nice) the shell skips
    before a command word; and inside a process substitution (`<(`)."""
    commands: list[str] = []
    opened = ["(" if token in ("<(", ">(") else token for token in tokens]
    for segment, _ in _segments_with_breaks(opened):
        words = _run_words(segment)[0]
        if words and Path(words[0]).name == "ssh":
            remote = _ssh_remote_words(words)
            if remote:
                commands.append(" ".join(remote))
    return commands


# git subcommands whose `-m`/`--message` value is a message (to `git log`, `-m`
# takes no value and a word after it is a path), the letters of their options
# that take one with the flags that share a cluster with it and take none
# (`-am x`). `git commit` and `git tag` store a `--trailer` as well: text beside
# the message, blanked with it, and no paragraph of it. `--author` and `--date`
# are parsed by git, and merge, notes and stash have no `--trailer`.
_GIT_MESSAGE_SUBCOMMANDS = {"commit", "merge", "tag", "notes", "stash"}
_GIT_MESSAGE_LETTERS = "m"
_GIT_MESSAGE_LONGS = ("--message",)
_GIT_MESSAGE_FLAGS = "aqs"
_GIT_TRAILER_SUBCOMMANDS = {"commit", "tag"}
_GIT_TRAILER_LONGS = ("--trailer",)
# The gh options whose value is free text a command stores (a title, body, notes,
# subject or description), by the command's path: the letters of its options that take
# one, and their long names, as `gh help reference` lists them (gh 2.92), with the
# aliases of a command (`pr new`). A command it does not list has none: an extension
# takes its arguments as its own, the `-b` of `gh secret set` is a secret's value, and
# a `-t` is a template or a team on the rest (`gh project edit`, `gh repo create`).
_GH_TITLE_BODY = ("tb", ("--title", "--body"))
_GH_BODY = ("b", ("--body",))
_GH_NOTES_TITLE = ("nt", ("--notes", "--title"))
_GH_DESC = ("d", ("--desc",))
_GH_DESCRIPTION = ("d", ("--description",))
_GH_TEXT_OPTIONS: dict[tuple[str, ...], tuple[str, tuple[str, ...]]] = {
    ("pr", "create"): _GH_TITLE_BODY,
    ("pr", "new"): _GH_TITLE_BODY,
    ("pr", "edit"): _GH_TITLE_BODY,
    ("pr", "revert"): _GH_TITLE_BODY,
    ("pr", "merge"): ("tb", ("--subject", "--body")),
    ("pr", "comment"): _GH_BODY,
    ("pr", "review"): _GH_BODY,
    ("issue", "create"): _GH_TITLE_BODY,
    ("issue", "new"): _GH_TITLE_BODY,
    ("issue", "edit"): _GH_TITLE_BODY,
    ("issue", "comment"): _GH_BODY,
    ("release", "create"): _GH_NOTES_TITLE,
    ("release", "new"): _GH_NOTES_TITLE,
    ("release", "edit"): _GH_NOTES_TITLE,
    ("gist", "create"): _GH_DESC,
    ("gist", "new"): _GH_DESC,
    ("gist", "edit"): _GH_DESC,
    ("label", "create"): _GH_DESCRIPTION,
    ("label", "edit"): _GH_DESCRIPTION,
    ("repo", "create"): _GH_DESCRIPTION,
    ("repo", "new"): _GH_DESCRIPTION,
    ("repo", "edit"): _GH_DESCRIPTION,
    ("project", "copy"): ("", ("--title",)),
    ("project", "create"): ("", ("--title",)),
    ("project", "edit"): ("d", ("--title", "--description")),
    ("project", "item-create"): ("", ("--title", "--body")),
    ("project", "item-edit"): ("", ("--title", "--body")),
    ("gpg-key", "add"): ("t", ("--title",)),
    ("ssh-key", "add"): ("t", ("--title",)),
    ("repo", "deploy-key", "add"): ("t", ("--title",)),
}
# The git or gh that leads a command whose messages _mask_message_words cuts
# out, and one word after it: its blanks (a newline ends the command) and the
# adjacent parts the shell joins. Text that can run code: `$(`, `${`, a backtick.
_MESSAGE_LEAD_RE = re.compile(r"\b(?:git|gh)(?=[ \t])")
_MESSAGE_WORD_RE = re.compile(rf"[ \t]+(?:{_SHELL_WORD_PART_RE.pattern})+")
# A redirection among those words (`2>&1`, `&>/dev/null`, `< in`), with the word it
# names: it is no word of the command. An fd number only after a blank, so a text
# is read in one parse.
_MESSAGE_REDIRECTION_RE = re.compile(
    rf"[ \t]+[0-9]*(?:<<<|<<-?|<>|<&|<|>>|>&|>\||>|&>>|&>)[ \t]*(?:{_SHELL_WORD_PART_RE.pattern})+"
)
_LIVE_TEXT_RE = re.compile(r"\$[({]|`")
# What ends the word before a command's first word: a list operator or an opening
# parenthesis (`$(`, `<(`), or a newline. A word of a path, or an assignment
# before the command, holds none of these, a quote or a backslash.
_COMMAND_SEPARATORS = ";&|(\n"
_COMMAND_WORD_END = " \t\n;&|()<>'\"`\\"
_PLAIN_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=[^\s;&|()<>'\"`\\]*")


def _is_command_separator(text: str, at: int, quoted: set[int]) -> bool:
    """True when the character at *at* in *text*, one of _COMMAND_SEPARATORS,
    ends a command: it is unquoted, no backslash escapes it, and it is no part
    of a redirection (`2>&1`, `>|`)."""
    before = at
    while before and text[before - 1] == "\\":
        before -= 1
    redirect = text[at] in "&|" and text[at - 1 : at] in ("<", ">")
    return at not in quoted and (at - before) % 2 == 0 and not redirect


def _starts_command(text: str, start: int, quoted: set[int]) -> bool:
    """True when the word at *start* in *text* is the first word of a command the
    shell runs: the text, a line, a list operator or an opening parenthesis
    comes before it, with only the `NAME=value` words that precede a command
    between. An argument of another command (`echo git commit -m ...`, `ssh
    host git commit -m ...`) is not: the shell that re-reads that line, or the
    ssh destination, runs the words after its message's `;`. Nor is a word
    behind a wrapper (`env`, `time`), which fails safe: its message stays in
    the text. A path to the command (`/usr/bin/git`) is the command."""
    if text[start - 1 : start] == "/":
        while start and text[start - 1] not in _COMMAND_WORD_END + "=":
            start -= 1
    while True:
        end = start
        while end and text[end - 1] in " \t":
            end -= 1
        if not end:
            return True
        char = text[end - 1]
        if char in _COMMAND_SEPARATORS:
            return _is_command_separator(text, end - 1, quoted)
        if end == start:
            return False
        start = end
        while start and text[start - 1] not in _COMMAND_WORD_END:
            start -= 1
        if not _PLAIN_ASSIGNMENT_RE.fullmatch(text, start, end):
            return False


def _text_options(
    args: list[str], letters: str, longs: tuple[str, ...], flags: str = ""
) -> list[tuple[int, str | None]]:
    """The text options (*letters*, *longs*) in *args*, as (the index of the
    option word, its value when glued on, None when the next word is the value):
    `-m x`, `-mx`, `--title x`, `--title=x`. In a cluster the first letter
    outside *flags*, the ones that take no value (`-am x`), owns the rest.
    Everything after a `--` is an operand.
    """
    found: list[tuple[int, str | None]] = []
    skip_next = False
    for pos, arg in enumerate(args):
        if skip_next:
            skip_next = False
        elif arg == "--":
            break
        elif arg.startswith("--"):
            name, equals, value = arg.partition("=")
            if name in longs:
                found.append((pos, value if equals else None))
                skip_next = not equals
        elif arg.startswith("-") and arg != "-":
            first = next((i for i, ch in enumerate(arg[1:], 1) if ch not in flags), None)
            if first is not None and arg[first] in letters:
                glued = arg[first + 1 :]
                found.append((pos, glued or None))
                skip_next = not glued
    return found


def _text_option_indexes(
    args: list[str], letters: str, longs: tuple[str, ...], flags: str = ""
) -> set[int]:
    """Indexes in *args* of the text options (_text_options) and their values: a
    message is words, never a file the command reads."""
    found: set[int] = set()
    for pos, glued in _text_options(args, letters, longs, flags):
        found.add(pos)
        if glued is None and pos + 1 < len(args):
            found.add(pos + 1)
    return found


def _gh_text_spec(args: list[str]) -> tuple[int, str, tuple[str, ...]]:
    """Where the options of the gh command with the words *args* start, and the
    letters and long names of those that take free text (_GH_TEXT_OPTIONS): the
    entry of its path, the words that lead *args* with gh's repo selector among
    them passed over (_gh_path). Nothing (0, no letters, no long names) for a
    command the table does not list, and for a path another option breaks
    (`gh --jq .x pr create`): its text stays visible."""
    path, ends = _gh_path(args, 3)
    for size in range(len(path), 1, -1):
        if (options := _GH_TEXT_OPTIONS.get(path[:size])) is not None:
            return ends[size - 1], *options
    return 0, "", ()


def _git_text_longs(sub: str) -> tuple[str, ...]:
    """The long names of the options of the git subcommand *sub* whose value is text
    it stores: its message, and a commit's or tag's trailer."""
    if sub in _GIT_TRAILER_SUBCOMMANDS:
        return (*_GIT_MESSAGE_LONGS, *_GIT_TRAILER_LONGS)
    return _GIT_MESSAGE_LONGS


def _message_word_indexes(lead: str, args: list[str]) -> set[int]:
    """Indexes in *args* of the words of a `lead` command that are message text:
    those of a git message subcommand (a commit's or tag's trailer too) and the
    free-text options of a gh command (_gh_text_spec)."""
    if lead == "gh":
        start, letters, longs = _gh_text_spec(args)
        return {start + pos for pos in _text_option_indexes(args[start:], letters, longs)}
    if lead == "git":
        idx = _git_globals(args).subcommand
        if idx < len(args) and args[idx] in _GIT_MESSAGE_SUBCOMMANDS:
            text = _text_option_indexes(
                args[idx + 1 :],
                _GIT_MESSAGE_LETTERS,
                _git_text_longs(args[idx]),
                _GIT_MESSAGE_FLAGS,
            )
            return {idx + 1 + pos for pos in text}
    return set()


def _inert_word(word: str) -> bool:
    """True when the shell word *word* (its parts as _SHELL_WORD_PART_RE reads
    them) can run no code: no unquoted `$`, and no `$(`, `${` or backtick inside
    double quotes. Single and ANSI-C quotes hold only text."""
    return not any(
        part.group(4) == "$" or (part.group(3) is not None and _LIVE_TEXT_RE.search(part.group(3)))
        for part in _SHELL_WORD_PART_RE.finditer(word)
    )


def _message_words(text: str, pos: int) -> list[tuple[int, int]]:
    """The (start, end) spans in *text* of the words that follow *pos* on its
    command line, the way _SHELL_WORD_PART_RE reads them: a list operator or a
    parenthesis ends the run. A redirection (_MESSAGE_REDIRECTION_RE) is passed
    over, since the shell takes it out of the command's words."""
    words: list[tuple[int, int]] = []
    while True:
        if redirection := _MESSAGE_REDIRECTION_RE.match(text, pos):
            pos = redirection.end()
        elif word := _MESSAGE_WORD_RE.match(text, pos):
            pos = word.end()
            words.append((pos - len(word.group().lstrip(" \t")), pos))
        else:
            return words


def _plain_message_word(text: str, start: int, end: int) -> bool:
    """Whether the message word *text*[start:end] can run no code: it is inert
    (_inert_word) and no `(` or backtick follows it glued on, which the shell
    reads as a substitution of the same word."""
    return text[end : end + 1] not in ("(", "`") and _inert_word(text[start:end])


def _mask_message_words(text: str) -> str:
    """*text* with the message words of its git and gh commands cut out
    (_message_word_indexes), so a nested shell that a message only quotes
    (`git commit -m "fix: block bash -c 'cat <env file>'"`) is no payload to
    read. A word is cut only when it can run no code (_inert_word): a message
    that holds a `$(...)` or backtick body stays whole, and so does a command
    that follows the message. The cut is kept only when the tokens of the result
    are those of *text* less the cut words: where the words read here are not
    the words the shell reads (a backslash before a quote, an unbalanced
    quote), the text comes back unchanged. The words are read from a git or gh
    that is the command the shell runs, outside quotes (_starts_command), never
    from one that is an argument of another command: `echo git commit -m "x; y"
    | bash` and `ssh host git commit -m "x; y"` run `y`. A message that is not
    cut stays visible to the nested-shell search, which over-blocks.
    """
    lead = _MESSAGE_LEAD_RE.search(text)
    if lead is None:
        return text
    quoted = _quoted_positions(text)
    cuts: list[tuple[int, int, str]] = []
    pos = 0
    while lead:
        pos = lead.end()
        if lead.start() not in quoted and _starts_command(text, lead.start(), quoted):
            words = _message_words(text, pos)
            pos = words[-1][1] if words else pos
            values = [_shell_word_text(text[start:end])[0] for start, end in words]
            for idx in sorted(_message_word_indexes(lead.group(), values)):
                start, end = words[idx]
                if _plain_message_word(text, start, end):
                    cuts.append((start, end, values[idx]))
        lead = _MESSAGE_LEAD_RE.search(text, pos)
    if not cuts:
        return text
    kept: list[str] = []
    last = 0
    for start, end, _ in cuts:
        kept.append(text[last:start])
        last = end
    kept.append(text[last:])
    masked = "".join(kept)
    before = _shell_tokens(_strip_heredoc_bodies(text))
    after = _shell_tokens(_strip_heredoc_bodies(masked))
    if before is None or after is None:
        return text
    cut_values = [value for _, _, value in cuts]
    left: list[str] = []
    seen = 0
    for token in before:
        if seen < len(cut_values) and token == cut_values[seen]:
            seen += 1
        else:
            left.append(token)
    return masked if seen == len(cut_values) and left == after else text


def _payloads_in(text: str, over_ssh: bool) -> list[tuple[str, bool, bool]]:
    """The command lines *text* runs one level down, as (payload, raw, remote).

    A payload is a `bash -c` command line (_shell_payloads_in), or a script
    fed on stdin to a shell (a heredoc, a here-string, or `echo`/`printf`
    piped in, through any commands between or out of a group or compound
    command around it: _output_runners), inside a command substitution
    that tokens miss too (_substitution_fed_scripts). With *over_ssh*, the
    command an ssh destination runs (its words, or a text fed to it unless
    its remote command takes that as text: _stdin_runner) is one too, marked
    remote. *text* holds its heredoc bodies, a payload's own as much as the
    command's (`bash -c 'bash <<EOF ...'`): shell-fed bodies are read from it
    (and come back raw: they hold heredoc bodies of their own), so is each
    command substitution in a body the shell expands (_body_substitutions, also
    raw), and every body is stripped before the rest is read, so a `bash -c` in
    a PR body is never a payload; nor is one in a commit message or a gh title
    or body (_mask_message_words). The one body read into the rest is that of
    a heredoc a command substitution only cats, which the substitution expands
    to: the payload that holds the substitution comes back with the body in its
    place, raw (_substituted_heredoc_payloads). Each payload is listed once,
    however many readings find it.
    """
    text = _mask_message_words(text)
    stripped = _strip_heredoc_bodies(text)
    found = [(body, True, runner) for _, body, runner in _heredoc_scripts(text)]
    for heredoc in _heredocs(text):
        found += [(body, True, "local") for body in _body_substitutions(text, heredoc)]
    found += [
        (payload, False, runner) for payload, runner in _cut_text_payloads(stripped, over_ssh)
    ]
    found += [
        (payload, True, runner) for payload, runner in _substituted_heredoc_payloads(text, over_ssh)
    ]
    return [
        (payload, is_raw, runner == "remote")
        for payload, is_raw, runner in dict.fromkeys(found)
        if over_ssh or runner == "local"
    ]


def _cut_text_payloads(cut: str, over_ssh: bool) -> list[tuple[str, str]]:
    """(payload, runner) for each command line the text *cut*, its heredoc
    bodies cut, runs one level down (_payloads_in): a script fed to a shell by
    its tokens (_token_fed_scripts) or in a substitution they miss
    (_substitution_fed_scripts), with *over_ssh* an ssh remote command, and a
    shell's `-c` payload (_shell_payloads_in)."""
    found: list[tuple[str, str]] = []
    tokens = _shell_tokens(_FD_NUMBER_RE.sub("", cut))
    if tokens is not None:
        found += _token_fed_scripts(tokens)
        if over_ssh:
            found += [(command, "remote") for command in _ssh_remote_commands(tokens)]
    found += _substitution_fed_scripts(cut, tokens is not None, over_ssh)
    found += [(payload, "local") for payload in _shell_payloads_in(cut)]
    return found


# A `cat` that writes a plain text on its stdin as it reads it, path-qualified or not: no
# operand but one that names its stdin (`-`, `--`, `/dev/stdin`), and no option but `-u` and
# those that only show what a plain text lacks (non-printing characters, runs of blank lines,
# tabs: `-v`, `-s`, `-t`, `-T`, their long forms). An option that changes every line (`-n`,
# `-b`, `-e`, `-E`, `-A`, their long forms) or a file operand makes its output other text.
# The heredoc message reader (_MESSAGE_CAT_RE) and the payload splice
# (_CAT_HEREDOC_SUBSTITUTION_RE) share it.
_STDIN_CAT = (
    r"(?:[^\s()`]*/)?cat"
    r"(?:[ \t]+(?:-(?:-(?:show-nonprinting|squeeze-blank|show-tabs)?|[uvstT]*)|/dev/stdin))*"
)
# A command substitution, `$(...)` or backticks, whose only command is a stdin cat
# (_STDIN_CAT) of heredocs their bodies were cut from, each heredoc's tag numbered
# (_HEREDOC_MARK), a comment at most after them on their line; group 1 or 2 the number
# of the last, the one cat reads (the shell reads each body, expanding an unquoted one,
# and gives cat the last as its stdin).
_CAT_HEREDOC_SUBSTITUTION_RE = re.compile(
    rf"\$\([ \t\n]*{_STDIN_CAT}(?:[ \t]*<<{_HEREDOC_MARK}\d+)*[ \t]*<<{_HEREDOC_MARK}(\d+)"
    rf"(?:[ \t]+#[^\n]*)?[ \t\n]*\)"
    rf"|`[ \t\n]*{_STDIN_CAT}(?:[ \t]*<<{_HEREDOC_MARK}\d+)*[ \t]*<<{_HEREDOC_MARK}(\d+)"
    rf"(?:[ \t]+#[^\n]*)?[ \t\n]*`"
)


def _substituted_heredoc_payloads(text: str, over_ssh: bool) -> list[tuple[str, str]]:
    """(payload, runner) for each command line of *text* (_cut_text_payloads,
    and an `eval`'s words: _eval_payloads) that holds a command substitution
    whose only command writes a heredoc as read (_CAT_HEREDOC_SUBSTITUTION_RE), with the
    heredoc's body in place of the substitution, as the shell expands it before
    the nested shell, eval or ssh destination reads the text: `bash -c "$(cat
    <<'EOF' ... EOF)"` runs the body. The body is the one cat writes, a `<<-`
    body without the tabs that open its lines, less the trailing newlines the
    substitution drops; that of an unquoted delimiter is
    taken as written, its expansions unread, as a body piped into a shell is
    (_heredoc_scripts). *text* holds its heredoc bodies; they are cut with
    numbered tags, so each substitution names its own heredoc, and the payload
    comes back raw: each other heredoc it opens gets its opener and body back as
    written, the body after the line that opens it, so no bare `<<TAG` reads as
    a body that runs to the end and hides what follows."""
    heredocs = _heredocs(text)
    if not heredocs:
        return []
    marked = [
        dataclasses.replace(heredoc, tag=f"{_HEREDOC_MARK}{idx}")
        for idx, heredoc in enumerate(heredocs)
    ]
    cut = _cut_heredoc_bodies(text, marked)[0]
    if not _CAT_HEREDOC_SUBSTITUTION_RE.search(cut):
        return []
    found = [
        *_cut_text_payloads(cut, over_ssh),
        *((payload, "local") for payload in _eval_payloads(cut)),
    ]
    return [
        (spliced, runner)
        for payload, runner in found
        if (spliced := _spliced_payload(text, heredocs, payload))
    ]


def _spliced_payload(text: str, heredocs: list[_Heredoc], payload: str) -> str | None:
    """*payload*, read from *text* with the bodies of *heredocs* cut under
    numbered tags (_substituted_heredoc_payloads), with each substitution that
    only cats them replaced by the body cat reads, that of the last, and each
    heredoc outside those substitutions given its opener and body back; None
    when it holds no such substitution."""
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement), in the order of *payload*
    for match in _CAT_HEREDOC_SUBSTITUTION_RE.finditer(payload):
        idx = int(match.group(1) or match.group(2))
        if idx < len(heredocs):
            heredoc = heredocs[idx]
            body = text[heredoc.body_start : heredoc.body_end]
            if text.startswith("<<-", heredoc.start):
                body = _LEADING_TABS_RE.sub("", body)
            # the substitution drops every trailing newline of what cat writes
            edits.append((match.start(), match.end(), body.rstrip("\n")))
    if not edits:
        return None
    starts = [start for start, _, _ in edits]
    ends = [end for _, end, _ in edits]
    for marker in _MARK_IN_WORD_RE.finditer(payload):
        idx = int(marker.group(1))
        at = bisect.bisect_right(starts, marker.start()) - 1
        if idx >= len(heredocs) or (at >= 0 and marker.start() < ends[at]):
            continue  # a forged tag, or the cat of a substitution being replaced
        heredoc = heredocs[idx]
        edits.append((marker.start(), marker.end(), text[heredoc.start : heredoc.word_end]))
        # the body comes back where it was cut: at the end of the opener line,
        # or before a substitution being replaced that the line runs into
        line_end = payload.find("\n", marker.end())
        line_end = len(payload) if line_end == -1 else line_end
        at = bisect.bisect_right(starts, line_end) - 1
        line_end = starts[at] if at >= 0 and line_end < ends[at] else line_end
        edits.append((line_end, line_end, text[heredoc.body_start - 1 : heredoc.end]))
    edits.sort(key=lambda edit: edit[:2])  # stable: bodies cut at one place keep their order
    parts: list[str] = []
    last = 0
    for start, end, replacement in edits:
        parts += [payload[last:start], replacement]
        last = end
    parts.append(payload[last:])
    return "".join(parts)


def _nested_payloads(command: str, over_ssh: bool, max_depth: int = 5) -> list[tuple[str, bool]]:
    """Every command line *command* runs below its top level, as (payload, runs on an ssh
    destination), each ready to tokenize: heredoc bodies stripped, and its continuations joined the
    way its own shell joins them (a single-quoted payload keeps the backslash-newlines of
    *command*). *command* still holds its heredoc bodies. See _payloads_in. Read once per command,
    since several checks ask for the same command's payloads."""
    return [
        (payload, remote)
        for payload, remote, _ in _cached_nested_payloads(command, over_ssh, max_depth)
    ]


@functools.lru_cache(maxsize=64)
def _cached_nested_payloads(
    command: str, over_ssh: bool, max_depth: int
) -> tuple[tuple[str, bool, str], ...]:
    payloads: list[tuple[str, bool, str]] = []
    queue = [(command, 0, False)]
    while queue:
        current, depth, remote = queue.pop()
        if depth >= max_depth:
            continue
        for inner, inner_raw, inner_remote in _payloads_in(current, over_ssh):
            inner = _join_line_continuations(inner)
            if inner:
                on_remote = remote or inner_remote
                payloads.append(
                    (_strip_heredoc_bodies(inner) if inner_raw else inner, on_remote, inner)
                )
                queue.append((inner, depth + 1, on_remote))
    return tuple(payloads)


def _ssh_remote_payloads(command: str) -> list[str]:
    """The command lines *command* runs on an ssh destination (_payloads_in), for
    a guard that matches the text of *command* with the quoted strings masked
    (_mask_quoted_text), which blanks a quoted remote command."""
    return [payload for payload, remote in _nested_payloads(command, True) if remote]


def _guarded_texts(command: str, over_ssh: bool = True) -> list[tuple[str, bool, bool]]:
    """The texts a guard checks for the commands *command* runs, as (text, nested, runs on an ssh
    destination): *command* with its commit message and heredoc bodies stripped, then every nested
    payload, the command an ssh destination runs included (*over_ssh*). A remote text cannot be
    resolved against a local repo. Each text is read by _read_runs."""
    command = strip_commit_message(command)
    return [
        (_strip_heredoc_bodies(command), False, False),
        *((payload, True, remote) for payload, remote in _nested_payloads(command, over_ssh)),
    ]


# Wrappers that run their operand, beyond _COMMAND_WRAPPERS and _EXEC_WRAPPERS.
_READER_WRAPPERS: dict[str, tuple[str, set[str], int]] = {
    "setsid": ("", set(), 0),
    "ionice": ("cnPpu", {"--class", "--classdata", "--pgid", "--pid", "--uid"}, 0),
    "watch": ("nq", {"--interval", "--equexit"}, 0),
}
# Precommand modifiers that run the next word as the command: zsh's `noglob`,
# `nocorrect` and `-`, and `builtin`.
_PRECOMMAND_MODIFIERS = frozenset({"noglob", "nocorrect", "-", "builtin"})
# Words skipped before the command word: reserved words, group openers, and the
# precommand modifiers.
_READER_SKIPPED = frozenset(
    {"!", "{", "}", "if", "then", "elif", "else", "while", "until", "do", *_PRECOMMAND_MODIFIERS}
)
_READER_CLOSERS = frozenset({"fi", "done", "esac"})
# How many commands past the last one the reader searches for the command that
# holds a backtick or double-quoted substitution.
_READER_HOLDER_WINDOW = 64
# A shell assignment word: `X=1`, `X+=1`, `X[0]=1`.
_READER_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_READER_REDIRECTIONS = frozenset({"<<<", "&>>", "<<", ">>", "<&", ">&", "<>", ">|", "&>", "<", ">"})
_READER_LIST_TOKENS = frozenset({"&&", "||", ";", ";;", "&", "|", "|&", "(", ")"})
# sudo options that run no command (list, validate, version, edit, drop the
# timestamp).
_SUDO_RUNS_NOTHING = frozenset(
    {
        "-l",
        "-ll",
        "--list",
        "-v",
        "--validate",
        "-V",
        "--version",
        "-e",
        "--edit",
        "-K",
        "--remove-timestamp",
    }
)


@dataclass(frozen=True)
class _Run:
    """One simple command a text runs, as _read_runs reads it."""

    argv: tuple[str, ...]  # what it runs, the program first; () for none
    assignments: tuple[str, ...] = ()  # the NAME=value words before argv
    wrapped: bool = False  # `command` or a wrapper stood before argv
    piped: bool = False  # a pipe feeds its stdin
    ends: str | None = None  # the list operator after it
    upstream: int | None = None  # the run whose output that pipe carries
    parent: int | None = None  # the run in whose words its `$(...)` sits
    subshell: tuple[int, ...] = ()  # the subshells it runs in, outermost first
    via_xargs: bool = False  # xargs runs it
    remote: bool = False  # it runs on an ssh destination
    unparsed: str | None = None  # a payload that could not be tokenized
    # It follows `&&` or `||` in its and-or list, or sits in an if, loop or case
    # of its subshell: the shell may skip it and still run what comes after.
    conditional: bool = False
    after: str | None = None  # the `&&` or `||` it follows in its and-or list
    chain: int = 0  # which and-or list it is in (a `( )` group's first one continues its parent's)


# What shlex.split reads as other than a literal character: its whitespace (a
# no-break space is a literal), quotes, the backslash escape.
_RESPLIT_CHANGES_RE = re.compile(r"[ \t\r\n'\"\\]")


def _resplit_keeps(word: str) -> bool:
    """Whether *word* comes back whole from shlex.split of words joined with
    spaces: it is not empty and holds no whitespace, quote or backslash."""
    return bool(word) and not _RESPLIT_CHANGES_RE.search(word)


def _resplit_from(words: list[str]) -> int:
    """The index in *words* past the last word _resplit_keeps does not keep."""
    for idx in range(len(words) - 1, -1, -1):
        if not _resplit_keeps(words[idx]):
            return idx + 1
    return 0


def _run_words(
    words: list[str], through_eval: bool = False
) -> tuple[list[str] | None, list[str], bool, str | None, str | None]:
    """(argv or None when it runs nothing, its assignments, whether `command`
    or a wrapper was skipped, an `eval` payload, xargs' replace string: None
    without xargs, "" when it appends) for the words of one simple command.
    With *through_eval*, an `eval` whose words each come back whole when the
    shell re-splits them joined (_resplit_keeps) is read past as a precommand
    modifier is, in the same pass: what it runs is the command those words
    start. Only an `eval` whose words the re-split changes gives its payload."""
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in _READER_REDIRECTIONS:
            skip = True
        else:
            kept.append(word)
    words = kept
    assignments: list[str] = []
    wrapped = False
    replace: str | None = None
    # With *through_eval*, from the first `eval` on: the index past the last
    # word a re-split changes (_resplit_from), kept up to date as words change.
    resplit: int | None = None
    i = 0
    while i < len(words):
        word = words[i]
        if _READER_ASSIGNMENT_RE.match(word):
            assignments.append(word)
            i += 1
            continue
        if word in _READER_CLOSERS and i == len(words) - 1:
            return None, assignments, wrapped, None, replace
        if word in _READER_SKIPPED:
            i += 1
            continue
        if word == "function" or word == "repeat":
            i += 2
            continue
        if word == "coproc":
            i += 3 if words[i + 2 : i + 3] == ["{"] else 1
            continue
        if word == "eval":
            if through_eval:
                if resplit is None:
                    resplit = _resplit_from(words)
                if resplit <= i + 1:
                    i += 1
                    continue
            return None, assignments, wrapped, " ".join(words[i + 1 :]), replace
        if word.startswith("=") and len(word) > 1:
            # one word for another, which the re-split keeps when it kept this one
            words[i] = word[1:]
            continue
        name = Path(word).name
        if name == "command":
            i += 1
            while i < len(words) and words[i].startswith("-") and words[i] != "-":
                if set(words[i][1:]) & {"v", "V"}:
                    return None, assignments, True, None, replace
                i += 1
                if words[i - 1] == "--":
                    break
            wrapped = True
            continue
        if name in _COMMAND_WRAPPERS:
            letters, long_options, operands = _COMMAND_WRAPPERS[name]
            i += 1
            while i < len(words) and words[i].startswith("-"):
                option = words[i]
                if name == "sudo" and option in _SUDO_RUNS_NOTHING:
                    return None, assignments, True, None, replace
                takes_next = _wrapper_option_takes_next_word(option, letters, long_options)
                following = words[i + 1] if i + 1 < len(words) else None
                split = _env_split_string(option, following) if name == "env" else None
                if split is not None:
                    try:
                        split_words = _linear_split(split)
                    except ValueError:
                        split_words = split.split()
                    end = i + 1 + takes_next
                    words[i:end] = split_words
                    if resplit is not None:
                        changed = _resplit_from(split_words)
                        if resplit > end:
                            resplit += len(split_words) - (end - i)
                        else:
                            # the words past the split ones are kept; a word before them
                            # is read already
                            resplit = i + changed if changed else min(resplit, i)
                    continue
                i += 2 if takes_next else 1
                if option == "--":
                    break
            i += operands
            wrapped = True
            continue
        wrapper = _EXEC_WRAPPERS.get(name) or _READER_WRAPPERS.get(name)
        if wrapper is not None:
            letters, long_options, operands = wrapper
            start = i
            i = _past_options(words, i + 1, letters, long_options) + operands
            if name == "xargs":
                replace = _xargs_replace(words[start:i])
            wrapped = True
            continue
        break
    return words[i:], assignments, wrapped, None, replace


class _RunReader:
    """Reads the runs of one text and the command lines nested in it."""

    def __init__(self, depth: int, remote: bool) -> None:
        self.runs: list[_Run] = []
        self.depth = depth
        self.remote = remote
        self.ids = itertools.count(1)
        self.base: tuple[int, ...] = ()
        # At the paren level being read: the `&&` or `||` the pipeline follows in
        # its and-or list (None at the list's start), the list's id, and for each
        # open if, loop, case or `{ }` group whether the shell may skip what runs
        # in it and the id of the list around it.
        self.joined: str | None = None
        self.list = 0
        self.compound: list[tuple[bool, int]] = []
        # Right after a `(` or `$(`: the `&&` or `||` the group follows in its
        # and-or list, which its first command follows too (_Run.after).
        self.entry: tuple[str | None, ...] = ()

    def open_compound(self, skippable: bool) -> None:
        """Enter an if, loop, case or `{ }` group: its body holds lists of its own."""
        self.compound.append((skippable, self.list))
        self.list = next(self.ids)

    def close_compound(self) -> None:
        if self.compound:
            _, self.list = self.compound.pop()

    def read_text(self, text: str, subshell: tuple[int, ...]) -> bool:
        """Read *text*: its tokens, then each substitution body they miss.
        False when *text* cannot be tokenized."""
        tokens = _command_tokens(_FD_NUMBER_RE.sub("", text))
        if tokens is None:
            return False
        first = len(self.runs)
        self.read_tokens(tokens, subshell)
        outer, cursor = len(self.runs), first
        for body in _missed_substitutions(text, True):
            # The command whose word holds the substitution runs its output.
            # Bodies come in text order, so the search goes on from the last
            # holder, _READER_HOLDER_WINDOW commands at most.
            parent = next(
                (
                    i
                    for i in range(cursor, min(outer, cursor + _READER_HOLDER_WINDOW))
                    if any(body in word for word in self.runs[i].argv)
                ),
                None,
            )
            cursor = cursor if parent is None else parent
            start = len(self.runs)
            if not self.read_text(body, (*subshell, next(self.ids))):
                self.runs.append(_Run((), remote=self.remote, unparsed=body))
            for i in range(start, len(self.runs)):
                if self.runs[i].parent is None:
                    self.runs[i] = dataclasses.replace(self.runs[i], parent=parent)
        return True

    def read_payload(
        self,
        payload: str,
        subshell: tuple[int, ...],
        piped: bool,
        upstream: int | None,
        conditional: bool,
        after: str | None,
        chain: int,
    ) -> None:
        """Read an `eval` payload as the command line it is (_guarded_texts);
        each of its commands is conditional when the eval is, and its first
        follows the `&&` or `||` the eval does, in the eval's list."""
        if self.depth >= _MAX_EVAL_DEPTH:
            self.runs.append(_Run((), remote=self.remote, unparsed=payload))
            return
        first = len(self.runs)
        for text, _, remote in _guarded_texts(payload):
            inner = _RunReader(self.depth + 1, self.remote or remote)
            inner.ids = self.ids
            offset = len(self.runs)
            if not inner.read_text(text, subshell):
                self.runs.append(_Run((), remote=inner.remote, unparsed=text))
                continue
            for run in inner.runs:
                run = _offset_run(run, offset, piped, upstream, conditional)
                self.runs.append(
                    dataclasses.replace(run, after=after, chain=chain)
                    if len(self.runs) == first
                    else run
                )

    def read_tokens(self, tokens: list[str], subshell: tuple[int, ...]) -> None:
        words: list[str] = []
        piped, upstream, previous = False, None, None
        children: list[int] = []
        self.joined, self.compound, self.entry, self.list = None, [], (), next(self.ids)
        # Each open `(`: ("$", the command around a substitution and its state)
        # or ("(", the subshell outside a group), then the and-or and compound
        # state outside it.
        stack: list[tuple] = []
        # Inside a `case`: how many are open, and whether the tokens are an
        # arm's patterns (`x|y)`, `(x)`), which run nothing.
        cases, pattern = 0, False
        for token in [*tokens, None]:
            if pattern and token is not None:
                if token == "esac":
                    cases, pattern = cases - 1, False
                    self.close_compound()
                elif token == ")":
                    pattern = False
                continue
            if token == "esac" and cases and not words:
                cases -= 1
                self.close_compound()
                continue
            if token is not None and token not in _READER_LIST_TOKENS:
                words.append(token)
                if (
                    token == "in"
                    and len(words) >= 3
                    and words[-3] == "case"
                    and set(words[:-3]) <= _READER_SKIPPED
                ):
                    cases, pattern = cases + 1, True
                    self.open_compound(True)
                    del words[-3:]
                continue
            if token == ";;" and cases:
                pattern = True
            if token == "(" and words and (words[-1].endswith("$") or words[-1] in ("<", ">")):
                stack.append(
                    (
                        "$",
                        words,
                        piped,
                        upstream,
                        previous,
                        subshell,
                        children,
                        len(self.runs),
                        self.joined,
                        self.compound,
                        self.list,
                    )
                )
                words, piped, upstream, previous, children = [], False, None, None, []
                self.entry = self.entry or (self.joined,)
                self.joined, self.compound = None, []
                subshell = (*subshell, next(self.ids))
                continue
            if token == ")" and stack and stack[-1][0] == "$":
                self.finish(words, piped, upstream, previous, subshell, children, token)
                (
                    _,
                    words,
                    piped,
                    upstream,
                    previous,
                    subshell,
                    children,
                    first,
                    self.joined,
                    self.compound,
                    self.list,
                ) = stack.pop()
                self.entry = ()
                children.extend(range(first, len(self.runs)))
                if words[-1] in ("<", ">"):
                    words[-1] += "()"
                continue
            self.finish(words, piped, upstream, previous, subshell, children, token)
            words, children = [], []
            if token == "(":
                stack.append(("(", subshell, self.joined, self.compound, self.list))
                self.entry = self.entry or (self.joined,)
                self.joined, self.compound = None, []
                subshell = (*subshell, next(self.ids))
            elif token == ")":
                if stack:
                    _, subshell, self.joined, self.compound, self.list = stack.pop()
                self.entry = ()
                piped, upstream = False, None
            else:
                piped = token in _PIPE_TOKENS
                upstream = len(self.runs) - 1 if piped and self.runs else None
                previous = token
                if token in ("&&", "||"):
                    self.joined = token
                elif not piped:
                    self.joined, self.list = None, next(self.ids)

    def finish(
        self,
        words: list[str],
        piped: bool,
        upstream: int | None,
        previous: str | None,
        subshell: tuple[int, ...],
        children: list[int],
        ends: str | None,
    ) -> None:
        if not words:
            return
        if previous == "&" and words[0].startswith("!") and words[0] != "!":
            words = [words[0][1:], *words[1:]]
        # An if or loop condition, or a loop's word list, runs before its body; a
        # `{ }` group may be skipped when its first command may.
        lead = list(itertools.takewhile(_READER_SKIPPED.__contains__, words))
        conditional = self.joined is not None or bool(self.compound and self.compound[-1][0])
        after, self.entry = self.entry[0] if self.entry else self.joined, ()
        for word in lead:
            if word in ("if", "while", "until"):
                self.open_compound(True)
            elif word == "{":
                self.open_compound(conditional)
            elif word == "}":
                self.close_compound()
        first = words[len(lead)] if len(lead) < len(words) else None
        if first in ("for", "select"):
            self.open_compound(True)
        elif first in _READER_CLOSERS:
            self.close_compound()
        chain = self.list
        argv, assignments, wrapped, payload, replace = _run_words(words)
        if payload is not None:
            self.read_payload(payload, subshell, piped, upstream, conditional, after, chain)
            return
        if not argv and not assignments:
            return
        variants = [argv or []]
        if replace is not None and argv and piped and upstream is not None:
            fed = _echoed_texts(list(self.runs[upstream].argv))
            if replace:
                variants = [
                    [word.replace(replace, line) for word in argv]
                    for text in fed
                    for line in text.splitlines()
                ] or variants
            else:
                variants = [[*argv, *text.split()] for text in fed] or variants
        index = len(self.runs)
        for variant in dict.fromkeys(tuple(v) for v in variants):
            self.runs.append(
                _Run(
                    variant,
                    tuple(assignments),
                    wrapped,
                    piped,
                    ends,
                    upstream,
                    None,
                    subshell,
                    replace is not None,
                    self.remote,
                    conditional=conditional,
                    after=after,
                    chain=chain,
                )
            )
        for child in children:
            if self.runs[child].parent is None:
                self.runs[child] = dataclasses.replace(self.runs[child], parent=index)


def _offset_run(
    run: _Run, offset: int, piped: bool, upstream: int | None, conditional: bool
) -> _Run:
    """*run*, read on its own, placed at *offset* among the runs of the text
    whose `eval` held it; it takes the eval's stdin, as every command of the
    payload does (one piped inside the payload keeps its own upstream), and it
    is conditional when the eval is."""
    return dataclasses.replace(
        run,
        upstream=run.upstream + offset
        if run.upstream is not None
        else (upstream if piped else None),
        parent=run.parent + offset if run.parent is not None else None,
        piped=run.piped or piped,
        conditional=run.conditional or conditional,
    )


@functools.lru_cache(maxsize=128)
def _cached_runs(text: str, remote: bool) -> tuple[_Run, ...] | None:
    reader = _RunReader(0, remote)
    if not reader.read_text(text, ()):
        return None
    return tuple(reader.runs)


def _read_runs(text: str, remote: bool = False) -> list[_Run] | None:
    """Each simple command *text* (a text of _guarded_texts) runs, in order, as
    the shells read it (see the reader's comment above); None when *text*
    cannot be tokenized. *remote*: the text runs on an ssh destination."""
    runs = _cached_runs(text, remote)
    return None if runs is None else list(runs)


def _all_runs(command: str) -> Iterator[_Run]:
    """Each simple command of *command*: each command _read_runs finds in each text
    of _guarded_texts, so quotes are removed and the command an ssh destination runs
    and a nested shell's payload are read. A text _read_runs cannot tokenize holds none."""
    for text, _, remote in _guarded_texts(command):
        yield from _read_runs(text, remote) or []


def _any_run(command: str, matches: Callable[[_Run], bool]) -> bool:
    """True when a simple command of *command* (_all_runs) satisfies *matches*. A word a
    search or a print takes is a word of its command too: *matches* checks the program
    for a pattern that names none."""
    return any(matches(run) for run in _all_runs(command))


# Commands that run the command in their operands, beyond the _COMMAND_WRAPPERS of shell_tokens:
# the options of theirs that take a value (short letters, glued or the next word; whole words,
# single-dash ones included), then how many operands come before the command. BSD (macOS) and GNU
# options, g-prefixed Homebrew coreutils names too.
_EXEC_WRAPPERS: dict[str, tuple[str, set[str], int]] = {
    "xargs": ("EIJLPRSadns", set(), 0),
    "nice": ("n", {"--adjustment"}, 0),
    "gnice": ("n", {"--adjustment"}, 0),
    "caffeinate": ("tw", set(), 0),
    "stdbuf": ("ioe", {"--input", "--output", "--error"}, 0),
    "gstdbuf": ("ioe", {"--input", "--output", "--error"}, 0),
    "arch": ("de", {"-arch"}, 0),
}


def _past_options(words: list[str], start: int, letters: str, long_options: set[str]) -> int:
    """The index of the first word of *words* from *start* on that is not an
    option of a command whose options taking a value are *letters* (short, the
    value glued or the next word) and *long_options* (whole words); `--` ends
    the options and a lone `-` is an operand."""
    i = start
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        option = words[i]
        i += 1
        if option == "--":
            break
        if option in long_options or _wrapper_option_takes_next_word(option, letters, long_options):
            i += 1
    return i


# How many `eval` levels deep a command is read. Past it the payload is left unparsed
# (_Run.unparsed).
_MAX_EVAL_DEPTH = 5


# What starts a heredoc message: the `$(` of a cat that writes its stdin as read (_STDIN_CAT)
# before a `<<`, a blank between them or none, which _heredocs reads the delimiter of
_MESSAGE_CAT_RE = re.compile(rf"\$\({_STDIN_CAT}\s*(?=<<)")
# The start of an option value that is a cat's substitution, path-qualified or not: a message
# body when that cat writes its stdin as read (_MESSAGE_CAT_RE), other text when not (`cat -n`)
_CAT_SUBSTITUTION_START_RE = re.compile(rf"\$\({_STDIN_CAT}")
# What ends a heredoc message's opening line after its delimiter: its newline, or the `#` of a
# comment before it
_OPENER_LINE_END_RE = re.compile(r"[ \t]*(?=[\n#])")

# What can end a simple command before its words do: a list operator, a newline or a
# parenthesis, not the `&` or `|` of a redirection (`2>&1`, `&>`, `>|`)
_COMMAND_END_RE = re.compile(r"[;\n()]|(?<![<>])[&|](?!>)")


def _without_data_heredocs(command: str) -> str:
    """*command* with the body of each data heredoc (_data_heredocs: a file being
    written, a fixture) blanked, its newlines and every position kept: a command
    named in one is text, not a command."""
    parts: list[str] = []
    last = 0
    for heredoc in _data_heredocs(command):
        if heredoc.body_start <= heredoc.end:
            parts.append(command[last : heredoc.body_start])
            parts.append(re.sub(r"[^\n]", " ", command[heredoc.body_start : heredoc.end]))
            last = heredoc.end
    parts.append(command[last:])
    return "".join(parts)


def _command_end(text: str, start: int, quoted: set[int]) -> int:
    """Where the simple command that starts at *start* in *text* ends: at the first
    unquoted list operator or newline outside a substitution (`$(...)`, `<(...)`),
    a parenthesis that opens a subshell, or one that closes the group the command
    is in; the end of *text* when none does. Redirections are part of the command.
    A comment (_comment_spans) holds none of these, `$(` included: the newline that
    ends it ends the command."""
    comments = iter(_comment_spans(text))
    comment = next(comments, None)
    depth = 0
    for end in _COMMAND_END_RE.finditer(text, start):
        at = end.start()
        while comment and comment[1] <= at:
            comment = next(comments, None)
        if at in quoted or (comment and comment[0] <= at):
            continue
        if text[at] == "(" and (depth or text[at - 1 : at] in ("$", "<", ">")):
            depth += 1
        elif text[at] == ")" and depth:
            depth -= 1
        elif not depth:
            return at
    return len(text)


@functools.lru_cache(maxsize=64)
def _heredocs_by_opener(text: str) -> dict[int, _Heredoc]:
    """Each heredoc of *text* (_heredocs) by where its `<<` is. Every commit asks
    for the same text's heredocs, so they are listed once."""
    return {heredoc.start: heredoc for heredoc in _heredocs(text)}


def _ends_opener_line(text: str, at: int) -> bool:
    """Whether the line of *text* that a heredoc opens on holds nothing the shell
    runs from *at*, the end of its delimiter word: blanks, then the newline or a
    comment (_comment_spans) that runs to it. Anything else (`| tr a b`, `>file`)
    changes the text the body is read as."""
    ends = _OPENER_LINE_END_RE.match(text, at)
    if ends is None:
        return False
    at = ends.end()
    if text.startswith("\n", at):
        return True
    comments = _comment_spans(text)
    found = bisect.bisect_left(comments, (at,))
    return (
        found < len(comments)
        and comments[found][0] == at
        and text.startswith("\n", comments[found][1])
    )


def _commit_heredocs(text: str, start: int, limit: int) -> Iterator[tuple[int, int, bool]]:
    """The span of the body of each heredoc message in the `git commit` command
    at *start*..*limit* in *text*, and whether its delimiter is quoted (the shell
    expands nothing in the body): each `$(cat <<TAG` the shell opens as a heredoc
    (_heredocs: one in a comment or in quotes opens none), whatever way the delimiter
    is spelled (`'EOF'`, `"EOF"`, `\\EOF`, `-EOF`, `EOF2`) and whether cat is named a
    stdin reader or path-qualified (`cat - <<TAG`, `/bin/cat <<TAG`: _MESSAGE_CAT_RE),
    alone on its line but for a comment
    (_ends_opener_line), within the commit's own command (_command_end, *limit*).
    One past it is the script of a later
    command (`git commit -m x && bash -c "$(cat <<'EOF'`), not a message. A body ends at
    the first line that closes the heredoc, as it does for the shell: the lines after
    it are commands of the substitution (`EOF`, then `terraform apply`)."""
    opened = _heredocs_by_opener(text)
    for cat in _MESSAGE_CAT_RE.finditer(text, start, limit):
        heredoc = opened.get(cat.end())
        if heredoc is None or not heredoc.closed:
            continue
        if _ends_opener_line(text, heredoc.word_end):
            body = heredoc.body_start
            yield body, max(body, heredoc.body_end), not _expands_body(text, heredoc)


def _without_message_bodies(text: str) -> str:
    """*text* with the body of each heredoc message that runs no code
    (_commit_heredocs: a quoted delimiter, or a body without `$(`, `${` or a
    backtick) blanked, its newlines and every position kept. The prose of a body
    holds quotes of its own (`"no` ... `work"`), which in the double quotes of its
    `$(cat <<EOF` end the quoted run too soon (_quoted_positions) and cut the
    commit's command short."""
    if "<<" not in text:
        return text
    parts: list[str] = []
    last = 0
    for first, stop, literal in _commit_heredocs(text, 0, len(text)):
        if literal or _LIVE_TEXT_RE.search(text, first, stop) is None:
            parts += [text[last:first], re.sub(r"[^\n]", " ", text[first:stop])]
            last = stop
    parts.append(text[last:])
    return "".join(parts)


def _literal_spans(text: str, first: int, last: int) -> list[tuple[int, int, bool, bool]]:
    """The spans of the shell word *text*[first:last] that stand for their own text
    in its value, as (start, end, escaped, bare): the text between single quotes,
    between double quotes, and the unquoted characters but a backslash (*bare*). A
    quote of the word and a `$'...'` string are not, so a cut inside a span leaves
    the quoting whole. A backslash in double quotes escapes the character after it,
    which a cut must not split from it: that text (*escaped*) is cut only as a whole.
    None are left when the word's parts (_SHELL_WORD_PART_RE) do not make up all of
    it, or an unquoted backslash escapes one."""
    spans: list[tuple[int, int, bool, bool]] = []
    pos = first
    for part in _SHELL_WORD_PART_RE.finditer(text, first, last):
        if part.start() != pos:
            return []
        pos = part.end()
        if part.group(4) is not None:
            if part.group(4) == "\\":
                return []
            if spans and spans[-1][1] == part.start() and spans[-1][3]:
                spans[-1] = (spans[-1][0], part.end(), False, True)
            else:
                spans.append((part.start(), part.end(), False, True))
        elif part.group(2) is not None:
            spans.append((*part.span(2), False, False))
        elif part.group(3) is not None:
            spans.append((*part.span(3), "\\" in part.group(3), False))
    return spans if pos == last else []


def _message_cuts(text: str, commit: _Commit) -> Iterator[tuple[int, int, bool]]:
    """The spans of *text* that strip_commit_message blanks for *commit*, a commit
    or another message command _message_commands read from it: each message of it
    that is text the shell takes it for, so that blanking it hides nothing the shell
    runs. The shell runs the command as its own (_Commit.direct); a message is the
    value of a text option word it reads there (_message_option_values: a commit's
    or tag's `--trailer` is one, though no paragraph of the message
    _Commit.message holds), which can run no code (_plain_message_word): the
    shell ends a word at a `;` or `&&` that shlex keeps in it (`-m x; terraform
    apply`), and runs a `$(...)` or backtick in it, or in the body of a heredoc whose
    opening line does not quote its delimiter (the lines after the closing one run).
    A command that follows is the shell's own to run, not the message's, so only the
    message text inside the message's own word or body is cut, never a copy of it
    elsewhere: `-m a && terraform apply` keeps its `a`s. Each span comes with
    whether it is unquoted text, which the blank must keep a word (_BLANKED_WORD)."""
    if not commit.direct or commit.source is not None:
        return
    values: list[tuple[str, list[tuple[int, int, bool, bool]]]] = []
    if commit.bodies:
        for first, last, literal in commit.bodies:
            if literal or _LIVE_TEXT_RE.search(text, first, last) is None:
                values.append((text[first:last].strip(), [(first, last, False, False)]))
    else:
        words = _message_words(text, commit.lead + len("gh" if commit.kind == "gh" else "git"))
        texts = [_shell_word_text(text[first:last])[0] for first, last in words]
        found = _message_option_values(commit.kind, texts, text=True)
        joined = "\n\n".join(value for _, value in _message_option_values(commit.kind, texts))
        if (joined or None) == commit.message:
            for idx, value in found:
                first, last = words[idx]
                if _plain_message_word(text, first, last):
                    values.append((value, _literal_spans(text, first, last)))
    for value, spans in values:
        # the value ends its word (`-mm`: the last `m`), so the last span that holds it
        for first, last, escaped, bare in reversed(spans) if value else ():
            if escaped:
                if _shell_word_text(f'"{text[first:last]}"')[0] == value:
                    yield first, last, False
                    break
                continue
            at = text.rfind(value, first, last)
            if at != -1:
                yield at, at + len(value), bare
                break


# What an unquoted message is blanked to: a word, so that the words after it keep
# their places (`git stash push -m x topic`: `topic` stays the pathspec), and one no
# guard reads as anything.
_BLANKED_WORD = "_"


@functools.lru_cache(maxsize=64)
def strip_commit_message(command: str) -> str:
    """Blank out the messages of a command before pattern scanning: those of a
    git commit, merge, tag, notes or stash (a commit's or tag's trailers too), and
    the free-text options of a gh command (_GH_TEXT_OPTIONS).

    A pattern scan reads the raw command. A message can legitimately *mention* a flagged pattern —
    e.g. `git commit -m "fix: block git branch -D"` — without executing anything, so the message
    text is replaced with a space in its quotes, or with a placeholder word where it is unquoted
    (_BLANKED_WORD: the words after it keep their places), before those scans. A message is blanked
    wherever _message_commands reads a command the shell runs as its own: behind assignments or by
    path, each message option of it, and each command of a list, and so is each message of a nested
    shell payload the command quotes whole (_without_payload_messages: `bash -c '...'` or `bash -c
    "...\\"...\\""`, an ssh remote command, `echo '...' | bash`, `printf '%s\\n' '...' | bash`), in
    that payload's own word only. Content outside the messages (commands chained after the commit,
    or a `$()`) is preserved verbatim and stays fully guarded. Degrades fail-closed: if the message
    can't be isolated, or the shell does not take it for inert text of a command it runs
    (_message_cuts: an `eval`'s joined words, a wrapper, `-m x; cmd`, a `$(...)` in it), the
    original command is returned (over-block, never under-block). Blanked once per command, since
    several checks ask for the same command's text.
    """
    if not _MESSAGE_LEAD_RE.search(command):
        return command
    cuts = sorted(
        cut for commit in _message_commands(command) for cut in _message_cuts(command, commit)
    )
    parts: list[str] = []
    last = 0
    for first, stop, bare in cuts:
        parts += [command[last:first], _BLANKED_WORD if bare else " "]
        last = stop
    parts.append(command[last:])
    return _without_payload_messages("".join(parts))


# A quoted word of a text as the shell writes one: in single quotes, or in double
# quotes with their escapes; an escaped character outside quotes is passed over.
_QUOTED_WORD_RE = re.compile(r"""'[^']*'|"(?:[^"\\]|\\[\s\S])*"|\\[\s\S]""")
# What a backslash escapes in double quotes.
_DOUBLE_QUOTE_ESCAPED_RE = re.compile(r'([\\"$`])')


def _quoted_spellings(written: str, blank: str) -> list[tuple[str, str]]:
    """The quoted words the shell reads as *written*, each with the same spelling of
    *blank*: in single quotes, and in double quotes with each `\\`, `"`, `$` and
    backtick escaped."""
    word, blanked = (
        '"' + _DOUBLE_QUOTE_ESCAPED_RE.sub(r"\\\1", value) + '"' for value in (written, blank)
    )
    single = [(f"'{written}'", f"'{blank}'")] if "'" not in written and "'" not in blank else []
    return [*single, (word, blanked)]


def _without_payload_messages(text: str) -> str:
    """*text* with the messages of each nested shell payload it runs one level
    down (_payloads_in, the command an ssh destination runs included) blanked by
    strip_commit_message, where the payload is a quoted word of *text* that the
    shell reads as it (_quoted_spellings: single quotes, or double quotes with
    escapes; the word an `echo` or `printf` writes with a newline after it). A word
    is replaced only where *text* holds it once, and the replacement stands only if
    reading the payloads again finds each blanked payload in place of its own and
    the rest unchanged: a word that only equals a payload (`ssh host git commit -m
    x; cat 'git commit -m x'`, where ssh joins unquoted words) leaves every payload
    as it was, and *text* comes back whole (over-block). The words are found in one
    pass and the payloads read twice, so a text is read in linear time. A git or gh
    in a payload sits after a quote."""
    quote = min((at for at in (text.find("'"), text.find('"')) if at != -1), default=-1)
    if quote == -1 or not _MESSAGE_LEAD_RE.search(text, quote):
        return text
    payloads = [written for _, _, written in _cached_nested_payloads(text, True, 1)]
    words: dict[str, list[tuple[int, int]]] = {}
    for word in _QUOTED_WORD_RE.finditer(text):
        words.setdefault(word.group(), []).append(word.span())
    edits: dict[tuple[int, int], str] = {}
    blanked: dict[str, str] = {}
    for payload in payloads:
        stripped = strip_commit_message(payload)
        if stripped == payload:
            continue
        forms = [(payload, stripped)]
        if payload.endswith("\n") and stripped.endswith("\n"):
            forms.append((payload[:-1], stripped[:-1]))
        spellings = [pair for written, blank in forms for pair in _quoted_spellings(written, blank)]
        for word, replacement in spellings:
            spans = words.get(word, [])
            if len(spans) == 1 and spans[0] not in edits:
                edits[spans[0]] = replacement
                blanked[payload] = stripped
                break
    if not edits:
        return text
    parts: list[str] = []
    last = 0
    for (first, stop), replacement in sorted(edits.items()):
        parts += [text[last:first], replacement]
        last = stop
    parts.append(text[last:])
    result = "".join(parts)
    # payloads blanked alike are listed once, as the reader lists them
    expected = list(dict.fromkeys(blanked.get(payload, payload) for payload in payloads))
    again = [written for _, _, written in _cached_nested_payloads(result, True, 1)]
    return result if again == expected else text


# What a message command can stand behind: `git` or `gh` itself (a path to it ends
# in `/git`, and a backslash keeps an alias from running: `\git`), or `eval`, whose
# words, quoted or not, are a command line of their own. A backtick opens a command too.
_COMMIT_LEAD_RE = re.compile(r"(?<![^\s;&|()/\\`])(?:git|gh|eval)(?=[ \t])")
# What stands in a commit's words for the body of a heredoc message, with the body's
# number: the words are read with the bodies cut out, since a quote in one ends nothing.
_BODY_MARK_RE = re.compile("\x00(\\d+)\x00")


@dataclass(frozen=True)
class _Commit:
    """One `git commit` a text runs, or another git or gh command that stores a
    message (_message_option_values), as _message_commands reads it."""

    start: int  # where its simple command starts
    lead: int  # where its `git` or `gh` word is, or the `eval` whose words it is
    direct: (
        bool  # that word starts a command, `(` and assignments before it at most (_starts_command)
    )
    bodies: tuple[
        tuple[int, int, bool], ...
    ]  # its heredoc texts: a text option's value (_commit_heredocs)
    source: str | None  # where `-F` reads its message from (_commit_file_source)
    message: str | None  # its message values, one paragraph each, joined; None without
    kind: str  # the git subcommand (commit, merge, tag, notes, stash), or "gh"


def _commit_argv(tokens: list[str]) -> list[str] | None:
    """What the command words *tokens* run (_run_words), the words of an `eval`
    read as the command line the shell makes of them: `eval git commit -m "a b"`
    commits `a`. Words the re-split gives back whole are read once, past every
    `eval` before them (_run_words), so a chain of evals is read in linear time."""
    argv, _, _, payload, _ = _run_words(tokens, through_eval=True)
    while payload is not None:
        try:
            argv, _, _, payload, _ = _run_words(_linear_split(payload), through_eval=True)
        except ValueError:
            return None
    return argv


def _text_values(
    options: list[str], letters: str, longs: tuple[str, ...], flags: str = ""
) -> list[tuple[int, str]]:
    """The text options (_text_options) in *options*, as (the index of the word
    that holds the value, the value), in order."""
    values: list[tuple[int, str]] = []
    for pos, glued in _text_options(options, letters, longs, flags):
        if glued is not None:
            values.append((pos, glued))
        elif pos + 1 < len(options):
            values.append((pos + 1, options[pos + 1]))
    return values


def _message_values(options: list[str]) -> list[tuple[int, str]]:
    """The `-m` / `--message` options in *options*, the words after `commit`, as
    (the index of the word that holds the value, the value), in order."""
    return _text_values(options, _GIT_MESSAGE_LETTERS, _GIT_MESSAGE_LONGS, _GIT_MESSAGE_FLAGS)


def _message_option_values(kind: str, args: list[str], text: bool = False) -> list[tuple[int, str]]:
    """The message values of the git or gh command of *kind* (_Commit.kind) whose
    words after the program are *args*, as (the index in *args* of the word that
    holds the value, the value), in order: the `-m` / `--message` of a git message
    subcommand, the free-text options of a gh command (the options
    _message_word_indexes names). With *text*, the other text a git command
    stores too (_git_text_longs: a commit's or tag's `--trailer`), which is no
    paragraph of the message."""
    if kind == "gh":
        start, letters, longs = _gh_text_spec(args)
        return [(start + pos, value) for pos, value in _text_values(args[start:], letters, longs)]
    sub = _git_globals(args).subcommand
    if args[sub : sub + 1] != [kind]:
        return []
    if text:
        found = _text_values(
            args[sub + 1 :], _GIT_MESSAGE_LETTERS, _git_text_longs(kind), _GIT_MESSAGE_FLAGS
        )
    else:
        found = _message_values(args[sub + 1 :])
    return [(sub + 1 + pos, value) for pos, value in found]


def _commit_message(options: list[str], bodies: list[str]) -> str | None:
    """The values of the `-m` / `--message` options in *options*, the words after
    `commit` (_message_values), one paragraph each, in order, joined as git joins
    them: a value that is a heredoc message (_BODY_MARK_RE) is the body, in its
    place. The paragraphs stop at a value that reads another substitution (the
    prefix is the start of the message); None when there are none."""
    paragraphs: list[str] = []
    for _, value in _message_values(options):
        if _CAT_SUBSTITUTION_START_RE.match(value):
            mark = _BODY_MARK_RE.search(value)
            if mark is None:
                break
            value = bodies[int(mark[1])]
        paragraphs.append(value)
    return "\n\n".join(paragraphs) if paragraphs else None


def _message_commands(text: str) -> list[_Commit]:
    """Each git message subcommand (commit, merge, tag, notes, stash) and each gh
    command *text* runs, in order. The `git` or `gh` is the command of its
    simple command (which starts after the last command separator before it:
    _is_command_separator), behind the assignments, groups, wrappers and `eval`
    that _run_words skips and git's own options (_git_globals): an
    argument of another command (`echo git commit`, `ssh host git commit`), a
    word in quotes or a comment, and one in the body of a data heredoc
    (_without_data_heredocs) run no command. Its words are those of its own
    command (_command_end), with each heredoc message cut out (_commit_heredocs),
    so the `-m` of a command that follows is no message of it. Each simple
    command is read once, so a long list is read in linear time. Where its
    command ends is read with the prose of the heredoc messages blanked
    (_without_message_bodies). The `$(...)` and backtick bodies of a double-quoted
    string run (`echo "$(git commit -m x)"`), as do the unquoted ones in the words
    of a git or gh command (`gh pr view $(git commit -m x)`): each is read as a
    text of its own (_substitution_bodies), so its own quotes hold what they hold. Every
    guard asks for the same text's commands (strip_commit_message), so each text
    is read once; the words of an `eval` are read again only where the shell's
    re-split of them changes them (_commit_argv)."""
    return list(_cached_message_commands(text))


@functools.lru_cache(maxsize=64)
def _cached_message_commands(text: str) -> tuple[_Commit, ...]:
    masked = _without_message_bodies(text)
    commits = {commit.lead: commit for commit in _read_message_commands(text, masked)}
    for body, starts in _substitution_bodies(text, masked):

        def at(pos: int, starts: list[tuple[int, int]] = starts) -> int:
            body_at, text_at = starts[bisect.bisect_right(starts, (pos, len(text))) - 1]
            return text_at + pos - body_at

        for commit in _read_message_commands(body, _without_message_bodies(body)):
            commits.setdefault(
                at(commit.lead),
                dataclasses.replace(
                    commit,
                    start=at(commit.start),
                    lead=at(commit.lead),
                    bodies=tuple(
                        (at(a), at(b - 1) + 1, literal) for a, b, literal in commit.bodies
                    ),
                ),
            )
    return tuple(commits[lead] for lead in sorted(commits))


def _substitution_bodies(text: str, masked: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """The body of each `$(...)`, backtick and process substitution of *text*, at any
    depth, that holds a git, gh or eval word of its own: the shell runs it. One
    that sits directly in a double-quoted string (_double_quoted_runs, read on
    *masked*, *text* with its heredoc messages blanked: the text between two runs
    of the string) holds quotes of its own; an unquoted one may sit in the words of
    a git or gh command, which _read_message_commands reads as one command.
    Each comes with where its pieces start, as (in the body, in *text*): the body
    of a substitution nested in it that holds one is cut out (`$()` stays), since
    it is read as a body of its own, so every character is read once."""
    gaps: list[tuple[int, int]] = []
    for runs in _double_quoted_runs(masked, gaps).values():
        for (_, gap), (end, _) in itertools.pairwise(runs):
            opener = 2 if masked.startswith("$(", gap) else 1
            if end - gap > opener and masked[end - 1] in ")`":
                gaps.append((gap + opener, end - 1))
    gaps = [(first, last) for first, last in gaps if last > first]
    if not gaps:
        return []
    gaps.sort(key=lambda gap: (gap[0], -gap[1]))
    # the innermost substitution around each word, and the one around each substitution
    leads = iter(lead.start() for lead in _COMMIT_LEAD_RE.finditer(_without_data_heredocs(masked)))
    lead = next(leads, None)
    owns = [False] * len(gaps)
    parent = [-1] * len(gaps)
    stack: list[int] = []
    for idx, (first, _) in enumerate([*gaps, (len(masked), len(masked))]):
        while lead is not None and lead < first:
            while stack and gaps[stack[-1]][1] <= lead:
                stack.pop()
            if stack:
                owns[stack[-1]] = True
            lead = next(leads, None)
        while stack and gaps[stack[-1]][1] <= first:
            stack.pop()
        if idx < len(gaps):
            parent[idx] = stack[-1] if stack else -1
            stack.append(idx)
    # a substitution with a word of its own anywhere inside it is cut from the one around it
    holds = owns[:]
    for idx in range(len(gaps) - 1, -1, -1):
        if holds[idx] and parent[idx] != -1:
            holds[parent[idx]] = True
    cut: dict[int, list[tuple[int, int]]] = {}
    for idx, up in enumerate(parent):
        if holds[idx] and up != -1:
            cut.setdefault(up, []).append(gaps[idx])
    bodies: list[tuple[str, list[tuple[int, int]]]] = []
    for idx, (first, last) in enumerate(gaps):
        if not owns[idx]:
            continue
        pieces: list[str] = []
        starts: list[tuple[int, int]] = []
        size = 0
        for piece_start, piece_end in zip(
            [first, *(end for _, end in cut.get(idx, []))],
            [*(start for start, _ in cut.get(idx, [])), last],
            strict=True,
        ):
            starts.append((size, piece_start))
            pieces.append(text[piece_start:piece_end])
            size += piece_end - piece_start
        bodies.append(("".join(pieces), starts))
    return bodies


def _read_message_commands(text: str, masked: str) -> list[_Commit]:
    """The message commands of *text* that _message_commands reads outside its
    double-quoted strings, *masked* being *text* with its heredoc messages blanked
    (_without_message_bodies)."""
    quoted = _quoted_positions(masked)
    comments = iter(_comment_spans(masked))
    comment = next(comments, None)
    commits: list[_Commit] = []
    done = 0
    for lead in _COMMIT_LEAD_RE.finditer(_without_data_heredocs(masked)):
        at = lead.start()
        while comment and comment[1] <= at:
            comment = next(comments, None)
        if at < done or at in quoted or (comment and comment[0] <= at):
            continue
        start = done
        for sep in _COMMAND_END_RE.finditer(masked, done, at):
            if masked[sep.start()] in _COMMAND_SEPARATORS and _is_command_separator(
                masked, sep.start(), quoted
            ):
                start = sep.end()
        end = _command_end(masked, start, quoted)
        done = max(end, at) + 1
        if end <= at:
            continue
        spans = (
            list(_commit_heredocs(text, start, end)) if text.find("<<", start, end) != -1 else []
        )
        bodies: list[str] = []
        parts: list[str] = []
        pos = start
        for first, last, _ in spans:
            parts += [text[pos:first], f"\x00{len(bodies)}\x00"]
            bodies.append(text[first:last].strip())
            pos = max(pos, last)
        parts.append(text[pos:end])
        line = "".join(parts)
        try:
            argv = _commit_argv(_linear_split(_strip_shell_comments(line)))
        except ValueError:
            continue
        program = Path(argv[0]).name if argv else ""
        words = argv[1:] if argv else []
        if program == "git":
            sub = _git_globals(words).subcommand
            kind = words[sub] if sub < len(words) else ""
            if kind not in _GIT_MESSAGE_SUBCOMMANDS:
                continue
            message = _commit_message(words[sub + 1 :], bodies)
        elif program == "gh":
            kind = "gh"
            message = "\n\n".join(value for _, value in _message_option_values(kind, words)) or None
        else:
            continue
        # a heredoc that is the value of another option (`--label "$(cat <<'EOF'`) is no
        # message or trailer
        messages = {
            int(mark[1])
            for _, value in _message_option_values(kind, words, text=True)
            for mark in _BODY_MARK_RE.finditer(value)
        }
        direct = text.startswith(program, at) and _starts_command(masked, at, quoted)
        commits.append(
            _Commit(
                start,
                at,
                direct,
                tuple(span for idx, span in enumerate(spans) if idx in messages),
                _commit_file_source(line),
                message,
                kind,
            )
        )
    return commits


# Heredoc payloads (<<'TAG' ... TAG). Their content is data fed to stdin (PR bodies, commit
# messages), not command tokens — but it sits unquoted in the command string, so it must be stripped
# before tokenizing, or prose in a heredoc reads as a command. The opener and its body are found by
# the one shell reader (shell_heredocs): a `<<` inside quotes or a comment opens nothing, so the
# commands after it stay visible.


def _strip_heredoc_bodies(text: str) -> str:
    """Remove heredoc payloads, keeping a `<<TAG` marker in their place and the
    rest of the line that opens each (_cut_heredoc_bodies): the shell runs that
    rest (`cat <<EOF && terraform apply`)."""
    return _cut_heredoc_bodies(text)[0]


def _join_line_continuations(text: str) -> str:
    """Delete the backslash-newline continuations the shell deletes outside
    heredoc bodies (shell_continuations: not inside single quotes, `$'...'` or
    a comment, where the shell keeps them). Line-based checks (segment splits
    on newline, `\\s+` patterns) otherwise read one continued command as two.
    Heredoc bodies are skipped: joining a body line into its terminator would
    hide the terminator, and the heredoc would then swallow the rest of the
    command as payload; so is the rest of each opener line. The continuations
    come from one reading of the whole text, so a quote open at a heredoc
    (`"$(cat <<EOF`) is still open after its body. A nested payload keeps its
    own continuations for its own shell to join (_nested_payloads)."""
    if "<<" not in text:
        return _join_continuations(text, shell_continuations(text))
    heredocs = iter(_heredocs(text))
    heredoc = next(heredocs, None)
    kept: list[int] = []
    for backslash in shell_continuations(text, read_heredocs=True):
        while heredoc is not None and heredoc.end <= backslash:
            heredoc = next(heredocs, None)
        if heredoc is None or backslash < heredoc.start:
            kept.append(backslash)
    return _join_continuations(text, kept)


def _join_continuations(text: str, backslashes: list[int]) -> str:
    """*text* with the backslash-newline at each of *backslashes* deleted."""
    out: list[str] = []
    last = 0
    for backslash in backslashes:
        out.append(text[last:backslash])
        last = backslash + 2
    out.append(text[last:])
    return "".join(out)


# A local shell word (bash/sh/zsh, optionally path-qualified) as its own token.
_LOCAL_SHELL_RE = re.compile(r"(?:^|[\s;&|(])(?:\S*/)?(?:bash|sh|zsh)(?=\s|$)")
# A match of _LOCAL_SHELL_RE that runs to the end of the text (`bash`, `/bin/sh`,
# `x;zsh`): the name after a word break, or after a `/`, from which its `\S*`
# always reaches back to a space or the start.
_LOCAL_SHELL_END_RE = re.compile(r"(?:^|[\s;&|(/])(?:bash|sh|zsh)$")
# Text ending in `python [opts] -c`: the next quoted string is python it runs.
_PYTHON_PAYLOAD_TAIL_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?" + PYTHON_NAME + r"(?:\s+-\w+)*\s+-\w*c\w*\s*$"
)
# Text ending in `eval`: the next quoted string is a command line it runs.
_EVAL_PAYLOAD_TAIL_RE = re.compile(r"(?:^|[\s;&|(])eval\s*$")
# Text ending in `bash|sh|zsh` and the words after it in the same command
# (group 1); _shell_args_run_next_word decides whether they end in a `-c`
# that runs the next quoted string.
_SHELL_PAYLOAD_TAIL_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?(?:bash|sh|zsh)((?:\s+[^\s;&|()]+)*)\s*$"
)


def _next_quote_runner(prefix: str) -> str | None:
    """What runs the quoted string that follows *prefix*: "python" for a
    `python -c` payload, "shell" for a command line run by a local `bash|sh|zsh
    -c` (options and their values in between) or `eval`, None for a string no
    runner takes."""
    if _PYTHON_PAYLOAD_TAIL_RE.search(prefix):
        return "python"
    if _EVAL_PAYLOAD_TAIL_RE.search(prefix):
        return "shell"
    match = _SHELL_PAYLOAD_TAIL_RE.search(prefix)
    return "shell" if match is not None and _shell_args_run_next_word(match.group(1)) else None


# The characters a _next_quote_runner match never spans after its runner's name.
_QUOTE_RUN_BREAKS = ";&|()"
# A word that may name the runner _next_quote_runner looks for (a shell, python or
# eval), at its end; the version and free-threaded suffix of `python3.14t` are
# cut first.
_QUOTE_RUNNER_WORD_RE = re.compile(r"(?:^|[/;&|(])(?:(?:ba|z)?sh|python|eval)$")
_PYTHON_SUFFIX_RE = re.compile(r"(?<=\d)t$")
# How much of the word being written _QuoteRunTail keeps.
_QUOTE_RUNNER_WINDOW = 64


class _QuoteRunTail:
    """The text _mask_quotes has written, kept so that _next_quote_runner reads
    only what it can match, in time linear in the text (a whole-prefix read per
    quote is quadratic in the text).

    _next_quote_runner matches a runner's name and the words after it to the end,
    and none of those words holds a `;&|()`, so it reads the text from the last such
    character on and finds what it finds in the whole text. A newline ends the
    runner's command too, unless a backslash continues the line (`bash \\` then
    `-c '...'`), so the read starts after the last such newline as well: a text
    masked in one pass (_mask_quoted_text) holds many lines. It finds nothing
    unless a word since then ends in a shell, python or eval name, so the read is
    skipped when none does."""

    def __init__(self) -> None:
        self.parts: list[str] = []  # the text from the last `;&|()` or newline on
        self.named = False  # a finished word since that character may name a runner
        self.word = ""  # the end of the word being written
        self.word_len = 0
        self.last = ""  # the last character written

    def add(self, piece: str) -> None:
        cut = max(piece.rfind(char) for char in _QUOTE_RUN_BREAKS)
        for at, char in enumerate(piece):
            continued, self.last = self.last == "\\", char
            if char.isspace():
                self.named = self.named or self._word_names_runner()
                self.word, self.word_len = "", 0
                if char == "\n" and not continued:
                    self.named, cut = False, max(cut, at)
                continue
            if char in _QUOTE_RUN_BREAKS:
                self.named = False
            self.word = (self.word + char)[-_QUOTE_RUNNER_WINDOW:]
            self.word_len += 1
        if cut >= 0:
            self.parts = [piece[cut:]]
        else:
            self.parts.append(piece)

    def _word_names_runner(self) -> bool:
        if not self.word_len:
            return False
        stem = _PYTHON_SUFFIX_RE.sub("", self.word).rstrip("0123456789.")
        if not stem:
            return True
        # Past the window the word's own start is unknown: no `^` anchor there.
        whole = self.word_len <= _QUOTE_RUNNER_WINDOW
        return bool(_QUOTE_RUNNER_WORD_RE.search(stem if whole else "\0" + stem))

    def next_quote_runner(self) -> str | None:
        """_next_quote_runner over all the text written so far."""
        if not (self.named or self._word_names_runner()):
            return None
        return _next_quote_runner("".join(self.parts))


def _mask_quotes(
    text: str, heredocs: Sequence[_Heredoc] = (), fed: frozenset[int] = frozenset()
) -> str:
    """
    Empty every quoted string, except one a local shell executes (`bash -c '...'`).

    The strings are the ones the shell reads, from one reading of the whole text
    (shell_quotes): a quote character inside a `$(...)` inside double quotes or
    in a comment (`# don't`) opens none, so no quote is left open past where the
    shell closes it.

    An executed payload has its quote characters turned into spaces, so patterns
    anchored on a command's end (`git stash$`) still match inside it. A `python
    -c` payload keeps its text as written: it is python. A shell's (`bash -c`,
    `eval`) is shell text, masked as its own command (_mask_quoted_text), so a
    quoted string in it is data unless a runner in it takes the string (`bash -c
    'bash -c "..."'`); each command substitution the shell expanding a
    double-quoted one runs follows on a line of its own. An ANSI-C `$'...'` or
    locale `$"..."` word is one quoted string without its `$`, a `$'...'`
    payload decoded. A comment (shell_comment_spans) is copied as it is.

    Given *heredocs* (_mask_quoted_text: those of *text*), the reading reads past
    their bodies, as the shell does. Each one whose `<<` starts in *fed* keeps
    its `<<TAG`, opener line and closing line as written, and its body is shell
    text, masked as its own command like a shell's payload, followed by a line
    for each command substitution the shell expanding it runs. Each other one
    leaves a `<<TAG` marker and the rest of its opener line, which the shell
    runs, and its body is cut to a line of its own for each command substitution
    the shell runs in it (_body_substitutions). Heredocs opened on one line
    (`cat <<A; bash <<B`) read their bodies in turn, each after the closing line
    of the one before; the rest of a fed one's opener line is kept as written,
    the other openers on it too. A heredoc opened inside a quoted string (`"$(cat
    <<EOF`) leaves its marker, or its fed text, inside the emptied string.
    Without them (a text whose bodies are already cut), no body is read past.
    """
    out: list[str] = []
    tail = _QuoteRunTail()
    comments = shell_comment_spans(text, read_heredocs=bool(heredocs))
    quotes = shell_quotes(text, bool(heredocs))

    def emit(piece: str) -> None:
        if piece:
            out.append(piece)
            tail.add(piece)

    def substitutions(heredoc: _Heredoc) -> str:
        return "".join(f"\n{_mask_quotes(inner)}" for inner in _body_substitutions(text, heredoc))

    def following(heredoc: _Heredoc) -> str:
        """What follows the opener line for *heredoc*: for one a shell runs, a
        newline, its body masked as that shell's own command text, the
        substitutions the shell expanding it runs, and its closing line; for any
        other, those substitutions."""
        if heredoc.start not in fed or heredoc.body_start > heredoc.end:
            return substitutions(heredoc)
        body_end = max(heredoc.body_start, heredoc.body_end)
        body = _mask_quoted_text(text[heredoc.body_start : body_end])
        return f"\n{body}{substitutions(heredoc)}{text[body_end : heredoc.end]}"

    def script(heredoc: _Heredoc) -> str:
        """A heredoc a shell runs: its `<<TAG` and opener line as written, then
        its body and closing line (following)."""
        line_end = text.find("\n", heredoc.word_end, heredoc.end)
        if line_end == -1:
            return text[heredoc.start : heredoc.end]
        return f"{text[heredoc.start : line_end]}{following(heredoc)}"

    def placed(heredoc: _Heredoc, opener_rest: str) -> str:
        if heredoc.start in fed:
            return script(heredoc)
        return f"<<{heredoc.tag}{opener_rest}{substitutions(heredoc)}"

    def runs(body: str, double_quoted: bool) -> str:
        """A payload a shell runs, masked as that shell's own command text, and
        the substitutions the shell expanding a double-quoted one runs."""
        if not double_quoted or ("$(" not in body and "`" not in body):
            return _mask_quoted_text(body)
        expanded = _missed_substitutions(body, False, heredoc=True)
        return _mask_quoted_text(body) + "".join(
            f"\n{_mask_quotes(inner)}" for inner in expanded if not inner.startswith("(")
        )

    def payload(start: int, end: int, inside: list[_Heredoc]) -> str:
        """The text from *start* to *end* as written, each heredoc *inside* it
        placed with the rest of its opener line, and one opened on the line of
        one before it by what follows that one's (following)."""
        parts: list[str] = []
        last = start
        for heredoc in inside:
            if heredoc.start < last:
                parts.append(following(heredoc))
            else:
                line_end = text.find("\n", heredoc.word_end, heredoc.end)
                rest = text[heredoc.word_end : heredoc.end if line_end == -1 else line_end]
                parts += [text[last : heredoc.start], placed(heredoc, rest)]
            last = max(last, min(heredoc.end, end))
        parts.append(text[last:end])
        return "".join(parts)

    n = len(text)
    i = next_comment = next_quote = next_heredoc = 0
    # the heredocs opened on the line read: their bodies start at the next newline
    # read, each after the one before
    pending: list[_Heredoc] = []
    while i < n:
        while next_comment < len(comments) and comments[next_comment][0] < i:
            next_comment += 1
        while next_quote < len(quotes) and quotes[next_quote][0] < i:
            next_quote += 1
        while next_heredoc < len(heredocs) and heredocs[next_heredoc].start < i:
            next_heredoc += 1
        if pending and pending[-1].end <= i:
            pending = []
        stop = min(
            comments[next_comment][0] if next_comment < len(comments) else n,
            quotes[next_quote][0] if next_quote < len(quotes) else n,
            heredocs[next_heredoc].start if next_heredoc < len(heredocs) else n,
        )
        newline = text.find("\n", i, stop) if pending else -1
        at = stop if newline == -1 else newline
        emit(text[i:at])
        i = at
        if i >= n:
            break
        if pending and i == newline:
            emit("".join(following(heredoc) for heredoc in pending))
            i = max(i, pending[-1].end)
            pending = []
        elif next_comment < len(comments) and comments[next_comment][0] == i:
            emit(text[i : comments[next_comment][1]])
            i = comments[next_comment][1]
        elif next_heredoc < len(heredocs) and heredocs[next_heredoc].start == i:
            heredoc = heredocs[next_heredoc]
            line_end = text.find("\n", heredoc.word_end, heredoc.end)
            if heredoc.start in fed and line_end == -1:
                emit(text[heredoc.start : heredoc.end])
                i = heredoc.end
            elif heredoc.start in fed:
                # the rest of its opener line as written, the heredocs opened on it too
                emit(text[heredoc.start : line_end])
                while next_heredoc < len(heredocs) and heredocs[next_heredoc].start < line_end:
                    pending.append(heredocs[next_heredoc])
                    next_heredoc += 1
                i = line_end
            else:
                emit(f"<<{heredoc.tag}")
                pending.append(heredoc)
                i = heredoc.word_end
        else:
            start, end, closed = quotes[next_quote]
            inside: list[_Heredoc] = []
            while next_heredoc < len(heredocs) and heredocs[next_heredoc].start < end:
                inside.append(heredocs[next_heredoc])
                next_heredoc += 1
            dollar = text[start] == "$"
            char = text[start + dollar]
            body_start, body_end = start + dollar + 1, end - 1 if closed else end
            runner = tail.next_quote_runner()
            if runner is not None:
                if dollar and char == "'":
                    body = _decode_ansi_c(text[body_start:body_end])
                    body = body if runner == "python" else runs(body, False)
                elif runner == "python":
                    body = payload(body_start, body_end, inside)
                else:
                    body = runs(text[body_start:body_end], char == '"')
                emit(f" {body} ")
            else:
                emit(
                    char
                    + "".join(placed(heredoc, "") for heredoc in inside)
                    + (char if closed else "")
                )
            i = end
    return "".join(out)


def _mask_quoted_text(command: str) -> str:
    """
    Blank the data in a command: quoted strings and heredoc bodies, read in one
    pass over the whole command (_mask_quotes).

    For a guard that asks "what does this command run?", a grep pattern, a sed script, or a file
    written through a heredoc is data, and matching inside it is a false positive. The rest of a
    heredoc's opener line runs, so it stays (`cat <<EOF > notes.md && gh workflow run deploy.yml`),
    its quotes emptied. Payloads that run locally stay visible: `bash|sh|zsh|python -c '...'`, `eval
    '...'`, a heredoc fed to bash/sh/zsh (a local script the reader finds: _heredoc_scripts, `cat
    <<EOF | bash`, `{ cat <<EOF; } | bash`, `cat > x.sh <<EOF && bash x.sh`, or one whose line names
    a shell before its `<<`, unless only a text reader takes it: _heredoc_text_starts, `sh -c 'cat >
    f' <<EOF`), and each command substitution of a body the shell expands (_body_substitutions), a
    line of its own after the `<<TAG` left for the body. A body or payload a shell runs is masked
    the same way, as its own command text: the quoted strings in it that no runner takes are data
    too.
    """
    heredocs = _heredocs(command)
    fed = {heredoc.start for heredoc, _, runner in _heredoc_scripts(command) if runner == "local"}
    text_only = _heredoc_text_starts(command) if "<<" in command else frozenset()
    # the opener line read last, and where its first shell name ends (_LOCAL_SHELL_RE)
    line, line_start, line_end, shell_end = "", 0, -1, None
    for heredoc in heredocs:
        if heredoc.start in text_only:
            continue
        if heredoc.start > line_end:
            line_start = command.rfind("\n", 0, heredoc.start) + 1
            line_end = command.find("\n", heredoc.start)
            if line_end == -1:
                line_end = len(command)
            line = command[line_start:line_end]
            match = _LOCAL_SHELL_RE.search(line)
            shell_end = match.end() if match else None
        # the line names a shell before this `<<`: its first, read once per line, or one
        # the `<<` follows with no space (`bash<<EOF`)
        before = heredoc.start - line_start
        if (shell_end is not None and shell_end < before) or _LOCAL_SHELL_END_RE.search(
            line, max(before - 5, 0), before
        ):
            fed.add(heredoc.start)
    return _mask_quotes(command, heredocs, frozenset(fed))


def _quote_end(text: str, i: int) -> int:
    """The index of the quote that closes the quoted word at *text*[*i*] (`'`,
    `"`, or the `$` of `$'` / `$"`; the end of *text* when none does). A
    `$(...)`, `$((...))`, `${...}`, `$[...]` or backtick body inside double
    quotes holds quotes of its own, as zsh reads them: a `'` quotes in a
    `$(...)` body, and in a `${...}` or `$[...]` opened there, but not in one
    opened in double quotes (`"${x:-'"'}"` leaves a `"` unmatched in zsh)."""
    ansi_c = text.startswith("$'", i)
    i += text[i] == "$"
    if text[i] == "'":
        j = i + 1
        while j < len(text) and text[j] != "'":
            j += 2 if ansi_c and text[j] == "\\" else 1
        return j
    # Each open body's closing character; a `${` or `$[` opened in double quotes
    # carries a `"` before it, as a `'` there is no quote.
    closers, j = ['"'], i + 1
    while j < len(text):
        char, closer = text[j], closers[-1]
        if closer == "'":
            if char == "'":
                closers.pop()
        elif char == "\\":
            j += 1
        elif char == closer[-1]:
            closers.pop()
            if not closers:
                return j
        elif closer == "`":
            pass
        elif char == "$" and text[j + 1 : j + 2] in ("(", "{", "["):
            end = {"(": ")", "{": "}", "[": "]"}[text[j + 1]]
            closers.append('"' + end if end != ")" and closer[0] == '"' else end)
            j += 1
        elif char == "`" or (char == '"' and closer != '"'):
            closers.append(char)
        elif char == "'" and closer in (")", "}", "]"):
            closers.append("'")
        elif char == "(" and closer == ")":
            closers.append(")")
        j += 1
    return j


def _eval_payloads(text: str) -> list[str]:
    """The command line each `eval` in *text* runs, as the command reader takes it (_run_words), one
    level down."""
    tokens = _command_tokens(_FD_NUMBER_RE.sub("", text)) or []
    payloads: list[str] = []
    words: list[str] = []
    for token in [*tokens, None]:
        if token is not None and token not in _READER_LIST_TOKENS:
            words.append(token)
            continue
        payload = _run_words(words)[3] if words else None
        if payload:
            payloads.append(payload)
        words = []
    return payloads
