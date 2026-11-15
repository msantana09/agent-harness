# When to Use Codex

## When Codex Adds Value

| Use case | Reason |
|----------|--------|
| **Plan gap-analysis (sweet spot)** | Codex reliably finds call-path and plan-grounding gaps that survive Claude review rounds. Use it to critique a drafted plan, not to author one: plan development stays with Claude |
| Architecture validation | A second model family's view |
| Complex debugging, security audits | Sol `high`, multi-step analysis |
| Large multi-file review | Codex reads the files itself; scope it in the prompt |

**Codex and Claude reviewers catch different classes. Run both; neither clears a plan alone.**

- Codex grounds the *call path*: it catches plan-grounding gaps the author missed, line-precise.
  Run it as a pre-execution reviewer for plans that touch live call graphs.
- Claude reviewers of the same plan find gaps in the runtime and integration class Codex does not
  probe: code running outside the request lifecycle, incomplete seed data,
  observability-mechanism mismatches.
