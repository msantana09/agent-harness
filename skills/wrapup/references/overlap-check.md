# Step 1 - check `<base>` for overlapping work

**Check `<base>` for overlapping work** before anything is spent on review. Parallel
sessions fix the same issues independently, so a PR can be reviewed and fixed in full
and still be dead on arrival.

```bash
git -C <worktree> fetch origin <base>
git -C <worktree> merge-tree --write-tree origin/<base> HEAD > /dev/null || echo "conflicts with <base>"
```

Then, once per issue the branch fixes (the `#<n>` references in the PR title and body or
the commit messages; none → skip), as its own command - a worktree-isolated session
refuses the same query wrapped in a shell loop:

```bash
gh issue view <n> --json number,state,closedByPullRequestsReferences --jq '"#\(.number) \(.state) linked PRs: \([.closedByPullRequestsReferences[].number])"'
```

An issue that reads `CLOSED` - with a PR other than this one, or with none (closed by
hand or as a duplicate) → `<base>` already holds that fix, or the work is moot: stop
before any review round. Compare the two changes (`gh pr diff <merged>` against
`git -C <worktree> diff origin/<base>...HEAD`). With no delta worth keeping, don't open
a PR (or close the existing one) as superseded, with a link to the merged one. With a
verified delta, stop this `/wrapup` too: the delta goes onto a fresh branch from
`origin/<base>` (through the repo's worktree script where it has one) and its own
`/wrapup`. Say which in the turn-final message. An `OPEN` issue linked to another open PR
→ a parallel session is on it; name that PR in the turn-final message. Conflicts alone
→ merge `origin/<base>` into the branch, resolve, and continue.
