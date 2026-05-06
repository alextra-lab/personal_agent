"""Tests for Phase C: separate routing call (route_skills).

The routing model is independent of the primary agent: when
skill_routing_mode=model_decided AND skill_routing_model_key is set,
the executor issues a single call to that model BEFORE the primary turn,
asking which skills are relevant. Returned skill bodies are pre-loaded
into the primary's system prompt.

This test file validates:
- route_skills() parses well-formed JSON arrays
- route_skills() drops names not in the loaded skill set
- route_skills() returns [] on parse failure or empty cache
- route_skills() returns [] when LLM client raises
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from personal_agent.orchestrator.skills import get_all_skills, route_skills


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_routing_client(content: str) -> MagicMock:
    """Build a mock LLM client whose respond() returns the given content string."""
    client = MagicMock()
    client.respond = AsyncMock(return_value={"content": content})
    return client


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRouteSkillsHappyPath:
    """route_skills returns valid skill names from a well-formed JSON response."""

    @pytest.mark.asyncio
    async def test_returns_valid_skill_names(self) -> None:
        """A JSON array of real skill names is returned verbatim."""
        skills = list(get_all_skills().keys())
        assert "query-elasticsearch" in skills
        assert "bash" in skills

        client = _mock_routing_client('["query-elasticsearch", "bash"]')
        result = await route_skills(
            user_message="check the logs",
            routing_client=client,
        )
        assert result == ["query-elasticsearch", "bash"]

    @pytest.mark.asyncio
    async def test_drops_unknown_skill_names(self) -> None:
        """Names not in the registered skill set are silently dropped."""
        client = _mock_routing_client('["query-elasticsearch", "nonexistent"]')
        result = await route_skills(
            user_message="show me telemetry",
            routing_client=client,
        )
        assert result == ["query-elasticsearch"]

    @pytest.mark.asyncio
    async def test_empty_array_returns_empty_list(self) -> None:
        """Router judging no skills relevant returns []."""
        client = _mock_routing_client("[]")
        result = await route_skills(
            user_message="hello",
            routing_client=client,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_handles_code_fenced_json(self) -> None:
        """Router output wrapped in ```json ... ``` is still parsed."""
        client = _mock_routing_client('```json\n["bash"]\n```')
        result = await route_skills(
            user_message="run a command",
            routing_client=client,
        )
        assert result == ["bash"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRouteSkillsRobustness:
    """route_skills returns [] for malformed responses or client failures."""

    @pytest.mark.asyncio
    async def test_parse_failure_returns_empty(self) -> None:
        """Invalid JSON returns [] without raising."""
        client = _mock_routing_client("not json at all")
        result = await route_skills(
            user_message="anything",
            routing_client=client,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_non_array_response_returns_empty(self) -> None:
        """A JSON object (not array) returns []."""
        client = _mock_routing_client('{"skills": ["bash"]}')
        result = await route_skills(
            user_message="anything",
            routing_client=client,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty(self) -> None:
        """Empty content returns []."""
        client = _mock_routing_client("")
        result = await route_skills(
            user_message="anything",
            routing_client=client,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_client_exception_returns_empty(self) -> None:
        """If the LLM client raises, route_skills returns [] (does not crash)."""
        client = MagicMock()
        client.respond = AsyncMock(side_effect=RuntimeError("network error"))
        result = await route_skills(
            user_message="anything",
            routing_client=client,
        )
        assert result == []


# ---------------------------------------------------------------------------
# Settings + factory wiring
# ---------------------------------------------------------------------------


class TestSkillRoutingSettings:
    """The skill_routing_model_key setting is wired through models.yaml."""

    def test_default_is_claude_haiku(self) -> None:
        """Default routing model key is a remote model (claude_haiku)."""
        from personal_agent.config import settings

        assert settings.skill_routing_model_key == "claude_haiku"

    def test_factory_for_key_rejects_unknown(self) -> None:
        """get_llm_client_for_key raises ValueError for unknown keys."""
        from personal_agent.llm_client.factory import get_llm_client_for_key

        with pytest.raises(ValueError, match="Unknown model key"):
            get_llm_client_for_key("nonexistent_xyz_model")
