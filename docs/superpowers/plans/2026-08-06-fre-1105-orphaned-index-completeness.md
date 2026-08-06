# FRE-1105 — fix silent index orphaning in the FRE-1036 migration script

**Linear**: [FRE-1105](https://linear.app/frenchforest/issue/FRE-1105) (Approved · High · Tier-2:Sonnet)
**Branch**: `fre-1105-orphaned-index-completeness` (off `main`)
**Backing ticket**: found by master running FRE-1036's migration; FRE-1036 must not reach Done until this lands.

---

## Context

`scripts/migrate_fre1036_monthly_indices.py` consolidates legacy per-day/per-month ES indices
into ILM-managed monthly indices, one family at a time. Each `FamilyConfig` carries exactly
one `legacy_pattern` regex. Two families — `agent-monitors-slm-health` and `user-turn-ratings`
— were configured with only the *monthly* legacy pattern, because that's what they already had
dot-separated indices for. Both families also carry dot-separated **daily** stragglers that no
pattern matches. A daily index that matches nothing is invisible to `plan`, `reindex`, `delta`
and `cleanup` alike — `cleanup` reports "deleted N/N" using the wrong denominator, since N never
included the unmatched indices.

Confirmed live against the real cluster today (`localhost:9200`, `cloud-sim-elasticsearch` —
this project's prod-equivalent single environment) via `plan --confirm-prod`:

- `agent-monitors-slm-health`: 12 orphaned daily-dotted indices (`2026.06.02`–`.13`), invisible
  to the monthly-only pattern. Its monthly sources were already cleaned up, so these 12 are
  pure orphans today — not attached to anything.
- `user-turn-ratings`: 7 orphaned daily-dotted indices (`2026.05.31`, `2026.06.01`–`.07`) plus 4
  monthly-dotted indices the monthly pattern already matches correctly.
- `agent-logs-000001`: confirmed 0 docs (`docs.count: 0`) — the dead rollover-alias bootstrap
  index the FRE-1036 ILM-policy rewrite already orphaned in configuration (see
  `docker/elasticsearch/ilm-policy.json`'s description) but never in the cluster itself.

## What changes

### 1. `FamilyConfig.legacy_pattern` (singular) → `legacy_patterns: tuple[re.Pattern[str], ...]`

A family may now carry more than one legacy pattern. `plan_family` matches a live index against
each pattern in turn (first match wins — the daily and monthly regexes are mutually exclusive by
construction: the daily pattern requires two separators + a day group, the monthly pattern is
anchored immediately after the month, so no string can satisfy both).

Give `agent-monitors-slm-health` and `user-turn-ratings` both `_daily_pattern(...)` and
`_monthly_pattern(...)`. No other family changes patterns.

### 2. Completeness assertion (the actual fix for the silent-orphan class of bug)

Add `_destination_pattern(prefix)` — recognizes an already-migrated destination shape
(`^{prefix}-\d{4}-\d{2}$`), distinct from a legacy source shape.

`plan_family` now classifies **every** live index under the family's prefix into exactly one of:
matched legacy source, existing destination, a declared `known_empty_deletions` name (see below),
or **unaccounted**. `FamilyPlan` gains `unaccounted: list[str]`.

New `IncompleteFamilyError(RuntimeError)` and `assert_family_complete(plan: FamilyPlan) -> None`,
raising when `plan.unaccounted` is non-empty. `_run()` calls this after `plan_family` for the
`reindex`, `delta`, and `cleanup` subcommands (not `plan` — `plan` stays a read-only diagnostic
that must still work, and show you the breakage, when a family is incomplete; that's its job).
An uncaught `IncompleteFamilyError` in `_run` is printed to stderr and maps to exit code 3, same
as an "HAD FAILURES"/"INCOMPLETE" outcome, for one family without aborting sibling families in a
`--family all` run.

### 3. `agent-logs-000001` — explicit, verified deletion

`FamilyConfig` gains `known_empty_deletions: frozenset[str] = frozenset()`. `agent-logs`'s entry
sets `known_empty_deletions=frozenset({"agent-logs-000001"})`. `plan_family` puts the live
intersection of this set into `FamilyPlan.pending_deletions` (accounted-for, not unaccounted).

`cleanup_family` gets a new step, independent of the per-destination reindex-verification loop
(this index carries no data to reindex and reindexing it into anything is meaningless): for each
name in `plan.pending_deletions`, re-verify live count is exactly 0, then delete it and add it to
`deleted`. If the live count is ever non-zero — config drift, or a name reused for something else
— refuse to delete it and set `all_ok = False`. Never trust the "known empty" label without a
fresh count.

## Files touched

- `scripts/migrate_fre1036_monthly_indices.py`
- `tests/scripts/test_migrate_fre1036_monthly_indices.py`

## TDD plan (failing tests first)

1. Update the shared test helper (`_plan`, `_cfg` usage) for `legacy_patterns` (plural) — mechanical,
   iterate `cfg.legacy_patterns` and take the first match, asserting exactly one pattern matches.
2. **Red**: flip `test_slm_health_dotted_monthly_maps_to_dash_monthly`'s trailing assertion — a
   daily-suffixed name (`agent-monitors-slm-health-2026.06.15`) must now MATCH (this reproduces
   the ticket's actual defect: today it asserts `None`). Add the equivalent daily-match assertion
   for `user-turn-ratings`. Run — confirm both fail against current code.
3. **Green**: implement `legacy_patterns` tuple + both patterns for the two families. Re-run — confirm green.
4. **Red→Green**: new tests for the completeness assertion:
   - `plan_family` with a fake ES returning a matched source, an existing destination, and a
     genuinely unaccounted stray index for the same family → `plan.unaccounted == [stray]`.
   - `assert_family_complete` raises `IncompleteFamilyError` when `unaccounted` is non-empty, and
     is a no-op when empty (the "assertion passes on a family with an unmatched index" failure
     condition from the ticket — assert it does NOT pass).
5. **Red→Green**: `agent-logs-000001` handling:
   - `plan_family` for `agent-logs` with `agent-logs-000001` present in live indices →
     `pending_deletions == ["agent-logs-000001"]` and `unaccounted == []` (not flagged as an
     orphan once explicitly declared).
   - `cleanup_family` deletes `agent-logs-000001` when its live count is 0, and includes it in
     `deleted`.
   - `cleanup_family` refuses to delete it (and sets `all_ok = False`) when its live count is
     non-zero — the config-drift safety guard.
6. Full family reproduction test using the exact real-cluster shape found above (10 slm-health
   daily orphans + already-migrated monthly + destinations; 7 user-turn-ratings daily orphans + 4
   monthly): `plan_family` returns zero `unaccounted` for both once the fix lands — this is the
   direct regression test for the ticket's own reported defect, not a synthetic minimal case.

## Verification against the real cluster (not just fixtures)

Per the ticket's own failure condition ("if the fix is proven only against synthetic fixtures
rather than the real cluster inventory that produced this finding"):

- **Before** the fix (confirmed above): `plan --confirm-prod` — `agent-monitors-slm-health` and
  `user-turn-ratings` show 0 and 4 source indices respectively; the 12 + 7 daily orphans are
  invisible.
- **After** the fix: re-ran `plan --confirm-prod` against the same live cluster. Confirmed —
  `agent-monitors-slm-health` shows all 12 daily sources, `user-turn-ratings` shows all 11
  (7 daily + 4 monthly) sources, `agent-logs` reports `agent-logs-000001` as
  `[pending deletion, verified empty at cleanup]`, and no family anywhere prints
  `!!! UNACCOUNTED` (`grep -c UNACCOUNTED` on the full output is 0). `plan`'s exit code is 0.
- This script only *executes* `--confirm-prod` for `reindex`/`delta`/`cleanup` when explicitly
  invoked with `--confirm-delete` for cleanup — I will NOT run the actual reindex/cleanup mutation
  against production data as part of this build session (data-destructive, and execution of the
  live migration for these two families is master's/deploy's operational call per the ticket's own
  "Sequencing" section, written from master's perspective). The PR ships the fix + read-only
  verification; the handoff comment gives master the exact commands and expected output to execute
  the actual migration for the two affected families plus the bootstrap-index cleanup.

## Acceptance criteria (from the ticket, restated as tests/probes)

| AC | Proof |
|----|-------|
| A family with an unaccounted index fails rather than reporting success | `assert_family_complete` raises `IncompleteFamilyError`; `_run` gates `reindex`/`delta`/`cleanup` on it |
| The orphaned 19 (now 12 + 7 daily + 4 monthly = 23 live today, drift since filing) are accounted for | Live `plan --confirm-prod` re-run post-fix: 0 `UNACCOUNTED` lines, exit code 0 |
| `agent-monitors-slm-health` / `user-turn-ratings` carry both patterns | New unit tests + live plan output |
| `agent-logs-000001` handled explicitly | `known_empty_deletions` + verified-count deletion path in `cleanup_family`, tests for both the empty and non-empty cases |
| Fix proven against real cluster inventory, not just fixtures | Live `plan --confirm-prod` before/after in this plan + PR + ticket comment; fixture test built from the exact real family shape found |

## Master gate bounce — cluster-level completeness (the ticket's thesis one level up)

Master reproduced the per-family fix exactly (12 / 11 / `agent-logs-000001` pending / zero
`UNACCOUNTED` / exit 0) and bounced on one finding: the completeness assertion above is per
*configured* family — `families()` returns nine, but the live cluster holds a tenth prefix,
`agent-topology` (39 live indices — dash-dailies through 31 Jul plus dash-monthly destinations
for Jul/Aug, so its write path already cut over and the dailies are legacy residue). It's
excluded by the `families()` docstring's claim of "zero live indices at authoring time" — true
when FRE-1036 was authored, false now, and nothing noticed the claim had expired. This is the
ticket's own thesis one level up: the PR fixed the denominator *within* a family and left the
denominator *over* families — the config list itself — free to go stale the same way, silently.

Investigating independently (not just trusting master's one example) turned up a **second**
prefix in the same state: `agent-monitors-projector-health`, 37 live indices, same shape
(dash-dailies + dash-monthly destinations already cut over) — also one of the four "zero at
authoring time" exclusions, also now false. The other two (`agent-captains-funnel-events`,
`agent-monitors-cache-reset-cadence`) are still genuinely zero.

Also found while surveying the full live cluster (271 non-system indices, 13 distinct prefixes):
two prefixes genuinely out of this migration's scope that were never in the docstring's
exclusion list at all — `caddy-access` (1 index, already writes its own monthly-dash shape
under a dedicated ILM policy, no legacy migration story) and `slm-requests` (26 indices, 100%
daily-dotted, never cut over — a distinct, already-ticketed gap, FRE-1106).

**Fix**: `cluster_unaccounted_indices()` — lists every live non-system index in the cluster
(`_list_all_indices`, dot-prefixed ES system indices excluded) and classifies each as belonging
to a configured `families()` entry, a new `EXCLUDED_PREFIXES: dict[str, str]` registry (each
entry carries a stated reason — `caddy-access`, `slm-requests`), or unaccounted.
`assert_cluster_complete`/`IncompleteClusterError` mirror the per-family shape. Wired into
`plan` only (not `reindex`/`delta`/`cleanup`) per master's explicit instruction not to widen the
migration — `agent-topology` and `agent-monitors-projector-health` are deliberately NOT added
to `EXCLUDED_PREFIXES` or to `families()`; surfacing them as unaccounted is the fix working, not
a regression to suppress. `families()`'s docstring no longer asserts a specific exclusion list
is safe — it points at this live check as the only thing that can't go stale the same way.

Live re-verification post-fix: `plan --confirm-prod` now exits 1 (expected — signal, not error),
reports a new `=== CLUSTER: 76 index(es) ===` section listing exactly the 39 `agent-topology` +
37 `agent-monitors-projector-health` indices, zero false positives among the other 11 prefixes
(including the two newly-excluded ones), and the original per-family checks are unchanged
(still 12 / 11 / `agent-logs-000001` pending / zero per-family `UNACCOUNTED`).

## Risk tier

Standard — touches `scripts/` logic that reindexes/deletes real production Elasticsearch data.
Codex plan-review required before implementation.

## Codex plan review — 5 findings, all addressed below

Codex reviewed this plan before any code was written (correct per the risk tier) and found 5 real
gaps in the design above:

1. **Sibling-prefix false positive.** `_list_indices` globs `f"{dest_prefix}-*"`. Two prefix pairs
   are sibling-containing: `agent-captains-captures` ⊃ `agent-captains-captures-subagents`, and
   `agent-monitors-joinability` ⊃ `agent-monitors-joinability-substrate`. A naive completeness scan
   for the parent family would see the sibling's own live indices in its glob results, match them
   against neither the parent's anchored legacy pattern (correctly excluded, that's the whole point
   of anchoring) nor the parent's destination pattern (the sibling's dest also isn't
   `^agent-captains-captures-\d{4}-\d{2}$`, it has `-subagents-` in between) — and wrongly flag them
   unaccounted. **Fix**: `plan_family` computes, from the full `families()` list, every *other*
   family whose `dest_prefix` starts with `f"{cfg.dest_prefix}-"` (a registered, more-specific
   sibling), and excludes any live index carrying that sibling's prefix from `unaccounted` — it's
   that sibling's own `plan_family` call that is responsible for it.
2. **`agent-logs-000001` could still never reach cleanup.** `_run`'s early-exit
   (`if not p.mappings: ... continue`) only checks `mappings`. Once `agent-logs`'s real daily
   stragglers are migrated away, a plan holding only `pending_deletions` would print "nothing to
   do" and skip `cleanup_family` — the exact case this feature exists for. **Fix**: the early-exit
   condition becomes `if not p.mappings and not p.pending_deletions`.
3. **Uncaught `IncompleteFamilyError` aborts `--family all` instead of skipping just the broken
   family.** **Fix**: wrap the `assert_family_complete` call per-family in `try/except
   IncompleteFamilyError`, print to stderr, set `exit_code = 3`, `continue` to the next family.
4. **`plan` stayed silent on `unaccounted`/`pending_deletions`.** The read-only diagnostic must
   actually show the breakage it exists to reveal. **Fix**: `plan`'s print loop also prints
   `pending_deletions` and, when present, an `!!! UNACCOUNTED` line per family; `plan` returns exit
   code 1 (distinct from mutating commands' 3) if any family has unaccounted indices, 0 otherwise —
   still never raises, still never touches data.
5. **Cleanup's reported denominator (`deleted/len(p.mappings)`) ignores `pending_deletions`.**
   Deleting only `agent-logs-000001` (0 mappings, 1 pending deletion) would print "deleted 1/0".
   **Fix**: report against `len(p.mappings) + len(p.pending_deletions)`.

Codex found no issue with the daily/monthly mutual-exclusivity claim, the destination-pattern
false-negative concern, `known_empty_deletions`/legacy-pattern precedence (moot — the sole
configured name, `agent-logs-000001`, doesn't match `agent-logs`'s daily pattern), or the
cleanup ordering/atomicity of the new independent deletion step.
