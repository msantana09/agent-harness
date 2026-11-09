# Job Control, Review and Writing Jobs

## 5. Resume, Fork, Steer, Interrupt, Cancel

| Command | Use |
|---------|-----|
| `resume <job-or-thread-id> <prompt-file>` | A follow-up in the same thread, with its history; a new consult job, except that resuming a finished `run` job continues it with write access in the same worktree |
| `fork <job-or-thread-id> <prompt-file>` | A new job that branches from the thread, leaving the original thread untouched |
| `steer <id> <text>` | Add direction to a running turn ("stop at the auth path") |
| `interrupt <id>` | Stop the running turn; the job ends `interrupted` |
| `cancel <id>` | Stop the job; it ends `cancelled` |

`steer` and `interrupt` need a job with a running turn; otherwise they print `FAILURE:`. `resume`
and `fork` accept the flags of `consult`; given a job id, they run in that job's working directory
with its model and effort unless `--model` or `--effort` says otherwise. Resuming a `run` job
keeps its write class, `allow_commit`, branch and base commit (`commits` stays measured from the
original base) and hands the worktree lease to the new job; it fails while the run job is still
live, after `release`, once another job holds the worktree, or once the worktree is off that
branch (or on a protected one); uncommitted edits there are kept for the resumed job. A fork runs
read-only. A fork of a `run` job that gets its own worktree (`--branch` or `--worktree`, as in
`run`) keeps that job's write access; pointed at the source's worktree it runs read-only.

## Review Jobs

```bash
~/.claude/scripts/codex/codex-jobs review "$PROMPT_FILE" --project-dir <repo> --effort high
```

`review` takes the flags of `consult` (`../SKILL.md` § 3. Submit), runs read-only, and is what
`/ship` launches. Codex answers with a structured review; on `SUCCESS: <output-dir>` the directory
holds:

| File | Content |
|------|---------|
| `review.md` | The Markdown review as Codex wrote it; a completed review must carry `## Summary` + `## Review Findings` or `## Findings` + `## Verdict` |
| `findings.json` | The findings, severity `critical`/`high`/`medium`/`low` (a Blocker is `critical`), ids assigned by the codex-jobs findings validator |
| `findings.md` | `findings.json` rendered by that validator: the file a caller merges, only when `result.json` reports the review succeeded |
| `attestation.json` | Repo root, branch, merge base, head and diff hashes, `job_id`, model, effort; `exit_status` is `ok` only when head and diff at completion equal those at launch; `uncommitted_tracked` is the tracked files' `git status --porcelain` at launch. A push gate reads it from `<repo>/.artifacts/codex/**` |
| `result.json` | As in `consult` (`../SKILL.md` § 4. Wait for the Result); `review_approved` is `true` when the verdict is `approve`, `false` when it needs attention, null unless the review completed |

A review the model answers `blocked` or `failed`, one missing the headings, one whose findings
the validator rejects, or one whose head or diff moved while it ran (`head moved during review`:
relaunch on the new head) prints `FAILURE:` (exit 1) and keeps its files; its `result.json` says
`task_succeeded: false`. A review outside a git repo,
in one with no commit yet, or in one without `origin/HEAD`, runs and is never attested `ok`
(`git remote set-head origin --auto` sets the base). Read `review.md` for the full review and
`findings.md` for the list.

`review --native --target uncommitted|base:<branch>|commit:<sha>` runs Codex's built-in reviewer
instead (no prompt file, no `--image`): `review.md` is its prose message, `findings.json` and
`findings.md` are absent, `review_approved` is null, and `attestation.json` is written as above.
Only a `base:<branch>` review whose merge base with HEAD is the repo's default-branch merge base
(`base:origin/dev` in a repo whose `origin/HEAD` is dev) is attested `ok`; `uncommitted`, `commit:`
and any other base cover less than the branch's diff, so the attestation says `not attestable` and
a push gate rejects it. Codex's reviewer diffs the working tree, so a `base:` review is also
`not attestable` when tracked files have staged or unstaged changes at launch or at completion:
commit or discard them first. `--target` is required with `--native` and refused without it; the
MCP `codex_review` tool takes `native` and `target`.

## Writing Jobs

```bash
~/.claude/scripts/codex/codex-jobs run <prompt-file> --model luna --effort low \
  --project-dir <repo> --branch codex/<topic>      # the service creates and leases a worktree
~/.claude/scripts/codex/codex-jobs run <prompt-file> --model sol --effort high \
  --worktree <clean linked worktree>               # or one you made
```

- `--model` and `--effort` are required: `run` has no defaults.
- The job writes only inside its worktree, never a main checkout or a `dev`/`uat`/`main` branch,
  and one job holds a worktree at a time (a second gets `FAILURE: … is leased by <job-id>`).
- Codex does not commit unless `--allow-commit` is given; the caller reviews and commits.
- The sandbox makes only the job's worktree writable. With `--allow-commit` it also opens what a
  commit from a linked worktree writes in the main repo's `.git`: the worktree's own git dir,
  `objects/`, `refs/heads/` and `logs/refs/heads/`. A committing job can therefore move any local
  branch of that repo; tags, remote-tracking refs, `packed-refs`, `config` and the main checkout's
  working tree stay read-only.
- `result.json` `changed_files` and `commits` come from git, not from the model; `answer.md` holds
  Codex's summary and any blockers. Verify the diff before building on it.
- `codex-jobs release <id>` removes a finished job's service-created worktree (it refuses one with
  uncommitted edits) or drops the lease on yours.

## Usage Rules

- Admission is the service's job: at most `max_concurrent_jobs` (4) jobs run at once on this Mac
  and the rest wait in `queued`; before each turn the job reads Codex's live usage and refuses past
  the reserve (15%) with `FAILURE: usage limit (resets <time>)`. Submit a whole batch at once; do
  not count usage or walk `~/.codex/sessions/**/rollout-*.jsonl` yourself.
  `codex-jobs capabilities` shows the live numbers.
- Resubmit with the same `--idempotency-key` after an unclear submission: it returns the first
  job's id and starts nothing.
