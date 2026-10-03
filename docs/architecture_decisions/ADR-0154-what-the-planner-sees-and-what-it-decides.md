# ADR-0154: What the Planner Sees and What It Decides — an Isolated Planner, Thinking Off, Asked on Every Turn of the Four Register Types

**Status:** Proposed
**Date:** 2026-10-03
**Deciders:** Owner (one ADR in place of two, 2026-10-02; the design choice, the memory digest and the measurement budget, 2026-10-03), `master` (the five ADR-0152 review findings and the scope of FRE-1537), `adr` seat at Opus 5.5 (author, the 2026-10-03 planner measurement)
**Tags:** routing, expansion, planner, memory, decomposition, latency

**Supersedes:** [ADR-0147](ADR-0147-memory-belongs-to-the-planner.md) and [ADR-0152](ADR-0152-the-planner-decides-whether-to-expand.md), both Proposed. **Amends:** ADR-0036 D1 and D5 ("the LLM does not decide whether to expand") for the four task types in D2. **Supersedes:** the routing half of ADR-0142 D1. **Umbrella:** FRE-1537.

---

## Context

### Why one ADR

ADR-0147 gives the planner a bounded memory digest. ADR-0152 lets the planner decide whether to expand, and lets it decline. Both write to the same planner prompt and the same planner call. A digest in that prompt can change the decline decision. So the two are one design: first what the planner sees, then what it decides.

On 2026-09-14 master reviewed ADR-0152 and posted five findings on FRE-1502. The `adr` seat agreed with all five:

1. ADR-0152 did not mention ADR-0147, and the two conflict.
2. Every follow-up test case expected a decline, so a planner that declines every short follow-up scored perfectly.
3. The delay was measured on the planner call alone, and no arm ran with thinking off.
4. ADR-0152 D2 set worker round budgets that FRE-1487 owns. FRE-1487's AC-6 forbids a limit change before the owner decides on its evidence.
5. The statistics claimed more than the data showed.

This ADR answers all five. It carries over from ADR-0152, unchanged:
- the failure path that does not reach the fallback planner;
- a decline shown as a decline in the derived views;
- `DECOMPOSE` dispatched as `HYBRID`;
- the rejection of a router model in front of the planner (ADR-0152 Option 4).

### What changed since ADR-0152

- **The planner already receives the conversation.** FRE-1521 added `planner_brief_mode=briefing`. The owner switched it on in production on 2026-09-15 (`.env`: `AGENT_PLANNER_BRIEF_MODE=briefing`). The planner user message carries up to `planner_history_max_chars` (60,000) characters of rendered history, plus the briefing rules. FRE-1517 F6 measured the gain: rule carry-through rose from 14/53 to 40/41 tasks.
- **Workers are on in production** (`AGENT_EXPANSION_ENABLED=true`, owner, 2026-09-15).
- **The production engine is llama.cpp**, serving `unsloth/qwen3.8-flash-next` (UD-IQ4_XS, 131,072-token context). ADR-0152's MTPLX delay figures do not describe production.
- **The skill-bodies duplication is fixed** (FRE-1529), so the primary's prompt is smaller than during the ADR-0152 probes.

### Today the planner runs on almost no turn

`_apply_matrix` (`request_gateway/decomposition.py`) forces `SINGLE` for `CONVERSATIONAL` (`conversational_always_single`) and `TOOL_USE` (`tool_use_single`). It also forces `SINGLE` for `ANALYSIS` and `PLANNING` when the message is short. These four types are almost all traffic. In July and August 2026, 432 of 444 typed turns in `route_traces` (97%) were of these types: conversational 360 (81%), tool use 39 (9%), analysis 25 (6%), planning 8 (2%). A further 55 rows carry no task type.

FRE-1498 showed that the planner, when asked, routes better than this ladder. It declined all seven commands, greetings, tool requests and follow-ups, and it expanded all twelve research questions. The ladder had labelled ten of those twelve `conversational`.

### The measurement of 2026-10-03

Three designs were measured on the production llama.cpp. Appendix A holds the method, the per-fixture tables and the instrument notes.

- **A — isolated planner.** The production planner call: the briefing system prompt with ADR-0152's decline rule, and a user message with the rendered history and the query. It shares no prefix with the primary's prompt.
- **B — fork planner.** The primary's own request (system prompt, 16 tools, history, this turn's memory and skill bodies), with a planner instruction appended as a final message, `tool_choice: none` and JSON output. The planner sees everything the primary sees.
- **C — the primary decides.** The primary's own request with one more tool, `start_workers`. The primary chooses during its own turn. There is no extra call.

**Method, in brief.** The primary's real request bodies came from the eval gateway, built from `origin/main` with primitive tools on, as in production. The gateway's model endpoint was a recording stub, so the gateway made no model call. The bodies were then replayed direct to the production llama.cpp. The fixtures are the 19 of FRE-1498 plus 8 new two-turn fixtures. The 8 new fixtures are four histories, each with one follow-up that must expand and one that must decline. 26 fixtures are scored, and `c5_noverb` is excluded as borderline. The rates below use 95% Wilson intervals.

**Decision quality**

| Design | Draws | Decline-correct | Expand-correct | Follow-ups: decline / expand |
|---|---|---|---|---|
| A, thinking off | 3 | 30/30 = 100% (89–100%) | 46/48 = 96% (86–99%) | 12/12 / 12/12 |
| A, thinking on (production setting) | 1 | 10/10 = 100% (72–100%) | 15/16 = 94% (72–99%) | 4/4 / 4/4 |
| B, thinking off | 3 | 25/30 = 83% (66–93%) | 43/48 = 90% (78–95%) | 12/12 / 12/12 |
| C, first action, thinking on | 3 | 30/30 = 100% (89–100%) | 1/48 = 2% (0–11%) | 12/12 / 1/12 |

**Whole-turn time to first token**, one draw per fixture, p50 / p90. The time is the planner call plus the primary's first streamed token.

| Path | Turns that must decline | All turns |
|---|---|---|
| SINGLE today (primary alone) | 6.0 / 14.6 s | 6.1 / 14.6 s |
| A, thinking off | 6.8 / 8.3 s | 14.4 / 18.5 s |
| B, thinking off | 9.0 / 26.2 s | 23.6 / 29.2 s |
| A, thinking on (production setting) | — | 26.2 / 36.9 s |

**Long histories, design A thinking off**, timing only:

| Rendered history | Planner prompt tokens | Cold call | Next turn, history extended |
|---|---|---|---|
| 8,000 chars | 2,765 | 6.0 s | 5.3 s (717 cached) |
| 30,000 chars | 8,646 | 19.4 s | 5.4 s (6,598 cached, 2,071 new) |
| 60,000 chars | 16,684 | 40.2 s | 5.9 s (14,636 cached, 2,071 new) |

What the measurement settles:

1. **Thinking off does not make the planner worse, and it makes it much faster.** A thinking-off and A thinking-on are not shown different on either rate, because the intervals overlap. A declined planner call took 0.8 s at the p50 with thinking off, against 5.3 s with thinking on. Over all calls, the p50 is 6.8 s against 20.1 s. FRE-1430 F16 found the same direction (11.3 s to 5.1 s).
2. **The fork does not pay for itself.** B was expected to be cheap, because its prefill is work the primary does anyway. The primary did start in 0.2 s after B. But B's planner instruction comes after this turn's new content, so the engine prefills it again on every call. B also declined worse: it expanded "Please run the nfl-prediction tool" in 3 of 3 draws, with a plan to collect NFL schedules. Its full context, with the tools and the date, seems to pull it toward doing the work.
3. **The primary does not delegate.** Under C, the primary's first action was `web_search` 43 times, `search_memory` 18, text 9, `bash` 5, `recall_personal_history` 2, and `start_workers` 1. This confirms FRE-1498 C4.
4. **A did not push the primary's cache out on any of the 27 fixtures** when the run held one session and no worker. The primary's `cache_n` stayed at 6,540 tokens. Eviction does occur: after the 27-call A thinking-on arm, the primary's prefix was gone (`cache_n` 0, a 20 s cold prefill).
5. **A long-session planner call costs about 5–6 s even with a warm cache.** Every warm extension prefilled about 2,071 tokens, not only the new text. The primary shows the same pattern (`prompt_n` p50 2,157). This model mixes attention types, and llama.cpp seems to restore its cache only from saved checkpoints. That cause is an inference, not a measurement. A cold call scales with the history: 40.2 s at 60,000 characters.

### What this ADR does not decide

- **What expansion is worth.** Whether a fan-out answer beats the single lane is FRE-1495's question. The owner scoped this decision to the choice axis on FRE-1502 (2026-09-13).
- **Worker round budgets.** They belong to FRE-1487 by the owner's decision ("accuracy and quality are what is need"). This ADR changes no value of `sub_agent_rounds_by_thoroughness`.
- **A worker-side memory tool.** ADR-0147 rejected it, FRE-1463 measured it inert, and that holds.
- **Which engine or model serves production.**

---

## Decision

### D1 — The planner's inputs, and nothing else

The planner call carries exactly four inputs, in this order:

| # | Input | Bound | Token cost, measured on Flash-Next llama.cpp | Cache behaviour |
|---|---|---|---|---|
| 1 | System prompt: schema, rules, briefing rules, decline rule (D2) | Static text | 574 tokens (cached on every measured call) | Stable across turns |
| 2 | Rendered conversation history | `planner_history_max_chars` = 60,000 chars | about 3.7 chars per token, so at most about 16,100 tokens | Extends forward across turns until the trim starts |
| 3 | Memory digest (D5) | 300 estimated tokens (ADR-0147 D4) | At most about 300 tokens. Not measured: the eval graph was empty | Changes every turn |
| 4 | The current message, as `Strategy: HYBRID\nQuery: …` | The message itself | 33–148 tokens over the 19 single-turn fixtures (prompt total 607–722) | Changes every turn |

The user message puts the stable parts first: history, then digest, then query. A change in the digest or the query never breaks the cached history before it.

The planner receives no tools array, no skill bodies and no primary system prompt. Those are design B's inputs, and B lost (Context, point 2).

`planner_brief_mode=briefing` becomes the only behaviour, and the `current` path and its setting are removed. Production already runs `briefing`. The `current` mode is the input set that FRE-1517 F6 measured at 14/53 carry-through.

**Against FRE-1138.** The planner runs once per turn, before the tool loop, so its input is not within-turn growth. Its worst case is about 16,100 + 574 + 300 tokens plus the message, and D6 records the real figure per turn.

### D2 — The planner decides expansion for the four register types

`_apply_matrix` routes `CONVERSATIONAL`, `TOOL_USE`, `ANALYSIS` and `PLANNING` to `HYBRID` with the reason `planner_asked`, at every complexity. This deletes `conversational_always_single`, `tool_use_single`, `analysis_simple` and `planning_simple`. It replaces the routing half of FRE-1394, including the `TOOL_USE` forcing that FRE-1394 kept. FRE-1394's cap half (the per-type iteration cap) stays with FRE-1394.

These stay as they are:
- `MEMORY_RECALL` and `SELF_IMPROVE` keep `SINGLE`. They rest on the shape of the work, not on the register of the request.
- `DELEGATION` keeps its current branches.
- The resource forcings `expansion_denied` and `zero_budget` still force `SINGLE` before the matrix runs.

The rest of D2 carries ADR-0152 D1 unchanged:
- The executor passes a typed flag, `planner_may_decline`, that is true exactly when `gw.decomposition.reason == "planner_asked"`. The controller reads this flag and no other signal.
- When the flag is true, the system prompt carries the decline rule, with the FRE-1498 text unchanged, and the schema admits `SINGLE`.
- Validation: `SINGLE` with no tasks is a valid decline. `SINGLE` with tasks is a planner failure. `HYBRID` or `DECOMPOSE` with at least one valid task is a valid expansion. Anything else is a planner failure.
- A decline returns the turn to the ordinary tool loop. It dispatches no worker and appends no synthesis message.
- A planner failure (invalid output, a timeout or an exception) on a `planner_asked` turn also returns the turn to the tool loop. The fallback planner does not run. When the flag is false, the fallback planner keeps its role.

### D3 — The bound on expansion

1. At most min(3, `governance.expansion_budget`) workers per turn (FRE-1382).
2. A `DECOMPOSE` answer on a `planner_asked` turn is dispatched as `HYBRID` under the same cap. The stored plan and all telemetry carry the effective strategy.
3. No round budget changes. The prompt renders the round budget that binds, and under `briefing` it already does: "(20 tool round(s) each)" while every level is 20.

### D4 — The planner call runs with thinking off

The planner call uses the session's primary deployment, with that deployment's default sampling and its thinking disabled. On the local Flash-Next binding, this is the measured configuration: temperature 1.0, top_p 0.95, top_k 20, min_p 0, presence penalty 0, and `chat_template_kwargs.enable_thinking: false`. It is not the `worker` mode, which also changes the sampling (temperature 0.7, presence penalty 1.5).

The setting lives in the deployment's catalog entry as a `planner` mode in `config/models.yaml`, and the planner call requests that mode. A deployment with no `planner` mode runs the planner in its default mode, and D6 records that the planner ran with thinking on. D7 decides whether such a deployment qualifies.

This amends ADR-0152 D5 ("no planner-specific mode"). ADR-0152 tested effort levels, never thinking off. The planner keeps the primary's model and temperature. No planner temperature is adopted.

### D5 — The memory digest, carried from ADR-0147

ADR-0147 D1 to D7 carry into this ADR unchanged in substance:
- **D1:** a digest of at most one line per item, built from the primary renderer's own selection and text helpers, so no fact reaches the planner that was not eligible for the primary's render.
- **D2:** no memory item ever enters `SubAgentSpec.context`, and no rendered memory section reaches a worker. A fact may travel inside a goal, by design, and nothing bounds that mechanically.
- **D3:** the plan's `memory_relevance` field (`used`, `none_relevant`, `unstated`, `not_applicable`).
- **D4:** at most 20 items, 120 characters per line and 300 estimated tokens.
- **D5:** one terminal planner event on every path.
- **D6:** no enrichment write path. **D7:** no further memory tool for a worker.

Two changes follow from this ADR:
- **Placement.** The digest goes in the user message after the history and before the query (D1). ADR-0147 put it in the system prompt, where its per-turn change breaks the cached prefix.
- **A decline is a plan.** On a valid decline, `memory_relevance` is still the planner's judgment of the digest, so a decline can carry `used` (for example, memory shows that the request is already answered). D6's matrix applies unchanged.

The digest's effect on the decline decision is unmeasured. AC-8 measures it before the umbrella closes.

### D6 — The planner's decision, inputs and delay are recorded

Every turn gains these durable fields, in `route_traces` and in the ES projection:

| Field | Values | Set when |
|---|---|---|
| `planner_decision` | `declined`, `expanded`, `failed`, or null | null when the planner did not run |
| `planner_deployment` | the deployment key of the planner call | the planner ran |
| `planner_thinking` | true or false, as sent | the planner ran |
| `planner_duration_ms` | the planner call's wall clock | the planner ran |
| `planner_prompt_tokens`, `planner_completion_tokens` | the engine's usage counts | the planner ran |
| `planner_history_chars` | the rendered history length | the planner ran |
| `expansion_budget` | `governance.expansion_budget` for the turn | always |
| `synthesis_appended` | true when a synthesis message was added | always |
| `first_token_ms` | from request receipt to the first token streamed to the user | always |

`first_token_ms` is new. Nothing records it today: `latency_total_ms` and `latency_breakdown` are null on all 12 conversational turns since 2026-09-15. It is the measure the owner experiences, and AC-5 compares against it. **It ships before the routing change**, so that a pre-change baseline of `SINGLE` turns exists.

The fields need an idempotent migration under `docker/postgres/migrations/` and the matching columns in `docker/postgres/init.sql`, plus the row type, ledger, assembler and ES mapping.

A decline reads as a decline in every derived view, as ADR-0152 D4 states:
- `_LABEL_LIE_SQL` excludes `planner_asked` rows whose `planner_decision` is `declined` or `failed`.
- `_resolve_topology` returns `primary` for those rows.
- `ctx.expansion_strategy` is cleared on a decline or a failure.

### D7 — A planner configuration qualifies on a committed probe

The probe used for Appendix A must be committed under `scripts/eval/` before the routing change ships. That means the capture step, the replay arms, both fixture sets, the scorer and the long-history arm. A configuration is the engine, model, quant and planner mode. It qualifies when the probe gives:

| Criterion | Threshold | Measured, A thinking off |
|---|---|---|
| Decline-correct | at least 28 of 30 | 30/30 |
| Expand-correct | at least 43 of 48 | 46/48 |
| Each follow-up direction | at least 11 of 12 | 12/12 and 12/12 |
| Plans that fail to parse | 0 | 0 |
| Declined planner call, single-turn fixtures | p50 at most 2 s | 0.8 s |
| Planner call, 60,000-char history, extended | at most 10 s | 5.9 s |
| Planner call, 60,000-char history, cold | at most 60 s | 40.2 s |

The decision thresholds sit two to three draws below the measured counts. The delay thresholds sit at about twice the measured values.

**Where qualification binds.** This carries ADR-0152 D6:
- The default local primary binding must qualify before a new deployment replaces it.
- Other deployments a user can select for a session, today the OVH `Qwen3.8-27B` and `claude_sonnet`, are probed and the result is reported to the owner.
- For a selectable deployment that fails, the owner decides: a gate on D2 for that deployment, or removal from selection.

A change to the planner prompt is a routing change and needs a new probe run.

---

## Alternatives Considered

### Option 1: The fork planner (design B)

**Description:** The planner call is the primary's own request with a planner instruction appended. It sees the memory section, the skills, the tools and the history exactly as the primary does, so ADR-0147's digest is unnecessary. Its prefill is work the primary does anyway.

**Pros:**
- Full input fidelity at no new rendering code.
- The primary's next call is almost free: 0.2 s to first token, with 5 new tokens.

**Cons:**
- Decline-correct 25/30 = 83% (66–93%), against A's 30/30. It expanded the command fixtures (`susan_1_run_tool` 3 of 3).
- On declined turns, slower than A: 9.0 / 26.2 s to first token against 6.8 / 8.3 s. The planner instruction sits after this turn's new content, so it is prefilled on every call.

**Why Rejected:** It is worse on the decision that most traffic needs, and slower.

### Option 2: The primary decides through a tool (design C)

**Description:** No planner call. The primary holds a `start_workers` tool and calls it when it judges that it needs workers. This matches ADR-0142 D3's "demonstrated need".

**Pros:**
- No added delay on any turn.
- The decision uses everything the primary sees.

**Cons:**
- Expand-correct 1/48 = 2% (0–11%). The primary researches the question itself.
- The measurement stopped at the first action, so a later `start_workers` call is not measured. 43 of 48 research questions began with `web_search`. So this limit does not rescue the design.

**Why Rejected:** It does not expand.

### Option 3: Keep the planner's thinking on (ADR-0152 D5)

**Description:** The planner runs in the primary's default mode, with thinking at the server's base effort. This is production's setting today.

**Pros:**
- No new mode in the catalog.

**Cons:**
- Not shown better: 10/10 and 15/16 against thinking off's 30/30 and 46/48.
- The call's p50 is 20.1 s against 6.8 s. On declines, 5.3 s against 0.8 s.

**Why Rejected:** It adds about 13 s per call for no measured gain.

### Option 4: No memory for the planner

**Description:** ADR-0147 is superseded without its digest. The planner sees the history and the message only.

**Pros:**
- No digest code, no new failure surface.
- The measured configuration is exactly this, so the D7 thresholds need no new run.

**Cons:**
- The history carries what the user said in this session, not what the system knows from earlier sessions. The digest is the one input that the history lacks.

**Why Rejected:** The owner chose the digest on 2026-10-03. Its cost is at most 300 tokens, after the cached history. AC-8 checks that it does not damage the decision.

### Option 5: Remove the forcings and let the complexity matrix decide (FRE-1394 as scoped)

**Description:** Delete the `CONVERSATIONAL` branch, so those turns fall through to the complexity matrix.

**Pros:** No planner call on short turns.

**Cons:** In FRE-1498 S2, 17 of 18 fixtures resolved exactly as before, because every message under 15 words is `SIMPLE`, and `SIMPLE` maps to `SINGLE`.

**Why Rejected:** It does not reach the short requests it was written for. This is ADR-0152 Option 1, unchanged.

### Option 6: Keep the ladder

**Description:** Leave the matrix as it is.

**Pros:** No added delay.

**Cons:** Ten of twelve research questions go to `SINGLE` (FRE-1498 C1), and the owner's "Research …" prefix stays the only way to reach expansion.

**Why Rejected:** It keeps the defect that ADR-0142 and FRE-1498 measured.

---

## Consequences

### Positive Consequences

- Research questions reach expansion by what they ask, not by a keyword prefix.
- The planner decides with the conversation and with a bounded view of memory, so a follow-up such as "OK, now research the second option" has the turn it refers to.
- A failed planner no longer dispatches workers on turns that nobody chose to expand.
- The turn's time to first token is recorded for the first time (D6), so the delay is measured where the owner experiences it.
- The planner call drops from about 20 s to about 7 s at the p50 (D4), on turns that already reach the planner today.

### Negative Consequences

- **Every turn of the four types pays for one planner call.** On a short declined turn this adds about 0.8 s at the p50. On a long session it adds about 5–6 s with a warm cache, and up to about 40 s cold at 60,000 characters of history. A turn that expands also pays for writing the plan: 318 completion tokens at the p50, 695 at the most.
- **The same message can route differently on a retry.** The planner samples at temperature 1.0. Under A thinking-off, `c4_deliverable` and `c4_process` each declined in 1 of 3 draws.
- **Cache eviction is real under load.** One session at a time showed no eviction. After 27 distinct planner prompts, the primary's prefix was gone, at a cost of a 20 s cold prefill. An expansion turn, with its workers on the same server, is the load case. It was not measured.
- **The routing policy depends on the selected deployment.** The OVH `Qwen3.8-27B` and `claude_sonnet` are unmeasured as thinking-off planners.
- **The planner prompt is part of routing.** A prompt change needs the probe (D7).
- **The change carries a Postgres migration and an ES mapping change** (D6), so its deployment is in the stricter class.
- **The digest is new exposure, carried from ADR-0147 D2.** A memory-derived fact can reach a worker inside a goal, and a worker that holds `web_search` can carry it into a search query. Nothing bounds that mechanically.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| The delay on long sessions is higher than measured, because the cache misses more often under real load | High | AC-5 compares `first_token_ms` per history size against the pre-change baseline. D6 records prompt and history size, so a cold-cache pattern is visible. |
| The fixtures overfit: 27 messages, 10 of them decline-type | Medium | AC-4 scores the planner against owner labels on real turns, with separate rates. |
| The digest changes declines for the worse | Medium | AC-8 runs the probe with a seeded digest before the umbrella closes. |
| A selectable deployment expands most turns | Medium | AC-10 probes each one, and D7 hands a failure to the owner. D3's cap bounds the cost meanwhile. |
| A planner failure costs the call's time on the single lane | Low | D2 sends a failure to the tool loop, so the cost is the call and nothing else. AC-3 checks it. |

---

## Implementation Notes

**Files:**
- `src/personal_agent/request_gateway/decomposition.py`: the `planner_asked` branch.
- `src/personal_agent/orchestrator/expansion_controller.py`: the decline rule and schema, validation, the decline and failure returns, `DECOMPOSE` normalization, `briefing` as the only path, the digest's place in the user message, the planner mode request, and the terminal planner event.
- `src/personal_agent/orchestrator/executor.py`: `planner_may_decline`, the return to `LLM_CALL` without a synthesis message, clearing `ctx.expansion_strategy`, the digest builder (ADR-0147 D1), and `first_token_ms`.
- `src/personal_agent/orchestrator/expansion_types.py`: `PlannerMemoryDigest` and `ExpansionPlan.memory_relevance`.
- `config/models.yaml`: a `planner` mode on each local primary deployment.
- `src/personal_agent/config/settings.py`: remove `planner_brief_mode`.
- `src/personal_agent/observability/route_trace/` (types, assembler, ledger and `_LABEL_LIE_SQL`), `src/personal_agent/observability/topology/`, `docker/postgres/init.sql`, `docker/postgres/migrations/`, and the ES index template.
- `scripts/eval/`: the probe of D7.

**Order:**
1. The committed probe (D7), because AC-8 and AC-10 need it.
2. The decision record with `first_token_ms` (D6). It ships first and runs long enough to give a pre-change baseline.
3. The digest (D5).
4. The planner mode (D4).
5. The routing change (D2, D3), after 1 to 4.
6. Qualification of the selectable deployments (D7).

**Eval-stack hazard found during the measurement.** Running the eval compose files with `-p seshat` from a worktree recreated production's `cloud-sim-searxng` with the worktree's bind mount. That cost about 7 minutes of degraded web search on 2026-10-03. The probe of D7 must run its gateway as a separate container or compose project, never under `-p seshat`.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR. They are adjudicated on FRE-1537 after the implementation chain deploys, not at the merge of this ADR. "The window" is a deployed window of at least 200 turns after the routing change. "The baseline" is the window of at least 100 turns between the D6 deploy and the routing change.

- **AC-1 — The four types reach the planner.** · **Check:** a deterministic test of `_apply_matrix` over every combination of the four types and every `Complexity` value asserts `HYBRID` with `planner_asked`. The same test asserts that `MEMORY_RECALL`, `SELF_IMPROVE`, `DELEGATION` and the two resource forcings are unchanged. Live: `route_traces` over the window, grouped by `task_type` and `decomposition_reason`. · *Fails if* any test case differs, or any turn of the four types with expansion permitted records one of the four deleted reasons, or a null `planner_decision`.

- **AC-2 — A decline is honoured and reads as a decline.** · **Check:** a test drives a `planner_asked` turn whose planner stub declines, and asserts no worker, no synthesis message, and the next state `LLM_CALL`. Live, over the window: rows with `planner_decision = declined`. · *Fails if* the test finds a worker or a synthesis message, or any live declined row has `sub_agent_count > 0`, `synthesis_appended = true`, a match on the label-lie predicate, or an ES `topology` other than `primary`.

- **AC-3 — A failed planner does not expand.** · **Check:** three tests drive a `planner_asked` turn with a planner stub that returns invalid JSON, times out, or raises. Each asserts zero workers, no `fallback_planner_used` event, `planner_decision = failed`, and an answer through `LLM_CALL`. A fourth test asserts that a turn with `planner_may_decline` false still reaches the fallback planner. Live: rows with `planner_decision = failed`. · *Fails if* any of the first three tests dispatches, the fourth does not reach the fallback, or any live failed row has `sub_agent_count > 0`.

- **AC-4 — On real traffic, the planner agrees with the owner, in each direction.** · **Check:** the owner labels `planner_asked` turns from the window as "should expand" or "should not", blind to the decision. Each message is read from its Captain's Log capture (`user_message`, joined on `trace_id`). The sample holds at least 30 turns the owner labels "should not" and at least 15 labelled "should expand". Decline-correct and expand-correct are reported separately, each with a 95% Wilson interval. Real traffic is small (18 real chat turns from 2026-09-15 to 2026-10-02) and mostly declines. So the "should expand" stratum may be filled with research questions the owner sends live during the window, marked as such. · *Fails if* decline-correct is below 27 of 30 (90%) or expand-correct below 12 of 15 (80%), if either rate is computed from fewer turns than stated, or if one pooled agreement figure is reported in place of the two.

- **AC-5 — The delay the owner experiences is the one measured.** · **Check:** `first_token_ms` on declined `planner_asked` turns in the window, against `SINGLE` turns of the same task types in the baseline, on the same deployment. Both are split at `planner_history_chars` 8,000 (for baseline turns, the history the planner would have received). · *Fails if* the median added delay exceeds 3 s below 8,000 characters or 10 s at or above it, if the baseline holds fewer than 30 turns in either split, or if the comparison uses `planner_duration_ms` in place of `first_token_ms`.

- **AC-6 — The inputs stay inside their bounds.** · **Check:** over the window, `planner_history_chars` and `planner_prompt_tokens` per planner call, and the digest fields of ADR-0147 D5. · *Fails if* any call records `planner_history_chars` above 60,000, a digest above 20 items, 120 characters per line or 300 estimated tokens, or a planner prompt above 574 + 16,100 + 300 tokens plus the message's own tokens.

- **AC-7 — Thinking off is what ran.** · **Check:** over the window, `planner_thinking` and `planner_completion_tokens` on the local binding. · *Fails if* any local planner call records `planner_thinking = true`, or the p50 `planner_completion_tokens` of declined calls exceeds 40. The measured p50 is 13 tokens, at most 19. A decline that thinks spends hundreds of tokens, so a thinking leak cannot pass this check.

- **AC-8 — The digest informs the plan and does not damage the decision.** · **Check, two parts.** *Seeded:* ADR-0147 AC-2's paired integration test (a coined token in a relevant digest must reach a goal, and an unrelated digest must return `none_relevant` and leak nothing), run on the D2 prompt. *Probe:* the committed probe re-run with an unrelated seeded digest on every fixture. · *Fails if* the seeded test fails, or the probe with the digest misses any D7 decision threshold.

- **AC-9 — The bound holds and no round budget moved.** · **Check:** over the window, `sub_agent_count` against `expansion_budget`. Then compare `sub_agent_rounds_by_thoroughness` in the deployed configuration at the end of the chain with its value on 2026-10-03, which is unset. · *Fails if* any turn records more than min(3, `expansion_budget`) workers, or the round budget value differs.

- **AC-10 — The deployments a user can select are probed.** · **Check:** the committed probe, run from the repo, on the local primary binding and on each selectable primary deployment in its planner configuration. The scorer output is posted on FRE-1537 against the D7 thresholds. · *Fails if* the probe cannot run from the repo against a deployment, the local binding misses a threshold, or a selectable deployment misses one and FRE-1537 holds no owner decision on it. The miss itself does not fail this criterion.

---

## References

- FRE-1537 (umbrella), FRE-1502 (ADR-0152, with master's review and the seat's reply of 2026-09-14), FRE-1470 (ADR-0147)
- FRE-1498 — `docs/research/2026-09-12-fre-1498-what-the-model-chooses-when-nothing-forces-it.md` (C1, C4, C6, S2)
- FRE-1517 — `docs/research/2026-09-15-fre-1517-primary-model-long-sessions.md` (F6, F7, F11)
- FRE-1521 — the briefing planner and its setting
- FRE-1430 F16 — `docs/research/2026-09-06-fre-1430-provider-dialect-matrix.md`
- FRE-1487 — worker round limits, the owner's
- FRE-1394 — its cap half stays, its routing half is replaced by D2
- FRE-1138 — within-turn growth
- FRE-1529 — the skill-bodies duplication, fixed
- FRE-1471, FRE-1472 — ADR-0147's implementation tickets
- ADR-0036 — Expansion Controller (D1, D3, D5)
- ADR-0142 — Capability Is Not a Property of Register (D1, D3)
- ADR-0145 — Two Nouns and the Dialect Between Them (modes, D1)
- ADR-0150 — The Worker Returns Data, Not Prose (worker types and thoroughness)
- `src/personal_agent/request_gateway/decomposition.py`, `src/personal_agent/orchestrator/expansion_controller.py`, `src/personal_agent/orchestrator/executor.py`, `config/models.yaml`

---

## Status Updates

### 2026-10-03 - Proposed
**Changed By:** `adr` seat (FRE-1537)
**Reason:** Drafted on the owner's "Draft it" after the 2026-10-03 measurement, the choice of design A with thinking off, and the choice to keep the ADR-0147 digest.

---

## Appendix A — The planner measurement, 2026-10-03

### A1. Design

- **Engine.** The production llama.cpp, reached at `127.0.0.1:8600`, serving `unsloth/qwen3.8-flash-next` UD-IQ4_XS with a 131,072-token context. One call at a time, streamed with usage, as the gateway streams.
- **Request capture.** The eval gateway image was built from `origin/main`. The gateway ran with primitive tools and prefer-primitives on, as production does, and with expansion off. Its model endpoint was a recording stub that returned a scripted reply and made no model call. Each fixture ran as a real `/chat` turn. A two-turn fixture ran turn 1 with the scripted assistant reply, then turn 2 in the same session. The primary's request body for the final turn was saved. It held 16 tools, a 9,779-character system prompt, and the skill bodies in the last user message. The primary's first call was 8,697 prompt tokens at the p50.
- **Design A prompts.** Rendered inside the eval container with the production functions `_build_planner_system_prompt(…, "briefing")` and `_render_planner_history(…, 60000)`. ADR-0152's decline rule was inserted as the first rule, with the schema admitting `SINGLE`, exactly as the FRE-1502 probe inserted it.
- **Design B tail.** A final user message: the design A system prompt, then "The query is the user's latest message above. Do not answer it and do not call a tool. Produce the JSON plan." Sent with the same tools, `tool_choice: none`, and `response_format: json_object`.
- **Design C tool.** `start_workers`, appended last to the tools array. Its description restates the decline rule. The stream was closed at the first action: the first tool call, or the first content text.
- **Sampling.** The captured primary sampling for every arm: temperature 1.0, top_p 0.95, top_k 20, min_p 0, presence penalty 0, repetition penalty 1.0. Thinking off is `chat_template_kwargs.enable_thinking: false`. Thinking on is the server default, which is the production planner setting. The thinking-off arms recorded 0 reasoning characters on every call.
- **Fixtures.** The 19 of `scripts/eval/fre1498/fixtures.yaml`, with the expected decisions of ADR-0152 Appendix A: decline for `susan_1`–`susan_4`, `greeting` and `tool_logs`, expand for twelve, and `c5_noverb` excluded. Then 8 new two-turn fixtures: four histories (a gas-boiler replacement, a Lisbon plan, a photo-editing laptop, French electricity tariffs), each with one follow-up that must expand and one that must decline.
- **Timing protocol (T).** Per fixture, one draw:
  1. Prime the previous-turn prefix: the request without its last user message, `max_tokens` 1.
  2. T1: the primary alone, `max_tokens` 1.
  3. Prime again, then design B's planner call, then the primary.
  4. Prime again, then design A's thinking-off planner call, then the primary.

  The engine's `timings.cache_n` and `prompt_n` are recorded on every call.
- **Long-history arm.** Design A thinking-off with synthetic histories built from the four fixture conversations, at 8,000, 30,000 and 60,000 characters. Each size ran a cold call, then a primary call, then the next turn's planner call with one more exchange appended.

### A2. Per-fixture decisions (draws that did not expand / draws)

For C, the count is draws whose first action was not `start_workers`.

| Fixture | Expected | A off | A on | B off | C on |
|---|---|---|---|---|---|
| `boiler_decline` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `greeting` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `laptop_decline` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `lisbon_decline` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `susan_1_run_tool` | decline | 3/3 | 1/1 | 0/3 | 3/3 |
| `susan_2_able_to_run` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `susan_3_thought_i_asked` | decline | 3/3 | 1/1 | 1/3 | 3/3 |
| `susan_4_build_and_run` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `tariffs_decline` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `tool_logs` | decline | 3/3 | 1/1 | 3/3 | 3/3 |
| `boiler_expand` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `c1_tool_imperative` | expand | 0/3 | 0/1 | 3/3 | 3/3 |
| `c2_noverb` | expand | 0/3 | 0/1 | 2/3 | 3/3 |
| `c2_verb` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `c3_paragraph` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `c3_terse` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `c4_deliverable` | expand | 1/3 | 1/1 | 0/3 | 3/3 |
| `c4_process` | expand | 1/3 | 0/1 | 0/3 | 3/3 |
| `c5_verb` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `gpsr_tuna` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `heatpump` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `laptop_expand` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `lisbon_expand` | expand | 0/3 | 0/1 | 0/3 | 2/3 |
| `mallorca` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `regulatory` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `tariffs_expand` | expand | 0/3 | 0/1 | 0/3 | 3/3 |
| `c5_noverb` | excluded | 0/3 | 0/1 | 1/3 | 3/3 |

### A3. Per-fixture time to first token (protocol T, one draw)

| Fixture | Expected | SINGLE | B planner + primary | A planner + primary | primary `cache_n` after A |
|---|---|---|---|---|---|
| `boiler_decline` | decline | 5.5 s | 7.6 s | 7.3 s | 6540 |
| `greeting` | decline | 4.9 s | 7.4 s | 5.7 s | 6540 |
| `laptop_decline` | decline | 6.9 s | 9.0 s | 8.3 s | 6540 |
| `lisbon_decline` | decline | 7.0 s | 9.0 s | 8.3 s | 6540 |
| `susan_1_run_tool` | decline | 6.0 s | 22.6 s | 6.8 s | 6540 |
| `susan_2_able_to_run` | decline | 6.0 s | 18.1 s | 6.9 s | 6540 |
| `susan_3_thought_i_asked` | decline | 5.6 s | 26.2 s | 6.6 s | 6540 |
| `susan_4_build_and_run` | decline | 5.2 s | 7.7 s | 6.0 s | 6540 |
| `tariffs_decline` | decline | 14.6 s | 16.7 s | 16.0 s | 6540 |
| `tool_logs` | decline | 24.5 s | 26.9 s | 0.9 s | 15299 |
| `boiler_expand` | expand | 14.8 s | 47.4 s | 28.2 s | 6540 |
| `c1_tool_imperative` | expand | 6.3 s | 8.6 s | 15.7 s | 6540 |
| `c2_noverb` | expand | 6.1 s | 8.4 s | 18.3 s | 6540 |
| `c2_verb` | expand | 6.1 s | 26.6 s | 6.8 s | 6540 |
| `c3_paragraph` | expand | 6.3 s | 31.6 s | 17.1 s | 6540 |
| `c3_terse` | expand | 5.9 s | 23.6 s | 16.5 s | 6540 |
| `c4_deliverable` | expand | 5.9 s | 23.6 s | 12.2 s | 6540 |
| `c4_process` | expand | 5.7 s | 24.9 s | 12.5 s | 6540 |
| `c5_verb` | expand | 6.0 s | 23.3 s | 13.0 s | 6540 |
| `gpsr_tuna` | expand | 5.8 s | 21.0 s | 14.4 s | 6540 |
| `heatpump` | expand | 6.1 s | 28.6 s | 18.7 s | 6540 |
| `laptop_expand` | expand | 6.9 s | 26.2 s | 15.8 s | 6540 |
| `lisbon_expand` | expand | 7.0 s | 25.4 s | 18.5 s | 6540 |
| `mallorca` | expand | 6.1 s | 29.2 s | 18.7 s | 6540 |
| `regulatory` | expand | 5.8 s | 26.2 s | 16.1 s | 6540 |
| `tariffs_expand` | expand | 24.5 s | 52.3 s | 16.1 s | 15134 |
| `c5_noverb` | excluded | 6.0 s | 25.6 s | 16.5 s | 6540 |

### A4. Instrument notes and limits

- **No memory, no MCP tools.** The eval graph was empty, so the captured prompts carry no memory section. The MCP gateway is a production container and stayed off, so the prompts carry no MCP tools. Production's first primary call reaches about 25,000 prompt tokens on a long session (a 2026-09-28 capture: 25,040). The eval prompts were about 8,700.
- **A cache artifact on two rows.** On `tool_logs` and `tariffs_expand`, the primary after design A found the full prompt cached from the preceding B step (`cache_n` 15,299 and 15,134). Those two "A planner + primary" figures are optimistic. Both prompts were larger than the others, and their SINGLE figures (24.5 s) include a colder prefill.
- **Cache restore granularity.** Every warm extension prefilled about 2,071 tokens (planner) or 2,157 (primary, p50), not only the new text. The cause given in Context point 5 is an inference.
- **C stops at the first action.** A `start_workers` call later in the primary's loop is not measured.
- **Sample size.** 10 decline-type fixtures and 16 expand-type fixtures. The intervals are wide, and AC-4 exists for that reason.
- **Real-traffic weights.** The 2026-07/08 mix in Context is the weight to apply. At 90% decline traffic, A thinking-off's weighted agreement is 100%, and B's is 84%. These weighted figures do not decide anything. The two separate rates do.
- **Raw rows.** One JSON line per call, kept in the `adr` worktree's git-ignored `telemetry/archive/fre1537-planner-probe/`, with the stub, the capture driver, the replay and analysis scripts, and the captured request bodies. They are not committed, because they are run output and the request bodies hold full prompts. D7 commits the probe.
