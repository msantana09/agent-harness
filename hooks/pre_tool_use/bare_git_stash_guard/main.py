#!/usr/bin/env python3
"""Deny a Bash command that runs a bare `git stash`, `git stash pop`, `git stash apply` or
`git stash save`.

A bare stash takes every working-tree change, other sessions' included, and a bare pop or apply
reapplies stash@{0}, which may be a stash someone else made. Only a pathspec scopes a stash and
only an explicit ref scopes a pop or apply: a flag (`-q`, `-u`, `-m msg`, `--staged`, `-p`), a
redirection, a pipe or a git global option (`git -C dir`) changes neither.

Usage: PreToolUse hook, matcher `Bash`, timeout 10 s, onFailure "block"; Claude Code runs
`python3 main.py` with the tool call as JSON on stdin.
Input: `tool_name` and `tool_input.command`.
Output: nothing to allow, or a `permissionDecision: deny` JSON object on stdout.
Exit code: 0 whenever the hook starts.

Decisions:
- The command is read the way the shell reads it (hooks/lib_shared/shell_runs.py): line
  continuations joined first, then each simple command's words, past assignments, wrappers and
  git's own options, in nested shells (`bash -c '...'`, `eval`) and in what an ssh destination
  runs. The quote-masked text is matched too (BARE_GIT_STASH), so text that cannot be tokenized
  still blocks on a bare stash.
- Quoted data and messages are not commands: `grep 'git stash' docs/`, `echo 'git stash'` and a
  commit message that mentions a stash pass.
- Allowed: `git stash push -- <paths>`, `git stash push <paths>`, `--pathspec-from-file`,
  `pop`/`apply` with a stash ref, `list`, `show`, `drop`, `branch`, and `-h`/`--help`.
- Malformed input, a tool other than Bash, and an exception anywhere in the check allow the
  command with no output. A hook that cannot start (a missing lib, a timeout) is blocked by
  onFailure "block".
"""

import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib_shared"))

from shell_runs import (  # noqa: E402
    _any_run,
    _join_line_continuations,
    _mask_quoted_text,
    _ssh_remote_payloads,
)
from shell_tokens import _git_globals  # noqa: E402

# Matched against the quote-masked text: a flag or git global option hides a bare stash from it,
# which _is_bare_stash catches from each command's words.
BARE_GIT_STASH = re.compile(
    r"\bgit\s+stash(?:\s+(?:pop|apply))?\s*(?:;|&&|\|\||$)"  # bare stash / pop / apply
    r"|\bgit\s+stash\s+save\b"  # deprecated `save` (bare stash, no pathspec)
)

BLOCK_REASON = (
    "Blocked: do not run bare `git stash` / `git stash pop` / "
    "`git stash apply` / `git stash save` — they clobber unrelated changes "
    "(pop/apply reapply stash@{0}, which may be someone else's stash). Use "
    "`git stash push -- <paths>` to scope the stash, check `git stash list` first, "
    "and pop/apply an explicit ref (`stash@{N}`) you created this session."
)


def _stash_names_paths(args: Sequence[str]) -> bool:
    """True when the arguments of `git stash [push]` hold a pathspec (a word
    that is no option or option value, a word after `--`, or
    `--pathspec-from-file`)."""
    words = iter(args)
    for word in words:
        if word == "--":
            return next(words, None) is not None
        if word.startswith("--pathspec-from-file"):
            return True
        if word.startswith("--"):
            if word == "--message":
                next(words, None)
        elif word.startswith("-") and word != "-":
            # `-m` takes the next word as its message, alone or last in a cluster
            if word.endswith("m") and "m" not in word[1:-1]:
                next(words, None)
        else:
            return True
    return False


def _is_bare_stash(argv: Sequence[str]) -> bool:
    """True when the simple command *argv* is a bare `git stash`, `git stash save`,
    or a `git stash [push]` with no pathspec, or a `git stash pop` / `apply` with
    no stash ref, whatever flags it carries and past git's own options."""
    if not argv or Path(argv[0]).name != "git":
        return False
    args = list(argv[1:])
    idx = _git_globals(args).subcommand
    if args[idx : idx + 1] != ["stash"]:
        return False
    rest = args[idx + 1 :]
    if any(word in ("-h", "--help") for word in rest):
        return False
    subcommand = rest[0] if rest else None
    if subcommand in ("pop", "apply"):
        return all(word.startswith("-") for word in rest[1:])
    if subcommand == "save":
        return True
    if subcommand == "push":
        rest = rest[1:]
    elif subcommand is not None and not subcommand.startswith("-"):
        return False
    return not _stash_names_paths(rest)


def check_bare_git_stash(command: str) -> str | None:
    """The block reason when *command* runs a bare `git stash` / `pop` / `apply` / `save`,
    else None.

    The command an ssh destination runs is read too, quoted or not. The quote-masked text is
    matched by BARE_GIT_STASH and each command's words (_any_run) by _is_bare_stash.
    """
    if any(
        BARE_GIT_STASH.search(_mask_quoted_text(text))
        for text in [command, *_ssh_remote_payloads(command)]
    ) or _any_run(command, lambda run: _is_bare_stash(run.argv)):
        return BLOCK_REASON
    return None


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
        if hook_input.get("tool_name") != "Bash":
            return
        reason = check_bare_git_stash(_join_line_continuations(hook_input["tool_input"]["command"]))
    except Exception:
        return
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
