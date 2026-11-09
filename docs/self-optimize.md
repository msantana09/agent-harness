# harness-skills

Skills and other resources I use with Claude Code. Each one is something I run myself.

## self-optimize

Reads your own session transcripts, finds where you corrected the agent, and proposes one fix to
your instructions that would have prevented the most common correction. On the next run it counts
again and tells you whether the fix worked.

```
/self-optimize
```

What a run does:

1. **Scan** (free): sessions, interrupted turns, hook blocks, skills that never load, spend.
2. **Find corrections**: a small model reads your typed messages and flags the ones where you
   corrected the agent. It says roughly what that costs as it starts; a week of heavy use is
   about $0.15 at list price.
3. **Propose one fix**, with your own quotes as the evidence. Nothing changes until you say so.

Reports and the ledger stay on your machine in `~/.claude/self-optimize/`.

### Install

```bash
git clone https://github.com/msantana09/harness-skills
cp -r harness-skills/skills/self-optimize ~/.claude/skills/
```

Needs Claude Code and `python3.14` on PATH. No other dependencies.

### Limits

- It reads Claude Code transcripts only.
- The correction pass sends your typed messages to a Claude model.
- "Worked" means the correction showed up less often afterward. It does not prove the edit was
  the reason.
