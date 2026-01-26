"""FastAPI service application."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.brainstem.scheduler import BrainstemScheduler
from personal_agent.config.settings import get_settings
from personal_agent.memory.service import MemoryService
from personal_agent.service.database import get_db_session, init_db
from personal_agent.service.models import SessionCreate, SessionResponse, SessionUpdate
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import add_elasticsearch_handler, get_logger
from personal_agent.telemetry.es_handler import ElasticsearchHandler

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
        log.info("elasticsearch_logging_enabled")

    # Connect to Neo4j (if enabled)
    if settings.enable_memory_graph:
        memory_service = MemoryService()
        await memory_service.connect()
        log.info("memory_service_initialized")

    # Start Brainstem scheduler for second brain (Phase 2.2)
    if settings.enable_second_brain:
        scheduler = BrainstemScheduler()
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
                error=str(e),
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
            log.error("mcp_gateway_shutdown_error", error=str(e), exc_info=True)

    if es_handler:
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


@app.get("/health")
async def health_check():
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
):
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
):
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
):
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
):
    """List recent sessions."""
    repo = SessionRepository(db)
    return await repo.list_recent(limit)


# ============================================================================
# Chat Endpoint (Main Entry Point)
# ============================================================================


@app.post("/chat")
async def chat(
    message: str,
    session_id: str | None = None,
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
):
    """Process a chat message.

    This is the main entry point for user interactions.

    Args:
        message: User's message
        session_id: Optional existing session ID (creates new if not provided)
        db: Database session (injected by FastAPI)

    Returns:
        Response with assistant message and session_id
    """
    repo = SessionRepository(db)

    # Get or create session
    if session_id:
        session = await repo.get(UUID(session_id))
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
    else:
        session = await repo.create(SessionCreate())

    # Append user message
    await repo.append_message(session.session_id, {"role": "user", "content": message})

    # Call orchestrator (Phase 2.2: with memory enrichment)
    try:
        from personal_agent.orchestrator import Orchestrator

        # Create orchestrator with session manager
        orchestrator = Orchestrator()
        session_manager = orchestrator.session_manager

        # Get or create session in orchestrator's session manager
        orchestrator_session = session_manager.get_session(str(session.session_id))
        if not orchestrator_session:
            from personal_agent.governance.models import Mode
            from personal_agent.orchestrator.channels import Channel

            session_manager.create_session(
                Mode.NORMAL, Channel.CHAT, session_id=str(session.session_id)
            )

        # Handle request via orchestrator
        result = await orchestrator.handle_user_request(
            session_id=str(session.session_id),
            user_message=message,
            mode=None,  # Will query brainstem
            channel=None,  # Defaults to CHAT
        )

        response_content = result.get("reply", "No response generated")

        # Record request completion for scheduler
        if scheduler:
            scheduler.record_request()

    except Exception as e:
        log.error("orchestrator_call_failed", error=str(e), exc_info=True)
        response_content = f"Error processing request: {str(e)}"

    # Append assistant message
    await repo.append_message(
        session.session_id, {"role": "assistant", "content": response_content}
    )

    # Store conversation in memory graph (Phase 2.2) - basic version
    # Full entity extraction happens in second brain consolidation
    if memory_service and memory_service.connected:
        try:
            from uuid import uuid4

            from personal_agent.memory.models import ConversationNode

            # Create a basic conversation node (entities will be extracted by second brain)
            conversation = ConversationNode(
                conversation_id=str(uuid4()),
                trace_id=result.get("trace_id") if "trace_id" in locals() else None,
                session_id=str(session.session_id),
                timestamp=datetime.now(timezone.utc),
                summary=None,  # Will be filled by second brain
                user_message=message,
                assistant_response=response_content,
                key_entities=[],  # Will be extracted by second brain
                properties={},
            )
            await memory_service.create_conversation(conversation)
        except Exception as e:
            log.warning("memory_conversation_storage_failed", error=str(e), exc_info=True)

    return {"session_id": str(session.session_id), "response": response_content}


# ============================================================================
# Memory Endpoints (Phase 2.2)
# ============================================================================


@app.get("/memory/interests")
async def get_user_interests(limit: int = 20):
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
async def query_memory(query: dict):
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
    result = await memory_service.query_memory(memory_query)
    return result.model_dump()
