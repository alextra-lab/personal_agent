# FRE-103: Local vs Cloud Inference Concurrency Research

**Status**: Complete  
**Date**: 2026-03-07  
**Linear**: [FRE-103](https://linear.app/frenchforest/issue/FRE-103)  
**Related**: ADR-0029 (Inference Concurrency Control)

## Hypothesis

The HTTP 400 and timeout errors observed on 2026-03-07 are caused by **local SLM inference server contention** (LM Studio single-GPU limitation), and would **not** occur with cloud Foundation Model providers (OpenAI, Anthropic, Google) which handle concurrency server-side.

## Experiment Design

### Test 1: Confirm local contention

Fire 3 concurrent requests at the local LM Studio endpoint, simulating the real-world scenario where router classification, response generation, and background reflection all compete for the same GPU:

| Request | Role | Model | Expected |
|---------|------|-------|----------|
| 1 | Router | lfm2.5-1.2b (small) | May succeed (fast, small) |
| 2 | Standard | qwen3.5-4b (medium) | Likely fails (400 or timeout) |
| 3 | Reasoning | qwen3.5-35b (large) | Likely fails (400 or timeout) |

**Expected outcome**: At least 1 of the 3 requests fails with HTTP 400 or timeout.

### Test 2: Cloud provider comparison

Fire 3 identical concurrent requests at Anthropic's Claude API:

| Request | Role | Model | Expected |
|---------|------|-------|----------|
| 1 | Router equivalent | Claude Sonnet | Success |
| 2 | Standard equivalent | Claude Sonnet | Success |
| 3 | Reasoning equivalent | Claude Sonnet | Success |

**Expected outcome**: All 3 requests succeed (higher latency but no failures).

### Test 3: Mixed mode

Validate that a mixed local/cloud deployment works correctly:

| Request | Role | Provider | Model |
|---------|------|----------|-------|
| 1 | Router | Local | lfm2.5-1.2b |
| 2 | Reasoning | Cloud | Claude Sonnet |
| 3 | Standard | Local | qwen3.5-4b |

**Expected outcome**: Cloud reasoning succeeds; local requests may contend but with only 2 local requests (vs 3 in Test 1), contention is reduced.

## Running the Experiment

### Prerequisites

- LM Studio running with models loaded (for local tests)
- `AGENT_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` set (for cloud tests)

### Commands

```bash
# From project root

# Run all tests
python -m experiments.concurrency_research.run_experiment

# Run only local test (no API key needed)
python -m experiments.concurrency_research.run_experiment --test local

# Run only cloud test
python -m experiments.concurrency_research.run_experiment --test cloud

# Run only mixed-mode test
python -m experiments.concurrency_research.run_experiment --test mixed

# Dry run (verify config, no requests)
python -m experiments.concurrency_research.run_experiment --dry-run

# Use a different cloud model
python -m experiments.concurrency_research.run_experiment --cloud-model claude-sonnet-4-5-20250514
```

### Output

Results are saved as JSON to `experiments/concurrency_research/results/experiment-<timestamp>.json`.

## Success Criteria

- [x] Local contention confirmed with reproduction steps
- [x] Cloud provider handles parallel requests without 400/timeout
- [x] Mixed mode (local router + cloud reasoning) works end-to-end
- [x] Results documented (JSON output + this README updated)
- [x] Findings feed back into ADR-0029 `provider_type` configuration defaults

## Results (2026-03-07)

### Test 1: Local contention (3 concurrent)

| Role | Model | Status | Latency | Notes |
|------|-------|--------|---------|-------|
| Router | lfm2.5-1.2b | success | 194ms | Small model, completes first |
| Standard | qwen3.5-4b | success | 2747ms | Queued behind router |
| Reasoning | qwen3.5-35b | success | 4965ms | Queued behind standard |

**Result**: All 3 succeeded, but latency staircase (194ms → 2747ms → 4965ms) confirms **serial queuing** — LM Studio processes one request at a time and queues the rest.

### Test 1b: Local contention heavy (5 concurrent)

| Role | Model | Status | Latency | Notes |
|------|-------|--------|---------|-------|
| Router | lfm2.5-1.2b | success | 149ms | Small model, fast |
| Standard 1 | qwen3.5-4b | success | 6754ms | Queued, longer prompt |
| Reasoning 1 | qwen3.5-35b | **error_400** | 9696ms | **Model crashed** |
| Standard 2 | qwen3.5-4b | success | 10988ms | Queued, completed after crash |
| Reasoning 2 | qwen3.5-35b | **error_400** | 9695ms | **Model crashed** |

**Result**: **HYPOTHESIS CONFIRMED.** Under heavier load (5 concurrent, longer prompts), the 35B reasoning model crashes with HTTP 400: "The model has crashed without additional information." Smaller models (1.2B, 4B) survive via queuing but with degraded latency.

**Error message**: `{"error":"The model has crashed without additional information. (Exit code: null)"}`

### Test 2: Cloud comparison (3 concurrent to Claude Haiku 4.5)

| Role | Model | Status | Latency | Notes |
|------|-------|--------|---------|-------|
| Router equivalent | claude-haiku-4-5 | success | 1020ms | |
| Standard equivalent | claude-haiku-4-5 | success | 903ms | |
| Reasoning equivalent | claude-haiku-4-5 | success | 1914ms | Longer prompt |

**Result**: **All 3 succeeded in 1.9s total.** Cloud provider handles concurrent requests with no contention — all requests processed in parallel with similar latencies (no staircase).

### Test 3: Mixed mode (local router + cloud reasoning + local standard)

| Role | Provider | Model | Status | Latency | Notes |
|------|----------|-------|--------|---------|-------|
| Router | Local | lfm2.5-1.2b | success | 185ms | Fast, small model |
| Reasoning | Cloud | claude-haiku-4-5 | success | 2116ms | No local GPU contention |
| Standard | Local | qwen3.5-4b | success | 2503ms | Queued behind router |

**Result**: **All 3 succeeded in 2.5s total.** Mixed mode works end-to-end. Cloud reasoning completes independently while local models queue serially. Total wall-clock time is bounded by the slowest local request, not cumulative.

### Key Findings

1. **LM Studio queues requests serially** — even 3 concurrent requests don't fail, but latency degrades linearly (serial processing)
2. **Large models crash under concurrent load** — the 35B model (qwen3.5-35b-a3b) crashes with HTTP 400 when multiple requests compete for GPU memory
3. **Small models are more resilient** — the 1.2B router and 4B standard models survive concurrent access via queuing
4. **The failure mode is a crash, not a timeout** — LM Studio returns 400 with "model has crashed" rather than queuing indefinitely
5. **ADR-0029 concurrency controller is essential** — without it, background tasks (reflection, extraction) will crash the reasoning model during user-facing requests
6. **Cloud providers handle concurrency natively** — 3 concurrent requests to Claude Haiku all complete in ~1-2s with no failures
7. **Mixed mode is viable** — local router (fast, small) + cloud reasoning (concurrent-safe) + local standard works seamlessly

## Implications for ADR-0029

The hypothesis is **confirmed** for local contention:

1. **`provider_type: local`** must enforce strict concurrency control (already implemented) — especially for large models
2. **`max_concurrency: 1`** is the safe default for large local models (35B+) — the current config has `max_concurrency: 2` for reasoning which is too high for single-GPU
3. **Per-endpoint semaphore** is critical — multiple models sharing the same LM Studio instance must be coordinated
4. **Background tasks** (reflection, extraction) using the reasoning model must have finite `priority_timeout` to avoid cascading crashes
5. **Mixed mode** is the recommended deployment for heavy workloads: keep router/standard local, offload reasoning to cloud
6. Auto-detection logic (`infer_provider_type()`) defaults are correct: localhost → local, everything else → cloud

### Recommended Config Changes

Based on findings, `config/models.yaml` should consider:

```yaml
reasoning:
  max_concurrency: 1  # Changed from 2 — single-GPU can't handle parallel 35B inference
```
