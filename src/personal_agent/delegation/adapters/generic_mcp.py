"""Generic MCP-capable agent delegation adapter — stub.

Fallback adapter for any MCP-capable agent harness. Stub until the
Seshat API Gateway (Phase C2) is deployed and the MCP client protocol
for agent-to-agent delegation is finalised.

See: docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md D6
"""

from __future__ import annotations

import structlog

from personal_agent.request_gateway.delegation_types import (
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

_NOT_IMPLEMENTED_MSG = (
    "GenericMCPAdapter is not yet implemented — pending Seshat API Gateway (Phase C2)"
)


class GenericMCPAdapter:
    """Fallback adapter for any MCP-capable external agent.

    Satisfies DelegationExecutorProtocol via structural subtyping.

    This adapter will be wired to the MCP client once the Seshat API Gateway
    is deployed. Until then it returns a not-implemented failure outcome so
    the delegation system can enumerate it without crashing.

    Attributes:
        _server_url: URL of the target agent's MCP server endpoint.
    """

    def __init__(self, server_url: str) -> None:
        """Initialise the adapter.

        Args:
            server_url: URL of the target agent's MCP server endpoint.
        """
        self._server_url = server_url

    def available(self) -> bool:
        """Return whether a server URL is configured.

        Returns:
            True if ``server_url`` is non-empty, False otherwise.
            Does NOT perform a live connectivity check.
        """
        return bool(self._server_url)

    async def delegate(
        self,
        package: DelegationPackage,
        timeout: float = 300.0,
        trace_ctx: TraceContext | None = None,
    ) -> DelegationOutcome:
        """Return a not-implemented failure outcome.

        Args:
            package: Delegation package (unused until implementation).
            timeout: Timeout in seconds (unused).
            trace_ctx: Trace context for telemetry correlation.

        Returns:
            DelegationOutcome with success=False and informative error message.
        """
        trace_id = trace_ctx.trace_id if trace_ctx else "unknown"
        log.info(
            "generic_mcp_adapter.not_implemented",
            trace_id=trace_id,
            task_id=package.task_id,
            server_url=self._server_url,
        )
        return DelegationOutcome(
            task_id=package.task_id,
            success=False,
            rounds_needed=0,
            what_worked="",
            what_was_missing=_NOT_IMPLEMENTED_MSG,
            duration_minutes=0.0,
        )
