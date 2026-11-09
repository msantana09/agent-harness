#!/usr/bin/env bash
# The release gate: builds a clean machine from the committed tree at HEAD, installs this repo in
# it as a plugin, and runs /agent-harness:self-optimize there on invented sessions (check.py
# says what passes). Needs Docker and a Claude subscription login.
#
#   tests/container/run.sh login      build, then open Claude Code to sign in once (/login, then
#                                     /exit); the login is kept in the agent-harness-gate-login
#                                     volume and copied into each run's fresh config folder
#   tests/container/run.sh [OUT_DIR]  build and run; the run's output, transcripts and
#                                     self-optimize files land in OUT_DIR (a new temp folder
#                                     when omitted, printed at the end)
#
# Exits with the container's code: 0 passed, 1 a check failed, 2 no transcript, 3 no login.
set -euo pipefail

repo=$(git rev-parse --show-toplevel)
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
git -C "$repo" archive HEAD | tar -x -C "$context"
docker build --quiet --tag agent-harness-gate --file "$context/tests/container/Dockerfile" "$context"

if [[ ${1:-} == login ]]; then
  exec docker run --rm -it -v agent-harness-gate-login:/login -e CLAUDE_CONFIG_DIR=/login \
    --entrypoint claude agent-harness-gate
fi
out=${1:-$(mktemp -d)}
status=0
docker run --rm -v agent-harness-gate-login:/login:ro -v "$out:/out" agent-harness-gate || status=$?
echo "output: $out"
exit "$status"
