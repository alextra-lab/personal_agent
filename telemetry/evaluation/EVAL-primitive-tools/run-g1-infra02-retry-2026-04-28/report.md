# FRE-262 PIVOT-3 Evaluation Report

Generated: 2026-04-28T17:19:05.690060+00:00
Control URL: http://localhost:9002
Treatment URL: http://localhost:9003

## Results

| # | ID | Category | Prompt | Ctrl response (truncated) | Trt response (truncated) | Ctrl tok | Trt tok | Ctrl turns | Trt turns | Cache hit % | Quality |
|---|----|-----------|--------------------|--------------------------|--------------------------|---------|---------|------------|-----------|-------------|---------|
| 1 | infra-02 | infrastructure-health | Is Postgres reachable? | Yes! ✅ **Postgres is reachable** at `postgres:5432`.

All other core services are up as well — Neo4j, Elasticsearch, and Redis are all reachable. | ✅ **Postgres is reachable.** The query `SELECT 1` returned successfully, confirming a live connection to the database. | 10905 | 13165 | 2 | 2 | ctrl 100% / trt 86% | <!-- ✅/⚠️/❌ --> |

## Gate Criteria

- [ ] Primitive success rate >= curated success rate on >= 17/20 prompts
- [ ] Zero "could not find primitive equivalent" failures (dead-end failures)

## Token + Turns Summary

| Metric | Control | Treatment |
|--------|---------|-----------|
| Total tokens | 10,905 | 13,165 |
| Mean turns / prompt | 2.0 | 2.0 |
| Cache hit % | 100% | 86% |
| Total wall clock | 4s | 5s |

## Per-Category Gate

If primitive < curated for a specific category, move that category's tools to PIVOT-4 keep list.

## Grading Key

Fill in the Quality column:  ✅ = correct/complete  ⚠️ = partial/extra turns  ❌ = wrong/missing

Session IDs and full token breakdowns are in results.json — use them to look up traces in Kibana.
