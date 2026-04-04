# EVAL — Proactive Memory A/B (FRE-177)

**Purpose:** Compare evaluation harness outcomes with `AGENT_PROACTIVE_MEMORY_ENABLED` off (control) vs on (treatment), per [PROACTIVE_MEMORY_DESIGN.md](../../../docs/specs/PROACTIVE_MEMORY_DESIGN.md) §A/B Testing.

## Prerequisites

- Agent service + inference stack running (see [EVALUATION_PHASE_GUIDE.md](../../../docs/guides/EVALUATION_PHASE_GUIDE.md)).
- `PERSONAL_AGENT_EVAL=1` (harness safety gate).
- Same model stack and config for both runs except the proactive flag.

## Control run

```bash
export PERSONAL_AGENT_EVAL=1
# proactive_memory_enabled defaults to false; optionally:
# export AGENT_PROACTIVE_MEMORY_ENABLED=false

uv run python -m tests.evaluation.harness.run \
  --output-dir telemetry/evaluation/EVAL-proactive-memory/control \
  --run-id EVAL-proactive-memory-control
```

## Treatment run

```bash
export PERSONAL_AGENT_EVAL=1
export AGENT_PROACTIVE_MEMORY_ENABLED=true

uv run python -m tests.evaluation.harness.run \
  --output-dir telemetry/evaluation/EVAL-proactive-memory/treatment \
  --run-id EVAL-proactive-memory-treatment
```

## Comparison (fill in after runs)

| Metric | Control | Treatment | Notes |
|--------|---------|-----------|--------|
| Paths passed | | | From `evaluation_results.md` summary |
| Assertions passed | | | |
| Assertion pass rate | | | Primary gate |
| Memory Quality category | | | If present in category breakdown |
| Avg / p95 response time | | | Secondary (embedding + Neo4j overhead) |

### Qualitative

- Spot-check a few turns for relevance of injected memory (structured logs: `proactive_memory_suggest_*`, `proactive_memory_budget_trimmed`).
- Note false-positive or noisy injections.

### Threshold tuning

- If treatment regresses assertions or adds noise, adjust `AGENT_PROACTIVE_MEMORY_*` weights/thresholds in [settings.py](../../../src/personal_agent/config/settings.py) and re-run treatment only.

## Status

- **Procedure / template:** complete (this file).
- **Measured comparison:** not yet filled — requires two full harness runs and pasting summary metrics from each `evaluation_results.md` (or JSON) into the table above.
- Artifact directories `control/` and `treatment/` are created by `--output-dir` when you run the harness.
