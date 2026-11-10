"""The command reader: the simple commands a Bash command line runs, read as the shell reads them.

Cases: the reader's argv per shape (reserved words, precommand words, wrappers, `eval` and
`env -S` payloads, case arms, function bodies), the heredoc scanner and quote masking on
quotes and comments, and the linear-time pins.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import shell_heredoc_data  # noqa: E402
import shell_runs  # noqa: E402
import shell_tokens  # noqa: E402
from shell_runs import _ssh_remote_commands  # noqa: E402

T = "terraform -chdir=infra apply"


def argvs(text: str) -> list[list[str]]:
    """The argv of each command the reader finds in *text* (assignment-only ones left out)."""
    runs = shell_runs._read_runs(text)
    assert runs is not None
    return [list(run.argv) for run in runs if run.argv]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("nice -n 10 timeout -s KILL 600 sudo -u root " + T, [T.split()]),
        ("command builtin exec -a x " + T, [T.split()]),
        ("noglob nocorrect - =terraform -chdir=infra apply", [T.split()]),
        ("repeat 3 time -p " + T, [T.split()]),
        ("X=1 Y+=2 Z[0]=3 >/dev/null 2>&1 " + T, [T.split()]),
        ("env -i -S 'terraform -chdir=infra apply'", [T.split()]),
        ("eval '" + T + "; echo done'", [T.split(), ["echo", "done"]]),
        ("if ! " + T + "; then { :; }; fi", [T.split(), [":"]]),
        ("case x in (a|b) " + T + ";; esac", [T.split()]),
        ("case $c in pkill|killall) echo no;; (x) " + T + ";; esac", [["echo", "no"], T.split()]),
        ("function f { " + T + "; }", [T.split()]),
        ("echo a &!" + T, [["echo", "a"], T.split()]),
        ('echo "$(' + T + ')" `date`', [["echo", "$(" + T + ")", "`date`"], T.split(), ["date"]]),
        ("echo apply | xargs -r terraform -chdir=infra", [["echo", "apply"], T.split()]),
    ],
)
def test_reader_skips_what_the_shell_skips(command, expected):
    assert argvs(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # A quoted or escaped operator is a word.
        ("echo ';' cd /tmp", [["echo", ";", "cd", "/tmp"]]),
        ("echo '<(' x ')' \\| $'\\n' y", [["echo", "<(", "x", ")", "|", "\n", "y"]]),
        (
            "find . -exec cat {} \\; -print; ls",
            [["find", ".", "-exec", "cat", "{}", ";", "-print"], ["ls"]],
        ),
    ],
)
def test_reader_reads_quoted_operators_as_words(command, expected):
    assert [[str(word) for word in argv] for argv in argvs(command)] == expected


@pytest.mark.parametrize(
    "command",
    [
        "ssh host 'python3 -' < <(echo \"print(1)\")",
        "ssh host 'python3 -' < <(echo a | grep \"b(c)\")",
        "ssh host 'python3 -' 2> >(grep \"a(b)\")",
        "ssh host 'python3 -' < <(echo $(echo \"x(y)\"))",
    ],
)
def test_a_process_substitution_redirection_target_is_no_remote_word(command):
    # The process substitution runs locally; ssh sends only its own words.
    tokens = shell_tokens._shell_tokens(shell_tokens._FD_NUMBER_RE.sub("", command))
    assert _ssh_remote_commands(tokens) == ["python3 -"]


@pytest.mark.parametrize(
    "command",
    ["command -v git push origin main", "command -V terraform apply", "sudo -l terraform apply"],
)
def test_lookups_run_nothing(command):
    assert argvs(command) == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd a && ls; ls", [False, True, False]),
        ("true || cd a && ls; ls", [False, True, True, False]),
        ("if true; then cd a; else cd b; fi; ls", [False, True, True, False]),
        ("for d in a; do cd $d; done; ls", [False, True, False]),
        ("while true; do cd a; done; ls", [False, True, False]),
        ("case x in x) cd a;; esac; ls", [True, False]),
        ("if true; then (cd a; ls); fi", [False, False, False]),
        ("true && eval 'cd a'; ls", [False, True, False]),
        ("false && { cd a; cd b; }; ls", [False, True, True, False]),
        ("{ cd a; cd b; }; ls", [False, False, False]),
    ],
)
def test_reader_marks_what_the_shell_may_skip(command, expected):
    runs = shell_runs._read_runs(command)
    assert runs is not None
    assert [run.conditional for run in runs if run.argv] == expected


def test_eval_payloads_are_read_to_the_nesting_cap():
    # Past _MAX_EVAL_DEPTH nested evals the payload is unparsed, however many ifs hold it.
    runs = shell_runs._read_runs("eval " * 400 + "ls")
    assert runs is not None and any(run.unparsed for run in runs)
    nested = shell_runs._read_runs("if a; then " * 6 + "eval 'git push origin main'" + "; fi" * 6)
    assert nested is not None
    assert ("git", "push", "origin", "main") in [run.argv for run in nested]


def test_heredoc_scanner_skips_quotes_and_comments():
    assert shell_heredoc_data._heredocs("echo '<<EOF'; ls\nEOF") == []
    assert shell_heredoc_data._heredocs('echo "<<EOF"; ls\nEOF') == []
    assert shell_heredoc_data._heredocs("echo a # see <<EOF\nls\nEOF") == []
    assert shell_heredoc_data._heredocs("echo $((1<<2))\nls\n2") == []
    text = "cat <<-'EOF' && ls\n\tbody\n\tEOF\nls"
    (heredoc,) = shell_heredoc_data._heredocs(text)
    assert (heredoc.tag, text[heredoc.end :]) == ("EOF", "\nls")
    assert shell_runs._strip_heredoc_bodies(text) == "cat <<EOF && ls\nls"


def test_masking_reads_an_apostrophe_in_a_comment_as_text():
    assert "pkill -f x" in shell_runs._mask_quoted_text("echo x # don't\npkill -f x")
    assert shell_runs._mask_quoted_text("echo 'a # b' && ls") == "echo '' && ls"


@pytest.mark.parametrize("unit", ['"$(true);"', '"$(true);" ', "'a' "])
def test_masking_runs_in_linear_time(assert_linear, unit):
    assert_linear(
        shell_runs, shell_runs._mask_quoted_text, lambda size: "echo " + unit * size, 3000
    )


@pytest.mark.parametrize(
    "unit",
    [
        "{ ls; }; ",
        "if ls; then :; fi; ",
        "eval ls; ",
        "nice -n 1 env A=1 ls | ",
        "echo $(ls) ",
        'echo "$(a "$(b)")" ',
        "kill $(pgrep -P 1 x); ",
        "echo a | xargs -I{} kill {}; ",
        "case x in x) ls;; esac; ",
        "true && cd a; ",
    ],
)
def test_reader_runs_in_linear_time(assert_linear, unit):
    assert_linear(shell_runs, shell_runs._read_runs, lambda size: unit * size + "ls", 2000)
