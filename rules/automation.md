# Automation Rules

How tools, subagents and parallel sessions are used.

## Tool Usage Patterns

### No Redundant Execution

Never run the same command twice to extract different information.

- **First run**: tests and builds go through `~/.claude/scripts/triage_run.sh
  <scratchpad>/run.txt -- <cmd>`; a run whose output you need verbatim (a single test with `-s`,
  a collect-only listing) and any other long command redirect to a file in the scratchpad.
- **Subsequent analysis**: grep/tail/head that file — never re-execute
- This applies to tests, builds, migrations, docker compose, data queries — any command
  that takes more than a few seconds
- See `Skill("command-output-patterns")` for patterns

### Clarification & Resumption Protocol

A subagent (no `AskUserQuestion`) blocked on a decision ends its turn with the heading
`## Clarifications Needed` at the start of its own line, then each question numbered with its
options (A, B) and why it matters, then `## Work Completed So Far` with the partial work.
The caller finds the questions by that line in the agent's result (a tool result or task
notification; a Workflow stage bound to a schema carries them in a schema field), relays them
through `AskUserQuestion` and resumes the agent with the answers through `SendMessage`; the agent
continues with its full context.

### Command Output Conventions

| Command | Use Instead | Why |
|---------|-------------|-----|
| `git status` / `git branch --show-current` / `pwd` chains | `~/.claude/scripts/git_state.sh [dir] [base-ref]`; file list: `git status --short` | One line: branch, head, drift (plus `behind_base` with a base ref), dirty count, worktree |
| `git diff` | `git diff --stat` or `git diff \| head -100` | Avoids 30KB+ truncation |
| `git log` | `git log --oneline -10` | Compact, limited |
| `pre-commit run --all-files` | `pre-commit run --files $(git diff --name-only HEAD)` | Only changed files |
| `markdownlint-cli2 "**/*.md"` | Where the repo has a pre-commit hook: `.venv/bin/pre-commit run markdownlint-cli2 --files <paths>` | Only named files; bare binary is off PATH (agents then report "lint skipped"); the hook brings its own node |
| Inline Python walking `~/.claude/projects` jsonl | `python3 ~/.claude/skills/transcripts/scripts/read.py tools`, `messages --typed`, `around`, `since-reply` or `lead-up` (see `--help`) | Plain-text rows with session, agent, kind and cwd already mapped, read from an index: `--search` finds whole words at once (`word*` for other endings), `--match` takes a regex, `--skip-session` leaves a session out |

**Directory flags over `cd`**: `git -C dir`, `make -C dir`, `npm --prefix dir` (`run` runs in
`dir`; `exec` runs in the caller's cwd), a path argument for `ruff`. `gh` has none: run it as
`(cd dir && gh …)`. `cd` in a compound command can prompt, and a bare leading `cd` persists only
in the main session.

## Agent Coordination

### Surface Check Before Delegating

Before delegating substantive work or making the edit, answer two questions and check that the
surface is the one the user has worked on this session. If any is unclear or differs, ask via
`AskUserQuestion` — do not guess; wrong-surface work costs more than the confirmation.

1. **Mode** — Are we in design exploration (playground HTML, mockups, a throwaway prototype) or
   implementation (real source edits)? A phrase like "update the other pages" inside
   a playground session extends the playground; the same phrase in an implementation
   session edits source. When in doubt, ask "playground or real code?"

2. **Surface** — Where does the user expect the change to land? For products with a
   dedicated UI (e.g., an admin console or web app), default to UI changes over manual
   CLI/API/curl workarounds, even when the backend route already works. Reserve manual
   API calls for true emergencies (UI broken, production incident) and state the
   emergency reason.

## Orchestrator Mode

Pick the orchestration tier by *shape*, not headcount: a few independent tasks → inline `Agent`
fan-out; fan-out-then-verify / loops / multi-phase / migrations / audits → `Workflow`; state that
must survive compaction → files the agents read and update, since context does not survive it.
`Workflow` is main-loop-only. The tiers in full, the durable-file fallback included:
`Skill("agent-patterns")` § Choosing an Orchestration Tier.

Standing authorization: a user who keeps this paragraph in their rules has opted in to Workflows
launched without being asked first; a user who deletes it is asked before each run. While it
stays: launch a Workflow directly when the shape fits; announce a one-line scale note; pick
per-stage effort per core-rules EXECUTE DIRECTLY.

Locating code across 10+ files goes to Explore; reading them in full goes to general-purpose.

### Model Tiers

Fit the tier to the task; misfit in either direction is the failure (core-rules RESPECT AGENT
MODEL CONFIG requires a tier on every generic subagent):

- **Fable**: mystery-bug debugging; adjudicating between conflicting verdicts, binding-artifact
  synthesis, recall-critical final audits — single calls whose output decides the outcome;
  the hardest long-horizon problems.
- **Opus**: reasoning/judgment-heavy work — architects, auditors, security engineers, curation,
  code review, and per-item verify/refute passes in a fan-out (adversarial or not) — and strong
  coding/agentic execution when the task warrants it. A stage that runs once per item
  multiplies its tier's cost, so per-item stages default to Opus or below.
- **Sonnet** (`xhigh` for the hardest): execution — engineers, QA, designers; implementation,
  test-writing, verify batteries, refactors, rebases.
- **Haiku** (5.5; `medium`, `high` for multi-step work): chores and bounded tasks that end
  in a checkable artifact — file:line locate sweeps, run-and-report, test-failure
  digests and summaries, formatting-level edits. Never implementation, review/verify,
  design, or open-ended research.

The model tier is the cost lever, not effort: route mechanical work to Sonnet/Haiku instead
of pinning Opus at low/medium effort (no low/medium opus pins); effort tunes performance
within a tier. Ask the user only when the call is genuinely ambiguous and the run is expensive
or hard to redo; otherwise pick and state the mix (e.g. in the Workflow scale note).

## Concurrency Awareness

### Refresh Before Acting

Assume commits landed while you were working: run `~/.claude/scripts/git_state.sh` before
summarizing or editing. A status answer ("where are we", "is it up to date", a workstream or
PR-stack report) is a summary: after `git fetch origin <integration branch>`, open it with
`git_state.sh <worktree> origin/<integration branch>` for each worktree it names (`behind_base=N`; a
branch with no worktree: `git rev-list --count <branch>..origin/<integration branch>`), and check
each item it lists as open at its binding surface first. Run `git_state.sh` on the main checkout
before creating a worktree and before handing the user a step that needs local files: a note or
summary line saying the main tree is someone else's or detached is a snapshot, not a rule. After a
compaction, the first action re-checks the summary's environment lines (current branch, which
checkout belongs to whom, what is dirty, what is deployed) with `git_state.sh`.

### File-Level Coordination

When multiple agents touch the same area:

- Pass state between agents via files (a folder in the repo for the work, or the scratchpad for
  ad-hoc fan-outs), not assumptions
- Different files: safe to parallelize
- Same file: one editor per file in a shared tree. Parallel edits to one file
  run only across worktrees, with `git merge-tree --write-tree` dry-runs as *signal*; re-dispatch
  with merged context when reconciliation fails
- Same function: one agent only, or explicit handoff, across worktrees too
