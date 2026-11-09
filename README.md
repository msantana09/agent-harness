# agent-harness

The skills, hooks, rules and scripts I use every day to build software with coding agents, cleaned
up to share. They cover how a change is made, reviewed and checked, and how the agent's
instructions improve from what happens in real sessions.

## What's here

| Skill | What it does |
|---|---|
| [ship](skills/ship/SKILL.md) | Takes a committed branch through its pull request: opens the draft, has Codex review it within a round budget, fixes the findings it accepts, and hands the merge to you. |
| [inspect-pr](skills/inspect-pr/SKILL.md) | A read-only, evidence-backed review of a pull request, findings first. It is the framework the Codex reviewer follows for ship. |
| [codex](skills/codex/SKILL.md) | Hands work to OpenAI Codex through codex-jobs: a second opinion on a plan or bug, a pull request review, or a bounded code change in its own worktree. |
| [external-feedback](skills/external-feedback/SKILL.md) | Checks each suggestion from a reviewer or another AI against the code before accepting, rejecting or filing it. |
| [commit](skills/commit/SKILL.md) | Commits exactly the files the agent changed, through the repo's pre-commit hooks, with a message that describes the diff. |
| [command-output-patterns](skills/command-output-patterns/SKILL.md) | Saves a long command's output to a file once, then filters the file instead of running the command again. |
| [default-stack](skills/default-stack/SKILL.md) | The languages, frameworks, database and test tools a new project starts from, so the agent does not pick a different stack for every build. |
| [self-optimize](docs/self-optimize.md) | Finds where you corrected the agent in your own Claude Code sessions, then proposes and tests the instruction change that would have prevented it. |
| [transcripts](skills/transcripts/SKILL.md) | Lets the agent read past Claude Code sessions as plain text: search what was typed, list tool calls and results, and show the context around one message. |

`rules/` holds the instructions every session follows: when to act and when to ask, how to verify
and review, and how to report back.

`scripts/` holds the shared scripts skills and rules call: `scripts/codex/codex-jobs` runs Codex
review, consult and run jobs as a CLI or an MCP server, `scripts/triage_run.sh` runs a long
command and prints a short digest of what failed, and `scripts/git_state.sh` prints a repo's
branch, drift and dirty count on one line.

`hooks/` holds Claude Code hooks that check the agent as it works. Before a write, `emoji_guard`
refuses emoji in docs and asks before writing them into code, and `edit_trailing_space_guard`
refuses an edit whose replacement ends in a space or tab, which the Edit tool drops outside
Markdown files. When the
agent ends its turn, `prose_question_blocker` sends back a reply that asks the user a question in
prose instead of through AskUserQuestion, offers a step instead of taking it, or announces work it
never started, and `peer_reply_guard` sends back a session that answered another session's task
in its own chat instead of with SendMessage. When the user types a bare "stop", `stop_flag` marks
the session and `stop_gate` refuses its tool calls until the user's next prompt.

More will be added as I clean them up.

## Install

As a Claude Code plugin, which installs every skill here. In Claude Code, run:

```text
/plugin marketplace add msantana09/agent-harness
/plugin install agent-harness@agent-harness
```

Skills installed this way take the plugin's name as a prefix, as in `/agent-harness:self-optimize`.
To update later, run `claude plugin marketplace update agent-harness`, then
`claude plugin update agent-harness@agent-harness`.

Or copy single skills. Each skill is a folder; copy the ones you want into your Claude Code skills
folder. self-optimize reads sessions through transcripts, so it needs both:

```bash
git clone https://github.com/msantana09/agent-harness
cp -r agent-harness/skills/self-optimize agent-harness/skills/transcripts ~/.claude/skills/
```

A plugin cannot install rules. Link them from the clone, with the skills and scripts they name by
path; Claude Code loads every file in `~/.claude/rules/` at the start of each session:

```bash
mkdir -p ~/.claude/rules ~/.claude/skills ~/.claude/scripts
ln -s "$PWD"/agent-harness/rules/*.md ~/.claude/rules/
ln -s "$PWD"/agent-harness/skills/* ~/.claude/skills/
ln -s "$PWD"/agent-harness/scripts/* ~/.claude/scripts/
```

The review skills (ship, inspect-pr, codex, external-feedback, commit, command-output-patterns)
load each other and run `scripts/` by path, so install them this way. When Codex reviews for ship
it loads inspect-pr from its own skills folder; link it there too:

```bash
mkdir -p ~/.agents/skills
ln -s "$PWD"/agent-harness/skills/inspect-pr ~/.agents/skills/
```

The plugin installs no hooks. Copy them into `~/.claude/hooks/`, then merge the `hooks` key of the
repo's `settings.json` into your `~/.claude/settings.json`; each command there runs a hook's
`main.py` from that folder with `python3`:

```bash
mkdir -p ~/.claude/hooks
cp -R agent-harness/hooks/. ~/.claude/hooks/
```

## Requirements

- [Claude Code](https://code.claude.com).
- Python 3.14 or newer, run as `python3`; tested on 3.14 and 3.15. The scripts use only the
  standard library, so there is nothing to install. The `python3` that ships with macOS is 3.9,
  too old for them.
- For the review skills: the [Codex CLI](https://developers.openai.com/codex/cli), signed in,
  and the GitHub CLI `gh`, signed in.

## License

[MIT](LICENSE).
