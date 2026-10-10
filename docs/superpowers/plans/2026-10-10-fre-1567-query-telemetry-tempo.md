# FRE-1567 — query_telemetry: a guarded Tempo read for model-call timing, and the fields before the first query

**Ticket:** FRE-1567 (Tier-1:Opus, security-sensitive, Codex plan review required).
**Design intent:** ADR-0129 (timing lives on OTel spans in Tempo), ADR-0150 D2 (the `general`
worker reads telemetry), FRE-1564 (the tool and its guard rules: the model never writes the
request, allow and never deny).
**Follows:** FRE-1564 (`src/personal_agent/tools/telemetry_query.py`). **Blocks:** FRE-1568.

## 1. Facts measured before the plan (2026-10-10, about 14:55 UTC)

### 1.1 The conversation-text check on Tempo span attributes (scope item 2)

Source: `GET /api/v2/search/tags?scope=span|resource|intrinsic` on live Tempo, from the gateway
container, and value samples from `/api/v2/search/tag/<tag>/values` over 24 h.

| Attribute | What it holds | Decision |
|-----------|---------------|----------|
| `gen_ai.operation.name` | role: `primary`, `sub_agent`, `span_extraction`, `entity_extraction`, `entailment`, `chat` (slm-server side) | **allow** |
| `gen_ai.request.model` | model id, e.g. `unsloth/gemma-4-26B-A4B-it` | **allow** |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | integers | **allow** |
| `slm.prefill_ms`, `slm.decode_ms` | integers, slm-server `chat <model>` spans | **allow** |
| `slm.ttfb_ms` and the other `slm.*` integers | integers, not needed for the ticket | **exclude** (not selected) |
| span duration (`durationNanos`), `span:status` | intrinsics | **allow** |
| `statusMessage` (intrinsic) | **exception text.** Live value: `BadRequestError(... "Preview of field's value: '24h'" ...)`. An exception string can quote a document value or user input | **exclude** |
| `url.full` | full URLs with document ids; a URL can carry a query string | **exclude** |
| `http.target`, span names of HTTP spans | request paths with session ids | **exclude** |
| `db.elasticsearch.path_parts.*`, `db.operation` | document ids and index names | **exclude** |
| `slm.session_id`, `slm.client.trace_id` | identifiers that join to a conversation | **exclude** |
| `event:name`, `link:*`, other intrinsics | not needed | **exclude** |
| every other span, resource and intrinsic tag | not needed | **exclude** |

No allowed attribute holds text from a conversation. Every value is an enum-like label or an
integer. The model cannot name an attribute at all: it picks a span kind, a group key and two
filters from fixed lists, and the tool writes the TraceQL. The tool also builds its result from
the allowed attributes only, so an extra attribute in Tempo's reply cannot reach the model.

### 1.2 Elasticsearch fields with no data (scope item 3)

`_count` with `exists` per allowlisted field, last 30 days:

* `agent-logs`: `duration_ms` 0, `latency_ms` 0, `response_time_ms` 0. No `src/` code writes
  `response_time_ms` (only the allowlist names it), so it is a fake field. All three leave the
  allowlist. The 30-day window cap means data before 2026-08-12 can never be read, so "mark as
  history" has no value.
* `agent-topology`: `latency_total_ms` 0 — FRE-1568 decides restore or remove. This PR keeps it
  in the allowlist and the description says "empty since 2026-08-08 (FRE-1568)". FRE-1568's
  scope already says that a removal also removes it from this allowlist.
* Other zero-count fields (`agent-topology` `planner_*`, some `agent-monitors-slm-health`
  fields) still have writers in `src/`. They stay. The handoff reports the counts.

### 1.3 Tempo search behaviour (live)

* `GET /api/search?q=<TraceQL>&start=<s>&end=<s>&limit=<traces>&spss=<spans per span set>`.
  A reply holds `traces[].spanSets[].spans[]` with `durationNanos`, the selected attributes, and
  `matched` (spans that matched in that span set).
* `limit` caps traces, not spans. A capped result is visible two ways: the number of traces
  equals `limit`, or a span set has `matched` greater than its returned spans.
* Retention is 14 days (`block_retention: 336h`), but **search refuses a window over 168 h**
  (live: `range specified by start and end exceeds 168h0m0s`). So the `latency` window cap is
  7 days.
* Live sizes, `model_call` spans, `limit=2000&spss=100`: 6 h 22 traces / 195 spans, 12 h 48 /
  397, 24 h 139 / 1,000, 48 h 145 / 1,044. A 24 h search took 0.55 s and 773 KB with three
  selected attributes. When the trace limit stops a search early, `metrics.completedJobs` is
  below `metrics.totalJobs`.
* `model_call <model>` spans (service `seshat-vps`) carry the role. slm-server `chat <model>`
  spans carry `slm.prefill_ms` and `slm.decode_ms`, with `gen_ai.operation.name = chat`.

### 1.4 Planner and cost baseline (`main`, `baa281af`)

* The planner system prompt lists worker tool **names** only
  (`expansion_controller._build_planner_system_prompt`). A tool description change does not
  reach the planner. SHA-256 prefix on `main`: `2b337f17d9649e54`.
* `query_telemetry` definition as a worker receives it: 2,135 chars, **533 cl100k tokens**. All
  six `general` tool definitions: 1,428 tokens.

## 2. Decisions

1. **Fields before the first query: in the tool description** (not a `fields` action). A
   `fields` action costs one tool round and needs the model to know to call it. The rendered
   lists are generated from `ALLOWED_FIELDS` and `TEMPO_GROUP_KEYS`, so they cannot drift. The
   cost is measured for AC-3.
2. **Tempo read: a new action `latency`** on `query_telemetry`. New parameters: `span`
   (`model_call` default, or `slm_chat`), `group_by` (list from `role`, `model`), `role`,
   `model` (exact-match filters) and `limit` (traces). `index` becomes optional in the schema,
   because `latency` does not use it. `search` and `count` still refuse a missing index.
3. **The tool computes the statistics.** Per group: `count`, `errors`, `p50_ms`, `p90_ms`,
   `max_ms`, `input_tokens`, `output_tokens`; for `slm_chat` also `prefill_p50_ms`,
   `prefill_p90_ms`, `decode_p50_ms`, `decode_p90_ms`. Nearest-rank percentiles. The model gets
   a small table, never raw spans. TraceQL metrics (`quantile_over_time`) are not used: they use
   coarse power-of-two buckets.
4. **Bounds:** `since` is required for `latency` (missing is refused), from `1m` to `7d`.
   `limit` default 200, more than 500 refused (not clamped). `spss` fixed at 100. Client
   timeout 15 s. At most 50 groups. The reply states `trace_limit`, `traces_returned`,
   `spans_used`, `complete`, and when incomplete, why.
5. **Endpoint:** `GET {tempo_url}/api/search` only, built by the tool. New setting
   `tempo_url` (`AGENT_TEMPO_URL`, default `http://localhost:3200`, the same pattern as
   `elasticsearch_url`). `docker-compose.cloud.yml` sets `http://tempo:3200` for the gateway.
   The guarded HTTP client is used, as for Elasticsearch.
6. **Filter values** match `[A-Za-z0-9._/:-]{1,100}`. No quote or backslash can enter the
   TraceQL string.
7. **Assignment unchanged:** the tool stays `worker_only` and only `general` holds it.

### 2.1 Changes from the Codex plan review (2026-10-10)

Codex found no route escape and no TraceQL injection. Five findings, all accepted:

1. **Completeness is conservative.** `complete` is true only if all of these hold, else it is
   false and `incomplete_reasons` lists each failed check:
   * the number of traces is below `limit`;
   * every span set has an integer `matched` equal to its returned spans;
   * `metrics.completedJobs` equals `metrics.totalJobs` (when Tempo gives them);
   * no group, span or byte cap discarded data.
2. **Processing is bounded.** The reply is streamed and refused over 16 MB (a tool error that
   says to narrow the window or lower `limit`). At most 50,000 spans are read. At most 50
   groups are kept; more sets `complete: false`.
3. **The output contract is structural.** The result is built from scratch from fixed keys.
   A group label (role or model value from Tempo) must match `[A-Za-z0-9._/:-]{1,100}`, else it
   becomes `"<unrecognised>"`. Tests put `statusMessage`, `url.full`, `http.target`,
   `slm.session_id`, a span name and an over-long or text-like role value into a canned reply,
   in span attributes, trace fields and span names, and assert none reaches the output.
4. **Percentiles are sample statistics.** The result says `percentiles_over: "spans_used"`.
   When `complete` is false the result also says `sample: true`, and the description says the
   numbers cover the returned spans only.
5. **AC-2 wording.** The ES guard is tightened, not only unchanged. The AC-2 evidence is the
   unchanged refusal tests plus the five reason-matching twin tests.

## 3. Steps

Tests first in each step; run each new test and see it fail before the code.

1. **Settings + compose.** `src/personal_agent/config/settings.py`: add `tempo_url` beside
   `elasticsearch_url`. `docker-compose.cloud.yml`: `AGENT_TEMPO_URL: http://tempo:3200`.
   `.env.example`: a commented line.
2. **ES allowlist.** Remove `duration_ms`, `latency_ms`, `response_time_ms` from
   `ALLOWED_FIELDS["agent-logs"]`.
   * The FRE-1564 pass-path test `test_a_realistic_error_query_and_a_latency_aggregation_pass`
     names `latency_ms` and `duration_ms` in allowed positions. It changes to `elapsed_ms` and
     `input_tokens`. This is the one change in `test_telemetry_query.py` besides the parameter
     set in `test_definition_is_a_low_risk_read_only_tool`. No refusal test changes (AC-2).
   * Five FRE-1564 refusal tests use `latency_ms` as the field of a refused aggregation or
     range. After the removal they still pass, but on the field check and not on the rule they
     name: `avg` with sub-aggregations, 20 percents, more than ten aggregations, a NaN in a
     range, a NaN in percents. New twin tests in the new test file repeat each with a live
     field (`input_tokens`) and match the refusal **reason**, so each rule keeps a test that
     proves it.
3. **Description (AC-1).** Rebuild `query_telemetry_tool.description` from a function that
   renders: what the tool reads, one line per family `"<family> fields: a, b, ..."`, the Tempo
   line `"latency group_by: role, model; span kinds: model_call, slm_chat"`, and the retired
   note (`duration_ms`, `latency_ms`, `response_time_ms` are not in agent-logs; model-call
   timing is the `latency` action; `latency_total_ms` empty since 2026-08-08, FRE-1568).
4. **Tempo action.** In `telemetry_query.py`: `TEMPO_SPANS` (span kind -> TraceQL selector and
   the selected attributes), `TEMPO_GROUP_KEYS` (`role` -> `gen_ai.operation.name`, `model` ->
   `gen_ai.request.model`), `_build_tempo_request(...) -> dict[str, str]` (params only; it
   raises `refused:` on any bad argument), `_summarise_spans(...)`, and the executor branch
   for `action == "latency"`. Logs: `query_telemetry_tempo_started` / `_refused` / failure
   events with `trace_id` and `session_id`.
5. **Governance text.** `config/governance/tools.yaml` `tools.query_telemetry.reason`: add the
   owner's 2026-10-10 widening and the Tempo attribute allowlist.
6. **Planner unchanged (scope item 4).** A test builds the planner system prompt, replaces the
   tool description, builds it again and asserts byte equality. The handoff gives the SHA on
   `main` and on the branch.
7. **Cost (AC-3).** Re-run the scratch measurement script; report the token delta.

## 4. Tests — `tests/personal_agent/tools/test_telemetry_query_tempo.py` (new)

All use `httpx.MockTransport`; nothing reaches Tempo.

* AC-1: the description's family lines equal `ALLOWED_FIELDS` exactly (set equality per family);
  the Tempo line equals `TEMPO_GROUP_KEYS` and `TEMPO_SPANS`; no `_CONTENT_FIELDS` name and no
  excluded Tempo attribute (`statusMessage`, `url.full`, `http.target`, `slm.session_id`,
  `slm.client.trace_id`) appears in the description.
* AC-4 works: `latency` with `since="24h"` sends one `GET /api/search`; `q` is the expected
  TraceQL; `start`/`end` span 24 h; `limit` and `spss` are set. A filter `role="primary"`
  appears in `q`.
* AC-4 refused, each with `calls == 0`: missing `since`; `since` over 7 d; `limit` 501, 0,
  -1, a string, NaN; `group_by` with `statusMessage`, `url.full`, `http.target`,
  `slm.session_id` (seeded negative: attributes that carry text or a conversation id);
  `group_by` not a list; `span` set to `../api/traces/x`, `traces`, `http`; filter values with
  `"`, `\`, `}`, `||`, over 100 chars.
* AC-4 endpoint: across every accepted call, the only path sent is `/api/search` and the only
  method is GET.
* Result shape: a canned Tempo reply with two roles and a span that carries an extra
  `url.full` attribute -> two groups with correct p50/p90/count/errors/tokens, and no
  `url.full` value anywhere in the output (`json.dumps`).
* Completeness: traces returned == limit -> `complete: false`; `matched` > returned spans ->
  `complete: false`; otherwise `complete: true`.
* Failure mapping: connect error, timeout, HTTP 400 with a long body -> short `ToolExecutionError`.
* The five twin refusal tests from step 2.
* Planner byte-identity (step 6), in `tests/personal_agent/orchestrator/` beside the FRE-1564
  split test, or in this file.

## 5. Acceptance criteria — how each is shown

| AC | Evidence |
|----|----------|
| AC-1 fields known before the first query | description set-equality tests (section 4) |
| AC-2 ES guard unchanged | `git diff origin/main -- tests/personal_agent/tools/test_telemetry_query.py` shows only the definition param set and the pass-path test field names; the whole file passes |
| AC-3 cost stated | before/after cl100k tokens of the definition, in the PR |
| AC-4 Tempo read bounded | the refusal tests with `calls == 0`, the seeded negative, the endpoint test |
| AC-5 live | master runs it after deploy (the same question as trace `2dfb04e8…`) |

## 6. Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files`. Then `feature-dev:code-reviewer` and `security-review` on
`git diff origin/main...HEAD`.

**Diff class: escalated** — a new data source for a worker is security-sensitive (ticket note).
Flag for the owner's `/code-review ultra` before merge.

## 7. Deploy notes (master)

`seshat-gateway` rebuild plus the new `AGENT_TEMPO_URL` env line (the container must be
recreated, not only restarted). No schema change. Verify: from the gateway container,
`curl -s http://tempo:3200/status` returns 200; then AC-5.
