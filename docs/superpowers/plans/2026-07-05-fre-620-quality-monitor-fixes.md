# FRE-620 — Fix quality-monitor `:Conversation` bug + recalibrate anomaly thresholds + promotion floor

Ticket: https://linear.app/frenchforest/issue/FRE-620
Refs: ADR-0030 (promotion), ADR-0060 (KGQ stream), ADR-0040 (budget gate / FRE-598),
ADR-0073/FRE-374 (render filter), ADR-0087 (Memory Recall Quality).
Related: FRE-598, FRE-560, FRE-621 (standing graph-hygiene backlog, Needs Approval).

No ADR — corrections to an existing detector, not a design decision (owner call, 2026-06-26).

## Live-data validation (done before writing this plan)

Queried the live `cloud-sim` Neo4j graph + `cloud-sim` Elasticsearch (both reachable on
localhost:7687/9200 from this VPS) directly, since the quality-monitor telemetry itself has been
broken the whole time (see below):

- **`:Conversation` confirmed dead**: `MATCH (c:Conversation)` → 0. `MATCH (t:Turn)` → 2199.
  `MATCH (e:Entity)` → 7831. Confirms the bug and blast radius exactly as described in the ticket.
- **`quality_monitor_entity_report` ES history (2026-06-06 → 2026-07-05, all 70 docs)**: every
  single day logged `conversations=0` and `ratio=0.0` — the false-positive high-severity anomaly
  has been firing every day this metric has existed. Nothing to "validate" from this series; it's
  100% artifact of the bug.
- **True historical ratio, reconstructed**: `Turn.timestamp` is a string property (ISO8601) on all
  2199 turns (earliest 2026-04-15). Queried cumulative turn count at each day boundary for
  2026-06-06→2026-07-05 and joined with the (correctly-logged) `entities` series from ES:
  ratio ranged **3.22 (06-13) → 3.56 (today)**, climbing slowly and monotonically, never outside
  that band. This directly supports the ticket's instinct that ~3.46 is "by design," not anomalous.
  → New band: **`(2.0, 5.0)`** — enough headroom around the validated 3.2–3.6 range to absorb
  normal graph growth, tight enough that a real structural break (e.g. ratio collapsing to 1.0 or
  jumping to 7) still fires. (High-severity threshold from `_range_anomaly` becomes `<1.0` or
  `>7.5`.)
- **`relationship_density` 30-day history**: 2.54 → 2.61, steady, always inside the existing
  `(1.0, 3.0)` band. **No change needed** — ticket doesn't ask for one; confirmed it isn't
  contributing to the false-positive problem.
- **`redundant_relationship_pairs` (absolute)**: grew 570 → 624 over 30 days, always past the
  abs-50 threshold (the "always red" bug). Live snapshot: 624 redundant pairs /
  11265 total relationship-bearing entity pairs = **5.54%**. No historical rate series exists yet
  (denominator was never logged) — this is a single live anchor point, not a 30-day series. New
  threshold: **rate > 0.10 (10%)** — comfortably above the current 5.5% (avoids an immediate
  false positive) while still catching roughly a doubling (5.54%×2 = 11.08% > 10%). (Codex review
  caught that a 15% floor, tried first, didn't actually satisfy "catches a doubling" — 11.08% < 15%
  — so this was tightened to 10%.) Document in the PR/ticket comment that this is a single-snapshot
  basis; the newly-added rate field will accumulate real history for a future re-validation once
  the dashboard has it.
- **`entity_extraction_started` daily counts, 32 days**: mostly 0–13/day with one outlier day at 50
  (2026-06-13). A `min_absolute_spike` floor of **15** lets normal day-to-day fluctuation
  (6, 9, 10, 11, 13) through without firing (since `latest - baseline_mean` stays under 15 for all
  of those) while the 50-count batch day still fires (delta far exceeds 15).
- **`empty_description_rate`**: live 1737/7831 = 22.2%, already documented in the ticket as
  render-mitigated stored cruft (FRE-374, `executor.py:1337`) — demoting entirely per Part 2.
- **Standing hygiene backlog ticket**: **FRE-621** already exists (Needs Approval) —
  "[knowledge] Graph hygiene: dedup entities, normalize redundant relationship-type pairs, backfill
  empty descriptions." It already supersedes FRE-423/428/430. No new ticket needed; reference it.

## Part 1 — Correctness

**1a. `:Conversation` → `:Turn`.** Three sites in `quality_monitor.py`:
- `check_entity_extraction_quality` (currently line 191): conversation count query.
- `check_graph_health` (currently line 256): conversation_nodes query.
- `check_graph_health` (currently line 283): timestamps query for `_max_gap_hours`.

Field/attribute names on `QualityReport`/`GraphHealthReport` (`conversations`,
`conversation_nodes`) are left unchanged — only the Cypher label changes. Renaming the dataclass
fields is out of scope (not requested, would touch every caller for no behavioral benefit).

**1b. Empty-denominator guard.** `entity_conversation_ratio` and `relationship_density` do **not**
share a denominator — ratio divides by `quality.conversations` (turn count), density divides by
`graph.entity_nodes`. (Caught in codex review: an earlier draft of this plan gated both range
checks on one combined condition, which would have silently skipped the density check whenever
turns were empty/disconnected even though entities — density's actual denominator — were fine, a
new false-negative.) Gate each independently in `detect_anomalies`:

```python
disconnected = not self._memory_service.connected
ratio_insufficient = disconnected or quality.conversations == 0
density_insufficient = disconnected or graph.entity_nodes == 0

if ratio_insufficient or density_insufficient:
    anomalies.append(
        Anomaly(anomaly_type="insufficient_data", severity="medium", ...)
    )  # one anomaly even if both triggered
if not ratio_insufficient:
    anomalies.extend(_range_anomaly("entity_conversation_ratio_out_of_range", ...))
if not density_insufficient:
    anomalies.extend(_range_anomaly("relationship_density_out_of_range", ...))
```

`severity="medium"` means `insufficient_data` maps to `ChangeCategory.KNOWLEDGE_QUALITY` in
`src/personal_agent/events/pipeline_handlers.py:715-717`, which — combined with Part 3's promotion
floor — makes it non-promotable through the *default* pipeline. No new severity tier needed. (Note
the "by construction" guarantee only holds for the default `PromotionCriteria()`; a caller that
passes explicit criteria without the exclusion — none currently do — could still promote it.)
Other anomaly checks (`duplicate_rate`, `extraction_failure_rate`, `no_relationships_created`,
spike detection) are untouched — they're independently guarded already or don't share either
denominator (`extraction_failure_rate` comes from ES telemetry, not Neo4j).

## Part 2 — Recalibration

1. `ENTITY_RATIO_TARGET`: `(0.5, 2.0)` → `(2.0, 5.0)`.
2. `_detect_spike`: add `MIN_ABSOLUTE_SPIKE = 15` module constant. After the existing
   `threshold`/`baseline_mean` checks, additionally require
   `(latest_value - baseline_mean) >= MIN_ABSOLUTE_SPIKE` before returning an anomaly.
3. `redundant_relationship_pairs`:
   - Add a new Cypher query in `check_graph_health` for total relationship-bearing entity pairs:
     ```cypher
     MATCH (a:Entity)-[r]-(b:Entity)
     WHERE id(a) < id(b)
     WITH a, b, count(*) AS cnt
     RETURN count(*) AS value
     ```
   - Add `relationship_bearing_pairs: int = 0` to `GraphHealthReport`.
   - Replace the absolute-count anomaly check with a rate:
     `redundant_relationship_pair_rate = redundant / bearing_pairs if bearing_pairs > 0 else 0.0`.
     New constant `REDUNDANT_RELATIONSHIP_PAIR_RATE_TARGET_MAX = 0.10`. Anomaly type stays
     `redundant_relationship_pairs_high` (only the trigger condition and `observed_value` change to
     the rate); `redundant_relationship_pairs` (the absolute count) remains on `GraphHealthReport`
     as dashboard/log context, just no longer anomaly-triggering directly.
4. `empty_description_rate_high`: remove the anomaly block from `detect_anomalies` entirely.
   `empty_description_entity_count` stays on `GraphHealthReport`/dashboard logs (info only).
   `EMPTY_DESCRIPTION_RATE_TARGET_MAX` constant becomes unused — remove it (orphaned by this
   change). Reference FRE-621 in the removed block's replacement comment.

## Part 3 — Promotion floor

1. `PromotionCriteria.excluded_categories` default: `[]` → `[ChangeCategory.KNOWLEDGE_QUALITY]`.

   **Scope call, flagged explicitly (codex review pushed on this — worth the owner's eyes before
   coding):** the ticket names the field `PromotionCriteria.excluded_categories` directly and
   frames the floor generically ("only RELIABILITY/high-severity auto-promotes to Linear"), not
   scoped to "graph-quality anomalies only." There is also only **one** production promotion path
   (`build_consolidation_promotion_handler` in `src/personal_agent/events/pipeline_handlers.py:241`,
   triggered on `consolidation.completed`) and it uses one shared `PromotionCriteria` for every
   Captain's Log entry regardless of origin — there's no existing per-source scoping mechanism, and
   building one would be new design surface the ticket explicitly disclaims ("this is corrections,
   not a design decision"). So changing the class default is the only way to implement "the floor"
   as literally requested, but it **is** global: it will also stop auto-promotion for
   `freshness_review.py` and `insights/engine.py` KNOWLEDGE_QUALITY proposals (`graph_staleness`,
   `graph_staleness_trend`), not just quality-monitor anomalies. That's an intended, not
   accidental, side effect under this reading of the ticket — but it's a real behavior change to
   two other subsystems this ticket doesn't otherwise touch, so it's called out here rather than
   applied silently.

   Verified this doesn't break existing tests: grepped `tests/test_captains_log/test_promotion.py`
   and `tests/personal_agent/memory/test_promotion_pipeline.py` for `KNOWLEDGE_QUALITY` — no hits.
   The shared `_write_entry` test helper defaults to `ChangeCategory.RELIABILITY`, and every test
   that constructs `PromotionCriteria()` without an explicit `excluded_categories` override
   (`test_promotion.py:256,319,355,388,459`) exercises entries in `RELIABILITY`/`CONCURRENCY`
   categories, not `KNOWLEDGE_QUALITY` — so none of them are affected by the new default. Add one
   new test asserting the default explicitly.
2. Document the tombstone vs re-arm disposition rule as a docstring note on
   `PromotionPipeline._existing_linear_issue_for_fingerprint` (where the `includeArchived=False`
   dedup lookup lives): noise/structural anomalies → cancel-not-archive (permanent tombstone,
   fingerprint stays suppressed); reliability anomalies → archive on close (frees the fingerprint
   so a genuine recurrence re-promotes). This is documentation only — no behavior change (closing
   tickets happens in the Linear UI, not in this codebase).

## Files touched

- `src/personal_agent/second_brain/quality_monitor.py` — Parts 1 + 2.
- `src/personal_agent/captains_log/promotion.py` — Part 3.
- `tests/test_second_brain/test_quality_monitor.py` — update existing anomaly-detection test's
  stub reports (add `conversations`, `connected`) for the insufficient-data guard; update ratio
  test values against the new band if needed.
- `tests/personal_agent/second_brain/test_quality_monitor_new_signals.py` — rewrite
  empty-description tests (now: never produces an anomaly regardless of magnitude) and
  redundant-relationship-pairs tests (now: rate-based, need a `relationship_bearing_pairs`
  denominator on the mock health stub).
- `tests/integration/test_quality_monitor_e2e.py` — insert the new
  `relationship_bearing_pairs` scalar-query value into the `_run_scalar_query` side_effect list
  (both the direct-call pass and the `detect_anomalies` pass).
- `tests/test_captains_log/test_promotion.py` — add a test for the new
  `excluded_categories` default.
- New tests: `insufficient_data` anomaly (disconnected driver; zero conversations only — density
  still evaluated; zero entities only — ratio still evaluated; does NOT fire when data is
  present), spike floor (small delta on near-zero baseline does not fire; large delta does),
  redundant-pairs rate (fires above 10%, not below), ratio band with new bounds.

**Checked and confirmed out of scope** (codex review flagged, verified, not touching):
- `tests/test_second_brain/test_graph_quality_stream.py` — imports `_range_anomaly` directly and
  passes its own `(0.5, 2.0)` target tuples as literals, never importing `ENTITY_RATIO_TARGET` or
  any other module constant, and never calls `detect_anomalies`/`check_graph_health`. Confirmed via
  grep: none of this ticket's changed constants or methods appear in that file. Not touched.
- `tests/manual/cleanup_graph_noise.py:141` has its own independent `MATCH (c:Conversation)` —
  same underlying bug, different file, not one of the ticket's three cited sites and not exercised
  by the acceptance criteria. Out of scope for this ticket; will file as a follow-up issue (Step 5)
  rather than fix inline, per surgical-changes discipline.

## Acceptance criteria (from ticket, restated as verifiable)

1. `quality_monitor.py` queries `:Turn`; ratio, turn-count, and freshness metrics compute non-zero
   against the live graph. → Verify: re-run the live-query script against `cloud-sim` post-fix,
   confirm `conversations`/`conversation_nodes` (now populated via `:Turn`) and
   `max_temporal_gap_hours` are non-zero.
2. Empty/disconnected graph yields `insufficient_data`, never a high-severity anomaly. → Unit
   tests: disconnected driver, zero-conversations, zero-entities cases.
3. Recalibrated thresholds validated against 30-day history (shown above); spike no longer fires on
   `<baseline-floor` deltas; redundant-pairs is a rate. → Unit tests + the validation data in this
   plan/PR description.
4. `empty_description_rate` no longer auto-promotes. → Unit test: large empty-description count no
   longer produces `empty_description_rate_high`, or any anomaly, at all.
5. Promotion floor live: synthetic medium `KNOWLEDGE_QUALITY` anomaly does NOT reach Linear; high
   `RELIABILITY` one does. → Unit test on `PromotionPipeline.scan_promotable_entries` /
   `PromotionCriteria` default with mixed-category entries.
6. `make test` / `make mypy` / `make ruff-check` clean.

## Test plan (TDD order)

1. `quality_monitor.py`: write failing tests for `:Turn` label queries (mock `_run_scalar_query`
   call args or just assert the report fields are non-zero given turn-labeled mock data) → fix
   queries.
2. Write failing tests for `insufficient_data` (disconnected / zero-denominator) → implement guard.
3. Write failing tests for the new ratio band, spike floor, redundant-pairs rate → implement Part 2.
4. Remove/replace `empty_description_rate_high` tests → remove the anomaly block.
5. `promotion.py`: write failing test for the new `excluded_categories` default → change default.
6. Run full `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
