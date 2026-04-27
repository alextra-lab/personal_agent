# EVAL-primitive-tools — FRE-262 PIVOT-3

Dual-path evaluation comparing the curated-tool baseline against the primitives + skill docs approach introduced in ADR-0063 PIVOT-3.

## Purpose

PIVOT-3 replaces a set of deprecated curated tools (`query_elasticsearch`, `fetch_url`, `list_directory`, `system_metrics_snapshot`, `run_sysdiag`, `infra_health`) with two primitives (`bash`, `run_python`) backed by nine SKILL.md skill docs. This eval measures whether the agent can satisfy the same 20 prompts via primitives at least as well as it did via curated tools.

## Gate Criteria

All three criteria must pass for PIVOT-3 to be declared successful:

1. **Success rate**: Primitive success rate >= curated success rate on >= 17/20 prompts.
2. **Zero dead-ends**: Zero "model could not figure out primitive equivalent" failures (hand-graded ❌ with this specific failure mode).
3. **Per-tool gate**: If primitive < curated for a specific tool category, that category's tools are moved to the PIVOT-4 keep list for re-evaluation.

## Setup

Two service instances must be running simultaneously. Use separate terminals.

```bash
# Terminal 1 — Control (curated tools only, no primitives)
AGENT_SERVICE_PORT=9000 AGENT_PRIMITIVE_TOOLS_ENABLED=false AGENT_PREFER_PRIMITIVES=false make dev

# Terminal 2 — Treatment (primitives + skill docs)
AGENT_SERVICE_PORT=9001 AGENT_PRIMITIVE_TOOLS_ENABLED=true AGENT_PREFER_PRIMITIVES=true AGENT_APPROVAL_UI_ENABLED=false make dev
```

> **CRITICAL**: `AGENT_APPROVAL_UI_ENABLED=false` is required for the treatment instance.
> Without it, `bash` commands not in the auto-approve list will block indefinitely
> waiting for PWA approval input, causing the eval to hang.

## Run the Eval

```bash
PERSONAL_AGENT_EVAL=1 uv run python tests/evaluation/run_primitive_tools_eval.py \
  --control-url http://localhost:9000 \
  --treatment-url http://localhost:9001 \
  --output-dir telemetry/evaluation/EVAL-primitive-tools/run-$(date +%Y-%m-%d)/
```

All flags (defaults shown):

```
--prompts      telemetry/evaluation/EVAL-primitive-tools/prompts.yaml
--control-url  http://localhost:9000
--treatment-url http://localhost:9001
--output-dir   telemetry/evaluation/EVAL-primitive-tools/run-<timestamp>/
--delay        2        (seconds between prompt pairs)
--session-prefix eval-fre262
```

## Output

Each run writes two files to `--output-dir`:

| File | Contents |
|------|----------|
| `results.json` | Raw results — one object per prompt with full responses and latency |
| `report.md` | Side-by-side markdown table for human grading |

Session IDs are embedded in `results.json` (format: `eval-fre262-ctrl-{id}` / `eval-fre262-trt-{id}`) so full traces can be looked up in Elasticsearch or Kibana.

## Grading Guide

Open `report.md` and fill in the `Quality` column for each row:

| Mark | Meaning |
|------|---------|
| ✅ | Answer is correct and complete — equivalent or better than the curated baseline |
| ⚠️ | Answer is partial, has a minor error, or used more turns / tool calls than necessary |
| ❌ | Answer is wrong, missing, or the model could not find the primitive equivalent |

Focus the ❌ column on whether the model was *unable* to use a primitive (dead-end), not just on answer quality — those are tracked separately by the gate criteria above.

## Per-Category Breakdown

| Category | Tool being replaced | Prompt IDs |
|----------|--------------------|---------:|
| query-elasticsearch | `query_elasticsearch`, `self_telemetry_query` | es-01 – es-04 |
| fetch-url | `fetch_url` | fetch-01 – fetch-03 |
| list-directory | `list_directory` | ls-01 – ls-03 |
| system-metrics | `system_metrics_snapshot` | metrics-01 – metrics-03 |
| system-diagnostics | `run_sysdiag` | diag-01 – diag-03 |
| infrastructure-health | `infra_health` | infra-01 – infra-04 |
