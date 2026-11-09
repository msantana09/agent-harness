# Step 6 - post-merge steps and wrap-up

**Repo notes' post-merge steps**: once either cleanup block's `ls-remote` prints nothing
(merged on GitHub or on the user's instruction; for a stack, after the top PR's), run them.
Their results go under `Done:` / `Flags:` with the cleanup's.

`Done:` in the wrap-up cites the empty `ls-remote` line, not `gh`'s output.

The wrap-up then answers "what's left?" unasked, as its last two lines:

- `Left:` what this session raised and did not finish, each with its status (filed as issue
  `#<n>`, or not started), or `nothing left in this session`.
- `Issues:` the count of open issues at medium or higher that nobody has claimed, plus the
  first one by priority, then age, with its number and title. Count from the main tree, so a
  session rooted in another repo reads the merged repo's backlog:

  ```bash
  (cd <main tree> && gh issue list --state open --limit 1000 --json number,title,labels,createdAt \
    --jq 'map(select(.labels | map(.name) | any(IN("priority:low", "in-progress", "parked", "trigger-gated")) | not))
          | sort_by([(.labels | map(.name) | if any(. == "priority:high") then 0 else 1 end), .createdAt])
          | "\(length) open\(if length > 0 then ", first: #\(.[0].number) \(.[0].title)" else "" end)"')
  ```
