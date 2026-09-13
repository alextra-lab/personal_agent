# FRE-1498 — What the model chooses when nothing forces it: the planner asked, the cap lifted, and how phrasing steers it before the harness does

**Ticket:** FRE-1498 · **Seat:** explore · **Run:** 2026-09-12 20:46 UTC → 2026-09-13 07:26 UTC (local regimes A/B/D; OVH regimes A/D; planner probes on both models) · **Status:** complete; every patch reverted, no default moved

Owner's commission, verbatim (2026-09-12): *"We should test like we did with the limits — ask a
series of queries and let the model do what it wants — subagents, tools, anything — what does this
model choose naturally — it biases. How words and phrases steer it … before the harness does. We
never did that research. We constrained first."*

## Two axes, kept apart

The owner set the rule on 2026-09-13 and this document is built on it. **Choice** is what the model
decided before any worker ran: the planner's raw plan, the primary's first tool call, the effect of
phrasing on either. **Delivery** is whether the machinery turned that choice into an answer: landing
rate, stop reasons, wall clock, what the user received. A delivery failure says nothing about the
merit of a choice, and a choice finding cites nothing downstream of dispatch. Between them sits one
narrow axis, **what the primary received**, which is downstream of the choice and upstream of the
answer; it is reported on its own so it pollutes neither.

Every choice finding below cites only pre-dispatch evidence. Every delivery finding cites only
post-dispatch evidence. The bound AC-6 asks for is a choice-axis statement.

## Verdict, up front

**On the choice axis.** Asked, the model is a better router than the ladder. Given the option to
decline expansion (regime D), the planner declined on all seven command, greeting, tool and
follow-up requests, and expanded on all twelve research-shaped ones. The deterministic ladder had
labelled ten of those twelve "conversational". Phrasing does not move it the way it moves the ladder: across
152 planner draws per model, neither the steering verb nor register (terse against paragraph,
deliverable against process, imperative against question) changed the local planner's decision
beyond its own draw-to-draw variance, while the same verb flips the deterministic classifier every
time. What phrasing of the *prompt* does move is thoroughness: an explicit "fast" collapses every
level to `quick` on the OVH model, and removing the "(20 tool rounds)" annotation, which today
reads the same on all three levels, triples the use of `thorough` on both models. The primary's own economy is its own: at a ceiling of 6 and at 25 it stopped at two
searches on the purchase questions.

**On the delivery axis.** The expansion machinery could not carry the choice on either backend,
for two different reasons. Local: of 32 workers dispatched across the three regimes, 4 landed a
report; the dominant failure is a runaway tool-call argument at rounds 4 to 17 that llama.cpp
answers with a 500, after which every call, the primary's synthesis included, gets a 503 for the
next minute; every fan-out took 5 to 8 times the single lane's wall clock and, in 14 of 16,
delivered an error or the failure trailer. OVH: every fan-out returned in 4 to 7 minutes with zero
model errors and 2 of 28 workers landed, because the schema landing call hands the provider a
frozen mapping it cannot serialise, a shallow-copy defect that has broken every OVH-selected
landing since FRE-1494 merged.

**On the axis between.** Context isolation works when a report lands: 637k characters of search
results reached the primary as 33k tokens on the Mallorca turn. On these questions the single lane
never needed the relief, because it stops at two searches and peaks at 51k of a 131k window. When
landing fails, the primary keeps its tools and re-does the research at full cost.

**The commissioned Arm B measures almost nothing.** With the CONVERSATIONAL and TOOL_USE forcings
removed, 17 of 18 fixtures resolve exactly as under the control, because the complexity heuristic
sends every message under 15 words to `SIMPLE`, and `SIMPLE` still means `SINGLE`. FRE-1394's
cutover, as scoped, changes the strategy of long messages only.

## Structural findings — established before a turn was fired

### S1 — The model never chooses expansion in this architecture

**Verdict: POSITIVE** (a property of the code, confirmed at the deployed revision).

Three facts, read from the deployed container `cloud-sim-seshat-gateway` (fingerprint
`75ef33bca0b9…`) on 2026-09-12:

1. The gateway matrix decides `SINGLE` / `HYBRID` / `DECOMPOSE` deterministically
   (`request_gateway/decomposition.py::_apply_matrix`), and the planner receives that strategy as
   an input. Its user message is literally `Strategy: {strategy}\nQuery: {query}` (`orchestrator/expansion_controller.py:512`).
2. The planner cannot decline. `_validate_plan_json` returns `None` for an empty `tasks` list, and
   `None` reaches the deterministic fallback planner, which always dispatches
   (`expansion_controller.py:1204`, `:380`).
3. No tool lets the primary spawn a worker. `config/governance/tools.yaml` has no spawn, delegate or
   dispatch entry; the only `expand_*` tool is `expand_tool_result`.

So "let the model do what it wants — subagents" had, before this study, no place to be observed.
Regime D exists to give the planner the one option it lacked.

### S2 — The commissioned Arm B is Arm A with a different reason string for short requests

**Verdict: POSITIVE.**

**The query** — `classify_intent` and `_apply_matrix` run offline on every fixture, at this branch's
revision, with and without the two forcings (`PYTHONPATH=src uv run python -c …`).

**Its actual output** — of 19 fixtures, exactly one changes strategy when the forcings are removed:
`c3_paragraph` (64 words, MODERATE → HYBRID). Every message under 15 words is `SIMPLE`, and the
fall-through branch maps `SIMPLE` to `SINGLE`. Susan's four requests, the tuna, heat-pump and
regulatory questions, all stay `SINGLE`. Confirmed live in regime B: 17 of 18 rows carry the same
strategy as regime A (table in M4).

### S3 — AC-5's premise is stale: the planner already runs on the primary role, thinking on

**Verdict: POSITIVE.**

FRE-1390 merged 2026-09-05 (PR #1045). In the deployed container:
`grep -n "role=ModelRole" /app/src/personal_agent/orchestrator/expansion_controller.py` →
`526: role=ModelRole.PRIMARY`. The primary binding is `qwen3.8-flash-next` (`modes.default`,
`enable_thinking: true`); `sub_agent` is `deployment: inherit, mode: worker` (`enable_thinking:
false`). Every local arm therefore has **one served model in every role**, `unsloth/qwen3.8-flash-next`
via llama.cpp at 131072 context, with the planner reasoning and the workers not. AC-5 is discharged
by holding this constant (M2).

## Design as run

| Regime | What decides expansion | Patch (eval image only) | Iteration cap |
|---|---|---|---|
| **A** control | today's matrix | one log line, `fre1498_planner_raw` (proposed task count before the `[:max_tasks]` slice) | per-type (conversational 6) |
| **B** FRE-1394 shape | matrix with CONVERSATIONAL / TOOL_USE forcings removed | + `regime_B.patch` | flat 25 (`AGENT_ORCHESTRATOR_MAX_TOOL_ITERATIONS_BY_TASK_TYPE={}`) |
| **D** planner asked | the planner: every register-classified type reaches it as HYBRID, and it may answer `{"strategy":"SINGLE","tasks":[]}` | + `regime_D.patch` (matrix, planner prompt, validator, controller early return) | flat 25 |

Arm C (phrasing) is a fixture family run inside every regime. The deterministic classification of
each phrasing is computed offline and recorded with the row, so the classifier effect and the model
effect are separable by construction (AC-3). 19 fixtures: Susan's four real requests of 2026-09-08,
five FRE-1337 fixtures, FRE-1487's Mallorca request, and nine phrasing variants (M4).

Every turn ran on the isolated eval gateway with production's behaviour-relevant settings passed
through (M1), against a knowledge graph wiped before every turn (M3), one turn at a time on one
llama-server, with generation confirmed by a trivial completion before each fixture.

Two owner-directed additions during the run: the same regimes A and D with the OVH primary
selected on every turn and workers dispatched concurrently (D6, C7, P4); and a planner-only probe,
four prompt variants × 19 fixtures × 2 trials on each model, no worker dispatched (C6).

---

## Choice-axis findings

### C1 — Asked, the planner declines every command and expands every research-shaped question

**Verdict: POSITIVE.**

**The query** — regime D, 19 fixtures, the `fre1498_planner_raw` and `planner_completed` events of
each turn in the eval ES (`agent-logs-*`, `term trace_id`), which record the raw plan before any
worker is dispatched.

**Its actual output:**

| Planner said | Fixtures | Ladder's label |
|---|---|---|
| `SINGLE`, no tasks (declined) | susan_1 "run the nfl-prediction tool" · susan_2 "able to run the tool and provide 5 picks" · susan_3 "I thought I asked you to create…" · susan_4 "build + run that now" · greeting · tool_logs "check the logs" · c5_noverb "What events are happening in Palma…" | 5 conversational, 2 tool_use |
| `HYBRID`, 1–3 tasks | gpsr_tuna (2) · heatpump (3) · regulatory (3) · mallorca (3) · c1_tool_imperative (1, fallback) · c2_verb (3) · c2_noverb (3) · c3_terse (2) · c3_paragraph (1, fallback) · c4_deliverable (2) · c4_process (2) · c5_verb (3) | 8 conversational, 2 analysis, 1 tool_use, 1 conversational/moderate |

Seven declines, twelve expansions. Of the twelve, the ladder labelled ten `conversational`, and would
have routed them `SINGLE` with a six-round cap. Two planner calls failed validation (c1, c3_paragraph)
and reached the fallback planner, which dispatched one worker each; those two are fallback
decisions, not the model's.

The decision is not a length gate. c5_noverb (14 words) was declined; c3_terse (7 words) was
expanded. It is not the ladder's keyword either: c2_noverb has no steering verb and was expanded
with three workers.

### C2 — The steering verb flips the classifier every time and the planner not at all

**Verdict: POSITIVE.** This is AC-3's separation, and the live run and the probe disagree in a way
that matters.

**The query** — the two verb pairs: deterministic classification offline; the planner's live
decision from regime D (one draw each); the planner's decision over eight draws each from the
planner-only probe (C6, four prompt variants × two trials, local model); the primary's pre-dispatch
tool calls from regimes A and B.

**Its actual output:**

| Pair | Phrasing | Classifier | Planner, live D (1 draw) | Planner, probe (declines / 8) | Primary alone (A / B) |
|---|---|---|---|---|---|
| c5 | "**Investigate** what events are happening in Palma de Mallorca between 19 and 26 September 2026" | analysis / simple | HYBRID, 3 | 0 / 8 | 2 searches + 3 fetches / 2 + 2 |
| c5 | "What events are happening in Palma de Mallorca between 19 and 26 September 2026" | conversational / simple | **declined** | 1 / 8 | 3 searches + 3 fetches / 3 + 1 |
| c2 | "**Research** what's changed in the EU's revised General Product Safety Regulation" | analysis / simple | HYBRID, 3 | 1 / 8 | 3 searches / 4 + 1 fetch |
| c2 | "What's changed in the EU's revised General Product Safety Regulation" | conversational / simple | HYBRID, 3 | 1 / 8 | 2 searches / 1 |

The classifier flips on both pairs; that is F19's incantation, reproduced. The live regime D run
showed the planner flipping on the Palma pair, and that was a one-in-eight draw: over eight draws
each, both phrasings of both pairs expand, and the single decline on the verb-less Palma form is
within the planner's own variance (C6: trial agreement on decline 66 of 76 on the local model). The
primary alone shows no verb effect in tool count either. **The steering verb is a property of the
ladder, not of the model.** The owner's incantation works because it moves a regex, and for no
other reason.

### C3 — Register does not move the decision: terse against paragraph, deliverable against process, imperative against question

**Verdict: POSITIVE.**

| Pair | Phrasing | Classifier | Planner asked (D) | Primary alone (A / B), tool calls |
|---|---|---|---|---|
| c3 | "Best heat pump, 1930s stone house, Brittany" (7 words) | conversational / simple | HYBRID, 2 | 2 / 2 |
| c3 | the same need as a 64-word paragraph | conversational / **moderate** | HYBRID (fallback, 1) | 4 / 4 (B: HYBRID 2, the one B divergence) |
| c4 | "Give me five picks for this week's NFL games" | conversational / simple | HYBRID, 2 | 5 / 4 |
| c4 | "Work out which five NFL games this week are the best bets and tell me why" | conversational / simple | HYBRID, 2 | 6 / 6 |
| c1 | "Use the web_search tool to find out which tinned tuna I should buy in France" | tool_use / simple | HYBRID (fallback, 1) | 3 / 2 |
| c1 | "Which tinned tuna should I buy in France" | conversational / simple | HYBRID, 2 | 2 / 2 |

The planner expanded on every one of these. The primary's tool count differs by at most two
within a pair. The one register effect in the whole study is the ladder's own: the paragraph
crosses the 40-word MODERATE threshold and, under B, becomes the only fixture whose strategy the
cutover changes. Naming the tool (`web_search`) moved the ladder to `tool_use` and nothing else.

### C4 — The primary's economy is its own, not the countdown's

**Verdict: POSITIVE.**

**The query** — the same 18 single-lane fixtures under regime A (per-type ceiling, conversational =
6) and regime B (flat 25), `tool_call_completed` counts and the primary's `input_tokens` per call.

**Its actual output** — tool calls A → B: unchanged on 9 fixtures, ±1 on 6, ±2 on 3. Web searches
on the purchase questions: tuna 2 → 2, heat pump 2 → 2. The cap bound once in the whole series:
the regulatory question under A, `tool_budget_warning_injected` at the fifth search; under B the
same question used four. Peak primary context under A 51k tokens (c5_verb), under B 35k. Nothing
in B suggests a primary held back by 6 rounds. The primary is told every round to "prioritize
synthesis", and it does.

### C5 — The ladder's label and the primary's behaviour disagree on the "conversational" bucket, in the direction F14 measured

**Verdict: POSITIVE.**

Under regime A, of 16 fixtures labelled `conversational` or `tool_use`, 12 used the web, and every
one of Susan's four "conversational" requests called a tool (`bash`, `search_memory`,
`recall_personal_history`, `find_linear_issues`). The greeting alone made no call. F14's population
figure (128 of 339 conversational turns searched) reproduces on this fixture set: the label
describes the absence of a keyword, not the shape of the work.

### C6 — The planner asked directly, 152 draws per model: the decision is stable per fixture, model-specific, and the prompt's time proxy shapes thoroughness

**Verdict: POSITIVE.** Owner-directed addition, 2026-09-13 03:14 UTC.

**The query** — the planner call alone, no worker dispatched: the app's own planner system prompt
(with regime D's decline rule) and the app's own user message `Strategy: HYBRID\nQuery: …`, sent
directly to each backend through its OpenAI-compatible endpoint (local via Caddy, OVH via its
endpoint with the managed token). 19 fixtures × 4 prompt variants × 2 trials = 152 draws per model.
Variants: **current** (the prompt as shipped plus the decline rule); **no_rounds** (the same with the
"(20 tool round(s))" annotation removed from every level); **fast** (plus *"The user has said they
want this answered within a couple of minutes"*); **slow** (plus *"The user has said to take the time
needed for accuracy and quality; they do not mind waiting"*). Temperature 1.0, the primary role's
own sampling. Raw plan parsed; 3 of 304 draws unparseable (all OVH), excluded.

**Its actual output — per variant:**

| Model | Variant | Declined | Mean tasks when expanding | quick / standard / thorough |
|---|---|---|---|---|
| local `qwen3.8-flash-next`, thinking on | current | 13 / 38 (34%) | 2.44 | 11% / 84% / 5% |
| | no_rounds | 14 / 38 (37%) | 2.54 | 8% / 75% / 16% |
| | fast | 15 / 38 (39%) | 2.39 | 15% / 76% / 9% |
| | slow | 12 / 38 (32%) | 2.58 | 4% / 73% / 22% |
| OVH `Qwen3.8-27B` | current | 4 / 38 (11%) | 2.35 | 8% / 84% / 9% |
| | no_rounds | 3 / 38 (8%) | 2.23 | 12% / 56% / 32% |
| | fast | 3 / 36 (8%) | 2.00 | 82% / 18% / 0% |
| | slow | 3 / 37 (8%) | 2.21 | 11% / 56% / 33% |

**Per fixture, all variants and trials pooled (declines / draws), local model:** susan_1 8/8 ·
susan_2 8/8 · susan_3 5/8 · susan_4 8/8 · greeting 8/8 · tool_logs 8/8 · gpsr_tuna 1/8 · heatpump
0/8 · regulatory 0/8 · mallorca 0/8 · c1 2/8 · c2_verb 1/8 · c2_noverb 1/8 · c3_terse 0/8 ·
c3_paragraph 0/8 · c4_deliverable 3/8 · c4_process 0/8 · c5_verb 0/8 · c5_noverb 1/8. **OVH:**
greeting 8/8; susan_1 2/8; susan_2 2/7; tool_logs 1/8; every other fixture 0.

Four things this settles:

1. **The local planner's decision is a stable property of the request.** Six fixtures decline in
   every draw under every prompt; eleven expand in seven or eight of eight. Regime D's live split
   (C1) was not luck. Trial-to-trial agreement on decline: 66 of 76.
2. **The decision is model-specific.** The OVH model declines only the greeting, and plans one or
   two workers even for "Please run the nfl-prediction tool" and "Check the logs". Same prompt,
   same temperature. "What the model chooses" has no single answer across the two Qwen 3.8 sizes.
3. **An explicit appetite line barely moves the decision and strongly moves thoroughness.**
   On the local model, "fast" adds two declines and "slow" removes one against "current", inside
   the variance. On OVH, "fast" collapses thoroughness to 82% `quick` and task count to 2.0;
   "slow" raises `thorough` to a third. The planner treats the appetite as a scope instruction,
   not as a go/no-go.
4. **The shipped prompt's round annotation suppresses `thorough`.** Every level reads "(20 tool
   round(s))" today (D5). Removing that annotation, and changing nothing else, raises `thorough`
   from 5% to 16% on the local model and from 9% to 32% on OVH. The planner reads three equal
   numbers as "the levels do not differ" and picks the middle one.

---

## The axis between — what the primary received

### P1 — Isolation works when a report lands, and the single lane never needed it here

**Verdict: POSITIVE.**

| Turn | Workers absorbed | Primary's synthesis input | Same question, single lane (A) | Reply |
|---|---|---|---|---|
| c4_deliverable, D, both workers landed | 423k chars, 33 searches | 26,434 tokens | 16,405 tokens, 3 searches + 2 fetches | 4.2k vs 2.2k chars |
| mallorca, A (control HYBRID), one report landed | 637k chars, 37 searches | 32,564 tokens | never runs single | 15.6k chars |
| c5_verb, D, all three workers failed | 532k chars | 28k, then the primary re-searched itself to 53,234 | 51,434 tokens | 6.1k chars |

Isolation is real: 637k characters became 33k tokens at the primary. It is also not yet the limit:
the single lane's largest context in 18 fixtures was 51k of a 131k window, because the single lane
stops at two or three searches. The fan-out reads twenty times more and hands the primary a context
larger, not smaller, than the single lane's on the same question. The relief pays where the single
lane would overflow, on deep research, which is where the local worker fails to land (D1).

### P2 — When landing fails, isolation is undone by re-search

**Verdict: POSITIVE.** The primary keeps its tools after synthesis (ADR-0149 D6). On c5_verb (D),
after three failed workers and 30 minutes, it made 7 more tool calls and grew to 53k tokens, the
single lane's size. On mallorca (D), 5 more calls after three failures.

### P3 — The synthesis context is larger than the reports it carries

**Verdict: POSITIVE, cause UNVERIFIED.** c4_deliverable (D): two landed reports of 8,998 and 10,709
characters (`route_traces.sub_agents.summary_chars`), roughly 5k tokens, arrived as a synthesis call
of 26,434 input tokens against an 8,349-token base. About 13k tokens are unaccounted for by the
reports. The capture's `context_messages` is empty and `llm_call_messages_debug` fires only for the
first primary call, so the composition could not be read back. Recorded as a measurement to make,
not a defect.

---

## Delivery-axis findings

### D1 — 4 of 32 workers landed a report on the local backend

**Verdict: POSITIVE.**

**The query** — every sub-agent capture of the three local regimes (`agent-captains-captures-subagents-*`
in the eval ES, `term trace_id` per turn), `success`, `stop_reason`, `tool_iterations`.

**Its actual output:** 32 dispatched (A: 2, B: 2, D: 28); 4 landed (all in D: regulatory 1 of 3,
c2_verb 1 of 3, c4_deliverable 2 of 2). Stop reasons across the 28 failures: `error` 20, `timeout` 3,
`time_reserve` 1, `completed` with a report the primary could not use 4 (three narration, one
empty ledger). The workers that landed did so at 8, 8, 10 and 10 rounds; the failures stopped at 0
to 17.

### D2 — The failure is one event: a runaway tool-call argument, a 500, then a 503 storm

**Verdict: POSITIVE.**

**The query** — `model_call_error` events, eval ES, 2026-09-12 22:35 → 23:10 UTC, ordered by time.

**Its actual output** (trace e575d9e9, then 1abde463):

```
22:53:49 sub_agent  500  Failed to parse tool call arguments as JSON: parse error at line 1, column 12014 … unexpected end of input
22:54:08 sub_agent  503  Backend server unreachable … LiteLLM Retried: 3 times
22:54:09 primary    503  Backend server unreachable
23:05:44 sub_agent  500  Failed to parse tool call arguments as JSON: … column 11461 … missing closing quote
23:05:49 sub_agent  503  Backend server unreachable
23:05:53 sub_agent  503  Backend server unreachable
23:05:54 primary    503  Backend server unreachable
```

The worker's round-7 (resp. round-4) call has `finish_reason: length` with 1,024 characters of
arguments recorded, i.e. the model generated to its ceiling inside a tool-call argument. llama.cpp
rejects the parse with a 500, and within 20 seconds every call to the origin, including the primary's
synthesis, returns 503. The origin goes down behind one malformed call. This is the shape
FRE-1487 found in the stderr pipe: an origin-side failure that our timeouts and retries then pay for
twice. Across the night the eval ES holds 30 `model_call_error` events on 9 traces; every one is a
500 parse error, a 503 that follows one, or a 600-second generation timeout.

### D3 — Every fan-out took 5 to 8 times the single lane's wall clock on the same question

**Verdict: POSITIVE.**

| Fixture | Single lane, A | Fan-out, D |
|---|---|---|
| gpsr_tuna | 2 searches, 105 s, answer delivered | 2 researchers, 12 searches, 800 s, `RUN_ERROR` |
| heatpump | 2 searches, 107 s, answer delivered | 3 researchers, 6 searches, 520 s, `RUN_ERROR` |
| c3_terse | 2 searches, 237 s | 2 researchers, 24 searches, 1,492 s, timeout |
| c4_deliverable | 3 searches + 2 fetches, 137 s | 2 researchers, 31 searches, 2,065 s, both landed |
| c2_noverb | 2 searches, 163 s | 3 researchers, 43 searches, 3,600 s, lifetime cap |

The single lane's slowest fixture was 388 s. The fan-out's fastest was 520 s. Workers run one at a
time behind one llama-server (FRE-1380, owner-directed), each round absorbs 15 to 20k characters,
prefill is superlinear in accumulated context (FRE-1487), and a failed landing costs a 600-second
generation budget plus three litellm retries before the sibling is even started.

### D4 — What the user receives on a failed landing is the ledger

**Verdict: POSITIVE.** On B's one fan-out (c3_paragraph, both workers 503), the delivered reply was
4,665 characters opening *"The model call failed before I could finish, but here's what I gathered:"*,
followed by the worker's brief, its error, and its tool-call ledger with byte counts. On D's
c2_noverb, 40,069 characters opening *"This turn was stopped early — it exceeded its 3600s lifetime
cap."* That is ADR-0149 D4's deterministic trailer doing its job: nothing is hidden. It is also not
an answer, and in the single lane the same questions were answered in 3 to 4k characters.

### D5 — Every thoroughness level buys 20 rounds

**Verdict: POSITIVE.** `sub_agent_rounds_by_thoroughness` is unset in production and in the eval
gateway, so `quick`, `standard` and `thorough` all resolve to the cap of 20, and the planner prompt
says so: *"quick (20 tool round(s)), standard (20 tool round(s)), thorough (20 tool round(s))"*. The
planner chose `quick` for four briefs tonight and each got 20 rounds. The four workers that landed
stopped by themselves at 8 to 10.

### D6 — On the OVH worker every fan-out returns in 4 to 7 minutes with zero model errors, and 2 of 28 workers land, because the schema landing call cannot be serialised for that provider

**Verdict: POSITIVE.** Owner-directed arm, 2026-09-13 02:27 UTC: the same fixtures and regimes
with `model=qwen3.8-27b-ovh` selected on every turn (the primary, and by `inherit` every worker, on
OVH's `Qwen3.8-27B`), and, at the owner's direction, workers dispatched concurrently
(`concurrent_dispatch.patch`, eval image only). Regimes A and D ran; B was stopped before its first
row because locally it reproduced A on 17 of 18 fixtures.

**The query** — OVH regime D, 19 rows; sub-agent captures, `sub_agent_forced_synthesis` events
(`dialect`, `schema_active`), `expansion_dispatch_intervals`, `model_call_error` count per trace.

**Its actual output:**

| | Local worker (regime D) | OVH worker (regime D) |
|---|---|---|
| Fan-outs | 12 | 13 |
| Workers dispatched / landed | 28 / 4 | 28 / 2 |
| `model_call_error` events | 30 across the series | 0 |
| Failure shape | `error` 20 (runaway argument → 500 → 503), `timeout` 3, `time_reserve` 1 | `completed` with a ledger 26: the worker finished its rounds and said DONE, and the landing call raised |
| Fan-out wall clock | 520 – 3,600 s | 213 – 738 s |
| Primary reached synthesis | 6 of 12 | 13 of 13 |
| Delivered reply | 175 – 40,542 chars, 8 of 12 an error or a trailer | 3,396 – 16,784 chars, every one an answer synthesised from the ledgers |

The landing failure is one defect, verbatim from the worker's own trailer: *"no model-written
report was possible because the synthesis call failed on provider 'ovhcloud' (dialect 'ovh_qwen'):
LiteLLM call failed: litellm.APIConnectionError: OvhcloudException - Object of type mappingproxy
is not JSON serializable."* `LANDING_ACCEPTS_JSON_SCHEMA[Dialect.OVH_QWEN]` is `True`
(`llm_client/models.py:259`), so the landing call carries `response_format`; that payload is built
by `dict(WORKER_REPORT_RESPONSE_FORMAT)` (`orchestrator/sub_agent.py:1039`), a shallow copy whose
nested schema is still the `MappingProxyType` declared at `worker_types.py:158`. Reproduced offline
on this branch: `json.dumps(dict(WORKER_REPORT_RESPONSE_FORMAT))` → *"Object of type mappingproxy
is not JSON serializable"*. The llama.cpp path tolerates it; the litellm `ovhcloud` provider does
not. Every schema-backed landing on an OVH-selected turn has failed since FRE-1494 merged
(2026-09-12 05:25 UTC); nobody saw it because the owner's sessions select the local primary.

**Concurrency did what it was asked and nothing more.** `expansion_dispatch_intervals` on the OVH
Mallorca turn: two workers, `0.03 → 321 s` and `0.03 → 251 s`, side by side. Fan-out wall clock fell
to the slowest worker plus synthesis. Neither the planner's decisions (below) nor the primary's
first move changed, as C-axis theory said they would not.

### C7 — The OVH planner, asked live, makes the same split as the local one on 18 of 19 fixtures

**Verdict: POSITIVE.** OVH regime D, one draw each: declined on Susan's four, the greeting and the
log check; expanded on the twelve research-shaped fixtures plus c5_noverb (which the local planner
declined in its single live draw and in 1 of 8 probe draws). Task counts differ by one on seven
fixtures. The probe (C6) is the better instrument for the cross-model comparison and puts the OVH
decline rate at 8–11% against the local 32–39%; the live run's agreement is the live run's own
draw.

### P4 — On OVH the primary's synthesis input is 33 to 42k tokens on every fan-out, against 15 to 46k for the same questions in the single lane

**Verdict: POSITIVE.** OVH regime D synthesis inputs: 32,512 – 42,491 tokens on all thirteen
fan-outs, with every report arriving as a ledger. OVH regime A single-lane peak inputs on the same
fixtures: 14,214 – 45,834. The ledger a failed landing hands the primary is as large as a report
would have been; the isolation dividend depends on the report being written, which on OVH it never
was.

---

## Instrument findings

### I1 — The eval gateway as shipped is a different agent from production (FRE-1464, live)

**Verdict: POSITIVE.** Detail in M1. Memory graph off, every recall path off, no embedder, no
grounding, no Docker socket, turn bounds 900 / 1800, a Postgres volume from 2026-08-30 whose schema
made every route-trace write fail (`route_trace_write_failed` in the eval ES, nothing in the
container log). About 70 `AGENT_*` keys set in production and absent in eval.

### I2 — The isolation runner's client timeout is below the turn's own bound

**Verdict: POSITIVE.** `scripts/eval/eval_isolation.py` posts `/chat` with `timeout=1200.0`; the
turn lifetime is 3600 s. A fan-out that outlives the client is not cancelled on the gateway, the next
fixture starts on top of it, and the single backend saturates. Nine regime D turns were lost to the
client this way and recovered from the eval store afterwards (M5); one regime A turn likewise.

### I3 — The FRE-1341 fingerprint hashes bytecode

**Verdict: POSITIVE.** One offline `import personal_agent.request_gateway.intent` from the worktree
wrote `.pyc` files under `src/` and made the running eval image "stale". Every offline run in this
study set `PYTHONDONTWRITEBYTECODE=1`.

### I4 — `route_traces.channel` reads `CHAT` on a turn sent with `channel=EVAL`

**Verdict: POSITIVE.** Every turn in this study was posted with `channel=EVAL`; the capture's
`eval_mode` reads `True` on every one; `route_traces.channel` reads `CHAT` on every one. FRE-1487's
"the turn ran on CHAT, not EVAL" finding may have read this column.

### I5 — Planner truncation never occurred, and is now observable

**Verdict: NEGATIVE — arms carried.** Across 33 planner calls with a raw plan, no `raw_task_count`
exceeded `max_tasks`. **Arm 1:** the `fre1498_planner_raw` event is emitted by this study's own
patch at the deployed eval revision, and fired on every planner call (33 instances, `raw_task_count`
0–3). **Arm 2:** the same query with `planner_completed` as the identifier returns 33. **Arm 3:**
scope is the eval ES, this study's three local regimes, 2026-09-12 20:46 → 2026-09-13 04:14 UTC.

---

## Acceptance criteria, as run

| AC | Result | Evidence |
|---|---|---|
| AC-1 unforced arm is unforced | **PASS** | regime B, 18 rows: `unforced_matrix_simple_single` 15, `analysis_simple` 2, `unforced_matrix_moderate_hybrid` 1; zero `conversational_always_single` / `tool_use_single` |
| AC-2 behaviour recorded per turn | **PASS** | every row carries tool count and names, web-search count and result counts, fetch count, per-call primary input tokens and growth, wall clock (HTTP and capture), budget exhaustion, strategy and reason, sub-agent count, each worker's type and thoroughness, the planner's proposed count; trace ids in M4 |
| AC-3 phrasing separated from classifier | **PASS** | C2: both effects reported per pair; the c2 pair classifies differently and plans identically |
| AC-4 seeded agreement | **PASS** | greeting and Susan's four: identical shape in A, B and D (declined) |
| AC-5 planner confound | **PASS** | S3 / M2: one model in every role; planner thinking on, workers off; held constant |
| AC-6 a bound, not only a finding | **PASS** | Proposals P1–P3 |
| AC-7 no default moved | **PASS** | M5: `git diff --stat` on `src/` empty after restore; the patches live outside the repo |

## Proposals

Ten. The first three are the choice-axis bound AC-6 asks for; the rest are delivery, instrument,
and design items, each traceable to a finding above. Master dispositions each one.

**P1 — Let the planner decline, and make that the expansion decision (regime D in production).**
Evidence: C1, C6, C7, S1. Keep the shape-of-work forcings (MEMORY_RECALL, SELF_IMPROVE,
DELEGATION); route every register-classified type to the planner as HYBRID with the decline rule;
a declined plan returns the turn to the ordinary tool loop with no synthesis message (the
96-line regime D patch is the implementation, minus its study comments). **The bound on unforced
expansion, in the form FRE-1394 and FRE-1382 can consume:** expansion occurs only when the
planner, asked, returns at least one task; at most three workers per turn (the HYBRID cap, unchanged);
per-level round budgets of quick 3, standard 6, thorough 10, set from where landed workers stopped
(8–10) and where the single lane answers (2–3); and FRE-1393's spend pause as the turn-level
control. What sets it: 152 planner draws per model showing a stable per-fixture decision, and
zero cases in 57 local turns where the ladder expanded a request the planner declined. Deserves an
ADR: it moves the routing decision from Stage 5 to the model, which ADR-0142 argued for and
ADR-0036 argued against.

**P2 — FRE-1394 as scoped is the cap half of its own ticket; adopt P1 for the routing half.**
Evidence: S2, C4, C5. Removing the two forcings changes strategy for messages over 40 words only;
under 15 words nothing moves, and that is 81 of 339 real conversational turns (F14). Delete the
per-type cap as FRE-1394 says (the primary is not cap-bound, C4, so the cost is small); do not
expect the matrix fall-through to change allocation for 78% of traffic.

**P3 — Give the three thoroughness levels three different budgets, and stop printing equal
numbers to the planner.** Evidence: D5, C6 item 4. `sub_agent_rounds_by_thoroughness` unset means
every level is 20 and the prompt says so; removing the annotation alone tripled `thorough` on both
models. Set the values in P1 and let the annotation render them.

**P4 — Fix the OVH landing: deep-copy `WORKER_REPORT_RESPONSE_FORMAT`, and test that the payload
serialises.** Evidence: D6. `dict()` of a nested `MappingProxyType` is not a copy. One line in
`_landing_response_format`, one test that `json.dumps` the result. Every OVH-selected fan-out has
landed nothing since 2026-09-12. Bug ticket filed (below).

**P5 — Bound the local worker's tool-call generation, and report the origin's 500 → 503 storm to
slm_server.** Evidence: D1, D2. 20 of 28 local failures are a tool-call argument generated to the
token ceiling; llama.cpp's parse rejection takes the origin down behind it. On our side: a
`finish_reason: length` inside a tool call must end the worker's loop, not be retried three times.
On the origin's side: a malformed tool call should return 400, not fell the server. Bug ticket filed.

**P6 — After a failed landing, the primary must not redo the research.** Evidence: P2 (axis
between). ADR-0149 D6 keeps the primary's tools after synthesis; with the trailer reporting three
failed workers it re-searched to 53k tokens. Either the post-synthesis tool budget is zero when the
fan-out failed, or the turn returns the partial per ADR-0149 D4 and asks.

**P7 — Scope the briefs to the question as asked.** Evidence: owner's reading of the briefs
(2026-09-13 02:33), D3. The planner turns an 8-word question into two research commissions and the
researcher prompt spends the whole budget on each. One planner rule, measurable with the probe
before it ships: a worker fetches the specifics the answer needs, it does not review the field.

**P8 — Decompose the synthesis context.** Evidence: P3 (axis between). Two reports of 20k
characters arrived as 18k tokens above base; the composition is unreadable today. Emit one event
per synthesis call with the message size and each report's size.

**P9 — Instrument: the isolation runner's client timeout must exceed the turn lifetime, and it
must wait for the gateway to go idle.** Evidence: I2, M3. Also attach M1's parity list to
FRE-1464 rather than a new ticket. Small ticket filed.

**P10 — Dispatch cloud workers concurrently; keep FRE-1380's serialisation for the local
backend.** Evidence: D6 (owner-directed arm). Wall clock fell to the slowest worker plus synthesis;
decisions did not move. Condition on the worker deployment's `placement: cloud`, not on a global
setting.

## Filed tickets

All `Backlog`, none promoted (ADR-0135 D5):

- **FRE-1500** — every schema-backed landing on an OVH-selected turn fails: `dict(WORKER_REPORT_RESPONSE_FORMAT)` is a shallow copy (P4, D6).
- **FRE-1501** — the local worker's runaway tool-call argument, llama.cpp's 500, and the origin's 503 storm (P5, D1, D2).
- **FRE-1502** — ADR needed: the planner decides expansion, with the bound (P1, P3, P7).
- **FRE-1503** — `eval_isolation`'s client timeout below the turn lifetime, the idle wait, and committing this harness (P9, I2).

Comments, not tickets: FRE-1464 (the parity list of M1) and FRE-1394 (S2, the scope finding).

## Method appendix

### M1 — where the turns ran, and what "today's harness" meant

The isolated eval stack (`docker-compose.eval.yml`, FRE-375 / FRE-1372): `seshat-gateway-treatment`
on :9003, `postgres-eval` :5434, `neo4j-eval` :7689, `elasticsearch-eval` :9202, `redis-eval`.
Built from this worktree (`context: .`), image tagged `seshat-gateway:fre1498`, brought up under
`-p seshat` with `--no-deps` so `caddy` and `searxng` resolve by name and nothing production-owned
is recreated. The FRE-1341 freshness guard was asserted before every regime's run.

**The eval gateway as shipped is not today's harness.** It declares no `env_file`, so it ran the
`settings.py` defaults: memory graph off (Neo4j never connected), proactive memory off, multipath /
relevance-bounded / lexical / multiquery recall off, `substrate_profile=private` (no managed
embedder), grounding off, location off, no Docker socket (so `run_python` could not sandbox), task
timeout 900 s and turn lifetime 1800 s against production's 3600 / 3600. For this study the
behaviour-relevant subset was passed through from `/opt/seshat/.env` by compose interpolation
(memory, recall, embedder, reranker, grounding, location, second brain, skill routing, owner name,
history depth, Perplexity and Linear keys, the two turn bounds), the socket was mounted, and the eval
Postgres volume was re-initialised from the current `init.sql`. Deliberately not passed: the sysgraph
DSN (production Postgres), R2, freshness, insights.

Remaining, recorded differences from production: MCP off (production carries the browser tools;
the eval primary sees 16 tools, read from `tools_passed_to_llm`), approval UI off (headless),
`AGENT_DELEGATION_ENABLED=true` (production: off; no coding-shaped fixture is in the set).

### M2 — the model in every role, per arm

From the eval container's `/app/config/model_roles.yaml` and `config/models.yaml` at the built
revision, and from `tools_passed_to_llm` / `sub_agent_start` / `model_call_completed.model` per
turn: primary `qwen3.8-flash-next` (`modes.default`, thinking on) · planner = primary role
(FRE-1390) · sub_agent `inherit` + `mode: worker` (thinking off) · one served model,
`unsloth/qwen3.8-flash-next`, llama.cpp, 131072 context, `UD-IQ4_XS`, confirmed by `GET /v1/models`
through Caddy before the run. Sub-agent tool surface: `web_search`, `run_python`, `search_memory`,
`recall_personal_history` (governance-granted; `fetch_url` refused).

### M3 — controls, as inherited from FRE-1487 and as actually applied

- **Channel.** Every turn `POST /chat?channel=EVAL` via `IsolatedArmRunner`; the capture's
  `eval_mode: True` is the evidence (I4).
- **Contamination.** `neo4j-eval` wiped before every turn; each turn waited out
  `entity_extraction_completed` (90 s) before the next wipe. The graph is empty at every turn's
  start by construction.
- **One backend, serialised.** One llama-server; one turn at a time. Two windows broke this
  control: 22:39–01:31 and 01:57–02:41 UTC, when the client timeout (I2) let turns overlap.
  Timing figures from those windows are marked ᴿ in M4 and are not used in D3.
- **Generation confirmed.** A trivial 8-token completion through Caddy before every fixture;
  the runner stopped once, at 01:31 UTC, on a Cloudflare 502, and resumed at 01:36.
- **Window.** Owner-granted 2026-09-12 20:45 → 2026-09-13 06:00 UTC; no new local fixture started
  after 05:00 UTC.

### M4 — the fixture set, its classification, and every row

The 19 fixtures, their deterministic classification, and the per-regime rows with trace ids are in
the tables appendix (A1). Rows marked ᴿ were recovered from the eval store after a client timeout;
their choice columns are intact, their wall clock is confounded by overlap.

### M5 — instrument notes and restore

- Patches: `regime_A_obs.patch` (20 lines), `regime_B.patch` (72), `regime_D.patch` (96),
  `concurrent_dispatch.patch` (86, OVH arm only), applied to the worktree before each eval build and
  reverted after; overrides `override.yml`, `override-unforced.yml`, `override-prodparity.yml`.
  Runner, recovery and probe scripts in the explore scratchpad; listed in M6.
- Restore: `git checkout -- src/` on the worktree; eval gateway stopped; eval substrate containers
  down. Verified at the end of the OVH arm (`git diff --stat` empty).
- Eval ES fields are `text` + `.keyword`; the captures indices have no `@timestamp`; `route_traces`
  in `postgres-eval` needed the current `init.sql`.

### M6 — reproducing this

Everything below lived in the explore seat's scratchpad and nothing in it entered the repo; the
patches are reproduced in Appendix A2 because they are the substance of the regimes.

- `fixtures.yaml` — the 19 fixtures (A1 lists every message).
- `override.yml` (image tag, socket, turn bounds), `override-prodparity.yml` (M1's pass-through),
  `override-unforced.yml` (the flat cap) — compose overrides for `seshat-gateway-treatment`.
- `regime.sh A|B|D|restore` — `git checkout -- src/`, apply the regime's patch (plus
  `concurrent_dispatch.patch` when `CONCURRENT=1`), rebuild and restart the gateway under
  `-p seshat --no-deps`, with `PYTHONDONTWRITEBYTECODE=1` and `src/**/__pycache__` cleared.
- `runner.py` — `IsolatedArmRunner` per turn, then the read-back from `elasticsearch-eval` and
  `postgres-eval`; one JSONL row per turn; `--model` selects the primary; `--deadline` refuses to
  start a fixture after an instant; waits for gateway idle before each fixture.
- `recover.py` — rebuilds a row from the eval store for a turn whose client call timed out.
- `planner_probe.py` — the planner call alone, four prompt variants, either backend.
- `tables.py` — the A1 tables from the rows.
- `run_all.sh`, `run_ovh.sh`, `run_probe_local.sh` — the unattended drivers.

Committing the runner and the probe under `scripts/eval/fre1498/` is proposed in the P9 ticket.

## Appendix A1 — the fixtures and every row

Columns: classifier = deterministic task type / complexity; planner = the raw plan's strategy / task count before the cap slice (— when no planner call); workers (landed) = dispatched (with a usable report); ctx max = the primary's largest input in tokens (on a fan-out whose synthesis never ran this is the planner call alone, about 500); secs = HTTP wall clock of the /chat call, excluding the isolation waits; ᴿ = row recovered from the eval store after a client timeout.

### The fixtures

| label | group | message |
|---|---|---|
| susan_1_run_tool | susan | Please run the nfl-prediction tool |
| susan_2_able_to_run | susan | Are you able to run the tool and provide 5 top picks? |
| susan_3_thought_i_asked | susan | I thought I ask you to create a weekly prediction tool to help me make my weekly picks. |
| susan_4_build_and_run | susan | Please go ahead and build + run that now |
| greeting | agreement | How is your day going? |
| tool_logs | agreement | Check the logs for errors in the last hour. |
| gpsr_tuna | research_short | Which tinned tuna should I buy in France |
| heatpump | research_short | Which heat pump should I install in a 1930s stone house in Brittany |
| regulatory | research_short | I need to understand how the EU AI Act's transparency duties differ from the GDPR's for a product we're shipping in France. |
| mallorca | research_long | I will visit Mallorca Sept 19th, 2026 and car return Saturday 26th, 2026. 2 people staying at the Myseahoue Flamingo - Mision de San Diego, 2 07600 Playa de Palma Espagne. Research any interesting events taking place that week - 2026. We will have a car. Propose towns and beaches to visit. We love history, archeology, museums, botanical gardens, beautiful views, relaxation, and great food. My husband does not eat shell fish. |
| c1_tool_imperative | c1 | Use the web_search tool to find out which tinned tuna I should buy in France |
| c2_verb | c2 | Research what's changed in the EU's revised General Product Safety Regulation |
| c2_noverb | c2 | What's changed in the EU's revised General Product Safety Regulation |
| c3_terse | c3 | Best heat pump, 1930s stone house, Brittany |
| c3_paragraph | c3 | We live in a 1930s stone house in Brittany, quite draughty, with old radiators and no insulation in the walls. We are thinking about replacing the oil boiler with a heat pump but we are not sure which kind would actually work in a house like this, or whether it makes sense at all given the climate here. What would you suggest we install? |
| c4_deliverable | c4 | Give me five picks for this week's NFL games |
| c4_process | c4 | Work out which five NFL games this week are the best bets and tell me why |
| c5_verb | c5 | Investigate what events are happening in Palma de Mallorca between 19 and 26 September 2026 |
| c5_noverb | c5 | What events are happening in Palma de Mallorca between 19 and 26 September 2026 |


### Local, regime A — run 2026-09-12

| fixture | classifier | strategy / reason | planner | workers (landed) | worker stops | tools | web | ctx max | secs | reply chars | error | trace |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| susan_1_run_tool | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | bash×1 | 0 | 8,576 | 19 | 653 |  | `c5db6f27` |
| susan_2_able_to_run | tool_use/simple | single / tool_use_single | —/— | 0 (0) | — | search_memory×1 | 0 | 8,548 | 27 | 779 |  | `cdcba584` |
| susan_3_thought_i_asked | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | recall_personal_history×1, search_memory×1, bash×1, find_linear_issues×1 | 0 | 9,043 | 37 | 1128 |  | `4b9ab19d` |
| susan_4_build_and_run | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | recall_personal_history×1, bash×1, search_memory×1 | 0 | 8,956 | 29 | 831 |  | `5659ed91` |
| greeting | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | — | 0 | 8,345 | 9 | 297 |  | `3ecf6833` |
| tool_logs | tool_use/simple | single / tool_use_single | —/— | 0 (0) | — | bash×3 | 0 | 16,455 | 52 | 493 |  | `5dde63c6` |
| gpsr_tuna | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 23,113 | 105 | 2127 |  | `9ddf518b` |
| heatpump | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 15,920 | 107 | 3269 |  | `c7b00dec` |
| regulatory | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×5 | 5 | 43,607 | 359 | 6620 |  | `81c4a8d0` |
| mallorca ᴿ | analysis/moderate | hybrid / analysis_moderate_hybrid | HYBRID/2 | 2 (0) | timeout×1, completed×1 | web_search×37 | 37 | 32,564 | 2752 | 15588 |  | `4c4796f2` |
| c1_tool_imperative | tool_use/simple | single / tool_use_single | —/— | 0 (0) | — | web_search×2, fetch_url×1 | 2 | 21,374 | 255 | 2499 |  | `4ca59d47` |
| c2_verb | analysis/simple | single / analysis_simple | —/— | 0 (0) | — | web_search×3 | 3 | 15,671 | 256 | 4667 |  | `06e3e914` |
| c2_noverb | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 12,643 | 163 | 3819 |  | `a10ee130` |
| c3_terse | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 21,550 | 237 | 3075 |  | `fea7c222` |
| c3_paragraph | conversational/moderate | single / conversational_always_single | —/— | 0 (0) | — | web_search×4 | 4 | 31,261 | 330 | 4267 |  | `63122215` |
| c4_deliverable | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×3, fetch_url×2 | 3 | 16,405 | 137 | 2227 |  | `4da21fc5` |
| c4_process | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×3, fetch_url×3 | 3 | 24,500 | 257 | 3554 |  | `fe7ab54c` |
| c5_verb | analysis/simple | single / analysis_simple | —/— | 0 (0) | — | fetch_url×3, web_search×2 | 2 | 51,434 | 388 | 4382 |  | `707fbc08` |
| c5_noverb | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×3, fetch_url×3 | 3 | 34,684 | 200 | 3637 |  | `7451136e` |

### Local, regime B — run 2026-09-12

| fixture | classifier | strategy / reason | planner | workers (landed) | worker stops | tools | web | ctx max | secs | reply chars | error | trace |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| mallorca | | ANALYSIS/MODERATE -> analysis_moderate_hybrid is untouched by the B patch; B == A for this fixture. Skipped for the run window. | | | | | | | | | | |
| susan_1_run_tool | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | bash×1 | 0 | 8,642 | 24 | 889 |  | `655dadae` |
| susan_2_able_to_run | tool_use/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | search_memory×1 | 0 | 8,541 | 25 | 911 |  | `13da1bdb` |
| susan_3_thought_i_asked | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | find_linear_issues×1, search_memory×1, recall_personal_history×1, bash×1 | 0 | 9,055 | 34 | 1321 |  | `9760893b` |
| susan_4_build_and_run | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | recall_personal_history×1, search_memory×1, bash×1 | 0 | 8,987 | 32 | 888 |  | `8a93af62` |
| greeting | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | — | 0 | 8,345 | 12 | 424 |  | `0b269e9a` |
| tool_logs | tool_use/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | bash×2 | 0 | 16,216 | 42 | 293 |  | `b7fb554a` |
| gpsr_tuna | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×2 | 2 | 24,716 | 119 | 2346 |  | `299a5d54` |
| heatpump | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×2 | 2 | 18,220 | 90 | 3899 |  | `ca57141a` |
| regulatory | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×4 | 4 | 17,792 | 142 | 5479 |  | `c478ecc5` |
| c1_tool_imperative | tool_use/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×2 | 2 | 25,748 | 103 | 2666 |  | `a8b6514e` |
| c2_verb | analysis/simple | single / analysis_simple | —/— | 0 (0) | — | web_search×4, fetch_url×1 | 4 | 24,357 | 164 | 6304 |  | `bf90056a` |
| c2_noverb | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×1 | 1 | 12,139 | 60 | 3457 |  | `c9cbeb00` |
| c3_terse | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×2 | 2 | 22,528 | 111 | 3342 |  | `26c65e9e` |
| c3_paragraph | conversational/moderate | hybrid / unforced_matrix_moderate_hybrid | HYBRID/2 | 2 (0) | error×2 | web_search×4 | 4 | 462 | 393 | 4665 | LLMServerError | `14fb12de` |
| c4_deliverable | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×2, fetch_url×2 | 2 | 17,278 | 117 | 1542 |  | `9f150c5f` |
| c4_process | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×3, fetch_url×3 | 3 | 23,820 | 171 | 3313 |  | `fc0e759c` |
| c5_verb | analysis/simple | single / analysis_simple | —/— | 0 (0) | — | web_search×2, fetch_url×2 | 2 | 34,847 | 177 | 3903 |  | `35b4d07f` |
| c5_noverb | conversational/simple | single / unforced_matrix_simple_single | —/— | 0 (0) | — | web_search×3, fetch_url×1 | 3 | 26,597 | 167 | 3182 |  | `6dc0b940` |

### Local, regime D — run 2026-09-12

| fixture | classifier | strategy / reason | planner | workers (landed) | worker stops | tools | web | ctx max | secs | reply chars | error | trace |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| susan_1_run_tool | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | read_skill×1 | 0 | 8,574 | 40 | 329 |  | `7d36b243` |
| susan_2_able_to_run | tool_use/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | search_memory×1 | 0 | 8,556 | 26 | 551 |  | `8e943d05` |
| susan_3_thought_i_asked | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | recall_personal_history×1, search_memory×1, bash×1 | 0 | 8,957 | 43 | 1105 |  | `cac989b1` |
| susan_4_build_and_run | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | recall_personal_history×1, find_linear_issues×1, search_memory×1 | 0 | 11,304 | 51 | 1445 |  | `a51fa135` |
| greeting | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | — | 0 | 8,345 | 12 | 324 |  | `270db889` |
| tool_logs | tool_use/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | bash×2 | 0 | 16,416 | 58 | 533 |  | `1fb5e0d1` |
| gpsr_tuna | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | error×2 | web_search×12 | 12 | 490 | 800 | 6341 | LLMServerError | `e575d9e9` |
| heatpump | conversational/simple | hybrid / planner_asked | HYBRID/3 | 3 (0) | error×2, completed×1 | web_search×6 | 6 | 499 | 520 | 5933 | LLMServerError | `1abde463` |
| regulatory ᴿ | conversational/simple | hybrid / planner_asked | HYBRID/3 | 3 (1) | completed×2, error×1 | web_search×53 | 53 | 507 | 2435 | 40542 | LLMTimeout | `51e47d3c` |
| mallorca ᴿ | analysis/moderate | hybrid / planner_asked | HYBRID/3 | 3 (0) | error×2, timeout×1 | web_search×33 | 33 | 53,263 | 2595 | 254 | LLMServerError | `2dc6153f` |
| c1_tool_imperative ᴿ | tool_use/simple | hybrid / planner_asked | —/— (fallback) | 1 (0) | error×1 | web_search×16 | 16 | 35,263 | 980 | 254 | LLMServerError | `e0ac8b82` |
| c2_verb ᴿ | analysis/simple | hybrid / planner_asked | HYBRID/3 | 3 (1) | completed×1, timeout×1, error×1 | web_search×41 | 41 | 41,954 | 2132 | 189 | LLMTimeout | `8722eaa9` |
| c2_noverb ᴿ | conversational/simple | hybrid / planner_asked | HYBRID/3 | 3 (0) | time_reserve×1, completed×1, error×1 | web_search×43 | 43 | 493 | 3600 | 39931 |  | `19ad6075` |
| c3_terse ᴿ | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | error×2 | web_search×24 | 24 | 495 | 1492 | 9546 | LLMTimeout | `3ca6b176` |
| c3_paragraph ᴿ | conversational/moderate | hybrid / planner_asked | —/— (fallback) | 1 (0) | error×1 | web_search×10 | 10 | 23,882 | 525 | 175 | LLMConnectionError | `6bca4ece` |
| c4_deliverable ᴿ | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (2) | completed×2 | web_search×31 | 31 | 26,434 | 2089 | 4206 |  | `cd6b94ff` |
| c4_process ᴿ | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | error×2 | web_search×47 | 47 | 30,486 | 2144 | 232 | LLMServerError | `729aac1a` |
| c5_verb | analysis/simple | hybrid / planner_asked | HYBRID/3 | 3 (0) | error×3 | web_search×37, fetch_url×1 | 37 | 53,234 | 1838 | 6072 |  | `554326c2` |
| c5_noverb | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | web_search×2, fetch_url×2 | 2 | 31,481 | 174 | 3462 |  | `3f3ac2ee` |


### OVH, regime A — run 2026-09-13-ovh

| fixture | classifier | strategy / reason | planner | workers (landed) | worker stops | tools | web | ctx max | secs | reply chars | error | trace |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| susan_1_run_tool | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | bash×1, search_memory×1 | 0 | 8,970 | 22 | 851 |  | `dcaea9f8` |
| susan_2_able_to_run | tool_use/simple | single / tool_use_single | —/— | 0 (0) | — | recall_personal_history×1, search_memory×1 | 0 | 8,607 | 19 | 770 |  | `15e7cea4` |
| susan_3_thought_i_asked | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | recall_personal_history×1, search_memory×1, read_skill×1, bash×1 | 0 | 10,280 | 28 | 1746 |  | `d593ce41` |
| susan_4_build_and_run | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | search_memory×2, recall_personal_history×1 | 0 | 8,792 | 27 | 719 |  | `f6c17ce5` |
| greeting | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | — | 0 | 8,338 | 4 | 178 |  | `8e3e46e3` |
| tool_logs | tool_use/simple | single / tool_use_single | —/— | 0 (0) | — | bash×2 | 0 | 16,146 | 16 | 353 |  | `58d130f8` |
| gpsr_tuna | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2, search_memory×1, fetch_url×1 | 2 | 19,810 | 47 | 2650 |  | `0c200a95` |
| heatpump | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2, search_memory×1 | 2 | 15,745 | 52 | 4275 |  | `1b3a83a9` |
| regulatory | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 15,162 | 63 | 4026 |  | `77c0d334` |
| mallorca | analysis/moderate | hybrid / analysis_moderate_hybrid | HYBRID/2 | 2 (1) | completed×2 | web_search×23, search_memory×2, run_python×2 | 23 | 41,431 | 477 | 14387 |  | `7287c1a6` |
| c1_tool_imperative | tool_use/simple | single / tool_use_single | —/— | 0 (0) | — | web_search×2 | 2 | 21,074 | 64 | 2594 |  | `9d5b631e` |
| c2_verb | analysis/simple | single / analysis_simple | —/— | 0 (0) | — | web_search×2, fetch_url×2 | 2 | 16,800 | 55 | 3686 |  | `18ca5ac0` |
| c2_noverb | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 14,214 | 68 | 3441 |  | `3214183d` |
| c3_terse | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×4 | 4 | 36,513 | 77 | 4377 |  | `beb79711` |
| c3_paragraph | conversational/moderate | single / conversational_always_single | —/— | 0 (0) | — | web_search×3 | 3 | 23,630 | 74 | 4541 |  | `e7f98926` |
| c4_deliverable | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2 | 2 | 14,634 | 38 | 1909 |  | `bc555607` |
| c4_process | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×4, fetch_url×3 | 4 | 45,834 | 93 | 4144 |  | `c183ccdd` |
| c5_verb | analysis/simple | single / analysis_simple | —/— | 0 (0) | — | web_search×3 | 3 | 26,985 | 62 | 4574 |  | `3c2dec25` |
| c5_noverb | conversational/simple | single / conversational_always_single | —/— | 0 (0) | — | web_search×2, fetch_url×2 | 2 | 24,060 | 65 | 3489 |  | `7fe79a2a` |

### OVH, regime D — run 2026-09-13-ovh

| fixture | classifier | strategy / reason | planner | workers (landed) | worker stops | tools | web | ctx max | secs | reply chars | error | trace |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| susan_1_run_tool | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | bash×3, search_memory×1 | 0 | 9,220 | 46 | 1125 |  | `fe999cfa` |
| susan_2_able_to_run | tool_use/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | search_memory×2, recall_personal_history×1 | 0 | 8,760 | 20 | 738 |  | `6d476ef6` |
| susan_3_thought_i_asked | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | recall_personal_history×2, search_memory×2 | 0 | 8,922 | 28 | 825 |  | `4f1894f8` |
| susan_4_build_and_run | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | search_memory×2, recall_personal_history×1, bash×1 | 0 | 8,977 | 21 | 484 |  | `31dcc829` |
| greeting | conversational/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | — | 0 | 8,338 | 8 | 111 |  | `c4f1b5d4` |
| tool_logs | tool_use/simple | hybrid / planner_asked | SINGLE/0 | 0 (0) | — | bash×2 | 0 | 16,270 | 34 | 418 |  | `b95aed61` |
| gpsr_tuna | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×19 | 19 | 37,189 | 238 | 7203 |  | `c06d0ae5` |
| heatpump | conversational/simple | hybrid / planner_asked | HYBRID/3 | 3 (0) | completed×3 | web_search×32 | 32 | 36,293 | 289 | 12652 |  | `f87e66d4` |
| regulatory | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×34 | 34 | 34,888 | 293 | 16784 |  | `07f9c267` |
| mallorca | analysis/moderate | hybrid / planner_asked | HYBRID/2 | 2 (1) | completed×2 | run_python×16, web_search×13, search_memory×3 | 13 | 42,491 | 737 | 13202 |  | `4cc41171` |
| c1_tool_imperative | tool_use/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×16 | 16 | 37,466 | 213 | 5676 |  | `8301b9ab` |
| c2_verb | analysis/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×24 | 24 | 33,867 | 275 | 10453 |  | `2910bcd9` |
| c2_noverb | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×17 | 17 | 35,924 | 271 | 10547 |  | `54b8dcaa` |
| c3_terse | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×20 | 20 | 33,736 | 313 | 7854 |  | `40616d4c` |
| c3_paragraph | conversational/moderate | hybrid / planner_asked | HYBRID/3 | 3 (1) | completed×3 | web_search×22, search_memory×3, run_python×2 | 22 | 35,295 | 271 | 9084 |  | `e8e93d93` |
| c4_deliverable | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×25 | 25 | 33,096 | 296 | 3396 |  | `81311f78` |
| c4_process | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×28 | 28 | 32,512 | 307 | 4965 |  | `4aa0635a` |
| c5_verb | analysis/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×41 | 41 | 35,007 | 318 | 8139 |  | `21a92317` |
| c5_noverb | conversational/simple | hybrid / planner_asked | HYBRID/2 | 2 (0) | completed×2 | web_search×24 | 24 | 40,380 | 236 | 7001 |  | `e00537b9` |


## Appendix A2 — the regime patches (eval image only; reverted)

### regime_A_obs.patch

```diff
diff --git a/src/personal_agent/orchestrator/expansion_controller.py b/src/personal_agent/orchestrator/expansion_controller.py
index 3d3d0285..6994a50e 100644
--- a/src/personal_agent/orchestrator/expansion_controller.py
+++ b/src/personal_agent/orchestrator/expansion_controller.py
@@ -550,6 +550,26 @@ class ExpansionController:
             result.planner_cost_usd = float(raw_response.get("cost_usd") or 0.0)
             plan = _validate_plan_json(raw_response["content"], strategy)
 
+            # FRE-1498 study (eval image only, never shipped): record how many tasks
+            # the planner PROPOSED before _validate_plan_json's [:max_tasks] slice.
+            try:
+                _raw_plan = json.loads(raw_response["content"])
+                _raw_tasks = _raw_plan.get("tasks") if isinstance(_raw_plan, dict) else None
+                _raw_task_count = len(_raw_tasks) if isinstance(_raw_tasks, list) else None
+                _raw_strategy = _raw_plan.get("strategy") if isinstance(_raw_plan, dict) else None
+            except (json.JSONDecodeError, TypeError, AttributeError):
+                _raw_task_count, _raw_strategy = None, None
+            logger.info(
+                "fre1498_planner_raw",
+                raw_task_count=_raw_task_count,
+                raw_strategy=_raw_strategy,
+                given_strategy=strategy,
+                max_tasks=_MAX_TASKS.get(strategy, _MAX_TASKS["HYBRID"]),
+                content_chars=len(raw_response["content"] or ""),
+                finish_reason=raw_response.get("finish_reason"),
+                trace_id=trace_id,
+            )
+
             if plan is not None:
                 result.phase_results.append(
                     PhaseResult(
```

### regime_B.patch

```diff
diff --git a/src/personal_agent/orchestrator/expansion_controller.py b/src/personal_agent/orchestrator/expansion_controller.py
index 3d3d0285..6994a50e 100644
--- a/src/personal_agent/orchestrator/expansion_controller.py
+++ b/src/personal_agent/orchestrator/expansion_controller.py
@@ -550,6 +550,26 @@ class ExpansionController:
             result.planner_cost_usd = float(raw_response.get("cost_usd") or 0.0)
             plan = _validate_plan_json(raw_response["content"], strategy)
 
+            # FRE-1498 study (eval image only, never shipped): record how many tasks
+            # the planner PROPOSED before _validate_plan_json's [:max_tasks] slice.
+            try:
+                _raw_plan = json.loads(raw_response["content"])
+                _raw_tasks = _raw_plan.get("tasks") if isinstance(_raw_plan, dict) else None
+                _raw_task_count = len(_raw_tasks) if isinstance(_raw_tasks, list) else None
+                _raw_strategy = _raw_plan.get("strategy") if isinstance(_raw_plan, dict) else None
+            except (json.JSONDecodeError, TypeError, AttributeError):
+                _raw_task_count, _raw_strategy = None, None
+            logger.info(
+                "fre1498_planner_raw",
+                raw_task_count=_raw_task_count,
+                raw_strategy=_raw_strategy,
+                given_strategy=strategy,
+                max_tasks=_MAX_TASKS.get(strategy, _MAX_TASKS["HYBRID"]),
+                content_chars=len(raw_response["content"] or ""),
+                finish_reason=raw_response.get("finish_reason"),
+                trace_id=trace_id,
+            )
+
             if plan is not None:
                 result.phase_results.append(
                     PhaseResult(
diff --git a/src/personal_agent/request_gateway/decomposition.py b/src/personal_agent/request_gateway/decomposition.py
index def07943..287c44e6 100644
--- a/src/personal_agent/request_gateway/decomposition.py
+++ b/src/personal_agent/request_gateway/decomposition.py
@@ -98,10 +98,10 @@ def _apply_matrix(
     Returns:
         Tuple of (strategy, reason).
     """
+    # FRE-1498 regime B (eval image only, never shipped): the CONVERSATIONAL and
+    # TOOL_USE forcings are removed, so both fall through to the complexity matrix
+    # at the bottom — FRE-1394's post-cutover shape.
     match task_type:
-        case TaskType.CONVERSATIONAL:
-            return DecompositionStrategy.SINGLE, "conversational_always_single"
-
         case TaskType.MEMORY_RECALL:
             return DecompositionStrategy.SINGLE, "memory_recall_always_single"
 
@@ -128,9 +128,6 @@ def _apply_matrix(
                         "delegation_no_target_fallback_decompose",
                     )
 
-        case TaskType.TOOL_USE:
-            return DecompositionStrategy.SINGLE, "tool_use_single"
-
         case TaskType.ANALYSIS:
             match complexity:
                 case Complexity.SIMPLE:
@@ -140,9 +137,9 @@ def _apply_matrix(
                 case _:
                     return DecompositionStrategy.DECOMPOSE, "analysis_complex_decompose"
 
-        case _:  # TaskType.PLANNING (and any future task types)
+        case _:  # PLANNING, CONVERSATIONAL, TOOL_USE (FRE-1498 regime B)
             match complexity:
                 case Complexity.SIMPLE:
-                    return DecompositionStrategy.SINGLE, "planning_simple"
+                    return DecompositionStrategy.SINGLE, "unforced_matrix_simple_single"
                 case _:
-                    return DecompositionStrategy.HYBRID, "planning_moderate_hybrid"
+                    return DecompositionStrategy.HYBRID, "unforced_matrix_moderate_hybrid"
```

### regime_D.patch

```diff
diff --git a/src/personal_agent/orchestrator/expansion_controller.py b/src/personal_agent/orchestrator/expansion_controller.py
index 3d3d0285..38402a87 100644
--- a/src/personal_agent/orchestrator/expansion_controller.py
+++ b/src/personal_agent/orchestrator/expansion_controller.py
@@ -136,10 +136,17 @@ def _build_planner_system_prompt(available_sub_agent_tools: list[str]) -> str:
         "You are a task decomposition planner. Given a user query and a strategy, "
         "produce a JSON plan that breaks the query into independent sub-tasks.\n\n"
         "Output ONLY valid JSON matching this schema:\n"
-        '{"strategy": "HYBRID|DECOMPOSE", "tasks": [{"name": "string", '
+        '{"strategy": "SINGLE|HYBRID|DECOMPOSE", "tasks": [{"name": "string", '
         f'"goal": "string", "constraints": ["string"], "type": "{type_names}", '
         f'"thoroughness": "{level_names}"}}]}}\n\n'
         "Rules:\n"
+        # FRE-1498 regime D (eval image only, never shipped): the planner may decline.
+        "- First decide whether this query needs independent sub-tasks at all. If one "
+        "assistant working alone, with the same tools, would answer it well in one pass "
+        "— a greeting, a short factual question, a single lookup, a follow-up — output "
+        '{"strategy": "SINGLE", "tasks": []} and nothing else. Choose HYBRID or '
+        "DECOMPOSE only when splitting the work into independent sub-tasks would "
+        "produce a better answer than one pass\n"
         "- Each task must be independently answerable. No task sees another task's "
         "result, and the final answer is written from all task reports together, so "
         "do not add a task that combines or synthesises other tasks' results\n"
@@ -370,6 +377,13 @@ class ExpansionController:
         )
         result.plan = plan
 
+        # FRE-1498 regime D (eval image only, never shipped): the planner declined.
+        # No workers, no synthesis context, not degraded — the executor returns to
+        # LLM_CALL and the primary answers in the ordinary tool loop.
+        if plan is not None and plan.strategy == "SINGLE" and not plan.tasks:
+            logger.info("fre1498_planner_declined", strategy_given=strategy, trace_id=trace_id)
+            return result
+
         if not plan or not plan.tasks:
             result.degraded = True
             result.degradation_reason = "No valid plan produced"
@@ -550,6 +564,26 @@ class ExpansionController:
             result.planner_cost_usd = float(raw_response.get("cost_usd") or 0.0)
             plan = _validate_plan_json(raw_response["content"], strategy)
 
+            # FRE-1498 study (eval image only, never shipped): record how many tasks
+            # the planner PROPOSED before _validate_plan_json's [:max_tasks] slice.
+            try:
+                _raw_plan = json.loads(raw_response["content"])
+                _raw_tasks = _raw_plan.get("tasks") if isinstance(_raw_plan, dict) else None
+                _raw_task_count = len(_raw_tasks) if isinstance(_raw_tasks, list) else None
+                _raw_strategy = _raw_plan.get("strategy") if isinstance(_raw_plan, dict) else None
+            except (json.JSONDecodeError, TypeError, AttributeError):
+                _raw_task_count, _raw_strategy = None, None
+            logger.info(
+                "fre1498_planner_raw",
+                raw_task_count=_raw_task_count,
+                raw_strategy=_raw_strategy,
+                given_strategy=strategy,
+                max_tasks=_MAX_TASKS.get(strategy, _MAX_TASKS["HYBRID"]),
+                content_chars=len(raw_response["content"] or ""),
+                finish_reason=raw_response.get("finish_reason"),
+                trace_id=trace_id,
+            )
+
             if plan is not None:
                 result.phase_results.append(
                     PhaseResult(
@@ -1174,6 +1208,10 @@ def _validate_plan_json(
         return None
 
     tasks_raw = data.get("tasks")
+    # FRE-1498 regime D (eval image only, never shipped): a declined expansion is
+    # a valid plan — SINGLE with no tasks — not a validation failure.
+    if data.get("strategy") == "SINGLE" and (tasks_raw is None or tasks_raw == []):
+        return ExpansionPlan(strategy="SINGLE", tasks=[], is_fallback=False)
     if not isinstance(tasks_raw, list) or len(tasks_raw) == 0:
         return None
 
diff --git a/src/personal_agent/request_gateway/decomposition.py b/src/personal_agent/request_gateway/decomposition.py
index def07943..1648d55e 100644
--- a/src/personal_agent/request_gateway/decomposition.py
+++ b/src/personal_agent/request_gateway/decomposition.py
@@ -98,9 +98,14 @@ def _apply_matrix(
     Returns:
         Tuple of (strategy, reason).
     """
+    # FRE-1498 regime D (eval image only, never shipped): every register-classified
+    # type reaches the planner as HYBRID, and the planner may answer
+    # {"strategy": "SINGLE", "tasks": []} to decline expansion. The decision is
+    # then the model's, not the matrix's. MEMORY_RECALL / SELF_IMPROVE / DELEGATION
+    # keep their shape-of-work forcings (FRE-1394's own distinction).
     match task_type:
-        case TaskType.CONVERSATIONAL:
-            return DecompositionStrategy.SINGLE, "conversational_always_single"
+        case TaskType.CONVERSATIONAL | TaskType.TOOL_USE | TaskType.ANALYSIS | TaskType.PLANNING:
+            return DecompositionStrategy.HYBRID, "planner_asked"
 
         case TaskType.MEMORY_RECALL:
             return DecompositionStrategy.SINGLE, "memory_recall_always_single"
```

### concurrent_dispatch.patch

```diff
diff --git a/src/personal_agent/orchestrator/expansion_controller.py b/src/personal_agent/orchestrator/expansion_controller.py
index 3d3d0285..7970bb7b 100644
--- a/src/personal_agent/orchestrator/expansion_controller.py
+++ b/src/personal_agent/orchestrator/expansion_controller.py
@@ -764,11 +764,12 @@ class ExpansionController:
             phase=Phase.EXPANSION,
             detail=f"{len(specs)} sub-agents",
         ) as _parent_id:
-            for task, spec in zip(plan.tasks, specs, strict=True):
-                # FRE-1397: recomputed fresh for every task rather than divided
-                # up-front across the plan — most sub-agents finish well under
-                # their own ceiling, so a live "whatever's left" check wastes
-                # none of that headroom on tasks earlier in the loop.
+            # FRE-1498 OVH arm (eval image only, never shipped): concurrent dispatch,
+            # owner-directed 2026-09-13. Every task starts at once; results keep plan
+            # order; the redispatch-on-gap step runs afterwards, sequentially, as before.
+            async def _dispatch_one(
+                task: PlanTask, spec: SubAgentSpec
+            ) -> tuple[SubAgentResult | Exception | None, SubAgentInterval | None]:
                 worker_max_deadline: float | None = None
                 if turn_deadline_monotonic is not None:
                     remaining = turn_deadline_monotonic - time.monotonic()
@@ -779,11 +780,10 @@ class ExpansionController:
                             trace_id=trace_id,
                         )
                         result.skipped_tasks.append(task.name)
-                        continue
+                        return None, None
                     worker_max_deadline = remaining
-
                 interval_start = time.monotonic()
-                sub_result: SubAgentResult | None = None
+                outcome: SubAgentResult | Exception | None = None
                 try:
                     async with phase_span(
                         session_id=session_id,
@@ -791,7 +791,7 @@ class ExpansionController:
                         detail=spec.task[:80],
                         parent_id=_parent_id,
                     ):
-                        sub_result = await run_sub_agent(
+                        outcome = await run_sub_agent(
                             spec=spec,
                             llm_client=llm_client,
                             trace_id=trace_id,
@@ -802,25 +802,25 @@ class ExpansionController:
                             authenticated=authenticated,
                         )
                 except Exception as exc:
-                    raw_results.append(exc)
-                else:
-                    raw_results.append(sub_result)
-                finally:
-                    intervals.append(SubAgentInterval(task.name, interval_start, time.monotonic()))
-
-                if sub_result is None:
+                    outcome = exc
+                return outcome, SubAgentInterval(task.name, interval_start, time.monotonic())
+
+            pairs = list(zip(plan.tasks, specs, strict=True))
+            outcomes = await asyncio.gather(*(_dispatch_one(task, spec) for task, spec in pairs))
+            logger.info("fre1498_concurrent_dispatch", task_count=len(pairs), trace_id=trace_id)
+            for (task, spec), (outcome, interval) in zip(pairs, outcomes, strict=True):
+                if interval is not None:
+                    intervals.append(interval)
+                if outcome is None:
                     continue
-                sub_results.append(sub_result)
-
-                # FRE-1389 AC-5: single-shot replacement dispatch when this
-                # result stated a tool gap — the controller (not the
-                # sub-agent) decides whether to grant more, acting in-loop so
-                # the replacement is always paired with the right task even
-                # if an earlier task in this same plan raised a raw exception.
+                raw_results.append(outcome)
+                if isinstance(outcome, Exception):
+                    continue
+                sub_results.append(outcome)
                 replacement = await self._maybe_redispatch_on_gap(
                     task=task,
                     spec=spec,
-                    original_result=sub_result,
+                    original_result=outcome,
                     llm_client=llm_client,
                     trace_id=trace_id,
                     session_id=session_id,
```


