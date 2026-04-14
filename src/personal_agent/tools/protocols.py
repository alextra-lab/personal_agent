"""ADR-0049 Phase 1: Protocol interface for tool execution.

Defines the structural contract that tool execution implementations must satisfy.
Enables dependency inversion: orchestrator and gateway code depends on
ToolExecutorProtocol, not ToolExecutionLayer directly.

See: docs/architecture_decisions/ADR-0049.md
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.types import ToolDefinition, ToolResult


class ToolExecutorProtocol(Protocol):
    """Protocol for tool discovery and execution.

    Structural contract for ToolExecutionLayer and any future sandboxed
    or remote execution backends. Implementations handle governance checks,
    argument validation, telemetry emission, and the actual invocation.

    Key invariants:
        - ``execute`` never raises; failures are encoded in ToolResult.success.
        - ``list_available`` reflects the currently registered and mode-allowed tools.
        - All tool calls are recorded in telemetry via the provided TraceContext.
    """

    async def execute_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        trace_ctx: TraceContext,
    ) -> ToolResult:
        """Execute a named tool with governance and observability.

        Args:
            tool_name: Name of the tool to invoke.
            arguments: Keyword arguments to pass to the tool executor.
            trace_ctx: Trace context for span creation and log correlation.

        Returns:
            ToolResult with success flag, output, error, and latency_ms populated.
        """
        ...

    def list_available(self) -> Sequence[ToolDefinition]:
        """Return all tool definitions currently available for execution.

        Returns:
            Sequence of ToolDefinition objects describing registered tools.
        """
        ...
