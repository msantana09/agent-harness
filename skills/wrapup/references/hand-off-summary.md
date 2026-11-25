# Step 5 - hand-off summary

Hand-off summary. It opens with a **`What this is:`** recap: PR number plus 1-2 lines
in plain terms, written for someone switching in from other work cold. Say what now
works or changed for a user or operator, and why it matters. Name no files, functions,
or review mechanics, and give it no background. Write it against the final pushed head,
after the last round's fixes. A scope shift during review shows up here, not in the
original PR title.
Example: `What this is: #<N> — the export now includes every row instead of stopping at
10,000, so large reports download in full.`
Then the status block, phone-glance budget (≤5 lines, `Done:` / `Found:` / `Flags:` /
`Next:`, skipping empty slots): rounds run, accepted-vs-rejected counts, the `Repo notes:` line
from Step 1, anything the user must know before merging. `Flags:` also names what this change
touches that the green checks never exercised: a flow no test runs, an environment CI skips, a
page nobody loaded. It also names what no reviewer read: every change pushed after the head the last
round reviewed (that round's fixes, a stacked parent's merge and its conflict
resolutions, any later fix), as unreviewed. While one exists the summary never calls the
PR reviewed, complete or safe to merge. Below the status block, a `Not requested:` line per
addition or remedy logged `REJECT not requested` (valid and worth having, outside the
request), in plain words with what it would add; the round block below the PR divider is not
read, so this line is the only place the user sees it. `Next:` is at most "waiting on your
merge" - never the Step 6 cleanup commands, which run on the merge event, not by the user.
