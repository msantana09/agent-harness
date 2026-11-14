# Testing an instruction edit

An edit is tested by running the same cases twice, once with the file as it is and once with the
edit, using `claude plugin eval`. The edit is worth applying when the edited version scores
higher in total on cases it was not written from, under the eval's grader and a second one. A
script or hook edit is replayed on real input instead: see Script and hook edits below.

## What the eval sandbox does

- Each run gets its own temporary home, working folder and config, so the user's rules,
  `CLAUDE.md`, hooks, memories and other skills never load. A case brings the instruction under
  test itself.
- `append_system_prompt` in a case's frontmatter adds text to the system prompt. That is how a
  rule, memory or `CLAUDE.md` edit is tested: carry the whole file, not the changed paragraph,
  since a rule read alone can look stronger than it is among the rest.
- A command or skill body arrives as the user's message, so it goes in the prompt instead.
- Reads outside the run's folder are denied unless granted with `--allow-tools`, and a grant
  needs an absolute path written with `//` (`Read(//home/me/notes/**)`). A `~/` grant points at
  the temporary home.

## Layout

One folder per suite, two cases per held-out instance: `<case>-current` carries the file as it
is now, `<case>-edited` carries the edited file. Everything else in the pair is identical.

```text
~/.claude/self-optimize/<date>-evals/
  <case>-current/
    prompt.md
    graders/criteria.md
  <case>-edited/
    prompt.md
    graders/criteria.md
```

`prompt.md` is frontmatter, then the situation the agent is in and the user's message:

```markdown
---
max_turns: 15
allowed_tools: [Read, Glob, Grep]
append_system_prompt: |
  <the whole instruction file, current or edited>
---

## Context

<what the agent holds at this point: the task, the files, an excerpt of the session>

## User

<the user's message from the real instance, or one like it>
```

`graders/criteria.md` says what passes and what fails, concretely enough that two readers would
grade the same reply the same way:

```markdown
---
type: llm
weight: 1
---
<what happened in the real session, in two sentences>

PASS if the reply <the behavior the edit asks for>.

FAIL if the reply <the behavior the user corrected>, for example <a phrasing that fails>.
```

## Replaying a session

A behavior that shows up late in a long session needs the conversation that led to it, which a
short case cannot set up. Replay the real session instead: the case resumes it just before the
user's message and grades the agent's next reply. Cut it with the script in this skill's
`scripts/` folder:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/cut_session.py --session <id> \
  --before "<start of the user's message>" --out <suite>/<case>-current/history.jsonl
```

It prints the message whole, which is the case's prompt, and drops the session's standing
context: the instruction files it loaded, the memories it recalled, its system prompt, output
style and tool listings. A rule, `CLAUDE.md` or memory file under test then reaches the agent
only through the case. Copy the history into `<case>-edited/` as well. A replay case is a
`case.yaml` in place of `prompt.md`, with the same `graders/`:

```yaml
schema_version: "1.1"
name: <case>-current
context:
  history_file: history.jsonl
execution:
  max_turns: 15
  allowed_tools: [Read, Glob, Grep]
  append_system_prompt: |
    <the whole instruction file, current or edited>
  prompt: |
    <the message cut_session.py printed>
```

- Tool results, skill and command bodies loaded during the session, and text hooks added stay in
  the history as they were. An edit to a skill, a command, a hook's message or a memory a hook
  injects shows there in its old form in both versions when it loaded before the cut: test that
  edit with a short case.
- Each run sends the whole history again, so a replay case costs more than a short one; the
  eval reports what each run cost.

## Writing held-out cases

A fresh subagent writes them, never the agent that diagnosed the problem. Give it one sentence
naming the behavior and the session ids to skip. It finds other sessions where the same
situation came up with the transcripts skill (`Skill("transcripts")`): `messages --typed` with
`--search` or `--match`, and one `--skip-session` per session to skip. It reads each one around
the moment with `around` and `since-reply`, and writes one case pair per instance, naming the
session it came from. An instance late in a long session becomes a replay case (Replaying a
session above).
Three to five instances are enough; fewer than two cannot separate the versions.

## Running

From `~/.claude/self-optimize/`:

```bash
claude plugin eval . --eval-dir <date>-evals --runs 5 -j 4 \
  --judge-model opus --no-publish --trust-plugin
```

- `--judge-model opus`: the default small grader fails replies that meet their criteria when the
  phrasing differs from what it expects, such as a label followed by its explanation.
- `--no-publish` keeps the report local, since the cases quote the user.
- Add `--allow-tools` grants when a case reads files outside its folder.

Compare scores between the `-current` and `-edited` cases, not the exit code: one miss fails the
default threshold. A second attempt at the edit goes in its own suite folder, with cases named
`<case>-edited2`; its `-current` cases stay in the first folder.

## Checking the grader

One judge model grades every run, and a judge misreads some replies. Most of those misreadings
fall on runs where all its votes agree, so its split votes do not reveal them. After the eval,
re-grade the round with a second model:

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/grader_check.py <date>-evals [<date>-evals-r2 ...]
```

Pass every suite folder of the round. The script sends each graded run's reply and its criteria
to Sonnet, blind to the case, the version and the first verdict, one call per run. It prints the
runs each version passed out of the runs it had under both graders, and compares pass rates, so an
attempt run more times does not score higher for that. It also prints each case's win, tie or
loss against `-current`, and the runs the graders disagree on. When the two graders reach different outcomes for an edit, its verdict
is "too close to call".

## Reading the result

- Edited higher in total under both graders: recommend applying, and name the sessions each case
  came from. Show each case's win, tie or loss beside the totals.
- Too close to call: the graders disagree on the outcome. Do not recommend it; show both totals
  and the runs they disagree on, and let the user decide.
- Both versions at or near full marks: the suite cannot separate them, usually because the real
  failure appears late in a long session, which a short case does not reproduce. Rewrite those
  cases as replays and run them again; if they still tie, say so.
- Edited lower: do not recommend it. The diagnosis or the wording is wrong; go back to step 6.

## Script and hook edits

A script or a hook acts on input, not on a prompt, so its cases are the real input from the
held-out instances. The subagent that finds the instances saves, for each one, the input and what
should have happened, judged from what followed in that session: the hook should have blocked the
call or let it through, the script should have printed a given line. It never sees the edit.

- **Hook input** is built from the tool call's row in the transcripts skill (`tools --results
  --json`), with every field taken from that row, its `cwd` included. Never use one fixed folder:
  hooks often decide by the folder they run in. A row marked `cwd_uncertain` ran after a sibling
  call changed directory, so its folder is unknown: leave it out of the cases.
- **Run each case** through the current and the edited script or hook, and compare each output
  with what should have happened. The edited version scores higher when it gets more cases right.
