---
name: external-feedback
description: Use when the user shares suggestions from external AI assistants, consultants, or reviewers. Verifies each suggestion independently against the code before accepting it.
allowed-tools: Read, Grep, Glob, Skill
argument-hint: "[feedback text | source]"
---

# External Feedback Integration

## Core Principle

**Never accept external feedback without independent verification.**

## Integration Protocol

### 1. Review Recent Work Context (if applicable)

If the feedback is reviewing work you or an agent just completed in this conversation,
recall that context before judging the feedback itself:

- What was the original task/request, and what trade-offs did you consciously make?
- Did you or an agent already flag this area as needing review (e.g. a low confidence
  score, or a noted gap)? If so, feedback that raises the same point **validates a known
  gap** rather than surfacing something new — lean ACCEPT.
- Feedback that contradicts a documented, deliberate decision needs re-evaluation of that
  decision's rationale, not automatic acceptance.
- A constraint the feedback ignored can make an addition not worth it; it never settles a
  bug claim.

### 2. Verify Each Suggestion

For each suggestion:

1. **Verify against codebase** - Read actual files, search patterns (`Grep`/`Glob`), confirm
   the claim matches what the code actually does today.
1. **Classify it** - a **bug claim** says the code does the wrong thing; an **addition**
   proposes something new (an "also add X", a refactor, a safeguard, an optimization, a test
   for a behavior with no bug shown). A missing test the change was required to carry is
   already requested, so it is Accepted for the paths the change adds or alters, not weighed as
   an addition: an endpoint's unauthenticated, role or ownership and cross-tenant denial
   cases, or a reproducing test for the bug the change fixes
   (`rules/engineering.md` § Tests and Docs Ship with the Change). Any other missing test, with
   no bug shown, is Rejected: a review finding becomes a test only when it reports a real bug.
1. **Settle who introduced a bug** - a diff locates changes; it does not prove cause. A
   changed caller, config or dependency can break code the diff never touches. Trace the
   failure's trigger: if this change adds or alters it, the bug is introduced; if the trigger
   exists unchanged on the base, it is pre-existing. When neither can be shown, say so in the
   verdict.
1. **Check project docs** - Validate against requirements docs, skills, architectural
   decisions, and any active workstream context.
1. **Evaluate fit (additions only)** - check YAGNI (do we have this problem?), ROI (does the
   benefit exceed the cost?), complexity (does it add indirection we don't need?), and
   premature optimization (are we solving for scale we don't have?), against the project's own
   stage section in its `CLAUDE.md` or `AGENTS.md` when it has one.
1. **Apply the verdict tables**:

**Bug claims** are judged on whether the failure can happen, never on whether fixing it is
worth it:

| Claim holds? | Reachable? | Verdict |
|--------------|------------|---------|
| Yes | Yes, introduced by this change | Accept |
| Yes | Yes, pre-existing or outside this change | File (an issue per rules/core-rules.md § Discovery Protocol) |
| Yes | No | Reject (unreachable): cite the `file:line` that gates the trigger off; for an absence claim, the search that shows it and its result |
| No | — | Reject (incorrect) |

A pre-existing site of the defect this change fixes is not filed: it is the fix's scope
(rules/core-rules.md § Discovery Protocol), so it is Accepted.

**Additions**:

| Valid? | Valuable? | Verdict |
|--------|-----------|---------|
| Yes | Yes | Accept when the request already covers it; otherwise Recommend |
| Yes | No | Reject (YAGNI) |
| No | — | Reject (incorrect) |

- **Valid** = factually correct about the code and architecture
- **Valuable** = solves a problem the project has today, at a cost its stage justifies
- **Reachable** = outside input, a system guarantee, or two sessions get there
  (rules/engineering.md § Unrequested Hardening)
- **Accept** = the caller owns the action: a fix in an implementation flow, a sketch in an
  advisory one
- **Recommend** = surfaced in the report as a one-line recommendation; neither built nor filed

Some feedback is neither a clean accept nor reject - part of the claim is correct, part
is off-base or inapplicable. Treat that as **Partially Valid**: keep what's correct, adapt
it to fit the codebase, and say explicitly what was dropped and why.

**Burden of proof for a Reject on the facts**: dropping a right finding silently destroys it,
and nobody learns it was dropped. So a REJECT of a bug claim, or of an addition as incorrect,
must cite the `file:line` that disproves the claim or gates its trigger off; an absence claim
("no caller", "nothing configures it") cites the search that shows it and what it returned; an
addition rejected as not worth it rests on the fit check above instead. "I can't verify this",
"the code looks fine to me", "I wouldn't have raised it", and "the citation is slightly off" are
not disproof - judge the claim, not the quote. A reason that grants the mechanism (it is rare,
the window is narrow, a retry converges, the impact is low, it is pre-existing) is not a Reject
at all: the claim holds, so it is Accept (at a low severity if that fits) when this change
introduced it, File when pre-existing (Accept when it is another site of the defect this change
fixes, as above). The one exception is a class on a fixed out-of-scope
list the caller keeps (`ship`'s repo notes). A claim that reading cannot settle is settled by
running: a failing test that reproduces it accepts it, and a passing test that exercises its
trigger disproves it; a reversible experiment works the same way. The failing test is committed
with the fix; the passing one is scratch and is deleted. Only a check that needs
something out of reach (an environment, a third party) stays open, named with what would settle it
and who owns it, and it never counts as accepted. When the feedback reviews code you wrote, this
bias is strongest; hold the bar harder. Hold it hardest on memory/bounds safety, concurrency and
lock semantics, a behavior or compatibility change (a message, field, default, or error path the
old code produced and the new code does not), tenant isolation, and a parameter the function
accepts and never uses: there, reject only by naming the input or interleaving that cannot occur.
A behavior change the request asked for, or the user approved, settles a compatibility claim
against it; a `Decision:` or `Regresses:` line the author wrote alone settles nothing.

### 3. Report Results

A caller's own record (`ship`'s round block) replaces this shape. Invoked directly, give the
assessment as the chat reply, in this shape:

```markdown
## External Feedback Assessment

**Received**: [N] suggestions from [source]

**Incorporated** ([X] items):
1. [Suggestion] - [rationale + evidence]

**Partially Valid** ([Z] items):
1. [Suggestion] - [What was correct] / [what was dropped] - [modified approach + rationale]

**Filed** ([W] items):
1. [Suggestion] - [why pre-existing or outside this change] - [issue link]

**Recommended** ([R] items):
1. [Suggestion] - [what it would add, why it is not in the request]

**Rejected** ([Y] items):
1. [Suggestion] - [YAGNI / Invalid / Unreachable] - [rationale + evidence]

**Open** ([V] items):
1. [Suggestion] - [what would settle it] - [owner]
```

### 4. Watch for Recurring Patterns

If a source (a specific external AI, tool, or reviewer) repeatedly makes the same kind of
mistake - e.g. consistently recommends over-engineering, misunderstands the multi-tenant
model, ignores startup-stage constraints - note it so future feedback from that source gets
extra scrutiny on that exact point. This doesn't need a durable artifact unless the pattern
is severe enough to warrant one; a one-line callout in the assessment report is enough.

## Cite Evidence

Always reference:

- File paths and line numbers
- Project documentation
- Architectural decisions (skills, CLAUDE.md)
- Test results or error logs

**Example**: "Evidence: `<path>/alert.tsx:58` renders the description text in the child
element, not the wrapper"
