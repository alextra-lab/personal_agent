# FRE-703 — `delegation_outcomes` dashboard: retire-proposal with evidence

**Worklist item:** `docs/research/2026-07-01-dashboard-value-audit-worklist.md` #2 — `delegation_outcomes`.
**Decision it must enable:** *"what delegation patterns occur, and what are their outcomes?"*
**Outcome of this pass:** could not build a redesign that answers this decision from real data.
Recommending retirement (or a hold pending an emit-side fix) — **decision left to master + owner**,
per the loop's standing rule that retirement is never a loop action.

## What the worklist asked for

> **delegation_outcomes** [1p] — **redesign the query.** It filters `delegation_package_created`
> (rare — 17 docs ever); the live signal is `delegation_pattern_analysis_*` (1,832 docs).

That framing (from the FRE-533/535 triage) assumed the 1,832-doc `delegation_pattern_analysis_*`
stream was a healthy substitute for the rare `delegation_package_created` event. Step 0 of this
pass (inspect the raw event, don't assume) found that assumption doesn't hold.

## Step 0 findings — raw event inspection (live agent-logs-*, 2026-07-01)

Time field for every event below is `@timestamp` (confirmed, not assumed).

### `delegation_package_created` — 17 docs, ALL from 2026-05-10

```
event_type: delegation_package_created
fields: task_id, target_agent, context_items, memory_items, criteria_count,
        pitfall_count, complexity
```

All 17 documents live in the `agent-logs-2026.05.10-v2` index. **Zero documents since.** The
structured delegation-handoff pathway (`delegation/` module, ADR-driven protocol adapters) has
been completely dormant for 50+ days — this isn't "rare," it's inactive in current usage.

### `delegation_pattern_analysis_start` / `_complete` — 916 docs each, active today

```
event_type: delegation_pattern_analysis_complete
fields: insights_found, total_delegations, days
logger: personal_agent.insights.engine / detect_delegation_patterns
```

This job runs continuously (every few minutes, 916 docs across the observed window, most recent
today). But:

- **`total_delegations` and `insights_found` are unqueryable.** `GET
  agent-logs-2026.07.01/_field_caps?fields=total_delegations,insights_found,days` returns an
  **empty field list** — despite the values being present in every document's `_source`, and every
  sampled hit carrying `"_ignored":["days","insights_found","total_delegations"]`. Confirmed
  identically on three separate daily indices (`2026.06.29`, `2026.06.30`, `2026.07.01`).
- **Root cause:** `docker/elasticsearch/index-template.json` sets
  `index.mapping.total_fields.limit: 300` with `ignore_dynamic_beyond_limit: true`. The three
  sampled daily indices are already at **292–294 / 300 mapped fields**. New dynamically-introduced
  fields that lose the race against the day's other ~300 event-type fields are **silently dropped**
  — not indexed, not aggregatable, not visible to Lens (they show under neither "available" nor
  "empty" fields; they simply don't exist to Kibana). This is not a `dynamic_templates` naming-
  convention miss (`total_delegations` / `insights_found` / `days` don't match any of the six
  existing templates, but that's irrelevant — the field-count ceiling drops them before template
  matching would even help).
- **Even where readable, the sampled values are always zero.** 20/20 manually inspected
  `delegation_pattern_analysis_complete` docs across all three days show
  `total_delegations: 0, insights_found: 0` — consistent with zero delegation packages created
  since 2026-05-10.
- **No outcome field exists in either event family.** Even a perfect fix for the above would only
  recover *volume* (`total_delegations`), not *outcome* (success / failure / quality) — the second
  half of the stated decision has no backing field anywhere in this event family.

## Why this blocks any redesign

1. The event the worklist proposed as the "live signal" (`delegation_pattern_analysis_*`) carries
   its one substantive metric in fields Elasticsearch silently discards.
2. Even if that were fixed, the metric has been zero for 50+ days, because the upstream
   `delegation_package_created` event that would feed it is dormant.
3. No telemetry field anywhere captures delegation *outcome* — the second half of the decision is
   unanswerable regardless of (1) and (2).

A Lens dashboard cannot manufacture a decision the underlying telemetry doesn't carry. Per the
skill's rule ("a chart that leads a human to the wrong answer is worse than no chart"), shipping a
redesigned chart here — e.g., a volume trend that would show a flat/near-empty line and imply
"nothing happens" — would itself be misleading, since the true story is a telemetry gap, not an
absence of delegation activity by design.

## Recommendation (master + owner call, not executed here)

- **Retire** `delegation_outcomes.ndjson` from the active dashboard suite — no redesign path exists
  with current telemetry.
- **Separately** (not in scope for this ticket): the `total_fields.limit: 300` /
  `ignore_dynamic_beyond_limit: true` combination is a **systemic risk**, not unique to delegation
  telemetry — any newly-added field on a busy day can be silently dropped index-wide, with no error
  surfaced anywhere. Worth its own investigation (raise the limit? prune stale/dead fields from the
  292–294 in current use? both?) independent of this dashboard's fate.
- If delegation telemetry is expected to matter again, the `delegation/` protocol-adapter pathway
  and the `detect_delegation_patterns` emit path would need a live-data pass of their own before a
  dashboard is worth rebuilding.

## What this PR changes

Nothing in `config/kibana/dashboards/` — the existing (already-broken, per FRE-535/593-class
issues) `delegation_outcomes.ndjson` is untouched. This document is the evidence artifact for the
master gate to weigh a retire decision against.
