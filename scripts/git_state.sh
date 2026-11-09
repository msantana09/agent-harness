#!/usr/bin/env bash
# One-line git state for a directory: branch, HEAD, upstream drift, dirty count and worktree
# root, in place of a `git status --short`, `git branch --show-current` and `pwd` chain.
#   git_state.sh [dir] [base-ref]
# With base-ref (origin/main), also prints behind_base=N: commits on base-ref that HEAD lacks.
# A stacked branch's upstream is its stack parent, so the upstream drift alone never shows how
# far the stack trails the integration branch.
set -euo pipefail
dir="${1:-.}"
base="${2:-}"
root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo: $dir"; exit 1; }
branch=$(git -C "$dir" branch --show-current)
[[ -z "$branch" ]] && branch="DETACHED"
head=$(git -C "$dir" rev-parse --short HEAD)
dirty=$(git -C "$dir" status --porcelain | wc -l | tr -d ' ')
drift="no-upstream"
if git -C "$dir" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  read -r ahead behind < <(git -C "$dir" rev-list --left-right --count '@{u}...HEAD' | awk '{print $2, $1}')
  drift="ahead=$ahead behind=$behind"
fi
if [[ -n "$base" ]]; then
  git -C "$dir" rev-parse --verify --quiet "$base^{commit}" >/dev/null || { echo "unknown base-ref: $base" >&2; exit 1; }
  drift="$drift behind_base=$(git -C "$dir" rev-list --count "HEAD..$base")"
fi
# sed reads the whole listing: a reader that quits after line one would SIGPIPE git under pipefail.
main_root=$(git -C "$dir" worktree list --porcelain | sed -n '1s/^worktree //p')
kind="main-tree"; [[ "$main_root" != "$root" ]] && kind="worktree"
echo "branch=$branch head=$head $drift dirty=$dirty $kind=$root"
