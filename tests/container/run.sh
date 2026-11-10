#!/usr/bin/env bash
# The release gate: builds a clean machine from the committed tree at HEAD with this repo
# installed one of the two ways the README gives, and runs it there. Needs Docker and a Claude
# subscription login.
#
#   tests/container/run.sh login             build, then open Claude Code to sign in once
#                                            (/login, then /exit); the login is kept in the
#                                            agent-harness-gate-login volume, copied into
#                                            each run's fresh config folder and saved back
#                                            when the run ends
#   tests/container/run.sh plugin [OUT_DIR]  install as a plugin and run
#                                            /agent-harness:self-optimize on invented sessions
#                                            (check.py says what passes)
#   tests/container/run.sh clone [OUT_DIR]   install from a clone (rules, skills, scripts and
#                                            hooks) and run one session per hook, rule and skill
#                                            probe (check_clone.py says what passes)
#
# The run's output, transcripts and files land in OUT_DIR (a new temp folder when omitted,
# printed at the end). Exits with the container's code: 0 passed, 1 a check failed, 2 no
# transcript or a wrong argument, 3 no login, 4 a clone probe was not attempted.
set -euo pipefail

mode=${1:-}
if [[ $mode != login && $mode != plugin && $mode != clone ]]; then
  echo "usage: run.sh login | plugin [OUT_DIR] | clone [OUT_DIR]" >&2
  exit 2
fi
target=$mode
[[ $mode == login ]] && target=plugin

repo=$(git rev-parse --show-toplevel)
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
git -C "$repo" archive HEAD | tar -x -C "$context"
docker build --quiet --target "$target" --tag "agent-harness-gate-$target" \
  --file "$context/tests/container/Dockerfile" "$context"

if [[ $mode == login ]]; then
  exec docker run --rm -it -v agent-harness-gate-login:/login -e CLAUDE_CONFIG_DIR=/login \
    --entrypoint claude agent-harness-gate-plugin
fi
out=${2:-$(mktemp -d)}
status=0
docker run --rm -v agent-harness-gate-login:/login -v "$out:/out" \
  "agent-harness-gate-$target" || status=$?
echo "output: $out"
exit "$status"
