# FRE-1038 — Telemetry naming census (reproducible measurements)

**Date:** 2026-07-29 · **Backs:** ADR-0128 · **Source:** live local Elasticsearch (:9200) + committed templates

Every number quoted in ADR-0128 is produced by the commands below. Re-run this file's
commands to re-derive them; do not copy the figures forward without re-running.

---

## 1. Date-field census — committed templates

Method: parse every `docker/elasticsearch/*index-template.json`, select mapping properties
whose declared `type` is `date`. **No name filter** — an earlier pass filtered on name
substrings and silently missed `ts`, which is why this is type-based.

| Template | Family | date-typed properties |
|---|---|---|
| `captains-captures-index-template.json` | `agent-captains-captures-*` | `timestamp` |
| `captains-funnel-events-index-template.json` | `agent-captains-funnel-events-*` | `@timestamp` |
| `captains-reflections-index-template.json` | `agent-captains-reflections-*` | `timestamp` |
| `captains-subagents-index-template.json` | `agent-captains-captures-subagents*` | `timestamp` |
| `index-template.json` | `agent-logs-*` | `@timestamp`, `window_start` |
| `insights-index-template.json` | `agent-insights-*` | `timestamp` |
| `monitors-cache-reset-cadence-index-template.json` | `agent-monitors-cache-reset-cadence-*` | `@timestamp` |
| `monitors-joinability-index-template.json` | `agent-monitors-joinability-*` | `started_at` |
| `monitors-joinability-substrate-index-template.json` | `agent-monitors-joinability-substrate-*` | `started_at` |
| `monitors-projector-health-index-template.json` | `agent-monitors-projector-health-*` | `@timestamp` |
| `monitors-slm-health-index-template.json` | `agent-monitors-slm-health-*` | `probed_at` |
| `slm-requests-index-template.json` | `slm-requests-*` | `ts` |
| `topology-index-template.json` | `agent-topology-*` | `@timestamp` |
| `user-turn-ratings-index-template.json` | `user-turn-ratings-*` | `rated_at` |

**14 templates. Record-timestamp spellings: 6** — `@timestamp` (5), `timestamp` (4), `started_at` (2), `probed_at` (1), `ts` (1), `rated_at` (1).

Note: `index-template.json` (agent-logs) declares a second date property `window_start`,
which is a payload field, not the record timestamp.

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

Method: `_count` per family pattern. **Not `_cat/indices`**, whose `docs.count` inflates
via nested sub-documents (up to 4.5x on this cluster).

| Family pattern | date field | documents |
|---|---|---|
| `agent-logs-*` | `@timestamp` | 3,206,232 |
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
| `agent-monitors-slm-health-*` | `probed_at` | 15,891 |
| `user-turn-ratings-*` | `rated_at` | 1,959 |

- **total corpus: 3,244,871 documents**
- **on a non-`@timestamp` spelling: 38,138 (1.18%)**
- **`agent-logs` share of corpus: 98.81%**

## 4. Identity-field presence on the highest-volume family

Method: `exists` query per field over all `agent-logs-*`. Total documents: **3,206,232** (all-time).

| Field | present | share |
|---|---|---|
| `@timestamp` | 3,206,232 | 100.0% |
| `event_type` | 3,206,232 | 100.0% |
| `trace_id` | 365,117 | 11.4% |
| `session_id` | 67,562 | 2.1% |
| `user_id` | 36,179 | 1.1% |
| `span_id` | 33,180 | 1.0% |

FRE-1038 cites 16% / 9% / 9% for trace/session/user over a narrower window; the all-time
figures above are the ADR's baseline.

## 5. Cluster shape and index age distribution

- active primary shards: **594** (single node; 1,000-per-node ceiling)
- cluster status: green

