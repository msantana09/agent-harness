# Testing an instruction edit

An edit is tested by running the same cases twice, once with the file as it is and once with the
edit, using `claude plugin eval`. The edit is worth applying when the edited version scores
higher on cases it was not written from. A script or hook edit is replayed on real input instead:
see Script and hook edits below.

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

## Writing held-out cases

A fresh subagent writes them, never the agent that diagnosed the problem. Give it one sentence
naming the behavior and the session ids to skip. It searches the transcript folders for other
sessions where the same situation came up (`grep` over the `.jsonl` files is enough), reads each
one around the moment, and writes one case pair per instance, naming the session it came from.
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
default threshold.

## Reading the result

- Edited higher on most pairs: recommend applying, and name the sessions each case came from.
- Both versions at or near full marks: the suite cannot separate them, usually because the real
  failure appears late in a long session, which a short case does not reproduce. Say so.
- Edited lower: do not recommend it. The diagnosis or the wording is wrong; go back to step 6.

## Script and hook edits

A script or a hook acts on input, not on a prompt, so its cases are the real input from the
held-out instances. The subagent that finds the instances saves, for each one, the input and what
should have happened, judged from what followed in that session: the hook should have blocked the
call or let it through, the script should have printed a given line. It never sees the edit.

- **Hook input** is built from the transcript record of the tool call, with every field taken from
  that record, its working directory included. Never use one fixed folder: hooks often decide by
  the folder they run in.
- **Run each case** through the current and the edited script or hook, and compare each output
  with what should have happened. The edited version scores higher when it gets more cases right.
