# FRE-1080 — Re-scope the ADR-0129 chain under ADR-0130's two-tier rule

**Ticket:** FRE-1080 (Approved, `stream:build1`, Tier-1:Opus)
**Realizes:** ADR-0130 D8, applying D1 (severed inheritance), D2 (one seam per ADR), D6 (decidability at dispatch)
**Backing design:** ADR-0129 (OpenTelemetry instrumentation) — read for design intent only; **not edited**
**Review:** codex adversarial plan-review, round 1 — 14 findings, all accepted and applied (§9)

This ticket produces no `src/` change. Its deliverable is eight Linear ticket bodies plus one mapping
comment; this document is the plan of record and the reviewable artifact. It also carries master's
one-line fold-in to ADR-0130's risks table (§8).

---

## 1 — The boundary conditions, stated first

| Constraint | Source | How it is honoured |
|---|---|---|
| ADR-0129's nine criteria are **not** rewritten | ticket § out of scope; ADR-0130 D8 | the ADR file is not opened for edit; the branch diff must show it unchanged. *Restating the nine on the seam ticket is not editing the ADR — D2 requires exactly that* |
| The chain is **eight** tickets, B1–B8, gapless | ticket § correction 2026-07-31 | membership verified from Linear, not from MASTER_PLAN prose |
| FRE-1066 and FRE-1068 are **not** chain members | ticket § correction | neither is edited |
| Exactly **one** seam | ADR-0130 D2 | FRE-1073 only; no other child's body designates a seam |
| No ticket outside B1–B8 is modified | ticket AC-5 | FRE-1043 receives a **comment**, not a body edit; FRE-1080 receives its own state change and handoff |

**Chain membership, verified from Linear on 2026-07-31** (`get_issue` on each; project *Observability
Foundation*; all `Needs Approval`):

| | Ticket | Title (abbrev.) | Realizes |
|---|---|---|---|
| B1 | FRE-1064 | OTel SDK bootstrap, request-boundary root span, structlog processor | D4 |
| B2 | FRE-1065 | Bridge `TraceContext` to OTel context | D1 |
| B3 | FRE-1067 | The span tree; `gen_ai` semconv; retire `RequestTimer` | D2, D3 |
| B4 | FRE-1069 | Root spans on every background entrypoint | D3 (second half) |
| B5 | FRE-1070 | The OTel Collector as trace egress, redaction, effective-config | D5 |
| B6 | FRE-1071 | `slm_server` exports OTLP (separate repository) | D5, D8 |
| B7 | FRE-1072 | Tempo, Grafana, retirement of Kibana | D6 |
| B8 | FRE-1073 | The acceptance verifier — **the seam** | Verification section |

Eight. No B-number skipped, none duplicated.

## 2 — The test every rewritten criterion must pass

ADR-0130 D6, stricter than "is it about its own work":

> decidable from that ticket's own deliverable **when the ticket is finished**

A criterion fails if it needs a time window, a production census, an owner action, or another ticket's
output. Citing an ADR-0129 AC number fails; so does an unlabelled paraphrase. D4's no-BS bar still
applies at the new scope: *"the record carries the id"*, never *"the processor is registered"*.

Two mechanisms make most of these decidable without a production window:

- **The in-memory span exporter** — lets a test assert span identity, parentage and attributes against
  the same run's log records, with no backend and no window.
- **Fixture injection** — where a criterion needs a deployed component (Tempo, Grafana), it is decided by
  injecting a fixture directly into that component rather than waiting for an upstream child to supply
  real traffic. This is what keeps FRE-1072 decidable without FRE-1070 having landed (codex #9).

## 2a — The three obligations that belong to master, not to a ticket

Codex #1, #2 and #6 each found an obligation that no ticket can discharge. Rather than parking them on a
child that cannot prove them or a seam frozen to evaluating, they are assigned to **master** explicitly.
An obligation owned by master is owned; an obligation owned by a ticket that cannot discharge it is not.

- **The falsification checkpoint (D4.d).** ADR-0129 D4 requires the first deliverable to move the identity
  figure *before any container is committed*. The seam's due date is 14 days after the **last** child
  deploys, so a seam-only measurement learns the answer after everything is built — the falsification
  property destroyed. **Master runs the seam's deciding query (§3 B8) once, after FRE-1064 deploys**, and
  decides whether the chain proceeds to FRE-1070. This is a sequencing gate, which is master's, and it is
  exactly what FRE-1080 means by the instruction surviving "as guidance in the chain's sequencing."
- **Retiring the tool log records (D3.d).** FRE-1067 retains them; the seam's tool-span verdict gates
  their retirement; the deletion itself is post-adjudication work a seam frozen to evaluating may not do.
  **Master files the retirement ticket on a green verdict**, per ADR-0130 D2's remediation pattern.
- **The metrics boundary (D7.a).** D7 states what the ADR does *not* do. There is no chain deliverable and
  no criterion can prove a negative across future children; **master's design-adherence check at each
  merge** (ADR-0130 D3) is what catches a child that adds a metrics pipeline.

---

## 3 — Rewritten acceptance criteria, per child

Each block replaces that ticket's `## Acceptance criteria slice` section. Scope, rationale and Refs are
preserved; the "Owns ADR-0129 AC-n" inheritance lines are deleted wherever they appear.

### B1 — FRE-1064 (bootstrap + request root span + structlog processor)

Removed: *"Owns the first half of ADR-0129 AC-3(a) — the identity share"* and the entire 7-complete-day
11.36% measurement, which moves to FRE-1073 (§4).

1. **A served request opens exactly one root span, and it is a root.** *Proven by:* an integration test
   issuing one request through the FastAPI app with an in-memory span exporter; exactly one exported span
   from that request has no parent, and every other span from it descends from that span. *Fails if* the
   request produces no root, more than one root, or a span whose parent is absent from the trace.
   *(Closes codex #4/D3.e — the old wording called a span "the root" without testing rootness.)*
2. **The request's log records carry that request's span identity.** *Proven by:* for every log record
   emitted during that request, `trace_id` equals the root span's trace id and `span_id` equals a span id
   belonging to that same trace. *Fails if* the field is present but does not match the span it was
   emitted under, or is absent from a record emitted inside the span.
3. **`trace_id` and `session_id` arrive together or not at all.** *Proven by:* the set of captured records
   carrying `trace_id` is exactly the set carrying `session_id`. *Fails if* any record carries one and not
   the other — the partially-populated context this criterion exists to catch.
4. **No emit site was changed to achieve this.** *Proven by:* the branch diff touches only
   `pyproject.toml`, the SDK bootstrap module, the request-boundary middleware and `telemetry/logger.py`;
   no existing `log.*` call gains an identity argument. *Fails if* any emit site was edited — that would
   mean identity is still supplied by hand.
5. **A record emitted with no active span carries no invented identity.** *Proven by:* a unit test
   emitting a record outside any span, asserting `trace_id` and `span_id` are **absent** — not a zero id,
   not a sentinel. *Fails if* a placeholder appears. (ADR-0129 D8 drops sentinels; this keeps them
   dropped — mapping row D8.g.)
6. **The processor introduces no second record timestamp.** *Proven by:* records emitted through the
   processor carry `@timestamp` and no additional timestamp field or alias. *Fails if* a new timestamp
   spelling appears. *(Closes codex #4/D8.a.)*
7. **This ticket wires no export path.** *Proven by:* a startup assertion that a tracer provider is
   installed and its configured exporter set contains no OTLP or network exporter. *Fails if* an export
   destination is configured here — that is FRE-1070's.

**Retained as sequencing guidance, not a criterion** (ADR-0130 D8 requires the falsification intent to
survive): FRE-1064 is the chain's falsification gate — the smallest change that should move the identity
share. **Master runs FRE-1073's deciding query after this deploys and decides whether the chain
proceeds.** If the figure does not move, that falsifies the decision, not this ticket.

### B2 — FRE-1065 (`TraceContext` bridge)

Removed: *"Owns ADR-0129 AC-9 in full"*. The five behavioural assertions **stay** — ADR-0130 D1 names this
exact case: preserving those fields *is* this ticket's own work; what it may not do is cite AC-9 as the
thing it discharges. Codex confirmed this reasoning holds and found no smuggled inheritance here.

1. **`authenticated` still gates group-visibility memory.** *Proven by:* a request pair — an authenticated
   recall returns `group`-visibility memory, the identical request unauthenticated does not. *Fails if*
   either half changes from pre-bridge behaviour; the unauthenticated half widening is a data-access
   regression, not merely a test failure.
2. **`user_id` still scopes rows.** *Proven by:* a request as user A returns no row scoped to user B.
3. **`session_id` still isolates history.** *Proven by:* two concurrent sessions, neither returning the
   other's session-scoped history.
4. **`eval_mode` still isolates substrate.** *Proven by:* an evaluation-mode request writes nothing to
   production Neo4j / Elasticsearch / Postgres (the FRE-375 guard passes unchanged).
5. **`kind` still separates scheduled from organic.** *Proven by:* a scheduler-originated context and a
   user-originated one are distinguishable by `kind` in the emitted record.
6. **`TraceContext` reads identity rather than minting it.** *Proven by:* with a span active,
   `TraceContext.trace_id` equals that span's trace id and `span_id` equals that span's. *Fails if* they
   disagree — a bridge minting its own id alongside the span's reproduces the divergence it ends.
7. **This is a bridge, not a flag day.** *Proven by:* an `ast-grep` census of `src/` for signatures
   accepting `trace_ctx: TraceContext` returns the same set as `origin/main` (19 signatures, censused
   2026-07-30). *Fails if* any signature was removed — removal is a separate decision.

### B3 — FRE-1067 (the span tree)

Removed: *"Owns ADR-0129 AC-1, AC-5, and AC-4(a)"* and the population phrasing of each. All below are
decided by one exercised tool-using turn under an in-memory exporter, plus static checks on the diff.

1. **A tool span's parent is the step span, never the model-call span.** *Proven by:* an integration test
   running one turn making ≥2 tool calls; every tool span's parent id equals the step span's. *Fails if*
   any tool span is parented to a model-call span — that encodes causality as containment, the trap
   ADR-0129 D3 states explicitly.
2. **The shape is root → step → {model-call, tool-call}.** *Proven by:* the root has no parent, the step's
   parent is the root, and model-call and tool-call spans are **siblings** beneath the step. *Fails if*
   any span lacks its expected parent, or the turn yields a single flat span.
3. **The tool-span count matches the retained log records for the same turn.** *Proven by:* the number of
   tool spans equals the number of `tool_call_completed` plus `tool_call_failed` records in that turn.
4. **No child span outlives its parent.** *Proven by:* start/end compared on every parent-child pair.
5. **The step-level event collapsed into the step span.** *Proven by:* the exercised turn emits **no**
   `tool_execution_completed` record, and the step span carries the tool count that record used to
   report; the per-tool `tool_call_*` records still emit. *Fails if* the step-level record survives
   alongside the span — that is the duplication D3 collapses. *(Closes codex #4/D3.c.)*
6. **Model-call span attributes equal the call that was made.** *Proven by:* `gen_ai.request.model` equals
   the model the client was invoked with; `gen_ai.usage.input_tokens` / `output_tokens` equal the usage
   the client reported; `gen_ai.system` identifies the provider actually called. *Fails if* an attribute
   is present but does not equal its source value — presence alone is the bar D4 rejects.
7. **`gen_ai.operation.name` takes its value from the project's `purpose` vocabulary.** *Proven by:* the
   attribute's value on the exercised turn is a member of the role/purpose enum FRE-1037 established
   (a shipped, durable vocabulary — not a pending ticket's output). *Fails if* it carries a free-form
   string. *(Closes codex #4/D8.c.)*
8. **Attributes with no semconv name carry the project namespace.** *Proven by:* every span attribute on
   the exercised turn is either a semconv name or begins with the declared project prefix. *Fails if* a
   bare un-namespaced custom key appears. *(Closes codex #4/D2.b.)*
9. **The converted emit sites stop writing both elapsed-time fields.** *Proven by:* records emitted from
   `orchestrator/executor.py:5373`, `tools/executor.py:462,481` and the model-call path during the
   exercised turn carry neither `duration_ms` nor `latency_ms`. (The corpus-wide form is the seam's.)
10. **`RequestTimer` has no callers left.** *Proven by:* an `ast-grep` census for `start_span`/`end_span`
    call sites returns zero, and the methods are removed rather than deprecated.
11. **The tool log records still emit.** *Proven by:* the exercised turn emits `tool_call_started` and
    `tool_call_completed`/`_failed` as before — criterion 3 depends on them, and ADR-0129 D3 retains them
    until the seam's tool-span verdict.
12. **No field registry is introduced.** *Proven by:* the diff adds no generated field registry, no
    per-field type declaration file and no template-generation step — semconv is the vocabulary.
    *(Closes codex #4/D2.c.)*
13. **The recorded semconv version is the one actually installed.** *Proven by:* a test asserting the
    version recorded in the repository equals the resolved version of the installed
    `opentelemetry-semantic-conventions` package. *Fails if* the record is an inert string that can drift
    from the dependency. *(Closes codex #11.)*

### B4 — FRE-1069 (background root spans)

Removed: *"Owns ADR-0129 AC-3(c) and the reported half of AC-2(c)"*. The reported-share half is a
production measurement and moves to the seam.

1. **Each named background entrypoint emits records carrying trace identity.** *Proven by:* for each of
   `brainstem/scheduler.py`, the consolidation runner, `observability/joinability/scheduler_runner.py`,
   `observability/slm_health/scheduler_runner.py`, `observability/cache_erosion/monitor.py` and service
   startup — a test invoking that entrypoint with an in-memory exporter asserts exactly one root span is
   exported and every log record emitted during the invocation carries that span's `trace_id`. *Fails if*
   any listed entrypoint produces a record with an absent `trace_id`; `trace_id: None` is today's defect
   and is what this ticket removes.
2. **A background root span is distinguishable from an organic one.** *Proven by:* the root span and its
   records carry `kind` in the `system:<source>` form, with the source naming that entrypoint.
3. **The pre-bootstrap population is enumerated by logger name, not by tolerance.** *Proven by:* a
   committed list naming every logger emitting before SDK bootstrap, plus a test capturing records
   emitted during import-time startup and asserting every logger name observed appears in that list.
   *Fails if* the list is absent, or the test observes a pre-bootstrap logger not on it.

**Retained as a stated limit, not a criterion:** background coverage is *observed, not proven* — these
entrypoints have no independent ledger, so a lost background trace and its lost log record are jointly
invisible. This ticket makes coverage observable; it does not prove it.

### B5 — FRE-1070 (the Collector)

Removed: *"Owns ADR-0129 AC-7"*. The cross-producer population form stays on the seam.

1. **A declared redaction rule demonstrably fires on a span that passed through the running Collector, and
   does not fire indiscriminately.** *Proven by:* emit one span carrying an attribute matching a declared
   rule **and** a second attribute matching no rule, through the Collector started from this ticket's
   compose service; read the span back from the Collector's own debug/file exporter. The matching
   attribute is absent; the non-matching one survives. *Fails if* the matching attribute survives (the
   redactor never fired), **or** the non-matching one is also stripped (a rule stripping everything is not
   redaction and yields the same clean-looking result), **or** the Collector does not start and process
   the span at all. *(Merged with the old criterion 2 per codex #10 — passing a real span through it is
   what proves the configured image is running and working, which an image reference alone cannot.)*
2. **The running Collector is the vanilla upstream distribution.** *Proven by:* inspecting the **running**
   container from criterion 1 and asserting its image is the upstream `otel/opentelemetry-collector`
   family — not Alloy, EDOT, Splunk or Datadog. Neutrality is what keeps the backend a configuration line
   rather than a commitment.
3. **Every in-repo span producer publishes a runtime-derived effective-configuration artifact naming the
   Collector.** *Proven by:* the ticket **enumerates the in-repo producers** (the gateway service and each
   background entrypoint that exports spans); for each, the artifact is generated from the process's
   *resolved* configuration at runtime — not hand-written — and its OTLP endpoint value equals the
   Collector's. *Fails if* any enumerated producer publishes no artifact, or an artifact is authored by
   hand rather than derived from the running configuration. *(Closes codex #7 — the old wording quantified
   over "producers this ticket adds an artifact for", which a forgotten producer silently escapes.)*
4. **No Collector exporter addresses anything off-box.** *Proven by:* every exporter endpoint in
   `config/otel/` resolves to a compose-internal service name or localhost.
5. **The log path is untouched.** *Proven by:* the diff contains no change to the `es_logger` path and the
   existing Elasticsearch logging tests pass unchanged. *Fails if* log egress moved — this is a trace
   chokepoint only, and reading it as a universal one is the overclaim ADR-0129 D5 forbids.

### B6 — FRE-1071 (`slm_server`, separate repository)

Removed: *"Owns ADR-0129 AC-6, and the `slm_server` half of AC-7"*, with the window-scoped span
reconciliation.

1. **An incoming trace context is continued rather than replaced.** *Proven by:* a test issuing a request
   carrying a known W3C `traceparent`; the exported span's trace id equals the one supplied. *Fails if*
   the server mints a new trace — only genuine continuation puts its spans inside the caller's turn.
2. **All four emit paths export a span carrying a distinct emit-path attribute.** *Proven by:* one test
   per path — chat, responses, rerank, streaming (`router.py:528, 696, 971, 1175`) — each asserting a span
   with that path's attribute value. *Fails if* any path exports no span, or two paths share a value;
   `api_costs` has no emit-path column, so this attribute is the only thing that can distinguish them.
3. **No code path writes telemetry to Elasticsearch at all.** *Proven by:* the Elasticsearch writer and
   the client-side index-URL formatting at `telemetry.py:38` are **removed**, and a test exercising all
   four paths produces zero outbound Elasticsearch requests. *Fails if* any ES write path survives —
   including one writing to an undated index, which removing only the date formatter would leave intact.
   *(Closes codex #5 — the old wording proved the formatter was gone, not that writes stopped.)*
4. **No existing `slm-requests-*` document is modified.** *Proven by:* the change performs no migration,
   reindex or backfill; existing documents keep their `ts` field untouched.
5. **The effective-configuration artifact is readable and names the Collector.** *Proven by:* fetching it
   from a locally-run instance and asserting its OTLP endpoint value is the Collector's.
6. **Telemetry export stays fail-soft.** *Proven by:* a test in which the OTLP endpoint is unreachable and
   the request still returns its normal response. *Fails if* a telemetry failure reaches the request path.
7. **Token usage rides under semconv.** *Proven by:* the model-call span's `gen_ai.usage.input_tokens` and
   `output_tokens` equal the usage the response reported.

### B7 — FRE-1072 (Tempo, Grafana, Kibana retirement)

Removed: *"Owns ADR-0129 AC-8 and AC-2(b), and delivers the surface AC-1 is read on"*, with AC-8's 14-day
active-day continuity and its per-day count reconciliation — population claims that move to the seam.

**All component-level criteria below use fixture injection**, so none depends on FRE-1070 having landed
(codex #9): a fixture span is sent directly to Tempo's OTLP receiver, and a fixture log record carrying
the same `trace_id` is written to Elasticsearch. What is under test is Tempo's configuration and Grafana's
datasource wiring — which *is* this ticket's own work.

1. **A 14-day TraceQL metrics query is accepted rather than rejected on the duration limit.** *Proven by:*
   running that query against the deployed Tempo and receiving a normal response — possibly an empty
   series — not a `max_duration` error. *Fails if* Tempo rejects it; the documented default is 24 hours,
   so this is what proves the configuration was committed.
2. **A fixture span is retrievable from Tempo by its trace id.** *Proven by:* injecting one at Tempo's
   OTLP receiver and fetching it back; the returned trace's id equals the one injected.
3. **Trace-to-logs resolves in both directions for that fixture pair.** *Proven by:* following Grafana's
   trace-to-logs link from the fixture span returns the fixture Elasticsearch record, whose `trace_id`
   equals the span's; and the reverse link from that record resolves the same trace. *Fails if* either
   direction returns empty or resolves a different id. (The per-`purpose`-stratum form is the seam's.)
4. **A single duration source answers the latency question.** *Proven by:* a Grafana panel plotting per-day
   p50 and p95 turn duration whose query reads span duration only — no union of two fields — returning a
   non-empty point for a day on which fixture spans exist. *Fails if* the panel unions two fields or
   returns nothing for a day with spans. *(Codex confirmed this clears the D4 no-BS bar: it requires a
   real query and a real result, not the panel's existence.)*
5. **Kibana is gone.** *Proven by:* `docker-compose.yml` declares no Kibana service and no Kibana container
   runs after the deploy.
6. **Every dashboard in the existing Kibana inventory has a Grafana equivalent that returns a well-formed
   result.** *Proven by:* the **FRE-533 panel inventory** — a completed, durable artifact, not this
   ticket's own list — enumerates the dashboards; each has a named Grafana equivalent whose query executes
   against its datasource without a query or datasource error. *Fails if* any inventoried dashboard has no
   equivalent, or an equivalent errors. **Emptiness is not failure** — a correctly rebuilt low-traffic
   panel legitimately returns no rows, and gating on live data would make close-out depend on production
   volume rather than on this ticket's deliverable. *(Closes codex #8, both halves.)*
7. **Elasticsearch's other consumers still work.** *Proven by:* the four consumers ADR-0129 D6 names —
   **Captain's Log, insights, ratings and the cost gate** — each exercised by its own named test or
   command, with the deciding output recorded. *Fails if* any returns an error against Elasticsearch after
   the change. *(Closes codex #14 — the previous wording substituted the joinability monitor for
   "insights" and named no deciding command.)*
8. **Nothing historical was backfilled or reindexed.** *Proven by:* the diff contains no reindex or
   backfill step.

### B8 — FRE-1073 (the verifier — **the seam**)

The body is restructured into two clearly separated parts.

**Part 1 — build work (the instrument).** `scripts/audit/adr0129_trace_verifier.py`, implementing the
ADR's four governing rules and the baseline file. Its own criteria, decidable from the script:

1. **The verifier enumerates from `api_costs` and nothing else.** *Proven by:* its population query reads
   `api_costs`; it contains no query against `session_events` and none against the telemetry index it is
   checking. *Fails if* the denominator is drawn from the signal under test.
2. **A collapsed population reports inconclusive, never pass.** *Proven by:* a unit test feeding a stratum
   whose population is zero, and one collapsed against its recorded pre-cutover size; both return
   `inconclusive`. *Fails if* either returns `pass`.
3. **A self-measured delivery baseline above 0.5% rejects the run.** *Proven by:* a unit test supplying a
   0.6% baseline; the run is rejected rather than accepted at the lenient number.
4. **An empty stratum fails unless explicitly listed inactive.** *Proven by:* a unit test with one empty
   stratum undeclared (fails) and the same stratum declared inactive (passes).

**Part 2 — the seam declaration.** FRE-1073 is ADR-0129's seam ticket and the only one. It owns **all
nine** of ADR-0129's acceptance criteria. ADR-0130 D6 requires each to be **present with a stated evidence
procedure** at dispatch, so they are carried in full on the ticket body — a pointer is not presence
(codex #3):

| AC | What it asserts | Evidence procedure | Earliest adjudicable |
|---|---|---|---|
| AC-1 | Every tool-using turn renders as a correctly shaped waterfall | Enumerate turns from `api_costs` over the window; select those whose retained tool records show ≥1 tool call; fetch each trace from Tempo; assert root → step → {model, tool} with tool spans siblings of model spans. Delivery loss counted separately against the bounded baseline | 7 days after full-chain cutover |
| AC-2 | Correlation resolves both directions; background coverage reported | (a) every `api_costs` `trace_id` resolves in Tempo; (b) per `purpose` stratum, one trace followed through Grafana's trace-to-logs returns non-empty records with equal `trace_id`; (c) background resolvable share measured and reported per entrypoint, not gated | 7 days after cutover |
| AC-3 | Identity is a runtime property; each trace belongs to one turn | (a) per `purpose`, share of post-bootstrap records with a `trace_id` resolving in Tempo, vs the 11.36% baseline; (b) all records for a `trace_id` share the one `session_id` the ledger records, and that trace id appears against no other; (c) pre-bootstrap records enumerated by logger name as an explicit excluded list | 7 days after cutover |
| AC-4 | One elapsed-time representation; spans are measurements | (a) zero post-cutover records carry `duration_ms` or `latency_ms`; (b) per provider, model-call span count reconciles against `api_costs` rows within the delivery baseline; (c) full-population join — every model-call span's duration agrees with its row's `latency_ms` within 10% | 7 days after FRE-1067 cutover |
| AC-5 | The tool span tree is correctly parented over a non-empty population | Per turn from `api_costs`, N = tool-call count from retained log records; assert exactly N tool spans, each a child of the step span, no child outliving its parent; rule-4 guard on collapsed tool-log volume | 7 days after cutover |
| AC-6 | `slm_server` exports OTLP on every active path; no dated indices | (a) total `slm_server` span count reconciles against its `api_costs` rows, every span's `trace_id` equal to the calling turn's; (b) per emit-path attribute, each of four paths shows propagation or is listed inactive; (c) no new `slm-requests-YYYY.MM.DD` index appears | 7 days after the `slm_server` release |
| AC-7 | Collector redaction fires; no producer bypasses it | (i) positive control — a planted attribute matching a rule is absent in Tempo; (ii) every producer publishes a machine-readable effective-config artifact the verifier reads, and none names an OTLP endpoint other than the Collector. A producer publishing no artifact fails | at FRE-1070 + FRE-1071 cutover |
| AC-8 | "Did responses get faster?" answerable from one query over a fortnight | A Grafana panel of per-day p50/p95 turn duration returns a non-empty series for every day `api_costs` records model calls across ≥14 days, computed without unioning two fields; each active day's turn-span count reconciles against that day's distinct `api_costs` `trace_id` count | **14 days** after cutover — the binding constraint |
| AC-9 | The `TraceContext` bridge preserved every retained field | One behavioural assertion per field — `authenticated`, `user_id`, `session_id`, `eval_mode`, `kind` — against existing suites plus a live request pair; any behaviour differing from pre-migration fails | at FRE-1065 cutover |

Adjudicated per ADR-0130 D2: one verdict per criterion with its evidence and that evidence's actual
output, written into ADR-0129's Status Updates; `Implemented` only if every verdict is green. **Scope is
frozen to evaluating** — a red or inconclusive verdict spawns a separately-scoped remediation ticket filed
by master, never a fix here. It closes on adjudication, not on success.

**Plus the mapping rows not decidable from any single child:** AC-4(a)'s corpus-wide form, the background
resolvable-share report (D3.e-bg), the per-`purpose` trace-to-logs population (D6.c), the cross-producer
no-bypass check (D5.f), and the identity-share measurement below.

**The identity-share measurement, moved here from FRE-1064 with its deciding query.**
Baseline **11.36%** — 374,685 of 3,299,635 `agent-logs-*` documents spanning 2026-04-15 → 2026-07-30,
measured 2026-07-30. Window: 7 complete days after cutover. Deciding query:

    POST agent-logs-*/_search
    {
      "size": 0,
      "query": {"bool": {"filter": [
        {"range": {"@timestamp": {"gte": "<cutover>", "lt": "<cutover>||+7d"}}}
      ]}},
      "aggs": {
        "by_purpose": {
          "terms": {"field": "purpose", "size": 100},
          "aggs": {"with_trace_id": {"filter": {"exists": {"field": "trace_id"}}}}
        }
      }
    }

Share per stratum = `with_trace_id.doc_count / doc_count`; the overall share is compared against 11.36%.
A `trace_id` that does not resolve in Tempo does not count as present. A share that has not risen
substantially falsifies the decision, not the ticket. **Master also runs this query once after FRE-1064
deploys**, as the chain's falsification checkpoint (§2a).

**Due date — provisional, and stated as such.** The earliest date all nine become adjudicable is **14
complete days after the last required child (FRE-1071, FRE-1072) is deployed** — AC-8 needs a ≥14-day
window and AC-6 needs the `slm_server` release. That date is **not knowable now**, because the chain is
not yet approved. ADR-0130 D2 requires a due date to exist as the wake-up, so one is set: **2026-10-01**,
a provisional marker carrying the derivation rule above. **Master re-dates it when the last child
deploys.** It is a marker, not an actuator. *(Closes codex #13 — the previous wording called this
"derived, not guessed" and then guessed.)*

**Parked:** no `stream:` label. Undispatchable until master activates it at an advance-dispatch pass on or
after the due date.

---

## 4 — What moves off FRE-1064, in one place

| | From | To |
|---|---|---|
| 7-complete-day identity share vs 11.36% baseline | FRE-1064 criterion | FRE-1073, with the deciding query above |
| "Owns the first half of ADR-0129 AC-3(a)" | FRE-1064 | deleted — no child cites an ADR AC |
| "Stop the chain if the number does not move" | FRE-1064 criterion | FRE-1064 guidance + **master's** post-deploy checkpoint (§2a) |

After the rewrite FRE-1064 retains **no** time-windowed criterion — ticket AC-3's second half.

## 5 — The obligation → owner mapping (published on FRE-1043)

One row per obligation ADR-0129's Decision section places on the chain. Anything not decidable from a
single child's deliverable is the seam's, by definition (ADR-0130 D1). **Every row names the criterion
that proves it** — an owner label with no criterion behind it is what codex #4 caught, and is exactly the
unfalsifiable coverage this mapping exists to prevent.

| # | Obligation (ADR-0129 Decision) | Owner | Proved by |
|---|---|---|---|
| D1.a | Adopt the OTel Python SDK; a tracer provider exists at runtime | FRE-1064 | B1-1, B1-7 |
| D1.b | `TraceContext` reads trace identity from the active span rather than minting it | FRE-1065 | B2-6 |
| D1.c | `TraceContext` retains `user_id`, `session_id`, `kind`, `eval_mode`, `authenticated`, each preserved behaviourally | FRE-1065 | B2-1…5 |
| D1.d | Existing signatures keep working — a bridge, not a flag day | FRE-1065 | B2-7 |
| D2.a | Span and attribute names come from OTel semconv where one exists (`gen_ai.*`) | FRE-1067 | B3-6 |
| D2.b | A namespaced project key is used where no semconv name exists | FRE-1067 | B3-8 |
| D2.c | No project field registry is built | FRE-1067 | B3-12 |
| D2.d | The `duration_ms` / `latency_ms` divergence is resolved structurally | FRE-1067 (converted paths) + **seam** (corpus-wide) | B3-9; AC-4(a) |
| D2.e | A named semconv version is pinned and recorded (ADR-0093 D5) | FRE-1067 | B3-13 |
| D3.a | The tree is root → step → {model-call, tool-call}, siblings beneath the step | FRE-1067 | B3-1, B3-2 |
| D3.b | `RequestTimer` is retired, not extended | FRE-1067 | B3-10 |
| D3.c | `tool_execution_completed` collapses into the step span, one child span per tool | FRE-1067 | B3-5 |
| D3.d | The tool log records are retained until the tool-span check passes, **retired only afterwards** | FRE-1067 (retention) + **seam** (verdict) + **master** (files the retirement ticket on a green verdict) | B3-11; AC-5; §2a |
| D3.e | Every entrypoint opens a root span — served | FRE-1064 | B1-1 |
| D3.e-bg | Every entrypoint opens a root span — background | FRE-1069; **seam** (reported resolvable share) | B4-1, B4-2; AC-2(c) |
| D3.f | Pre-bootstrap records enumerated by logger name, excluded by name not by tolerance | FRE-1069 | B4-3 |
| D4.a | A structlog processor injects `trace_id`, `span_id`, `session_id` on every record with no emit-site change | FRE-1064 | B1-2, B1-3, B1-4 |
| D4.b | `session_id` is included deliberately, so turn membership has a key to group by | FRE-1064 | B1-3 |
| D4.c | The first deliverable is bootstrap + root span + processor together | FRE-1064 | B1-1, B1-2, B1-7 |
| D4.d | The falsification gate — the identity figure moves, or the chain stops | **master** (runs the checkpoint after FRE-1064 deploys) + **seam** (the criterion) | §2a; AC-3(a) |
| D5.a | All trace telemetry leaves via OTLP to the Collector; no producer exports spans directly | FRE-1070 (in-repo) + FRE-1071 (`slm_server`) | B5-3, B5-4; B6-5 |
| D5.b | Span redaction is declarative and auditable at the Collector | FRE-1070 | B5-1 |
| D5.c | The vanilla upstream Collector is used, not a vendor distribution | FRE-1070 | B5-2 |
| D5.d | `slm_server` gets a network endpoint to ship to, replacing client-side index URL formatting | FRE-1071 | B6-1, B6-3 |
| D5.e | Logs continue to reach Elasticsearch directly — not a universal chokepoint | FRE-1070 | B5-5 |
| D5.f | Every producer publishes a machine-readable effective-configuration artifact | FRE-1070 (in-repo) + FRE-1071 (`slm_server`) + **seam** (no producer bypasses, across all) | B5-3; B6-5; AC-7(ii) |
| D6.a | Tempo receives spans; `query_frontend.metrics.max_duration` ≥ 14 days | FRE-1072 | B7-1, B7-2 |
| D6.b | Elasticsearch keeps the logs and its named consumers unchanged; no log migration | FRE-1072 | B7-7, B7-8 |
| D6.c | Grafana is the single UI; trace ↔ logs correlation on `trace_id` both directions | FRE-1072 (fixture pair) + **seam** (per-`purpose` stratum) | B7-3; AC-2(b) |
| D6.d | Kibana is retired and its dashboards rebuilt in Grafana | FRE-1072 | B7-5, B7-6 |
| D6.e | FRE-588 is superseded and closed | **master** — a ticket action, flagged on FRE-1072 | master's close-out |
| D7.a | Metrics are out of scope; Prometheus is not deployed; metric-shaped emissions stay as log records | **master** — a scope boundary, not a chain deliverable; caught by the design-adherence check at each merge | §2a |
| D7.b | Nothing goes off-box; no SaaS exporter is configured | FRE-1070 | B5-4 |
| D7.c | History is not migrated; no backfill and no reindex | FRE-1072 + FRE-1071 (its own store) | B7-8; B6-4 |
| D8.a | `@timestamp` remains the record timestamp; no aliases introduced | FRE-1064 | B1-6 |
| D8.b | `trace_id` and `session_id` come from propagated context, on spans and on log records | FRE-1064 | B1-2, B1-3 |
| D8.c | `gen_ai.operation.name` carries the work taxonomy, FRE-1037 supplying the vocabulary | FRE-1067 | B3-7 |
| D8.d | `slm-requests` stops being written to Elasticsearch; existing documents keep `ts` untouched | FRE-1071 | B6-3, B6-4 |
| D8.e | `slm_server` token names dissolve into `gen_ai.usage.*` | FRE-1071 | B6-7 |
| D8.f | ADR-0128 moves to Superseded when ADR-0129 is Accepted | **master** — an ADR-status action | master's ADR-status sweep |
| D8.g | Sentinels and violation tagging are dropped, not replaced — no invented identity outside a span | FRE-1064 | B1-5 |

**Coverage claim:** every D1–D8 obligation appears exactly once, owned by a child, the seam, or master,
and every row names the criterion that proves it. Rows split across owners are split at the decidability
boundary: the child's half is decidable from its own deliverable, the seam's half is the population or
cross-child form of the same obligation, and where a residue belongs to neither it is master's (§2a).

Implementation-Notes items are not separately enumerated: each falls under the Decision-section obligation
it implements, with the same owner — except `scripts/audit/adr0129_trace_verifier.py`, which is
FRE-1073's Part-1 build work.

---

## 6 — Execution steps

| # | Step | Verify |
|---|---|---|
| 1 | Re-verify chain membership from Linear (done — §1) | eight tickets, B1–B8, no gaps |
| 2 | `save_issue` FRE-1064 body — criteria replaced, measurement removed, guidance retained | re-read; no time window, no AC citation |
| 3 | `save_issue` FRE-1065, 1067, 1069, 1070, 1071, 1072 bodies | re-read each; §2 test passes on every criterion |
| 4 | `save_issue` FRE-1073 body — Part 1 + the nine carried in full + moved measurement with query | re-read; nine present with evidence procedures |
| 5 | `save_issue` FRE-1073 `dueDate: 2026-10-01`; confirm no `stream:` label | re-read labels |
| 6 | `save_comment` the mapping table on FRE-1043 | re-read; every row owned **and** proved |
| 7 | `git diff origin/main -- docs/architecture_decisions/ADR-0129-*.md` | **empty** |
| 8 | Quality gates, commit, PR, handoff comment on FRE-1080 | — |

## 7 — Acceptance criteria of this ticket, and where each is proven

| FRE-1080 AC | Proven by |
|---|---|
| Every child's criteria decidable from its own deliverable | §3 — each criterion names what is inspected; handoff quotes them |
| Mapping has an owner for every row, none unowned | §5 — 43 rows, D1–D8 complete, each with its proving criterion |
| Identity-share measurement on FRE-1073 with its deciding query | §3 B8 + §4; FRE-1064 retains no time-windowed criterion |
| FRE-1073 owns all nine and is the only seam | §3 B8 table; no other child body designates a seam |
| Chain membership stated and matches the board | §1 — eight, verified from Linear |
| ADR-0129 byte-identical | step 7 — `git diff` returns empty |

## 8 — Fold-in from master (ticket comment, 2026-07-31 08:16)

ADR-0130's risks table row *"Seam tickets accumulate unrun"* stated the activation trigger as *"master, at
the last child's merge"* — contradicting both D2's body (which rejects that trigger explicitly, because it
would park a long-horizon ticket in the `adr` stream and hold it for the length of the window) and the row
four lines below it. Corrected to *"master, at the first advance-dispatch on or after the due date"*. A
round-two remnant that survived the round-three revision. Scope of this correction is that one row: ADR-0130's
decisions and criteria, and ADR-0129 in its entirety, are untouched.

## 9 — Codex round 1 — 14 findings, all applied

| # | Finding | Resolution |
|---|---|---|
| 1 | D3.d's post-verdict retirement landed on nobody — the seam is frozen to evaluating | Master files the retirement ticket on a green verdict (§2a) |
| 2 | Falsification gate operationally empty — a seam due 14 days after the *last* child cannot gate the *first* | Master runs the deciding query after FRE-1064 deploys (§2a) |
| 3 | Seam pointed at ADR-0129's criteria instead of carrying them — fails D6's dispatch check | All nine carried in full with evidence procedures (B8 table) |
| 4 | Six mapping rows owned but unproved (D2.b, D2.c, D3.c, D3.e, D8.a, D8.c) | Criteria added: B3-8, B3-12, B3-5, B1-1, B1-6, B3-7 |
| 5 | "No dated URL formatter" ≠ "ES writes stopped" | B6-3 now requires the ES writer removed and zero outbound ES requests |
| 6 | D7.a metrics boundary unprovable by the seam | Reassigned to master's design-adherence check (§2a) |
| 7 | B5-3 self-selecting — a forgotten producer escapes the quantifier | Quantified over an enumerated producer list; artifacts runtime-derived |
| 8 | B7-6 self-selecting, and "non-empty against live data" depends on traffic | Quantified over the FRE-533 inventory; well-formed result, not non-empty |
| 9 | B7-2/3 were cross-child (needed FRE-1070's Collector) | Fixture injection direct to Tempo + a fixture ES record |
| 10 | B5-2 was pure component existence | Merged into B5-1 — the span must pass through the *running* Collector |
| 11 | B3 semconv version could be an inert string | Now asserted equal to the installed package's resolved version |
| 12 | D8 sentinel obligation had a criterion but no mapping row | Row D8.g added |
| 13 | Due date called "derived" then guessed | Stated as a provisional marker with its derivation rule; master re-dates |
| 14 | B7-7 vague, and substituted "joinability monitor" for D6's "insights" | Names D6's four consumers, each with a deciding command |

Codex found **no** smuggled inheritance, and confirmed FRE-1065's five retained-field assertions and
FRE-1072's panel criterion as correctly scoped.
