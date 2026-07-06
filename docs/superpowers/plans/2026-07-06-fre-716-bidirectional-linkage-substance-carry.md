# FRE-716 — ADR-0105 T3: bidirectional proposal↔ticket linkage + verbatim substance carry-through

**Backing ADR:** ADR-0105 (Accepted), decisions D4 and D5.
**Acceptance criteria this ticket carries:** AC-3 (bidirectional linkage, both sources), AC-4
(verbatim substance carry-through). AC-6 (loop-closure) and AC-5 (dashboard) belong to T4/T5.

## Scope recap

Building on `promotion.py` and the T1 `sysgraph` schema (already has `proposal`, `ticket`,
`promoted_to` tables — this ticket populates them, does not rebuild them):

1. Stamp the ticket id onto the source proposal (**already works** — `CaptainLogEntry.linear_issue_id`
   is source-agnostic and set by `_mark_promoted` today, for both `reflection` and
   `statistical_detector`). No change needed for this direction.
2. Stamp the source proposal id onto the ticket, queryable both ways — **missing today**. Implement
   via the `sysgraph.promoted_to` edge (D4: "In the graph this is the PROMOTED_TO edge").
3. Add the linkage field to the Insights-Engine ES surface (`agent-insights-*`), which has no linkage
   field today — **missing today**.
4. Carry full substance (what/why/how/rationale + experiment_design where present) verbatim into the
   promoted ticket body — **missing today** (`_format_linear_description` only carries what/why/how).

## Codex review findings (applied below)

A codex-rescue plan review flagged five points, all folded into the design below:
1. `sysgraph.proposal.fingerprint` has no UNIQUE constraint today — two concurrent promotions for the
   same fingerprint could create duplicate proposal nodes, making the reverse lookup ambiguous. →
   **Fixed with a new migration** adding the constraint (see Files touched #0) rather than working
   around it with select-then-insert. The live `sysgraph.proposal` table is empty in production today
   (T1 shipped the schema but nothing writes to it yet — this ticket is the first writer), so the
   migration is unconditionally safe to apply.
2. The only non-test `PromotionPipeline` call site (`pipeline_handlers.py:265`,
   `build_consolidation_promotion_handler`) passes no `sysgraph_repo` — without wiring it there, AC-3
   never fires live even though the mechanism exists. → **Added to Files touched (#3)**.
3. Legacy `CaptainLogEntry` rows with `proposed_change.source is None` (pre-T2/FRE-715 backlog) can't be
   inserted into `sysgraph.proposal` (NOT NULL + CHECK constraint) → **explicit, logged skip**, not a
   fabricated/inferred source. This is intentional: AC-3 is proven going forward on fresh promotions
   (T2 already guarantees every newly-produced proposal carries `source`); mislabeling old backlog
   entries with a guessed source would be worse than omitting them from the graph.
4. List-valued substance fields (`experiment_design`, `potential_implementation`) need each item to
   appear verbatim as its own line, not joined/summarized. → clarified in the substance section below.
5. `_update_by_query` cannot backfill insight docs indexed before the `fingerprint` field existed —
   acceptable; AC-3's check is about a fresh promoted pair, not full historical backfill.

## Design decisions

- **Graph-side linkage lives in `SysgraphRepository`**, not raw SQL in `promotion.py` — ADR-0105 D2
  states `SysgraphRepository` is the only code path permitted to open the `sysgraph` schema.
  `promotion.py` gets an optional `sysgraph_repo: SysgraphRepository | None` constructor param;
  `None` (tests, dry-run) skips the graph write entirely — never blocks Linear promotion.
- **Upsert via `ON CONFLICT` on both sides** — with the new UNIQUE constraint on
  `sysgraph.proposal.fingerprint` (Files touched #0), `proposal` gets
  `ON CONFLICT (fingerprint) DO UPDATE SET seen_count = EXCLUDED.seen_count, updated_at = NOW()
  RETURNING id`, exactly mirroring the `ticket`/`linear_issue_id` and `promoted_to`/edge treatment
  (`ON CONFLICT (linear_issue_id) DO NOTHING` with a select fallback for `ticket`;
  `ON CONFLICT (proposal_id, ticket_id) DO NOTHING` for the edge, already UNIQUE). Wrapped in one
  transaction (D2: "writes are transactional with promotion") — race-safe, not just low-volume-safe.
- **Legacy null-source entries**: `_record_sysgraph_linkage` returns early (logs at info level,
  `sysgraph_linkage_skipped_no_source`) when `entry.proposed_change.source is None`. Never fabricates
  a source.
- **Insights ES linkage uses `_update_by_query` matched on a new `fingerprint` field**, not a
  deterministic doc `_id`. Insight-detection docs are a time series (one doc per detection run,
  intentionally not deduped — that is the "465 insights/30d" volume metric). Reusing `fingerprint` as
  the doc `_id` would collapse repeated detections of the same idea into one doc and silently break
  that existing metric. `_update_by_query` over `agent-insights-*` matched on `term: fingerprint`
  stamps every historical doc for that idea — satisfies AC-3's check ("confirm the document carries
  `linear_issue_id`") without changing existing indexing semantics.
- **Fingerprint consistency**: extract a shared `_fingerprint_for_insight(insight, now)` helper in
  `insights/engine.py`, used by both `_index_insights` (new) and `create_captain_log_proposals`
  (existing, refactored to call it) — guarantees the ES doc's `fingerprint` and the promoted CL entry's
  `proposed_change.fingerprint` are byte-identical, which is what makes the `_update_by_query` lookup
  work at all.
- **New ES capability**: `ElasticsearchLogger.update_by_query()` (partial update via Painless script) —
  the only genuinely new ES capability. No new global wiring in `es_indexer.py`/`service/app.py`:
  `promotion.py` is already fully async, so it can `await` the update directly (wrapped in try/except,
  best-effort) using the existing `CaptainLogManager._default_es_handler` singleton — no fire-and-forget
  scheduling layer needed (that pattern exists for *sync* callers; `PromotionPipeline.run()` is async).
- **Substance carry-through** is a mechanical addition to `_format_linear_description`: append
  `entry.rationale` (always present, single string) verbatim as its own section, and, where present,
  `entry.experiment_design` / `entry.potential_implementation` (each `list[str]`) as one Markdown
  bullet per list item — each item's full text unchanged, never joined into one blob or summarized —
  and `entry.expected_outcome` (single string) verbatim. No truncation anywhere in this section (only
  the title truncates `pc.what[:80]`, per D5/AC-4 explicitly).
- **Fail-open, not fail-blocking**: sysgraph writes and ES linkage stamps are both wrapped in
  try/except and log a warning on failure. A promoted Linear ticket must never be rolled back or
  blocked because the graph/ES side-write failed — the ticket is the source of truth; linkage is
  additive metadata (consistent with the existing best-effort pattern already in `promotion.py` for
  event publishing).

## Files touched

0. `docker/postgres/migrations/0016_sysgraph_proposal_fingerprint_unique.sql` (new) — idempotent
   (`DO $$ ... IF NOT EXISTS (SELECT FROM pg_constraint ...) ...`) `ALTER TABLE sysgraph.proposal ADD
   CONSTRAINT ... UNIQUE (fingerprint)`, run as `sysgraph_role` per the T1 file's own stated convention
   ("wrap new sysgraph.* DDL in the same SET ROLE sysgraph_role / RESET ROLE pair"). Safe: nothing
   writes to `sysgraph.proposal` in production yet. Also update `docker/postgres/init.sql`'s
   `sysgraph.proposal` table definition (fresh installs) to declare `fingerprint TEXT NOT NULL UNIQUE`
   directly, keeping init.sql and the migration set in sync per project convention.
1. `src/personal_agent/sysgraph/repository.py` — add `ProposalRecord` dataclass + two methods:
   - `record_promotion(proposal: ProposalRecord, linear_issue_id: str, ticket_title: str | None) -> None`
     — `ON CONFLICT (fingerprint) DO UPDATE ... RETURNING id` for proposal, `ON CONFLICT
     (linear_issue_id) DO NOTHING` + select-fallback for ticket, `ON CONFLICT (proposal_id, ticket_id)
     DO NOTHING` for the edge — one transaction.
   - `ticket_source_proposal(linear_issue_id: str) -> UUID | None` — reverse lookup (ticket → source
     proposal id), the AC-3 "ticket→source-id resolves" query.
2. `src/personal_agent/captains_log/promotion.py`:
   - `PromotionPipeline.__init__` gains `sysgraph_repo: SysgraphRepository | None = None` and
     `es_handler: "ElasticsearchHandler | None" = None` (both appended after existing params —
     `linear_client` stays last-but-one — since call sites use keyword args).
   - Refactor the two success branches in `run()` (fresh-create and dedup-linked-existing) to share a
     new `_finalize_promotion(entry, linear_id)` that calls `_mark_promoted` (existing, unchanged
     return-shape — `run()` still returns `list[dict[str, str]]` with `entry_id`/`linear_issue_id`) +
     `_record_sysgraph_linkage` (new, best-effort, skips + logs on `source is None`) +
     `_stamp_insight_linkage` (new, best-effort, only when `proposed_change.source ==
     ProposalSource.STATISTICAL_DETECTOR`).
   - `_format_linear_description`: add rationale/experiment_design/expected_outcome/
     potential_implementation sections (list fields → one bullet per item, verbatim).
3. `src/personal_agent/events/pipeline_handlers.py` — `build_consolidation_promotion_handler`:
   construct a `SysgraphRepository(settings.sysgraph_database_url)`, `connect()` (best-effort — catch
   and proceed with `sysgraph_repo=None` on failure, matching the fail-open spirit already used
   elsewhere in this file), pass it into `PromotionPipeline(sysgraph_repo=repo, ...)`, `disconnect()`
   in a `finally`. This is the missing production wiring the codex review flagged — without it AC-3
   never fires live even though the mechanism exists.
4. `src/personal_agent/insights/engine.py`:
   - New module-level `_fingerprint_for_insight(insight, now)` helper.
   - `_index_insights`: add `"fingerprint"` and `"linear_issue_id": None` fields to the document.
   - `create_captain_log_proposals`: replace inline fingerprint computation with the shared helper —
     must preserve exact existing values (`tests/test_insights/test_engine.py:249,274,310` assert
     specific fingerprints today).
5. `src/personal_agent/telemetry/es_logger.py` — add `ElasticsearchLogger.update_by_query(index_pattern,
   query, script_source, params) -> int`, mirroring `index_document`'s style (best-effort, returns 0 and
   logs a warning on failure/no-connection).
6. `docker/elasticsearch/insights-index-template.json` — add explicit `fingerprint` mapping (`keyword`)
   to `properties`. (`linear_issue_id` is already covered by the existing `ids_keyword` dynamic
   template, but add it explicitly too for self-documentation, matching the file's own stated
   convention of auditing every field per FRE-704.)
7. Tests (new/updated):
   - `tests/personal_agent/sysgraph/test_repository.py` — `record_promotion` (creates proposal+ticket+
     edge; second call with same fingerprint/linear_issue_id is idempotent — no duplicate rows) and
     `ticket_source_proposal` (resolves back to the same proposal id). Real Postgres, `@pytest.mark.integration`,
     same fixture pattern as existing tests.
   - `tests/test_captains_log/test_promotion.py` — `_format_linear_description` contains
     `entry.rationale` and `entry.experiment_design` verbatim (AC-4); `PromotionPipeline.run()` calls
     `sysgraph_repo.record_promotion` when provided (mocked) and calls the ES stamp only for
     `STATISTICAL_DETECTOR`-sourced entries, never for `REFLECTION` (mocked `es_handler`).
   - `tests/test_insights/test_engine.py` — `_index_insights` document includes `fingerprint` and
     `linear_issue_id: None`; the fingerprint matches what `create_captain_log_proposals` computes for
     the same insight (regression-proofs the consistency the whole linkage depends on).
   - `tests/test_telemetry/test_es_logger.py` — `update_by_query` calls
     `client.update_by_query(index=..., query=..., script={...})` and returns the updated count;
     returns 0 without raising when `self.client` is None.

## Out of scope (belongs to other T-tickets)

- AC-5 dashboard/funnel (T5, FRE-719).
- AC-6 outcome→source loop-closure, realized-value signal (T4, FRE-717).
- D9/D10 generation-time read + semantic dedup (later tickets, gated on the FRE-720 probe).

## Test commands

```
make test-infra-up   # once, if not already running
make test-file FILE=tests/personal_agent/sysgraph/test_repository.py
make test-file FILE=tests/test_captains_log/test_promotion.py
make test-file FILE=tests/test_insights/test_engine.py
make test-file FILE=tests/test_telemetry/test_es_logger.py
make test
make mypy
make ruff-check
make ruff-format
```
