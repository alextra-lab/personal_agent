# ADR-0062: Tool Loop Gate — Per-Tool FSM-Based Loop Detection

**Status**: Accepted (Implemented)
**Date**: 2026-04-22
**Deciders**: Project owner
**Depends on**: ADR-0005 (Governance Configuration), ADR-0032 (Robust Tool Calling Strategy)
**Related**: ADR-0053 (Gate Feedback-Loop Monitoring Framework), ADR-0006 (Orchestrator Runtime Structure)
**Linear Issue**: None (implemented directly from spec `docs/superpowers/specs/2026-04-22-tool-loop-gate-design.md`)

---

## Context

### The problem with the global dedup threshold

The orchestrator's loop governance was a single blunt instrument: a global iteration
ceiling (`orchestrator_max_tool_iterations = 25`) and a global per-signature repeat
threshold (`orchestrator_max_repeated_tool_calls = 1`).

This caused two concrete failure modes:

**1. Legitimate polling tools blocked after one identical call.**  
`run_sysdiag`, `self_telemetry_query`, and `infra_health` are diagnostic tools designed
to be called with the same arguments across a request — their output changes each call
(CPU %, memory pressure, live metric snapshots). With `max_repeated_tool_calls = 1`, a
second call with the same args was blocked by a "you already called this with these args"
hint injected into the tool result. The model ignored the hint and re-called anyway,
burning iterations until the global ceiling fired and returned a useless fallback.

**2. No consecutive-call detection.**  
The model could call the same tool 10 times in a row with different args — a classic
reasoning loop — and the gate never intervened.

The `all_dedup_blocked` detection added as an interim stopgap detected the symptom (all
tool calls in a turn blocked) rather than the cause.

### Root cause

The `tool_call_signatures` list used a flat dedup strategy: one policy for all tools.
There was no concept of per-tool thresholds, no tracking of whether the output was
actually changing, and no FSM to represent the progression of loop severity.

---

## Decision

Replace the global `tool_call_signatures` dedup block and the `all_dedup_blocked`
detection with a **per-tool FSM-based gate** (`ToolLoopGate`) implemented in a new
module `src/personal_agent/orchestrator/loop_gate.py`.

### Three detection signals

| Signal | Trigger | Action |
|--------|---------|--------|
| **Call identity** | `signature_counts[args_hash] > loop_max_per_signature` | `BLOCK_IDENTITY` |
| **Output identity** | Same (tool, args) produced identical output ≥ 2 times | `BLOCK_OUTPUT` |
| **Consecutiveness** | Same tool N times in a row | `WARN_CONSECUTIVE` at N, `BLOCK_CONSECUTIVE` at N+1 |

Output identity detection is **skippable per tool** via `loop_output_sensitive: true` — for
tools whose output legitimately changes each call even with identical arguments.

### FSM states per tool

```
IDLE → ACTIVE → WARNED → BLOCKED
```

- **IDLE**: Not yet called this request
- **ACTIVE**: Called, within all thresholds
- **WARNED**: Consecutive threshold reached; hint appended to tool result, execution still allowed
- **BLOCKED**: Terminal; all further calls return a blocked result immediately

`WARNED → ACTIVE` reset: if a *different* tool is called between consecutive calls, the
counter resets and the FSM returns to `ACTIVE`. Legitimate multi-tool workflows proceed
normally.

### Signal evaluation order (in `check_before`)

1. **Identity**: `signature_counts[args_hash] > loop_max_per_signature` → `BLOCK_IDENTITY`
2. **Consecutive block**: `fsm.state == WARNED` → `BLOCK_CONSECUTIVE` (grace turn used)
3. **Consecutive warn**: `consecutive_count >= loop_max_consecutive` → `WARN_CONSECUTIVE`
4. **Output identity** (skipped if `loop_output_sensitive=True`): same output hash ≥ 2 times → `BLOCK_OUTPUT`
5. Otherwise → `ALLOW`

### Per-tool policy configuration

Three new fields added to `ToolPolicy` (`governance/models.py`):

```python
loop_max_per_signature: int = 1       # max calls with same args per request
loop_max_consecutive: int = 3         # WARN at N consecutive; BLOCK at N+1
loop_output_sensitive: bool = False   # if True, skip output-identity blocking
```

Per-tool overrides in `config/governance/tools.yaml`:

| Tool | `loop_max_per_signature` | `loop_max_consecutive` | `loop_output_sensitive` |
|------|--------------------------|------------------------|-------------------------|
| `run_sysdiag` | 3 | 4 | true |
| `self_telemetry_query` | 2 | 3 | true |
| `infra_health` | 2 | 3 | true |
| `create_linear_issue` | 1 | 2 | false |
| `write_file` | 1 | 2 | false |
| All others | 1 (default) | 3 (default) | false (default) |

### Telemetry

Every gate decision emits a structured `tool_loop_gate` log event with:
`decision`, `tool_name`, `state_before`, `state_after`, `reason`,
`consecutive_count`, `total_calls`.

This is Level 2 observability per the Four-Level Observability Framework and provides
raw signal for future auto-tuning of per-tool thresholds.

---

## Consequences

### Positive

- **Polling tools unblocked**: `run_sysdiag` and telemetry tools can now be called 2-3x
  with the same args without interference.
- **Consecutive loops detectable**: A tool called 4+ times in a row is warned then blocked
  regardless of argument variation.
- **Observable**: Every gate decision is a structured log event. Gate behavior is auditable
  in Kibana.
- **Evolvable**: Per-tool policy is YAML-configurable without code changes. The FSM state
  machine is explicit, so adding new signals or states is surgical.
- **Output-identity detection**: Calling the same tool with the same args and getting the
  same output multiple times — no new information — is detected and blocked.

### Negative / Trade-offs

- **YAML maintenance**: New tools with unusual calling patterns need explicit policy entries.
- **Global fallback retained**: `orchestrator_max_tool_iterations` and
  `orchestrator_max_repeated_tool_calls` are preserved as ceiling/fallback defaults.
  The gate runs *within* each tool execution, not as an outer ceiling.

### Neutral

- The output-identity signal fires on the *third* call (two prior identical outputs
  required), not the second — one grace call is allowed.
- `record_output` always stores output hashes even for `loop_output_sensitive=True` tools,
  for future telemetry analysis even when the signal doesn't drive blocking decisions.

---

## Files Changed

| File | Change |
|------|--------|
| `src/personal_agent/orchestrator/loop_gate.py` | **New** — FSM states, gate decisions, ToolFSM, ToolLoopGate, stable_hash |
| `tests/test_orchestrator/test_loop_gate.py` | **New** — 24 unit tests covering all signals and FSM transitions |
| `src/personal_agent/governance/models.py` | 3 new fields on `ToolPolicy` |
| `config/governance/tools.yaml` | Per-tool `loop_*` overrides for 5 tools |
| `src/personal_agent/orchestrator/types.py` | `tool_call_signatures: list[str]` → `loop_gate: ToolLoopGate` |
| `src/personal_agent/orchestrator/executor.py` | Gate calls replace dedup block; `all_dedup_blocked` detection removed |
