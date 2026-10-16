"""FRE-1612: each promotion run writes one ``promotion_run`` funnel event.

The funnel dashboard reads ``eligible``, ``filed``, ``linked``, ``open_pipeline_count`` and
``cap`` from ``agent-captains-funnel-events-*``. A run that files nothing must still write
its event, or the dashboard cannot tell "no run" from "a run that found nothing".
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import personal_agent.captains_log.promotion as promotion_module
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)
from personal_agent.captains_log.promotion import PromotionCriteria, PromotionPipeline


def _write(log_dir: pathlib.Path, n: int, *, scope: ChangeScope) -> str:
    """Write one promotable entry and return its id."""
    entry_id = f"CL-20260901-00000{n}"
    entry = CaptainLogEntry(
        entry_id=entry_id,
        timestamp=datetime.now(timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title=f"Task {n}",
        rationale="Corroborated.",
        proposed_change=ProposedChange(
            what=f"Reduce retries in path {n}",
            why=f"Retries doubled latency on path {n}.",
            how="Cap the retries at two.",
            category=ChangeCategory.RELIABILITY,
            scope=scope,
            source=ProposalSource.STATISTICAL_DETECTOR,
            fingerprint=f"fp-run-{n}",
            seen_count=5,
            first_seen=datetime.now(timezone.utc) - timedelta(days=14),
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        telemetry_refs=[],
    )
    (log_dir / f"{entry_id}-reflection.json").write_text(
        entry.model_dump_json(indent=2), encoding="utf-8"
    )
    return entry_id


def _linear(open_count: int | Exception) -> MagicMock:
    """A Linear client double."""
    lc = MagicMock()
    if isinstance(open_count, Exception):
        lc.count_open_pipeline_issues = AsyncMock(side_effect=open_count)
    else:
        lc.count_open_pipeline_issues = AsyncMock(return_value=open_count)
    lc.resolve_project_id = AsyncMock(return_value="project-uuid")
    return lc


def _es() -> MagicMock:
    handler = MagicMock()
    handler._connected = True
    handler.es_logger.index_document = AsyncMock()
    handler.es_logger.update_by_query = AsyncMock()
    return handler


def _events(handler: MagicMock, event_type: str) -> list[dict[str, Any]]:
    """Funnel documents of one type that the handler indexed."""
    docs = [call.args[1] for call in handler.es_logger.index_document.await_args_list]
    return [d for d in docs if d["event_type"] == event_type]


def _pipeline(
    log_dir: pathlib.Path,
    lc: MagicMock,
    handler: MagicMock,
    *,
    create: AsyncMock | None = None,
    dry_run: bool = False,
) -> PromotionPipeline:
    return PromotionPipeline(
        log_dir=log_dir,
        criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
        create_issue_fn=create or AsyncMock(side_effect=["FRE-9001", "FRE-9002"]),
        linear_client=lc,
        es_handler=handler,
        dry_run=dry_run,
    )


@pytest.fixture(autouse=True)
def _no_side_effects() -> Any:
    """Keep the run away from the bus, the sysgraph and the real Captain's Log manager."""
    with (
        patch.object(PromotionPipeline, "_publish_promotion_events", AsyncMock()),
        patch.object(
            PromotionPipeline,
            "_existing_linear_issue_for_fingerprint",
            AsyncMock(return_value=None),
        ),
        patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10),
    ):
        yield


@pytest.mark.asyncio
async def test_run_that_files_writes_its_counts(tmp_path: pathlib.Path) -> None:
    """Two eligible entries, 3 of 10 open: eligible=2, filed=2, linked=0, open=3, cap=10."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    _write(tmp_path, 2, scope=ChangeScope.ORCHESTRATOR)
    handler = _es()

    await _pipeline(tmp_path, _linear(3), handler).run()

    (event,) = _events(handler, "promotion_run")
    assert event["eligible"] == 2
    assert event["filed"] == 2
    assert event["linked"] == 0
    assert event["open_pipeline_count"] == 3
    assert event["cap"] == 10
    assert "@timestamp" in event


@pytest.mark.asyncio
async def test_throttled_run_still_writes_its_event(tmp_path: pathlib.Path) -> None:
    """At the cap, nothing files, and the event shows eligible entries, filed=0, open=cap."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    handler = _es()
    create = AsyncMock()

    await _pipeline(tmp_path, _linear(10), handler, create=create).run()

    create.assert_not_awaited()
    (event,) = _events(handler, "promotion_run")
    assert (event["eligible"], event["filed"], event["linked"]) == (1, 0, 0)
    assert event["open_pipeline_count"] == 10
    assert len(_events(handler, "throttled_budget")) == 1


@pytest.mark.asyncio
async def test_unreadable_count_omits_the_open_count(tmp_path: pathlib.Path) -> None:
    """When the Linear count fails, the event carries no open_pipeline_count."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    handler = _es()

    await _pipeline(tmp_path, _linear(RuntimeError("linear down")), handler).run()

    (event,) = _events(handler, "promotion_run")
    assert event["filed"] == 0
    assert "open_pipeline_count" not in event
    assert event["cap"] == 10


@pytest.mark.asyncio
async def test_run_with_no_entries_writes_an_event_of_zeros(tmp_path: pathlib.Path) -> None:
    """An empty run is a run: eligible=0, filed=0, linked=0."""
    handler = _es()

    await _pipeline(tmp_path, _linear(3), handler).run()

    (event,) = _events(handler, "promotion_run")
    assert (event["eligible"], event["filed"], event["linked"]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_link_to_an_existing_ticket_counts_as_linked_not_filed(
    tmp_path: pathlib.Path,
) -> None:
    """A fingerprint match links. It adds to linked, not filed, and uses no cap slot."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    handler = _es()
    create = AsyncMock()

    with patch.object(
        PromotionPipeline, "_existing_linear_issue_for_fingerprint", AsyncMock(return_value="FRE-5")
    ):
        await _pipeline(tmp_path, _linear(10 - 1), handler, create=create).run()

    create.assert_not_awaited()
    (event,) = _events(handler, "promotion_run")
    assert (event["eligible"], event["filed"], event["linked"]) == (1, 0, 1)


@pytest.mark.asyncio
async def test_dry_run_writes_no_event(tmp_path: pathlib.Path) -> None:
    """A dry run writes nothing to the funnel index."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    handler = _es()

    await _pipeline(tmp_path, _linear(3), handler, dry_run=True).run()

    handler.es_logger.index_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_filing_stamps_filed_at_on_the_reflection_document(tmp_path: pathlib.Path) -> None:
    """The reflection document gets filed_at beside linear_issue_id, as an ISO UTC time."""
    entry_id = _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    handler = _es()

    await _pipeline(tmp_path, _linear(0), handler, create=AsyncMock(return_value="FRE-9001")).run()

    stamps = [
        call.args
        for call in handler.es_logger.update_by_query.await_args_list
        if call.args[0] == "agent-captains-reflections-*"
    ]
    assert len(stamps) == 1
    _, query, script, params = stamps[0]
    assert query == {"term": {"entry_id": entry_id}}
    assert "ctx._source.filed_at = params.filed_at" in script
    assert "ctx._source.linear_issue_id = params.linear_issue_id" in script
    assert params["linear_issue_id"] == "FRE-9001"
    assert datetime.fromisoformat(params["filed_at"]).utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_link_to_an_existing_ticket_stamps_no_filed_at(tmp_path: pathlib.Path) -> None:
    """The pipeline did not file a linked ticket, so its document gets no filed_at."""
    _write(tmp_path, 1, scope=ChangeScope.LLM_CLIENT)
    handler = _es()

    with patch.object(
        PromotionPipeline, "_existing_linear_issue_for_fingerprint", AsyncMock(return_value="FRE-5")
    ):
        await _pipeline(tmp_path, _linear(0), handler).run()

    (stamp,) = [
        call.args
        for call in handler.es_logger.update_by_query.await_args_list
        if call.args[0] == "agent-captains-reflections-*"
    ]
    _, _, script, params = stamp
    assert script == "ctx._source.linear_issue_id = params.linear_issue_id"
    assert params == {"linear_issue_id": "FRE-5"}
