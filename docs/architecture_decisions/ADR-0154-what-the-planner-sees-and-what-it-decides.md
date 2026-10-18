# ADR-0154: What the Planner Sees and What It Decides — an Isolated Planner, Thinking Off, Asked on Every Turn of the Four Register Types

**Status:** Accepted — 2026-10-03 (owner). Proposed 2026-10-03 (PR #1196). Amendment 1 accepted 2026-10-11 (owner, FRE-1587): the planner's added delay is measured per configuration in the D7 probe, and AC-5 moves to it. Amendment 2 accepted 2026-10-18 (owner, FRE-1607): on a `planner_asked` turn the planner sees the skill names and declines a query that one skill covers, if the D7 probe shows the gain.
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

**Against FRE-1138.** The planner runs once per turn, before the tool loop, so its input is not within-turn growth. Its input is bounded at 2,439 + 64,000 characters, and D6 records the real size per turn. *Amended by Amendment 2 (2026-10-18):* on a `planner_asked` turn, the static text grows by the skill line, so the bound at that date is 2,733 + 64,000 characters with 11 skills.

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

`first_token_ms` is new. Nothing records it today: `latency_total_ms` and `latency_breakdown` are null on all 12 conversational turns since 2026-09-15. It is the measure the owner experiences, and AC-5 compares against it. *Amended by Amendment 1:* `first_token_ms` stays recorded, as telemetry and as context for the floor check (1.4). AC-5 is now decided by the paired arm of the D7 probe (1.3).

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

*Amended by Amendment 1 (2026-10-11):* the table gains two paired added-delay rows, one per history split. A configuration qualifies only if both pass (Amendment 1, 1.2).

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
| The delay on long sessions is higher than measured, because the cache misses more often under real load | High | AC-5 compares `first_token_ms` per history size against the pre-change baseline. D6 records the input sizes and prompt tokens, so a cold-cache pattern is visible. *Amended by Amendment 1:* the paired arm measures the cache loss per configuration before the routing change (1.1, 1.3), and the floor check compares the live planner call with it (1.4). |
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
2. The decision record with `first_token_ms` and `conversation_history_chars` (D6). It ships first and runs long enough to give the pre-change baseline of AC-5. *Amended by Amendment 1:* the baseline no longer gates the routing change. Step 5 waits for the paired arm instead (1.3).
3. The digest (D5).
4. The planner mode for the local binding (D4), with its passing probe result.
5. The routing change (D2, D3), after 1 to 4.
6. Qualification of the selectable deployments (D7), one at a time.

**Eval-stack hazard found during the measurement.** Running the eval compose files with `-p seshat` from a worktree recreated production's `cloud-sim-searxng` with the worktree's bind mount. That cost about 7 minutes of degraded web search on 2026-10-03. The probe of D7 must run its gateway as a standalone container or under its own compose project, never under `-p seshat`.

---

## Verification / Acceptance Criteria

These criteria belong to this ADR. They are adjudicated on FRE-1537 after the implementation chain deploys, not at the merge of this ADR. Two periods are used:
- **The baseline:** from the D6 deploy to the routing change: at least 14 days, and at least 20 `SINGLE` turns of the four types below 8,000 characters of history. The routing change waits until both hold. *Amended by Amendment 1:* the baseline is no longer a condition of the routing change. Its turns stay as context for the floor check (1.4).
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

- **AC-5 — The delay the owner experiences is the one measured.** *Replaced by Amendment 1 (1.3), 2026-10-11. The text below is the original and no longer applies.* · **Check:** `first_token_ms` on declined `planner_asked` turns in the window, against `SINGLE` turns of the same task types in the baseline, on the same deployment. Both are split by `conversation_history_chars` at 8,000. · *Fails if*:
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

### 2026-10-11 - Amendment 1 accepted
**Changed By:** `adr` seat (FRE-1587)
**Reason:** The owner, 2026-10-11, relayed by master: the planner assessment "could and should be integrated into our model testing", then "Go now" for both steps (amend AC-5, and add the measurement to the FRE-1581 run). On FRE-1515, the same day: "no 14 day delay". In the ADR session of 2026-10-11, the owner chose four points that the ticket left open: the pair measures to the primary's first token, direct on the engine; the 10 decline fixtures give the short pairs; every configuration needs the paired arm, and the managed path is built later; and the missing engine field is a stated limit, with FRE-1588 filed to close it. Codex round 1 (4 blocking): a fixed pair-key set per split, a balanced arm order, a precondition over the complete primary request, and the `timings` shape of the stream result. Round 2 (1 blocking): the precondition compares the final `litellm.acompletion` arguments, because the client adds the catalog values after the executor. Round 3: no blocking finding. Its three non-blocking points were applied: the calls are told apart by their order, only `client` and the trace and span headers may differ, and two more offline tests.


### 2026-10-18 - Amendment 2 accepted
**Changed By:** Owner (decision), `adr` seat at Opus 5.5 (author, FRE-1607)
**Reason:** The owner, 2026-10-18, relayed by master: "Do we care about adr right now? If so, approve its recommendation." Master judged yes and approved the seat's recommendation: the worker half first (ADR-0156 amendment of 2026-10-18), then the planner half (this amendment), each with its token cost and the smallest form. Codex review rounds are logged in the PR description.
---

## Amendment 1 — The planner's added delay is measured per configuration, in the D7 probe (FRE-1587, 2026-10-11)

**Status:** Accepted. The owner decided it on 2026-10-11 (Status Updates). It changes D7 and AC-5, and it adds a precondition that FRE-1515 must test. Every other part of this ADR is unchanged.

### Why

The original AC-5 compared production turns from two periods: `SINGLE` turns before the routing change, and declined `planner_asked` turns after it. Master's read of the baseline on 2026-10-11 (FRE-1515) found three faults:

- The two periods hold different questions. Answer length and tool calls add noise, and at 20 turns a 3 s limit is hard to separate from that noise.
- Model testing changed the answering model. The 40 short-history baseline turns split as Flash-Next 19, Gemma 19 and Sonnet 2.
- `model_call_completed` records no engine. The FRE-1517 study served Flash-Next through more than one engine on the same tunnel.

A paired measure removes all three faults. Both arms of a pair send the same turn to the same configuration. So the difference between the arms is the planner's own cost.

### 1.1 — The paired arm

The D7 probe gains a paired arm. A pair is two runs of the same turn on the same configuration (engine, model, quant, planner mode and planner prompt hash):

- **The `SINGLE` arm:** the primary call alone, as a turn routed `SINGLE` sends it.
- **The planner arm:** the planner call, then the same primary call, as a declined `planner_asked` turn sends them.

**The measure.** The added delay of a pair is the planner call's wall clock, plus the planner arm's primary time to first token, minus the `SINGLE` arm's primary time to first token. So the difference holds the planner call and any loss of the primary's prompt cache. The probe stops each primary call at its first token (`max_tokens: 1`). After the first token, the answer and the tool loop do the same work in both arms. They add noise and no planner cost.

The probe does not run a gateway turn, for two reasons:

- No gateway can make a declined `planner_asked` turn before FRE-1515 ships, and FRE-1515 merges on this result.
- Production `first_token_ms` measures the whole reply today, because nothing streams token by token (master, FRE-1515, 2026-10-03).

**The precondition.** The measure is valid only if a declined `planner_asked` turn sends the same primary request as a `SINGLE` turn with the same session state. "The same" means the final request of the first primary call: the keyword arguments that `LiteLLMClient` passes to `litellm.acompletion`. The executor passes only part of the request, and the client adds the mode, sampling, dialect parameters, `parallel_tool_calls` and the token limit from the catalog. So the comparison is made after the client, not at the executor. Only the HTTP `client` object and the trace and span header values inside `extra_headers` may differ. Every other key of `extra_headers`, and every other argument, must be equal. FRE-1515 asserts this in a test (1.3).

**Which turns.**

| Split | Turns | Draws | Pairs | Counted pairs needed | Limit |
|---|---|---|---|---|---|
| History below 8,000 characters | the 10 decline fixtures: 6 single-turn and 4 follow-ups | 3 per fixture | 30 | at least 28 | median added delay at most 3 s |
| History at or above 8,000 characters | the long-history turn at 8,000, 30,000 and 60,000 characters | 3 per size | 9 | at least 8 | median added delay at most 10 s |

- The limits are the original AC-5 limits, unchanged.
- The expand fixtures give no pair. The planner expands on them, and a turn that expands is not a declined turn.
- A pair counts when its planner call declines under D2's validation rule, no call of the pair errors, and the pair is not void (below).
- Each split has a fixed set of pair keys: (fixture, draw) for the 10 fixtures and draws 0 to 2, and (size, draw) for the 3 sizes and draws 0 to 2. The scorer keeps the last row without an error for each key. A missing key or an unexpected key is FAIL for that split.
- A split with fewer counted pairs than it needs is FAIL. It is never scaled.
- The median uses the index rule of Appendix A4.
- The split of a pair is set by the length of the planner's rendered history for that turn (`history_chars`).

**The engine state before each arm.** Each arm starts from the state that the previous turn left under that arm's routing:

- `SINGLE` arm: send the previous turn's primary request, then measure the primary call.
- Planner arm: send the previous turn's planner request, then the previous turn's primary request. Then measure the planner call and the primary call.

A prime is a request that sets this state. It has `max_tokens: 1`, and the probe does not measure it.

- **The previous primary request** is the turn's own primary request cut before its last user message, as `prime_body` cuts it today. For a single-turn fixture, that is the system prompt and tools with the user message ".".
- **The previous planner request** is the planner request of the previous turn, built with the production `build_planner_user_message`. Its history is the turn's history without its last exchange, and its query is the user message of that last exchange. A single-turn fixture has no previous turn, so its previous planner request is the planner system prompt with the user message ".".

**No reuse across arms or draws.** Each arm of each draw puts a line `probe nonce <8 hex characters>` at the start of the system message of every request in that arm. The nonce is new for each arm and draw. So no arm can reuse a prompt that another arm or draw cached. The rendered planner prompt does not hold the nonce, so the configuration fingerprint does not change.

**The arm order.** A pair runs the `SINGLE` arm first when (index + draw) is even, and the planner arm first when it is odd. The index is the zero-based position of the fixture in the sorted list of decline fixture labels, or the zero-based position of the size in `SIZES`. So 15 of the 30 short pairs and 5 of the 9 long pairs run `SINGLE` first. Appendix A4 found a bias from a fixed order.

**A void pair.** A pair is void when a measured call's `timings.cache_n` exceeds the prompt tokens (`timings.cache_n + timings.prompt_n`) of the prime before it in the same arm. The planner call is compared with the planner prime. Each primary call is compared with its primary prime. A void pair means that the engine reused more than the previous turn left, so the instrument failed on that pair. A void pair does not count. The report states the void count and the reasons.

**Cost.** Every call goes to the local engine and costs no money. Each pair makes 6 calls: 180 calls in the short split and 54 in the long split. The long primes prefill up to about 15,000 tokens cold. The run time is not measured yet. An estimate is about one hour.

### 1.2 — D7 qualification includes the paired arm

A configuration qualifies only if both paired checks pass, together with every row of D7's table. So every candidate model in model testing is measured on the paired arm. D7's table gains two rows:

| Criterion | Threshold | Measured before this amendment |
|---|---|---|
| Paired added delay, history below 8,000 chars | median at most 3 s, at least 28 of 30 pairs counted | about 1.0 s median. Appendix A3 "A planner + primary" minus "SINGLE" on the 9 decline rows, one draw each, with no nonce and no void rule |
| Paired added delay, history at or above 8,000 chars | median at most 10 s, at least 8 of 9 pairs counted | not measured |

**Managed deployments.** Every configuration needs the paired arm, including a managed one. The probe's managed path calls the planner only, so it cannot run a pair today. The ticket that next qualifies a managed deployment for a `planner` mode builds the managed paired path. It uses the production client, under the probe's `--max-usd` cap. Until that path exists, no managed deployment can qualify. Today no managed deployment has a `planner` mode, so this changes nothing in production.

**A `planner` mode from before this amendment.** A deployment with a `planner` mode in `config/models.yaml` reaches D2 when FRE-1515 ships. So, before FRE-1515 merges, each such mode needs a passing D7 result that includes the paired arm, or it is removed (AC-10).

### 1.3 — AC-5 moves to the probe

The original AC-5 is replaced by this text:

- **AC-5 — The planner's added delay passes the paired arm on the production configuration.** · **Check:**
  - The scorer report of FRE-1581's D7 run, on the production configuration, posted on FRE-1515 with its fingerprint before FRE-1515 merges. The production configuration is the default local primary binding at its deployed engine build, model, quant, `planner` mode and planner prompt hash.
  - FRE-1515's test of the precondition in 1.1. The test drives the same input once routed `SINGLE` and once routed `planner_asked` with a declining planner stub. Both routes use one fixed session ID. The test patches `litellm.acompletion` and captures its calls in order, per route. In the `SINGLE` route the first call is the primary call. In the planner route the first call is the planner call and the second is the primary call. The test asserts that the two primary calls are equal except for the values that 1.1 allows to differ.

  · *Fails if*:
  - either paired check in the report is FAIL;
  - the posted fingerprint differs from the configuration that FRE-1515 deploys;
  - the precondition test is absent, or it finds a difference; or
  - the result reports `planner_duration_ms` alone in place of the paired measure.

**FRE-1515's merge condition** becomes: the paired arm passes on the production configuration in FRE-1581's D7 run. The 14-day and 20-turn baseline is no longer a merge condition. FRE-1515's other conditions do not change: the full D7 pass on the current planner prompt, and AC-10.

### 1.4 — The production check stays, as a floor and not a gate

After the routing change, the four types have no `SINGLE` turns on a planner deployment. So the live added delay cannot be seen directly. Master checks instead that the live planner call matches the probe. The check runs over the window, on the production deployment, in each split at 8,000 characters:

- **The comparison:** the median live `planner_duration_ms` on declined `planner_asked` turns, against the median planner-call time over the counted pairs of the D7 run that qualified the configuration, in the same split. The scorer reports that median per split (1.6).
- **The trigger:** a split with at least 10 live declined turns has a median more than twice the probe's median. Then master posts both figures on FRE-1537, tells the owner, and re-runs the paired arm on the deployed configuration.
- **Context only:** the live median `first_token_ms` of declined turns, against the `SINGLE` turns of the four types that the same deployment recorded before the routing change. The two sets hold different questions and whole replies, so this decides nothing.

The check is not a gate. No result of it reverts the routing change by itself.

**Limits, stated in advance:**

- The live check sees the planner call, not the loss of the primary's cache. Only the probe measures that loss.
- `model_call_completed` records no engine, and `planner_deployment` names a catalog key, not the engine behind it. So a live turn that another engine served on the same tunnel cannot be excluded. Until FRE-1588 records the engine, master counts only turns outside model-testing periods on that deployment.

### 1.5 — The owner's words

The dated entry in Status Updates records them.

### 1.6 — The probe change that FRE-1581 makes

The FRE-1581 seat builds this change, with its tests, before its D7 run. It makes no design decision beyond this text.

**`scripts/eval/fre1537/replay.py`**

- Replace the `timing` arm with a `paired` arm, in a function `run_paired`. `--arms` takes `decide` and `paired`, and the default is `decide,paired`. `--arms timing` exits with a message that names `paired`.
- The arm runs the fixtures whose `expected` is `decline` (10 fixtures), with `PAIRED_DRAWS = 3` draws each. It writes one row per fixture and draw to `rows/<tag>/paired.jsonl`. A rerun resumes on the fixture and draw.
- `common.py`: `RunPaths` gains `paired` and `longhist_paired`, and loses `timing` with the arm. A new helper `done_pair_keys(path, key_field)` returns the `(key, draw)` pairs of rows without an error, with `key_field` `label` or `size_chars`. The existing `done_keys` stays for the decision arm.
- A managed run (`--deployment`) still refuses every arm except `decide` (1.2).

**`scripts/eval/fre1537/longhist.py`**

- Add `--arms` with `cold` and `paired`. The default is `cold,paired`. `cold` is today's arm, unchanged, and it still writes `rows/<tag>/longhist.jsonl`.
- `paired` runs each size in `SIZES` for 3 draws. It writes one row per size and draw to `rows/<tag>/longhist_paired.jsonl`.
- The turn: the history H of that size, then `FIRST_QUERY` and `FIRST_REPLY` as one exchange, then `SECOND_QUERY`. The previous turn is H with `FIRST_QUERY`. The planner calls render the history with `render.build_user_message`, as `_call` does today.
- The primary request of the turn starts from the captured body of `reference_label(inputs)`. It keeps that body's system message (with the nonce line), tools and sampling. The messages between the system message and the last user message become the turn's history as role messages. The last user message becomes `SECOND_QUERY`. The messages after the last user message stay unchanged.
- One function builds the history pairs of a size and a nonce. `render_history` makes the planner's text from them, and the primary request makes role messages from them. So the two views of one history cannot differ.
- Every draw renders a new history with the arm's nonce, so no size or draw reuses another's cache.

**The row of both paired files**

| Field | Value |
|---|---|
| `arm` | `paired` or `longhist_paired` |
| `tag`, `ts` | as today |
| `label` or `size_chars` | the fixture, or the history size |
| `draw` | 0, 1 or 2 |
| `kind` | `single`, `followup` or `longhist` |
| `history_chars` | the length of the planner's rendered history for the turn. From `inputs.fixture(label)["history_chars"]`, or the length of the long arm's rendered history |
| `order` | `single_first` or `planner_first` |
| `single` | `{nonce, prime, primary}` |
| `planner` | `{nonce, prime_planner, prime_primary, planner, primary}` |
| `declined` | true when `classify` of the planner call's plan is a decline |
| `added_ms` | (`planner.planner.secs` + `planner.primary.ttft_any` − `single.primary.ttft_any`) × 1000, rounded. Null when an input is missing |
| `void` | null, or a list of reasons from `planner_cache_reuse`, `primary_cache_reuse_single` and `primary_cache_reuse_planner`. A call with a null `timings.cache_n` or `timings.prompt_n` gives the reason `timings_missing` |
| `error` | as today |

Each call in `single` and `planner` holds the `stream` result, with its shape unchanged and without `content`: `secs`, `ttft_any`, `ttft_content`, `reasoning_chars`, `system_fingerprint`, `usage`, and `timings` with `cache_n`, `prompt_n`, `prompt_ms`, `predicted_n` and `predicted_ms`. The void rule reads `timings.cache_n` and `timings.prompt_n`. The measured planner call also holds `plan`, as the decision arm records it.

**`scripts/eval/fre1537/score.py`**

- Constants: `PAIRED_SPLIT_CHARS = 8000`, `PAIRED_SHORT_MAX_S = 3.0`, `PAIRED_SHORT_MIN = 28`, `PAIRED_SHORT_OF = 30`, `PAIRED_LONG_MAX_S = 10.0`, `PAIRED_LONG_MIN = 8` and `PAIRED_LONG_OF = 9`.
- `score()` reads both paired files. It keeps the last row without an error for each pair key, and it checks each file's keys against its fixed set (1.1). It then puts the rows together and splits them on `history_chars` at 8,000. A pair counts when `declined` is true and `void` is null.
- Two checks: "Paired added delay, history < 8,000 chars (median)" and "Paired added delay, history >= 8,000 chars (median)". Each is FAIL when a pair key is missing or unexpected, when its counted pairs are below its minimum, or when its median is above its limit. The measured text shows the median and "k of n counted".
- The measured planner call of each pair counts for the parse threshold and the reasoning threshold, as `T3_planner` counts today. The planner prime counts for the reasoning threshold only. The primary calls count for neither.
- `report.json` gains `paired.short` and `paired.long`. Each holds `counted`, `of`, `added_s_p50`, `planner_s_p50` (the median planner-call time over counted pairs, which 1.4 reads), `cache_loss_s_p50` (the median of `planner.primary.ttft_any` − `single.primary.ttft_any` over counted pairs) and `void` (the count per reason).
- The info lines show the same figures and the median `added_ms` per fixture. The old timing line is removed.

**Tests** (offline, in `tests/test_eval/test_fre1537_score.py` and `tests/test_eval/test_fre1537_replay.py`):

1. Rows that pass in both splits give PASS on both checks.
2. A short median above 3 s gives FAIL.
3. 27 counted short pairs give FAIL, with a median below the limit.
4. A long median above 10 s gives FAIL, and 7 counted long pairs give FAIL with a median below the limit.
5. A run with a missing pair key gives FAIL, a run with an unexpected pair key gives FAIL, and a duplicated key does not raise the count.
6. A pair whose measured primary `timings.cache_n` exceeds its prime's prompt tokens is void and does not count. A call with no `timings` is void too.
7. A pair whose planner call expands does not count.
8. `done_pair_keys` returns only the keys of rows without an error, so a rerun skips only the completed (key, draw) pairs.
9. `run_paired` against a fake engine: the nonce differs for each arm and draw, the order follows the rule of 1.1, and each arm sends its calls in the order of 1.1.

**`scripts/eval/fre1537/README.md`:** steps 5 and 6 name the new files and their call counts, and the scorer's table gains the two rows.

The FRE-1581 run then uses `replay --arms decide,paired` and `longhist` with its default arms, in `--mode planner`, and posts the scorer report on FRE-1515 (1.3).

---

## Amendment 2 — The planner sees the skill names, and declines a query that one skill covers (FRE-1607, 2026-10-18)

**Status:** Accepted. The owner decided it on 2026-10-18 (Status Updates). It adds one line to the planner system prompt (D1 input 1, D2), and it adds fixtures, a baseline arm, two thresholds and a ship rule to D7. Every other part of this ADR is unchanged.

### Why

- **The planner expands before any model sees a skill.** FRE-1598 (`docs/research/2026-10-13-fre-1598-benefit-run.md`) found the same event order on all 4 expanded treatment turns. The planner call had 820 input tokens and no catalog, the workers ran, and the first `skill_index_assembled` came on the synthesis call. On 3 of the 4 turns, no skill loaded.
- **The single path with the skill was the better path.** On the 5 pairs that stayed single and where only the treatment loaded a skill, 4 improved and gave 11 of the 13 points of difference, and 1 tied. The 4 improved treatment turns took 28 to 105 s. The expanded skill-area treatment turns took 111 to 575 s and 17 to 56 tool calls. They gained 2 points in total, and FRE-1598 did not show that either point came from a skill.
- **The worker fix alone does not remove the expansion.** The ADR-0156 amendment of 2026-10-18 (FRE-1607) gives the `general` worker its skills. A worker still runs one goal in its own budget, and the primary then synthesises. For a query that one skill covers, one assistant with the skill does the same work in one lane.
- **Master's reason for the approval:** the owner's slow turn of 2026-10-18 (trace `2f36d026…`, 290 s) would have stayed single. This is master's judgment, and this amendment did not measure it.
- **The evidence is small.** FRE-1598 ran one draw per arm at temperature 1.0, and 4 expanded treatment turns. So the claim that expansion hurts a skill-area turn is a direction. The probe of 2.3 measures the planner's side of it.

### 2.1 — The skill line (amends D1 input 1 and D2)

1. When the decline rule is in the prompt (`planner_may_decline`, D2), the Rules list carries one more line, directly after the decline rule:

   `- One assistant working alone can load these skills: <names>. If one of them covers the query, output SINGLE`

2. `<names>` is the primary's skill names, sorted and comma-separated, rendered from the skill folders (`orchestrator/skills.py`). The text depends only on the skill files, so it is static per build and stays in the cached prefix.
3. The line holds names only. D1's rule "no skill bodies" holds, and the planner gets no descriptions (2.2).
4. Without `planner_may_decline`, the line is absent. The planner cannot output `SINGLE` there, so the line has no use.
5. The FRE-1498 decline text stays unchanged (D2). The new line is a separate rule.
6. `scripts/eval/fre1537/render.py` renders the same prompt, and the byte-identity test holds. The pinned hashes of `tests/personal_agent/orchestrator/test_fre1549_reasoning_discipline.py` move. This is a routing change under D7 requalification.

### 2.2 — The token cost

| Item | Value |
|---|---|
| The line, with today's 11 names | 294 chars |
| Tokens, at the FRE-1592 M1 catalog ratio (4.49 chars per token mean, 4.14 lowest) | about 65 / 71 |
| Tokens, at 3.0 chars per token, if the hyphenated names tokenize worse | about 98 |
| D1 input 1 | grows from 2,439 chars (at the D1 date) by 294 chars, on `planner_asked` turns only |
| Where it is paid | The planner system prompt. It is static, so after the first call it is in the cached prefix. A cold prefix pays it once: at most about 100 × 2.65 ms = 0.27 s |

**The smallest form.** Names only. The full catalog lines are 6,002 chars, about 1,340 tokens, so they cost about 20 times as much. The planner does not load a skill. It decides only whether one assistant covers the query. The skill names state an objective (ADR-0156 A3), so a name can be enough for that decision. The probe of 2.3 tests this.

### 2.3 — The D7 probe gains skill fixtures, a baseline arm and a ship rule (amends D7)

1. **Fixtures.** A new file `scripts/eval/fre1537/skills.yaml` holds 10 single-turn fixtures:
   - **6 that must decline:** the prompts of FRE-1598 `log-search#2`, `log-search#3` and `turn-explanation#1` (`scripts/eval/fre1598/part2_scenarios.yaml`), and one prompt each from the `evals.yaml` of `spend-and-tokens`, `model-latency` and `interaction-outcomes`;
   - **4 seeded negatives that must expand:** research questions on the open web that name the subject of a skill. One example: "Research how three open-source agent frameworks measure model latency, and compare their methods." The others name logs, spend and a knowledge graph.
2. **Arms.** Each fixture runs 5 draws in two arms: the current prompt (the baseline) and the amended prompt. That is 100 planner calls, thinking off, on the local engine. At the D7 delays (a decline p50 0.8 s, an expansion p50 9.6 s), the run takes about 10 minutes.
3. **Thresholds, on the amended arm:**

   | Criterion | Threshold |
   |---|---|
   | Skill-area decline | at least 27 of 30 |
   | Skill-word expand | at least 18 of 20 |
   | Every existing D7 row, with Amendment 1 | passes as before |

   The existing expand-correct row (at least 43 of 48) is the main guard against a planner that declines too much.
4. **The ship rule.** The line ships only if the amended arm passes all three rows **and** it declines more skill-area draws than the baseline arm by a one-sided Fisher exact test at p < 0.05 (30 draws in each arm). For example, 27 of 30 against 20 of 30 gives p ≈ 0.03 and passes, and 27 of 30 against 21 of 30 gives p ≈ 0.05 and fails. The draws of one fixture are correlated, so the p-value is optimistic. A smaller gain is noise at this sample size, and a line that shows no gain adds tokens for nothing. It then does not ship, and the ticket records the result.
5. **Where it runs.** FRE-1581 is Done, and model testing resumed after it. So the run belongs to the planner build ticket. It runs on the production configuration (Flash-Next, llama.cpp, the `planner` mode), and it needs the owner's approval under the preflight rule.

### 2.4 — The plan has no skill field

The plan schema gets no skill field, and the harness never assigns a skill to a worker. The acting model chooses its skill (ADR-0156 D2), and the `general` worker now gets its catalog (ADR-0156 amendment of 2026-10-18, B1). A structured field adds tokens to every plan. The planner may still write a skill's name into a goal as free text, and nothing enforces otherwise. ADR-0156 B4.2 gives the full reason.

### 2.5 — Alternatives considered

**The full catalog lines for the planner.** About 1,340 tokens. *Rejected as the first form:* it costs about 20 times the names, and the planner does not load skills. If the names fail 2.3, this form is the next candidate, in a new amendment.

**No planner change: the worker fix only.** *Rejected:* the owner ordered both halves (option 3). One expanded turn still costs a planner call, the workers and a synthesis call, where one lane with the skill was enough.

**A harness rule that skips the planner when the message matches a skill.** *Rejected:* ADR-0156 D2 deleted keyword routing.

**The primary decides expansion (design C).** *Rejected* as in Option 2: the primary started workers on 1 of 48 research draws.

**A structured skill field in each task.** *Rejected* as the default (2.4).

### 2.6 — Verification / Acceptance Criteria of this amendment

| # | Criterion | How it is checked | It fails if |
|---|---|---|---|
| A2-1 | The line is in the planner prompt exactly when the decline rule is, with the live names. | A unit test builds the prompt with and without `admit_single`. A seeded fixture skill added to the folder must appear in the line. The `render.py` byte-identity test. | The line is missing with the decline rule, present without it, a name differs from the skill folders, or the seeded skill is missing. |
| A2-2 | The cost is as stated. | The served count of the line, by the FRE-1592 M1 method, in the approved window, posted on the ticket. `planner_input_chars` (D6) of a `planner_asked` turn before and after. | The served count is above 100 tokens, or the system prompt grows by more than the line's length. |
| A2-3 | The probe shows the gain, and only then does the line ship. | The 2.3 run, its scorer report with both arms, the Fisher p-value and the fingerprint, posted on the ticket before merge. | A threshold row fails, an existing D7 row fails, or the gain over the baseline has p ≥ 0.05. In the last case, the line does not ship. |

Production is a floor, not a gate, as in Amendment 1, 1.4. The owner's traffic is too small to give a rate in a short window.

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
