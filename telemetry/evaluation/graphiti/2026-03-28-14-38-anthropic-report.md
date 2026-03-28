## Experiment Run: 2026-03-28-14-38-anthropic

**Date:** 2026-03-28 15:54 UTC
**LLM:** anthropic (medium: claude-haiku-4-5-20251001, small: claude-haiku-4-5-20251001)
**Embedder:** text-embedding-3-small
**Episodes:** 50 quality, 500 scaling

### Scenario 1: Episodic Memory — Store + Retrieve

| Metric | Seshat | Graphiti |
|--------|--------|---------|
| Ingest p50 (ms) | 0.97 | 8224.08 |
| Query p50 (ms) | 2.41 | 325.89 |
| Query p95 (ms) | 11.4 | 357.02 |
| Avg Precision | 0.0 | 0.7 |
| Avg Recall | 0.0 | 0.7 |

### Scenario 4: Entity Deduplication

| Metric | Seshat | Graphiti |
|--------|--------|---------|
| Raw Mentions | 40 | 40 |
| Unique Entities | 467 | 10 |
| Dedup Ratio | 11.675 | 0.25 |
| Expected Canonical | 10 | 10 |

### Scenario 6: Scaling

| Checkpoint | Seshat Ingest (ms) | Graphiti Ingest (ms) | Seshat Query p50 | Graphiti Query p50 |
|------------|-------------------|---------------------|-----------------|-------------------|
| 100 | 6.01 | 7580.24 | 3.25 | 183.55 |
| 250 | 6.5 | 7661.77 | 3.97 | 186.75 |
| 500 | 6.44 | 7484.15 | 3.78 | 191.93 |

### Cost Comparison

| Metric | Seshat | Graphiti |
|--------|--------|---------|
| LLM Input Tokens | 0 | 0 |
| LLM Output Tokens | 0 | 0 |
| Estimated Cost (USD) | $0.0 | $0.0 |
