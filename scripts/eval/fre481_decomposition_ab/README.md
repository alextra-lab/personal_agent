# FRE-481 — ADR-0086 artifact-decomposition before/after A/B

Compares an artifact-build turn on the same live stack under two flag states, so
the discovery-decomposition win (ADR-0086) can be measured against the `a0a07227`
baseline (the FRE-433 recipe, research doc §9).

- **arm `baseline`** — `AGENT_ARTIFACT_DECOMPOSITION_ENABLED=false` (current `main`):
  TOOL_USE artifact builds route to `SINGLE` → serial discovery, the parent context
  grows monotonically (~14 k → ~71 k `fresh_in`).
- **arm `decompose`** — `AGENT_ARTIFACT_DECOMPOSITION_ENABLED=true`: high-complexity
  artifact builds route to `HYBRID` → concurrent tool-using discovery sub-agents,
  each returning a **digest**; only digests cross into the parent.

The arm is set by **what flag the gateway is deployed with**; the harness only tags
`--arm` and drives traffic. The backend is per-request via `--profile {local,cloud}`.

> ⚠️ **This is a master post-deploy action.** It needs a deploy with the flag in the
> desired state (`baseline` = off, `decompose` = on). The build session ships this
> harness; running it, filing the before/after report, and flipping the flag are
> master-owned per the lifecycle rules.

## Headline metrics

| Axis | Claim | Source |
|---|---|---|
| `max_fresh_in` (parent) | **Deterministic** — bounded under `decompose` (digests, not the 71 k tail) | `model_call_completed.input_tokens`, role=`primary` |
| Σ tokens (in/cache/out) | **Measurement-gated** — can rise if discovery slices overlap | sum over the trace's rounds |
| wall-time | **Measurement-gated** — near-zero win on single-GPU local (shared `:8502`); real only where tiers have separate inference capacity (cloud) | first→last `@timestamp` |
| artifact quality | **Post-deploy human eval** — N≥5 baseline-vs-decompose pairs, side-by-side (ADR §2) | `response_text` captured in the JSON |
| routing sliceability | `strategy`/`reason` (`tool_use_*_hybrid`) + `artifact_build` signal | `decomposition_assessed` / `intent_classified` |
| discovery joinability | `sub_agent_tooled_iteration` / `sub_agent_complete` counts (joinable by `session_id`, FRE-481) | `agent-logs-*` |

### Backend-aware truth source (FRE-433 / research doc §9)

A cross-turn **cloud** call reports the reused prefix as `cache_read_input_tokens`
and only the uncached portion as `input_tokens`; **local** reports the full prompt
as `input_tokens`. The harness keys parent context size on the max of the token
fields. For the ground-truth local cache signal use the slm_server's own
`timings.cache_n`, not a conflated ES field.

## Run protocol (2 passes, 1 redeploy)

> ⚠️ **Shared gateway.** Flipping `baseline → decompose` is a cloud-sim gateway
> redeploy with `AGENT_ARTIFACT_DECOMPOSITION_ENABLED=true`. Get explicit owner go
> before deploying; revert to `false` after the measurement.

```bash
EMAIL=<loopback-eval-email>          # CF-Access user to impersonate
RUN=ab-$(date +%Y%m%d)

# --- arm baseline: flag OFF (current main) ---
uv run python scripts/eval/fre481_decomposition_ab/harness.py \
    --run-id $RUN --arm baseline --profile cloud --auth-email $EMAIL

# --- redeploy gateway with the flag ON ---
#   on the VPS: AGENT_ARTIFACT_DECOMPOSITION_ENABLED=true, then
#   ENV=cloud make rebuild SERVICE=seshat-gateway   (needs owner approval)

# --- arm decompose: flag ON ---
uv run python scripts/eval/fre481_decomposition_ab/harness.py \
    --run-id $RUN --arm decompose --profile cloud --auth-email $EMAIL

# (optional) repeat both with --profile local to confirm the single-GPU wall-time caveat.
```

Each pass writes `<run-id>_<arm>_<profile>.{json,md}` under
`telemetry/evaluation/fre481-decomposition-ab/`. Diff the two `.md` tables on
`max_fresh_in` (deterministic) and report Σ tokens / wall-time honestly. The `.json`
holds the captured `response_text` for the side-by-side quality eval.

## Gate

Enable `artifact_decomposition_enabled` in production **only after**: (1) `max_fresh_in`
is demonstrably bounded under `decompose`, (2) the side-by-side quality eval shows no
regression, and (3) `joinability_probe.py` reports no orphans for the new
discovery-sub-agent events. The flag is the rollback (`= false` restores the legacy
`SINGLE` path with no migration).
