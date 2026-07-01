"""High-level orchestrator API.

This module provides the public API for the orchestrator, matching
the interface defined in ORCHESTRATOR_CORE_SPEC_v0.1.md.
"""

from collections.abc import Sequence
from uuid import UUID

from personal_agent.brainstem import get_current_mode
from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.types import AttachmentRef, ExecutionContext, OrchestratorResult
from personal_agent.request_gateway.types import GatewayOutput
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.request_timer import RequestTimer
from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)


class Orchestrator:
    """High-level orchestrator interface.

    This class provides the main entry point for user requests as defined
    in the orchestrator specification.
    """

    def __init__(self, session_manager: SessionManager | None = None) -> None:
        """Initialize orchestrator with optional session manager.

        Args:
            session_manager: Optional session manager. If None, creates a new one.
        """
        self.session_manager = session_manager or SessionManager()

    async def handle_user_request(
        self,
        session_id: str,
        user_message: str,
        mode: Mode | None = None,
        channel: Channel | None = None,
        trace_id: str | None = None,
        request_timer: RequestTimer | None = None,
        gateway_output: GatewayOutput | None = None,
        user_id: UUID | None = None,
        user_email: str | None = None,
        user_display_name: str | None = None,
        eval_mode: bool = False,
        authenticated: bool = False,
        attachments: Sequence[AttachmentRef] | None = None,
    ) -> OrchestratorResult:
        """Top-level entrypoint for a single user turn.

        This is the main public API for the orchestrator. It creates an
        execution context and runs it through the state machine.

        Args:
            session_id: Session identifier for multi-turn conversations.
            user_message: The user's input message.
            mode: Optional operational mode. If None, queries brainstem
                for current mode.
            channel: Optional communication channel. If None, defaults to CHAT.
            trace_id: Optional trace ID from the entry point (e.g. service/CLI).
                If provided, used for request-to-reply latency tracing.
            request_timer: Optional RequestTimer for inline span-based timing.
                If provided, the orchestrator records timing spans for each phase.
            gateway_output: Optional GatewayOutput from the request gateway pipeline.
                When present, executor skips inline routing and uses pre-assembled context.
            user_id: Authenticated user UUID — passed through to TaskCapture for
                visibility scoping of written memory nodes (FRE-229).
            user_email: CF Access email of the connected user (FRE-213).
            user_display_name: Display name from the users table, if set (FRE-213).
            eval_mode: When True, side-effecting tools (e.g. create_linear_issue)
                refuse to execute. Set by the /chat endpoint when channel=EVAL.
            authenticated: Whether the request carries a verified CF Access identity.
                Threaded into the executor's memory-recall visibility scoping so
                'group'-visibility memory is revealed (FRE-229 / FRE-673).
            attachments: Structured attachment references for this turn (FRE-661 /
                ADR-0101 §2), kept separate from user_message so Captain's Log and
                entity extraction never see attachment metadata (AC-5).

        Returns:
            OrchestratorResult with reply, steps, and trace_id.
        """
        # Query current mode from brainstem if not provided
        if mode is None:
            mode = get_current_mode()
            log.debug(
                "mode_queried_from_brainstem",
                mode=mode.value,
                trace_id=trace_id,
                session_id=session_id,
            )

        # Default to CHAT channel if not provided
        if channel is None:
            channel = Channel.CHAT

        # Create or get session
        session = self.session_manager.get_session(session_id)
        if not session:
            # Create new session with the provided session_id if it doesn't exist
            self.session_manager.create_session(mode, channel, session_id=session_id)
            session = self.session_manager.get_session(session_id)

        # Use provided trace_id for request-to-reply tracing, or create new.
        # Propagate user_id / session_id so tool executors that receive
        # `ctx` can scope per-user (notes_*, recall_personal_history).
        if trace_id is not None:
            trace_ctx = TraceContext(trace_id=trace_id, user_id=user_id, session_id=session_id)
        else:
            trace_ctx = TraceContext.new_trace(user_id=user_id, session_id=session_id)

        # Create execution context with timer
        ctx = ExecutionContext(
            session_id=session_id,
            trace_id=trace_ctx.trace_id,
            user_message=user_message,
            mode=mode,
            channel=channel,
            request_timer=request_timer,
            gateway_output=gateway_output,
            user_id=user_id,
            user_email=user_email,
            user_display_name=user_display_name,
            eval_mode=eval_mode,
            authenticated=authenticated,
            attachments=tuple(attachments or ()),
        )

        # Execute task
        return await execute_task_safe(ctx, self.session_manager)
