# Feedback Loops course — findings & recommendations for personal_agent

**Date:** 2026-06-20
**Author:** external review (the "Feedback Loops: Earning Autonomy by Being Observable" course uses
`personal_agent` as its worked example)
**Status:** advisory — for the owner's consideration; the one concrete decision is drafted as
ADR-0093 (Proposed)

---

## What this is

A 13-unit course on observability and feedback loops was built using `personal_agent` as its
real-world spine: every unit names an industry concept and grounds it in an actual PA artifact
(`telemetry/trace.py`, `events.py`, `request_timer.py`, `orchestrator/loop_gate.py`, `cost_gate/`,
`captains_log/*`, `observability/joinability/`, `brainstem/scheduler.py`, the FRE-453 eval set).
Reading the harness closely enough to teach it was, in effect, an **observability pass over PA**.
This note records what that pass surfaced — strengths worth preserving and gaps worth deciding on.
It is advisory; nothing here changes code.

## Strengths worth preserving (so the gaps read in context)

- **Joinability as a first-class, continuously-checked property.** The `observability/joinability/`
  walker (ADR-0074 Phase 5) asserts the identity tuple across PG/ES/Neo4j/Redis and **degrades
  loudly** (one substrate down → distinguishable from all-clear). Most systems never check this.
- **The frozen `TraceContext` + user/system trace separation** (`telemetry/trace.py`) — an
  OTel-shaped correlation primitive without the SDK weight.
- **An enforced event vocabulary** — `CANONICAL_MODEL_CALL_*_FIELDS` frozensets gate emit-site shape
  in CI, so "one event, one shape" is checked, not hoped for.
- **Eval-as-hypothesis** (FRE-453) — comparisons are findings, the only hard gate is instrument
  health, and `eval_mode` isolates eval traffic from the learning loop. This is the right stance and
  rarer than it should be.
- **A closed reflective loop** — ADR-0067 surfacing deduplicated reflections back into context is a
  clean output→future-behavior loop with a sensible recurrence gate.

## Findings & recommendations

### 1. (Headline) Adopt OpenTelemetry at the substrate boundary — drafted as ADR-0093

**Observation.** `trace.py` is deliberately "OpenTelemetry-compatible without the full OTel SDK."
That is the right call for a single-process, thin-deps system — but the joinability walker already
reaches across four substrates, and the moment signal crosses process/service boundaries a bespoke
format costs bespoke parsing and forecloses standard backends.

**Recommendation.** A **boundary migration**, not a wholesale SDK swap: adopt OTel's data-model
naming + the GenAI semantic conventions (`gen_ai.*`) at the emission seam and add an OTLP exporter,
while keeping the lightweight in-process layer. Written up as **ADR-0093 (Proposed)**.

### 2. Make the shipped-vs-aspirational line explicit in the self-improvement loop (ADR-0040)

**Observation.** The human-closed loop (reflect → dedup → promote → Linear → verdict → suppress)
ships and works. The *autonomous* self-implementation step (ADR-0040 Phase 3) does not, and the
prerequisites it lists (proposal quality unevaluated, no external-agent delegation) are unmet.

**Recommendation.** Before pursuing Phase 3, instrument a **proposal acceptance-rate signal** over
the Captain's Log corpus (accept/reject/re-evaluate by category/scope). A rising acceptance rate is
the evidence that should precede any move toward autonomy — "earn autonomy by being observable."
Until then, keep Phase 3 clearly marked pending (it currently is).

### 3. Resolve the status of the Proposed observability ADRs

**Observation.** Several load-bearing observability ADRs are still **Proposed**: ADR-0053
(deterministic gate feedback-loop monitoring), ADR-0090 (telemetry surface contract), ADR-0091
(eval conversation driver / completion-status layer). ADR-0053 in particular means the gateway's
deterministic decisions are **not yet monitored as a class** — a real meta-monitoring gap.

**Recommendation.** Status hygiene: accept, re-scope, or explicitly park each. Prioritise ADR-0053
or formally defer it — a course unit ("watching the apparatus") leans on the idea that the gates are
observable as a class, and it is currently aspirational.

### 4. Confirm the joinability walker asserts value-match, not just presence

**Observation.** The walker reads as asserting that identity **exists and matches** across
substrates (not merely that a `trace_id` field is non-empty). The course modelled this explicitly,
because "present but wrong" is as dangerous as "missing."

**Recommendation.** Confirm `walk.py` compares the *value* of the tuple across substrates (not just
presence); if any path only checks presence, tighten it. Low effort, high signal-integrity payoff.

### 5. Telemetry surface drift (supports ADR-0090)

**Observation.** ADR-0068/0090 already document emit↔ES-mapping↔dashboard drift (e.g.
`prompt_tokens` vs `input_tokens`, dead explicit ES mappings). Aligning emit-site field names to the
OTel GenAI conventions (Finding 1) is an opportunity to close this drift in the same pass.

**Recommendation.** Treat the ADR-0093 attribute mapping as the moment to reconcile the three
surfaces (emit / storage / display) ADR-0090 calls for.

## Suggested sequence

1. Confirm Finding 4 (cheap, de-risks everything downstream).
2. Land the ADR-0093 attribute mapping (Finding 1 + 5) at the model-call emit sites.
3. Add the proposal acceptance-rate signal (Finding 2).
4. Decide ADR-0053/0090/0091 status (Finding 3).

The OTLP exporter (ADR-0093 D3) can come last — the data-model/attribute alignment is the part that
pays off immediately and is reversible.
