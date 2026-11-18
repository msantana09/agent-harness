# Step 3 - checks

- **Static**: read PR CI with `gh pr checks <N>`, and run a lint or type check locally
  only to reproduce a red one.
- **Tests** (the tests line): the test files the branch adds or changes, plus the
  existing tests named for a module it changes (`test_<module>.py`; `<name>.test.ts(x)`
  beside `<name>.ts(x)` or in its `__tests__/`). A changed module with no named test
  takes the test files that import it (`rg -l <its import path>` over the tree's tests);
  with none of those either, the narrowest existing test that exercises it. Never call
  a runner with an empty list: with no files it runs the whole suite. The runner and how
  to invoke it come from the repo notes, else from the repo's `CLAUDE.md`. A green run
  of those files after the last edit counts; don't repeat it.

After a fix pass, rerun only the tests the fixes touched: beside the next round, or
before Step 5 when none follows. CI reruns itself on the push. Redirect each run to its
own temp file and read pass/fail counts from that file, never from a fix agent's
summary; a fix agent's brief carries the tests line verbatim. A red that also fails on
`origin/<base>` is pre-existing: file it (`gh issue create --label
finding,priority:<level>`), link it in the PR body below the divider, and don't fix it
here. A red is root-caused before any edit (hard rules). A red test of behavior the PR set out
to change, other than a contract or a past bug, is deleted or rewritten to the new behavior,
never patched until it passes; a deleted one is listed in the PR body with its reason. A red the
PR did not
intend is a bug to fix in the code (`rules/engineering.md` § Tests and Docs Ship with the Change).
