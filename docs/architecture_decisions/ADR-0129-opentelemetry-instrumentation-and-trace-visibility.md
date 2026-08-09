# ADR-0129: OpenTelemetry Instrumentation, with Trace Visibility as the Acceptance Bar

**Status:** Accepted — 2026-07-31 (owner, relayed in session); **D6 amended 2026-08-07** (retirement deferred) and **again 2026-08-08** — retirement is directed and sequenced, with the owner declaring when it is complete (see Status Updates)
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

The VPS has **22 GiB total**, with production containers consuming roughly **4.8 GiB** (Elasticsearch 1.84, Neo4j 0.93, reranker 0.57, Kibana 0.55, gateway 0.50, all others under 0.4 combined). There is real headroom. This decision originally claimed to recover Kibana's 551 MiB; **that claim is withdrawn and stays withdrawn** — the Status Update of 2026-08-07 records the measurement showing the figure never carried the weight given it. The 2026-08-08 ruling directs Kibana's retirement (D6) on Grafana's demonstrated superiority, **not** on memory: reinstating the recovery as a justification would resurrect an argument that was measured false.

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

**"The Collector" means the terminus, not the first hop** — ruled 2026-08-09 (FRE-1225), on the FRE-1220 study's Proposal 4. A producer may export to a Collector **on its own host, over loopback**, which forwards to the VPS Collector. That is ordinary OpenTelemetry agent→gateway topology and it honours this decision's actual prohibition, which is on exporting to a **backend** directly — a local Collector is not a backend. Two bounds stop it becoming a general licence:

- **Same host, loopback only.** An intermediate Collector reached over a network is not permitted: it would add a hop that is neither inside the trust boundary nor behind the edge, and the reason for the hop — holding a credential the producer should not hold — does not require one.
- **The terminus is the VPS Collector**, where redaction is applied and from which Tempo is written. **Redaction of everything that arrives is therefore unaffected by the hop** — and that is the whole of the claim. It is *not* a claim that the hop sends nothing anywhere else: a hop fanning out an unredacted copy to a second destination would leave arrivals fully redacted and the copy unaccounted for. AC-7 (ii)–(iii) do not exclude that, and say so.

**What the hop costs is stated rather than glossed, because it is a capability limit and not a wording choice.** The vanilla upstream Collector **publishes no effective configuration**: its zpages expose component *names* (`servicez`, `pipelinez`, `extensionz`, `featurez`, `tracez`, `expvarz`), not the endpoints its exporters address, and the `configz/effective` endpoint that would is a Splunk-distribution feature — excluded by this decision's own distro choice, and removed even there. So an intermediate Collector cannot satisfy AC-7's *method*, a live self-reported config read from the running process, the way an application producer can.

**What replaces it is a reachability proof, not a termination proof, and the difference is the honest part.** AC-7 is amended to establish from the data that the producer's spans **reach** the VPS Collector — reconciled against an independent ledger population, not merely present. That is a genuine discriminator: a hop shipping *nothing* to the terminus fails it cleanly, which a config declaration nobody can read would not. It is **not** proof of exclusivity. A hop splitting its output — most to the terminus, some elsewhere — reconciles within tolerance and passes. Combined with the unreadable configuration, the residual exposure is precisely: **a second destination on the intermediate hop is invisible to this decision's verification.** That is the price of the hop, it is accepted deliberately, and it is the reason the loopback allowance is bounded to the same host rather than generalised.

**Persistent buffering is not adopted along with the hop.** The Collector's persistent sending queue requires the `file_storage` extension from contrib. An intermediate hop under this decision buffers **in memory only**, and the coverage that buys is narrower than "the process survives a sleep": the sending queue holds a batch only while retries continue, and `retry_on_failure.max_elapsed_time` defaults to **300s**, after which the batch is dropped — so a sleep outlasting the configured retry window loses spans whether or not the process lived. Coverage is therefore a function of the configured retry duration and queue size (`queue_size` default 1000), not of process lifetime. Recorded as a known limit rather than solved, so a later loss investigation starts from the right place.

### D6 — Tempo stores traces; Elasticsearch keeps logs; Grafana correlates, and Kibana's retirement is directed

- **Tempo** receives spans, and its `query_frontend.metrics.max_duration` is configured to at least 14 days (the documented default is 24 hours, which would make AC-8's fortnight-long percentile query unrunnable — a configuration this ADR commits to rather than discovers).
- **Elasticsearch keeps the logs**, and keeps serving Captain's Log, insights, ratings and the cost gate unchanged. There is no log migration and no historical trace backfill.
- **Grafana** is the trace UI, and the intended single UI. Its Tempo datasource links span → logs against the Elasticsearch logs datasource on `trace_id`, and back. Its dashboards are rebuilt from the FRE-533 panel inventory.
- **Kibana's retirement is directed and sequenced; the owner declares when it is complete** — owner ruling of 2026-08-08, quoted in the Status Update of that date, superseding the 2026-08-07 deferral. The deferral asked for evidence of Grafana's superiority before deciding; that evidence was produced (the render audit and the Postgres-backed rebuilds — FRE-1207, FRE-1209, FRE-1210, FRE-1211), and the owner ruled on it. **This ADR does not assert Kibana is gone.** It is running as this is written; the retirement lands as its own separately sequenced work (FRE-1214), and the owner has reserved the declaration that it is complete. Writing *"retired"* here would repeat, in the opposite direction, the exact error the 2026-08-07 amendment was filed to correct: an Accepted ADR asserting a state that is not true. The 551 MiB recovery this decision originally claimed **stays withdrawn** — the retirement rests on Grafana's demonstrated superiority, which is what the 2026-08-07 ruling said it should rest on.

**How Grafana is exposed** — decided 2026-08-07 with the owner, and recorded here rather than in a ticket because it is a property of "Grafana is the UI" rather than of any one delivery. Grafana is served at **its own Cloudflare-fronted host, `observe`**, referred to by that placeholder only: no literal deployment domain appears in any tracked file. It follows **Kibana's topology rather than Caddy's** — the tunnel routes the hostname straight to the container, bypassing Caddy.

That is deliberate, and the reasoning is worth keeping because the instinct is to reach for Caddy on consistency grounds. Every existing Caddy site block earns its hop by doing routing work: a path split for `agent`, a WebSocket/HTTP protocol split for `graph`, a path allowlist for `es`, header rewriting for `api`. Grafana needs none of them — one container, one port, no split to make, and an allowlist would break an interactive UI. A Caddy hop here would be pure pass-through: an added failure point and config surface earning nothing, and it would not even save a tunnel ingress rule, since a new hostname requires one wherever it points.

**Cloudflare Access is the gate; Grafana's login form is hidden, not its authentication.** Grafana ships a login screen where Kibana has none (`docker/kibana/kibana.yml` configures no authentication; Elasticsearch security is off). Grafana runs with **anonymous access at the `Viewer` role** — `auth.anonymous.enabled` with `auth.anonymous.org_role=Viewer` — and with the login **form** hidden (`auth.disable_login_form`) while **basic auth stays enabled for the admin API**, so an admin path survives the form being hidden — reachable with a password supplied from the environment. Hiding the form and disabling authentication are different settings, and only the first is done here. **Dashboards and datasources are not created through that API**: they are provisioned from files (below), and the admin credential exists for the occasional operation that has no file representation. **Grafana's router logging is enabled**, which AC-10's positive control depends on: without it Grafana does not log successful requests, and an empty log cannot distinguish a working edge gate from no logging at all.

**Its dashboards are provisioned from files in this repository** rather than assembled in the UI — load-bearing independently of authentication, because it is what makes "every Kibana dashboard has a Grafana equivalent" reviewable in a diff instead of verifiable only by logging in.

**What the sole-gate posture costs, stated plainly, because the tempting formulation — "behind Access a second password protects nothing" — is false.** Three exposures are real:

- **`Viewer` in Grafana OSS is not "may read the dashboards."** It can issue arbitrary queries against **every datasource in the org**, which here includes the Elasticsearch logs datasource — the corpus this ADR's own Context measures as holding verbatim `user_message` turns and auto-indexed tool `stdout`/`stderr`. Per-datasource permissions are a Grafana Enterprise feature and are not available to narrow this.
- **A stolen Access session, or an Access policy scoped too broadly, defeats only the edge.** An independent Grafana credential would still stand. This is precisely the case the "protects nothing" formulation gets wrong.
- **Anything reaching the compose network bypasses the edge entirely**, including via the loopback port binding over SSH.

**None of the three is a regression:** Kibana today has all three properties, against the same Elasticsearch, behind the same single gate. That equivalence — not the false formulation — is what justifies the posture. It is recorded here so the choice stays visible when the trust model changes: **if Access ever fronts more than the owner, this decision must be revisited**, because anonymous `Viewer` grants every Access-holder full query access to the logs corpus.

**The `monitoring` host is the owner's, and this decision does not touch it.** `docker-compose.cloud.yml` records that the tunnel serves `monitoring` directly from `kibana:5601`, bypassing Caddy. The ingress mapping lives in the Cloudflare dashboard rather than in this repository, so there was never a diff here to make; the owner ruled on 2026-08-08 that the repoint is theirs and outside this program's scope.

**The consequence is stated rather than left implicit.** Once Kibana's container is removed (FRE-1214), `monitoring` addresses a service that no longer exists, and **nothing in this repository will detect that**. AC-10(e) — the criterion that read the *live* route precisely because a compose comment can stay accurate-looking long after a repoint — is **retired together with the subject it tested** (see Verification). That state is therefore unmonitored **by design and by owner ruling**, not by oversight. Grafana is unaffected: it has its own `observe` host, and AC-10(a)–(d) continue to assert it.

**Why Tempo rather than Elastic's own OTLP path**, which FRE-588 proposed and which the owner previously asked for by name: not capability. Kibana's APM/Traces UI renders distributed traces perfectly well, and any claim otherwise would be false. The reason is capacity and direction. Traces would land on the one component already at 92% of its memory cap with a 602-shard pathology, as data streams that add more indices to the thing under pressure; and Grafana — not Kibana — is the owner's chosen trace UI, so routing traces into a Kibana-only surface would build the target on a surface not chosen for it. Tempo has no shard model at all. **FRE-588 is superseded by this ADR** and should be closed rather than left as a competing parked plan.

*(The direction argument above has been reworded twice and now rests on the one clause that survived both rulings. It originally read "Kibana is being retired… a surface being retired"; the 2026-08-07 deferral removed the scheduled end and made that false, so it was restated to rest on Grafana being the **chosen** trace UI. The 2026-08-08 ruling restores a directed retirement — and the wording is deliberately **not** reverted, because "Grafana is the chosen trace UI" held under the deferral and holds under the retirement, which is exactly what makes it the right thing for a rejection to rest on.)*

### D7 — Scope boundary, stated rather than implied

**Metrics are not in this decision.** Prometheus is not deployed; metric-shaped emissions (`sensor_poll`'s CPU and memory series, budget counters) stay as log records. They are the correct next rung and are not required by the target — so the 51.5% `metrics.sampled` volume problem is untouched here.

**Nothing goes off-box.** No SaaS exporter is configured. **The scope word is "third-party", not "this host"** — clarified 2026-08-09, because D5's loopback allowance makes the bare sentence ambiguous: a producer on the Mac forwarding to the VPS Collector does leave *that* box, and is not what this rule prohibits. What it prohibits is a destination outside this deployment. ADR-0136 AC-2 already treats this rule as *adjacent* to its own — broader in signal, narrower in protocol — and neither discharges the other.

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
- Ties trace visibility to Kibana, which is not the owner's chosen trace UI — building the target on a surface not chosen for it. *(Reworded twice; see the Status Updates of 2026-08-07 and 2026-08-08. The objection is, and remains, about which surface was **chosen** as the trace UI. That clause held while Kibana was being retained and holds now that its retirement is directed, which is why it was not reverted when the ruling changed direction a second time.)*

**Why Rejected:** On capacity and direction, not capability. It would be the right answer if Elasticsearch were healthy; it is measurably not, and Tempo has no shard model to make worse. The owner's June question was answered correctly for June — Grafana was not yet the intended UI and the shard pathology was not yet measured. **FRE-588 is superseded by this ADR**, not left parked alongside it. **Both legs of this rejection have since moved and the movement is recorded rather than absorbed** — see the Status Updates of 2026-07-31, 2026-08-07 and 2026-08-08: the capacity leg weakened and then recovered, and the direction leg was restated when Kibana stopped having a scheduled end, then left unreverted when it regained one.

### Option 3: Grafana as a Kibana replacement only

**Description:** Point Grafana at the Elasticsearch datasource, rebuild the dashboards, retire Kibana. Change nothing else.

**Pros:**
- Smallest possible change; no instrumentation work, no new data path.
- Better alerting and a single pane over multiple datasources, immediately.

**Cons:**
- Fixes nothing measured above: same 11.36% identity, same two disjoint latency fields, same lost span tree.
- Grafana's Elasticsearch datasource has no trace view, so the target remains unreachable.

**Why Rejected:** Swapping the dashboard tool does not change the data model beneath it. It is a *consequence* of D6 rather than an alternative to it — the swap happens anyway, inside a decision that also fixes the data. The 2026-08-08 ruling directs exactly this swap as its own sequenced work, which does **not** promote Option 3 to the decision: on its own it still leaves every defect measured in Context untouched.

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
- ~~**Kibana's 551 MiB is recovered.**~~ **Withdrawn 2026-08-07, and still withdrawn** — the measurement in that day's Status Update shows the figure was never load-bearing. The 2026-08-08 retirement ruling does **not** reinstate it: the retirement was directed on Grafana's demonstrated superiority, and the memory figure remains a measured-false argument that must not be resurrected because the conclusion it once supported came back.

### Negative Consequences

- **Two validation guarantees are abandoned, not replaced** (D8): ADR-0128's typed exclusive envelope would have rejected misspelled attributes at development time, and its pipeline provenance would have recorded which normalisation rules still fire. Neither has an equivalent here. A misspelled span attribute will be accepted silently.
- **Instrumentation touches the call chain.** Bridging `TraceContext` reaches 19 function signatures and the orchestrator step loop, in a codebase with 7,000+ tests. This is the largest risk and is mitigated only by sequencing.
- **Three more containers to operate** — Collector, Tempo, Grafana. Under the 2026-08-07 deferral this was **against none retired**, a cost the owner accepted explicitly (*"I accept maintaining the 2 UI"*) and which that Status Update's resource measurement showed was affordable. The 2026-08-08 ruling bounds the concurrent-UI period rather than ending it: **Kibana still runs until FRE-1214 lands**, so the cost is real until then, and the honest statement is "temporary" rather than "gone".
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
- `docker-compose.yml` / `docker-compose.cloud.yml` — Collector, Tempo, Grafana added. **Kibana's service block is removed by its own ticket (FRE-1214), not by this ADR's chain** (D6, retirement directed 2026-08-08); until that lands it stays declared. Grafana carries the tunnel-target comment in the style `docker-compose.cloud.yml` already uses for Kibana, naming `observe` as a placeholder and never a literal domain.
- `config/otel/` — Collector configuration including redaction processors; Tempo `query_frontend.metrics.max_duration`.
- `config/grafana/` — datasource and **dashboard provisioning files** (D6): dashboards are defined here rather than assembled in the UI, which is what makes their equivalence to the Kibana inventory reviewable in a diff.
- `src/personal_agent/tools/executor.py` — the `tool_call_started` / `tool_call_completed` / `tool_call_failed` log records are **retained** through verification (D3) and retired only once AC-5 has passed; they supply AC-5's expected tool count per turn.
- **`scripts/audit/adr0129_trace_verifier.py`** — new, committed with the implementation. The acceptance criteria are population-level and cross-store (Postgres → Tempo → Elasticsearch); this is the artifact that runs them, so each criterion is executable rather than aspirational. It follows the precedent of `scripts/audit/fre1038_naming_census.py`. It also **writes and reads a baseline file** holding the measured delivery loss over the first 48 hours after cutover (bounded by rule 2's 0.5% ceiling) and each stratum's pre-cutover population size for rule 4's non-vacuity guard. Clock skew is deliberately *not* a recorded quantity: AC-4 compares span **duration** against `api_costs.latency_ms` rather than comparing timestamps across hosts, so skew never enters the comparison.

**Files affected (`slm_server`, separate repository, separate ticket):** OTLP export to the Collector endpoint, replacing the client-formatted index URL at `src/slm_server/telemetry.py:38`, across all four emit paths (chat, responses, rerank, streaming); plus a **machine-readable effective-configuration endpoint or artifact** the verifier can read, since AC-7 must inspect a host it cannot otherwise reach.

**Sequence:** SDK bootstrap + request-boundary root span + structlog processor (measure identity share before/after) → `TraceContext` bridge → step/model/tool spans → background root spans → Collector → Tempo + Grafana (exposed at `observe`) → `slm_server` OTLP. **Kibana retirement is not a step in this sequence** — the 2026-08-08 ruling directs it as separately sequenced work (FRE-1214, under the FRE-1203 Grafana migration program), and nothing in this chain waits on it.

**Dependencies:** FRE-583 (ADR-0093 D1/D2 — absorbed by D2 here) · FRE-588 (ADR-0093 D3 un-park — **superseded**, close it) · FRE-1036 (index consolidation — **independent and still on its own deadline**; logs remain in Elasticsearch) · FRE-1037 (role-enum widening, supplying the vocabulary `gen_ai.operation.name` adopts).

**Testing strategy:** `tests/personal_agent/llm_client/test_telemetry_parity.py` is re-pointed from frozen field-name sets to span attribute conformance. The 7,000-test suite is the regression net for the bridge, and AC-9 asserts the behaviours the bridge is most likely to break.

---

## Verification / Acceptance Criteria

**On when and over what these are evaluated.** These are *post-implementation* invariants over **real production traffic** across a defined window, never a single trace — a working example proves a code path exists, not that the system converged, and a half-rollout can always produce one good trace. Unless stated otherwise the window is **7 complete days after cutover** and the runner is `scripts/audit/adr0129_trace_verifier.py` (committed per Implementation Notes). The pre-change identity figures in Context (11.36% `trace_id`, 2.15% `session_id`, measured 2026-07-30) are the baseline.

**Four rules govern every criterion below. They exist because the obvious formulations are all satisfiable by a broken system, and each rule names the specific way its formulation fails.**

1. **Population is enumerated from Postgres `api_costs`, never from the telemetry under test.** `api_costs` is the per-call billing ledger (`docker/postgres/init.sql`, written by `llm_client/cost_tracker.py` over a raw asyncpg pool). It carries `trace_id UUID NOT NULL`, `session_id`, `provider`, `model`, `purpose`, `latency_ms` and `timestamp`; it does not traverse the OTLP path, the structlog path, or Elasticsearch; and **no cleanup task purges it**. Every served turn makes at least one model call, so every turn appears with a `trace_id` that must resolve. *`session_events` is deliberately not used*: it is the AG-UI WebSocket transport buffer (ADR-0075), its rows are purged by the cleanup task at `ws_event_ttl_hours` — **default 24** — and a 7-day or 14-day denominator cannot be drawn from a table that empties daily.
2. **Structural correctness carries zero tolerance; delivery is measured separately, and the baseline is bounded.** A mis-parented or malformed span is a defect, not noise, so no allowance applies to *shape*. Span **loss** is a different quantity: the exporter's drop rate is measured over the first 48 hours after cutover and recorded in the verifier's baseline file, and steady-state loss is judged against it. **The recorded baseline must itself be below 0.5%, or the run is rejected rather than accepted at a lenient number** — a self-measured tolerance with no ceiling would let a rollout suffering severe loss enshrine that loss as its own passing grade. The 0.5% ceiling is not arbitrary: the hop is process → local Collector with a persistent queue, where sustained loss above a fraction of a percent indicates a misconfiguration rather than normal operation. **That justification describes the compose-network hop and does not transfer to the same-host loopback hop D5 permits**, which buffers in memory only and whose retry window is bounded (D5): a producer routing through such a hop may exceed this ceiling for reasons that are the topology's rather than a misconfiguration's. The ceiling still applies — a run above it is still rejected rather than accepted at a lenient number — but the *diagnosis* differs, and recording that here stops a sleeping laptop being read as a broken exporter.
3. **Every criterion is evaluated per stratum, and empty strata are named rather than skipped.** Strata are the partitions the ledger can actually express — `provider`, `model` and `purpose` from `api_costs`, plus entrypoint class. A global percentage lets one wholly uninstrumented low-volume path hide inside the allowance, which is the failure this ADR exists to eliminate. A stratum with no traffic **fails the run unless explicitly listed as inactive in the verifier output**, so an untested path is visible rather than silently absent.
4. **Every criterion fails on an empty or implausibly small population.** The verifier records each criterion's enumerated population size and compares it against the pre-cutover volume for that stratum (from the same ledger, before the change). A criterion whose population has collapsed **fails as inconclusive rather than passing**. This is the general guard against the most dangerous failure mode available to an acceptance suite: a check that quietly evaluates zero rows and reports success.

- **AC-1 — Every tool-using turn renders as a correctly shaped waterfall.** · **Check:** enumerate turns over the window from `api_costs` (distinct `trace_id`), and select those whose retained tool log records (D3) show at least one tool call; for each, fetch the trace from Tempo and assert the shape **root → step → {model-call, tool-call}**, with model-call and tool-call spans as *siblings* beneath the step span and every span having its expected parent. · *Fails if* **any** enumerated turn whose trace was delivered has a missing parent link, a single-span trace, or a tool span parented to a model-call span — **or** if rule 4's population guard trips. Turns with **no trace at all** are counted separately as delivery loss and judged against the bounded baseline (rule 2); availability and structural correctness are different failures and may not mask each other. The shape assertion is the discriminator: unparented spans still render a trace view, and that is today's defect drawn in colour.

- **AC-2 — Correlation resolves in both directions for served turns, and background coverage is reported.** · **Check:** (a) for every `trace_id` in `api_costs` over the window, the trace resolves in Tempo; (b) for each `purpose` stratum, at least one trace is followed through Grafana's trace-to-logs link and returns non-empty Elasticsearch records whose `trace_id` equals the span's; (c) for background entrypoints, the resolvable share is **measured and reported per entrypoint, but not gated**. · *Fails if* (a) falls below the bounded delivery baseline, or any `purpose` stratum's trace-to-logs direction returns empty, or (c) is unreported. **The gap is named rather than hidden:** background entrypoints have no independent Postgres ledger — nothing records that a scheduler tick happened except the telemetry under test — so a lost background trace *and* its lost log are jointly invisible. Building an invocation ledger purely to close this is scope this ADR declines; the honest consequence is that background coverage is observed, not proven, and D3's root-span commitment is what makes it observable at all.

- **AC-3 — Identity is a property of the runtime, and each trace belongs to exactly one turn.** · **Check:** over the window, (a) **per `purpose` stratum**, the share of post-bootstrap log records carrying a `trace_id` that resolves in Tempo, against the 11.36% baseline; (b) for every `trace_id` in `api_costs`, all log records carrying that trace id share the **one** `session_id` the ledger records for it, and that `trace_id` appears against **no other** `session_id` — membership established from the ledger row, never from the trace id under test; (c) records emitted before SDK bootstrap are enumerated **by logger name** and reported as an explicit excluded list, not absorbed into a tolerance. · *Fails if* any stratum's resolvable share is below the delivery baseline, **or** any trace id maps to more than one session, **or** (c) is unenumerated, **or** rule 4 trips. (b) carries the weight: a producer stamping one valid, resolvable id onto every record passes a bare presence-and-resolvability test, and only independent membership rejects it. This is why D4 injects `session_id` as well as the trace ids — without it there is no key to group by.

- **AC-4 — Elapsed time has one representation, and spans are measurements rather than placeholders.** · **Check:** (a) zero records written after cutover carry `duration_ms` or `latency_ms`; (b) **per `provider`**, model-call span count reconciles against that provider's `api_costs` row count for the window, within the bounded delivery baseline; (c) **for every** model-call span matched to its `api_costs` row by `trace_id` — a full-population join, not a sample — the span's **duration** agrees with that row's recorded **`latency_ms`** within 10% — a tolerance for the two timers bracketing slightly different boundaries (the span opens marginally before, and closes marginally after, the client's own stopwatch), not for measurement error; a dummy span cannot land inside it by chance. · *Fails if* a legacy field appears post-cutover, any provider's counts diverge beyond baseline, or any joined span's duration disagrees beyond tolerance. Comparing *durations* against a column the ledger already records — rather than comparing timestamps across hosts — is what removes clock skew from the question entirely; there are two independently measured elapsed times for the same call, and they must agree. (a) alone is satisfied by a producer that stopped emitting; (b) alone is satisfied by one dummy span per ledger row; (c) is what forces the spans to be measurements of the thing they claim to measure.

- **AC-5 — The tool span tree is correctly parented, over a population proven non-empty.** · **Check:** for each turn enumerated from `api_costs`, N is that turn's tool-call count from the retained tool log records (D3); assert the trace contains exactly N tool spans, each a child of the step span, and no child span's duration exceeds its parent's. · *Fails if* any turn's tool-span count differs from N, any tool span is not a child of the step span, a child outlives its parent, **or** the tool-log population over the window has collapsed against its pre-cutover volume (rule 4). The retained log records are the same repository as the spans, so they are not fully independent — this is stated rather than glossed. Rule 4 is what makes the residual risk tolerable: if both mechanisms were dropped together, the population guard trips and the criterion reports inconclusive instead of passing.

- **AC-6 — `slm_server` exports OTLP on every active emit path and stops minting dated indices.** · **Check:** after the `slm_server` change, over the window: (a) total `slm_server` span count reconciles against its `api_costs` rows within the bounded delivery baseline, and every such span's `trace_id` equals the calling turn's ledger `trace_id`; (b) spans are stratified by an emit-path attribute **that `slm_server` must set**, and each of the four paths (chat, responses, rerank, streaming) either shows shared-trace propagation or is listed as inactive; (c) no new `slm-requests-YYYY.MM.DD` index is created. · *Fails if* the total diverges beyond baseline, any active path shows no propagation, any path is neither active nor listed inactive, or a new dated index appears. **A limitation is recorded honestly:** `api_costs` has **no emit-path column**, so per-path *denominators* are not independently available — (a) gates the total against the ledger and (b) gates per-path presence using an attribute the producer supplies. A path that both stopped emitting and stopped serving traffic would be indistinguishable, which is why an inactive path must be declared rather than inferred. (a) remains unfakeable from inside this repository: only genuine cross-process propagation yields a shared trace id.

- **AC-7 — Collector redaction demonstrably fires, and no producer bypasses it.** *(Clause (ii) reworded and clause (iii) added 2026-08-09 for the loopback-hop topology D5 now permits; see the Status Update of that date.)* · **Check:** (i) *positive control* — emit a span carrying an attribute matching a declared redaction rule and confirm the attribute is **absent** in Tempo; (ii) every **span-originating** producer publishes a **machine-readable effective-configuration artifact** that the verifier reads — including `slm_server`, whose ticket must expose one, since it runs on a separate host the verifier cannot otherwise inspect — the artifact is **derived at read time from the process's live exporter registry**, not hand-maintained, and reports that registry in full rather than a single representative endpoint; and **every** destination it reports is one of exactly two admissible classes: the VPS Collector's ingress, or a **same-host loopback** Collector permitted by D5; (iii) where an artifact names a loopback hop, that producer's spans **reach Tempo across the population, measured as coverage rather than as a count** — for every `trace_id` in that producer's independently enumerated `api_costs` population over the window, at least one span from that producer resolves in Tempo under that trace id, and the resolvable share is judged against the bounded delivery baseline (rules 1, 2 and 4 apply in full, so an implausibly small population fails as **inconclusive**, never as a pass). · *Fails if* the planted attribute survives to storage, any artifact reports a destination outside (ii)'s two admissible classes, any artifact is hand-maintained rather than derived from the live registry, **any producer publishes no artifact** (an uninspectable producer is a failure, not an exemption), or a producer whose artifact names a loopback hop has a resolvable share below the delivery baseline — **or has no independent ledger population against which to reconcile at all, in which case (iii) is inconclusive rather than satisfied.** The positive control is required because a redactor that never fires yields the same clean result as one that works.

  **Why (ii) reads a registry rather than a value, and why (iii) measures coverage rather than a count.** The original wording — *"no artifact names an OTLP endpoint other than the Collector"* — is singular, so an artifact reporting one compliant endpoint alongside an unreported second exporter arguably satisfied it. Demanding "the complete set" does not by itself fix that, because **completeness is not decidable from the artifact**: the fix is to constrain how the artifact is *produced*. An artifact derived at read time from the live exporter registry cannot omit a registered exporter without the producer being written to lie about its own runtime state; a hand-maintained one can go stale by neglect, which is the realistic failure. That is a genuine strengthening and it is not a solution to a dishonest producer — see the limits below. · (iii) avoids a count ratio deliberately. Reconciling span *volume* against `api_costs` rows assumes one span per ledger row, an obligation nothing here imposes: a healthy producer emitting three spans per call would fail at 3:1, while a hop dropping two of three would pass at 1:1. **Coverage per `trace_id`** is cardinality-independent and asks the question that matters — does this producer's work reach the terminus — against the ledger that does not traverse the path under test (rule 1).

  **Three limits are named rather than implied, because each is a way this criterion can pass while something is wrong.** *First*, (ii) is self-report, and no construction rule changes that: a producer that reports a curated subset of its live registry defeats the clause, and **no criterion in this ADR can detect it.** What the registry-derivation rule buys is the elimination of the *stale artifact*, not of the *lying* one. *Second*, (iii) establishes **reachability, not exclusivity**: a hop splitting its output, most to the terminus and some elsewhere, keeps full per-trace coverage and passes. *Third*, combining the two, **a second destination configured on the intermediate hop is invisible to this criterion** — the vanilla Collector publishes no config to read (D5), so no clause here can see it. This is a capability limit, accepted deliberately as the price of the hop, and it is why the allowance is bounded to a Collector on the producer's own host. For `slm_server` specifically, (iii) restates AC-6(a)'s reconciliation at AC-7's scope, which keeps this criterion self-contained; the two are not independent evidence and must not be counted as such.

  **How a component is classified, since one component can be both.** The distinction is by **role for the span in question**, not by software identity. A component is a *span-originating producer* for spans it creates — including a Collector emitting its own internal telemetry, which is then subject to (ii) exactly like any application — and a *forwarding hop* for spans it relays, which is what D5's loopback allowance and clause (iii) govern. A Collector that does both is assessed under both readings rather than assigned to one.

  **Scope is explicit and narrow:** this covers the OTLP trace path only. Logs reach Elasticsearch directly (D5), so the `free_text` dynamic template auto-indexing `stdout`, `stderr` and `raw_*` is untouched by this criterion and is filed separately. This must not be read as asserting log-side redaction.

- **AC-8 — "Did responses get faster?" is answerable from one query over a fortnight.** · **Check:** a Grafana panel plotting per-day p50 and p95 turn duration from spans returns a non-empty series for **every day on which `api_costs` records model calls** across a ≥14-day post-cutover window, computed **without unioning two fields**; and each active day's turn-span count reconciles against that day's distinct `api_costs` `trace_id` count within the bounded delivery baseline. · *Fails if* an active day is empty, answering requires more than one duration field, or a day's span count diverges beyond baseline. Continuity is defined over *active* days because a genuinely idle day is not a defect and a criterion that failed one would be unachievable. `api_costs` supplies the fortnight-long denominator that `session_events` cannot, being purged at 24 hours (rule 1). The count reconciliation is load-bearing: one surviving span per day yields a continuous series that is a lie. This criterion also depends on D6's committed `max_duration` configuration — a 14-day metrics query fails against Tempo's 24-hour default.

- **AC-9 — The `TraceContext` bridge preserved every field it retained.** · **Check:** one behavioural assertion per retained field, against existing suites plus a live request pair: `authenticated` — an authenticated recall request returns `group`-visibility memory and an unauthenticated one does not (FRE-229/FRE-673); `user_id` — a request as user A never returns user B's scoped rows (ADR-0064); `session_id` — session-scoped history stays isolated across two concurrent sessions; `eval_mode` — an evaluation-mode request does not write to production substrate (FRE-375); `kind` — a scheduler-originated trace is distinguishable from a user-originated one. · *Fails if* any assertion changes behaviour from pre-migration. D1 commits to retaining all five; a bridge that works for tracing while silently dropping one would be invisible to every other criterion here, and in the `authenticated` case would widen data access.

- **AC-10 — Grafana is reachable by the owner at its own gated host, by nobody else, and grants no more than `Viewer` anonymously.** *(Clause (e) — "`monitoring` still serves Kibana" — is **retired 2026-08-08**; see the note at the end of this criterion.)* · **Check:** (a) with a valid Cloudflare Access session the `observe` host returns the Grafana UI and a **named** provisioned dashboard renders, at least one panel executing a datasource query **over the fixture data the chain already injects** and returning that fixture — not merely "without error", which a correctly-wired-but-wrong datasource also achieves. Fixture data rather than live traffic is deliberate: it makes the check non-empty by construction without making close-out depend on production volume. · (b) the anonymous role is bracketed **behaviourally, from both sides** — an anonymous `POST /api/ds/query` succeeds (the role is *at least* `Viewer`) **and** an anonymous `POST /api/dashboards/db` is refused 403 (it is *no more than* `Viewer`). Introspection is unavailable: Grafana registers `/api/user` behind `ReqSignedInNoAnonymous` and rejects anonymous callers outright, so the role cannot be read back and must be inferred from what it can and cannot do. `Editor` and `Admin` both satisfy the first clause and fail the second, which is the discrimination (b) exists for. · (c) the edge gate is proven by a **two-nonce positive control**: one request carrying a valid Access session to nonce path **A**, one without to a *different* nonce path **B**. It passes only if **A** appears in Grafana's router log (D6 requires it enabled), **B** does not, and **B**'s response is Cloudflare Access's own challenge rather than any other failure. · (d) the Access policy fronting `observe` is read and its principal set is **exactly the owner**. · *Fails if* the fixture does not come back through the named panel, the anonymous identity can create a dashboard or cannot query, **B** reaches the container or fails with anything other than an Access challenge, or the policy admits any principal beyond the owner. **An unlogged A makes (c) inconclusive, never passing.** · **Where this criterion earns its keep:** absence of a log line proves nothing alone, since Grafana does not log successful requests unless router logging is on — hence the control. Requiring Access's *specific* challenge separates "Access blocked it" from "the tunnel was down", "a WAF rule fired" or "a cache answered"; distinct nonces stop a cached **A** from satisfying **B**. **(d) exists because (a)–(c) all pass under an Access policy admitting every authenticated user** — which is the exact condition D6 names as its revisit trigger, so leaving it untested would let the assumption the whole sole-gate posture rests on fail silently. This criterion requires owner action — the tunnel ingress rule and the Access policy are outside this repository and outside CI — which is permitted of an ADR's *own* criteria (ADR-0130 D1) and is precisely why no implementation ticket can prove it.

  **Clause (e) is retired, 2026-08-08, and what that gives up is stated rather than glossed.** (e) asserted that the live `monitoring` host still resolved to Kibana, and it read the *live* route rather than `docker-compose.cloud.yml`'s comment because the ingress mapping lives in the Cloudflare dashboard, where a stale comment can stay accurate-looking indefinitely. The 2026-08-08 ruling directs Kibana's retirement, so the criterion's subject is being deleted — and the owner ruled the `monitoring` repoint out of this program's scope and into their own hands. **The reasoning that motivated (e) survives its subject:** after FRE-1214, `monitoring` addresses a container that no longer exists, and no criterion here will notice. That is accepted on the owner's ruling and recorded in D6, not treated as covered.

  **A replacement clause was considered, and the honest reason for declining it is scope, not unfalsifiability.** The first formulation tried — *"`monitoring` does not serve Kibana"* — **is** unfalsifiable: once no Kibana exists nothing can serve it, so no defect could fail the check. But a second formulation is **not**, and adversarial review (Codex, round 1) was right to press it: *read the Cloudflare ingress rule for `monitoring` and fail if it still targets `kibana:5601`*. That inspects the **route**, not the response, and a route left stale after the container's deletion fails it cleanly — which is precisely the state this ADR would otherwise never notice. It is also well within what an ADR's own criteria may demand: AC-10(a)–(d) already require owner action outside this repository, expressly permitted by ADR-0130 D1. **So the criterion was available and is being given up deliberately.** It is declined because the owner ruled the `monitoring` repoint outside this program's scope and into their own hands, and a criterion asserting a state nobody is tasked with reaching would gate this ADR on work no ticket owns. **Recorded this way so the trade is visible:** the check is not impossible, it is unowned — and if the owner ever wants it back, the formulation above is the one that works.

**Seam ticket:** **FRE-1073**, designated by FRE-1080 under ADR-0130 D2, is the single place **all ten** criteria above are asserted. No implementation ticket carries, quotes, restates or discharges any part of them (ADR-0130 D1); each child instead carries criteria written for its own deliverable, and what this ADR gates at a child's merge is **design adherence** (ADR-0130 D3). **AC-1, AC-2, AC-3, AC-5, AC-6 and AC-8** hold only once *every* child has landed, and **AC-10** additionally requires an owner action outside this repository — so none is provable by any single child, and this ADR does not close when its last child merges. An `adr` session adjudicates FRE-1073, producing one verdict per criterion — green, red or inconclusive, with the evidence and its actual output — recorded into the Status Updates below. **ADR-0129 reaches `Implemented` only if every verdict is green; otherwise it stays `Accepted`.**

**FRE-1073's AC-7 walk changed on 2026-08-09, and no other criterion moved.** The criterion count is again unchanged — all ten are still asserted at FRE-1073 — but AC-7 now reads a producer's **live exporter registry in full** rather than one endpoint, and adds clause (iii), a **per-`trace_id` Tempo coverage reconciliation against `api_costs`** for any producer routing through a loopback hop — coverage, not a presence check and not a span-count ratio. **No new seam ticket is created and none is needed:** these are ADR-0129's own criteria, they stay asserted in exactly one place, and FRE-1073 remains that place (ADR-0130 D2). AC-1 through AC-6 and AC-8 through AC-10 are untouched.

**FRE-1073's scope narrowed on 2026-08-08 and the change is recorded here so master does not have to derive it.** The criterion *count* is unchanged — all ten are still asserted there, and no criterion was added or removed — but **AC-10 lost clause (e)**, so FRE-1073's AC-10 walk no longer includes a live `monitoring` request. Nothing else moved: AC-1 through AC-9 are untouched by the retirement ruling, exactly as the 2026-08-07 amendment left them.

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
- ADR-0130 — the two-tier acceptance-criteria rule: D1 severs criterion inheritance (an ADR's criteria are its seam ticket's alone), D2 requires exactly one seam ticket per ADR, D3 keeps design adherence gating at every child merge, D6 requires an implementation ticket's criteria to be decidable from its own deliverable — the reason AC-10 belongs here and not on FRE-1072
- ADR-0132 — the Caddy egress blocks (`:8600`, `:8601`); relevant to D6 only as the reason the Caddyfile's listeners are not all inbound site blocks, which is what makes `graph` rather than "all six" the comparison Grafana was weighed against
- `config/cloud-sim/Caddyfile` — the four inbound host blocks (`agent`, `graph`, `es`, `api`), each earning its hop with routing work Grafana does not need; D6's reason for bypassing it
- `docker-compose.cloud.yml` — the Kibana service comment recording that the tunnel serves `monitoring` directly from `kibana:5601`, bypassing Caddy: the topology Grafana follows, and the mapping D6 leaves to the owner (both service block and comment are removed by FRE-1214)
- `docker/kibana/kibana.yml` — configures no authentication, with Elasticsearch security off; the reason Cloudflare Access is today's only gate and the baseline D6 held Grafana to. Removed by FRE-1214; the equivalence argument it grounds is a statement about what was true when the posture was chosen, and does not expire with the file
- `tests/integration/test_fre1072_tempo_grafana_acceptance.py` — asserts Kibana stays declared in `docker-compose.cloud.yml` and that its live status is `available`, citing D6's retention as a deliberate design decision. **Both assertions invert under the 2026-08-08 ruling**, and FRE-1214 owns that inversion — a test enforcing a superseded ruling is the mechanism by which a stale decision outlives its amendment
- Linear FRE-1203 / FRE-1214 — the Grafana migration program and its retirement ticket; the separately sequenced work D6's 2026-08-08 amendment directs, and the reason this ADR states the retirement is directed rather than done
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
- ADR-0136 — The Cloudflare Edge Carries HTTP, Not gRPC (Accepted — 2026-08-09): its D4 explicitly defers the chained-Collector question to FRE-1225, which the 2026-08-09 amendment to D5/AC-7 settles; its D2 is why protocol conversion, where any is needed, happens before the edge
- ADR-0132 — D2's environment-credential principle: the Cloudflare Access pair belongs to the environment layer rather than inside an application process, which is the surviving justification for the loopback hop D5 now permits
- `docs/research/2026-08-08-fre-1220-otlp-ingress-security-and-cloudflare-capability.md` — the FRE-1220 study; Proposal 4 raised the AC-7 wording gap and F5/F9 supply the measured custody and protocol positions
- Linear FRE-1225 — the adjudication ticket the 2026-08-09 amendment was authored under
- Linear FRE-1224 — the Mac-local Collector as credential custodian; the topology D5's loopback allowance exists for, and the ticket whose gRPC premise the amendment corrects
- Linear FRE-1071 — `slm_server`'s OTLP export; its AC-5 is the implementation-side criterion the amendment resolves, and `alextra-lab/slm_server#14` is where OTLP/HTTP over gRPC was chosen
- [OpenTelemetry Collector — zPages extension](https://github.com/open-telemetry/opentelemetry-collector/blob/main/extension/zpagesextension/README.md) — the vanilla upstream debug surface: component names, not exporter endpoints, which is the capability limit D5 records
- [Splunk Distribution of the OpenTelemetry Collector — troubleshooting](https://help.splunk.com/en/splunk-observability-cloud/manage-data/splunk-distribution-of-the-opentelemetry-collector/get-started-with-the-splunk-distribution-of-the-opentelemetry-collector/troubleshooting/general-troubleshooting) — `debug/configz/effective`, the effective-configuration endpoint that exists only in a distribution this ADR excludes
- [OpenTelemetry Collector — resiliency and the persistent sending queue](https://opentelemetry.io/docs/collector/resiliency/) — persistence requires contrib's `file_storage`, which is why D5 records the hop as buffering in memory only

---

## Status Updates

### 2026-08-09 — D5 clarified and AC-7 reworded: a same-host loopback Collector is permitted, and reachability is established from the data
**Changed By:** `/adr` session (FRE-1225), recording the owner's ruling reached in session.
**Reason:** The FRE-1220 study (Proposal 4) found AC-7's wording narrower than D5's decision: D5
prohibits exporting to a **backend**, but AC-7 required that no artifact name an OTLP endpoint other
than "the Collector" — which would fail a two-tier topology the decision itself permits. ADR-0136 D4
explicitly deferred the question here rather than settling it, and an implementer guessing either way
produces a criterion that passes vacuously or a topology that cannot pass. Status is unchanged:
**Accepted**.

**The ticket's premise was stale, and correcting it changed the answer.** FRE-1225 and FRE-1224 both
state that `slm_server` exports OTLP/**gRPC** to loopback, making a local Collector necessary to
convert to HTTP before the Cloudflare edge (ADR-0136 D2). The merged FRE-1071 change
(`alextra-lab/slm_server#14`) exports **OTLP/HTTP**, by an explicit design note — *"avoids pulling
`grpcio` into a deliberately lean dependency list"* — at a default endpoint of `http://localhost:4318`.
There is no gRPC on that host and no conversion to perform. The Mac-local Collector's remaining
justification is therefore **credential custody** (ADR-0132 D2's environment-credential principle: the
Cloudflare Access token belongs to the environment layer, not inside an application process) and
**buffering**, not protocol.

**A thinner custodian was proposed and rejected.** A loopback transport proxy — Caddy on the producer's
host attaching the Access headers, mirroring ADR-0132 D1's VPS-side mechanism — decouples the credential
just as well and never parses OTLP, so it raises no chain question at all. It was rejected on
simplicity and maintenance, and the reasoning is worth keeping: `slm_server`'s default endpoint is
already `http://localhost:4318`, which is the Collector's own default OTLP/HTTP port, so a Collector hop
needs **no producer change whatsoever**; it is the same software already running on the VPS, so it is
one component to maintain rather than two; and the host in question is a laptop running local models,
where a second component type earns nothing. The owner ruled on that basis.

**What the ruling gives up, and why it is accepted.** AC-7's method is deliberately paranoid — prove
non-bypass by reading a **live** config from the running process, because a producer on an uninspectable
host can otherwise claim anything. A vanilla Collector cannot meet it: verified against the upstream
zpages extension, which exposes component *names* and not exporter endpoints, and against the
`configz/effective` endpoint, which is a Splunk-distribution feature this ADR's own distro choice
excludes. Rather than reword AC-7 to accept an unverifiable declaration — the exact criterion-inflation
this ADR's four verification rules exist to prevent — **reachability** is established from the data: the
producer's spans reaching Tempo in volume reconciled against `api_costs`, the ledger that does not
traverse the path under test. A hop shipping *nothing* to the terminus fails that cleanly, which is what
makes the clause discriminating rather than decorative.

**It is reachability and not termination, and the first draft of this entry overclaimed by saying
otherwise.** A hop splitting its output — most to the VPS Collector, some elsewhere — reconciles within
tolerance and passes. So the residual exposure is stated in AC-7 as three named limits rather than one:
the artifact is self-report and only as good as its completeness; (iii) proves reachability, not
exclusivity; and a second destination configured on the intermediate hop is invisible to every clause
here. Adversarial review (Codex, round 1) is what forced that correction, and the weaker claim is the
true one.

**An adjacent precedent was weighed and is what made the trade acceptable.** D6 already accepts
file-provisioned Grafana dashboards over UI assembly precisely because a reviewed file *"makes it
reviewable in a diff instead of verifiable only by logging in"* — the same substitution, for a component
with strictly broader reach than a forwarding hop.

**Buffering is bounded and said so in D5.** The Collector's persistent sending queue requires contrib's
`file_storage` extension, so the hop buffers in memory only — and the coverage that buys is narrower than
process survival: `retry_on_failure.max_elapsed_time` defaults to 300s, after which the batch is dropped,
so a sleep outlasting the configured retry window loses spans whether or not the process lived.

**Consequences for the chain.** **FRE-1071 AC-5** — *"the effective-configuration artifact is readable
and names the Collector"* — now reads as satisfied by a same-host loopback endpoint, so the merged
evidence (`"otlp_endpoint": "http://localhost:4318"`) stands, **conditional on clause (iii)**: its spans
must reach Tempo once the ingress exists, which is AC-6(a)'s substance and FRE-1073's to adjudicate.
**FRE-1224** keeps its custody and buffering rationale and loses its protocol rationale; its body
asserts a gRPC premise that is no longer true. **No implementation ticket is filed by this amendment** —
it changes a criterion's wording and a decision's scope, and every implementation it bears on already
exists.

### 2026-08-08 — D6 amended again: the retirement is directed and sequenced; the owner declares completion
**Changed By:** `/adr` session (FRE-1213), recording the owner's ruling relayed in session.
**Reason:** The 2026-08-07 deferral was explicitly conditional — retirement would be decided once
Grafana had demonstrated the functionality this project needs. That evidence was produced and the
owner ruled on it. An Accepted ADR may not stand in contradiction to a live owner decision, in
either direction.

**The ruling, quoted rather than paraphrased**, across two messages on 2026-08-08:

> "if I can do everything in Grafana and more than what I can do in Elastic and the visualizations are better and I can access more data sources then fucking remove Kibana and replace it with Grafana but do it in a way that makes sense, is efficient. We're in no hurry. There's no stress — just make the move and be done with it."

> "Retire kibana focus on grafana. I will tell you when kibana is retired."

**Directed, not done — and the distinction is the whole point of this amendment.** The second message
reserves the declaration of completion to the owner, and Kibana is running as this is written. D6
therefore records that the retirement is *directed and sequenced*, never that Kibana *is* retired.
Writing the latter would commit, in the opposite direction, the exact error the 2026-08-07 amendment
was filed to correct: an Accepted ADR asserting a state that is not true. The retirement lands as
its own work — FRE-1214, under the FRE-1203 Grafana migration program — and nothing in the ADR-0129
chain waits on it.

**What produced the evidence the deferral asked for.** The 2026-08-07 ruling said the decision should
rest on Grafana's demonstrated superiority, and made the dashboard rebuild "more load-bearing, not
less" for that reason. It was: the Playwright render audit of all sixteen Grafana dashboards
(FRE-1207) and the Postgres-backed rebuilds (FRE-1209, FRE-1210, FRE-1211) are what the owner ruled
on. **The decision rests where the previous amendment said it should.**

**The 551 MiB figure is not resurrected, and this is stated because the temptation is structural.**
That figure was withdrawn on 2026-08-07 against a measurement — Kibana at 562.6 MiB of a 1 GiB cap
with 6.0 GiB available, so running both UIs was affordable. The conclusion it once supported has now
returned by a different route, which is exactly the circumstance in which a withdrawn argument gets
quietly reinstated. It is not. Context, Positive Consequences and this section all continue to
record it as withdrawn, and the retirement rests on demonstrated superiority alone.

**AC-10(e) is retired with its subject, and the loss is recorded rather than papered over.** (e)
asserted that the live `monitoring` host still resolved to Kibana. The owner ruled the `monitoring`
Cloudflare repoint out of this program's scope and into their own hands — *"Don't you worry about
the monitoring repoint"* — and the ingress mapping lives in the Cloudflare dashboard, not in this
repository, so there was never a diff here to make. After FRE-1214 removes the container,
`monitoring` addresses a service that no longer exists and **no criterion in this ADR will detect
it**; that state is unmonitored by design and by owner ruling. **A replacement clause was considered and
declined on scope, not on unfalsifiability** — the correction is recorded at AC-10 itself. The naive
formulation ("`monitoring` does not serve Kibana") is indeed unfalsifiable once no Kibana exists, but
a workable one is not: read the Cloudflare ingress rule and fail if it still targets `kibana:5601`.
That criterion was available and is given up deliberately, because the owner ruled the repoint into
their own hands and no ticket owns the state it would assert. **Adversarial review caught this
reasoning error before merge**, and it is corrected in place rather than quietly improved. **Seam ticket
FRE-1073 is affected only here** — it still asserts all ten criteria, AC-1 through AC-9 are
untouched, and only AC-10's walk loses one clause.

**Consequences for the chain, recorded so master does not re-derive them.** A live test
(`tests/integration/test_fre1072_tempo_grafana_acceptance.py`) currently asserts that Kibana stays
declared in `docker-compose.cloud.yml` and that its live status is `available`, citing D6's retention
as a deliberate design decision. **Both assertions now enforce a superseded ruling**, and FRE-1214
owns inverting them. This is the mechanism by which a stale decision outlives its own amendment, and
it is why the amendment and the retirement must not be separated by long.

**ADR-0090 and two other ADRs were amended alongside this one** (FRE-1213), because a census of every
ADR mentioning Kibana found three carrying live commitments to it rather than historical references:
ADR-0090's dashboard corner named `config/kibana/dashboards/` as the sole dashboard location in git,
which FRE-1214 deletes; ADR-0053 selected a Kibana panel as its visual tier; ADR-0055 committed two
Kibana panels. Each is amended in the same change. Implemented ADRs that mention Kibana as the
surface of their day are deliberately **left alone** — they are history, and rewriting history to
match a later ruling is a different and worse failure than drift.

### 2026-08-07 — D6 amended: Kibana's retirement is deferred, not cancelled
**Changed By:** `/adr` session (FRE-1193), recording the owner's ruling relayed in session.
**Reason:** D6 as written stated that Kibana is retired. The owner ruled otherwise, and an Accepted
ADR may not stand in contradiction to a live owner decision.

**The ruling, quoted rather than paraphrased**, across three messages on 2026-08-07:

> "I accept maintaining the 2 UI until Grafana has shown its superior functionality needed for this project."

> "We will probably retire Kibana - but not yet, and I accept the cost. We will revisit Kibana once Grafana is online and providing value. For now, it is an idea not yet manifested."

> "Keep Kibana, allow limited dev. We will gate at each Kibana ticket whether we want to proceed or not. It all depends on your grafana sequencing. Complete it sooner, the sooner we can make a decision about Kibana's retirement."

**Deferred, not cancelled.** The owner's own words are that retirement will *probably* happen; what
changed is that it is now a decision to be taken later, on evidence, rather than a consequence this
ADR discharges. **The two backend choices do not move** — Grafana remains the trace UI, Tempo remains
the trace store, and the reasoning for both is untouched. Other parts of D6 *do* move, and they are
enumerated below: Option 2's direction argument is restated, and the exposure, authentication and
dashboard-provisioning decisions are added along with AC-10.

**The 551 MiB premise is corrected in place.** D6 justified the retirement partly by recovering
551 MiB. Measured on the VPS 2026-08-07 at 09:40Z:

| | Measured |
|---|---|
| VPS memory | 22 GiB total, **6.0 GiB available** |
| `cloud-sim-kibana` | **562.6 MiB** against a 1 GiB cap (54.9%) |

Running both UIs is affordable, and the memory argument never carried the weight the ADR gave it.
The figure is withdrawn from Context and from Positive Consequences rather than left standing beside
its own correction.

**A second measurement, recorded because it moves an argument in the opposite direction.**
`cloud-sim-elasticsearch` measured **1.829 GiB of its 2 GiB cap — 91.5%** — the same reading as the
2026-07-30 Context, and **above** the 85.8% the 2026-07-31 update recorded after FRE-1036's first
five families migrated. The capacity leg of Option 2's rejection, which that update reported as
"materially weaker", has **recovered**. This is recorded unprompted because an update that logged
only the weakening would leave the file misleading in the other direction.

**Option 2's direction leg had to be restated.** It read that Kibana "is being retired… a surface
with a scheduled end." After this ruling there is no scheduled end, so that wording became false.
What carries the rejection is that **Grafana is the owner's chosen trace UI** — a call the deferral
does not touch. D6 and Option 2 are both reworded; the rejection itself stands.

**Grafana's exposure was decided in the same session** and is recorded in D6: its own
Cloudflare-fronted host `observe` (placeholder only — no literal domain in any tracked file),
following Kibana's tunnel-to-container topology rather than Caddy's, with Cloudflare Access as the
gate, anonymous `Viewer` access with the login **form** hidden (basic auth retained for the admin
API), router logging enabled, and dashboards provisioned from files in this repository. **AC-10 was
added** to assert that outcome, and `monitoring` stays pointed at Kibana — repointing it belongs to
the eventual retirement decision, not to this sequence.

**The sole-gate posture's residual exposure is named in D6 rather than assumed away.** An initial
framing — that behind Access a second password protects nothing — did not survive review and is
recorded as rejected: a stolen or over-broad Access session defeats only the edge, and anonymous
`Viewer` in Grafana OSS can query *every* datasource in the org, including the Elasticsearch logs
corpus this ADR's Context measures as holding verbatim user turns. What justifies the posture is
narrower and checkable: **Kibana already has exactly these properties behind exactly this gate**, so
it is not a regression. D6 records the trigger to revisit — Access fronting anyone but the owner.

**Consequences for the chain.** FRE-1072's AC-5 is re-scoped from "Kibana is gone" to two separable
checks — Grafana online, Kibana deliberately retained — and gains a criterion for Grafana's health
on the compose network; its title drops "and the retirement of Kibana". **AC-6 is untouched and is
now more load-bearing, not less**: dashboard equivalence is the evidence the owner's eventual
decision will rest on. AC-9's ADR-0134 rule-port obligation is untouched and still reads correctly
under either FRE-1187 outcome. **This amendment changes none of AC-1 through AC-9**, so seam ticket
FRE-1073's existing scope is unaffected — it gains only AC-10.

**Also corrected here:** the seam-owner paragraph predated ADR-0130 and assigned the assembled
criteria to the authoring `/adr` session with master's gate asserting them. Under ADR-0130 D2 and
FRE-1080 that is seam ticket **FRE-1073**, adjudicated by an `adr` session. Corrected in place.

### 2026-07-31 — Accepted
**Changed By:** master, transcribing the owner's decision relayed in session.
**Reason:** The owner accepted this ADR's direction — traces move to OpenTelemetry — and confirmed
its scope explicitly: **logs stay in Elasticsearch**, per D5 and D6 as written. There is no log
migration in this decision. ADR-0128 is marked **Superseded by this ADR**, which its own supersession
table already effects clause by clause.

**Two facts recorded at acceptance, because this ADR's Context is now partly stale and an accepted
record should not cite figures that have moved.** Measured 2026-07-31 at 15:22Z, after FRE-1036's
consolidation deployed and its first five families migrated:

- The Context cites **602 active shards for 719 MB** and Elasticsearch at **1.839 GiB of a 2 GiB cap
  (92%)** as a live operational risk. Both have moved: **363 active shards, 714 MB, 1.716 GiB (85.8%)**.
  Index count fell 556 → 306. Three families remain unmigrated, so the figure will fall further.
- **This bears on Option 2's rejection, not on the decision.** Option 2 — an EDOT Collector into the
  existing Elasticsearch — was rejected "on capacity and direction, not capability… It would be the
  right answer if Elasticsearch were healthy; it is measurably not." The capacity half of that
  rationale is materially weaker than when it was written. The direction half — Grafana as the
  intended UI, Kibana retired — is unchanged and is the owner's, and it is what continues to carry
  the rejection.

The owner also noted that Elasticsearch supports OpenTelemetry natively, so "traces to OTel" and
"logs stay in Elasticsearch" are not in tension: the constraint here is which backend stores traces
and which UI correlates them, not whether the wire format is OTLP.

**Neither fact is recorded as a reopening.** They are recorded so a future reader is not misled by
the Context's numbers, and so the Option 2 comparison is re-derivable from what was true rather than
from what was measured on 2026-07-30.

### 2026-07-30 — Proposed
**Changed By:** `/adr` session (FRE-1043)
**Reason:** Owner-directed, arising from a design session that began as ADR-0128's rename-table deliverable and established by live measurement that the naming divergence is a symptom: `TraceContext` is accepted by 19 hand-threaded function signatures, which is why `trace_id` sits at 11.36%; elapsed time carries two disjoint concurrent names across the whole corpus; and the step-to-tool span edge is emitted without a `span_id` and therefore lost. The owner set the target as trace visibility, which no naming convention can reach, and ruled that historical data is not to be made to fit.
