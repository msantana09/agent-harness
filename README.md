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

Each skill is a folder. Copy the ones you want into your Claude Code skills folder:

```bash
git clone https://github.com/msantana09/agent-harness
cp -r agent-harness/skills/self-optimize ~/.claude/skills/
```

## Requirements

- [Claude Code](https://code.claude.com).
- Python 3.14. Everything here is tested only on 3.14, and the commands call it as `python3.14`.
  The scripts use only the standard library, so there is nothing to install. Other versions may
  run some of them, but they are not tested.

## License

[MIT](LICENSE).
