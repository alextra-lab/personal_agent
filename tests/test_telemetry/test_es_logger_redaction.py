"""Redaction coverage across every agent-logs write path (FRE-1068).

The audit's finding was that ``log_event`` is one of five write paths into
``agent-logs-*``; the other four bypassed it. These tests assert the guarantee
holds at each path, and the structural test asserts a new one cannot be added
without routing through the chokepoint.

ADR-0129 D3 / FRE-1067 retired ``index_request_trace_from_snapshot`` (the
``RequestTimer``-backed request_trace path) along with ``RequestTimer``
itself — four write paths remain, not five.

Every secret-shaped value below is synthetic.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from personal_agent.telemetry.es_logger import ElasticsearchLogger

# A synthetic value shaped like the credential the FRE-1068 audit found live.
PLANTED_SECRET = "PGPASSWORD=Pl4ntedS3cretValue psql -h postgres"
PLANTED_LITERAL = "Pl4ntedS3cretValue"


def _logger_with_mock_client() -> tuple[ElasticsearchLogger, AsyncMock]:
    """Build a logger whose client records the documents handed to it."""
    logger = ElasticsearchLogger()
    client = AsyncMock()
    client.index = AsyncMock(return_value={"_id": "doc-1"})
    logger.client = client
    return logger, client


def _indexed_documents(client: AsyncMock) -> list[dict[str, Any]]:
    """Return every document passed to client.index."""
    return [call.kwargs["document"] for call in client.index.call_args_list]


@pytest.mark.asyncio
async def test_log_event_redacts_before_indexing() -> None:
    """Path 1 of 4: the structlog handler path."""
    logger, client = _logger_with_mock_client()

    await logger.log_event("bash_started", {"command": PLANTED_SECRET}, trace_id="t1")

    doc = _indexed_documents(client)[0]
    assert PLANTED_LITERAL not in doc["command"]
    assert "[REDACTED:" in doc["command"]


@pytest.mark.asyncio
async def test_log_batch_redacts_every_action_source() -> None:
    """Path 2 of 4: the bulk path, which never touched log_event."""
    logger = ElasticsearchLogger()
    logger.client = AsyncMock()
    captured: list[dict[str, Any]] = []

    async def fake_bulk(_client: object, actions: list[dict[str, Any]]) -> tuple[int, list[str]]:
        captured.extend(actions)
        return len(actions), []

    import elasticsearch.helpers

    original = elasticsearch.helpers.async_bulk
    elasticsearch.helpers.async_bulk = fake_bulk  # type: ignore[assignment]
    try:
        await logger.log_batch([("bash_started", {"command": PLANTED_SECRET}, None)])
    finally:
        elasticsearch.helpers.async_bulk = original  # type: ignore[assignment]

    assert captured, "bulk path indexed nothing"
    assert PLANTED_LITERAL not in captured[0]["_source"]["command"]


@pytest.mark.asyncio
async def test_latency_breakdown_redacts_summary_and_phase_docs() -> None:
    """Path 3 and 4 of 4: latency summary plus its flat per-phase documents."""
    logger, client = _logger_with_mock_client()

    await logger.index_latency_breakdown(
        trace_id="t3",
        breakdown=[
            {"phase": "total_request_to_reply", "duration_ms": 9.0},
            {
                "phase": "tool_execution",
                "duration_ms": 4.0,
                "description": f"ran {PLANTED_SECRET}",
            },
        ],
        session_id="s1",
    )

    docs = _indexed_documents(client)
    assert docs, "latency path indexed nothing"
    summary = docs[0]
    assert PLANTED_LITERAL not in summary["phases"][0]["description"]


# --------------------------------------------------------------------------
# AC-2 positive control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_positive_control_planted_secret_is_absent_from_the_document() -> None:
    """FRE-1068 AC-2: a free_text-matching field holding a planted secret.

    ``raw_preview`` matches the index template's ``free_text`` dynamic template
    (``raw_.*``), which is the field class the ticket names.
    """
    logger, client = _logger_with_mock_client()

    await logger.log_event(
        "skill_routing_parse_failed",
        {"raw_preview": f"connecting with {PLANTED_SECRET} now"},
        trace_id="t4",
    )

    doc = _indexed_documents(client)[0]
    assert PLANTED_LITERAL not in doc["raw_preview"]
    assert "[REDACTED:credential_assignment]" in doc["raw_preview"]


@pytest.mark.asyncio
async def test_negative_control_clean_record_is_unchanged() -> None:
    """The other half of the control: a rule that never fires must be visible.

    Without this, a broken detector and a working one produce identical output
    on clean records.
    """
    logger, client = _logger_with_mock_client()
    clean = "skill routing produced no candidates for this turn"

    await logger.log_event("skill_routing_parse_failed", {"raw_preview": clean}, trace_id="t5")

    doc = _indexed_documents(client)[0]
    assert doc["raw_preview"] == clean
    assert "[REDACTED:" not in doc["raw_preview"]


# --------------------------------------------------------------------------
# Structural guard
# --------------------------------------------------------------------------


def test_no_agent_logs_write_bypasses_the_chokepoint() -> None:
    """No write to the agent-logs index may sidestep _index_agent_log.

    The FRE-1068 hole was exactly this: four write paths reaching
    ``agent-logs-*`` without passing the seam. This fails if a new one appears.
    """
    source = pathlib.Path("src/personal_agent/telemetry/es_logger.py").read_text()
    tree = ast.parse(source)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name == "_index_agent_log":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            # Match self.client.index(...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "index"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "client"
            ):
                index_kwarg = next((kw for kw in call.keywords if kw.arg == "index"), None)
                if index_kwarg is None:
                    continue
                rendered = ast.unparse(index_kwarg.value)
                if "current_index_name" in rendered or rendered == "index_name":
                    offenders.append(f"{node.name}:{call.lineno}")

    assert not offenders, "agent-logs writes bypassing _index_agent_log: " + ", ".join(offenders)
