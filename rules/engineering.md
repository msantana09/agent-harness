# Engineering Principles

## Code Quality Principles

### Code Search

- Use the LSP tool (pyright-lsp / typescript-lsp plugins) for "who calls this", "where is this
  defined", and type errors — grep counts of callers are floors: dependency injection,
  decorators and re-exports hide some.

### No Time Estimates

- Never include time estimations (days, weeks, hours, "day-ish") in any output — docs, plans,
  and chat alike. Size work in files, components, or tests, or not at all.

### Verify Before Implementing (No Speculative Code)

- Before adding a code path, function, or capability nobody asked for: grep the app source tree for callers to verify they exist (a requested capability's caller is the request)
- No callers = don't implement. Raise explicit error instead of handling gracefully.

**Simplicity check**: overcomplication shows up as needless indirection or an abstraction
with only one caller — collapse those before adding more.

### Verify External Facts

**Important for upgrades, migrations, and external dependencies** — training data has a cutoff date.

- **Before hand-rolling behavior**: check for a native platform/framework feature (MDN/spec search
  for browser behavior)
- **Specs/standards, version dates, release history**: Use `WebSearch`
- Before stating "X is/isn't supported" or "Y version does/doesn't exist": verify first.
- A fix relayed from a search summary or forum post (a flag, setting or command) is unverified until
  checked against the user's installed version (the binary, `--help`, the settings path).

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
- Unrelated dead code you notice is a `priority:low` finding: file it per automation.md
  § Discovery Protocol and leave the code in place
- A pre-existing bug or hazard found while working: route it per
  automation.md § Discovery Protocol; never bury the fix in the diff unflagged
- Tests are sized like the neighboring test files — roughly one focused test per stated
  behavior. Scratch scripts and quick checks used to verify are not turned into permanent
  test files. This is about extras only: every behavior the task asks for is implemented
  completely

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

### Complete Changes

- Code changes must include corresponding updates to:
  - **Tests**: Add or update tests covering the changed functionality
  - **Documentation**: Update docstrings, READMEs, and inline comments
