#!/usr/bin/env python3
"""Check one container run of /agent-harness:self-optimize.

    python3 check.py CONFIG_DIR SESSION_ID RUN_OUTPUT

Inputs: the run's config folder, its session id, and the stream-json output of `claude -p`
(RUN_OUTPUT), whose init record names the folder each plugin runs from. Reads the session's
transcript under CONFIG_DIR/projects/ and the transcripts of the subagents it started.
Outputs: one line per check on stdout, each followed by what failed it.

Three checks. The run reached step 7: a non-empty `self-optimize/<date>-evals/` folder exists.
The run stayed inside the install: every absolute or home-relative path in a tool call's input
is the config folder itself or lies under an allowed root (allowed: inside the config folder,
only what Claude Code itself keeps or loads there (CONFIG_PARTS) plus self-optimize's own folder,
even when the config folder sits under another allowed root; outside it, the folders the plugins
run from, the index cache, the working folder, /tmp, the system folders and the home folder
itself, which a `cd ~` names or an `ls -d ~/*/` lists the folders of). A file of the
author's own layout under the config folder (a `memory/` file) or any other folder fails it.
Paths are found by PATH: a word starting with /, ~ or $HOME, not preceded by a word character, a
colon, a slash, a glob character or a quote joined on with + (so `and/or`, URLs, `*/x.md` and
`dst + '/x.md'` are not paths). Two more things are text, not paths: a string that ends in an
ellipsis and repeats the start of a longer string in the same call (a tool's preview of its own
message, cut wherever it fell), and a word joined to a closed code span that names nothing on
disk (`make test`/slow). In a Bash command a backtick also closes a command substitution, whose
next word is a path, so there the span must be prose: its backtick escaped, or the word inside the
body of a heredoc with a quoted delimiter. PATH needs a name after the slash, so the disk root
on its own is judged apart, from the Bash command alone: a `/` is outside when, read as the shell
reads it (quotes, continuations, comments, heredoc bodies), it is a folder a walking command
(WALKERS) starts from, as in `find / -name x` or `du -sh "/"`. A `/` that a walker reads as its
pattern or test (`grep -r / tests`, `find . -name /`), or that any other command or prose holds,
is a division or text. The run found
every file it named: a Read call that came back with no such file fails it, and so does a path a
tool call names in the plugin's folder or the config folder that does not exist once the run
ends, however it was opened (a shell `cat ... 2>/dev/null` reports nothing). Absence is expected,
so not a failure, in self-optimize's folder (a first run finds no ledger), under `projects/` (a
project may have no memory yet), at a CONFIG_PARTS location itself, which a skill may probe, and
for a path holding a glob or a variable, which names no single file.

Exit codes: 0 every check passes; 1 a check fails; 2 invalid arguments or no transcript for the
session.
"""

import json
import os
import re
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HOME = Path("/home/dev")
PATH = re.compile(
    r"(?<![\w.:/$}*?\]-])(?<!\+')(?<!\+\")(?<!\+ ')(?<!\+ \")"
    r"((?:~|\$HOME|\$\{HOME\})(?=/|$)[^\s'\"`;|&<>(),]*|/[\w.@+-][^\s'\"`;|&<>(),]*)"
)
# A stand-in such as <case-name> in prose: what follows it is the rest of a relative path. In a
# Bash command the same characters can end a redirect, so only other tools' text skips it, and a
# path closed by a tag (<file>/a/b</file>) is the tag's content, not a stand-in's.
PLACEHOLDER = re.compile(r"<[\w-]+>$")
# One name at the disk root that is not there is text, not a path: a closing tag (</p>), the
# file after a stand-in (<case>/prompt.md). A path from another machine has a folder in it and is
# still judged, and so is a name the shell expands (/et[c]).
ROOT = Path("/")
# The script sed is given: its slashes separate patterns (/needle/p) and name no path. Two things
# in a sed call do name one: the script file after -f, and the file of an r or w command.
SED_SCRIPT = re.compile(r"""(\bsed\b(?:\s+-(?!\w*f\b)\w+)*\s+)('[^']*'|"[^"]*")""")
SED_FILE = re.compile(r"[rw]\s+/")
SYSTEM = ("/tmp", "/usr", "/bin", "/dev/null", "/proc/self")
# What Claude Code keeps or loads in its config folder that a skill may read or write: the
# installed plugins, the transcripts (with each project's memory), the user's own instructions,
# rules and settings.
CONFIG_PARTS = (
    "plugins",
    "projects",
    "CLAUDE.md",
    "rules",
    "settings.json",
    "skills",
    "agents",
    "commands",
)
# The config folder parts a plugin run may use: Claude Code's own and self-optimize's folder.
PLUGIN_PARTS = (*CONFIG_PARTS, "self-optimize")
# What ends one command in a Bash call's text, a command substitution included.
SEPARATORS = (";", "&", "|", "\n", "(", ")", "`")
# The ls options that only shape a listing of names and take no value.
NAME_FLAGS = set("adlhFp1")
MISSING = ("does not exist", "no such file")
# The delimiters prose wraps a path in. Written into a heredoc or a double-quoted string each is
# escaped (\`/home/dev/shop\`), and the backslash before the closing one is then the shell's, not
# the path's. A backslash with no such pair around the word stays: it can be part of the name
# (an escaped space, or an escaped quote that the file's name ends with).
ESCAPED = ("`", '"')
# Characters a shell expands, so a path holding one names no single file.
EXPANDED = set("*?[{$")
# What a tool ends a preview with when it cuts a longer text short.
CUT = "…"
# Commands that descend into the folder they are given.
WALKERS = frozenset({"find", "fd", "du", "tree", "grep", "rg"})
# Per walker, the options whose value is the next word; the value is never a starting folder.
VALUED = {
    "find": frozenset({"-D"}),
    "fd": frozenset(
        {"-e", "--extension", "-t", "--type", "-d", "--max-depth", "--min-depth", "--exact-depth"}
        | {"-E", "--exclude", "-x", "--exec", "-X", "--exec-batch", "-S", "--size", "-j"}
        | {"--threads", "--changed-within", "--changed-before", "-o", "--owner"}
        | {"--search-path", "--base-directory"}
    ),
    "du": frozenset({"-d", "--max-depth", "-B", "--block-size", "-t", "--threshold", "-X"}),
    "tree": frozenset({"-L", "-P", "-I", "-o", "-H", "-T", "--filelimit", "--charset"}),
    "grep": frozenset(
        {"-e", "--regexp", "-f", "--file", "-m", "--max-count", "-A", "-B", "-C", "-d", "-D"}
    ),
    "rg": frozenset(
        {"-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T"}
        | {"--type-not", "-m", "--max-count", "-A", "-B", "-C", "-j", "--threads", "-M"}
        | {"--max-columns", "-d", "--max-depth", "-r", "--replace", "--sort", "--sortr", "-E"}
        | {"--encoding", "--pre", "--pre-glob", "--type-add", "--color", "--colors"}
    ),
}
# Walkers whose first operand is a pattern, unless an option below gives the pattern instead.
PATTERN_FIRST = frozenset({"fd", "grep", "rg"})
PATTERN_OPTIONS = {
    "grep": frozenset({"-e", "--regexp", "-f", "--file"}),
    "rg": frozenset({"-e", "--regexp", "-f", "--file"}),
}
# Options whose value is a folder the walker starts from.
FOLDER_OPTIONS = {"fd": frozenset({"--search-path", "--base-directory"})}
# Words before a command that run it rather than name it.
WRAPPERS = frozenset({"sudo", "env", "command", "exec", "time", "nohup", "nice", "xargs"})
KEYWORDS = frozenset({"if", "then", "elif", "else", "while", "until", "do", "!", "{"})
ASSIGNMENT = re.compile(r"\w+=")
# Any heredoc with its body (group 3): the body is text the command passes on, not commands.
ANY_HEREDOC = re.compile(r"""<<-?\s*(['"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)""", re.DOTALL)
# A heredoc whose delimiter is quoted, so the shell expands nothing in its body (group 3).
HEREDOC = re.compile(r"""<<-?\s*(['"])(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*(?:\n|$)""", re.DOTALL)


def plugin_roots(run_output: Path) -> list[Path]:
    """The folders the plugins ran from, as the init record of the run's output names them."""
    for line in run_output.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("subtype") == "init":
            plugins = record.get("plugins") or []
            return [Path(p["path"]) for p in plugins if p.get("path", "").startswith("/")]
    return []


def allowed(
    config_dir: Path, plugins: list[Path], path: Path, parts: tuple[str, ...] = PLUGIN_PARTS
) -> bool:
    """Whether *path* is the config folder, the home folder or lies under an allowed root. A path
    inside the config folder is judged by *parts* alone, wherever the config folder itself sits."""
    if path in (config_dir, HOME):
        return True
    if config_dir in path.parents:
        roots = [config_dir / part for part in parts]
    else:
        roots = [*plugins, HOME / ".cache" / "agent-harness", HOME / "shop", *map(Path, SYSTEM)]
    return any(path == root or root in path.parents for root in roots)


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def texts(tool_input: Any) -> list[str]:
    """Each string in a tool call's input, without the previews: a string that ends in CUT and
    whose text before it starts a string of the same call that is not cut short itself."""
    found = list(strings(tool_input))
    return [
        text
        for text in found
        if not (
            text.endswith(CUT)
            and any(not other.endswith(CUT) and other.startswith(text[:-1]) for other in found)
        )
    ]


def blocks(path: Path) -> Iterator[dict[str, Any]]:
    """Each content block of each message in the transcript at *path*."""
    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
        yield from (block for block in content or [] if isinstance(block, dict))


def tool_calls(path: Path) -> Iterator[tuple[str, Any]]:
    """(tool name, input) for each tool call recorded in the transcript at *path*."""
    for block in blocks(path):
        if block.get("type") == "tool_use":
            yield str(block.get("name")), block.get("input")


def missing_reads(config_dir: Path, transcripts: list[Path]) -> list[str]:
    """The files Read calls asked for and did not find, outside self-optimize's folder."""
    asked: dict[str, str] = {}
    missing: dict[str, None] = {}
    for transcript in transcripts:
        for block in blocks(transcript):
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                asked[str(block.get("id"))] = str((block.get("input") or {}).get("file_path"))
            elif block.get("type") == "tool_result" and block.get("is_error"):
                file = asked.get(str(block.get("tool_use_id")))
                said = " ".join(strings(block.get("content"))).lower()
                if file and any(words in said for words in MISSING):
                    path = resolve(file)
                    if config_dir / "self-optimize" not in path.parents:
                        missing[file] = None
    return list(missing)


def absent(config_dir: Path, plugins: list[Path], transcripts: list[Path]) -> list[str]:
    """The paths tool calls named in the plugins' folders or the config folder that do not exist,
    outside the places where absence is expected."""
    expected = [config_dir / "projects", config_dir / "self-optimize"]
    probed = {config_dir / part for part in CONFIG_PARTS}
    found: dict[str, None] = {}
    for transcript in transcripts:
        for _, tool_input in tool_calls(transcript):
            for text in texts(tool_input):
                for match in PATH.finditer(text):
                    word = match.group(1)
                    path = resolve(named(match))
                    in_config = config_dir in path.parents and path not in probed
                    in_plugin = any(root in path.parents for root in plugins)
                    if (
                        (in_config or in_plugin)
                        and not EXPANDED & set(str(path))
                        and not any(root == path or root in path.parents for root in expected)
                        and not path.exists()
                    ):
                        found[word] = None
    return list(found)


def resolve(word: str) -> Path:
    for prefix in ("${HOME}", "$HOME", "~"):
        if word.startswith(prefix):
            return HOME / word[len(prefix) :].lstrip("/")
    return Path(word)


def named(match: re.Match[str]) -> str:
    """The path a matched word names: without the backslash of the escaped delimiter that closes
    it, when the same escaped delimiter opens it, and without the sentence punctuation that ends
    it."""
    word, text = match.group(1), match.string
    closing = text[match.end() : match.end() + 1]
    wrapped = closing in ESCAPED and text[: match.start(1)].endswith("\\" + closing)
    if wrapped and word.endswith("\\"):
        word = word[:-1]
    return word.rstrip(".:")


def lists_names(match: re.Match[str]) -> bool:
    """Whether the command a matched word sits in only lists names: an `ls` whose options are all
    among NAME_FLAGS and include `-d`, which prints what the word expands to and opens none of it.
    Any other option (one that recurses, or takes the next word as its value) leaves the word to
    the path check."""
    text = match.string
    start = max(text.rfind(mark, 0, match.start()) for mark in SEPARATORS) + 1
    ends = [at for mark in SEPARATORS if (at := text.find(mark, match.end())) != -1]
    command = text[start : min(ends, default=len(text))].split()
    options = [word[1:] for word in command[1:] if word.startswith("-")]
    return (
        command[:1] == ["ls"]
        and all(option and set(option) <= NAME_FLAGS for option in options)
        and any("d" in option for option in options)
    )


def joined(match: re.Match[str], path: Path, shell: bool) -> bool:
    """Whether a matched word is joined to a code span that closes just before it, and names
    nothing on disk: the backtick before it is the second of a pair on its line. In a *shell*
    command the backtick must be escaped or sit in a quoted heredoc's body, where it cannot close a
    command substitution."""
    text, at = match.string, match.start(1)
    line = text[text.rfind("\n", 0, at) + 1 : at]
    if not line.endswith("`") or line.count("`") % 2 or path.exists():
        return False
    return (
        not shell
        or line.endswith("\\`")
        or any(body.start(3) <= at < body.end(3) for body in HEREDOC.finditer(text))
    )


def end_unquoted_lines(command: str) -> str:
    """The command with each newline outside quotes turned into `;`, so that it ends a command
    the way the shell ends one, and each backslash-newline continuation joined into one line."""
    out, quote, escaped = [], "", False
    for char in command:
        if escaped:
            out.append(" " if char == "\n" else "\\" + char)
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = "" if char == quote else quote
            out.append(char)
        elif char in "'\"":
            quote = char
            out.append(char)
        else:
            out.append(";" if char == "\n" else char)
    return "".join(out)


def simple_commands(command: str) -> Iterator[list[str]]:
    """The words of each command in a Bash call as the shell reads them, command substitutions
    included, with heredoc bodies, comments and redirection targets left out."""
    command = ANY_HEREDOC.sub(lambda body: body.group(0).split("\n", 1)[0], command)
    command = end_unquoted_lines(command).replace("$(", "; (").replace("`", " ; ")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    words: list[str] = []
    redirected = False
    for token in tokens:
        if redirected:
            redirected = False
        elif token and set(token) <= set("<>&") and set(token) & set("<>"):
            redirected = True
        elif token and set(token) <= set(";&|()"):
            if words:
                yield words
            words = []
        else:
            words.append(token)
    if words:
        yield words


def starting_folders(walker: str, args: list[str]) -> list[str]:
    """The folders a walker would start from, given its arguments."""
    if walker == "find":
        rest = list(args)
        while rest and (rest[0] in ("-H", "-L", "-P") or rest[0].startswith("-O")):
            rest.pop(0)
        if rest[:1] == ["-D"]:
            rest = rest[2:]
        folders = []
        for word in rest:
            if word.startswith("-") or word in ("(", "!", ")", ","):
                break
            folders.append(word)
        return folders
    words = iter(args)
    operands: list[str] = []
    named: list[str] = []
    pattern_given = False
    for word in words:
        if word == "--":
            operands += list(words)
            break
        if not word.startswith("-") or word == "-":
            operands.append(word)
            continue
        name, has_value, value = word.partition("=")
        if not has_value and name in VALUED[walker]:
            value = next(words, "")
        pattern_given = pattern_given or name in PATTERN_OPTIONS.get(walker, ())
        if name in FOLDER_OPTIONS.get(walker, ()):
            named.append(value)
    if walker in PATTERN_FIRST and not pattern_given:
        operands = operands[1:]
    return operands + named


def walks_the_disk(command: str) -> bool:
    """Whether a Bash command gives the disk root to a command that walks folders."""
    for words in simple_commands(command):
        while words and (
            words[0] in WRAPPERS or words[0] in KEYWORDS or ASSIGNMENT.match(words[0])
        ):
            wrapper = words.pop(0)
            while wrapper in WRAPPERS and words and words[0].startswith("-"):
                words.pop(0)
        if words and os.path.basename(words[0]) in WALKERS:
            if "/" in starting_folders(os.path.basename(words[0]), words[1:]):
                return True
    return False


def unscripted(match: re.Match[str]) -> str:
    """A sed call without its script, unless the script names a file to read or write."""
    return match.group(0) if SED_FILE.search(match.group(2)) else match.group(1)


def outside(
    config_dir: Path,
    plugins: list[Path],
    transcripts: list[Path],
    parts: tuple[str, ...] = PLUGIN_PARTS,
) -> list[tuple[str, str]]:
    """(tool, path) for each path a tool call used outside the allowed roots, each pair once."""
    found: dict[tuple[str, str], None] = {}
    for transcript in transcripts:
        for tool, tool_input in tool_calls(transcript):
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if tool == "Bash" and isinstance(command, str) and walks_the_disk(command):
                found[(tool, "/")] = None
            for text in texts(tool_input):
                if tool == "Bash":
                    text = SED_SCRIPT.sub(unscripted, text)
                for match in PATH.finditer(text):
                    word = match.group(1)
                    tagged = text.startswith("</", match.end())
                    if tool != "Bash" and not tagged and PLACEHOLDER.search(text, 0, match.start()):
                        continue
                    path = resolve(named(match))
                    if path.parent == ROOT and not EXPANDED & set(word) and not path.exists():
                        continue
                    if tool == "Bash" and path == HOME / "*" and lists_names(match):
                        continue
                    if joined(match, path, tool == "Bash"):
                        continue
                    if not allowed(config_dir, plugins, path, parts):
                        found[(tool, word)] = None
    return list(found)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: check.py CONFIG_DIR SESSION_ID RUN_OUTPUT", file=sys.stderr)
        return 2
    config_dir, session, run_output = Path(argv[0]), argv[1], Path(argv[2])
    mains = list((config_dir / "projects").glob(f"*/{session}.jsonl"))
    if not mains or not run_output.is_file():
        print(f"no transcript for session {session} in {config_dir}/projects", file=sys.stderr)
        return 2
    transcripts = mains + list((config_dir / "projects").glob(f"*/{session}/subagents/*.jsonl"))
    evals = [d for d in (config_dir / "self-optimize").glob("*-evals") if any(d.iterdir())]
    plugins = plugin_roots(run_output)
    stray = outside(config_dir, plugins, transcripts)
    missing = list(dict.fromkeys(missing_reads(config_dir, transcripts)))
    missing += [word for word in absent(config_dir, plugins, transcripts) if word not in missing]
    print(f"reached step 7: {'yes, ' + evals[0].name if evals else 'no (no eval suite written)'}")
    print(f"paths outside the install: {len(stray)} in {len(transcripts)} transcripts")
    for tool, word in stray:
        print(f"  {tool}: {word}")
    print(f"files named that do not exist: {len(missing)}")
    for file in missing:
        print(f"  {file}")
    return 0 if evals and not stray and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
