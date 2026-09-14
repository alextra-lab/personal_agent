# ADR-0152: The Planner Decides Whether to Expand — Asked, It May Decline

**Status:** Proposed
**Date:** 2026-09-14
**Deciders:** Owner (the direction in FRE-1502; "write the ADR on the choice-axis evidence", 2026-09-13; the planner-probe matrix, its quant and effort rules, and the dispositions of the open questions, 2026-09-14), `adr` seat at Opus 5 (author, the 2026-09-14 planner-probe matrix), `explore` seat (the FRE-1498 study), `slm_server` session (model serving, tuning and the server-side logs of the probe)
**Tags:** routing, expansion, planner, decomposition, sub-agents, latency

**Amends:** ADR-0036 D1 and D5 ("the LLM generates plan content only; it does not decide whether to expand") for the four task types in D1. **Supersedes:** the routing half of ADR-0142 D1 (`CONVERSATIONAL` through the complexity matrix). **Consumes:** FRE-1382 (the per-turn fan-out cap), FRE-1393 (the spend pause), ADR-0150 D3 (round budgets per thoroughness level).
**Umbrella:** FRE-1502.

---

## Context

### Today the model never chooses expansion

Three facts from the code, confirmed on the deployed container by FRE-1498 (finding S1):

1. The gateway matrix (`request_gateway/decomposition.py::_apply_matrix`) decides `SINGLE`, `HYBRID`
   or `DECOMPOSE`. The planner receives that strategy as an input: its user message is
   `Strategy: {strategy}\nQuery: {query}`.
2. The planner cannot decline. `_validate_plan_json` returns `None` for an empty task list, and
   `None` reaches the fallback planner, which always dispatches.
3. No tool lets the primary start a worker.

The matrix sends `CONVERSATIONAL` and `TOOL_USE` to `SINGLE` always, and `ANALYSIS` and `PLANNING` to
`SINGLE` when the message is short. The classifier labels by keyword, so the label records the
absence of a keyword, not the shape of the work (ADR-0142, FRE-1377 F14).

### Asked, the planner routes better than the ladder

FRE-1498 patched the planner so that every type sorted by tone reaches it as `HYBRID`, and it may
answer `{"strategy": "SINGLE", "tasks": []}`. Its findings (`docs/research/2026-09-12-fre-1498-what-the-model-chooses-when-nothing-forces-it.md`):

- **C1.** The planner declined all seven commands, greetings, tool requests and follow-ups, and
  expanded all twelve research-shaped questions. The ladder had labelled ten of those twelve
  `conversational`.
- **C2, C3.** The steering verb ("Research …") flips the classifier every time and does not move the
  planner beyond its own variance. Register (terse against paragraph) does not move it at all.
- **C6.** Over 152 planner-only draws per model, the decision is a stable property of the request.
  It is also model-specific: the OVH `Qwen3.8-27B` declined 8–11% of draws, the local model 32–39%.
- **S2.** The alternative that FRE-1394 scoped, removing the two forcings so that the complexity
  matrix decides, changed the strategy of one fixture in nineteen. Every message under 15 words is
  `SIMPLE`, and `SIMPLE` still maps to `SINGLE`.

### The planner-probe matrix of 2026-09-14

This ADR's own measurement extends C6 across models, quants, engines and effort levels, to find
whether D1 holds on the planners the owner can actually serve, and what it costs in delay.

**Method.** The committed planner prompt with the FRE-1498 decline rule, sent alone to each
configuration: no worker, no gateway turn. 19 FRE-1498 fixtures × 3 trials = 57 draws per arm,
streamed as the gateway streams, with each model's catalog sampling. Each draw is scored against an
expected decision: decline for the four real requests from 2026-09-08, the greeting and `tool_logs`;
expand for the twelve research questions; `c5_noverb` excluded as borderline. One configuration
served at a time on the owner's M4 Max (128 GB). No download, tuning run or other model load
overlapped an arm, except the 2026-09-14 10:45–10:59 UTC window of arm 4L, where downloads on the
serving machine slowed decode by about 10%. The full tables are in Appendix A.

**Result, per configuration** (agreement over 54 scored draws; planner seconds per call):

| Engine | Model / quant | Effort | Agreement (95% range) | Declines correct | p50 / p90 |
|---|---|---|---|---|---|
| llama.cpp | Qwen3.8-Flash-Next IQ4_XS | medium | 96% (87–99%) | 17/18 | 20.3 / 38.0 s |
| llama.cpp | Qwen3.8-Flash-Next IQ4_XS | low | 91% (80–96%) | 16/18 | 19.1 / 29.2 s |
| llama.cpp | Qwen3.8-27B Q4_K_XL | low | 100% (93–100%) | 18/18 | 49.0 / 87.6 s |
| llama.cpp | Qwen3.6-35B-A3B Q6_K_XL | none | 85% (73–92%) | 13/18 | 34.5 / 96.9 s |
| llama.cpp | Arch-Router-1.5B F16 | none | 65% (51–76%) | 18/18 | 0.2 / 0.2 s |
| MTPLX 2.11.2 | Qwen3.8-Flash-Next Optimized-Speed | low | 96% (87–99%) | 18/18 | 13.1 / 23.2 s |
| MTPLX 2.11.2 | Qwen3.8-Flash-Next Optimized-Speed | medium | 94% (85–98%) | 17/18 | 17.6 / 27.9 s |
| MTPLX 2.11.2 | Qwen3.8-27B Bare-Speed | low | 94% (85–98%) | 15/18 | 22.5 / 40.6 s |
| MTPLX 2.11.2 | Qwen3.8-27B Optimized-Speed | low | 98% (90–100%) | 17/18 | 23.2 / 34.9 s |
| MTPLX 2.11.2 | Qwen3.8-27B Optimized-Speed | medium | 94% (85–98%) | 15/18 | 30.0 / 41.5 s |

Five things this settles:

1. **D1 holds on every planner good enough to serve.** On the five configurations that pass the
   thresholds in D6, the planner declines commands and expands research questions at 91–98%. The
   hardest fixture for every model is "I thought I asked you to create a weekly prediction tool…".
2. **Higher reasoning effort never improved the decision.** On MTPLX, `medium` was slower than `low`
   on both models, with no better agreement. On llama.cpp Flash-Next, `low` was slightly worse than
   `medium`, inside the noise.
3. **A dedicated router in front of the planner does not work with the model tested.** Arch-Router
   answers in 0.2 s and declines 39 of 57 draws, including most research questions.
4. **The 35B-A3B is a worse planner, not a faster one.** It reasons without a limit (p50 1,926
   completion tokens), and one draw reasoned until it reached the 16,384-token cap. Its Q8 quant
   fixed none of its Q6 errors.
5. **The delay D1 adds is the planner call itself.** It is 13–23 s per turn on the best
   configurations and 20 s on today's production binding.

### What this ADR does not decide

**What expansion is worth.** Whether a fan-out answer beats the single lane is a delivery-axis
question. The owner's comment on FRE-1502 (2026-09-13) scopes this ADR to the choice axis, and scored
answer quality lives on FRE-1495.

**How briefs are scoped** (FRE-1498 P7). Its effect is visible only when workers run, and the probe
runs none. It moves to its own ticket.

**Which engine serves production.** MTPLX was faster on both Qwen3.8 models. Integrating it is
`slm_server`'s work, which the owner started as an option on 2026-09-14.

**Which model is the local primary.** Flash-Next on llama.cpp stays the binding. This ADR records
only how each candidate behaves as a planner.

---

## Decision

### D1 — Expansion is the planner's decision for the four types sorted by tone

`_apply_matrix` routes `CONVERSATIONAL`, `TOOL_USE`, `ANALYSIS` and `PLANNING` to `HYBRID` with the
reason `planner_asked`, at every complexity.

These stay as they are:

- `MEMORY_RECALL` and `SELF_IMPROVE` keep `SINGLE`. They rest on the shape of the work, not on the
  register of the request.
- `DELEGATION` keeps its current branches (FRE-1376).
- The resource-pressure forcings `expansion_denied` and `zero_budget` still force `SINGLE` before the
  matrix runs.

The planner prompt gains the decline rule that FRE-1498 measured, with its text unchanged:

> First decide whether this query needs independent sub-tasks at all. If one assistant working alone,
> with the same tools, would answer it well in one pass — a greeting, a short factual question, a
> single lookup, a follow-up — output {"strategy": "SINGLE", "tasks": []} and nothing else. Choose
> HYBRID or DECOMPOSE only when splitting the work into independent sub-tasks would produce a better
> answer than one pass

`_validate_plan_json` accepts `{"strategy": "SINGLE", "tasks": []}` (or a missing `tasks`) as a valid
decline.

**A declined plan returns the turn to the ordinary tool loop.** It dispatches no worker, appends no
synthesis message, and is not degraded. The primary answers as it does on a `SINGLE` turn today, with
its own tools and ceilings.

**A planner failure on a `planner_asked` turn also returns the turn to the tool loop.** A failure is
invalid output, a timeout or an error. The fallback planner does not run. The gateway did not decide
to expand these turns, so a failed planner must not expand them. FRE-1498 recorded the harm: two
planner failures reached the fallback planner and dispatched workers the model never chose. The
fallback planner (ADR-0036 D3) stays for the turns that the gateway itself sends to `HYBRID` or
`DECOMPOSE`, which after D1 means the `DELEGATION` fallbacks.

### D2 — The bound on expansion the planner chooses

1. **At most three workers per turn**, and never more than the per-turn load budget. This is the
   `HYBRID` cap of 3 taken as the minimum with `governance.expansion_budget` (FRE-1382).
2. **A `DECOMPOSE` answer on a `planner_asked` turn is dispatched as `HYBRID`** under the same cap. It
   is not a validation failure. Across 727 parsed plans in the probe matrix, 3 said `DECOMPOSE`. No
   measurement supports the `DECOMPOSE` cap of 5 for these turns.
3. **Round budgets per thoroughness level:** quick 3, standard 6, thorough 10, set through
   `sub_agent_rounds_by_thoroughness` (ADR-0150 D3), which is unset today, so every level is 20. These
   are starting values, not measured optimums. The basis: landed workers stopped by themselves at
   8–10 rounds, and the single lane answers these questions after 2–3 searches (FRE-1498 D5, C4). The
   owner can change them in configuration.
4. **The spend pause of FRE-1393** stays the control at the turn level.

### D3 — The planner prompt shows the budgets that are enforced

The thoroughness line of the planner prompt renders each level's real round budget. It stops
showing "(20 tool round(s))" on all three levels. The reason is accuracy: the prompt must not describe
a budget the controller does not apply.

This ADR claims no effect on the mix of levels the planner chooses. FRE-1498 C6 found that removing
the annotation raised `thorough` by 11 to 23 points. In the 2026-09-14 matrix, showing 3/6/10 on
llama.cpp Flash-Next lowered `thorough` from 13% to 7% over 95 draws per variant, inside the noise.

### D4 — The planner's decision is recorded on every turn

Every `planner_asked` turn records one planner decision: `declined`, `expanded`, or `failed`, the last
covering invalid output, a timeout or an error. The decision is recorded where `route_traces` and the
ES projection can read it, next to the task type and the deployment that planned. With this record,
the live decline rate per deployment can be measured.

On a declined or failed turn, `ctx.expansion_strategy` is cleared. The turn must not match the FRE-514
label-lie predicate (`_LABEL_LIE_SQL` in `observability/route_trace/ledger.py`), which reads a
non-`single` strategy with `orchestration_event = 'primary_handled'` as a gateway label that
orchestration contradicted. A planner that declined did not contradict anything.

### D5 — The planner uses the primary's model and mode

No planner-specific model, effort level or thinking budget is added. The planner keeps running on the
primary role (FRE-1390), in that role's selected mode.

- **Effort.** No effort level improved the decision in the matrix (Context, point 2). The effort
  that serves best on an engine is that engine's configuration, owned by `slm_server`.
- **A separate planner model.** On the owner's machine, one model of Flash-Next's size fits at a
  time, so a separate local planner would replace the primary. A cheap router in front of the planner
  failed at 65%.

### D6 — A planner configuration qualifies on the probe before it plans

The planner probe and its fixtures are committed to the repo. A configuration (engine, model, quant,
effort) qualifies as planner when the probe, 19 fixtures × 3 trials, gives:

| Criterion | Threshold |
|---|---|
| Agreement with the expected decisions | ≥ 90% (at least 49 of 54) |
| Commands, greeting and tool requests declined | ≥ 16 of 18 |
| Plans that fail to parse | 0 |
| Planner seconds per call | p50 ≤ 40 s and p90 ≤ 120 s |

The p90 limit stays below the 125 s origin timeout of the tunnel on non-streamed calls, and far below
the 600 s `planner_timeout_seconds`. The probe runs before a new deployment is bound to the primary
role. This ADR adds no gate in code. The check is part of changing the binding, the same way a
catalog change is checked today.

---

## Alternatives Considered

### Option 1: Remove the forcings and let the complexity matrix decide (FRE-1394 as scoped)

**Description:** Delete the `CONVERSATIONAL` and `TOOL_USE` branches, so these types fall through to the
complexity matrix.

**Pros:**
- No planner call on turns the matrix keeps `SINGLE`, so no added delay.
- A small code change.

**Cons:**
- It changes almost nothing. In FRE-1498 S2, 17 of 18 fixtures resolved exactly as before, because
  every message under 15 words is `SIMPLE` and `SIMPLE` maps to `SINGLE`.
- The decision stays a keyword and length heuristic, which the steering verb still flips (C2).

**Why Rejected:** It does not reach the short requests it was written for.

### Option 2: Keep the ladder

**Description:** Leave the matrix as it is.

**Pros:**
- No added delay and no model dependence.

**Cons:**
- Ten of twelve research questions go to `SINGLE` with the conversational round cap (FRE-1498 C1).
- The owner's "Research …" prefix remains the only way to reach expansion.

**Why Rejected:** It keeps the defect that ADR-0142 and FRE-1498 measured.

### Option 3: Give the primary a tool that starts workers

**Description:** No planner call before the turn. The primary decides during its tool loop, when it has
seen what the work needs. This matches ADR-0142 D3's "demonstrated need".

**Pros:**
- No delay on turns that never need workers.
- The decision uses evidence from the turn itself.

**Cons:**
- It is unmeasured: no arm of FRE-1498 or of the matrix offered the primary this option.
- FRE-1498 C4 found the primary economical: it stops after two or three searches and does not reach
  its ceiling. It may never start a worker.
- It moves plan authorship into the tool loop, which ADR-0150's worker contracts do not cover.

**Why Rejected:** Deciding on it now means deciding without evidence. It stays a candidate for a later
study.

### Option 4: A dedicated router model before the planner

**Description:** A fast routing model decides expand or decline. Only an expansion pays for a full planner
call.

**Pros:**
- A declined turn costs 0.2 s instead of 13–23 s.

**Cons:**
- Arch-Router-1.5B, the routing model tested, agreed on 65% of draws. It declined research questions
  that the planners expanded in every draw.
- A second decider can drift from the planner that writes the plan.

**Why Rejected:** The model tested fails the quality threshold by 25 points.

### Option 5: A planner-specific effort level

**Description:** Run the planner call at `low` effort, separately from the primary's mode, to cut the
delay.

**Pros:**
- A plausible cut in reasoning tokens.

**Cons:**
- On llama.cpp Flash-Next, `low` saved 1.2 s at the p50 and cost 3 correct draws of 54.
- On MTPLX, `low` was already the faster and at least equal setting, and that is the engine's own
  configuration.

**Why Rejected:** It adds a configuration surface for a gain the matrix did not find.

---

## Consequences

### Positive Consequences

- Research questions reach expansion by what they ask, not by a keyword prefix.
- The decision survives rephrasing that flips the classifier (FRE-1498 C2, C3).
- A failed planner no longer dispatches workers on turns nobody chose to expand.
- The live decline rate per deployment becomes measurable (D4).
- A candidate primary deployment has a defined, repeatable test for planning (D6).

### Negative Consequences

- **Every turn of the four types pays for one planner call before any work starts:** 13–23 s on the
  best configurations measured, 20 s at the p50 on today's binding. Most such turns decline, so most
  pay the delay for nothing. The owner accepted this cost on 2026-09-14.
- **The routing policy depends on the selected primary.** The OVH `Qwen3.8-27B` declined 8–11% of
  FRE-1498 probe draws, and that probe let OVH default to `xhigh` effort. Production sends `medium`,
  which is unmeasured.
- The planner prompt and its decline rule become part of routing, so a prompt change is a routing
  change and needs the probe (D6).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| The OVH primary expands most turns under D1 | High | AC-6 runs the committed probe on the OVH deployment at production effort before the umbrella closes. A failure is reported to the owner, who decides whether D1 needs a per-deployment gate. D2's three-worker cap and the spend pause bound the cost meanwhile. |
| The fixtures overfit: 19 messages shaped the decline rule and also score it | Medium | AC-4 scores the planner against owner labels on a sample of real turns, not the fixtures. |
| The planner call fails more often under load, and turns lose time | Medium | D1 sends a failed planner to the single lane, so a failure costs the call's time and nothing else. AC-3 checks the failure path. |
| The round budgets of D2 are too small for deep research | Medium | They are configuration values with their basis stated, and the owner can raise them. The spend pause lets the user continue a turn. |
| A declined turn is recorded as a label lie, and the FRE-514 candidate count inflates | Low | D4 clears `expansion_strategy` on a decline. AC-2 checks the predicate. |

---

## Implementation Notes

**Reference implementation.** The FRE-1498 `regime_D.patch` (Appendix A2 of the study document) is
about 96 lines, without its study comments. D1 needs three more parts that the study patch did not have:

- the failure path that returns to the tool loop instead of the fallback planner;
- `DECOMPOSE` mapped to `HYBRID` on `planner_asked` turns;
- the decision record and the cleared `expansion_strategy` of D4.

**Files:**
- `src/personal_agent/request_gateway/decomposition.py`: the matrix branch and the `planner_asked` reason.
- `src/personal_agent/orchestrator/expansion_controller.py`: prompt rule, validator, decline and
  failure returns, the `DECOMPOSE` mapping, the rendered budgets.
- `src/personal_agent/orchestrator/executor.py`: the return to `LLM_CALL` without a synthesis message,
  and clearing `ctx.expansion_strategy`.
- `src/personal_agent/observability/route_trace/`: the planner decision field, and the label-lie predicate.
- `config/`: `sub_agent_rounds_by_thoroughness` values.
- `scripts/eval/fre1498/`: the probe extensions used on 2026-09-14 (streaming, fixture and variant
  filters, effort, the Arch-Router mode, the scorer with its thresholds).

**Relationship to open tickets.** FRE-1394 keeps its cap half: deleting the per-type iteration cap.
Its routing half (the `CONVERSATIONAL` branch deletion, and its AC-2 and AC-4) is replaced by D1.
FRE-1382 is merged. FRE-1393 is done.

**Order.** The committed probe comes first, because D6 and AC-6 need it. The routing change and the
decision record ship together, because the decision record is what the ACs read. The budget values and
the prompt rendering can ship separately.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR and are adjudicated on FRE-1502 after the implementation chain is
deployed, not at the merge of this ADR.

- **AC-1 — The four types reach the planner.** Over a deployed window of at least 200 turns, every turn
  with `task_type` in the four D1 types and expansion permitted records `decomposition_reason =
  planner_asked` and one planner decision. · **Check:** `route_traces` grouped by `task_type`,
  `decomposition_reason` and the D4 decision field. · *Fails if* any such turn records
  `conversational_always_single`, `tool_use_single`, `analysis_simple` or `planning_simple`, or has no
  planner decision.

- **AC-2 — A decline is honoured and not mislabelled.** · **Check:** over the same window, every turn
  whose planner decision is `declined` records `sub_agent_count = 0`, and no synthesis message is added
  (the `expansion_controller_complete` event reports zero sub-agents). The same query also runs the
  label-lie predicate. · *Fails if* any declined turn dispatched a worker, or any declined turn matches
  the label-lie predicate.

- **AC-3 — A failed planner does not expand.** · **Check:** a test drives a `planner_asked` turn with a
  planner stub that returns invalid JSON, and a second stub that times out. It asserts zero dispatched
  workers, no `fallback_planner_used` event, and a turn that answers through `LLM_CALL`. Live: over the
  window, no `planner_asked` turn with decision `failed` records `sub_agent_count > 0`. · *Fails if*
  either stub produces a dispatch, or any live failed turn dispatched.

- **AC-4 — On real traffic, the planner agrees with the owner.** · **Check:** a random sample of 40
  `planner_asked` turns from the window, drawn with at least 15 declined and 15 expanded. The owner
  labels each "should expand" or "should not" from the message preview, blind to the planner's
  decision. · *Fails if* agreement is below 85%, or more than 2 of the turns the owner labels "should
  not" were expanded. The threshold sits 5 points below the probe's, because real messages carry
  conversation history that the fixtures do not.

- **AC-5 — The bound holds.** · **Check:** over the window, `sub_agent_count` against the turn's
  `expansion_budget` from `gateway_output`, and each worker capture's `tool_iterations` against its
  level's budget. · *Fails if* any turn records more workers than min(3, `expansion_budget`), or any
  worker's rounds exceed its level's configured budget.

- **AC-6 — The deployments the owner can select are probed.** · **Check:** the committed probe, run
  from the repo, on the local primary binding and on the OVH primary deployment at the effort
  production sends. The results are posted on FRE-1502 against the D6 thresholds. · *Fails if* the
  probe cannot run from the repo, or the local binding misses a D6 threshold. An OVH miss does not fail
  this criterion. It is reported, and the owner decides on a gate.

- **AC-7 — The delay is the one measured.** · **Check:** over the window, the planner phase wall clock
  (`planner_completed` duration) per deployment, at p50 and p90. · *Fails if* the local binding's
  planner p90 exceeds 120 s, or the p50 exceeds its D6 probe value by more than 50%.

- **AC-8 — The prompt shows the enforced budgets.** · **Check:** the planner system prompt built at the
  deployed revision, compared with `settings.sub_agent_rounds_for(level)` for each level, and a worker
  capture per level showing its round budget. · *Fails if* any level's rendered number differs from the
  budget that the worker receives.

---

## References

- FRE-1502 (umbrella), FRE-1498 (the study), FRE-1394, FRE-1382, FRE-1393, FRE-1390, FRE-1376, FRE-1495, FRE-514
- `docs/research/2026-09-12-fre-1498-what-the-model-chooses-when-nothing-forces-it.md` (S1, S2, C1–C7, D5, P1–P3, P7, Appendix A2)
- ADR-0036 — Expansion Controller (D1, D3, D5)
- ADR-0142 — Capability Is Not a Property of Register (D1, D2, D3)
- ADR-0145 — Two Nouns and the Dialect Between Them (FRE-1430 F2 and F16, the effort controls)
- ADR-0149 — Land Before the Cut
- ADR-0150 — The Worker Returns Data, Not Prose (D3, the thoroughness levels)
- `src/personal_agent/request_gateway/decomposition.py`, `src/personal_agent/orchestrator/expansion_controller.py`, `src/personal_agent/observability/route_trace/ledger.py`
- [Arch-Router-1.5B](https://huggingface.co/katanemo/Arch-Router-1.5B), [MTPLX](https://mtplx.com/docs/quickstart/)

---

## Status Updates

### 2026-09-14 - Proposed
**Changed By:** `adr` seat
**Reason:** Drafted on the owner's "draft it" after the planner-probe matrix and the dispositions of the open questions.

---

## Appendix A — The planner-probe matrix, 2026-09-14

### A1. Design

- **Prompt.** `_build_planner_system_prompt` at `origin/main` with the FRE-1498 decline rule, and the user
  message `Strategy: HYBRID\nQuery: {message}\n\nProduce the JSON plan.` Rendered once into a file, so
  the long-running probe process does not import the package.
- **Fixtures.** The 19 of `scripts/eval/fre1498/fixtures.yaml`. Expected decisions: decline for
  `susan_1`–`susan_4`, `greeting` and `tool_logs`; expand for the other twelve; `c5_noverb` excluded.
- **Draws.** 3 trials per fixture, one variant (`current`), one call at a time, streamed with usage.
  Arm 1 also ran 5 trials with a second variant (`d3`, rendering 3/6/10); its 3-trial `current`
  subset is the row in the Context table.
- **Sampling.** Each model's catalog preset: Flash-Next and the 27B at 1.0 / 0.95 / 20; the 35B at
  0.6 / 0.95 / 20; Arch-Router at its `generation_config.json` (0.7 / 0.8 / 20, repetition penalty 1.1).
  `max_tokens` 16,384 (512 for Arch-Router).
- **Effort.** llama.cpp: the server base `medium` from `--chat-template-kwargs`, overridden per request
  by a top-level `reasoning_effort`. The 35B template reads no effort. MTPLX: `--reasoning-effort
  medium`, overridden per request.
- **Rules applied** (owner, 2026-09-14). Effort `low` first, and `medium` only if `low` passes. The
  lowest quant first. A speed failure ends a model, because a higher quant or effort is slower. A
  quality shortfall with speed passing moves to the next quant. A higher quant must be clearly better to
  be kept.
- **Serving.** llama.cpp pinned build with MTP draft depth 1 on the Qwen models. MTPLX 2.11.2 with each
  pack tuned by `mtplx tune --retune` (depths 1–3 only; the tool rejects deeper) and served at the best
  tuned depth, `--preserve-thinking off`, `--context-window 131072`.

### A2. Per-fixture declines (declined draws / 3)

| Fixture | Expected | FN llama medium | FN llama low | 27B Q4 llama low | 35B Q6 | Arch-Router | FN MTPLX low | FN MTPLX med | 27B Bare low | 27B OptSpd low | 27B OptSpd med |
|---|---|---|---|---|---|---|---|---|---|---|---|
| susan_1_run_tool | decline | 3 | 3 | 3 | 2 | 3 | 3 | 3 | 3 | 3 | 3 |
| susan_2_able_to_run | decline | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| susan_3_thought_i_asked | decline | 2 | 1 | 3 | 0 | 3 | 3 | 2 | 0 | 2 | 0 |
| susan_4_build_and_run | decline | 3 | 3 | 3 | 2 | 3 | 3 | 3 | 3 | 3 | 3 |
| greeting | decline | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| tool_logs | decline | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| gpsr_tuna | expand | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 |
| heatpump | expand | 0 | 0 | 0 | 0 of 2 | 3 | 0 | 0 | 0 | 0 | 0 |
| regulatory | expand | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| mallorca | expand | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 0 | 0 |
| c1_tool_imperative | expand | 0 | 0 | 0 | 1 | 3 | 0 | 0 | 0 | 0 | 0 |
| c2_verb | expand | 0 | 0 | 0 | 2 | 0 | 0 | 1 | 0 | 0 | 0 |
| c2_noverb | expand | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| c3_terse | expand | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 |
| c3_paragraph | expand | 0 | 0 | 0 | 0 | 3 | 0 | 0 | 0 | 0 | 0 |
| c4_deliverable | expand | 0 | 1 | 0 | 0 | 3 | 2 | 1 | 0 | 0 | 0 |
| c4_process | expand | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| c5_verb | expand | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| c5_noverb | excluded | 0 | 2 | 1 | 0 | 2 | 1 | 1 of 2 | 0 | 0 | 0 |

The 35B Q6 `heatpump` column counts 2 draws because one plan failed to parse.

### A3. Speed and tokens

| Configuration | Completion tokens p50 / p90 | Decode on the tuning prompt |
|---|---|---|
| FN llama.cpp medium | 635 / 1,130 | 21.1 tok/s (llama-bench, 2026-08-28) |
| 27B Q4 llama.cpp low | 584 / 943 | about 12 tok/s on planner text (server timings) |
| 35B Q6 llama.cpp | 1,926 / 4,509 | 62.3 tok/s (llama-bench, 2026-08-28) |
| FN MTPLX Opt-Speed low | 556 / 955 | 91.9 tok/s at depth 3 (plain 42.3) |
| 27B MTPLX Bare-Speed low | 696 / 1,115 | 81.8 tok/s at depth 3 (plain 26.9) |
| 27B MTPLX Opt-Speed low | 634 / 934 | 72.2 tok/s at depth 3 (plain 22.1) |

### A4. Instrument notes

- **Streaming.** Two non-streamed 35B draws failed with HTTP 524 at 125 s, which is Cloudflare's origin
  timeout on the tunnel. The probe then streamed, as `LiteLLMClient` does, and all four retries
  succeeded. The gateway streams, so production does not have this limit.
- **One cut stream on MTPLX** (13:01:24 UTC). The MTPLX log shows a normal generation to its end. The
  break was on the path between the server and the probe. MTPLX kept generating after the disconnect,
  and the next draw waited about 7 s behind it. That draw does not change the arm's p50 or p90.
- **A 10% slowdown** in the last 12 draws of the 27B Q4 llama.cpp arm, from downloads on the serving
  machine. A correction does not bring that arm under the speed limit.
- **Prompt size.** MTPLX renders the planner prompt as 513 tokens, llama.cpp as 489. The chat templates
  differ.
- **Stopped arms.** The 27B Q6 llama.cpp arm was stopped after 13 draws when the owner set the
  lowest-quant-first rule, and those draws are set aside. The 35B Q8 arm was stopped after 29 of 31
  reduced draws on the owner's instruction, and it fixed none of the Q6 errors.
- **Raw rows.** One JSON line per draw, kept outside the repo in the `adr` worktree's ignored
  `telemetry/archive/fre1502-planner-probe/`. The probe and scorer used are copied beside them for the
  commit that D6 requires.
