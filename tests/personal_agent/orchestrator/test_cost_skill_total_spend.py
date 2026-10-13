"""Guard tests for FRE-979: total-spend recipes must use api_cost_recorded.

FRE-970 pointed the ES telemetry skills' cost/budget queries at
`model_call_completed`, but FRE-974 began routing OVH-embedding and
Voyage-reranker spend through the parallel `api_cost_recorded` ledger
instead (that event now carries every priced provider: anthropic, openai,
ovh, voyage). A total-spend recipe that filters on `model_call_completed`
alone silently understates spend by the embedding/rerank amount. These
tests pin the skill docs so that regression can't happen quietly.
"""

from __future__ import annotations

import re

import pytest

from personal_agent.orchestrator.skills import get_all_skills

_EVENT_TYPE_FILTER_PATTERNS = (
    re.compile(r'"event_type":\s*"([^"]+)"'),  # JSON DSL: "term": {"event_type": "X"}
    re.compile(r'event_type\s*==\s*"([^"]+)"'),  # ES|QL: event_type == "X" (post de-escape)
)


def _extract_block(body: str, start_marker: str, end_marker: str) -> str:
    """Return the text of ``body`` from ``start_marker`` up to ``end_marker``."""
    start = body.index(start_marker)
    end = body.index(end_marker, start + len(start_marker))
    return body[start:end]


def _event_type_filters(text: str) -> list[str]:
    r"""Return every ``event_type`` value a recipe in ``text`` actually filters on.

    Normalizes ES|QL's JSON-escaped quotes (``\"X\"``) to plain quotes first so
    both the JSON DSL (``_search``) and ES|QL (``_query``) recipe forms match with
    the same patterns. Deliberately narrower than a raw substring check: prose
    explaining *why* a recipe avoids ``model_call_completed`` legitimately names
    it, so only the actual filter value counts as a regression signal.
    """
    normalized = text.replace('\\"', '"')
    values: list[str] = []
    for pattern in _EVENT_TYPE_FILTER_PATTERNS:
        values.extend(pattern.findall(normalized))
    return values


class TestTotalSpendUsesApiCostRecorded:
    """Total-spend recipes must filter on api_cost_recorded, not model_call_completed alone."""

    def test_self_telemetry_daily_trend_uses_api_cost_recorded(self) -> None:
        """spend-and-tokens Pattern 3a (daily spend trend) filters on api_cost_recorded."""
        body = get_all_skills()["spend-and-tokens"].body
        block = _extract_block(body, "## Pattern 3a — Daily spend trend", "## Pattern 3b")
        filters = _event_type_filters(block)
        assert "api_cost_recorded" in filters
        assert "model_call_completed" not in filters

    def test_self_telemetry_total_by_provider_uses_api_cost_recorded(self) -> None:
        """spend-and-tokens Pattern 3c (total spend by provider) filters on api_cost_recorded."""
        body = get_all_skills()["spend-and-tokens"].body
        block = _extract_block(body, "## Pattern 3c — Total spend by provider", "## Pattern 3d")
        filters = _event_type_filters(block)
        assert "api_cost_recorded" in filters
        assert "model_call_completed" not in filters

    def test_query_elasticsearch_sends_total_spend_to_the_ledger_skill(self) -> None:
        """log-search names the spend skill and the ledger event (FRE-1578).

        It held two spend recipes (the daily trend and the total by provider). They are
        Patterns 3a and 3c of spend-and-tokens now, pinned by the tests above.
        """
        body = get_all_skills()["log-search"].body
        assert "spend-and-tokens" in body
        assert "api_cost_recorded" in body
        assert "total-spend" in body
        assert "model_call_completed" not in _event_type_filters(body)


class TestManagedInferenceEventsDocumented:
    """embedding_generated / reranker_applied must be documented for cost breakdowns."""

    @pytest.mark.parametrize("skill_name", ["spend-and-tokens", "log-search"])
    def test_embedding_and_reranker_events_present(self, skill_name: str) -> None:
        """Both managed-inference cost events are named in the skill body."""
        body = get_all_skills()[skill_name].body
        assert "embedding_generated" in body
        assert "reranker_applied" in body


class TestModelCallCompletedDocumentedAsLlmOnly:
    """model_call_completed must be explicitly scoped to LLM completions.

    It's the event a naive total-spend query reaches for first, so the docs
    must say plainly that it excludes embedding/rerank cost.
    """

    @pytest.mark.parametrize("skill_name", ["spend-and-tokens", "log-search"])
    def test_llm_only_caveat_present(self, skill_name: str) -> None:
        """The skill body explicitly scopes model_call_completed to LLM completions."""
        body = get_all_skills()[skill_name].body
        assert "LLM-only" in body or "LLM-completions only" in body
