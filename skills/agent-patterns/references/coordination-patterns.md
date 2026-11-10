# Coordination Patterns

Patterns for agent handoffs, parallel execution, and workflow coordination.

## Confidence-Based Coordination

Thresholds live in `../SKILL.md` § Confidence Scoring (one table, not restated here).

**Skip Coordination When.** Skip the full workflow when ALL conditions are met:

- [ ] Confidence > 0.8
- [ ] All quality gates pass
- [ ] Change < 50 lines
- [ ] No auth/tenant/security code touched
- [ ] Follows existing pattern exactly

Just commit and move on. Over-coordination is waste.

---

## Standard Output Format

An agent that returns its result inline ends its final response with coordination
recommendations. A Tier-3 agent returns the compact summary of `orchestrator-protocol.md`
§ Agent Output Protocol (File-First) instead, and a Workflow stage returns its schema.

```markdown
## Recommended Next Steps

**Confidence Score**: 0.XX

### Immediate Validation (Required)
- Run the project's lint, type and test gates

### Coordination Recommendations
1. **[If condition]**: Spawn `[agent-name]` for [specific task]
   - Focus: [key concerns]
   - Success criteria: [how to verify]
```

---

## Handoff Templates

Role names in angle brackets stand for whichever agent covers that role in your setup.

**After Backend Implementation.**

```markdown
**Confidence Score**: 0.XX

1. **Quality Gates** (Required)
   - Run the project's backend lint, type and test gates

2. **Architecture Review** (if confidence < 0.8 OR complex)
   - Spawn `<architecture reviewer>` to review [specific aspects]

3. **Security Validation** (if state-changing operations)
   - Spawn `<security reviewer>` for OWASP validation

4. **Test Enhancement** (if coverage < 80%)
   - Spawn `<test author>` for comprehensive test suite
```

**After Frontend Implementation.**

```markdown
**Confidence Score**: 0.XX

1. **Quality Gates** (Required)
   - Run the project's frontend lint, type and test gates

2. **Architecture Review** (if component > 300 lines OR complex state)
   - Spawn `<frontend architecture reviewer>` for component structure review
```

**After Investigation.**

```markdown
**Confidence Score**: 0.XX

1. **Implementation** (if root cause found)
   - Spawn `<backend implementer>` or `<frontend implementer>` with fix

2. **Architecture Review** (if systemic issue)
   - Spawn `<architecture reviewer>` to design solution

3. **Escalate** (if blocked)
   - Report to main agent with findings and blockers
```

**Handoff Template (Generic).** When recommending a specialist, provide:

```markdown
- **Spawn**: `[agent-name]`
- **Task**: [Specific, actionable task]
- **Context**: [Key information specialist needs]
- **Files**: [Specific paths with line numbers]
- **Success Criteria**: [How to verify completion]
```

**Bad**: "Spawn architect to review"
**Good**: "Spawn `<architecture reviewer>` to review tenant isolation in
`app/billing/invoice_service.py:145-280`"

---

## Parallel vs Sequential Execution

**Run in Parallel.** Use a single message with multiple `Agent` calls when:

- Independent architecture reviews (backend + frontend)
- Security reviews across domains
- Domain analysis (auditor + security + performance)
- Validation checks that don't depend on each other

**Run Sequentially.** When output from one agent is input to another:

- Investigation -> Architecture -> Implementation
- Backend API -> Frontend integration, until the API contract is fixed; after that the two
  implementations run in parallel, each built against the contract
- Quality gates -> Deployment
- Planning -> Development -> Testing

When a task spans domains (e.g., "fix API and update frontend consumer"), the domain with the
most code changes owns it: its implementer goes first and hands the next one a summary
("Backend complete, API contract is X"). The API is the source of truth its consumers build on.

**Reconciling Parallel Results.** When parallel agents return different recommendations:

| Situation | Action |
|-----------|--------|
| Both high confidence (> 0.8) | Merge recommendations, proceed |
| One low confidence (< 0.6) | Address low-confidence concerns first |
| Conflicting advice | Surface both to orchestrator with tradeoffs and your lean |

---

## Failure Handling

| Situation | Detection | Action |
|-----------|-----------|--------|
| Specialist returns confidence < 0.5 | Confidence field in output | Escalate to architect or human |
| Platform error (`[Tool result missing...]`, `internal error`, tool use rejected) | Error text in Agent result | Log error. Retry once. If retry fails, do the work directly or report blocker. |
| Agent returns empty/malformed output | Missing expected files or status markers | Treat as failure. Retry once with simplified prompt. |
| Agent appears hung | User reports it, or its completion notification never arrives | Stop it with `TaskStop`. Do the work directly if feasible, otherwise retry once with a smaller assignment or escalate. |
| Conflicting recommendations | Multiple agents disagree | Surface both with tradeoffs |
| Specialist unavailable | Agent type not found | Use general-purpose with domain prompt from agent definition |

**Platform Error Recovery Protocol.** When ANY `Agent()` call returns an error (platform error, tool
rejection, empty result):

1. **Log it** — record the error in the session's working artifact (review log, plan doc, etc.)
2. **Assess scope** — can you do this work directly? Research/exploration often can be done inline.
3. **Retry once** — same agent type, same prompt. Platform errors are often transient.
4. **If retry fails** — either:
   a. Do the work directly (if scope is small enough)
   b. Decompose into smaller agent calls (reduce blast radius per call)
   c. Report blocker to user with what was attempted
5. **Never silently continue** — the user should know an agent failed, even if you recovered.

---

## File-First Briefs (Input Side)

The output protocol (`orchestrator-protocol.md` § Agent Output Protocol (File-First)) has an
input twin. An inline prompt longer than ~25 lines is the one input nothing can audit
afterwards: it is paraphrased or truncated silently, it is gone when the agent is resumed after
compaction, and a re-run Workflow stage has no artifact to diff against.

- Write the assignment to a brief file (`<work-dir>/briefs/{agent-id}-{task}.md`, or the
  scratchpad for ad-hoc fan-outs) carrying the evidence, constraints, file list, and the exact
  output contract. Hand the agent a pointer plus the one-line goal: "Read `<brief>` and do it."
- Applies to `Agent` prompts and `Workflow` `agent()` stages alike; a task-list entry is already
  a brief when it names the files and the acceptance check. The check is of the claim's own
  kind: a code fact at file:line, a data inference by the query that measures it, a domain
  definition by a primary source.
- **Inline payloads stall.** Stages whose prompt inlines tens of KB of JSON hit "no progress for
  180000ms" and exhaust their retries; the same work as short prompts pointing at a scratchpad
  file (`<scratchpad>/<stage>.json`) finishes first try. A stall kill loses the stage's work and
  leaves a half-edited file for the retry. A Workflow script has no fs access: write bulky
  inputs to scratchpad files first, keep inline payloads to a few KB.
- **Per-stage output paths in shared briefs.** Parallel stages following one brief write test/run
  logs to templated paths (`runs/full-suite-<branch>-<stage>.txt`), never one fixed filename: a
  concurrent run overwrites another stage's log end to end, and the stage notices only when its
  counts do not match. When reading a stage's result, check the log it cites is its own.
- **A brief's helper convention is a premise.** Before handing N agents a shared helper as "the
  convention" (`toResult()`), run it once against each real response shape the sites will see
  (2xx with body, 204 empty, 4xx/5xx with and without body, no response); mocks copy the client's
  real shape, not the helper's assumption; mark unverified premises "verify first". Untested, a
  helper that treats an empty body as an error calls every successful 204 DELETE a failure, and
  agents then diverge into hand-rolled checks of their own.
- **UI briefs carry a design reference.** A screen dispatch names an approved mockup or a
  screenshot (none: design it first); audit rendered screenshots against it before hand-off.
- **Investigation briefs carry onboarding context.** A debugging, root-cause, or data-investigation
  dispatch gets what a new engineer would be handed on day one: the entry-point `file:line` (route,
  job, or producer), one query or command already run against the real data with its output, and
  the decision doc or the work's context notes that explain why the code is shaped that way. A
  brief that only restates the symptom sends the agent to rediscover all three, or to guess.
- **Long-lived processes are stopped before the agent returns.** A brief that lets an agent start
  a stack or server (`docker compose up`, a dev server) tells it to stop what it started before
  returning (`docker compose stop`, never `down -v`); the orchestrator checks `docker ps` after the
  agent finishes. Left-running stacks exhaust the Docker VM's memory.
- Short assignments (a few lines, no evidence to carry) stay inline. The threshold is
  auditability, not style.

---

## Agent Resumption Patterns

Agents can be **continued** with `SendMessage` (by agent id or name) to preserve their previous
context — `Explore` included. A new `Agent` call always starts fresh. (Tool names: see
`harness-api.md`.)

**When to Continue vs Spawn Fresh.**

| Scenario | Action | Why |
|----------|--------|-----|
| < 500 lines read total | **Continue** (`SendMessage`) | Context preserved, agent remembers findings |
| Single-focus investigation | **Continue** (`SendMessage`) | Agent knows what it was looking for |
| 5+ files over 100 lines each | **Spawn fresh + summary** | Context consumed by file contents |
| Investigation pivoted to new area | **Spawn fresh + summary** | Original context no longer relevant |
| Different domain needed | **Spawn fresh** | Different specialist, no shared context |

**Continue Pattern (Context Light).**

```markdown
## First invocation
Agent:
  subagent_type: general-purpose
  model: opus
  prompt: "Investigate flaky test in test_invoice_service.py"
# Note the agent's id/name from the launch response, plus its investigation report

## After applying fix - continue the same agent
SendMessage:
  to: "<agent id or name>"
  prompt: "Verify the fix I applied to the race condition"
# Agent remembers: original symptoms, files examined, root cause
```

**The 2-3-task ceiling applies across resumptions, not just within one dispatch.**
Continuing one agent through sequential review-fix rounds (e.g. Codex round 1 → 2 → 3)
preserves ALL prior context, so it accumulates past the ceiling even though each round
looks small. After ~2 fix rounds, stop the agent and spawn fresh with a self-contained
handoff (findings at file:line + repo state) instead of continuing — a well-written
dispatch loses nothing, and continuing risks the agent degrading/compacting mid-task
pointed at sensitive code.

**Handoff Pattern (Context Heavy).** When an agent has consumed significant context, spawn fresh but
pass findings:

```markdown
## Don't continue - pass summary instead
Agent:
  subagent_type: general-purpose
  model: opus
  prompt: |
    Verify fix for race condition.

    ## Previous Investigation Summary
    - Root cause: Missing lock in InvoiceService.update()
    - File: app/billing/invoice_service.py:145
    - Fix applied: Added asyncio.Lock() around DB operations

    Confirm the fix resolves the original flaky test.
```

**Context Consumption Heuristics.**

**Low context** (safe to continue via `SendMessage`):

- Quick lookups (grep, glob returning paths only)
- Single file reads < 200 lines
- Status checks and verification
- Total content read < 500 lines

**High context** (spawn fresh + summary):

- Read 5+ files over 100 lines each
- Large file analysis (> 500 lines per file)
- Full-file reads of multiple modules
- Investigation that pivoted or expanded scope
- Total content read > 2000 lines

---

## Fresh-Context Reviews

For quality-critical code, spawn a **separate** reviewer agent AFTER implementation completes. The
reviewer runs in its own context without bias from having written the code. A review is a
judgment read that is not anchored on the implementer's; it never stands in for verification.
Whether the code works binds at its tests, its output or the rendered page, and that check is
the orchestrator's own (`rules/core-rules.md`: don't spawn a subagent to verify your own work).

**When to Use Fresh-Context Review.**

| Trigger | Action |
|---------|--------|
| Security-sensitive code (auth, tenant, payments) | Use fresh-context review |
| Confidence < 0.75 after implementation | Use fresh-context review |
| Complex logic (> 200 lines changed) | Consider fresh-context review |
| User requests review | Use fresh-context review |

**Pattern.**

```text
1. Implementation agent completes work
2. Run quality gates (lint, types, tests)
3. Spawn SEPARATE review agent (fresh, not continued)
4. Review agent has no prior context about implementation decisions
5. Address findings, iterate if needed
```

**What the Reviewer Gets: Contract, Not Claim.** Split the implementer's output in two before
writing the review prompt:

| Part | Contents | Pass to reviewer? |
|------|----------|-------------------|
| **Contract** | Task intent as originally stated, constraints, acceptance criteria, the files | Yes |
| **Claim** | The implementer's Decision Log, rationale, confidence, "why this is correct" | **No** |

A reviewer who reads the claim anchors on it and reviews the argument instead of the code.
Hand over the artifact and the contract; the reviewer derives its own conclusions. If a
decision needs judging, ask the reviewer what it would have decided *before* showing the
Decision Log, then compare.

**Review Agent Prompt Template.**

```markdown
Adversarial review. Find what is wrong with the changes in [files]. Assume the author is
overconfident. Report issues only, with no validation or praise; if there are none, state
explicitly that you searched and found none.

Look for:
- **Intent mismatch**: code that works but solves the wrong problem or misreads the contract
- Unstated assumptions, edge cases, error-handling gaps
- Hidden coupling and broken codebase conventions
- Security concerns (injection, auth bypass, data exposure)
- Performance implications

You have NOT seen these changes before. Review with fresh eyes.

Contract: [task intent as originally stated + constraints — NOT the implementer's reasoning]

Files to review:
- [file1:lines]
- [file2:lines]

Provide specific findings with line references and suggested fixes.
```

**Reconcile and Stop.** The reviewer's output is data, not a verdict. Re-read the artifact against
each finding and classify: contract misread (fix the contract, re-review) → valid and actionable
(fix, re-review) → valid trade-off (document, accept) → noise (note it; ask whether the contract was
unclear).

Stop when a cycle yields only trivial or repeated findings, or after 3 cycles — then escalate
to the user, don't keep looping. **Doubt theater**: 2+ cycles with substantive findings and
zero classified actionable means the reconcile step is rubber-stamping — escalate.

**Anti-Patterns.**

- **Resuming the same agent**: Defeats the purpose - same context, same blind spots
- **Asking implementer to "review your own work"**: Self-review is weak validation
- **Skipping for "simple" changes**: Simple changes can have non-obvious security implications
- **Passing the claim**: Decision Log or rationale in the reviewer prompt biases agreement
- **Prompting "is this good?"**: A balanced ask gets a balanced answer; ask for issues only
- **Re-spawning the same unchanged artifact**: Stalling, not reviewing
