# Security

The hooks in `hooks/` run on your machine on every tool call once you register them, and the
scripts in `scripts/` run commands in your repositories. Read what you install. Every Codex job,
read-only reviews included, runs with network access and, when `gh` is signed in to the account
named for the repo's owner, that account's token, so only the prompt keeps a job from pushing or
writing through the GitHub API
([details](skills/codex/references/jobs.md#network-and-github-access)).

To report a vulnerability, use GitHub's private vulnerability reporting: the "Report a
vulnerability" button under this repo's Security tab. Please do not open a public issue for it.
