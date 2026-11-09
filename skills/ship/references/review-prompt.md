# Step 2 - the review prompt

The review framework is `inspect-pr`; the prompt only supplies what that skill cannot
derive on its own. Prompt template (fill the bracketed slots; keep the section names):

```text
ROLE: External reviewer giving a second opinion on this repository. Do not create,
modify, or delete files, and do not change git state. Your final message is the
deliverable.

GOAL: Use $inspect-pr to inspect PR #<N> ("<title>") in this worktree. The change under
review is exactly `git diff origin/<base>...HEAD`; the PR body, prior reviews, and CI are
readable with `gh`. Return the inspect-pr response contract in full (Findings, Open
Questions, Missed Opportunities, Validation with the file ledger, Verdict), severities
Blocker / High / Medium / Low.

SESSION CONTEXT: <what the PR sets out to do, in two or three lines — the intent the
diff should be judged against, plus any constraint that shaped it (scope boundary,
deliberate trade-off). Round <N> of this PR: prior rounds' ACCEPT/REJECT/FILE logs are in the
PR body under "### Codex review round", "### Opus review round" or "### Fable review round"
— apply inspect-pr § Multi-Round Mode.>

ANCHOR FILES:
<the changed files, from `git diff --name-only origin/<base>...HEAD`, plus the one or two
unchanged files a reviewer must read to judge them (the caller, the test file), plus the
workstream's `plan.md` when the branch belongs to one, so inspect-pr grades its criteria>

STOP RULES: An anchor path that does not resolve is noted in your answer and the review
continues. If the diff is empty or the PR is not reachable with `gh`, say exactly what
blocked you instead of guessing.
```

`--project-dir <worktree>` sandboxes Codex to that tree, and gitignored files (audits and
Codex outputs under `.artifacts/`) do not travel with the branch. Before launching a round
whose ANCHOR FILES cite one, `mkdir -p` and `cp` that file from the main tree to the same
relative path under `<worktree>`; a round that stops on a missing anchor is wasted.

**A PR whose subject is a detector** (guardrail test, lint rule, scanner, validator): from
round 1 add to SESSION CONTEXT the one-ranked-gap-list ask in `Skill("codex")`
(codebase-present vs conceivable-only = Low), then decide the detector's scope once.
