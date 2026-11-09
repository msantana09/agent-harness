---
name: commit
description: Use when committing changes, or when the user types /commit. Stages exactly your own paths, passes the repo's pre-commit hooks, and writes a message that describes the staged diff.
argument-hint: "[files...]"
allowed-tools: Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git show:*), Bash(git add:*), Bash(git commit:*), Bash(git restore:*), Bash(git apply:*), Bash(.venv/bin/pre-commit:*)
---

# Commit

Commit on the current branch, running the git commands yourself. Arguments, when given, are the
paths in scope; the message comes from the session's work and the staged diff.

**Recent commits (style reference):**

!`git log --oneline -5 2>/dev/null`

## Steps

1. **Live-writer check.** In the same turn as the commit, run `git branch --show-current` and
   `git status --short`. If another agent of yours (a subagent, a Workflow stage) may still be
   writing in this worktree, wait until it has finished: pre-commit sets uncommitted changes aside
   while its hooks run, and a writer racing that restore loses its edits. A "killed" or "stopped"
   task notification is not proof the writer is gone: `TaskStop` the id, then check for fresh
   mtimes or commits before touching the tree.
2. **Scope is exactly the files you changed.** Name every in-scope path: modified files and new
   untracked (`??`) ones, which `git diff --stat` leaves out. Stage by name, never `git add -A` or
   `git add .`, which sweep in a sibling agent's half-done files and stray untracked files. Leave
   deleted paths out of `git add`: `git rm` already staged them, and a missing path aborts the
   whole `git add`. An in-scope file that also carries older edits with no live writer is
   committed whole, the message covering both; use `git add -p` only when the user asks or the
   other edits are another author's unrelated work.
3. **Pre-commit**, in a repo with a `.pre-commit-config.yaml`: run
   `.venv/bin/pre-commit run --files <paths>` (or the command the repo's `CLAUDE.md` names) and fix
   only mechanical failures: line length, list prefix, trailing whitespace, final newline. A
   substantive failure (types, tests, a quality baseline or ratchet) stops the commit: report it.
   Source, tests and baselines stay as they are; editing them to get past a hook corrupts the
   gate. `--no-verify` only when the user asked for it in this session.
4. **Stage and count.** `git add <paths>`, then `git diff --cached --name-only | wc -l` must equal
   your expected file count (a short count means a partial stage).
5. **Commit** in the message format below. When a hook auto-fixes files and the commit fails,
   re-add the fixed paths and make a new commit. The failed commit never landed, so
   `git commit --amend` would rewrite the previous, already-landed commit.
6. **Verify**: `git show --stat HEAD` and `git status --short`.

## Commit per phase

Commit when the plan or phase is done, not after each task, unless the user asks for an
intermediate commit. Split a large changeset into phase-scoped commits; one bundled commit is
right when the user says "one commit". In a repo with pre-commit, flush autofixes before
splitting: stage the full set, run pre-commit on `$(git diff --cached --name-only)`, restage and
re-run until clean, then `git commit -- <paths>` per phase. Otherwise an autofix on a staged file
conflicts with the stashed unstaged siblings and the commit aborts ("Stashed changes conflicted
with hook auto-fixes").

## A hung commit

Interrupt a hung commit with Ctrl-C (SIGINT) or let it time out, so pre-commit restores the
changes it set aside. If unstaged work has vanished, it is in
`~/.cache/pre-commit/patch{timestamp}-{pid}`; `git stash list`, `reflog` and `fsck` show nothing.
Restore it before redoing any work: `git worktree add <dir> <preimage sha>`, `git apply` the
newest patch there, then copy the files over. `git apply --3way` on a confused index can exit 0
and half-apply, so verify by content.

## Commit message format

```text
<type>(<scope>): <description>

<optional body explaining WHY, not what>
```

Type ∈ {feat, fix, docs, style, refactor, test, chore, perf, build, ci, revert}. First line
imperative, under 72 characters.

The message describes the diff, not the brief: read `git diff --cached` before naming a mechanism.
A pushed message cannot be corrected.
