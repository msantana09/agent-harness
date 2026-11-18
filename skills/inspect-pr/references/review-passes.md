# Phase 4: Perform Independent Review Passes

Keep the passes distinct so success in one area does not create confidence in another.

## 4.1 Intent, Correctness, and Completeness

Check whether the implementation solves the stated problem fully and only once. Examine branching,
boundaries, empty and null states, error propagation, cleanup, async control flow, stale state,
partial success, retries, ordering, and deleted behavior. Look for required surfaces the diff never
touches.

## 4.2 Security, Tenancy, Data, and Contracts

Check authentication, authorization, tenant-context propagation, input validation, injection and
data exposure, secrets, audit implications, transaction boundaries, constraints, migration order,
backfill safety, rollback compatibility, and old-code/new-data coexistence. Trace contract changes
through every producer and consumer.

Treat security, tenant isolation, irreversible data loss, and broken compatibility as correctness
requirements, never optional hardening.

## 4.3 Integration and Operational Behavior

Check concurrency, idempotency, retries, timeouts, resource lifetime, caching, background work,
deployment order, configuration defaults, feature-flag states, observability, and recovery.
Consider performance only when the diff introduces a credible hot path or measurable regression.

For UI changes, include loading, empty, error, permission-denied, keyboard, responsive, and stale
request states when relevant. For infrastructure changes, include plan impact, permissions,
rollout, rollback, and environment drift.

## 4.4 Tests and CI

Determine whether the tests the PR adds or changes prove the behavior rather than merely execute
it: look for weak assertions, mocks that bypass the risk, and copied expectations. Use untested
positive, negative, boundary, authorization, tenant-isolation, failure, and regression paths to
choose what to trace by hand, and report the defect found there, not the missing test. A behavior
tested once, at any layer, is covered. A missing test is a finding only when:

- an endpoint or access path the PR adds or changes has no test, at any layer, of its
  unauthenticated, role or ownership denial, or (in a multi-tenant app) cross-tenant denial case;
- the PR fixes a bug and no test it adds fails on the base code.

Treat every edit to an existing test or check in the same diff as a code change as a claim that the
old expectation was wrong, and verify that claim. A green suite can be gamed: assertions deleted or
loosened, expected values rewritten to match the new output, a tolerance or timeout widened, a skip,
xfail, or `.only` added, the unit under test mocked, production code branching on test-only inputs,
or an exception swallowed so the failure path never reaches the assertion. Each one with no stated
reason is a finding at the severity of the behavior it now hides.

Two removals are not findings once checked. A test or assertion removed because a stronger test
the PR names (`file::test`) still makes that assertion: read the named test and confirm it does.
A test deleted or rewritten because the PR set out to change that behavior, listed in the PR body
with its reason. Either removal is a finding when the named test does not make the assertion, when
no reason is listed, or when the test guarded a contract or a past bug the PR does not say it
changes.

Inspect CI conclusions and failure logs. Distinguish a PR-caused failure from flaky or
infrastructure noise, and distinguish a passing suite from actual coverage of the risky behavior.
Use the repository's quality-gate skill for canonical local commands when additional execution is
justified. Never run tests against a checkout that does not match the recorded PR head.

## 4.5 Simplicity and Missed Opportunities

Ask whether the same outcome could use an existing abstraction, delete obsolete behavior, reduce
duplication, or avoid a new compatibility layer. Identify adjacent improvements only when the PR
uniquely enables them and the value is disproportionate to the scope.

Apply the repository's architectural decision framework when the PR adds
meaningful complexity, and the repository's stage constraints when it does not: the Company Stage
section of the root `CLAUDE.md` (or the nearest equivalent) is authoritative on what
infrastructure and generality a change may recommend. Never propose scale infrastructure, queues,
caches, or abstractions that section forbids.
