# Tool Loop Gate — Design Spec

**Date:** 2026-04-22
**Status:** Approved
**Related issues:** FRE-253 (to be created)

---

## Problem

The orchestrator's tool loop governance is a single blunt instrument: a global iteration ceiling (`orchestrator_max_tool_iterations = 25`) and a global per-signature repeat threshold (`orchestrator_max_repeated_tool_calls = 1`). This causes two failure modes:

1. **Legitimate polling tools** (`run_sysdiag`, `self_telemetry_query`) are blocked after one call with the same args, even when output changes between calls. The agent ignores the "already called" hint and re-calls anyway, burning iterations until the global ceiling fires and returns a useless fallback.
2. **No consecutive detection**: the model can call the same tool 10 times in a row with different args and the gate never intervenes.

The interim dedup-blocked fix (detecting all-BLOCKED turns) is a stopgap. It needs to be replaced with a principled, per-tool, observable gate.

---

## Design Goals

1. Per-tool loop policy (thresholds vary by tool class)
2. Three detection signals: call identity, output identity, consecutiveness
3. Finite state machine per tracked tool — transitions are explicit, observable, evolvable
4. All gate decisions are structured log events (foundation for future feedback loop)
5. Backward compatible — tools without explicit policy get current behavior

---

## Approach: `ToolLoopGate` Module (ADR-B)

New file `src/personal_agent/orchestrator/loop_gate.py`. A `ToolLoopGate` dataclass is a registry of per-tool FSMs. The executor delegates all loop decisions to it. `ExecutionContext` holds one instance per request.

---

## Section 1: Policy Configuration

### New fields on `ToolPolicy` (`governance/models.py`)

```python
class ToolPolicy(BaseModel):
    # ... existing fields unchanged ...
    loop_max_per_signature: int = 1        # max executions of same (tool, args) pair per request
    loop_max_consecutive: int = 3          # WARN at N consecutive calls; BLOCK at N+1
    loop_output_sensitive: bool = False    # if True, skip output-identity blocking
```

Defaults reproduce current behavior for any tool without explicit loop config.

### Per-tool overrides in `config/governance/tools.yaml`

**Polling / diagnostic tools** (output changes legitimately each call):

```yaml
run_sysdiag:
  loop_max_per_signature: 3
  loop_max_consecutive: 4
  loop_output_sensitive: true

self_telemetry_query:
  loop_max_per_signature: 2
  loop_max_consecutive: 3
  loop_output_sensitive: true

infra_health:
  loop_max_per_signature: 2
  loop_max_consecutive: 3
  loop_output_sensitive: true
```

**Write tools** (repetition is almost always a bug):

```yaml
create_linear_issue:
  loop_max_per_signature: 1
  loop_max_consecutive: 2

write_file:
  loop_max_per_signature: 1
  loop_max_consecutive: 2
```

All other tools use `ToolPolicy` defaults.

---

## Section 2: FSM Module Design

**File:** `src/personal_agent/orchestrator/loop_gate.py`

### States

Each tool tracked by the gate has its own `ToolFSM` instance:

```
IDLE → ACTIVE → WARNED → BLOCKED
                    ↑         ↑
             (consecutive)  (identity or output signal, from ACTIVE or WARNED)
```

- `IDLE`: not yet called this request
- `ACTIVE`: called, within all thresholds
- `WARNED`: consecutive threshold reached; hint injected into tool result, execution still allowed
- `BLOCKED`: terminal within this request; all further calls return a blocked result immediately

`WARNED → ACTIVE` transition: if a *different* tool is called between consecutive calls, the consecutive counter resets and the FSM returns to `ACTIVE`. This allows legitimate multi-tool workflows to proceed normally.

### Gate Decisions

Directly map to FSM transitions:

```python
class GateDecision(str, Enum):
    ALLOW             = "allow"              # IDLE→ACTIVE or ACTIVE→ACTIVE
    WARN_CONSECUTIVE  = "warn_consecutive"   # ACTIVE→WARNED (execute + inject hint)
    BLOCK_CONSECUTIVE = "block_consecutive"  # WARNED→BLOCKED
    BLOCK_IDENTITY    = "block_identity"     # any→BLOCKED (same args exceeded per-signature limit)
    BLOCK_OUTPUT      = "block_output"       # any→BLOCKED (same output hash seen before)
```

### `GateResult`

Every decision produces a frozen structured record (used for logging and telemetry):

```python
@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    tool_name: str
    state_before: ToolCallState
    state_after: ToolCallState
    reason: str
    consecutive_count: int
    total_calls: int
```

### `ToolFSM`

```python
@dataclass
class ToolFSM:
    state: ToolCallState = ToolCallState.IDLE
    total_calls: int = 0
    consecutive_count: int = 0
    # args_hash → call count (incremented in check_before, before execution)
    signature_counts: dict[str, int] = field(default_factory=dict)
    # args_hash → [output_hash, ...] for output-identity tracking (populated by record_output)
    output_history: dict[str, list[str]] = field(default_factory=dict)
```

### `ToolLoopGate`

```python
@dataclass
class ToolLoopGate:
    _fsms: dict[str, ToolFSM] = field(default_factory=dict)
    _last_tool_name: str | None = None

    def check_before(
        self,
        tool_name: str,
        args_hash: str,
        policy: ToolPolicy,
    ) -> GateResult:
        """Pre-execution check. Returns gate decision and drives FSM transition."""

    def record_output(
        self,
        tool_name: str,
        args_hash: str,
        output_hash: str,
        policy: ToolPolicy,
    ) -> None:
        """Post-execution hook. Stores output hash for future output-identity checks.
        Records even for output_sensitive tools (for future telemetry analysis)."""
```

### Signal evaluation order in `check_before`

1. **Identity**: `fsm.signature_counts.get(args_hash, 0) >= loop_max_per_signature` → `BLOCK_IDENTITY`
   (`signature_counts[args_hash]` is incremented at the start of `check_before`, before any blocking check, so the count reflects the current call attempt)
2. **Output identity** (skipped if `loop_output_sensitive=True`): previous execution of same `(tool, args_hash)` produced identical `output_hash` → `BLOCK_OUTPUT`
3. **Consecutive — block**: `fsm.state == WARNED` → `BLOCK_CONSECUTIVE`
4. **Consecutive — warn**: `fsm.consecutive_count >= loop_max_consecutive` → `WARN_CONSECUTIVE`, transition to `WARNED`
5. Otherwise → `ALLOW`

**Consecutive counter**: incremented when `_last_tool_name == tool_name`, reset to 1 when it differs. The `WARNED → ACTIVE` reset happens when a different tool call is observed between consecutive calls to the warned tool.

**Output identity timing**: `record_output` is called *after* execution. The output-identity check fires on the *next* call with the same `(tool, args_hash)` — "we ran this before and got output X; running again with the same args will produce X again, which adds no new information."

---

## Section 3: Executor Integration

### `ExecutionContext` (`orchestrator/types.py`)

```python
# Remove:
tool_call_signatures: list[str]
# (force_synthesis_from_limit retained — still used by iteration ceiling)

# Add:
loop_gate: ToolLoopGate = field(default_factory=ToolLoopGate)
```

### `step_tool_execution` (`orchestrator/executor.py`)

Per tool call, replacing the existing `tool_call_signatures` dedup block and the `all_dedup_blocked` detection:

```python
args_hash = _stable_hash(arguments)          # sha256 of sort-keyed JSON
tool_policy = _get_tool_loop_policy(tool_name)  # TODO: lru_cache — static per process

# 1. Pre-execution gate check
gate_result = ctx.loop_gate.check_before(tool_name, args_hash, tool_policy)
log.info("tool_loop_gate", trace_id=ctx.trace_id, **dataclasses.asdict(gate_result))

if gate_result.decision in (BLOCK_CONSECUTIVE, BLOCK_IDENTITY, BLOCK_OUTPUT):
    tool_results.append(_blocked_result(tool_call_id, tool_name, gate_result))
    continue

# 2. Execute
result = await tool_layer.execute_tool(tool_name, arguments, trace_ctx)

# 3. Post-execution — register output hash
output_hash = _stable_hash(result.output)
ctx.loop_gate.record_output(tool_name, args_hash, output_hash, tool_policy)

# 4. Append gate warning hint to tool result if WARN_CONSECUTIVE
if gate_result.decision == GateDecision.WARN_CONSECUTIVE:
    result = _append_gate_warning(result, gate_result)
```

### Removed code

- `tool_call_signatures` list and all references
- The `all_dedup_blocked` detection block — this change was deployed but never committed; this spec supersedes it. The implementation must NOT apply that change separately; the FSM `WARNED → BLOCKED` transition replaces it.
- `orchestrator_max_repeated_tool_calls` setting retained as the default value for `loop_max_per_signature` when no per-tool policy is configured

### Policy lookup

```python
def _get_tool_loop_policy(tool_name: str) -> ToolPolicy:
    """Returns ToolPolicy for tool_name, or ToolPolicy() defaults if not configured.
    TODO: wrap with @lru_cache(maxsize=None) — governance config is process-static.
    """
```

---

## Section 4: Telemetry Hooks

Every `GateResult` is emitted as a structured log event:

```
tool_loop_gate | decision=warn_consecutive | tool=run_sysdiag |
  state_before=active | state_after=warned | consecutive_count=3 | total_calls=3
```

This provides Level 2 gate-decision observability (per the Four-Level Observability Framework). When the feedback loop is implemented, these events are the raw signal for learning which tools loop and under what thresholds, and for auto-tuning per-tool policy.

`record_output` stores output hashes even for `output_sensitive=True` tools — the data is captured for future analysis even though it doesn't drive blocking decisions today.

---

## Section 5: Testing

**New file:** `tests/orchestrator/test_loop_gate.py`

Key test cases (all pure unit tests, no orchestrator overhead):

| Test | Signal | Expected |
|------|--------|----------|
| Same args twice, `max_per_signature=1` | Identity | `BLOCK_IDENTITY` on second call |
| N consecutive calls | Consecutive | `WARN_CONSECUTIVE` at N, `BLOCK_CONSECUTIVE` at N+1 |
| Different tool between consecutive calls | Consecutive reset | Counter resets, `ALLOW` resumes |
| Same args + same output twice | Output identity | `BLOCK_OUTPUT` on third call |
| `output_sensitive=True`, same output | Output identity bypass | No `BLOCK_OUTPUT` |
| `WARNED → ACTIVE` reset | FSM transition | Different tool call unblocks consecutive |

Integration: extend `tests/test_tools/test_self_telemetry.py` with a fixture that pre-populates `ctx.loop_gate` in `WARNED` state and asserts synthesis is forced on next tool call.

---

## Files Changed

| File | Change |
|------|--------|
| `src/personal_agent/orchestrator/loop_gate.py` | **new** — FSM, gate, decisions, result |
| `src/personal_agent/governance/models.py` | add 3 fields to `ToolPolicy` |
| `config/governance/tools.yaml` | add `loop_*` to polling and write tools |
| `src/personal_agent/orchestrator/types.py` | swap `tool_call_signatures` → `loop_gate` |
| `src/personal_agent/orchestrator/executor.py` | replace dedup logic with gate calls; remove `all_dedup_blocked` |
| `src/personal_agent/config/settings.py` | keep `orchestrator_max_repeated_tool_calls` as fallback default |
| `tests/orchestrator/test_loop_gate.py` | **new** — FSM unit tests |

---

## Future Work (out of scope for this spec)

- `@lru_cache` on `_get_tool_loop_policy` (one-liner optimization)
- PWA config console for editing `tools.yaml` loop policies without raw YAML
- Feedback loop integration: auto-tune per-tool thresholds from `tool_loop_gate` events
