"""Quoted data and heredoc text are blanked for a reader that asks "what does this command
run?".

Quoted strings and heredoc bodies are data (a grep pattern, a sed script, a file being
written) unless something local executes them: `bash|sh|zsh|python -c '...'`, `eval '...'`, a
heredoc fed or piped to bash/sh/zsh, or a command substitution in a heredoc body the shell
expands. Those payloads stay visible, a shell's masked as its own command text (a python
payload stays as written): the quoted strings in it that no runner takes are data too.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import shell_runs  # noqa: E402


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("grep -n 'git stash' f", "grep -n '' f"),
        ('echo "a \\" b" && ls', 'echo "" && ls'),
        ("bash -c 'git stash'", "bash -c  git stash "),
        ('zsh -lc "git stash"', "zsh -lc  git stash "),
        ("eval 'git stash'", "eval  git stash "),
        ("python3 -c 'git stash'", "python3 -c  git stash "),
        ("python3.14t -c 'git stash'", "python3.14t -c  git stash "),
        # an option value before `-c` (quoted too) does not hide the payload
        ("bash -euo pipefail -c 'git stash'", "bash -euo pipefail -c  git stash "),
        ("bash -o 'pipefail' -c 'git stash'", "bash -o '' -c  git stash "),
        ("bash +o posix -c 'git stash'", "bash +o posix -c  git stash "),
        # without `-c` the quoted word is a script path, not a payload
        ("bash -o pipefail 'git stash'", "bash -o pipefail ''"),
        # ANSI-C and locale quoting: `\'` stays inside the word, the payload decoded
        ("echo $'it\\'s' && git stash", "echo '' && git stash"),
        ('echo $"git stash"', 'echo ""'),
        ("bash -c $'git stash\\n'", "bash -c  git stash\n "),
        ("cat > f <<'EOF'\ngit stash\nEOF", "cat > f <<EOF"),
        # the rest of the opener line runs: only the body is data
        ("cat <<'EOF' > f && git stash 'x'\nbody\nEOF", "cat <<EOF > f && git stash ''"),
        # the body starts after the line the shell reads, a quoted newline in it too
        (
            "cat <<'EOF' > f && echo \"a\nb\" && git stash\nbody\nEOF",
            'cat <<EOF > f && echo "" && git stash',
        ),
        # quotes are read once over the whole text, the bodies read past: a quote
        # open at a heredoc (`"$(cat <<EOF`) closes after its body
        (
            "git commit -m \"$(cat <<'EOF'\nx\nEOF\n)\" && git stash",
            'git commit -m "<<EOF" && git stash',
        ),
        ("echo \"$(cat <<'A'\nx\nA\n)\" \"$(cat <<'B'\ny\nB\n)\"; ls", 'echo "<<A" "<<B"; ls'),
        ("x=\"$(cat <<'EOF'\nit's\nEOF\n)\" # don't\ngit stash", 'x="<<EOF" # don\'t\ngit stash'),
        (
            "x=\"$(sed 's/\"//g' f)\" && cat <<'EOF' > n.md\nb\nEOF\ngit stash",
            'x="" && cat <<EOF > n.md\ngit stash',
        ),
        ("bash <<'EOF'\ngit stash\nEOF", "bash <<'EOF'\ngit stash\nEOF"),
        # a shell list that only writes the body to a file takes it as text
        ("sh -c 'cat > f' <<'EOF'\ngit stash\nEOF", "sh -c  cat > f  <<EOF"),
        # a body piped into a shell runs; an expanded body's substitutions run
        ("cat <<'EOF' | bash\ngit stash\nEOF", "cat <<'EOF' | bash\ngit stash\nEOF"),
        ("cat > f <<EOF\nx $(git stash) `echo 'a b'`\nEOF", "cat > f <<EOF\ngit stash\necho ''"),
        ("cat > f <<'EOF'\nx $(git stash)\nEOF", "cat > f <<EOF"),
        # a body or payload a shell runs is masked as that shell's own command text:
        # a quoted string in it is data unless a runner in it takes the string
        ("bash <<'EOF'\necho 'git stash' \"x\"\nEOF", "bash <<'EOF'\necho '' \"\"\nEOF"),
        (
            "cat > s.sh <<'EOF'\ngrep 'git stash' f\nEOF\nbash s.sh",
            "cat > s.sh <<'EOF'\ngrep '' f\nEOF\nbash s.sh",
        ),
        ("bash -c 'echo \"git stash\"; ls'", 'bash -c  echo ""; ls '),
        ("eval 'echo \"git stash\"'", 'eval  echo "" '),
        ("bash -c $'echo \\'git stash\\''", "bash -c  echo '' "),
        ("bash -c 'bash -c \"git stash\"'", "bash -c  bash -c  git stash  "),
        (
            "cat <<'EOF' | bash\nsh -c 'git stash'\nEOF",
            "cat <<'EOF' | bash\nsh -c  git stash \nEOF",
        ),
        # what the outer shell expands in the body or payload runs, quoted or not
        ("bash <<EOF\necho '$(git stash)'\nEOF", "bash <<EOF\necho ''\ngit stash\nEOF"),
        ("bash -c \"echo '$(git stash)'\"", "bash -c  echo ''\ngit stash "),
        # a python payload is python, not shell: it stays as written
        ("python3 -c 'print(\"git stash\")'", 'python3 -c  print("git stash") '),
        # heredocs opened on one line read their bodies in turn
        ("cat <<'A'; bash <<'B'\nit's\nA\necho 'x'\nB\nls", "cat <<A; bash <<'B'\necho ''\nB\nls"),
        (
            "bash <<'A'; cat <<'B'\necho 'x'\nA\nit's\nB\nls",
            "bash <<'A'; cat <<'B'\necho ''\nA\nls",
        ),
    ],
)
def test_mask_quoted_text(command, expected):
    assert shell_runs._mask_quoted_text(command) == expected


@pytest.mark.parametrize(
    "unit",
    [
        "cat > s.sh <<'E'\nfor c in \"a 'b'\"; do echo \"$c\"; done\nE\nbash s.sh\n",
        "cat <<'E' | bash\necho 'a' \"b\"\nE\n",
        "bash -c \"bash -c 'echo \\\"a\\\"'; eval 'echo $(b)'\" && ",
    ],
)
def test_scripts_a_shell_runs_are_masked_in_linear_time(assert_linear, unit):
    assert_linear(shell_runs, shell_runs._mask_quoted_text, lambda size: unit * size + "ls", 2000)


def test_quotes_around_many_heredocs_are_masked_in_linear_time(assert_linear):
    unit = "echo \"$(cat <<'EOF'\nx\nEOF\n)\" && "
    assert_linear(shell_runs, shell_runs._mask_quoted_text, lambda size: unit * size + "ls", 2000)


def test_quotes_on_many_lines_after_heredocs_are_masked_in_linear_time(assert_linear):
    # a newline ends the command a quote's runner is read from, as `;` does
    unit = "cat <<'E' > f\nx\nE\nsh f 'a'\n"
    assert_linear(shell_runs, shell_runs._mask_quoted_text, lambda size: unit * size, 2000)
