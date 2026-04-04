"""Tests for proactive memory scoring and budget (FRE-174–175)."""

# ruff: noqa: D103

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import personal_agent.memory.proactive as proactive_mod
from personal_agent.memory.proactive import build_proactive_suggestions, estimate_tokens_from_text
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter


def _row(
    *,
    name: str = "Neo4j",
    vector_score: float = 0.9,
    turn_id: str | None = "t1",
    key_entities: list[str] | None = None,
    timestamp_iso: str | None = "2026-04-01T12:00:00+00:00",
) -> dict:
    return {
        "name": name,
        "entity_type": "Technology",
        "description": "Graph db",
        "vector_score": vector_score,
        "turn_id": turn_id,
        "session_id": "other",
        "timestamp_iso": timestamp_iso,
        "user_message": "hello neo4j",
        "summary": "sum",
        "key_entities": key_entities or [name],
        "mention_count": 0,
    }


@pytest.fixture
def loose_proactive_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relax thresholds so small fixture rows survive scoring."""
    s = proactive_mod.settings
    monkeypatch.setattr(s, "proactive_memory_min_score", 0.0)
    monkeypatch.setattr(s, "proactive_memory_diminishing_score_floor", 0.0)
    monkeypatch.setattr(s, "proactive_memory_diminishing_score_gap", 1.0)
    monkeypatch.setattr(s, "proactive_memory_max_tokens", 10_000)
    monkeypatch.setattr(s, "proactive_memory_max_candidates", 20)
    monkeypatch.setattr(s, "proactive_memory_max_injected_items", 20)


def test_estimate_tokens_from_text() -> None:
    assert estimate_tokens_from_text("one two three") == int(3 * 1.3)


def test_build_empty_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_min_score", 0.3)
    out = build_proactive_suggestions([], set(), None, "tr", None)
    assert out.candidates == []


@pytest.mark.asyncio
async def test_failure_fallback_empty_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_service = MagicMock()
    mock_service.fetch_session_discussed_entity_names = AsyncMock(return_value=[])
    mock_service.suggest_proactive_raw = AsyncMock(side_effect=RuntimeError("neo4j"))

    async def fake_embed(*_a: object, **_k: object) -> list[float]:
        return [0.1, 0.2]

    monkeypatch.setattr(
        "personal_agent.memory.protocol_adapter.generate_embedding",
        fake_embed,
    )
    adapter = MemoryServiceAdapter(service=mock_service)

    result = await adapter.suggest_relevant(
        user_message="hi",
        session_entity_names=[],
        session_topic_hint=None,
        current_session_id="s1",
        trace_id="t1",
    )
    assert result.candidates == []


@pytest.mark.asyncio
async def test_adapter_zero_embedding_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_service = MagicMock()

    async def zero_embed(*_a: object, **_k: object) -> list[float]:
        return [0.0, 0.0]

    monkeypatch.setattr(
        "personal_agent.memory.protocol_adapter.generate_embedding",
        zero_embed,
    )
    adapter = MemoryServiceAdapter(service=mock_service)
    result = await adapter.suggest_relevant(
        user_message="hi",
        session_entity_names=[],
        session_topic_hint=None,
        current_session_id="s1",
        trace_id="t1",
    )
    assert result.candidates == []
    mock_service.suggest_proactive_raw.assert_not_called()


def test_score_combination_non_empty(loose_proactive_settings: None) -> None:
    raw = [_row(vector_score=0.8)]
    out = build_proactive_suggestions(
        raw,
        {"Neo4j"},
        "neo4j graph",
        "trace",
        12.5,
    )
    assert len(out.candidates) == 1
    assert out.candidates[0].relevance_score > 0.5
    assert out.query_embedding_ms == 12.5


def test_min_score_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_min_score", 0.99)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 10_000)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_candidates", 20)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 20)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_floor", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_gap", 1.0)

    raw = [_row(vector_score=0.5)]
    out = build_proactive_suggestions(raw, set(), None, "tr", None)
    assert out.candidates == []


def test_token_budget_trims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_min_score", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 50)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_candidates", 10)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 10)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_floor", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_gap", 1.0)
    monkeypatch.setattr(proactive_mod, "_estimate_payload_tokens", lambda _p: 35)

    raw = [
        _row(name="A", vector_score=0.95, turn_id="a"),
        _row(name="B", vector_score=0.9, turn_id="b"),
    ]
    out = build_proactive_suggestions(raw, set(), None, "tr", None)
    assert len(out.candidates) <= 1


def test_diminishing_injected_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_min_score", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 100_000)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_candidates", 20)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 2)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_floor", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_gap", 1.0)

    raw = [_row(name="E1", vector_score=1.0 - i * 0.01, turn_id=f"t{i}") for i in range(6)]
    out = build_proactive_suggestions(raw, set(), None, "tr", None)
    assert len(out.candidates) == 2


def test_diminishing_score_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_min_score", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 100_000)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_candidates", 10)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 10)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_floor", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_gap", 0.05)

    # Artificially force final scores via weights: only embedding matters
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_w_embedding", 1.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_w_entity", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_w_recency", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_w_topic", 0.0)

    raw = [
        _row(name="H1", vector_score=0.9, turn_id="x1"),
        _row(name="H2", vector_score=0.7, turn_id="x2"),
    ]
    out = build_proactive_suggestions(raw, set(), None, "tr", None)
    assert len(out.candidates) == 1


def test_dedupe_same_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_min_score", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_tokens", 100_000)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_candidates", 10)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_max_injected_items", 10)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_floor", 0.0)
    monkeypatch.setattr(proactive_mod.settings, "proactive_memory_diminishing_score_gap", 1.0)

    r1 = _row(name="A", vector_score=0.9, turn_id="same")
    r2 = _row(name="B", vector_score=0.85, turn_id="same")
    out = build_proactive_suggestions([r1, r2], set(), None, "tr", None)
    assert len(out.candidates) == 1
