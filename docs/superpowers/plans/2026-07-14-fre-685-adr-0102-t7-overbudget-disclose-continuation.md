# FRE-685 — ADR-0102 T7: Over-budget UX (disclose included/dropped pages + working continuation)

**Ticket:** FRE-685 (Approved) · **Backing ADR:** ADR-0102 §4 · **AC slice:** AC-4 (the
continuation half; the bounded/auto-select half already landed in FRE-683/T3)
**Blocked by:** FRE-683 (T3, merged to main via PR #508) — code dependency satisfied.

## Scope

FRE-683 (T3) already bounds Tier-2 page selection to the per-turn budget and emits a
count-only disclosure ("N of M page(s)... not included (per-turn page budget)"). T7 must:

1. Make the disclosure name **specific page ranges** — included and dropped — not just counts.
2. Make the offered continuation **actually work**: a follow-up turn asking for a dropped
   range, for the same artifact, must deliver exactly those pages, without a re-upload.

This requires `document_resolution.py`'s resolver to accept an explicit page range for an
already-stored artifact (the ticket's own wording), plus turn-level plumbing so a follow-up
turn can trigger it from R2 storage already holding the bytes (no new attachment needed).

## Design

### 1. `documents/document_resolution.py` — range-aware disclosure + explicit page selection

- New helper `_format_page_ranges(one_indexed_pages) -> str`: compresses a page-number list
  into "1-23, 27" style ranges (1-indexed, for human disclosure).
- New frozen dataclass `DocumentContinuationOffer` (artifact_id, content_type, title, r2_key,
  processing_target, dropped_pages: tuple[int, ...] 1-indexed) — the bare facts needed to
  serve a continuation, no session/turn identity (that's the caller's concern).
- `ResolvedDocuments` gains `continuation_offers: tuple[DocumentContinuationOffer, ...] = ()`.
- `AttachmentRef` (types.py) gains `requested_pages: tuple[int, ...] | None = None` —
  1-indexed page numbers to force-select for a Tier-2 document, bypassing salience
  auto-selection. `None` for a normal upload. Docstring updated to note this is a resolution
  directive for a continuation request, not upload metadata (codex flagged the class's
  existing docstring as upload-metadata-only; `processing_target` already established the
  precedent of a per-attachment resolution override living on this carrier).
- `_resolve_one_document`: when `attachment.requested_pages` is set, skip
  `compute_page_scores`/outline-boost entirely and select exactly those pages (clipped to
  the document's actual page count and the remaining per-turn budget, preserving ascending
  order like today's selector). Otherwise, unchanged salience auto-select.
- Both paths now build the included/dropped range disclosure via `_format_page_ranges` and
  emit a `DocumentContinuationOffer` whenever pages remain dropped after selection (auto-select
  drops the non-selected pages; continuation drops any still-truncated remainder of the
  requested range if the budget can't fit it all).
- `resolve_documents` aggregates `continuation_offers` across documents in the turn.

Byte/pixel/payload guardrail drops (existing AC-7 concerns) are untouched — continuation
offers are scoped to page-budget selection only, per AC-4's wording.

### 2. `orchestrator/types.py` — durable pending-continuation record

- New frozen dataclass `PendingDocumentContinuation` holding **all** of a turn's offers, not
  just one: `offers: tuple[DocumentContinuationOffer, ...]`, `created_at`, `ttl_seconds`,
  `original_trace_id`. (Revised after codex review: the original single-offer plan silently
  dropped the second offer when two documents were over budget in the same turn — a real case
  since the page budget is already shared/threaded across documents in one turn,
  `document_resolution.py:240-243,430-448`.)

### 3. `service/repositories/session_repository.py` — new JSONB key, no migration

Add `save_pending_document_continuation` / `load_pending_document_continuation` /
`clear_pending_document_continuation`, identical `jsonb_set`/`->`/`-` pattern as the existing
`pending_cloud_confirmation` trio, keyed `pending_document_continuation` in the same
`sessions.metadata` JSONB column (already exists — no DDL/migration needed). The stored
payload is the full `asdict(PendingDocumentContinuation)`, i.e. a dict with an `offers` list.

### 4. `orchestrator/executor.py` — turn-level wiring

- `_parse_requested_page_range(message) -> tuple[int, int] | None`: regex over "pages 24-40",
  "page 24 to 40", "24-40", "page 5" (mirrors `_is_affirmative_confirmation`'s style/location).
- `_maybe_reinject_pending_document_continuation(ctx)`: loads the pending record (a list of
  offers, one per over-budget document from the offering turn). Revised policy (codex flagged
  the original "clear on any parse miss" as too aggressive — a conversational reply like "yes,
  those next" would kill a legitimate offer, unlike the cloud-confirmation gate where a
  narrow yes/no is the *entire* interaction contract):
  - Try `_parse_requested_page_range` first. If it matches, intersect the requested range
    against **every** offer's `dropped_pages` (not just the first); for each offer with a
    non-empty intersection, reconstruct an `AttachmentRef` with `requested_pages` set to that
    intersection and append it to `ctx.attachments`. Multiple documents can be continued in
    one follow-up turn.
  - If no numeric range parses but the message is a broad affirmative (reuse/extend
    `_is_affirmative_confirmation`'s pattern set — "yes", "continue", "show me more", "the
    rest"), treat it as "all dropped pages, across all pending offers".
  - If neither matches, **leave the pending record in place** (do not clear) — an unrelated
    turn shouldn't destroy a live offer; the existing TTL (600s, same as cloud confirmation)
    is what bounds staleness, not turn-adjacency.
  - On a successful (partial or full) match, clear the record only for the offers that were
    fully satisfied; if any offer still has undelivered dropped pages after resolution's own
    budget clipping (below), leave those in the pending record instead of clearing outright.
- `_save_pending_document_continuation` / `_load_pending_document_continuation` /
  `_clear_pending_document_continuation` thin wrappers (mirror the cloud-confirmation trio at
  lines 353-440).
- Wire the reinject call in `step_init` alongside the existing
  `_maybe_reinject_pending_cloud_attachment` call (before the attachments block).
- After `resolve_documents` runs, if `doc_resolved.continuation_offers` is non-empty, wrap
  **all** offers into one `PendingDocumentContinuation` and persist it (replacing, not
  merging with, any prior record — a fresh turn's offers supersede stale ones).

**AC-4 exact-match note:** AC-4 requires the follow-up turn's assembled request to contain
*exactly* the previously-omitted pages. `_resolve_one_document`'s existing per-turn budget
still applies to a continuation request, so an extremely large dropped range could itself get
clipped and re-offered (intentional, matches the ADR's own "offer to continue on a specific
range" framing — continuation is allowed to be iterative for large documents). This does not
conflict with AC-4's own test scenario: a dropped range sized to fit within one fresh turn's
budget (the natural case — a single continuation turn has no other attachments competing for
budget) resolves in exactly one follow-up turn, which is what the AC-4 proof test exercises.

### Test plan (TDD)

- `tests/personal_agent/orchestrator/test_document_resolution.py`: `_format_page_ranges` unit
  tests; auto-select disclosure names both included and dropped ranges (not just counts);
  `requested_pages` selects exactly those pages, clipped to budget; out-of-bounds
  `requested_pages` disclosed, not crashed; `continuation_offers` populated only when pages
  are actually dropped; update `TestAC12CleanTaskText`'s closed-field assertion for the new
  `continuation_offers` field.
- `tests/personal_agent/orchestrator/test_executor.py` (or a new
  `test_document_continuation.py`): `_parse_requested_page_range` unit tests;
  `_maybe_reinject_pending_document_continuation` — reinjects on a matching range, clears on a
  non-matching message, no-ops with no pending record.
- **AC-4 proof (the definition of done):** an end-to-end `step_init`/`execute_task`-level test
  — turn 1 feeds an N>budget-page scanned PDF, asserts the disclosure enumerates included +
  dropped ranges; turn 2 (same session, no new attachment) asks for the dropped range, asserts
  the assembled `request_messages` for turn 2 contain exactly those previously-omitted pages.

### Exact commands

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_document_resolution.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor.py
make test
make mypy
make ruff-check && make ruff-format
```

## Risk-tier self-classification

**Standard** — touches `src/` orchestrator logic (executor.py, document_resolution.py,
types.py) and a new durable-storage key. Codex plan-review required before implementation.
