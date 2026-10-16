"""FRE-1612: an outcome stamp carries the time of the outcome and the latency from filing.

The Painless script is the part that can be wrong in a way a mock does not show. The
unit tests pin its shape. The ``live`` test runs it on the isolated test stack
(``AGENT_ELASTICSEARCH_URL``, port 9201) when that stack is up, and skips otherwise.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.brainstem.jobs.outcome_ingestion import (
    _STAMP_OUTCOME_SCRIPT,
    _stamp_ticket_outcome,
)

pytestmark = pytest.mark.asyncio


async def test_stamp_sends_the_outcome_time_as_iso_utc() -> None:
    """The params carry ticket_outcome and an ISO ticket_outcome_at in UTC."""
    handler = MagicMock()
    handler._connected = True
    handler.es_logger.update_by_query = AsyncMock(return_value=1)

    await _stamp_ticket_outcome(handler, "FRE-77", "owner-rejected")

    _, query, script, params = handler.es_logger.update_by_query.await_args.args
    assert query == {"term": {"linear_issue_id": "FRE-77"}}
    assert script == _STAMP_OUTCOME_SCRIPT
    assert params["ticket_outcome"] == "owner-rejected"
    assert datetime.fromisoformat(params["ticket_outcome_at"]).tzinfo is not None


async def test_script_computes_latency_only_when_filed_at_exists() -> None:
    """The script guards on filed_at, so a pre-FRE-1612 document gets no hours_to_outcome."""
    assert "ctx._source.filed_at != null" in _STAMP_OUTCOME_SCRIPT
    assert "ctx._source.hours_to_outcome" in _STAMP_OUTCOME_SCRIPT


async def test_live_script_on_the_test_stack() -> None:
    """Run the script on the test Elasticsearch: 3.5 h from filing, and none without filed_at."""
    from elasticsearch import AsyncElasticsearch

    url = os.environ.get("AGENT_ELASTICSEARCH_URL", "")
    if ":9201" not in url:
        pytest.skip("not pointed at the isolated test stack")
    client = AsyncElasticsearch(url)
    index = f"fre1612-scratch-{uuid.uuid4().hex[:8]}"
    try:
        try:
            await client.indices.create(
                index=index,
                mappings={
                    "properties": {
                        "linear_issue_id": {"type": "keyword"},
                        "filed_at": {"type": "date"},
                    }
                },
            )
        except Exception:
            pytest.skip("test Elasticsearch is not reachable")
        await client.index(
            index=index,
            id="filed",
            document={"linear_issue_id": "FRE-1", "filed_at": "2026-10-16T10:00:00.123456+00:00"},
        )
        await client.index(index=index, id="old", document={"linear_issue_id": "FRE-2"})
        # A document re-indexed from the entry carries filed_at in Pydantic's "Z" form.
        await client.index(
            index=index,
            id="zulu",
            document={"linear_issue_id": "FRE-3", "filed_at": "2026-10-16T10:00:00Z"},
        )
        await client.indices.refresh(index=index)
        for issue in ("FRE-1", "FRE-2", "FRE-3"):
            await client.update_by_query(
                index=index,
                query={"term": {"linear_issue_id": issue}},
                script={
                    "lang": "painless",
                    "source": _STAMP_OUTCOME_SCRIPT,
                    "params": {
                        "ticket_outcome": "shipped",
                        "ticket_outcome_at": datetime(
                            2026, 10, 16, 13, 30, 0, 500000, tzinfo=timezone.utc
                        ).isoformat(),
                    },
                },
                refresh=True,
            )
        filed = (await client.get(index=index, id="filed"))["_source"]
        old = (await client.get(index=index, id="old"))["_source"]
        assert filed["hours_to_outcome"] == pytest.approx(3.5, abs=0.001)
        assert filed["ticket_outcome"] == "shipped"
        zulu = (await client.get(index=index, id="zulu"))["_source"]
        assert zulu["hours_to_outcome"] == pytest.approx(3.5, abs=0.001)
        assert "hours_to_outcome" not in old
        assert old["ticket_outcome_at"].startswith("2026-10-16T13:30:00")
    finally:
        await client.indices.delete(index=index, ignore_unavailable=True)
        await client.close()
