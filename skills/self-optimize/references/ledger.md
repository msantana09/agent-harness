# The ledger

`~/.claude/self-optimize/ledger.md` records what each run scanned and every proposal the user
answered. Later runs read it to set their window, skip what was already decided, and check whether
an applied proposal worked. Create it with a `# Self-optimize ledger` heading when it is missing.

## Scanned lines

Once step 2 has finished, append one line for the machine, named as `hostname -s` prints it:

```markdown
Scanned <run date>: <machine>; <each transcript folder>; through <yesterday>
```

The line stops at yesterday because today's messages are still coming in. A folder whose scan or
correction pass failed is left off the line, so its days are scanned next time. A run that stops
before step 2 finishes writes no line.

A folder's next window starts the day after the through date on the newest Scanned line that names
it on that machine, which is the run date of that line.
Folders with different start dates are scanned in separate runs of steps 1 and 2.

## Entries

One entry per proposal, whether or not it was applied:

```markdown
## <date>: <the behavior, in one line>
- Files: <path> (edit), <path> (new file), <path> (delete)
- Change: <one line on what the proposal does>
- Measure: <the kind of correction, or the hook block by its name in the scan; when the change
  targets one pattern the hook checks, that pattern's quoted value as the scan's `--json` prints it>
- Baseline: <count> in <sessions with activity> sessions, <window>, <machine>, <transcript folders>
- Eval: <current score> vs <edited score>, <number of held-out cases>
- Status: applied <date>
```

When a change was skipped at apply, the Status line says which and why:
`applied <date>, except <path>: <reason>`.

When the user asks for another attempt instead of answering yes or no, the entry is written once,
for the attempt they finally answered: its Files, Change and Eval lines describe that attempt, and
each dropped attempt gets one line after Eval, so the next run does not try it again:

```markdown
- Earlier attempt: <files>, <current score> vs <edited score>; <why it was dropped>
```

The entry keeps a single Status line.

## States

The Status line holds what the user decided. A Re-count line holds what the measure showed.

| Status | Means | Later runs |
|---|---|---|
| `applied <date>` | The changes landed | Re-count it until a final verdict |
| `declined <date>` | The user said no | Do not propose it again |
| `reverted <date>` | The user undid it | Do not re-count it; propose it again only anew |

Set `reverted` when the user says they undid the changes.

| Verdict | When |
|---|---|
| `worked` | The rate is at most half the baseline even with one more correction counted, over at least as many sessions, and the previous Re-count line reads `on track`. Final until its files change |
| `on track` | The rate is at most half the baseline, over at least as many sessions, but `worked` does not hold yet |
| `did not work` | The rate is above half the baseline, over at least as many sessions |
| `too early` | Fewer sessions than the baseline window |
| `no baseline` | No baseline was recorded, or it was under 5, too few to show a drop. Final |
| `not countable` | The measure cannot be counted from transcripts. Final |

## Re-counting

Re-count every `applied` entry whose newest Re-count line is missing, `on track`, `did not work`
or `too early`. Count on the machine and folders of its baseline; an entry from another machine
waits for a run there.

Re-count a `worked` entry too when any file on its Files line changed after the date of its
newest Re-count line: a commit to it (`git log --since=<that date> -- <file>` in the repository
that holds it), or, for a file outside a repository, a modification time after that date. A
later edit can undo the fix, and the count is the only check that would notice. The new line
continues from the `worked` line's count, and its verdict replaces `worked`.

Each Re-count line is cumulative from the day after the apply date, so the newest line is the
whole count and earlier lines are never summed with it:

```markdown
- Re-count <run date>: <count> in <sessions> sessions, <day after apply> through <date>; <verdict>
```

- **Corrections** are never classified twice, since step 2 is paid. The first line counts
  this run's step 2 lines of that kind dated from the day after the apply date. A later line takes
  the newest line's count and adds this run's step 2 lines dated after its through date.
- **A hook measure** comes from the scan, which is free to re-run: run `scripts/scan.py` from the
  day after the apply date through yesterday on the baseline's folders and read the hook block by
  its name, or that value's count in the scan's `--json` when the measure names a quoted value.
- **Sessions** are the sessions with activity in that same scan.
- **Add what the change created**, such as a new hook block or a new kind of correction, to the
  count.

Compare the rate per session with the baseline and write the verdict from the table above.
