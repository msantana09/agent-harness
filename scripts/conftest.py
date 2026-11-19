"""Setup for every test under scripts/: no inherited variable points git at a repository.

`git rebase -x` from a linked worktree, and git hooks, export GIT_DIR to the commands they run.
Inherited by the tests, it sends their fixtures' `git init`, commits and pushes to the caller's
repository instead of their temp repos. The variables are dropped from this process's
environment when the conftest loads, before any test module builds an environment from it; the
list is git's own (`git rev-parse --local-env-vars`).
"""

import os
import subprocess

for name in subprocess.run(
    ["git", "rev-parse", "--local-env-vars"], check=True, capture_output=True, text=True
).stdout.split():
    os.environ.pop(name, None)
