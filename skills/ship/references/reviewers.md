# Step 2 - effort, and reviewers other than Codex

**Effort**: every round runs on the `review` default, Sol, at `--effort high`. Never
`xhigh` or Astra here - Astra is reserved for blockers and open-ended consultations
(`Skill("codex")` § Routing), not PR review. `codex-jobs review` writes an `attestation.json`
bound to the exact head and diff; a repo whose notes gate the hand-off on it says how in its
Step 5 notes.

**Opus reviewer** (`--reviewer opus`; Codex quota or cost is the standing reason): launch
a `general-purpose` agent (model `opus`, effort `high`; the repo notes may name another
role) directly with the same prompt, prefixed with
`Load Skill("inspect-pr") first and follow it exactly, including its Multi-Round Mode.`
Nothing else in the loop changes: same rounds, same triage, same round blocks (titled
`### Opus review round <N>`), same post-round clean-tree and head check (the agent holds
Write and Bash). No `codex-jobs review` runs, so no `attestation.json` exists: where the repo
notes gate the hand-off on one, record the waiver they name.

**Codex out of credits** (`FAILURE: usage limit`, followed by `(resets <time>)` when Codex
reports one): run the same prompt on a Fable agent (a `fork`, or a role whose `model:` is
`fable`) at high effort, with the same `inspect-pr` prefix. Title the round block
`### Fable review round <N>`. No `codex-jobs review` runs, so no `attestation.json` exists:
where the repo notes gate the hand-off on one, record the waiver they name.
