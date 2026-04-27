# FRE-262 PIVOT-3 Evaluation Report

Generated: 2026-04-27T17:04:04.046693+00:00
Control URL: http://localhost:9002
Treatment URL: http://localhost:9003

## Results

| # | ID | Category | Prompt | Ctrl response (truncated) | Trt response (truncated) | Quality |
|---|----|-----------|--------------------|--------------------------|--------------------------|---------|
| 1 | es-01 | query-elasticsearch | Show me errors in the last hour. | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 2 | es-02 | query-elasticsearch | How many request.completed events fired today, by task_type? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 3 | es-03 | query-elasticsearch | What's the p95 LLM call latency over the last 24 hours? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 4 | es-04 | query-elasticsearch | Find the trace where the agent looped on web_search this wee... | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 5 | fetch-01 | fetch-url | Fetch https://example.com/api/status and tell me what it say... | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 6 | fetch-02 | fetch-url | Read the README on https://github.com/anthropics/anthropic-s... | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 7 | fetch-03 | fetch-url | What's the current Anthropic pricing? Check https://www.anth... | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 8 | ls-01 | list-directory | List files in /app/config. | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 9 | ls-02 | list-directory | What's in the /app/src/personal_agent/tools folder? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 10 | ls-03 | list-directory | How many YAML files are under /app/config? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 11 | metrics-01 | system-metrics | What's the current CPU load? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 12 | metrics-02 | system-metrics | How much memory is the agent service using right now? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 13 | metrics-03 | system-metrics | Is disk space getting low? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 14 | diag-01 | system-diagnostics | List the top 10 processes by memory usage. | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 15 | diag-02 | system-diagnostics | Which container ports are listening? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 16 | diag-03 | system-diagnostics | Show me what the system has been doing for the last 5 minute... | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 17 | infra-01 | infrastructure-health | Check infrastructure health. | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 18 | infra-02 | infrastructure-health | Is Postgres reachable? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 19 | infra-03 | infrastructure-health | Are Neo4j and Elasticsearch both up? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |
| 20 | infra-04 | infrastructure-health | All backend services healthy right now? | ERROR: 422 Unprocessable Entity | ERROR: 422 Unprocessable Entity | <!-- check/warn/fail --> |

## Gate Criteria

- [ ] Primitive success rate >= curated success rate on >= 17/20 prompts
- [ ] Zero "could not find primitive equivalent" failures (dead-end failures)

## Per-Category Gate

If primitive < curated for a specific category, move that category's tools to PIVOT-4 keep list.

## Grading Key

Fill in the Quality column:  ✅ = correct/complete  ⚠️ = partial/extra turns  ❌ = wrong/missing

Session IDs are in results.json — use them to look up full traces in Elasticsearch / Kibana.
