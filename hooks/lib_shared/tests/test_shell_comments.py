"""Where a `#` starts a shell comment, and which backslash-newlines the shell joins.

One reader decides both for each caller (shell_comments.py). Its rules were probed on /bin/zsh 5.9,
/bin/bash 3.2 and Homebrew bash 5.3 with echo stand-ins; a text one of the shells runs is never a
comment. A comment starts only at a word start: after a blank, a newline, `;`, `&`, `|` or an
operator paren, never after `<`, `>`, a brace, a `\\r` or an escape, and never after the `)` that
closes `$(`, `$((`, `<(` or an array's `=(` (`echo $(true)#x; cmd` runs cmd). A comment inside
`$(...)` runs to its newline; one in a backtick body ends at the closing backtick. A
backslash-newline inside single quotes or `$'...'` is kept, so `bash -c 'true # c\\` + newline +
`cmd'` runs cmd.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import shell_comments  # noqa: E402
import shell_runs  # noqa: E402
from shell_tokens import _strip_shell_comments  # noqa: E402


@pytest.mark.parametrize(
    ("text", "stripped"),
    [
        ("echo ${#X}; terraform apply", "echo ${#X}; terraform apply"),
        ("echo {#x}; ls", "echo {#x}; ls"),
        ("echo a}#b; ls", "echo a}#b; ls"),
        ("echo a#b", "echo a#b"),
        ("{ #c\necho x; }", "{ \necho x; }"),
        ("echo a;#b; ls", "echo a;"),
        ("echo a #b\nls", "echo a \nls"),
        ("echo '#x' \"#y\" $'#z'", "echo '#x' \"#y\" $'#z'"),
        # `<` and `>` start no word: a syntax error, or zsh's numeric glob `<->`.
        ("echo a >#b; ls", "echo a >#b; ls"),
        ("echo a <#b; ls", "echo a <#b; ls"),
        ("ls <->#(N); ls", "ls <->#(N); ls"),
        # An escaped character and a `\r` are word text.
        ("echo a\\>#b; ls", "echo a\\>#b; ls"),
        ("echo a\\ #b; ls", "echo a\\ #b; ls"),
        ("echo a\r#b; ls", "echo a\r#b; ls"),
        # The `)` of a substitution, an array or a zsh glob group is word text.
        ("echo $(true)#x; ls", "echo $(true)#x; ls"),
        ("cat <(true)#x; ls", "cat <(true)#x; ls"),
        ("echo $((1))#x; ls", "echo $((1))#x; ls"),
        ("a=(1 2)#x; ls", "a=(1 2)#x; ls"),
        ("echo x(N)#x; ls", "echo x(N)#x; ls"),
        # An operator paren: a subshell, `((`, a function's `()`, a case pattern.
        ("(true)#x; ls", "(true)"),
        ("((1))#x; ls", "((1))"),
        ("f()#x\n{ ls; }", "f()\n{ ls; }"),
        ("case a in a) #c\nls;; esac", "case a in a) \nls;; esac"),
        # A case pattern's `)` inside `$(...)` does not close it.
        ("echo $(case a in a) echo;; esac)#x; ls", "echo $(case a in a) echo;; esac)#x; ls"),
        # Inside `$(...)` a comment runs to its newline, past a `)`.
        ("echo $(echo a #); ls\necho b); ls", "echo $(echo a \necho b); ls"),
        ('echo "$(echo a #)"; ls\necho b)"', 'echo "$(echo a \necho b)"'),
        ("cat <(echo a #)\necho b)#x; ls", "cat <(echo a \necho b)#x; ls"),
        # A backtick body ends at its backtick, a comment in it too.
        ("echo `echo a #x`; ls", "echo `echo a `; ls"),
        # No comment inside `${...}` or arithmetic.
        ("echo ${X:- #y}; ls", "echo ${X:- #y}; ls"),
        ("echo $(( 1 #x\n)); ls", "echo $(( 1 #x\n)); ls"),
        # A backslash-newline is no word start (`a #x` once joined), nor word text.
        ("echo a \\\n#x; ls", "echo a \\\n"),
        ("echo a\\\n#x; ls", "echo a\\\n#x; ls"),
    ],
)
def test_comment_starts_only_at_a_word_start(text, stripped):
    assert _strip_shell_comments(text) == stripped


def test_comment_spans_name_each_comment():
    text = "echo ${#X} # one\nls >#two\necho `a #three` #four"
    assert shell_comments.shell_comment_spans(text) == [(11, 16), (34, 40), (42, 47)]


def test_heredoc_body_comments_live_only_in_its_substitutions():
    body = "# text\n$(echo a #x\n) b"
    assert shell_comments.shell_comment_spans(body, heredoc_body=True) == [(16, 18)]


@pytest.mark.parametrize(
    ("text", "stripped"),
    [
        # Only a line that is the tag (after tabs, for `<<-`) closes a body.
        ("python3 - <<'E'\nE=1\nprint(E)\nE\nls", "python3 - <<E\nls"),
        ("cat <<E\nE;\nE )\nE)\nE \nE x\nE\nls", "cat <<E\nls"),
        ("cat <<-E\n\tE=1\n\tE\nls", "cat <<E\nls"),
        ("(cat <<E\nE)\nE\n)", "(cat <<E\n)"),
        # bash also closes a body inside `$(...)`, `<(...)` or `>(...)` at a line that
        # is the tag and `)`, and reads that line on from the `)`.
        ("x=$(cat <<E\nhi\nE)\nls", "x=$(cat <<E)\nls"),
        ("x=$(cat <<E\nhi\nE) && ls", "x=$(cat <<E) && ls"),
        ('x="$(cat <<E\nhi\nE)"; ls', 'x="$(cat <<E)"; ls'),
        ("x=$( (cat <<E\nhi\nE) ) && ls", "x=$( (cat <<E) ) && ls"),
        ("cat <(cat <<E\nhi\nE)\nls", "cat <(cat <<E)\nls"),
        ("x=$(cat <<-E\n\thi\n\tE)\nls", "x=$(cat <<E)\nls"),
        ("x=$(cat <<E\nE=1\nE x\nE\n); ls", "x=$(cat <<E\n); ls"),
        # A backtick body ends the heredoc body in it, closed or not.
        ("x=`cat <<E\nhi\nE`; ls", "x=`cat <<E`; ls"),
        ("x=`cat <<E\nhi\n`; ls", "x=`cat <<E`; ls"),
        ("x=`cat <<E\nhi\n`\nls", "x=`cat <<E`\nls"),
    ],
)
def test_heredoc_body_ends_where_the_shell_ends_it(text, stripped):
    assert shell_runs._strip_heredoc_bodies(text) == stripped


@pytest.mark.parametrize(
    ("text", "joined"),
    [
        ("echo ${#X} \\\nls", "echo ${#X} ls"),
        ("echo a >#b \\\nls", "echo a >#b ls"),
        ('echo "a\\\nb"', 'echo "ab"'),
        ('echo $"a\\\nb"', 'echo $"ab"'),
        ("echo ${X:-a\\\nb}", "echo ${X:-ab}"),
        ("echo $(echo a\\\nb)", "echo $(echo ab)"),
        # Kept inside single quotes and `$'...'`, and in a comment.
        ("echo 'a\\\nb'", "echo 'a\\\nb'"),
        ("echo $'a\\\nb'", "echo $'a\\\nb'"),
        ("echo ${X:-'a\\\nb'}", "echo ${X:-'a\\\nb'}"),
        ("echo \"$(echo 'a\\\nb')\"", "echo \"$(echo 'a\\\nb')\""),
        ("bash -c 'true # c\\\ncmd'", "bash -c 'true # c\\\ncmd'"),
        # A quote open at a heredoc (`"$(cat <<E`) closes after its body.
        ("echo \"$(cat <<'E'\nx\nE\n)\" 'a\\\nb'", "echo \"$(cat <<'E'\nx\nE\n)\" 'a\\\nb'"),
        ("echo $(echo a # c\\\necho b)", "echo $(echo a # c\\\necho b)"),
        # A single quote inside double quotes is a character.
        ("echo \"'a\\\nb'\"", "echo \"'ab'\""),
        # Inside a backtick body every backslash-newline joins, quoted or not.
        ("echo `echo 'a\\\nb'`", "echo `echo 'ab'`"),
        ("echo `echo a # c\\\necho b`; ls", "echo `echo a # cecho b`; ls"),
    ],
)
def test_backslash_newline_joins_where_the_shell_joins_it(text, joined):
    assert shell_runs._join_line_continuations(text) == joined


def read(text: str) -> None:
    """Run the comment reader, the heredoc reader and the joiner on *text*."""
    shell_comments.shell_comment_spans(text)
    shell_comments.shell_heredocs(text)
    shell_runs._join_line_continuations(text)


# Each construct the reader opens a frame for, repeated, and left unterminated.
REPEATED = [
    "echo $(true)#x; ",
    "echo `echo a #x`; ",
    "echo $(case a in a) echo;; esac)#x; ",
    "cat <(echo a #)\n); ",
    "echo \"$(echo 'a\\\nb')\" ${X:-'y'} $((1)); ",
    "echo $(",
    "echo `",
    "echo '",
    # Heredoc bodies inside `$(...)` and backticks, each line read once.
    "x=$(cat <<E\nE=1\nE)\n",
    "x=$(cat <<E\nE x\n",
    "x=`cat <<E\nE=1\n`; ",
    "cat <<A <<B\nA\nB\n",
]


@pytest.mark.parametrize("unit", REPEATED)
def test_reader_runs_in_linear_time(assert_linear, unit):
    assert_linear(shell_runs, read, lambda size: unit * size, 2000)


def test_backslash_newline_inside_a_comment_or_single_quotes_is_not_a_continuation():
    text = "ls # note \\\ncat x\necho 'a \\\nb' \\\n -n"
    assert shell_runs._join_line_continuations(text) == "ls # note \\\ncat x\necho 'a \\\nb'  -n"
