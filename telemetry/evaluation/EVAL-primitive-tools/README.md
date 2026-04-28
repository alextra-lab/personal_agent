# EVAL-primitive-tools — FRE-262 PIVOT-3

Dual-path evaluation comparing the curated-tool baseline against the primitives + skill docs approach introduced in ADR-0063 PIVOT-3.

## Purpose

PIVOT-3 replaces a set of deprecated curated tools (`query_elasticsearch`, `fetch_url`, `list_directory`, `system_metrics_snapshot`, `run_sysdiag`, `infra_health`) with two primitives (`bash`, `run_python`) backed by nine SKILL.md skill docs. This eval measures whether the agent can satisfy the same 20 prompts via primitives at least as well as it did via curated tools.

## Gate Criteria

All three criteria must pass for PIVOT-3 to be declared successful:

1. **Success rate**: Primitive success rate >= curated success rate on >= 17/20 prompts.
2. **Zero dead-ends**: Zero "model could not figure out primitive equivalent" failures (hand-graded ❌ with this specific failure mode).
3. **Per-tool gate**: If primitive < curated for a specific tool category, that category's tools are moved to the PIVOT-4 keep list for re-evaluation.

## Setup — production-equivalent dual-instance stack

Bring up the cloud infrastructure plus two seshat-gateway instances.
Both run inside the `cloud-sim` Docker network, so Docker DNS, GNU
`procps`, and `/app/*` paths all match the production container.

```bash
docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml \
  up -d seshat-gateway-control seshat-gateway-treatment
```

Wait until both report healthy (start-up takes ~60 s):

```bash
until curl -fsS http://localhost:9002/health && curl -fsS http://localhost:9003/health; do
  echo "waiting..."; sleep 5
done
```

## Run the Eval

```bash
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.run_primitive_tools_eval \
  --control-url http://localhost:9002 \
  --treatment-url http://localhost:9003 \
  --es-url http://localhost:9200 \
  --output-dir telemetry/evaluation/EVAL-primitive-tools/run-$(date +%Y-%m-%d)/
```

All flags (defaults shown):

```
--prompts        telemetry/evaluation/EVAL-primitive-tools/prompts.yaml
--control-url    http://localhost:9000
--treatment-url  http://localhost:9001
--es-url         http://localhost:9200
--output-dir     telemetry/evaluation/EVAL-primitive-tools/run-<timestamp>/
--delay          2        (seconds between prompt pairs)
--session-prefix eval-fre262
--skip           (prompt IDs to omit, e.g. --skip es-04 ls-01)
```

## Tear Down

```bash
docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml \
  down seshat-gateway-control seshat-gateway-treatment
```

## Output

Each run writes two files to `--output-dir`:

| File | Contents |
|------|----------|
| `results.json` | Raw results — one object per prompt with responses, latency, and token metrics |
| `report.md` | Side-by-side table with token counts, turn counts, cache hit %, and Quality column |

`trace_id` and `session_id` are in each result object — use them to look up full
traces in Elasticsearch or Kibana. Token metrics are fetched automatically from ES
after each prompt pair completes.

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
