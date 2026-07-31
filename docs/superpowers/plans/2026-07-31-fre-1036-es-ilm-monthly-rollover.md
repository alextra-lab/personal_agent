# FRE-1036 — Elasticsearch index consolidation: monthly indices + ILM retention

> **Linear**: [FRE-1036](https://linear.app/frenchforest/issue/FRE-1036) — Tier-2:Sonnet, Approved, Urgent
> **Plan author**: Claude (Sonnet, build session)
> **Plan date**: 2026-07-31
> **Backing ADRs**: none (structural/operational change, not a new architectural surface)

## Revision history

**v2 (this version)** — revised after a codex plan-review pass (`codex resume
019fb7e7-2234-7e31-bad4-3b5c1ab6ee45`) found 5 blocking issues in v1. All 5 were verified
directly against the source before accepting; two of codex's other citations turned out to
be about files that were confirmed present and accurate (`docker/elasticsearch/*.json`
per-family policy/template files — v1 under-described these as inline shell heredocs based
on an earlier Explore pass that didn't catch the file layout). Fixes below, each tagged
with the finding it addresses:

1. **[capture.py disk/ES date coupling, confirmed]** `write_capture()`
   (`capture.py:224-251`) uses one `date_str` for *both* the disk directory
   (`captures/YYYY-MM-DD/`) and the ES index name. `read_captures()` (`capture.py:322`)
   parses that directory name strictly as `%Y-%m-%d` and silently skips anything else. The
   v1 plan's "one-line strftime change" would have broken disk-based capture reads. Fixed:
   introduce a second variable (`es_month_str`) for the ES index name only, in `capture.py`
   and its `backfill.py` capture-replay counterpart; the disk-directory format is
   unchanged. Verified this coupling does **not** exist anywhere else in scope — grepped
   `manager.py`, `es_projection.py`, `projector.py`, `executor.py`, `promotion.py` for any
   date-named disk path and found none; `manager.py`'s reflection file write uses a flat
   `entry_id`-based filename, not a date directory, so its ES-index `date_str` (computed
   separately, `manager.py:336`) was already independent.
2. **[in-flight-write race during migration, confirmed as a real gap]** v1 sequenced
   client cutover (step 2) before historical reindex (step 4), which already means old
   source indices stop receiving *new-family* writes once cutover deploys — but a
   deploy-restart window (seconds, not the "downtime" codex assumed the plan required) can
   still land a straggler write in an old index after the reindex snapshot is taken. Fixed:
   added an explicit delta-reconciliation pass immediately before each family's source
   deletion (see step 4 below) — safe and cheap here because every in-scope family's
   writes are either idempotent-by-`_id` (reindex overwrite is a no-op) or, for the two
   without an explicit id (`agent-logs`, `agent-captains-funnel-events`), the delta pass is
   a time-bounded `_reindex` with a query filter on the cutover timestamp, not a blind
   re-run of the whole family.
3. **[ILM `min_age` measures from index creation, not per-document age, confirmed]**
   Two distinct consequences, both addressed below: (a) for *newly created* monthly
   indices going forward, the current-month bucket must not be force-merged while still
   receiving writes — the existing `user-turn-ratings-policy` already encodes this via a
   32-day warm-phase `min_age` rather than a short one; v1's generic "warm forcemerge"
   language didn't state this. Fixed: **32d min_age on warm, uniformly, for every
   monthly-bucketed family** (matching the existing precedent), not a family-specific
   shorter value. (b) for indices *created during migration* from historical data, ILM's
   age clock would start at migration time, not the data's true period, silently extending
   retention by however long the backlog is. Fixed: after creating each migrated
   destination index, `PUT` `index.lifecycle.origination_date` set to the end-of-month
   timestamp for that bucket, so the delete-phase clock reflects the data's real age.
4. **[`date_index_name` pipeline design under-specified, confirmed]** A single pipeline's
   `index_name_prefix` is fixed, so "5 pipelines, one per timestamp field" cannot route
   `@timestamp`-field families (5 different family prefixes) to 5 different destinations.
   Fixed: **one pipeline per family** (not per field) — ~11 pipelines, each with its
   family's fixed `index_name_prefix`, the correct `field` for that family, explicit
   `date_rounding: "M"`, explicit `index_name_format: "yyyy-MM"` (the processor's default
   is `yyyy-MM-dd`), and `date_formats` covering the actual `datetime.isoformat()` output
   (variable microsecond precision) rather than relying on the processor's default parser.
   Each pipeline's config gets a dry-run against 3-5 real sampled documents from its family
   before the migration reindex runs against it.
5. **[overlapping wildcard source patterns, confirmed]** `agent-captains-captures-*`
   matches `agent-captains-captures-subagents-*`; `agent-monitors-joinability-*` matches
   `agent-monitors-joinability-substrate-*`. A wildcard-pattern reindex source would sweep
   sibling-family documents into the wrong destination. Fixed: migration reindex uses an
   **explicit enumerated list of concrete source index names** (pulled fresh from
   `_cat/indices` at migration time, per family, with sibling-family names excluded by
   construction), never a wildcard pattern, for every family's `_reindex` call. This also
   makes the before/after count proof (AC-1) auditable per source index rather than
   per pattern.

**Also softened, non-blocking:** the `agent-captains-captures`/`agent-captains-reflections`
retention increase (30d→90d/180d) is no longer framed as "fixing an inconsistency" —
codex correctly pushed back that the filesystem archive and the ES copy are not
duplicates of the same store (`capture.py:615-627` states ES is the durable source of
truth and the disk copy is not authoritative on its own), so matching their durations
isn't self-evidently correct. This is now presented as a standalone proposal requiring
its own owner sign-off, decoupled from the rest of the migration (default to keeping the
current 30d for both if the owner doesn't weigh in, rather than assuming the increase).
The `agent-logs-000001`/alias deletion in step 1 now includes a fresh zero-doc-count
recheck immediately before deleting, rather than relying on the investigation's
already-two-hours-old reading.

## Scope confirmed with owner before writing this plan

Field-level timestamp aliasing (the 2026-07-28 owner-directed comment layering ES field
aliases on top of ADR-0128) is **excluded**. ADR-0128's mechanism (D2–D8) was superseded
2026-07-30 by ADR-0129, which deliberately leaves log-record field names outside the
identity spine ungoverned. Neither ADR is Accepted. Building field aliasing now risks
throwing it away or fighting whichever design the owner eventually accepts. This build
does **index-name-level** normalization only (separators, suffixes, daily→monthly), never
touching document field names.

## What "unblocked either way" actually requires — current live state (verified 2026-07-31)

The ticket's own description is two days stale. Live investigation (cluster at
`localhost:9200`, this VPS's real ES — 610 active primary shards / 572 indices today, up
from 586 when filed, ~8-12 shards/day) found the starting position has moved:

1. **Three families already ship correct monthly indices with working ILM retention**
   (FRE-543/559, shipped between ticket filing and now): `agent-insights` (`YYYY-MM` dash,
   365d), `agent-monitors-slm-health` (`YYYY.MM` dot, 90d), `user-turn-ratings` (`YYYY.MM`
   dot, 365d). Confirmed via live `_ilm/explain`: all three show `managed: true`, clean
   `hot→warm→complete` progression, no errors. **These are not "no policy at all" — the
   ticket's "starting position" section is outdated.** Their policies are retention-only
   (`warm`/`delete` phases, no `rollover` action) — the client code itself picks the
   monthly bucket name; ILM only ages it out. This is a simpler, already-proven pattern
   than the rollover-alias mechanism the ticket's original description specifies, and it
   matches the owner's actual directive (monthly indices, not size-based rollover — see
   master's 2026-07-28 scope-adjustment comment on the ticket).
2. **`agent-monitors-joinability-policy` has a real `rollover` action but is broken in
   production right now.** Live `_ilm/explain` on `agent-monitors-joinability-2026.07.31`
   and the `-substrate` sibling: `managed: true`, stuck in `hot`/`check-rollover-ready`,
   `failed_step_retry_count: 32`, error `setting [index.lifecycle.rollover_alias] ... is
   empty or not defined`. No index template sets that setting, so every joinability index
   ever created is permanently stuck in `hot` — the stated 180d delete phase has never
   fired for this family. This is a live production bug this build will fix as part of its
   own scope (same family, same mechanism being introduced everywhere else).
3. **A dormant, half-built rollover-alias scaffold for `agent-logs` already exists and is
   dead.** `scripts/setup-elasticsearch.sh` bootstraps `agent-logs-000001` behind alias
   `agent-logs` (`is_write_index: true`), and PUTs `agent-logs-policy` with a real
   `rollover` action (`docker/elasticsearch/ilm-policy.json`) — but `agent-logs-template`
   never sets `index.lifecycle.name`, so nothing is attached. `agent-logs-000001` has 0
   docs, `managed: false`. The real write path (`agent-logs-{date}`, dotted, daily) never
   uses this alias at all. This build removes the dead scaffold rather than finishing it —
   monthly-by-name is the chosen mechanism, not rollover-by-alias (see point 1).
4. **`telemetry/lifecycle_manager.py::cleanup_elasticsearch_indices`** runs a client-side
   date-suffix-parsing delete sweep for `agent-logs`, `agent-captains-captures`,
   `agent-captains-reflections` today (30d cutoff, shared `elasticsearch_logs`
   `RETENTION_POLICIES` entry, `telemetry/lifecycle.py:70-78`). It parses `%Y.%m.%d` /
   `%Y-%m-%d` off literal index names and will silently no-op once these move to
   ILM-managed monthly indices. FRE-559 already retired this sweep for `user-turn-ratings`
   for exactly this reason (`telemetry/lifecycle_manager.py:375-379`, comment in place).
   This build applies the same retirement to the three families it still touches.
5. **Legacy pre-cutover daily indices from the FRE-543/559 migration were left orphaned,
   not backfilled or cleaned up.** `agent-insights-2026-04-17` (predates the monthly
   cutover): `_ilm/explain` → `managed: false` — it will never be aged out by the new
   policy, since template/policy attachment only happens at index-creation time.
   `user-turn-ratings-2026.05.31` is inconsistently `managed: true` (worth checking during
   implementation, not assumed here). This build reindexes these into the monthly
   structure and deletes the sources, finishing what FRE-543/559 left incomplete — this is
   the same reindex-and-verify mechanism the rest of this build needs anyway, applied
   uniformly.
6. **`agent-captains-funnel-events` and `agent-monitors-cache-reset-cadence`** are real,
   wired, currently-empty families (0 live indices; rare-event writers gated on ADR-0040
   budget-breach / ADR-0092 §D7 frozen-reset respectively). In scope for template/policy
   creation; no migration needed (nothing to reindex).

## Target design (uniform across all in-scope families)

- **Index name**: `<family>-YYYY-MM`, dash separator, monthly. One convention, chosen
  because 7 of 9 daily families already use dashes, and it matches `agent-insights`
  (already correct). The 3 already-monthly-but-dotted families (`agent-monitors-slm-health`,
  `user-turn-ratings`, and joinability's *daily* dotted format) migrate to dash too — the
  ticket's own reasoning for consolidating is that *any* heterogeneity, regardless of
  which side shipped first, causes wrong-index bugs (it already did, today, per the
  ticket's own report).
- **ILM policy**: `warm` (min_age **32d** — not sooner, matching the `user-turn-ratings`
  precedent, so the still-being-written current-month bucket is never force-merged; then
  forcemerge to 1 segment, lower priority) + `delete` (min_age = family retention, measured
  from `index.lifecycle.origination_date` for migrated indices — see revision #3 above, not
  from index-creation time). **No `rollover` action anywhere** — monthly bucketing is done
  by the client at write time (the proven pattern), not by ILM. This is a deliberate
  divergence from the ticket description's original "rollover aliases" framing, which
  predates master's 2026-07-28 clarification that the owner's actual direction is monthly
  indices, and predates discovery that the rollover mechanism is what's currently broken in
  production for joinability.
- **Migration**: for each family with existing daily/legacy indices, an explicit enumerated
  list of concrete source index names (fresh from `_cat/indices`, sibling families
  excluded — revision #5), reindexed through **one `date_index_name` pipeline per family**
  (not per field — revision #4; ~11 pipelines total, each with its own fixed
  `index_name_prefix`, `date_rounding: "M"`, `index_name_format: "yyyy-MM"`, and
  `date_formats` covering real `isoformat()` output) into the new monthly dash
  destination. Reindex preserves `_id` (default reindex behavior), so the 8+
  explicit-doc-ID writers found in investigation (broader than the ticket's stated 3 — see
  investigation notes) keep their overwrite/idempotency semantics unchanged. Client cutover
  (this same section, one step earlier in execution order) happens *before* migration, so
  source indices are no longer receiving new-family writes by the time reindex runs;
  a delta-reconciliation pass immediately before deletion (step 4) catches any stragglers
  from the deploy-restart window — revision #2.
- **Verify before delete**: per-family document count comparison (count API, not
  `_cat/indices`, which inflates on nested fields), field-count and mapping check on the
  new monthly destination (prior incident on this project: a field limit silently dropped
  fields on first pass), and `_reindex` response `.failures` confirmed empty. Only delete
  sources after all three checks pass.

## Retention per family

| Family | Retention | Basis |
|---|---|---|
| `agent-logs` | 30d (unchanged) | Matches current `elasticsearch_logs` policy + dormant `docker/elasticsearch/ilm-policy.json` |
| `agent-captains-captures` | 90d (**owner-approved increase, 2026-07-31**) | Aligns to the file-based `captains_log_captures` retention (`telemetry/lifecycle.py:58-63`). Codex correctly noted the disk archive and ES copy aren't duplicates of the same store (`capture.py:615-627`), so this is a genuine storage-duration decision, not a mechanical fix — flagged to the owner as such and explicitly approved as part of this build |
| `agent-captains-captures-subagents` | 90d | Same as parent captures |
| `agent-captains-reflections` | 180d (**owner-approved increase, 2026-07-31**) | Aligns to file-based `captains_log_reflections` retention (`telemetry/lifecycle.py:64-69`), same basis and approval as captures |
| `agent-monitors-joinability` | 180d (unchanged) | Matches the existing (currently non-functional) policy's stated delete phase |
| `agent-monitors-joinability-substrate` | 180d (unchanged) | Shares the joinability policy already |
| `agent-monitors-projector-health` | 90d (**new**) | Same category as `slm-health` — periodic operational health snapshot |
| `agent-topology` | 90d (**new**) | No existing precedent; operational-telemetry default, same as other monitors |
| `agent-captains-funnel-events` | 90d (**new**) | Rare operational event, no existing precedent |
| `agent-monitors-cache-reset-cadence` | 90d (**new**) | Rare operational event, no existing precedent |
| `agent-insights` | 365d (unchanged) | Already correct, already dash-`YYYY-MM` — no change needed except folding in orphaned pre-cutover stragglers |
| `agent-monitors-slm-health` | 90d (unchanged) | Already correct retention; only the separator changes (dot→dash) |
| `user-turn-ratings` | 365d (unchanged) | Already correct retention; only the separator changes (dot→dash) |

**`slm-requests` is explicitly out of scope** — formats its own dated index names
client-side in a different service (`slm_server`), tracked separately (FRE-1049/1071 under
ADR-0129's chain). Excluded per master's 2026-07-29 comment on this ticket; the
before/after shard metric will state this exclusion so it isn't misread as underdelivery.

**Owner sign-off obtained 2026-07-31** (interactive AskUserQuestion, this build session):
90d default approved for the four new-retention families; the `agent-captains-captures`/
`agent-captains-reflections` increase to 90d/180d approved; per-family verify-then-delete
sequencing (no separate pause before deletion) approved.

## Implementation steps

1. **Templates + policies first** (mechanical, low risk, no data touched): for each
   in-scope family, edit that family's existing `docker/elasticsearch/*-index-template.json`
   / `*-ilm-policy.json` file (confirmed these exist per-family already — not inline shell
   heredocs) to add/update the ILM policy (32d warm min_age + forcemerge, delete at
   retention, no rollover) and index template (`index.lifecycle.name` set, dash-`YYYY-MM`
   pattern). Remove the dead `agent-logs-000001` bootstrap block from
   `scripts/setup-elasticsearch.sh`; immediately before deleting that index + its alias,
   re-check its doc count is still 0 (don't rely on the investigation's reading, which will
   be hours old by implementation time). Fix `agent-monitors-joinability-ilm-policy.json`
   (drop the broken `rollover` action — this is the fix for the confirmed-broken production
   ILM state). Verify idempotent re-run of `setup-elasticsearch.sh` against the live
   cluster.
2. **Cut over client code to monthly, dash-separated names**: index-name-format-string
   change per write site (`es_logger.py:_get_index_name`, `capture.py` — new
   `es_month_str` variable, disk `date_str` untouched, `manager.py` ×2 sites,
   `backfill.py` — new `es_month_str` variable matching `capture.py`'s, disk-side date
   logic untouched, `sink.py` ×2, `projector.py`, `es_projection.py`, `executor.py`,
   `promotion.py`, `feedback_api.py`). Every site except `capture.py`/`backfill.py` is a
   one-line `strftime` format change (`%Y-%m-%d`/`%Y.%m.%d` → `%Y-%m`); no field or `_id`
   logic changes anywhere. Deploy this before starting step 4 so source indices stop
   receiving new-family writes.
3. **Retire the client-side ES cleanup sweep** for `agent-logs`/`agent-captains-captures`/
   `agent-captains-reflections` in `lifecycle_manager.py::cleanup_elasticsearch_indices`,
   same as the existing `user-turn-ratings` precedent (comment already in place explaining
   why).
4. **Migrate historical data**, per family: build that family's `date_index_name` pipeline
   (one per family, not per field — see Target design), dry-run it against 3-5 sampled real
   documents, enumerate the exact source index names from live `_cat/indices` (excluding
   sibling-family matches), reindex into the new monthly dash destination, set
   `index.lifecycle.origination_date` on each destination index to reflect the bucket's
   true period, verify doc counts + field/mapping counts + empty `_reindex.failures`, run a
   final delta-reconciliation pass for any post-snapshot stragglers, verify again, only
   then delete sources. Same treatment for the orphaned pre-cutover
   `agent-insights`/`user-turn-ratings` stragglers found in investigation.
5. **Re-measure and record proof**: before/after index count, before/after active shard
   count, headroom expressed as time-to-ceiling (recomputed growth rate: ~11 families × 1
   new index/month vs. the current ~8-12 shards/day — expect roughly a 20x reduction in
   growth rate), and confirm the wildcard queries found in investigation
   (`telemetry/queries.py`, `feedback_api.py`, `session_api.py`, `observation_api.py`,
   `joinability/status.py`, `cache_erosion/monitor.py`, `delivery_ratio/collect.py`, plus
   the skill docs) return identical results against a sample of real production queries
   before and after.

## Explicitly not in this build

- Field-level timestamp aliasing/renaming (excluded per owner decision above).
- The shard-headroom monitor (removed from ticket scope by master 2026-07-28, filed
  separately, parked).
- `slm-requests` (out of scope, separate ticket chain).
- Data streams (ruled out — 8+ write paths depend on explicit-`_id` idempotent overwrite,
  which data streams reject).

## Acceptance criteria (this ticket's own, per the "Proof required" section, monitor item removed)

1. Before-and-after index and shard counts, stated explicitly (live measurement, not the
   ticket's stale 2026-07-28 numbers).
2. Headroom against the shard ceiling expressed as time-to-ceiling, both before and after,
   with the growth-rate assumption stated.
3. Evidence that real queries this project already runs return the same results after the
   change — sampled against production data, not synthetic.
4. Field-count/mapping verification on every reindexed destination, before any source
   deletion.
5. `slm-requests` stated as excluded from the shard-count improvement so the metric isn't
   misread as underdelivery.

## Build-time verification (against the isolated test ES substrate, :9201 — never :9200/prod)

Everything below was actually run, not just reasoned about, before this PR was opened:

- `ES_URL=http://localhost:9201 bash scripts/setup-elasticsearch.sh` — all 21
  templates/policies applied with **zero failures**. This caught a real bug: a uniform
  32d warm min_age on `agent-logs-policy` (30d retention) violated ES's hot→warm→delete
  monotonicity requirement (`HTTP 400 action_request_validation_exception`) — fixed by
  dropping the warm/forcemerge phase for that one family (see Target design / retention
  table above).
- Created a live `agent-monitors-joinability-2026-07` index against the fixed policy and
  read `_ilm/explain`: `phase: hot, action: complete, step: complete` — no error, no
  retries. (Contrast with the pre-fix production state this plan diagnosed: `managed:
  true`, stuck in `hot`/`check-rollover-ready`, `failed_step_retry_count: 32`.)
- Ran the full migration script (`scripts/migrate_fre1036_monthly_indices.py`) against
  20 real `agent-logs-2026.07.*` daily indices seeded in the test substrate:
  `plan` correctly enumerated all 20 → one destination `agent-logs-2026-07`; `reindex`
  moved all 1295 documents with zero failures and set `origination_date`; `delta`
  re-run left the destination count unchanged at 1295 (empirical proof of the
  idempotent-by-`_id` claim, not just the theoretical argument); `cleanup` re-verified
  and deleted all 20 sources, leaving the destination intact at 1295 docs.
- All test artifacts (indices) created for this verification were deleted afterward;
  the isolated test substrate was left as found.
