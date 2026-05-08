# Postfix Comparison — Router-Only vs End-to-End Metrics (FRE-331)

> **Purpose**: Document the recovered-vs-clean split introduced by FRE-331's
> ground-truth metric framework, applied to the 2026-05-07 Phase D data.
>
> **Status**: Analysis framework ready (FRE-331 ✅). Full re-run pending FRE-330.
> Numbers in this file are derived from the qualitative signals in report.md
> files; authoritative values will be written by the updated analysis script
> once FRE-330 re-runs produce fresh raw.json files.

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

## What the 2026-05-07 Data Shows (Qualitative)

### `cloud-model-decided` cell

| Trace-level signal | Value |
|--------------------|-------|
| `routing_call_fired` | `True` (call fired but returned `[]`) |
| `routing_skills_returned` | `[]` for every trace |
| `read_skill_invoked_rate` | Non-zero (primary recovered) |
| `tool_iteration_limit_reached_rate` | 0% |

**Under FRE-331 classification:**
- `success_class = recovered_success` for all ES/Neo4j/diagnostics prompts
  (router missed → primary fetched via `read_skill`)
- `success_class = clean_success` for `no_skill_needed` (no skill required,
  no iteration limit)
- `router_empty_rate ≈ 100%` (consistent with budget bug)
- `router_recall_mean ≈ 0.0`
- `router_precision_mean ≈ 1.0` (empty return never contains a wrong skill)

**Interpretation**: The 2026-05-07 model-decided results are
`recovered_success`, not `clean_success`. The system survived but the router
was never exercised.

### `cloud-hybrid` and `cloud-keyword` cells

| Signal | Value |
|--------|-------|
| `routing_call_fired` | `False` (no separate routing LLM) |
| Skill injection | Via keyword matching into system prompt |
| `tool_iteration_limit_reached_rate` | 0% |

**Under FRE-331 classification:**
- `routing_call_fired = False` → router recall/precision are undefined (N/A)
- `success_class` is determined by iteration limit + guard blocks alone
- Expected: `clean_success` for most prompts (skills were keyword-injected)
- Note: for prompts where no keyword matched (e.g. `python_calculation` in
  keyword mode if `run-python` keywords don't match), it would be
  `recovered_success` if the model called `read_skill`

### `local-*` cells

Same structure as cloud cells but with Qwen 35B as the primary model. Full
analysis pending FRE-330 re-run.

---

## Expected Post-FRE-329 Numbers (model_decided after budget fix)

After FRE-329 fixed the `skill_routing` budget role, the router now fires and
returns actual skill names. Re-running the Phase D matrix (FRE-330) will
produce the first clean `clean_success` data for `model_decided` cells.

Projected success-class breakdown for `cloud-model-decided` (post-329):

| Class | Rate (projected) | Basis |
|-------|-----------------|-------|
| `clean_success` | 70–90% | Router now returns correct skills for clear prompts |
| `recovered_success` | 5–20% | Edge cases where router misses; primary recovers |
| `guard_saved` | 0–10% | B.5 guard fires on bad ES patterns |
| `failed` | 0% | Iteration limit not expected with correct routing |

These projections will be replaced with actuals when FRE-330 completes.

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
