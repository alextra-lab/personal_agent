# ADR-0129: OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar

**Status:** Proposed
**Date:** 2026-07-30
**Deciders:** Project owner (FRE-1043, owner-directed 2026-07-30)
**Tags:** telemetry, observability, opentelemetry, tracing, instrumentation, grafana

---

## Context

### What is being decided

Whether to instrument `personal_agent` with the OpenTelemetry SDK — spans, context propagation, semantic conventions — and where the resulting traces are stored and viewed. This ADR supersedes the enforcement mechanisms of ADR-0128 and un-parks ADR-0093 D3, choosing a different backend than the one FRE-588 proposed.

### Why a fifth telemetry ADR must have a different shape

Four ADRs have touched this ground and changed nothing observable. ADR-0128 catalogued that failure precisely and then proposed a fifth mechanism of its own design. The distinguishing property of this ADR is not a better mechanism — it is a **target that cannot be satisfied by a document**:

> Open a real turn in Grafana and see its span waterfall — the orchestrator step, the model call, the tool calls nested beneath it, with real durations — then click through to that turn's logs.

"Adopt OpenTelemetry" is an unobservable state, which is why ADR-0093 has been Accepted since 2026-06-21 with `grep -rn "gen_ai" src/` still returning nothing. "I can see a trace" is observable, and a half-instrumented system produces a flat or empty waterfall rather than a passing test.

### Why ADR-0093 stalled: the standard was chosen, the destination was parked

ADR-0093's status line records the shape of the stall exactly: *D1/D2 accepted & sequenced (FRE-583); **D3 (OTLP exporter) parked** behind FRE-588; D4 confirmed-deferred; D5 adopted.* The naming half was approved and the *destination* half was parked — so spans had nowhere to go, and the naming half was never built either. FRE-588, the un-park ticket, was filed 2026-06-21 and was still unapproved when this ADR was authored on 2026-07-30 (Linear state read that day; ticket state is not repository-verifiable, so this is an as-of claim rather than a standing one).

This ADR does not re-choose the standard. It commits the destination, which is the half that was missing.

### What was measured

All figures were taken live against the production cluster on 2026-07-30, during the design session that produced this ADR. They are a snapshot and drift with every run; the ratios are what the decision rests on.

**Identity is absent far more often than present.** Over `agent-logs-*` (3,299,635 documents, spanning 2026-04-15 → 2026-07-30):

| Field | Documents | Share |
|---|---|---|
| `@timestamp`, `event_type` | 3,299,635 | 100% |
| `trace_id` | 374,685 | **11.36%** |
| `session_id` | 70,940 | **2.15%** |

**This is not a discipline failure at the emit sites.** `TraceContext` (`telemetry/trace.py`) is threaded **by hand**: an `ast-grep` census of `src/` finds **19 function signatures** accepting `trace_ctx: TraceContext` as an explicit parameter. (That is a count of *signatures*, not of call sites; no call-site census was taken, and none is claimed.) Every emit site must therefore remember to pass identity and every author must remember to accept it. A convention cannot fix that, because the failure mode is *forgetting*, and a rule adds one more thing to remember. Context propagation removes the need to remember. That is the entire argument for the SDK over any naming rule.

**Elapsed time has two concurrent names, and the populations are disjoint.**

| | Documents | Date range |
|---|---|---|
| `duration_ms` | 22,771 | 2026-04-15 → 2026-07-30 |
| `latency_ms` | 14,146 | 2026-04-15 → 2026-07-30 |
| **carrying both** | **0** | — |
| **union (eligible population)** | **36,917** | — |

Not a rename split at a point in time — both span the whole corpus. The intersection was measured and is **zero**, so the two sets partition the eligible population exactly: a query on either name alone reaches **61.7%** or **38.3%** of records that carry an elapsed time, and returns no error for the rest. `model_call_completed` appears under *both* names (4,272 and 3,361) — the same event, from two code paths, each with its own field. This is a third undocumented instance of the divergence ADR-0068 recorded on 2026-05-10, and it landed on the metric the owner most wants to ask about.

**The span tree already exists and is already being lost.** Two emit sites describe one tool invocation at two granularities:

```
orchestrator/executor.py:5373   tool_execution_completed   duration_ms, tool_count   (the step: N tools)   n=4,140
tools/executor.py:462           tool_call_completed        latency_ms, span_id       (one tool)            n=4,084
tools/executor.py:481           tool_call_failed           latency_ms, span_id                             n=254
```

`tool_call_started` (4,338) reconciles exactly with `tool_call_completed` + `tool_call_failed`. The parent emits **no `span_id`**, so the edge between step and tool is unrecoverable: the system cannot answer "which tool calls made up this step." A hand-rolled span timer already exists at `telemetry/request_timer.py:88` (`start_span`, `end_span`). **We are building a tracing system, badly, and losing the one property that makes tracing worth having.**

**The event vocabulary is not trustworthy either.** `metrics.sampled` is the single largest event type at **1,720,095 documents — 51.5% of the 3,339,414-document telemetry corpus** (`agent-*` plus `slm-requests-*` plus `user-turn-ratings-*`, measured the same day) — and contains no metrics. It is the Redis bus logging *"I published a message"*, with `event_type` populated from the stream name being published to.

**The storage cost is a live operational risk.** Elasticsearch holds **602 active shards for 719 MB** — about 1.2 MB per shard — and sits at **1.839 GiB of a 2 GiB container cap (92%)**. Shard overhead is the reason. ES is a functional dependency of Captain's Log (`captains_log/capture.py:556`), the cost gate and the joinability monitors, so this is memory pressure on something the application needs in order to work.

**Telemetry contains conversation content.** `user_message` holds verbatim user turns (1,524 documents), and `docker/elasticsearch/index-template.json` carries a dynamic template auto-indexing as full text anything matching `^(.*_message|.*_content|.*_description|reason|hint|stderr|stdout|raw_.*|.*_text|.*_prompt|content|content_value|.*_preview|.*_excerpt|summary)$`. No declaration is required, so tool `stdout`, `stderr` and `raw_*` land searchable without review. **This is not caused by this decision and is explicitly not fixed by it** — it is recorded because it establishes why a single egress chokepoint has value, and it is filed as its own ticket.

### The resource envelope

The VPS has **22 GiB total**, with production containers consuming roughly **4.8 GiB** (Elasticsearch 1.84, Neo4j 0.93, reranker 0.57, Kibana 0.55, gateway 0.50, all others under 0.4 combined). There is real headroom. Kibana's 551 MiB is recovered by this decision.

---

## Decision

### D1 — Instrument with the OpenTelemetry SDK; context propagation replaces hand-threading

`personal_agent` adopts the OpenTelemetry Python SDK. Trace and span identity propagate through OTel's context mechanism rather than being passed as function arguments.

**`TraceContext` is bridged, not deleted.** OTel takes ownership of `trace_id` and `span_id` only. `TraceContext` retains `user_id`, `session_id`, `kind`, `eval_mode` and `authenticated`, and reads its trace identity from the active span rather than minting its own. Those retained fields are load-bearing outside telemetry — `user_id` and `authenticated` drive ADR-0064 per-user scoping and FRE-229/FRE-673 visibility filtering, `eval_mode` gates evaluation isolation, `kind` separates organic from scheduled work — so the bridge must preserve each of them behaviourally, and AC-9 asserts each one individually rather than trusting the refactor. Existing signatures keep working throughout; this is not a flag-day change.

### D2 — Semantic conventions are the vocabulary; no project field registry is built

Names come from OpenTelemetry semantic conventions where one exists (`gen_ai.operation.name`, `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`), and from a namespaced project key where none does. This is ADR-0093 D1/D2 restated, absorbing FRE-583.

The `duration_ms` / `latency_ms` divergence is resolved structurally rather than by decree: **span duration is intrinsic to a span**, so there is no field to name and therefore no second name to diverge into.

ADR-0093 D5 governs stability — pin a named semconv version and record it; attributes not yet stable ride under a namespaced project key until they stabilise. `gen_ai.*` is explicitly still evolving and is governed by that rule.

### D3 — Real spans replace the hand-rolled timer, and every entrypoint opens one

The span tree is **root → step → {model-call, tool-call}**, with model-call and tool-call spans as **siblings** beneath the step span — not nested one inside the other. This is stated explicitly because the intuitive reading is wrong: tool execution begins *after* the model response returns, so a tool span cannot be a child of the model-call span it was triggered by without misrepresenting causality as containment. The step span is the only legitimate common parent.

`RequestTimer` (`telemetry/request_timer.py:88`) is retired rather than extended, and the `tool_execution_completed` / `tool_call_completed` pair collapses into the step span with one child span per tool.

**The existing tool log records (`tool_call_started` / `tool_call_completed` / `tool_call_failed`) are retained until AC-5 has passed, and retired only afterwards.** Keeping the old signal alive until the new one is proven is what gives AC-5 an expected tool count per turn; the spans and the log records are different mechanisms, so they do not fail together silently. This is stated as a decision because the natural instinct is to delete them in the same change that adds the spans, which would leave tool-span completeness unverifiable.

**Every entrypoint opens a root span — including background ones.** A scheduler tick, a consolidation run, a monitor poll and a startup sequence each begin a trace, exactly as a served turn does. This is decided here rather than left to implementation because without it there is a permanent class of records emitted outside any span, for which no processor can supply identity — which is the gap that made ADR-0128 invent sentinels. Records emitted before SDK bootstrap (module import time) remain outside any trace; they are a small, enumerable set and AC-3 excludes them explicitly by name rather than by tolerance.

### D4 — The first deliverable is bootstrap + root span + structlog processor, together

A structlog processor registered in `telemetry/logger.py:232` reads the active span context and injects `trace_id`, `span_id` **and `session_id`** onto every log record, with no change to any emit site. `session_id` is included deliberately: `TraceContext` already carries it (D1), ADR-0128 D3 made it a spine field, and without it that guarantee would be silently abandoned rather than replaced — and AC-3's turn-membership check would have no key to group by.

**The processor alone cannot move the 11.36% figure, and this ADR does not claim it can.** It reads the *active span*; with no SDK bootstrapped and no spans opened, it injects nothing. The first deliverable is therefore the smallest combination that produces an observable change: **SDK bootstrap + a root span at the request boundary + the processor**. That is still days of work, still reversible, and still lands before any container is committed — and if it does not move the identity share, nothing further should be built.

### D5 — The OpenTelemetry Collector is the single egress point **for traces**

**All *trace* telemetry leaves the process via OTLP to a Collector, and no producer exports spans to a backend directly.** The scope word is load-bearing and the weaker claim is the honest one: **logs continue to reach Elasticsearch directly** through the existing `es_logger` path (D7), so the Collector is *not* a universal egress chokepoint and this ADR must not be read as making one. Log content — including the verbatim `user_message` and the auto-indexed `stdout` / `stderr` measured in Context — still reaches storage unredacted, and closing that is the separately filed ticket, not this decision.

Two consequences are the reason rather than side effects. First, it is the one place where **span** redaction can be declarative and auditable rather than scattered across emit sites — a real but partial improvement, bounded to the trace signal. Second, it gives `slm_server` a network endpoint to ship to, replacing the client-side index URL formatting at its `telemetry.py:38` that ADR-0128 D8 could not solve from inside this repository.

**The vanilla upstream Collector is used, not a vendor distribution** — not Grafana Alloy, not EDOT, not the Splunk or Datadog distros. All are competent; choosing the neutral one is what keeps the backend a configuration line rather than a commitment, and it is what makes Option 5's deferral real rather than rhetorical.

### D6 — Tempo stores traces; Elasticsearch keeps logs; Grafana correlates and replaces Kibana

- **Tempo** receives spans, and its `query_frontend.metrics.max_duration` is configured to at least 14 days (the documented default is 24 hours, which would make AC-8's fortnight-long percentile query unrunnable — a configuration this ADR commits to rather than discovers).
- **Elasticsearch keeps the logs**, and keeps serving Captain's Log, insights, ratings and the cost gate unchanged. There is no log migration and no historical trace backfill.
- **Grafana** is the single UI. Its Tempo datasource links span → logs against the Elasticsearch logs datasource on `trace_id`, and back. **Kibana is retired**, recovering 551 MiB; its dashboards are rebuilt in Grafana.

**Why Tempo rather than Elastic's own OTLP path**, which FRE-588 proposed and which the owner previously asked for by name: not capability. Kibana's APM/Traces UI renders distributed traces perfectly well, and any claim otherwise would be false. The reason is capacity and direction. Traces would land on the one component already at 92% of its memory cap with a 602-shard pathology, as data streams that add more indices to the thing under pressure; and Grafana is independently replacing Kibana, so routing traces to a Kibana-only UI builds on a surface being retired. Tempo has no shard model at all. **FRE-588 is superseded by this ADR** and should be closed rather than left as a competing parked plan.

### D7 — Scope boundary, stated rather than implied

**Metrics are not in this decision.** Prometheus is not deployed; metric-shaped emissions (`sensor_poll`'s CPU and memory series, budget counters) stay as log records. They are the correct next rung and are not required by the target — so the 51.5% `metrics.sampled` volume problem is untouched here.

**Nothing goes off-box.** No SaaS exporter is configured.

**History is not migrated.** Existing telemetry is not backfilled with trace identity, and no reindex is performed by this ADR. Pre-cutover records keep whatever identity they have. This is a deliberate owner ruling — the priority is that changes support the work going forward, not that historical data is made to fit — and it is why AC-3 and AC-4 are scoped to post-cutover windows.

### D8 — Disposition of ADR-0128, decision by decision

ADR-0128's diagnosis was correct and is carried forward wholesale. Its mechanisms are not adopted. **Where a guarantee is dropped rather than replaced, this table says so plainly** — claiming the SDK and Collector are drop-in equivalents of its enforcement tiers would be the same overclaim ADR-0128 criticises in its own predecessors.

| ADR-0128 | Disposition |
|---|---|
| Context / diagnosis | **Survives** — carried forward above |
| D1 — OTel is the vocabulary | **Survives**, restated as D2 |
| D2 — `@timestamp` is the record timestamp, no aliases | **Survives, narrowed.** Elasticsearch remains the log store and `@timestamp` remains its record timestamp; the no-alias rule stands. The six-spelling retirement across the *other* families is **dropped**, not solved — those families keep their spellings until they are re-emitted through the SDK |
| D3 — mandatory-for-presence spine (four fields) | **Partly replaced, partly dropped.** `trace_id` and `session_id` come from propagated context, on spans and (via D4) on log records. `event.name` coverage is **dropped as a guarantee** — semconv names spans, not every log record's event key, and no criterion here asserts it |
| D3 — governed-for-naming surface (59 cross-family names, every `*_id`, every ADR-0090 trap-class field) | **Abandoned for log records, replaced only on spans.** Semconv governs span and metric attribute names; it says nothing about the field names inside an Elasticsearch log document. Outside the spine, log-record field naming becomes **ungoverned** — a real regression against ADR-0128's intent, accepted because governing 59 names by decree is what the previous four ADRs already failed to do |
| D3 — canonical work taxonomy (Postgres `purpose` vocabulary adopted by Elasticsearch) | **Replaced in scope, narrowed in reach.** `gen_ai.operation.name` carries it on spans, and FRE-1037 still supplies the vocabulary. The guarantee that the *Elasticsearch* `role` / `model_role` fields converge on it is **dropped** |
| D4 — sentinels, violation tagging, ceilings | **Dropped, not replaced.** D3's root-span-on-every-entrypoint removes most of the population sentinels existed to cover, but there is no violation-tagging equivalent and none is proposed |
| D5 — two enforcement tiers | **Replaced with a weaker guarantee, stated honestly.** The SDK is not a typed exclusive envelope: it will not reject a misspelled non-canonical attribute the way D5's Tier 1 promised. The Collector normalises but ships no violation provenance. Both enforcement properties are **abandoned**, in exchange for identity that propagates rather than being asserted |
| D6 — generated field registry | **Replaced for *names* only** (semconv). The registry's other declarations — per-field type, required-or-optional, owning families — and its **generation of Elasticsearch templates and Tier-1 field sets are abandoned**, along with the CI drift gate that would have made a hand-edit a build failure. Templates stay hand-written and can drift, exactly as today |
| D7 — reindex + retention migration | **Dropped** per D7 above; history is not migrated |
| D8 item 1 — `slm_server` `ts` → `@timestamp` | **Moot rather than dissolved.** ADR-0128 D2 is explicit that semconv does *not* name a stored document's date field, so "semconv handles it" would be false. The rename disappears because `slm-requests` **stops being written to Elasticsearch at all** — that telemetry becomes spans in Tempo. Any `slm-requests-*` documents that remain keep `ts` untouched |
| D8 items 2–3 — `slm_server` token names, write alias | **Survives, modified.** Item 2 dissolves into semconv (these *are* span attributes: `gen_ai.usage.*`); item 3 becomes "ship OTLP to the Collector," which subsumes the index-naming problem entirely |
| D9 — Neo4j `entity_class`, Redis stream enum | **Survives untouched** — orthogonal to tracing, and still worth doing |

ADR-0128 moves to **Superseded** when this ADR is Accepted.

---

## Alternatives Considered

### Option 1: Build ADR-0128 as written — two bespoke enforcement tiers plus a generated registry

**Description:** A typed emit envelope for producers we own, an Elasticsearch ingest pipeline with rename rules and provenance for those we do not, and a field registry generating both.

**Pros:**
- No new infrastructure; everything lands in the Elasticsearch already running.
- Genuinely stronger on *validation* than this ADR: a typed exclusive envelope rejects misspelled keys at development time, and pipeline provenance records which rules still fire. This ADR abandons both (D8).
- The rename table is deliverable in days and unblocks FRE-1036's reindex window.

**Cons:**
- Every component reimplements something OTel ships — the envelope is the SDK, the pipeline is the Collector, the registry is semconv — and all three become ours to maintain.
- **It cannot create identity that was never propagated.** Its criteria correctly distinguish real identities from sentinels and hold an absolute count against a recorded baseline, so it does not confuse filling with preservation — but the best outcome it can reach is honest bookkeeping of an 11.36% figure, not a higher one.
- It has no span model, so a waterfall is unreachable by construction and the owner's target cannot be met.

**Why Rejected:** It treats naming as the disease. Naming is a symptom of identity being hand-threaded, and the cure for forgetting is not having to remember. Its diagnosis is carried forward wholesale; its mechanisms are not — at the cost, recorded above, of two real validation guarantees.

### Option 2: EDOT Collector into existing Elasticsearch, traces in the Kibana APM UI (FRE-588)

**Description:** The path ADR-0093 D3 was parked behind and FRE-588 specifies — an Elastic Distribution of OpenTelemetry Collector writing OTLP into self-hosted ES 8.19, with Kibana's APM/Traces UI rendering the waterfalls. The owner asked for exactly this in June: *"Can Elastic provide this, as we already have it?"*

**Pros:**
- **It works, and the capability objection is void** — Kibana's APM UI renders distributed traces properly. Any rejection resting on "Elastic can't show traces" would be false.
- One storage system, one backup story, one operational surface, all already owned and understood.
- The instrumentation half of this ADR (D1–D5) is entirely unchanged and reusable, so this is a backend swap, not a different project.
- Answers the owner's own recorded question affirmatively, and un-parks ADR-0093 D3 by its intended route.

**Cons:**
- Puts trace volume on the single component measured at **92% of its memory cap with 602 shards over 719 MB** — the one under real pressure, and the one Captain's Log and the cost gate depend on.
- Elastic APM data streams add indices to a cluster whose index count is already the subject of a shard-ceiling deadline (FRE-1036).
- Ties trace visibility to Kibana, which is being retired in favour of Grafana — building the target on a surface with a scheduled end.

**Why Rejected:** On capacity and direction, not capability. It would be the right answer if Elasticsearch were healthy; it is measurably not, and Tempo has no shard model to make worse. The owner's June question was answered correctly for June — Grafana was not yet the intended UI and the shard pathology was not yet measured. **FRE-588 is superseded by this ADR**, not left parked alongside it.

### Option 3: Grafana as a Kibana replacement only

**Description:** Point Grafana at the Elasticsearch datasource, rebuild the dashboards, retire Kibana. Change nothing else.

**Pros:**
- Smallest possible change; no instrumentation work, no new data path.
- Better alerting and a single pane over multiple datasources, immediately.

**Cons:**
- Fixes nothing measured above: same 11.36% identity, same two disjoint latency fields, same lost span tree.
- Grafana's Elasticsearch datasource has no trace view, so the target remains unreachable.

**Why Rejected:** Swapping the dashboard tool does not change the data model beneath it. Retained as a *consequence* of D6 rather than an alternative to it — the swap happens anyway, inside a decision that also fixes the data.

### Option 4: Full LGTM replatform now — Loki and Prometheus as well as Tempo

**Description:** Move logs to Loki and metrics to Prometheus in the same change, reducing Elasticsearch to a small application datastore.

**Pros:**
- Resolves the shard pathology permanently — Loki has no shard model — rather than leaving it to FRE-1036.
- Ends the three-signals-in-one-index confusion completely, including the 51.5% `metrics.sampled` volume.
- Roughly memory-neutral: Kibana and Elasticsearch relief pay for most of the new components.

**Cons:**
- Two more stateful services to operate and back up, for one person.
- A real log migration on top of an instrumentation change that is already the larger risk.
- Changes storage and instrumentation simultaneously, which is how migrations fail.

**Why Rejected:** Sequencing only, and it is explicitly the next rung. Neither Loki nor Prometheus is required to reach the target, and adding them makes the first observable outcome later and riskier.

### Option 5: A hosted backend — Splunk Observability, Grafana Cloud or Datadog

**Description:** Instrument with OTel and export to a SaaS platform instead of self-hosting a trace store.

**Pros:**
- Near-zero operational load — no containers to patch, back up or debug.
- Splunk in particular is OTel-native and already familiar to the owner, worth real day-one productivity.

**Cons:**
- **Retention defeats the purpose.** The motivating questions are longitudinal; free tiers keep traces for weeks, which is the single axis these questions cannot tolerate.
- Telemetry demonstrably carries verbatim user turns and auto-indexed tool `stdout`/`stderr`, from a personal agent holding the owner's knowledge graph plus two testers' data. Export is a data-governance decision that has not been made.
- Paid tiers are recurring cost for a single-user research harness.

**Why Rejected:** On retention and content, not quality. D5's neutral Collector keeps all three one exporter block away.

---

## Consequences

### Positive Consequences

- **The span tree becomes real.** "Which tool calls made up this step, and where did the time go" becomes answerable; today the edge is not recorded at all.
- **Identity stops depending on memory.** Propagation makes `trace_id` presence a property of the runtime rather than of every author remembering a parameter.
- **The latency ambiguity cannot recur.** Span duration is intrinsic, so there is no field to name and no second name to diverge into.
- **`slm_server` becomes joinable at all.** This requires **one cross-repository release** — the ADR does not pretend otherwise, and AC-6 gates on it — but after that release its spans land inside the calling turn's trace through cross-process propagation, which no field rename could achieve, and subsequent backend changes are Collector configuration rather than another release.
- **An auditable egress point exists for traces**, bounded as stated in D5 — logs still reach Elasticsearch directly, so this is a partial improvement, not a universal chokepoint.
- **Two parked decisions resolve.** ADR-0093 D3 un-parks with a committed destination, and ADR-0090's deferred field-registry question is answered by adopting semconv rather than building one.
- **Kibana's 551 MiB is recovered.**

### Negative Consequences

- **Two validation guarantees are abandoned, not replaced** (D8): ADR-0128's typed exclusive envelope would have rejected misspelled attributes at development time, and its pipeline provenance would have recorded which normalisation rules still fire. Neither has an equivalent here. A misspelled span attribute will be accepted silently.
- **Instrumentation touches the call chain.** Bridging `TraceContext` reaches 19 function signatures and the orchestrator step loop, in a codebase with 7,000+ tests. This is the largest risk and is mitigated only by sequencing.
- **Three more containers to operate** — Collector, Tempo, Grafana — against one retired.
- **Observability shares a failure domain with the observed system.** A VPS-level outage takes the trace store with it. Not a regression (Elasticsearch is already on the box), but not fixed; a second VPS remains the eventual answer.
- **Dashboards are rebuilt.** ADR-0090's reconciliation and FRE-533's panel inventory targeted Kibana; that effort is partly re-spent.
- **Historical telemetry stays as it is** — a permanent discontinuity at cutover, with pre-cutover data unable to answer trace-shaped questions.
- **Metrics remain mis-shaped**, so the largest single volume class is untouched by this ADR.
- **ADR-0128's other five timestamp spellings survive** in families not yet re-emitted through the SDK.
- **Log-record field naming becomes ungoverned outside the spine.** Semconv governs span attributes, not the field names inside an Elasticsearch log document, so ADR-0128's 59 cross-family names, `*_id` fields and trap-class fields lose their naming guarantee entirely (D8). The `duration_ms` / `latency_ms` class of divergence is prevented *on spans* and remains possible *in logs*.
- **`slm_server` must expose an inspectable effective configuration** for AC-7 — a new obligation on a repository we do not own, on top of the OTLP change itself.
- **Background-entrypoint coverage is observed, not proven.** Scheduler ticks, consolidation runs and monitor polls have no independent Postgres ledger — nothing records that they happened except the telemetry under test — so a lost background trace and its lost log record are jointly invisible. AC-2 reports that coverage without gating on it. Closing this would mean building an invocation ledger purely to satisfy a criterion, which this ADR declines; the gap is named in AC-2 rather than papered over.
- **AC-5 is not fully independent.** Its expected tool count comes from log records in the same repository as the spans it checks. Rule 4's population guard converts the worst case from a false pass into an inconclusive result, but it is a guard, not independence.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Instrumentation stalls half-done, leaving two span systems and no waterfall | **High** | D4 sequences bootstrap + root span + processor as one first deliverable that moves the 11.36% figure before any container is committed, falsifying the decision early and cheaply |
| This becomes the fifth telemetry ADR that changes nothing | **High** | The target is an observable artifact; AC-1 and AC-5 are population-level over a window and cannot be satisfied by one happy-path trace or by a merged PR |
| `TraceContext` bridge silently drops a retained field, widening data access | **High** | AC-9 asserts each retained field's behaviour individually — visibility, session isolation, eval isolation, `kind` — rather than testing tracing and assuming the rest |
| Trace volume lands on an Elasticsearch already at 92% of cap | Medium | Traces go to Tempo; ES trace volume is zero, and FRE-1036 relieves the shard pathology independently |
| Spans emitted but not parented — a trace view that looks fine and answers nothing | Medium | AC-5 reconciles tool-span count and ancestry per turn across a window, and fails on any mis-parented turn |
| Dummy or timing-wrong spans satisfy count parity | Medium | AC-4 reconciles sampled span start/end against the production model-call records, not just counts |
| `slm_server` change does not land, cross-process traces stay broken | Medium | AC-6 measures shared-trace propagation per emit path against its own request records, not merge status |
| FRE-1036's deadline slips because this looks like it supersedes it | Medium | It does not — logs stay in Elasticsearch. Stated in D6 and D7 |

---

## Implementation Notes

**Files affected (this repository):**

- `pyproject.toml` — `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, instrumentation packages (none present today).
- `src/personal_agent/telemetry/logger.py:232` — register the span-context structlog processor in `structlog.configure`.
- `src/personal_agent/telemetry/trace.py` — `TraceContext` reads identity from the active span; retains `user_id`, `session_id`, `kind`, `eval_mode`, `authenticated`.
- `src/personal_agent/telemetry/request_timer.py:88` — `RequestTimer` retired in favour of SDK spans.
- `src/personal_agent/orchestrator/executor.py:5373` — the step span becomes a real parent; `tool_execution_completed` collapses into it.
- `src/personal_agent/tools/executor.py:462,481` — tool spans become children of the step span.
- `src/personal_agent/llm_client/` — model-call spans carry `gen_ai.*` attributes.
- `src/personal_agent/brainstem/scheduler.py` and the monitor entrypoints — root spans per D3.
- `docker-compose.yml` — Collector, Tempo, Grafana; Kibana removed.
- `config/otel/` — Collector configuration including redaction processors; Tempo `query_frontend.metrics.max_duration`.
- `src/personal_agent/tools/executor.py` — the `tool_call_started` / `tool_call_completed` / `tool_call_failed` log records are **retained** through verification (D3) and retired only once AC-5 has passed; they supply AC-5's expected tool count per turn.
- **`scripts/audit/adr0129_trace_verifier.py`** — new, committed with the implementation. The acceptance criteria are population-level and cross-store (Postgres → Tempo → Elasticsearch); this is the artifact that runs them, so each criterion is executable rather than aspirational. It follows the precedent of `scripts/audit/fre1038_naming_census.py`. It also **writes and reads a baseline file** holding the measured delivery loss over the first 48 hours after cutover (bounded by rule 2's 0.5% ceiling) and each stratum's pre-cutover population size for rule 4's non-vacuity guard. Clock skew is deliberately *not* a recorded quantity: AC-4 compares span **duration** against `api_costs.latency_ms` rather than comparing timestamps across hosts, so skew never enters the comparison.

**Files affected (`slm_server`, separate repository, separate ticket):** OTLP export to the Collector endpoint, replacing the client-formatted index URL at `src/slm_server/telemetry.py:38`, across all four emit paths (chat, responses, rerank, streaming); plus a **machine-readable effective-configuration endpoint or artifact** the verifier can read, since AC-7 must inspect a host it cannot otherwise reach.

**Sequence:** SDK bootstrap + request-boundary root span + structlog processor (measure identity share before/after) → `TraceContext` bridge → step/model/tool spans → background root spans → Collector → Tempo + Grafana → Kibana retirement → `slm_server` OTLP.

**Dependencies:** FRE-583 (ADR-0093 D1/D2 — absorbed by D2 here) · FRE-588 (ADR-0093 D3 un-park — **superseded**, close it) · FRE-1036 (index consolidation — **independent and still on its own deadline**; logs remain in Elasticsearch) · FRE-1037 (role-enum widening, supplying the vocabulary `gen_ai.operation.name` adopts).

**Testing strategy:** `tests/personal_agent/llm_client/test_telemetry_parity.py` is re-pointed from frozen field-name sets to span attribute conformance. The 7,000-test suite is the regression net for the bridge, and AC-9 asserts the behaviours the bridge is most likely to break.

---

## Verification / Acceptance Criteria

**On when and over what these are evaluated.** These are *post-implementation* invariants over **real production traffic** across a defined window, never a single trace — a working example proves a code path exists, not that the system converged, and a half-rollout can always produce one good trace. Unless stated otherwise the window is **7 complete days after cutover** and the runner is `scripts/audit/adr0129_trace_verifier.py` (committed per Implementation Notes). The pre-change identity figures in Context (11.36% `trace_id`, 2.15% `session_id`, measured 2026-07-30) are the baseline.

**Four rules govern every criterion below. They exist because the obvious formulations are all satisfiable by a broken system, and each rule names the specific way its formulation fails.**

1. **Population is enumerated from Postgres `api_costs`, never from the telemetry under test.** `api_costs` is the per-call billing ledger (`docker/postgres/init.sql`, written by `llm_client/cost_tracker.py` over a raw asyncpg pool). It carries `trace_id UUID NOT NULL`, `session_id`, `provider`, `model`, `purpose`, `latency_ms` and `timestamp`; it does not traverse the OTLP path, the structlog path, or Elasticsearch; and **no cleanup task purges it**. Every served turn makes at least one model call, so every turn appears with a `trace_id` that must resolve. *`session_events` is deliberately not used*: it is the AG-UI WebSocket transport buffer (ADR-0075), its rows are purged by the cleanup task at `ws_event_ttl_hours` — **default 24** — and a 7-day or 14-day denominator cannot be drawn from a table that empties daily.
2. **Structural correctness carries zero tolerance; delivery is measured separately, and the baseline is bounded.** A mis-parented or malformed span is a defect, not noise, so no allowance applies to *shape*. Span **loss** is a different quantity: the exporter's drop rate is measured over the first 48 hours after cutover and recorded in the verifier's baseline file, and steady-state loss is judged against it. **The recorded baseline must itself be below 0.5%, or the run is rejected rather than accepted at a lenient number** — a self-measured tolerance with no ceiling would let a rollout suffering severe loss enshrine that loss as its own passing grade. The 0.5% ceiling is not arbitrary: the hop is process → local Collector with a persistent queue, where sustained loss above a fraction of a percent indicates a misconfiguration rather than normal operation.
3. **Every criterion is evaluated per stratum, and empty strata are named rather than skipped.** Strata are the partitions the ledger can actually express — `provider`, `model` and `purpose` from `api_costs`, plus entrypoint class. A global percentage lets one wholly uninstrumented low-volume path hide inside the allowance, which is the failure this ADR exists to eliminate. A stratum with no traffic **fails the run unless explicitly listed as inactive in the verifier output**, so an untested path is visible rather than silently absent.
4. **Every criterion fails on an empty or implausibly small population.** The verifier records each criterion's enumerated population size and compares it against the pre-cutover volume for that stratum (from the same ledger, before the change). A criterion whose population has collapsed **fails as inconclusive rather than passing**. This is the general guard against the most dangerous failure mode available to an acceptance suite: a check that quietly evaluates zero rows and reports success.

- **AC-1 — Every tool-using turn renders as a correctly shaped waterfall.** · **Check:** enumerate turns over the window from `api_costs` (distinct `trace_id`), and select those whose retained tool log records (D3) show at least one tool call; for each, fetch the trace from Tempo and assert the shape **root → step → {model-call, tool-call}**, with model-call and tool-call spans as *siblings* beneath the step span and every span having its expected parent. · *Fails if* **any** enumerated turn whose trace was delivered has a missing parent link, a single-span trace, or a tool span parented to a model-call span — **or** if rule 4's population guard trips. Turns with **no trace at all** are counted separately as delivery loss and judged against the bounded baseline (rule 2); availability and structural correctness are different failures and may not mask each other. The shape assertion is the discriminator: unparented spans still render a trace view, and that is today's defect drawn in colour.

- **AC-2 — Correlation resolves in both directions for served turns, and background coverage is reported.** · **Check:** (a) for every `trace_id` in `api_costs` over the window, the trace resolves in Tempo; (b) for each `purpose` stratum, at least one trace is followed through Grafana's trace-to-logs link and returns non-empty Elasticsearch records whose `trace_id` equals the span's; (c) for background entrypoints, the resolvable share is **measured and reported per entrypoint, but not gated**. · *Fails if* (a) falls below the bounded delivery baseline, or any `purpose` stratum's trace-to-logs direction returns empty, or (c) is unreported. **The gap is named rather than hidden:** background entrypoints have no independent Postgres ledger — nothing records that a scheduler tick happened except the telemetry under test — so a lost background trace *and* its lost log are jointly invisible. Building an invocation ledger purely to close this is scope this ADR declines; the honest consequence is that background coverage is observed, not proven, and D3's root-span commitment is what makes it observable at all.

- **AC-3 — Identity is a property of the runtime, and each trace belongs to exactly one turn.** · **Check:** over the window, (a) **per `purpose` stratum**, the share of post-bootstrap log records carrying a `trace_id` that resolves in Tempo, against the 11.36% baseline; (b) for every `trace_id` in `api_costs`, all log records carrying that trace id share the **one** `session_id` the ledger records for it, and that `trace_id` appears against **no other** `session_id` — membership established from the ledger row, never from the trace id under test; (c) records emitted before SDK bootstrap are enumerated **by logger name** and reported as an explicit excluded list, not absorbed into a tolerance. · *Fails if* any stratum's resolvable share is below the delivery baseline, **or** any trace id maps to more than one session, **or** (c) is unenumerated, **or** rule 4 trips. (b) carries the weight: a producer stamping one valid, resolvable id onto every record passes a bare presence-and-resolvability test, and only independent membership rejects it. This is why D4 injects `session_id` as well as the trace ids — without it there is no key to group by.

- **AC-4 — Elapsed time has one representation, and spans are measurements rather than placeholders.** · **Check:** (a) zero records written after cutover carry `duration_ms` or `latency_ms`; (b) **per `provider`**, model-call span count reconciles against that provider's `api_costs` row count for the window, within the bounded delivery baseline; (c) **for every** model-call span matched to its `api_costs` row by `trace_id` — a full-population join, not a sample — the span's **duration** agrees with that row's recorded **`latency_ms`** within 10% — a tolerance for the two timers bracketing slightly different boundaries (the span opens marginally before, and closes marginally after, the client's own stopwatch), not for measurement error; a dummy span cannot land inside it by chance. · *Fails if* a legacy field appears post-cutover, any provider's counts diverge beyond baseline, or any joined span's duration disagrees beyond tolerance. Comparing *durations* against a column the ledger already records — rather than comparing timestamps across hosts — is what removes clock skew from the question entirely; there are two independently measured elapsed times for the same call, and they must agree. (a) alone is satisfied by a producer that stopped emitting; (b) alone is satisfied by one dummy span per ledger row; (c) is what forces the spans to be measurements of the thing they claim to measure.

- **AC-5 — The tool span tree is correctly parented, over a population proven non-empty.** · **Check:** for each turn enumerated from `api_costs`, N is that turn's tool-call count from the retained tool log records (D3); assert the trace contains exactly N tool spans, each a child of the step span, and no child span's duration exceeds its parent's. · *Fails if* any turn's tool-span count differs from N, any tool span is not a child of the step span, a child outlives its parent, **or** the tool-log population over the window has collapsed against its pre-cutover volume (rule 4). The retained log records are the same repository as the spans, so they are not fully independent — this is stated rather than glossed. Rule 4 is what makes the residual risk tolerable: if both mechanisms were dropped together, the population guard trips and the criterion reports inconclusive instead of passing.

- **AC-6 — `slm_server` exports OTLP on every active emit path and stops minting dated indices.** · **Check:** after the `slm_server` change, over the window: (a) total `slm_server` span count reconciles against its `api_costs` rows within the bounded delivery baseline, and every such span's `trace_id` equals the calling turn's ledger `trace_id`; (b) spans are stratified by an emit-path attribute **that `slm_server` must set**, and each of the four paths (chat, responses, rerank, streaming) either shows shared-trace propagation or is listed as inactive; (c) no new `slm-requests-YYYY.MM.DD` index is created. · *Fails if* the total diverges beyond baseline, any active path shows no propagation, any path is neither active nor listed inactive, or a new dated index appears. **A limitation is recorded honestly:** `api_costs` has **no emit-path column**, so per-path *denominators* are not independently available — (a) gates the total against the ledger and (b) gates per-path presence using an attribute the producer supplies. A path that both stopped emitting and stopped serving traffic would be indistinguishable, which is why an inactive path must be declared rather than inferred. (a) remains unfakeable from inside this repository: only genuine cross-process propagation yields a shared trace id.

- **AC-7 — Collector redaction demonstrably fires, and no producer bypasses it.** · **Check:** (i) *positive control* — emit a span carrying an attribute matching a declared redaction rule and confirm the attribute is **absent** in Tempo; (ii) every producer publishes a **machine-readable effective-configuration artifact** that the verifier reads — including `slm_server`, whose ticket must expose one, since it runs on a separate host the verifier cannot otherwise inspect — and no artifact names an OTLP endpoint other than the Collector. · *Fails if* the planted attribute survives to storage, any producer is configured to export elsewhere, or **any producer publishes no artifact** (an uninspectable producer is a failure, not an exemption). The positive control is required because a redactor that never fires yields the same clean result as one that works. **Scope is explicit and narrow:** this covers the OTLP trace path only. Logs reach Elasticsearch directly (D5), so the `free_text` dynamic template auto-indexing `stdout`, `stderr` and `raw_*` is untouched by this criterion and is filed separately. This must not be read as asserting log-side redaction.

- **AC-8 — "Did responses get faster?" is answerable from one query over a fortnight.** · **Check:** a Grafana panel plotting per-day p50 and p95 turn duration from spans returns a non-empty series for **every day on which `api_costs` records model calls** across a ≥14-day post-cutover window, computed **without unioning two fields**; and each active day's turn-span count reconciles against that day's distinct `api_costs` `trace_id` count within the bounded delivery baseline. · *Fails if* an active day is empty, answering requires more than one duration field, or a day's span count diverges beyond baseline. Continuity is defined over *active* days because a genuinely idle day is not a defect and a criterion that failed one would be unachievable. `api_costs` supplies the fortnight-long denominator that `session_events` cannot, being purged at 24 hours (rule 1). The count reconciliation is load-bearing: one surviving span per day yields a continuous series that is a lie. This criterion also depends on D6's committed `max_duration` configuration — a 14-day metrics query fails against Tempo's 24-hour default.

- **AC-9 — The `TraceContext` bridge preserved every field it retained.** · **Check:** one behavioural assertion per retained field, against existing suites plus a live request pair: `authenticated` — an authenticated recall request returns `group`-visibility memory and an unauthenticated one does not (FRE-229/FRE-673); `user_id` — a request as user A never returns user B's scoped rows (ADR-0064); `session_id` — session-scoped history stays isolated across two concurrent sessions; `eval_mode` — an evaluation-mode request does not write to production substrate (FRE-375); `kind` — a scheduler-originated trace is distinguishable from a user-originated one. · *Fails if* any assertion changes behaviour from pre-migration. D1 commits to retaining all five; a bridge that works for tracing while silently dropping one would be invisible to every other criterion here, and in the `authenticated` case would widen data access.

**Seam owner:** the **`/adr` session that authored this ADR** owns the assembled-intent criteria. **AC-1, AC-2, AC-3, AC-5, AC-6 and AC-8** hold only once *every* child ticket has landed — no individual child can prove them — so this ADR does not close when its last child merges. Master's acceptance gate asserts those six before ADR-0129 moves to Implemented.

---

## References

- ADR-0004 — Telemetry & Metrics Implementation Strategy (Accepted): set the telemetry model; left both the field vocabulary and the trace store unspecified
- ADR-0064 — per-user scoping; the reason `TraceContext` retains `user_id` through the bridge (AC-9)
- ADR-0068 — Agent Self-Telemetry Data Plane (Accepted, 2026-05-10): documented the `prompt_tokens` / `input_tokens` divergence; the disjoint `duration_ms` / `latency_ms` split measured here is a third instance of the same pattern
- ADR-0074 — End-to-End Traceability & Joinability (Accepted): the identity tuple this ADR delivers by propagation rather than by convention
- ADR-0090 — Telemetry Surface Contract (Accepted — 2026-06-21): its deferred *"declared field registry"* open decision is answered here by adopting semantic conventions instead of building one
- ADR-0093 — OpenTelemetry at the Substrate Boundary (Accepted with scope change — 2026-06-21; D1/D2 sequenced as FRE-583, **D3 parked behind FRE-588**, D4 confirmed-deferred, D5 adopted): the standard this ADR realizes, and the parked destination it commits
- ADR-0128 — Telemetry Naming and Structure Convention (Proposed): superseded except its Context, D1, D9, a narrowed D2 and a modified D8 — see the D8 disposition table, which records which guarantees are replaced and which are abandoned
- `src/personal_agent/telemetry/trace.py` — `TraceContext`, accepted by 19 function signatures in `src/`; the mechanism this decision replaces
- `src/personal_agent/telemetry/logger.py:232` — `structlog.configure`, where D4's span-context processor is registered
- `src/personal_agent/telemetry/request_timer.py:88,110,119` — `RequestTimer.start_span` / `end_span`, the hand-rolled tracing this decision retires
- `src/personal_agent/orchestrator/executor.py:5373` — `tool_execution_completed`, the parent that emits no `span_id`
- `src/personal_agent/tools/executor.py:462,481` — `tool_call_completed` / `tool_call_failed`, the children whose parent edge is lost
- `docker/elasticsearch/index-template.json` — the `free_text` dynamic template auto-indexing `stdout`, `stderr` and `raw_*` without declaration; explicitly out of scope (D7, AC-7) and filed separately
- `docker/postgres/init.sql` (`api_costs`) and `src/personal_agent/llm_client/cost_tracker.py` — the durable per-call billing ledger every criterion enumerates from, chosen because it carries `trace_id UUID NOT NULL`, `provider`, `purpose` and `latency_ms`, does not traverse the telemetry path under test, and has no cleanup task
- `src/personal_agent/config/settings.py:121` (`ws_event_ttl_hours`, default 24) and `src/personal_agent/transport/agui/event_buffer.py:160` — why `session_events` is **not** the enumeration source: it is the AG-UI transport buffer and is purged daily
- `scripts/audit/fre1038_naming_census.py` — the ADR-0128 census whose measurements this ADR's Context re-uses, and the precedent for the committed verifier
- Linear FRE-1043 — the ticket this ADR was authored under; its original rename-table scope is superseded
- Linear FRE-583 — ADR-0093 D1/D2, unapproved since 2026-06-21; absorbed by D2
- Linear FRE-588 — ADR-0093 D3 un-park via EDOT into Elasticsearch; filed 2026-06-21 and still unapproved as of 2026-07-30 per the author's Linear check that day (ticket state is not repository-verifiable, so this is an as-of observation, not a standing status). **Superseded by D6** — see Option 2
- Linear FRE-1036 — index consolidation, on its own shard-ceiling deadline and independent of this decision
- Linear FRE-1038 — the originating naming investigation
- [OpenTelemetry — traces data model and context propagation](https://opentelemetry.io/docs/specs/otel/overview/)
- [OpenTelemetry — semantic conventions for generative AI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [OpenTelemetry Collector — architecture and processors](https://opentelemetry.io/docs/collector/)
- [Grafana Tempo — TraceQL metrics and query-frontend limits](https://grafana.com/docs/tempo/latest/operations/traceql-metrics/)
- [Elastic — EDOT Collector and OTLP intake](https://www.elastic.co/docs/reference/opentelemetry/) — the Option 2 path FRE-588 specifies

---

## Status Updates

### 2026-07-30 — Proposed
**Changed By:** `/adr` session (FRE-1043)
**Reason:** Owner-directed, arising from a design session that began as ADR-0128's rename-table deliverable and established by live measurement that the naming divergence is a symptom: `TraceContext` is accepted by 19 hand-threaded function signatures, which is why `trace_id` sits at 11.36%; elapsed time carries two disjoint concurrent names across the whole corpus; and the step-to-tool span edge is emitted without a `span_id` and therefore lost. The owner set the target as trace visibility, which no naming convention can reach, and ruled that historical data is not to be made to fit.
