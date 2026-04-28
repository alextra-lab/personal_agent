# FRE-262 PIVOT-3 Evaluation Report

Generated: 2026-04-28T17:15:38.990661+00:00
Control URL: http://localhost:9002
Treatment URL: http://localhost:9003

## Results

| # | ID | Category | Prompt | Ctrl response (truncated) | Trt response (truncated) | Ctrl tok | Trt tok | Ctrl turns | Trt turns | Cache hit % | Quality |
|---|----|-----------|--------------------|--------------------------|--------------------------|---------|---------|------------|-----------|-------------|---------|
| 1 | metrics-02 | system-metrics | How much memory is the agent service using right now? | Here's a quick look at the current memory usage for the system hosting the agent service:

&#124; Metric &#124; Value &#124;
&#124;---&#124;---&#124;
&#124; **Memory Used** &#124; **33.1%** of total &#124;
&#124; **Total RAM** &#124; 22.93 GB &#124;
&#124; **Used R... | Here's the current memory usage for the agent service:

&#124; PID &#124; RSS &#124; % Mem &#124; Process &#124;
&#124;-----&#124;-----&#124;-------&#124;---------&#124;
&#124; 19 &#124; **344,072 KB (~336 MB)** &#124; 1.4% &#124; `uvicorn` (main app worker) &#124;
&#124; 1 &#124; 82,... | 27280 | 14200 | 5 | 2 | ctrl 75% / trt 67% | <!-- ✅/⚠️/❌ --> |
| 2 | diag-01 | system-diagnostics | List the top 10 processes by memory usage. | Here are the top processes by memory usage on this host:

&#124; Rank &#124; User &#124; PID &#124; %CPU &#124; %MEM &#124; VSZ (KB) &#124; RSS (KB) &#124; Command &#124;
&#124;------&#124;------&#124;-----&#124;------&#124;------&#124;----------&#124;----------&#124;---------&#124;
&#124; 1 &#124; ... | Here are the **top processes by memory usage** on this container:

&#124; Rank &#124; PID &#124; User &#124; %CPU &#124; %MEM &#124; VSZ &#124; RSS &#124; Command &#124;
&#124;------&#124;-----&#124;------&#124;------&#124;------&#124;-----&#124;-----&#124;---------&#124;
&#124; 1 &#124; 19 &#124; root &#124;... | 11094 | 13192 | 2 | 2 | ctrl 100% / trt 86% | <!-- ✅/⚠️/❌ --> |
| 3 | infra-02 | infrastructure-health | Is Postgres reachable? | Yes, **Postgres is reachable!** ✅

It's responding on `postgres:5432` with no issues. All other core services are up as well (Neo4j, Elasticsearch, Redis). | **Postgres is not reachable.** ❌

The connection attempt failed with:
> `connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: No such file or directory`

This means `$AGENT_DATAB... | 10906 | 13284 | 2 | 2 | ctrl 100% / trt 86% | <!-- ✅/⚠️/❌ --> |
| 4 | infra-03 | infrastructure-health | Are Neo4j and Elasticsearch both up? | Both are up! Here's a quick summary:

&#124; Service &#124; Status &#124; Details &#124;
&#124;---&#124;---&#124;---&#124;
&#124; **Neo4j** &#124; ✅ Up &#124; Bolt (7687) + HTTP (7474) both reachable; v5.26.24 Community Edition &#124;
&#124; **Elasticsearch** &#124; ✅ U... | Both services are up:

&#124; Service &#124; Status &#124; Details &#124;
&#124;---&#124;---&#124;---&#124;
&#124; **Neo4j** &#124; ✅ Up &#124; Community Edition v5.26.24, reachable on HTTP (7474) and Bolt (7687) &#124;
&#124; **Elasticsearch** &#124; ✅ Up &#124; Cluster `do... | 11027 | 13524 | 2 | 2 | ctrl 100% / trt 100% | <!-- ✅/⚠️/❌ --> |

## Gate Criteria

- [ ] Primitive success rate >= curated success rate on >= 17/20 prompts
- [ ] Zero "could not find primitive equivalent" failures (dead-end failures)

## Token + Turns Summary

| Metric | Control | Treatment |
|--------|---------|-----------|
| Total tokens | 60,307 | 54,200 |
| Mean turns / prompt | 2.8 | 2.0 |
| Cache hit % | 90% | 86% |
| Total wall clock | 34s | 32s |

## Per-Category Gate

If primitive < curated for a specific category, move that category's tools to PIVOT-4 keep list.

## Grading Key

Fill in the Quality column:  ✅ = correct/complete  ⚠️ = partial/extra turns  ❌ = wrong/missing

Session IDs and full token breakdowns are in results.json — use them to look up traces in Kibana.
