"""FastAPI service application."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from personal_agent.brainstem import (
    get_current_mode,
    get_mode_controller,
    get_mode_manager,
)
from personal_agent.brainstem.scheduler import BrainstemScheduler
from personal_agent.brainstem.sensors.metrics_daemon import (
    MetricsDaemon,
    set_global_metrics_daemon,
)
from personal_agent.captains_log.es_indexer import build_es_indexer_from_handler, set_es_indexer
from personal_agent.config.settings import get_settings
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter
from personal_agent.memory.service import MemoryService
from personal_agent.request_gateway import run_gateway_pipeline
from personal_agent.security import sanitize_error_message
from personal_agent.service.auth import RequestUser, get_request_user
from personal_agent.service.database import AsyncSessionLocal, get_db_session, init_db
from personal_agent.service.models import SessionCreate, SessionResponse, SessionUpdate
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import add_elasticsearch_handler, get_logger
from personal_agent.telemetry.es_handler import ElasticsearchHandler
from personal_agent.telemetry.request_timer import RequestTimer
from personal_agent.transport.agui.endpoint import get_event_queue
from personal_agent.transport.events import TextDeltaEvent

log = get_logger(__name__)
settings = get_settings()

# Global instances (initialized in lifespan)
es_handler: ElasticsearchHandler | None = None
memory_service: MemoryService | None = None
scheduler: BrainstemScheduler | None = None
metrics_daemon: MetricsDaemon | None = None
mcp_adapter: "MCPGatewayAdapter | None" = None  # type: ignore  # noqa: F821
consumer_runner: "ConsumerRunner | None" = None  # type: ignore  # noqa: F821
freshness_consumer: "FreshnessConsumer | None" = None  # type: ignore  # noqa: F821
cost_gate: "CostGate | None" = None  # type: ignore  # noqa: F821
cost_gate_reaper_task: asyncio.Task[None] | None = None

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
            await repo.append_message(
                session_id,
                {
                    "role": "assistant",
                    "content": content,
                    "trace_id": trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"source": "service.app"},
                },
            )
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


async def _process_chat_stream_background(
    session_id: str,
    message: str,
    profile_name: str,
    user_id: UUID,
) -> None:
    """Run the full orchestrator pipeline and push the result to the SSE queue.

    Runs as a fire-and-forget ``asyncio.Task``.  A ``None`` sentinel is always
    pushed to the SSE queue, even on error, so the client stream closes cleanly.

    Args:
        session_id: Client-generated session UUID (used for SSE queue key and DB).
        message: User's message text.
        profile_name: Execution profile name (e.g. ``"local"``, ``"cloud"``).
        user_id: Authenticated user UUID — used for session ownership scoping.
    """
    from personal_agent.config.profile import load_profile, set_current_profile

    queue = get_event_queue(session_id)
    trace_id = str(uuid4())

    try:
        # Wire execution profile so LLM factory dispatches to the correct model.
        try:
            _profile = load_profile(profile_name)
            set_current_profile(_profile)
        except FileNotFoundError:
            log.warning(
                "chat_stream.unknown_profile",
                profile=profile_name,
                trace_id=trace_id,
            )

        # ── Session ──────────────────────────────────────────────────────
        session_uuid = UUID(session_id)
        db_messages: list[dict] = []
        prior_messages: list[dict] = []

        async with AsyncSessionLocal() as db:
            repo = SessionRepository(db)
            session = await repo.get(session_uuid, user_id=user_id)
            if not session:
                # Create with client-provided UUID so history is addressable
                # across turns without the client needing to track a separate DB ID.
                from personal_agent.service.models import SessionModel

                now = datetime.now(timezone.utc)
                session = SessionModel(
                    session_id=session_uuid,
                    user_id=user_id,
                    created_at=now,
                    last_active_at=now,
                    mode="NORMAL",
                    channel="CHAT",
                    metadata_={},
                    messages=[],
                )
                db.add(session)
                await db.commit()
                await db.refresh(session)

            db_messages = list(session.messages or [])
            max_history = settings.conversation_max_history_messages
            prior_messages = db_messages[-max_history:] if max_history > 0 else db_messages
            await repo.append_message(
                session_uuid,
                {
                    "role": "user",
                    "content": message,
                    "trace_id": trace_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"source": "service.app"},
                },
            )

        # ── Gateway pipeline ─────────────────────────────────────────────
        from personal_agent.brainstem.expansion import compute_expansion_budget
        from personal_agent.brainstem.sensors import poll_system_metrics

        try:
            system_metrics = poll_system_metrics()
            expansion_budget = compute_expansion_budget(
                system_metrics, max_budget=settings.expansion_budget_max
            )
        except Exception:
            log.warning("chat_stream.expansion_budget_failed", trace_id=trace_id, exc_info=True)
            expansion_budget = 0

        gateway_output = None
        try:
            from personal_agent.memory.protocol_adapter import MemoryServiceAdapter

            memory_adapter = (
                MemoryServiceAdapter(service=memory_service)
                if memory_service and memory_service.driver
                else None
            )
            gateway_output = await run_gateway_pipeline(
                user_message=message,
                session_id=session_id,
                session_messages=prior_messages,
                trace_id=trace_id,
                mode=get_current_mode(),
                memory_adapter=memory_adapter,
                expansion_budget=expansion_budget,
                full_session_messages=db_messages,
                user_id=user_id,
                authenticated=True,
            )
        except Exception as e:
            log.warning(
                "chat_stream.gateway_pipeline_failed",
                trace_id=trace_id,
                error=sanitize_error_message(e),
            )

        # ── Orchestrator ─────────────────────────────────────────────────
        response_content = ""
        request_started = False
        try:
            from personal_agent.orchestrator import Orchestrator
            from personal_agent.orchestrator.channels import Channel

            orchestrator = Orchestrator()
            session_mgr = orchestrator.session_manager

            if not session_mgr.get_session(session_id):
                session_mgr.create_session(get_current_mode(), Channel.CHAT, session_id=session_id)
            if prior_messages:
                session_mgr.update_session(session_id, messages=prior_messages)

            if scheduler:
                scheduler.notify_request_start()
                request_started = True

            result = await orchestrator.handle_user_request(
                session_id=session_id,
                user_message=message,
                mode=None,
                channel=None,
                trace_id=trace_id,
                request_timer=RequestTimer(trace_id=trace_id),
                gateway_output=gateway_output,
                user_id=user_id,
            )
            response_content = result.get("reply", "No response generated")

        except Exception as e:
            error_id = str(uuid4())[:8]
            log.error(
                "chat_stream.orchestrator_failed",
                error_id=error_id,
                trace_id=trace_id,
                error=sanitize_error_message(e),
                exc_info=True,
            )
            # Do not include exception details in the SSE stream to avoid
            # information exposure; full context is in the structured log.
            response_content = f"An error occurred processing your request. (Error ID: {error_id})"
        finally:
            if scheduler and request_started:
                scheduler.notify_request_end()

        # Push full response to SSE queue then persist to DB.
        await queue.put(TextDeltaEvent(text=response_content, session_id=session_id))

        try:
            async with AsyncSessionLocal() as db:
                repo = SessionRepository(db)
                await repo.append_message(
                    session_uuid,
                    {
                        "role": "assistant",
                        "content": response_content,
                        "trace_id": trace_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "metadata": {"source": "service.app"},
                    },
                )
        except Exception as e:
            log.error(
                "chat_stream.db_append_assistant_failed",
                trace_id=trace_id,
                error=sanitize_error_message(e),
            )

    except Exception as e:
        bg_error_id = str(uuid4())[:8]
        log.error(
            "chat_stream.background_failed",
            session_id=session_id,
            error_id=bg_error_id,
            error=sanitize_error_message(e),
            exc_info=True,
        )
        # Do not include exception details in the SSE stream to avoid
        # information exposure; full context is in the structured log.
        await queue.put(
            TextDeltaEvent(
                text=f"\n\n[An error occurred. Error ID: {bg_error_id}]",
                session_id=session_id,
            )
        )
    finally:
        await queue.put(None)  # Always close the SSE stream.


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
    global \
        es_handler, \
        memory_service, \
        scheduler, \
        metrics_daemon, \
        mcp_adapter, \
        consumer_runner, \
        freshness_consumer, \
        cost_gate, \
        cost_gate_reaper_task

    # Startup
    log.info("service_starting")

    # Pre-flight: verify PostgreSQL is reachable before attempting any DB operations
    pg_host, pg_port = _parse_db_host_port(settings.database_url)
    await _preflight_check_tcp("PostgreSQL", pg_host, pg_port)

    # Initialize database
    await init_db()
    log.info("database_initialized")

    # Cost Check Gate (ADR-0065 / FRE-305): atomic Postgres-backed reservation
    # primitive in front of every paid LLM call. Loaded here so the
    # subsequent service-init code can already issue paid calls if needed.
    try:
        from personal_agent.cost_gate import (
            CostGate,
            load_budget_config,
            run_reaper,
            set_default_gate,
        )

        budget_config = load_budget_config()
        cost_gate = CostGate(config=budget_config, db_url=settings.database_url)
        await cost_gate.connect()
        set_default_gate(cost_gate)
        cost_gate_reaper_task = asyncio.create_task(run_reaper(cost_gate))
        log.info(
            "cost_gate_initialized",
            roles=len(budget_config.roles),
            caps=len(budget_config.caps),
        )
    except Exception as e:
        # Failing to initialise the gate is fatal — without it, paid calls
        # would fall back to the unprotected advisory check the gate replaces.
        log.error(
            "cost_gate_init_failed",
            error=str(e),
            remedy="Verify config/governance/budget.yaml and DB connectivity.",
            exc_info=True,
        )
        raise

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

    # Event bus (ADR-0041): Redis Streams or NoOpBus based on feature flag
    from personal_agent.events.bus import NoOpBus, set_global_event_bus
    from personal_agent.events.consumer import ConsumerRunner

    if settings.event_bus_enabled:
        try:
            from personal_agent.events.redis_backend import RedisStreamBus

            redis_bus = await RedisStreamBus.connect()
            set_global_event_bus(redis_bus)
            log.info("event_bus_redis_initialized")
        except Exception as e:
            log.warning(
                "event_bus_redis_connect_failed",
                error=str(e),
                remedy="Falling back to NoOpBus. Polling continues as normal.",
            )
            set_global_event_bus(NoOpBus())
    else:
        set_global_event_bus(NoOpBus())
        log.info("event_bus_disabled_using_noop")

    # ADR-0055: wire bus producers into brainstem singletons so metrics.sampled
    # and mode.transition events flow through the event bus from this point on.
    # MetricsDaemon is created above without a bus (bus wasn't ready yet); inject
    # now that the bus is resolved.  ModeManager singleton is created here for
    # the first time so it receives the bus in its constructor.
    from personal_agent.events.bus import get_event_bus as _get_event_bus_for_wiring

    _wiring_bus = _get_event_bus_for_wiring()
    # Inject bus into the already-running MetricsDaemon singleton.
    if metrics_daemon is not None:
        metrics_daemon._event_bus = _wiring_bus  # noqa: SLF001
    # Boot ModeManager singleton with bus so mode.transition events are published.
    get_mode_manager(event_bus=_wiring_bus)
    log.info("brainstem_bus_producers_wired", bus_type=type(_wiring_bus).__name__)

    # LinearClient: key-based, no gateway dependency (FRE-243 follow-up of FRE-224).
    if settings.linear_api_key:
        from personal_agent.captains_log.linear_client import LinearClient

        linear_client = LinearClient()
    else:
        linear_client = None
        log.warning(
            "linear_client_unavailable",
            reason="AGENT_LINEAR_API_KEY not set — promotion and feedback polling disabled",
        )

    # Start Brainstem scheduler for second brain, lifecycle, and/or insights tasks.
    if (
        settings.enable_second_brain
        or settings.data_lifecycle_enabled
        or settings.insights_enabled
        or settings.freshness_enabled
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

    # Wire event bus consumers (ADR-0041 Phase 1 + Phase 2 / FRE-158, Phase 3 / FRE-159)
    from personal_agent.events.bus import get_event_bus
    from personal_agent.events.consumers.error_monitor import ErrorMonitorConsumer
    from personal_agent.events.consumers.freshness_consumer import FreshnessConsumer
    from personal_agent.events.models import (
        CG_CAPTAIN_LOG,
        CG_CONSOLIDATOR,
        CG_ERROR_MONITOR,
        CG_ES_INDEXER,
        CG_FEEDBACK,
        CG_FRESHNESS,
        CG_INSIGHTS,
        CG_MODE_CONTROLLER,
        CG_PROMOTION,
        CG_SESSION_WRITER,
        STREAM_CONSOLIDATION_COMPLETED,
        STREAM_CONTEXT_COMPACTION_QUALITY_POOR,
        STREAM_ERRORS_PATTERN_DETECTED,
        STREAM_FEEDBACK_RECEIVED,
        STREAM_MEMORY_ACCESSED,
        STREAM_MEMORY_ENTITIES_UPDATED,
        STREAM_METRICS_SAMPLED,
        STREAM_MODE_TRANSITION,
        STREAM_PROMOTION_ISSUE_CREATED,
        STREAM_REQUEST_CAPTURED,
        STREAM_REQUEST_COMPLETED,
        STREAM_SYSTEM_IDLE,
        EventBase,
    )
    from personal_agent.events.pipeline_handlers import (
        build_compaction_quality_captain_log_handler,
        build_consolidation_insights_handler,
        build_consolidation_promotion_handler,
        build_error_pattern_captain_log_handler,
        build_feedback_insights_handler,
        build_feedback_suppression_handler,
        build_promotion_captain_log_handler,
    )
    from personal_agent.events.redis_backend import RedisStreamBus
    from personal_agent.events.request_completed_handlers import (
        build_request_trace_es_handler,
        build_session_writer_handler,
    )

    active_bus = get_event_bus()
    if isinstance(active_bus, RedisStreamBus):
        if scheduler is not None:

            async def _on_request_captured(event: EventBase) -> None:
                """Route request.captured events to the scheduler."""
                if scheduler is not None:
                    await scheduler.on_request_captured(
                        trace_id=getattr(event, "trace_id", "unknown"),
                        session_id=getattr(event, "session_id", "unknown"),
                    )

            async def _on_system_idle(event: EventBase) -> None:
                """Route system.idle events to the scheduler (Phase 3)."""
                if scheduler is not None:
                    await scheduler.on_system_idle()

            await active_bus.subscribe(
                stream=STREAM_REQUEST_CAPTURED,
                group=CG_CONSOLIDATOR,
                consumer_name="consolidator-0",
                handler=_on_request_captured,
            )
            await active_bus.subscribe(
                stream=STREAM_SYSTEM_IDLE,
                group=CG_CONSOLIDATOR,
                consumer_name="consolidator-idle-0",
                handler=_on_system_idle,
            )

        # Phase 2 — request.completed consumers
        await active_bus.subscribe(
            stream=STREAM_REQUEST_COMPLETED,
            group=CG_ES_INDEXER,
            consumer_name="es-indexer-0",
            handler=build_request_trace_es_handler(es_handler),
        )
        await active_bus.subscribe(
            stream=STREAM_REQUEST_COMPLETED,
            group=CG_SESSION_WRITER,
            consumer_name="session-writer-0",
            handler=build_session_writer_handler(),
        )

        # Phase 3 — pipeline decoupling consumers
        await active_bus.subscribe(
            stream=STREAM_CONSOLIDATION_COMPLETED,
            group=CG_INSIGHTS,
            consumer_name="insights-0",
            handler=build_consolidation_insights_handler(
                memory_service=memory_service,
                event_bus=active_bus,
            ),
        )
        await active_bus.subscribe(
            stream=STREAM_CONSOLIDATION_COMPLETED,
            group=CG_PROMOTION,
            consumer_name="promotion-0",
            handler=build_consolidation_promotion_handler(linear_client=linear_client),
        )
        await active_bus.subscribe(
            stream=STREAM_PROMOTION_ISSUE_CREATED,
            group=CG_CAPTAIN_LOG,
            consumer_name="captain-log-0",
            handler=build_promotion_captain_log_handler(),
        )
        await active_bus.subscribe(
            stream=STREAM_FEEDBACK_RECEIVED,
            group=CG_INSIGHTS,
            consumer_name="insights-feedback-0",
            handler=build_feedback_insights_handler(),
        )
        await active_bus.subscribe(
            stream=STREAM_FEEDBACK_RECEIVED,
            group=CG_FEEDBACK,
            consumer_name="feedback-suppression-0",
            handler=build_feedback_suppression_handler(),
        )

        # Phase 4 — memory access tracking (FRE-164 / ADR-0042 Step 4)
        _settings = get_settings()
        freshness_consumer = FreshnessConsumer(
            batch_window_seconds=_settings.freshness_consumer_batch_window_seconds,
            batch_max_events=_settings.freshness_consumer_batch_max_events,
        )
        await freshness_consumer.start()
        await active_bus.subscribe(
            stream=STREAM_MEMORY_ACCESSED,
            group=CG_FRESHNESS,
            consumer_name="freshness-access-0",
            handler=freshness_consumer.handle,
        )

        # memory.entities_updated events are informational at this phase — no-op ACK
        async def _noop_freshness_entities(event: EventBase) -> None:
            pass

        await active_bus.subscribe(
            stream=STREAM_MEMORY_ENTITIES_UPDATED,
            group=CG_FRESHNESS,
            consumer_name="freshness-entities-0",
            handler=_noop_freshness_entities,
        )

        # ADR-0055: cg:mode-controller — receives metrics.sampled and
        # mode.transition events, drives ModeManager.evaluate_transitions().
        if settings.mode_controller_enabled:
            _mode_controller = get_mode_controller()
            await active_bus.subscribe(
                stream=STREAM_METRICS_SAMPLED,
                group=CG_MODE_CONTROLLER,
                consumer_name="mode-controller-0",
                handler=_mode_controller.handle,
            )
            await active_bus.subscribe(
                stream=STREAM_MODE_TRANSITION,
                group=CG_MODE_CONTROLLER,
                consumer_name="mode-controller-transition-0",
                handler=_mode_controller.handle,
            )
            log.info("mode_controller_registered", consumer_group=CG_MODE_CONTROLLER)

        # ADR-0056: cg:error-monitor — scans ES on consolidation.completed,
        # dual-writes EP-*.json and stream:errors.pattern_detected events.
        # cg:captain-log subscribed to stream:errors.pattern_detected for CL entry.
        if settings.error_monitor_enabled:
            from personal_agent.telemetry.error_monitor import ErrorMonitor
            from personal_agent.telemetry.queries import TelemetryQueries

            _shared_es = (
                es_handler.es_logger.client
                if es_handler is not None and getattr(es_handler, "_connected", False)
                else None
            )
            _error_queries = TelemetryQueries(es_client=_shared_es)
            _error_monitor = ErrorMonitor(
                queries=_error_queries,
                bus=active_bus,
                window_hours=settings.error_monitor_window_hours,
                min_occurrences=settings.error_monitor_min_occurrences,
                max_patterns_per_scan=settings.error_monitor_max_patterns_per_scan,
            )
            _error_monitor_consumer = ErrorMonitorConsumer(
                monitor=_error_monitor,
                enabled=True,
            )
            await active_bus.subscribe(
                stream=STREAM_CONSOLIDATION_COMPLETED,
                group=CG_ERROR_MONITOR,
                consumer_name="error-monitor-0",
                handler=_error_monitor_consumer.handle,
            )
            _ep_cl_handler = build_error_pattern_captain_log_handler()
            await active_bus.subscribe(
                stream=STREAM_ERRORS_PATTERN_DETECTED,
                group=CG_CAPTAIN_LOG,
                consumer_name="captain-log-error-pattern-0",
                handler=_ep_cl_handler,
            )
            log.info(
                "error_monitor_registered",
                consumer_group=CG_ERROR_MONITOR,
                window_hours=settings.error_monitor_window_hours,
                min_occurrences=settings.error_monitor_min_occurrences,
            )

        # ADR-0059 — Context Quality Stream (FRE-249).
        # cg:captain-log subscribes to stream:context.compaction_quality_poor;
        # each per-incident event becomes a CaptainLogEntry(KNOWLEDGE_QUALITY,
        # ORCHESTRATOR). Emission gate is the producer-side flag
        # context_quality_stream_enabled (checked in recall_controller); the
        # subscription itself is harmless when no events are published, so we
        # always wire it whenever the bus is RedisStreamBus.
        if settings.context_quality_stream_enabled:
            _cq_cl_handler = build_compaction_quality_captain_log_handler()
            await active_bus.subscribe(
                stream=STREAM_CONTEXT_COMPACTION_QUALITY_POOR,
                group=CG_CAPTAIN_LOG,
                consumer_name="captain-log-compaction-quality-0",
                handler=_cq_cl_handler,
            )
            log.info(
                "context_quality_stream_registered",
                consumer_group=CG_CAPTAIN_LOG,
                stream=STREAM_CONTEXT_COMPACTION_QUALITY_POOR,
                governance_enabled=settings.context_quality_governance_enabled,
                governance_threshold=settings.context_quality_governance_threshold,
            )

        # ADR-0060 — Knowledge Graph Quality Stream (FRE-250).
        # cg:graph-monitor subscribes to stream:graph.quality_anomaly (daily anomaly
        # scan, Stream 8) and stream:memory.staleness_reviewed (weekly freshness
        # review, Stream 6). Both become CaptainLogEntry(KNOWLEDGE_QUALITY|RELIABILITY,
        # SECOND_BRAIN). Phase 2 governance (ModeAdvisoryEvent) is flag-gated off.
        if settings.graph_quality_stream_enabled:
            from personal_agent.events.models import (
                CG_GRAPH_MONITOR,
                STREAM_GRAPH_QUALITY_ANOMALY,
                STREAM_MEMORY_STALENESS_REVIEWED,
            )
            from personal_agent.events.pipeline_handlers import (
                build_graph_quality_captain_log_handler,
            )

            _gq_cl_handler = build_graph_quality_captain_log_handler()
            await active_bus.subscribe(
                stream=STREAM_GRAPH_QUALITY_ANOMALY,
                group=CG_GRAPH_MONITOR,
                consumer_name="graph-monitor-0",
                handler=_gq_cl_handler,
            )
            await active_bus.subscribe(
                stream=STREAM_MEMORY_STALENESS_REVIEWED,
                group=CG_GRAPH_MONITOR,
                consumer_name="graph-monitor-staleness-0",
                handler=_gq_cl_handler,
            )
            log.info(
                "graph_quality_stream_registered",
                consumer_group=CG_GRAPH_MONITOR,
                stream_anomaly=STREAM_GRAPH_QUALITY_ANOMALY,
                stream_staleness=STREAM_MEMORY_STALENESS_REVIEWED,
                governance_enabled=settings.graph_quality_governance_enabled,
            )

        consumer_runner = ConsumerRunner(active_bus)
        await consumer_runner.start()
        registered = [f"{s.stream}→{s.group}" for s in active_bus.subscriptions]
        log.info(
            "event_bus_ready",
            bus_type=type(active_bus).__name__,
            consumer_count=len(registered),
            consumers=registered,
        )

    # Wire gateway state (FRE-206): attach storage backends to app.state so
    # gateway endpoints (mounted below in local-dev mode) can reach them.
    if settings.gateway_mount_local:
        from personal_agent.gateway.app import _KnowledgeGraphAdapter as _KGAdapter  # noqa: PLC0415
        from personal_agent.service.database import AsyncSessionLocal as _ASL  # noqa: PLC0415

        app.state.db_session_factory = _ASL
        app.state.knowledge_graph = (
            _KGAdapter(memory_service) if memory_service is not None else None
        )
        app.state.es_client = (
            es_handler.es_logger.client
            if es_handler is not None and getattr(es_handler, "_connected", False)
            else None
        )
        log.info("gateway_state_wired")

    log.info("service_ready", port=settings.service_port)

    # MCP gateway — initialised last so that an anyio cancel-scope leak from a
    # failed 'docker mcp' subprocess cannot cascade into Redis subscriptions that
    # are already running.  asyncio.shield() prevents the lifespan task from being
    # cancelled by anyio's internal task-group cleanup.  BaseExceptionGroup covers
    # the Python 3.11+ grouped exception that anyio raises on subprocess failure.
    if settings.mcp_gateway_enabled:
        try:
            from personal_agent.mcp.gateway import MCPGatewayAdapter
            from personal_agent.tools import get_default_registry

            log.info("mcp_gateway_initializing", command=settings.mcp_gateway_command)
            registry = get_default_registry()
            mcp_adapter = MCPGatewayAdapter(registry)
            await asyncio.shield(mcp_adapter.initialize())
            log.info(
                "mcp_gateway_initialized",
                tools_count=len(mcp_adapter._mcp_tool_names),
                tools=list(mcp_adapter._mcp_tool_names)[:10],
            )
        except (Exception, BaseExceptionGroup) as e:
            log.warning(
                "mcp_gateway_init_failed",
                error=sanitize_error_message(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            mcp_adapter = None

    yield

    # Shutdown
    log.info("service_shutting_down")

    # Stop event bus consumers first (before scheduler)
    if consumer_runner is not None:
        await consumer_runner.stop()
        consumer_runner = None
    if freshness_consumer is not None:
        await freshness_consumer.stop()
        freshness_consumer = None
    active_bus_shutdown = get_event_bus()
    if not isinstance(active_bus_shutdown, NoOpBus):
        await active_bus_shutdown.close()
    set_global_event_bus(NoOpBus())

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

    # Cost Check Gate teardown (FRE-305)
    if cost_gate_reaper_task is not None:
        cost_gate_reaper_task.cancel()
        try:
            await cost_gate_reaper_task
        except asyncio.CancelledError:
            pass
        cost_gate_reaper_task = None
    if cost_gate is not None:
        from personal_agent.cost_gate import set_default_gate as _set_default_gate

        _set_default_gate(None)
        await cost_gate.disconnect()
        cost_gate = None

    log.info("service_stopped")


app = FastAPI(
    title="Personal Agent Service",
    description="Cognitive architecture service with persistent memory",
    version="2.0.0",
    lifespan=lifespan,
)


# ── BudgetDenied → structured 503 (ADR-0065 D5 / FRE-306) ───────────────────
# Catches ``BudgetDenied`` raised by ``CostGate.reserve`` anywhere downstream
# (LiteLLMClient or the streaming /chat path) and renders it as an explicit
# error response with cap, current spend, and reset-time so the PWA can show
# the user *why* the call failed instead of an empty assistant turn.
from fastapi import Request as _Request  # noqa: E402
from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402

from personal_agent.cost_gate import BudgetDenied as _BudgetDenied  # noqa: E402


@app.exception_handler(_BudgetDenied)
async def _budget_denied_handler(  # type: ignore[misc]
    _request: _Request, exc: _BudgetDenied
) -> _JSONResponse:
    """Render :class:`BudgetDenied` as a structured HTTP 503.

    The PWA error card consumes ``error="budget_denied"`` and renders the
    cap / spend / reset_time fields explicitly. This was the regression
    fixed by ADR-0065: previously the cap-exceeded ValueError was swallowed
    and rendered as an empty assistant turn.
    """
    log.warning(
        "http_budget_denied",
        role=exc.role,
        time_window=exc.time_window,
        current_spend=str(exc.current_spend),
        cap=str(exc.cap),
        denial_reason=exc.denial_reason,
    )
    return _JSONResponse(
        status_code=503,
        content={
            "error": "budget_denied",
            "role": exc.role,
            "time_window": exc.time_window,
            "cap": str(exc.cap),
            "spend": str(exc.current_spend),
            "reset_time": exc.window_resets_at.isoformat(),
            "denial_reason": exc.denial_reason,
            "status": 503,
        },
    )


# CORS — allows the Next.js dev server (localhost:3000) to reach the backend (localhost:9000).
# In production Caddy proxies both through the same origin so this middleware is a no-op there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# AG-UI transport — streaming SSE endpoint (ADR-0046, FRE-204)
from personal_agent.transport.agui.endpoint import router as transport_router  # noqa: E402

app.include_router(transport_router)

# Seshat API Gateway (FRE-206) — additive, does not affect existing routes.
# In local dev mode the gateway router mounts on this app (port 9000).
# In production, run personal_agent.gateway.app:gateway_app on its own port.
if settings.gateway_mount_local:
    from personal_agent.gateway.app import create_gateway_router  # noqa: E402

    _gateway_router = create_gateway_router()
    app.include_router(_gateway_router)


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
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> SessionResponse:
    """Create a new session."""
    repo = SessionRepository(db)
    session = await repo.create(data, user_id=request_user.user_id)

    log.info(
        "session_created",
        session_id=str(session.session_id),
        channel=session.channel,
        mode=session.mode,
        user_id=str(request_user.user_id),
    )

    return session


@app.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> SessionResponse:
    """Get session by ID."""
    repo = SessionRepository(db)
    session = await repo.get(UUID(session_id), user_id=request_user.user_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@app.patch("/sessions/{session_id}", response_model=SessionResponse)
async def update_session(
    session_id: str,
    data: SessionUpdate,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> SessionResponse:
    """Update session."""
    repo = SessionRepository(db)
    session = await repo.update(UUID(session_id), data, user_id=request_user.user_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return session


@app.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    limit: int = 50,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> list[SessionResponse]:
    """List recent sessions."""
    repo = SessionRepository(db)
    sessions = await repo.list_recent(limit, user_id=request_user.user_id)
    return cast(list[SessionResponse], sessions)


# ============================================================================
# Chat Endpoint (Main Entry Point)
# ============================================================================


@app.post("/chat")
async def chat(
    message: str,
    session_id: str | None = None,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, str]:
    """Process a chat message.

    This is the main entry point for user interactions.

    Args:
        message: User's message
        session_id: Optional existing session ID (creates new if not provided)
        request_user: Resolved user identity (injected by FastAPI)
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
            try:
                parsed_session_id = UUID(session_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="session_id must be a valid UUID"
                ) from exc
            session = await repo.get(parsed_session_id, user_id=request_user.user_id)
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
        else:
            session = await repo.create(SessionCreate(), user_id=request_user.user_id)

    # FRE-51: await prior turn's assistant append (NoOp: background task; Redis: session-writer).
    sid = str(cast(UUID, session.session_id))
    from personal_agent.events.bus import get_event_bus
    from personal_agent.events.redis_backend import RedisStreamBus
    from personal_agent.events.session_write_waiter import await_previous_session_write

    if isinstance(get_event_bus(), RedisStreamBus):
        await await_previous_session_write(sid)
    elif sid in _pending_append_tasks:
        await _pending_append_tasks.pop(sid)

    # --- Phase: session_hydration ---
    with timer.span("session_hydration"):
        db_messages = list(session.messages or [])
        max_history = settings.conversation_max_history_messages
        prior_messages = db_messages[-max_history:] if max_history > 0 else db_messages

    # --- Phase: db_append_user_message ---
    with timer.span("db_append_user_message"):
        await repo.append_message(
            cast(UUID, session.session_id),
            {
                "role": "user",
                "content": message,
                "trace_id": trace_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {"source": "service.app"},
            },
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
                mode=get_current_mode(),
                memory_adapter=memory_adapter,
                expansion_budget=expansion_budget,
                full_session_messages=db_messages,
                user_id=request_user.user_id,
                authenticated=True,
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
                    get_current_mode(), Channel.CHAT, session_id=str(session.session_id)
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
            user_id=request_user.user_id,
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
        # Do not include exception details in the HTTP response to avoid
        # information exposure; full context is in the structured log.
        response_content = f"An error occurred processing your request. (Error ID: {error_id})"
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

    # Durable side effects: Redis Streams (FRE-158) or legacy fire-and-forget (NoOp bus).
    bus = get_event_bus()
    if isinstance(bus, RedisStreamBus):
        from personal_agent.events.models import STREAM_REQUEST_COMPLETED, RequestCompletedEvent
        from personal_agent.events.session_write_waiter import (
            register_session_write_waiter,
            release_session_write_wait,
        )

        # Published after response timing is finalized (full RequestTimer spans in event).
        register_session_write_waiter(sid)
        try:
            await bus.publish(
                STREAM_REQUEST_COMPLETED,
                RequestCompletedEvent(
                    trace_id=trace_id,
                    session_id=sid,
                    assistant_response=response_content,
                    trace_summary=timer.to_trace_summary(),
                    trace_breakdown=timer.to_breakdown(),
                    source_component="service.app",
                ),
            )
        except Exception:
            release_session_write_wait(sid)
            raise
    else:
        if es_handler and getattr(es_handler, "_connected", False):
            asyncio.create_task(
                es_handler.es_logger.index_request_trace(
                    trace_id=trace_id,
                    timer=timer,
                    session_id=str(session.session_id),
                )
            )
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
# AG-UI Streaming Chat Endpoint (ADR-0046 / FRE-207)
# ============================================================================


@app.post("/chat/stream")
async def chat_stream_endpoint(
    message: str = Form(...),
    session_id: str = Form(...),
    profile: str = Form(default="local"),
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, str]:
    """AG-UI fire-and-forget chat endpoint for the PWA.

    Accepts a user message via form data, launches the full Seshat orchestrator
    pipeline as a background task, and returns immediately.  The client should
    connect to ``GET /stream/{session_id}`` to receive ``TEXT_DELTA`` events as
    the model generates its reply.

    The execution profile is resolved inside the background task so the
    LLM client factory dispatches to the correct cloud or local model.

    Args:
        message: User message text.
        session_id: Client-generated session UUID.
        profile: Execution profile name (e.g. ``"local"``, ``"cloud"``).
        request_user: Resolved user identity (injected by FastAPI).

    Returns:
        ``{"session_id": ..., "status": "streaming"}`` once the background
        task is launched.

    Raises:
        HTTPException: 422 if ``session_id`` is not a valid UUID v4.
    """
    try:
        UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID v4") from exc

    asyncio.create_task(
        _process_chat_stream_background(
            session_id=session_id,
            message=message,
            profile_name=profile,
            user_id=request_user.user_id,
        )
    )

    log.info("chat_stream.launched", session_id=session_id, profile=profile)
    return {"session_id": session_id, "status": "streaming"}


# ============================================================================
# Inference Availability (Mac SLM Tunnel)
# ============================================================================

_SLM_HEALTH_URL = "https://slm.frenchforet.com/health"


@app.get("/api/inference/status")
async def inference_status() -> dict[str, Any]:
    """Probe the Mac SLM tunnel and return availability for the local profile.

    Makes a GET /health request to https://slm.frenchforet.com/health with
    Cloudflare Access service token headers. Times out in 3 seconds.

    Returns:
        {"local": "up", "latency_ms": N} if reachable,
        {"local": "down", "latency_ms": None} otherwise.
    """
    headers: dict[str, str] = {}
    if settings.cf_access_client_id and settings.cf_access_client_secret:
        headers["CF-Access-Client-Id"] = settings.cf_access_client_id
        headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(_SLM_HEALTH_URL, headers=headers)
            resp.raise_for_status()
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"local": "up", "latency_ms": latency_ms}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            log.warning(
                "inference_tunnel_auth_failed",
                status=403,
                hint="Rotate CF_ACCESS_CLIENT_ID/SECRET via terraform apply",
            )
        return {"local": "down", "latency_ms": None}
    except Exception:
        return {"local": "down", "latency_ms": None}


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
