#!/usr/bin/env python3
"""Check one container run of the install from a clone: the hooks, rules, skills and scripts.

    python3 check_clone.py CONFIG_DIR OUT_DIR

Inputs: the run's config folder and the folder clone_inside.sh wrote its probes to: one
stream-json output of `claude -p` per probe (`<probe>.jsonl`), `sessions.json` naming the stop
probe's session, the stop flag listings and the scratch repo's git state.
Outputs: one line per check on stdout, `<check>: passed`, `<check>: FAILED, <why>` or
`<check>: not attempted, <why>`.

A probe is a session asked to do one thing a hook, a rule or a skill decides. A hook check passes
only on the hook's own words: three hooks are registered to block a call when they cannot start,
so a refusal alone would also pass an install whose hook crashes. A probe whose session never
made the call the hook judges is not attempted, neither passed nor failed.

Checks:
- bare_git_stash_guard, emoji_guard, stop_gate: the session's tool call came back refused with
  the hook's reason.
- prose_question_blocker: a reply that asked a question in prose was sent back with the hook's
  feedback.
- stop_flag: a bare "stop" left the session's flag, a machine prompt kept it and the next human
  prompt removed it, after which a tool call ran.
- edit_trailing_space_guard, peer_reply_guard: no session can be made to trip them (the model
  drops a trailing space before it calls Edit, and Claude Code drops a typed message from another
  session), so the command the installed settings.json registers is run on a payload.
- commit: `/commit` put the linked skill's text into the session, and the dirty scratch repo was
  left with one new commit with a typed subject and a clean tree.
- rules and scripts: asked to run the tests and report the repo's state, the session ran
  `triage_run.sh` and `git_state.sh`, which only the linked rules name, and each printed its own
  line.
- self-optimize: `/self-optimize` on invented sessions wrote an eval suite, so a linked skill
  that runs a sibling skill's scripts works.
- every session ended normally, used no path outside the install and named no file in it that
  does not exist (check.py's path checks, with the clone and the linked folders allowed).

Exit codes: 0 every check passed; 1 a check failed; 2 invalid arguments or no probe output;
4 no check failed and at least one was not attempted.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import check  # noqa: E402

PASSED, FAILED, SKIPPED = "passed", "FAILED", "not attempted"
Verdict = tuple[str, str]

CLONE = check.HOME / "agent-harness"
# What the install from a clone adds to the config folder, and what its hooks keep there.
CLONE_PARTS = (*check.CONFIG_PARTS, "hooks", "scripts", "state", "self-optimize")
STASH_REASON = "do not run bare `git stash`"
EMOJI_REASON = "this write puts emoji in a doc or text file"
STOP_REASON = "The user typed stop. Make no tool calls."
PROSE_FEEDBACK = "Prose question detected in assistant turn"
TRAILING_REASON = "new_string ends in a space or tab"
PEER_REASON = "no SendMessage was sent"
SUBJECT = re.compile(
    r"(feat|fix|docs|style|refactor|test|chore|perf|build|ci|revert)(\([^)]+\))?: "
)
SKILL_LOADED = "Base directory for this skill: "
TRIAGE_LINE = re.compile(r"\(exit \d+, \d+ lines\) full output: ")
STATE_LINE = re.compile(r"branch=\S+ head=\S+ ")
SESSIONS = (
    "stash",
    "emoji",
    "prose",
    "stop1",
    "stop2",
    "stop3",
    "commit",
    "tests",
    "selfoptimize",
)


def refused(
    output: Path, tool: str, wanted: Callable[[dict[str, Any]], bool], reason: str
) -> Verdict:
    """Whether a call of *tool* whose input *wanted* accepts came back refused with *reason*."""
    asked: dict[str, None] = {}
    said: list[str] = []
    for block in check.blocks(output):
        if block.get("type") == "tool_use" and block.get("name") == tool:
            if wanted(block.get("input") or {}):
                asked[str(block.get("id"))] = None
        elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in asked:
            text = " ".join(check.strings(block.get("content")))
            if block.get("is_error") and reason in text:
                return PASSED, ""
            said.append(text[:200])
    if not asked:
        return SKIPPED, f"the session made no such {tool} call"
    return FAILED, f"the call was not refused with the hook's reason: {said}"


def sent_back(output: Path, feedback: str) -> Verdict:
    """Whether a reply holding a question mark was sent back with *feedback*."""
    asked = False
    for block in check.blocks(output):
        text = str(block.get("text", ""))
        if block.get("type") == "text" and feedback in text:
            return PASSED, ""
        asked = asked or (block.get("type") == "text" and "?" in text)
    if not asked:
        return SKIPPED, "the session's reply asked no question"
    return FAILED, "a reply asked a question and was not sent back"


def ran(output: Path, tool: str) -> Verdict:
    """Whether a call of *tool* ran without an error."""
    asked: dict[str, None] = {}
    for block in check.blocks(output):
        if block.get("type") == "tool_use" and block.get("name") == tool:
            asked[str(block.get("id"))] = None
        elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in asked:
            if not block.get("is_error"):
                return PASSED, ""
    if not asked:
        return SKIPPED, f"the session made no {tool} call"
    return FAILED, f"every {tool} call came back with an error"


def stop_flag(out: Path, session: str) -> Verdict:
    """Whether the flag listings show the flag set, kept and removed."""
    listed = [
        (out / f"flag_after_{step}.txt").read_text().split()
        for step in ("stop", "machine_prompt", "human_prompt")
    ]
    if listed == [[session], [session], []]:
        return PASSED, ""
    return FAILED, f"flags after stop, a machine prompt and a human prompt: {listed}"


def piped(config_dir: Path, event: str, hook: str, payload: dict[str, Any]) -> str:
    """What the command settings.json registers for *hook* prints when given *payload*."""
    settings = json.loads((config_dir / "settings.json").read_text())
    commands = [
        entry["command"]
        for group in settings["hooks"][event]
        for entry in group["hooks"]
        if f"/{hook}/" in entry["command"]
    ]
    if len(commands) != 1:
        return f"{len(commands)} commands registered"
    done = subprocess.run(
        ["bash", "-c", commands[0]],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return f"exit {done.returncode}: {done.stdout.strip()} {done.stderr.strip()[-300:]}"


def trailing_space(config_dir: Path) -> Verdict:
    edit = {"file_path": "/x/b.py", "old_string": "Hello,", "new_string": "Hello, "}
    refusal = piped(config_dir, "PreToolUse", "edit_trailing_space_guard", _edit(edit))
    edit["new_string"] = "Hello, w"
    passing = piped(config_dir, "PreToolUse", "edit_trailing_space_guard", _edit(edit))
    if (
        refusal.startswith("exit 0:")
        and TRAILING_REASON in refusal
        and passing.strip() == "exit 0:"
    ):
        return PASSED, ""
    return FAILED, f"a trailing space gave `{refusal}`, none gave `{passing}`"


def _edit(tool_input: dict[str, str]) -> dict[str, Any]:
    return {"tool_name": "Edit", "tool_input": dict(tool_input)}


def peer_reply(config_dir: Path) -> Verdict:
    fixtures = config_dir / "hooks" / "stop" / "peer_reply_guard" / "tests" / "fixtures"
    said = [
        piped(
            config_dir,
            "Stop",
            "peer_reply_guard",
            {"transcript_path": str(fixtures / f"{name}.jsonl"), "stop_hook_active": False},
        )
        for name in ("unanswered_task", "answered_task")
    ]
    if PEER_REASON in said[0] and said[1].strip() == "exit 0: {}":
        return PASSED, ""
    return FAILED, f"an unanswered task gave `{said[0]}`, an answered one `{said[1]}`"


def committed(config_dir: Path, out: Path) -> Verdict:
    """Whether the commit session was given the linked skill's text and the scratch repo holds
    one commit past its first, typed, on a clean tree."""
    loaded = f"{SKILL_LOADED}{config_dir / 'skills' / 'commit'}"
    session = session_of(out / "commit.jsonl")
    transcripts = list((config_dir / "projects").glob(f"*/{session}.jsonl"))
    if not any(loaded in t.read_text(errors="replace") for t in transcripts):
        return FAILED, f"the session's transcript does not hold `{loaded}`"
    subjects = (out / "git_log.txt").read_text().splitlines()
    dirty = (out / "git_status.txt").read_text().strip()
    if len(subjects) == 2 and SUBJECT.match(subjects[0]) and not dirty:
        return PASSED, ""
    return FAILED, f"commits, newest first: {subjects}; uncommitted: {dirty!r}"


def session_of(output: Path) -> str:
    """The session id a probe's output names, or "" when it names none."""
    for line in output.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("session_id"):
            return str(record["session_id"])
    return ""


def ran_script(output: Path, script: str, line: re.Pattern[str]) -> Verdict:
    """Whether a Bash call naming *script* came back with the *line* that script prints."""
    asked: dict[str, None] = {}
    for block in check.blocks(output):
        if block.get("type") == "tool_use" and block.get("name") == "Bash":
            if script in str((block.get("input") or {}).get("command")):
                asked[str(block.get("id"))] = None
        elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in asked:
            if line.search(" ".join(check.strings(block.get("content")))):
                return PASSED, ""
    if not asked:
        return FAILED, f"no Bash call named {script}"
    return FAILED, f"no Bash call naming {script} came back with the line it prints"


def ended(out: Path) -> Verdict:
    """Whether every probe session's output ends in a result that is no error (a session that
    could not sign in ends in an error result)."""
    bad = []
    for name in SESSIONS:
        last: dict[str, Any] = {}
        for line in (out / f"{name}.jsonl").read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("type") == "result":
                last = record
        if last.get("subtype") != "success" or last.get("is_error"):
            bad.append(f"{name}: {str(last.get('result'))[:120]}")
    return (FAILED, f"{bad}") if bad else (PASSED, "")


def self_optimized(config_dir: Path) -> Verdict:
    """Whether the run left a non-empty eval suite in self-optimize's folder."""
    suites = [d for d in (config_dir / "self-optimize").glob("*-evals") if any(d.iterdir())]
    return (PASSED, "") if suites else (FAILED, "no eval suite written")


def paths(config_dir: Path, out: Path) -> Verdict:
    """check.py's path checks over the probe sessions and the subagents they started (the
    invented sessions self-optimize reads are input, not part of the run)."""
    sessions = dict.fromkeys(session_of(out / f"{name}.jsonl") for name in SESSIONS)
    transcripts = []
    for session in filter(None, sessions):
        transcripts += (config_dir / "projects").glob(f"*/{session}.jsonl")
        transcripts += (config_dir / "projects").glob(f"*/{session}/subagents/*.jsonl")
    roots = [CLONE, check.HOME / ".agents"]
    stray = [
        f"{tool}: {word}"
        for tool, word in check.outside(config_dir, roots, transcripts, CLONE_PARTS)
    ]
    missing = list(dict.fromkeys(check.missing_reads(config_dir, transcripts)))
    missing += [w for w in check.absent(config_dir, roots, transcripts) if w not in missing]
    if not transcripts:
        return FAILED, "no transcripts"
    if stray or missing:
        return FAILED, f"outside the install: {stray}; named and absent: {missing}"
    return PASSED, ""


def stash(out: Path) -> Verdict:
    verdict = refused(
        out / "stash.jsonl",
        "Bash",
        lambda i: str(i.get("command")).strip() == "git stash",
        STASH_REASON,
    )
    if verdict[0] == PASSED and (out / "stash_list.txt").read_text().strip():
        return FAILED, "the hook refused the stash and a stash was made anyway"
    return verdict


def checks(config_dir: Path, out: Path) -> list[tuple[str, Verdict]]:
    session = json.loads((out / "sessions.json").read_text())["stop"]
    return [
        ("bare_git_stash_guard", stash(out)),
        (
            "emoji_guard",
            refused(out / "emoji.jsonl", "Write", lambda _: True, EMOJI_REASON),
        ),
        ("prose_question_blocker", sent_back(out / "prose.jsonl", PROSE_FEEDBACK)),
        ("stop_flag", stop_flag(out, session)),
        ("stop_gate", refused(out / "stop2.jsonl", "Bash", lambda _: True, STOP_REASON)),
        ("a tool call after the next prompt", ran(out / "stop3.jsonl", "Bash")),
        ("edit_trailing_space_guard", trailing_space(config_dir)),
        ("peer_reply_guard", peer_reply(config_dir)),
        ("commit", committed(config_dir, out)),
        (
            "rules: triage_run.sh",
            ran_script(out / "tests.jsonl", "triage_run.sh", TRIAGE_LINE),
        ),
        ("rules: git_state.sh", ran_script(out / "tests.jsonl", "git_state.sh", STATE_LINE)),
        ("self-optimize", self_optimized(config_dir)),
        ("every session ended normally", ended(out)),
        ("paths", paths(config_dir, out)),
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_clone.py CONFIG_DIR OUT_DIR", file=sys.stderr)
        return 2
    config_dir, out = Path(argv[0]), Path(argv[1])
    absent = [name for name in SESSIONS if not (out / f"{name}.jsonl").is_file()]
    if absent or not (out / "sessions.json").is_file():
        print(f"no probe output in {out} for: {absent or 'sessions.json'}", file=sys.stderr)
        return 2
    verdicts = checks(config_dir, out)
    for name, (verdict, why) in verdicts:
        print(f"{name}: {verdict}" + (f", {why}" if why else ""))
    states = {verdict for _, (verdict, _) in verdicts}
    return 1 if FAILED in states else 4 if SKIPPED in states else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
