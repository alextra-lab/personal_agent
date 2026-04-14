"""ADR-0049 Phase 1: Protocol interface for context assembly.

Defines the structural contract for the Pre-LLM Gateway's context assembler
(Stages 6+7). Consumers that need to build LLM message lists depend on this
protocol rather than the concrete ``assemble_context`` function, enabling
test doubles and alternative assemblers.

See: docs/architecture_decisions/ADR-0049.md
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from personal_agent.request_gateway.types import AssembledContext, IntentResult


class ContextAssemblerProtocol(Protocol):
    """Protocol for assembling LLM context from session history and memory.

    Structural contract for the Stage 6+7 pipeline step. The assembler
    combines session history, Seshat memory enrichment, and the current
    user message into a final message list ready for the primary agent.

    Key invariants:
        - Returned ``AssembledContext.messages`` is always non-empty (at minimum
          contains the current user message).
        - Assembler must not mutate ``session_messages`` in place.
        - If memory is unavailable, context assembly degrades gracefully.
    """

    async def assemble(
        self,
        user_message: str,
        session_messages: Sequence[dict[str, Any]],
        intent: IntentResult,
        trace_id: str,
        session_id: str,
    ) -> AssembledContext:
        """Assemble the full LLM context for the primary agent.

        Args:
            user_message: The current user message to process.
            session_messages: Prior conversation history in OpenAI message format.
            intent: Classified intent from Gateway Stage 4.
            trace_id: Request trace identifier for observability.
            session_id: Client session identifier for memory scoping.

        Returns:
            AssembledContext with messages and enrichment metadata.
        """
        ...
