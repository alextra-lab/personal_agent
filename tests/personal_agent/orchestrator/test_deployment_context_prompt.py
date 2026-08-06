"""FRE-1165: the production deployment-context block must not claim dead services.

The ``embeddings:8503`` and ``reranker:8504`` containers have been exited for
days — the live embedding/reranking path is the managed substrate profile,
reached over Caddy egress to an external endpoint (ADR-0112, ADR-0132), not a
Docker-internal DNS name. Naming them as DNS-reachable gives the model a false
belief about its own environment (this block is only injected in production —
``executor.py``'s ``settings.environment == Environment.PRODUCTION`` gate).

The four other entries (postgres, neo4j bolt+HTTP, elasticsearch, redis) are
still accurate — confirmed against ``docker-compose.cloud.yml``, where each
service is reachable by its Compose service name on the shared network — so
this pins that they remain, not just that the dead pair is gone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore the executor's lazily-cached registry globals after each test.

    Mirrors the fixture in ``test_skill_injection.py``: driving ``step_llm_call``
    with a patched ``get_default_registry`` seeds module-level globals that would
    otherwise leak the small test registry into later tests in the process.
    """
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    saved_layer = _ex._tool_execution_layer
    yield
    _ex._tool_registry = saved_registry
    _ex._tool_execution_layer = saved_layer


def _make_ctx() -> object:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    return ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="hello",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "hello"}],
    )


def _mock_llm() -> MagicMock:
    client = MagicMock()
    client.respond = AsyncMock(
        return_value={
            "content": "hi",
            "tool_calls": [],
            "response_id": None,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    client.model_configs = {}
    return client


async def _dispatched_system_prompt(monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive the real step_llm_call under Environment.PRODUCTION.

    Returns the system_prompt actually sent to the LLM client.
    """
    from personal_agent.config import settings
    from personal_agent.config.env_loader import Environment
    from personal_agent.telemetry.trace import TraceContext

    monkeypatch.setattr(settings, "environment", Environment.PRODUCTION)
    monkeypatch.setattr(settings, "prefer_primitives_enabled", False)

    ctx = _make_ctx()
    client = _mock_llm()
    session = MagicMock()
    session.add_message = AsyncMock()
    session.get_messages = AsyncMock(return_value=[])

    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=[])),
        ),
    ):
        from personal_agent.orchestrator.executor import step_llm_call

        await step_llm_call(ctx, session, TraceContext.new_trace())  # type: ignore[arg-type]

    return client.respond.call_args.kwargs["system_prompt"]


class TestDeploymentContextDeadServices:
    """AC: the production system prompt names no service that isn't reachable."""

    @pytest.mark.asyncio
    async def test_does_not_claim_dead_embedder_reranker_are_docker_dns_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exited embeddings/reranker containers must not appear as DNS-reachable."""
        system_prompt = await _dispatched_system_prompt(monkeypatch)

        assert "embeddings:8503" not in system_prompt
        assert "reranker:8504" not in system_prompt

    @pytest.mark.asyncio
    async def test_still_names_the_live_docker_dns_services(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix must not collaterally drop the four services still reachable."""
        system_prompt = await _dispatched_system_prompt(monkeypatch)

        assert "postgres:5432" in system_prompt
        assert "neo4j:7687" in system_prompt
        assert "neo4j:7474" in system_prompt
        assert "elasticsearch:9200" in system_prompt
        assert "redis:6379" in system_prompt
