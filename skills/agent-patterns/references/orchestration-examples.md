# Phase-Contract Template

This is the handoff shape for multi-phase orchestration: what one phase must hand the next so
work stays composable instead of ad hoc. Under Tier 2, each phase below maps to a `Workflow`
`pipeline` or `parallel` stage — see `harness-api.md` for the stage API; it is not restated here.

## Template

```markdown
Phase N — <name>
- Agents: <role(s)>. Every generic spawn (no named role) states a `model=` tier explicitly;
  every Workflow `agent()` stage passes an explicit `effort`.
- Input: <what this phase consumes — prior phase deliverables, specialist recommendations>
- Expected Output: <the artifact or decision this phase produces>
- Deliverables:
  * <concrete output 1>
  * <concrete output 2>
- Validation: <how the phase's own output is checked before it's treated as done>
- Decision Point: GO if <criteria> — proceed to Phase N+1.
  NO-GO if <criteria> — return to Phase <earlier N> with findings.
```

## Ordering Note

- Consult specialists **in parallel, before** detailed planning — their combined output is the
  planning phase's `Input`, not a parallel track to it.
- The planning phase must address every specialist constraint raised in consultation; its
  `Validation` checks that explicitly.
- Implementation phases run sequentially within one domain (each depends on the prior phase's
  deliverables) and in parallel across independent domains (no shared `Input`/`Deliverables`
  overlap).
- **In the `Workflow` script itself, this means wrapping independent lanes in one
  `parallel([() => laneA(), () => laneB()])`, not awaiting them top-down.** Before launching,
  list every top-level `await` and ask what it needs from the previous one — if the answer is
  "nothing", it belongs in the `parallel()` call, not after it. Serialize only the specific unit
  that shares a file (e.g. reconcile one dir after its synth), never the whole lane around it.
  Get the shape right at launch: the resume cache (`harness-api.md`) makes reordering lanes
  after launch expensive.
- A `Decision Point` gates every phase transition — GO/NO-GO is evaluated against the phase's
  own `Validation`, not assumed from the deliverable existing.
