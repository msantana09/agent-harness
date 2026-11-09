---
name: transcripts
description: Use whenever you need to know what happened in past Claude Code sessions - what the user typed, what the agent said, which tools it ran and what they returned, or the context around one message - instead of opening or parsing the .jsonl transcript files yourself. Reads every session through a local search index and prints readable text, or JSON Lines for scripts.
---

# Transcripts

Read past Claude Code sessions with `scripts/read.py`. It prints plain text you can read as it
stands: do not open the `.jsonl` files, and do not pipe the output into code to parse it. Add
`--json` only when a script consumes the rows.

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/read.py <command> [options]
```

The first query after new sessions waits while the index catches up; a first query over months
of history can take a few minutes. `read.py index` builds it ahead of time.

## Find messages and tool calls

- `messages --typed --search 'word*'`: what the user typed. `--typed` drops skill text, hook
  feedback, compaction summaries and other text the harness added; without it those rows print
  with their label (`user (meta)`). `--role assistant` gives the agent's replies.
- `tools --tool Bash --match 'pytest'`: tool calls. `--results` adds each call's result and
  marks errors. `--tool` is a full match on the name (`Edit|Write`).
- `--search` reads whole words and ignores case. `flag*` finds flags and flagged; quote a phrase
  or a term with punctuation (`"run-health"*`); AND, OR and NOT work in capitals. The session
  with most matches prints first. No hits proves only that those word forms are absent: retry
  with OR between single words, other forms, or `--match` with a regex before saying there is no
  record.
- Narrow with `--days N` (default 7) or `--since YYYY-MM-DD`, `--until YYYY-MM-DD`,
  `--project <part of the folder name>`, `--session <id prefix>`, `--kind main,subagent,workflow`.
  `--skip-session <id prefix>` (repeatable) leaves sessions out, such as the one you are in.

Each row shows its time, its uuid and the working directory when it changes. A tool row marked
`cwd_uncertain` ran after a sibling call in the same message changed directory, so its recorded
directory may be wrong.

## Read the context around one message

Name the message by a quote of its text (words in order, spacing ignored) or by its uuid. A turn
is a typed user message or an agent reply.

- `around "<quote>" --before 3 --after 3`: the turns before and after it.
- `since-reply "<quote>"`: the agent's last reply before it, then every tool call it made and
  each result, up to the message. Use it to see what the agent did that the user then corrected.
- `lead-up "<quote>"`: the session from its start to the message.

`around` and `lead-up` show tool calls as one line naming them; `--tools` prints each call with
its input and result. A quote found in several sessions prints the context of each. The window
options pick where to look for the message; the context around it prints whatever its date.

## Transcript folders

The default folder is `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects`. Pass `--root DIR`
once per folder when the user runs more than one Claude Code login. A forked or resumed session
repeats the records it came from; each repeated row prints once, under the session that wrote
it, unless `--session` names the copy.

The index lives in `<config dir>/transcripts/index.db`, readable only by the user, since it holds
whole transcripts. Deleting it loses nothing.
