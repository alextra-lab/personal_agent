# ADR-0093 — OpenTelemetry at the Substrate Boundary (adopt the data model + GenAI semantic conventions + an exporter at the seam; keep the in-process layer)

**Status:** Accepted (with scope change) — 2026-06-21 (FRE-582). D1/D2 accepted & sequenced (FRE-583); D3 (OTLP exporter) parked behind FRE-588 (stand up an EDOT/OTLP intake on existing Elastic as the trace backend); D4 confirmed-deferred; D5 adopted. Originally Proposed 2026-06-20. Decision log: `docs/research/2026-06-21-fre-582-feedback-loops-proposal-analysis.md`.
**Related:** ADR-0004 (telemetry & metrics strategy), ADR-0020 (request traceability — `RequestTimer`, spans/phases), ADR-0074 (end-to-end traceability & joinability — the identity tuple across PG/ES/Neo4j/Redis), ADR-0088 (execution-topology observability), ADR-0090 (telemetry surface contract — emit ↔ mapping ↔ display reconciliation), ADR-0068 (self-telemetry data plane). Origin: external "Feedback Loops" course (Unit 11), which uses PA as its worked example; see `docs/research/2026-06-20-feedback-loops-course-findings.md`.
**Project:** Observability Foundation (L0/L1)

---

## Context

### The in-process layer is already OpenTelemetry-shaped

`telemetry/trace.py` is, by its own docstring, *"OpenTelemetry-compatible without the full OTel
SDK"*: a `TraceContext` carrying `trace_id` / `parent_span_id`, `new_trace()` / `new_span()`, and a
user-vs-`system:<source>` split. `RequestTimer` (ADR-0020) records spans with phases. `events.py`
enforces a canonical model-call field set (`CANONICAL_MODEL_CALL_*_FIELDS`). This was a deliberate
**thin-dependencies** choice and remains correct for a single-process harness: we get the trace/span
model and full control of the shape without taking the SDK as a runtime dependency.

### Why revisit now

Signal already crosses substrates. ADR-0074's joinability walker walks one session across
**Postgres ↔ Elasticsearch ↔ Neo4j ↔ Redis** asserting the identity tuple is *present and threaded*
(per-key reachability + identity-field non-nullity; cross-substrate *value*-coherence is a known gap
being closed under FRE-585 — see the FRE-582 decision log). As soon as a run
spans more than one process/service, a bespoke trace format costs:

1. **Bespoke parsing** at every consumer (dashboards, the self-telemetry tool path, eval read-back).
2. **No standard backend.** We cannot point a standard trace UI (Tempo / Langfuse / Phoenix / Arize)
   at our spans without a translation layer.
3. **Drift** between emit-site names and storage/display (the exact gap ADR-0090 and ADR-0068
   already document: `prompt_tokens` vs `input_tokens`, dead explicit ES mappings).

OpenTelemetry's **GenAI semantic conventions** (`gen_ai.*`) standardise model/operation/token
attributes, and OTLP gives a standard wire format. The question is not whether OTel is the right
*model* — we already built it — but **how much of the machinery to adopt, and where**.

## Decision

A **boundary migration**, explicitly *not* a wholesale SDK adoption:

- **D1 — Adopt the OTel data-model naming at the emission boundary.** Map `trace_id` → OTel TraceId
  (32-hex), the span identifier → SpanId (16-hex), and `parent_span_id` → `parent_span_id`. Keep the
  in-process `TraceContext` as the carrier; the mapping happens where records leave the process.

- **D2 — Stamp GenAI semantic-convention attributes on model-call events.** Align the existing
  `CANONICAL_MODEL_CALL_*_FIELDS` to the conventions where they map:
  `gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`. This doubles as the
  emit↔mapping↔display reconciliation ADR-0090 asks for. Project-specific keys (e.g. the join tuple,
  `prompt_*` identity hashes from ADR-0078) ride along as custom attributes.

- **D3 — Add an OTLP exporter at the substrate seam.** A thin exporter/collector hop so spans can
  reach a standard backend without changing in-process call sites. Elasticsearch logging stays as-is;
  this is additive. **PARKED (FRE-582, 2026-06-21)** behind a concrete enabling step: FRE-588 stands
  up an EDOT Collector (or APM Server OTLP intake) against the existing self-hosted Elasticsearch as
  the standard trace backend, with the Kibana APM/Traces UI rendering spans. D3 lands once FRE-588
  proves the intake path and the D1/D2 spans are flowing — it is not in-scope now.

- **D4 — Do NOT take the full OTel SDK as an in-process dependency yet.** The thin-deps value holds
  for a single-owner, single-process harness. Revisit D4 only if PA grows to multiple services that
  must share one trace context at runtime.

- **D5 — Treat unstable conventions as custom.** The GenAI conventions are still evolving (now in
  their own semconv repo). Pin to a named semconv version; any field not yet stable (e.g. a cost
  attribute) is emitted under a clearly-namespaced custom key until it stabilises.

## Consequences

**Positive**
- Cross-substrate joinability survives growth from one process to many — a standard contract instead
  of a bespoke one.
- Standard backends become usable with no per-tool parser; the self-telemetry path (ADR-0068) can
  lean on standard tooling.
- Forces the ADR-0090 three-surface reconciliation as a side effect of D2.

**Negative / costs**
- GenAI conventions churn → periodic mapping maintenance (mitigated by D5's version pin).
- An exporter/collector to operate (D3) — but additive and reversible.
- Two naming worlds during migration (internal vs OTel) until D2 lands everywhere.

## Alternatives considered

- **Full OTel SDK adoption now.** Rejected (for now): dependency weight against the thin-deps value,
  and convention churn would land directly in the runtime. D4 leaves this open for a multi-service
  future.
- **Status quo (stay fully bespoke).** Rejected: bespoke parsing at every consumer and no path to a
  standard backend; the cost grows with every new substrate.
- **ES-only standardisation (no OTLP).** Partial: helps storage/display drift (D2) but does not give
  a standard trace contract at the boundary; D3 is what unlocks standard backends.

## Notes

This ADR arose from an external review (the Feedback Loops course, Unit 11), not an internal
incident. It was filed **Proposed** for the owner's consideration and was **Accepted (with scope
change) on 2026-06-21 under FRE-582**: D1/D2 accepted & sequenced (FRE-583), D3 parked behind FRE-588,
D4 confirmed-deferred, D5 adopted. The companion findings note
(`docs/research/2026-06-20-feedback-loops-course-findings.md`) lists the lower-priority items the
same review surfaced; the full per-finding decision log is
`docs/research/2026-06-21-fre-582-feedback-loops-proposal-analysis.md`. The decided sequence lands
the D1/D2 attribute mapping first (immediate payoff, reversible) and defers the D3 exporter.
