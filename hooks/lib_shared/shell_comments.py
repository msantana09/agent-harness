"""
Where a shell comment starts, which backslash-newlines the shell joins, and where
each heredoc body ends.

The one reader of these: a reader or a guard that decides either on its own reads some command
differently from the shell, and the command it misreads runs unseen. The rules, probed on /bin/zsh
5.9 (the Bash tool's shell), /bin/bash 3.2 and Homebrew bash 5.3 with echo stand-ins; where the
shells disagree and one of them runs the text, the text is no comment:

- A `#` starts a comment only at a word start: the start of the text, or after
  an unquoted, unescaped blank, newline, `;`, `&`, `|`, or an operator paren.
  A `<` or `>` is no word start (`echo a >#x` is a syntax error, and zsh reads
  `<->#(N)` as one numeric-glob word), nor is a brace (`${#X}`), a `\\r`, or an
  escaped character (`a\\ #x` is one word).
- An operator paren opens or closes a subshell (`(` in command position), a
  `((` arithmetic command, a function's `()`, or a `case` pattern. The `)` that
  closes `$(`, `$((`, `<(`, `>(`, an array assignment's `=(` or a zsh glob
  group (`x(N)`, `(a|b)` as an argument) is word text: `echo $(true)#x; cmd`
  runs cmd in every shell.
- Inside `$(...)`, `<(...)`, `>(...)`, a subshell and a backtick body the same
  rules apply, and a comment there runs to its newline: `$(echo a #)` keeps the
  substitution open on the next line. A backtick body ends at the first
  unescaped backtick, quotes or not, so a comment in it ends there too. No
  comment starts inside double quotes, `${...}` or arithmetic.
- `case` is followed through its patterns, so a pattern's `)` inside `$(...)`
  does not close the substitution (bash 5.3 and zsh; bash 3.2 rejects it).
- A backslash-newline is deleted (a continuation) outside quotes, inside double
  quotes and `$"..."`, inside `${...}` and arithmetic, and everywhere inside a
  backtick body, single quotes and comments included. It is kept inside single
  quotes, inside `$'...'` (bash keeps both characters, zsh the newline) and in
  a comment outside backticks, where it is comment text and the next line runs.

Heredoc bodies are not read unless asked (*read_heredocs*): left off, a body
still in the text is lexed as code (the readers of command text read the bodies a
shell runs that way), and set, each body is read past as the shell reads it.
Where a body ends is one rule for every reader (heredoc_closes). Known limits:
zsh's `case WORD { ... }` form, `$((...))` that bash re-reads as a nested
`$( (...) )`, and a heredoc body inside a substitution of text read without
*read_heredocs*.
"""

import re

_CMD_KINDS = frozenset({"top", "subst", "subshell", "wparen", "backtick"})
# Reserved words after which the next word is still in command position.
_KEEP_COMMAND_POSITION = frozenset(
    {"if", "then", "else", "elif", "while", "until", "do", "!", "time", "{", "coproc"}
)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
# A run of characters with no meaning to the command reader: word text.
_ORDINARY_RUN_RE = re.compile(r"[^ \t\n\\#'\"$`<>()|;&]+")
_DQ_SPECIAL_RE = re.compile(r'["\\`$]')
_PARAM_SPECIAL_RE = re.compile(r"[}\\'\"$`]")
_ARITH_SPECIAL_RE = re.compile(r"[()\\'\"$`]")
_BODY_SPECIAL_RE = re.compile(r"[\\$`]")
_CASE_ENDS = (";;&", ";;", ";&", ";|")
_LIST_OPERATORS = ("&&", "||", "|&", ";", "&", "|")
_REDIRECTIONS = ("<<<", "<<-", "&>>", "<<", "<>", "<&", ">>", ">&", ">|", "&>", "<", ">")
_DELIMITER_END = frozenset(" \t\n;&|()<>")


class _Frame:
    """One level of the reader: a command context (the top, `$(`, a subshell,
    a word paren, a backtick body), double quotes, `${...}`, arithmetic, or a
    heredoc body."""

    __slots__ = (
        "kind", "limit", "in_backtick", "in_subst", "word_start", "word_open", "word_begin",
        "word_plain", "cmd_pos", "target", "case", "depth", "dq", "word_closer", "quote_start",
    )  # fmt: skip

    def __init__(self, kind: str, limit: int, in_backtick: bool, in_subst: bool) -> None:
        self.kind = kind
        self.limit = limit  # a backtick body's end bounds every frame inside it
        self.in_backtick = in_backtick
        self.in_subst = in_subst  # inside `$(...)`, `<(...)` or `>(...)`, at any depth
        self.word_start = kind != "wparen"
        self.word_open = False
        self.word_begin = 0
        self.word_plain = True
        self.cmd_pos = kind != "wparen"
        self.target = False  # the next word is a redirection's target
        self.case: list[str] = []  # case states: subject, in, pat_start, pat, body
        self.depth = 0  # open parens inside arithmetic
        self.dq = False  # a `${...}` inside double quotes, where `'` is a character
        self.word_closer = True  # arithmetic: `$((` (word text) or `((` (operator)
        self.quote_start = 0  # double quotes: where the `"` (or `$"`) is


def heredoc_closes(line: str, tag: str, in_substitution: bool) -> bool:
    """Whether *line* (its leading tabs cut, for `<<-`) ends the body of a heredoc whose delimiter
    word is *tag*: a line that is the tag, where bash and zsh both end it, or, for a heredoc opened
    inside `$(...)`, `<(...)` or `>(...)` (*in_substitution*), a line that is the tag and then `)`,
    which bash alone ends a body at and reads on from the `)` as commands (`EOF)` then `"`, `&&
    cmd`). zsh reads that line as body, so the commands read after it fail closed there. A line that
    only starts with the tag (`EOF=1`, `EOF x`, `EOF `) ends nothing. Every heredoc reader closes a
    body here (shell_heredocs and every reader built on it)."""
    return line == tag or (in_substitution and line.startswith(tag + ")"))


def _ansi_c_text(body: str) -> str:
    """The text of the ANSI-C quote `$'...'` whose content is *body*, as the
    shell decodes it (shell_tokens._decode_ansi_c, which reads this module, so it
    is imported when a body holds an escape)."""
    if "\\" not in body:
        return body
    from shell_tokens import _decode_ansi_c

    return _decode_ansi_c(body)


def _lex(
    text: str, heredoc_body: bool, read_heredocs: bool
) -> tuple[
    list[tuple[int, int]],
    list[int],
    list[tuple[int, int, str, int, int]],
    list[tuple[int, int, bool]],
]:
    """(comment spans, the index of each joined backslash-newline's backslash,
    the heredocs read: see shell_heredocs, the quoted strings: see shell_quotes)."""
    n = len(text)
    spans: list[tuple[int, int]] = []
    joins: set[int] = set()
    heredocs: list[tuple[int, int, str, int, int]] = []
    quotes: list[tuple[int, int, bool]] = []
    stack = [_Frame("hbody" if heredoc_body else "top", n, False, False)]
    # Each heredoc whose body starts on the next line: (tag, strip tabs, where
    # its `<<` is, where its delimiter word ends, whether it is opened inside a
    # substitution, the end of the backtick body it is opened in or of the text).
    pending: list[tuple[str, bool, int, int, bool, int]] = []
    i = 0

    def push(kind: str, **fields: object) -> _Frame:
        parent = stack[-1]
        frame = _Frame(kind, parent.limit, parent.in_backtick, parent.in_subst or kind == "subst")
        for name, value in fields.items():
            setattr(frame, name, value)
        stack.append(frame)
        return frame

    def open_word(frame: _Frame, at: int, plain: bool) -> None:
        if frame.kind not in _CMD_KINDS:
            return
        if not frame.word_open:
            frame.word_open, frame.word_begin, frame.word_plain = True, at, plain
        elif not plain:
            frame.word_plain = False
        frame.word_start = False

    def end_word(frame: _Frame, at: int) -> None:
        if not frame.word_open:
            return
        frame.word_open = False
        word = text[frame.word_begin : at] if frame.word_plain else None
        assignment = bool(_ASSIGNMENT_RE.match(text, frame.word_begin, at))
        if frame.target:
            frame.target = False
            return
        case = frame.case
        state = case[-1] if case else "body"
        if state == "subject":
            case[-1] = "in"
        elif state == "in":
            if word == "in":
                case[-1] = "pat_start"
            else:
                case.pop()
        elif state == "pat_start":
            if word == "esac":
                case.pop()
                frame.cmd_pos = False
            else:
                case[-1] = "pat"
        elif state == "pat":
            pass
        elif frame.cmd_pos:
            if word == "case":
                case.append("subject")
                frame.cmd_pos = False
            elif word == "esac" and case:
                case.pop()
                frame.cmd_pos = False
            else:
                frame.cmd_pos = word in _KEEP_COMMAND_POSITION or assignment

    def quote_end(start: int, limit: int, ansi_c: bool) -> int:
        """Index of the `'` closing the quote whose text starts at *start*."""
        if not ansi_c:
            close = text.find("'", start, limit)
            return limit if close == -1 else close
        j = start
        while j < limit and text[j] != "'":
            j += 2 if text[j] == "\\" else 1
        return min(j, limit)

    def read_quote(at: int, start: int, limit: int, ansi_c: bool) -> int:
        """Read the single-quoted string whose quote (or `$`) is at *at* and
        whose text starts at *start*; the index after it."""
        close = quote_end(start, limit, ansi_c)
        quotes.append((at, min(close + 1, limit), close < limit))
        return close + 1

    def comment_end(start: int, frame: _Frame) -> int:
        if not frame.in_backtick:
            newline = text.find("\n", start, frame.limit)
            return frame.limit if newline == -1 else newline
        j = start
        while j < frame.limit and text[j] != "\n":
            j += 2 if text.startswith("\\\n", j) else 1
        return min(j, frame.limit)

    def open_backtick(at: int) -> int:
        parent = stack[-1]
        j = at + 1
        while j < parent.limit and text[j] != "`":
            if text.startswith("\\\n", j):
                joins.add(j)
            j += 2 if text[j] == "\\" else 1
        frame = push("backtick")
        # bash reads a backtick body as a script of its own, outside the substitution
        frame.limit, frame.in_backtick, frame.in_subst = min(j, parent.limit), True, False
        return at + 1

    def open_dollar(frame: _Frame, at: int) -> int:
        """Read the `$` at *at*; the index after what it opens."""
        following = text[at + 1 : at + 2]
        quotes_live = frame.kind in _CMD_KINDS or (frame.kind == "param" and not frame.dq)
        if following == "'" and quotes_live:
            return read_quote(at, at + 2, frame.limit, True)
        if following == '"' and quotes_live:
            push("dq", quote_start=at)
            return at + 2
        if following == "(":
            if text.startswith("((", at + 1):
                push("arith")
                return at + 3
            push("subst")
            return at + 2
        if following == "{":
            push("param", dq=frame.kind == "dq" or (frame.kind == "param" and frame.dq))
            return at + 2
        return at + 1

    def read_delimiter(at: int) -> tuple[int, str]:
        while at < n and text[at] in " \t":
            at += 1
        tag: list[str] = []
        while at < n and text[at] not in _DELIMITER_END:
            char = text[at]
            if text.startswith("$'", at):
                # an ANSI-C quote: its text, escapes decoded (`<<$'EOF'` closes at `EOF`)
                close = quote_end(at + 2, n, True)
                tag.append(_ansi_c_text(text[at + 2 : close]))
                at = close + 1
            elif char in "'\"":
                close = text.find(char, at + 1)
                close = n if close == -1 else close
                tag.append(text[at + 1 : close])
                at = close + 1
            elif char == "\\":
                tag.append(text[at + 1 : at + 2])
                at += 2
            else:
                tag.append(char)
                at += 1
        return at, "".join(tag)

    def skip_bodies(at: int) -> int:
        """Read the body of each pending heredoc from *at*, in turn, each to the
        line that closes it (heredoc_closes) or to the end of the backtick body
        or text it is opened in; where the reading goes on. A body closed by
        `TAG)` leaves the rest of that line, from the `)`, to read as commands;
        the heredocs still pending then read their bodies from the next line."""
        read = 0
        for tag, strip_tabs, start, word_end, in_subst, limit in pending:
            read += 1
            body_end = end = limit
            line_rest = False
            while at < limit:
                newline = text.find("\n", at, limit)
                line_end = limit if newline == -1 else newline
                line = text[at:line_end]
                stripped = line.lstrip("\t") if strip_tabs else line
                if heredoc_closes(stripped, tag, in_subst):
                    body_end, end = at - 1, at + len(line) - len(stripped) + len(tag)
                    line_rest = stripped != tag
                    at = end if line_rest else line_end + 1
                    break
                at = line_end + 1
            heredocs.append((start, word_end, tag, body_end, end))
            if line_rest:
                break
        del pending[:read]
        return min(at, n)

    while i < n:
        frame = stack[-1]
        if i >= frame.limit:
            stack.pop()
            if frame.kind == "dq":
                quotes.append((frame.quote_start, frame.limit, False))
            if frame.kind == "backtick":
                # a heredoc opened in the body and still pending ends with it
                while pending and pending[-1][5] == frame.limit:
                    tag, _, start, word_end, _, limit = pending.pop()
                    heredocs.append((start, word_end, tag, limit, limit))
                i = frame.limit + 1
            continue
        char = text[i]
        kind = frame.kind

        if kind in _CMD_KINDS:
            run = _ORDINARY_RUN_RE.match(text, i, frame.limit)
            if run:
                open_word(frame, i, True)
                i = run.end()
                continue
            if char == "\\":
                if text.startswith("\n", i + 1):
                    joins.add(i)
                else:
                    open_word(frame, i, False)
                i += 2
            elif char == "#":
                if frame.word_start and not frame.word_open:
                    end = comment_end(i, frame)
                    spans.append((i, end))
                    i = end
                else:
                    open_word(frame, i, True)
                    i += 1
            elif char in " \t":
                end_word(frame, i)
                frame.word_start = True
                i += 1
            elif char == "\n":
                end_word(frame, i)
                frame.word_start = True
                if not (frame.case and frame.case[-1] in ("subject", "in", "pat")):
                    frame.cmd_pos = True
                i += 1
                if pending and read_heredocs:
                    i = skip_bodies(i)
            elif char == "'":
                open_word(frame, i, False)
                i = read_quote(i, i + 1, frame.limit, False)
            elif char == '"':
                open_word(frame, i, False)
                push("dq", quote_start=i)
                i += 1
            elif char == "$":
                open_word(frame, i, False)
                i = open_dollar(frame, i)
            elif char == "`":
                open_word(frame, i, False)
                i = open_backtick(i)
            elif char in "<>" and text.startswith("(", i + 1):
                open_word(frame, i, False)
                push("subst")
                i += 2
            elif char == "(":
                if text.startswith(")", i + 1) and not (frame.word_open and text[i - 1 : i] == "="):
                    # A function's `()`: `f()`, `f ()`, zsh's `echo x()`.
                    end_word(frame, i)
                    frame.word_start = frame.cmd_pos = True
                    i += 2
                elif frame.word_open:
                    push("wparen")
                    i += 1
                elif frame.case and frame.case[-1] == "pat_start":
                    frame.case[-1] = "pat"
                    frame.word_start = True
                    i += 1
                elif frame.cmd_pos:
                    if text.startswith("(", i + 1):
                        push("arith", word_closer=False)
                        i += 2
                    else:
                        push("subshell")
                        i += 1
                else:
                    open_word(frame, i, False)
                    push("wparen")
                    i += 1
            elif char == ")":
                end_word(frame, i)
                i += 1
                if frame.case and frame.case[-1] == "pat":
                    frame.case[-1] = "body"
                    frame.word_start = frame.cmd_pos = True
                elif kind == "subshell":
                    stack.pop()
                    parent = stack[-1]
                    parent.word_start, parent.cmd_pos = True, False
                elif kind in ("subst", "wparen"):
                    stack.pop()
                else:
                    frame.word_start = True  # unmatched: a syntax error
            else:
                operator = next(
                    (op for op in (*_CASE_ENDS, *_LIST_OPERATORS) if text.startswith(op, i)),
                    None,
                )
                if operator and not text.startswith(("&>", "&>>"), i):
                    end_word(frame, i)
                    frame.word_start = True
                    i += len(operator)
                    if frame.case and frame.case[-1] == "pat" and operator == "|":
                        continue
                    if frame.case and frame.case[-1] == "body" and operator in _CASE_ENDS:
                        frame.case[-1] = "pat_start"
                    frame.cmd_pos = True
                    continue
                redirection = next(op for op in _REDIRECTIONS if text.startswith(op, i))
                end_word(frame, i)
                frame.word_start = False
                frame.target = True
                start = i
                i += len(redirection)
                if redirection in ("<<", "<<-") and read_heredocs:
                    i, tag = read_delimiter(i)
                    frame.target = False
                    if tag:
                        pending.append(
                            (tag, redirection == "<<-", start, i, frame.in_subst, frame.limit)
                        )
            continue

        if kind == "dq":
            found = _DQ_SPECIAL_RE.search(text, i, frame.limit)
            if not found:
                i = frame.limit
                continue
            i = found.start()
            char = text[i]
            if char == '"':
                stack.pop()
                quotes.append((frame.quote_start, i + 1, True))
                i += 1
            elif char == "\\":
                if text.startswith("\n", i + 1):
                    joins.add(i)
                i += 2
            elif char == "`":
                i = open_backtick(i)
            else:
                i = open_dollar(frame, i)
            continue

        if kind == "param":
            found = _PARAM_SPECIAL_RE.search(text, i, frame.limit)
            if not found:
                i = frame.limit
                continue
            i = found.start()
            char = text[i]
            if char == "}":
                stack.pop()
                i += 1
            elif char == "\\":
                if text.startswith("\n", i + 1):
                    joins.add(i)
                i += 2
            elif char == "'":
                i = i + 1 if frame.dq else read_quote(i, i + 1, frame.limit, False)
            elif char == '"':
                push("dq", quote_start=i)
                i += 1
            elif char == "`":
                i = open_backtick(i)
            else:
                i = open_dollar(frame, i)
            continue

        if kind == "arith":
            found = _ARITH_SPECIAL_RE.search(text, i, frame.limit)
            if not found:
                i = frame.limit
                continue
            i = found.start()
            char = text[i]
            if char == "(":
                frame.depth += 1
                i += 1
            elif char == ")":
                if frame.depth:
                    frame.depth -= 1
                    i += 1
                    continue
                i += 2 if text.startswith(")", i + 1) else 1
                stack.pop()
                if not frame.word_closer:
                    parent = stack[-1]
                    parent.word_start, parent.cmd_pos = True, False
            elif char == "\\":
                if text.startswith("\n", i + 1):
                    joins.add(i)
                i += 2
            elif char == "'":
                i = read_quote(i, i + 1, frame.limit, False)
            elif char == '"':
                push("dq", quote_start=i)
                i += 1
            elif char == "`":
                i = open_backtick(i)
            else:
                i = open_dollar(frame, i)
            continue

        # A heredoc body whose delimiter is unquoted: text, with its expansions.
        found = _BODY_SPECIAL_RE.search(text, i, frame.limit)
        if not found:
            break
        i = found.start()
        if text[i] == "\\":
            i += 2
        elif text[i] == "`":
            i = open_backtick(i)
        else:
            i = open_dollar(frame, i)
    if pending:
        skip_bodies(n)
    quotes += [(frame.quote_start, n, False) for frame in stack if frame.kind == "dq"]
    return spans, sorted(joins), sorted(heredocs), quotes


def shell_comment_spans(
    text: str, heredoc_body: bool = False, read_heredocs: bool = False
) -> list[tuple[int, int]]:
    """Each comment of *text* as (where its `#` is, where it ends: the newline,
    the backtick that closes its body, or the end of the text), in order and at
    any depth (inside `$(...)` and backtick bodies too). With *heredoc_body*,
    *text* is the body of a heredoc whose delimiter is unquoted: only its
    substitutions hold code. With *read_heredocs*, the bodies of the heredocs
    *text* opens are read past, as the shell reads them."""
    return _lex(text, heredoc_body, read_heredocs)[0]


def shell_continuations(text: str, read_heredocs: bool = False) -> list[int]:
    """The index of the backslash of each backslash-newline of *text* that
    the shell deletes as a line continuation (see the module docstring). With
    *read_heredocs*, the bodies of the heredocs *text* opens are read past and
    hold none."""
    return _lex(text, False, read_heredocs)[1]


def shell_quotes(text: str, read_heredocs: bool = False) -> list[tuple[int, int, bool]]:
    """Each quoted string of *text* (`'...'`, `"..."`, `$'...'`, `$"..."`) as
    (where its quote, or its `$`, is, where it ends: past its closing quote, or
    the end of the text or of the backtick body it is in, whether a quote closes
    it), in order of where it starts and at any depth: a string inside a
    `$(...)` inside another is listed after it. A quote character in a comment
    or inside another kind of quotes opens none. With *read_heredocs*, the
    bodies of the heredocs *text* opens are read past, as the shell reads them
    (shell_heredocs), and hold none either."""
    return sorted(_lex(text, False, read_heredocs)[3])


def shell_heredocs(text: str) -> list[tuple[int, int, str, int, int]]:
    """Each heredoc *text* opens, in order, as (where its `<<` is, where its
    delimiter word ends, its tag, where its body ends: the newline before the
    closing line, where the closing tag ends), the last two the end of the text,
    or of the backtick body the heredoc is opened in, for a body no line closes.
    A `<<` inside quotes, a comment, arithmetic (`$((1<<2))`) or another
    heredoc's body opens none, nor does `<<<`; each heredoc opened on one line
    has its body after that line, in turn. A body ends at the line
    heredoc_closes names: so after `EOF)` inside `$(...)`, where bash ends a
    body and zsh does not, the text from the `)` on reads as commands, and a
    body line like `EOF=1` ends nothing."""
    return _lex(text, False, True)[2]
