# FRE-582 — Feedback-Loops findings: proposal analysis & decision log

**Date:** 2026-06-21
**Author:** adr session (Opus), for the owner
**Ticket:** FRE-582 — *ADR-0093 review deep-dive + Feedback-Loops findings proposal analysis*
**Project:** Observability Foundation (L0)
**Inputs reviewed:**
`docs/architecture_decisions/ADR-0093-opentelemetry-boundary-migration.md` (Proposed) ·
`docs/research/2026-06-20-feedback-loops-course-findings.md` ·
`src/personal_agent/observability/joinability/walk.py` ·
ADR-0040 / 0053 / 0067 / 0074 / 0088 / 0090 / 0091 ·
Linear FRE-540 / 541 / 555.

---

## What this is

PR #236 merged ADR-0093 (Proposed) plus the Feedback-Loops course findings note as
**recommendations awaiting an owner decision** — not accepted decisions. This is the deep-dive that
rules **accept / re-scope / park** on each of the 5 findings, records rationale + effort +
reversibility, files sequenced follow-up tickets for the accepted items, and reconciles the status
of the still-Proposed observability ADRs. The owner's rulings on the three genuinely-open forks
(ADR-0053 disposition, ADR-0093 D3, the joinability severity) were taken on 2026-06-21 and are
recorded inline.

This doc changes no code. The only edits in its PR are this note, the ADR **status lines**
(0093 / 0090 / 0091 / 0053) and a one-sentence sharpening in ADR-0040 — all docs.

---

## Grounding facts established during the review

These reframe several findings, so they are recorded first:

- **PA is single-process today.** ADR-0093's cross-process argument is forward-looking. The D1/D2
  **naming** payoff is real now (it closes the ADR-0090 emit↔mapping↔display drift); the D3
  **exporter** has no backend to point at yet.
- **Finding #4 is confirmed in code, not a question.** `walk.py` queries every substrate
  key-filtered (`WHERE session_id = $1` / `trace_id = ANY($set)`), so the anchor key matches **by
  construction**. The walk then asserts only that the *other* identity field is **non-null** (e.g.
  `if r["trace_id"] is None: status = "red"`). `trace_ids` is a **monotonic union accumulator**,
  never diffed. The two cross-set comparisons that exist (`unknown_in_es`, Neo4j
  `otrace not in trace_ids`) emit **yellow** informational orphans only — there is no red
  tuple-value-mismatch and no symmetric-difference assertion. **"Present but wrong" passes green.**
- **ADR-0090 is de-facto accepted.** FRE-533 ✅ (1023-row inventory), FRE-540 ✅ (shipped
  `scripts/audit/telemetry_surface_check.py`, hermetic CI, report-only), FRE-555 Approved (flip to
  gate), 534/535 buildable. Leaving it "Proposed" is status drift.
- **ADR-0091 is being built** — FRE-541 In Progress (PR #216 is the ADR itself).
- **FRE-540/555 explicitly leave the "field registry" out of scope** as an *ADR-0090 open decision*.
  That is precisely the slot Finding #5's D1 attribute map fills.
- **ADR-0053 (gate feedback-loop monitoring) is Proposed and unworked** — the one genuinely dormant
  meta-monitoring gap: the gateway's deterministic decisions are not monitored as a class.
- **Elastic self-hosted can be the standard trace backend.** The **EDOT Collector** (a curated OTel
  Collector) writes OTLP directly to our existing Elasticsearch and replaces standalone APM Server
  for most cases; Kibana's APM/Traces UI renders the spans. We already run ES + Kibana 8.19, so the
  only missing piece for D3 is the collector hop — not a new backend.
  ([Elastic OTel docs](https://www.elastic.co/docs/solutions/observability/apm/opentelemetry),
  [EDOT](https://www.elastic.co/docs/reference/opentelemetry/motlp))

---

## Preserved strengths (acknowledged, not changed)

Per the findings note and confirmed in review: joinability as a continuously-checked property with
loud degradation · the frozen `TraceContext` + user/system trace split · the
`CANONICAL_MODEL_CALL_*_FIELDS` parity test (one event, one shape, gated in CI) · eval-as-hypothesis
(FRE-453, instrument-health the only hard gate) · the closed ADR-0067 reflection-surfacing loop.
None of the decisions below weaken these.

---

## Decisions — one per finding

### Finding 1 — Adopt OpenTelemetry at the substrate boundary (ADR-0093) → **ACCEPT, with scope change**

**Decision.** Accept the boundary migration as **D1 + D2 now**, **D3 parked** (behind a concrete
enabling step, below), **D4 confirmed-deferred**, **D5 adopted as an authoring rule**.

- **D1 — OTel data-model naming at the emission boundary.** Map `trace_id` → OTel TraceId (32-hex),
  span id → SpanId (16-hex), `parent_span_id` → `parent_span_id`, at the point records leave the
  process. The in-process `TraceContext` stays the carrier. **Accept.**
- **D2 — `gen_ai.*` semantic-convention attributes on model-call events.** Align
  `CANONICAL_MODEL_CALL_*_FIELDS` to `gen_ai.operation.name` / `gen_ai.system` /
  `gen_ai.request.model` / `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` where they
  map; project keys (join tuple, ADR-0078 prompt hashes) ride along as custom attributes. **Accept.**
  This is also the mechanism for Finding #5.
- **D3 — OTLP exporter at the seam.** **Park** — see the owner ruling below.
- **D4 — no full OTel SDK in-process yet.** **Confirm.** The thin-deps value holds for a
  single-owner single-process harness; D4 revisits only on multi-service.
- **D5 — pin a named semconv version; unstable fields stay custom.** **Adopt.** The `gen_ai.*`
  conventions still churn; a version pin keeps mapping maintenance bounded.

**Owner ruling (D3, 2026-06-21):** *"Park — but we need to address adding a standard trace backend.
Can Elastic provide this, as we already have it?"* → **Yes.** D3 is parked **with a concrete
un-park trigger**: stand up an **EDOT Collector** (or APM Server OTLP intake) against our existing
self-hosted Elasticsearch and use the Kibana APM/Traces UI as the standard trace backend. This is a
collector hop, not a new backend. Filed as **FRE-588** (evaluate + stand up); D3 lands once FRE-588
proves the intake path and the spans from D1/D2 are flowing.

**Rationale.** We already *built* the OTel model; D1/D2 are a naming alignment at the seam with
immediate payoff (closes ADR-0090 drift) and full reversibility (additive attributes; old keys can
ride alongside during migration). D3 alone had no consumer until the Elastic-backend question
reframed it — now it has a defined, low-incremental-cost target.

**Effort / reversibility.** D1/D2: moderate, reversible (additive). D3: parked. D4/D5: zero (policy).

**Status set:** ADR-0093 → **Accepted (with scope change)** — D1/D2 accepted & sequenced; D3 parked
behind FRE-588; D4 confirmed; D5 adopted.

**Follow-ups:** **FRE-583** (D1/D2 attribute map at model-call emit sites) · **FRE-588** (Elastic
trace-backend enabling step; un-parks D3).

---

### Finding 2 — Make the shipped-vs-aspirational line explicit (ADR-0040) + proposal acceptance-rate signal → **ACCEPT**

**Decision.** Accept both halves, sized small.

- **The explicit line** is *almost* already in ADR-0040 ("Phases 1–2 implemented; Phase 3
  meta-learning pending"). Sharpen it to one unambiguous sentence — the human-closed loop (reflect →
  dedup → promote → Linear → verdict → suppress) **ships**; the autonomous self-implementation step
  (Phase 3) **does not**, and its prerequisites are unmet. This is a one-line doc edit folded into
  this PR.
- **The acceptance-rate signal** — instrument accept / reject / re-evaluate over the Captain's Log
  proposal corpus, bucketed by category/scope — is a measurement ticket that fits the
  measure-before-autonomy methodology ("earn autonomy by being observable"). It is evidence that
  must *precede* any Phase-3 move, not a commitment to Phase 3. Filed as **FRE-586**.

**Rationale / effort.** Low effort, high signal; aligns with the project's measurement-first ethos
and the pedagogical North Star. Reversible (a new read-only metric over an existing corpus).

**Status set:** ADR-0040 stays **Accepted**; line sharpened in this PR.

**Follow-ups:** **FRE-586** (acceptance-rate signal).

---

### Finding 3 — Status-hygiene the Proposed observability ADRs (0053 / 0090 / 0091) → **RECONCILE**

| ADR | Was | Decision | Why |
|-----|-----|----------|-----|
| **0090** Telemetry Surface Contract | Proposed | **Promote → Accepted** | Shipping against it (FRE-533/540 ✅, 555 Approved). "Proposed" is drift. |
| **0091** Eval conversation driver | Proposed | **Promote → Accepted** | FRE-541 In Progress (PR #216). Being built. |
| **0053** Gate feedback-loop monitoring | Proposed | **Park — but scheduled** | Genuinely unworked; the load-bearing aspirational gap. |

**Owner ruling (ADR-0053, 2026-06-21):** *"Park but schedule. It must be in the planning."* →
ADR-0053 status set to **Parked (scheduled)** with an explicit revisit trigger, **and** a planning/
design ticket filed so gate-class monitoring is sequenced into the roadmap rather than forgotten:
**FRE-589**. Trigger: after the OTel-boundary (T1) and joinability-coherence (T3) spine lands, since
both feed the signal a gate-monitor would consume.

**Status set:** ADR-0090 → **Accepted**; ADR-0091 → **Accepted**; ADR-0053 → **Parked (scheduled,
see FRE-589)**.

**Follow-ups:** **FRE-589** (ADR-0053 gate-class monitoring; planning + design).

---

### Finding 4 — Joinability walker value-coherence (confirmed gap) → **ACCEPT as its own ticket**

**Decision.** The gap is confirmed (above): the walker asserts presence + per-key reachability +
identity-field **non-nullity**, never cross-substrate **value-equality** of the
`(trace_id, session_id, task_id)` tuple, and the shared `trace_ids` set is **never diffed**. Add:

1. **Tuple value-coherence** — where a substrate row carries more than one identity field (api_costs
   `trace_id`+`session_id`; Neo4j Turn `originating_trace_id`+`originating_session_id`), assert the
   values are mutually consistent with the anchor, not merely non-null.
2. **Symmetric-difference orphan kind** — a `required` substrate present on one side and absent on
   the other (e.g. a PG trace with zero ES `agent-logs` docs) becomes an explicit orphan instead of
   passing silently. Keep benign system-span one-directional unknowns (consolidation/brainstem turns
   with no api_costs row) as the existing yellow informational orphans.

**Owner ruling (severity, 2026-06-21):** *"Yellow-first, observe, then red."* → Ship both checks at
**yellow** (informational) first to gather real-world orphan rates, then a follow-up tightens the
confirmed-bad classes to **red**. Matches the "ship observable-first, don't clamp round 1" stance.

**Rationale / effort / reversibility.** Moderate, self-contained walker extension; high
signal-integrity payoff ("present but wrong" is as dangerous as missing). Fully reversible
(additive checks, yellow-only round 1). Filed as **FRE-585**.

**Follow-ups:** **FRE-585** (value-coherence pass, yellow-first; a later ticket promotes to red).

---

### Finding 5 — The D1 attribute map as the single source feeding the reconciliation checker → **ACCEPT, sequenced after D1/D2**

**Decision.** FRE-540 shipped the reconciliation checker with the **emit→mapping** check as a
grep heuristic, **report-only "until a field registry exists (ADR-0090 open decision)."** Finding #5
fills that slot: the **D1/D2 OTel attribute map becomes the canonical field registry** the checker
reads from, replacing the heuristic with a registry-backed emit↔mapping↔display reconciliation. This
is the convergence of #1 and #5 — closing the ADR-0090 drift is a *side effect* of D2, exactly as the
ADR predicts.

**Rationale / effort.** Depends on FRE-583 (the map must exist first). Moderate; turns a report-only
heuristic into an authoritative check feeding FRE-555's gate. Reversible. Filed as **FRE-587**.

**Follow-ups:** **FRE-587** (field registry from the OTel map → reconciliation checker; depends
on FRE-583; relates FRE-540 / 555).

---

## Filed follow-up tickets & sequence

All filed **Needs Approval** under their project; owner approves → build session picks up. Model tier
noted per ticket.

| # | Ticket | Finding | Project | Tier | Depends on |
|---|--------|---------|---------|------|------------|
| T1 | FRE-583 — OTel D1/D2 attribute map at model-call emit sites | 1 | Observability Foundation | Sonnet | — |
| T2 | FRE-587 — Field registry from the OTel map → reconciliation checker | 5 | Telemetry Surface Audit | Sonnet | T1 |
| T3 | FRE-585 — Joinability value-coherence pass (yellow-first) | 4 | Observability Foundation | Sonnet | — |
| T4 | FRE-586 — Captain's Log proposal acceptance-rate signal | 2 | Observability Foundation | Sonnet | — |
| T5 | FRE-589 — ADR-0053 gate-class monitoring (planning + design) | 3 | Observability Foundation | Opus | T1, T3 |
| T6 | FRE-588 — Elastic (EDOT) trace backend; un-parks ADR-0093 D3 | 1 (D3) | Observability Foundation | Sonnet (Opus review) | T1 |

**Recommended order.** T1 (foundation) → T2 + T6 (consume the map) in parallel with T3 + T4
(independent) → T5 (after the T1/T3 spine feeds the gate-monitor signal). The exporter (D3 via T6)
and the reconciliation upgrade (T2) both wait on T1's attribute map; the joinability and
acceptance-rate work is independent and can run alongside.

---

## ADR status changes made in this PR

- **ADR-0093** → Accepted (with scope change): D1/D2 accepted & sequenced (FRE-583); D3 parked behind
  FRE-588; D4 confirmed; D5 adopted.
- **ADR-0090** → Accepted (was Proposed) — shipping against it.
- **ADR-0091** → Accepted (was Proposed) — FRE-541 In Progress.
- **ADR-0053** → Parked (scheduled) — revisit trigger + FRE-589.
- **ADR-0040** → unchanged status; shipped-vs-aspirational line sharpened to one sentence.
