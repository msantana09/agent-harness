# Engineering Principles

How code is written and changed.

## Code Quality Principles

### Code Search

- Use the LSP tool (pyright-lsp / typescript-lsp plugins) for "who calls this", "where is this
  defined", and type errors — grep counts of callers are floors: dependency injection,
  decorators and re-exports hide some.

### Verify Before Implementing (No Speculative Code)

- Before adding a code path, function, or capability nobody asked for: grep the app source tree for
  callers to verify they exist (a requested capability's caller is the request)
- No callers = don't implement. Raise explicit error instead of handling gracefully.
- **Before hand-rolling behavior**: check for a native platform/framework feature (MDN/spec search
  for browser behavior)

**Simplicity check**: overcomplication shows up as needless indirection or an abstraction
with only one caller — collapse those before adding more.

### Unrequested Hardening

A lock, retry, guard or validation nobody asked for, whether Claude thinks of it or a reviewer
flags it, is a recommendation, not scope (communication.md § Surface ≠ Scope In). Test
reachability, not likelihood: name the input and interleaving that make it fail here. Reachable
means outside input gets there (auth, injection, tenant isolation, secrets), the system
guarantees it (at-least-once delivery, retries, double submit, two users editing one record), or
two sessions reproduce it: a defect, so fix it or file it. Unreachable is not a finding: no code,
no issue, one `Flags:` line (the failure and its impact, why it looks unreachable, the complexity
it adds), built only on the user's yes.

### No Fallback Values

Never invent an input in code: `os.getenv("KEY", "default")` — wrong (silent failure).
`os.environ["KEY"]` or explicit raise — correct. Applies to env vars, config files, secrets,
feature flags, any external setting. On the command line the same rule is core-rules NO
FABRICATED VALUES.

### Test-First Bug Fixes

When the user reports a bug or shares an error log, first write a failing test that reproduces
it, then fix the code and confirm the test passes. The reproducing test comes before the fix,
not after. It is committed with the fix (a bug that happened passes the keep rule in § Tests and
Docs Ship with the Change), at the highest cheap layer that reproduces the bug.

### Clean-Room Reference Designs

A feature modeled on an outside project takes its design from docs, schema descriptions or
screenshots, never from that project's source files: copyleft code read into context gets
reproduced in ours. Reading a dependency's source to debug or learn its API is not covered.

### Surgical Changes

**Touch only what you must. Clean up only YOUR mess.**

- Every changed line should trace directly to the user's request
- Don't "improve" adjacent code, comments, or formatting
- A new file goes in the subdirectory that matches its kind; the repo root takes only what tools
  or conventions require there (README, AGENTS.md, manifests)
- Coin no shorthand in identifiers (`billing-api`, not `bapi`); a rename or refactor that touches
  an abbreviated identifier expands it
- Unrelated dead code you notice is a `priority:low` finding: file it per core-rules
  § Discovery Protocol and leave the code in place
- A pre-existing bug or hazard found while working: route it per
  core-rules § Discovery Protocol; never bury the fix in the diff unflagged
- Tests are sized by § Tests and Docs Ship with the Change — one focused test per kept
  behavior, at one layer; neighboring test files are no guide to size. Scratch scripts and
  quick checks used to verify are deleted, not committed. This is about extras only: every
  behavior the task asks for is implemented completely

**Orphan cleanup rule**:

- Remove imports/variables/functions that YOUR changes made unused

**Minimal ≠ small-scope.** "Surgical" means the smallest *amount of code* that solves the
problem, not the smallest *area touched*. A two-line HTML restructure beats a hundred-line
CSS workaround. If a layer-appropriate fix (restructure markup, refactor the component) is
cleaner than patching where the bug surfaced, propose it — ask first if it's out of scope.

**Rule of 500**: a change that touches more than ~500 lines (rename sweeps, signature
migrations, import rewrites) is done with automation — `ast-grep`, a codemod, or a `sed`
script — never by hand-editing each site. Hand edits at that scale drift, and the diff is
unreviewable; a script is both the change and its own inventory. Any scripted multi-file
rewrite: snapshot first (git for tracked files, a tarball for untracked), preview a real
`diff -U0` of every touched file before applying (the list of intended changes is not a
preview), and run cleanup passes only on files the primary change touched, each regex scoped
to the residue the change leaves.

### Surface Conflicts, Don't Average Them

When two existing patterns in the codebase contradict each other, pick one — don't
write new code that blends both.

- **Pick the more recent or better-tested pattern.** Whichever has more callers, is more
  recently merged, or has cleaner test coverage.
- **Log the pick** as a `Decision:` / `Instead of:` pair (core-rules ACT BY DEFAULT
  § Log format).
- **Flag the other for cleanup** as a separate finding — don't silently leave both alive.

### No Backward Compatibility

- Delete what your change deprecates and update every call site; no shims, aliases or dual paths.
  Persisted customer data and public API/MCP contracts go through core-rules ACT BY DEFAULT
  never-decide-alone.

### No Breadcrumbs

When you delete or move code, remove it completely. No trace.

- No `// moved to X` or `// relocated to Y` comments
- No `// removed` or `// deprecated` markers
- No commented-out code blocks "for reference"
- No `_unused_param` renames - delete the parameter and update callers

Comments state what the code does now and why. Review rounds, finding ids, plan steps and chat
requests (`Codex r2 #5`, `review round 2, finding R4`, `as requested`) go in the PR body below the
divider: keep the reason, drop the citation. An issue link stays, and so does a regression test
naming the bug it guards. A correction from the conversation, or a reviewer's finding, changes the
behavior and nothing else: it never lands in the deliverable as a caveat, note or explanation, in
a docstring, a README, a skill or a script's output. Code and docs describe what the code does and
what it assumes of its environment, never the review that shaped it.

### Tests and Docs Ship with the Change

A code change ships with its documentation (docstrings, READMEs, inline comments) and with the
tests these rules keep, no more:

- **Keep rule**: commit a test only when it pins something a user, an API client, security or the
  domain's calculations depend on, or a bug that happened. A test written only to check a change
  while building it is scratch: run it, then delete it.
- **One layer per behavior**: test a behavior once, at the highest layer that can observe it and
  still runs cheaply, not at the unit and the integration layer both. An endpoint's cases scale
  with its risk; each endpoint keeps its unauthenticated, role or ownership denial and (in a
  multi-tenant app) cross-tenant denial cases.
- **Removal counterpart**: a change that adds a stronger test names the tests whose assertions
  it now makes and deletes them in the same change.
- **Break → delete or justify**: when an existing test fails because the change set out to
  alter that behavior (its PR or plan says so), delete the test or rewrite it to the new
  behavior; never patch it until it passes. A test that guards a contract or a past bug is the
  exception: the change keeps that behavior, or states why the contract changes. A break the
  change did not intend is a bug in the code: fix the code, not the test.
- **Deleted tests are listed**: the PR body names every test the change deletes, with its reason.
- **Review findings**: a finding becomes a test only when it reports a real bug. Missing coverage
  alone adds no test, unless it is a case this section requires: an endpoint's denial cases, or
  the reproducing test for the bug a change fixes.
