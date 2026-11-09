# Workflow Stages and MCP Tools

## Workflow Stages

A Workflow stage runs on Codex instead of a Claude engineer when the value is a second model
family's read (plan gap-analysis, architecture validation, an unbiased review), a cheap Luna
scratch task that needs no Claude tooling, or a job that must outlive the session. Keep the Claude
engineer when the task needs Claude skills, hooks, `AskUserQuestion`, MCP tools or live
back-and-forth. A stage that writes code through Codex uses `run` with `--branch`
(`jobs.md` § Writing Jobs).

The stage dispatches an agent (Haiku, `low` effort, Bash and Read only). It runs the brief's
command, loops `wait` until a terminal state, and returns `result.json` and the deliverable
verbatim; when the submit prints `FAILURE:`, it returns that line unchanged:

```js
const result = await agent(
  "Run: ~/.claude/scripts/codex/codex-jobs consult /path/prompt.txt --model sol --effort high " +
    "--project-dir /path/repo --detach --idempotency-key <run-id>-<stage>\n" +
    "Use only Bash and Read. Repeat ~/.claude/scripts/codex/codex-jobs wait <id> " +
    "--max-seconds 540 while it prints RUNNING:, then run ~/.claude/scripts/codex/codex-jobs " +
    "result <id>. Return the STATE: line, that JSON and every .md file in its output_dir " +
    "verbatim. " +
    "If the submit prints FAILURE:, return that line and stop; never resubmit. " +
    "Copy the text exactly into each schema field: no summary, verdict or paraphrase.",
  { model: "haiku", effort: "low", schema }   // schema: { state, result_json, deliverable } strings
)
```

- The brief always carries `--detach` and `--idempotency-key <run-id>-<stage>`; a retried stage
  reattaches to the first job.
- The stage's own logic reads `state` and `result_json`, then verifies the deliverable's claims
  (`checking-output.md` § Claim Verification) the way the main loop would.
- Fan out N stages in one `Promise.all`; jobs past this Mac's cap wait in `queued`
  (`jobs.md` § Usage Rules).

## MCP Tools

Register the server once with
`claude mcp add codex-jobs -- ~/.claude/scripts/codex/codex-jobs mcp`. It serves:
`codex_capabilities`, `codex_review`, `codex_consult`, `codex_run`, `codex_status`,
`codex_result`, `codex_wait`, `codex_cancel`, `codex_resume`, `codex_fork`, `codex_steer`,
`codex_interrupt`. Each tool is the CLI subcommand of the same name without the
prefix, over the same job records, so a job started by one front end is read by the other.
`codex_run` takes `branch` or `worktree` and `allow_commit`. `codex_wait` returns the terminal
envelope (`result.json`); read the deliverable (`answer.md`, or `review.md` and `findings.md`)
from its `output_dir`. Use it where a tool call suits better than background Bash. Pass
`project_dir` on every start tool: the server's own working directory is the session's start
directory, not the worktree you are in.
