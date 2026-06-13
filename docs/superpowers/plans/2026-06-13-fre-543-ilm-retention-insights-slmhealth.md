# FRE-543 — ILM + retention for `agent-insights-*` and `agent-monitors-slm-health-*`

> **Date:** 2026-06-13 · **Ticket:** FRE-543 (Tier-2:Sonnet) · **Project:** Telemetry Surface Audit
> **Refs:** FRE-534 (A2 — authored these templates, ILM out of scope) · FRE-533 (A1 reconciliation) ·
> substrate-observability / data-lifecycle gaps backlog

## Problem

Two `agent-*` families roll daily with **no ILM policy and no retention** — they accrete daily
indices forever:
- `agent-insights-*` (daily `agent-insights-YYYY-MM-DD`; insights-engine records, ~handful/day)
- `agent-monitors-slm-health-*` (daily `agent-monitors-slm-health-YYYY.MM.DD`; SLM-health probe
  snapshots, `slm_health_probe_interval_seconds=300` → ~288 tiny docs/day)

Today only `agent-logs-*` (rollover 7d/1gb → delete 30d) and `agent-monitors-joinability-*`
(rollover → delete 180d) have lifecycle policies. FRE-534 authored the two templates above but left
ILM out of scope.

## Approach

Both are daily-index families with no rollover write-alias. The policies use a **`min_age`-based
`delete` phase, not rollover** (`min_age` is measured from index creation for non-rollover indices);
mirror the joinability policy shape minus the rollover action, plus a `warm` `forcemerge` for cheap
long-lived compaction.

**Repartition daily → monthly (owner decision, 2026-06-13).** Daily indices at these volumes
(insights ~handful/day, slm-health ~288 tiny docs/day) over a long retention create hundreds of
single-shard sub-KB indices — textbook ES over-sharding. The index-name granularity is set by the
**writer's `strftime`**, not the template/policy, so we change one format string per family to a
monthly suffix. Template patterns (`agent-insights-*`, `agent-monitors-slm-health-*`) still match;
dashboards glob those patterns and aggregate on `@timestamp`/`probed_at`, so they are unaffected.
Steady-state index count drops from ~365 → ~12 (insights) and ~90 → ~4 (slm-health). We keep each
family's existing separator: insights `%Y-%m` (dash), slm-health `%Y.%m` (dot).

Rollover was considered and rejected: it self-sizes but needs a write-alias + bootstrap index +
writer indexing into the alias — over-engineered at a handful of docs/day; monthly date-partition is
simpler and right-sized.

**Retention (owner-confirmed 2026-06-13), recorded in each policy's `_meta.retention_days` + `_meta.description`:**
- `agent-insights-*` → **365d**. Low-volume analytical/learning records (cross-session patterns,
  reflections) feeding the pedagogical model — a full year of cross-session history, still tiny.
- `agent-monitors-slm-health-*` → **90d**. Operational health diagnostics — a quarter of
  capacity/latency trend while bounding growth.

Monthly + `min_age` caveat: a monthly index is deleted `min_age` after the *month's first doc*, so
the oldest data can live ~retention + 1 month. Negligible at 365d/90d.

## Files

### 1. `docker/elasticsearch/insights-ilm-policy.json` — NEW
```json
{
  "policy": {
    "_meta": {
      "description": "ILM for agent-insights-* (daily index, low-volume analytical/learning records). min_age-based delete (no rollover alias). Retention: <N>d.",
      "retention_days": <N>,
      "managed_by": "scripts/setup-elasticsearch.sh"
    },
    "phases": {
      "hot":    { "min_age": "0ms", "actions": { "set_priority": { "priority": 100 } } },
      "warm":   { "min_age": "7d",  "actions": { "forcemerge": { "max_num_segments": 1 }, "set_priority": { "priority": 50 } } },
      "delete": { "min_age": "<N>d", "actions": { "delete": {} } }
    }
  }
}
```

### 2. `docker/elasticsearch/monitors-slm-health-ilm-policy.json` — NEW
Same shape; `_meta` describes the SLM-health family + its retention.

### 3. `docker/elasticsearch/insights-index-template.json` — add lifecycle ref
Add to `template.settings`:
```json
"index.lifecycle.name": "agent-insights-policy"
```
(mirrors how the joinability template references `agent-monitors-joinability-policy`; no
`rollover_alias` since there is no rollover.)

### 4. `docker/elasticsearch/monitors-slm-health-index-template.json` — add lifecycle ref
```json
"index.lifecycle.name": "agent-monitors-slm-health-policy"
```

### 5. `scripts/setup-elasticsearch.sh` — register both policies
Insert each policy PUT immediately **before** its template PUT (locality; the policy ref on a
template is resolved at index-creation, but PUT-first keeps the script readable):
- `agent-insights-policy` → `/_ilm/policy/agent-insights-policy` before the §3b insights template PUT.
- `agent-monitors-slm-health-policy` → `/_ilm/policy/agent-monitors-slm-health-policy` before the §3c
  slm-health template PUT.

### 6. Writers — repartition daily → monthly
- `src/personal_agent/insights/engine.py:1120` — `now.strftime("%Y-%m-%d")` → `now.strftime("%Y-%m")`
  (index `agent-insights-YYYY-MM`).
- `src/personal_agent/observability/slm_health/sink.py:35` — `strftime('%Y.%m.%d')` →
  `strftime('%Y.%m')` (index `agent-monitors-slm-health-YYYY.MM`); update the `index_name_for`
  docstring (`YYYY.MM.DD` → `YYYY.MM`, "daily" → "monthly").

### 7. Tests
`tests/scripts/test_es_templates.py` — new static tests (no live cluster):
- both ILM policy files are valid JSON with a `delete` phase carrying `actions.delete` and a
  `min_age` matching `_meta.retention_days` (365d insights / 90d slm-health);
- neither policy has a `rollover` action (daily/monthly-index constraint);
- `insights-index-template.json` / `monitors-slm-health-index-template.json` set
  `index.lifecycle.name` to the matching policy name;
- `setup-elasticsearch.sh` PUTs both `/_ilm/policy/<name>` (registration parity).

`tests/observability/test_slm_health_sink.py` — update the two assertions pinning
`agent-monitors-slm-health-2026.06.02` → `agent-monitors-slm-health-2026.06` (monthly).

New `tests/insights/` (or extend existing engine tests) — assert the insights index name is
`agent-insights-YYYY-MM` (monthly) via the indexing path.

## Verification (TDD order)
1. Add tests → `make test-file FILE=tests/scripts/test_es_templates.py` → **fail** (no policy files,
   no lifecycle ref).
2. Author policies + template refs + script registration → tests **pass**.
3. `python -c "import json; json.load(open(...))"` per new policy file; full file
   `make test-file FILE=tests/scripts/test_es_templates.py`.
4. `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
5. `make test` (no Python source touched, but run the suite for regression safety).

## Post-deploy (Linear comment for master — NOT in PR checklist)
- Re-run `scripts/setup-elasticsearch.sh` (or `ENV=cloud`) so ES registers the two new policies and
  the updated templates. `_meta` source-of-truth = repo files; live ES is re-derived by re-running
  setup.
- Templates attach the policy only to **newly-created** daily indices. New days are managed
  automatically; no action needed for go-forward management.
- ⚠️ **Back-attach is optional and risky (codex review #1/#5).** Manually enrolling an *existing*
  index via `PUT <index>/_settings {"index.lifecycle.name": ...}` evaluates `min_age` against that
  index's **creation date** — so any existing `agent-insights-*` / `agent-monitors-slm-health-*`
  index already older than the `delete` `min_age` (180d / 90d) is **deleted immediately**, and any
  older than the warm `min_age` (7d) triggers an immediate `forcemerge`. The safe default is to
  **not back-attach** and let ILM govern new indices only. If master does back-attach, gate it on a
  per-index age check (`GET <pattern>/_settings?filter_path=**.creation_date`) and exclude any index
  older than the retention window — this is the "no historical rows dropped" guarantee.
- Verify with `GET <index>/_ilm/explain` that new indices are managed by the right policy.

## Halt-condition check
JSON policy files + template settings + shell registration + static tests — **one phase, one PR**.
No historical rows dropped (ILM only governs future deletion at `min_age`; no immediate purge). No
ADR-phase bundling. No Python source touched → no mypy regression expected.
