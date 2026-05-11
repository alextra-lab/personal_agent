# Plan — Diagnose & Fix EVAL-2026-05-10 Post-PR-#34 Regressions

## Context

PR #34 (merged 2026-05-10, `ffe8e7c`) shipped five fixes targeting multi-turn tool flows
and long generations: loop gate, tool_call ID prefixing, role validator, SSE streaming,
preserve_thinking. The post-merge eval against the cloud profile produced:

| | Baseline EVAL-10 | Post-PR-#34 |
|---|---|---|
| Paths passed | 33/37 | 33/37 |
| Assertions | 175/181 (96.7%) | 170/181 (93.9%) |
| 524 errors | many | 2 (1 trace) |

**Four baseline failures fixed** (CP-05, CP-07, CP-11, CP-22 — exactly the PR #34 target paths).
**Four new regressions:**
- **CP-10, CP-24** — 300s timeout hangs, no error chunks in ES (silent failure)
- **CP-01** — spurious `tool_call_completed` on a conversational turn
- **CP-20** — intent misclassification (`tool_use` instead of `conversational`)

## Diagnosis

### Root cause A — Model is mis-configured against its specification

Confirmed against the Qwen3.6-35B-A3B HuggingFace model card. Architecture is a
Qwen3-Next-style hybrid (gated attention + DeltaNet linear attention), 40 layers,
2 KV heads, head_dim 256. The card explicitly states: *"maintain a context
length of at least 128K tokens to preserve thinking capabilities."*

Current SLM-side config (llamacpp backend, Q4_K_XL GGUF) vs. card spec:

| Setting | Card recommendation | Current SLM `reasoning` block | Delta |
|---|---|---|---|
| Native context | 262,144 | `context_length: 65536` | 4× under |
| **Thinking minimum** | **131,072** | `context_length: 65536` | **below floor** |
| temperature (thinking, general) | 1.0 | `temp: 0.6` | Wrong preset |
| top_p | 0.95 | `top_p: 0.95` | ✓ |
| top_k | 20 | `top_k: 20` | ✓ |
| min_p | 0.0 | `min_p: 0.0` | ✓ |
| presence_penalty | 1.5 | unset | missing |
| Standard max output | 32,768 | gateway caps `thinking_budget_tokens: 3000` | thinking gets truncated |

`preserve_thinking: true` is already enabled in the live SLM config under
`chat_template_kwargs` (per user confirmation; not shown in the snippet).

Compounding this, the gateway's hard ceiling allows **65,536** input tokens
(`src/personal_agent/config/settings.py:346`) — slightly larger than the
SLM-side `context_length: 65536` (no headroom for generation reserve), so a
maximally-budgeted gateway request leaves zero room for the model to think
or respond.

The 3000-token `thinking_budget_tokens` cap is also suspicious: the model
expects up to 32K of thinking output; capping at 3K likely truncates reasoning
mid-stream on complex tasks. CP-10 (DECOMPOSE — Complex Multi-Part) and
CP-24 (Ambiguous Intent) are precisely the paths where deep thinking matters.

Token estimation in `src/personal_agent/request_gateway/budget.py:44-60`
ignores `reasoning_content` — counts only `message.content`. PR #34 grew
prompts with the field the budget can't see.

### Root cause B — Harness assumptions stale relative to PR #34

Three already patched this session (uncommitted):

1. `trace_id.keyword` → `trace_id` in `tests/evaluation/harness/telemetry.py:83`
2. `--cf-email` CLI flag added to `tests/evaluation/harness/run.py`
3. Retry config bumped 4×1.5s → 8×2s in `tests/evaluation/harness/telemetry.py:33-34`

Three defaults borderline at the larger context size:

4. `DEFAULT_CHAT_TIMEOUT_S = 300.0` (`runner.py:34`)
5. `_RESPONSIVENESS_PROBE_TIMEOUT_S = 20.0` (`runner.py:41`)
6. `DEFAULT_INTER_PATH_DELAY_S = 8.0` (`runner.py:38`)

### Root cause C — Non-deterministic single-sample noise (CP-01, CP-20)

Single-assertion, single-run failures with no structural connection to PR #34
or root cause A. Verify with isolated re-runs.

### What's _not_ broken

Streaming error handling correctly raises `LLMInvalidResponse` on error chunks
(`src/personal_agent/llm_client/adapters.py:57`); the retry loop catches and
breaks. CP-10/CP-24 traces have **no error chunks at all** — the hang is the
SLM grinding on an oversize prompt with a truncated thinking budget, not the
client mishandling an error response.

### Hardware feasibility

128GB Apple Silicon (laptop) trivially supports 128K native context:
- Q4_K_XL 35B weights: ~18GB
- KV cache at 128K, q8_0, hybrid architecture: ~1.3–5.1 GB depending on how
  llama.cpp accounts for DeltaNet layers (worst case: all 40 layers treated
  as standard attention)
- Total: ~24GB. Headroom: ~80GB+ even with macOS overhead.

256K would still fit (~10GB KV worst case) but prefill latency doubles. Start
at 128K (the documented thinking minimum); bump to 256K only if eval data
warrants.

## Plan

Six workstreams in priority order.

### P0 — Commit pending harness fixes (no behavioural risk)

Bundle the three already-patched harness files into one PR. Branch:
`fre-harness-pr34-alignment`. Files:

- `tests/evaluation/harness/runner.py` (auth header plumbing)
- `tests/evaluation/harness/run.py` (`--cf-email` flag)
- `tests/evaluation/harness/telemetry.py` (`trace_id.keyword` → `trace_id` and retry config)

PR title: "harness: align eval runner with PR #34 (auth + ES mapping + refresh)".

### P1 — Configure the model to its specification

#### P1a — SLM server config (llamacpp backend, laptop, separate repo)

Update the `reasoning` model entry. Diff against the snippet you shared:

```diff
   reasoning:
     id: "unsloth/qwen3.6-35-A3B"
     backend: "llamacpp"
     port: 8502
     model_type: "lm"
-    context_length: 65536
+    context_length: 131072            # 128K — Qwen card stated thinking minimum
     quantization: "UD-Q4_K_XL"
     max_concurrency: 1
     host: "0.0.0.0"
-    default_timeout: 300
+    default_timeout: 600              # 128K prefill + up to 32K thinking output
     enable_auto_tool_choice: true
     supports_function_calling: true
     tool_call_parser: "qwen3"
     reasoning_parser: "qwen3"
-    chat_template_kwargs: {"enable_thinking": true}
+    chat_template_kwargs: {"enable_thinking": true, "preserve_thinking": true}
     chat_template_file: "config/templates/qwen3.6-unsloth.jinja"
-    temp: 0.6
+    temp: 1.0                         # Qwen card "Thinking Mode — General Tasks"
     top_p: 0.95
     top_k: 20
     min_p: 0.0
+    presence_penalty: 1.5             # Qwen card recommended for thinking
+    repetition_penalty: 1.0           # Explicit
     kv_unified: true
     cache_type_k: "q8_0"
     cache_type_v: "q8_0"
     flash_attn: true
     fit: true
     model_path: "/Volumes/EnvoyUltra/lm-studio/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
     enabled: true
```

Notes:
- `preserve_thinking: true` line is shown in the diff for completeness in case
  the snippet you pasted was stripping it; if your live config already has it,
  keep the existing value.
- If the SLM loader doesn't yet pass `presence_penalty` / `repetition_penalty`
  to `llama-server`, add a one-line mapping in the loader (these correspond to
  llama.cpp's `--presence-penalty` and `--repeat-penalty` flags). Confirm by
  checking the spawn command in the SLM repo.
- KV cache is already `q8_0` — at 128K, peak KV is ≤ 5 GB worst case. No need
  to change. `flash_attn: true` and `kv_unified: true` stay.
- After restart, confirm via `/v1/models` (or llama-server's `/props`) that
  `n_ctx = 131072` is what the server actually loaded.

#### P1b — Gateway model config

`config/models.yaml`, primary entry:

```yaml
primary:
  id: "unsloth/qwen3.6-35-A3B"
  context_length: 131072                       # was 64000 — align with SLM
  quantization: "8bit"                         # unchanged (this field reflects weight quant; SLM is Q4_K_XL — consider renaming or removing this field if not used)
  default_timeout: 600                         # was 180 — match SLM
  temperature: 1.0                             # was 0.6
  top_p: 0.95
  top_k: 20
  thinking_budget_tokens: 32768                # was 3000 — match card "standard" output
  # Pass-through fields (loader must map to extra_body / chat_completions params):
  presence_penalty: 1.5
  min_p: 0.0
  repetition_penalty: 1.0
```

Verify the LLM client adapter (`src/personal_agent/llm_client/client.py` or
`adapters.py`) propagates `presence_penalty`, `min_p`, `repetition_penalty` to
the SLM. If it doesn't today, add the plumbing.

#### P1c — Gateway budgets

`src/personal_agent/config/settings.py`:

```python
context_budget_comfortable_tokens: int = Field(default=64000, ...)    # was 32000
context_budget_max_tokens:         int = Field(default=120000, ...)   # was 65536; 11K margin in a 131K window
context_budget_generation_reserve_tokens: int = Field(default=32768, ...) # was 4096; match thinking output
context_window_max_tokens:         int = Field(default=96000, ...)    # was 49152
```

Proportional: comfortable ~50% of model window, max ~92%, leaving ~11K for
system + tool definitions + thinking output.

### P2 — Fix budget to count reasoning_content

Correctness fix independent of context size. In
`src/personal_agent/request_gateway/budget.py:44-60`:

```python
parts: list[str] = [
    (m.get("content") or "") + " " + (m.get("reasoning_content") or "")
    for m in messages
]
```

A wrong estimator can silently overrun any budget. Lands with P1.

### P3 — Bump harness defaults for the new generation profile

`tests/evaluation/harness/runner.py`:

```python
DEFAULT_CHAT_TIMEOUT_S = 600.0                     # was 300.0
DEFAULT_INTER_PATH_DELAY_S = 12.0                  # was 8.0
_RESPONSIVENESS_PROBE_TIMEOUT_S = 30.0             # was 20.0
```

Inter-turn delay stays at 2s.

### P4 — Re-run the eval and verify

After P0–P3 deployed and SLM running at 128K with corrected sampling:

```bash
PERSONAL_AGENT_EVAL=1 uv run python -m tests.evaluation.harness.run \
    --agent-url http://localhost:9001 \
    --cf-email lextra@gmail.com \
    --output-dir telemetry/evaluation \
    --run-id EVAL-2026-05-11-model-aligned \
    --inter-path-delay 12 \
    --skip-responsiveness-probe
```

Pass criteria:
- CP-10, CP-24 complete in < 600s (regression resolved)
- Assertion pass rate ≥ 96.7% (back to EVAL-10 baseline)
- No new regressions among the 33 that passed today
- CP-01, CP-20: if either still fails, re-run that path 5x in isolation;
  accept as non-deterministic if ≥ 4/5 pass
- Per-path turn p50 ≤ ~30s on short paths

If CP-10/CP-24 still hang: bump SLM `context_length` to 262144 and gateway
budgets proportionally, then re-run.

### P5 — preserve_thinking cap (deferred unless P4 shows need)

At 128K, the model has the room it was designed for. Indefinite reasoning
accumulation cap is not urgent. Defer until either (a) P4 still shows hangs
on long paths, or (b) production traces show prompts approaching 100K+ tokens.

If needed later: cap at last N turns' reasoning_content in
`src/personal_agent/orchestrator/executor.py:1965-1972`, configurable
(`preserve_thinking_max_turns`, default 4).

### P6 — Hardening (defer)

- Integration test in `tests/test_llm_client/test_streaming_aggregation.py`
  mocking an SSE stream emitting an error chunk; assert exception propagates
  without hanging.
- Hard SLM-side read timeout (e.g., 540s for 128K) in the streaming block of
  `src/personal_agent/llm_client/client.py:401-506` so silent SLM hangs fail
  loudly before the harness wall-clock fires.

## Files to Modify

| File | Workstream |
|---|---|
| `tests/evaluation/harness/runner.py` | P0 (already patched) + P3 |
| `tests/evaluation/harness/run.py` | P0 (already patched) |
| `tests/evaluation/harness/telemetry.py` | P0 (already patched) |
| SLM server `models.yaml` (laptop) | P1a |
| SLM server loader (if missing params plumbing) | P1a |
| `config/models.yaml` | P1b |
| `src/personal_agent/config/settings.py` | P1c |
| `src/personal_agent/llm_client/client.py` (or `adapters.py`) | P1b plumbing if needed |
| `src/personal_agent/request_gateway/budget.py` | P2 |
| `src/personal_agent/orchestrator/executor.py` | P5 (defer) |
| `tests/test_llm_client/test_streaming_aggregation.py` | P6 (defer) |

## Open Questions

- **Loader plumbing** for `presence_penalty` and `repetition_penalty` — does
  the current SLM server / personal_agent LLM client already pass these
  through, or do we need a one-line mapping each side?
- **Thinking budget calibration.** Card says standard 32K, complex 81K.
  Starting at 32K (P1b); if eval shows mid-thinking truncations on complex
  paths after P4, bump to 81K.
- **Cold-cache first-turn latency at 128K.** Likely 1–3 minutes on first turn
  of a path; subsequent turns benefit from KV reuse. Within the bumped 600s
  harness timeout.
- **PR structure.** P0 stands alone. P1 + P2 + P3 ship together as
  "config: align with Qwen3.6-35B-A3B spec (128K, sampling, budgets, harness)".

## Out of Scope

- YaRN-extended context (1M tokens). Not needed; quality cost.
- Bumping to 256K up front. Available as a fallback if P4 still shows hangs.
- Switching primary model. The fix is to run it correctly, not replace it.
- Adding a context-overflow retry-with-trim path in the orchestrator. Cleaner
  to never overflow than to recover from it.
