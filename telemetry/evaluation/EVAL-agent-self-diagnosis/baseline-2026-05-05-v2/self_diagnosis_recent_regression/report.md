# Prompt: `self_diagnosis_recent_regression`

_Ask the agent to investigate a known recent regression in its own pipeline. Tests whether self-diagnosis prompts route to source/log inspection rather than premature synthesis.
_

Tags: diagnostic, self-inspection

## Turn 1

- session_id: `335d75ea-54e1-4c57-bd10-c4b9c7284e62`
- trace_id:   `b6ce4de3-95dc-45ef-81f1-beab9fd46c1b`
- duration:   30.38s

**User**

```
A recent change to skill injection (FRE-282) may have affected your
ability to use bash and other primitive tools effectively. Inspect
your own source under src/personal_agent/orchestrator/skills.py and
report what skill injection currently does and which docs it
surfaces. Use read_file or bash if those tools are available.

```

**Assistant**

```
Here's a full report on the current state of skill injection in `skills.py`:

---

## What Skill Injection Does (Post-FRE-282)

**Strategy:** Intent-based injection. Instead of injecting all 9 skill docs (~8.7K tokens) on every request, the system now injects only `bash.md` (always) plus **one** additional skill doc matched by keyword (~2–3K tokens total). This reduces per-request cost overhead from 3–4× down to ~1.1–1.2×.

### Loading
- At import time, `_load_skill_cache()` reads all 9 `.md` files from `docs/skills/` into an in-memory dict (`_SKILL_CACHE`).
- Missing/unreadable files are logged as warnings and silently excluded.

### Routing
When a user message is provided, it's lowercased and checked against **7 keyword routes** (first match wins). Each route maps to one or two skill files.

### The 9 Skill Files Loaded

| # | File | Triggered By |
|---|------|-------------|
| 1 | **bash.md** | **Always injected** (base tool reference) |
| 2 | read-write.md | Directory/filesystem queries (secondary, when matched) |
| 3 | run-python.md | Explicit Python scripting |
| 4 | query-elasticsearch.md | Agent-log/trace/Elasticsearch query keywords |
| 5 | fetch-url.md | URL fetching / web keywords |
| 6 | list-directory.md | Directory/filesystem queries (primary) |
| 7 | system-metrics.md | CPU/memory/disk keywords |
| 8 | system-diagnostics.md | Process/port/IO diagnostics keywords |
| 9 | infrastructure-health.md | Service health/reachability keywords |

### Keyword Routes (in priority order)

1. **Elasticsearch/telemetry** — keywords: `agent-log`, `trace_id`, `kibana`, `query_elasticsearch`, `loop gate`, `litellm`, `tool_call`, `last hour`, `p95`, `latency`, `errors in the`, `esql`, `agent-logs`, `loop trace`, `warn_consecutive`, `block_consecutive`, `query elasticsearch`, `search elasticsearch`
   → surfaces: `query-elasticsearch.md`

2. **URL fetching** — keywords: `fetch `, `https://`, `http://`, `readme on`, `github.com`, `anthropic.com`, `current pricing`, `what's on the page`, `check the url`
   → surfaces: `fetch-url.md`

3. **Directory/filesystem** — keywords: `list files`, `files in /`, `what's in /`, `yaml files`, `python files`, `how many files`, `under /app`, `in /app/`, `/app/config`, `/app/src`, `directory`, `folder /`, `how many yaml`, `how many python`
   → surfaces: `list-directory.md`, `read-write.md`

4. **System metrics** — keywords: `cpu load`, `cpu usage`, `memory is the agent`, `disk space`, `disk usage`, `load average`, `how much memory`, `current cpu`, `is disk`
   → surfaces: `system-metrics.md`

5. **System diagnostics** — keywords: `top 10 process`, `processes by memory`, `listening ports`, `ports are listening`, `which ports`, `container ports`, `vmstat`, `system has been doing`, `load swap`, `io activity`, `network connections`, `iostat`
   → surfaces: `system-diagnostics.md`

6. **Infrastructure health** — keywords: `infrastructure health`, `services healthy`, `postgres reachable`, `neo4j`, `backend services`, `health check`, `all services`, `is postgres`, `are neo4j`, `infra health`, `reachable`, `check infrastructure`
   → surfaces: `infrastructure-health.md`

7. **Explicit Python** — keywords: `run python`, `python script`, `calculate using python`, `write a python`
   → surfaces: `run-python.md`

### Guard Rail
The entire mechanism is gated by `settings.prefer_primitives_enabled`. If that setting is `False`, `get_skill_block()` returns an empty string — no injection at all.

### Notable Design Decisions (from comments)
- Bare `"elasticsearch"` was **removed** from the ES route to avoid catching infra-health questions like "are Neo4j and Elasticsearch both up?"
- Bare `"memory usage"` was **removed** from the metrics route because "List top 10 processes by memory usage" is a diagnostics question, not a metrics question
- The header `SKILL_BLOCK_HEADER` is always prepended, describing the skill library as "reference material for using the `bash`, `read`, `write`, and `run_python` primitives"
```

**ES log summary**

- log_count: 72
- skill_block_size_tokens: None
- memory_context_size_tokens: None
- forced_synthesis_events: 0
- compression_events: 2
- capture_id: None
- extraction_outcome: None
- tool_calls: 1 requested / 1 executed (bash: 0)
- loop_gate_decisions: ['allow']

**Neo4j writes scoped to this trace_id**

- Turn nodes:        0
- Entity nodes:      0
- Relationships:     0

**Events by type (top 20)**

| event | count |
|---|---:|
| `request_trace_step` | 11 |
| `state_transition` | 5 |
| `model_call_started` | 4 |
| `model_call_completed` | 4 |
| `tools_passed_to_llm` | 2 |
| `llm_call_messages_debug` | 2 |
| `history_sanitised` | 2 |
| `chat_completions_payload` | 2 |
| `raw_llm_response` | 2 |
| `within_session_compression_completed` | 2 |
| `within_session_compression_recorded` | 2 |
| `request_received` | 1 |
| `intent_classified` | 1 |
| `recall_controller_skipped` | 1 |
| `proactive_memory_suggest_start` | 1 |
| `decomposition_assessed` | 1 |
| `proactive_memory_budget_trimmed` | 1 |
| `proactive_memory_suggest_complete` | 1 |
| `context_assembled` | 1 |
| `context_budget_applied` | 1 |
