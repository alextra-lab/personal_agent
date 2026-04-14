"""Delegation executor protocol for external agent harnesses.

This module defines the structural contract for external agent delegation
adapters (ADR-0050 D6). The existing DelegationPackage and DelegationOutcome
types from Slice 2 (request_gateway.delegation_types) are the canonical types —
this module re-exports them and adds the executor protocol.

See: docs/architecture_decisions/ADR-0050-remote-agent-harness-integration.md
"""

from __future__ import annotations

from typing import Protocol

from personal_agent.request_gateway.delegation_types import (
    DelegationContext,
    DelegationOutcome,
    DelegationPackage,
)
from personal_agent.telemetry.trace import TraceContext

__all__ = [
    "DelegationContext",
    "DelegationOutcome",
    "DelegationPackage",
    "DelegationExecutorProtocol",
]


class DelegationExecutorProtocol(Protocol):
    """Protocol for external agent delegation adapters.

    Implementations wrap a specific external agent harness (Claude Code CLI,
    Codex REST API, generic MCP, etc.) and adapt the DelegationPackage/
    DelegationOutcome contract to that harness's invocation model.

    ADR-0049 modularity principle: new agent integrations are new adapter
    implementations, not modifications to the orchestrator.

    Key invariants:
        - ``available`` MUST be cheap (no network I/O) and idempotent.
        - ``delegate`` MUST return DelegationOutcome regardless of outcome;
          it MUST NOT raise exceptions to the caller.
        - Depth limiting is the caller's responsibility; adapters are stateless.
    """

    async def delegate(
        self,
        package: DelegationPackage,
        timeout: float,
        trace_ctx: TraceContext,
    ) -> DelegationOutcome:
        """Execute a delegation via the external agent.

        Args:
            package: Structured delegation package with task, context, and
                constraints. The ``target_agent`` field identifies which agent
                should process this package.
            timeout: Maximum seconds to wait for completion.
            trace_ctx: Trace context for telemetry correlation.

        Returns:
            DelegationOutcome with success flag, result text, and artifacts.
            Never raises — errors are encoded in the outcome.
        """
        ...

    def available(self) -> bool:
        """Check if the external agent is reachable without I/O.

        Returns:
            True if the agent CLI/API appears available locally
            (e.g., binary exists in PATH, API key configured).
            Does NOT perform a live connectivity check.
        """
        ...
