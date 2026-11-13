#!/usr/bin/env python3
"""Summarize recent Claude Code session transcripts: activity, interruptions, hook blocks,
tool errors, skill loads and spend.

Reads the JSONL transcripts Claude Code keeps under <config dir>/projects and prints a markdown
report. Standard library only; makes no model calls.

    python3 scan.py [--days N | --since YYYY-MM-DD] [--until YYYY-MM-DD] [--root DIR ...]
                       [--json PATH]

Inputs: the transcripts under each `--root` (default $CLAUDE_CONFIG_DIR/projects, else
~/.claude/projects), and the skills installed under <config dir>/skills and each active
project's .claude/skills.
Outputs: the report on stdout; with `--json`, the same numbers as JSON. On stderr, a count of
unreadable lines and records, and a warning when sessions had activity but no typed message.

Exit codes: 0 success; 2 a `--root` that is not a folder, no transcripts under any root, or
invalid arguments.

How the counts are made: which records, sessions and typed messages count, and how forks and
blocking hooks are told apart, is in transcripts.py. Beyond that:

* An interrupted turn is one the user stopped mid-turn, or a tool call they rejected without a
  comment. Sessions are ranked by the two together.
* Each hook block names the session that hit it most; a block in a subagent counts under the
  session the subagent ran in. It also counts the blocks quoting each part its messages quote
  (the pattern, file or command one block was about), so a hook that checks several patterns
  can be counted per pattern. The report leaves out a part every block quotes, which is the
  rule's own text.
* Each tool error names how many sessions it touched, the session with the most of it and how
  many of it came from subagents; a subagent's errors count under the session it ran in. Errors
  are ranked by sessions touched, then by count, so one session stuck in a loop does not outrank
  an error that recurs across sessions. The report lists the first TOOL_ERRORS_SHOWN and counts
  the rest; --json has every one.
* A skill load is counted by how it loaded (a Skill tool call, typed as a slash command, or
  preloaded by an agent definition). A typed command counts only when it names an installed
  skill, since built-in commands look the same.
* A model call is priced once per request, from the last transcript line that logs it (a request
  is logged once per content block, and later lines carry the final usage). An advisor call a
  request made counts as its own call under the advisor's model, as its tokens are logged apart
  from the request's. Cost is list price, made as pricing.py describes; a model with no price
  counts as $0 and is named in the report.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pricing

# The transcript reader is its own skill, installed beside this one.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "transcripts" / "scripts"))
import transcripts  # noqa: E402

HOOK_VALUES_SHOWN = 3  # quoted parts listed under a hook block in the report; --json has all
HOOK_VALUE_CHARS = 60  # each shown at most this long, its middle cut; --json has it whole
TOOL_ERRORS_SHOWN = 20  # tool errors listed in the report; --json has all

type Summary = dict[str, Any]


@dataclass
class ModelSpend:
    calls: int = 0
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    cost_usd: float = 0.0

    @property
    def input_side(self) -> int:
        return self.input + self.cache_write + self.cache_read

    @property
    def cache_read_share(self) -> float:
        return self.cache_read / self.input_side if self.input_side else 0.0


def spend_summary(read: list[transcripts.Transcript]) -> Summary:
    """Tokens and cost by model over the in-window requests, each request counted once.

    A request's advisor calls count as calls of the advisor's model.
    """
    rows: dict[str, ModelSpend] = defaultdict(ModelSpend)
    unpriced: Counter[str] = Counter()
    seen: set[str] = set()
    for transcript in read:
        for request_id, request in transcript.requests.items():
            if request_id in seen:
                continue
            seen.add(request_id)
            if not request.in_window:
                continue
            for model, usage in request.calls():
                tokens = pricing.token_counts(usage)
                cost = pricing.call_cost(model, tokens)
                if cost is None:
                    unpriced[model] += 1
                row = rows[model]
                row.calls += 1
                row.cost_usd += cost or 0.0
                row.input += tokens["input"]
                row.cache_write += tokens["cache_write"]
                row.cache_read += tokens["cache_read"]
                row.output += tokens["output"]
    ranked = sorted(rows.items(), key=lambda item: -item[1].cost_usd)
    total = ModelSpend(
        calls=sum(r.calls for r in rows.values()),
        input=sum(r.input for r in rows.values()),
        cache_write=sum(r.cache_write for r in rows.values()),
        cache_read=sum(r.cache_read for r in rows.values()),
        cost_usd=sum(r.cost_usd for r in rows.values()),
    )
    return {
        "models": [
            {"model": model, **asdict(row), "cache_read_share": row.cache_read_share}
            for model, row in ranked
        ],
        "calls": total.calls,
        "cost_usd": total.cost_usd,
        "cache_read_share": total.cache_read_share,
        "unpriced": dict(unpriced),
    }


def installed_skills(project_dirs: set[str]) -> set[str]:
    """Names of the skills installed for the user and under each project's .claude/skills."""
    folders = [
        transcripts.config_dir() / "skills",
        *(Path(p) / ".claude" / "skills" for p in project_dirs if p),
    ]
    return {manifest.parent.name for folder in folders for manifest in folder.glob("*/SKILL.md")}


def activity_summary(mains: list[transcripts.Transcript]) -> Summary:
    active = [t for t in mains if t.first_epoch is not None and t.last_epoch is not None]
    starts = [t.first_epoch for t in active if t.first_epoch is not None]
    ends = [t.last_epoch for t in active if t.last_epoch is not None]
    return {
        "sessions": len(active),
        "sessions_with_typed_message": sum(1 for t in active if t.typed),
        "typed_messages": sum(len(t.typed) for t in active),
        "first_day": transcripts.local_day(min(starts)) if starts else None,
        "last_day": transcripts.local_day(max(ends)) if ends else None,
    }


def stops_summary(mains: list[transcripts.Transcript]) -> Summary:
    per_session = sorted(
        ((sum(t.stops.values()), t) for t in mains if t.stops),
        key=lambda item: (-item[0], item[1].session),
    )
    return {
        "interrupted": sum(t.stops["interrupt"] for t in mains),
        "rejected_without_comment": sum(t.stops["rejection"] for t in mains),
        "top_sessions": [
            {"session": t.session, "project": t.project, "count": n} for n, t in per_session[:3]
        ],
    }


def hooks_summary(read: list[transcripts.Transcript]) -> list[Summary]:
    """Each hook block's count, the session with the most of it, and how many of its blocks
    quote each part.

    A subagent's blocks count under the session it ran in; a session's project is the one its
    first transcript in `read` names, so list main transcripts first.
    """
    counts: Counter[str] = Counter()
    per_session: dict[str, Counter[str]] = defaultdict(Counter)
    quoted: dict[str, Counter[str]] = defaultdict(Counter)
    projects: dict[str, str] = {}
    for t in read:
        projects.setdefault(t.session, t.project)
        for (hook, parts), n in t.hook_blocks.items():
            counts[hook] += n
            per_session[hook][t.session] += n
            for part in parts:
                quoted[hook][part] += n
    rows = []
    for hook, n in counts.most_common():
        worst = worst_session(per_session[hook], projects)
        values = [{"value": v, "count": c} for v, c in quoted[hook].most_common()]
        rows.append({"hook": hook, "count": n, "worst_session": worst, "values": values})
    return rows


def worst_session(per_session: Counter[str], projects: dict[str, str]) -> Summary:
    """The session with the most of one thing, the first by id on a tie."""
    session, most = min(per_session.items(), key=lambda item: (-item[1], item[0]))
    return {"session": session, "project": projects[session], "count": most}


def errors_summary(
    mains: list[transcripts.Transcript], subs: list[transcripts.Transcript]
) -> list[Summary]:
    """Each tool error's count, the sessions it touched, the session with the most of it and
    how many of it came from subagents, ranked by sessions touched, then count."""
    per_session: dict[str, Counter[str]] = defaultdict(Counter)
    in_subagents: Counter[str] = Counter()
    projects: dict[str, str] = {}
    for t in mains + subs:
        projects.setdefault(t.session, t.project)
        for error, n in t.tool_errors.items():
            per_session[error][t.session] += n
    for t in subs:
        in_subagents.update(t.tool_errors)
    rows = [
        {
            "error": error,
            "count": sum(sessions.values()),
            "sessions": len(sessions),
            "in_subagents": in_subagents[error],
            "worst_session": worst_session(sessions, projects),
        }
        for error, sessions in per_session.items()
    ]
    return sorted(rows, key=lambda r: (-r["sessions"], -r["count"], r["error"]))


def skills_summary(
    mains: list[transcripts.Transcript], subs: list[transcripts.Transcript]
) -> Summary:
    installed = installed_skills({t.cwd for t in mains if t.active})
    loads: dict[str, Counter[str]] = defaultdict(Counter)
    for t in mains + subs:
        for name, how in t.skill_loads:
            if how != "typed" or name in installed:
                loads[name][how] += 1
    ranked = sorted(loads.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    return {
        "loads": [
            {"skill": name, "count": sum(how.values()), "how": dict(how)} for name, how in ranked
        ],
        "installed": len(installed),
        "never_loaded": sorted(installed - loads.keys()),
    }


def summarize(
    mains: list[transcripts.Transcript],
    subs: list[transcripts.Transcript],
    window: transcripts.Window,
    roots: list[Path],
) -> Summary:
    def stamp(epoch: float | None) -> str | None:
        if epoch is None:
            return None
        return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")

    return {
        "window": {"label": window.label, "start": stamp(window.start), "end": stamp(window.end)},
        "roots": [str(r) for r in roots],
        "activity": activity_summary(mains),
        "interrupts": stops_summary(mains),
        "hook_blocks": hooks_summary(mains + subs),
        "tool_errors": errors_summary(mains, subs),
        "skills": skills_summary(mains, subs),
        "spend": spend_summary(mains + subs),
    }


def plural(n: int, noun: str) -> str:
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


def compact(n: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{n:.0f}"


def render_activity(a: Summary, window_label: str) -> list[str]:
    lines = [
        "## Activity",
        f"- Window: {window_label}",
        f"- {plural(a['sessions'], 'session')} with activity, "
        f"{a['sessions_with_typed_message']} with a typed message",
        f"- {plural(a['typed_messages'], 'message')} typed",
    ]
    if a["sessions"]:
        lines.append(f"- Activity dates: {a['first_day']} to {a['last_day']}")
    return lines


def render_interrupts(s: Summary) -> list[str]:
    if not (s["interrupted"] or s["rejected_without_comment"]):
        return []
    lines = [
        "## Interrupted turns",
        f"- {s['interrupted']} interrupted mid-turn, "
        f"{plural(s['rejected_without_comment'], 'tool call')} rejected without a comment",
    ]
    if s["top_sessions"]:
        top = ", ".join(f"{t['session']} {t['project']} ({t['count']})" for t in s["top_sessions"])
        lines.append(f"- Most in: {top}")
    return lines


def shorten(value: str) -> str:
    """`value`, or its start and end around `…` when longer than HOOK_VALUE_CHARS: two paths
    often differ only in their last part."""
    if len(value) <= HOOK_VALUE_CHARS:
        return value
    half = (HOOK_VALUE_CHARS - 1) // 2
    return value[:half] + transcripts.MASK + value[-half:]


def render_hooks(hooks: list[Summary]) -> list[str]:
    if not hooks:
        return []
    lines = ["## Hook blocks"]
    for h in hooks:
        worst = h["worst_session"]
        lines.append(
            f"- {h['count']}x  {h['hook']}  "
            f"(most in {worst['session']} {worst['project']}, {worst['count']})"
        )
        varying = [v for v in h["values"] if v["count"] < h["count"]]
        if varying:
            shown = (f"`{shorten(v['value'])}` {v['count']}" for v in varying[:HOOK_VALUES_SHOWN])
            lines.append("  - quoted: " + ", ".join(shown))
    return lines


def render_tool_errors(errors: list[Summary]) -> list[str]:
    if not errors:
        return []
    lines = ["## Tool errors"]
    for e in errors[:TOOL_ERRORS_SHOWN]:
        worst = e["worst_session"]
        lines.append(
            f"- {e['count']}x in {plural(e['sessions'], 'session')}  {e['error']}  "
            f"(most in {worst['session']} {worst['project']}, {worst['count']}; "
            f"{e['in_subagents']} in subagents)"
        )
    rest = errors[TOOL_ERRORS_SHOWN:]
    if rest:
        lines.append(
            f"- {plural(len(rest), 'other error')} not listed, "
            f"{sum(e['count'] for e in rest):,} in all; --json has them"
        )
    return lines


def render_skills(skills: Summary) -> list[str]:
    if not skills["loads"] and not skills["installed"]:
        return []
    lines = ["## Skills"]
    for load in skills["loads"]:
        how = ", ".join(f"{k} {n}" for k, n in sorted(load["how"].items(), key=lambda kv: -kv[1]))
        lines.append(f"- {load['count']}x  {load['skill']}  ({how})")
    if skills["installed"]:
        never = ", ".join(skills["never_loaded"]) or "none"
        lines.append(
            f"- Installed here and never loaded "
            f"({len(skills['never_loaded'])} of {skills['installed']}): {never}"
        )
    return lines


def render_spend(spend: Summary) -> list[str]:
    if not spend["models"]:
        return []
    lines = [
        "## Spend",
        "| Model | Calls | Input | Cache write | Cache read | Output | Cost | Cache-read share |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for m in spend["models"]:
        lines.append(
            f"| {m['model']} | {m['calls']:.0f} | {compact(m['input'])} "
            f"| {compact(m['cache_write'])} | {compact(m['cache_read'])} | {compact(m['output'])} "
            f"| ${m['cost_usd']:,.2f} | {m['cache_read_share']:.0%} |"
        )
    lines.append(
        f"\nTotal ${spend['cost_usd']:,.2f} at list price over {plural(spend['calls'], 'call')}; "
        f"cache reads were {spend['cache_read_share']:.0%} of input-side tokens."
    )
    if spend["unpriced"]:
        unpriced = ", ".join(
            f"{model} ({plural(n, 'call')})" for model, n in spend["unpriced"].items()
        )
        lines.append(f"Not priced (counted as $0, add them to PRICES): {unpriced}")
    return lines


def render_markdown(summary: Summary) -> str:
    sections = [
        render_activity(summary["activity"], summary["window"]["label"]),
        render_interrupts(summary["interrupts"]),
        render_hooks(summary["hook_blocks"]),
        render_tool_errors(summary["tool_errors"]),
        render_skills(summary["skills"]),
        render_spend(summary["spend"]),
    ]
    return "\n\n".join("\n".join(lines) for lines in sections if lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize recent Claude Code session transcripts."
    )
    transcripts.add_window_arguments(parser)
    parser.add_argument(
        "--json", metavar="PATH", help="also write the numbers to this file as JSON"
    )
    args = transcripts.parse_window_arguments(parser, argv)

    loaded = transcripts.load(args, "scan.py", subagents=True)
    if loaded is None:
        return transcripts.NO_TRANSCRIPTS
    summary = summarize(loaded.mains, loaded.subs, loaded.window, loaded.roots)
    print(render_markdown(summary), end="")
    activity = summary["activity"]
    if activity["sessions"] and not activity["typed_messages"]:
        print(
            f"scan.py: warning: {plural(activity['sessions'], 'session')} had activity but no "
            "typed messages; the transcript format may have changed",
            file=sys.stderr,
        )
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
