#!/usr/bin/env bash
# Run a long command, keep its full output on disk, and return a Haiku-written
# failure digest instead of the raw dump, so pytest/vitest/mypy/build output
# stays out of the agent's context. Full output stays at <out-file> for
# grep/tail.
#
#   triage_run.sh <out-file> -- <command...>
#
# Exit code is the wrapped command's exit code. On success prints one line;
# on failure prints the digest (failing item, location, first message).
#
# TRIAGE_RUN_RECORDER, when set, names an executable that keeps a record of
# the run: `<recorder> start <out-file> -- <command...>`, with TRIAGE_RUN_PID
# set to this script's pid, prints a record id; `<recorder> end <id> <exit
# code> <output file>` closes it with this run's own output. Best-effort: this
# script's stdout lines and exit code are the same whether or not a record
# gets written.
set -uo pipefail
if [[ $# -lt 3 || "$2" != "--" ]]; then
  echo "usage: triage_run.sh <out-file> -- <command...>" >&2; exit 2
fi
out="$1"; shift 2
mkdir -p "$(dirname "$out")"
recorder="${TRIAGE_RUN_RECORDER:-}"
rec=""
if [[ -n "$recorder" ]]; then
  rec=$(TRIAGE_RUN_PID="$$" "$recorder" start "$out" -- "$@") || rec=""
fi
# The record snapshots this run's own output file; <out-file> is a hard link
# to it. A concurrent run reusing the same <out-file> repoints the name
# instead of truncating and interleaving the output this record reads. A
# post-hoc copy of <out-file> can't replace this: by the time this run's
# command exits, a concurrent run's truncation may already have overwritten
# bytes this run wrote, so only writing to a private file from the start
# avoids it. Skipped when <out-file> is already a directory, so that case
# fails exactly like a plain `>` redirect always has: no run, nothing
# created inside it.
cap=""
# EXIT covers a normal return and TERM/INT/HUP (Bash's own 2-min timeout,
# TaskStop), set before the file exists so a signal while it is being
# linked still removes it; it can't fire for KILL -9, which leaves it behind.
trap '[[ -z "$cap" ]] || rm -f "$cap"' EXIT
if [[ -n "$rec" && ! -d "$out" ]] && cap=$(mktemp "$(dirname "$out")/.triage-run.XXXXXX") \
    && ! ln -f "$cap" "$out"; then
  rm -f "$cap"; cap=""
fi
"$@" >"${cap:-$out}" 2>&1
rc=$?
if [[ -n "$rec" ]]; then
  "$recorder" end "$rec" "$rc" "${cap:-$out}" || true
fi
[[ -z "$cap" ]] || rm -f "$cap"
lines=$(wc -l <"$out" | tr -d ' ')
if [[ $rc -eq 0 ]]; then
  echo "ok (exit 0, $lines lines) full output: $out"
  exit 0
fi
schema='{"type":"object","properties":{"summary":{"type":"string"},"items":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"},"location":{"type":"string"},"message":{"type":"string"}},"required":["name","location","message"]}}},"required":["summary","items"]}'
prompt="This is a failed command's output (exit $rc): $*. Output over 120 KB arrives as its grep-matched failure lines, first 10 KB and last 120 KB. List every distinct failing test/error: name, file:line if present, and the first assertion or error message (one line each, verbatim). Do not speculate about causes. summary = one sentence with counts."
# Output over 120 KB keeps its last 120 KB and gains its grep-matched failure lines (with 2
# lines of after-context) and first 10 KB: vitest can print failure names near the top and
# end on DOM dumps, where the tail holds none of them. ~140 KB stays under Haiku 5.5's
# 100K-token rate card.
digest_input() {
  if (( $(wc -c <"$1") <= 120000 )); then
    cat "$1"
    return
  fi
  printf '[failure lines]\n'
  grep -nE -A2 '^(FAILED|ERROR) |^ *FAIL |^ +(×|✗|✕) |^E  |: error:|error TS[0-9]+' "$1" | head -c 10000
  printf '\n[first 10 KB]\n'
  head -c 10000 "$1"
  printf '\n[last 120 KB]\n'
  tail -c 120000 "$1"
}
parse='
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
if d.get("is_error"):
    print(str(d.get("api_error_status") or d.get("subtype")) + ": " + str(d.get("result"))[:200])
    sys.exit(1)
r = d.get("structured_output")
if not isinstance(r, dict) or "summary" not in r:
    print("no digest in the reply (stop_reason " + str(d.get("stop_reason")) + ")")
    sys.exit(1)
print(r["summary"])
for it in r.get("items", []):
    print("- " + it["name"] + "  " + it["location"])
    print("    " + it["message"])
'
# Run from the output dir so no project CLAUDE.md loads; empty setting sources skip settings
# hooks and plugins, and unsetting CLAUDE_CODE_PLUGIN_DIRS skips the plugins it lists, which
# load even with empty setting sources (not --bare: it skips the keychain, so auth fails). A
# digest is 18-77K prompt tokens on Haiku 5.5. When the digest fails, claude's stderr and JSON
# reply stay in <out-file>.digest-err.
err="$out.digest-err"
raw=$(digest_input "$out" | (cd "$(dirname "$out")" && env -u CLAUDE_CODE_PLUGIN_DIRS claude -p --setting-sources "" --disable-slash-commands \
  --model haiku --tools "" --no-session-persistence \
  --output-format json --json-schema "$schema" "$prompt" 2>"$err"))
crc=$?
if digest=$(printf '%s' "$raw" | python3 -P -c "$parse"); then
  rm -f "$err"
else
  printf '%s\n' "$raw" >>"$err"
  digest="(digest unavailable: ${digest:-claude -p exit $crc}; claude output in $err)"
fi
echo "FAILED (exit $rc, $lines lines) full output: $out"
echo "$digest"
exit $rc
