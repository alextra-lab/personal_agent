---
name: eval-harness-launch
description: Pre-flight check and launch the recovery eval harness. Probes the primary chat endpoint AND the SLM/sub-agent endpoint before running so sub-agent 404s don't cascade into 600s primary timeouts. Use when the user asks to run, launch, or kick off an eval, the recovery harness, or `scripts/eval/recovery_harness.py`.
disable-model-invocation: true
---

# eval-harness-launch

Run the recovery eval harness safely. Memory `feedback_check_subagent_before_eval` (2026-05-08) records the trap: when the SLM server / sub-agent endpoint 404s, the primary `/chat` request hangs and burns the full 600s timeout. The result looks like a budget/context issue when it's actually plumbing.

## Pre-Flight Probes (always run first)

```bash
# 1. Primary agent service (FastAPI on :9000)
curl -fsS http://localhost:9000/health || { echo "❌ primary :9000 down"; exit 1; }

# 2. SLM Server / sub-agent LLM endpoint
LLM_BASE=$(grep -E '^AGENT_LLM_BASE_URL=' /opt/seshat/.env | cut -d= -f2- | tr -d '"')
LLM_BASE="${LLM_BASE:-http://localhost:8000/v1}"
curl -fsS "${LLM_BASE%/v1}/v1/models" >/dev/null || { echo "❌ SLM endpoint $LLM_BASE not reachable"; exit 1; }

echo "✅ both endpoints healthy"
```

Refuse to launch the harness if either probe fails. Surface the failure to the user — likely fixes: `make up SERVICE=seshat-gateway` for the primary, or start the SLM server (separate repo, MLX).

## Launching the Harness

Once both probes pass:

```bash
# Full run from a prompts.yaml
uv run python scripts/eval/recovery_harness.py \
    --run-id "<run-id>" \
    --prompts telemetry/evaluation/<EVAL-DIR>/prompts.yaml

# Single prompt for fast iteration
uv run python scripts/eval/recovery_harness.py \
    --run-id "<run-id>" \
    --prompt "<prompt_name>"
```

Ask the user for the run-id and prompts file/name if not provided.

## After the Run

- Output goes to `telemetry/evaluation/<EVAL-DIR>/<run-id>/`
- Per memory `feedback_no_log_dumps_in_git`: do **not** stage `raw.json` files. Only summary `report.md` and roll-up `summary.md` get committed.

## Don't

- Don't launch without the two probes — that's the whole point of this skill.
- Don't bump prompt counts past what fits the daily cost cap (see memory `feedback_budget_cap_values`).
- Don't write to the prod substrate from evals — use `make eval-infra-up` for isolated stack.
