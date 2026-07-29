# FRE-1038 — Telemetry naming census (reproducible measurements)

**Date:** 2026-07-29 · **Backs:** ADR-0128 · **Regenerate:** `python3 scripts/audit/fre1038_naming_census.py`

Every number quoted in ADR-0128 is produced by the script above, which is
committed alongside this output. Re-run it and diff; do not copy these
figures forward on trust. Live sections state explicitly when Elasticsearch
was unreachable, so an unmeasured section can never read as a zero.

---

## 1. Date-field census — committed templates

Selection is by declared ``type == 'date'``. **Never by field name** — a
name-substring filter misses `ts` and returns a short, confident, wrong answer.

| Template | date-typed properties |
|---|---|
| `captains-captures-index-template.json` | `timestamp` |
| `captains-funnel-events-index-template.json` | `@timestamp` |
| `captains-reflections-index-template.json` | `timestamp` |
| `captains-subagents-index-template.json` | `timestamp` |
| `index-template.json` | `@timestamp`, `window_start` |
| `insights-index-template.json` | `timestamp` |
| `monitors-cache-reset-cadence-index-template.json` | `@timestamp` |
| `monitors-joinability-index-template.json` | `started_at` |
| `monitors-joinability-substrate-index-template.json` | `started_at` |
| `monitors-projector-health-index-template.json` | `@timestamp` |
| `monitors-slm-health-index-template.json` | `probed_at` |
| `slm-requests-index-template.json` | `ts` |
| `topology-index-template.json` | `@timestamp` |
| `user-turn-ratings-index-template.json` | `rated_at` |

**14 templates. Record-timestamp spellings: 6** — `@timestamp` (5), `timestamp` (4), `started_at` (2), `probed_at` (1), `ts` (1), `rated_at` (1).

`agent-logs` declares a second date property, `window_start` — a payload
field, not the record timestamp.

## 2. Field-sharing distribution — committed templates

- total property declarations: **363**
- distinct field names: **234**
- appearing in exactly one family: **175**
- crossing families (>=2): **59**
- appearing in >=3 families: **33**

| families containing the name | count of such names |
|---|---|
| 1 | 175 |
| 2 | 26 |
| 3 | 18 |
| 4 | 6 |
| 5 | 4 |
| 6 | 3 |
| 9 | 1 |
| 11 | 1 |

Most widely shared: `trace_id` (11 families), `session_id` (9 families), `status` (6 families), `duration_ms` (6 families).

## 3. Live corpus — per-family document counts

Method: `_count` per family pattern. **Not `_cat/indices`**, whose `docs.count`
inflates via nested sub-documents (up to 4.5x on this cluster).

| Family pattern | date field | documents |
|---|---|---|
| `agent-logs-*` | `@timestamp` | 3,206,830 |
| `agent-topology-*` | `@timestamp` | 278 |
| `agent-monitors-projector-health-*` | `@timestamp` | 223 |
| `agent-captains-funnel-events-*` | `@timestamp` | 0 |
| `agent-monitors-cache-reset-cadence-*` | `@timestamp` | 0 |
| `slm-requests-*` | `ts` | 1,474 |
| `agent-captains-captures-2*` | `timestamp` | 1,896 |
| `agent-captains-captures-subagents*` | `timestamp` | 69 |
| `agent-captains-reflections-*` | `timestamp` | 1,966 |
| `agent-insights-*` | `timestamp` | 3,705 |
| `agent-monitors-joinability-2*` | `started_at` | 1,736 |
| `agent-monitors-joinability-substrate-*` | `started_at` | 9,442 |
| `agent-monitors-slm-health-*` | `probed_at` | 15,894 |
| `user-turn-ratings-*` | `rated_at` | 1,959 |

- **total corpus: 3,245,472 documents**
- **on a non-`@timestamp` spelling: 38,141 (1.18%)**
- **`agent-logs` share of corpus: 98.81%**

## 4. Identity-field presence on the highest-volume family

Method: `exists` query per field over all `agent-logs-*`. Total: **3,206,830** (all-time).

| Field | present | share |
|---|---|---|
| `@timestamp` | 3,206,830 | 100.0% |
| `event_type` | 3,206,830 | 100.0% |
| `trace_id` | 365,157 | 11.4% |
| `session_id` | 67,562 | 2.1% |
| `user_id` | 36,179 | 1.1% |
| `span_id` | 33,180 | 1.0% |

## 5. Pre-change daily volume baseline (ADR-0128 AC-3)

| day | documents |
|---|---|
| 2026-07-22 | 48,722 |
| 2026-07-23 | 70,272 |
| 2026-07-24 | 70,094 |
| 2026-07-25 | 72,665 |
| 2026-07-26 | 29,053 |
| 2026-07-27 | 66,730 |
| 2026-07-28 | 71,698 |
| 2026-07-29 | 20,876 |

- **trailing 7-day daily mean: 56,264 documents/day** — this, not the
  all-time total, is AC-3's volume baseline.

## 6. Cluster shape

- active primary shards: **594** (single node; 1,000-per-node ceiling)
- cluster status: green

