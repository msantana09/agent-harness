# Step 6 - cleanup and merge by instruction

## The cleanup block

On the merged event (the user merged on GitHub): the cleanup block below, then the repo notes'
post-merge steps (`references/wrap-up.md`). One Bash call, and its first line moves the
shell into the main tree: the worktree is about to be deleted, and a later line run from a
deleted cwd fails with "Unable to read current working directory". This is the one place a
session-wide `cd` is required; the Bash tool's cwd persists, so the next call starts in the
main tree too.

From an `EnterWorktree` session, call `ExitWorktree(action: "keep")` before either block
below, in the same turn: the worktree-isolation hook refuses the `cd` into the main tree and
every git line after it. "Merge it" is the request to leave
the worktree; never hand the user a phrase to say. Don't route around either hook (e.g.
`gh api -X DELETE`). A session that never ran `EnterWorktree` but whose cwd sits in the
worktree (it `cd`-ed there) skips `ExitWorktree`, a no-op for it, and wraps the block in a
subshell: `(cd <main tree> || exit 1` as the first line, the block's other lines unchanged
on their own lines, then `)`. A Bash guard on worktree sessions cannot tell the two
sessions apart, so the subshell form is the one that passes. Once the worktree is
gone, the harness moves the session's cwd to the main tree. Hand the cleanup to a main-tree
session only if a hook actually refuses it.

The gate names `<branch>`, not `<N>`: `gh pr view <branch>` resolves that branch's own PR
(merged or closed ones too, after the remote branch is gone). A gate on a number can let a
merged PR clear the delete of another, closed PR's branch. A branch whose PR closed without
merging is not `/ship`'s to delete: the user deletes it.

`<remove worktree>` is the repo notes' worktree-removal command when they name one, else the
repo's own worktree-removal script when it ships one (raw `git worktree remove` skips its
teardown). Without either it is `git worktree remove <worktree>`, and when `<worktree>` is the
main tree itself (Step 1) it is `git switch <base>`. `<sync>` is
`git pull --ff-only origin <base>` unless the repo notes replace it. `<merge flag>` is the repo
notes' merge method, else `--squash`.

```bash
cd <main tree>
[ "$(gh pr view <branch> --json state --jq .state)" = MERGED ] || { echo "PR <N> not merged"; exit 1; }
<remove worktree>
[ "$(git branch --show-current)" = <base> ] || { echo "main tree not on <base>"; exit 1; }
<sync>
git branch -D <branch> 2>/dev/null || true               # a squash merge leaves it "not fully merged"; -d refuses
git push origin --delete <branch> 2>/dev/null || true    # GitHub may have deleted it already
git ls-remote --heads origin <branch>                    # must print nothing; this line is the completion check
```

## Merge-by-instruction sequence

Merge-by-instruction sequence (the user said "merge and pull" / "merge it" in chat). Merge
from the main tree in one Bash call, then run the cleanup block above as the next call: the
auto-mode classifier can deny a whole call that chains other writes onto a merge. Nothing
that removes the worktree or a branch runs until the PR reads MERGED. A refused merge stops at
the cleanup's gate with the worktree and branch untouched:

```bash
cd <main tree>
gh pr ready <N> && gh pr merge <N> <merge flag>          # drafts refuse the merge; no --delete-branch — the worktree still holds the branch
```

Stacked PRs on the merge-by-instruction path only: merge bottom-up from the main tree, each
merge gated on MERGED before the next, and before each upper merge confirm `gh pr view
<next> --json baseRefName --jq .baseRefName` prints `<base>`; if it does not, `gh pr edit
<next> --base <base>` before merging. Run the cleanup lines for every stack worktree only
after the top PR reads MERGED, with one gate line per branch the command deletes.

After a lower squash merge, the next PR can show add/add conflicts in files the lower PR added.
When `git diff <lower PR head> origin/<base> -- <file>` is empty, the upper branch's side is
correct.

## Re-lands

Re-lands (stacked or not): never re-land by `git diff base branch | git apply` onto a moved
`<base>`: it reverts whatever `<base>` learned since the branch's last merge-forward. Merge
`<base>` into the branch and let the PR merge instead. If a content-diff re-land is forced,
first cherry-pick every commit in `git log <merge-forward parent>..<re-land base>` that touches
the diffed files.
