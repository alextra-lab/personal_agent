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
