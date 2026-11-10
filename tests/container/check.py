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
itself, which a `cd ~` names). A file of the
author's own layout under the config folder (a `memory/` file) or any other folder fails it.
Paths are found by PATH: a word starting with /, ~ or $HOME, not preceded by a word character, a
colon, a slash, a glob character or a quote joined on with + (so `and/or`, URLs, `*/x.md` and
`dst + '/x.md'` are not paths). The run found every file it named: a Read call that came back
with no such file fails it, and so does a path a tool call names in the plugin's folder or the
config folder that does not exist once the run
ends, however it was opened (a shell `cat ... 2>/dev/null` reports nothing). Absence is expected,
so not a failure, in self-optimize's folder (a first run finds no ledger), under `projects/` (a
project may have no memory yet), at a CONFIG_PARTS location itself, which a skill may probe, and
for a path holding a glob or a variable, which names no single file.

Exit codes: 0 every check passes; 1 a check fails; 2 invalid arguments or no transcript for the
session.
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HOME = Path("/home/dev")
PATH = re.compile(
    r"(?<![\w.:/$}*?\]-])(?<!\+')(?<!\+\")(?<!\+ ')(?<!\+ \")"
    r"((?:~|\$HOME|\$\{HOME\})(?=/|$)[^\s'\"`;|&<>(),]*|/[\w.@+-][^\s'\"`;|&<>(),]*)"
)
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
MISSING = ("does not exist", "no such file")
# Characters a shell expands, so a path holding one names no single file.
EXPANDED = set("*?[{$")


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
            for text in strings(tool_input):
                for word in PATH.findall(text):
                    path = resolve(word.rstrip(".:"))
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
            for text in strings(tool_input):
                for word in PATH.findall(text):
                    if not allowed(config_dir, plugins, resolve(word.rstrip(".:")), parts):
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
