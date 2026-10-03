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

**Planner call time**, from the decision arms (Appendix A5):

| Design | Declined calls: n, p50 / p90 | Expanded calls: n, p50 / p90 | Completion tokens on declines: p50, max |
|---|---|---|---|
| A, thinking off | 32, 0.8 / 1.3 s | 46, 9.6 / 14.4 s | 13, 19 |
| A, thinking on (production setting) | 11, 5.3 / 11.5 s | 15, 25.5 / 31.2 s | 165, 646 |
| B, thinking off | 30, 8.4 / 16.7 s | 48, 24.6 / 37.1 s | 13, 13 |

**Whole-turn time to first token, paired per fixture.** Protocol T measured each fixture three ways on the same prompt: the primary alone (today's `SINGLE` turn), the planner then the primary, and the other planner then the primary. The added delay is the planner path minus the primary alone, for the same fixture. Two rows are excluded because the preceding step left their prompt cached (Appendix A4). One draw per fixture.

| Fixtures | SINGLE today, median | A thinking off, added: median (range) | B thinking off, added: median (range) |
|---|---|---|---|
| 9 that must decline | 6.0 s | 1.0 s (0.8–1.9 s) | 2.5 s (2.1–20.6 s) |
| 15 that must expand | 6.1 s | 10.3 s (0.7–13.4 s) | 19.1 s (2.3–32.7 s) |

**Long histories, design A thinking off**, timing only:

| Rendered history | Planner prompt tokens | Cold call | Next turn, history extended |
|---|---|---|---|
| 8,000 chars | 2,765 | 6.0 s | 5.3 s (717 cached) |
| 30,000 chars | 8,646 | 19.4 s | 5.4 s (6,598 cached, 2,071 new) |
| 60,000 chars | 16,684 | 40.2 s | 5.9 s (14,636 cached, 2,071 new) |

What the measurement settles, and what it does not:

1. **Thinking off showed no loss of quality, and it is much faster.** The sample detected no difference between A thinking-off and A thinking-on on either rate. The sample is small and the intervals are wide, so this is not a proof of equal quality. The delay difference is large and consistent: a declined call takes 0.8 s at the p50 against 5.3 s. FRE-1430 F16 found the same direction (11.3 s to 5.1 s).
2. **B showed no gain to pay for its cost.** B was expected to be cheap, because its prefill is work the primary does anyway. The primary did start in 0.2 s after B. But B's planner instruction comes after this turn's new content, so the engine prefills it again on every call. On the 9 clean declines, B added 2.5 s at the median against A's 1.0 s, and on every one of the 9 B added more than A. On quality, B's point estimates are lower on both rates, but the intervals overlap, so the sample does not show B worse overall. One pattern is consistent: B expanded "Please run the nfl-prediction tool" in 3 of 3 draws, with a plan to collect NFL schedules, where A declined it 3 of 3. Its full context, with the tools and the date, may pull it toward doing the work.
3. **The primary almost never delegates as its first action.** Under C, the primary's first action was `web_search` 43 times, `search_memory` 18, text 9, `bash` 5, `recall_personal_history` 2, and `start_workers` 1. Its expand-correct interval (0–11%) does not overlap A's (86–99%). A `start_workers` call later in the loop is not measured. This agrees with FRE-1498 C4.
4. **A did not push the primary's cache out on any of the 25 clean timing rows.** The run held one session and no worker, and the primary's `cache_n` stayed at 6,540 tokens on every clean row. Eviction does occur under more load: after the 27-call A thinking-on arm, the primary's prefix was gone (`cache_n` 0, a 20 s cold prefill). The load of an expansion turn, with its workers on the same server, was not measured.
5. **A long-session planner call costs about 5–6 s even with a warm cache.** Every warm extension prefilled about 2,071 tokens, not only the new text. The primary shows the same pattern (`prompt_n` p50 2,157). This model mixes attention types, and llama.cpp may restore its cache only from saved checkpoints. That cause is an inference, not a measurement. A cold call scales with the history: 40.2 s at 60,000 characters.

**The choice is operational, under uncertainty.** The sample shows A thinking-off at least as good as B and C on its point estimates and clearly better than C on expansion. It does not prove A's quality superior to B's. A is chosen because it is the fastest on the turns that most traffic produces, and because no measured quality difference favours another design.

### What this ADR does not decide

- **What expansion is worth.** Whether a fan-out answer beats the single lane is FRE-1495's question. The owner scoped this decision to the choice axis on FRE-1502 (2026-09-13).
- **Worker round budgets.** They belong to FRE-1487 by the owner's decision ("accuracy and quality are what is need"). This ADR changes no value of `sub_agent_rounds_by_thoroughness`.
- **A worker-side memory tool.** ADR-0147 rejected it, FRE-1463 measured it inert, and that holds.
- **Which engine or model serves production.**

---

## Decision

### D1 — The planner's inputs, and nothing else

The planner call carries exactly four inputs, in this order:

| # | Input | Enforced bound | Token cost, measured on Flash-Next llama.cpp | Cache behaviour |
|---|---|---|---|---|
| 1 | System prompt: schema, rules, briefing rules, decline rule (D2) | Static text, 2,439 chars at this date | 574 tokens (cached on every measured call) | Stable across turns |
| 2 | Rendered conversation history | The history budget below | About 3.7 chars per token on the measured content. 60,000 chars measured 16,069 tokens | Extends forward across turns until the trim starts |
| 3 | Memory digest (D5) | 20 items, 120 chars per line, 300 estimated tokens (ADR-0147 D4) | At most 2,400 chars. Not measured: the eval graph was empty | Changes every turn |
| 4 | The current message, as `Strategy: HYBRID\nQuery: …` | The total bound below | 33–148 tokens over the 19 single-turn fixtures (prompt total 607–722) | Changes every turn |

**The total bound.** The user message (history, digest and message together) is at most `planner_input_max_chars`, 64,000 characters. The parts are filled in a fixed order:
1. The message is never cut.
2. The digest is built to its own bounds.
3. The history receives what remains, at most `planner_history_max_chars` (60,000), and is trimmed whole-message from the oldest end, as today.
4. If the message and the digest alone exceed 64,000 characters, the planner does not run. The turn records `planner_decision = failed` with `planner_failure_reason = input_too_large`, and it returns to the tool loop as any planner failure does (D2).

The bound is in characters because characters are what the code controls before the call. The served token count varies by tokenizer, so it is recorded, not predicted: D6 records the engine's own `planner_prompt_tokens` for the whole prompt, and the character length of each input.

The user message puts the stable parts first: history, then digest, then query. A change in the digest or the query never breaks the cached history before it.

The planner receives no tools array, no skill bodies and no primary system prompt. Those are design B's inputs, and B was rejected (Option 1).

`planner_brief_mode=briefing` becomes the only behaviour, and the `current` path and its setting are removed. Production already runs `briefing`. The `current` mode is the input set that FRE-1517 F6 measured at 14/53 carry-through.

**Against FRE-1138.** The planner runs once per turn, before the tool loop, so its input is not within-turn growth. Its input is bounded at 2,439 + 64,000 characters, and D6 records the real size per turn.

### D2 — The planner decides expansion for the four register types

When the session's primary deployment declares a `planner` mode (D4), `_apply_matrix` routes `CONVERSATIONAL`, `TOOL_USE`, `ANALYSIS` and `PLANNING` to `HYBRID` with the reason `planner_asked`, at every complexity. This deletes `conversational_always_single`, `tool_use_single`, `analysis_simple` and `planning_simple` for those sessions. It replaces the routing half of FRE-1394, including the `TOOL_USE` forcing that FRE-1394 kept. FRE-1394's cap half (the per-type iteration cap) stays with FRE-1394.

**A deployment without a `planner` mode keeps today's routing.** The gateway resolves the session's primary deployment, the same one the `/chat` response reports as `primary_selection`, and passes `_apply_matrix` one boolean: whether that deployment declares a `planner` mode. Without one, the four types route exactly as they do today, with today's `decomposition_reason`. The separate field `planner_gate_reason` (D6) records `planner_mode_absent`. This fails closed: a deployment never reaches D2 with an unverified thinking control. D7 says when a deployment receives a `planner` mode.

These stay as they are:
- `MEMORY_RECALL` and `SELF_IMPROVE` keep `SINGLE`. They rest on the shape of the work, not on the register of the request.
- `DELEGATION` keeps its current branches.
- The resource forcings `expansion_denied` and `zero_budget` still force `SINGLE` before the matrix runs.

The rest of D2 carries ADR-0152 D1 unchanged:
- The executor passes a typed flag, `planner_may_decline`, that is true exactly when `gw.decomposition.reason == "planner_asked"`. The controller reads this flag and no other signal.
- When the flag is true, the system prompt carries the decline rule, with the FRE-1498 text unchanged, and the schema admits `SINGLE`.
- Validation: `SINGLE` with no tasks is a valid decline. `SINGLE` with tasks is a planner failure. `HYBRID` or `DECOMPOSE` with at least one valid task is a valid expansion. Anything else is a planner failure.
- A decline returns the turn to the ordinary tool loop. It dispatches no worker and appends no synthesis message.
- A planner failure (invalid output, a timeout, an exception, or D1's `input_too_large`) on a `planner_asked` turn also returns the turn to the tool loop. The fallback planner does not run. When the flag is false, the fallback planner keeps its role.

### D3 — The bound on expansion

1. At most min(3, `governance.expansion_budget`) workers per turn (FRE-1382).
2. A `DECOMPOSE` answer on a `planner_asked` turn is dispatched as `HYBRID` under the same cap. The stored plan and all telemetry carry the effective strategy.
3. No round budget changes. At this date `sub_agent_rounds_by_thoroughness` is unset, so `settings.sub_agent_rounds_for(level)` returns `sub_agent_max_tool_iterations` (20) for every level. This chain changes neither setting. FRE-1487 may change them under its own ticket and the owner's decision. The prompt renders the round budget that binds, and under `briefing` it already does: "(20 tool round(s) each)".

### D4 — The planner call runs with thinking off

The planner call uses the session's primary deployment, with that deployment's default sampling and its thinking disabled. On the local Flash-Next binding, this is the measured configuration: temperature 1.0, top_p 0.95, top_k 20, min_p 0, presence penalty 0, and `chat_template_kwargs.enable_thinking: false`. It is not the `worker` mode, which also changes the sampling (temperature 0.7, presence penalty 1.5).

The setting lives in the deployment's catalog entry as a `planner` mode in `config/models.yaml`, and the planner call requests that mode. Each provider dialect states thinking off in its own way (ADR-0145): `enable_thinking: false` on the local llama.cpp templates, and the provider's own control on a cloud deployment. A `planner` mode is written for one deployment at a time, and only after the probe of D7 shows that its thinking is off in effect. The evidence is the response, not the request: zero reasoning characters and a small completion count on declines.

D6 records the reasoning the response actually carried, so a mode that stops disabling thinking is visible on the first turn.

This amends ADR-0152 D5 ("no planner-specific mode"). ADR-0152 tested effort levels, never thinking off. The planner keeps the primary's model and temperature. No planner temperature is adopted.

### D5 — The memory digest, carried from ADR-0147

ADR-0147 D1, D2, D4, D6 and D7 carry into this ADR unchanged in substance:
- **D1:** a digest of at most one line per item, built from the primary renderer's own selection and text helpers, so no fact reaches the planner that was not eligible for the primary's render.
- **D2:** no memory item ever enters `SubAgentSpec.context`, and no rendered memory section reaches a worker. A fact may travel inside a goal, by design, and nothing bounds that mechanically.
- **D4:** at most 20 items, 120 characters per line and 300 estimated tokens.
- **D6:** no enrichment write path. **D7:** no further memory tool for a worker.

Three parts change:

- **Placement.** The digest goes in the user message after the history and before the query (D1). ADR-0147 put it in the system prompt, where its per-turn change breaks the cached prefix.
- **The relevance field (ADR-0147 D3) is keyed on the planner's decision.** ADR-0147's matrix assumed that a missing plan always meant a fallback plan. Under D2, a failed `planner_asked` turn has no plan and no fallback. The allowed matrix becomes:

  | `memory_digest_items` | `planner_decision` | Allowed `memory_relevance` |
  |---|---|---|
  | 0 | any | `not_applicable` only |
  | > 0 | `failed`, or a fallback plan | `not_applicable` only |
  | > 0 | `declined` or `expanded` | `used`, `none_relevant` or `unstated` only |

  A valid decline is a plan, so it carries the planner's judgment of the digest. A decline can carry `used`, for example when memory shows that the request is already answered.
- **The terminal planner event (ADR-0147 D5) fires exactly once per planner run,** on every path: a decline, an expansion, every failure reason including `input_too_large`, and a fallback plan outside D2. A run that fails and then uses the fallback plan is one run, and its one event records the fallback. It carries the D6 planner fields, the prompt hash of D7 and the digest fields of ADR-0147 D5.

The digest's effect on the decision is unmeasured. AC-8 measures it before the umbrella closes, and AC-11 carries ADR-0147's invariant checks.

### D6 — The planner's decision, inputs and delay are recorded

Every turn gains these durable fields, in `route_traces` and in the ES projection:

| Field | Values | Set when |
|---|---|---|
| `planner_decision` | `declined`, `expanded`, `failed`, or null | null when the planner did not run |
| `planner_failure_reason` | `invalid`, `timeout`, `exception`, `input_too_large`, or null | `planner_decision = failed` |
| `planner_deployment`, `planner_mode` | the deployment key and the catalog mode of the planner call | the planner ran |
| `planner_reasoning_chars` | the reasoning characters the response carried | the planner ran |
| `planner_duration_ms` | the planner call's wall clock | the planner ran |
| `planner_prompt_tokens`, `planner_completion_tokens` | the engine's usage counts | the planner ran |
| `planner_input_chars` | the characters of each D1 input: system prompt, history, digest, message | every planner attempt, including `input_too_large` |
| `planner_gate_reason` | `planner_mode_absent`, or null | the four types on a deployment without a `planner` mode |
| `conversation_history_chars` | the length the planner's history render has, or would have, for this turn | always, on the four types |
| `expansion_budget` | `governance.expansion_budget` for the turn | always |
| `synthesis_appended` | true when a synthesis message was added | always |
| `first_token_ms` | from request receipt to the first token streamed to the user | always |

`first_token_ms` is new. Nothing records it today: `latency_total_ms` and `latency_breakdown` are null on all 12 conversational turns since 2026-09-15. It is the measure the owner experiences, and AC-5 compares against it.

`conversation_history_chars` is recorded on every turn of the four types, whether or not the planner runs. The render is a pure function of the session messages and costs no model call. On a turn before the routing change, it gives the history size the planner would have received, so that AC-5 can compare like with like.

**These fields ship before the routing change,** so that a pre-change baseline of `SINGLE` turns exists.

The fields need an idempotent migration under `docker/postgres/migrations/` and the matching columns in `docker/postgres/init.sql`, plus the row type, ledger, assembler and ES mapping.

A decline reads as a decline in every derived view, as ADR-0152 D4 states:
- `_LABEL_LIE_SQL` excludes `planner_asked` rows whose `planner_decision` is `declined` or `failed`.
- `_resolve_topology` returns `primary` for those rows.
- `ctx.expansion_strategy` is cleared on a decline or a failure.

### D7 — A planner configuration qualifies on a committed probe, and only a qualified one reaches D2

The probe used for Appendix A must be committed under `scripts/eval/` before the routing change ships. That means the capture step, the replay arms, both fixture sets, the scorer and the long-history arm. The probe must run its eval gateway outside the production compose project (Implementation Notes). A configuration is the engine, model, quant and planner mode. It qualifies when the probe gives:

| Criterion | Threshold | Measured, A thinking off |
|---|---|---|
| Decline-correct | at least 28 of 30 | 30/30 |
| Expand-correct | at least 43 of 48 | 46/48 |
| Each follow-up direction | at least 11 of 12 | 12/12 and 12/12 |
| Plans that fail to parse | 0 | 0 |
| Reasoning characters on any thinking-off call | 0 | 0 |
| Completion tokens on declined calls | p50 at most 40 | 13 |
| Declined planner call, single-turn fixtures | p50 at most 2 s | 0.8 s |
| Planner call, 60,000-char history, extended | at most 10 s | 5.9 s |
| Planner call, 60,000-char history, cold | at most 60 s | 40.2 s |

The decision thresholds sit one to three draws below the measured counts. The delay thresholds sit at about twice the measured values. The thresholds are a regression floor for this fixture set, not a claim about real-traffic accuracy. AC-4 measures real traffic.

**Qualification is the gate.** A deployment receives a `planner` mode in `config/models.yaml` only in a change that posts its passing probe result on FRE-1537 (or, later, on the ticket of that change). Without a `planner` mode, D2 does not apply to the deployment (D2). So a deployment that misses any threshold keeps today's routing until it passes. Only an amendment to this ADR, approved by the owner, can change a threshold or admit a deployment that missed one.

The default local primary binding must qualify before the routing change ships. Other deployments a user can select for a session, today the OVH `Qwen3.8-27B` and `claude_sonnet`, are probed after it. Until each one qualifies, its sessions keep today's routing.

**Requalification.** The probe output records a configuration fingerprint: the engine and its build, the model and quant, the `planner` mode's parameters, and a hash of the rendered planner system prompt. A change to any of these is a routing change. It needs a new passing probe run, on every deployment it affects, before it ships. The terminal planner event carries the same prompt hash, so a live turn shows which configuration ran.

---

## Alternatives Considered

The sample is small (Appendix A4). Each rejection below is an operational choice on the measured evidence, not a proof that the alternative is worse in every case.

### Option 1: The fork planner (design B)

**Description:** The planner call is the primary's own request with a planner instruction appended. It sees the memory section, the skills, the tools and the history exactly as the primary does, so ADR-0147's digest is unnecessary. Its prefill is work the primary does anyway.

**Pros:**
- Full input fidelity at no new rendering code.
- The primary's next call is almost free: 0.2 s to first token, with 5 new tokens.

**Cons:**
- Slower than A on every one of the 9 clean declined fixtures: 2.5 s added at the median against A's 1.0 s. The planner instruction sits after this turn's new content, so it is prefilled on every call.
- Lower point estimates on both rates: decline-correct 25/30 = 83% (66–93%) against A's 30/30 (89–100%). The intervals overlap. The misses are consistent on commands: `susan_1_run_tool` expanded in 3 of 3 draws.

**Why Rejected:** It costs more on the turns that most traffic produces, and no measured quality gain pays for that cost.

### Option 2: The primary decides through a tool (design C)

**Description:** No planner call. The primary holds a `start_workers` tool and calls it when it judges that it needs workers. This matches ADR-0142 D3's "demonstrated need".

**Pros:**
- No added delay on any turn.
- The decision uses everything the primary sees.

**Cons:**
- As its first action, the primary chose `start_workers` on 1 of 48 research draws: 2% (0–11%), against A's 96% (86–99%). The intervals do not overlap.
- A later `start_workers` call in the loop is not measured. 43 of 48 research draws began with `web_search`, so the primary was doing the research itself.

**Why Rejected:** It almost never delegates at the point where delegation saves work. Measuring complete C turns stays a candidate for a later study.

### Option 3: Keep the planner's thinking on (ADR-0152 D5)

**Description:** The planner runs in the primary's default mode, with thinking at the server's base effort. This is production's setting today.

**Pros:**
- No new mode in the catalog.

**Cons:**
- No difference detected: 10/10 and 15/16 against thinking off's 30/30 and 46/48, on one draw against three.
- A declined call takes 5.3 s at the p50 against 0.8 s, and an expanded call 25.5 s against 9.6 s.

**Why Rejected:** It adds about 4.5 s to every declined call, for no measured gain.

### Option 4: No memory for the planner

**Description:** ADR-0147 is superseded without its digest. The planner sees the history and the message only.

**Pros:**
- No digest code, no new failure surface.
- The measured configuration is exactly this, so the D7 thresholds need no new run.

**Cons:**
- The history carries what the user said in this session, not what the system knows from earlier sessions. The digest is the one input that the history lacks.

**Why Rejected:** The owner chose the digest on 2026-10-03. Its cost is at most 300 estimated tokens, after the cached history. AC-8 checks that it does not damage the decision.

### Option 5: A router model in front of the planner

**Description:** A small routing model decides expand or decline. Only an expansion pays for a planner call. ADR-0152 Option 4 measured Arch-Router-1.5B on llama.cpp on 2026-09-14.

**Pros:**
- A declined turn costs about 0.2 s.

**Cons:**
- It agreed on 35 of 54 draws (65%, 51–76%) and declined research questions that every planner expanded. It was measured on the old prompt and the old fixtures only, with no history.
- With A thinking off, a declined planner call already costs 0.8 s at the p50, so the router's gain is small.

**Why Rejected:** It fails on quality, and the delay it saves is now small. Carried from ADR-0152 unchanged.

### Option 6: Remove the forcings and let the complexity matrix decide (FRE-1394 as scoped)

**Description:** Delete the `CONVERSATIONAL` branch, so those turns fall through to the complexity matrix.

**Pros:** No planner call on short turns.

**Cons:** In FRE-1498 S2, 17 of 18 fixtures resolved exactly as before, because every message under 15 words is `SIMPLE`, and `SIMPLE` maps to `SINGLE`.

**Why Rejected:** It does not reach the short requests it was written for. This is ADR-0152 Option 1, unchanged.

### Option 7: Keep the ladder

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
- A deployment reaches D2 only after its thinking-off planner is measured (D7), so an unmeasured deployment keeps today's behaviour.

### Negative Consequences

- **Every turn of the four types pays for one planner call.** On a short declined turn this added 1.0 s at the median (0.8–1.9 s) in the paired measurement. On a long session the planner call costs about 5–6 s with a warm cache, and up to about 40 s cold at 60,000 characters of history. A turn that expands also pays for writing the plan: 319 completion tokens at the p50, 695 at the most.
- **The same message can route differently on a retry.** The planner samples at temperature 1.0. Under A thinking-off, `c4_deliverable` and `c4_process` each declined in 1 of 3 draws.
- **Cache eviction is real under load.** One session at a time showed no eviction on the 25 clean timing rows. After 27 distinct planner prompts, the primary's prefix was gone, at a cost of a 20 s cold prefill. An expansion turn, with its workers on the same server, is the load case. It was not measured.
- **The routing policy depends on the selected deployment.** The OVH `Qwen3.8-27B` and `claude_sonnet` keep today's routing until each qualifies (D7).
- **The planner prompt is part of routing.** A prompt change needs the probe on every qualified deployment (D7).
- **The change carries a Postgres migration and an ES mapping change** (D6), so its deployment is in the stricter class.
- **The digest is new exposure, carried from ADR-0147 D2.** A memory-derived fact can reach a worker inside a goal, and a worker that holds `web_search` can carry it into a search query. Nothing bounds that mechanically.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| The delay on long sessions is higher than measured, because the cache misses more often under real load | High | AC-5 compares `first_token_ms` per history size against the pre-change baseline. D6 records the input sizes and prompt tokens, so a cold-cache pattern is visible. |
| The fixtures overfit: 27 messages, 10 of them decline-type | Medium | AC-4 scores the planner against owner labels on real turns, with separate rates. |
| The digest changes the decision for the worse | Medium | AC-8 runs the probe with relevant and unrelated seeded digests before the umbrella closes. |
| A selectable deployment expands most turns | Medium | D7: it keeps today's routing until it qualifies. |
| A planner failure costs the call's time on the single lane | Low | D2 sends a failure to the tool loop, so the cost is the call and nothing else. AC-3 checks it. |

---

## Implementation Notes

**Files:**
- `src/personal_agent/request_gateway/decomposition.py`: the `planner_asked` branch and the `planner_mode_absent` gate.
- `src/personal_agent/orchestrator/expansion_controller.py`: the decline rule and schema, validation, the decline and failure returns, `DECOMPOSE` normalization, `briefing` as the only path, the D1 total bound, the digest's place in the user message, the planner mode request, and the terminal planner event.
- `src/personal_agent/orchestrator/executor.py`: `planner_may_decline`, the return to `LLM_CALL` without a synthesis message, clearing `ctx.expansion_strategy`, the digest builder (ADR-0147 D1), `conversation_history_chars` and `first_token_ms`.
- `src/personal_agent/orchestrator/expansion_types.py`: `PlannerMemoryDigest` and `ExpansionPlan.memory_relevance`.
- `config/models.yaml`: a `planner` mode on the local primary deployment, after it qualifies.
- `src/personal_agent/config/settings.py`: remove `planner_brief_mode`. Add `planner_input_max_chars`.
- `src/personal_agent/observability/route_trace/` (types, assembler, ledger and `_LABEL_LIE_SQL`), `src/personal_agent/observability/topology/`, `docker/postgres/init.sql`, `docker/postgres/migrations/`, and the ES index template.
- `scripts/eval/`: the probe of D7.

**Order:**
1. The committed probe (D7), because AC-8 and AC-10 need it.
2. The decision record with `first_token_ms` and `conversation_history_chars` (D6). It ships first and runs long enough to give the pre-change baseline of AC-5.
3. The digest (D5).
4. The planner mode for the local binding (D4), with its passing probe result.
5. The routing change (D2, D3), after 1 to 4.
6. Qualification of the selectable deployments (D7), one at a time.

**Eval-stack hazard found during the measurement.** Running the eval compose files with `-p seshat` from a worktree recreated production's `cloud-sim-searxng` with the worktree's bind mount. That cost about 7 minutes of degraded web search on 2026-10-03. The probe of D7 must run its gateway as a standalone container or under its own compose project, never under `-p seshat`.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR. They are adjudicated on FRE-1537 after the implementation chain deploys, not at the merge of this ADR. Two periods are used:
- **The baseline:** from the D6 deploy to the routing change: at least 14 days, and at least 20 `SINGLE` turns of the four types below 8,000 characters of history. The routing change waits until both hold.
- **The window:** 60 days after the routing change, or until AC-4's natural sample is full, whichever is first.

Real traffic is small (18 real chat turns between 2026-09-15 and 2026-10-02) and mostly declines. Each criterion states the minimum count it needs, and what happens when real traffic cannot reach it.

- **AC-1 — The four types reach the planner, and only on a qualified deployment.** · **Check:** a deterministic test of `_apply_matrix` over every combination of the four types and every `Complexity` value asserts `HYBRID` with `planner_asked` for a deployment with a `planner` mode, and, for one without, today's strategy and `decomposition_reason` with `planner_gate_reason = planner_mode_absent`. The same test asserts that `MEMORY_RECALL`, `SELF_IMPROVE`, `DELEGATION` and the two resource forcings are unchanged. Live: `route_traces` over the window, grouped by `task_type`, `decomposition_reason` and `planner_deployment`. · *Fails if* any test case differs, or any live turn of the four types on a qualified deployment, with expansion permitted, records one of the four deleted reasons or a null `planner_decision`.

- **AC-2 — A decline is honoured and reads as a decline.** · **Check:** a test drives a `planner_asked` turn whose planner stub declines, and asserts no worker, no synthesis message, and the next state `LLM_CALL`. Live, over the window: rows with `planner_decision = declined`. · *Fails if* the test finds a worker or a synthesis message, or any live declined row has `sub_agent_count > 0`, `synthesis_appended = true`, a match on the label-lie predicate, or an ES `topology` other than `primary`.

- **AC-3 — A failed planner does not expand.** · **Check:** four tests drive a `planner_asked` turn with a planner stub that returns invalid JSON, times out, raises, or receives a message over D1's total bound. Each asserts zero workers, no `fallback_planner_used` event, `planner_decision = failed` with the matching `planner_failure_reason`, and an answer through `LLM_CALL`. A fifth test asserts that a turn with `planner_may_decline` false still reaches the fallback planner. Live: rows with `planner_decision = failed`. · *Fails if* any of the first four tests dispatches, the fifth does not reach the fallback, or any live failed row has `sub_agent_count > 0`.

- **AC-4 — On real traffic, the planner agrees with the owner, in each direction.** Two samples, reported separately and never pooled.
  - *Natural sample.* Every `planner_asked` turn in the window, labelled by the owner "should expand" or "should not", blind to the decision. Each message is read from its Captain's Log capture (`user_message`, joined on `trace_id`).
  - *Challenge sample.* 15 research questions that the owner writes after the routing change, not taken from the fixtures, labelled "should expand" before they are sent, and sent live in the window. The instrument is a list of their `trace_id`s, posted on FRE-1537 before any of them is scored. The natural sample excludes those `trace_id`s.
  - *Coverage.* A capture is written only for a completed turn. The denominator is every `planner_asked` turn in the window in `route_traces`. The check first reports how many have a joinable capture.
  - *A failed planner.* `planner_decision = failed` sends the turn to the single lane, so it counts as a non-expansion: correct on a "should not" turn, incorrect on a challenge turn. Failed turns are also reported as their own count.

  **Error budget, and its basis.** An unwanted expansion costs the owner time and money on a turn that needed neither: the measured plan alone adds about 10 s, and the workers add far more (FRE-1517: 6,576 s with workers against 1,712 s without, over 8 turns). A wrong decline costs answer depth, and the owner can recover it by asking again. So the budget is tighter on declines.

  · **Check:** decline-correct over the natural "should not" turns, and expand-correct over the challenge sample, each with a 95% Wilson interval. The natural "should expand" turns are reported with their interval as exploratory. · *Fails if* fewer than 95% of the window's `planner_asked` turns have a joinable capture, the natural sample holds fewer than 30 "should not" turns at the end of the window, or more than 2 of them were expanded (decline-correct below 28 of 30), or the challenge sample shows expand-correct below 12 of 15, or one pooled figure is reported in place of the two. At these sizes, a pass still allows a true rate as low as the interval's lower bound (78.7% for 28/30, 54.8% for 12/15). The ADR states that limit, and a real-traffic result stays a floor check, not an accuracy estimate.

- **AC-5 — The delay the owner experiences is the one measured.** · **Check:** `first_token_ms` on declined `planner_asked` turns in the window, against `SINGLE` turns of the same task types in the baseline, on the same deployment. Both are split by `conversation_history_chars` at 8,000. · *Fails if*:
  - the short split (below 8,000 characters) holds fewer than 20 turns in either period, or its median added delay exceeds 3 s; or
  - the median added delay exceeds 10 s at or above 8,000 characters, when each period holds at least 10 such turns; or
  - the comparison uses `planner_duration_ms` in place of `first_token_ms`.

  If the long split cannot reach 10 turns in either period, it is decided instead by the committed probe's long-history arm on the production engine (D7), with its extended-call threshold. The ADR states this replacement in advance, because long sessions are rare in real traffic.

- **AC-6 — The inputs stay inside their bounds.** · **Check:** over the window, `planner_input_chars` on every row with a non-null `planner_decision`, and the digest fields of ADR-0147 D5. A unit test feeds an oversized history, an oversized digest set and an oversized message. · *Fails if* any row that ran the planner records history, digest and message together above 64,000 characters, any row above that total lacks `planner_failure_reason = input_too_large`, a history above 60,000 characters, or a digest above 20 items, 120 characters per line or 300 estimated tokens. It also fails if the test finds the message cut, the history trimmed other than from the oldest end, or an oversized message that does not fail with `input_too_large`.

- **AC-7 — Thinking off is what ran, on every deployment.** · **Check:** over the window, `planner_reasoning_chars`, `planner_mode` and `planner_completion_tokens` on every planner call. · *Fails if* any call in a `planner` mode records `planner_reasoning_chars` above 0, or the p50 `planner_completion_tokens` of declined calls on any deployment exceeds 40. The p50 needs at least 10 live declined calls on a deployment. For a deployment with fewer, the committed probe's declined calls on that deployment, run in the window, take their place under the same threshold. With thinking off, declines took a median 13 completion tokens, at most 19. With thinking on, they took a median 165, so a thinking leak cannot pass this check.

- **AC-8 — The digest informs the plan and does not damage the decision.** · **Check, three parts:**
  1. *Seeded pair:* ADR-0147 AC-2's integration test, run on the D2 prompt. A coined token in a relevant digest must reach a goal, and an unrelated digest must return `none_relevant` and leak nothing.
  2. *Unrelated digest:* the committed probe re-run with an unrelated seeded digest on every fixture.
  3. *Relevant digest:* 4 expand fixtures and 4 decline fixtures re-run with a digest that bears on the question but does not change its right answer. For example, a heat-pump question with a digest line that the user lives in Brittany.

  · *Fails if* the seeded pair fails, if part 2 misses any D7 decision threshold, or if part 3 changes the decision on more than 1 of the 24 draws (8 fixtures × 3).

- **AC-9 — The bound holds, and this chain moved no round budget.** · **Check:** over the window, `sub_agent_count` against `expansion_budget`. Then the combined diff of this ADR's implementation PRs, for `sub_agent_rounds_by_thoroughness`, `sub_agent_max_tool_iterations` and `sub_agent_rounds_for`, in code, configuration and `.env.example`. · *Fails if* any turn records more than min(3, `expansion_budget`) workers, or any implementation PR of this chain changes one of those three. A change made under FRE-1487 is outside this check.

- **AC-10 — Only qualified deployments reach D2.** · **Check:** for each deployment with a `planner` mode in the deployed `config/models.yaml`, the ticket that added the mode holds the probe's passing scorer output, run from the repo, with its configuration fingerprint. A deployment admitted below a threshold needs an owner-approved amendment to this ADR. Live: `route_traces` over the window, grouped by `planner_deployment`. · *Fails if* a deployment has a `planner` mode without a posted passing result or an amendment, if the posted fingerprint differs from the deployed configuration, if the local binding has no passing result, or if any live `planner_asked` turn ran on a deployment without a `planner` mode.

- **AC-11 — ADR-0147's invariants hold under this design.** · **Check, carried from ADR-0147 AC-1, AC-5 and AC-6:**
  - *Parity:* a unit test feeds session items, blank items and more than 47 items, and asserts that every digest line is a prefix of the renderer's own line for the same item. Live, on the terminal planner event, the digest keys are an ordered sub-multiset of the rendered keys.
  - *No memory section in a worker:* `memory_in_context` on every sub-agent capture in the window.
  - *Relevance matrix:* D5's matrix over every terminal planner event in the window.
  - *Event coverage:* every turn with a non-null `planner_decision` has exactly one terminal planner event, joined on `trace_id`.

  · *Fails if* the parity test fails or any live digest key is absent from the rendered keys, any capture reports `memory_in_context = true`, any event falls outside the matrix, or a turn with a non-null `planner_decision` has zero or more than one terminal event.

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
**Reason:** Drafted on the owner's "Draft it" after the 2026-10-03 measurement, the choice of design A with thinking off, and the choice to keep the ADR-0147 digest. Codex round 1 (8 blocking): an enforced input bound, a per-deployment planner gate with response evidence of thinking off, the relevance matrix keyed on the planner's decision, a counterfactual history size for the baseline, paired per-fixture delays with two contaminated rows excluded, statistics stated as "no difference detected", and the router alternative restored. Codex round 2 (5 blocking): no owner bypass of the gate except an ADR amendment, a completion-token threshold in D7, a separate `planner_gate_reason`, capture coverage and a pre-registered challenge list in AC-4, and a minimum short-history sample in AC-5. Codex round 3 (2 blocking): a minimum sample for AC-7 and one terminal event per planner run in AC-11. The round budget is exhausted at three.

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
- **A cache artifact on two rows.** On `tool_logs` and `tariffs_expand`, the primary after design A found the full prompt cached from the preceding B step (`cache_n` 15,299 and 15,134). Those two "A planner + primary" figures are optimistic, and both rows are excluded from every paired figure in this ADR. Both prompts were larger than the others, and their SINGLE figures (24.5 s) include a colder prefill. On the other 25 rows, the primary's `cache_n` after A was 6,540 on every row. The protocol ran B before A on every fixture, in one fixed order. A randomized order was not run.
- **Cache restore granularity.** Every warm extension prefilled about 2,071 tokens (planner) or 2,157 (primary, p50), not only the new text. The cause given in Context point 5 is an inference.
- **C stops at the first action.** A `start_workers` call later in the primary's loop is not measured.
- **Sample size.** 10 decline-type fixtures and 16 expand-type fixtures. The intervals are wide, and AC-4 exists for that reason.
- **Quantiles.** A p50 or p90 is the value at index round(q·(n−1)) of the sorted values. With n of 9 to 15, a p90 is close to the maximum and moves with one draw. For that reason the timing comparison uses paired per-fixture differences, with their median and range. The paired figures come from the unrounded rows, so a median recomputed from A3's one-decimal cells can differ by 0.1 s.
- **Real-traffic weights.** The 2026-07/08 mix in Context is the weight to apply. At 90% decline traffic, A thinking-off's weighted agreement is 100%, and B's is 84%. These weighted figures do not decide anything. The two separate rates do.
- **Raw rows.** One JSON line per call, kept in the `adr` worktree's git-ignored `telemetry/archive/fre1537-planner-probe/`, with the stub, the capture driver, the replay and analysis scripts, and the captured request bodies. They are not committed, because they are run output and the request bodies hold full prompts. D7 commits the probe.

### A5. Planner call summaries (decision arms)

Scored draws only (`c5_noverb` excluded). Seconds are the planner call's wall clock, streamed.

| Arm | Declined: n | p50 / p90 s | Completion tokens p50 / max | Expanded: n | p50 / p90 s | Completion tokens p50 / max | All: n, p50 / p90 s |
|---|---|---|---|---|---|---|---|
| A, thinking off | 32 | 0.76 / 1.25 | 13 / 19 | 46 | 9.61 / 14.44 | 319 / 695 | 78, 6.81 / 13.33 |
| A, thinking on | 11 | 5.32 / 11.49 | 165 / 646 | 15 | 25.52 / 31.22 | 825 / 1,359 | 26, 20.05 / 29.66 |
| B, thinking off | 30 | 8.43 / 16.70 | 13 / 13 | 48 | 24.60 / 37.14 | 584 / 1,371 | 78, 20.29 / 32.34 |

The declined counts include draws that declined an expand fixture, so they differ from the decline-fixture counts in A2. Design A's prompt was 607–722 tokens on single-turn fixtures and 776–820 with the short fixture histories. Design B's prompt was 9,300 tokens at the p50.
