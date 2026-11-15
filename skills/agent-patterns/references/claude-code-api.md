# Claude Code orchestration facts

What Claude Code's own tool descriptions leave out: observed behavior of agents, Workflows and
hooks. Facts that name a build were observed on that build; the rest were checked on earlier
builds and not re-run.

---

## Spawning agents — `Agent`

`model` takes a bare tier token only (`fable`, `opus`, `sonnet`, `haiku`): no full model IDs, no
`inherit`. Agent-definition `model:` frontmatter follows the same rule (`fable`, not
`claude-fable-5`).

**Reading background results:** read the summarized `<result>` in the completion notification.
**Do not** read Claude Code's `output_file` (a `…/tasks/{id}.output` path) — it is the full
sub-agent JSONL transcript (~20× the summary) and overflows context.

**Nesting** *(verified empirically)*: a subagent **can** spawn its own sub-subagents via `Agent`,
including parallel fan-out (multiple `Agent` calls in one message) — **if it holds the `Agent`
tool**. `general-purpose` and `claude` carry `*` (all tools) and can spawn; so does any custom
agent whose `tools:` includes `Agent`.
A subagent does **not** hold the `Workflow` tool (it is main-loop-only), so fan-out *below* a
subagent uses `Agent`, not `Workflow`. (Verified one level deep: subagent → sub-subagent; deeper
recursion untested.) `Workflow` itself nests one level via `workflow()`.

## Effort tier defaults (per agent tier, either main loop)

Within-tier `effort:` pins:

- **fable agents** — default `high`, set explicitly (an omitted effort falls to a saved per-model
  level or the model's default, which is why it is set); pin `xhigh` only where a measured gain
  on the current model is worth its extra tokens.
- **opus agents** — default `high`; raise to `xhigh` for demanding coding/agentic work.
- **sonnet agents** — `high` baseline; supports `xhigh`; raise to `xhigh` only for the
  hardest execution tasks.
- **haiku agents** — Haiku 5.5 honours `effort:`; from a session on another model, an agent with no
  pin takes a saved per-model level or the model's default (`medium`).
  `medium` suits single-shot extraction (digests, labels); multi-step agentic or search-heavy
  work takes `high`, since `low` skips searches and checks there. `xhigh`/`max` cost far more for
  small gains and can end with no visible text.

> These pins were calibrated on a prior model generation; re-observe them through dogfooding
> rather than treating them as fixed. Effort names do not mean the same amount of thinking
> across models, so re-sweep per model rather than carrying a pin forward. On Fable 5.2, most
> of the gain arrives at `high`; `xhigh` adds a little for many more tokens, and at `xhigh` and
> `max` it starts subagents to review its own work. At `low` and `medium` it ends long tasks
> early more often, and effort barely changes how long its replies are.

---

## Orchestrating at scale — `Workflow` (dynamic workflows)

- **Primitives:** `agent(prompt, {schema, label, phase, model, effort, isolation, agentType})`,
  `parallel(thunks)` (barrier), `pipeline(items, ...stages)` (no barrier — default for multi-stage),
  `phase(title)`, `log(msg)`, `workflow(nameOrRef, args)` (one level of nesting), `budget`.
- **`effort`** (`low|medium|high|xhigh|max`) is passed explicitly on every stage — an omitted
  effort takes the session's level only on the session's model, else that model's saved level or
  default, so `rules/core-rules.md` § Orchestration & Execution Rules (item EXECUTE DIRECTLY)
  requires it. **`model`**: Claude Code's built-in `workflow-authoring` skill says "default to
  omitting it"; `rules/core-rules.md` says "assign a tier deliberately" — the rule wins, the
  skill text is Claude Code's default, not this policy.
- **`schema`** forces a validated structured return — no file/parse dance needed.
- **A stage's model resolves in this order** (observed on build 2.1.318, 147 stages):
  `opts.model`, then the `agentType` file's `model:` pin, then
  `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL` or `CLAUDE_CODE_SUBAGENT_MODEL`, then the main-loop model.
  Sonnet-pinned `agentType` stages resolved to Sonnet under an Opus main loop; stages with no
  `agentType` resolved to the env default (Opus). Earlier builds ignored the pin (an `architect`
  call, pinned `opus`, ran on Fable). Verify the first spawned agent's `*.meta.json` in the
  workflow transcript dir before assuming a pin held.
- **`resumeFromRunId` replays only the longest unchanged prefix** of `agent()` calls in journal
  order. Parallel lanes journal in finish order, not launch order, so after a partial failure a
  resume can re-run already-finished lanes live and regress a sibling's file. Do NOT resume a
  parallel fan-out after a partial failure: read `<transcriptDir>/journal.jsonl`, recover lane
  identity from each `agent-<id>.jsonl`'s first prompt, digest per-lane state (built / verified
  / failed / never started), and launch a NEW workflow whose args carry only the unfinished
  lanes with "state of the tree" briefs. Session-limit failures show as agent errors with 0
  tool calls — those lanes' files are untouched.
- **Launch from the turn whose user message requested the work.** Claude Code relays the latest
  user message to every subagent as the request that triggered the run, and discounts consent
  quoted in the prompt (computed task text counts as script output). Launched in the same turn as
  an unrelated request, 3 of 5 stages stopped with `## Clarifications Needed` instead of working.
  Launch from the requesting turn, or have the user restate the request first.
- **Limits:** concurrency capped at `min(16, cores−2)`; 1000-agent lifetime cap per run; ≤4096
  items per `parallel`/`pipeline` call. Resumable within a session.
- **Hook visibility:** a `Workflow` is a **single** main-session tool call. Its internal `agent()`
  calls run in a separate runtime and do **not** emit main-session `PostToolUse` events — so
  PostToolUse hooks that match `Agent` never see workflow-internal agents.

---

## Other orchestration primitives

| Need | Primitive |
|------|-----------|
| Recurring / self-paced task | Claude Code's loop command; `ScheduleWakeup` (dynamic pacing) |
| Scheduled (cron) runs | `CronCreate` / `CronList` / `CronDelete` |
| Wait on an external condition without polling | One event (a merge, a deploy): an `until` loop as a Bash call with `run_in_background=True`; its completion notification is the event. A stream of events: `Monitor`, with every `sleep` under 10s — Claude Code runs a Monitor command through the Bash hooks with no background flag, so a hook that blocks long sleeps blocks it too |
| Abandon a running background task | `TaskStop` (general primitive — **ungated**; loads via `ToolSearch` in a plain session, and is the cancel primitive named by the `Monitor` tool doc) |
| Work until a completion condition is met | Claude Code's goal command (a small/Haiku model evaluates the condition each turn) |
| Isolated workspace for parallel sessions/edits | `EnterWorktree` / `ExitWorktree`; `isolation:"worktree"` on `Agent` |
| Parallel lanes once the main session sits in a worktree | Every `Agent` spawn and `Workflow` agent is pinned to that worktree: writes and git (`-C`, `cd`) into any other worktree are refused, and named agent types have no `ToolSearch` to load `EnterWorktree`. Per-lane worktrees made outside the session are unreachable. Parallelize inside the one worktree: agents edit disjoint files and make no git writes; the main loop commits per issue afterwards (`git apply --cached` splits a shared file's hunks). Never let parallel agents commit: pre-commit stashes every unstaged edit while its hooks run. The pin is live: a parent `EnterWorktree` or a top-level `cd` mid-run re-pins agents already running, so a probe must prove a Bash `pwd` ran inside the target worktree before trusting it. |
| Coordinated long-running peers (shared task list, cross-agent messaging) | **Agent teams** — gated behind `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Shared task list via `TaskCreate`/`TaskList`/`TaskGet`/`TaskUpdate`; `SendMessage` between teammates. (`TaskStop` is general, not team-gated — see the row above.) Under this flag, a **named** subagent launches as a teammate: the spawn returns at once and the teammate's final answer arrives later in its idle notice (the `result` field), not as the `Agent` call's result, so a loop that waits on the call's result gets nothing. A long answer arrives cut off, ending in a `result truncated` marker, so a teammate whose report matters writes it to a file, or the lead asks for the rest via `SendMessage`. Teammates ignore the definition's `skills:` frontmatter. A team needs an interactive lead session: under `-p` a named subagent stays an ordinary subagent, and teammates cannot spawn teammates. |
| Code review from a worktree, subagent or workflow | Claude Code's built-in code-review skill forks from the **main session's cwd**, not the caller's worktree, and its result lands in the MAIN session as a background-task notification — a workflow agent never receives it and polls forever. A relative target (a branch name such as the integration branch, or none) reviews that branch against itself or the main tree's `HEAD~1`. Pass a PR number or `--path /abs/worktree`; never invoke it inside a workflow agent — review inline in the stage (schema carries the reviewed commit sha) and run the skill from the main loop after the workflow returns. "Self-reviewed manually instead" in an agent report = not reviewed. |
| Subagent tool denylist | `AskUserQuestion`, `Workflow`, `EnterPlanMode`, `ExitPlanMode` (unless `permissionMode: plan`), `ScheduleWakeup`, `EndConversation`, `WaitForMcpServers` — removed from a subagent's tool surface even when listed in its `tools:` frontmatter. |

---

## Hook payload shapes (for hooks that match agent calls)

`PreToolUse` / `PostToolUse` hook stdin (JSON):

```json
{ "tool_name": "Agent", "tool_input": { ... }, "tool_response": { ... }, "cwd": "/path" }
```

- For `Agent`, `tool_input` carries `subagent_type`, `description`, `prompt`, `model`, `isolation`.
- `tool_response` carries the agent's (summarized) output.
- A hook that gates on the spawn tool must check `tool_name == "Agent"` **and** be registered in
  `settings.json` under a `"matcher": "Agent"` block. Both must agree.

`SubagentStop` hook stdin (JSON):

- `last_assistant_message`: the subagent's final text, handed over directly (no file read, no
  flush race). Validate subagent output from this.
- `agent_transcript_path`: the subagent's own jsonl (`.../subagents/agent-<id>.jsonl`).
- `transcript_path`: the PARENT session's transcript, never the subagent's. Reading it judges
  the orchestrator's own turn.
- Also `agent_id`, `agent_type`, `effort`.
- Hook `additionalContext` goes to the subagent itself, never the parent, and makes it take
  another turn (the hooks docs: it "continues the conversation"). Observed on 2.1.304: a
  detector returning it on every `## Clarifications Needed` stop looped 9 Workflow agents in 2
  sessions (up to 15 extra turns each), and no parent transcript received it. A message meant for
  the caller cannot come from a SubagentStop hook.

Hook text cap, observed on 2.1.296: each hook's injected text is capped at 10,000 chars **per
hook**. Over it, the model sees a 2KB preview plus a saved-file path instead (a 15.3KB
SessionStart context collapsed that way). Several hooks' outputs merge into one attachment with
no merged cap (two 7k outputs arrived intact). Large injections get a hook of their own.

`Stop` hook stdin (JSON), observed on Claude Code 2.1.295:

- `last_assistant_message`: the turn's final reply text only (text written before a tool call
  is excluded), handed over directly. The transcript is written asynchronously and lacked the
  final reply at Stop time on ~1 in 4 turns, so a Stop hook that judges the reply reads this
  field, never `transcript_path`. `prose_question_blocker` does.
- Also `stop_hook_active`, `transcript_path`, `cwd`, `prompt_id`, `effort`, `background_tasks`.
