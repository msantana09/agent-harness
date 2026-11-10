"""A heredoc body that only a known consumer reads is data: the raw-text checks skip it.

A file written with `cat > f.md <<EOF`, a script python or node reads, a commit message:
their bodies hold text, so a command named in one runs nothing. Every other body is code
and stays in the text: it goes to a shell, a program the reader does not know, a pipe, a
group or a substitution, or a file a shell may run.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import shell_heredoc_data  # noqa: E402
import shell_runs  # noqa: E402


@pytest.mark.parametrize(
    ("command", "stripped"),
    [
        ("cat > f.txt <<'EOF'\nbody\nEOF\nls", "cat > f.txt <<EOF\nls"),
        ("python3 - <<'EOF'\nbody\nEOF\nls", "python3 - <<EOF\nls"),
        ("tee f.txt <<EOF && ls\nbody\nEOF", "tee f.txt <<EOF && ls"),
        ("git commit -F - <<'EOF'\nfeat: x\nEOF", "git commit -F - <<EOF"),
        ("git -C d commit -q -F - <<'EOF'\nfeat: x\nEOF", "git -C d commit -q -F - <<EOF"),
        ("cat <<'EOF'\nbody\nEOF", "cat <<EOF"),
        ("node - <<'EOF'\nbody\nEOF", "node - <<EOF"),
        ("python -I - <<'EOF'\nbody\nEOF", "python -I - <<EOF"),
        # the interpreter reads its own source from stdin; what follows the `-` is argv
        ("python3 <<'EOF' && ls\nbody\nEOF", "python3 <<EOF && ls"),
        ("python3 -I - \"$F\" a <<'EOF'\nbody\nEOF", 'python3 -I - "$F" a <<EOF'),
        ("node --no-warnings - <<'EOF'\nbody\nEOF", "node --no-warnings - <<EOF"),
        ("timeout 30 python3 - <<'EOF'\nbody\nEOF", "timeout 30 python3 - <<EOF"),
        ("sudo tee f.md <<'EOF'\nbody\nEOF", "sudo tee f.md <<EOF"),
        # a body a shell reads is code
        ("bash <<'EOF'\nbody\nEOF\nls", "bash <<'EOF'\nbody\nEOF\nls"),
        ("ssh host <<'EOF'\nbody\nEOF", "ssh host <<'EOF'\nbody\nEOF"),
        ("echo a; (bash) <<'EOF'\nbody\nEOF", "echo a; (bash) <<'EOF'\nbody\nEOF"),
        ("bash <<'A'\nx\nA\ncat <<'B'\ny\nB", "bash <<'A'\nx\nA\ncat <<B"),
        # heredocs opened on one line read their bodies in turn
        ("cat <<'A' <<'B'\na\nA\nb\nB\nls", "cat <<A <<B\nls"),
        ("cat > a.md <<'A'; bash <<'B'\na\nA\nb\nB", "cat > a.md <<A; bash <<'B'\nb\nB"),
        ("bash <<'A'; cat <<'B'\na\nA\nb\nB", "bash <<'A'; cat <<B\na\nA"),
        (
            'python - x <<\'EOF\' 2>/dev/null || python - x <<\'EOF2\'\nEOF\nimport sys\no="""  a={b}"""\nEOF2\nnpx vitest run src',  # noqa: E501
            "python - x <<EOF 2>/dev/null || python - x <<EOF2\nnpx vitest run src",
        ),
    ],
)
def test_only_the_bodies_no_shell_runs_are_cut(command: str, stripped: str) -> None:
    assert shell_heredoc_data.strip_data_heredoc_bodies(command) == stripped


# A body whose consumer is not on the list of data readers stays code: a shell or a program
# that runs it, or one the reader does not know.
@pytest.mark.parametrize(
    "command",
    [
        "xargs -0 sh -c <<'EOF'\nbody\nEOF",
        "docker compose exec db psql -U app <<'EOF'\nbody\nEOF",
        "sudo -s <<'EOF'\nbody\nEOF",
        "su - root <<'EOF'\nbody\nEOF",
        ". /dev/stdin <<'EOF'\nbody\nEOF",
        "at now <<'EOF'\nbody\nEOF",
        "crontab - <<'EOF'\nbody\nEOF",
        "git apply fix.patch <<'EOF'\nbody\nEOF",
        "fish <<'EOF'\nbody\nEOF",
        "docker run -i ubuntu <<'EOF'\nbody\nEOF",
        "parallel <<'EOF'\nbody\nEOF",
        "while read -r l; do eval \"$l\"; done <<'EOF'\nbody\nEOF",
        "$READER <<'EOF'\nbody\nEOF",
        "sudo <<'EOF'\nbody\nEOF",
        # python and node are readers of their own source only: with a script, a module or
        # code of its own, stdin is input to a program the reader cannot see (`\x20` is the
        # space: the repo's check that commands name python3 reads this file's text)
        ".venv/bin/python\x20manage.py dbshell <<'EOF'\nbody\nEOF",
        "python3 -m sqlite3 db.sqlite <<'EOF'\nbody\nEOF",
        "python3 -I tool.py - <<'EOF'\nbody\nEOF",
        "python3 -c 'import os,sys; os.system(sys.stdin.read())' <<'EOF'\nbody\nEOF",
        "node run.js <<'EOF'\nbody\nEOF",
        "node -e 'process.stdin.pipe(process.stdout)' <<'EOF'\nbody\nEOF",
        # a gh command reads its stdin as text only where an option of its own names it: the
        # `-F` of `pr` is a body file and that of `api` a field, and gh runs none of it
        "gh pr create --title t --body-file b.md <<'EOF'\nbody\nEOF",
        "gh pr create --title t -F b.md <<'EOF'\nbody\nEOF",
        "gh pr create --title t --body x <<'EOF'\nbody\nEOF",
        "gh issue comment 1 --body-file /dev/stdin <<'EOF'\nbody\nEOF",
        "gh api repos/o/r --input b.json <<'EOF'\nbody\nEOF",
        "gh api graphql -F query=@q.graphql <<'EOF'\nbody\nEOF",
        "gh api graphql -F - <<'EOF'\nbody\nEOF",
        "gh workflow run ci.yml --json=false <<'EOF'\nbody\nEOF",
        # a path the table does not list, an option before the path, a cluster of short options
        "gh extension exec tool -F - <<'EOF'\nbody\nEOF",
        "gh codespace ssh -F - <<'EOF'\nbody\nEOF",
        "gh gist create f.sh <<'EOF'\nbody\nEOF",
        "gh gist create <<'EOF'\nbody\nEOF",
        "gh alias set -s co - <<'EOF'\nbody\nEOF",
        "gh secret set TOKEN <<'EOF'\nbody\nEOF",
        "gh --jq .x pr create --body-file - <<'EOF'\nbody\nEOF",
        "gh pr create -dF - <<'EOF'\nbody\nEOF",
    ],
)
def test_a_body_for_a_consumer_not_listed_stays(command: str) -> None:
    assert shell_heredoc_data.strip_data_heredoc_bodies(command) == command


# A body a data reader's output reaches a shell with, or one the shell may run as a
# substitution, stays code: a pipe, a group or a substitution around it, a line
# continuation that moves the pipe off the opener line, an unquoted body that expands.
@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF' | bash\nbody\nEOF",
        "cat <<'EOF' | ssh host\nbody\nEOF",
        "tee f.md <<'EOF' | sh\nbody\nEOF",
        "python3 - <<'EOF' | bash\nbody\nEOF",
        "cat <<'EOF' > >(bash)\nbody\nEOF",
        "cat <<'EOF' \\\n| bash\nbody\nEOF",
        "bash <(cat <<'EOF'\nbody\nEOF\n)",
        "sh -c \"$(cat <<'EOF'\nbody\nEOF\n)\"",
        "x=$(\ncat <<'EOF'\nbody\nEOF\n); $x",
        "{ cat <<'EOF'\nbody\nEOF\n} | bash",
        "{ cat <<'EOF'\nbody\nEOF\ntrue; } | bash",
        "( cat <<'EOF'\nbody\nEOF\n) | bash",
        "cat > f.txt <<EOF\n$(date)\nEOF",
        "cat > f.txt <<EOF\n`date`\nEOF",
        "gh pr create --body-file - <<EOF\n$(date)\nEOF",
        "gh pr create --body-file - <<EOF\n`date`\nEOF",
        # a compound command around it, whose output the closing word sends on
        "for i in 1; do\ncat <<'EOF'\nbody\nEOF\ndone | bash",
        "while true; do\ncat <<'EOF'\nbody\nEOF\nbreak\ndone | sh",
        "time -p for i in 1; do\ncat <<'EOF'\nbody\nEOF\ndone | bash",
        "until false; do cat <<'EOF'\nbody\nEOF\nbreak; done | bash",
        "select x in a; do\ncat <<'EOF'\nbody\nEOF\nbreak; done | bash",
        "printf 'a\\n' | while read l; do\ncat <<'EOF'\nbody\nEOF\ndone | sh",
        "if true; then\ncat <<'EOF'\nbody\nEOF\nfi | bash",
        "if true; then :; cat <<'EOF'\nbody\nEOF\nfi | bash",
        "case x in (x) cat <<'EOF'\nbody\nEOF\n;; esac | bash",
        "case x in\nx)\ncat <<'EOF'\nbody\nEOF\n;;\nesac | bash",
        "for i in 1; do for j in 2; do\ncat <<'EOF'\nbody\nEOF\ndone; done | bash",
        "for i in 1; do\ncat <<'EOF'\nbody\nEOF\ndone > >(bash)",
        "for i in 1; do\ncat <<'EOF'\nbody\nEOF\ndone > run.sh; bash run.sh",
    ],
)
def test_a_body_that_reaches_a_shell_stays(command: str) -> None:
    assert shell_heredoc_data.strip_data_heredoc_bodies(command) == command


# A file written is data only when it is no script: a shell script, a startup file or a
# hook (no data extension), one a shell later runs, or a target the reader cannot name.
@pytest.mark.parametrize(
    "command",
    [
        "cat > .git/hooks/pre-commit <<'EOF'\nbody\nEOF",
        "cat > .husky/pre-commit <<'EOF'\nbody\nEOF",
        "cat >> ~/.zshrc <<'EOF'\nbody\nEOF",
        "cat > x.sh <<'EOF'\nbody\nEOF\nbash x.sh",
        "tee x.sh <<'EOF'\nbody\nEOF\nsource x.sh",
        "cat > x.sh <<'EOF'\nbody\nEOF\nchmod +x x.sh && ./x.sh",
        "cat > x.sh <<'EOF'\nbody\nEOF\n. ./x.sh",
        "cat > x.txt <<'EOF'\nbody\nEOF\nbash x.txt",
        "cat > x.md <<'EOF'\nbody\nEOF\nsource x.md",
        "cat > x.txt <<'EOF'\nbody\nEOF\nsh < x.txt",
        "cat > x.txt <<'EOF'\nbody\nEOF\ncat x.txt | sh",
        "cat > run.py <<'EOF'\nbody\nEOF\nchmod +x run.py && ./run.py",
        "cat > m.sql <<'EOF'\nbody\nEOF",
        "cat > \"$F\" <<'EOF'\nbody\nEOF",
        "tee a.txt b.sh <<'EOF'\nbody\nEOF",
        "cat > a.txt > b.sh <<'EOF'\nbody\nEOF",
        # a command that runs the file sits behind a compound command's reserved word
        "cat > x.txt <<'EOF'\nbody\nEOF\nif true; then bash x.txt; fi",
        "cat > x.sh <<'EOF'\nbody\nEOF\nfor i in 1; do ./x.sh; done",
        "cat > x.txt <<'EOF'\nbody\nEOF\nwhile bash x.txt; do break; done",
        # a shell option that takes a value, a `-c` list or an ssh remote command that runs
        # it, a copy of it that runs, and a crontab, whose lines cron runs
        "cat > x.md <<'EOF'\nbody\nEOF\nbash -o pipefail x.md",
        "cat > x.md <<'EOF'\nbody\nEOF\nbash -euo pipefail x.md",
        "cat > x.txt <<'EOF'\nbody\nEOF\nsh -c 'bash x.txt'",
        "cat > x.txt <<'EOF'\nbody\nEOF\nscp x.txt h: && ssh h bash x.txt",
        "cat > x.txt <<'EOF'\nbody\nEOF\ncp x.txt y.sh && bash y.sh",
        "cat > x.txt <<'EOF'\nbody\nEOF\ncrontab x.txt",
    ],
)
def test_a_file_a_shell_may_run_stays(command: str) -> None:
    assert shell_heredoc_data.strip_data_heredoc_bodies(command) == command


@pytest.mark.parametrize(
    ("command", "stripped"),
    [
        ("cat > f.txt <<EOF\n$HOME\nEOF", "cat > f.txt <<EOF"),
        ("cat > $SCRATCH/n.md <<'EOF'\nbody\nEOF", "cat > $SCRATCH/n.md <<EOF"),
        ("cat > f.md 2>&1 <<'EOF'\nbody\nEOF", "cat > f.md 2>&1 <<EOF"),
        # a file written and then run by something that is no shell, or a shell before it
        ("cat > x.py <<'EOF'\nbody\nEOF\npython3 x.py", "cat > x.py <<EOF\npython3 x.py"),
        (
            "source .venv/bin/activate && cat > f.txt <<'EOF'\nbody\nEOF",
            "source .venv/bin/activate && cat > f.txt <<EOF",
        ),
        # a shell that is given the file as an input, not as its script
        (
            "cat > in.txt <<'EOF'\nbody\nEOF\nbash bench.sh in.txt",
            "cat > in.txt <<EOF\nbash bench.sh in.txt",
        ),
        # a shell that only checks its syntax, and a bare command named like the file
        ("cat > x.txt <<'EOF'\nbody\nEOF\nbash -n x.txt", "cat > x.txt <<EOF\nbash -n x.txt"),
        (
            "cat > test.md <<'EOF'\nbody\nEOF\ntest -f test.md",
            "cat > test.md <<EOF\ntest -f test.md",
        ),
        # dd, sponge, git apply and kubectl apply take the body as data
        ("sudo dd of=f.txt <<'EOF'\nbody\nEOF", "sudo dd of=f.txt <<EOF"),
        ("sponge f.md <<'EOF'\nbody\nEOF", "sponge f.md <<EOF"),
        ("git apply --index <<'EOF'\nbody\nEOF", "git apply --index <<EOF"),
        ("git --git-dir .git apply <<'EOF'\nbody\nEOF", "git --git-dir .git apply <<EOF"),
        ("git -C d --namespace n apply <<'EOF'\nbody\nEOF", "git -C d --namespace n apply <<EOF"),
        ("kubectl apply -f - <<'EOF'\nbody\nEOF", "kubectl apply -f - <<EOF"),
        # a gh command that reads its stdin as the text it stores or sends: a body, notes, a
        # task, a request
        (
            "gh pr create --title t --body-file - <<'EOF'\nbody\nEOF",
            "gh pr create --title t --body-file - <<EOF",
        ),
        ("gh pr new -t t -F - <<'EOF'\nbody\nEOF", "gh pr new -t t -F - <<EOF"),
        ("gh pr edit 1 -F- <<'EOF'\nbody\nEOF", "gh pr edit 1 -F- <<EOF"),
        ("gh pr comment 1 --body-file=- <<'EOF'\nbody\nEOF", "gh pr comment 1 --body-file=- <<EOF"),
        ("gh issue create -t t -F=- <<'EOF'\nbody\nEOF", "gh issue create -t t -F=- <<EOF"),
        (
            "gh release create v1 --notes-file - <<'EOF'\nbody\nEOF",
            "gh release create v1 --notes-file - <<EOF",
        ),
        ("gh agent create -F - <<'EOF'\nbody\nEOF", "gh agent create -F - <<EOF"),
        (
            "gh api repos/o/r/issues --input - <<'EOF'\nbody\nEOF",
            "gh api repos/o/r/issues --input - <<EOF",
        ),
        ("gh api graphql -F query=@- <<'EOF'\nbody\nEOF", "gh api graphql -F query=@- <<EOF"),
        ("gh workflow run ci.yml --json <<'EOF'\nbody\nEOF", "gh workflow run ci.yml --json <<EOF"),
        (
            "gh workflow run ci.yml --field a=@- <<'EOF'\nbody\nEOF",
            "gh workflow run ci.yml --field a=@- <<EOF",
        ),
        ("gh auth login --with-token <<'EOF'\nbody\nEOF", "gh auth login --with-token <<EOF"),
        ("gh gist create -f a.sh - <<'EOF'\nbody\nEOF", "gh gist create -f a.sh - <<EOF"),
        # the repo selector before or among the words of the path, in every form
        (
            "gh -R o/r pr create --body-file - <<'EOF'\nbody\nEOF",
            "gh -R o/r pr create --body-file - <<EOF",
        ),
        ("gh pr -Ro/r create -F - <<'EOF'\nbody\nEOF", "gh pr -Ro/r create -F - <<EOF"),
        (
            "gh --repo=o/r issue comment 1 -F - <<'EOF'\nbody\nEOF",
            "gh --repo=o/r issue comment 1 -F - <<EOF",
        ),
        (
            "sudo /opt/homebrew/bin/gh pr merge 1 -F - <<'EOF'\nbody\nEOF",
            "sudo /opt/homebrew/bin/gh pr merge 1 -F - <<EOF",
        ),
        # an unquoted body that expands no `$` or backtick is read as written
        ("gh pr create --body-file - <<EOF\nbody\nEOF", "gh pr create --body-file - <<EOF"),
        # a compound command around it whose output stays on the terminal
        (
            "if [ ! -f n.md ]; then\ncat > n.md <<'EOF'\nbody\nEOF\nfi",
            "if [ ! -f n.md ]; then\ncat > n.md <<EOF\nfi",
        ),
        (
            "if [ ! -f n.md ]; then cat > n.md <<'EOF'\nbody\nEOF\nfi",
            "if [ ! -f n.md ]; then cat > n.md <<EOF\nfi",
        ),
        (
            "for i in 1; do\ncat > n.md <<'EOF'\nbody\nEOF\ndone 2>&1",
            "for i in 1; do\ncat > n.md <<EOF\ndone 2>&1",
        ),
        (
            "case x in\nx)\ncat > n.md <<'EOF'\nbody\nEOF\n;;\nesac",
            "case x in\nx)\ncat > n.md <<EOF\n;;\nesac",
        ),
        (
            "case x in (x) cat > n.md <<'EOF'\nbody\nEOF\n;; esac",
            "case x in (x) cat > n.md <<EOF\n;; esac",
        ),
        # another body in the text that is code stays; this one is cut
        (
            "cat > a.txt <<'EOF'\nbody\nEOF\ncat > b.sh <<'EOF'\n#!/bin/bash\nEOF",
            "cat > a.txt <<EOF\ncat > b.sh <<'EOF'\n#!/bin/bash\nEOF",
        ),
    ],
)
def test_a_body_nothing_runs_is_data(command: str, stripped: str) -> None:
    assert shell_heredoc_data.strip_data_heredoc_bodies(command) == stripped


def test_a_quote_an_opener_line_closes_below_keeps_its_script() -> None:
    # bash runs heredoc A as /dev/fd/3; the second heredoc's quote closes on the next line:
    # A stays a script the reader finds
    command = "bash 3<<'A' '/dev/fd/3' <<'B' 'x\n#'\nterraform apply\nA\n:\nB"
    assert any("terraform apply" in body for _, body, _ in shell_runs._heredoc_scripts(command))


@pytest.mark.parametrize(
    "construct",
    [
        "cat > f.txt <<'EOF'\nbody\nEOF\n",
        "python3 - <<'EOF'\nbody\nEOF\n",
        "bash <<'EOF'\nbody\nEOF\n",
        "su - me <<'EOF'\nbody\nEOF\n",
        "cat > f.md <<'EOF'\nbody\nEOF\nbash f.md\n",
        "for i in 1; do\ncat <<'EOF'\nbody\nEOF\ndone | bash\n",
        "if true; then\ncat > f.md <<'EOF'\nbody\nEOF\nfi\n",
        "case x in\nx)\ncat > f.md <<'EOF'\nbody\nEOF\n;;\nesac\n",
        "cat > a.md <<'A'; bash <<'B'\na\nA\nb\nB\n",
    ],
)
def test_many_heredocs_are_read_in_linear_time(assert_linear, construct: str) -> None:
    assert_linear(
        shell_runs, shell_heredoc_data.strip_data_heredoc_bodies, lambda size: construct * size, 500
    )


@pytest.mark.parametrize("tail", [" `x`", " 'x", ' "x', " 'q' 'x"])
def test_heredocs_on_a_line_that_does_not_tokenize_are_read_in_linear_time(
    assert_linear, tail: str
) -> None:
    # a backtick or a quote the line does not close: each heredoc's command is read from the
    # line read once, not the line once per heredoc
    def build(size: int) -> str:
        openers = " ".join(f"<<A{idx}" for idx in range(size))
        return f"cat {openers}{tail}\n" + "".join(f"x\nA{idx}\n" for idx in range(size)) + "ls"

    assert_linear(shell_runs, shell_runs._heredoc_feeds, build, 400)


def test_heredocs_between_backticks_on_one_line_are_read_in_linear_time(assert_linear) -> None:
    def build(size: int) -> str:
        openers = " `x` ".join(f"<<A{idx}" for idx in range(size))
        return f"cat {openers}\n" + "".join(f"x\nA{idx}\n" for idx in range(size)) + "ls"

    assert_linear(shell_runs, shell_runs._heredoc_feeds, build, 1600)


def test_linear_timing_reads_the_shared_heredoc_reader_cold(assert_linear) -> None:
    # the reader's cache lives in shell_heredoc_data: a warm one would time a lookup at both sizes
    cache = sys.modules[
        shell_heredoc_data.strip_data_heredoc_bodies.__module__
    ]._cached_data_heredocs
    sizes = []

    def run(text: str) -> None:
        sizes.append(cache.cache_info().currsize)
        shell_heredoc_data.strip_data_heredoc_bodies(text)

    assert_linear(shell_runs, run, lambda size: "cat > f.txt <<'EOF'\nbody\nEOF\n" * size, 40)
    assert len(sizes) >= 4 and not any(sizes)
