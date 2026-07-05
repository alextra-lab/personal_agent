# FRE-472 — The `conversational` capability trap: a behavioral tool-runway floor

**Date:** 2026-07-05 · **Type:** Research / measurement (measure-don't-assert)
**Ticket:** FRE-472 (Seshat Inference Architecture) · **Backing incident:** post-mortem
`docs/postmortems/2026-06-04-artifact-turn-failure-cache-control.md`
**Related:** FRE-469 (point-fix, Done), FRE-497 (self-correcting gates, Approved), FRE-391 (dynamic
max_tokens, Approved), FRE-432/447 (pedagogical North Star), FRE-256/210 (the recurring family).
**Scope guard (from the ticket, non-negotiable):** any runway lever must be **conditioned on the turn
actually emitting native tool calls**, never a global raise of the conversational cap. Ordinary
conversational turns are the Socratic layer and must stay cheap.
**Status of conclusions:** the mechanism is code-grounded (citations at `origin/main` `1f7e39e`); the
rates are measured on ~20 days of production telemetry (n=1558 classified turns); the recommended floor
*value* is a calibration parameter to A/B, not a measured optimum.

---

## TL;DR

1. **`conversational` is not a class, it is the fallthrough.** The intent classifier is a deterministic
   first-match-wins regex ladder (no LLM, no tiebreak). `conversational` is the *default* branch reached
   only when none of the six positive pattern banks match — hard-coded to `confidence=0.7`, signal
   `no_special_patterns` (`intent.py:341-348`). Measured: **every** one of the 1107 conversational
   classifications carries `confidence=0.7` — the 0.7↔conversational mapping is exact. **0.7 is a
   sentinel for "nothing matched," not a calibrated confidence.** 71% of all turns land in this default.

2. **The "conversational ⇒ no tools" assumption is wrong 53% of the time.** Of 1107 conversational
   turns, **587 (53.0%) emitted at least one native tool call.** The low-capability default
   (tool-iteration cap **6** vs **25** for tool_use/analysis/planning; `settings.py:197-212`) is being
   applied to turns that provably need tools half the time.

3. **The low cap causes disproportionate starvation — measured, not asserted.** Among *tool-emitting*
   turns (the only ones the cap can bite), conversational hits the hard iteration limit at **3.4%** vs
   **0.5%** for tool_use — ~7× the starvation rate for the *same kind of work*. **87% of all
   iteration-limit hits and 74% of all budget warnings in the whole corpus land on conversational
   turns.** Conversational and tool_use tool-emitting turns have near-identical tool-call-count
   distributions; they differ in the *budget granted*, not the work done.

4. **The scope guard is already satisfiable — for free.** `ctx.tool_iteration_count` is incremented
   *only* inside `step_tool_execution`, which is reached *only* when the model emitted tool calls
   (`executor.py:3715`, `3567-3568`). So **`tool_iteration_count >= 1` already is** the "this turn
   actually emitted a tool call" predicate the ticket demands. A floor conditioned on it touches
   *only* tool-using turns; a chit-chat turn keeps count 0, cap 6, and trips nothing. No new signal,
   no classifier change, North Star preserved by construction. **This is the systemic fix.**

5. **Validation retries silently burn the scarce budget.** The iteration counter is incremented
   *unconditionally at the top* of `step_tool_execution`, **before** either arg-validation layer runs
   (`executor.py:3715` precedes `tool_dispatch.py:82-113` and `executor.py:397-416`). A tool call
   rejected for a missing required param (never executed, `latency_ms:0`) costs a full iteration of a
   cap of 6, with no decrement and no free-retry allowance. Measured 18 conversational turns hit this.

6. **The "budget-pressure → bad output" hypothesis does NOT survive population measurement.** Warned
   conversational emitters fail tools at 15.8% vs 12.1% unwarned — a weak ~1.3× lift, fully confounded
   by call-count (turns that make more calls both get warned *and* have more chances to fail). The
   post-mortem's single-incident inference (pressure → 10.5K one-shot plan) is plausible for that trace
   but is **not** a measurable population effect. Downgrade it.

**Recommendation in one line:** decouple capability from the (wrong-half-the-time) initial
classification and re-couple it to observed behavior — a **behavioral tool-runway floor** gated on
`tool_iteration_count >= 1`. Do **not** touch the classifier, do **not** add an LLM tiebreak.

---

## 1. Method & data

- **Code:** all citations verified at `origin/main` `1f7e39e` (the tip this note branched from).
  `ORCHESTRATOR_MAX_TOOL_ITERATIONS` is commented out in the primary `.env`, so the `settings.py`
  defaults (global 25; conversational 6, memory_recall 8, rest 25) are the live values.
- **Telemetry:** Elasticsearch `agent-logs-*`, `event_type` key (per the ES event-key convention),
  retention window 2026-06-16 → 2026-07-05 (~20 days). n = **1558** `intent_classified` events.
  Turns joined across event types on `trace_id` (distinct-trace sets intersected offline).
- **Honesty note:** `tool_call_started` counts *individual* tool calls; the iteration counter increments
  once per assistant round (a parallel batch = one iteration). So per-turn `tool_call_started` counts
  over-state iterations for batched turns. Every *starvation* claim below therefore rests on the
  un-confounded `tool_iteration_limit_reached` / `tool_budget_warning_injected` events, not on
  tool-call counts. Tool-call histograms are used only qualitatively (work-shape similarity).

---

## 2. The trap mechanism (code-grounded)

The full chain from "unclassifiable input" to "starved turn":

| Step | Site | Behavior |
|------|------|----------|
| Classify | `intent.py:224-349` | Deterministic first-match ladder, 6 positive banks then a default. No LLM (`intent.py:6,226-227`), no low-confidence tiebreak. |
| Fallthrough | `intent.py:341-348` | No bank matched ⇒ `TaskType.CONVERSATIONAL`, `confidence=0.7`, `signals=['no_special_patterns']`. |
| Decompose | `decomposition.py:100-101` | `conversational_always_single` — forced `SINGLE`, no HYBRID/DECOMPOSE, no sub-agent expansion. |
| Cap | `settings.py:197-212` → `executor.py:105-122` | `effective_max = min(by_task_type[6], global[25]) + bonus` = **6** iterations. |
| Loop | `executor.py:3715` | `ctx.tool_iteration_count += 1` per tool round (unconditional, top of `step_tool_execution`). |
| Warn | `executor.py:3091,3096-3100` | At `count >= max-2`, inject a `user` message: *"⚠️ Tool budget: {N} tool call(s) remaining. Prioritize synthesis — only make additional tool calls if they are strictly necessary to answer the user's question."* |
| Exhaust | `executor.py:3719,3070-3088` | At `count > max`: offer a user constraint-pause (`continue_10` → `+10` bonus); else set `force_synthesis_from_limit`, strip tools (`tool_choice="none"`, FRE-484 override `1365-1394`), inject *"You have reached the tool call limit. Do NOT call any more tools…"* |

The sole escape hatch today is **FRE-469's `_ARTIFACT_BUILD_REGEX`** (`intent.py:82-89,125`, Done +
live), which redirects "build me a / make an interactive …" to `TOOL_USE` (cap 25) *before* the
default. That is a point-fix on one phrasing — the third in the family (FRE-256 tools-stripped, FRE-210
recall-missed, FRE-469 runway-starved). Each new mis-routing phrasing needs another regex. That
treadmill is the problem this note is chartered to end.

**The confidence value carries no information.** `confidence` is hard-coded per branch (0.9 memory,
0.85 self-improve/coding, 0.8 planning/analysis/tool_use, **0.7 default**). It is not computed from any
feature; a 0.7 means "fell through," full stop. The measurement confirms this: 0.7 appears on exactly
the 1107 conversational turns and nowhere else.

---

## 3. Area 1 — Min tool-runway floor (the core question)

**Hypothesis (ticket):** once a turn is actually emitting tool calls, a cap of 6 starves it mid-work; a
floor conditioned on tool-emission would let it finish or trigger continuation, without raising the
budget of turns that never call tools.

**Measured — the misroute and its cost:**

| Metric | Value |
|--------|-------|
| Turns classified `conversational` | 1107 / 1558 (**71.1%**) |
| … that emitted ≥1 tool call | **587 (53.0%)** |
| Conversational turns hitting the **hard iteration limit** | 20 (1.8% of all conv; **3.4% of conv emitters**) |
| Conversational turns getting a **budget warning** | 190 (17.2% of all conv; **32.4% of conv emitters**) |
| Share of **all** iteration-limit hits that are conversational | 20/23 = **87%** |
| Share of **all** budget warnings that are conversational | 190/258 = **74%** |

**Per-task-type, among tool-emitting turns only** (normalizes away turns that never used tools):

| task_type | cap | emitters | budget-warned | hit hard limit |
|-----------|-----|----------|---------------|----------------|
| conversational | **6** | 587 | **32.4%** | **3.4%** |
| tool_use | 25 | 220 | 26.4% | 0.5% |
| analysis | 25 | 38 | 13.2% | 0.0% |
| memory_recall | 8 | 20 | 15.0% | 0.0% |
| planning | 25 | 13 | 7.7% | 0.0% |

The cap is the causal variable: the same *kind* of turn (tool-emitting) starves ~7× more often at cap 6
than at cap 25. The budget warning fires at `max-2`, i.e. the **4th** call for cap-6 vs the **23rd** for
cap-25 — so a conversational turn doing a perfectly ordinary 4-tool investigation is told to stop and
synthesize, while an identical tool_use turn sails through unwarned. Work-shape confirms the turns are
alike: conversational and tool_use tool-emitters have near-identical tool-call-count histograms (both
~35% single-call with a long multi-call tail).

**Finding:** the premise holds and is quantified. The cap-6 default meaningfully starves the 53% of
conversational turns that turn out to need tools, and does so ~7× more than the generative classes.

**The clean lever (TL;DR #4):** `tool_iteration_count` moves *only* when the model emits tool calls
(`executor.py:3715` is inside `step_tool_execution`, reached only via `executor.py:3567-3568`). So the
predicate the ticket asks us to gate on **already exists as a side effect of the loop**. A floor
applied when `tool_iteration_count >= 1` is, by construction, invisible to any turn that never calls a
tool.

---

## 4. Area 2 — Validation-retry budget accounting

**Hypothesis (ticket):** a `search_memory` call missing its required `query_text` returned a retry hint
but still consumed a full iteration. Should validation/malformed-arg retries decrement the hard cap the
same as real work?

**Measured mechanism (code):** two arg-validation layers can reject a call before it executes —
required-param presence (`tool_dispatch.py:82-113`, emits `tool_call_missing_required_params`, returns
`status:"retry"`, `latency_ms:0`, `tool_layer_error:"missing_required_params"`) and full JSON-Schema
(`executor.py:397-416`). `search_memory`'s only required param is `query_text`
(`memory_search.py:29-38`). **But** `ctx.tool_iteration_count += 1` runs *unconditionally at the top of*
`step_tool_execution` (`executor.py:3715`) — before the gate, before dispatch, before either validation
layer. There is **no decrement anywhere** and **no bounded free-retry allowance**; the only counter
mutations are that `+= 1` and the user-driven `+10` continue bonus. A never-executed, malformed call is
budget-indistinguishable from real work.

**Measured volume:** 18 conversational turns (24 total) hit `tool_call_missing_required_params` in the
window — low absolute volume, but each one burns a full iteration of a cap of **6**, i.e. ~17% of the
turn's entire tool runway spent on an arg typo the model immediately corrects.

**Finding:** confirmed — validation retries consume the hard cap. This is the budget-accounting
complement of **FRE-497** (Approved: "feed validation failures back to the model for N bounded retries
before hard-fail"). FRE-497 grants the *retry*; the missing half is that the retry should not spend the
scarce iteration budget. The `tool_loop_gate` (`executor.py:3875-3885`, repetition FSM in
`loop_gate.py`) already blocks *identical* malformed repeats, so a bounded free-retry cannot loop
forever.

---

## 5. Area 3 — Budget-pressure → bad output

**Hypothesis (ticket / post-mortem):** budget warnings ("before the budget runs out") pushed the model
to dump a maximal 10.5K-char plan in one shot; budget pressure correlates with oversized/low-quality
tool args.

**Note on the artifact:** the exact phrasing "before the budget runs out" does **not** exist in the
code; the real warning copy is the `⚠️ Tool budget…` message in §2. The post-mortem paraphrased.

**Measured:** on conversational emitters, tool-failure rate is **15.8% for warned** turns vs **12.1%
for unwarned** — a ~1.3× lift, and confounded: a turn only gets warned by making ≥4 calls, and more
calls independently means more failure opportunities. There is no population-scale signal that the
warning *causes* worse output.

**Finding:** **not supported** at population scale. The single-incident inference is plausible for that
trace but does not generalize. The genuine defect here is not "pressure causes bad output" — it is that
the flat `max-2` warning threshold fires *proportionally much earlier* for low-cap turns (67% through a
cap-6 budget vs 92% through cap-25), applying scarcity signaling to legitimate mid-investigation turns.
That is subsumed by the Area-1 fix (raising the effective conversational ceiling moves the warning
later) and does not warrant its own workstream.

---

## 6. Area 4 — Classifier confidence floor

**Hypothesis (ticket):** `confidence=0.7, signals=['no_special_patterns']` produced a consequential
routing decision. Should low-confidence "no signal" classifications default to a higher-capability
bucket, or trigger an LLM tiebreak?

**Measured:** `confidence=0.7` is a constant sentinel, not an estimate (§2, §TL;DR #1). "Raise the
bucket for low-confidence" would mean "raise the bucket for the 71% default" — i.e. exactly the global
conversational-cap raise the scope guard forbids. An LLM tiebreak would add a model call to most turns,
taxing latency and cost on the Socratic layer the North Star wants cheap, to recover a signal that is
strictly weaker than the one we already get for free.

**Finding / recommendation:** **reject both.** The behavioral runway floor (Area 1) dominates a
confidence-floor approach on every axis: it costs nothing per turn, it needs no classifier change, and
it keys on *observed tool emission* — a ground-truth signal, not a pre-execution guess. The right answer
to "confidence 0.7 caused a bad routing decision" is to make the routing decision *non-fatal* when the
turn later reveals it needs tools, not to make the guess louder.

---

## 7. Recommended design

Three parts; only R1 is new work. All honor the scope guard.

### R1 — Behavioral tool-runway floor (the systemic fix) — NEW ticket

Once a turn has emitted at least one native tool call, lift its effective iteration ceiling to a floor
`F`, independent of the initial task_type cap:

> `effective_max = max(task_type_cap, F)` **iff** `tool_iteration_count >= 1`; otherwise
> `effective_max = task_type_cap` unchanged.

- Applied in `_resolve_max_iterations` (`executor.py:105-122`) and read by the warning/limit guards
  (`executor.py:3091,3719`). Because `tool_iteration_count >= 1` is only ever true *after* a tool round,
  a turn that never calls a tool keeps cap 6 and trips nothing — the North Star holds by construction.
- **Starting `F ≈ 10`**, to be A/B-calibrated (measure-don't-assert): grounded by the tool_use baseline
  (cap 25 → 0.5% starvation) and the conversational tail; `F=10` is well below the generative cap while
  covering the bulk of the tool-emitting tail. Ship it flag-gated, observable-first, tighten via A/B —
  do not hard-code a "final" value.
- **Fold in the Area-3 sub-finding:** optionally make the budget-warning threshold proportional
  (`max * 0.8`) rather than a flat `max-2`, so low-cap turns are not warned proportionally earlier. This
  is a one-line refinement inside R1, not a separate ticket.
- **Effect:** the classifier may keep mis-labeling; the runway self-corrects the moment a turn proves it
  needs tools. This ends the FRE-256/210/469 point-fix treadmill without touching `intent.py`.

### R2 — Validation-only rejections get a bounded free-retry against the cap — FOLD INTO FRE-497

A tool round whose results are *all* validation rejections (never executed: `missing_required_params`
or schema-validation failure, `latency_ms:0`) should not decrement the hard iteration budget, up to a
small bound (e.g. 2 per turn); beyond the bound it counts (belt-and-suspenders — `loop_gate` already
blocks identical malformed repeats). This is the budget-accounting half of FRE-497's retry-allowance.
**Recommend the owner extend FRE-497's scope to include it rather than filing a duplicate** — the two
are one coherent "self-correction shouldn't be self-punishing" change.

### R3 — Do NOT add an LLM tiebreak or raise the conversational bucket — DECISION, no ticket

Per §6. Recorded here so it is not re-litigated: the confidence-floor / LLM-tiebreak path is explicitly
rejected in favor of R1.

---

## 8. Limitations (honest scope)

- **Window is ~20 days** (ES retention), single deployment, mixed owner + synthetic traffic. Rates are
  stable across the window but not seasonally validated.
- **`F` is not a measured optimum.** No turn-level final-iteration-count event is emitted, so the floor
  value is grounded by analogy (tool_use baseline + tail shape), not by a direct "what cap removes N% of
  starvation" curve. R1 must ship flag-gated and be A/B-calibrated — that calibration is the proof, not
  this note.
- **Starvation is rare in absolute terms** (20 hard-limit hits, 190 warnings over 20 days). The case for
  R1 is not volume — it is that (a) the failures are *concentrated* (87%/74% on one class), (b) they are
  *severe* when they occur (a lost or truncated turn, per the post-mortem), and (c) the fix is nearly
  free and removes an entire recurring bug family. A cheap fix for a concentrated, high-severity,
  recurring failure is worth shipping even at low base rate.
- **`tools_suppressed_by_task_type` is retired** — all 53 events are 2026-04-23/24 and no emit site
  exists in current code; an earlier, harsher form of the same "conversational strips capability" family
  (it zeroed `allowed_categories`), already removed. Noted for family history, not a live issue.

---

## 9. Follow-up disposition

- **File (Needs Approval):** R1 — behavioral tool-runway floor. Project *Turn Reliability Hardening
  (2026-06-04 incident)*. Tier-2:Sonnet (well-specified config + `_resolve_max_iterations` change,
  flag-gated, with an A/B calibration plan). Backing: this note + the North Star scope guard.
- **Recommend, do not duplicate:** R2 — extend the Approved **FRE-497** with validation-retry budget
  accounting (the budget-accounting complement of its retry-allowance). Flagged to the owner as a
  one-line scope note.
- **No ticket:** R3 (a recorded "don't"); Area 3 (negative finding, sub-part folded into R1).
- Per the workspace preference (advance priorities before creating work; tickets propose solutions, not
  open investigations), the net new footprint is **one** decision-ready ticket.

---

## References

- Post-mortem: `docs/postmortems/2026-06-04-artifact-turn-failure-cache-control.md` (the anchor
  incident; trace `c216bd40-9d92-4864-bd04-10b6858304da`).
- Classifier: `src/personal_agent/request_gateway/intent.py` (ladder `224-349`, default `341-348`,
  `_TOOL_INTENT_PATTERNS` `93-126`, `_ARTIFACT_BUILD_REGEX` `82-89,125`); `TaskType`
  `request_gateway/types.py:15-28`; decomposition `request_gateway/decomposition.py:100-101`.
- Caps: `config/settings.py:166-175` (global 25), `:197-212` (per-task_type dict); resolver
  `orchestrator/executor.py:105-122`.
- Loop/budget: `orchestrator/executor.py:3715` (counter), `:3091,3096-3100` (warning + text),
  `:3719,3070-3088` (limit + forced synthesis), `:3567-3568` (tool-emission → TOOL_EXECUTION),
  `:1365-1394` (FRE-484 forced-synthesis tool override).
- Validation: `orchestrator/tool_dispatch.py:82-113` (required-param), `tools/executor.py:397-416`
  (schema), `tools/memory_search.py:29-38` (`search_memory` required `query_text`); loop gate
  `orchestrator/executor.py:3875-3885`, `orchestrator/loop_gate.py`.
- Telemetry: ES `agent-logs-*`, `event_type` = `intent_classified`, `tool_call_started`,
  `tool_iteration_limit_reached`, `tool_budget_warning_injected`, `tool_call_missing_required_params`,
  `tool_call_failed`, `tools_suppressed_by_task_type`; joined on `trace_id`, window 2026-06-16 → 07-05.
- Tickets: FRE-469 (point-fix, Done), FRE-497 (self-correcting gates, Approved — R2 home), FRE-391
  (dynamic max_tokens, Approved — the output-token analog), FRE-432/447 (pedagogical North Star),
  FRE-256/210 (the recurring family).
</content>
</invoke>
