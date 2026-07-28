"""Mocked-driver unit tests for the ADR-0124 Phase 1 session-digest read path (FRE-948).

Covers:

* The batched read never round-trips per session — one query for a whole page.
* Graceful degradation: not connected, the query itself failing, and a row-fetch
  failure inside the ``async with`` block all return ``{}`` without raising.
* A malformed stored digest never suppresses a valid label (they are written and
  parsed independently) — the confirmed fix from this ticket's codex plan review.
* Malformed rows (bad ``session_id``, whitespace-only label) degrade per-row
  rather than dropping the whole batch.
* ``ensure_session_id_index()`` emits the expected idempotent Cypher.

Sibling to ``test_session_digest_write.py`` (which is scoped to the write path per
its own docstring) rather than an addition to it.
"""

# ruff: noqa: D103

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest

from personal_agent.memory.service import MemoryService
from personal_agent.memory.session_digest import (
    DigestItem,
    SessionDigest,
    parse_stored_digest,
    render_digest,
)

_VALID_DIGEST = SessionDigest(
    established=[
        DigestItem(text="Uses Neo4j for the knowledge graph.", basis="assistant_reasoning")
    ],
)
_VALID_DIGEST_JSON = orjson.dumps(_VALID_DIGEST.model_dump(mode="json")).decode()


def _make_service_with_mock(
    *,
    data_return: list[dict[str, object]] | None = None,
    data_side_effect: Exception | None = None,
    session_side_effect: Exception | None = None,
) -> tuple[MemoryService, list[tuple[str, dict[str, object]]]]:
    """Build a MemoryService whose driver captures every Cypher statement.

    Args:
        data_return: What ``result.data()`` resolves to.
        data_side_effect: When given, ``result.data()`` raises this instead.
        session_side_effect: When given, ``driver.session()`` itself raises this
            (models a failure acquiring the driver session, before any query runs).

    Returns:
        The service and the list of ``(cypher, params)`` pairs it ran.
    """
    service = MemoryService.__new__(MemoryService)
    service.connected = True

    captured: list[tuple[str, dict[str, object]]] = []
    result = AsyncMock()
    if data_side_effect is not None:
        result.data = AsyncMock(side_effect=data_side_effect)
    else:
        result.data = AsyncMock(return_value=data_return or [])

    async def capture_run(cypher: str, **kwargs: object) -> AsyncMock:
        captured.append((cypher, dict(kwargs)))
        return result

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=capture_run)

    service.driver = MagicMock()
    if session_side_effect is not None:
        service.driver.session = MagicMock(side_effect=session_side_effect)
    else:
        service.driver.session = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_session),
                __aexit__=AsyncMock(return_value=None),
            )
        )
    return service, captured


# --------------------------------------------------------------------------
# Empty input / not connected
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_digest_views_empty_ids_returns_empty_without_a_query() -> None:
    service, captured = _make_service_with_mock()

    result = await service.get_session_digest_views([])

    assert result == {}
    service.driver.session.assert_not_called()
    assert captured == []


@pytest.mark.asyncio
async def test_get_session_digest_views_not_connected_returns_empty() -> None:
    service, _ = _make_service_with_mock()
    service.connected = False

    result = await service.get_session_digest_views(["sess-1"])

    assert result == {}


# --------------------------------------------------------------------------
# Success paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_digest_views_parses_label_and_renders_digest() -> None:
    service, captured = _make_service_with_mock(
        data_return=[
            {
                "session_id": "sess-1",
                "session_label": "Neo4j knowledge graph setup",
                "session_digest": _VALID_DIGEST_JSON,
            }
        ]
    )

    result = await service.get_session_digest_views(["sess-1"])

    assert result["sess-1"].label == "Neo4j knowledge graph setup"
    assert result["sess-1"].digest_text == render_digest(_VALID_DIGEST)
    # $session_ids is parameter-bound, not interpolated.
    _cypher, params = captured[0]
    assert params["session_ids"] == ["sess-1"]


@pytest.mark.asyncio
async def test_get_session_digest_views_label_only_row() -> None:
    service, _ = _make_service_with_mock(
        data_return=[{"session_id": "sess-1", "session_label": "A label", "session_digest": None}]
    )

    result = await service.get_session_digest_views(["sess-1"])

    assert result["sess-1"].label == "A label"
    assert result["sess-1"].digest_text is None


@pytest.mark.asyncio
async def test_get_session_digest_views_digest_only_row() -> None:
    service, _ = _make_service_with_mock(
        data_return=[
            {"session_id": "sess-1", "session_label": None, "session_digest": _VALID_DIGEST_JSON}
        ]
    )

    result = await service.get_session_digest_views(["sess-1"])

    assert result["sess-1"].label is None
    assert result["sess-1"].digest_text == render_digest(_VALID_DIGEST)


@pytest.mark.asyncio
async def test_get_session_digest_views_no_digest_yet_is_absent_from_the_mapping() -> None:
    service, _ = _make_service_with_mock(
        data_return=[{"session_id": "sess-1", "session_label": None, "session_digest": None}]
    )

    result = await service.get_session_digest_views(["sess-1"])

    assert "sess-1" not in result
    assert result == {}


@pytest.mark.asyncio
async def test_get_session_digest_views_whitespace_only_label_is_treated_as_absent() -> None:
    service, _ = _make_service_with_mock(
        data_return=[{"session_id": "sess-1", "session_label": "   ", "session_digest": None}]
    )

    result = await service.get_session_digest_views(["sess-1"])

    assert "sess-1" not in result


# --------------------------------------------------------------------------
# Malformed rows — the confirmed high-severity fix
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_digest_views_malformed_digest_preserves_the_valid_label() -> None:
    """A malformed stored digest must never suppress an independently valid label.

    Confirmed finding from this ticket's codex plan review: label and digest are
    written as two separate Neo4j properties and must be parsed independently.
    """
    service, _ = _make_service_with_mock(
        data_return=[
            {
                "session_id": "sess-1",
                "session_label": "A perfectly good label",
                "session_digest": "{not valid json",
            }
        ]
    )

    result = await service.get_session_digest_views(["sess-1"])

    assert result["sess-1"].label == "A perfectly good label"
    assert result["sess-1"].digest_text is None


@pytest.mark.asyncio
async def test_get_session_digest_views_malformed_digest_alongside_a_good_row() -> None:
    service, _ = _make_service_with_mock(
        data_return=[
            {"session_id": "sess-bad", "session_label": "Has a label", "session_digest": "{bad"},
            {
                "session_id": "sess-good",
                "session_label": "Good label",
                "session_digest": _VALID_DIGEST_JSON,
            },
        ]
    )

    result = await service.get_session_digest_views(["sess-bad", "sess-good"])

    assert result["sess-bad"].label == "Has a label"
    assert result["sess-bad"].digest_text is None
    assert result["sess-good"].digest_text == render_digest(_VALID_DIGEST)


@pytest.mark.asyncio
async def test_get_session_digest_views_malformed_session_id_row_is_skipped() -> None:
    service, _ = _make_service_with_mock(
        data_return=[
            {"session_id": None, "session_label": "Orphaned label", "session_digest": None},
            {
                "session_id": "sess-good",
                "session_label": "Good label",
                "session_digest": None,
            },
        ]
    )

    result = await service.get_session_digest_views(["sess-good"])

    assert list(result.keys()) == ["sess-good"]


# --------------------------------------------------------------------------
# Legacy `tool_evidence` basis (FRE-969) — retired by Amendment B, still on disk
# --------------------------------------------------------------------------


def test_parse_stored_digest_coerces_retired_tool_evidence_basis_to_mixed() -> None:
    """A pre-Amendment-B digest carrying `tool_evidence` parses, coerced to `mixed`.

    The pydantic BasisTag literal narrowed to user_statement/assistant_reasoning/mixed
    (Amendment B) and would otherwise reject the whole stored digest, exactly as
    session_digest_view_parse_failed reported live on FRE-969.
    """
    raw = {
        "established": [{"text": "Uses Neo4j for the knowledge graph.", "basis": "tool_evidence"}],
    }

    digest = parse_stored_digest(raw)

    assert digest.established[0].basis == "mixed"
    assert digest.established[0].text == "Uses Neo4j for the knowledge graph."


def test_parse_stored_digest_coerces_tool_evidence_in_every_slot() -> None:
    raw = {
        "established": [{"text": "e", "basis": "tool_evidence"}],
        "decisions": [{"text": "d", "basis": "tool_evidence"}],
        "unresolved": [{"text": "u", "basis": "tool_evidence", "as_of": "2026-07-01T00:00:00Z"}],
        "corrections": [
            {
                "text": "c",
                "basis": "tool_evidence",
                "tier": "self_correction",
                "span": "cl",
                "locator": {"capture_id": "cap-1", "field": "assistant_text"},
                "evidence_span": "ev",
                "evidence_locator": {"capture_id": "cap-1", "field": "assistant_text"},
            }
        ],
    }

    digest = parse_stored_digest(raw)

    assert digest.established[0].basis == "mixed"
    assert digest.decisions[0].basis == "mixed"
    assert digest.unresolved[0].basis == "mixed"
    assert digest.corrections[0].basis == "mixed"


def test_parse_stored_digest_reads_a_pre_fre1024_correction() -> None:
    """A digest stored while the model still transcribed its own spans must still read.

    The write path changed (spans are now quoted from the locator), but nothing rewrote
    what is already on disk. The stored shape is unchanged — all four provenance fields
    were required before and are required now — so an old record parses as-is, and its
    model-authored span text is preserved rather than reinterpreted.

    Verified against the live graph before tightening the type: the one stored correction
    there carries all four fields.
    """
    raw = {
        "established": [{"text": "The sweep ran hourly.", "basis": "mixed"}],
        "corrections": [
            {
                "text": "The assistant corrected the token ceiling.",
                "basis": "assistant_reasoning",
                "tier": "self_correction",
                "span": "I said 2048",
                "locator": {"capture_id": "cap-1", "field": "assistant_text"},
                "evidence_span": "it is actually 2856",
                "evidence_locator": {"capture_id": "cap-1", "field": "assistant_text"},
            }
        ],
    }

    digest = parse_stored_digest(raw)

    assert digest.corrections[0].span == "I said 2048"
    assert digest.corrections[0].evidence_span == "it is actually 2856"
    # Absent on an old record, so it must default rather than fail the read.
    assert digest.corrections_dropped == 0
    assert "Corrections" in render_digest(digest)


def test_parse_stored_digest_leaves_valid_basis_untouched() -> None:
    raw = {"established": [{"text": "e", "basis": "assistant_reasoning"}]}

    digest = parse_stored_digest(raw)

    assert digest.established[0].basis == "assistant_reasoning"


@pytest.mark.asyncio
async def test_get_session_digest_views_legacy_tool_evidence_basis_renders_instead_of_parse_failing() -> (
    None
):
    """The end-to-end read path: a stored digest with the retired basis still renders.

    Reproduces the two live sessions from FRE-969 — before the fix this row's digest
    would hit the ``except`` branch and log ``session_digest_view_parse_failed``.
    """
    legacy_digest_json = orjson.dumps(
        {"established": [{"text": "Legacy fact.", "basis": "tool_evidence"}]}
    ).decode()
    service, _ = _make_service_with_mock(
        data_return=[
            {
                "session_id": "sess-legacy",
                "session_label": "Old session",
                "session_digest": legacy_digest_json,
            }
        ]
    )

    result = await service.get_session_digest_views(["sess-legacy"])

    assert result["sess-legacy"].digest_text is not None
    assert "Legacy fact." in result["sess-legacy"].digest_text


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_digest_views_query_failure_returns_empty() -> None:
    service, _ = _make_service_with_mock(session_side_effect=RuntimeError("boom"))

    result = await service.get_session_digest_views(["sess-1"])

    assert result == {}


@pytest.mark.asyncio
async def test_get_session_digest_views_row_fetch_failure_returns_empty() -> None:
    service, _ = _make_service_with_mock(data_side_effect=RuntimeError("boom"))

    result = await service.get_session_digest_views(["sess-1"])

    assert result == {}


# --------------------------------------------------------------------------
# Index bootstrap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_session_id_index_creates_idempotent_index() -> None:
    service, captured = _make_service_with_mock()

    assert await service.ensure_session_id_index() is True

    cypher = captured[0][0]
    assert "CREATE INDEX session_id_index IF NOT EXISTS" in cypher
    assert "FOR (s:Session) ON (s.session_id)" in cypher


@pytest.mark.asyncio
async def test_ensure_session_id_index_not_connected_returns_false() -> None:
    service, _ = _make_service_with_mock()
    service.connected = False

    assert await service.ensure_session_id_index() is False
