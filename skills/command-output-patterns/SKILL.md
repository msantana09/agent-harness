---
name: command-output-patterns
description: Use when running test suites, builds, migrations, or other commands expected to take longer than 30 seconds. Patterns for preserving long-running command output before filtering it.
---

# Command Output Patterns

## Core Rule

**Never pipe output from long-running commands.** Save full output to a file first, then
filter/process the file.

**Rule**: If a command takes more than ~30 seconds, keep its output in a temp file: test and
build runs go through `triage_run.sh` (below); a run whose output you need verbatim (a single
test with `-s`, a collect-only listing) and anything else go through a plain redirect.

## Examples

```text
WRONG (pipes away output — must re-run if filter is wrong or you need more data)
.venv/bin/pytest tests/integration/ --tb=line -q | grep "^FAILED"

RIGHT (captures everything — filter locally as many times as needed)
~/.claude/scripts/triage_run.sh <scratchpad>/test-output.txt -- \
    .venv/bin/pytest tests/integration/ --tb=line -q
grep "^FAILED" <scratchpad>/test-output.txt
tail -5 <scratchpad>/test-output.txt
```

## Digest Instead of Dump

For test suites, type checks and builds, wrap the command so the full output lands on disk
and only a Haiku-written failure digest (name, `file:line`, first message) enters context:

```bash
~/.claude/scripts/triage_run.sh <scratchpad>/pytest.txt -- .venv/bin/pytest tests/ -q
```

Exit code is the command's. `<scratchpad>` is the session scratchpad.
Grep the saved file for anything the digest did not carry.

During bisection, save each run to a distinct file (`<scratchpad>/bisect-services.txt`,
`<scratchpad>/bisect-core.txt`) so every run stays open to re-analysis without re-running.

## Probe Before a Long Measured Run

Before a coverage, benchmark or full-suite run that takes tens of minutes, run the same
tooling on one file or one test first. A measurement of `0/0` (no lines collected, no tests
selected) is an environment fault, not a result: fix the checkout or tooling before any full run.
Long runs also fail from sleep and contention rather than from the code: keep the machine awake
for the run (`caffeinate -is` on macOS), check that no other session's suite is running before
it starts, and re-run each failing file alone before calling a timeout real.

## When This Applies

Which commands qualify: read `rules/automation.md` § No Redundant Execution — it binds here.

## Exception

For a non-test command, piping to `tee` is fine since it saves AND displays output. Test runs
go through `triage_run.sh`, except the verbatim-output runs that rule names: a long `tee`'d run
overflows the tool result, which then loses its pass/fail summary.

```bash
docker compose logs api 2>&1 | tee <scratchpad>/api-logs.txt
```
