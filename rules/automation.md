# Automation Rules

## Tool Usage Patterns

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

## Findings

### Discovery Protocol

A discovery from your OWN investigation is a finding; a suggestion from outside (reviewer,
Codex, the user's musing) is a recommendation. Recommendations are surfaced, never scoped in
(communication.md § Stay in Your Lane). A defect a report names is a fact to verify; once verified
it is your own finding. Another site showing the defect in hand is the fix's
scope, not a finding (core-rules EXHAUSTIVE INVESTIGATION). A different root cause met in the same
file is a finding. Findings must land somewhere actionable:

- **Critical** (security, data integrity): surface immediately with a `CRITICAL Finding:` prefix.
- **High** (must land soon): at the moment you notice it, either fold it into the workstream
  in flight (add the task, fix it in the same PR) or create its own workstream and notify
  the user. Both are pre-approved; creating the workstream needs no permission.
- **Not landing in this PR** (a reachable failure that can wait): open a GitHub issue in the repo
  at discovery — `gh issue create --label finding,priority:<level>` (add `security` etc. as fits),
  claim + location + why it matters — and link it from the PR body. The label is the only triage
  an issue gets (the backlog drains by label, then age). Pick the level by checkable questions,
  not feel:
  - `priority:high` — data integrity, silently wrong output, a security delta, or a named person
    loses something under today's usage (delta test: trigger frequency times cost).
  - `priority:medium` — operator/dev pain, or a latent hazard with a plausible trigger.
  - `priority:low` — cleanup, cosmetics, dead code, naming, doc drift.
  Torn between two → the lower, unless the body names the delta (who loses what, how often).
  A claim that stored data is wrong or at risk (bad values a migration or writer stored, lost or
  corrupted rows) carries its count in the body: affected rows and tenants, read-only, on an
  environment that holds the data. Code proves the mechanism, never the reach. No reachable
  environment → the body opens `Unsized:` with why and the read-only query that would size it, and
  the label is `priority:low` until a count lifts it. Counting comes before escalation: whoever
  picks it up (a fix, a re-check, a decision round) takes the count first, and while none is
  reachable it stays low with that unblock condition, never re-rated or put to the user as a
  decision.
  An unlabelled issue sorts as medium. If the repo lacks the labels, create them first:
  `gh label create finding --color 0E8A16 -d "Incidental discovery parked at discovery time"`
  plus `priority:high`, `priority:medium` and `priority:low`. Never park it in `findings.md` and
  move on — logged findings became a graveyard.
- **Genuinely inert** (a corrected assumption with no forward consequence): a plain note is fine.
