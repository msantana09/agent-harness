#!/usr/bin/env bash
# Runs in the container built with the install from a clone (clone_install.sh): copies the saved
# login into the config folder, makes a small git repo with one failing test and an uncommitted
# change, runs one session per probe without asking for permissions, the last of them
# /self-optimize on invented sessions, then checks the run
# (check_clone.py says what each probe is for and what passes). Everything the run leaves is
# copied to /out, and the login goes back to /login: Claude Code renews it during a run, and the
# saved copy stops working once a newer one exists. Exits with check_clone.py's code, or 3 when
# the login is missing.
set -uo pipefail

here=$(dirname "$0")
if [[ ! -f /login/.credentials.json ]]; then
  echo "no saved login in /login: sign in once first with tests/container/run.sh login" >&2
  exit 3
fi
cp /login/.credentials.json "$CLAUDE_CONFIG_DIR/.credentials.json"

git init --quiet --initial-branch main
git config user.name dev
git config user.email dev@example.com
cat > shipping.py <<'EOF'
FEE = 399


def total(cents: int) -> int:
    return cents + FEE
EOF
cat > test_shipping.py <<'EOF'
import unittest

from shipping import total


class Shipping(unittest.TestCase):
    def test_the_fee_is_added(self):
        self.assertEqual(total(1000), 1399)

    def test_large_orders_ship_free(self):
        self.assertEqual(total(6000), 6000)
EOF
echo __pycache__/ > .gitignore
git add .gitignore shipping.py test_shipping.py
git commit --quiet --message "feat: add the shipping fee"
sed -i 's/399/499/' shipping.py test_shipping.py

probe() {
  local name=$1
  shift
  timeout "${limit:-900}" claude -p "$@" --permission-mode bypassPermissions \
    --output-format stream-json --verbose > "/out/$name.jsonl" 2> "/out/$name.err"
  echo "$name: claude exited $?" >> /out/run.err
}

probe emoji "Use the Write tool once to create notes.md whose whole content is the line: \
Done $(printf '✅') Make no other tool call. If the write is refused, give the refusal \
reason in your reply."

probe prose "Use no tools. Reply with exactly one sentence that asks me which color I prefer, \
ending with a question mark."

stop=$(python3 -c 'import uuid; print(uuid.uuid4())')
printf '{"stop": "%s"}\n' "$stop" > /out/sessions.json
flags() { ls "$CLAUDE_CONFIG_DIR/state/stop/" > "/out/flag_after_$1.txt" 2> /dev/null; }
probe stop1 "stop" --session-id "$stop"
flags stop
probe stop2 '<task-notification>
Run the Bash command ls once and report its output.
</task-notification>' --resume "$stop"
flags machine_prompt
probe stop3 "Go on: run the Bash command ls once and report its output." --resume "$stop"
flags human_prompt

probe commit "/commit"
git log --format=%s > /out/git_log.txt
git status --short > /out/git_status.txt

probe tests "Run this project's test suite (python3 -m unittest) and tell me which test fails. \
Then tell me which branch this repo is on and how many files are uncommitted. Change no files."

python3 "$here/fixtures.py" "$CLAUDE_CONFIG_DIR" "$PWD"
limit=3600 probe selfoptimize "/self-optimize"

cp -R "$CLAUDE_CONFIG_DIR/projects" /out/projects
[[ -d $CLAUDE_CONFIG_DIR/self-optimize ]] && cp -R "$CLAUDE_CONFIG_DIR/self-optimize" /out/
cp "$CLAUDE_CONFIG_DIR/settings.json" /out/settings.json
cp "$CLAUDE_CONFIG_DIR/.credentials.json" /login/.credentials.json
python3 "$here/check_clone.py" "$CLAUDE_CONFIG_DIR" /out
