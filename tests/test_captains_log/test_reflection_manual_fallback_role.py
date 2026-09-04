"""FRE-1037: the manual-JSON reflection fallback must route through the factory.

The now-deleted local-only dispatch class (no auth headers) could not honor
captains_log's configured model when it resolved to a cloud deployment
(claude_sonnet) — the fallback must construct its client via
get_llm_client_for_key using the already-resolved captains_log role key, and
label the call role=ModelRole.CAPTAINS_LOG rather than the old
role=ModelRole.PRIMARY mislabel.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.captains_log import reflection
from personal_agent.llm_client.types import ModelRole


@pytest.mark.asyncio
async def test_manual_fallback_routes_via_factory_with_captains_log_role() -> None:
    """DSPy unavailable -> manual fallback resolves+labels the captains_log role."""
    with (
        patch.object(reflection, "DSPY_AVAILABLE", False),
        patch(
            "personal_agent.captains_log.reflection._fetch_trace_events",
            AsyncMock(return_value=[]),
        ),
        patch(
            "personal_agent.captains_log.reflection.load_mean_rating_lookup",
            AsyncMock(return_value={}),
        ),
        patch(
            "personal_agent.config.resolve_role_model_key",
            return_value="claude_sonnet",
        ) as mock_resolve,
        patch("personal_agent.captains_log.reflection.get_llm_client_for_key") as mock_get_client,
    ):
        mock_get_client.return_value.respond = AsyncMock(
            return_value={"content": '{"rationale": "r"}'}
        )

        entry = await reflection.generate_reflection_entry(
            user_message="hi",
            trace_id="trace-test",
            steps_count=1,
            final_state="COMPLETED",
            reply_length=5,
        )

        mock_resolve.assert_any_call("captains_log")
        mock_get_client.assert_called_once_with("claude_sonnet", budget_role="captains_log")
        _, respond_kwargs = mock_get_client.return_value.respond.call_args
        assert respond_kwargs["role"] is ModelRole.CAPTAINS_LOG
        assert entry.rationale == "r"
