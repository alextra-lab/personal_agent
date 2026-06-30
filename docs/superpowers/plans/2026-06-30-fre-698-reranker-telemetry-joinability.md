# FRE-698 — Reranker telemetry joinability (ADR-0074)

**Backing ADR:** ADR-0074 (end-to-end traceability §I1 identity tuple, §I3 every async boundary preserves identity). Feeds FRE-655.

**Problem:** `memory/reranker.py:rerank()` takes no trace context; `reranker_applied` and
`reranker_failed` log `candidate_count`/`top_k`/`result_count`/`duration_ms` but NO
`trace_id`/`session_id`/`task_id`. A turn that fires two reranks (two `search_memory` queries)
cannot attribute either rerank to its query/recall/turn. The SLM-side `/v1/rerank` log also has
no join keys.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| 1. Rerank event carries `trace_id`, `session_id`, `task_id`; joins to turn, zero orphans | unit test asserts both events stamp identity; live ADR-0074 joinability probe post-deploy |
| 2. Two-reranks-per-turn distinguishable (own `span_id` + query/candidate metadata) | unit test: two `rerank()` calls → distinct `span_id`; each event carries its own `candidate_count` |
| 3. Event includes `model_id`, input cap, returned count, top score, latency | unit test asserts all fields present on `reranker_applied` |

## Steps

### Step 1 — `rerank()` signature + enriched events (`src/personal_agent/memory/reranker.py`)
- Add `import uuid`.
- Add keyword-only params to `rerank()`: `trace_id: str | None = None`, `session_id: str | None = None`, `task_id: str | None = None`. (Backward-compatible — existing callers/tests unaffected.)
- Generate `span_id = str(uuid.uuid4())` at the top of the function (shared by success + failure paths).
- Read `input_cap = settings.reranker_input_cap`.
- Stamp `reranker_applied` with: `trace_id`, `session_id`, `task_id`, `span_id`, `model_id`,
  `candidate_count` (= `len(documents)`), `input_cap`, `top_k`, `result_count`,
  `top_score` (= max raw `relevance_score` among results, else `None`), `duration_ms`.
- Stamp `reranker_failed` with: `trace_id`, `session_id`, `task_id`, `span_id`, `model_id`,
  `candidate_count`, `input_cap`, `error`, `duration_ms`.
- Add `trace_id`/`session_id`/`span_id` to `reranker_config_missing` (request-scoped warning).
- **Layer 2 (gateway side):** when `trace_id is not None`, add `X-Trace-Id`/`X-Session-Id`
  (if present)/`X-Task-Id` (if present)/`X-Span-Id` headers to the POST, merged with the CF
  headers. Gated on `trace_id` so context-less calls (tests, scripts) send only CF headers —
  preserves existing CF-header tests and matches "joining only makes sense with a real trace".

### Step 2 — thread identity at the call site (`src/personal_agent/memory/service.py` ~1818)
- Pass `trace_id=trace_id, session_id=session_id` to the `rerank(...)` call (both already in scope;
  they stamp the sibling `memory_recall` event). `task_id` is not in scope in `query_memory`
  (sub-agent/spine concept) → not passed (defaults `None`; documented contract).

### Step 2b — feed identity INTO `query_memory` on the live rerank paths (codex HIGH)
Step 2 only helps if the recall path actually gives `query_memory` a `session_id`. The two
agent-invoked paths that rerank do not, so the ADR-0074 probe (keys on `session_id`) would still
not join. Thread the identity already in scope (same FRE-673 pattern already at both sites):
- `src/personal_agent/tools/memory_search.py:156` (the incident `search_memory` path): already
  passes `trace_id=trace_id`; **add** `session_id=getattr(ctx, "session_id", None)`.
- `src/personal_agent/orchestrator/executor.py:2047` (entity-name recall): passes neither; **add**
  `trace_id=ctx.trace_id, session_id=ctx.session_id` (both already used elsewhere in the block).

### Step 3 — tests
**Unit (`tests/personal_agent/memory/test_reranker.py`, patch `reranker.log`):**
- `test_applied_event_carries_identity_and_enrichment`: call with `trace_id/session_id/task_id`;
  assert `reranker_applied` kwargs include all identity + enrichment fields, `top_score == 0.9`,
  `candidate_count == 3`, `input_cap == 25`, `span_id` truthy.
- `test_two_reranks_have_distinct_span_ids`: two calls → two distinct `span_id` values.
- `test_failed_event_carries_identity`: server down → `reranker_failed` carries
  `trace_id/session_id/task_id/span_id/candidate_count`.
- `test_task_id_none_contract`: call with `trace_id/session_id` only → event has `task_id` present
  and `None` (codex Q5 — explicit contract).
- `test_trace_headers_sent_to_slm_when_trace_id`: slm endpoint + `trace_id` → POST headers include
  `X-Trace-Id`/`X-Session-Id`/`X-Span-Id` AND CF headers.
- `test_no_trace_headers_without_trace_id`: no `trace_id` → no `X-Trace-Id` header (CF-only).
- Existing CF tests stay green (called without trace context).

**Propagation — unit (`tests/personal_agent/tools/test_memory_search.py` or extend existing):**
- assert `search_memory_executor` calls `query_memory` with `trace_id=` AND `session_id=` from
  `ctx` (mock the service) — proves the incident call site (codex condition 2).

**Propagation — integration (`tests/test_memory/test_memory_service.py`):**
- the existing `_fake_rerank(query, documents, top_k=None)` will now receive `trace_id=/session_id=`
  kwargs → its signature MUST gain `**kwargs` (else TypeError). Capture them and assert the
  query_memory→rerank link threads identity. (Needs Neo4j test substrate :7688; run explicitly.)

### Step 3b — lint receiver rename (codex MEDIUM)
Rename the module logger `logger` → `log` in `reranker.py` so `check_identity_threaded.py` actually
enforces identity on these calls (it only matches a receiver literally named `log`). With `trace_id`
now in `rerank()` scope, the lint then requires every `log.*` in the function to thread it — a real
regression guard, and the file's three log calls all comply.

### Step 4 — follow-up ticket
- File (Needs Approval, project "Memory Recall Quality"): SLM server (separate repo) — read
  `X-Trace-Id`/`X-Session-Id`/`X-Span-Id` on `/v1/rerank` and stamp `routing_rerank_request` with
  them so the SLM-side log joins.

## Test commands
```bash
make test-file FILE=tests/personal_agent/memory/test_reranker.py
make mypy
make ruff-check && make ruff-format
uv run python scripts/check_identity_threaded.py src/personal_agent/memory/reranker.py
pre-commit run --all-files
```

## Out of scope
- SLM server repo change (follow-up ticket).
- Back-stamping historical rerank events.
