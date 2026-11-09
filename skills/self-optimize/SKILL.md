---
name: self-optimize
description: Use weekly, or after a stretch of repeated corrections, to improve your agent's instructions from evidence. Mines your own Claude Code session transcripts and saved feedback memories for the places you corrected the agent, picks the one pattern that costs you most, and proposes the changes that would prevent it, tested before they are applied. Proposes; applies only on your yes.
---

# Self-optimize

You are improving how this user and their agent work together, using the user's own sessions as
the evidence. One run produces one proposal: the changes to the user's instruction files that
would prevent one pattern, with the quotes that justify them, an eval that compares the old and
new text, and a way to check later whether they worked.

The scripts are in this skill's `scripts/` folder. Reports, eval suites and the ledger go in
`~/.claude/self-optimize/`, outside any public repository, because they quote the user's messages.

## Ground rules

- **One target per run.** A report with five proposals gets none of them applied.
- **Nothing changes without a yes.** Show each change's before and after, the eval result and the
  review, then ask.
- **Quote, don't paraphrase.** Every claim about what the user said is their words, with the
  session and date. A pattern you cannot quote is not a pattern.
- **This skill is no exception.** A change to it is proposed like any other and applied only on
  the user's yes.

## 1. Scan (free)

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/scan.py --since <start date> --until <yesterday>
```

Every run stops at yesterday, since today's sessions are still going. Start the day after the
through date on this machine's newest Scanned line that names the transcript folder being
scanned. Folders with different start dates get their own run of steps 1 and 2. If an entry
step 4 will re-count here is counted through an earlier date (its newest Re-count line's through
date, or its apply date when it has none), start the day after that instead, so the re-count has
every day it needs. On a first run, omit `--since` for 30 days. The ledger's lines are in
`references/ledger.md`.

It reports sessions with activity, messages the user typed, turns they interrupted, hook blocks,
skills that never loaded, and spend by model. Each hook block is named
`<event>: <start of the hook's message>`, with quoted parts, paths and numbers masked. It shows
the session it hit most and, when they differ, the quoted parts it blocked on (the pattern, file
or command), so one pattern of a hook can be counted alone. Read it for where
friction clusters: a session with many interrupts or hook blocks is worth opening. A skill that
never loads is a candidate to fix or delete only after a 30-day scan (`--days 30`) agrees, since
some skills run monthly. A count is a lead, not a finding.

Pass `--root` once per transcript folder when the user runs more than one Claude Code login on
this machine. A user who works on more than one machine runs steps 1 and 2 on each and keeps the
outputs apart: rates differ by machine, so a later count compares a machine only with itself.

## 2. Find the corrections

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/corrections.py --since <same date> --until <yesterday>
```

A small model reads the user's typed messages and flags the ones that correct the agent or state
a preference. The script says roughly what the calls cost as it starts, and the report ends with
what they cost; tell the user both. Each
line carries the session, project, date, the user's quote and a one-line lesson. The quotes are
copied from the transcript, so they are exact; a long or multi-line message shows its start, and
`--json <path>` holds every flagged message whole.

If the script exits non-zero, report the error and stop. Do not fall back to guessing. When it
finishes, append this run's Scanned line to the ledger, so the next run starts where this one
ended.

## 3. Read the saved lessons and the work

- **Feedback memories.** Read every memory file whose frontmatter says `type: feedback`: the
  `projects/*/memory/` folders under the Claude Code config folder, and any memory folder the
  user's instructions name. A memory records that a lesson was learned; the corrections show
  whether it still fails. Check each correction against them: one that repeats a saved lesson
  means the lesson is not landing, and one with no memory is a lesson never captured.
- **Git history.** For each repository the window's sessions ran in (the projects the scan
  names), read `git log --oneline --since=<same date>`. What kind of work dominated tells you
  which corrections are rare and which are this week's work.

## 4. Check the last changes first

Read `~/.claude/self-optimize/ledger.md`. Re-count every entry marked `applied` whose newest
Re-count line is missing, `on track`, `did not work` or `too early`, on the same machine and
transcript folders as its baseline and per session with activity. A correction measure carries the last
count forward and adds only the days since, from this run's step 2 output; a hook measure is
read again from the scan. Each Re-count line replaces the numbers on the ones before it. The
line, the verdicts and the statuses are in `references/ledger.md`.

A `did not work` entry is a candidate for this run: the first fix missed, so start from why.
Reverting is the user's call, and a `reverted` entry is not re-counted.

## 5. Pick one target

Group the corrections and memories by what the agent did wrong, not by topic. Rank the groups by
how many sessions they touch and what each one cost the user (rework, a repeated instruction, lost
work).

Skip a group when the ledger shows it `worked` or `declined`, when it is a one-off, or when no
instruction could change it. A `reverted` one may be proposed again from a different angle. If two
groups are close, show the top three with their quotes and let the user choose.

## 6. Diagnose

Find the instruction that governs the behavior: the user's `CLAUDE.md` files, rules, skills, hook
messages and saved memories. Read the whole file, not a search hit. Then answer three questions:

- What does the current text say, exactly?
- Why does it fail here? Missing, ambiguous, contradicted by another file, or buried?
- What are the smallest changes that would have prevented the quoted cases?

When nothing governs the behavior, the fix is a new rule in the file the agent reads first. When
the rule already exists and was ignored, adding it again will not help: sharpen the trigger, or
propose a hook that enforces it.

## 7. Test the changes

Build an eval suite and score the current text against the edited text before asking for a yes.
How to write the cases and run them, including replaying a changed script or hook on real input:
`references/evals.md`.

- **Held-out cases.** The cases that judge the changes are never written from the sessions that
  produced them. Give a fresh subagent one sentence naming the behavior under test and the ids of
  the sessions the diagnosis used, as ones to skip. It never sees the changes. It finds real
  instances in other sessions and writes the cases from them.
- **Recommend applying only when the edited text scores higher** on those cases. A tie means the
  suite cannot tell them apart: say so, and let the user decide on the evidence alone.
- When no case can set up the behavior (it only appears late in a long session, or after a tool
  call), say so in the report and name what to watch for on the next run instead.

## 8. Propose and apply

Write `~/.claude/self-optimize/<date>-report.md` from `references/report-template.md`, with the
suite in `~/.claude/self-optimize/<date>-evals/`. A proposal can hold several changes (edits, new
files, deletions), listed in the order they are applied. Before asking, a fresh subagent reviews
them against the quoted evidence and the target files; `references/apply.md` covers the review and
the apply. Show the user the target, the quotes, each change's before and after, the eval scores
and the review.

On their yes, apply the changes as that file describes, the way the user's own instructions say
changes to each file are made, and add one ledger entry for the proposal. On a no, add the entry
with `Status: declined <date>` so later runs do not propose it again. When they ask for another
attempt, go back to step 6 with what the eval showed; the ledger entry is written once, for the
attempt they answer.

## What to tell the user at the end

The target and why it won, the eval result, the changes and which were applied, the verdicts on
earlier changes, and what the run cost.
