# FRE-974: Instrument OVH embedding + Voyage reranker cost

**Branch:** fre-974-managed-embedding-reranker-cost
**Ticket:** https://linear.app/frenchforest/issue/FRE-974
**Related:** ADR-0120 (Proposed) §9 "Step zero" names this exact gap as T0; ADR-0121 (provider/placement layer); FRE-970 (LLM cost event names, prior session)

## Scope note — one ticket premise is stale, verified against source

The ticket says: *"The concurrency/semaphore telemetry tags these calls `provider_type: local`, which is wrong for OVH/Voyage."*

Verified this is **not present in current code**:
- `provider_type` was fully removed by ADR-0121/FRE-916 phase 2 (`config/models.yaml:32-35`, `llm_client/models.py:39-40`). It doesn't exist anywhere in `src/` anymore (grepped).
- `ovh` and `voyage` are declared as `providers:` with `placement: cloud` (`config/models.yaml:54-59,70-75`) — correctly, not `local`.
- The concurrency semaphore (`llm_client/concurrency.py`) is only instantiated and called by `LocalLLMClient`. `embeddings.py`/`reranker.py` dispatch via raw `openai`/`httpx` clients directly — they never touch this semaphore at all, so there is no mislabel to find there.

**No code change for this item.** The "$0, local infra" symptom the ticket's Impact section describes came from a budget-report generator seeing *no* `api_costs` rows for these vendors and inferring "must be local/free" — an absence-of-data problem, not a mislabeling bug. It resolves as a side effect of T0 (instrumenting real cost rows) below. This will be called out explicitly in the final ticket comment so master doesn't look for a fix that isn't there.

## What's actually being built (the real gap, confirmed)

Neither `memory/embeddings.py` (OVH) nor `memory/reranker.py` (Voyage) computes or records cost anywhere. Confirmed:
- `config/models.yaml`'s `embedding` and `reranker` deployments carry no pricing fields (contrast `claude_sonnet`/`claude_haiku`/`gpt-5.4-mini`, which do).
- `config/governance/budget.yaml` has no `embedding`/`reranker` role — **left alone**; the ticket explicitly defers cap decisions to a separate owner call.
- Real pricing (verified live, 2026-07-24):
  - **Voyage rerank-2.5**: $0.05 / 1M tokens (`docs.voyageai.com/docs/pricing`), single rate — no separate input/output split. Response includes `usage.total_tokens` (`query_tokens × num_documents + sum(doc_tokens)`).
  - **OVH Qwen3-Embedding-8B**: €0.10 / 1M tokens (OVH AI Endpoints catalog page). Response is OpenAI-compatible: `usage.prompt_tokens` / `usage.total_tokens` at the top level, alongside `data`. (OVH's stated max batch size of 25 on this page matches the code's existing `_MANAGED_MAX_BATCH = 25` — corroborates this is the right catalog entry.)
  - OVH bills in **EUR**; `api_costs.cost_usd` is USD-only (`docker/postgres/init.sql:90`, `DECIMAL(10,6)`, non-nullable). Needs an explicit conversion — ADR-0120 T0 flags this as a concrete requirement, not yet resolved anywhere in the codebase (no currency/exchange-rate plumbing exists today).

## Codex plan-review round (2026-07-24) — findings folded in

Ran `codex:rescue` on the first draft of this plan. Four blocking issues, all addressed below (this section documents what changed and why; the "Files touched" section further down already reflects the corrected design):

1. **`DECIMAL(10,6)` under-ranges the smallest real calls.** A ~1-token OVH embedding call costs ≈$0.000000114 — below the 6-decimal floor, rounds to `$0.000000`. That would fail the ticket's own "non-zero cost_usd" proof criterion on a legitimately-tiny call. **Fix:** widen `api_costs.cost_usd` to `DECIMAL(18,12)` via a new migration (`docker/postgres/migrations/0022_api_costs_cost_precision.sql`), run via `AGENT_DATABASE_ADMIN_URL` per the project's no-Alembic convention (`.claude/CLAUDE.md` pre-merge checklist). `DECIMAL(10,6)` stays fine for every existing LLM-chat cost row (those are already ≥$0.0001-scale); this only helps the new, much-smaller-denomination calls.

2. **Silently skipping cost recording when identity is absent quietly reintroduces the exact "invisible spend" bug this ticket exists to fix**, for the subset of call sites that are real background/system work with no live session (Neo4j entity/claim embedding backfills — `memory/service.py` `_backfill_entity_embeddings`/`_backfill_claim_embeddings`, and `assert_claim()` — all three have `trace_id` but genuinely no `session_id`, confirmed by reading their signatures). **Fix:** these three call sites pass a **system-session sentinel** (`SYSTEM_SESSION_ID = "00000000-0000-0000-0000-000000000000"`, mirroring the identical pattern already used for identity-less system work in `captains_log/capture.py:291` and `captains_log/backfill.py:231`) instead of `None` — so the row is still written and attributed as "system", not dropped. The identity-gated skip in `record_vendor_cost()` remains as the fail-safe for the genuinely unexpected case (a caller passes a malformed string), not as the normal path for known session-less work.

3. **Per-chunk vs. per-logical-call granularity was unresolved** — `_embed_managed()` chunks at 25 texts/request (`_MANAGED_MAX_BATCH`), so a single `generate_embeddings_batch()` call can be N separate HTTP requests to OVH, each with its own `usage.total_tokens`. The original draft aggregated all chunks then recorded one row, skipping the whole thing if any one chunk's usage was missing. **Fix, simpler than the original:** record cost **per chunk, inside `_embed_managed_batch()`** (which already makes exactly one HTTP request) rather than aggregating in `_embed_managed()`. This is both more literally "per-call" (one `api_costs` row per actual OVH HTTP request) and removes the all-or-nothing aggregation logic entirely — a missing `usage` on one chunk only drops that chunk's row, not the whole batch's.

4. **Correction, not a blocker:** `CostTrackerService.record_api_call()` already best-effort publishes a `ModelCallCompletedEvent` on the Redis event bus after a successful Postgres insert (`cost_tracker.py:200-208`, `_publish_model_call_completed`). So calling `record_api_call()` (as this plan already does) means these rows **do** reach the live-meter/`model_call_completed`-consuming path — just not the `llm_client/telemetry.py` structlog helper, which is what point 3 (design decisions, below) actually avoids. The plan's original wording overstated the avoidance; corrected here.

## Design decisions (flagging judgment calls explicitly, per CLAUDE.md "surface tradeoffs")

1. **EUR→USD conversion: a static, owner-adjustable rate**, not a live FX API call. New setting `AppConfig.eur_usd_rate: float` (default `1.14`, sourced from XE mid-market 2026-07-24, documented as approximate), overridable via `.env` `AGENT_EUR_USD_RATE`. A live-FX integration is out of scope — ADR-0120 itself only asks for "a conversion step... with a known EUR→USD test," not live rates, and this is a single-user research system where exact-to-the-cent accuracy isn't the bar; visibility is.

2. **New `ModelDefinition.input_cost_per_token_eur: float | None`** field (alongside the existing USD `input_cost_per_token`), used only by the `embedding` deployment. Reusing `input_cost_per_token` for a EUR rate would silently corrupt every USD-denominated consumer of that field (`register_model_pricing()` → `litellm.model_cost`, which assumes USD) — a new, clearly-named field is safer than overloading semantics.

3. **Do NOT reuse `emit_model_call_completed()` / the `model_call_completed` event verbatim.** That helper hard-requires `prompt_identity: PromptIdentity` (callsite/component_ids/hashes — a chat-prompt-composition concept) and a `role` from the LLM client's `ModelRole` enum, neither of which maps onto an embedding or rerank call. This codebase already has direct precedent for *not* force-fitting a differently-shaped call into that event name: `events.py`'s own comment on `LLM_STEP_COMPLETED` — *"FRE-352: step-level emit uses `llm_step_completed` to avoid conflating with the richer client-level `model_call_completed` payload in ES consumers."* Same logic applies here. Instead:
   - **reranker.py**: extend the *existing* `reranker_applied` event (already fired for every rerank call, `_log_reranker_applied`) with `provider` and `cost_usd` fields. No new event needed — this is the more surgical change (Step 5, "touch only what you must").
   - **embeddings.py**: there is currently no per-call success event at all (only failure warnings) — add one, `embedding_generated`, mirroring `reranker_applied`'s shape (provider, model, tokens, cost_usd, latency_ms, identity).
   - **Postgres `api_costs`** (the ledger real budget reports actually read, via `CostTrackerService.get_cost_by_model()` etc.) gets a row through the *existing* `CostTrackerService.record_api_call()` — reused as-is, since that table has no chat-specific columns. This is the literal "same telemetry the LLM path uses" for the budget-report half of the Proof criterion.
   - ES/Kibana queries for "OVH/Voyage spend" filter on `provider` (`ovh`/`voyage`) + `cost_usd` in whichever event is queried; both new events and the existing `reranker_applied` carry `provider` now, so this works without inventing a shared event name across two structurally different calls.

4. **Cost recording is best-effort and identity-gated, not identity-mandatory — but session-less system work gets a sentinel, not a skip.** `CostTrackerService.record_api_call()` raises `MissingIdentityError` if `trace_id`/`session_id` are `None` (ADR-0074's hard rule for the *LLM* path). A new shared helper (`cost_tracker.record_vendor_cost()`) validates both are present *and* parse as UUIDs before calling `record_api_call()`; otherwise it logs `vendor_cost_unattributed` at debug and returns — never raises, never blocks the embedding/rerank result (mirrors the fail-open philosophy already in both files). Genuinely session-less **background** call sites (the two Neo4j backfills + `assert_claim()`) pass `SYSTEM_SESSION_ID` explicitly rather than `None` (see codex round above, point 2) — so real vendor spend is never dropped just because it happened outside a live user turn.

5. **`trace_id`/`session_id` become optional kwargs on `generate_embedding()` / `generate_embeddings_batch()`**, mirroring `rerank()`'s existing signature (`trace_id: str | None = None, session_id: str | None = None`). Every call site that already has both identities in scope threads them straight through (mechanical, 1 line each — see file list below); the three session-less background sites pass `session_id=SYSTEM_SESSION_ID` explicitly (point 4); `notes_tools.py`/`artifact_tools.py` (tool-call context, always has both via `ctx`) thread through normally.

## Files touched

**Schema migration:**
- `docker/postgres/migrations/0022_api_costs_cost_precision.sql` — `ALTER TABLE api_costs ALTER COLUMN cost_usd TYPE DECIMAL(18,12)`. Applied via `AGENT_DATABASE_ADMIN_URL` (the app's `seshat_app` role cannot run DDL, FRE-808). Mirror the same widen in `docker/postgres/init.sql:97` so a fresh stack matches. `DECIMAL(10,6)` is kept for every other existing cost row's assumptions (nothing downstream reads a fixed scale) — this is a widen, not a semantic change.

**Config / schema:**
- `config/models.yaml` — add `input_cost_per_token: 0.00000005` to the `reranker` deployment (line ~222); add `input_cost_per_token_eur: 0.0000001` to the `embedding` deployment (line ~207).
- `src/personal_agent/llm_client/models.py` — add `input_cost_per_token_eur: float | None` field to `ModelDefinition` (near existing `input_cost_per_token`/`output_cost_per_token`, ~line 373).
- `src/personal_agent/config/settings.py` — add `eur_usd_rate: float = Field(default=1.14, gt=0, description=...)` near `voyage_api_key` (~line 1321).

**Cost recording (new shared helper):**
- `src/personal_agent/llm_client/cost_tracker.py`:
  - `SYSTEM_SESSION_ID: Final[str] = "00000000-0000-0000-0000-000000000000"` — sentinel for genuinely session-less background work, mirroring `captains_log/capture.py:291` / `captains_log/backfill.py:231`'s existing identical pattern for `user_id`.
  - `record_vendor_cost(*, provider, model, tokens, cost_usd, trace_id, session_id, purpose, latency_ms) -> None`: identity-gated wrapper around `CostTrackerService().connect()` → `record_api_call()` → `disconnect()`, swallowing (debug-logging) missing/invalid identity and any DB error. Callers pass `SYSTEM_SESSION_ID` explicitly for known session-less work (point 4 above) — the gate here is the fail-safe for unexpected malformed input, not the normal path for that case.

**Embeddings (OVH):**
- `src/personal_agent/memory/embeddings.py`:
  - `_embed_managed_batch()`: also parse `response.json().get("usage", {}).get("total_tokens")`; accept new `trace_id`/`session_id` params; on success (usage present) compute `cost_usd = total_tokens * pricing.input_cost_per_token_eur * settings.eur_usd_rate` and call `record_vendor_cost()` + emit a new `embedding_generated` info log (provider="ovh", model, tokens, cost_usd, latency_ms, trace_id, session_id) — **one row per chunk/HTTP request**, not aggregated. Missing `usage` on a chunk skips that chunk's cost row only (logged at debug) — vectors are unaffected either way.
  - `_embed_managed()`: thread `trace_id`/`session_id` through to each `_embed_managed_batch()` call; no aggregation logic needed (removed from the original draft).
  - `_generate_vectors()`, `generate_embedding()`, `generate_embeddings_batch()`: thread `trace_id`/`session_id` optional kwargs down to `_embed_managed()`. Local-fallback branch inside `_generate_vectors` and the non-managed `_call_embeddings_api` path emit nothing cost-related (not billed).

**Reranker (Voyage):**
- `src/personal_agent/memory/reranker.py`:
  - `_attempt_rerank()`: also parse `data.get("usage", {}).get("total_tokens")`; return `(results, total_tokens | None)` instead of bare `results`.
  - `rerank()` / `_rerank_fallback()`: capture the new tuple; only the **Voyage primary success** branch (not the Mac-tunnel fallback, not passthrough) computes `cost_usd = total_tokens * 0.00000005` (reading the price from the `reranker` deployment's `input_cost_per_token` via `resolve_role_definition`, not hardcoded — the literal 0.05/1M lives in `config/models.yaml`) and calls `record_vendor_cost(provider="voyage", ...)`.
  - `_log_reranker_applied()`: add `provider: str` and `cost_usd: float | None` params, included in the `reranker_applied` log payload.

**Call-site identity threading:**
- `src/personal_agent/memory/service.py` — 8 call sites total (corrected count from codex review). 5 sites already have both `trace_id`+`session_id` in local scope — thread through directly: lines ~1758, 2271→3178 (query embedding in `query_memory`), 3943 (`dense_recall_arm`), 4604 (paraphrase path), 4730. 3 sites are genuinely session-less background work — pass `session_id=SYSTEM_SESSION_ID` explicitly: `_backfill_entity_embeddings` (~2014, has `trace_id` only), `_backfill_claim_embeddings` (~2097, has `trace_id` only), `assert_claim()` (~2271, has `trace_id`+`user_id` but no `session_id` at all — confirmed by reading its full signature).
- `src/personal_agent/tools/notes_tools.py` — 2 call sites (lines ~314, 414) — has `trace_id`/`session_id` via `ctx`.
- `src/personal_agent/tools/artifact_tools.py` — 1 call site (line ~392).
- `src/personal_agent/memory/protocol_adapter.py` — 1 call site (line ~261).
- `rerank()` callers: no signature change needed (already optional) — just confirm cost flows through where they already pass identity.

**Tests (TDD — write failing first):**
- `tests/personal_agent/llm_client/test_cost_tracker.py` (new or extend) — `record_vendor_cost()`: records on valid identity (including `SYSTEM_SESSION_ID`); skips + logs on missing/invalid identity; swallows a DB error without raising.
- `tests/personal_agent/memory/test_embeddings_managed.py` — extend: mock an OVH chunk response with `usage.total_tokens`; assert computed `cost_usd` (EUR × rate) and `embedding_generated` event with `provider="ovh"`, non-zero `cost_usd`; assert a chunk response missing `usage` skips that chunk's cost row (no event, no `record_vendor_cost` call) rather than emitting a wrong number, while still returning its vectors; assert the local-fallback and non-managed paths emit no cost event; assert a multi-chunk batch (>25 texts) produces one cost row per chunk.
- `tests/personal_agent/memory/test_reranker.py` — extend: mock Voyage response with `usage.total_tokens`; assert `reranker_applied` carries `provider="voyage"` and non-zero `cost_usd`; assert the Mac-tunnel fallback path carries `provider="slm_local"` (or whatever the fallback role's provider resolves to) and `cost_usd=None`.
- A small config guard test (mirrors the existing "config_guard identity check" convention) asserting the live `config/models.yaml`'s `embedding` and `reranker` deployments carry non-`None` pricing — regression protection against silent drift back to unpriced.
- A migration/schema test (or a direct assertion in the cost-tracker tests) that a sub-$0.000001 cost value round-trips through `cost_usd` non-zero after the `DECIMAL(18,12)` widen.

## Explicitly out of scope (per ticket's own "What to build" section)

- `config/governance/budget.yaml` caps for `embedding`/`reranker` roles — ticket defers this to a separate owner decision.
- Perplexity instrumentation (ADR-0120 also names it, but FRE-974 is scoped to embedding+reranker only).
- Any part of ADR-0120's pause/alert/anomaly/approval-card machinery (Sections B/C, T1-T6) — that ADR is still Proposed, not Accepted, and this ticket is T0-only (visibility), matching FRE-974's actual Proof criterion.

## Acceptance criteria (from the ticket's own Proof section)

1. A 7-day query returns per-call cost for embedding and rerank with the correct provider tag (`ovh`/`voyage`) and non-zero `cost_usd` for a session that embedded/reranked.
   - **Proof:** new tests above + a live post-deploy check (query `api_costs` / the new structlog events for a real recall turn).
2. A budget report includes OVH and Voyage spend instead of $0.
   - **Proof:** `CostTrackerService.get_cost_by_model()` / `get_total_cost(provider="ovh"|"voyage")` return non-zero after a live call, verified post-deploy.

## Risk tier

**Standard** (touches `src/` cost logic + a schema-adjacent config field + memory call-site signatures across 4 files). Codex plan review required before coding, per build skill Step 3.
