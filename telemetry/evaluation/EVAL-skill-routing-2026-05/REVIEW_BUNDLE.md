# Skill-Routing Eval — Review Bundle

> Self-contained packet for an external SOTA LLM (ChatGPT, Gemini, Grok, second Claude) to critique. Copy-paste this whole document into a fresh chat with the prompt at the top.

---

## Prompt for the reviewing model (paste this first)

I built an LLM agent system. To prevent it from hallucinating tool arguments (e.g. wrong Elasticsearch index names), I added "skill docs" — markdown files describing how each tool should be used, plus YAML frontmatter that the agent's harness routes on.

I want **your unbiased opinion on the eval methodology, not the implementation**. Specifically:

1. **Are the 10 prompts in Section A representative of real agent traffic?** What's missing? What's over-tested? Are there obvious failure modes a router could pass while still being broken?
2. **Is the 6-cell matrix in Section B the right design for this question?** Should I drop a cell, add a cell, or change a dimension?
3. **Are the metrics in Section C measuring what I think they're measuring?** Specifically: `es_first_call_correct_rate`, `read_skill_invoked_rate`, `routing_call_rate`, `tool_iteration_limit_reached_rate`. Any blind spots?
4. **Is the harness in Section D introducing measurement bias?** Things like: trusting `trace_id` correlation in ES, the 5-second indexing wait, the per-prompt session reset semantics, the `--profile` flag.
5. **What false-positive results would I see if the system were broken in subtle ways?** (e.g. router silently failing closed; primary model compensating for a bad pre-flight; ES partial-indexing under load.)

Constraints on your answer: be terse. Bullet-point critique. No congratulations, no executive summary. Quote line numbers when you spot something concrete.

---

## Context (one paragraph)

The agent runs on a FastAPI service. A user message hits `/chat`, goes through a 7-stage pre-LLM gateway (intent classification, decomposition, context assembly), then reaches an "executor" that calls the primary LLM. The primary LLM has access to ~5 primitive tools (`bash`, `read`, `write`, `run_python`, `read_skill`). The agent decides which tools to use based on a system prompt that includes a "skill block". Three routing modes choose what goes in that block:

- **`keyword`** — match user message against `keywords:` lists in skill frontmatter, inject matching skill bodies (full content) into the system prompt.
- **`hybrid`** — same as keyword, plus a compact index of all skills appended at the end. The model can call `read_skill(name)` to pull any skill body lazy-loaded.
- **`model_decided`** — a separate routing-LLM (Haiku) sees the user message + compact index and returns a JSON list of relevant skill names. Those bodies are pre-loaded; primary model can still `read_skill` for more.

The eval runs each of 10 prompts × 6 cells (3 modes × 2 model profiles {local Qwen 35B, cloud Sonnet}) = 60 traces. Each trace produces ES logs that the analysis script aggregates into per-cell metrics.

The triggering incident: agent was asked "check the logs", routed to the wrong skill (bash, no ES guidance), confabulated index name `logs-*` instead of `agent-logs-*`, looped 25 iterations against an empty result set.

---

## Section A — Test prompts (`prompts.yaml`)

10 prompts. One incident-class (the original failure), 3 ES queries, 1 Neo4j, 2 system diagnostics, 1 infra health, 1 codebase search, 1 Python computation, 1 no-skill baseline.

```yaml
# Phase D — Skill Routing Eval Prompts
# 10 prompts covering the main skill surfaces.
# Each prompt is run once per cell (6 cells × 10 prompts = 60 total runs).
#
# Prompt design goals:
#   - Cover all skill docs that have keywords (ES, Neo4j, bash, metrics, etc.)
#   - Include the original triggering incident prompt (#1)
#   - Include prompts that should NOT need any skill (to measure false injection)
#   - Vary complexity: single-tool, multi-tool, multi-step

prompts:
  # --- Incident-class prompt (the triggering failure) ---
  - id: es_incident_class
    description: >
      The exact class of request that triggered the original agent diagnosis incident.
      The agent must query ES logs without hallucinating a wrong index name.
      Pass: first bash call uses agent-logs-* or guard fires (tool_call_blocked_known_bad_pattern).
    tags: [incident, telemetry, b5-guard]
    turns:
      - message: |
          Check the logs and show me any errors or warnings from the last 12 hours.
          I want to understand what has been going wrong with the agent recently.

  # --- ES telemetry queries ---
  - id: es_tool_error_analysis
    description: >
      Ask for a structured analysis of tool errors. Should route to ES skill.
    tags: [telemetry, es]
    turns:
      - message: |
          Look at the recent traces and tell me: which tools have been failing most
          often in the last 24 hours? Show me the top 3 by error count.

  - id: es_skill_routing_telemetry
    description: >
      Query the skill routing telemetry itself. Validates skill_index_assembled and
      skill_routing_call_completed events are visible in ES.
    tags: [telemetry, es, meta]
    turns:
      - message: |
          Show me the skill_index_assembled events from the last hour.
          What routing mode has been used and how many chars were injected per turn?

  # --- Neo4j knowledge graph ---
  - id: neo4j_entity_count
    description: >
      Direct Cypher query for entity counts. Should route to neo4j-direct skill.
      Tests whether the skill doc prevents wrong connection patterns.
    tags: [neo4j, memory]
    turns:
      - message: |
          Connect directly to the knowledge graph and tell me how many Entity nodes
          exist, and how many DISCUSSES relationships. Show me the 5 most recently
          created entities.

  # --- System diagnostics ---
  - id: system_metrics_snapshot
    description: >
      Ask for current resource utilization. Should route to system-metrics skill.
    tags: [diagnostics, metrics]
    turns:
      - message: |
          How much memory is the agent currently using? What is the CPU load
          and how much disk space is left?

  - id: process_and_ports
    description: >
      Ask for process and port information. Should route to system-diagnostics skill.
    tags: [diagnostics]
    turns:
      - message: |
          What processes are consuming the most memory right now?
          Which ports is the agent listening on?

  # --- Infrastructure health ---
  - id: infra_health_check
    description: >
      Probe all backend services. Should route to infrastructure-health skill.
    tags: [infra, health]
    turns:
      - message: |
          Run a health check on all backend services — Postgres, Elasticsearch,
          Neo4j, Redis. Which ones are reachable and which are not?

  # --- Filesystem / codebase ---
  - id: codebase_search
    description: >
      Search the codebase for a specific symbol. Should route to bash + list-directory skills.
    tags: [codebase, bash]
    turns:
      - message: |
          Find every Python file in src/personal_agent/orchestrator/ that defines
          an async function. How many are there and what are the file names?

  # --- Python computation ---
  - id: python_calculation
    description: >
      Computation task. Should route to run-python skill.
    tags: [computation, run-python]
    turns:
      - message: |
          Calculate the 95th percentile of the following response times in milliseconds:
          [120, 340, 89, 2100, 450, 210, 178, 990, 67, 3400, 230, 560, 44, 780, 155].
          Show your work.

  # --- No-skill baseline ---
  - id: no_skill_needed
    description: >
      A factual question that requires no tool use and no skill injection.
      Baseline: measures false positives (skill injection when none needed).
    tags: [baseline, no-tool]
    turns:
      - message: |
          What is the Fibonacci sequence and can you give me the first 10 numbers?
          No need to use any tools — just answer from memory.
```

---

## Section B — Cell matrix (`matrix.yaml`)

6 cells = `{cloud, local} × {keyword, hybrid, model_decided}`. Cloud = Anthropic Sonnet via API. Local = self-hosted Qwen 35B via Cloudflare tunnel.

```yaml
cells:
  # --- Cloud cells ---
  - id: cloud-keyword
    profile: cloud
    env:
      AGENT_SKILL_ROUTING_MODE: keyword
      AGENT_SKILL_ROUTING_MODEL_KEY: ""
    description: "Cloud primary (Sonnet) + keyword-only skill injection (Phase A legacy)"

  - id: cloud-hybrid
    profile: cloud
    env:
      AGENT_SKILL_ROUTING_MODE: hybrid
      AGENT_SKILL_ROUTING_MODEL_KEY: ""
    description: "Cloud primary (Sonnet) + hybrid (index + keyword bodies)"

  - id: cloud-model-decided
    profile: cloud
    env:
      AGENT_SKILL_ROUTING_MODE: model_decided
      AGENT_SKILL_ROUTING_MODEL_KEY: claude_haiku
    description: "Cloud primary (Sonnet) + model_decided; Haiku routing pre-flight"

  # --- Local cells ---
  - id: local-keyword
    profile: local
    description: "Local primary (Qwen 35B) + keyword-only (baseline)"

  - id: local-hybrid
    profile: local
    description: "Local primary (Qwen 35B) + hybrid — expected best for local"

  - id: local-model-decided
    profile: local
    env:
      AGENT_SKILL_ROUTING_MODE: model_decided
      AGENT_SKILL_ROUTING_MODEL_KEY: claude_haiku
    description: "Local primary (Qwen 35B) + model_decided; Haiku routing pre-flight"

metrics:
  primary:
    - tool_iteration_limit_reached_rate     # fraction exhausting tool budget
  skill_routing:
    - skill_routing_call_completed_rate     # how often Phase C pre-flight fires
    - read_skill_invoked_rate               # how often model calls read_skill
    - tool_call_blocked_known_bad_pattern_rate   # how often a guard intercepts a bad arg
  incident_class:
    - es_first_call_correct_rate            # ES prompt: first bash uses agent-logs-* or guard fires
  cost:
    - p95_wall_time_ms
    - total_tokens
    - usd_cost
```

---

## Section C — Analysis script (`skill_routing_analysis.py`)

For each prompt's `raw.json`, reads pre-fetched ES events and extracts per-trace metrics, then aggregates to a cell-level summary.

```python
"""Phase D — Skill Routing Eval: per-trace metric extraction from ES."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

ES_URL = "http://localhost:9200"
ES_INDEX = "agent-logs-*"


def _search(query: dict[str, Any]) -> list[dict[str, Any]]:
    resp = httpx.post(
        f"{ES_URL}/{ES_INDEX}/_search",
        json=query,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return [hit["_source"] for hit in resp.json().get("hits", {}).get("hits", [])]


def analyse_trace(trace_id: str, es_hits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Extract Phase B/C metrics for a single trace.

    Reads from *es_hits* (events already fetched by the harness and stored in
    raw.json) when available, falling back to a direct ES query otherwise.
    """
    result: dict[str, Any] = {"trace_id": trace_id}

    def _events(name: str) -> list[dict[str, Any]]:
        if es_hits is not None:
            return [h for h in es_hits if h.get("event_type") == name]
        return _search({
            "size": 50,
            "query": {"bool": {"must": [
                {"term": {"trace_id.keyword": trace_id}},
                {"term": {"event_type": name}},
            ]}},
            "sort": [{"@timestamp": "asc"}],
        })

    # Primary metric
    result["tool_iteration_limit_reached"] = len(_events("tool_iteration_limit_reached")) > 0

    # Phase B: skill_index_assembled
    index_events = _events("skill_index_assembled")
    if index_events:
        first = index_events[0]
        result["skill_routing_mode"] = first.get("routing_mode", "unknown")
        result["skill_index_injected_chars"] = first.get("injected_chars", 0)
        result["skill_index_turns"] = len(index_events)
    else:
        result["skill_routing_mode"] = "none"
        result["skill_index_injected_chars"] = 0
        result["skill_index_turns"] = 0

    # Phase B: read_skill invocations
    read_events = _events("read_skill_invoked")
    result["read_skill_count"] = len(read_events)
    result["read_skill_names"] = [e.get("skill_name") for e in read_events]

    # Phase B.5: guard blocks
    guard_events = _events("tool_call_blocked_known_bad_pattern")
    result["guard_blocks"] = len(guard_events)
    result["guard_patterns"] = [e.get("pattern") for e in guard_events]

    # Phase C: routing call
    routing_events = _events("skill_routing_call_completed")
    if routing_events:
        r = routing_events[0]
        result["routing_call_fired"] = True
        result["routing_model_key"] = r.get("routing_model_key", "")
        result["routing_latency_ms"] = r.get("latency_ms", 0)
        result["routing_skills_returned"] = r.get("skills_returned", [])
    else:
        result["routing_call_fired"] = False

    # Incident-class: first bash command
    bash_events = _events("bash_started")
    if bash_events:
        cmd = bash_events[0].get("command", "")
        result["first_bash_command"] = cmd[:200]
        # Correct: uses agent-logs-* (not /logs-* which is the hallucinated pattern)
        result["first_bash_uses_correct_index"] = (
            "agent-logs-" in cmd or "/logs-*" not in cmd
        )
    else:
        result["first_bash_command"] = ""
        result["first_bash_uses_correct_index"] = None

    return result


def analyse_run(run_dir: Path) -> dict[str, Any]:
    """Analyse all prompt results in a harness run directory."""
    prompt_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir())
    traces: list[dict[str, Any]] = []

    for p in prompt_dirs:
        raw_path = p / "raw.json"
        if not raw_path.exists():
            log.warning("raw_json_missing", path=str(raw_path))
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        first = raw[0] if isinstance(raw, list) and raw else raw
        trace_id = first.get("trace_id") if isinstance(first, dict) else None
        if not trace_id:
            log.warning("no_trace_id", path=str(raw_path))
            continue
        es_hits: list[dict[str, Any]] = []
        for turn in (raw if isinstance(raw, list) else [raw]):
            es_hits.extend(turn.get("es_hits", []))
        trace_metrics = analyse_trace(trace_id, es_hits=es_hits or None)
        trace_metrics["prompt_id"] = p.name
        traces.append(trace_metrics)

    n = len(traces)
    if n == 0:
        return {"prompts_analysed": 0, "error": "no traces found"}

    summary: dict[str, Any] = {
        "prompts_analysed": n,
        "tool_iteration_limit_reached_rate": sum(
            1 for t in traces if t.get("tool_iteration_limit_reached")
        ) / n,
        "read_skill_invoked_rate": sum(
            1 for t in traces if t.get("read_skill_count", 0) > 0
        ) / n,
        "guard_block_rate": sum(
            1 for t in traces if t.get("guard_blocks", 0) > 0
        ) / n,
        "routing_call_rate": sum(
            1 for t in traces if t.get("routing_call_fired")
        ) / n,
        "es_first_call_correct_rate": sum(
            1 for t in traces
            if t.get("first_bash_uses_correct_index") is True
        ) / max(1, sum(1 for t in traces if t.get("first_bash_command"))),
        "traces": traces,
    }
    return summary
```

---

## Section D — Harness driver (`recovery_harness.py`, abridged to the relevant parts)

Drives prompts through `/chat`, captures `trace_id`, waits 5s for ES indexing, fetches all logs tagged with that `trace_id`, queries Neo4j for any writes, writes a per-prompt `raw.json` + `report.md`.

```python
async def call_chat(
    client: httpx.AsyncClient,
    chat_url: str,
    message: str,
    session_id: str | None,
    auth_email: str | None,
    profile: str = "local",
    skill_routing_mode: str | None = None,
) -> tuple[str, str, str]:
    """POST /chat. Return (response_text, session_id, trace_id)."""
    params = {"message": message, "profile": profile}
    if session_id is not None:
        params["session_id"] = session_id
    if skill_routing_mode:
        params["skill_routing_mode"] = skill_routing_mode
    headers: dict[str, str] = {}
    if auth_email:
        headers["Cf-Access-Authenticated-User-Email"] = auth_email
    resp = await client.post(chat_url, params=params, headers=headers, timeout=600.0)
    resp.raise_for_status()
    data = resp.json()
    return (
        str(data.get("response", "")),
        str(data["session_id"]),
        str(data["trace_id"]),
    )


async def fetch_trace_logs(
    queries: TelemetryQueries, trace_id: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Pull all ES log documents tagged with this trace_id in the window."""
    settings = get_settings()
    client = await queries._get_client()
    response = await client.search(
        index=f"{settings.elasticsearch_index_prefix}-*",
        query={
            "bool": {
                "filter": [
                    {"term": {"trace_id.keyword": trace_id}},
                    {"range": {"@timestamp": {"gte": since.isoformat(),
                                              "lte": until.isoformat()}}},
                ]
            }
        },
        size=500,
        sort=[{"@timestamp": {"order": "asc"}}],
    )
    return [hit.get("_source", {}) for hit in response.get("hits", {}).get("hits", [])]


async def run_prompt(
    prompt: PromptDef,
    *,
    chat_url: str,
    auth_email: str | None,
    profile: str = "local",
    skill_routing_mode: str | None = None,
    es_wait_seconds: int,
    queries: TelemetryQueries,
    memory_service: MemoryService,
    out_dir: Path,
) -> list[TurnResult]:
    """Execute one prompt's turns and write its per-prompt report."""
    results: list[TurnResult] = []
    session_id: str | None = None
    async with httpx.AsyncClient() as client:
        for turn in prompt.turns:
            if turn.new_session:
                session_id = None
            started_at = datetime.now(timezone.utc)
            response_text, session_id, trace_id = await call_chat(
                client, chat_url, turn.message, session_id, auth_email,
                profile=profile, skill_routing_mode=skill_routing_mode,
            )
            finished_at = datetime.now(timezone.utc)
            await asyncio.sleep(es_wait_seconds)   # wait for ES indexing
            es_hits = await fetch_trace_logs(
                queries,
                trace_id,
                started_at - timedelta(seconds=2),
                finished_at + timedelta(seconds=es_wait_seconds + 5),
            )
            n4_turn, n4_ent, n4_rel = await fetch_neo4j_for_trace(memory_service, trace_id)
            results.append(TurnResult(...))
    # ... writes raw.json + report.md per prompt
```

---

## Sample observed results (one cell, for grounding)

`cloud-model-decided` cell, 10 prompts (one of the six cells):

| metric | value |
|---|---|
| `tool_iteration_limit_reached_rate` | 0% |
| `es_first_call_correct_rate` | 100% |
| `read_skill_invoked_rate` | 40% |
| `guard_block_rate` | 0% |
| `routing_call_rate` | 100% |
| skill index injected chars (constant) | 1,661 |
| routing latency p50 | 50ms (pre-fix; bug masked exception) |

Same metrics for `cloud-hybrid`: 0% iter_limit, 100% es_correct, **0%** read_skill (keyword injection sufficient), **0%** routing_call, ~7,000–15,000 chars injected per request.

---

## Specific failure modes I want you to think about

1. The eval was run with `routing_skills_returned: []` for **every** model_decided trace because of a budget-config bug (now fixed). Correctness was preserved end-to-end because the primary model fell back to `read_skill`. **Question for you:** what other "silently-degraded" failure modes could the metrics here pass? How would I detect a router that returns `[wrong_skill]` consistently vs returns `[]`?
2. `es_first_call_correct_rate` is computed as "first bash command contains `agent-logs-` OR does not contain `/logs-*`" — i.e. a very loose negative check. Is this trustworthy?
3. The harness waits 5s for ES indexing then queries. Some events I emit are buffered through a structlog → ES handler with batching. Could I be missing late-arriving events? Should I poll instead of sleep?
4. `cloud-keyword` and `cloud-hybrid` produce identical correctness in this eval. Is that diagnostic of "modes are equivalent" or "prompts are too easy to differentiate them"?

---

## Section C Addendum — FRE-331 Metrics (Router-Only + Success Class)

Added 2026-05-08. These metrics require `expected_router_skills` /
`forbidden_router_skills` ground-truth labels in `prompts.yaml` (now present
for all 10 prompts).

### Router-only metrics (meaningful for `model_decided` mode)

| Metric | Definition | Blind spot |
|--------|-----------|-----------|
| `router_recall_mean` | `mean(|returned ∩ expected| / max(1,|expected|))` | Undefined (None) for `no_skill_needed` prompts where expected=[] |
| `router_precision_mean` | `mean(|returned ∩ expected| / max(1,|returned|))` | Returns 0 for the expected=[] case when router returns anything (false positive signal) |
| `router_empty_rate` | Fraction of traces where `routing_skills_returned == []` | Only meaningful for `model_decided` (keyword/hybrid never fire the routing call) |
| `router_wrong_skill_rate` | Fraction where router returned ≥1 forbidden skill | Only catches skills explicitly listed in `forbidden_router_skills` |

**Note:** `router_recall` and `router_precision` are `None` (excluded from the
mean) for prompts where `expected_router_skills: []` and `router_recall` is
undefined. For those prompts, the false-positive signal lives in
`router_precision` (0.0 when router returns something it shouldn't) and
`router_wrong_skill_rate`.

### 4-way success class

Replaces the binary pass/fail with a 4-way per-trace classification, aggregated
as rates:

| Class | Definition | Key signal |
|-------|-----------|-----------|
| `clean_success` | No iteration limit + router pre-loaded expected skills (or none needed) | The "true success" metric |
| `recovered_success` | No iteration limit + router missed; primary fetched via `read_skill` | System survived but router failed |
| `guard_saved` | B.5 guard intercepted ≥1 bad tool call; trace completed without limit | Guard is load-bearing |
| `failed` | Iteration limit reached | Structural failure; LLM-judge correctness not yet implemented |

**Known limitation**: "correctness" within `clean_success` and
`recovered_success` is structural (no tool iteration limit) not semantic (no
LLM judge). A trace that answers with plausible-sounding but wrong data still
scores `clean_success`. Semantic correctness is out of scope until FRE-330+.

### read_skill 3-bucket

Decomposes `read_skill_invoked_rate` into three explanatory buckets:

| Bucket | Definition | What it means |
|--------|-----------|--------------|
| `needed_and_invoked` | Expected skill ∈ `read_skill_names` | Router missed; primary recovered (good fallback) |
| `needed_but_not_invoked` | Expected skill ∉ (`returned` ∪ `read_skill_names`) | Silent failure: skill never loaded |
| `not_needed_but_invoked` | `read_skill_names` − `expected_router_skills` is non-empty | Over-fetching; unnecessary latency |

**Interpretation**:
- High `needed_and_invoked` + low `router_recall` → `recovered_success` pattern; primary model compensates well
- High `needed_but_not_invoked` → silent failure; router AND primary both missed the skill
- High `not_needed_but_invoked` → primary model over-fetches; increases p95 latency unnecessarily

### Impact on ADR-0066 D2 threshold

ADR-0066 recommends switching from `hybrid` to `model_decided` when `p95 > 6000 tokens`.
That decision requires trustworthy `router_recall_mean` data showing `model_decided`
actually routes correctly (`clean_success` ≥ 80%). The 2026-05-07 data showed
`recovered_success` for all `model_decided` traces (budget bug). FRE-330 re-run
data is required before the D2 threshold can be meaningfully set.

---

## What's NOT in scope

- I'm not asking for code review of the implementation files — only the eval design.
- I'm not asking for prose feedback on the test names or YAML formatting.
- The fact that this is markdown-formatted is incidental; treat it as the source of truth.
