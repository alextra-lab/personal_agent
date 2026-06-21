# FRE-567 — agent-logs Guarded-dynamic: generic numeric dynamic_template

**Ticket:** FRE-567 (Approved, Tier-2:Sonnet, project: Telemetry Surface Audit)
**Refs:** ADR-0090 §D2 (Guarded-dynamic must cover numerics) · FRE-544 (discovery + the cap) ·
FRE-534 (A2 trap fixes) · `insights-index-template.json` `cost_ratio_as_float` (the model)

## Problem

`docker/elasticsearch/index-template.json` (agent-logs-*) covers strings via dynamic_templates and
`*_ms|*_seconds|*_latency|*_duration|*_offset` numerics via `ms_fields_as_float`, plus explicit props
for every *known* numeric. But there is **no generic numeric rule**: a new, not-yet-explicit
float/ratio/cost field first-seen as bare `0` infers `long`, then every later non-integer value is
truncated/rejected. ADR-0090 §D2 wants this class closed.

## Measurement (done — local ES :9200, throwaway probe indices, cleaned up)

Proposed pattern `^(.*_ratio|.*_rate|.*_score|.*_pct|.*_percent|.*_cost_usd|confidence|.*_confidence)$`
with `match_mapping_type:"*"` → `float`:

| field (first value = bare `0`) | with rule | genuine-int control |
|---|---|---|
| cache_hit_ratio, success_rate, quality_score, completion_pct, completion_percent, extra_cost_usd, model_confidence, confidence | **float** ✅ | — |
| widget_count, retry_count, iteration, max_iterations, total_steps | — | **long** ✅ (no collision) |

Negative control (default dynamic, no rule): `cache_hit_ratio:0 → long` (trap reproduced).
→ The rule is the cause; pattern fires on the trap class and does not touch `*_count`/`iteration`/`max_iterations`.

## Design decisions (surfaced for sign-off)

1. **One rule → `float`** named `numeric_ratios_as_float`, mirroring the insights `cost_ratio_as_float`
   precedent (which lumps cost+ratio+confidence into one float rule). Simplicity over a cost→double split.
2. **`*_cost_usd` → float in the generic rule.** All *known* money fields are already explicit `double`
   props, and explicit props win over dynamic_templates — so the generic rule only ever affects a
   *new/unknown* cost field, as a trap-net (float ≈ 7 sig-figs is fine for that net; a real cost field
   gets an explicit `double` prop when added, per D2). Matches insights.
3. **Placement:** immediately after `ms_fields_as_float` (both numeric, `match_mapping_type:"*"`,
   first-match-wins; the two name-patterns are disjoint), before the string rules.
4. **Accepted trade-off:** a *new* field deliberately meant to be integer but named `*_rate`/`*_score`/
   etc. would map float. This is ADR-0090's deliberate "governed, not inferred" stance — rate/ratio/
   score/pct/percent/confidence are float quantities; integer counters use `*_count`/`iteration`.

## Steps

### 1 — Failing test first (TDD)
File: `tests/scripts/test_es_templates.py` — add `test_logs_has_numeric_ratios_as_float_rule`:
- `_dynamic_rule(tpl, "numeric_ratios_as_float")` is not None and `mapping.type == "float"`.
- compiled `match` regex matches: `cache_hit_ratio`, `success_rate`, `quality_score`, `completion_pct`,
  `completion_percent`, `extra_cost_usd`, `confidence`, `model_confidence`.
- compiled `match` regex does **not** match: `widget_count`, `retry_count`, `iteration`,
  `max_iterations`, `total_steps`.
- Verify: `make test-file FILE=tests/scripts/test_es_templates.py` → the new test FAILS (rule absent).

### 2 — Implement
File: `docker/elasticsearch/index-template.json` — insert after the `ms_fields_as_float` block
(lines 16–23), before `ids_keyword`:
```json
{
  "numeric_ratios_as_float": {
    "match_pattern": "regex",
    "match": "^(.*_ratio|.*_rate|.*_score|.*_pct|.*_percent|.*_cost_usd|confidence|.*_confidence)$",
    "match_mapping_type": "*",
    "mapping": { "type": "float" }
  }
}
```
Update `_meta.description`: note the dynamic_templates now cover numerics (ratio/rate/score/pct/
cost) → float, closing the §D2 generic-numeric gap.
- Verify: `make test-file FILE=tests/scripts/test_es_templates.py` → all pass (incl. new test +
  existing `test_logs_stays_guarded_dynamic_not_locked`).

### 3 — Quality gates
- `make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
  `pre-commit run --all-files`.
  (mypy/ruff are effectively no-ops here — JSON + a static test edit — but run them.)

### 4 — Follow-ups / docs
- No code-path changes (template + test only); no doc beyond `_meta`. No follow-up tickets expected.

### 5 — PR + Linear handoff comment for master
- PR with pre-merge checklist only.
- Linear comment (post-deploy runbook): redeploy registers the template via
  `scripts/setup-elasticsearch.sh`; FRE-558's `apply_live_index_mapping` patches the live write
  index additively (dynamic_templates are template-only, so they take effect for the **next** daily
  `agent-logs-*` index — existing indices keep their frozen mappings, which is correct/expected).
  Live verification: PUT a throwaway field as bare `0` to a fresh agent-logs index and confirm float.

## Out of scope
- Backfilling/reindexing existing agent-logs indices (their mappings are frozen; ILM ages them out).
- Promoting any specific new field to an explicit prop (none identified as emitted-but-unmapped here).
