# FRE-1215 — "44% of observed model spend never reaches api_costs"

**Ticket:** [FRE-1215](https://linear.app/frenchforest/issue/FRE-1215) (Approved, Urgent, Tier-1:Opus)
**Related:** FRE-1186 (shipped the detector + dash normalization), FRE-1064/FRE-1065 (ADR-0129 OTel bridge),
FRE-1205 (gateway emit lacks cost_usd), FRE-1217 (recurring reconciliation — separate ticket)
**Backing ADR:** ADR-0129 (OpenTelemetry owns trace and span identity — D1/D4)

---

## 1. Diagnosis — the headline is wrong, and that matters

**No spend was lost. The ledger is complete. The ES↔Postgres join is broken because the two
substrates carry two *different* trace identifiers for the same call.**

### Evidence

For the smallest named trace, `563c6781dcf5b016edb1e5b09ce6636b`:

| Observation | Value |
|---|---|
| ES `model_call_completed` @ 06:01:11.155133 | `cost_usd=0.001085`, logger `llm_client.litellm_client` |
| ES `api_cost_recorded` @ 06:01:11.151883 | `record_id=11455`, `cost_usd=0.001085` — **the write succeeded** |
| Postgres `api_costs` WHERE id=11455 | exists; `trace_id = 6306095c-a2b4-4f4b-811e-8a640255e115` |
| Postgres `api_costs` WHERE trace_id=`563c6781…` | **0 rows** |

`record_api_call` ran, inserted, and returned an id. The row is in the ledger — under a trace id the
ES documents never mention.

All four named traces resolve the same way, one-to-one, by matching cost + timestamp:

| ES trace (OTel-shaped) | ES cost | Postgres trace (uuid4) | PG total | Δ |
|---|---|---|---|---|
| `ff085f2882c5fcdad6f57b97e7d97517` | 0.113607 | `d5b3e36b-2ef0-43e9-8b40-498cfe1c321c` | 0.113649 | embeddings only |
| `d14f27817c9666f2a52d59e735aab3ed` | 0.052786 | `24c0c7cb-ac5d-43ba-b758-b37dd3bd4cc2` | 0.052825 | embeddings only |
| `3b23cd2731e85269ff5537743feeaa15` | 0.043777 | `cada0e9f-49a9-4def-b196-a894227c277c` | 0.043809 | embeddings only |
| `563c6781dcf5b016edb1e5b09ce6636b` | 0.001085 | `6306095c-a2b4-4f4b-811e-8a640255e115` | 0.001135 | embeddings only |

The residual in each row is the `embedding` spend, which records to the ledger but emits no
cost-bearing `model_call_completed` — so it is present on the Postgres side only, as expected.

### Mechanism

1. `service/app.py:2475` (`/chat/stream`) does `trace_id = str(uuid4())` — minting an identifier
   with no relationship to any span. That value is threaded down through
   `TraceContext(trace_id=…)` → `litellm_client` → `cost_tracker.record_api_call` → the
   `api_costs.trace_id` column.
2. `RequestRootSpanMiddleware` (`telemetry/otel_middleware.py`, ADR-0129 D4) has already opened a
   root span for the request.
3. `logger._add_span_context` (`telemetry/logger.py:156`) **unconditionally overwrites**
   `event_dict["trace_id"]` with the active span's id, so every ES document — including
   `api_cost_recorded`, which passes `trace_id=str(trace_id)` explicitly — carries the *span's* id.

Result: Postgres gets the minted uuid4, Elasticsearch gets the OTel span id. Neither component is
failing; they simply disagree about what the trace is called.

`_add_span_context` is behaving exactly as ADR-0129 D4 designed. The defect is that the three HTTP
entrypoints mint their own id instead of reading the span, which ADR-0129 D1 requires and which
`TraceContext.new_trace()` already implements (`telemetry/trace.py:60` `_read_or_mint_trace_id`).
Those endpoints predate the bridge and were missed by FRE-1064/1065.

### Why exactly these four, and why ~44%

The shape of the id tells you which side minted it. Across all 16 ledger traces in the ticket's
window:

| Path | Root span active? | Ledger trace shape | ES docs under that id |
|---|---|---|---|
| chat turns (6 traces) | yes | **v4 uuid4** | **0** — entire ES footprint is under the span id |
| `entity_extraction`, some `captains_log` (8) | no | v4 uuid4 | 4 each — kwarg survives, both sides agree |
| 2 × `captains_log` | yes, read correctly via `SystemTraceContext` | OTel-shaped | 208 / 114 — agree |

So the rule is the inverse of the ticket's assumption: **the calls that reconcile are the ones with
no span; the ones that "lose" spend are precisely the user-facing chat turns.** Those are the
expensive calls, which is why the gap is ~44% of dollars while being a minority of traces.

This also reproduces the ticket's other two numbers exactly: 10 matching ES traces = 6
`entity_extraction` + 4 `captains_log` background paths; 16 PG vs 14 ES = two chat turns whose PG
rows fell inside the 25h window while their ES docs fell outside the 24h one.

FRE-1186's `_normalize_trace_id` (dash stripping) was correct but cannot help here — it makes two
renderings of the *same* value comparable; these are different values.

---

## 2. Fix

Make the two **service** HTTP entrypoints read the active span's trace id instead of minting an
unrelated one — the behaviour ADR-0129 D1 already specifies and `TraceContext.new_trace()` already
implements. Changing `_add_span_context` instead is rejected: preserving a divergent explicit kwarg
would give one OTel trace two log identities and break span↔log correlation, which ADR-0129 D4 and
`test_otel_root_span.py:84` both require.

**Note on shape:** the helper returns 32-hex rather than dashed. This is not a new shape in
production — `SystemTraceContext.new()` and `new_trace()` already write hex-form ids into
`api_costs.trace_id` today (the two `captains_log` rows above prove it round-trips through the
Postgres `uuid` column). The change makes chat turns consistent with paths that are already correct.

### Steps

**Step 1 — make the mint helper public** (`src/personal_agent/telemetry/trace.py`)
Rename `_read_or_mint_trace_id` → `read_or_mint_trace_id`; update its two internal callers
(lines 149, 263). No test references the private name (verified by grep over `src/` and `tests/`).
→ *verify:* `make test-file FILE=tests/test_telemetry/test_trace_otel_bridge.py` passes.

**Step 2 — failing test, endpoint seam** (new file
`tests/personal_agent/service/test_chat_trace_identity.py`)
Modelled on `tests/personal_agent/service/test_otel_root_span.py` (in-memory tracer provider +
`RequestRootSpanMiddleware` + real `_add_span_context` via `structlog.testing.capture_logs`).

Calls the **real** `chat_stream_endpoint` inside an active root span with
`_resolve_session_selection`, `get_deduplicator` and `asyncio.create_task` patched, then asserts the
trace id threaded to `_process_chat_stream_background` equals the trace id stamped on the captured
`chat_stream.launched` record.
→ *verify:* fails before the fix (two different ids).

**Step 3 — failing test, both artefacts via real adapters** (same file) — this is AC-2 proper.
Codex review flagged the first draft of this step as tautological: it labelled two already-captured
strings "ES" and "ledger" without running either adapter. Redesigned so each side is produced
independently by real code, inside one active root span:
- **Ledger side** — drive the real `CostTrackerService.record_api_call` against a fake asyncpg pool
  that captures bound parameters, so the real INSERT binding of `trace_id` is exercised.
- **ES side** — drive the real `emit_model_call_completed` through the real structlog chain with
  `_add_span_context` active, producing a document with `cost_usd > 0`.

Assert both artefacts exist and name the same trace.
→ *verify:* fails before the fix (AC-2).

**Step 4 — reconciliation over the Step-3 corpus** (same file)
Run the same comparison the measurement used — dash-normalize both sides via
`observability.joinability.walk._normalize_trace_id` (the FRE-1186 normalizer, cited by line) and
take the set difference. Assert zero cost-bearing traces without a ledger row.
→ *verify:* fails before the fix, passes after (AC-3).

**Step 5 — apply the fix** (2 one-line changes)
| File | Line | Change |
|---|---|---|
| `src/personal_agent/service/app.py` | 1978 (`/chat`) | `trace_id = read_or_mint_trace_id()` |
| `src/personal_agent/service/app.py` | 2475 (`/chat/stream`) | same |

Remove `uuid4` imports only if they become orphaned.
→ *verify:* Steps 2–4 pass.

**Step 6 — fold in: fix the session cost-map double-count**
(`src/personal_agent/observability/topology/projector.py`)
Found by codex review. `sess.costs` is keyed two different ways: hydration inserts **dashed** keys
(`route_trace/ledger.py:163` — `str(r["trace_id"])` off a Postgres `uuid` column) while live
completion inserts the **raw event string** (`projector.py:401`), and `projector.py:430` sums the
values. Today both are dashed for chat turns so they collapse; after Step 5 the live key becomes hex
and the same turn occupies two entries — **double-counting the PWA session cost meter.**

Add a module-level `_cost_key(trace_id)` that dash-normalizes, and apply it at both writers
(`projector.py:264` hydration, `projector.py:401` completion). This also closes the pre-existing
instance of the same flaw on `SystemTraceContext` traffic. Folded in per build SKILL §5 — a
supporting change required to make this build correct, not separate work.
→ *verify:* a regression test asserting one hydrated dashed key + one live hex key for the same
trace sums once, not twice.

**Step 7 — quality gates**
`make test-file FILE=tests/personal_agent/service/test_chat_trace_identity.py` → then
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

**Step 8 — AC-4 disposition (documented, no code)**
No backfill. The four traces' spend is already in `api_costs` (rows 11455, 11511–11513 and their
siblings under `d5b3e36b…`, `24c0c7cb…`, `cada0e9f…`, `6306095c…`). Backfilling would **double-count**
real money in the substrate the cost gate reserves against. Historical rows keep their split ids;
the probe self-heals as those traces age out of its 24h window. Recorded in the ticket handoff.

---

## 3. Explicitly out of scope

- **Gateway `/chat` (`gateway/chat_api.py:481`)** — dropped from scope on codex's finding, and it is
  right: the standalone gateway app has **no root-span middleware**, so it has no divergence to fix;
  it bypasses LiteLLM for the Anthropic SDK; none of the four traces came from it; and its response
  contract is **tested as a dashed 36-char UUID** (`tests/personal_agent/gateway/test_chat_api.py:107`),
  so changing it is a contract migration, not a one-line fix. → **file a Needs-Approval ticket** for
  gateway root-span instrumentation (ADR-0129 D3) plus the response-format migration.
- **Recurring reconciliation** — FRE-1217 owns it.
- **FRE-1205** (gateway emit never sets `cost_usd`) — separate, still real; the ticket named it as an
  AC-1 candidate but it is not the cause here. All four traces emit from `litellm_client`.
- **Rewriting historical `api_costs.trace_id`** — see AC-4.
- **Changing `_add_span_context`** — it implements ADR-0129 D4 correctly.

## 3b. Blast radius — checked, not assumed

Everything read back from a Postgres `uuid` column renders dashed regardless of insert shape, so the
change cannot affect those consumers. `UUID(trace_id)` coercions accept both forms. Surfaces that
carry the in-process string directly were each checked:

| Surface | Verdict |
|---|---|
| `sess.costs` session cost meter | **New breakage — fixed in Step 6** |
| `/chat` response body (`app.py:2294`) | Rendering changes; no in-repo consumer requires dashed (eval harness treats it as opaque) |
| `/chat/stream` response body | Does not include `trace_id` — unaffected |
| Turn-rating join (`session_api.py:332`) | **Safe.** I initially called this a regression; codex was right. Session messages are `JSONB` (`models.py:221`), so the message `trace_id` is the same raw string the ES rating doc was keyed with — both sides move to hex together |
| Dedup store (`idempotency.py`) | Keyed on session/message id; trace_id is an opaque value |
| `api_costs`, budget reservations, `route_traces` | `UUID(...)` at the boundary — tolerant |
| Captain's Log capture (file + ES doc id) | Unconstrained string |
| PWA | Treats `trace_id` as an opaque string throughout |

## 4. Acceptance criteria → evidence

| AC | Evidence |
|---|---|
| **AC-1** all four attributed | All four emit from `llm_client/litellm_client.py` `acomplete` via `emit_model_call_completed`, on the `/chat/stream` entrypoint (`service/app.py` `chat_stream_endpoint`, confirmed by a `chat_stream.launched` doc on each). `record_api_call` **was** reached on every one and **did** insert (`api_cost_recorded` with a non-null `record_id`); no row results *under the queried id* because the insert used the endpoint-minted uuid4 while ES stamped the OTel span id. Row-level proof for `563c6781…` → id 11455; cost+timestamp proof for the other three (§1 table). |
| **AC-2** failing test | Step 2 — the two ids differ before the fix. |
| **AC-3** reconciliation over a test corpus | Step 3 — same join, zero unmatched. |
| **AC-4** disposition | Step 6 — no backfill, reason recorded. |
