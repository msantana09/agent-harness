"""
The shell tokens and command words of a Bash command line.

The one tokenizer of the shell readers (shell_runs, shell_heredoc_data) and any Bash guard built on
them. `_shell_tokens` splits a line into words and operators as the shell does (quotes, `$'...'`
words and comments read; an operator character that is quoted or escaped is a `_QuotedWord`, no
operator); `_command_tokens` splits it for a reader that takes each operator apart; `_command_words`
and `_without_redirections` cut one simple command to the words that name its program and operands;
`_shell_option_readings`, `_shell_c_payload_indexes` and `_shell_script_indexes` read what a shell's
options run, and `_ssh_remote_words` the command ssh runs on its destination. shell_runs reads the
same texts with these names.
"""

from __future__ import annotations

import functools
import os
import re
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from shell_comments import shell_comment_spans

# Shells whose `-c` option runs its next argument as a command line.
_NESTED_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh"}
# The commands that move the shell's directory, zsh's chdir (cd by another name) among them.
_DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd", "chdir"})


def _changes_like_cd(name: str, args: Sequence[str]) -> bool:
    """Whether the directory changer *name* given *args* moves the shell to the
    directory its operand names, or home without one, as cd does: chdir does,
    and so does a pushd given a directory. popd and a pushd given none (bare it
    swaps the top two stack entries; `+N` and `-N` rotate the stack) go where
    the stack says, which the words do not show; a pushd -n goes nowhere
    (_keeps_directory)."""
    if name == "pushd":
        return not _keeps_directory(name, args) and any(
            not word.startswith(("-", "+")) for word in args
        )
    return name in ("cd", "chdir")


def _keeps_directory(name: str, args: Sequence[str]) -> bool:
    """Whether the directory changer *name* given *args* leaves the shell where
    it is: a pushd with an `n` among the option letters before its operand,
    which bash reads as a change to the stack alone and zsh, which has no -n,
    as a directory or string that is not there, so the pushd fails."""
    if name != "pushd":
        return False
    for word in args:
        if word in ("-", "--") or not word.startswith("-"):
            return False
        if "n" in word[1:]:
            return True
    return False


# Commands that run the command after their own options (env: and its
# assignments), with the options of theirs that take a value: the short option
# letters, and the long options that take the next word unless given `=value`;
# then how many operands come before the command (timeout's duration). Each
# table is the union of the BSD (macOS) and GNU options. sudo's are those of
# sudo(8), plus Linux's SELinux `-r`/`-t`. env splits the value of its
# `-S`/`--split-string` into words of its own (_env_split_string).
_COMMAND_WRAPPERS: dict[str, tuple[str, set[str], int]] = {
    "env": ("uCPSa", {"--unset", "--chdir", "--split-string", "--argv0"}, 0),
    "sudo": (
        "CDghpRrTtUu",
        {
            "--chdir",
            "--chroot",
            "--close-from",
            "--command-timeout",
            "--group",
            "--host",
            "--other-user",
            "--prompt",
            "--role",
            "--type",
            "--user",
        },
        0,
    ),
    "nohup": ("", set(), 0),
    "exec": ("a", set(), 0),
    "command": ("", set(), 0),
    "time": ("fo", {"--format", "--output"}, 0),
    "timeout": ("ks", {"--kill-after", "--signal"}, 1),
    "gtimeout": ("ks", {"--kill-after", "--signal"}, 1),
}


def _wrapper_option_takes_next_word(option: str, letters: str, long_options: set[str]) -> bool:
    """True when the wrapper option word *option* takes the next word as its
    value: one of *long_options* without `=value`, or a short cluster whose
    first value letter (one of *letters*) is its last (`-Eu root`; in
    `-Euroot` the rest of the cluster is the value)."""
    if option.startswith("--"):
        return option in long_options
    for pos, letter in enumerate(option[1:], 1):
        if letter in letters:
            return pos == len(option) - 1
    return False


def _env_split_string(option: str, next_word: str | None) -> str | None:
    """The string env splits into words when its option word *option* is
    `-S` or `--split-string` (the value glued, after `=`, or *next_word*),
    or None for any other option."""
    if option.startswith("--"):
        name, equals, value = option.partition("=")
        if name != "--split-string":
            return None
        return value if equals else next_word
    letters = _COMMAND_WRAPPERS["env"][0]
    for pos, letter in enumerate(option[1:], 1):
        if letter in letters:
            return (option[pos + 1 :] or next_word) if letter == "S" else None
    return None


def _command_words(words: list[str]) -> list[str]:
    """*words* from the command word on, past env assignments and the
    _COMMAND_WRAPPERS with their options and operands. The words env splits
    from a `-S` string are read as if written in its place."""
    i = 0
    while i < len(words):
        name = Path(words[i]).name
        if ENV_ASSIGNMENT_RE.match(words[i]):
            i += 1
        elif name in _COMMAND_WRAPPERS:
            letters, long_options, operands = _COMMAND_WRAPPERS[name]
            i += 1
            while i < len(words) and words[i].startswith("-"):
                takes_next = _wrapper_option_takes_next_word(words[i], letters, long_options)
                next_word = words[i + 1] if i + 1 < len(words) else None
                split = _env_split_string(words[i], next_word) if name == "env" else None
                if split is not None:
                    try:
                        split_words = _linear_split(split)
                    except ValueError:
                        split_words = split.split()
                    words = [*words[:i], *split_words, *words[i + 1 + takes_next :]]
                    continue
                i += 2 if takes_next else 1
            i += operands
        else:
            break
    return words[i:]


def _without_redirections(words: list[str]) -> list[str]:
    """*words* without redirection operators (`<<`, `<<<`, `>`, `>&`) and their
    targets. Attached fd numbers are cut before tokenizing (_FD_NUMBER_RE). A
    process substitution target (`< <(echo x)`) runs locally and is cut through
    its closing `)`, or to the end of *words* when that falls in a later
    segment."""
    kept: list[str] = []
    skip = False
    depth = 0  # the parens open in a process substitution target
    for word in words:
        quoted = isinstance(word, _QuotedWord)
        if depth:
            if not quoted and word in ("(", "<(", ">("):
                depth += 1
            elif not quoted and word == ")":
                depth -= 1
        elif skip:
            skip = False
            depth = 1 if not quoted and word in ("<(", ">(") else 0
        elif _redirection(word):
            skip = True
        else:
            kept.append(word)
    return kept


# Words that open a compound command and sit before its first command word.
_RESERVED_PREFIXES = {"!", "{", "}", "do", "then", "else", "elif", "if", "while", "until"}


# The shell's control and redirection operators, longest first, and the
# newline: what a run of operator characters shlex returns whole (`);`, `)&&`,
# `&&(`, `()`, `)` then a newline) splits into.
_SHELL_OPERATORS = (
    "<<<",
    "&>>",
    "&&",
    "||",
    ";;",
    "|&",
    "<<",
    ">>",
    "<&",
    ">&",
    "<>",
    ">|",
    "&>",
    ";",
    "&",
    "|",
    "(",
    ")",
    "<",
    ">",
    "\n",
)
# Operators after which a newline does not end a command: the list goes on
# (`&&`, `|`, `(`, _shell_tokens' `<(` and `>(`), or the operator already ended it.
_NEWLINE_CONTINUES = {"&&", "||", "|", "|&", "(", "<(", ">(", ";", ";;", "&"}


def _command_tokens(text: str) -> list[str] | None:
    """The shell tokens of *text* (_lexed_shell_tokens) with each run of
    operator characters split into its operators (_SHELL_OPERATORS), as
    _shell_tokens splits them, `<(` and `>(` into two. A newline in a run ends
    a command, as a `;` token, unless the operator before it continues the
    list or already ended one, as in _shell_tokens. A word of operator
    characters (_QuotedWord) stays one word."""
    lexed = _lexed_shell_tokens(text)
    if lexed is None:
        return None
    tokens: list[str] = []
    for token in lexed:
        if not _operator_run(token):
            tokens.append(token)
            continue
        pos = 0
        while pos < len(token):
            operator = next(op for op in _SHELL_OPERATORS if token.startswith(op, pos))
            pos += len(operator)
            if operator != "\n":
                tokens.append(operator)
            elif tokens and tokens[-1] not in _NEWLINE_CONTINUES:
                tokens.append(";")
    return tokens


# Operator tokens that end a pipeline segment, and the pipes among them: `|&`
# pipes stderr along with stdout.
_PIPE_TOKENS = {"|", "|&"}
_SEGMENT_BREAK_TOKENS = {"&&", "||", ";", ";;", "&", "(", ")", *_PIPE_TOKENS}


def _segments_with_breaks(tokens: list[str]) -> list[tuple[list[str], str | None]]:
    """The pipeline segments of *tokens*, each with the operator that ends it."""
    segments: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SEGMENT_BREAK_TOKENS:
            segments.append((current, token))
            current = []
        else:
            current.append(token)
    segments.append((current, None))
    return segments


# shlex operator characters, plus the newline that ends a command.
_TOKEN_PUNCTUATION = "();<>|&\n"
_TOKEN_PUNCTUATION_SET = frozenset(_TOKEN_PUNCTUATION)


class _QuotedWord(str):
    """A word made only of operator characters because they are quoted or
    escaped (`';'`, `\\;`, the newline `$'\\n'` decodes to): the shell reads a
    word there, so it equals no operator, only a _QuotedWord of the same text
    (find's `\\;`)."""

    __slots__ = ()
    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _QuotedWord) and str.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self == other


# Stand-ins _mark_quoting writes into a text before shlex drops its quotes: one
# for a `$` or `~` the shell keeps literal (single-quoted, or escaped where no
# quote is open), one after a `$NAME` whose name a quote or an escape ends.
_LITERAL_DOLLAR = ""
_LITERAL_TILDE = ""
_NAME_END = ""
_SHELL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENDED_NAME_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)" + _NAME_END)


class _BindingWord(str):
    """A word whose quotes said where a `$NAME` ends or that a `$` or `~` is
    literal, which its text (quotes removed) no longer shows: `"$HOME"x` reads
    `$HOMEx`, `'$HOME'` reads `$HOME`. *binding* is the word as a variable
    assignment binds it: each such name braced (`${HOME}x`), each literal `$`
    or `~` as _LITERAL_DOLLAR or _LITERAL_TILDE, which no reference or tilde
    reading matches."""

    __slots__ = ("binding",)

    def __new__(cls, text: str, binding: str) -> "_BindingWord":  # noqa: UP037
        word = super().__new__(cls, text)
        word.binding = binding
        return word


def _binding_text(word: str) -> str:
    """*word* as a variable assignment binds it (_BindingWord.binding)."""
    return word.binding if isinstance(word, _BindingWord) else word


def _literal_text(text: str) -> str:
    """*text* with each _LITERAL_DOLLAR and _LITERAL_TILDE back as the `$` or
    `~` it stands for: a bound value once no reading expands it again, where
    it names a file or shows in a message."""
    return text.replace(_LITERAL_DOLLAR, "$").replace(_LITERAL_TILDE, "~")


def _mark_quoting(text: str) -> str:
    """*text* (its `$'...'` words decoded) with each `$` or `~` the shell keeps
    literal replaced by its stand-in, the backslash that escapes one dropped,
    and _NAME_END after each `$NAME` a quote or a backslash ends, which shlex
    would join to the text after it. A `\\$` inside double quotes keeps both
    characters, as shlex reads it."""
    if "$" not in text and "~" not in text:
        return text
    out: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if quote == "'":
            if char == "'":
                quote = None
            out.append(_LITERAL_DOLLAR if char == "$" else _LITERAL_TILDE if char == "~" else char)
            i += 1
            continue
        if char == "\\":
            escaped = text[i + 1 : i + 2]
            if quote is None and escaped in ("$", "~"):
                out.append(_LITERAL_DOLLAR if escaped == "$" else _LITERAL_TILDE)
            else:
                out.append(text[i : i + 2])
            i += 2
            continue
        if char == '"' or (char == "'" and quote is None):
            quote = None if quote == char else char
        elif char == "~" and quote == '"':
            char = _LITERAL_TILDE
        elif char == "$":
            name = _SHELL_NAME_RE.match(text, i + 1)
            if name is not None:
                out.append(text[i : name.end()])
                i = name.end()
                after = text[i : i + 1]
                if after == '"' or (quote is None and after in ("'", "\\")):
                    out.append(_NAME_END)
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _operator_run(token: str) -> bool:
    """True when *token* (of _lexed_shell_tokens) is a run of operator
    characters the shell reads as operators."""
    return (
        bool(token) and set(token) <= _TOKEN_PUNCTUATION_SET and not isinstance(token, _QuotedWord)
    )


# One shell operator, longest first; splits a run shlex returns as one token.
# A process substitution's `<(` / `>(` stays one token.
_SHELL_OPERATOR_RE = re.compile(r"&>>|&&|&>|\|\||\|&|;;|>>|>\||>&|<<<|<<|<>|<&|[<>]\(|[;&|()<>]")
# The backslash escapes of an ANSI-C quoted `$'...'` word, as bash decodes them.
_ANSI_C_ESCAPE_RE = re.compile(
    r"\\(?:([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})"
    r"|c([\s\S])|([abeEfnrtv\\'\"?]))"
)
_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "e": "\x1b",
    "E": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "?": "?",
}


def _escaped_char(code: int, escape: str) -> str:
    """The character with code point *code*, or the *escape* text itself when
    *code* is none: past the Unicode range, or a lone surrogate (which no
    path, argument or subprocess call can encode, so a guard would crash)."""
    if code > sys.maxunicode or 0xD800 <= code <= 0xDFFF:
        return escape
    return chr(code)


def _decode_ansi_c_escape(match: re.Match[str]) -> str:
    octal, hex_byte, short_code, long_code, control, simple = match.groups()
    if simple is not None:
        return _ANSI_C_SIMPLE_ESCAPES[simple]
    if control is not None:
        return "\x7f" if control == "?" else chr(ord(control.upper()) & 0x1F)
    code = int(octal, 8) if octal else int(hex_byte or short_code or long_code, 16)
    return _escaped_char(code, match.group(0))


def _decode_ansi_c(body: str) -> str:
    """The text of an ANSI-C quoted word whose content is *body*: escapes
    decoded, and cut at a NUL the way bash ends the word there."""
    return _ANSI_C_ESCAPE_RE.sub(_decode_ansi_c_escape, body).split("\0", 1)[0]


def _decode_ansi_c_quotes(text: str) -> str | None:
    """Rewrite each unquoted `$'...'` in *text* as the single-quoted word it
    decodes to (`$'it\\'s'` becomes `'it'\\''s'`), and each `$"..."` as
    `"..."`, so shlex reads them as bash does. A `$'` inside quotes or after
    a backslash is literal text. Comments are not recognised: _shell_tokens
    strips them first, and the nested-shell prefilter only searches the result.

    Returns None when a `$'` word is unterminated.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if char == "$" and text[i + 1 : i + 2] in ("'", '"'):
            if text[i + 1] == '"':
                i += 1
                continue
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                return None
            decoded = _decode_ansi_c(text[i + 2 : j])
            out.append("'" + decoded.replace("'", "'\\''") + "'")
            i = j + 1
            continue
        if char in "'\"":
            j = i + 1
            while j < n and text[j] != char:
                j += 2 if char == '"' and text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _shell_tokens(text: str) -> list[str] | None:
    """Shell-tokenize *text* with quotes respected and operators split out.

    An unquoted newline ends a command the way `;` does, so it comes back as a `;` token, or folds
    into the operator it follows (`&&` then a newline stays `&&`; `)` then a newline is `)` and
    `;`). shlex returns a run of operator characters whole (`(true);`, `$(date)&&`); each run is
    split into its operators (_SHELL_OPERATOR_RE), or the command after it would ride on the segment
    before it, since a reader breaks segments only at a lone operator. `#` comments are stripped
    first: shlex would otherwise swallow the newline that ends a comment's line. ANSI-C `$'...'` and
    `$"..."` words are then rewritten into quoting shlex knows (_decode_ansi_c_quotes). A quoted or
    escaped operator character is a word (_QuotedWord), never an operator. zsh's glob words are not
    read. Continuations arrive already joined (_join_line_continuations runs first).

    Returns None when the text cannot be tokenized (e.g. unbalanced quotes).
    """
    tokens = _lexed_shell_tokens(text)
    if tokens is None:
        return None
    split: list[str] = []
    for token in tokens:
        if not _operator_run(token):
            split.append(token)
            continue
        # A newline in the run ends a command unless the operator before it,
        # in this run or an earlier one, continues the list or already ended
        # one (`$(date)` then a newline ends the `echo`), as in _command_tokens.
        for index, part in enumerate(token.split("\n")):
            if index and split and split[-1] not in _NEWLINE_CONTINUES:
                split.append(";")
            split += _SHELL_OPERATOR_RE.findall(part)
    return split


class _TokenBuffer:
    """A token shlex builds one character at a time (`self.token += char`),
    kept as a list so a long word is built in linear time: shlex's own string
    concatenation copies the word so far on every character."""

    __slots__ = ("parts",)
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, text: str) -> None:
        self.parts = [text]

    def __iadd__(self, text: str) -> "_TokenBuffer":  # noqa: UP037
        self.parts.append(text)
        return self

    def __bool__(self) -> bool:
        return any(self.parts)

    def __eq__(self, other: object) -> bool:
        return str(self) == other

    def __str__(self) -> str:
        if len(self.parts) > 1:
            self.parts = ["".join(self.parts)]
        return self.parts[0]


class _LinearShlex(shlex.shlex):
    """shlex.shlex with its token held in a _TokenBuffer, and *operator* set
    when the token it last read is a run of unquoted operator characters.
    shlex starts such a run, and only such a run, by setting its token to one
    punctuation character; quoted and escaped characters it adds to the word
    it is building."""

    operator = False

    @property
    def token(self) -> _TokenBuffer:
        return self._token

    @token.setter
    def token(self, value: "str | _TokenBuffer") -> None:  # noqa: UP037
        if isinstance(value, _TokenBuffer):
            self._token = value
            return
        if value in _TOKEN_PUNCTUATION_SET:
            self.operator = True
        self._token = _TokenBuffer(value)

    def read_token(self) -> "str | _TokenBuffer | None":  # noqa: UP037
        self.operator = False
        return super().read_token()


def _linear_split(text: str) -> list[str]:
    """The words shlex.split reads from *text*, with its errors (ValueError on
    an unclosed quote or a trailing backslash), each word built in linear time
    (_LinearShlex): shlex.split copies the word so far on every character, so
    one long quoted word costs time quadratic in its length."""
    lexer = _LinearShlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [str(token) for token in lexer]


@functools.lru_cache(maxsize=256)
def _cached_lexed_shell_tokens(text: str) -> tuple[str, ...] | None:
    return _read_lexed_shell_tokens(text)


def _assignment_value(text: str) -> str | None:
    """The value the raw assignment value *text* binds (_binding_text) when the
    shell tokens read it as one word; None otherwise. Read uncached: a command
    holds as many values as bindings, which would crowd its own texts out of
    _cached_lexed_shell_tokens."""
    tokens = _read_lexed_shell_tokens(text)
    return _binding_text(tokens[0]) if tokens is not None and len(tokens) == 1 else None


def _read_lexed_shell_tokens(text: str) -> tuple[str, ...] | None:
    decoded = _decode_ansi_c_quotes(_strip_shell_comments(text))
    if decoded is None:
        return None
    lexer = _LinearShlex(_mark_quoting(decoded), posix=True, punctuation_chars=_TOKEN_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.whitespace_split = True
    tokens: list[str] = []
    try:
        for token in lexer:
            word = str(token)
            if _LITERAL_DOLLAR in word or _LITERAL_TILDE in word or _NAME_END in word:
                plain = _literal_text(word.replace(_NAME_END, ""))
                tokens.append(_BindingWord(plain, _ENDED_NAME_RE.sub(r"${\1}", word)))
                continue
            quoted = not lexer.operator and word and set(word) <= _TOKEN_PUNCTUATION_SET
            tokens.append(_QuotedWord(word) if quoted else word)
    except ValueError:
        return None
    return tuple(tokens)


def _lexed_shell_tokens(text: str) -> list[str] | None:
    """The tokens shlex reads from *text* (see _shell_tokens), before any newline is folded: a run
    of operator characters, newlines among them, comes back as one token, a word of operator
    characters as a _QuotedWord, and a word whose quotes end a `$NAME` or keep a `$` or `~` literal
    as a _BindingWord (_mark_quoting). Read once per text, since several checks tokenize the same
    text (a fresh list each call)."""
    tokens = _cached_lexed_shell_tokens(text)
    return None if tokens is None else list(tokens)


ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# `git` and any run of its global options (`man git`). Those that take a value (-C,
# -c, --git-dir, --work-tree, --namespace, --config-env, --attr-source) read the
# next word as that value; the rest, and `--opt=value`, are one word.
# A word is read the way the shell reads it, so `-C "my dir"`,
# `-C $(git rev-parse --show-toplevel)` and `-C $(dirname $(pwd))` (a substitution
# nested one deep) are one word each. Each text has one parse:
# `-[Cc]` rather than `-C|-c` because the scan ignores case, and two alternatives
# that match one text make a long run of options backtrack exponentially.
_GIT_VALUE_OPTION_NAMES = (
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--attr-source",
)
_GIT_VALUE_OPTIONS = (
    "(?:-[Cc]|"
    + "|".join(re.escape(name) for name in _GIT_VALUE_OPTION_NAMES if name.lower() != "-c")
    + ")"
)
# The same options as the words a reader of split words skips with their value.
_GIT_OPTIONS_WITH_VALUE = set(_GIT_VALUE_OPTION_NAMES)
# Those that name where git runs or the repository instead of the working directory.
_GIT_REPO_OPTIONS = ("-C", "--git-dir", "--work-tree")


class _GitGlobals(NamedTuple):
    """The global options of a git command, read from the words after `git`."""

    # Index of the subcommand in those words, their length when there is none.
    subcommand: int
    # Each -C, --git-dir and --work-tree with its value, in the order given.
    repo_options: tuple[tuple[str, str], ...]

    def directory(self, expand: Callable[[str], str] = lambda word: word) -> str | None:
        """Where git runs, relative to the shell's directory: the `-C` paths joined in
        order (`-C a -C b` runs in a/b), each through *expand* first; None without -C."""
        paths = [expand(value) for name, value in self.repo_options if name == "-C"]
        return os.path.join(*paths) if paths else None

    def git_dir(self, expand: Callable[[str], str] = lambda word: word) -> str | None:
        """The repository git reads: the last `--git-dir`, through *expand*, relative to
        where git runs (directory()); a linked worktree's `.git` file names its gitdir
        and counts too. None without one. `--work-tree` names only the working tree."""
        git_dirs = [value for name, value in self.repo_options if name == "--git-dir"]
        return expand(git_dirs[-1]) if git_dirs else None


def _git_globals(words: Sequence[str]) -> _GitGlobals:
    """Read git's global options from *words*, the words after `git`, the way git reads
    them: an option in _GIT_OPTIONS_WITH_VALUE takes the next word, a long one also
    `--name=value`; every other word starting with `-` is a flag of its own (git
    rejects `-C<path>` and `-c<name>=<value>`)."""
    idx = 0
    repo_options: list[tuple[str, str]] = []
    while idx < len(words) and words[idx].startswith("-"):
        word = words[idx]
        idx += 1
        name, equals, value = word.partition("=")
        if word in _GIT_OPTIONS_WITH_VALUE:
            name, value = word, words[idx] if idx < len(words) else ""
            idx += 1
        elif not (equals and name.startswith("--") and name in _GIT_OPTIONS_WITH_VALUE):
            continue
        if name in _GIT_REPO_OPTIONS:
            repo_options.append((name, value))
    return _GitGlobals(min(idx, len(words)), tuple(repo_options))


# gh's repo selector, the one option it takes before the words of a command's path
# (`gh -R o/r pr create`, `gh pr -R o/r create`, which cobra reads the same): gh's own
# flags are `--help` and `--version` (`gh --help`, gh 2.103) and every other option
# belongs to a command, so it comes after the path. Its value is the next word, glued
# on (`-Ro/r`) or behind an `=`.
_GH_REPO_OPTIONS = ("-R", "--repo")


def _gh_option_words(arg: str) -> int | None:
    """How many words gh's repo selector takes where *arg* starts it: 2 with its value in
    the next word, 1 with the value glued on or behind an `=`. None where *arg* is another
    option, whose value this reader cannot tell from a word of the command."""
    if arg in _GH_REPO_OPTIONS:
        return 2
    glued = arg.startswith("-R") and len(arg) > 2 and not arg.startswith("--")
    return 1 if glued or arg.startswith("--repo=") else None


def _gh_path(args: Sequence[str], limit: int) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """The path of the gh command whose words after the program are *args* - up to *limit*
    of the plain words that lead them - and the index in *args* just past each, where the
    options of a command named by that many words start.

    gh takes its repo selector among the words of the path (_gh_option_words), so those
    words are passed over; any other option ends the path, since its value is no word this
    reader can size (`gh --jq .x pr create` reads as no path at all).
    """
    words: list[str] = []
    ends: list[int] = []
    idx = 0
    while idx < len(args) and len(words) < limit:
        arg = args[idx]
        if arg.startswith("-"):
            if (size := _gh_option_words(arg)) is None:
                break
            idx += size
            continue
        words.append(arg)
        idx += 1
        ends.append(idx)
    return tuple(words), tuple(ends)


_SHELL_WORD_PIECE = (
    r"""(?:"[^"]*"|'[^']*'|\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`|\$\{[^}]*\}|\\.|\$(?![({])"""
    r"""|[^\s"'`$\\;&|<>()])"""
)
_GIT_GLOBAL_OPTION = rf"(?:{_GIT_VALUE_OPTIONS}\s+{_SHELL_WORD_PIECE}+|(?!{_GIT_VALUE_OPTIONS}\s)-{_SHELL_WORD_PIECE}*)"  # noqa: E501
_GIT_WITH_GLOBAL_OPTIONS = rf"git(?:\s+{_GIT_GLOBAL_OPTION})*\s+"


_REDIRECT_CHARS = set("<>&")


def _redirection(word: str) -> bool:
    """True when *word* (a shell token) is a redirection operator (`<`, `>>`,
    `>&`, `<<<`, `>|`): redirect characters with a `<` or `>`, or the `>|` that
    writes past noclobber, and not a quoted or escaped word (_QuotedWord: `'>'`
    is an argument)."""
    return (
        (set(word) <= _REDIRECT_CHARS and bool(set(word) & {"<", ">"})) or word == ">|"
    ) and not isinstance(word, _QuotedWord)


# Redirections that write their target, and the targets that are no file.
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
_NO_FILE_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}


def _writes_file(operator: str, target: str) -> bool:
    """Whether the redirection *operator* writes the file *target* (`> f`, `>> f`,
    `&> f`, `>& f`): the one rule every reader of written files asks. A target that
    is no file (`>/dev/null`) is none, and so are `>& 2` and `>&-`, which move or
    close a descriptor."""
    return (
        operator in _WRITE_REDIRECTS
        and target not in _NO_FILE_TARGETS
        and not (operator == ">&" and (target.isdigit() or target == "-"))
    )


# A file-descriptor number is a whole word attached to its operator
# (`2>/dev/null`); a spaced or quoted number (`12 >x`, `"12">x`) or digits
# ending a longer word (`:12>x`, `v1.12>x`) are arguments. The
# tokenizer loses that difference, so the fd number is cut from the raw text first.
_FD_NUMBER_RE = re.compile(r"(?<![^\s;&|()])\d+(?=[<>])")


def _strip_shell_comments(text: str) -> str:
    """*text* without its `# ...` comments (shell_comment_spans), which the
    shell never runs; the lexer drops them before it splits words."""
    out: list[str] = []
    last = 0
    for start, end in shell_comment_spans(text):
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    return "".join(out)


# Long shell options that consume the next word as their value.
_SHELL_LONG_OPTIONS_WITH_VALUE = {"--rcfile", "--init-file"}


def _shell_option_readings(option: str) -> set[tuple[int, bool]]:
    """Each way a shell reads the option word *option*, as (how many of the
    following words it consumes as values, whether it sets `-c`).

    `-o`/`-O`/`+o`/`+O` take a value, as do `--rcfile`/`--init-file`. Within a
    short cluster, bash has every `o`/`O` consume a following word
    (`-oc pipefail` is `-o pipefail -c`) while zsh reads the letters after
    one as its glued value (`-opipefail`); the two agree on a cluster that
    ends in its only `o`/`O` (`-euo pipefail`). Only a `-` cluster sets `-c`.
    """
    if option.startswith("--"):
        return {(1 if option in _SHELL_LONG_OPTIONS_WITH_VALUE else 0, False)}
    letters = option[1:]
    dash = option.startswith("-")
    bash = (letters.count("o") + letters.count("O"), dash and "c" in letters)
    first = next((i for i, letter in enumerate(letters) if letter in "oO"), len(letters))
    zsh = (int(first == len(letters) - 1), dash and "c" in letters[:first])
    return {bash, zsh}


def _shell_c_payload_indexes(tokens: list[str], idx: int) -> list[int]:
    """Indexes of the command line that the shell at *tokens[idx]* runs with
    `-c`: its first argument after the options and their values, provided an
    option sets `c`. Every reading of an ambiguous option cluster (see
    _shell_option_readings) gives its own candidate."""
    found: set[int] = set()
    pending = [(idx + 1, False)]
    seen: set[tuple[int, bool]] = set()
    while pending:
        state = pending.pop()
        if state in seen:
            continue
        seen.add(state)
        arg_idx, has_c = state
        if arg_idx >= len(tokens) or tokens[arg_idx] in _SEGMENT_BREAK_TOKENS:
            continue
        token = tokens[arg_idx]
        if not token.startswith(("-", "+")):
            if has_c:
                found.add(arg_idx)
            continue
        for consumed, sets_c in _shell_option_readings(token):
            pending.append((arg_idx + 1 + consumed, has_c or sets_c))
    return sorted(found)


def _shell_script_indexes(tokens: list[str], idx: int) -> list[int]:
    """Indexes of the script file that the shell at *tokens[idx]* runs: its
    first operand after the options and their values (`bash -euo pipefail
    x.sh`, `bash --rcfile r x.sh`), or the word after a `-` or `--`. Every
    reading of an ambiguous option cluster (_shell_option_readings) gives its
    own candidate. A reading that sets `-c` runs that command line instead, and
    one that sets `-s` (the script is stdin) or `-n` (a syntax check) before
    the cluster's first `o`/`O` runs no file."""
    found: set[int] = set()
    pending = [idx + 1]
    seen: set[int] = set()
    while pending:
        arg_idx = pending.pop()
        if arg_idx in seen or arg_idx >= len(tokens) or tokens[arg_idx] in _SEGMENT_BREAK_TOKENS:
            continue
        seen.add(arg_idx)
        token = tokens[arg_idx]
        if token in ("-", "--"):
            if arg_idx + 1 < len(tokens) and tokens[arg_idx + 1] not in _SEGMENT_BREAK_TOKENS:
                found.add(arg_idx + 1)
            continue
        if not token.startswith(("-", "+")):
            found.add(arg_idx)
            continue
        flags = re.split("[oO]", token[1:], maxsplit=1)[0]
        if token.startswith("-") and not token.startswith("--") and set(flags) & {"s", "n"}:
            continue
        pending.extend(
            arg_idx + 1 + consumed
            for consumed, sets_c in _shell_option_readings(token)
            if not sets_c
        )
    return sorted(found)


# Commands that read no stdin and run no text: file and directory setup (`cd`,
# `mkdir`, `chmod`, `cp`, `rm`, `ls`, `mktemp`), shell state and tests (`umask`,
# `set`, `export`, `test`, `true`, `exit`), `echo`, `printf`. A closed command list
# (shell_runs' _stdin_runner) takes none of them for a reader of the text fed
# to it, and a pipe from one feeds its command only its output. A bare one of them
# names no file it runs (_last_runs_of_files: `test -f test`).
_STDIN_FREE_COMMANDS = {
    "cd", "pwd", "echo", "printf", "umask", "set", "export", "test", "[", "[[", ":", "true",
    "false", "exit", "mkdir", "rmdir", "touch", "chmod", "chown", "chgrp", "cp", "mv", "rm",
    "ln", "ls", "mktemp",
}  # fmt: skip
# How many command lists deep (`bash -c '...'`, an ssh remote command, each
# holding the next) shell_runs' _stdin_runner and shell_heredoc_data's file
# tracker (_last_runs_of_files, _files_written) read. Each level re-tokenizes the
# rest of the command and takes stack frames, so an unbounded `ssh h ssh h ...`
# chain would overflow the stack. _stdin_runner takes a deeper list to run its stdin;
# the file tracker does not read one, so a file written or run there is not counted.
_MAX_STDIN_LIST_DEPTH = 5
# ssh options that take a value, glued (`-p22`) or as the next word.
_SSH_OPTIONS_WITH_VALUE = set("oiplFJLRDESWbcemOQwBIP")


def _ssh_config_option(text: str) -> tuple[str, str] | None:
    """The (keyword in lower case, value) of an ssh `-o` option *text*, None
    without a value. ssh_config separates a keyword from its value with `=` or
    blanks, in any mix."""
    parts = re.split(r"[=\s]+", text.strip(), maxsplit=1)
    return (parts[0].casefold(), parts[1]) if len(parts) == 2 and parts[1] else None


def _ssh_parse(words: list[str]) -> tuple[list[str], list[str]] | None:
    """(the words after the destination, the values of the `-o` options around
    it) of the ssh command *words*. None when there is no destination."""
    i, has_destination = 1, False
    options: list[str] = []
    while i < len(words):
        word = words[i]
        if word == "--":
            rest = words[i + 1 :]
            if has_destination:
                return rest, options
            return (rest[1:], options) if rest else None
        if word.startswith("-") and len(word) > 1:
            for pos, letter in enumerate(word[1:], 1):
                if letter in _SSH_OPTIONS_WITH_VALUE:
                    value = word[pos + 1 :]
                    if pos == len(word) - 1:
                        i += 1  # the value is the next word
                        value = words[i] if i < len(words) else ""
                    if letter == "o":
                        options.append(value)
                    break
            i += 1
            continue
        if has_destination:
            return words[i:], options
        has_destination = True
        i += 1
    return ([], options) if has_destination else None


def _ssh_remote_words(words: list[str]) -> list[str] | None:
    """The words of the command the ssh command *words* runs on its
    destination, which ssh joins with spaces: those after the destination and
    the options around it, or else the one word of its first `-o
    RemoteCommand=` option (ssh keeps the first, and refuses a command given
    both ways; `none` is no command). None when there is no destination."""
    parsed = _ssh_parse(words)
    if parsed is None:
        return None
    command, options = parsed
    if command:
        return command
    for option in map(_ssh_config_option, options):
        if option and option[0] == "remotecommand":
            return [] if option[1].casefold() == "none" else [option[1]]
    return []
