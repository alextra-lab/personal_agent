"""FRE-1215: the chat entrypoints must adopt the active span's trace identity.

The ticket reported "44% of observed model spend never reaches ``api_costs``". No spend
was lost. ``/chat`` and ``/chat/stream`` minted ``trace_id = str(uuid4())`` while
:class:`~personal_agent.telemetry.otel_middleware.RequestRootSpanMiddleware` already had a
root span open, and
:func:`~personal_agent.telemetry.logger._add_span_context` (ADR-0129 D4) overwrites
``event_dict["trace_id"]`` with the *active span's* id on every record. So Postgres
recorded the minted uuid4 while Elasticsearch recorded the span id, and the
reconciliation join between the two substrates compared unrelated strings.

ADR-0129 D1 makes OpenTelemetry the owner of trace identity — ``TraceContext.new_trace()``
already reads the active span. These endpoints predate that bridge and were missed by
FRE-1064/FRE-1065.

The fixture mirrors ``test_otel_root_span.py``: an in-memory tracer provider plus the real
``_add_span_context`` processor, so a captured record carries exactly the trace_id
production would ship to Elasticsearch. No live Postgres/ES/Neo4j is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
import structlog
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from personal_agent.llm_client.cost_tracker import CostTrackerService
from personal_agent.llm_client.prompt_identity import derive_prompt_identity
from personal_agent.llm_client.telemetry import emit_model_call_completed
from personal_agent.observability.joinability.walk import _normalize_trace_id
from personal_agent.service.auth import RequestUser
from personal_agent.service.idempotency import DeduplicationResult
from personal_agent.telemetry.logger import _add_span_context
from personal_agent.telemetry.trace import TraceContext

_SESSION_ID = "35d379be-b465-4b46-86b3-ff6db56c57ab"
_USER_ID = UUID("1f7cc4bc-3f83-4f21-88e5-96b4b08b116a")


@pytest.fixture()
def root_span() -> Iterator[str]:
    """Open a root span the way the request middleware does; yield its trace id.

    Yields:
        The active span's trace id as 32 lowercase hex characters — what
        ``_add_span_context`` stamps onto every log record emitted inside the block.
    """
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("POST /chat/stream") as span:
        yield format(span.get_span_context().trace_id, "032x")


@contextmanager
def _capture() -> Iterator[list[MutableMapping[str, Any]]]:
    """Capture log records through the real span-context processor.

    ``structlog.testing.capture_logs`` swaps the processor chain for the duration of
    the block and restores it afterwards, so global structlog state never leaks.

    Yields:
        The list captured records are appended to.
    """
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars, _add_span_context]  # type: ignore[list-item]
    ) as captured:
        yield captured


class _FakeConnection:
    """asyncpg connection stand-in that records the parameters bound to each INSERT."""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls

    async def fetchval(self, _sql: str, *params: Any) -> int:
        """Record the bound parameters and return a synthetic row id.

        Args:
            _sql: The statement text (unused — the binding is what matters here).
            *params: Positional parameters as bound by ``record_api_call``.

        Returns:
            A fixed row id, standing in for ``RETURNING id``.
        """
        self._calls.append(params)
        return 11455


class _FakePool:
    """asyncpg pool stand-in exposing the ``async with pool.acquire()`` protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def acquire(self) -> "_FakePool":
        """Return self as the async context manager ``record_api_call`` expects."""
        return self

    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection(self.calls)

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _call_chat_stream() -> tuple[str, list[MutableMapping[str, Any]]]:
    """Drive the real ``/chat/stream`` endpoint, capturing both sides of the identity.

    The background task is patched out — the defect lives at the endpoint's minting
    expression, and running the orchestrator would need the whole live substrate. What
    is *not* stubbed is the value under test: the trace id the endpoint produces and
    threads into ``_process_chat_stream_background`` is the same string that reaches
    ``record_api_call`` and therefore ``api_costs.trace_id``.

    Returns:
        A tuple of (trace id threaded to the background task, captured log records).
    """
    from personal_agent.service.app import chat_stream_endpoint

    threaded: dict[str, str] = {}

    def _capture_task(coro: Any) -> MagicMock:
        threaded["trace_id"] = coro.cr_frame.f_locals["trace_id"]
        coro.close()  # never awaited — avoid an "unawaited coroutine" warning
        return MagicMock()

    dedup = MagicMock()
    dedup.check_and_record.return_value = DeduplicationResult(is_duplicate=False)

    with (
        patch("personal_agent.service.app.get_deduplicator", return_value=dedup),
        patch(
            "personal_agent.service.app._resolve_session_selection",
            new=AsyncMock(return_value=("anthropic-sonnet", "default")),
        ),
        patch.object(asyncio, "create_task", side_effect=_capture_task),
        _capture() as captured,
    ):
        await chat_stream_endpoint(
            message="hello",
            session_id=_SESSION_ID,
            primary_selection=None,
            client_msg_id=None,
            attachments=None,
            request_user=RequestUser(user_id=_USER_ID, email="o@example.test"),
        )

    return threaded["trace_id"], captured


@pytest.mark.asyncio
async def test_chat_stream_ledger_id_matches_the_id_elasticsearch_receives(
    root_span: str,
) -> None:
    """The id bound for ``api_costs`` is the id ES records for the same turn.

    Before the fix the endpoint minted an unrelated uuid4, so these two differ and the
    cross-substrate join can never match.
    """
    ledger_side, captured = await _call_chat_stream()

    launched = next(r for r in captured if r["event"] == "chat_stream.launched")
    es_side = launched["trace_id"]

    assert _normalize_trace_id(ledger_side) == _normalize_trace_id(es_side), (
        f"split trace identity: Postgres would record {ledger_side!r} while "
        f"Elasticsearch records {es_side!r}"
    )
    assert _normalize_trace_id(ledger_side) == root_span, (
        "the turn must adopt the active span's identity (ADR-0129 D1)"
    )


@pytest.mark.asyncio
async def test_cost_bearing_turn_produces_a_joinable_es_event_and_ledger_row(
    root_span: str,
) -> None:
    """AC-2 — a cost-bearing call yields both artefacts, and they name one trace.

    Each side is produced by the *real* adapter rather than asserted from a shared
    string: the ledger side runs ``CostTrackerService.record_api_call`` against a fake
    pool that captures the bound INSERT parameters, and the ES side runs
    ``emit_model_call_completed`` through the real structlog chain with the real
    span-context processor active.
    """
    trace_id, _ = await _call_chat_stream()

    # --- ledger side: the real INSERT binding ---------------------------------
    tracker = CostTrackerService()
    pool = _FakePool()
    tracker.pool = pool  # type: ignore[assignment]
    await tracker.record_api_call(
        provider="anthropic",
        model="anthropic/claude-haiku-4-5-20251001",
        input_tokens=1200,
        output_tokens=64,
        cost_usd=0.001085,
        trace_id=UUID(trace_id),
        session_id=UUID(_SESSION_ID),
        purpose="skill_routing",
        latency_ms=914,
    )
    # trace_id is the 9th bound parameter of the INSERT (see record_api_call).
    ledger_rows = [
        {"trace_id": str(params[8]), "cost_usd": Decimal(str(params[5]))} for params in pool.calls
    ]

    # --- ES side: the real emit through the real processor --------------------
    with _capture() as captured:
        emit_model_call_completed(
            log=structlog.get_logger(__name__),
            role="skill_routing",
            model="anthropic/claude-haiku-4-5-20251001",
            endpoint="anthropic",
            provider="anthropic",
            trace_ctx=TraceContext(trace_id=trace_id, session_id=_SESSION_ID),
            span_id="7e072d7ce19ce517",
            input_tokens=1200,
            output_tokens=64,
            prompt_identity=derive_prompt_identity(
                "role.skill_routing", static_prefix="sys", full_prompt="sys"
            ),
            extra={"cost_usd": 0.001085},
        )
    es_events = [r for r in captured if r["event"] == "model_call_completed"]

    assert len(ledger_rows) == 1, "the ledger write must have happened"
    assert len(es_events) == 1, "the ES event must have been emitted"
    assert es_events[0]["cost_usd"] > 0
    assert _normalize_trace_id(ledger_rows[0]["trace_id"]) == _normalize_trace_id(
        es_events[0]["trace_id"]
    ), "the ledger row and its ES counterpart must name the same trace"


@pytest.mark.asyncio
async def test_reconciliation_over_the_generated_corpus_finds_no_orphan_spend(
    root_span: str,
) -> None:
    """AC-3 — re-run the measurement's join over a test-generated corpus.

    Mirrors the joinability probe's comparison (``observability/joinability/walk.py``
    ``cost_bearing_trace`` aggregation, ~line 685): collect the traces whose
    ``model_call_completed`` carries ``cost_usd > 0``, normalise both sides with the
    FRE-1186 normaliser, and subtract the traces present in ``api_costs``. The result
    must be empty.
    """
    trace_id, _ = await _call_chat_stream()

    tracker = CostTrackerService()
    pool = _FakePool()
    tracker.pool = pool  # type: ignore[assignment]
    await tracker.record_api_call(
        provider="anthropic",
        model="anthropic/claude-haiku-4-5-20251001",
        input_tokens=1200,
        output_tokens=64,
        cost_usd=0.001085,
        trace_id=UUID(trace_id),
        session_id=UUID(_SESSION_ID),
        purpose="skill_routing",
    )

    with _capture() as captured:
        emit_model_call_completed(
            log=structlog.get_logger(__name__),
            role="skill_routing",
            model="anthropic/claude-haiku-4-5-20251001",
            endpoint="anthropic",
            provider="anthropic",
            trace_ctx=TraceContext(trace_id=trace_id, session_id=_SESSION_ID),
            span_id="7e072d7ce19ce517",
            input_tokens=1200,
            output_tokens=64,
            prompt_identity=derive_prompt_identity(
                "role.skill_routing", static_prefix="sys", full_prompt="sys"
            ),
            extra={"cost_usd": 0.001085},
        )

    cost_bearing = {
        _normalize_trace_id(r["trace_id"])
        for r in captured
        if r["event"] == "model_call_completed" and (r.get("cost_usd") or 0) > 0
    }
    ledgered = {_normalize_trace_id(str(params[8])) for params in pool.calls}

    assert cost_bearing, "the corpus must contain at least one cost-bearing trace"
    assert cost_bearing - ledgered == set(), (
        f"cost-bearing traces with no api_costs row: {sorted(cost_bearing - ledgered)}"
    )


def test_system_paths_are_unaffected() -> None:
    """A context built with no active span still mints its own id (ADR-0129 D1).

    Guards the fallback the background/scheduler paths depend on: reading the span is
    only correct when there *is* one, and a nil-id context would collide on every row.
    """
    assert otel_trace.get_current_span().get_span_context().is_valid is False
    ctx = TraceContext.new_trace(session_id=_SESSION_ID)
    assert len(ctx.trace_id) == 32
    assert UUID(ctx.trace_id)  # coercible to the Postgres uuid column
    assert ctx.trace_id != TraceContext.new_trace().trace_id  # freshly minted each time
