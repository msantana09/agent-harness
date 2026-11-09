# agent-harness

The skills, hooks, rules and scripts I use every day to build software with coding agents, cleaned
up to share. They cover how a change is made, reviewed and checked, and how the agent's
instructions improve from what happens in real sessions.

## What's here

| Skill | What it does |
|---|---|
| [self-optimize](docs/self-optimize.md) | Finds where you corrected the agent in your own Claude Code sessions, then proposes and tests the instruction change that would have prevented it. |

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
folder:

```bash
git clone https://github.com/msantana09/agent-harness
cp -r agent-harness/skills/self-optimize ~/.claude/skills/
```

## Requirements

- [Claude Code](https://code.claude.com).
- Python 3.14 or newer, run as `python3`; tested on 3.14 and 3.15. The scripts use only the
  standard library, so there is nothing to install. The `python3` that ships with macOS is 3.9,
  too old for them.

## License

[MIT](LICENSE).
