# Step 6 - the merge watch

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
