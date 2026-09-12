"""FRE-958/ADR-0145 D1 (FRE-1445) regression guard.

The ``sub_agent`` role binds ``inherit``: it resolves to whatever the
``primary`` binding resolves to (today, ``qwen3.8-flash-next``), never to a
hardcoded literal. Before FRE-1443/FRE-1445 that meant a *different* catalog
key from primary's (a dedicated local instruct twin, or a background model);
under `inherit` the two roles now DELIBERATELY resolve to the same catalog
key. The FRE-958 bug this file guards against — the sub-agent dispatch client
silently falling back to a PRIMARY-role client — is still real, but the
literal-key comparison that used to catch it (``sub_key != primary_key``) no
longer discriminates, because equal keys are now the *correct* outcome. What
must still differ is the **effective resolved definition**: sub_agent's own
``mode: worker`` override must reach the client, not primary's own mode.

**Placement no longer discriminates either, for the same reason it didn't
before FRE-1319's one-day split reversed** — both roles are local again
(``qwen3.8-flash-next``), so a fallback to primary's binding would build the
same local-placement client a correct resolution does. The client-class
assertion below stays corroborating.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from personal_agent.config.model_loader import resolve_role_target
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import Placement
from personal_agent.llm_client.types import ModelRole
from personal_agent.security import DomainGuard
from personal_agent.telemetry.trace import SystemTraceContext


class TestSubAgentResolution:
    """sub_agent resolves through its own `inherit` binding, at its own mode."""

    def test_role_resolves_to_primarys_deployment_at_its_own_mode(self) -> None:
        """`inherit` resolves sub_agent's key to primary's; the mode override is its own."""
        sub_key, sub_def = resolve_role_target("sub_agent")
        primary_key, _ = resolve_role_target("primary")

        assert sub_key == primary_key == "qwen3.8-flash-next"
        assert sub_def is not None
        assert sub_def.default_mode == "worker"

    def test_builds_local_client_matching_its_deployment_placement(self) -> None:
        """sub_agent dispatches at local placement — qwen3.8-flash-next's.

        Corroborating only, same as before FRE-1445: both roles are
        ``slm_local``, so a fallback to primary's binding would satisfy this
        assertion too. Kept because it still catches a client built for the
        wrong *placement* (a cloud client for a local deployment).
        """
        client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)

        assert isinstance(client, LiteLLMClient)
        assert client.placement is Placement.LOCAL

    def test_sub_agent_mode_differs_from_primarys_even_though_the_key_is_shared(self) -> None:
        """The FRE-958 bug's successor assertion, stated for the `inherit` shape.

        Equal resolved KEYS are correct under D1 (both `qwen3.8-flash-next`),
        so that comparison can no longer catch a sub-agent dispatch silently
        built from PRIMARY's binding instead of its own. What must still hold
        is that sub_agent's binding-level `mode: worker` override reaches the
        resolved definition, distinctly from primary's own `default` mode —
        a fallback to primary's binding would collapse this to `default` too.
        """
        _, sub_def = resolve_role_target("sub_agent")
        _, primary_def = resolve_role_target("primary")

        sub_mode = sub_def.resolve_mode()
        primary_mode = primary_def.resolve_mode()

        assert sub_mode.enable_thinking is False
        assert primary_mode.enable_thinking is True
        assert sub_mode != primary_mode


def _permissive_guard() -> DomainGuard:
    """A DomainGuard that refuses nothing — never touches network or disk."""
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


def _stream_chunk(content: str = "ok") -> Any:
    class _Chunk:
        def model_dump(self) -> dict[str, Any]:
            return {
                "id": "chunk-1",
                "choices": [{"delta": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    return _Chunk()


async def _fake_stream() -> Any:
    yield _stream_chunk()


@pytest.mark.asyncio
class TestSubAgentWorkerPresetReachesTheWire:
    """AC-4 — the wire-dispatched call, not the resolved definition, is the proof.

    ADR-0145's own risk table names this failure mode directly: "the worker's
    mode is missing on a newly added model, and the sub-agent silently runs at
    the model's default (thinking on, billed)". Reading ``resolve_mode()`` back
    (as ``TestSubAgentResolution`` above does) cannot rule that out on its own —
    the client still has to actually build the request from it. This asserts
    the real ``qwen3.8-flash-next`` deployment's ``worker`` mode against the
    kwargs ``litellm.acompletion`` receives when a real ``sub_agent`` client
    dispatches, mirroring the deleted ``qwen3.8-flash-next-instruct`` entry's
    preset exactly.
    """

    async def test_worker_thinking_and_sampler_preset_dispatched_on_the_real_catalog(
        self,
    ) -> None:
        _, model_def = resolve_role_target("sub_agent")
        assert model_def is not None
        assert model_def.provider is not None

        client = LiteLLMClient(
            model_id=model_def.id,
            model_key="qwen3.8-flash-next",
            provider=model_def.provider,
            max_tokens=model_def.max_tokens,
            budget_role="sub_agent",
            placement=Placement.LOCAL,
            model_def=model_def,
            egress_guard=_permissive_guard(),
        )

        acompletion = AsyncMock(side_effect=lambda **_: _fake_stream())
        with patch("litellm.acompletion", acompletion):
            await client.respond(
                role=ModelRole.SUB_AGENT,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=SystemTraceContext.new(
                    "test", session_id="00000000-0000-0000-0000-000000000002"
                ),
            )
        kwargs = dict(acompletion.call_args.kwargs)

        assert kwargs["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert kwargs["temperature"] == 0.7
        assert kwargs["top_p"] == 0.8
        assert kwargs["presence_penalty"] == 1.5
        assert kwargs["extra_body"]["top_k"] == 20
        assert kwargs["extra_body"]["min_p"] == 0.0
        assert kwargs["extra_body"]["repetition_penalty"] == 1.0
        assert kwargs["max_tokens"] == 8192
