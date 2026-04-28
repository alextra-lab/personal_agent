# Expected Outputs — PIVOT-3 Eval Prompts

**Created**: 2026-04-28 (FRE-276)
**Purpose**: Human grader reference for the Quality column in report.md
**Scale**: ✅ correct answer + correct tool approach · ⚠️ answer partially correct or wrong tool · ❌ wrong answer / loop / timeout

---

## Grading principles

1. **Correct answer** matters more than exact wording. A number within 10% is ✅.
2. **Tool choice** is secondary unless it reveals a fundamental error (e.g. querying non-existent index).
3. **Treatment** (primitives) may use bash/curl/run_python; **control** uses curated tools. Both are valid if the answer is correct.
4. **Timeout / loop** always = ❌. With FRE-279 in place, es-04 should terminate within 5 consecutive calls.
5. **"No data found"** without a real attempt = ❌. "No data found" after a valid query returning empty = ⚠️.

---

## ES category

### es-01 — Errors in last hour

**Prompt**: "Show me errors in the last hour."

**Data**: `agent-logs-*`, field `level == "ERROR"`, time filter `@timestamp > NOW()-1hour`

**✅ Correct**:
- Returns a count (may be 0 if no errors occurred — that's a valid answer)
- Or returns a list of error messages with context
- Uses `agent-logs-*` wildcard (not `agent-events-*` etc.)

**⚠️ Partial**:
- Returns errors but from a different time window
- Queries the right index but misreads the response format

**❌ Wrong**:
- "No index found" / 404 (wrong index name)
- Loops or times out

---

### es-02 — query_elasticsearch call count today

**Prompt**: "How many times did the agent call query_elasticsearch today?"

**Data**: `agent-logs-*`, field `event_type == "tool_call_started"` AND `tool_name == "query_elasticsearch"`, time filter last day. Expected count: ~47 as of 2026-04-28 (will grow during eval).

**✅ Correct**:
- Returns a specific non-zero count
- Breaks it down further (e.g. by session, by hour) — bonus

**⚠️ Partial**:
- Returns "0 events" (may be correct if eval hadn't run yet, but suspicious)
- Queries for all tool calls without filtering by tool_name

**❌ Wrong**:
- "request.completed events" — wrong event type from old es-02 prompt (should not appear with new prompt)
- 404 / "no index"
- Timeout

---

### es-03 — p95 LLM latency

**Prompt**: "What's the p95 LLM call latency over the last 24 hours?"

**Data**: `agent-logs-*`, `event_type == "litellm_request_complete"`, `elapsed_s` field. As of 2026-04-28 p95 ≈ 7.7s.

**✅ Correct**:
- Returns a latency number in seconds (range 3–30s is plausible)
- Uses PERCENTILE(elapsed_s, 95) or equivalent aggregation

**⚠️ Partial**:
- Returns mean latency instead of p95
- Uses `duration_ms` / 1000 correctly (same field, different unit — acceptable)
- Returns p95 from a different time window

**❌ Wrong**:
- "No latency data" (elapsed_s confirmed present)
- Queries `interactions` table (PostgreSQL — that table doesn't exist)
- Timeout

---

### es-04 — Loop gate trace

**Prompt**: "Find a trace from the past week where the agent hit the consecutive loop gate. What tool was involved and how many times was it called?"

**Data**: `agent-logs-*`, `event_type == "tool_loop_gate"`, `decision IN ("warn_consecutive","block_consecutive")`, last 7 days. As of 2026-04-28: 136 warn + 85 block events across multiple traces.

**✅ Correct**:
- Returns at least one `trace_id` and tool name (likely `query_elasticsearch`)
- Reports `consecutive_count` from the gate event
- Terminates within 5 consecutive ES calls (FRE-279 terminal active)

**⚠️ Partial**:
- Returns traces but misidentifies the tool
- Takes multiple ES queries to find the right field names (but still terminates)

**❌ Wrong**:
- Loops indefinitely / 120s timeout (FRE-279 regression)
- "No loop traces found" without attempt
- Reports `web_search` without checking data (the old hallucinated answer)

---

## Fetch category

### fetch-01 — example.com/api/status

**✅ Correct**: Returns whatever example.com responds (404 HTML, redirect, or actual content — any HTTP response is ✅)
**❌ Wrong**: Connection error to a well-known public domain

---

### fetch-02 — anthropic-sdk-python README

**✅ Correct**: Summary mentions Python SDK, Claude, Anthropic API
**⚠️ Partial**: Very short summary with correct gist
**❌ Wrong**: Hallucinates content; cannot fetch; returns GitHub rate limit error without fallback

---

### fetch-03 — Anthropic pricing

**✅ Correct**: References Claude model names and pricing tier (even if approximate)
**⚠️ Partial**: Cannot parse dynamic JS pricing page; reports "check the website" with relevant model names
**❌ Wrong**: Cannot fetch; hallucinates prices; completely wrong content

---

## List directory category

*All paths confirmed present in seshat-gateway container as of 2026-04-28.*

### ls-01 — /app/config

**✅ Correct**: Lists files in /app/config — expect governance/, models.*.yaml, etc.
**❌ Wrong**: "Path not found" / "no such directory"

### ls-02 — /app/src/personal_agent/tools

**✅ Correct**: Lists Python files — executor.py, web.py, fetch.py, elasticsearch.py, primitives/, etc.
**❌ Wrong**: "Path not found"

### ls-03 — YAML count in /app/config

**✅ Correct**: Specific number ≥ 1 (expect 5-10 YAML files)
**⚠️ Partial**: Lists files but doesn't count
**❌ Wrong**: 0 without attempt; "path not found"

---

## System metrics category

### metrics-01 — CPU load

**✅ Correct**: Load average ×.×× or CPU% — any numeric value
**❌ Wrong**: "CPU unavailable"; "I cannot check system metrics"

### metrics-02 — Agent memory usage

**✅ Correct**: ~300-400 MB RSS for uvicorn process, or the %MEM figure
**⚠️ Partial**: Reports total system memory (23GB) — technically present but not "agent service" memory
**❌ Wrong**: "Cannot check"; no process-level breakdown

### metrics-03 — Disk space

**✅ Correct**: "Not critically low" — overlay fs ~50GB used of ~200GB (27% as of eval stack)
**⚠️ Partial**: Reports numbers but no verdict (grader judges)
**❌ Wrong**: "Disk check unavailable"

---

## Diagnostics category

### diag-01 — Top 10 processes by memory

**✅ Correct**: List with uvicorn/python at top (~300MB RSS), PID, command names
**❌ Wrong**: "Process list unavailable"; empty; timeout

### diag-02 — Listening ports

**✅ Correct**: At least port 9001 listed (uvicorn listener)
**❌ Wrong**: "Cannot check ports"; empty; timeout

### diag-03 — System activity (load/swap/IO)

**✅ Correct**: Load numbers + "swap: 0" + IO figures from bounded vmstat or iostat snapshot
**⚠️ Partial**: Only reports one of load/swap/IO
**❌ Wrong**: Unbounded `vmstat` timeout (30s bash timeout fires); "unavailable"

> **Grader note on diag-03**: The prompt says "last 5 minutes" but the bash tool has a 30s timeout. A correct answer uses `vmstat 1 3` (3s snapshot) and frames it as "recent activity, not 5-minute average." The framing is acceptable — the data is correct.

---

## Infrastructure health category

### infra-01 — Full health check

**✅ Correct**: All 7 services reachable (postgres, neo4j_bolt, neo4j_http, elasticsearch, redis, embeddings, reranker), all_reachable: true
**⚠️ Partial**: Checks 4-6 services correctly
**❌ Wrong**: DNS failures (running from host, not container); "I cannot check"; only checks 1-2

### infra-02 — Postgres reachable

**✅ Correct**: "reachable: true" / "yes, postgres is up" — TCP to postgres:5432 succeeds
**❌ Wrong**: DNS failure; "cannot check from this environment"

### infra-03 — Neo4j and Elasticsearch

**✅ Correct**: Both reachable — neo4j HTTP 200, elasticsearch HTTP 200
**❌ Wrong**: DNS failure; only checks one; "cannot verify"

### infra-04 — All services healthy

**✅ Correct**: "Yes, all backend services are healthy" with evidence (all_reachable: true for all 7)
**⚠️ Partial**: Checks most but misses 1-2 services
**❌ Wrong**: DNS failures; partial check with wrong conclusion

---

## Gate summary

**Minimum pass threshold** (from side plan): ≥ 17/20 prompts ✅ (85%)

Per Wave E gate decision criteria:
- ≥ 17/19 ✅ AND token cost ratio ≤ 1.5× → PIVOT-4 cleared
- ≥ 17/19 ✅ AND cost ratio > 1.5× → partial PIVOT-4 (per-category verdict)
- < 17/19 ✅ → per-category deprecation only where treatment ≥ control
