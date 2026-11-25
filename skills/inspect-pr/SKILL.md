---
name: inspect-pr
description: Perform a thorough, evidence-backed, read-only inspection of a GitHub pull request. Use when the user asks to inspect, review, audit, or assess a PR by number, URL, or current branch; asks what issues, omissions, regressions, test gaps, or missed opportunities exist; or wants a merge-readiness opinion. Gather PR intent, the exact base-to-head diff, repository instructions, relevant unchanged context, review discussion, and CI evidence; examine correctness, security, data safety, contracts, operations, tests, and simplicity; counter-check every candidate finding; and return findings first with precise locations and concrete failure modes.
---

# Inspect Pull Request

Optimize for verified defects and consequential omissions, not finding count.

## Operating Contract

- Keep the inspection read-only. Do not edit files, switch branches, approve, merge, close, comment
  on, label, or rerun workflows for the PR unless the user separately asks. This binds even when
  the runtime does not enforce it (a Codex review job from `codex-jobs review` has network
  access, so `gh` can mutate); the caller checks `git status --short` afterwards.
- Do not disturb the current worktree. Fetch objects when needed, but never use `gh pr checkout` in
  a worktree containing user changes. If exact-head execution is necessary, follow repository
  automation to use an isolated worktree.
- Record the base and head commit IDs. Review that immutable snapshot, and check whether the head
  changed before giving the verdict.
- Follow repository instructions, then load only the domain skills relevant to the changed
  surfaces. Treat their checklists as supplements to this protocol.
- Inspect every meaningful changed file. Deprioritize generated and mechanical output only after
  verifying its source, regeneration path, and expected consistency.
- Report only issues introduced by the PR or exposed because the PR now relies on affected
  behavior. Put unrelated pre-existing debt outside the findings.
- Separate merge concerns from optional opportunities. Do not turn preferences, style nits, or
  speculative future scale into defects.
- Verify every finding against surrounding code.

## Phase 1: Establish the Review Snapshot

1. Resolve the PR from its number, URL, or the current branch. Confirm the repository when context
   is ambiguous.
2. Collect, preferably in one pass:
   - number, URL, state, draft status, author, title, body, labels, and milestone;
   - base and head branch names and commit IDs;
   - commits and changed files;
   - linked issues or workstream documents;
   - review decision, submitted reviews, unresolved discussion, and prior inline comments;
   - CI/check names, conclusions, and relevant failure logs.
3. Command output volume: read NO REDUNDANT EXECUTION in
   `rules/core-rules.md` § Orchestration & Execution Rules — it binds here. Use a
   repository-connected GitHub tool when available; otherwise use read-only `gh pr`, `gh api`, and
   `git` commands.
4. Verify repository branch policy. For promotion PRs, distinguish expected branch-to-branch
   changes from feature work and apply the repository's deployment restrictions.
5. Ensure the exact base and head objects are locally inspectable when deeper source context is
   needed. Fetch the PR ref without checking it out, then verify the fetched commit matches the
   recorded head ID.

Do not trust a patch alone for large or truncated diffs. Use the exact commits to inspect complete
files and history.

## Phase 2: Reconstruct Intent

Write a compact private review brief before judging implementation:

- the user or business outcome;
- explicit acceptance criteria;
- non-goals and compatibility expectations;
- affected users, tenants, data, APIs, workflows, and deployment paths;
- the riskiest assumption in the proposed approach.

Derive this from the PR body, linked issue, plan, commits, and code. Label inferred intent as an
inference. If those sources conflict, treat the conflict as review evidence rather than silently
choosing one.

When a plan lists success criteria or non-goals (a plan's success-criteria and non-goals
sections, or a list in the PR body or linked issue), grade each one against the diff for the
Validation section's criteria ledger:

- **Satisfied**: quote the criterion, then cite the `file:line` that meets it (code, a test
  assertion, a measurement). "Looks good" is not evidence.
- **Gap**: quote the criterion and name the shortfall ("the test asserts only the truthy
  branch"). A gap the PR claims to close is a finding.
- **Not addressed**: the diff does not touch it; say whether the PR body scopes it out.
- **Non-goal respected** or **violated**: a violation cites the `file:line` that adds the
  excluded scope and is a finding.

## Phase 3: Map the Change and Blast Radius

1. Inventory added, modified, renamed, and deleted files. Categorize:
   - handwritten behavior;
   - tests and fixtures;
   - schemas, models, migrations, and persisted data;
   - API types, generated clients, and other generated artifacts;
   - configuration, CI/CD, infrastructure, docs, and observability.
2. Read the nearest `AGENTS.md`, `CLAUDE.md`, and relevant project documentation for each changed
   subtree.
3. Read complete changed functions, classes, modules, and tests—not only added lines.
4. Search outward from each behavioral change:
   - callers, callees, implementations, registrations, and dependency injection;
   - producers and consumers of changed data or events;
   - request/response schemas and generated clients;
   - feature flags, permissions, configuration, migrations, and deploy manifests;
   - references to renamed or deleted symbols and files.
5. Trace at least one principal success path and one credible failure or boundary path end to end.
6. Compare against the base version when intent or regression ownership is unclear.

Create a mental coverage matrix of changed behavior versus tests, contracts, data effects,
operations, and user-visible states. Use gaps in that matrix to choose what to trace by hand; a
gap is not a finding by itself (exceptions: `references/review-passes.md` § 4.4 Tests and CI).

Run Phase 4, the independent review passes, from `references/review-passes.md`.

## Phase 5: Verify Every Candidate Finding

Before reporting a candidate:

1. State the concrete trigger and trace it to the incorrect behavior.
2. Confirm the PR introduces or newly depends on the problem by comparing the base.
3. Search for validation, authorization, fallback handling, cleanup, or compensation elsewhere.
4. Check tests, contracts, comments, history, and linked requirements for evidence of intent.
5. Reproduce with an existing test or a safe read-only check when proportionate. Do not modify the
   PR to prove the review.
6. Anchor the finding to the narrowest changed line in the head snapshot. For a missing change,
   anchor to the changed code that creates the obligation and name the omitted destination.
7. Sweep for the class. When the trigger is a mechanism that can recur (a check-then-act read, an
   unscoped query, a guard present on one path and absent on its peers), search the diff and the
   code the PR newly relies on for the same shape. Report the class as one finding: every
   instance under **Instances**, and a recommendation that fixes the mechanism once rather than
   each line. The sweep is done when every path performing the same operation is listed or
   cleared in **Evidence** with the reason it is safe. Unchanged pre-existing siblings stay
   outside the findings.
8. Move unresolved interpretations to open questions. Do not disguise uncertainty as a defect.

Reject candidates based only on:

- style or naming preference;
- a pre-existing problem unchanged by the PR;
- hypothetical misuse with no reachable call path;
- a request for more tests where no defect was found, outside the missing-test findings that
  `references/review-passes.md` § 4.4 Tests and CI names;
- an alternative architecture with no concrete advantage;
- tool output that has not been tied to the PR.

## Multi-Round Mode

When the caller runs review rounds against successive heads (the `/wrapup` command does this), the
PR body carries the prior rounds below its `---` divider as `### <Reviewer> review round <N>`
blocks (Codex, Opus or Fable, whichever ran that round), one `ACCEPT`/`REJECT`/`FILE` line per finding with the fixing commit for accepted ones and the
issue for filed ones. Read them in Phase 1 and apply:

- A `REJECT`ed or `FILE`d finding is settled. Do not re-report it unless the new head adds
  evidence the recorded reason did not cover; if so, say which line of the reason no longer holds.
- An `ACCEPT`ed finding is a verification obligation: confirm the named commit is in the head and
  actually resolves the trigger. Report a fix that does not resolve it as a new finding anchored to
  the fix, and list resolved ones under `Validation` as `Round <N> fixes verified`.
- Everything else is reviewed as a first round. The new head is the snapshot, not the delta since
  the last round: a fix can regress a file the previous round cleared.
- State the round number in `Validation`.

## Severity

- **Blocker**: Likely security or tenant breach, irreversible data loss, invalid deployment path,
  or change that cannot work as intended.
- **High**: Concrete major regression, broken core workflow or contract, unsafe migration, or
  serious operational failure.
- **Medium**: Real, reachable correctness or integration problem with limited blast radius that
  should normally be fixed before merge.
- **Low**: Minor but concrete defect. Exclude pure polish and preference.

Calibrate severity from reachability and consequence, not the size of the proposed fix.

## Optional Supplemental Review

Use a supplemental diff review only when the user requests an independent or multi-lens sweep, or
when repository workflow explicitly requires it. Treat its output as candidate findings and apply
this skill's snapshot, ownership, and counter-check standards before reporting them. Do not make an
ordinary PR inspection depend on artifact-heavy or multi-agent review.

## Response Contract

Return the report as the chat reply, findings first, ordered by severity and then confidence.

```markdown
## Findings

- **[High] Short imperative title** — `path/to/file.py:123`
  - **Trigger:** Exact input, state, or sequence.
  - **Impact:** Concrete user, data, security, or operational consequence.
  - **Evidence:** Diff and surrounding-code evidence, including the counter-check.
  - **Instances:** Class findings only (Phase 5 step 7): every `file:line` sharing the mechanism.
  - **Recommendation:** Smallest root-cause correction or verification.

## Open Questions

- Only questions that could materially change the verdict.

## Missed Opportunities

- High-value, non-blocking improvements directly enabled by this PR.

## Validation

- Snapshot: `<base-id>...<head-id>`
- CI and commands inspected or run, with results.
- File ledger: every changed file as `reviewed` or `skipped: <reason>`. Reviewing an
  implementation file does not cover its test, migration, schema, config, or interface
  counterpart; a file being the smaller or secondary member of the change is not a reason
  to skip it. Anything else not verified.
- Criteria ledger, when Phase 2 found a criteria list: one line per success criterion and
  non-goal with its grade and evidence.

## Verdict

`Request changes`, `Ready with follow-ups`, or `Ready`, with one-sentence rationale.
```

Omit empty sections except `Findings`, `Validation`, and `Verdict`. If there are no findings, say
“No confirmed findings” explicitly; never invent issues to fill the report. State residual risk
when checks were unavailable, the diff was unusually broad, or intent remained ambiguous.

Keep the `## Findings` and `## Verdict` headers literal: `codex-jobs review` validates the review
on them. A caller that fixes findings treats Blocker, High, and Medium as holding the
merge and Low as optional; label accordingly.
