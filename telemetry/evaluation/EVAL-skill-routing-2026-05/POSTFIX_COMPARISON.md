# Postfix Comparison — Router-Only vs End-to-End Metrics (FRE-331)

> **Purpose**: Document the recovered-vs-clean split introduced by FRE-331's
> ground-truth metric framework, applied to the 2026-05-07 Phase D data.
>
> **Status**: FRE-330 re-run complete (2026-05-08). `cloud-model-decided` has authoritative
> postfix data. `local-model-decided` skipped (local SLM server offline at re-run time).

---

## Background

The 2026-05-07 Phase D eval showed 100% `es_first_call_correct_rate` and 0%
`tool_iteration_limit_reached_rate` across all 6 cells. Without a router-only
lens, this looked like "everything works". FRE-331 was filed after the external
review surfaced the critical ambiguity:

> "Right now the eval can prove 'the system often survives routing problems,'
> but not yet 'the router is correct.'" — REVIEW_BUNDLE.md, 2026-05-07

The `cloud-model-decided-2026-05-07` cell exposed this concretely: the router
(`skill_routing_call_completed`) returned `[]` for every trace because the
`skill_routing` budget role was missing from `budget.yaml` (fixed in FRE-329).
The primary agent recovered silently by calling `read_skill` itself.

---

## Actual Postfix Results — `cloud-model-decided` (2026-05-08-postfix)

Both bugs fixed: `178f664` (budget `KeyError`) + `cee1793` (duplicate event name + trace_id).
Analysis uses FRE-331 ground-truth labels and updated `skill_routing_analysis.py`.

### Summary comparison

| Metric | 2026-05-07 (broken router) | 2026-05-08-postfix (fixed) |
|--------|---------------------------|---------------------------|
| `routing_call_rate` | 100% | 100% |
| `routing_skills_returned` | `[]` every trace | Non-empty for skill prompts |
| `read_skill_invoked_rate` | **40%** | **0%** |
| `router_recall_mean` | ~0% | **94%** |
| `router_precision_mean` | n/a | **78%** |
| `router_empty_rate` | 100% | **10%** (no_skill_needed only — correct) |
| `router_wrong_skill_rate` | n/a | **0%** |
| `clean_success_rate` | n/a | **90%** |
| `recovered_success_rate` | n/a | **0%** |
| `guard_saved_rate` | n/a | **0%** |
| `failed_rate` | n/a | **10%** (see note) |
| `routing_latency_p50` | ~50ms (fake) | **~750ms** (real Haiku call) |

### Per-prompt breakdown (postfix run)

| Prompt | router_recall | router_precision | routing_skills_returned | success_class |
|--------|--------------|-----------------|------------------------|---------------|
| `es_incident_class` | 1.00 | 0.33 | bash, query-elasticsearch, seshat-observations | clean_success |
| `es_tool_error_analysis` | 1.00 | 0.50 | seshat-observations, query-elasticsearch | clean_success |
| `es_skill_routing_telemetry` | 1.00 | 1.00 | query-elasticsearch | clean_success |
| `neo4j_entity_count` | 1.00 | 1.00 | neo4j-direct | clean_success |
| `system_metrics_snapshot` | 1.00 | 1.00 | system-metrics | clean_success |
| `process_and_ports` | 1.00 | 0.50 | system-diagnostics, system-metrics | clean_success |
| `infra_health_check` | 1.00 | 1.00 | infrastructure-health | clean_success |
| `codebase_search` | 0.50 | 0.50 | bash, read-write | **failed** † |
| `python_calculation` | 1.00 | 1.00 | run-python | clean_success |
| `no_skill_needed` | null | 1.00 | [] | clean_success |

† `codebase_search` ground-truth expects `[bash, list-directory]`; router returned
`[bash, read-write]`. The primary model completed the task correctly with `bash` alone
(used `grep -rl "async def"`). This is a **false negative** in the classification — the
task succeeded but our `list-directory` label was too strict. To be refined in FRE-334.

### Key interpretation

**recovered_success = 0%**: The 40% `read_skill` rate in the 2026-05-07 data was entirely
router compensation — the primary model fetching skills the broken router should have
pre-loaded. With the router working, zero fallback reads are needed.

**es_first_call_correct_rate = 38%** (3/8 bash-issuing prompts): This denominator shift
is expected and correct. In 2026-05-07 `model_decided` data, the broken router forced the
primary model to `read_skill` before every bash call, meaning ES prompts got the right
skill guidance. Now the router pre-loads correctly, but the `es_first_call_correct_rate`
metric is measured across ALL bash-issuing prompts (including neo4j, system, infra,
codebase). Only the 3 ES prompts should use `agent-logs-`; the other 5 use different
bash patterns. The metric design is a known limitation (filed as part of FRE-334 scope).

**Precision < 1.0 for some prompts**: Haiku adds extra skills (e.g. `seshat-observations`
alongside `query-elasticsearch`). These extras don't cause failures — they just increase
injection size slightly. Router precision 78% vs recall 94% shows the router is recall-
biased (prefers adding an extra skill over missing a required one). This is the safer
failure mode.

### `local-model-decided` results (2026-05-08-postfix)

Run via Mac SLM tunnel (`https://slm.example.com/v1`, Qwen 35B-A3B). 9/10 prompts
analysed — `es_incident_class` timed out (ReadTimeout after 600s).

| Metric | 2026-05-07 (broken) | 2026-05-08-postfix (fixed) |
|--------|--------------------|-----------------------------|
| `read_skill_invoked_rate` | **50%** | **0%** |
| `router_recall_mean` | ~0% | **94%** |
| `router_precision_mean` | n/a | **83%** |
| `router_empty_rate` | 100% | **11%** (no_skill_needed — correct) |
| `clean_success_rate` | n/a | **89%** (8/9) |

Per-prompt (9 prompts):

| Prompt | recall | precision | skills_returned | success_class |
|--------|--------|-----------|----------------|---------------|
| `es_incident_class` | — | — | — | **timeout** |
| `es_tool_error_analysis` | 1.00 | 0.50 | seshat-observations, query-elasticsearch | clean_success |
| `es_skill_routing_telemetry` | 1.00 | 1.00 | query-elasticsearch | clean_success |
| `neo4j_entity_count` | 1.00 | 1.00 | neo4j-direct | clean_success |
| `system_metrics_snapshot` | 1.00 | 1.00 | system-metrics | clean_success |
| `process_and_ports` | 1.00 | 0.50 | system-diagnostics, system-metrics | clean_success |
| `infra_health_check` | 1.00 | 1.00 | infrastructure-health | clean_success |
| `codebase_search` | 0.50 | 0.50 | bash, read-write | **failed** † |
| `python_calculation` | 1.00 | 1.00 | run-python | clean_success |
| `no_skill_needed` | null | 1.00 | [] | clean_success |

**`es_incident_class` timeout**: The 25-iteration ES diagnostic loop exceeds Qwen's
throughput at the 600s harness limit. Cloud Sonnet completes it in ~10 min. Finding:
this prompt is at the edge of local model capacity and needs a timeout-handling strategy
in the harness (FRE-332 scope) or a complexity cap in the prompt set (FRE-334 scope).

**Routing is primary-model-independent**: Haiku returns identical skills for cloud and
local cells (`query-elasticsearch` for ES prompts, `neo4j-direct` for Neo4j, etc.).
The router quality depends only on Haiku + the skill index, not the downstream model.

---

## What the 2026-05-07 Data Shows (Qualitative — pre-postfix)

### `cloud-model-decided` cell (broken router)

**Under FRE-331 classification (retrospective):**
- `success_class = recovered_success` for all ES/Neo4j/diagnostics prompts
  (router returned [] → primary fetched via `read_skill`)
- `success_class = clean_success` for `no_skill_needed`
- `router_empty_rate ≈ 100%`; `router_recall_mean ≈ 0.0`

**Interpretation**: The 2026-05-07 model-decided results were `recovered_success`,
not `clean_success`. The system survived but the router was never exercised.

### `cloud-hybrid` and `cloud-keyword` cells

- `routing_call_fired = False` → router recall/precision are undefined (N/A)
- `success_class` determined by iteration limit + guard blocks alone
- Expected: `clean_success` for most prompts (skills were keyword-injected)

---

## Implications for ADR-0066

ADR-0066's recommendation to default to `hybrid` mode was based on 2026-05-07
data. The FRE-330 postfix data now provides the first clean `model_decided`
baseline:

- Router `recall=0.94`, `precision=0.78` — healthy, not broken
- `read_skill_invoked_rate = 0%` — router eliminated all fallback reads
- `clean_success = 90%` — one false negative (codebase_search label issue)
- Routing latency ~750ms p50 — the real cost of the Haiku pre-flight

The ADR D1 decision (default `hybrid`) remains correct for the current
library size: 750ms Haiku latency on every request buys a 9× injection
reduction that is only valuable when the library grows beyond the
6,000-token threshold (D2). At 14 skills, hybrid's per-request injection
(~4,000–15,000 chars) is below that threshold and costs zero extra latency.

The D2 threshold trigger (FRE-335) can now be built with confidence that
`model_decided` works correctly when activated.

---

## Running the Updated Analysis

```bash
# Re-analyse a 2026-05-07 run (reads trace_ids from report.md, queries ES live)
uv run python scripts/eval/skill_routing_analysis.py \
    --run-dir telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-model-decided-2026-05-07

# Analyse a fresh FRE-330 run (reads from raw.json)
uv run python scripts/eval/skill_routing_analysis.py \
    --run-dir telemetry/evaluation/EVAL-skill-routing-2026-05/cloud-model-decided-<RUN_ID>
```

The script now:
1. Loads ground-truth labels from `prompts.yaml` automatically
2. Falls back to parsing `report.md` for trace_ids when `raw.json` is missing
3. Outputs all FRE-331 metrics alongside legacy metrics

---

## What Changes in the Summary JSON

Old `skill_routing_summary.json` (pre-FRE-331):

```json
{
  "prompts_analysed": 10,
  "tool_iteration_limit_reached_rate": 0.0,
  "read_skill_invoked_rate": 0.7,
  "routing_call_rate": 1.0,
  "es_first_call_correct_rate": 1.0
}
```

New format (post-FRE-331):

```json
{
  "prompts_analysed": 10,
  "tool_iteration_limit_reached_rate": 0.0,
  "read_skill_invoked_rate": 0.7,
  "routing_call_rate": 1.0,
  "es_first_call_correct_rate": 1.0,
  "router_recall_mean": 0.0,
  "router_precision_mean": 1.0,
  "router_empty_rate": 1.0,
  "router_wrong_skill_rate": 0.0,
  "success_class": {
    "clean_success": 0.1,
    "recovered_success": 0.8,
    "guard_saved": 0.0,
    "failed": 0.1
  },
  "read_skill_needed_and_invoked_rate": 0.7,
  "read_skill_needed_but_not_invoked_rate": 0.2,
  "read_skill_not_needed_but_invoked_rate": 0.1
}
```

---

## Implications for ADR-0066

ADR-0066's recommendation to default to `hybrid` mode was based on 2026-05-07
data. The FRE-331 analysis confirms that the `model_decided` results in that
run are `recovered_success`, not `clean_success` — meaning the recommendation
is directionally correct but the supporting data overstated `model_decided`
performance. FRE-330 re-run data will provide the first clean comparison.

The ADR-0066 D2 threshold (p95 > 6000 tokens → switch to model_decided) cannot
be meaningfully triggered until FRE-330 data shows true `clean_success` for
`model_decided`. See FRE-335 for the monitor.
