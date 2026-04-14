"""ADR-0049 Phase 1: Protocol interfaces for UI transport and visualization.

Defines structural contracts for streaming UI event delivery (AG-UI compatible)
and rich visualization payloads. Future implementations may target WebSocket,
SSE, AG-UI, or other transports without changing consumer code.

See: docs/architecture_decisions/ADR-0049.md
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class UITransportProtocol(Protocol):
    """Protocol for streaming UI event transport (AG-UI compatible).

    Structural contract for real-time delivery of text deltas, tool events,
    state snapshots, and human-in-the-loop interrupts from the agent to a
    connected frontend.

    Key invariants:
        - All methods are non-blocking coroutines; callers should not assume
          delivery is synchronous with the return.
        - ``session_id`` scopes events to a single client session; implementations
          must not cross-deliver events between sessions.
        - ``send_interrupt`` suspends agent execution until the human responds;
          return value carries the human's decision payload.
    """

    async def send_text_delta(self, text: str, session_id: str) -> None:
        """Stream an incremental text chunk to the UI.

        Args:
            text: Partial text token or chunk to deliver.
            session_id: Target session identifier.
        """
        ...

    async def send_tool_event(self, event: Any, session_id: str) -> None:
        """Deliver a tool lifecycle event to the UI (start, result, error).

        Args:
            event: Tool event payload (implementation-defined structure).
            session_id: Target session identifier.
        """
        ...

    async def send_state(self, state: Mapping[str, Any], session_id: str) -> None:
        """Push a full agent state snapshot to the UI.

        Args:
            state: JSON-serialisable state mapping (e.g. mode, memory summary).
            session_id: Target session identifier.
        """
        ...

    async def send_interrupt(self, context: Any, session_id: str) -> Any:
        """Suspend execution and wait for a human decision.

        Sends an interrupt payload to the UI, then awaits the human's response
        before returning. Used for approval gates and human-in-the-loop flows.

        Args:
            context: Interrupt context payload (e.g. tool approval request).
            session_id: Target session identifier.

        Returns:
            Human decision payload (structure defined by the interrupt type).
        """
        ...


class VisualizationProtocol(Protocol):
    """Protocol for rich visualization payloads (charts, diagrams).

    Structural contract for rendering domain-specific visualizations — such
    as memory graphs, metrics charts, or architecture diagrams — and
    delivering them to the connected UI session.

    Key invariants:
        - ``render_chart`` accepts a Vega-Lite or Chart.js spec (Mapping);
          implementations interpret the schema format.
        - ``render_diagram`` accepts a Mermaid-compatible source string.
        - Both methods are best-effort; failure must not crash the agent loop.
    """

    async def render_chart(self, spec: Mapping[str, Any], session_id: str) -> None:
        """Render and deliver a chart from a declarative spec.

        Args:
            spec: Chart specification (e.g. Vega-Lite JSON schema).
            session_id: Target session identifier.
        """
        ...

    async def render_diagram(self, source: str, session_id: str) -> None:
        """Render and deliver a diagram from a text source.

        Args:
            source: Diagram source in a text-based format (e.g. Mermaid DSL).
            session_id: Target session identifier.
        """
        ...
