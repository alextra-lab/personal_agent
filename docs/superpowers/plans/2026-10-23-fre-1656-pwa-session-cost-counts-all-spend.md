# FRE-1656 — PWA session cost counts all spend, including web search

Ticket: FRE-1656 (Approved, bugfix). Related: ADR-0088 (turn projector), ADR-0092 (session lane), FRE-1648, FRE-1547, FRE-1401, FRE-974 (`record_vendor_cost`).

## Evidence (read-only query of `api_costs`, session db146e97)

* The turn trace `4b72555a` wrote rows at turn start (embedding, reranker) and more rows AFTER the turn completed (about 30 embeddings, `captains_log` $0.0125) under the SAME trace id.
* Entity extraction ($0.0153) ran under a DIFFERENT trace id (`e14407e8`) that never has a turn lifecycle.
* The projector drops every event for a completed trace (FRE-1401), and a non-turn trace folds its cost into its own `TurnObservation`, which never reaches `SessionAggregate.costs`. Both kinds of row are lost from `session_cost_usd`.
* The stored snapshot (`sessions.metadata['turn_status']`) is written only by `emit_turn_status`. The PWA reload path can show that snapshot, so a stale `session_cost_usd` of 0 survives a reload.
* `web_search` (`tools/web.py`) records no cost. Brave and Exa engines run inside SearXNG.

## Design

### Part 1 — projector (`observability/topology/projector.py`)

1. `SessionAggregate` gets `last_status: dict[str, Any] | None`. `_emit` stores the value it sends.
2. New `_fold_background_cost(sess)`:
   * Re-read the session's per-trace costs through the injected hydration source and assign them into `sess.costs` (assignment, never `+=`, so a redelivered event or a row already counted at hydration cannot double count). No source → no-op (carry-only).
   * If no turn of the session is running (`_by_trace` holds no `is_turn` observation for the session) and `last_status` exists, compute the new total (sum of all `sess.costs`). If it differs from the last emitted `session_cost_usd` at the bar's precision (2 decimals), re-emit `last_status` with only `session_cost_usd` replaced.
3. Call it from the `ModelCallCompletedEvent` branch when the trace is completed (before the early `return`) and when the observation is not a turn after folding the event.
4. The re-emit changes no ctx, tool or topology field, so the FRE-1401 and FRE-1547 guards (a late event must not null a ceiling or replace the reading) still hold.

### Part 2 — web search cost (`tools/web.py`, `config/settings.py`)

1. Settings: `brave_search_cost_per_call_usd = 0.005` (ADR-0034), `exa_search_cost_per_call_usd = 0.007` (FRE-1310 plan, Exa pricing page 2026-08-29).
2. After a successful SearXNG response, the paid engines used are: the `braveapi`/`exa` engines named in the raw (untruncated) `data["results"]`, plus the paid engines named in the `engines` argument that are not in `unresponsive_engines`. (Codex review: `engines_contributed` is cut at `max_results`, so it under-counts.) For each, call `record_vendor_cost(provider="brave"|"exa", model="brave/braveapi"|"exa/exa", tokens=0, cost_usd=<setting>, trace_id=ctx.trace_id, session_id=ctx.session_id, purpose="web_search", latency_ms=<request ms>)`. The model id uses the provider-prefixed canonical form (migration 0021). `record_vendor_cost` never raises and skips when the identity is missing.
3. Known limit: a `general` search whose paid engine returns zero results is not seen in the response, so that billed call is under-counted. Handoff states this.

## Codex plan review (2026-10-23)

Accepted: engine signal (above), canonical model id (above). Accepted as limits: session or active-trace LRU eviction loses `last_status` or the running-turn scan, so a late row shows on the next full turn emit instead of at once. Out of scope: a duplicate event before `TopologyEnteredEvent` doubles `obs.live_cost_usd` until completion (existing behavior). Noted: `record_vendor_cost` bypasses `CostGate.reserve` (existing vendor-cost behavior).

## Tests (TDD — each fails before the change)

* AC-1 `tests/observability/topology/test_projector.py`:
  * late rows on a completed trace: turn completes at $0.00, hydration source later returns the rows → `session_cost_usd` in the re-emit equals their sum.
  * rows on a separate non-turn trace → same sum.
  * running turn in the session → no re-emit, the next turn emit carries the sum.
  * redelivered event → no double count.
  * change under one cent → no re-emit.
* AC-2 `tests/test_tools/test_web_search.py`: mock SearXNG with results from `braveapi` and `exa` → two `record_vendor_cost` calls with non-zero cost, `purpose="web_search"`, the ctx ids. Only `exa` → one call. No session id → no call. No paid engine → no call.

## Commands

```
make test-file FILE=tests/observability/topology/test_projector.py
make test-file FILE=tests/test_tools/test_web_search.py
make test · make mypy · make ruff-check · make ruff-format · pre-commit run --all-files
```

## Diff class

Escalated: cost code and a production `api_costs` write path. Flag for owner `/code-review ultra` before merge.

## Post-deploy (master)

`seshat-gateway` rebuild (not a reversible class). Verify: reload session db146e97, bar shows at least $0.03. Run one `web_search` turn and `SELECT provider, cost_usd FROM api_costs WHERE purpose='web_search'` shows the rows.
