"""Codex delegation adapter — REST API stub.

Codex API is not yet generally available. This stub satisfies the
DelegationExecutorProtocol so the adapter registry can enumerate it
without failing at import time.

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

_NOT_IMPLEMENTED_MSG = "Codex REST API adapter is not yet implemented (API not GA)"


class CodexAdapter:
    """Codex delegation adapter — REST API (stub).

    Satisfies DelegationExecutorProtocol via structural subtyping.

    This adapter will be implemented once the Codex API reaches general
    availability. Until then, ``available()`` always returns False and
    ``delegate()`` returns a failure outcome.
    """

    def available(self) -> bool:
        """Return False — Codex adapter is not yet implemented.

        Returns:
            Always False.
        """
        return False

    async def delegate(
        self,
        package: DelegationPackage,
        timeout: float = 300.0,
        trace_ctx: TraceContext | None = None,
    ) -> DelegationOutcome:
        """Return a not-implemented failure outcome.

        Args:
            package: Delegation package (unused).
            timeout: Timeout in seconds (unused).
            trace_ctx: Trace context (unused).

        Returns:
            DelegationOutcome with success=False and informative error message.
        """
        trace_id = trace_ctx.trace_id if trace_ctx else "unknown"
        log.info(
            "codex_adapter.not_implemented",
            trace_id=trace_id,
            task_id=package.task_id,
        )
        return DelegationOutcome(
            task_id=package.task_id,
            success=False,
            rounds_needed=0,
            what_worked="",
            what_was_missing=_NOT_IMPLEMENTED_MSG,
            duration_minutes=0.0,
        )
