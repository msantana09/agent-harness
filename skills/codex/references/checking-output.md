# Checking Codex Output

## Validation Checklist

Check Codex output for common mistakes:

- Recommending technologies not in the project's stack
- Misunderstanding the project's architecture patterns
- Suggesting patterns that don't match the project's conventions
- Confusing sync vs async patterns

## Correction Re-run (factual errors only, max 3)

If the output rests on an obvious factual error about the project (wrong database, wrong
framework), write a new prompt file with a correction note (e.g. "NOTE: We use PostgreSQL, not
MySQL") and submit it as a new job. This cap is for correcting a broken premise, not a limit on
review rounds: `/ship` sets its own round budget (its Step 4).

## Claim Verification

After reading Codex output, verify significant claims:

1. Read relevant files to confirm code/patterns exist
1. Check line numbers, function names, behaviors

Document as: **Confirmed**, **Corrected**, **Refuted**, or **Unable to Verify**.

**Prior for PR findings: probably right.** Its strongest pattern is *state-combination* reasoning:
it reads the PRODUCER to find flag combinations the UI or a test never exercises. It also reliably
catches incomplete sweeps: the one surface a "fixed everywhere" claim missed. Still verify each
finding at the producer before accepting, but a `Request changes` verdict from it is worth a full
pass before merge. It also checks external premises: a code-only refutation of an industry
definition it raised can be wrong, and a primary source settles it.
