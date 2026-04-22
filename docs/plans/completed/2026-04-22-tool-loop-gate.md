# Tool Loop Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the global dedup-threshold with a per-tool FSM-based loop detection gate that detects call identity, output identity, and consecutive repetition — eliminating the root cause of the tool loop bug.

**Architecture:** A new `loop_gate.py` module in `orchestrator/` defines `ToolLoopGate` — a registry of per-tool `ToolFSM` finite state machines. `ExecutionContext` holds one gate per request. `step_tool_execution` in `executor.py` delegates all loop decisions to the gate before and after each tool call. Per-tool thresholds live in `config/governance/tools.yaml`.

**Tech Stack:** Python 3.12 dataclasses, Pydantic (governance models), structlog (telemetry), pytest

**Spec:** `docs/superpowers/specs/2026-04-22-tool-loop-gate-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/personal_agent/orchestrator/loop_gate.py` | **Create** | FSM states, gate decisions, per-tool FSM, gate registry, stable hash |
| `tests/test_orchestrator/test_loop_gate.py` | **Create** | Unit tests for all gate signals and FSM transitions |
| `src/personal_agent/governance/models.py` | **Modify** | Add 3 loop fields to `ToolPolicy` |
| `config/governance/tools.yaml` | **Modify** | Add `loop_*` overrides for polling and write tools |
| `src/personal_agent/orchestrator/types.py` | **Modify** | Replace `tool_call_signatures` with `loop_gate: ToolLoopGate` |
| `src/personal_agent/orchestrator/executor.py` | **Modify** | Replace dedup block + remove `all_dedup_blocked`; add gate calls + helpers |

---

## Task 1: Add loop fields to `ToolPolicy` and `tools.yaml`

**Files:**
- Modify: `src/personal_agent/governance/models.py`
- Modify: `config/governance/tools.yaml`

- [ ] **Step 1: Add three fields to `ToolPolicy`**

In `src/personal_agent/governance/models.py`, find the `ToolPolicy` class (around line 100) and add after the existing `rate_limit_per_hour` field:

```python
    rate_limit_per_hour: int | None = Field(None, ge=0, description="Rate limit per hour")
    loop_max_per_signature: int = Field(
        default=1,
        ge=0,
        description="Max executions of the same (tool, args) pair per request",
    )
    loop_max_consecutive: int = Field(
        default=3,
        ge=1,
        description="Consecutive calls before warning; hard-block fires at this + 1",
    )
    loop_output_sensitive: bool = Field(
        default=False,
        description="If True, skip output-identity blocking (for polling tools whose output changes each call)",
    )
```

- [ ] **Step 2: Verify defaults load correctly**

```bash
cd /opt/seshat
uv run python -c "
from personal_agent.governance.models import ToolPolicy
p = ToolPolicy(category='network', allowed_in_modes=['NORMAL'])
assert p.loop_max_per_signature == 1
assert p.loop_max_consecutive == 3
assert p.loop_output_sensitive is False
print('ToolPolicy defaults OK')
"
```

Expected output: `ToolPolicy defaults OK`

- [ ] **Step 3: Add loop overrides for polling tools in `tools.yaml`**

In `config/governance/tools.yaml`, find each of the following tool entries and add the `loop_*` fields shown. Add them after the existing `rate_limit_per_hour` line (or at the end of the tool block if that field is absent).

For `run_sysdiag` (around line 358):
```yaml
  run_sysdiag:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    timeout_seconds: 65
    rate_limit_per_hour: 300
    loop_max_per_signature: 3
    loop_max_consecutive: 4
    loop_output_sensitive: true
```

For `self_telemetry_query` — this tool is registered natively but may not yet have a `tools.yaml` entry. Add a new entry near the end of the `tools:` section (before MCP tools):
```yaml
  self_telemetry_query:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    loop_max_per_signature: 2
    loop_max_consecutive: 3
    loop_output_sensitive: true

  infra_health:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED"]
    risk_level: "low"
    requires_approval: false
    loop_max_per_signature: 2
    loop_max_consecutive: 3
    loop_output_sensitive: true
```

For `create_linear_issue` (around line 1009):
```yaml
  create_linear_issue:
    category: "network"
    allowed_in_modes: ["NORMAL"]
    risk_level: "medium"
    requires_approval: false
    timeout_seconds: 30
    rate_limit_per_hour: 20
    loop_max_per_signature: 1
    loop_max_consecutive: 2
```

For `write_file` (around line 41):
```yaml
  write_file:
    category: "system_write"
    allowed_in_modes: ["NORMAL"]
    requires_approval_in_modes: ["ALERT", "DEGRADED", "RECOVERY"]
    forbidden_in_modes: ["LOCKDOWN"]
    allowed_paths:
      - "$HOME/**/personal_agent/**"
      - "$HOME/Documents/agent_workspace/**"
    forbidden_paths:
      - "/System/**"
      - "/Library/**"
      - "$HOME/.ssh/**"
    loop_max_per_signature: 1
    loop_max_consecutive: 2
```

- [ ] **Step 4: Verify governance config loads with new fields**

```bash
uv run python -c "
from personal_agent.config import load_governance_config
cfg = load_governance_config()
sysdiag = cfg.tools.get('run_sysdiag')
assert sysdiag is not None, 'run_sysdiag not found'
assert sysdiag.loop_max_per_signature == 3
assert sysdiag.loop_output_sensitive is True
print('tools.yaml loop fields OK')
"
```

Expected: `tools.yaml loop fields OK`

- [ ] **Step 5: Run type check**

```bash
make mypy
```

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/governance/models.py config/governance/tools.yaml
git commit -m "feat(governance): add loop detection fields to ToolPolicy and tools.yaml"
```

---

## Task 2: Create `loop_gate.py` — data structures and `stable_hash`

**Files:**
- Create: `src/personal_agent/orchestrator/loop_gate.py`
- Create: `tests/test_orchestrator/test_loop_gate.py`

- [ ] **Step 1: Write the failing import test**

Create `tests/test_orchestrator/test_loop_gate.py`:

```python
"""Unit tests for ToolLoopGate FSM-based loop detection."""

import pytest
from personal_agent.orchestrator.loop_gate import (
    GateDecision,
    GateResult,
    ToolCallState,
    ToolFSM,
    ToolLoopGate,
    ToolLoopPolicy,
    stable_hash,
)


def test_stable_hash_is_deterministic():
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_stable_hash_length():
    h = stable_hash("hello")
    assert len(h) == 16


def test_tool_fsm_initial_state():
    fsm = ToolFSM()
    assert fsm.state == ToolCallState.IDLE
    assert fsm.total_calls == 0
    assert fsm.consecutive_count == 0


def test_tool_loop_policy_defaults():
    p = ToolLoopPolicy()
    assert p.loop_max_per_signature == 1
    assert p.loop_max_consecutive == 3
    assert p.loop_output_sensitive is False


def test_gate_result_is_frozen():
    result = GateResult(
        decision=GateDecision.ALLOW,
        tool_name="test",
        state_before=ToolCallState.IDLE,
        state_after=ToolCallState.ACTIVE,
        reason="ok",
        consecutive_count=1,
        total_calls=1,
    )
    with pytest.raises((AttributeError, TypeError)):
        result.decision = GateDecision.BLOCK_IDENTITY  # type: ignore[misc]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v
```

Expected: `ModuleNotFoundError: No module named 'personal_agent.orchestrator.loop_gate'`

- [ ] **Step 3: Create `loop_gate.py` with data structures only (no gate logic yet)**

Create `src/personal_agent/orchestrator/loop_gate.py`:

```python
"""Tool loop detection gate using per-tool finite state machines.

Each tool call in a request is evaluated against three signals:
  1. Call identity: same (tool, args) pair called more than loop_max_per_signature times
  2. Output identity: same (tool, args) produced identical output on ≥2 prior executions
  3. Consecutiveness: same tool called N times in a row (loop_max_consecutive)

The gate is a registry of per-tool ToolFSM instances. ExecutionContext
holds one ToolLoopGate per request. All decisions are returned as GateResult
dataclasses for structured telemetry logging.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolCallState(str, Enum):
    """FSM states for a single tool's call history within a request."""

    IDLE = "idle"      # not yet called this request
    ACTIVE = "active"  # called, within all thresholds
    WARNED = "warned"  # consecutive threshold reached; hint injected, execution still allowed
    BLOCKED = "blocked"  # terminal; all further calls return a blocked result


class GateDecision(str, Enum):
    """Gate verdict — maps directly to FSM transitions."""

    ALLOW = "allow"                          # IDLE→ACTIVE or ACTIVE→ACTIVE
    WARN_CONSECUTIVE = "warn_consecutive"    # ACTIVE→WARNED (execute + inject hint)
    BLOCK_CONSECUTIVE = "block_consecutive"  # WARNED→BLOCKED
    BLOCK_IDENTITY = "block_identity"        # any→BLOCKED (same args exceeded limit)
    BLOCK_OUTPUT = "block_output"            # any→BLOCKED (same output hash seen ≥2x)


@dataclass(frozen=True)
class GateResult:
    """Structured record of a gate decision — logged as telemetry event."""

    decision: GateDecision
    tool_name: str
    state_before: ToolCallState
    state_after: ToolCallState
    reason: str
    consecutive_count: int
    total_calls: int


@dataclass
class ToolLoopPolicy:
    """Loop-specific policy for a tool. Extracted from governance ToolPolicy by executor."""

    loop_max_per_signature: int = 1      # max executions of same (tool, args) per request
    loop_max_consecutive: int = 3        # WARN at N consecutive calls; BLOCK at N+1
    loop_output_sensitive: bool = False  # if True, skip output-identity blocking


@dataclass
class ToolFSM:
    """Per-tool finite state machine tracking call patterns within a request."""

    state: ToolCallState = ToolCallState.IDLE
    total_calls: int = 0
    consecutive_count: int = 0
    # args_hash → call count (incremented in check_before before any blocking)
    signature_counts: dict[str, int] = field(default_factory=dict)
    # args_hash → [output_hash, ...] populated by record_output after each execution
    output_history: dict[str, list[str]] = field(default_factory=dict)


def stable_hash(value: Any) -> str:
    """Compute a stable 16-char hex hash of any JSON-serializable value.

    Uses sort_keys=True for dict stability. Falls back to repr() for
    non-serializable values.
    """
    try:
        serialized = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = repr(value)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


@dataclass
class ToolLoopGate:
    """Per-request registry of per-tool FSMs for deterministic loop detection.

    Instantiate one per request in ExecutionContext. Call check_before() before
    each tool execution and record_output() after.
    """

    _fsms: dict[str, ToolFSM] = field(default_factory=dict)
    _last_tool_name: str | None = None

    def _get_or_create_fsm(self, tool_name: str) -> ToolFSM:
        if tool_name not in self._fsms:
            self._fsms[tool_name] = ToolFSM()
        return self._fsms[tool_name]

    def check_before(
        self,
        tool_name: str,
        args_hash: str,
        policy: ToolLoopPolicy,
    ) -> GateResult:
        """Pre-execution check. Drives FSM transition. Returns gate decision.

        Not yet implemented — added in Tasks 3, 4, 5.
        """
        raise NotImplementedError

    def record_output(
        self,
        tool_name: str,
        args_hash: str,
        output_hash: str,
        policy: ToolLoopPolicy,
    ) -> None:
        """Post-execution hook. Stores output hash for output-identity detection.

        Not yet implemented — added in Task 5.
        """
        raise NotImplementedError
```

- [ ] **Step 4: Run tests to verify data structure tests pass**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v
```

Expected: 5 tests pass (the `NotImplementedError` tests are not yet written).

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/orchestrator/loop_gate.py tests/test_orchestrator/test_loop_gate.py
git commit -m "feat(loop-gate): data structures — ToolCallState, GateDecision, GateResult, ToolFSM, ToolLoopGate skeleton"
```

---

## Task 3: Implement call-identity signal in `check_before`

**Files:**
- Modify: `tests/test_orchestrator/test_loop_gate.py`
- Modify: `src/personal_agent/orchestrator/loop_gate.py`

- [ ] **Step 1: Write failing identity tests**

Append to `tests/test_orchestrator/test_loop_gate.py`:

```python
# ── Identity signal tests ──────────────────────────────────────────────────


def test_first_call_is_allowed():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.ALLOW
    assert result.state_before == ToolCallState.IDLE
    assert result.state_after == ToolCallState.ACTIVE


def test_second_call_same_args_blocked_when_max_is_one():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.BLOCK_IDENTITY
    assert result.state_after == ToolCallState.BLOCKED


def test_different_args_not_blocked_by_identity():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    result = gate.check_before("web_search", "hash_xyz", policy)
    assert result.decision == GateDecision.ALLOW


def test_identity_respects_per_tool_max():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=2)
    gate.check_before("run_sysdiag", "hash_same", policy)
    result2 = gate.check_before("run_sysdiag", "hash_same", policy)
    assert result2.decision == GateDecision.ALLOW  # second call within limit
    result3 = gate.check_before("run_sysdiag", "hash_same", policy)
    assert result3.decision == GateDecision.BLOCK_IDENTITY  # third call exceeds limit


def test_blocked_tool_stays_blocked():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=1)
    gate.check_before("web_search", "hash_abc", policy)
    gate.check_before("web_search", "hash_abc", policy)  # → BLOCKED
    result = gate.check_before("web_search", "hash_abc", policy)
    assert result.decision == GateDecision.BLOCK_IDENTITY
    assert result.state_after == ToolCallState.BLOCKED
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v -k "identity"
```

Expected: `NotImplementedError`

- [ ] **Step 3: Implement identity signal in `check_before`**

Replace the `check_before` method body in `loop_gate.py`:

```python
    def check_before(
        self,
        tool_name: str,
        args_hash: str,
        policy: ToolLoopPolicy,
    ) -> GateResult:
        """Pre-execution check. Drives FSM transition. Returns gate decision.

        Evaluation order (first match wins):
          1. Call identity: signature_counts[args_hash] > loop_max_per_signature
          2. Output identity: ≥2 prior identical outputs for same args (Task 5)
          3. Consecutive block: state is WARNED — grace turn used (Task 4)
          4. Consecutive warn: consecutive_count >= loop_max_consecutive (Task 4)
          5. Allow
        """
        fsm = self._get_or_create_fsm(tool_name)
        state_before = fsm.state

        # Update consecutive counter
        if self._last_tool_name == tool_name:
            fsm.consecutive_count += 1
        else:
            fsm.consecutive_count = 1
            # WARNED → ACTIVE reset when a different tool ran in between
            if fsm.state == ToolCallState.WARNED:
                fsm.state = ToolCallState.ACTIVE
        self._last_tool_name = tool_name

        # Increment signature call count and total before any blocking check
        fsm.signature_counts[args_hash] = fsm.signature_counts.get(args_hash, 0) + 1
        fsm.total_calls += 1

        # Signal 1: Call identity
        if fsm.signature_counts[args_hash] > policy.loop_max_per_signature:
            fsm.state = ToolCallState.BLOCKED
            return GateResult(
                decision=GateDecision.BLOCK_IDENTITY,
                tool_name=tool_name,
                state_before=state_before,
                state_after=ToolCallState.BLOCKED,
                reason=(
                    f"Same args called {fsm.signature_counts[args_hash]}x, "
                    f"max={policy.loop_max_per_signature}"
                ),
                consecutive_count=fsm.consecutive_count,
                total_calls=fsm.total_calls,
            )

        # Signals 2, 3, 4 — to be added in Tasks 4 and 5

        # Allow — transition IDLE → ACTIVE on first call
        if fsm.state == ToolCallState.IDLE:
            fsm.state = ToolCallState.ACTIVE
        return GateResult(
            decision=GateDecision.ALLOW,
            tool_name=tool_name,
            state_before=state_before,
            state_after=fsm.state,
            reason="within thresholds",
            consecutive_count=fsm.consecutive_count,
            total_calls=fsm.total_calls,
        )
```

- [ ] **Step 4: Run identity tests**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v -k "identity or first_call or different_args or blocked_tool"
```

Expected: all pass.

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
make test
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/orchestrator/loop_gate.py tests/test_orchestrator/test_loop_gate.py
git commit -m "feat(loop-gate): implement call-identity signal in check_before"
```

---

## Task 4: Implement consecutive signal (warn + block + reset)

**Files:**
- Modify: `tests/test_orchestrator/test_loop_gate.py`
- Modify: `src/personal_agent/orchestrator/loop_gate.py`

- [ ] **Step 1: Write failing consecutive tests**

Append to `tests/test_orchestrator/test_loop_gate.py`:

```python
# ── Consecutive signal tests ───────────────────────────────────────────────


def test_consecutive_warn_at_threshold():
    gate = ToolLoopGate()
    # max_consecutive=2: WARN fires on the 2nd consecutive call
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("run_sysdiag", "hash_a", policy)  # consecutive=1, ALLOW
    result = gate.check_before("run_sysdiag", "hash_b", policy)  # consecutive=2, WARN
    assert result.decision == GateDecision.WARN_CONSECUTIVE
    assert result.state_after == ToolCallState.WARNED


def test_consecutive_block_after_warn():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("run_sysdiag", "hash_a", policy)
    gate.check_before("run_sysdiag", "hash_b", policy)  # → WARNED
    result = gate.check_before("run_sysdiag", "hash_c", policy)  # → BLOCKED
    assert result.decision == GateDecision.BLOCK_CONSECUTIVE
    assert result.state_after == ToolCallState.BLOCKED


def test_consecutive_counter_resets_when_different_tool_runs():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    gate.check_before("run_sysdiag", "hash_a", policy)
    gate.check_before("run_sysdiag", "hash_b", policy)  # → WARNED
    gate.check_before("web_search", "hash_q", ToolLoopPolicy())  # different tool
    result = gate.check_before("run_sysdiag", "hash_c", policy)  # consecutive=1, ALLOW (reset from WARNED→ACTIVE)
    assert result.decision == GateDecision.ALLOW
    assert result.state_after == ToolCallState.ACTIVE


def test_two_tools_alternating_do_not_trigger_consecutive():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    for i in range(5):
        r1 = gate.check_before("tool_a", f"hash_{i}a", policy)
        r2 = gate.check_before("tool_b", f"hash_{i}b", policy)
        assert r1.decision == GateDecision.ALLOW
        assert r2.decision == GateDecision.ALLOW


def test_gate_result_includes_consecutive_count():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=5)
    for i in range(3):
        gate.check_before("run_sysdiag", f"hash_{i}", policy)
    result = gate.check_before("run_sysdiag", "hash_3", policy)
    assert result.consecutive_count == 4
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v -k "consecutive"
```

Expected: FAIL (consecutive logic not yet implemented).

- [ ] **Step 3: Add consecutive signals to `check_before`**

In `loop_gate.py`, replace the `# Signals 2, 3, 4` comment with:

```python
        # Signal 3a: Consecutive block — grace turn (WARNED) already used
        if fsm.state == ToolCallState.WARNED:
            fsm.state = ToolCallState.BLOCKED
            return GateResult(
                decision=GateDecision.BLOCK_CONSECUTIVE,
                tool_name=tool_name,
                state_before=state_before,
                state_after=ToolCallState.BLOCKED,
                reason=(
                    f"Consecutive calls exceeded after warning "
                    f"({fsm.consecutive_count} consecutive)"
                ),
                consecutive_count=fsm.consecutive_count,
                total_calls=fsm.total_calls,
            )

        # Signal 3b: Consecutive warn — first threshold breach, one grace turn follows
        if fsm.consecutive_count >= policy.loop_max_consecutive:
            fsm.state = ToolCallState.WARNED
            return GateResult(
                decision=GateDecision.WARN_CONSECUTIVE,
                tool_name=tool_name,
                state_before=state_before,
                state_after=ToolCallState.WARNED,
                reason=(
                    f"Consecutive threshold reached "
                    f"({fsm.consecutive_count}/{policy.loop_max_consecutive})"
                ),
                consecutive_count=fsm.consecutive_count,
                total_calls=fsm.total_calls,
            )

        # Signal 2 placeholder — added in Task 5
```

- [ ] **Step 4: Run consecutive tests**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v -k "consecutive"
```

Expected: all pass.

- [ ] **Step 5: Run full test suite**

```bash
make test
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/orchestrator/loop_gate.py tests/test_orchestrator/test_loop_gate.py
git commit -m "feat(loop-gate): implement consecutive warn/block signals and WARNED→ACTIVE reset"
```

---

## Task 5: Implement `record_output` and output-identity signal

**Files:**
- Modify: `tests/test_orchestrator/test_loop_gate.py`
- Modify: `src/personal_agent/orchestrator/loop_gate.py`

- [ ] **Step 1: Write failing output-identity tests**

Append to `tests/test_orchestrator/test_loop_gate.py`:

```python
# ── Output identity signal tests ───────────────────────────────────────────


def test_record_output_does_not_raise():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=5)
    gate.check_before("self_telemetry_query", "hash_args", policy)
    gate.record_output("self_telemetry_query", "hash_args", "hash_out_1", policy)  # no error


def test_block_output_identity_after_two_identical_outputs():
    gate = ToolLoopGate()
    # max_per_signature=5 so identity won't block; output_sensitive=False (default)
    policy = ToolLoopPolicy(loop_max_per_signature=5, loop_max_consecutive=10)
    # Call 1
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_same", policy)
    # Call 2
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_same", policy)  # identical!
    # Call 3 — output-identity should block
    result = gate.check_before("query_es", "hash_args", policy)
    assert result.decision == GateDecision.BLOCK_OUTPUT
    assert result.state_after == ToolCallState.BLOCKED


def test_different_outputs_do_not_trigger_output_block():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=5, loop_max_consecutive=10)
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_1", policy)
    gate.check_before("query_es", "hash_args", policy)
    gate.record_output("query_es", "hash_args", "out_2", policy)  # different!
    result = gate.check_before("query_es", "hash_args", policy)
    assert result.decision != GateDecision.BLOCK_OUTPUT


def test_output_sensitive_bypasses_output_identity_block():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(
        loop_max_per_signature=5,
        loop_max_consecutive=10,
        loop_output_sensitive=True,
    )
    gate.check_before("run_sysdiag", "hash_args", policy)
    gate.record_output("run_sysdiag", "hash_args", "out_same", policy)
    gate.check_before("run_sysdiag", "hash_args", policy)
    gate.record_output("run_sysdiag", "hash_args", "out_same", policy)
    result = gate.check_before("run_sysdiag", "hash_args", policy)
    # Should NOT be BLOCK_OUTPUT — output_sensitive=True bypasses this check
    assert result.decision != GateDecision.BLOCK_OUTPUT


def test_output_sensitive_still_records_for_telemetry():
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=5, loop_output_sensitive=True)
    gate.check_before("run_sysdiag", "hash_args", policy)
    gate.record_output("run_sysdiag", "hash_args", "out_hash", policy)
    fsm = gate._fsms["run_sysdiag"]
    assert "hash_args" in fsm.output_history
    assert fsm.output_history["hash_args"] == ["out_hash"]
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v -k "output"
```

Expected: `NotImplementedError` for `record_output`.

- [ ] **Step 3: Implement `record_output` and add output-identity signal to `check_before`**

Replace the `record_output` method body in `loop_gate.py`:

```python
    def record_output(
        self,
        tool_name: str,
        args_hash: str,
        output_hash: str,
        policy: ToolLoopPolicy,  # noqa: ARG002 — reserved for future per-tool recording config
    ) -> None:
        """Post-execution hook. Records output hash for output-identity detection.

        Always records even for output_sensitive=True tools, so future telemetry
        and feedback loop analysis can observe actual output variation.
        """
        fsm = self._get_or_create_fsm(tool_name)
        if args_hash not in fsm.output_history:
            fsm.output_history[args_hash] = []
        fsm.output_history[args_hash].append(output_hash)
```

In `check_before`, replace the `# Signal 2 placeholder` comment with:

```python
        # Signal 2: Output identity (skipped for output-sensitive tools)
        if not policy.loop_output_sensitive:
            prior_outputs = fsm.output_history.get(args_hash, [])
            if len(prior_outputs) >= 2 and len(set(prior_outputs)) == 1:
                fsm.state = ToolCallState.BLOCKED
                return GateResult(
                    decision=GateDecision.BLOCK_OUTPUT,
                    tool_name=tool_name,
                    state_before=state_before,
                    state_after=ToolCallState.BLOCKED,
                    reason=(
                        f"Identical output seen {len(prior_outputs)}x for same args "
                        f"(hash={prior_outputs[0][:8]})"
                    ),
                    consecutive_count=fsm.consecutive_count,
                    total_calls=fsm.total_calls,
                )
```

- [ ] **Step 4: Run output-identity tests**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v -k "output"
```

Expected: all pass.

- [ ] **Step 5: Run full test suite**

```bash
make test
```

Expected: all pass.

- [ ] **Step 6: Run mypy**

```bash
make mypy
```

Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
git add src/personal_agent/orchestrator/loop_gate.py tests/test_orchestrator/test_loop_gate.py
git commit -m "feat(loop-gate): implement record_output and output-identity signal"
```

---

## Task 6: Update `ExecutionContext` — swap `tool_call_signatures` for `loop_gate`

**Files:**
- Modify: `src/personal_agent/orchestrator/types.py`

- [ ] **Step 1: Add `ToolLoopGate` import and replace field**

In `src/personal_agent/orchestrator/types.py`:

1. Add a runtime import (needed by `default_factory=ToolLoopGate`) after the existing imports at the top of `types.py`, outside `TYPE_CHECKING`:
```python
from personal_agent.orchestrator.loop_gate import ToolLoopGate
```

   Do NOT add it to the `TYPE_CHECKING` block — `default_factory` requires the class at runtime, not just for type checking. The existing `TYPE_CHECKING` block should remain unchanged:
```python
if TYPE_CHECKING:
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.expansion_types import ExpansionPlan, PhaseResult
    from personal_agent.orchestrator.sub_agent_types import SubAgentResult
    from personal_agent.telemetry.request_timer import RequestTimer
```

3. In `ExecutionContext`, find the line:
```python
    tool_call_signatures: list[str] = field(default_factory=list)
```
   Replace it with:
```python
    loop_gate: "ToolLoopGate" = field(default_factory=ToolLoopGate)
```

   The `default_factory=ToolLoopGate` requires the runtime import above (not just `TYPE_CHECKING`).

- [ ] **Step 2: Verify no other references to `tool_call_signatures`**

```bash
grep -rn "tool_call_signatures" /opt/seshat/src/ /opt/seshat/tests/
```

Expected: zero results (only `executor.py` references remain — those are removed in Task 7).

- [ ] **Step 3: Run type check**

```bash
make mypy
```

Expected: no new errors.

- [ ] **Step 4: Run tests**

```bash
make test
```

Expected: tests that construct `ExecutionContext` directly will fail if they reference `tool_call_signatures`. Fix any such tests by removing the field reference — `loop_gate` is created automatically by `default_factory`.

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/orchestrator/types.py
git commit -m "feat(orchestrator): replace tool_call_signatures with loop_gate in ExecutionContext"
```

---

## Task 7: Wire `ToolLoopGate` into `step_tool_execution`

**Files:**
- Modify: `src/personal_agent/orchestrator/executor.py`

This is the largest change. Make each sub-step separately before running tests.

- [ ] **Step 1: Add imports to `executor.py`**

In `executor.py`, add to the existing import block (after the `personal_agent.*` imports):

```python
from personal_agent.orchestrator.loop_gate import (
    GateDecision,
    GateResult,
    ToolLoopGate,
    ToolLoopPolicy,
    stable_hash,
)
```

- [ ] **Step 2: Add module-level helpers after imports**

Add these three functions near the top of `executor.py`, after the import block and before the first function definition:

```python
# ── Tool loop gate helpers ─────────────────────────────────────────────────

_cached_governance_config: object = None


def _get_cached_governance_config() -> object:
    """Module-level governance config cache. TODO: replace with @lru_cache after config singleton."""
    global _cached_governance_config
    if _cached_governance_config is None:
        from personal_agent.config import load_governance_config
        _cached_governance_config = load_governance_config()
    return _cached_governance_config


def _get_tool_loop_policy(tool_name: str) -> ToolLoopPolicy:
    """Returns loop policy for tool_name, or ToolLoopPolicy() defaults if not configured."""
    try:
        gov_config = _get_cached_governance_config()
        tool_policy = gov_config.tools.get(tool_name)  # type: ignore[union-attr]
        if tool_policy is None:
            return ToolLoopPolicy()
        return ToolLoopPolicy(
            loop_max_per_signature=tool_policy.loop_max_per_signature,
            loop_max_consecutive=tool_policy.loop_max_consecutive,
            loop_output_sensitive=tool_policy.loop_output_sensitive,
        )
    except Exception:
        return ToolLoopPolicy()


def _gate_blocked_result(
    tool_call_id: str,
    tool_name: str,
    gate_result: GateResult,
) -> dict[str, Any]:
    """Formats a tool result dict for gate-blocked calls."""
    hints: dict[GateDecision, str] = {
        GateDecision.BLOCK_IDENTITY: (
            "Already retrieved these results. Use the previous tool output to answer."
        ),
        GateDecision.BLOCK_OUTPUT: (
            "Retrieved the same result before. Use the previous tool output to answer."
        ),
        GateDecision.BLOCK_CONSECUTIVE: (
            f"{tool_name} called too many times consecutively. "
            "Synthesize from already-gathered results."
        ),
    }
    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "name": tool_name,
        "content": json.dumps({
            "status": "done",
            "hint": hints.get(gate_result.decision, "Tool call blocked by loop gate."),
            "gate_decision": gate_result.decision.value,
        }),
    }
```

- [ ] **Step 3: Remove the old dedup block (lines ~1736–1766)**

Find and **remove** the following block in `step_tool_execution` (it starts with the comment `# Repeat-call detection: prevent identical tool call signatures from looping`):

```python
        # Repeat-call detection: prevent identical tool call signatures from looping
        try:
            args_signature = json.dumps(arguments, sort_keys=True)
        except TypeError:
            # Non-JSON-serializable args shouldn't happen; fall back to repr
            args_signature = repr(arguments)
        call_signature = f"{tool_name}:{args_signature}"
        repeats = ctx.tool_call_signatures.count(call_signature)
        if repeats >= settings.orchestrator_max_repeated_tool_calls:
            log.warning(
                "repeated_tool_call_blocked",
                ...
            )
            tool_results.append({...})
            continue
        ctx.tool_call_signatures.append(call_signature)
```

Replace it with the new gate check block:

```python
        # Loop gate: pre-execution check
        args_hash = stable_hash(arguments)
        loop_policy = _get_tool_loop_policy(tool_name)
        gate_result = ctx.loop_gate.check_before(tool_name, args_hash, loop_policy)
        log.info(
            "tool_loop_gate",
            trace_id=ctx.trace_id,
            decision=gate_result.decision.value,
            tool_name=gate_result.tool_name,
            state_before=gate_result.state_before.value,
            state_after=gate_result.state_after.value,
            reason=gate_result.reason,
            consecutive_count=gate_result.consecutive_count,
            total_calls=gate_result.total_calls,
        )
        if gate_result.decision in (
            GateDecision.BLOCK_IDENTITY,
            GateDecision.BLOCK_OUTPUT,
            GateDecision.BLOCK_CONSECUTIVE,
        ):
            tool_results.append(_gate_blocked_result(tool_call_id, tool_name, gate_result))
            continue
```

- [ ] **Step 4: Add `record_output` + consecutive warning after tool execution**

Find the `tool_results.append(...)` block after successful tool execution (around line 1836). It currently reads:

```python
            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": content,
                }
            )
```

Replace with:

```python
            # Loop gate: record output for output-identity detection
            output_hash = stable_hash(result.output)
            ctx.loop_gate.record_output(tool_name, args_hash, output_hash, loop_policy)

            # Inject gate warning into result content if consecutive threshold just hit
            if gate_result.decision == GateDecision.WARN_CONSECUTIVE:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        parsed["_gate_warning"] = (
                            f"{tool_name} called {gate_result.consecutive_count} times "
                            f"consecutively. Consider synthesizing from gathered results."
                        )
                        content = json.dumps(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass

            tool_results.append(
                {
                    "tool_call_id": tool_call_id,
                    "role": "tool",
                    "name": tool_name,
                    "content": content,
                }
            )
```

- [ ] **Step 5: Remove the `all_dedup_blocked` block**

Find and **remove** the entire block that starts with:

```python
    # If every tool call this turn was dedup-blocked ("done" hint), the model is looping
```

This block (approximately lines 1902–1925) ends with `ctx.force_synthesis_from_limit = True`. Delete it entirely — the FSM `WARNED → BLOCKED` transition replaces this logic.

- [ ] **Step 6: Run type check**

```bash
make mypy
```

Fix any type errors before proceeding.

- [ ] **Step 7: Run full test suite**

```bash
make test
```

Expected: all pass. If any executor test directly checks for `"status": "done"` hint strings, update them to check for `"gate_decision"` in the blocked content.

- [ ] **Step 8: Commit**

```bash
git add src/personal_agent/orchestrator/executor.py
git commit -m "feat(orchestrator): wire ToolLoopGate into step_tool_execution — replace dedup block with FSM gate"
```

---

## Task 8: Integration test and final verification

**Files:**
- Modify: `tests/test_orchestrator/test_loop_gate.py`

- [ ] **Step 1: Add an integration-style test for the full gate + executor path**

Append to `tests/test_orchestrator/test_loop_gate.py`:

```python
# ── Integration-style tests ────────────────────────────────────────────────


def test_full_request_scenario_self_telemetry():
    """Simulates a request where self_telemetry_query is called twice with same args.
    With loop_output_sensitive=True and loop_max_per_signature=2, both should ALLOW.
    Third call should BLOCK_IDENTITY.
    """
    gate = ToolLoopGate()
    policy = ToolLoopPolicy(loop_max_per_signature=2, loop_output_sensitive=True)
    args_hash = stable_hash({"query_type": "health"})

    r1 = gate.check_before("self_telemetry_query", args_hash, policy)
    assert r1.decision == GateDecision.ALLOW
    gate.record_output("self_telemetry_query", args_hash, stable_hash({"status": "ok"}), policy)

    r2 = gate.check_before("self_telemetry_query", args_hash, policy)
    assert r2.decision == GateDecision.ALLOW
    gate.record_output("self_telemetry_query", args_hash, stable_hash({"status": "ok"}), policy)

    r3 = gate.check_before("self_telemetry_query", args_hash, policy)
    assert r3.decision == GateDecision.BLOCK_IDENTITY  # 3rd call exceeds max=2


def test_consecutive_warn_then_synthesis_via_different_tool():
    """After a WARN, calling a different tool resets the FSM to ACTIVE."""
    gate = ToolLoopGate()
    diag_policy = ToolLoopPolicy(loop_max_per_signature=10, loop_max_consecutive=2)
    search_policy = ToolLoopPolicy()

    gate.check_before("run_sysdiag", "h1", diag_policy)
    r_warn = gate.check_before("run_sysdiag", "h2", diag_policy)
    assert r_warn.decision == GateDecision.WARN_CONSECUTIVE

    gate.check_before("web_search", "hq", search_policy)  # different tool

    r_resume = gate.check_before("run_sysdiag", "h3", diag_policy)
    assert r_resume.decision == GateDecision.ALLOW  # consecutive reset


def test_gate_result_fields_are_complete():
    """GateResult always has all fields populated."""
    gate = ToolLoopGate()
    policy = ToolLoopPolicy()
    result = gate.check_before("web_search", stable_hash({"q": "test"}), policy)
    assert result.tool_name == "web_search"
    assert result.consecutive_count >= 1
    assert result.total_calls >= 1
    assert result.reason != ""
```

- [ ] **Step 2: Run all loop gate tests**

```bash
uv run pytest tests/test_orchestrator/test_loop_gate.py -v
```

Expected: all pass.

- [ ] **Step 3: Run full test suite and type check**

```bash
make test && make mypy && make ruff-check
```

Expected: all pass with no new errors.

- [ ] **Step 4: Push to remote**

```bash
git push origin main
```

- [ ] **Step 5: Final commit if any fixups were needed**

```bash
git add -p  # stage only relevant changes
git commit -m "test(loop-gate): integration-style gate scenario tests"
git push origin main
```

---

## Verification Checklist

Before marking implementation complete, confirm:

- [ ] `uv run pytest tests/test_orchestrator/test_loop_gate.py -v` — all pass
- [ ] `make test` — no regressions
- [ ] `make mypy` — no new errors
- [ ] `make ruff-check` — no new lint errors
- [ ] `grep -rn "tool_call_signatures" src/ tests/` — zero results
- [ ] `grep -rn "all_dedup_blocked" src/` — zero results
- [ ] `grep -n "tool_loop_gate" src/personal_agent/orchestrator/executor.py` — confirms gate logging is present
