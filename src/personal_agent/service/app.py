"""FastAPI service application."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, status
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
from personal_agent.service.auth import (
    RequestUser,
    get_or_create_user_by_email,
    get_request_user,
    upsert_display_name_for_email,
)
from personal_agent.service.database import AsyncSessionLocal, get_db_session, init_db
from personal_agent.service.idempotency import get_deduplicator
from personal_agent.service.models import (
    ConstraintPreferenceUpdate,
    LocationPreferenceUpdate,
    SessionCreate,
    SessionResponse,
    SessionUpdate,
)
from personal_agent.service.repositories.session_repository import SessionRepository
from personal_agent.telemetry import add_elasticsearch_handler, get_logger
from personal_agent.telemetry.es_handler import ElasticsearchHandler
from personal_agent.telemetry.request_timer import RequestTimer
from personal_agent.telemetry.trace import SystemTraceContext
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
cost_gate_snapshotter_task: asyncio.Task[None] | None = None

# Fire-and-forget assistant message appends: session_id -> task (FRE-51).
# Next request for same session awaits this so history is consistent before hydration.
_pending_append_tasks: dict[str, asyncio.Task[None]] = {}


def _resolve_active_model_attribution(
    *,
    trace_id: str | None = None,
) -> tuple[str | None, str]:
    """Thin wrapper kept for clarity at call sites in this module.

    Delegates to :func:`personal_agent.config.model_loader.resolve_active_attribution`
    which is shared with the Redis session-writer consumer so both append
    paths emit identical attribution (ADR-0074 / FRE-376). The optional
    ``trace_id`` is forwarded so the warning path correlates with the
    originating chat turn (ADR-0074 §I3).
    """
    from personal_agent.config.model_loader import resolve_active_attribution

    return resolve_active_attribution(trace_id=trace_id)


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
    primary_model_id, config_path_str = _resolve_active_model_attribution(trace_id=trace_id)
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
                    "metadata": {
                        "source": "service.app",
                        "model": primary_model_id,
                        "model_role": "primary",
                        "model_config_path": config_path_str,
                    },
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


async def _validate_attachments(
    attachments_json: str | None,
    *,
    user_id: UUID,
    trace_id: str,
) -> list[dict[str, str]]:
    """Parse attachment JSON and return only rows owned by user_id with upload_pending=FALSE.

    Args:
        attachments_json: JSON-encoded list of ``{artifact_id, content_type, title}``,
            or ``None`` / empty string when the turn has no attachments.
        user_id: The authenticated caller's UUID.
        trace_id: Request trace id for log correlation.

    Returns:
        Validated attachment dicts (empty list if none pass).
    """
    import json as _json  # noqa: PLC0415

    if not attachments_json:
        return []
    try:
        items: list[dict[str, str]] = _json.loads(attachments_json)
    except (ValueError, TypeError):
        log.warning("chat_stream.invalid_attachments_json", trace_id=trace_id)
        return []
    if not items:
        return []

    ids = [att.get("artifact_id", "") for att in items if att.get("artifact_id")]
    if not ids:
        return []

    from sqlalchemy import text as _text  # noqa: PLC0415

    valid: list[dict[str, str]] = []
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            _text(
                "SELECT id, content_type, title FROM artifacts "
                "WHERE id = ANY(:ids) AND user_id = :uid AND upload_pending = FALSE"
            ),
            {"ids": ids, "uid": str(user_id)},
        )
        for row in result.fetchall():
            valid.append(
                {
                    "artifact_id": str(row.id),
                    "content_type": row.content_type or "",
                    "title": row.title or str(row.id),
                }
            )
    return valid


def _augment_message_with_attachments(
    message: str,
    attachments: list[dict[str, str]],
) -> str:
    """Prepend attachment context to ``message`` for the orchestrator.

    Does NOT mutate the message stored in DB or passed to ``run_gateway_pipeline``.
    Called AFTER intent classification so gateway routing is unaffected.

    Args:
        message: Original user message text.
        attachments: Validated attachment dicts from ``_validate_attachments``.

    Returns:
        Augmented message string, or ``message`` unchanged when ``attachments`` is empty.
    """
    if not attachments:
        return message
    lines = ["[Attachments — call artifact_read(artifact_id) to read content:]"]
    for att in attachments:
        lines.append(
            f"  - artifact_id: {att['artifact_id']}, "
            f"content_type: {att['content_type']}, filename: {att['title']}"
        )
    return "\n".join(lines) + "\n\n" + message


async def _process_chat_stream_background(
    session_id: str,
    message: str,
    profile_name: str,
    user_id: UUID,
    trace_id: str,
    user_email: str | None = None,
    user_display_name: str | None = None,
    client_msg_id: str | None = None,
    attachments_json: str | None = None,
) -> None:
    """Run the full orchestrator pipeline and push the result to the SSE queue.

    Runs as a fire-and-forget ``asyncio.Task``.  A ``None`` sentinel is always
    pushed to the SSE queue, even on error, so the client stream closes cleanly.

    Args:
        session_id: Client-generated session UUID (used for SSE queue key and DB).
        message: User's message text.
        profile_name: Execution profile name (e.g. ``"local"``, ``"cloud"``).
        user_id: Authenticated user UUID — used for session ownership scoping.
        trace_id: Pre-generated trace ID (from the endpoint, used for dedup record).
        user_email: CF Access email of the connected user (FRE-213).
        user_display_name: Display name from the users table, if set (FRE-213).
        client_msg_id: Client-provided idempotency key (FRE-392); used to release
            the dedup entry when the task completes so retries work immediately.
        attachments_json: JSON-encoded list of completed upload dicts (FRE-369),
            or ``None``. Injected into the orchestrator message AFTER gateway
            classification so TaskType routing is unaffected.
    """
    from personal_agent.config.profile import load_profile, set_current_profile

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
        db_messages: list[dict[str, Any]] = []
        prior_messages: list[dict[str, Any]] = []

        async with AsyncSessionLocal() as db:
            repo = SessionRepository(db)
            session = await repo.get(session_uuid, user_id=user_id)
            if not session:
                # Create with client-provided UUID so history is addressable
                # across turns without the client needing to track a separate DB ID.
                from personal_agent.service.models import SessionModel

                primary_model_id, config_path_str = _resolve_active_model_attribution(
                    trace_id=trace_id,
                )
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
                    primary_model_at_creation=primary_model_id,
                    model_config_path=config_path_str,
                    # ADR-0079: persist the resolved profile on first turn so the
                    # session row is the source of truth from creation onward.
                    execution_profile=profile_name,
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

        # ── Attachment injection (FRE-369) ───────────────────────────────
        # Validate ownership + completeness AFTER gateway classification, so that
        # attachment context does NOT pollute TaskType routing.
        validated_attachments = await _validate_attachments(
            attachments_json, user_id=user_id, trace_id=trace_id
        )
        orchestrator_message = _augment_message_with_attachments(message, validated_attachments)

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
                user_message=orchestrator_message,
                mode=None,
                channel=None,
                trace_id=trace_id,
                request_timer=RequestTimer(trace_id=trace_id),
                gateway_output=gateway_output,
                user_id=user_id,
                user_email=user_email,
                user_display_name=user_display_name,
                authenticated=True,
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

        # Push full response via dual-write path (Postgres + WS queue).
        from personal_agent.transport.agui.transport import _push_event  # noqa: E402

        await _push_event(TextDeltaEvent(text=response_content, session_id=session_id), session_id)

        try:
            primary_model_id, config_path_str = _resolve_active_model_attribution(
                trace_id=trace_id,
            )
            async with AsyncSessionLocal() as db:
                repo = SessionRepository(db)
                await repo.append_message(
                    session_uuid,
                    {
                        "role": "assistant",
                        "content": response_content,
                        "trace_id": trace_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "metadata": {
                            "source": "service.app",
                            "model": primary_model_id,
                            "model_role": "primary",
                            "model_config_path": config_path_str,
                        },
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
            trace_id=trace_id,
            exc_info=True,
        )
        # Do not include exception details in the stream to avoid
        # information exposure; full context is in the structured log.
        from personal_agent.transport.agui.transport import _push_event  # noqa: E402

        await _push_event(
            TextDeltaEvent(
                text=f"\n\n[An error occurred. Error ID: {bg_error_id}]",
                session_id=session_id,
            ),
            session_id,
        )
    finally:
        # Persist DONE to Postgres (so reconnect replay delivers it) then push
        # the None sentinel to close the live WS drain loop — serialized under
        # the per-session emit lock so the DONE seq + sentinel stay ordered
        # behind every prior live emit (FRE-518).
        from personal_agent.transport.agui.transport import emit_done  # noqa: E402

        await emit_done(session_id)
        # Release the dedup entry so the user can immediately retry on error
        # without waiting for TTL expiry (FRE-392).
        get_deduplicator().release(session_id, message, client_msg_id=client_msg_id)


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
        cost_gate_reaper_task, \
        cost_gate_snapshotter_task

    # Startup
    log.info("service_starting")

    # Pre-flight: verify PostgreSQL is reachable before attempting any DB operations
    pg_host, pg_port = _parse_db_host_port(settings.database_url)
    await _preflight_check_tcp("PostgreSQL", pg_host, pg_port)

    # Initialize database
    await init_db()
    log.info("database_initialized")

    # Route-trace ledger (FRE-452 / ADR-0088 D6): direct durable observability sink.
    # Connect early so every turn's terminal write has a pool; non-fatal on failure.
    from personal_agent.observability.route_trace import get_route_trace_ledger

    await get_route_trace_ledger().connect()

    # Cost Check Gate (ADR-0065 / FRE-305): atomic Postgres-backed reservation
    # primitive in front of every paid LLM call. Loaded here so the
    # subsequent service-init code can already issue paid calls if needed.
    try:
        from personal_agent.cost_gate import (
            CostGate,
            load_budget_config,
            run_counter_snapshotter,
            run_reaper,
            set_default_gate,
        )

        budget_config = load_budget_config()
        cost_gate = CostGate(config=budget_config, db_url=settings.database_url)
        await cost_gate.connect()
        set_default_gate(cost_gate)
        cost_gate_reaper_task = asyncio.create_task(run_reaper(cost_gate))
        # Cap-utilization snapshot emitter (FRE-547): mirrors budget_counters to
        # ES so the Cost & Budget dashboard can render utilization vs caps. The
        # snapshotter sleeps before its first emit, so the ES log handler
        # (attached below) is wired before any snapshot fires.
        cost_gate_snapshotter_task = asyncio.create_task(run_counter_snapshotter(cost_gate))
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
    es_handler = ElasticsearchHandler(
        settings.elasticsearch_url, index_prefix=settings.elasticsearch_index_prefix
    )
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
            # Bootstrap owner identity (FRE-213 / ADR-0052) — idempotent, no-op when empty
            if settings.owner_name and settings.agent_owner_email:
                try:
                    async with AsyncSessionLocal() as db:
                        owner_user_id = await get_or_create_user_by_email(
                            db, settings.agent_owner_email
                        )
                    await memory_service.bootstrap_owner_identity(
                        agent_id=settings.agent_id,
                        user_id=owner_user_id,
                        email=settings.agent_owner_email,
                        name=settings.owner_name,
                    )
                except Exception as boot_e:
                    log.warning("owner_bootstrap_failed", error=str(boot_e))
            # Seed display names for non-owner CF Access users (FRE-344)
            for _email, _display_name in settings.user_display_names.items():
                try:
                    async with AsyncSessionLocal() as db:
                        _uid = await upsert_display_name_for_email(db, _email, _display_name)
                    local_part = _email.split("@")[0]
                    await memory_service.update_person_name_if_default(
                        user_id=_uid,
                        current_default=local_part,
                        new_name=_display_name,
                    )
                    log.info("display_name_seeded", email=_email)
                except Exception as seed_e:
                    log.warning("display_name_seed_failed", email=_email, error=str(seed_e))
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
        CG_TURN_PROJECTOR,
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
        STREAM_TURN_OBSERVED,
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

            await active_bus.subscribe(
                stream=STREAM_REQUEST_CAPTURED,
                group=CG_CONSOLIDATOR,
                consumer_name="consolidator-0",
                handler=_on_request_captured,
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

        # ADR-0088: cg:turn-projector — the sole emitter of turn_status (FRE-513).
        # Consumes stream:turn.observed and projects the live meter. Live-only; under
        # NoOpBus (not RedisStreamBus) this block is skipped and the durable route-trace
        # + api_costs writes still happen (ADR-0088 D8). FRE-507: the resulting dark meter
        # is accepted graceful degradation (no in-band fallback — see projector.py docstring).
        if settings.turn_projector_enabled:
            from personal_agent.observability.route_trace import get_route_trace_ledger
            from personal_agent.observability.topology.projector import (
                SessionHydration,
                TurnObservationProjector,
            )

            _ledger = get_route_trace_ledger()

            async def _hydrate_session(session_id: str) -> SessionHydration:
                costs = await _ledger.fetch_session_costs_by_trace(session_id)
                # B/D/A compaction identity sets start carry-only — no durable substrate
                # query yet; a follow-up ticket will wire ES/JSONL reads (ADR-0092 §D4).
                return SessionHydration(costs=costs)

            _turn_projector = TurnObservationProjector(hydration_source=_hydrate_session)
            await active_bus.subscribe(
                stream=STREAM_TURN_OBSERVED,
                group=CG_TURN_PROJECTOR,
                consumer_name="turn-projector-0",
                handler=_turn_projector.handle,
            )
            log.info("turn_projector_registered", consumer_group=CG_TURN_PROJECTOR)

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
                error=sanitize_error_message(e) if isinstance(e, Exception) else str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            mcp_adapter = None

    # WebSocket session_events cleanup task (ADR-0075 / FRE-388)
    from personal_agent.transport.agui.ws_endpoint import run_event_cleanup  # noqa: E402

    async def _ws_event_cleanup_loop() -> None:
        while True:
            await asyncio.sleep(3600)  # hourly
            try:
                await run_event_cleanup()
            except Exception:
                log.exception("ws_event_cleanup_failed")

    ws_cleanup_task = asyncio.create_task(_ws_event_cleanup_loop())

    # Message dedup entry cleanup task (FRE-392)
    async def _dedup_cleanup_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                get_deduplicator().cleanup_expired()
            except Exception:
                log.exception("dedup_cleanup_failed")

    dedup_cleanup_task = asyncio.create_task(_dedup_cleanup_loop())

    # Upload pending-row expiry cleanup task (FRE-369)
    from personal_agent.service.uploads_router import (  # noqa: PLC0415
        expire_pending_uploads,
    )

    async def _upload_expiry_loop() -> None:
        while True:
            await asyncio.sleep(1800)  # every 30 min
            try:
                n = await expire_pending_uploads(AsyncSessionLocal)
                if n:
                    log.info("upload_expiry_cleaned", count=n)
            except Exception:
                log.exception("upload_expiry_failed")

    upload_expiry_task = asyncio.create_task(_upload_expiry_loop())

    yield

    # Shutdown
    log.info("service_shutting_down")
    ws_cleanup_task.cancel()
    dedup_cleanup_task.cancel()
    upload_expiry_task.cancel()

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

    # Route-trace ledger teardown (FRE-452)
    from personal_agent.observability.route_trace import get_route_trace_ledger

    await get_route_trace_ledger().disconnect()

    # Cost Check Gate teardown (FRE-305)
    if cost_gate_reaper_task is not None:
        cost_gate_reaper_task.cancel()
        try:
            await cost_gate_reaper_task
        except asyncio.CancelledError:
            pass
        cost_gate_reaper_task = None
    if cost_gate_snapshotter_task is not None:
        cost_gate_snapshotter_task.cancel()
        try:
            await cost_gate_snapshotter_task
        except asyncio.CancelledError:
            pass
        cost_gate_snapshotter_task = None
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
async def _budget_denied_handler(_request: _Request, exc: _BudgetDenied) -> _JSONResponse:
    """Render :class:`BudgetDenied` as a structured HTTP 503.

    The PWA error card consumes ``error="budget_denied"`` and renders the
    cap / spend / reset_time fields explicitly. This was the regression
    fixed by ADR-0065: previously the cap-exceeded ValueError was swallowed
    and rendered as an empty assistant turn.
    """
    _bd_ctx = SystemTraceContext.new("budget_denied_handler")
    log.warning(
        "http_budget_denied",
        role=exc.role,
        time_window=exc.time_window,
        current_spend=str(exc.current_spend),
        cap=str(exc.cap),
        denial_reason=exc.denial_reason,
        trace_id=_bd_ctx.trace_id,
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

# AG-UI transport — WebSocket endpoint (ADR-0075, FRE-388)
from personal_agent.transport.agui.ws_endpoint import ws_router as transport_router  # noqa: E402

app.include_router(transport_router)

# Artifact substrate (ADR-0069 / FRE-227) — internal resolve endpoint (Worker)
# + FRE-368 public CF-Access-gated endpoints (PWA).
from personal_agent.service.artifacts_router import router as artifacts_router  # noqa: E402

app.include_router(artifacts_router)

# FRE-369 — user upload presign/complete endpoints.
from personal_agent.service.uploads_router import router as uploads_router  # noqa: E402

app.include_router(uploads_router)

# FRE-368 — client-side telemetry for ADR-0070 D8 measurement (card_click events).
from personal_agent.service.telemetry_router import router as telemetry_router  # noqa: E402

app.include_router(telemetry_router)

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


def _require_memory_service(trace_id: str) -> MemoryService:
    """Return the connected memory service or raise HTTP 503."""
    if not memory_service or not memory_service.connected:
        log.warning("memory_service_required_unavailable", trace_id=trace_id)
        raise HTTPException(status_code=503, detail="Memory service unavailable")
    return memory_service


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
    primary_model_id, config_path_str = _resolve_active_model_attribution()
    session = await repo.create(
        data,
        user_id=request_user.user_id,
        primary_model_at_creation=primary_model_id,
        model_config_path=config_path_str,
    )

    _sc_ctx = SystemTraceContext.new("create_session", session_id=str(session.session_id))
    log.info(
        "session_created",
        session_id=str(session.session_id),
        channel=session.channel,
        mode=session.mode,
        user_id=str(request_user.user_id),
        primary_model_at_creation=primary_model_id,
        model_config_path=config_path_str,
        trace_id=_sc_ctx.trace_id,
    )

    return SessionResponse.model_validate(session)


@app.put("/api/v1/preferences/constraint")
async def update_constraint_preference(
    data: ConstraintPreferenceUpdate,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, str]:
    """Upsert a standing constraint governance preference (ADR-0076).

    Validates ``preferred_action`` against the action-ID registry for the
    named constraint, then upserts the preference for the authenticated user.

    Args:
        data: Constraint name and preferred action.
        request_user: Authenticated user (CF Access).
        db: Async database session.

    Returns:
        The stored constraint name and preferred action.

    Raises:
        HTTPException: 422 if the constraint or action is unknown.
    """
    from personal_agent.orchestrator.constraint_options import CONSTRAINT_OPTIONS, option_ids
    from personal_agent.service.repositories.constraint_preferences_repository import (
        ConstraintPreferencesRepository,
    )

    if data.constraint_name not in CONSTRAINT_OPTIONS:
        raise HTTPException(status_code=422, detail=f"unknown constraint: {data.constraint_name}")
    valid_actions = {"always_pause", *option_ids(data.constraint_name)}
    if data.preferred_action not in valid_actions:
        raise HTTPException(
            status_code=422,
            detail=f"invalid preferred_action for {data.constraint_name}: {data.preferred_action}",
        )

    repo = ConstraintPreferencesRepository(db)
    await repo.upsert(
        user_id=request_user.user_id,
        constraint_name=data.constraint_name,
        preferred_action=data.preferred_action,
    )
    log.info(
        "constraint_preference_updated",
        user_id=str(request_user.user_id),
        constraint=data.constraint_name,
        preferred_action=data.preferred_action,
    )
    return {
        "constraint_name": data.constraint_name,
        "preferred_action": data.preferred_action,
    }


@app.get("/api/v1/preferences/location")
async def get_location_preference(
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, bool]:
    """Read the authenticated user's location feature and consent gates.

    Args:
        request_user: Authenticated user (CF Access or owner fallback).

    Returns:
        Operator feature flag and per-user consent flag.
    """
    ctx = SystemTraceContext.new(
        "get_location_preference",
        user_id=request_user.user_id,
    )
    if not settings.location_enabled:
        log.info(
            "location_preference_read",
            trace_id=ctx.trace_id,
            user_id=str(request_user.user_id),
            feature_enabled=False,
            location_consent_enabled=False,
        )
        return {"feature_enabled": False, "location_consent_enabled": False}

    svc = _require_memory_service(ctx.trace_id)
    consent = await svc.get_person_location_consent(str(request_user.user_id), ctx.trace_id)
    log.info(
        "location_preference_read",
        trace_id=ctx.trace_id,
        user_id=str(request_user.user_id),
        feature_enabled=True,
        location_consent_enabled=consent,
    )
    return {"feature_enabled": True, "location_consent_enabled": consent}


@app.patch("/api/v1/preferences/location")
async def update_location_preference(
    data: LocationPreferenceUpdate,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, bool]:
    """Update location consent and optionally store client coordinates.

    Args:
        data: Location preference update payload.
        request_user: Authenticated user (CF Access or owner fallback).

    Returns:
        Operator feature flag and effective per-user consent flag.

    Raises:
        HTTPException: 403 when the deployment-wide feature gate is disabled.
    """
    ctx = SystemTraceContext.new(
        "update_location_preference",
        user_id=request_user.user_id,
    )
    if not settings.location_enabled:
        log.info(
            "location_preference_update_denied",
            trace_id=ctx.trace_id,
            user_id=str(request_user.user_id),
            reason="feature_disabled",
        )
        raise HTTPException(status_code=403, detail="Location features are disabled.")

    svc = _require_memory_service(ctx.trace_id)
    user_id = str(request_user.user_id)
    if data.consent_enabled is not None:
        await svc.set_person_location_consent(user_id, data.consent_enabled, ctx.trace_id)
        consent = data.consent_enabled
    else:
        consent = await svc.get_person_location_consent(user_id, ctx.trace_id)

    if data.latitude is not None and data.longitude is not None and consent:
        from personal_agent.tools.location import ClientCoordinatesProvider

        resolution = await ClientCoordinatesProvider(
            data.latitude,
            data.longitude,
            data.timezone,
            settings.location_precision,
        ).resolve(ctx)
        if resolution.latitude is not None and resolution.longitude is not None:
            await svc.update_person_location(
                user_id,
                resolution.latitude,
                resolution.longitude,
                resolution.timezone,
                "client",
                ctx.trace_id,
            )
            log.info(
                "location_preference_coordinates_stored",
                trace_id=ctx.trace_id,
                user_id=user_id,
                source="client",
                timezone_set=resolution.timezone is not None,
            )

    log.info(
        "location_preference_updated",
        trace_id=ctx.trace_id,
        user_id=user_id,
        feature_enabled=True,
        location_consent_enabled=consent,
        coordinates_present=data.latitude is not None and data.longitude is not None,
    )
    return {"feature_enabled": True, "location_consent_enabled": consent}


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

    return SessionResponse.model_validate(session)


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

    return SessionResponse.model_validate(session)


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
    profile: str = "local",
    skill_routing_mode: str | None = None,
    channel: str = "CHAT",
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, str]:
    """Process a chat message.

    This is the main entry point for user interactions.

    Args:
        message: User's message
        session_id: Optional existing session ID (creates new if not provided)
        profile: Model profile to use (default: "local")
        skill_routing_mode: Override skill routing mode if provided
        channel: Request channel — pass "EVAL" from eval/benchmark harnesses to
            prevent side-effecting tools (e.g. create_linear_issue) from executing.
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

    # Idempotency guard: reject duplicate submissions within the TTL window
    # (FRE-392).  Only meaningful when a session_id is provided — new-session
    # requests always get a fresh session UUID so they can never collide.
    if session_id:
        dedup = get_deduplicator().check_and_record(session_id, message, trace_id)
        if dedup.is_duplicate:
            log.info(
                "chat.deduplicated",
                session_id=session_id,
                trace_id=trace_id,
                original_trace_id=dedup.original_trace_id,
            )
            raise HTTPException(status_code=409, detail="Message already being processed")

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
            primary_model_id, config_path_str = _resolve_active_model_attribution(
                trace_id=trace_id,
            )
            session = await repo.create(
                SessionCreate(execution_profile=profile),
                user_id=request_user.user_id,
                primary_model_at_creation=primary_model_id,
                model_config_path=config_path_str,
            )

    # FRE-51: await prior turn's assistant append (NoOp: background task; Redis: session-writer).
    sid = str(cast(UUID, session.session_id))
    from personal_agent.events.bus import get_event_bus
    from personal_agent.events.redis_backend import RedisStreamBus
    from personal_agent.events.session_write_waiter import await_previous_session_write

    if isinstance(get_event_bus(), RedisStreamBus):
        await await_previous_session_write(sid, trace_id=trace_id)
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

    # Activate execution profile using the stored session value (ADR-0079).
    # session.execution_profile is the server-authoritative source — for new
    # sessions it was just persisted from the request profile; for existing
    # sessions it is the DB-stored value (the session row is the source of
    # truth, same policy as /chat/stream via _resolve_session_profile).
    from personal_agent.config.profile import (  # noqa: PLC0415
        load_profile,
        set_current_profile,
        set_skill_routing_mode,
    )

    _effective_profile = str(session.execution_profile)
    try:
        _chat_profile = load_profile(_effective_profile)
        set_current_profile(_chat_profile)
    except Exception:
        log.warning("chat.unknown_profile", profile=_effective_profile, trace_id=trace_id)

    if skill_routing_mode:
        set_skill_routing_mode(skill_routing_mode)

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
            user_email=request_user.email,
            user_display_name=request_user.display_name,
            eval_mode=(channel.upper() == "EVAL"),
            authenticated=True,
        )

        response_content = result.get("reply", "No response generated")

    except Exception as e:
        error_id = str(uuid4())[:8]
        log.error(
            "orchestrator_call_failed",
            error_id=error_id,
            error=sanitize_error_message(e),
            error_type=type(e).__name__,
            trace_id=trace_id,
            exc_info=True,
        )
        # Do not include exception details in the HTTP response to avoid
        # information exposure; full context is in the structured log.
        response_content = f"An error occurred processing your request. (Error ID: {error_id})"
    finally:
        if scheduler and request_started:
            scheduler.notify_request_end()
        # Release the dedup entry so retries work immediately (FRE-392).
        if session_id:
            get_deduplicator().release(session_id, message)

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
                    eval_mode=(channel.upper() == "EVAL"),
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
        "profile": _effective_profile,
    }


# ============================================================================
# WebSocket Ticket Endpoint (ADR-0075 / FRE-388)
# ============================================================================


@app.post("/api/ws-ticket")
async def mint_ws_ticket_endpoint(
    body: dict[str, str],
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
    db: AsyncSession = Depends(get_db_session),  # noqa: B008
) -> dict[str, Any]:
    """Mint a short-lived single-use WebSocket ticket.

    The PWA calls this over HTTPS before opening a WebSocket connection.
    The returned ticket is passed as a query parameter on the WS handshake,
    avoiding the need to expose the real bearer token in the URL.

    Args:
        body: Must contain ``session_id`` (UUID string).
        request_user: Authenticated user from HTTPS headers.
        db: Database session for ownership verification.

    Returns:
        ``{"ticket": "...", "expires_in": 30}``
    """
    from personal_agent.service.ws_ticket import mint_ws_ticket  # noqa: E402

    session_id_str = body.get("session_id", "")
    try:
        session_uuid = UUID(session_id_str)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID") from exc

    repo = SessionRepository(db)
    session = await repo.get(session_uuid, user_id=request_user.user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    ticket = mint_ws_ticket(request_user, session_uuid)
    return {"ticket": ticket, "expires_in": settings.ws_ticket_ttl_seconds}


# AG-UI Streaming Chat Endpoint (ADR-0046 / FRE-207)
# ============================================================================


async def _resolve_session_profile(
    session_id: str, supplied: str | None, user_id: UUID, *, trace_id: str
) -> str:
    """Resolve the server-authoritative execution profile for a turn (ADR-0079).

    The session row is the source of truth, with a single asymmetry so a
    brand-new session can still honour the user's selection:

    - **Existing session** → always use the stored ``execution_profile``. The
      ``supplied`` value is advisory and **ignored** — a stale/reloaded client
      cannot overwrite it, and the sole mutator is ``PATCH /api/v1/sessions/{id}``
      (the toggle). This keeps the original cloud→local desync fixed.
    - **New session (no row yet)** → adopt ``supplied`` (the client's pill),
      falling back to ``"local"`` only when nothing was sent. The value is
      persisted when the background task creates the row. Without this a new
      "Cloud" session would silently run local (the FRE-419 regression).

    Args:
        session_id: Client-generated session UUID string.
        supplied: Profile name from the request, or None when omitted.
        user_id: Authenticated owner — scopes the read.
        trace_id: Trace id for logging (reserved for callers).

    Returns:
        The resolved profile name to run this turn with.

    Raises:
        HTTPException: 422 when ``supplied`` is not a known profile.
    """
    from personal_agent.config.profile import is_valid_profile

    if supplied is not None and not is_valid_profile(supplied):
        raise HTTPException(status_code=422, detail=f"unknown execution profile: {supplied}")

    session_uuid = UUID(session_id)
    async with AsyncSessionLocal() as db:
        repo = SessionRepository(db)
        session = await repo.get(session_uuid, user_id=user_id)

    if session is not None:
        # Existing session: stored value is authoritative; supplied is ignored.
        return str(session.execution_profile)

    # New session: adopt the client's selection (persisted at row creation).
    return supplied or "local"


@app.post("/chat/stream")
async def chat_stream_endpoint(
    message: str = Form(...),
    session_id: str = Form(...),
    profile: str | None = Form(default=None),
    client_msg_id: str | None = Form(default=None),
    attachments: str | None = Form(default=None),
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, str]:
    """AG-UI fire-and-forget chat endpoint for the PWA.

    Accepts a user message via form data, launches the full Seshat orchestrator
    pipeline as a background task, and returns immediately.  The client should
    connect to ``GET /ws/{session_id}`` to receive events as the model replies.

    The execution profile is **server-authoritative** (ADR-0079 / FRE-416):
    when ``profile`` is omitted the session's stored profile is used (never a
    silent ``local`` default); when provided it is validated, persisted, and
    echoed back. The resolved profile is returned so the client can reconcile.

    Args:
        message: User message text.
        session_id: Client-generated session UUID.
        profile: Optional execution profile override (e.g. ``"local"``,
            ``"cloud"``). Omitted → use the session's stored profile.
        client_msg_id: Optional client-generated idempotency key (UUID v4).
            When provided, duplicate submissions within the TTL window are
            detected and silently ignored (FRE-392).
        attachments: JSON-encoded list of completed upload dicts (FRE-369),
            or ``None``. Forwarded to the background task; injected into the
            orchestrator message after gateway classification.
        request_user: Resolved user identity (injected by FastAPI).

    Returns:
        ``{"session_id": ..., "status": "streaming", "profile": <resolved>}``
        once the background task is launched, or the dict with
        ``"deduplicated": "true"`` when the request was a duplicate.

    Raises:
        HTTPException: 422 if ``session_id`` is not a valid UUID v4, or if
            ``profile`` is supplied but not a known profile.
    """
    try:
        UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID v4") from exc

    trace_id = str(uuid4())

    dedup = get_deduplicator().check_and_record(
        session_id, message, trace_id, client_msg_id=client_msg_id
    )
    if dedup.is_duplicate:
        log.info(
            "chat_stream.deduplicated",
            session_id=session_id,
            trace_id=trace_id,
            original_trace_id=dedup.original_trace_id,
        )
        return {"session_id": session_id, "status": "streaming", "deduplicated": "true"}

    # Validate attachments JSON before task launch so we can surface a 422 (FRE-369).
    # Per-row ownership filtering happens inside the background task after the gateway
    # pipeline (so TaskType routing is unaffected).
    if attachments:
        import json as _json  # noqa: PLC0415

        try:
            _json.loads(attachments)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="attachments must be valid JSON",
            ) from exc

    resolved_profile = await _resolve_session_profile(
        session_id, profile, request_user.user_id, trace_id=trace_id
    )

    asyncio.create_task(
        _process_chat_stream_background(
            session_id=session_id,
            message=message,
            profile_name=resolved_profile,
            user_id=request_user.user_id,
            trace_id=trace_id,
            user_email=request_user.email,
            user_display_name=request_user.display_name,
            client_msg_id=client_msg_id,
            attachments_json=attachments,
        )
    )

    log.info(
        "chat_stream.launched",
        session_id=session_id,
        profile=resolved_profile,
        profile_supplied=profile,
        trace_id=trace_id,
    )
    return {"session_id": session_id, "status": "streaming", "profile": resolved_profile}


# ============================================================================
# Inference Availability (Mac SLM Tunnel)
# ============================================================================


def _cf_access_headers() -> dict[str, str]:
    """Build Cloudflare Access service-token headers from settings.

    Returns an empty dict when the CF Access credentials are not configured
    (local-only or test deployments).

    Returns:
        A dict with ``CF-Access-Client-Id`` and ``CF-Access-Client-Secret``
        when both settings are present, else an empty dict.
    """
    headers: dict[str, str] = {}
    if settings.cf_access_client_id and settings.cf_access_client_secret:
        headers["CF-Access-Client-Id"] = settings.cf_access_client_id
        headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret
    return headers


@app.get("/api/inference/status")
async def inference_status(profile: str = "local") -> dict[str, Any]:
    """Report availability of the requested execution profile's inference path.

    - ``local`` (default): live-probes the Mac SLM tunnel via
      :func:`~personal_agent.observability.slm_health.probe.probe_slm_health`
      and updates the process-global health cache. Returns backward-compatible
      keys (``status`` / ``profile`` / ``local`` / ``latency_ms``) plus
      optional enriched fields (``gpu_util_pct``, ``queue_depth``,
      ``model_loaded``, ``degrade_reason``) when the SLM exposes them.
    - ``cloud``: reports ``up`` when the cloud provider is configured
      (Anthropic API key present), else ``down``. Configuration check only —
      live provider outages are not detected here.

    The ``local`` key is retained for backward compatibility with the FRE-421
    PWA availability pill.

    Args:
        profile: ``"local"`` or ``"cloud"``.

    Returns:
        Dict with ``status``, ``profile``, ``local``, ``latency_ms`` (all
        existing callers), plus optional enriched fields for new consumers.
    """
    if profile == "cloud":
        status = "up" if settings.anthropic_api_key else "down"
        return {"status": status, "profile": "cloud", "local": status, "latency_ms": None}

    from personal_agent.observability.slm_health import (
        probe_slm_health,
        set_cached_snapshot,
    )

    inf_ctx = SystemTraceContext.new("inference_status_probe")
    snapshot = await probe_slm_health(
        url=settings.slm_health_url,
        cf_headers=_cf_access_headers(),
        timeout_s=3.0,
        trace_id=inf_ctx.trace_id,
        gpu_util_degraded_pct=settings.slm_gpu_util_degraded_pct,
        queue_depth_degraded=settings.slm_queue_depth_degraded,
    )
    # Update process cache so the executor error-hint reads fresh state.
    set_cached_snapshot(snapshot)

    # Map "degraded" to "local" key value so the PWA pill can distinguish.
    local_val = snapshot.status  # "up" | "degraded" | "down"
    latency_ms = int(snapshot.probe_latency_ms) if snapshot.probe_latency_ms is not None else None

    response: dict[str, Any] = {
        # --- backward-compatible keys (FRE-421 PWA pill) ---
        "status": snapshot.status,
        "profile": "local",
        "local": local_val,
        "latency_ms": latency_ms,
        # --- enriched fields (new consumers; None until Mac-side child ships) ---
        "gpu_util_pct": snapshot.gpu_util_pct,
        "queue_depth": snapshot.queue_depth,
        "model_loaded": snapshot.model_loaded,
        "degrade_reason": snapshot.degrade_reason(),
    }
    return response


# ============================================================================
# Memory Endpoints (Phase 2.2)
# ============================================================================


@app.get("/memory/interests")
async def get_user_interests(
    limit: int = 20,
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, Any]:
    """Get the caller's interest profile (frequently mentioned entities).

    Scoped to the authenticated user via visibility filter (FRE-229).
    Closes the cross-user data leak hotfix — previously this endpoint
    returned entities across all users.

    Args:
        limit: Maximum number of entities to return.
        request_user: Resolved user identity (injected by FastAPI from
            the CF Access header).

    Returns:
        List of entities sorted by mention frequency, scoped to the caller.
    """
    if not memory_service or not memory_service.connected:
        raise HTTPException(status_code=503, detail="Memory service not available")

    entities = await memory_service.get_user_interests(
        limit=limit,
        user_id=request_user.user_id,
        authenticated=True,
    )
    return {"entities": [e.model_dump() for e in entities]}


@app.post("/memory/query")
async def query_memory(
    query: dict[str, Any],
    request_user: RequestUser = Depends(get_request_user),  # noqa: B008
) -> dict[str, Any]:
    """Query memory graph for the caller's conversations and entities.

    Scoped to the authenticated user via the visibility filter (FRE-229).
    The ``user_id`` field in the query body is overwritten with the
    authenticated identity to prevent a PWA-side modification from
    requesting another user's data.

    Args:
        query: Query parameters (entity_names, entity_types, limit, etc.).
            Any caller-supplied ``user_id`` is ignored.
        request_user: Resolved user identity (injected by FastAPI).

    Returns:
        Memory query results with conversations and entities, scoped to
        the caller.
    """
    if not memory_service or not memory_service.connected:
        raise HTTPException(status_code=503, detail="Memory service not available")

    from personal_agent.memory.models import MemoryQuery

    # Strip any caller-supplied user_id / authenticated flag — these come
    # from the verified identity only, never from the request body.
    safe_query = {k: v for k, v in query.items() if k not in ("user_id", "authenticated")}
    memory_query = MemoryQuery(
        **safe_query,
        user_id=request_user.user_id,
        authenticated=True,
    )
    result = await memory_service.query_memory(
        memory_query,
        feedback_key=query.get("session_id"),
        query_text=query.get("query_text"),
    )
    return result.model_dump()
