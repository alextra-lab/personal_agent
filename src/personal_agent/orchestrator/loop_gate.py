"""Tool loop detection gate using per-tool finite state machines.

Each tool call in a request is evaluated against three signals:
  1. Call identity: same (tool, args) pair called more than loop_max_per_signature times
  2. Output identity: same (tool, args) produced identical output on ≥2 prior executions
  3. Consecutiveness: same tool called N times in a row (loop_max_consecutive)

Signal severity (ADR-0063 §D5):
  - Output identity: terminal (BLOCK_OUTPUT). Identical output is pathological.
  - Call identity: advisory (ADVISE_IDENTITY) up to max+2, then terminal (BLOCK_IDENTITY).
    Retries after transient errors are often legitimate.
  - Consecutiveness: advisory only (WARN_CONSECUTIVE). Reading N files in a row is legitimate.

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

#: Number of advisory calls permitted above loop_max_per_signature before
#: the identity signal escalates to terminal BLOCK_IDENTITY.
IDENTITY_TERMINAL_OFFSET = 2


class ToolCallState(str, Enum):
    """FSM states for a single tool's call history within a request."""

    IDLE = "idle"  # not yet called this request
    ACTIVE = "active"  # called, within all thresholds
    BLOCKED = "blocked"  # terminal; all further calls return a blocked result


class GateDecision(str, Enum):
    """Gate verdict — maps directly to FSM transitions.

    Advisory decisions (ADVISE_IDENTITY, WARN_CONSECUTIVE) allow execution
    but inject a hint into the tool result content. Terminal decisions
    (BLOCK_IDENTITY, BLOCK_OUTPUT) skip dispatch entirely.
    """

    ALLOW = "allow"  # IDLE→ACTIVE or ACTIVE→ACTIVE
    WARN_CONSECUTIVE = "warn_consecutive"  # advisory: same tool N times in a row (execute + hint)
    ADVISE_IDENTITY = "advise_identity"  # advisory: same args > max (execute + hint)
    BLOCK_IDENTITY = "block_identity"  # terminal: same args > max + IDENTITY_TERMINAL_OFFSET
    BLOCK_OUTPUT = "block_output"  # terminal: same output hash seen ≥2x


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

    loop_max_per_signature: int = 1  # max executions of same (tool, args) before advisory
    loop_max_consecutive: int = 2  # WARN_CONSECUTIVE fires at N consecutive calls
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
          1. Call identity terminal: count > max + IDENTITY_TERMINAL_OFFSET → BLOCK_IDENTITY
          2. Call identity advisory: count > max → ADVISE_IDENTITY (execute + hint)
          3. Consecutive advisory: count >= loop_max_consecutive → WARN_CONSECUTIVE (execute + hint)
          4. Output identity: ≥2 prior identical outputs for same args → BLOCK_OUTPUT
          5. Allow
        """
        fsm = self._get_or_create_fsm(tool_name)
        state_before = fsm.state

        # Update consecutive counter — reset when a different tool runs
        if self._last_tool_name == tool_name:
            fsm.consecutive_count += 1
        else:
            fsm.consecutive_count = 1
        self._last_tool_name = tool_name

        # Increment signature call count and total before any blocking check
        fsm.signature_counts[args_hash] = fsm.signature_counts.get(args_hash, 0) + 1
        fsm.total_calls += 1

        # Signal 1a: Call identity terminal
        terminal_threshold = policy.loop_max_per_signature + IDENTITY_TERMINAL_OFFSET
        if fsm.signature_counts[args_hash] > terminal_threshold:
            fsm.state = ToolCallState.BLOCKED
            return GateResult(
                decision=GateDecision.BLOCK_IDENTITY,
                tool_name=tool_name,
                state_before=state_before,
                state_after=ToolCallState.BLOCKED,
                reason=(
                    f"Same args called {fsm.signature_counts[args_hash]}x, "
                    f"terminal ceiling={terminal_threshold}"
                ),
                consecutive_count=fsm.consecutive_count,
                total_calls=fsm.total_calls,
            )

        # Signal 1b: Call identity advisory — allow execution but inject hint
        if fsm.signature_counts[args_hash] > policy.loop_max_per_signature:
            return GateResult(
                decision=GateDecision.ADVISE_IDENTITY,
                tool_name=tool_name,
                state_before=state_before,
                state_after=fsm.state,  # stays ACTIVE — advisory does not block
                reason=(
                    f"Same args called {fsm.signature_counts[args_hash]}x "
                    f"(advisory window: >{policy.loop_max_per_signature} to <={terminal_threshold})"
                ),
                consecutive_count=fsm.consecutive_count,
                total_calls=fsm.total_calls,
            )

        # Signal 3: Consecutive advisory — warn but never block
        if fsm.consecutive_count >= policy.loop_max_consecutive:
            return GateResult(
                decision=GateDecision.WARN_CONSECUTIVE,
                tool_name=tool_name,
                state_before=state_before,
                state_after=fsm.state,  # stays ACTIVE — advisory does not block
                reason=(
                    f"Consecutive threshold reached "
                    f"({fsm.consecutive_count}/{policy.loop_max_consecutive})"
                ),
                consecutive_count=fsm.consecutive_count,
                total_calls=fsm.total_calls,
            )

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
