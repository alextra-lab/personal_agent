# Recovery Survey — survey-2026-05-05

**Generated**: 2026-05-05T14:35:13.253656+00:00
**Window**: last 7 day(s)

Source plan: `docs/plans/2026-05-05-agent-self-diagnosis-recovery-execution-waves-0-2.md` (Wave 1.1).

## A. Pipeline-flow counts

| event_type | count |
|---|---:|
| `request.captured` | 207 |
| `entity_extraction_started` | 1131 |
| `entity_extraction_completed` | 110 |
| `entity_extraction_failed` | 1021 |
| `entity_extraction_timeout` | 0 |
| `entity_extraction_json_parse_failed` | 0 |
| `elasticsearch_logging_enabled` | 34 |
| `memory_service_initialized` | 19 |
| `event_bus_ready` | 19 |

- **capture → extraction gap**: 0 (non-trivial → scheduler is dropping work)
- **extraction success ratio**: 9.73%
- **extraction failure ratio**: 90.27%

## B. Quality reports (existing ConsolidationQualityMonitor)

### Entity extraction quality

- conversations: 0
- entities: 1134
- entities/conversation ratio: 0.000
- duplicate entity count: 43
- duplicate rate: 3.792%
- extraction started (window): 1131
- extraction failed  (window): 1021
- extraction failure rate: 90.274%
- name length distribution: {'min': 3.0, 'avg': 18.00970017636684, 'p50': 17.0, 'p90': 28.0, 'max': 55.0}

### Graph health

- total nodes: 2853
- entity nodes: 1134
- conversation nodes: 0
- relationship count: 2975
- relationship density: 2.623
- orphaned entities: 174
- orphaned entity rate: 15.344%
- clustered entity rate: 0.000%
- max temporal gap (h): 0.00

### Threshold flags

- ⚠ entities/conversation < 1.0 — under-extraction.
- ⚠ extraction failure rate > 20% — model crashing.

## C. Model identity audit

| role | model |
|---|---|
| `entity_extraction_role` | `gpt-5.4-nano` |
| `captains_log_role` | `gpt-5.4-nano` |
| `insights_role` | `gpt-5.4-nano` |

⚠ **All roles share one model** — single point of failure. If that model degrades, three pipelines fail simultaneously. See FRE-319 follow-up for the broader audit.

### Embedding model

- id: `Qwen/Qwen3-Embedding-0.6B`
- endpoint: `http://localhost:8503/v1`
- context_length: 32768

## D. Embedding health probe

- model: `Qwen/Qwen3-Embedding-0.6B`
- endpoint: `http://localhost:8503/v1`
- expected dimensions: 1024

### Live probe (3 test strings)

- returned dimensions: [1024, 1024, 1024]
- all-zero: False
- pairwise similarities: [0.395, 0.3405, 0.2226]
- mean pairwise similarity: 0.3194
✓ Live embeddings non-degenerate.

### Elasticsearch

- `proactive_memory_suggest_empty` events (last 7d): 6 (superset; filter by `reason=zero_embedding` in Kibana to isolate the embedding-degradation slice)
⚠ Non-zero empty-suggest events. Drill into logs by `reason` to see whether `zero_embedding` (model degraded) or `no_raw_rows` (no semantic neighbours) dominates.

### Neo4j sample (≤50 Entity embeddings)

- sampled: 50
- null or all-zero: 0
- mean pairwise similarity: 0.4723
✓ Stored embeddings non-degenerate.

## Likely localized failures

- Extraction failing: success_ratio=9.73%.
- All extraction-side roles share one model — failure of that model cascades to entity_extraction, captains_log, and insights.
