# Step 4 - round budget

Whether another round runs is decided by the accepted findings of the round just triaged:

| Round just finished | Accepted findings | Next |
|---|---|---|
| 1 | none | Step 5 |
| 1 | Lows only | fix (Step 3), Step 5 |
| 1 | a Blocker, High, or Medium | fix (Step 3), run round 2 |
| 2 or later | no Blocker or High | fix (Step 3), Step 5 |
| 2 or later | a Blocker or High | fix (Step 3), run the next round |

A next round re-runs Step 2 against the new head with the round number bumped; the
previous rounds' logs are already on the PR body, which is where `inspect-pr` reads them.
Rejected and filed findings never count, nor does an accepted one whose fix touches no file
in the diff (a PR-body step such as an After merge line): a next round has nothing of it to
read. The fixes from the last round are pushed without a covering round - no confirmation
round, whatever the delta.

After each round, append that round's ACCEPT/REJECT/FILE log to the PR body **below the `---`
divider** (the agent-audience half - the user does not read it, a future debugger does):

```bash
gh pr view <N> --json body --jq .body > <scratchpad>/pr-body-current.md
# append the round block, then:
gh pr edit <N> --body-file <scratchpad>/pr-body-current.md
```

Round block shape - one line per finding, no prose, and no `Open question` lines (a fact
goes in as a fact; anything unresolved is resolved before the block is written):

```markdown
### Codex review round <N> — <output-dir>

- ACCEPT · High · `path/to/file.py:120` — <reason> → fixed in <sha>
- REJECT · Medium · `path/to/other.ts:44` — <reason the claim does not hold>
- FILE · Medium · `path/to/third.py:88` — <why pre-existing or outside this PR> → #<issue>
```

The review trail lives on the PR, not in this session's scrollback.
