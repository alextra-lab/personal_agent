"""FastAPI service application."""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.brainstem.scheduler import BrainstemScheduler
from personal_agent.brainstem.sensors.metrics_daemon import (
    MetricsDaemon,
    set_global_metrics_daemon,
)
from personal_agent.captains_log.es_indexer import build_es_indexer_from_handler, set_es_indexer
from personal_agent.config.settings import get_settings
from personal_agent.governance.models import Mode
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway import run_gateway_pipeline
from personal_agent.security import sanitize_error_message
from personal_agent.service.database import AsyncSessionLocal, get_db_session, init_db
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
metrics_daemon: MetricsDaemon | None = None
mcp_adapter: "MCPGatewayAdapter | None" = None  # type: ignore  # noqa: F821

# Fire-and-forget assistant message appends: session_id -> task (FRE-51).
# Next request for same session awaits this so history is consistent before hydration.
_pending_append_tasks: dict[str, asyncio.Task[None]] = {}


async def _append_assistant_message_background(
    session_id: UUID,
    content: str,
    trace_id: str,
) -> None:
    """Append assistant message in a dedicated DB session (fire-and-forget).

    Used so the /chat response can return immediately; rapid follow-ups await
    this task before loading session history.
    """
    sid = str(session_id)
    try:
        async with AsyncSessionLocal() as db:
            repo = SessionRepository(db)
            await repo.append_message(session_id, {"role": "assistant", "content": content})
    except Exception as e:
        log.error(
            "db_append_assistant_message_background_failed",
            trace_id=trace_id,
            session_id=sid,
            error=sanitize_error_message(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
    finally:
        _pending_append_tasks.pop(sid, None)


def _parse_db_host_port(database_url: str) -> tuple[str, int]:
    """Extract host and port from a SQLAlchemy database URL.

    Args:
        database_url: Full database URL (e.g. postgresql+asyncpg://user:pw@host:5432/db)

    Returns:
        Tuple of (host, port).
    """
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    return host, port


async def _preflight_check_tcp(service: str, host: str, port: int) -> None:
    """Attempt a raw TCP connection to verify a service is reachable before startup.

    Args:
        service: Human-readable service name for log/error messages.
        host: Hostname or IP address to connect to.
        port: TCP port to connect to.

    Raises:
        RuntimeError: If the service is not reachable within 2 seconds, with an
            actionable message directing the developer to run 'make infra-up'.
    """
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError) as e:
        log.error(
            "startup_preflight_failed",
            service=service,
            host=host,
            port=port,
            remedy="Run 'make infra-up' to start required Docker services",
            error=str(e),
        )
        raise RuntimeError(
            f"{service} at {host}:{port} is unreachable — "
            f"run 'make infra-up' to start Docker services."
        ) from e


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan management."""
    global es_handler, memory_service, scheduler, metrics_daemon, mcp_adapter

    # Startup
    log.info("service_starting")

    # Pre-flight: verify PostgreSQL is reachable before attempting any DB operations
    pg_host, pg_port = _parse_db_host_port(settings.database_url)
    await _preflight_check_tcp("PostgreSQL", pg_host, pg_port)

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

    # Connect to Neo4j (if enabled) — non-fatal, matches ES graceful-degradation pattern
    if settings.enable_memory_graph:
        try:
            memory_service = MemoryService()
            await memory_service.connect()
            log.info("memory_service_initialized")
            # Ensure Neo4j vector index exists for embedding search (idempotent)
            try:
                await memory_service.ensure_vector_index()
                log.info("neo4j_vector_index_ensured")
            except Exception as idx_e:
                log.warning("neo4j_vector_index_setup_failed", error=str(idx_e))
        except Exception as e:
            log.warning(
                "memory_service_connect_failed",
                error=str(e),
                remedy="Neo4j may not be running. Run 'make infra-up'.",
            )
            memory_service = None

    metrics_daemon = MetricsDaemon(
        poll_interval_seconds=settings.metrics_daemon_poll_interval_seconds,
        es_emit_interval_seconds=settings.metrics_daemon_es_emit_interval_seconds,
        buffer_size=settings.metrics_daemon_buffer_size,
    )
    await metrics_daemon.start()
    app.state.metrics_daemon = metrics_daemon
    set_global_metrics_daemon(metrics_daemon)

    # MCP gateway before brainstem scheduler so promotion/feedback can use Linear (ADR-0040).
    if settings.mcp_gateway_enabled:
        try:
            from personal_agent.captains_log.linear_client import LinearClient
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
            if mcp_adapter.client:
                linear_client = LinearClient(mcp_adapter)
            else:
                linear_client = None
        except Exception as e:
            log.warning(
                "mcp_gateway_init_failed",
                error=sanitize_error_message(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            mcp_adapter = None
            linear_client = None
    else:
        linear_client = None

    # Start Brainstem scheduler for second brain, lifecycle, and/or insights tasks.
    if (
        settings.enable_second_brain
        or settings.data_lifecycle_enabled
        or settings.insights_enabled
        or getattr(settings, "quality_monitor_enabled", True)
        or getattr(settings, "promotion_pipeline_enabled", True)
        or getattr(settings, "feedback_polling_enabled", True)
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
            metrics_daemon=metrics_daemon,
            linear_client=linear_client,
        )
        await scheduler.start()
        log.info("brainstem_scheduler_started")

    log.info("service_ready", port=settings.service_port)

    yield

    # Shutdown
    log.info("service_shutting_down")

    set_global_metrics_daemon(None)
    daemon = cast(MetricsDaemon | None, getattr(app.state, "metrics_daemon", None))
    if daemon is not None:
        await daemon.stop()
    metrics_daemon = None

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

    # Await any in-flight assistant-message append for this session (FRE-51 edge case).
    sid = str(cast(UUID, session.session_id))
    if sid in _pending_append_tasks:
        await _pending_append_tasks.pop(sid)

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

    # --- Phase: gateway_pipeline ---
    # Compute expansion budget from brainstem sensors before pipeline.
    # Graceful degradation: budget defaults to 0 on any sensor failure.
    from personal_agent.brainstem.expansion import compute_expansion_budget
    from personal_agent.brainstem.sensors import poll_system_metrics

    try:
        system_metrics = poll_system_metrics()
        expansion_budget = compute_expansion_budget(
            system_metrics,
            max_budget=settings.expansion_budget_max,
        )
    except Exception:
        log.warning("expansion_budget_computation_failed", trace_id=trace_id, exc_info=True)
        expansion_budget = 0

    gateway_output = None
    try:
        with timer.span("gateway_pipeline"):
            memory_adapter = (
                MemoryServiceAdapter(service=memory_service)
                if memory_service and memory_service.driver
                else None
            )
            gateway_output = await run_gateway_pipeline(
                user_message=message,
                session_id=str(session.session_id),
                session_messages=prior_messages,
                trace_id=trace_id,
                mode=Mode.NORMAL,  # From brainstem in future
                memory_adapter=memory_adapter,
                expansion_budget=expansion_budget,
                full_session_messages=db_messages,
            )
    except Exception as e:
        log.warning(
            "gateway_pipeline_failed",
            trace_id=trace_id,
            error=sanitize_error_message(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        gateway_output = None

    # --- Phase: orchestrator ---
    result: Any = {}
    response_content = ""
    request_started = False
    try:
        from personal_agent.orchestrator import Orchestrator

        with timer.span("orchestrator_setup"):
            orchestrator = Orchestrator()
            session_manager = orchestrator.session_manager

            orchestrator_session = session_manager.get_session(str(session.session_id))
            if not orchestrator_session:
                from personal_agent.orchestrator.channels import Channel

                session_manager.create_session(
                    Mode.NORMAL, Channel.CHAT, session_id=str(session.session_id)
                )

            if prior_messages:
                session_manager.update_session(str(session.session_id), messages=prior_messages)

        if scheduler:
            scheduler.notify_request_start()
            request_started = True

        result = await orchestrator.handle_user_request(
            session_id=str(session.session_id),
            user_message=message,
            mode=None,
            channel=None,
            trace_id=trace_id,
            request_timer=timer,
            gateway_output=gateway_output,
        )

        response_content = result.get("reply", "No response generated")

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
    finally:
        if scheduler and request_started:
            scheduler.notify_request_end()

    # --- Emit timing breakdown (before response so timer reflects time-to-reply) ---
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
            es_handler.es_logger.index_request_trace(
                trace_id=trace_id,
                timer=timer,
                session_id=str(session.session_id),
            )
        )

    # Fire-and-forget DB write: return response immediately, append assistant message in background (FRE-51).
    session_uuid = cast(UUID, session.session_id)
    task = asyncio.create_task(
        _append_assistant_message_background(session_uuid, response_content, trace_id)
    )
    _pending_append_tasks[sid] = task

    return {
        "session_id": str(session.session_id),
        "response": response_content,
        "trace_id": trace_id,
    }


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
