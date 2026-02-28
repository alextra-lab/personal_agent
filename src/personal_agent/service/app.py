"""FastAPI service application."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, cast
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.brainstem.scheduler import BrainstemScheduler
from personal_agent.captains_log.es_indexer import build_es_indexer_from_handler, set_es_indexer
from personal_agent.config.settings import get_settings
from personal_agent.memory.service import MemoryService
from personal_agent.security import sanitize_error_message
from personal_agent.service.database import get_db_session, init_db
from personal_agent.service.models import SessionCreate, SessionResponse, SessionUpdate
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import add_elasticsearch_handler, get_logger
from personal_agent.telemetry.es_handler import ElasticsearchHandler
from personal_agent.telemetry.request_timer import RequestTimer

log = get_logger(__name__)
settings = get_settings()

# Global instances (initialized in lifespan)
es_handler: ElasticsearchHandler | None = None
memory_service: MemoryService | None = None
scheduler: BrainstemScheduler | None = None
mcp_adapter: "MCPGatewayAdapter | None" = None  # type: ignore  # noqa: F821


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan management."""
    global es_handler, memory_service, scheduler, mcp_adapter

    # Startup
    log.info("service_starting")

    # Initialize database
    await init_db()
    log.info("database_initialized")

    # Connect to Elasticsearch and integrate with logging
    es_handler = ElasticsearchHandler(settings.elasticsearch_url)
    if await es_handler.connect():
        add_elasticsearch_handler(es_handler)
        set_es_indexer(build_es_indexer_from_handler(es_handler))
        log.info("elasticsearch_logging_enabled")

        # Captain's Log → ES indexing (Phase 2.3): pass handler during lifespan
        from personal_agent.captains_log.capture import (
            set_default_es_handler as set_capture_es_handler,
        )
        from personal_agent.captains_log.manager import CaptainLogManager

        set_capture_es_handler(es_handler)
        CaptainLogManager.set_default_es_handler(es_handler)
        log.info("captains_log_es_indexing_enabled")

        # Captain's Log ES backfill (FRE-30): one replay pass on startup
        try:
            from personal_agent.captains_log.backfill import run_backfill

            asyncio.create_task(run_backfill(es_handler.es_logger))
        except Exception as e:
            log.warning("captains_log_backfill_startup_failed", error=str(e))

    # Connect to Neo4j (if enabled)
    if settings.enable_memory_graph:
        memory_service = MemoryService()
        await memory_service.connect()
        log.info("memory_service_initialized")

    # Start Brainstem scheduler for second brain, lifecycle, and/or insights tasks.
    if (
        settings.enable_second_brain
        or settings.data_lifecycle_enabled
        or settings.insights_enabled
        or getattr(settings, "quality_monitor_enabled", True)
    ):
        es_client = (
            es_handler.es_logger.client
            if (es_handler and getattr(es_handler, "_connected", False))
            else None
        )
        backfill_logger = (
            es_handler.es_logger
            if (es_handler and getattr(es_handler, "_connected", False))
            else None
        )
        scheduler = BrainstemScheduler(
            lifecycle_es_client=es_client,
            backfill_es_logger=backfill_logger,
            memory_service=memory_service,
        )
        await scheduler.start()
        log.info("brainstem_scheduler_started")

    # Initialize MCP gateway (Phase 2.3+)
    if settings.mcp_gateway_enabled:
        try:
            from personal_agent.mcp.gateway import MCPGatewayAdapter
            from personal_agent.tools import get_default_registry

            log.info("mcp_gateway_initializing", command=settings.mcp_gateway_command)
            registry = get_default_registry()
            mcp_adapter = MCPGatewayAdapter(registry)
            await mcp_adapter.initialize()
            log.info(
                "mcp_gateway_initialized",
                tools_count=len(mcp_adapter._mcp_tool_names),
                tools=list(mcp_adapter._mcp_tool_names)[:10],  # Log first 10 tools
            )
        except Exception as e:
            log.warning(
                "mcp_gateway_init_failed",
                error=sanitize_error_message(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            mcp_adapter = None

    log.info("service_ready", port=settings.service_port)

    yield

    # Shutdown
    log.info("service_shutting_down")

    if scheduler:
        await scheduler.stop()

    if mcp_adapter:
        try:
            await mcp_adapter.shutdown()
        except Exception as e:
            log.error("mcp_gateway_shutdown_error", error=sanitize_error_message(e), exc_info=True)

    if es_handler:
        from personal_agent.captains_log.capture import (
            set_default_es_handler as set_capture_es_handler,
        )
        from personal_agent.captains_log.manager import CaptainLogManager

        set_capture_es_handler(None)
        CaptainLogManager.set_default_es_handler(None)
        set_es_indexer(None)
        await es_handler.disconnect()

    if memory_service:
        await memory_service.disconnect()

    log.info("service_stopped")


app = FastAPI(
    title="Personal Agent Service",
    description="Cognitive architecture service with persistent memory",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================================
# Health Check
# ============================================================================

HealthResponse = dict[str, Any]


@app.get("/health")
async def health_check() -> HealthResponse:
    """Service health check endpoint."""
    return {
        "status": "healthy",
        "components": {
            "database": "connected",
            "elasticsearch": "connected"
            if es_handler and es_handler._connected
            else "disconnected",
            "neo4j": "connected" if memory_service and memory_service.connected else "disconnected",
            "second_brain": "running" if scheduler and scheduler.running else "stopped",
            "mcp_gateway": "connected"
            if mcp_adapter and getattr(mcp_adapter, "client", None)
            else "disconnected",
        },
    }


# ============================================================================
# Session Endpoints
# ============================================================================


@app.post("/sessions", response_model=SessionResponse)
async def create_session(
    data: SessionCreate,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> SessionResponse:
    """Create a new session."""
    repo = SessionRepository(db)
    session = await repo.create(data)

    # Log session creation (now automatic via ES handler)
    log.info(
        "session_created",
        session_id=str(session.session_id),
        channel=session.channel,
        mode=session.mode,
    )

    return session


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> SessionResponse:
    """Get session by ID."""
    repo = SessionRepository(db)
    session = await repo.get(UUID(session_id))

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@app.patch("/sessions/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    data: SessionUpdate,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> SessionResponse:
    """Update session."""
    repo = SessionRepository(db)
    session = await repo.update(UUID(session_id), data)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@app.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    limit: int = 50,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[SessionResponse]:
    """List recent sessions."""
    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit)
    return cast(list[SessionResponse], sessions)


# ============================================================================
# Chat Endpoint (Main Entry Point)
# ============================================================================


@app.post("/chat")
async def chat(
    message: str,
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, str]:
    """Process a chat message.

    This is the main entry point for user interactions.

    Args:
        message: User's message
        session_id: Optional existing session ID (creates new if not provided)
        db: Database session (injected by FastAPI)

    Returns:
        Response with assistant message and session_id
    """
    from personal_agent.telemetry.events import REQUEST_RECEIVED, REQUEST_TIMING

    trace_id = str(uuid4())
    timer = RequestTimer(trace_id=trace_id)

    log.info(
        REQUEST_RECEIVED,
        trace_id=trace_id,
        entry="service",
        message_length=len(message),
    )
    repo = SessionRepository(db)

    # --- Phase: session_db_lookup ---
    with timer.span("session_db_lookup"):
        if session_id:
            session = await repo.get(UUID(session_id))
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
        else:
            session = await repo.create(SessionCreate())

    # --- Phase: session_hydration ---
    with timer.span("session_hydration"):
        db_messages = list(session.messages or [])
        max_history = settings.conversation_max_history_messages
        prior_messages = db_messages[-max_history:] if max_history > 0 else db_messages

    # --- Phase: db_append_user_message ---
    with timer.span("db_append_user_message"):
        await repo.append_message(
            cast(UUID, session.session_id), {"role": "user", "content": message}
        )

    # --- Phase: orchestrator ---
    result: dict[str, Any] = {}
    response_content = ""
    try:
        from personal_agent.orchestrator import Orchestrator

        with timer.span("orchestrator_setup"):
            orchestrator = Orchestrator()
            session_manager = orchestrator.session_manager

            orchestrator_session = session_manager.get_session(str(session.session_id))
            if not orchestrator_session:
                from personal_agent.governance.models import Mode
                from personal_agent.orchestrator.channels import Channel

                session_manager.create_session(
                    Mode.NORMAL, Channel.CHAT, session_id=str(session.session_id)
                )

            if prior_messages:
                session_manager.update_session(str(session.session_id), messages=prior_messages)

        result = await orchestrator.handle_user_request(
            session_id=str(session.session_id),
            user_message=message,
            mode=None,
            channel=None,
            trace_id=trace_id,
            request_timer=timer,
        )

        response_content = result.get("reply", "No response generated")

        if scheduler:
            scheduler.record_request()

    except Exception as e:
        error_id = str(uuid4())[:8]
        log.error(
            "orchestrator_call_failed",
            error_id=error_id,
            error=sanitize_error_message(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        sanitized_msg = sanitize_error_message(e)
        response_content = f"{sanitized_msg} (Error ID: {error_id})"

    # --- Phase: db_append_assistant_message ---
    with timer.span("db_append_assistant_message"):
        await repo.append_message(
            cast(UUID, session.session_id), {"role": "assistant", "content": response_content}
        )

    # --- Phase: memory_storage ---
    if memory_service and memory_service.connected:
        with timer.span("memory_storage"):
            try:
                from personal_agent.memory.models import ConversationNode

                conversation = ConversationNode(
                    conversation_id=str(uuid4()),
                    trace_id=result.get("trace_id") if result else None,
                    session_id=str(session.session_id),
                    timestamp=datetime.now(timezone.utc),
                    summary=None,
                    user_message=message,
                    assistant_response=response_content,
                    key_entities=[],
                    properties={},
                )
                await memory_service.create_conversation(conversation)
            except Exception as e:
                log.warning(
                    "memory_conversation_storage_failed",
                    error=sanitize_error_message(e),
                    exc_info=True,
                )

    # --- Emit timing breakdown ---
    breakdown = timer.to_breakdown()
    total_ms = timer.get_total_ms()

    log.info(
        REQUEST_TIMING,
        trace_id=trace_id,
        session_id=str(session.session_id),
        total_ms=total_ms,
        phases=[
            {"phase": s["phase"], "duration_ms": s["duration_ms"], "offset_ms": s["offset_ms"]}
            for s in breakdown
        ],
    )

    # Index to Elasticsearch (non-blocking)
    if es_handler and getattr(es_handler, "_connected", False):
        asyncio.create_task(
            es_handler.es_logger.index_request_timing(
                trace_id=trace_id,
                breakdown=breakdown,
                session_id=str(session.session_id),
                total_ms=total_ms,
            )
        )

    return {"session_id": str(session.session_id), "response": response_content}


# ============================================================================
# Memory Endpoints (Phase 2.2)
# ============================================================================


@app.get("/memory/interests")
async def get_user_interests(limit: int = 20) -> dict[str, Any]:
    """Get user interest profile (frequently mentioned entities).

    Args:
        limit: Maximum number of entities to return

    Returns:
        List of entities sorted by mention frequency
    """
    if not memory_service or not memory_service.connected:
        raise HTTPException(status_code=503, detail="Memory service not available")

    entities = await memory_service.get_user_interests(limit=limit)
    return {"entities": [e.model_dump() for e in entities]}


@app.post("/memory/query")
async def query_memory(query: dict[str, Any]) -> dict[str, Any]:
    """Query memory graph for related conversations and entities.

    Args:
        query: Query parameters (entity_names, entity_types, limit, etc.)

    Returns:
        Memory query results with conversations and entities
    """
    if not memory_service or not memory_service.connected:
        raise HTTPException(status_code=503, detail="Memory service not available")

    from personal_agent.memory.models import MemoryQuery

    memory_query = MemoryQuery(**query)
    result = await memory_service.query_memory(
        memory_query,
        feedback_key=query.get("session_id"),
        query_text=query.get("query_text"),
    )
    return result.model_dump()
