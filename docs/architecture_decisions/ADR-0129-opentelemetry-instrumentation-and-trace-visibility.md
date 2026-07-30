# ADR-0129: OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar

**Status:** Proposed
**Date:** 2026-07-30
**Deciders:** Project owner (FRE-1043, owner-directed 2026-07-30)
**Tags:** telemetry, observability, opentelemetry, tracing, instrumentation, grafana

---

## Context

### What is being decided

Whether to instrument `personal_agent` with the OpenTelemetry SDK — spans, context propagation, semantic conventions — and stand up somewhere to *see* the resulting traces. This ADR supersedes the enforcement mechanisms of ADR-0128 and finally realizes ADR-0093, whose decision has been Accepted and unbuilt since 2026-06-21.

### Why a fifth telemetry ADR must have a different shape

Four ADRs have touched this ground and changed nothing observable. ADR-0128 catalogued that failure precisely and then proposed a fifth mechanism of its own design. The distinguishing property of this ADR is not a better mechanism — it is a **target that cannot be satisfied by a document**:

> Open a real turn in Grafana and see its span waterfall — the orchestrator step, the model call, the tool calls nested beneath it, with real durations — then click through to that turn's logs.

"Adopt OpenTelemetry" is an unobservable state, which is exactly why ADR-0093 produced zero bytes in fourteen months. "I can see a trace" is observable, and a half-instrumented system produces a flat or empty waterfall rather than a passing test.

### Why ADR-0093 produced nothing: the standard had no destination

ADR-0093 chose a *tracing* standard while the only telemetry backend was a log index. A span had nowhere to go, so the decision could only ever land as "rename some fields" — which is precisely what its successor, ADR-0128, set out to do. The missing half was never the standard. It was a trace store.

### What was measured

All figures below were taken live against the production cluster on 2026-07-30, during the design session that produced this ADR. They are a snapshot and drift with every run; the ratios are what the decision rests on.

**Identity is absent far more often than present.** Over `agent-logs-*` (3,299,635 documents, 2026-04-15 → 2026-07-30):

| Field | Documents | Share |
|---|---|---|
| `@timestamp`, `event_type` | 3,299,635 | 100% |
| `trace_id` | 374,685 | **11.36%** |
| `session_id` | 70,940 | **2.15%** |

**This is not a discipline failure at the emit sites.** `TraceContext` (`telemetry/trace.py`) is threaded **by hand** — 19 functions take `trace_ctx: TraceContext` as an explicit parameter — so every emit site must remember to pass it and every author must remember to accept it. A convention cannot fix that, because the failure is *forgetting*, and a rule adds one more thing to remember. Context propagation removes the need to remember at all. That is the whole argument for the SDK over any naming rule.

**Elapsed time has two concurrent names, for the entire life of the corpus.**

| Field | Documents | Date range |
|---|---|---|
| `duration_ms` | 22,771 | 2026-04-15 → 2026-07-30 |
| `latency_ms` | 14,146 | 2026-04-15 → 2026-07-30 |

Not a rename split at a point in time — **both span the whole corpus**, and `model_call_completed` emits *both* (4,272 under one name, 3,361 under the other). Every latency question ever asked of this system has therefore been answered from either 62% or 38% of the data, silently and without error. That is a third undocumented instance of the divergence ADR-0068 recorded on 2026-05-10, and it landed on the metric the owner most wants to ask about.

**The span tree already exists and is already being lost.** Two emit sites describe one tool invocation at two granularities:

```
orchestrator/executor.py:5373   tool_execution_completed   duration_ms, tool_count   (the step: N tools)   n=4,140
tools/executor.py:462           tool_call_completed        latency_ms, span_id       (one tool)            n=4,084
tools/executor.py:481           tool_call_failed           latency_ms, span_id                             n=254
```

`tool_call_started` (4,338) reconciles exactly with `tool_call_completed` + `tool_call_failed`. The parent emits **no `span_id`**, so the edge between step and tool is unrecoverable: the system cannot answer "which tool calls made up this step." A hand-rolled span timer already exists at `telemetry/request_timer.py:88` (`start_span`, `end_span`). **We are building a tracing system, badly, and losing the one property that makes tracing worth having.**

**The event vocabulary is not trustworthy either.** `metrics.sampled` is the single largest event type at **1,720,095 documents — 52% of the entire telemetry corpus** — and contains no metrics. It is the Redis bus logging *"I published a message"*, with `event_type` populated from the stream name being published to. A query for `event_type: metrics.sampled` returns 1.7M publish receipts with total confidence.

**The storage cost of all this is a live operational risk.** Elasticsearch holds **602 active shards for 719 MB of data** — about 1.2 MB per shard — and sits at **1.839 GiB of a 2 GiB cap (92%)**. Shard overhead is the reason. ES is a functional dependency of Captain's Log (`captains_log/capture.py:556`), the cost gate and the joinability monitors, so this is memory pressure on something the application needs to work.

**Telemetry contains conversation content, by design and by accident.** `user_message` holds verbatim user turns (1,524 documents). More significantly, `docker/elasticsearch/index-template.json` carries a dynamic template that auto-indexes as full text anything matching `^(.*_message|.*_content|.*_description|reason|hint|stderr|stdout|raw_.*|.*_text|.*_prompt|content|content_value|.*_preview|.*_excerpt|summary)$`. No declaration is required, so `stdout`, `stderr` and `raw_*` from tool execution land searchable without review. `.claude/CLAUDE.md` states *"Never log secrets/PII"*; that policy currently has no enforcement point. **This is not caused by the present decision and is not fixed by it** — it is recorded because it establishes why a single egress chokepoint has value, and it is filed separately.

### The resource envelope

The VPS has **22 GiB total**, with production containers consuming roughly **4.8 GiB** (Elasticsearch 1.84, Neo4j 0.93, reranker 0.57, Kibana 0.55, gateway 0.50, all others under 0.4 combined). There is real headroom. Kibana's 551 MiB is recovered by this decision, and Elasticsearch's shard-driven pressure is relieved independently by FRE-1036.

---

## Decision

### D1 — Instrument with the OpenTelemetry SDK; context propagation replaces hand-threading

`personal_agent` adopts the OpenTelemetry Python SDK. Trace and span identity propagate through OTel's context mechanism rather than being passed as function arguments.

**`TraceContext` is bridged, not deleted.** It carries fields OTel has no opinion about — `user_id`, `session_id`, `kind`, `eval_mode`, `authenticated` — which are load-bearing for ADR-0064 per-user scoping and FRE-229 visibility filtering. The bridge is: OTel owns `trace_id` and `span_id`; `TraceContext` retains the rest and reads its identity from the active span rather than minting its own. Existing signatures keep working throughout, so this is not a flag-day change across 19 call sites.

### D2 — Semantic conventions are the vocabulary; no project field registry is built

Field and attribute names come from OpenTelemetry semantic conventions where one exists (`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`), and from a namespaced project key where none does.

This is ADR-0093 D1/D2 restated, and it **retires ADR-0128 D6's generated field registry**. A registry is the correct idea and the wrong build: semconv already is one, maintained by people who are not us. The `duration_ms` / `latency_ms` divergence is resolved by span duration being intrinsic to a span — there is no field to name, and therefore no second name to diverge into.

ADR-0093 D5 governs stability: pin a named semconv version and record it; attributes not yet stable ride under a namespaced project key until they stabilise. `gen_ai.*` is explicitly still evolving and is governed by that rule.

### D3 — Real spans replace the hand-rolled timer and the flattened parent/child logs

The step → model-call → tool-call structure becomes actual parent and child spans. `RequestTimer` (`telemetry/request_timer.py:88`) is retired rather than extended, and the `tool_execution_completed` / `tool_call_completed` pair collapses into a parent span with one child span per tool.

This is the decision that makes the target reachable. Naming conventions cannot produce a waterfall; only parented spans can.

### D4 — The structlog processor is the first deliverable, and it is correlation's proof

A structlog processor registered in `telemetry/logger.py:232` reads the active span context and injects `trace_id` and `span_id` onto **every** log record, with no change to any emit site.

It is sequenced first deliberately. It is small, reversible, independently valuable, and it converts the 11.36% presence figure into a measurable outcome **before any infrastructure is committed**. If it does not move that number, nothing further should be built.

### D5 — The OpenTelemetry Collector is the single egress point

All telemetry leaves the process via OTLP to a Collector. Nothing exports to a backend directly.

Two consequences are the reason, not side effects. First, it is the only place where redaction can be declarative and auditable rather than scattered across emit sites — which is the answer to the content finding above, whether or not anything is ever exported off-box. Second, it gives `slm_server` a network endpoint to ship to, replacing the client-side index URL formatting at `slm_server`'s `telemetry.py:38` that ADR-0128 D8 could not solve from inside this repository.

**The vanilla upstream Collector is used, not a vendor distribution.** Grafana Alloy, the Splunk distro and the Datadog distro are all competent; choosing the neutral one is what keeps the backend a configuration line rather than a commitment.

### D6 — Tempo stores traces; Elasticsearch keeps logs; Grafana correlates and replaces Kibana

- **Tempo** receives spans. Elasticsearch cannot render a trace waterfall and neither can Grafana over an Elasticsearch datasource; a trace store is a requirement of the target, not an optional nicety.
- **Elasticsearch keeps the logs**, and keeps serving Captain's Log, insights, ratings and the cost gate unchanged. There is no data migration.
- **Grafana** is the single UI. Its Tempo datasource links span → logs against the Elasticsearch logs datasource on `trace_id`, and back. **Kibana is retired**, recovering 551 MiB; its dashboards are rebuilt in Grafana.

Correlation across two stores is what makes it exact: each signal stops pretending to be the other, and `trace_id` is the join.

### D7 — Scope boundary: metrics are deferred, and this is stated rather than implied

Prometheus and the metrics signal are **not** in this decision. Metric-shaped emissions (`sensor_poll`'s CPU and memory series, budget counters) stay as logs for now. They are the correct next rung and they are not required by the target.

Nothing off-box. No SaaS exporter is configured. Splunk Observability, Grafana Cloud and Datadog were considered (below) and all are reachable later by adding an exporter, which is what D5's neutrality buys.

### D8 — What this supersedes in ADR-0128, and what survives

| ADR-0128 | Disposition |
|---|---|
| Context and diagnosis | **Survives** — carried forward above; it was correct |
| D1 (OTel is the vocabulary) | **Survives**, restated as D2 here |
| D2 (`@timestamp` canonical), D3 (governed surface + spine), D7 (rename table) | **Superseded** — semconv supplies names; span identity supplies the spine |
| D4 (sentinels, violation tagging) | **Superseded** — identity comes from propagated context, not from filling gaps after the fact |
| D5 (two enforcement tiers) | **Superseded** — the SDK is Tier 1, the Collector is Tier 2, and neither is ours to build |
| D6 (generated field registry) | **Superseded** — semconv is the registry |
| D8 (`slm_server` fixes) | **Survives**, modified — items 1 and 2 dissolve into semconv; item 3 becomes "ship OTLP to the Collector" |
| D9 (Neo4j `entity_class`, Redis stream enum) | **Survives untouched** — orthogonal to tracing, and still worth doing |

ADR-0128 moves to **Superseded** when this ADR is Accepted.

---

## Alternatives Considered

### Option 1: Build ADR-0128 as written — two bespoke enforcement tiers plus a generated registry

**Description:** A typed emit envelope for producers we own, an Elasticsearch ingest pipeline with rename rules and provenance stamps for those we do not, and a field registry generating both.

**Pros:**
- No new infrastructure; everything lands in the Elasticsearch already running.
- The rename table is deliverable in days and unblocks FRE-1036's reindex window.
- Fully self-contained — no dependency on a standard's release cycle.

**Cons:**
- Every component is a bespoke reimplementation of something OTel ships: the envelope is the SDK, the ingest pipeline is the Collector, the registry is semconv. All three become ours to maintain forever.
- **It cannot fix the 11.36%.** Renaming fields and filling sentinels does not create identity that was never propagated; it makes absence *look* like presence, which AC-3 of that ADR then measures as success.
- It cannot produce a trace waterfall at all, because it has no span model — so the owner's stated target is unreachable by construction.

**Why Rejected:** It solves a naming problem that is a symptom. The disease is that identity is hand-threaded, and the only cure for forgetting is not having to remember. Its diagnosis was excellent and is carried forward wholesale; its mechanisms were the wrong build.

### Option 2: Elastic APM — OTLP into the stack already running

**Description:** Stand up Elastic APM Server, which ingests OTLP natively, and view traces in Kibana. Keep Elasticsearch as the single backend for every signal.

**Pros:**
- Genuinely OTLP-native; the instrumentation half of this ADR is unchanged and fully reusable.
- One storage system, one operational surface, one backup story — and the Elastic stack is already owned and understood here.
- Preserves the invested Kibana work, including ADR-0090's surface reconciliation and FRE-533's panel inventory.

**Cons:**
- Adds APM Server, so the container saving over Tempo is one component, not zero — and it puts trace volume onto the Elasticsearch that is already at 92% of its memory cap with a 602-shard pathology.
- Deepens Elastic coupling at the moment the rest of the decision is deliberately reducing it.
- Grafana was independently wanted as the Kibana replacement, so this path keeps a UI the owner intends to retire.

**Why Rejected:** It is the closest contender and would be the right answer if Elasticsearch were healthy. It is not: the trace store would land on the component under the most pressure, and the pressure is what FRE-1036 is racing. Tempo puts traces somewhere with no shard model at all.

### Option 3: Grafana as a Kibana replacement only

**Description:** Point Grafana at the Elasticsearch datasource, rebuild the dashboards, retire Kibana. Change nothing else.

**Pros:**
- Smallest possible change; no instrumentation work, no new data path.
- Better alerting and a single pane over multiple datasources, immediately.

**Cons:**
- Fixes nothing measured above. Same 11.36% identity, same two latency names, same lost span tree, same content in the index.
- A nicer view of a corpus that cannot answer the questions being asked of it.

**Why Rejected:** Swapping the dashboard tool does not change the data model beneath it. This is retained as a *consequence* of D6 rather than an alternative to it — the swap happens anyway, as part of a decision that also fixes the data.

### Option 4: Full LGTM replatform now — Loki and Prometheus as well as Tempo

**Description:** Move logs to Loki and metrics to Prometheus in the same change, reducing Elasticsearch to a small application datastore.

**Pros:**
- Resolves the shard pathology permanently — Loki has no shard model — rather than deferring it to FRE-1036.
- Ends the three-signals-in-one-index confusion completely.
- Roughly memory-neutral: Kibana and Elasticsearch relief pay for most of the new components.

**Cons:**
- Two more stateful services to operate, back up and reason about, for one person.
- A real log migration, on top of an instrumentation change that is already the larger risk.
- Changes the storage layer and the instrumentation layer simultaneously, which is how migrations fail.

**Why Rejected:** Sequencing only, and it is explicitly the next rung. The target is trace visibility; Loki and Prometheus are not required to reach it, and adding them would make the first observable outcome later and riskier.

### Option 5: A hosted backend — Splunk Observability, Grafana Cloud or Datadog

**Description:** Instrument with OTel and export to a SaaS platform instead of self-hosting a trace store.

**Pros:**
- Near-zero operational load — no containers to patch, back up or debug.
- Splunk in particular is OTel-native and already familiar to the owner, which is worth real day-one productivity.

**Cons:**
- **Retention defeats the purpose.** The motivating questions are longitudinal — did responses get faster, cheaper, better, across the changes shipped since April. Free tiers keep traces for weeks, which is the single axis these questions cannot tolerate.
- Telemetry demonstrably contains verbatim user turns and auto-indexed tool `stdout`/`stderr`, from a personal agent holding the owner's knowledge graph, plus two other testers' data. Export is a data-governance decision that has not been made and should not be made implicitly.
- Paid tiers are real recurring cost for a single-user research harness.

**Why Rejected:** On retention and on content, not on quality — all three are good products. D5's vendor-neutral Collector keeps every one of them one exporter block away, so this is a deferral rather than a door closing.

---

## Consequences

### Positive Consequences

- **The span tree becomes real.** "Which tool calls made up this step, and where did the time go" becomes answerable for the first time; today the edge is not recorded.
- **Identity stops depending on memory.** Context propagation makes `trace_id` presence a property of the runtime rather than of every author remembering a parameter — which is the only durable fix for 11.36%.
- **The latency ambiguity cannot recur.** Span duration is intrinsic; there is no field to name and therefore no second name to diverge into.
- **`slm_server` binds without a cross-repository release.** It ships OTLP to a Collector endpoint, and cross-process context propagation puts its spans inside the calling turn's trace — which no amount of field renaming could achieve.
- **A single, auditable egress point exists** for the first time, which is where redaction belongs and where any future export decision is enforced.
- **Three ADRs stop being open.** ADR-0093 is realized, ADR-0128's mechanisms resolve, and ADR-0090's deferred field-registry open decision is answered by adopting semconv instead of building one.
- **Kibana's 551 MiB is recovered**, and the replatform is close to memory-neutral on the current box.

### Negative Consequences

- **Instrumentation touches the call chain.** Bridging `TraceContext` to OTel context reaches 19 explicit call sites and the orchestrator's step loop, in a codebase with 7,000+ tests. This is the largest risk in the decision and it is not mitigated by anything except sequencing.
- **Three more containers to operate** — Collector, Tempo, Grafana — against one retired. Patching, backups and failure modes are the owner's, alone.
- **Observability shares a failure domain with the observed system.** A VPS-level outage takes the trace store with it. This is not a regression — Elasticsearch is already on the same box — but it is not fixed here either, and a second VPS remains the correct eventual answer.
- **Dashboards are rebuilt.** ADR-0090's reconciliation work and FRE-533's panel inventory targeted Kibana; that effort is partly re-spent in Grafana.
- **Interim double-instrumentation.** Between the structlog processor and the retirement of `RequestTimer`, both span mechanisms exist. Queries work throughout, but the transitional period is where a half-migration could quietly persist.
- **Metrics remain mis-shaped.** `sensor_poll` and the budget counters stay as log records until the deferred rung lands, so the 52% `metrics.sampled` volume problem is untouched by this ADR.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Instrumentation stalls half-done, leaving two span systems and no waterfall | **High** | D4 sequences the structlog processor first — it moves the 11.36% figure before any container is committed, so the decision is falsified early and cheaply |
| This becomes the fifth telemetry ADR that changes nothing | **High** | The target is an observable artifact, not a state; AC-1 cannot be satisfied by a merged PR, and AC-5 cannot be satisfied by unparented spans |
| `TraceContext` bridge breaks per-user scoping or visibility filtering | **High** | `TraceContext` is retained and keeps its `user_id` / `authenticated` fields; only `trace_id` / `span_id` ownership moves. AC-9 asserts the scoping behaviour directly rather than trusting the refactor |
| Trace volume lands on an Elasticsearch already at 92% of its cap | Medium | Traces go to Tempo, not ES; ES volume falls rather than rises, and FRE-1036 relieves the shard pathology on its own timeline |
| Content reaches a backend it should not | Medium | D5 makes the Collector the only egress; AC-7 proves redaction fires with a positive control rather than assuming it |
| `slm_server` change does not land, cross-process traces stay broken | Medium | AC-6 measures spans appearing in the caller's trace, not merge status of another repository |
| FRE-1036's shard deadline slips because this looks like it supersedes it | Medium | It does not — logs stay in Elasticsearch. Stated explicitly in D6 and in the implementation sequence below |

---

## Implementation Notes

**Files affected (this repository):**

- `pyproject.toml` — `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, instrumentation packages (none present today).
- `src/personal_agent/telemetry/logger.py:232` — register the span-context structlog processor in `structlog.configure`.
- `src/personal_agent/telemetry/trace.py` — `TraceContext` reads identity from the active span; retains `user_id`, `session_id`, `kind`, `eval_mode`, `authenticated`.
- `src/personal_agent/telemetry/request_timer.py:88` — `RequestTimer` retired in favour of SDK spans.
- `src/personal_agent/orchestrator/executor.py:5373` — the step span becomes a real parent; `tool_execution_completed` collapses into it.
- `src/personal_agent/tools/executor.py:462,481` — tool spans become children of the step span.
- `src/personal_agent/llm_client/` — model-call spans carry `gen_ai.*` semconv attributes.
- `docker-compose.yml` — Collector, Tempo, Grafana; Kibana removed.
- `config/otel/` — Collector configuration, including redaction processors.

**Files affected (`slm_server`, separate repository, separate ticket):** OTLP export to the Collector endpoint, replacing the client-formatted index URL at `src/slm_server/telemetry.py:38`.

**Sequence:** structlog processor (measure the identity share before and after) → SDK bootstrap and `TraceContext` bridge → step/model/tool spans → Collector → Tempo + Grafana → Kibana retirement → `slm_server` OTLP.

**Dependencies:** FRE-1036 (index consolidation — **independent of this ADR and still on its own deadline**; logs remain in Elasticsearch) · FRE-1037 (role-enum widening, supplying the `purpose` vocabulary that `gen_ai.operation.name` adopts) · ADR-0093 (the standard this realizes).

**Testing strategy:** the parity test at `tests/personal_agent/llm_client/test_telemetry_parity.py` is re-pointed from frozen field-name sets to span attribute conformance. The 7,000-test suite is the regression net for the `TraceContext` bridge, and AC-9 asserts the scoping behaviour the bridge is most likely to break.

---

## Verification / Acceptance Criteria

**On when these are evaluated.** These are *post-implementation* invariants over **real production traffic**, never a synthetic probe alone — a probe proves a code path exists, not that the system converged. Where a positive control is needed to prove a detector fires, it is stated. The pre-change identity figures recorded in Context (11.36% `trace_id`, 2.15% `session_id`, measured 2026-07-30) are the baseline several criteria compare against.

- **AC-1 — A real turn's trace renders as a nested waterfall.** · **Check:** take a `trace_id` from a user turn served in the trailing 24 hours; open it in Grafana. · *Fails if* the trace is absent, or contains a single span, or shows the orchestrator step, model call and tool calls as **siblings rather than nested**. Depth is the discriminator: unparented spans still produce a trace view, and that is precisely today's defect rendered in colour.

- **AC-2 — Correlation resolves in both directions, on production data.** · **Check:** (a) from a span in Tempo, follow Grafana's trace-to-logs link and confirm it returns non-empty Elasticsearch log records whose `trace_id` equals the span's; (b) take a log record written after cutover, and confirm its `trace_id` resolves to an existing trace in Tempo. · *Fails if* either direction returns empty, or (b) resolves to no trace. (b) is what catches a `trace_id` that is present but fabricated — a stamped constant satisfies an `exists` query and fails to resolve.

- **AC-3 — Identity presence is a property of the runtime, not of authorship.** · **Check:** over a 7-complete-day post-cutover window on `agent-logs-*`, the share of records carrying a `trace_id` that **resolves to a span in Tempo**, against the recorded pre-change 11.36%; and, for five sampled served turns, *every* log record emitted during the turn carries a resolvable `trace_id`. · *Fails if* the aggregate resolvable share is below 95%, **or** any record in a sampled turn lacks one. Resolvability rather than presence is the whole criterion: presence alone is satisfiable by writing a literal, which is the failure mode ADR-0128's sentinel design would have institutionalised.

- **AC-4 — The latency ambiguity is gone at source, and no spans are being dropped.** · **Check:** (a) zero records written after cutover carry `duration_ms` or `latency_ms`; (b) the count of model-call spans over a 7-day window reconciles with the Postgres cost-ledger call count for the same window within 5%. · *Fails if* either legacy field appears on a post-cutover record, or the counts diverge beyond tolerance. (b) is load-bearing: (a) alone is satisfied by a producer that stopped emitting *anything*, and source-to-sink parity against an independent ledger is what distinguishes "renamed" from "silently lost".

- **AC-5 — The tool span tree is complete and correctly parented.** · **Check:** for a turn that invoked N tools (N established independently from that trace's `tool_call_started` count in Elasticsearch), the trace contains exactly N tool spans, each a descendant of the step span, and no child span's duration exceeds its parent's. · *Fails if* the count differs, any tool span is not a descendant of the step span, or a child outlives its parent. This is the criterion that directly asserts the defect measured in Context — 4,140 parent records carrying no `span_id` — has been fixed, and it cannot be satisfied by emitting spans that are merely present.

- **AC-6 — `slm_server`'s work appears inside the calling turn's trace, and it stops minting indices.** · **Check:** after the `slm_server` change, (a) a model call served by `slm_server` appears as a span whose `trace_id` equals the calling turn's, and (b) no new `slm-requests-YYYY.MM.DD` index is created over a 7-day window. · *Fails if* `slm_server` spans carry an unrelated `trace_id` (cross-process propagation not working — the hard part, and the point), or a new dated index appears. (a) is unfakeable from inside this repository: only genuine context propagation across the process boundary produces a shared trace.

- **AC-7 — The Collector is the only egress, and its redaction demonstrably fires.** · **Check:** (i) *positive control* — emit a span carrying an attribute matching a declared redaction rule and confirm the attribute is **absent** in Tempo; (ii) no process in `docker-compose.yml` is configured with a backend endpoint other than the Collector. · *Fails if* the planted attribute survives to storage, or any service exports directly. The positive control is required because a redactor that never fires produces the same clean result as one that works.

- **AC-8 — "Did responses get faster?" is answerable from one query.** · **Check:** a Grafana panel plotting per-day p50 and p95 turn duration from spans returns a continuous non-empty series across a ≥14-day post-cutover window, computed **without unioning two fields**. · *Fails if* the series has gaps, or answering it requires reading more than one duration field. This is the criterion that ties the decision back to the question that motivated it; a system that produces traces nobody can aggregate has not delivered.

- **AC-9 — The `TraceContext` bridge did not break identity-scoped behaviour.** · **Check:** a memory-recall request from an authenticated user returns that user's `group`-visibility memory, and the same request unauthenticated does not — asserted against the existing FRE-229 / FRE-673 visibility tests plus one live request pair. · *Fails if* either result changes from pre-migration behaviour. `TraceContext` carries `user_id` and `authenticated` into visibility filtering, so a bridge that "works" for tracing while dropping those fields would be invisible to every other criterion here and would silently widen data access.

**Seam owner:** the **`/adr` session that authored this ADR** owns the assembled-intent criteria. **AC-1, AC-2, AC-5, AC-6 and AC-8** hold only once *every* child ticket has landed — no individual child can prove them — so this ADR does not close when its last child merges. Master's acceptance gate asserts those five before ADR-0129 moves to Implemented.

---

## References

- ADR-0004 — Telemetry & Metrics Implementation Strategy (Accepted): set the telemetry model; left the field vocabulary and the trace store unspecified
- ADR-0064 — per-user scoping, the reason `TraceContext` retains `user_id` through the bridge
- ADR-0068 — Agent Self-Telemetry Data Plane (Accepted): documented the `prompt_tokens` / `input_tokens` divergence on 2026-05-10; the `duration_ms` / `latency_ms` split measured here is a third instance of the same pattern
- ADR-0074 — End-to-End Traceability & Joinability (Accepted): the identity tuple and join-key discipline this ADR delivers by propagation rather than by convention
- ADR-0090 — Telemetry Surface Contract (Accepted): its deferred *"declared field registry"* open decision is answered here by adopting semantic conventions instead of building one
- ADR-0093 — OpenTelemetry at the Substrate Boundary (Accepted, unrealized): the standard this ADR realizes; FRE-583 has sat unapproved since 2026-06-21
- ADR-0128 — Telemetry Naming and Structure Convention (Proposed): superseded by this ADR except its Context, D1 and D9 — see D8 for the disposition table
- `src/personal_agent/telemetry/trace.py` — `TraceContext`, threaded explicitly through 19 call sites; the mechanism this decision replaces
- `src/personal_agent/telemetry/logger.py:232` — `structlog.configure`, where D4's span-context processor is registered
- `src/personal_agent/telemetry/request_timer.py:88,110,119` — `RequestTimer.start_span` / `end_span`, the hand-rolled tracing this decision retires
- `src/personal_agent/orchestrator/executor.py:5373` — `tool_execution_completed`, the parent that emits no `span_id`
- `src/personal_agent/tools/executor.py:462,481` — `tool_call_completed` / `tool_call_failed`, the children whose parent edge is lost
- `docker/elasticsearch/index-template.json` — the `free_text` dynamic template that auto-indexes `stdout`, `stderr` and `raw_*` without declaration; filed separately, not addressed here
- Linear FRE-1043 — the ticket this ADR was authored under; its original rename-table scope is superseded
- Linear FRE-1036 — index consolidation, on its own shard-ceiling deadline and independent of this decision
- Linear FRE-1038 — the originating naming investigation
- [OpenTelemetry — traces data model and context propagation](https://opentelemetry.io/docs/specs/otel/overview/)
- [OpenTelemetry — semantic conventions for generative AI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [OpenTelemetry Collector — architecture and processors](https://opentelemetry.io/docs/collector/)
- [Grafana Tempo — trace-to-logs correlation](https://grafana.com/docs/grafana/latest/datasources/tempo/configure-tempo-data-source/)

---

## Status Updates

### 2026-07-30 — Proposed
**Changed By:** `/adr` session (FRE-1043)
**Reason:** Owner-directed, arising from a design session that began as ADR-0128's rename-table deliverable and established by live measurement that the naming divergence is a symptom: identity is hand-threaded through 19 call sites, which is why `trace_id` sits at 11.36%; elapsed time carries two concurrent names for the entire corpus; and the step-to-tool span edge is emitted without a `span_id` and therefore lost. The owner set the target as trace visibility, which no naming convention can reach.
