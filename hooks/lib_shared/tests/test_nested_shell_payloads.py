"""Nested shell payloads, and the messages strip_commit_message blanks.

A `bash|sh|zsh -c` payload is read the way the shell reads it, inside a double-quoted word
too, and ANSI-C `$'...'` quoting is decoded. The message of a commit, a tag or a gh command
is blanked only where the shell reads it as inert text of a command it runs itself, and in
its own word only; a wrapper, a group, an `eval` or a word the shell re-reads keeps it as
written. Each reader here runs in time linear in the text.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import shell_runs  # noqa: E402
import shell_tokens  # noqa: E402


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        # a shell inside a double-quoted word runs it after its `\$`, `\"` and `\\` decode
        (
            'ssh host "bash -c \'F=\\$(grep -l x a.json) && python3 -c \\"print(1)\\" \\\\\'"',
            'F=$(grep -l x a.json) && python3 -c "print(1)" \\',
        ),
        ('ssh host "bash -c \\"echo hi\\""', "echo hi"),
        # a `$(...)` body in that word starts its quotes over, and keeps its escapes
        ('echo "$(bash -c \'echo \\"x\\"\')"', 'echo \\"x\\"'),
    ],
)
def test_walked_payload_is_read_as_its_double_quotes_decode_it(command, payload):
    assert shell_runs._shell_payloads_in(command) == [payload]


def test_message_spelled_in_parts_is_not_blanked():
    # the message's value is not in its word as written: the copy outside it is a command's
    command = "git commit -m 'te'\"rra\" ; echo terra"
    assert shell_runs.strip_commit_message(command) == command


@pytest.mark.parametrize(
    ("command", "blanked"),
    [
        # the value `"` is also the word's own quotes: a cut there would leave one unbalanced
        ('git commit -am "\\"" && ls', 'git commit -am " " && ls'),
        # the glued value `m` is the end of `-mm`, not its option letter
        ("git commit -mm && ls", "git commit -m_ && ls"),
        # an unquoted message stays a word, so the words after it keep their places
        ("git commit -m m && ls", "git commit -m _ && ls"),
        ("git commit -m'm' && ls", "git commit -m' ' && ls"),
        # an escape in double quotes is cut with its text as a whole, never split from its backslash
        ('git commit -m "a\\\\b" && ls', 'git commit -m " " && ls'),
        ('git commit -m "\\$ block" && ls', 'git commit -m " " && ls'),
        # a `$'...'` string is not text as written
        ("git commit -m $'a\\nb' && ls", "git commit -m $'a\\nb' && ls"),
        # single quotes hold a backslash as it is
        ("git commit -m 'a\\b' && ls", "git commit -m ' ' && ls"),
    ],
)
def test_message_is_cut_only_where_its_word_holds_it_as_written(command, blanked):
    assert shell_runs.strip_commit_message(command) == blanked


def test_message_is_blanked_in_its_own_word_only():
    assert (
        shell_runs.strip_commit_message('git commit -m "feat: x" && ls')
        == 'git commit -m " " && ls'
    )
    # a copy of it in another command stays: the word is cut, not every occurrence of its text
    command = 'git commit -m "feat: x" && echo "feat: x"'
    assert shell_runs.strip_commit_message(command) == 'git commit -m " " && echo "feat: x"'


# The messages of a commit are blanked wherever the commit reader finds a commit the shell
# runs as its own command: behind assignments or by path, however its `-m` is spelled, and
# for each commit of a list.
@pytest.mark.parametrize(
    "command",
    [
        'X=1 git commit -m "feat: x" && ls',
        'A=1 B=2 git commit -m "feat: x" && ls',
        '/usr/bin/git commit -m "feat: x" && ls',
        'X=1 /usr/bin/git commit -m "feat: x" && ls',
        'git commit -am "feat: x" && ls',
        'git commit -qm "feat: x" && ls',
        'git commit -m"feat: x" && ls',
        'git commit --message="feat: x" && ls',
        "X=1 git commit -m \"$(cat <<'EOF'\nfeat: x\nEOF\n)\" && ls",
    ],
)
def test_message_of_a_commit_the_shell_runs_is_blanked(command):
    assert shell_runs.strip_commit_message(command) == command.replace("feat: x", " ")


@pytest.mark.parametrize(
    ("command", "blanked"),
    [
        ('git commit -m "feat: x" -m "body: y" && ls', 'git commit -m " " -m " " && ls'),
        (
            'git commit -m "feat: x" && X=1 git commit -m "fix: y"',
            'git commit -m " " && X=1 git commit -m " "',
        ),
        # a paragraph that runs code stays whole
        ('git commit -m "feat: x" -m "$(ls)" && ls', 'git commit -m " " -m "$(ls)" && ls'),
    ],
)
def test_each_message_of_each_commit_is_blanked(command, blanked):
    assert shell_runs.strip_commit_message(command) == blanked


@pytest.mark.parametrize(
    "command",
    [
        # a wrapper, a group or an `eval` is not the shell running git: its words may be re-read
        'env git commit -m "feat: x"',
        'sudo git commit -m "feat: x"',
        '{ git commit -m "feat: x"; }',
        'watch git commit -m "feat: x; terraform apply"',
        'eval git commit -m "feat: x"',
        # a word that is not the commit's own
        'X=1 echo git commit -m "feat: x"',
        'sudo -u git git commit -m "feat: x"',
    ],
)
def test_message_of_a_commit_the_shell_does_not_run_itself_stays(command):
    assert shell_runs.strip_commit_message(command) == command


def test_payload_quoted_twice_keeps_its_messages():
    # the copy blanked might not be the payload's own (here python reads it)
    command = """bash -c 'git commit -m "docs: x"'; echo 'git commit -m "docs: x"' | python3"""
    assert shell_runs.strip_commit_message(command) == command


@pytest.mark.parametrize(
    "command",
    [
        # ssh joins its unquoted words into the payload: the quoted word is cat's
        "ssh host git commit -m rel; cat 'git commit -m rel'",
        "ssh host git commit -m '\"rel\"'; cat 'git commit -m \"rel\"'",
    ],
)
def test_a_word_that_only_equals_a_payload_keeps_its_text(command):
    assert shell_runs.strip_commit_message(command) == command


def test_many_payload_messages_are_blanked_in_linear_time(assert_linear):
    def build(size):
        return "".join(f"""bash -c 'git commit -m "feat: m{n:05}"' ; """ for n in range(size))

    assert_linear(shell_runs, shell_runs.strip_commit_message, build, 1000)
    assert (
        shell_runs.strip_commit_message(build(1000)) == """bash -c 'git commit -m " "' ; """ * 1000
    )


def test_many_payload_messages_are_blanked_with_two_reads_of_the_text(monkeypatch):
    # the timing above cannot tell a whole-text pass per payload from load on the machine:
    # the text is read for its payloads twice, however many there are (to find them, then to
    # check the blanked ones stand in their place), and a payload with no git or gh after a
    # quote is not read again
    for value in vars(shell_runs).values():
        if callable(getattr(value, "cache_clear", None)):
            value.cache_clear()
    reads: list[str] = []
    read = shell_runs._payloads_in
    monkeypatch.setattr(
        shell_runs,
        "_payloads_in",
        lambda text, over_ssh: reads.append(text) or read(text, over_ssh),
    )
    command = "".join(f"""bash -c 'git commit -m "feat: m{n:05}"' ; """ for n in range(500))
    shell_runs.strip_commit_message(command)
    assert len(reads) == 2
    assert reads[0] == command and len(reads[1]) < len(command)


def test_many_tag_and_gh_messages_are_blanked_in_linear_time(assert_linear):
    unit = (
        'X=1 git tag -m "m" v1 ; X=1 gh pr create --title t --body "b" ; '
        'gh -R o/r issue comment 1 --body "b" ; git status ; '
    )

    assert_linear(shell_runs, shell_runs.strip_commit_message, lambda size: unit * size, 1000)
    assert shell_runs.strip_commit_message(unit * 1000) == (
        'X=1 git tag -m " " v1 ; X=1 gh pr create --title _ --body " " ; '
        'gh -R o/r issue comment 1 --body " " ; git status ; ' * 1000
    )


# The options of a gh command that take free text (_GH_TEXT_OPTIONS, by the command's path): a
# title, body, notes, subject or description. Each is blanked, `a b` standing for its value.
GH_TEXT_COMMANDS = [
    "gh pr create --title 'a b' --body 'a b'",
    "gh pr new -t 'a b' -b 'a b'",
    "gh pr edit 5 -t 'a b' --body 'a b'",
    "gh pr merge 5 --subject 'a b' -b 'a b'",
    "gh pr merge 5 -t 'a b' --body 'a b'",
    "gh pr comment 5 -b 'a b'",
    "gh pr review 5 --body 'a b'",
    "gh pr revert 5 -t 'a b' -b 'a b'",
    "gh issue create --title 'a b' -b 'a b'",
    "gh issue new -t 'a b' --body 'a b'",
    "gh issue edit 5 -t 'a b' -b 'a b'",
    "gh issue comment 5 --body 'a b'",
    "gh release create v1 -n 'a b' -t 'a b'",
    "gh release new v1 --notes 'a b' --title 'a b'",
    "gh release edit v1 --notes 'a b' --title 'a b'",
    "gh gist create f.txt -d 'a b'",
    "gh gist new f.txt --desc 'a b'",
    "gh gist edit 5 -d 'a b'",
    "gh label create bug -d 'a b'",
    "gh label edit bug --description 'a b'",
    "gh repo create x -d 'a b'",
    "gh repo new x --description 'a b'",
    "gh repo edit -d 'a b'",
    "gh project copy 1 --title 'a b'",
    "gh project create --title 'a b'",
    "gh project edit 1 --title 'a b' -d 'a b'",
    "gh project item-create 1 --title 'a b' --body 'a b'",
    "gh project item-edit --id I --title 'a b' --body 'a b'",
    "gh gpg-key add k.pub -t 'a b'",
    "gh ssh-key add k.pub --title 'a b'",
    "gh repo deploy-key add k.pub -t 'a b'",
    # behind the global option position, after the path, among other options
    "gh pr create -R o/r --draft --title 'a b'",
    # gh takes its repo selector anywhere, the words of the path among them, in every form
    "gh -R o/r pr create --title 'a b'",
    "gh pr -R o/r create --title 'a b'",
    "gh -Ro/r issue comment 5 --body 'a b'",
    "gh --repo o/r pr comment 5 -b 'a b'",
    "gh --repo=o/r issue edit 5 -t 'a b' -b 'a b'",
]


# The same option words where the command's table entry does not name them: another command
# (an extension, an unknown subcommand), a secret or variable value, a `-t` that names a
# template or a team, an option the command does not have, and a path that does not lead.
# An option other than the repo selector before the path breaks it: this reader cannot tell
# whether the next word is its value or the command.
GH_OTHER_COMMANDS = [
    "gh foo --title 'a b'",
    "gh extension exec foo --title 'a b' -b 'a b'",
    "gh secret set K -b 'a b'",
    "gh variable set K --body 'a b'",
    "gh repo create x -t 'a b'",
    "gh project edit 1 -t 'a b'",
    "gh pr view 5 --title 'a b'",
    "gh pr merge 5 --title 'a b'",
    "gh pr comment 5 -t 'a b'",
    "gh issue close 5 --comment 'a b'",
    "gh pr --title 'a b' create",
    "gh --jq .x pr create --title 'a b'",
    "gh pr --jq .x create --title 'a b'",
]


@pytest.mark.parametrize("command", GH_TEXT_COMMANDS)
def test_gh_text_option_of_a_command_that_takes_it_is_blanked(command):
    blanked = shell_runs.strip_commit_message(command)
    assert blanked == command.replace("'a b'", "' '")


@pytest.mark.parametrize("command", GH_OTHER_COMMANDS)
def test_gh_option_of_a_command_that_does_not_take_text_stays(command):
    assert shell_runs.strip_commit_message(command) == command


# `git commit` and `git tag` store a `--trailer` as text beside their message (`git commit -h`,
# `git tag -h`): it is blanked with the message, but it is no paragraph of it. --author and
# --date are parsed by git, and merge, notes and stash have no --trailer.
@pytest.mark.parametrize(
    ("command", "blanked"),
    [
        ("git commit -m 'feat: x' --trailer 'Note: a b'", "git commit -m ' ' --trailer ' '"),
        ("git commit --trailer 'Note: a b' -m 'feat: x'", "git commit --trailer ' ' -m ' '"),
        ("git commit -m 'feat: x' --trailer='Note: a b'", "git commit -m ' ' --trailer=' '"),
        (
            "git commit -m 'a b' --trailer 'N: a b' --trailer 'M: a b'",
            "git commit -m ' ' --trailer ' ' --trailer ' '",
        ),
        ("git tag -a v1 -m 'a b' --trailer 'Note: a b'", "git tag -a v1 -m ' ' --trailer ' '"),
        # only a trailer: the message comes from the editor
        ("git commit --trailer 'Note: a b'", "git commit --trailer ' '"),
        (
            "git commit -m 'feat: x' --trailer \"$(cat <<EOF\nNote: a b\nEOF\n)\"",
            "git commit -m 'feat: x' --trailer \"$(cat <<EOF\n \nEOF\n)\"",
        ),
        (
            "git commit --trailer \"$(cat <<'EOF'\nNote: a b\nEOF\n)\"",
            "git commit --trailer \"$(cat <<'EOF'\n \nEOF\n)\"",
        ),
        # not text
        ("git commit -m 'a b' --author 'a b <c@d>'", "git commit -m ' ' --author 'a b <c@d>'"),
        ("git commit -m 'a b' --date 'a b'", "git commit -m ' ' --date 'a b'"),
        ("git merge -m 'a b' --trailer 'a b' topic", "git merge -m ' ' --trailer 'a b' topic"),
        ("git notes add -m 'a b' --trailer 'a b'", "git notes add -m ' ' --trailer 'a b'"),
    ],
)
def test_trailer_of_a_commit_or_tag_is_blanked_with_the_message(command, blanked):
    assert shell_runs.strip_commit_message(command) == blanked


def test_many_commits_are_blanked_in_linear_time(assert_linear):
    # each commit is read once and its messages are looked up in its own words, however many
    # follow
    def build(size):
        return "".join(f'X=1 git commit -m "feat: m{n:05}" ; ' for n in range(size))

    assert_linear(shell_runs, shell_runs.strip_commit_message, build, 2000)
    assert shell_runs.strip_commit_message(build(2000)) == 'X=1 git commit -m " " ; ' * 2000


def test_many_heredoc_commits_are_blanked_in_linear_time(assert_linear):
    # the heredocs of the text are listed once, not again for every commit that asks
    def build(size):
        return "".join(
            f"git commit -m \"$(cat <<'EOF'\nfeat: m{n:05}\nEOF\n)\" ;\n" for n in range(size)
        )

    assert_linear(shell_runs, shell_runs.strip_commit_message, build, 2000)
    assert (
        shell_runs.strip_commit_message(build(2000))
        == "git commit -m \"$(cat <<'EOF'\n \nEOF\n)\" ;\n" * 2000
    )


@pytest.mark.parametrize(
    "redirection", ["2>&1", ">/dev/null", "&>/dev/null", "2> err.log", ">| out"]
)
def test_message_after_a_redirection_is_blanked(redirection):
    # a redirection is no word of the commit: the `-m` behind it is still its option
    command = f'git commit {redirection} -m "feat: x" && ls'
    assert shell_runs.strip_commit_message(command) == command.replace("feat: x", " ")


def test_redirections_before_a_message_are_read_in_linear_time(assert_linear):
    def build(size):
        return "git commit" + " 2>&1" * size + ' -m "feat: x" && ls'

    assert_linear(shell_runs, shell_runs.strip_commit_message, build, 3000)
    assert shell_runs.strip_commit_message(build(3000)) == build(3000).replace("feat: x", " ")


def test_unquoted_heredoc_body_is_kept_whatever_it_says_of_a_quoted_one():
    # the delimiter is the one on the opening line: a `<<'EOF'` in the body quotes nothing
    command = "git commit -m \"$(cat <<EOF\nfeat: read <<'EOF' bodies\n$(terraform apply)\nEOF\n)\""
    assert shell_runs.strip_commit_message(command) == command


@pytest.mark.parametrize("comment", ["# what's staged", '# say "hi', "# `x"])
def test_comment_before_a_commit_leaves_its_message_blanked(comment):
    command = f"{comment}\ngit commit -m 'fix: x' && ls"
    assert shell_runs.strip_commit_message(command) == command.replace("fix: x", " ")


@pytest.mark.parametrize("quote", ["'", '"', "`"])
def test_quote_in_a_heredoc_body_before_a_commit_leaves_its_message_blanked(quote):
    # the body is text for its consumer: its quote opens nothing in the commit's command
    command = f"cat > notes.txt <<'EOF'\nit{quote}s done\nEOF\ngit commit -m 'fix: x' && ls"
    assert shell_runs.strip_commit_message(command) == command.replace("fix: x", " ")


@pytest.mark.parametrize(
    ("build", "size"),
    [
        # each double-quoted word is decoded once, not again from every shell name in it
        (lambda size: 'ssh h "' + "bash -c 'echo \\\"x\\\"'; " * size + "\"; echo it's", 2000),
        # ... and a word nested in another's `$(...)` once more, its own escapes only
        (lambda size: 'ssh h "' + "bash -c 'x' \\\" $(echo \"" * size + ')"' * size + '"', 300),
    ],
)
def test_shells_in_double_quoted_words_are_read_in_linear_time(assert_linear, build, size):
    assert_linear(shell_runs, shell_runs._shell_payloads_in, build, size)


def test_commit_message_is_blanked_in_linear_time(assert_linear):
    # the commit's message is found once and its words are read once, however many
    # commands follow it
    def build(size):
        return 'git commit -m "feat: x"' + " ; echo y" * size

    assert_linear(shell_runs, shell_runs.strip_commit_message, build, 1000)
    assert shell_runs.strip_commit_message(build(1000)) == 'git commit -m " "' + " ; echo y" * 1000


def test_heredoc_message_is_found_in_linear_time(assert_linear):
    # thousands of openers the shell reads as text in one quoted word, which is split
    # in linear time
    def build(size):
        return "git commit -m '" + "$(cat <<EOF\n" * size + "'"

    assert_linear(shell_runs, shell_runs.strip_commit_message, build, 12000)
    assert shell_runs.strip_commit_message(build(12000)) == build(12000)


@pytest.mark.parametrize(
    ("text", "prefix"),
    [
        # the line starts inside a quoted word opened above: the command starts there
        ('bash -s "a\nb" <<EOF\nx\nEOF', ["bash", "-s", "a\nb"]),
        ("python3 -c \"$(cat <<'A'\nx\nA\n)\" <<B\ny\nB", ["python3", "-c", "$(cat <<'A'\n)"]),
        # the quote opens on the heredoc's line: its command is read from that line
        ("echo a\ngit commit -m \"$(cat <<'EOF'\nfeat: x\nEOF\n)\"", ["cat"]),
    ],
)
def test_heredoc_command_is_read_from_the_line_its_quoted_words_open_on(
    text: str, prefix: list[str]
) -> None:
    assert shell_runs._heredoc_feeds(text)[-1][0] == prefix


@pytest.mark.parametrize(
    ("text", "commands"),
    [
        # a heredoc feeds the command whose redirections it is among; the others are no words of it
        ("cat <<A <<B\na\nA\nb\nB", [["cat"], ["cat"]]),
        ("cat <<A x <<B\na\nA\nb\nB", [["cat", "x"], ["cat", "x"]]),
        ("f() { cat; } <<A <<B\na\nA\nb\nB", [["cat", ";"], ["cat", ";"]]),
        ("cat <<A; (bash) <<B\na\nA\nb\nB", [["cat"], ["bash"]]),
        ("( cat <<A <<B )\na\nA\nb\nB", [["cat"], ["cat"]]),
        ("(cat <<A) <<B\na\nA\nb\nB", [["cat"], ["cat"]]),
        ('bash -c "$(cat <<A <<B\na\nA\nb\nB\n)"', [["cat"], ["cat"]]),
        # the line after both bodies starts a command of its own
        ("cat <<A <<B\na\nA\nit's\nB\n(\nbash ) <<C\nx\nC", [["cat"], ["cat"], ["bash"]]),
        # a backtick in single quotes, in a quoted delimiter or escaped is a character of a word
        ("cat <<'A`B'; (bash) <<C\nx\nA`B\ny\nC", [["cat"], ["bash"]]),
        ('cat <<"A`B"; bash <<C\nx\nA`B\ny\nC', [["cat"], ["bash"]]),
        ("cat <<A 'x`y'; (bash) <<C\nx\nA\ny\nC", [["cat", "x`y"], ["bash"]]),
        ("cat <<A $'x`y'; bash <<C\nx\nA\ny\nC", [["cat", "x`y"], ["bash"]]),
        ("cat <<A x\\`y; bash <<C\nx\nA\ny\nC", [["cat", "x`y"], ["bash"]]),
        ("bash 'x`y' <<C\ny\nC", [["bash", "x`y"]]),
        # one that opens or closes a substitution starts or ends a command, in double quotes too
        ('cat <<A "x`echo y`"; bash <<C\nx\nA\ny\nC', [["cat"], ["bash"]]),
        ("cat <<A `echo y`; bash <<C\nx\nA\ny\nC", [["cat"], ["bash"]]),
    ],
)
def test_heredocs_opened_on_one_line_feed_the_command_they_redirect(
    text: str, commands: list[list[str]]
) -> None:
    assert [feed[0] for feed in shell_runs._heredoc_feeds(text)] == commands


def test_heredocs_on_lines_inside_quoted_words_are_read_in_linear_time(assert_linear):
    # each line starts inside a quoted word the line above opens: the command is read
    # from that line, not from the top
    def build(size):
        return (
            'bash -c "\n' + "".join(f'echo "$(cat <<E{n}\nx\nE{n}\n)"\n' for n in range(size)) + '"'
        )

    assert_linear(shell_runs, shell_runs._heredoc_feeds, build, 400)


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        # ANSI-C quoting is one quoted word with its escapes decoded, as bash reads it
        ("bash -c $'git push origin main'", ["bash", "-c", "git push origin main"]),
        ("printf $'it\\'s\\n'", ["printf", "it's\n"]),
        ("echo $'\\\\ \\\" \\? \\t\\r\\a\\b\\e\\E\\f\\v'", ["echo", '\\ " ? \t\r\a\b\x1b\x1b\f\v']),
        ("echo $'\\x41\\101\\u0042\\U00000043\\cA\\c?'", ["echo", "AABC\x01\x7f"]),
        ("echo a$'b c'd", ["echo", "ab cd"]),
        # a `\u`/`\U` code in the surrogate range is no character: kept as written
        ("echo $'\\ud800 \\U0000DC80'", ["echo", "\\ud800 \\U0000DC80"]),
        # bash ends the word at a NUL; an unknown escape keeps its backslash
        ("echo $'a\\0b' $'\\z'", ["echo", "a", "\\z"]),
        ('echo $"x y"', ["echo", "x y"]),
        # inside quotes, after a backslash, or in a comment `$'` is literal text
        ("echo \"$'x'\" '$'\\''y' \\$'z' # $'it\\'s", ["echo", "$'x'", "$'y", "$z"]),
        ("echo $'it\\'s' # don't", ["echo", "it's"]),
    ],
)
def test_ansi_c_quoting_is_tokenized(text, tokens):
    assert shell_tokens._shell_tokens(text) == tokens


def test_unterminated_ansi_c_quote_cannot_be_tokenized():
    assert shell_tokens._shell_tokens("echo $'it\\'s") is None


# A cat option that changes every line it prints (`-n` numbers each line) or a file operand
# makes the message other text than the body: the body is not read as the message.
@pytest.mark.parametrize(
    "cat",
    [
        "cat -n",
        "cat -b",
        "cat -e",
        "cat -E",
        "cat -A",
        "cat -sn",
        "cat f",
        "cat --number",
        "cat --number-nonblank",
        "cat --show-ends",
        "cat --show-all",
    ],
)
def test_heredoc_message_of_a_cat_that_prints_other_text_is_not_the_body(cat: str) -> None:
    command = f"git commit -m \"$({cat} <<'EOF'\nBad message\nEOF\n)\""
    assert shell_runs.strip_commit_message(command) == command
