#!/usr/bin/env bash
# Runs in the container: copies the saved login into the fresh config folder, writes the invented
# sessions, runs /agent-harness:self-optimize once without asking for permissions, then checks the
# run. Everything the run leaves (its output, transcripts and self-optimize files) is copied to
# /out, and the login goes back to /login: Claude Code renews it during a run, and the saved
# copy stops working once a newer one exists. Exits with check.py's code, or 3 when the login is
# missing.
set -uo pipefail

here=$(dirname "$0")
if [[ ! -f /login/.credentials.json ]]; then
  echo "no saved login in /login: sign in once first with tests/container/run.sh login" >&2
  exit 3
fi
cp /login/.credentials.json "$CLAUDE_CONFIG_DIR/.credentials.json"
python3 "$here/fixtures.py" "$CLAUDE_CONFIG_DIR" "$PWD"

session=$(python3 -c 'import uuid; print(uuid.uuid4())')
timeout "${GATE_TIMEOUT:-3600}" claude -p "/agent-harness:self-optimize" \
  --session-id "$session" --permission-mode bypassPermissions \
  --output-format stream-json --verbose > /out/run.jsonl 2> /out/run.err
echo "claude exited $?" >> /out/run.err

cp -R "$CLAUDE_CONFIG_DIR/projects" /out/projects
[[ -d $CLAUDE_CONFIG_DIR/self-optimize ]] && cp -R "$CLAUDE_CONFIG_DIR/self-optimize" /out/
cp "$CLAUDE_CONFIG_DIR/.credentials.json" /login/.credentials.json
python3 "$here/check.py" "$CLAUDE_CONFIG_DIR" "$session" /out/run.jsonl
