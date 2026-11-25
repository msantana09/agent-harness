---
name: codex
description: Delegate work to OpenAI Codex through codex-jobs (CLI and MCP) on any tier (Luna, Terra, Sol, Astra) and effort. Use for an unbiased second opinion on a plan, architecture or bug, ideation or a blocker, a cheap scratch task, or a Workflow stage that runs on Codex instead of a Claude engineer.
allowed-tools: Bash, Read, Glob, Grep, Write
argument-hint: "[question] [--effort=low|medium|high|xhigh] [--model=luna|terra|sol|astra]"
---

# Codex

One job service runs Codex work: `~/.claude/scripts/codex/codex-jobs` (CLI) and the MCP tools
`codex_*` that `codex-jobs mcp` serves. A job gets an id at once, keeps running after the Claude
session ends, and any later session reads its result by id. Tier → model slug lives only in
`models.sh` beside `codex-jobs`; name tiers, never slugs. `codex-jobs capabilities` prints the
enabled kinds, the tier → slug table, the efforts each slug accepts and the `breadth` features
(native review, images, steer, interrupt, resume, fork, delegation), each with the tests that
cover it; a pair the slug does not list is refused before any process starts, with the valid list
in the message.

## What Is Enabled

| Kind | State | Use |
|------|-------|-----|
| `consult` (plus `resume`, `fork`, `steer`, `interrupt`, `cancel`) | works | Read-only questions, plan gap-analysis, ideation, debugging, scratch analysis |
| `review` | works | PR, branch or plan review that leaves `review.md`, `findings.json`, `findings.md` and `attestation.json`: `references/jobs.md` § Review Jobs |
| `run` (writing task in a worktree) | works | A bounded code change Codex makes in its own worktree: `references/jobs.md` § Writing Jobs |

A consult runs read-only with network on (`gh pr view` and `git fetch` work), declines every
approval request, and writes nothing to the repository.

## Routing

| Kind of work | Tier | Effort |
|--------------|------|--------|
| Reviews: PR or branch diff, plan gap-analysis, security audit, critical feature or bug | Sol | `high` |
| Complex debugging | Sol | `high` |
| Ideation and open-ended questions: breadth from a second model family | Astra | `low` / `medium` |
| Blocker: a Sol `high` run left no root cause and no lead | Astra | `low` / `medium` |
| Cheap sanity check, scratch task | Luna | `low` / `medium` |
| Middle tier | Terra | unmeasured until a real Terra run is logged |

- **Sol** (default) handles every review kind at `high`. `xhigh` is the escalation for plan
  gap-analysis when a `high` round missed gaps a later round caught, and the regression-bisect
  step.
- **Astra** costs $8/$40 per 1M tokens in/out, 4x Sol's $2/$10 (a request over 272K input tokens
  reprices at 2x input, 1.5x output). Run it at `low` or `medium` only. Subject matter (security,
  migrations, billing) never routes to Astra by itself.
- **`max` is not used.** A blocker that survives Sol at `high` goes to Astra at `low`/`medium`.
- **`ultra`** lets Codex split a turn across its own subagents (Sol, Terra, Astra; Luna has no
  `ultra`). They inherit the job's sandbox and run inside its app-server, so this Mac's cap counts
  the job once. Use it for a task that splits into independent parts.
- Change model tier or effort, one at a time. Before raising effort, audit the prompt for missing
  success criteria or verification loops: a prompt gap masquerades as an effort gap.
- **Bisect on regression**: when consultation quality drops, re-run one effort step higher on Sol;
  if still off, restore the previous prompt template from git history. Switching to Astra is gated
  by the table above.

When Codex adds value: [references/when-to-use.md](references/when-to-use.md).

## Review Routing

Pick the framework by what is being reviewed, before writing any prompt:

| Subject | Framework |
|---------|-----------|
| A PR or branch diff (correctness, regression, test adequacy, merge-readiness) | **`inspect-pr`**: put `Use $inspect-pr to inspect PR #<N>` in the GOAL; Codex loads the skill from `~/.agents/skills/inspect-pr` (link it once: `mkdir -p ~/.agents/skills && ln -s <clone>/skills/inspect-pr ~/.agents/skills/`) and returns its response contract (`## Findings` … `## Verdict`). `/wrapup` is the canonical caller. |
| A plan, design or architecture question; debugging; a security consultation | The prompt template in § Write the Prompt File. |

Do not hand-write a diff-review checklist in a prompt: `inspect-pr` carries snapshot discipline,
blast-radius search, per-finding counter-checks, severity calibration and a per-file ledger.

**A PR whose subject is a detector** (guardrail test, lint rule, scanner, validator): Codex has an
unbounded supply of synthetic bypasses and surfaces one per round, which never converges. From
round 1 add to the prompt: enumerate every remaining gap you can reproduce in ONE list, ranked by
whether the pattern exists in this codebase (cite the file) or is conceivable only;
conceivable-only is Low. Then decide the detector's scope once (record `Decision:` /
`Instead of:` on the PR and a Known-limits block in the file) and declare the rejected patterns
settled in later prompts. `/wrapup` runs two rounds by default and a third only on a Blocker/High,
so there is no later round to count on.

## Run a Job

### 1. Identify Context

Glob for 1-5 relevant files and do not read them: Codex will. `ls` every path before it goes into
ANCHOR FILES. For a PR or branch review build the list from `git diff --name-only <base>...HEAD`
plus `ls`, never from memory.

### 2. Write the Prompt File

```bash
PROMPT_FILE="<scratchpad>/codex-prompt-$(date +%s).txt"
cat > "$PROMPT_FILE" << 'PROMPT'
ROLE: External reviewer giving a second opinion on this repository. Do not create,
modify, or delete files, and do not change git state. Your final message is the deliverable.

GOAL: {user_question}

SESSION CONTEXT: {brief description of current workstream/focus area}

ANCHOR FILES:
{list_file_paths}

SUCCESS CRITERIA:
- Every finding is verified against the actual code before it is reported: confirm the
  file:line and behavior exist as claimed (function name where the finding concerns a
  function; file/section for documentation, configuration, or architecture findings).
- Unverifiable claims are dropped or explicitly downgraded to hypotheses.
- Recommendations name the specific files and what to change, with rationale.

CONSTRAINTS:
- Start from the ANCHOR FILES. Expand scope only when a specific finding requires it.
- Do not re-read files that have not changed during your session.

TOOLS: Read files and run read-only commands (`git`, `gh`, greps, test runners) to
verify claims. No file writes or state-changing commands.

STOP RULES: An anchor path that does not resolve is noted in your answer and the review
continues. Stop only if the question cannot be answered from this repository (or, for a PR, the
diff is empty or the PR is unreachable) and say exactly what blocked you instead of guessing.
PROMPT
```

The template states outcomes and constraints, not steps (OpenAI's GPT-5.6/GPT-6 prompt framework:
removing redundant instructions measurably improved evals). The consult answer keeps whatever
headings the prompt asks for.

### 3. Submit

```bash
~/.claude/scripts/codex/codex-jobs consult "$PROMPT_FILE" --model sol --effort high   # blocking
~/.claude/scripts/codex/codex-jobs consult "$PROMPT_FILE" --model astra --effort low --detach
```

| Flag | Meaning |
|------|---------|
| `--model` | `luna`, `terra`, `sol` (default), `astra`, or a raw slug present in `~/.codex/models_cache.json` |
| `--effort` | default `high`; the efforts the slug lists |
| `--project-dir` | default: git root, else cwd; a directory outside a git repo works |
| `--output-dir` | a strict descendant of `<project>/.artifacts`; default `.artifacts/codex/<session>/analysis-<timestamp>-<random>/` with a `*` `.gitignore`; a reused directory loses the previous job's `result.json`, `answer.md`, review files and `attestation.json` before this job's are copied in |
| `--timeout` | seconds, default 1800; pass 3600 for a diff of 300+ files (`git diff --name-only <base>...HEAD \| wc -l`) |
| `--image FILE` | repeatable; a screenshot or diagram the prompt refers to |
| `--idempotency-key K` | a repeat submission with the same key returns the first job's id and starts nothing |
| `--detach` / `--wait` | `--wait` (default) follows the job and prints one line; `--detach` prints `JOB: <id>` and returns |

Output lines: `SUCCESS: <output-dir>` (exit 0), `FAILURE: <reason>` (exit 1), `JOB: <id>`
(`--detach` or a reattached key), `STATE: <id> <state>`, `RUNNING: <id> <state>` (`wait` timed
out, exit 75). Usage errors print `FAILURE:` with exit 2. Logs go to stderr.

### 4. Wait for the Result

- **Main loop**: run the blocking form with `run_in_background=True`. The Bash exit is the
  completion notice and the last stdout line is `SUCCESS:` or `FAILURE:`. N jobs are N background
  launches in one message.
- **A job that outlives the step**: `--detach`, keep the id, and later run
  `codex-jobs wait <id> --max-seconds 540`. Repeat while it prints `RUNNING:` (exit 75). A second
  session waits on the same id.
- **Workflow stage or subagent**: `references/workflow-and-mcp.md` § Workflow Stages.

On `SUCCESS:`, read `<output-dir>/answer.md` (the answer plus an `## Open questions` section when
the job returned any) and `result.json`. `codex-jobs result <id>` prints `result.json`:
`output_dir` (where the deliverable is), `ran`, `task_succeeded`, `model_used`, `effort_used`,
`usage` (token counts, the account's limits read before and after the turn, and
`rate_limit_reached_type` when the job failed on a usage limit), `recovery`.
`task_succeeded: false` (the model answered `blocked` or `failed`, or a review's repository changed
while it ran) prints `FAILURE:` in the blocking form; the answer file (a review's `review.md`)
still exists.

| Outcome | Action |
|---------|--------|
| `SUCCESS:` | Read `answer.md` (a review: `review.md`, `references/jobs.md` § Review Jobs), verify claims (`references/checking-output.md` § Claim Verification), present with § Output Format |
| `FAILURE: timeout after Ns (job <id>)` | No `answer.md`, but the run may have found things: `$XDG_DATA_HOME/agent-harness/codex-jobs/<id>/events.jsonl` (default `~/.local/share/agent-harness/codex-jobs`) journals every notification, and at the end each code-mode script Codex ran with its output (`code_mode_calls`; a command the sandbox refused is only there). Recover what it reproduced before relaunching |
| `FAILURE: head moved during review`, `tracked files changed during review` or `the repository could not be read at completion` | The review read a state the branch no longer has; its `attestation.json` `exit_status` says which. Relaunch once the branch is settled: `references/jobs.md` § Review Jobs |
| `FAILURE:` otherwise | `codex-jobs status <id>` prints `job.json`; its `error` field names the cause |
| state `outcome_unknown` | The turn's outcome could not be derived from the thread or rollout. Report it as such; never resubmit as if it failed |
| `STATE: … interrupted` | Another client interrupted the turn |

`codex-jobs status` (no id) lists this Mac's jobs; `status <id>` reconciles a job whose runner died
and shows `recovery`. `codex-jobs reconcile` reconciles all of them. Reconcile never sends a second
`turn/start`.

Step 5, review jobs, writing jobs, usage rules: [references/jobs.md](references/jobs.md).

Workflow stages and the MCP tools: [references/workflow-and-mcp.md](references/workflow-and-mcp.md).

Validation and re-runs: [references/checking-output.md](references/checking-output.md).

## No-Input Handling

`/codex` without arguments: after recent documentation work, ask Codex to review the docs for
completeness; after recent code changes, ask it to analyze the changes for correctness.

Claim verification: [references/checking-output.md](references/checking-output.md).

---

## Output Format

```markdown
## Codex Analysis: {topic}

**Output Directory**: `{OUTPUT_DIR}`

### Summary

{1-2 sentence overview}

### Key Findings

#### Finding 1: {title}

- **Current State**: {what exists}
- **Recommendation**: {what Codex suggests}
- **Rationale**: {why}
- **Files to Modify**: {specific paths}

### Validation Against Project Patterns

**Conflicts**: {any conflicts with project patterns, or "None identified"}
**Alignment**: {where recommendations fit well}

### Claim Verification Results

**Confirmed Findings**:

- {finding}: {verification details}

**Corrected Findings**:

- {finding}: {what was wrong, correct information}

**Refuted Findings**:

- {finding}: {why it was incorrect}

**Unable to Verify**:

- {finding}: {what would be needed to verify}

### Re-invocations (if any)

- Attempt 1: {error found, e.g., "Referenced MySQL"}
- Attempt 2: {correction made, result}

### Next Steps

{What should happen next - implementation, further analysis, user decision needed}
```
