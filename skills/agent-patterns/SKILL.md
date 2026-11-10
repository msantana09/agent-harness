---
name: agent-patterns
description: Use before spawning subagents or launching a Workflow, and when coordinating multi-agent work, sizing an agent's assignment, writing a brief, or scoring confidence. Covers choosing an orchestration tier, agent selection, briefs and file-first output, task decomposition, phase contracts and confidence scoring.
---

# Agent Patterns

Unified patterns for agent selection, confidence-based coordination, and multi-agent orchestration.

## Quick Reference

| Topic | Reference File |
|-------|----------------|
| **Tool names, limits, deprecations, gated features** | `references/harness-api.md` |
| Per-tier `effort:` pins | `references/harness-api.md` § Effort tier defaults (per agent tier, either main loop) |
| Confidence scoring by role | `references/confidence-factors.md` |
| Handoffs, briefs and coordination | `references/coordination-patterns.md` |
| Fresh-context reviews | `references/coordination-patterns.md` § Fresh-Context Reviews |
| Phase-contract template (multi-phase handoffs, GO/NO-GO gates) | `references/orchestration-examples.md` |
| **Tier 3: durable-file fallback** (state survives compaction) | `references/orchestrator-protocol.md` |

> **Tool names change.** This skill states orchestration capabilities *behaviorally*; concrete
> tool names, parameters, limits, and deprecations live in `references/harness-api.md` (the single
> source of truth). If a tool reference here looks stale, that file is authoritative.

## Coordination Defaults

Current-generation models dispatch and sustain parallel subagents dependably, and async
orchestrator↔subagent communication is the native mode:

- **Parallel dispatch is dependable — don't over-prompt fan-out.** When the work is
  independent, state the independent units once; fan-out happens without "spawn N agents in
  parallel" repetition. Over-prescriptive prior-generation steering degrades output here.
- **Async by construction.** Agents always run in the background; read each summarized
  `<result>` as its notification arrives rather than polling. Prefer long-lived subagents
  (continue via `SendMessage`) over re-spawning per subtask — kept context means cache-read
  savings and no bottleneck on the slowest subagent. Only block when a result gates the
  immediate next decision.
- **Longer turns are normal.** Current models sustain long agentic turns; you don't need to
  pad prompts with "read aggressively" / "check all N files before concluding" to keep them
  digging. Let the turn run.
- **Goal + constraints over step enumeration.** Subagent prompts state what "done" looks
  like, the non-negotiable constraints, and why the work matters — not a numbered procedure.
  Step-by-step scaffolding written for prior generations reduces output quality.
- **Ground progress claims in tool results.** On long runs, audit each progress claim against
  a tool result from this session before reporting it; unverified work is reported as
  unverified, never as done.
- **A turn ends on a tool call while work is still owed.** A message with no tool call ends
  the subagent's turn and the work stops there. Current models (Opus 5.5 measurably) follow
  instructions that name the specific early stops, so every subagent prompt — `Agent` and
  Workflow `agent()` alike — ends with the block below, verbatim. Add it from the first
  request; it goes in the system-prompt position and the prompt template, never as a
  mid-run reminder.

  ```text
  How your turns end: a message with no tool call ends your turn, and the work stops there
  until someone asks you to continue. Four endings are unwanted while work you were asked
  for is still owed: a summary that closes by announcing the next step instead of taking
  it; an offer to carry on unless the user prefers otherwise; a list of decisions none of
  which blocks the rest; stopping to report because the turn has been long or a milestone
  landed. Status notes and recommendations on open decisions are welcome — put them in the
  same message as your next tool call and carry on with everything that does not depend on
  an answer. The wanted stops: nothing can move without the user, or the blocker is
  deliberately protected from you (a permission denial, a never-decide-alone call — use
  `## Clarifications Needed`). Confirmation for risky or destructive actions still applies.
  ```

**Don't delegate single-response work.** Delegation threshold: read `rules/core-rules.md`
§ Orchestration & Execution Rules (item EXECUTE DIRECTLY, DELEGATE WHEN IT PAYS) — it binds here.

## Choosing an Orchestration Tier

**Before coordinating agents, pick the tier by the *shape* of the work — not the headcount.**
Count is only a tiebreaker.

| Work shape | Tier | Mechanism |
|------------|------|-----------|
| A few independent one-shot tasks, no cross-checking needed | **1 — Inline fan-out** | `Agent(...)` ×N in one message; read each summarized `<result>` as it lands |
| Fan-out-then-verify · loops · unknown-size discovery · multi-phase · migrations · audits | **2 — Dynamic Workflow** *(standing authorization — launch and announce)* | `Workflow` script — `pipeline`/`parallel`/`agent(schema)`/`budget`/`workflow()`. The script holds the loop and results, so they never touch your context |
| State must survive across sessions/compaction | **3 — Durable-file fallback** | Manual loop + `agents.md` + file-first output (see Tier 3 below). Long-running coordinated peers → agent teams (gated) |

See `references/harness-api.md` for each mechanism's exact API, limits, and gating.

Workflow standing authorization and its launch/announce obligations: read
`rules/automation.md` § Orchestrator Mode — it binds here. The lone-spawn rule and per-stage
effort selection live in `rules/core-rules.md` § Orchestration & Execution Rules (item EXECUTE
DIRECTLY, DELEGATE WHEN IT PAYS).

## Agent Selection

Before spawning, prefer a custom agent defined for the domain (`~/.claude/agents/`,
`.claude/agents/`) over a generic spawn: it carries its domain's skills and model pin, which a
generic spawn lacks. Claude Code's own types:

| Need | Agent |
|------|-------|
| Codebase exploration | `Explore` |
| Implementation planning | `Plan` |
| Continue *this* conversation's context in a subagent | `fork` (inherits full context; always the caller's model) |
| Claude Code / Agent SDK / Claude API questions | `claude-code-guide` |

**Use `general-purpose` (or `claude`) only when**: No specialist matches the domain, task is
truly cross-cutting, or explicitly doing general research.

**Anti-pattern**: Using `general-purpose` with custom instructions that duplicate a specialist's
expertise. If you're writing detailed instructions, there's probably a specialist for it.

**Generic types need a `model=`.** `Explore`, `Plan`, `general-purpose`, and `claude` carry no
`model:` pin; `rules/core-rules.md` § Orchestration & Execution Rules requires a tier on every
generic spawn. Custom agents run their pin; `fork` ignores `model`.

## Confidence Scoring

**Core Formula.**

```python
confidence = sum(factors[k] * weights[k] for k in factors)  # 0.0-1.0
```

**Thresholds.**

| Confidence | Action |
|------------|--------|
| < 0.5 | STOP - Escalate to specialist or human immediately |
| 0.5-0.6 | Request specialist review before proceeding |
| 0.6-0.8 | Proceed with caution, document gaps |
| > 0.8 | Proceed with confidence |
| > 0.8 + critical path | Quick validation for auth/tenant/payments |

Factor weights by role, Backend and Frontend summaries included:
`references/confidence-factors.md` § Domain Factors (Summary).

> **Confidence is earned structurally, not asserted.** When a finding has been checked by an
> independent verification stage (a fresh-context reviewer, or a Workflow refute/judge pass),
> trust that over a high self-scored number from the agent that produced the finding.

## Coordination Patterns

**Standard Output Format.** An agent that returns its result inline ends with a
`## Recommended Next Steps` block — the one template lives in
`references/coordination-patterns.md` § Standard Output Format. A Tier-3 agent returns the
compact summary of `references/orchestrator-protocol.md` § Agent Output Protocol (File-First)
instead, and a Workflow stage returns its schema.

**Parallel vs Sequential.** Parallel for independent reviews and analyses; sequential while one
agent's output is the next one's input:
`references/coordination-patterns.md` § Parallel vs Sequential Execution.

**Agent Resumption.** Continue a prior agent with **`SendMessage`** (by agent id or name) — its
context is preserved, `Explore` included. A new `Agent` call always starts fresh.

Continue-vs-spawn-fresh table and handoff templates:
`references/coordination-patterns.md` § Agent Resumption Patterns.

## File-First Briefs (Input Side)

An assignment longer than ~25 lines goes in a brief file the agent is pointed at, not inline:
`references/coordination-patterns.md` § File-First Briefs (Input Side).

## Phase Contracts

Multi-phase work hands each phase an explicit Input / Expected Output / Deliverables /
Validation contract with a GO/NO-GO decision point, and consults specialists in parallel
*before* planning. Template: `references/orchestration-examples.md`. Under Tier 2 each phase is
a `pipeline`/`parallel` stage.

## Tier 3: Durable-File Fallback (large work without Workflow)

This is the **fallback** for large, multi-phase work — use it when **orchestration state must
survive across sessions and compaction** (a Workflow run does not outlive the session that
launched it). Otherwise prefer Tier 2: the script holds the loop and results, so you don't need
the manual registry and polling.

The mechanism: keep all state in files (a `agents.md` registry + a per-agent `outputs/` directory),
spawn agents with a file-first output contract, and drive a manual loop. This trades context for
durability — the work can be recovered after compaction by re-reading the files. Folder layout,
registry, loop and recovery: `references/orchestrator-protocol.md`.

**When this tier applies.**

| Situation | Tier |
|-----------|------|
| 10+ tasks / 5+ agents / 3+ phases, single session | Prefer **Tier 2** (launch and announce) |
| State must survive across sessions / compaction | **Tier 3** (durable files) |
| Simple fix, few files | Neither — just edit / Tier 1 |

**File-First Output Protocol.** In this tier every agent prompt carries the Output Requirements
template, and the deliverable file is what survives compaction:
`references/orchestrator-protocol.md` § Agent Output Protocol (File-First).

## Task Decomposition

| Scope | Agents | Notes |
|-------|--------|-------|
| Single file (~100-300 lines) | 1 agent | Ideal unit |
| Multi-file (2-5 files) | 1 agent | Clear scope required |
| Large feature (5+ files) | Multiple sequential | Break by component |
| Cross-cutting (10+ files) | Multiple | Break by domain |

**Rules**: One concern per agent. Max ~500 lines changed. Explicit file scope in prompt.
Each assignment must finish inside one context window — an agent that compacts repeatedly
was handed too much; cut the work, don't lengthen the prompt. Dispatch the cut pieces in
parallel, or as sequential Workflow stages when they depend on each other.
Uncuttable-before-dispatch work (discovery, emergent-scope migrations) ships with an explicit
instruction to fan out to its own subagents — custom agents whose `tools:` include `Agent`,
`general-purpose` and `claude` hold `Agent`; `Explore`, `Plan` and Workflow `agent()` stages do
not, so cut those before dispatch.
Sub-spawns follow the same model-tier rules (core-rules RESPECT AGENT MODEL CONFIG).

**Extraction / port prompts** (package extraction, bulk copy): give a negative spec — enumerate
what to SKIP (already extracted) and copy EVERYTHING ELSE; positive lists miss items, a negative
spec does not. State the expected size ("the module is ~700 lines, output should match").
Commit create-only per phase — delete old files only after consumers are updated. A sanity check
after each phase is non-negotiable. Same failure class as a partial cross-project sync: copy
100% of the source, never a summary.

**Heavy test-iteration/review-fix work gets a tighter cap: 2-3 tasks per agent bundle**, not
the ~500-line/one-context-window ceiling above. Gate-iteration loops (test → fix → re-test,
or review round → fix → re-review) burn context on retries the line-count rules don't see
coming, so agents doing this die of context overflow at report time with the work already on
disk. Two mitigations, both required for this work: cap the bundle at 2-3 tasks, and follow
the skeleton-first, append-per-task instruction in
`references/orchestrator-protocol.md` § Agent Output Protocol (File-First) instead of writing
the report at the end.

## Locate Cheap, Read Ranges

Implementer subagents (Sonnet/Opus) spend most of their context, and most of all Read tokens,
locating code before editing it. Split the two phases by model tier:

- **Locate on Haiku**: the orchestrator (or the implementer itself) spawns
  `Agent(subagent_type="Explore", model="haiku")` with the symbols/behaviors to find. It
  returns `file:line` pointers plus one-line summaries, never file contents.
- **Implement on Sonnet with ranges**: the implementer prompt carries those pointers and reads
  with `offset`/`limit` around them.
- **Triage on Haiku**: test/build runs go through `~/.claude/scripts/triage_run.sh` so the
  implementer sees a failure digest, not the raw dump (a run whose output it needs verbatim
  takes a plain redirect).

Skip the split when the file list is already known and short (1-3 files under ~300 lines).

`triage_run.sh` calls `claude` with `--setting-sources ""`, never `--bare` (skips the keychain, so auth fails).
