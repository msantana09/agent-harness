# Self-optimize report: {date}

## Target

{The behavior, in one sentence. Why it won over the runners-up.}

## Evidence

Window: {from} to {to}. {N} sessions with activity, {M} typed messages.

| Session | Date | What the user said |
|---|---|---|
| {8-char id} | {date} | "{exact quote}" |

Pattern: {what these have in common, in one or two sentences}

## Diagnosis

File: `{path}`

Current text:

```markdown
{exact current text, or "nothing governs this"}
```

Why it fails: {missing, ambiguous, contradicted by `{other file}`, or buried}

## Proposed changes

Applied in this order, each before the changes that depend on it.

### 1. {edit, new file or delete}: `{path}`

Before:

```markdown
{exact current text, or "new file", or "delete"}
```

After:

```markdown
{proposed text; the whole file for a new file; nothing for a delete}
```

Why this change: {how it helps prevent the quoted cases}

{Repeat for each change.}

## Review

{Each finding from the fresh review, and whether it was fixed or its change dropped. "No findings"
when there were none.}

## Eval

Suite: `{date}-evals/`, {N} held-out cases, {runs} runs each, written without the sessions above.

| Case | From session | Current | Edited |
|---|---|---|---|
| {case} | {8-char id} | {score} | {score} |

Second grader ({model}): {current score} vs {edited score}; {k} of {n} runs graded differently.
Cases: {won} won, {tied} tied, {lost} lost.

Verdict: {current score} vs {edited score}: {edited higher, cannot separate, edited lower or too
close to call}, with one line on why

## How we will know

Measure: {the kind of correction, or the hook block by its name in the scan; when the change
targets one pattern the hook checks, add that pattern's quoted value as the scan's `--json`
prints it}
Baseline: {count} in {sessions} sessions with activity, {from} to {to}, on {machine as
`hostname -s` prints it}, {transcript folders}

## Risk

- What else reads these files: {skills, agents or projects affected}
- If it makes things worse: {what that looks like}
- Undo: restore each before text and deleted file, and delete each new file.

## Runners-up

{Up to three other patterns with their counts, for a later run.}
