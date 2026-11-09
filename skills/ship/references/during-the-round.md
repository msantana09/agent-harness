# Step 2 - while the round runs, and its result

When `git -C <worktree> diff --name-only origin/<base>...HEAD | wc -l` prints 300 or more,
add `--timeout 3600` (the default is 1800 s). After a `FAILURE: timeout after Ns (job <id>)`,
`~/.local/share/agent-harness/codex-jobs/<id>/events.jsonl` (under `$XDG_DATA_HOME` when set)
journals what the run already reproduced; recover that before relaunching.

**While the round runs**, run Step 3's checks against the same head: the branch's own
tests (Step 3's tests line) and PR CI, waited on with `gh pr checks <N> --watch
--fail-fast` under `run_in_background` (CI usually finishes before Codex does). Workflow
runs register a few seconds after a push, so a `no checks reported` (exit 1) read within a
minute of one is re-read before the branch counts as covered by no workflow, which passes.
Edit nothing until the round returns, because a dirty tree invalidates it (below). A red
test or check joins the round's findings in Step 3 and is root-caused and fixed in the same
pass; a finding that only restates a red check folds into that fix.

On the completion notification, parse the `SUCCESS: <output-dir>` / `FAILURE:` line, then
read the review in `<output-dir>/review.md` (the full response contract; `findings.md` beside
it lists the same findings, Blocker written `critical`; `codex-jobs result <id>` prints
`result.json`). A `FAILURE:` line names its cause and `codex-jobs status <id>` shows the job's
`error` - diagnose it, don't silently retry. Then `git -C <worktree> status --short` must be
empty: the review sandbox is read-only, so a non-empty tree is an edit by the user or another
session while the review ran, and it invalidates the round. Never `checkout -- .` or
`clean -fd`. Report the dirty paths and stop the round; once their owner has committed or
discarded them, re-run the round on a clean tree.
