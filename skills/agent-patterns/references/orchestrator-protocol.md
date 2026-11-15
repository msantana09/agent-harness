# Orchestrator Protocol (Tier 3: durable-file fallback)

> **Tier-3 fallback**, for work whose state must survive sessions and compaction; otherwise prefer
> Tier 2 (`../SKILL.md` § Choosing an Orchestration Tier; "Dynamic Workflows" below). Tool facts:
> `claude-code-api.md`.

Pure orchestration pattern for managing 10s-100s of agents over extended sessions without
context accumulation, with all state in files so the work survives compaction.

## Core Philosophy

**Files are the source of truth.** Claude doesn't hold state - reads from files, writes to files.

| Component | Purpose |
|-----------|---------|
| The work's folder | Persistent memory (plan, context notes, task list, progress log, `agents.md`) |
| Agent outputs | Written to files, never held in orchestrator context |
| Claude's context | Minimal: current phase + active agents + pending decisions |

**Reasoning lives in agents.** For autonomous/agent-driven systems:

- Recovery and routing decisions default to LLM-agent reasoning.
- Bash/scripts stay thin infrastructure (phase dispatch, container setup); reasoning belongs in
  opus agents.
- Retry budgets and exit-code pattern matching are anti-patterns when the underlying failure is
  diagnosable.

## Dynamic Workflows (Tier 2 — the default for Tier-2-shaped work)

Prefer `Workflow` over the manual machinery in the rest of this file whenever state need not
outlive the session. A Workflow script *is* the orchestrator loop — it holds
phases, the worklist, and intermediate results in script variables, so **none of it lands in your
context** and there is **no `agents.md` registry, no `outputs/` files, and no polling to manage.**
The same large jobs the manual loop handles map directly to short scripts:

| Manual (Tier 3) job | Workflow (Tier 2) shape |
|---------------------|-------------------------|
| Audit: spawn per-area agents, collect outputs, dedup, verify | `parallel` finders → dedup in plain JS → `parallel` adversarial refuters; keep only findings that survive |
| Repetitive batch (e.g. 23 tests) | one `parallel`/`pipeline` over all items; runtime caps concurrency |
| Multi-phase build | `pipeline(items, implementStage, verifyStage)` — item B verifies while item C is still implementing |
| Unknown-size discovery | loop spawning finders until K consecutive rounds find nothing new |

See `claude-code-api.md` for the `Workflow` primitives, limits, and gating.

## Workspace Structure

All orchestration state lives in one folder in the repo for the work (no separate orchestration
folder). The plan, context notes, task list and progress log take whatever file names the project
uses; this protocol fixes only `agents.md` and `outputs/`:

```text
<work-dir>/
├── <plan>             # Strategy and phases
├── <context notes>    # Decisions and integration points
├── <task list>        # Tasks with status
├── <progress log>     # Append-only
├── agents.md          # Agent registry (active orchestration state)
└── outputs/           # Agent output files
    ├── {agent-id}-{task}.md
    └── ...
```

**Agent Registry Format (`agents.md`).**

```markdown
<!-- RECOVERY: {work} | Orchestration State -->
<!-- AGENTS: running=2 complete=5 pending=3 -->

# Agent Registry

| Agent ID | Type | Status | Quality | Output File | Started | Ended | Task |
|----------|------|--------|---------|-------------|---------|-------|------|
| abc123 | Plan | complete | success | outputs/abc123-api-design.md | 14:30 | 14:42 | Design API |
| def456 | general-purpose | running | - | outputs/def456-impl.md | 14:45 | - | Implement service |
| ghi789 | general-purpose | pending | - | - | - | - | Write tests |
```

**Status values**: `pending`, `running`, `complete`, `failed`

**Quality values** (set after completion):

| Quality | Meaning |
|---------|---------|
| `success` | Agent completed task fully and correctly |
| `partial` | Agent completed some work but not all (scope too large, context pressure) |
| `failed` | Agent could not complete the task (wrong approach, errors, blocked) |
| `overwhelmed` | Agent flagged scope issues or returned incomplete work due to context limits |

**Duration tracking**: `Started` and `Ended` columns enable cost/efficiency analysis over time.
Patterns like "architect tasks averaging 12min but engineer tasks averaging 25min" reveal
decomposition issues.

**Failure analysis**: When quality is `partial`, `failed`, or `overwhelmed`, append a brief
note to the agent's output file:

```markdown
## Failure Analysis

- **Symptom**: [What went wrong]
- **Cause**: [Scope too large | Ambiguous instructions | Missing context | Wrong agent type]
- **Action**: [How remainder was handled - decomposed, reassigned, etc.]
```

## Agent Output Protocol (File-First)

Agents write details to files and return only summaries, to keep the orchestrator's context small.

**Agent Prompt Template.** Every agent prompt in this tier carries this template, ad-hoc spawns
included; agents run in the background and the deliverable file is what survives compaction:

```markdown
## Output Requirements

1. Create <work-dir>/outputs/{agent-id}-{task}.md FIRST, with a skeleton
   section header per task. Append each task's section IMMEDIATELY when that task completes —
   never defer the write to the end. Long runs die of context overflow at report time with the
   work already on disk; incremental writes keep the report alive even if that happens.
2. Write detailed output to that file as you go.
3. Return ONLY this summary format in your response:

   Output: outputs/{agent-id}-{task}.md
   Summary:
   - [Finding 1]
   - [Finding 2]
   - [Finding 3]
   Next: [Recommended step]

4. Keep the full analysis in the file; the orchestrator reads it there when it needs detail.
```

**Output File Format.**

```markdown
<!-- AGENT: {agent-id} | {agent-type} | {timestamp} -->

# {Task Title}

## Summary (for orchestrator)

- [Key finding 1]
- [Key finding 2]
- [Key finding 3]

## Decision Log

Key choices made during this task and why:

- **[Decision]**: [What was chosen] — [Why, and what alternatives were considered]
- **[Decision]**: [What was chosen] — [Why]

## Recommendations

1. [Specific next step]
2. [Alternative if applicable]

## Details

[Full analysis here - orchestrator reads only if needed]

## Files Modified

- `path/to/file.py` - [what changed]
```

## Task Decomposition

**Before spawning any agent**, decompose to agent-sized chunks. The scope table (single file,
multi-file, large feature, cross-cutting) is `../SKILL.md` § Task Decomposition.

**Repetitive Task Decomposition.** When a task involves N similar items (tests, migrations,
updates), count the items, split them into batches by the table below, and run independent
batches in parallel; pass a completion summary between sequential batches.

| Task Type | Items per Agent | Rationale |
|-----------|-----------------|-----------|
| Complex tests (multi-fixture, edge cases) | 5-8 | Each needs careful setup/assertions |
| Simple tests (happy path, similar structure) | 8-12 | More templated, less cognitive load |
| Repetitive operations (migrations, updates) | 8-12 | Homogeneous work, clear pattern |
| Fix-until-green iteration (test → fix → re-test, review → fix) | 2-3 | Retries burn context; see `../SKILL.md` § Task Decomposition |
| **Hard ceiling** | 15 | Never exceed regardless of simplicity |

**Example: "Write 23 test cases" (Tier 1/3 — manual batching)**

```python
# BAD: Single agent for all 23 tests
Agent(prompt="Write 23 test cases for the batch processor")

# GOOD: Chunk into 3 parallel agents [8, 8, 7]
test_batches = [test_cases[0:8], test_cases[8:16], test_cases[16:23]]
for i, batch in enumerate(test_batches):
    Agent(
        subagent_type="general-purpose",
        model="sonnet",
        prompt=f"""Write tests for batch processor - Batch {i+1}/3:
        {format_test_specs(batch)}

        ## Output Requirements
        Write to: <work-dir>/outputs/tests-batch-{i+1}.md
        """,
    )
```

**Example: Decomposing "Implement Scheduler".**

```python
# BAD: Single agent for entire scheduler
Agent(prompt="Implement the entire task scheduler with queuing, execution, and retry logic")

# GOOD: Decomposed into agent-sized chunks with file-first output

# Agent 1: Core data models
Agent(
    subagent_type="general-purpose",
    model="sonnet",
    prompt="""Create scheduler models: Task, TaskRun, TaskQueue.

Files to create: app/models/scheduler.py

## Output Requirements
Write detailed output to: <work-dir>/outputs/agent1-models.md
Return ONLY the summary block: Output path, three findings, Next.
""",
)
```

## Orchestrator Loop

Core loop for executing the work, every turn:

```text
1. READ STATE
   - Read the task list -> identify current phase
   - Read agents.md -> check active agents

2. CHECK COMPLETIONS & DISPATCH
   - On each completion notification: read its `<result>` summary and check the output file
     exists via Bash
   - If complete: Update agents.md status
   - VERIFY DISPATCH (binding surface): for every row marked `running`, confirm dispatch
     actually fired. The evidence is the `Agent()` launch response (record its agent id in the
     row when it returns), the output file (`ls outputs/{agent-id}-*`; agents write
     incrementally, so even a partial file counts) or a completion <task-notification>.
     agents.md is a note you wrote, not proof the Agent() call fired. A row with a launch id
     and no file yet is pending: an agent writes nothing before its first finished task, so
     wait for its notification. Only a row with no launch id, file or notification was dropped
     (often after a notification interrupted the turn): re-spawn it. Replace a launched agent
     only after its notification reports a failure or you stop it with `TaskStop`, so two
     agents never hold one assignment. `TaskList` is an additional signal only under Agent
     teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) — not available by default.

3. READ OUTPUT FILES
   - For completed agents: Read their output file directly
   - Use Read tool on <work-dir>/outputs/{agent-id}-*.md
   - Extract summary, update the context notes if needed
   - Don't re-read the same output file

4. SPAWN NEXT (atomic dispatch)
   - Based on phase requirements, spawn next agents
   - Include file-first output requirements in every prompt
   - Emit the `Agent()` call and its agents.md `running` row in the SAME tool block.
     Never edit agents.md to `running` in an earlier turn/block than the launching call —
     writing `running` speculatively creates false confidence that work is in flight when
     nothing launched.

5. CHECKPOINT
   - Update the task list's progress
   - Append to the progress log every 5 agents

6. REPEAT or PAUSE
   - If agents running: carry on with work that does not depend on them; act on each
     completion notification as it arrives
   - If phase complete: move to next phase
   - If decision needed: AskUserQuestion
```

## Context Discipline

**Process-Then-Forget Pattern.** After reading substantial content:

1. Extract what's needed
1. Persist summary to the work's folder
1. Move on (don't reference the content again)

## Agent Overwhelm Protocol

If an agent returns incomplete work or flags scope issues:

1. **Don't resume** - they've already consumed context
1. **Extract what they completed** - persist to the work's folder
1. **Decompose the remainder** - break into smaller chunks
1. **Spawn fresh agent(s)** - with handoff summary of prior work

## Recovery After Compaction

1. Read the progress log - scan recovery headers
1. Read the task list - identify current phase and progress
1. Read `agents.md` - check agent status
1. Continue orchestrator loop from current state
