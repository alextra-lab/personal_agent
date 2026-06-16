# FRE-559 — ILM + retention for `user-turn-ratings-*`

**Linear:** FRE-559 (Approved, Tier-2, project *Telemetry Surface Audit*)
**Refs:** FRE-543 (sibling ILM pattern) · FRE-407 (per-turn ratings origin) · ADR-0090 (telemetry surface contract)
**Date:** 2026-06-16

## Problem

`docker/elasticsearch/user-turn-ratings-index-template.json` documented `_meta.retention_days: 90`
in prose, but **no mechanism enforced an ILM policy**. `scripts/setup-elasticsearch.sh` PUT the
template with no `index.lifecycle.name` and registered no `/_ilm/policy/...`, so the family accreted
daily indices forever — the exact gap FRE-543 closed for `agent-insights-*` / `agent-monitors-slm-health-*`.

A second, separate deleter *did* exist: `telemetry/lifecycle_manager.py` had a per-prefix override
sweeping `user-turn-ratings-*` at 90d by parsing the date out of the index name.

## Owner decisions

- **Retention: 365 days** (not the documented 90d). Ratings are per-turn ground-truth value labels —
  the join target for prompt-version quality analysis and training signal for the pedagogical model —
  so they get the same horizon as `agent-insights-*`.
- **Monthly partitioning** (`user-turn-ratings-YYYY.MM`), to keep a 365d-retained low-volume family
  from over-sharding (365 daily 1-shard indices → ~12/yr).
- **ILM is the sole authoritative deleter.** The `lifecycle_manager` 90d override is removed.

## Changes

| # | File | Change |
|---|------|--------|
| 1 | `docker/elasticsearch/user-turn-ratings-ilm-policy.json` *(new)* | `min_age` delete @365d (no rollover); `warm` forcemerge @**32d**; `_meta.retention_days:365`. |
| 2 | `docker/elasticsearch/user-turn-ratings-index-template.json` | `index.lifecycle.name: user-turn-ratings-policy`; `_meta.retention_days` 90→365; description. |
| 3 | `scripts/setup-elasticsearch.sh` | PUT the policy **before** the template; fix the stale "90 days" comment. |
| 4 | `src/personal_agent/gateway/feedback_api.py` | writer index `%Y.%m.%d` → `%Y.%m` (monthly). |
| 5 | `src/personal_agent/telemetry/lifecycle_manager.py` | remove the `user-turn-ratings` 90d sweep override + orphaned `timedelta` import. |
| 6 | `tests/scripts/test_es_templates.py` | one `ILM_FAMILIES` row (365d). |
| 6 | `tests/personal_agent/gateway/test_feedback_api.py` | assert monthly index name. |
| 6 | `tests/test_telemetry/test_lifecycle_manager.py` | regression: ratings absent from the sweep. |

### Why `warm.min_age: 32d` (deviation from sibling's 7d)

ILM phase ages count from index **creation**, and these indices have no rollover write-alias. A
monthly index is written for its whole month, so a `7d` warm would forcemerge the *current,
still-written* index (wasteful re-merges; ES warns against forcemerging an index still receiving
writes). `32d` guarantees the month has closed before forcemerge. Recorded in the policy `_meta`.

## TDD

1. Add `ILM_FAMILIES` row → `test_es_templates.py` fails (policy missing / unbound / unregistered) →
   create policy + bind template + register in setup script → green.
2. Tighten `test_feedback_api.py` to require `user-turn-ratings-YYYY.MM` (no day) → fails → flip the
   writer `strftime` → green.
3. Add `test_lifecycle_manager.py::test_cleanup_does_not_sweep_user_turn_ratings` (asserts the family
   is not in the `cat.indices` query) → fails → remove the override → green.

## Post-deploy (master runbook — see Linear handoff comment)

Template ILM binds only **newly created** indices. Existing `user-turn-ratings-YYYY.MM.DD` dailies
have no deleter once the Python override is gone, so a one-time back-attach is **required**:

```
PUT user-turn-ratings-*/_settings { "index.lifecycle.name": "user-turn-ratings-policy" }
```

Safe here: tiny low-volume indices; ILM `delete.min_age` counts from index creation, so no daily is
deleted before its own 365d.

## Discovered follow-up

Cross-period re-rate duplication: the writer reads existing ratings via the `user-turn-ratings-*`
glob but writes to the *current* period's index by `now`, so a re-rate in a later period leaves a
stale doc that `queries.py` aggregations double-count. Pre-existing; monthly shrinks the window
(per-day → per-month) but does not fix it. Filed as a follow-up (Needs Approval).
