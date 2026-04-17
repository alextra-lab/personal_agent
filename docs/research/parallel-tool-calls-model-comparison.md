# Parallel Tool Call Behavior: Model Comparison

> Observed: 2026-04-17 | Environment: VPS cloud deployment (`cloud-sim-seshat-gateway`)

## Finding

Models differ fundamentally in whether they honor the parallel batching instruction in `_TOOL_RULES` (prompts.py). This directly impacts iteration budget consumption and response latency.

## Test

User prompt (sent identically on both paths):
> "Run infra_health AND check my last 5 errors AND search memory for SkyExpress. Batch the tool calls."

The `_TOOL_RULES` system prompt contains:
> "PARALLEL CALLS: When a task needs multiple independent tool calls, issue ALL of them in a SINGLE response as multiple tool_calls entries. Never call them one at a time when they are independent — batching saves iterations."

## Results

### Qwen3-35B-A3B (local, trace `158a9af1`, 21:02 UTC)

```
step_executed  tool_count=1   # infra_health
step_executed  tool_count=1   # self_telemetry_query
step_executed  tool_count=1   # search_memory
```

3 iterations consumed for 3 independent tools. The instruction had no effect.

### Claude Sonnet 4.6 (cloud, trace `675cdbb7`, 21:05 UTC)

```
step_executed  tool_count=3   # all three tools in one LLM response
```

1 iteration consumed for 3 independent tools. Instruction respected.

Follow-up Sonnet traces (`f0e21d33`, `ebd9df82`) confirmed the same behavior: `tool_count=3` consistently.

## Root Cause

This is a **model capability difference**, not a configuration issue:

- **Qwen3-35B-A3B** generates tool calls one at a time per LLM response. The chat template and/or fine-tuning produces a single `tool_calls` entry regardless of prompt instructions. Verified across multiple requests — `tool_count` is always 1.
- **Claude Sonnet 4.6** natively supports parallel function calling. It correctly interprets the batching instruction and emits multiple `tool_calls` entries in a single response.

Note: The agent's self-diagnosis (blaming `DecompositionStrategy.SINGLE`) was **incorrect**. `SINGLE` is a fall-through with zero LLM constraints — it does not restrict tool call count. The constraint is entirely within the LLM's output behavior.

## Impact

| Model | Tools per step | Steps for 3 parallel tasks | Iteration budget hit |
|-------|---------------|---------------------------|----------------------|
| Qwen3-35B-A3B | 1 | 3 | High (was exhausting limit=6) |
| Claude Sonnet 4.6 | 3 | 1 | Low |

## Mitigations Applied

1. **Iteration limit raised** from 6 → 20 (`orchestrator_max_tool_iterations` in `settings.py`) — allows Qwen to complete multi-tool tasks sequentially without hitting the fallback.
2. **Parallel batching rule** kept in `_TOOL_RULES` — no effect on Qwen, but beneficial when running on cloud path.
3. **Fallback message** updated from "synthesis failed" to "tool-use limit reached" — accurate error surfacing when limit is still hit.

## Recommendation for Slice 3

When the cloud profile is the primary path, parallel tool batching works correctly and the iteration limit is unlikely to be a bottleneck. For local Qwen inference, design prompts and task decomposition to minimize independent tool calls per turn, or consider whether Qwen's fine-tuning could be updated to emit multi-tool responses.

If a future local model is evaluated as a Qwen replacement, parallel tool call support should be an explicit evaluation criterion.
