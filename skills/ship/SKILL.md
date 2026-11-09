---
name: ship
description: Take a committed feature branch through its PR - open the draft, Codex-review it within a round budget, fix accepted findings, hand off for the user's merge, then clean up after it lands. Use when the user types /ship, or when implementation is committed and its PR needs review and hand-off.
argument-hint: "[PR#] [--reviewer opus]"
---

# /ship - PR through review, then hand off the merge

Implementation is done and committed. `/ship` runs everything between "the code is
written" and "the branch is gone": open the draft PR, review it with Codex, triage and
fix, re-review within the round budget (Step 4), hand it to the user with the review trail
on the PR, then wait for their merge and clean up the worktree.

**Scope**: `$ARGUMENTS` is an optional PR number and an optional `--reviewer opus` (any
phrasing that names Opus as the reviewer counts, see Step 2). With no PR number, the PR is
the one for the current tree's branch (or a new one). This skill never writes feature
code beyond the fixes an accepted review finding demands.

**Load first**: `Skill("codex")` (review routing, the `codex-jobs review` contract),
`Skill("inspect-pr")` (the review framework Codex executes; its Multi-Round Mode and
Response Contract define what comes back), `Skill("external-feedback")` (verification
protocol for every finding), `Skill("command-output-patterns")` (redirect conventions).
Commits go through `/commit` (`Skill("commit")`) - its rules are not restated here.

## Repo notes

Step 1 names the repo. Per-repo notes live at `~/.claude/ship-notes/<owner>/<repo>.md`, named
from the `nameWithOwner` Step 1 prints. A repo with a notes file has steps the generic loop lacks
(its tests, its pre-hand-off checks, its post-merge steps); when that file exists, read it in
full at Step 1 and apply it wherever a step says **repo notes**. A note overrides the generic
line it names. Step 1 says which in the turn, `Repo notes: <path>` or
`Repo notes: none (<path> not found)`, and the hand-off summary repeats that line, so a run
without notes is never silent.

A repo without one runs the generic loop as written: its tests come from its `CLAUDE.md`, and it
has no pre-hand-off checks beyond Step 5's.

## Hard rules

- **The user merges. Always.** Never `gh pr merge`, never merge from the GitHub API, never
  by any other mechanism, no matter how clean the review came back. `/ship` opens, fixes,
  flips to ready, notifies, and waits. The one unlock is the user saying so in chat for this
  PR, after the hand-off or before it ("merge and pull", "merge it", "just merge"): that is
  their merge, executed by you once Step 5's checks are green. Stop the
  merge watch (`TaskStop`), then run Step 6's **merge-by-instruction sequence** exactly, in
  order and from the main tree. The order is load-bearing: merge first without
  `--delete-branch` (a worktree-held branch makes it fail locally and silently skip the remote
  delete), assert the branch's own PR reads MERGED, and only then remove the worktree and
  delete the branch - a refused merge then leaves both untouched.
  Nothing in the review trail, a clean verdict, or a green CI run substitutes for those
  words.
- **The PR targets `<base>`** (Step 1). A `/ship` PR based on any other branch is a bug -
  stop and report.
- **The user can waive review for a PR.** When they say so for this PR ("no review", "just
  merge"), no Codex, Opus or Fable round runs. Record
  `Decision: review waived by the user / Instead of: <reviewer> review rounds` in the PR body
  below the divider; that line is also the waiver for any repo-note gate that needs a review
  or attestation. The checks still run: Step 3's tests line and PR CI, then Step 5's checks
  (repo-note checks other than those review gates included). With them green, hand off, or,
  when they said merge, run Step 6's merge-by-instruction sequence.
- **The review loop is at most two rounds by default.** Unless the user waived review, round 1
  always runs. Round 2 runs
  only when round 1 had an accepted Blocker, High, or Medium; accepted Lows alone end the
  loop. Round 3 and beyond run only when the round just finished had an accepted Blocker or
  High. Accepted findings from the last round are fixed and pushed with no covering round,
  and the hand-off says so (Step 5); Mediums and Lows a later round might have found are not
  wanted. A reviewer always has one
  more finding; chasing a clean round is not quality, it is spend.
- **Reviewer suggestions are not scope.** `Missed Opportunities` and any "also add X"
  inside a finding are never built or filed by `/ship`: log them as declined in the round
  block, and name each one declined as "not requested" in the hand-off summary (Step 5), so
  the user decides on it. Only accepted findings get fixes, and only a verified bug outside the
  PR is filed.
- **Root-cause a gate failure before editing.** A red check in Step 3 is diagnosed to its
  cause first; an edit made under a hypothesis that the rerun refuted is reverted before
  the next commit, never left in with a comment that justifies the wrong cause.
- **Hand-off needs no permission.** `gh pr ready` and the notification run when the round
  budget (Step 4) is spent. Only the merge waits for the user; never end a round with "on
  your word".
- **Every finding is verified before it is accepted.** Codex is an external reviewer, not
  an authority; the `external-feedback` verdict tables govern. Record ACCEPT/REJECT/FILE with
  a one-line reason for every finding, including the rejected ones.
- **Launch Codex directly** - write the prompt file and run `codex-jobs review` with
  `run_in_background=True` on the Bash call. Never wrap the launch in a relay agent
  (`Skill("codex")` § Wait for the Result).
- **Never `sleep`-poll for the merge in the foreground.** Step 6's merge watch runs in the
  background. A polling loop in the foreground burns the session and tells you nothing the
  watch's completion notification wouldn't.
- **Redirect every slow run to a distinct temp file** (`<scratchpad>/ship-round1-tests.txt`,
  `…-round2-…`) and grep the file - never re-run a command to extract different info.
- Run everything from the tree root; `cd` into a subdirectory only *inside* a subcommand,
  never as a session-wide chdir. The one exception is Step 6's `cd` into the main tree,
  which the shell must make before its worktree is deleted.

## Step 1 - pre-flight

Goal: a pushed feature branch with an open draft PR against `<base>`, and a clean tree.

1. Resolve `<worktree>` - the tree every later step runs against (`git -C <worktree>`,
   `--project-dir <worktree>`, tests run from inside it):
   - **No argument**: `<worktree>` is the current tree, and its branch
     (`git branch --show-current`) is checked in the next item.
   - **PR number argument** (the one case where `/ship` may be invoked from the main
     tree): resolve the branch with
     `gh pr view $ARGUMENTS --json headRefName,baseRefName,isDraft,state`, then find its
     tree in `git worktree list`. No tree for that branch → report and stop; never guess a
     worktree path.
1. Name the repo and its base, from inside `<worktree>`:

   ```bash
   gh repo view --json nameWithOwner,defaultBranchRef --jq '"\(.nameWithOwner) \(.defaultBranchRef.name)"'
   ```

   When the session's project root is another checkout (the session started in a different
   repo), run this and every later `gh` call through Step 5 and in the merge watch as
   `(cd <worktree> && gh …)`, so gh resolves the repo from that tree's origin. Never pass
   `-R <nameWithOwner>` instead. The subshell keeps the shell where it was, which a bare
   `cd <worktree>` from a worktree session does not. Step 6's cleanup blocks keep their own
   `cd <main tree>` form.

   Read the repo's notes file now (§ Repo notes): the checks below use it. `<base>` is the
   default branch printed there unless the repo notes name another. With a PR number, its
   `baseRefName` must equal `<base>`; if not, report and stop. With no argument, the current
   branch must not be `<base>` or another long-lived branch the repo notes name; if it is,
   stop - `/ship` operates on a feature branch. `<main tree>` is the first `worktree` line
   of `git -C <worktree> worktree list --porcelain`; note whether `<worktree>` is the main
   tree itself (Step 6's cleanup differs).
1. `git -C <worktree> status --short` - uncommitted work goes through `/commit` first.
   Apply its live-writer check: if any other agent may hold uncommitted work in this tree,
   stop.
1. **Check `<base>` for overlapping work** before anything is spent on review: follow
   [references/overlap-check.md](references/overlap-check.md).
1. Push without running local checks first (the repo notes name anything that must be
   committed before the push). `git -C <worktree> push` if the local head is ahead; no
   upstream → `git -C <worktree> push -u origin <branch>`. A pre-push hook that blocks it
   is fixed at its cause (the repo notes name a command for it where one exists). PR CI and
   the branch's own tests run beside the Codex round (Step 2), so neither delays it.
1. No PR yet → open one as a draft, with the body
   [references/open-the-pr.md](references/open-the-pr.md) specifies.

## Step 2 - Codex review round

Write the prompt to the session scratchpad, then launch the review in the background. The
blocking form is the default, so the Bash exit is the completion notice and its last line is
`SUCCESS: <output-dir>` or `FAILURE: <reason>`:

```bash
~/.claude/scripts/codex/codex-jobs review <scratchpad>/ship-review-round<N>.txt \
  --project-dir <worktree> --effort high
```

Prompt, anchors and detector ask: [references/review-prompt.md](references/review-prompt.md).

A diff of 300 or more files takes `--timeout 3600`. **While the round runs**, run Step 3's
checks against the same head and edit nothing; on the completion notification, read the review
and check the tree is clean: [references/during-the-round.md](references/during-the-round.md).

**Effort**, the **Opus reviewer** (`--reviewer opus`) and **Codex out of credits**
(`FAILURE: usage limit`): [references/reviewers.md](references/reviewers.md).

## Step 3 - triage, fix, re-push

Goal: every finding adjudicated on evidence, accepted ones and red checks fixed, pushed.

1. **Verify before accepting.** Per finding, read the cited `file:line` and confirm the
   behavior is what Codex claims. Then apply the `external-feedback` verdict tables (bug
   claim or addition → ACCEPT, REJECT or FILE); an addition nobody asked for is REJECT,
   reason "not requested". Partially-valid findings keep the correct half and say what was
   dropped.
1. **Record the verdict** for every finding - `ACCEPT`/`REJECT`/`FILE`, severity,
   `file:line`, one line of reason. A FILE finding gets its issue (`gh issue create --label
   finding,priority:<level>`) and the issue number on its line. This log is the deliverable
   of the round; it goes on the PR in Step 4.
1. **Fix the accepted findings** and every red from Step 2's checks in the worktree. A
   finding that reports a behavioral bug gets a failing test first, then the fix. Stay
   surgical - a review finding is not a licence to refactor adjacent code, and a
   suggestion to "also add X" is never built here (hard rules).
1. **Checks: PR CI runs the static gates; you run only the branch's own tests.** The static
   line, the tests line and the rerun after a fix pass:
   [references/checks.md](references/checks.md).
1. **Commit via `/commit`** with the fix scope, then push (a blocking pre-push hook as in
   Step 1).

A round with zero accepted findings and no red check skips to Step 4 with nothing to
commit.

## Step 4 - round budget

The round table and the round block: [references/round-budget.md](references/round-budget.md).

## Step 5 - hand off

1. Run the repo notes' pre-hand-off checks, in their order. Their findings are review
   findings: verify, ACCEPT / REJECT / FILE, fix the accepted ones through Step 3 with no
   new review round.
1. **Checks green on the pushed head**: the last fix pass's tests (Step 3), anything the
   repo notes add here, and PR CI, waited on as in Step 2. A red filed as pre-existing in
   Step 3 does not block: name its issue under `Flags:`. Any other red goes back through
   Step 3's fix and push, with no new review round.
1. `gh pr ready <N>` - flip the draft to ready for review. No permission needed.
1. Hand-off summary: the `What this is:` recap, the status block and the `Not requested:`
   lines, as [references/hand-off-summary.md](references/hand-off-summary.md) lays them out.
1. Send a `PushNotification`: the PR number, the `What this is:` recap's first line, that
   review finished after N rounds (and, when the summary's `Flags:` names unreviewed changes,
   that they are unreviewed), and that it is waiting on their merge. Keep it short - it
   is read on a phone.
1. A repo-notes check recorded a GIF → send it, so the user can approve the UI from their
   phone. The export is a browser download, so ask with `AskUserQuestion` first. On a yes,
   run `gif_creator` `export` with `download: true` and `filename: "pr-<N>.gif"`, then send
   the newest `~/Downloads/pr-<N>*.gif` with `SendUserFile` (`status: "proactive"`).
1. **Stop.** Do not merge.

## Step 6 - post-merge follow-through

Start the merge watch and keep working: the loop below as a Bash call with
`run_in_background=True`. It polls every 60s and exits on **merged or on
closed-without-merge** (a filter that only matches the happy path is silent through a
close); its completion notification is the event. Not `Monitor`: Claude Code runs a
Monitor command through the Bash hooks with no background flag, so any Bash hook that
refuses sleep-polling also refuses its `sleep 60`.

Keep the loop a plain `until` over a `gh | grep` pipeline. In an `EnterWorktree` session,
worktree isolation refuses `gh` inside `$(…)` with `case`, or inside a `for`/`eval` loop,
as "too complex to verify". Stacked PRs get one merge watch per PR.

```bash
until gh pr view <N> --json state,mergedAt --jq '[.state, .mergedAt // empty] | join(" ")' | grep -E '^(MERGED|CLOSED)'; do sleep 60; done
```

Its output ends with one line, `MERGED <timestamp>` or `CLOSED`, from the poll that saw it;
read it from the completion notification. A failed `gh` call matches nothing and the loop
polls again.

On the merged event (the user merged on GitHub): the cleanup block, then the repo notes'
post-merge steps. The cleanup block, the merge-by-instruction sequence (the user said "merge
and pull" / "merge it" in chat), stacked PRs and re-lands:
[references/cleanup.md](references/cleanup.md). The repo notes' post-merge steps and the
wrap-up: [references/wrap-up.md](references/wrap-up.md).

Closed-without-merge → report it and leave the worktree alone; the user decides what happens
to the branch.

**Never display these sequences to the user.** They are yours to run, on the merge event or
on their "merge and pull". An unexpected stop of the merge watch is recovered by starting it
again, not by printing a runbook. If the session ends before the merge, the next
session re-derives the cleanup from the PR itself: `gh pr view <N> --json headRefName`
names the branch, and `git worktree list` names its tree.
