# ADR-0149: Land Before the Cut — a Sub-Agent Owes a Report on Every Path, and the Caller Cannot Hide a Failed Landing

**Status:** Proposed
**Date:** 2026-09-10
**Deciders:** Owner (design direction and the no-raise directive, 2026-09-10), adr seat at Fable 5.1 (author, owner-directed model)
**Tags:** orchestrator, sub-agent, tool-loop, budgets, prompt-cache, grounding, expansion

---

## Context

### What FRE-1483 asked, and what was already known

The owner asked the same research question three times on 2026-09-10 and got no usable answer.
The workers did real research on every run. None of it survived. FRE-1482 records the evidence.
FRE-1483 commissioned this review before the fix, because the fix as first written was a copy of
the primary's loop, and nobody had reviewed whether that loop deserved copying.

Three competing explanations were falsified by the controlled runs on FRE-1482:

| Explanation | How it was falsified |
|---|---|
| Thin search results | Exa live at ~1,900 characters per result. Identical outcome. |
| The wrong year | Year stated explicitly in the query. Identical outcome. |
| Model capability | OVH failed worse, on the per-call timeout instead of the round cap. |

What survived: the worker is never told a limit exists, and nothing makes it write when the limit
arrives. It paces for an open-ended search and is cut mid-stride by whichever limit fires first.

### The record, verified live

The sub-agent capture index for session `606ae6a4` holds the shape this ADR treats. Read on
2026-09-10 20:14 UTC:

| Worker | Rounds | Characters absorbed | Characters reported | Terminal path |
|---|---|---|---|---|
| 1 | 5 | 154,755 | 468 | iteration cap |
| 2 | 5 | 156,749 | 57 | iteration cap |
| 3 | 5 | 168,550 | 86 | iteration cap |
| 4 (no tools) | 0 | 0 | 7,203 | outer deadline at 75.5 s |

Worker 1's 468 characters are five stage directions ("Now let me check the specific week's
remaining events"). Its absorbed results held the answer the owner needed. Worker 4 is the
instructive one: it was cut mid-generation and still returned 7,203 characters, because the cut call
was a *writing* call and the streaming partial survived. The recovery path works when the call that
dies is the call that writes.

### The asymmetry between the two loops

The primary and the worker run the same model on different scaffolding.

| Mechanism | Primary (`orchestrator/executor.py`) | Worker (`orchestrator/sub_agent.py`) |
|---|---|---|
| Tool rounds | 25 global, 6/8 per TaskType (`settings.py:242,284`) | 5 (`settings.py:252`) |
| Budget told at start | `_TOOL_RULES` says "≤ 6 tool calls" regardless of the real cap | Nothing |
| Countdown | User message on every pass once `count >= max-2` (`:5885`) | Nothing |
| Forced synthesis | At `max+1`: the model's tool calls are dropped, then a tools-off call (`:6646`, `:5864`) | Nothing. `_ToolIterationLimitReached` docstring: "no injected 'please wrap up' round, just a stop" |
| Landing reserve | None | None |
| Current date | `render_current_datetime_block` in the volatile tail (`:6038`) | Nothing |
| Human at the limit | ADR-0076 pause: `continue_10` / `finish_now` | None (bounded worker, FRE-1389) |
| Recovery at exit | n/a | Cap path: `round_texts` (FRE-1399). Timeout, cancel and error: the in-flight fragment only (`_killed_result`) |
| Context-window control | Compression pause at `context_window_max_tokens` | None. Growth is bounded only by rounds × result size |

### What the primary's loop gets wrong, on its own merits (D1 of FRE-1483)

The review found four defects in the template before any copying:

1. **The countdown lies about its unit.** It says "N tool call(s) remaining". The counter counts
   *rounds* (`ctx.tool_iteration_count += 1` per `step_tool_execution`), and the system prompt
   tells the model to batch several calls per round. A model reading "2 calls remaining" plans
   differently from one reading "2 rounds remaining".
2. **The zero case invites a wasted call.** At `count == max` the message reads "0 tool call(s)
   remaining. Prioritize synthesis — only make additional tool calls if they are strictly
   necessary". The model that obeys emits tool calls at `max+1`, which are dropped, and only then
   does the tools-off call follow. One full generation is spent producing output that is thrown away.
   On a five-round budget that is a sixth of the loop. On OVH, with 57,000 to 75,000 characters of
   accumulated context, that discarded call is the one that dies at 90 s.
3. **The forced-synthesis call misses the prompt cache on every local call.**
   `_forced_synthesis_tool_overrides` (`executor.py:3126`) drops `tools=` on every non-Anthropic
   provider. Qwen chat templates render the tools array inside the system region. A probe on
   2026-09-10 (§D6) shows that dropping the tools re-prefills the entire prefix, while keeping the
   tools and pinning `tool_choice="none"` hits the cache and still prevents tool calls. The function's
   own docstring names the second form as the better one and applies it only where Anthropic forces it.
4. **No landing is reserved.** `_turn_deadline_remaining` bounds an in-flight call to whatever
   remains. Nothing stops the loop from starting a tool round it cannot finish with time left to
   write. The worker has the same gap, and on the worker it is fatal: with the turn budget shrinking
   per serialized worker (FRE-1397), the third worker of a fan-out is cut inside a tool round.

Two further observations are recorded and not acted on here: the cap is denominated in rounds while
the quantity that actually binds is context growth, and `settings.py:247` documents "max-3" for a
warning the code fires at max-2.

### The owner's constraint

> "We dont raise limits until we have properly implemented the behavior when the limits are
> reached." — owner, 2026-09-10, relayed on FRE-1482.

Clarified in this ADR's discussion: limits *can* be raised when the data says so. The sub-agents
have never worked, so no data says so yet. This ADR changes no limit. It makes every worker report
what its limit cost, so the raise decision has an input.

### One prior decision this ADR reverses

FRE-1389 (owner-directed, 2026-09-04) decided the worker gets "no injected 'please wrap up' round,
just a stop", to keep it a pure bounded function. That bound was placed on the rounds and not on the
landing. A pure function that returns 57 characters against 156,749 is not measurable either. This
ADR keeps the worker bounded and un-negotiating, and adds the one thing FRE-1389 left out: the
function must return its result.

---

## Decision

### D1 — What is copied from the primary, what is improved, what is only reported

| Mechanism | Disposition |
|---|---|
| Tools-off enforcement | **Copied**, in the cache-preserving form (D6). The text is advice. The absence of tools is the enforcement. |
| Date block | **Copied verbatim** (`render_current_datetime_block(turn_started_at)`), placed in the task user message, never in the static system prompt. |
| User-message injection at the tail | **Copied.** Every injected message appends. Nothing above it changes (ADR-0081 §D2). |
| Countdown on the last three rounds | **Improved:** every round. Five rounds is too short for a "last three" heuristic. |
| Forced synthesis at `max+1` | **Improved:** at `max`. The call after the last executed round is the tools-off call. No generation is discarded. |
| Landing reserve | **Added** to the worker (D3, move 4). **Reported** for the primary. |
| Countdown unit and zero case | **Fixed on the primary** (T3). |
| Forced-synthesis cache miss | **Fixed on the primary** (T3, D6). |
| Round-denominated cap | **Kept** as the enforced limit. The countdown reports characters so the model can pace on what binds. A character-denominated budget is a later decision, taken from the captures this ADR adds. |
| ADR-0076 pause at the limit | **Not copied.** The worker's limit is a contract, not a conversation (FRE-1389). |

### D2 — The tool surface is unchanged; the planner is told the budget

The grant set stays as `config/governance/tools.yaml` declares it: `run_python`, `web_search`,
`search_memory`, `recall_personal_history` granted with recorded reasons, `fetch_url` refused
while FRE-1360 is open. The three failing turns falsified the tool set as cause. Each entry carries
its own reason, and this ADR does not re-open them.

The grant already varies per task: the planner requests tools per task and governance filters the
request (`_compute_sub_agent_grants`). That structure is right. The ceiling is a governance decision.
The choice is a planning decision.

What does not vary per task is **scope**. The planner wrote "research events in Mallorca for that
week" with no knowledge that the worker had five rounds. `_build_planner_system_prompt`
(`expansion_controller.py:88`) gains one rule, rendered live from settings the way the tool surface
already is:

```
- Each sub-agent has at most {N} tool round(s) before it must write its report (a round may
  hold several parallel tool calls). Scope every task so a worker can answer it inside that
  budget. Prefer one precise task over one broad one.
```

`{N}` is `settings.sub_agent_max_tool_iterations`. A hard-coded number here is the drift FRE-1389
AC-1 already ruled out for the tool surface.

### D3 — The worker contract: five moves and four terminal paths

**Move 1 — the budget is stated at round 1.** Appended to `_SUB_AGENT_SYSTEM_PROMPT`, rendered
once from the setting so the bytes are identical for every worker and every turn:

```
You have a budget of {N} tool round(s). A round is one reply that calls tools; it may call
several tools in parallel. After each round you will be told how much budget remains. When
the budget is spent, your next reply will have no tools available, and you must write your
report from the results you already hold.
```

**Move 2 — the date is in the task message.** `SubAgentSpec` gains `turn_started_at: datetime |
None`. The value travels one path: the executor passes `ctx.turn_started_at` as a new keyword
argument of `ExpansionController.execute` (call site `executor.py:5036-5054`), `execute` hands it to
`_run_dispatch`, and `_run_dispatch` sets it on every spec it builds — the per-task specs at
`expansion_controller.py:661-683` and the replacement spec in `_maybe_redispatch_on_gap`. Today
`_run_dispatch` receives no context and no timestamp, so this signature change is part of the
obligation, not an implementation detail. `run_sub_agent` renders the task message as:

```
{render_current_datetime_block(turn_started_at)}

Task: {spec.task}
Output format: {spec.output_format}
Respond with the result only.
```

When `turn_started_at` is `None` (a caller outside a turn), the block is omitted and a WARNING
`sub_agent_no_turn_timestamp` is logged.

**Move 3 — a countdown after every round.** `_run_tool_loop` appends one user message after the
round's tool results, before the next inference call:

```
Tool budget: {remaining} of {N} round(s) remaining. Absorbed so far: {chars:,} characters of
tool output. Time remaining: about {seconds} s.
```

`remaining = N - state.tool_iterations`. `chars = state.tool_result_chars_absorbed`. `seconds =
int(deadline_monotonic - time.monotonic())`. It is appended at the tail only. Nothing earlier in
the message list changes.

**Move 4 — the worker lands before it is cut.** Before starting a tool round, the loop checks
whether time remains for that round *and* for a report:

```
remaining_s < mean_round_s + effective_timeout  →  do not start the round; synthesize now
```

`mean_round_s` is the mean wall-clock of the rounds completed so far in this worker. Before the first
round, with nothing measured, it is `effective_timeout` (the conservative estimate). The check uses the
worker's own `deadline_monotonic`, which already reflects the turn's remaining budget
(`max_deadline_seconds`, FRE-1397). No limit changes. The rounds are spent so that the last thing the
worker does is write.

**Move 5 — forced synthesis, at the cap or at the reserve.** When `state.tool_iterations == N`, or
when move 4 fires, the loop appends one user message and makes one call in the synthesis form the
resolved dialect declares (D6): tools retained with `tool_choice="none"` where
`synthesis_retains_tools()` is `True`, tools dropped where it is `False`. Every dialect measured so
far declares `True`.

```
Your tool budget is spent. Do NOT call any more tools. Using only the tool results already in
this conversation, write your report now. State every fact you found that answers the task,
each with the source it came from. Then list what you searched for and did not find. Keep the
report under 400 words. Output format: {spec.output_format}.
```

For the reserve case the first sentence reads "Your time budget is nearly spent." The call's
`timeout_s` is `min(effective_timeout, remaining_s)`. If this call itself is cut, its streamed partial
content is the report, and the ledger (below) follows it.

Today the sixth inference call emits tool calls that are discarded. After this ADR the sixth call is
the report. The number of inference calls per capped worker is unchanged.

**The terminal paths.** `SubAgentResult` and `SubAgentCapture` gain two fields. `stop_reason` is
one of `completed | cap | time_reserve | timeout | deadline | cancelled | error`. `report_kind` is
one of `synthesized | narration | ledger`. Every path that returns a result declares both.
Cancellation does not return a result — see its row. `success` is `True` only for `stop_reason ==
"completed"` with a non-empty report. Every other `stop_reason` carries `success == False`, even
when its report is `synthesized`: a worker that stopped at its cap did not finish its task, and
FRE-1389 AC-2's "explicit, distinct terminal state" holds for the new paths as it does for the cap
today (`sub_agent.py:1005-1027`).

| Path | Trigger | Contract |
|---|---|---|
| Completed | The model replies with no tool calls (`sub_agent.py:696`) | `stop_reason = completed`. The content is the report, `report_kind = synthesized`, **only if** it is non-empty after `strip()` and after the `TOOL_GAP:` line is removed. Empty content is not a report: the result becomes `report_kind = ledger` with `success = False`, and the ledger's `{why}` reads "the model returned no text". |
| Iteration cap | `state.tool_iterations == N` and the model returns tool calls, or the round after the last executed round | Forced synthesis (move 5). `report_kind = synthesized`. If the synthesis call fails or returns empty text, `ledger`. |
| Time reserve | Move 4 fires | Same as the cap, `stop_reason = time_reserve`. |
| Per-call timeout | `LLMTimeout` from `respond()` on a tool round | One forced-synthesis attempt with `timeout_s = min(effective_timeout, remaining_s)` whenever `remaining_s > 0`. Otherwise `ledger`. |
| Outer deadline | `asyncio.TimeoutError` from the `wait_for` in `run_sub_agent` | `ledger`. No time exists by definition. |
| Cancellation | `CancelledError` from the dispatcher | The ledger is written to the **capture** (`_emit_sub_agent_capture`, as `_killed_result` is today) and the cancellation is re-raised. No result reaches `_run_dispatch`, which catches only `Exception` (`expansion_controller.py:742`). A global cancel ends the turn, so no caller can use a result. The obligation on this path is the audit record, not the digest. |
| Upstream error | Any other exception from `respond()` or a tool | `ledger`. The retry policy lives in the client (ADR-0144), not in the worker. |

The rule: the worker recovers from its own limits. It does not retry the world.

**What "report" means, and what this ADR does not detect.** The validity predicate is
deterministic and weak on purpose: non-empty text. Worker 1's 468 characters of stage directions
would pass it. A narration detector is free-text parsing of model output, which FRE-1389 AC-5 and
FRE-1399 both refused, and this ADR refuses it too. What removes narration is not a detector but the
tools-off call: a model with no tools and an explicit instruction to report writes findings, and the
seeded fixture in AC-1 proves that on the mechanism. The live probe in AC-1 proves it on the owner's
own failing query.

**The ledger.** Deterministic, no inference, built from `_ToolLoopState`. It replaces the in-flight
fragment as the terminal content of every path that cannot synthesize:

```
[Worker stopped: {stop_reason} after {rounds} tool round(s); absorbed {chars:,} characters of
tool output; no model-written report was possible because {why}.]
Tool calls made:
  1. web_search(query="...", categories="...") → 15,701 chars
  2. ...
Model notes per round:
  - round 1: "I'll research events in Mallorca for that week."
Partial text from the interrupted call:
  "..."
```

The ledger carries the tool calls with their arguments and result sizes, never the results
themselves. The queries are actionable: the primary can re-run one. The raw results are what context
isolation exists to keep out (FRE-1389 AC-4). `_ToolLoopState` gains a per-round record of
`(tool_name, arguments, result_chars, wall_s)` to make this possible. `narrative_synthesized` is
retained and becomes `report_kind == "ledger"` so FRE-1399's warning keeps firing.

**What is not in the contract.** No pause on the worker. No re-dispatch with more rounds. No
second summarizer call over the raw results (FRE-1387 ruled that out for the digest, and the model's
own reasoning state is the thing worth keeping). The synthesized report is the model's, written
tools-off, from the context it built.

### D4 — The caller cannot hide a failed landing

The primary is already told "Synthesize from available results and note any gaps"
(`expansion_controller.py:1013`). It ignored the note and produced a confident itinerary with three
verified factual errors. An instruction is not enforcement. Two mechanisms are, and neither is an
instruction.

**Mechanism 1 — a constraint pause before synthesis.** When the fan-out returns and any result has
`success == False` or `report_kind == "ledger"`, or any task was skipped (FRE-1397), the executor
opens an ADR-0076 pause of a new kind, `sub_agent_fanout_incomplete`, before the synthesis LLM call
at `executor.py:5107`. The predicate reads `success` and `report_kind`, not `stop_reason` alone: a
worker that completed with empty text carries `stop_reason == "completed"` and is still a failed
landing.

| `action_id` | Label | Effect |
|---|---|---|
| `answer_from_partial` | Answer from what was gathered | The synthesis call runs. The final answer carries the trailer below. |
| `stop_and_show` | Stop and show me the worker reports | No model call. The final response is composed deterministically: each task's name, `stop_reason`, `report_kind`, rounds, characters absorbed, and its report in full. Safe default, last in the list (ADR-0144's convention). |

No option grants more rounds. That is a raise, and it is not this ADR's to make. The pause honours
a stored preference (`allow_preference=True`), so the owner can silence it once it is noise. The
card's `context` names the incomplete tasks and their stop reasons, so the owner sees the gap before
anything is composed over it.

**Time accounting of the pause, stated so it is not mistaken for a raise.** A genuine pause is
credited to the turn's work deadline (ADR-0142 D4a, `executor.py:202-213`) and bounded by the
lifetime cap (`orchestrator_turn_lifetime_seconds`). That is how every ADR-0076 pause already
works: time spent waiting for a human is not work time. The 900 s work budget is unchanged. The
lifetime cap still ends the turn.

**Callers with no one to ask.** `_maybe_pause_for_constraint` waits the full
`constraint_pause_timeout_seconds` (180 s) for a headless caller before applying the default
(`executor.py:700-704`). An eval run with three incomplete workers would wait nine minutes for
answers nobody will give. The call site therefore applies this rule before opening a pause: when
`ctx.eval_mode` is true, read the stored preference for the eval identity; if it is one of this
pause's actionable options (`answer_from_partial` or `stop_and_show`), apply it; otherwise — no
preference, or the reserved `always_pause` — apply the safe default (`stop_and_show`) immediately.
In `eval_mode` no pause event is emitted on any branch, and `always_pause` cannot open one, because
`_maybe_pause_for_constraint` would otherwise register and wait on it (`executor.py:760-835`). An
eval that wants synthesis over partial results stores `answer_from_partial` as its preference — the
platform's existing mechanism, not a new flag. An interactive session with a momentarily absent socket keeps
FRE-928's behaviour: the pause is registered and a reconnecting client is replayed the card.

**Mechanism 2 — a deterministic trailer.** When `answer_from_partial` is chosen, the executor
appends to the final answer, after generation and outside the model's control:

```
— Research note: {k} of {n} sub-tasks did not complete ({task_name}: {stop_reason}, ...).
Claims on those topics rest on partial results and are not verified by this turn's research.
```

The trailer is stored in history as part of the assistant message, so the persisted history equals
the wire form (ADR-0081) and the next turn replays it as a forward extension.

**The synthesis context tells the truth.** `_build_synthesis_context` replaces the closing sentence
"The sub-tasks above have been completed" — which today follows the failure note even when every
worker failed — with counts, and each worker's header carries its terminal facts:

```
### {task} [{status}: stop={stop_reason}, report={report_kind}, {rounds} round(s),
{chars:,} chars absorbed]
```

```
{n_ok} of {n} sub-tasks completed. {n_partial} stopped at their budget and wrote a partial
report. {n_ledger} stopped without a report; their ledger lists what they searched.
Synthesize from these results only. Where a sub-task did not complete, say so in your answer
rather than filling the gap from memory.
```

That last sentence is an instruction and is not the enforcement. It is kept because it is true.

**What D4 does not do, stated so nobody claims it.** Neither mechanism stops confabulation *inside*
the synthesized text. The citation contract cannot bind on worker findings today: a worker's report
is model-composed, and ADR-0138 D2 makes model-composed content inadmissible by design, so every
claim the primary makes from a worker is uncited by construction. The correct fix is a shared source
registry — the worker registers its own admissible `web_search` results into the turn's registry and
cites them in its report, and the primary inherits the identifiers. That re-opens an ADR-0138
decision and is a follow-on ADR (T4), not a clause here.

### D5 — No limit changes; the captures become the input to the raise decision

`sub_agent_max_tool_iterations` (5), the `sub_agent` role's `default_timeout` (90), and the turn
budget (900) are unchanged. `SubAgentCapture` gains `stop_reason`, `report_kind`, and the per-round
record (`rounds: [{tool, args_chars, result_chars, wall_s}]`) as additive fields. A proposal to
raise any of the three must cite those fields over real turns. A proposal without them fails this
ADR's own bar.

The arithmetic, reported so the owner holds it before the data arrives. Local, Exa era: ~52 s per
round × 5 rounds + a synthesis call of ~70 s ≈ 330 s per worker. Three serialized workers ≈ 990 s
against a 900 s turn budget. Under move 4 the third worker lands with fewer rounds instead of dying
in one. Whether that is acceptable, or the runway needs lengthening, is the decision the captures
inform.

### D6 — Every synthesis call keeps its tools and pins `tool_choice="none"`

Measured on 2026-09-10 against the live local backend (`unsloth/qwen3.8-flash-next` on
llama-server, through the Caddy egress), same 4,191-token prefix, `max_tokens=4`:

| Call | Prefilled | Cached | Prefill |
|---|---|---|---|
| tools + `auto`, warm | 4 | 4,187 | 0.1 s |
| **tools + `none`** | **4** | **4,187** | **0.1 s** — no tool call emitted |
| **tools dropped** | **3,915** | **0** | **9.8 s** |

Dropping the array changes the rendered prompt by 276 tokens and misses the whole prefix. Prefill
runs at ~350 tokens/s on this box, so a worker holding 40k tokens of results pays ~110 s to
re-prefill — past its 90 s budget. The call designed to land would be the call that dies.

Therefore: the worker's forced-synthesis call (move 5) and the primary's
(`_forced_synthesis_tool_overrides`) both take the form the resolved dialect declares, and for every
dialect measured so far that form is `(tool_defs, "none")`. `litellm_client.py:1836` already passes
`tool_choice` through on the local path. Anthropic already takes this form (FRE-484). Cache
continuity is asserted, not assumed (AC-6).

**Which form a provider gets is declared, not discovered at runtime.** ADR-0145 D3 puts provider
quirks on the dialect, and `Dialect` is an enum whose capabilities live in per-dialect tables beside
it (`llm_client/models.py:138-181`, read through `dialect_accepts()`). The capability is
represented the same way: a table `SYNTHESIS_RETAINS_TOOLS: Mapping[Dialect, bool]` in
`llm_client/models.py`, read through `synthesis_retains_tools(dialect: Dialect | None) -> bool`.
`None` — no dialect resolved — returns `True` and logs `synthesis_dialect_unresolved` at WARNING.
The client exposes `dialect_for_role(role) -> Dialect | None`, resolved through
`ModelDefinition.resolve_dialect(provider_def)` (`models.py:647`) for the role's effective
deployment. Both synthesis paths call `synthesis_retains_tools(llm_client.dialect_for_role(role))`
and branch on it: `True` keeps the tools and pins `"none"`; `False` drops the tools and logs
`forced_synthesis_cache_miss_declared` at WARNING on every such call. `_forced_synthesis_tool_overrides`
loses its `provider == "anthropic"` special case, and its call site (`executor.py:5944-5960`) builds
the synthesis tool definitions whenever the capability is `True`, not only for Anthropic.

Table values: `LLAMACPP_QWEN: True` from the probe in this ADR, `ANTHROPIC_ADAPTIVE` and
`ANTHROPIC_BUDGET: True` from FRE-484, `OVH_QWEN` and `OPENAI_GPT5` set by T1 from the same
five-call probe run against each, recorded in T1's close comment. A provider that rejects
`tool_choice="none"` at runtime despite a `True` declaration is a configuration defect: the synthesis
call fails, the worker returns a `ledger`, and the error names the provider and the dialect. There
is no drop-tools retry. That is the same rule as every other path: the worker does not retry the
world.

---

## Alternatives Considered

### Option 1: Port the primary's loop unchanged
**Description:** Inject the primary's two messages at max-2 and max+1, remove tools at max+1.
**Pros:** Copy-and-paste. Three sources agree the two injections are the fix.
**Cons:** A single late warning on a five-round budget. A discarded generation at max+1. A full
prefix re-prefill on the synthesis call. No landing reserve, so the third worker still dies.
**Why Rejected:** FRE-1483 AC-1 exists because the template was never reviewed. Reviewed, it has
four defects. Two of them are fixed on the primary itself by this ADR.

### Option 2: Raise the round cap, the timeout, or both
**Description:** Give the worker room to finish.
**Pros:** Some tasks finish.
**Cons:** A worker that does not know it has 10 rounds paces for 30. On OVH more rounds means more
context per call, so the 90 s timeout fires sooner. Every limit today severs work; a raise moves the
severing and pays more to reach it.
**Why Rejected:** Owner directive, 2026-09-10. Fix the landing, then decide the runway from data.

### Option 3: Enforce a character-denominated budget now
**Description:** Force synthesis when absorbed characters exceed a ceiling, since context growth is
what actually binds.
**Pros:** Aligns the budget with the binding quantity, as Anthropic's `task_budget` does.
**Cons:** The ceiling's value has no measurement behind it. It is a new limit, chosen by guess.
**Why Rejected:** Deferred, not refused. The countdown reports characters so the model paces on them,
and the captures record them. The ceiling is chosen later, from those records.

### Option 4: An ADR-0076 "continue" pause on the worker
**Description:** When a worker hits its cap, ask the owner for more rounds.
**Pros:** The human decides, as ADR-0144 prefers.
**Cons:** A raise by another name. Three workers, three pauses per turn. The worker stops being a
bounded function (FRE-1389).
**Why Rejected:** The worker's limit is a contract. The human's decision point is the caller (D4).

### Option 5: Register worker reports as citable sources so the citation contract enforces D4
**Description:** Put each worker's digest in the source registry, let ADR-0138 D3/D4 verify the
primary's claims against it.
**Pros:** Reuses the existing enforcement machinery.
**Cons:** A worker's report is model-composed. ADR-0138 D2 rejects model-composed content as a source,
for the reason ADR-0146 states: a model can echo its own parametric recall wearing a source's
identifier.
**Why Rejected:** Wrong form. The right form registers the worker's own admissible tool results and
lets the report cite them. That is a follow-on ADR (T4).

### Option 6: A stronger instruction to the primary
**Description:** Reword `_build_synthesis_context` to insist harder.
**Pros:** Cheap.
**Cons:** The instruction exists and was ignored (FRE-1483 AC-3).
**Why Rejected:** Adopted only as a truthful description alongside the two enforcing mechanisms.

### Option 7: Re-dispatch a failed worker with a "continue from" spec
**Description:** Mirror `_maybe_redispatch_on_gap`: a capped worker is re-run once with its report
as context.
**Pros:** Reuses an existing pattern.
**Cons:** Doubles the worker's budget. That is a raise.
**Why Rejected:** With the landing fixed, the first report already carries the findings.

### Option 8: Synthesize with a separate small-context summarizer over the raw results
**Description:** Instead of a tools-off call on the worker's full context, run a fresh call over the
tool results only.
**Pros:** Avoids the large-context call.
**Cons:** Loses the model's own reasoning state. It is a second inference over the same evidence,
which FRE-1387 ruled out for the digest. With D6 the large-context call is cheap to prefill.
**Why Rejected:** D6 removes the reason for it.

---

## Consequences

### Positive Consequences

- A worker that reads 150,000 characters returns findings, not stage directions. The sixth
  inference call, spent today on discarded tool calls, becomes the report.
- Every terminal path returns a declared kind of report. Zero-character results end.
- The third worker of a fan-out lands with fewer rounds instead of dying inside a round.
- The owner sees an incomplete fan-out before anything is composed over it, and the final answer
  carries a note the model cannot remove.
- The primary's local forced synthesis stops re-prefilling its whole context.
- The captures gain the fields a raise decision needs.

### Negative Consequences

- A capped worker takes longer: the synthesis call generates a report where today's discarded
  call generates a few tokens. Measured, not capped.
- A three-worker local fan-out does not fit 900 s. Later workers take fewer rounds. That is the
  runway question D5 hands back with numbers.
- A pause on every incomplete fan-out. Today that is every research turn. It is the observation
  surface the owner asked for, and a stored preference silences it.
- Additive ES fields on the sub-agent capture template (a reversible deploy class).
- FRE-1389's "no wrap-up round" decision is reversed, with the reason stated.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| The synthesis call itself times out on a large context | Medium | D6 keeps prefill cached. The report is asked for under 400 words. A cut synthesis keeps its streamed partial on the local path, and the ledger follows on every path. |
| The model ignores the countdown | Low | The countdown is advice. The enforcement is the tools-off call, which the model cannot bypass. |
| `tool_choice="none"` is not honoured by a provider | Medium | Verified on llama-server (D6) and Anthropic (FRE-484). OVH is verified by T1 with the same probe before its dialect flag is trusted. A dialect with `SYNTHESIS_RETAINS_TOOLS[dialect] == False` gets the drop-tools form with the miss logged. A runtime rejection despite the declaration is a `ledger` and a config finding, never a retry. |
| The D4 pause fires on an eval run with nobody to answer | Medium | `eval_mode` resolves the stored preference or applies the safe default at once, with no pause event and no 180 s wait (D4). |
| The pause becomes noise | Low | Stored preference. The card carries the stop reasons, so silencing it is an informed choice. |
| Move 4 under-estimates a round and the synthesis still gets cut | Low | The estimate is the worker's own measured mean, conservative before the first round. A cut synthesis still returns its partial and its ledger. |
| The ledger's queries disclose search terms to the primary's provider | Low | The primary already receives the worker's task text and digest over the same channel (`tools.yaml` reasoning for `search_memory`). No new destination. |

---

## Verification / Acceptance Criteria

Each criterion states its check and how it fails. A criterion that cannot fail is marked as such.

**AC-1 — A capped worker's report carries its evidence, not its narration.** *Check (seeded
fixture, owned by the test):* a stub tool returns results that each carry a coined marker token —
`K` distinct markers across `N` rounds, each paired with a coined source string. A stub model emits
tool calls with narration text ("Now let me check…") on every tool round, and on the one call
carrying `tool_choice="none"` writes a report that names every marker with its source. Assert
`summary` contains all `K` markers and all `K` sources, contains no narration line,
`report_kind == "synthesized"`, `stop_reason == "cap"`, and the synthesis request carried the tool
definitions with `tool_choice="none"`. *Seeded negative:* with forced synthesis disabled the terminal
content contains zero markers and every narration line. *Live:* re-run the owner's 2026-09-10
research query under the same conditions. The capped worker's report names at least one event with
a date inside the asked week. Today 0 of 5 workers did; that is the discrimination. *Fails if* the
terminal content is assembled from `round_texts`, or if the synthesis call carried no tools or
`tool_choice="auto"`.

**AC-2 — Every terminal path yields a declared report, never the in-flight fragment alone.**
*Check:* drive each of `LLMTimeout` after two completed rounds, outer-deadline `TimeoutError`, a
generic exception, and a completed reply with empty content. Assert the result's `stop_reason` is
that path's value, and that the ledger lists both completed rounds' tool calls with arguments and
result sizes. For `CancelledError`: assert the **capture** written before the re-raise carries
`stop_reason == "cancelled"` and the same ledger, and that the exception still propagates. *Fails if*
any path returns or records `progress.content` only — today's behaviour on three of four.

**AC-3 — The worker lands before it is cut.** *Check:* with a stub clock, set the remaining deadline
below `mean_round_s + effective_timeout` before round `k`; assert no round `k` runs, the next call is
tools-off, and `stop_reason == "time_reserve"`. *Live:* in a local three-worker fan-out, no capture
carries `stop_reason` of `timeout` or `deadline`. *Fails if* the third worker still ends on the outer
deadline, as it does today.

**AC-4 — The countdown reaches the model every round.** *This is a mechanism check, stated as
such; AC-1 is its outcome.* *Check:* capture the request messages of each round; assert a user
message with the correct remaining count, absorbed characters and remaining seconds follows every
round's tool results and none precedes the first. *Fails if* the message list holds only today's three
kinds, or the count is stated only in the system prompt (FRE-1482 AC-1's stated failure).

**AC-4a — The budget is stated at round 1.** *Mechanism check for move 1.* *Check:* the first
request's system message contains the move-1 text with `{N}` equal to
`settings.sub_agent_max_tool_iterations`, and the bytes are identical across two workers in one turn
and across two turns. *Fails if* the text is absent, carries a different number, or differs between
workers.

**AC-5 — The worker knows the date.** *Mechanism check for move 2; its outcome is AC-1's live
probe, which requires an event dated inside the asked week.* *Check:* the task message contains the
rendered block for `turn_started_at`, and a caller passing `None` gets no block and the WARNING.
*Live:* a date-relative task's `web_search` arguments (tool-call log events) name the current year.
*Fails if* the block is absent, or a worker on 2026-09-10 searches 2025, as the recorded run did.

**AC-6 — The synthesis call keeps its cache, and the declared form is the form sent.** *Check
(retained form):* on the local backend, `cache_read_tokens` of the forced-synthesis call is at least
90% of its prompt tokens, for the worker and for the primary's `_forced_synthesis_tool_overrides`
path. Baseline from the 2026-09-10 probe: 4,187 of 4,191 with tools retained, 0 of 3,915 with tools
dropped. *Check (declared-false branch):* with `SYNTHESIS_RETAINS_TOOLS[dialect]` patched to `False`,
the synthesis request carries no `tools` and `forced_synthesis_cache_miss_declared` is logged.
*Check (runtime rejection):* a stub client that raises on `tool_choice="none"` yields
`report_kind == "ledger"`, an error naming the provider and dialect, and exactly one synthesis
attempt. *Fails if* the retained call re-prefills its prefix, if the declared-false branch still
sends tools, or if a rejection triggers a second call. The seeded negative for the retained form:
drop the tools and the ratio falls to zero.

**AC-7 — The caller cannot hide a failed landing.** *Check:* a fan-out with one worker returning
`success == False`, `stop_reason == "cap"`, `report_kind == "synthesized"` — and, in a second
fixture, `success == False`, `stop_reason == "completed"`, `report_kind == "ledger"` — emits a
`sub_agent_fanout_incomplete` pause before the synthesis call.
`stop_and_show` produces a response containing every worker's report and makes no model call.
`answer_from_partial` produces a final answer whose last lines are the trailer naming the task and its
stop reason. The same fan-out with `ctx.eval_mode = True` and no stored preference emits no pause
event, applies `stop_and_show`, and resolves in under one second. *Fails if* a fan-out with all
workers `completed` emits the pause or the trailer, if an incomplete one reaches the synthesis call
without the pause, or if an eval fan-out waits on the pause timeout.

**AC-8 — No limit changed.** *Check:* `sub_agent_max_tool_iterations`, `sub_agent.default_timeout`
and `orchestrator_task_timeout_seconds` (the 900 s turn budget) are unchanged in the merged diff. *This is a guard, not a
discriminating criterion.* It is here because FRE-1483 AC-4 demands it.

**Required observation — not a criterion.** Per-worker wall-clock, rounds used, characters
absorbed, and the `stop_reason` and `report_kind` distributions, from the captures, on a local and
a cloud primary, in T1's close comment. This is an observation artifact, not evidence that the
landing achieved an outcome, and it is listed outside the criteria for that reason. A close comment
without these figures does not discharge T1. No rate threshold is set. Codex review asked for one,
and it is refused on the ground ADR-0147 recorded: no defensible rate exists before the first real
turns, and an invented floor measures the guess, not the system. The captures are the instrument.
The threshold is the owner's decision once they exist, and it is the input D5 names.

**Seeded negatives (FRE-1482 AC-6).** Each mechanism is disabled in turn, and the named criterion
must fail. A criterion that passes with its mechanism disabled is not measuring the mechanism.

| Mechanism disabled | Criterion that must fail |
|---|---|
| Move 1 (budget at round 1) | AC-4a |
| Move 2 (date in the task message) | AC-5 |
| Move 3 (countdown) | AC-4 |
| Move 4 (time reserve) | AC-3 |
| Move 5 (forced synthesis) | AC-1 (zero markers, every narration line) |
| Ledger on the killed paths | AC-2 |
| D4 pause | AC-7 (an incomplete fan-out reaches synthesis unpaused) |
| D4 trailer | AC-7 (no trailer after `answer_from_partial`) |
| D4 `eval_mode` rule | AC-7 (an eval fan-out waits on the pause timeout) |
| D6 retained form | AC-6 (cache ratio falls to zero) |

---

## Implementation

| Ticket | Scope | Tier | Depends on |
|---|---|---|---|
| T1 — FRE-1482 (existing) | D2 planner rule. D3 moves 1–5 including the `turn_started_at` threading through `ExpansionController.execute` and `_run_dispatch`, the terminal paths, the ledger, `stop_reason` / `report_kind` on `SubAgentResult` and `SubAgentCapture`. D6 on the worker, including the `synthesis_retains_tools` dialect field and the OVH probe. AC-1 to AC-6, AC-8, and the required observation. | Tier-1 (as labelled) | this ADR |
| T2 — FRE-1484 | D4: the `sub_agent_fanout_incomplete` pause and its `eval_mode` rule, `stop_and_show` composition, the trailer, the synthesis-context wording. AC-7. | Tier-2 | T1 (reads `stop_reason` / `report_kind`) |
| T3 — FRE-1485 | D1 fixes on the primary: countdown unit and zero case, `_forced_synthesis_tool_overrides` branches on `synthesis_retains_tools()` and loses its Anthropic special case, its call site builds synthesis tool definitions whenever the capability is `True`, `settings.py:247` docstring. AC-6 on the primary. | Tier-3 | T1 (introduces `SYNTHESIS_RETAINS_TOOLS` and `dialect_for_role`) |
| T4 — FRE-1486 (Backlog note) | Follow-on ADR: shared source registry so worker findings are citable. | — | after T1 lands and is observed |

Files touched by T1: `orchestrator/sub_agent.py` (`_SUB_AGENT_SYSTEM_PROMPT`, `_ToolLoopState`,
`_run_tool_loop`, `run_sub_agent`, `_killed_result`, `_build_capped_partial_content`),
`orchestrator/sub_agent_types.py` (`SubAgentSpec.turn_started_at`, `SubAgentResult.stop_reason`,
`.report_kind`), `orchestrator/expansion_controller.py` (`_build_planner_system_prompt`,
`execute`, `_run_dispatch`, `_maybe_redispatch_on_gap`), `orchestrator/executor.py:5036-5054` (the
`execute` call site), `llm_client/models.py` (`SYNTHESIS_RETAINS_TOOLS`, `synthesis_retains_tools`),
the client's `dialect_for_role`, `captains_log` sub-agent capture model and its ES template.
Files touched by T2: `orchestrator/executor.py` (before `:5107`), `orchestrator/constraint_options.py`,
`orchestrator/expansion_controller.py` (`_build_synthesis_context`).
Files touched by T3: `orchestrator/executor.py` (`:5885-5896`, `:3100-3129`), `config/settings.py:247`.

---

## References

- FRE-1483 — this review's commission, with D1–D5 and AC-1–AC-5
- FRE-1482 — the implementation this ADR unblocks; all three sessions' evidence; the owner's no-raise directive
- FRE-1389 — built the loop and its cap; the "no wrap-up round" decision reversed here
- FRE-1399 — the cap path's empty digest; `round_texts` and `narrative_synthesized`
- FRE-1379 — the killed-worker black hole; `_killed_result` and the streaming partial
- FRE-1397 — the shrinking per-worker deadline that move 4 reads
- FRE-1387 — the digest cap; the ruling against a second inference for the digest
- FRE-1360 — `fetch_url`'s open blocker
- ADR-0076 — constraint pauses; the option registry D4 extends
- ADR-0081 — frozen append-only layout; why every injection is a tail append
- ADR-0138 — the citation contract; D2 admissibility, and why worker reports cannot bind under it
- ADR-0144 — the human decides the retry; `stop_and_report` as the safe default
- ADR-0145 — D1 the one generation budget; D6 the worker mode
- ADR-0146 — the answer must be unguessable; why a model-composed source is rejected
- ADR-0147 — memory belongs to the planner; the planner prompt this ADR extends
- `orchestrator/executor.py:1914`, `:3100-3129`, `:5864-5896`, `:6038`, `:6583-6647`
- `orchestrator/sub_agent.py` `_SUB_AGENT_SYSTEM_PROMPT`, `_ToolLoopState`, `_run_tool_loop`, `_killed_result`, `run_sub_agent`
- `orchestrator/expansion_controller.py:88-126`, `:574-819`, `:977-1024`
- `llm_client/litellm_client.py:1836`, `:1946`
- `config/governance/tools.yaml` `sub_agent_tools`
- Anthropic API reference — `output_config.task_budget`, the countdown marker the model sees during generation
- Sub-agent capture index `agent-captains-captures-subagents-2026-09`, session `606ae6a4`, read 2026-09-10
- Cache probe, 2026-09-10 20:22 UTC, five calls against the local backend through the Caddy egress
