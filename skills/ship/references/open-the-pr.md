# Step 1 - open the draft PR

No PR yet → open one as a draft:

```bash
git -C <worktree> branch --show-current   # must print the feature branch, not <base>
gh pr create --draft --base <base> --title "<type(scope): summary>" --body-file <scratchpad>/pr-body.md
```

Body follows `rules/core-rules.md` § Pull Request Bodies. No attribution trailers.
Resolve open questions before writing the body - a PR body never carries them. The body
describes the diff, not the brief: read `git -C <worktree> diff origin/<base>...HEAD`
before naming a mechanism, and never copy one from the fix brief or an agent's report.
The same holds for every commit message: a pushed one cannot be corrected.
