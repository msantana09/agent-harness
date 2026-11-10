#!/usr/bin/env bash
# The README's install from a clone, command for command. Run from the folder that holds the clone, as the README says.
set -euo pipefail

mkdir -p ~/.claude/rules ~/.claude/skills ~/.claude/scripts ~/.claude/hooks ~/.agents/skills
ln -s "$PWD"/agent-harness/rules/*.md ~/.claude/rules/
ln -s "$PWD"/agent-harness/skills/* ~/.claude/skills/
ln -s "$PWD"/agent-harness/scripts/*.sh ~/.claude/scripts/
ln -s "$PWD"/agent-harness/scripts/codex ~/.claude/scripts/
ln -s "$PWD"/agent-harness/skills/inspect-pr ~/.agents/skills/
cp -R agent-harness/hooks/. ~/.claude/hooks/
[ -e ~/.claude/settings.json ] || cp agent-harness/settings.json ~/.claude/settings.json
