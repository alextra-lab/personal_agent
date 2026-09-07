"""FRE-1448 / ADR-0145 D6 — a worker runs at its dialect's own cheap mode, proven on the wire.

D6's table (FRE-1430 F13) says which setting is cheap per dialect; FRE-1445's PR already
declared it in ``config/models.yaml`` and proved it structurally
(``tests/personal_agent/llm_client/test_dialect_modes.py::TestEverySelectablePrimaryDeclaresAWorkerMode``
— AC-1). What was still missing is the wire-level proof this file adds: for one primary per
dialect, dispatching ``sub_agent`` (bound ``deployment: inherit, mode: worker``) actually sends
the declared worker value to ``litellm.acompletion`` — not just that ``resolve_mode()`` returns
it. Before this file only the local dialect (``llamacpp_qwen``) had that proof
(``tests/test_llm_client/test_factory_sub_agent.py::TestSubAgentWorkerPresetReachesTheWire``);
the ticket's own AC-2 names "only local+cloud exercised" as a fail condition, because that covers
two of five dialects and can pass while three are wrong.

**AC-3 and AC-4's outcome half are OUT OF SCOPE here, deliberately, not overlooked.** AC-3 asks
what the provider *bills* — reasoning tokens, thinking-block presence, ``reasoning_content``
length — for the SAME calls AC-2 exercises. AC-4 asks for an executed cost comparison on a real
HYBRID turn. Both need a live call to OVH/OpenAI/Anthropic; this repo's own precedent
(``tests/personal_agent/llm_client/test_dialect_parameter_block.py``, module docstring) already
draws this exact line for a sibling ticket: "The one criterion that cannot be met here is AC-2
[of FRE-1440], which asks what the provider bills. That needs a real OVH call and is recorded on
the ticket." A build session does not fire a live cloud-billed turn on its own authority (standing
directive: no live-gateway turn without an explicit owner/master OK) — that live capture, run once
and recorded with both halves together (not a mocked dispatch plus a separately-run outcome check,
which does not satisfy the "same calls" wording), is the FRE-1448 handoff's post-deploy runbook
item for master.

What IS proven here, deterministically:

* AC-2 — the worker mode's declared value(s) reach ``litellm.acompletion``, for all five dialects.
* AC-4's structural half — at one fixed primary selection (``qwen3.8-27b-ovh``), the ``sub_agent``
  dispatch carries the worker value (``none``), never the primary's own ``default`` value
  (``medium``) — the provider-default exposure master flagged on FRE-1445's PR. The cost delta
  this parameter difference produces (823 tokens/$0.0035 vs 4606/$0.0156, FRE-1430 F13) is already
  measured; it is cited, not re-measured, here.

``claude_haiku``'s ``worker`` mode is declared empty (``{}``, matching D6's "thinking disabled" —
already true of its ``default`` too), so kwarg absence cannot distinguish a correct ``worker``
resolution from a silent fallback to ``default``: both dispatch nothing. The discriminator used
below is the resolved mode's own NAME (``client.model_def.default_mode``), read directly off the
client the factory built — not inferred from what did or did not reach the wire.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.llm_client.types import ModelRole
from tests._helpers.litellm_capability import pinned_litellm_capabilities
from tests._helpers.trace import make_test_ctx


@pytest.fixture(autouse=True)
def _pin_capabilities() -> Iterator[None]:
    """Litellm's GitHub-fetched capability map must not decide these results."""
    with pinned_litellm_capabilities():
        yield


@pytest.fixture(autouse=True)
def _reset_selection() -> Iterator[None]:
    """Each test picks its own primary; never leak a selection across tests."""
    token = _current_selection.set({})
    try:
        yield
    finally:
        _current_selection.reset(token)


def _mock_response() -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None
    usage.completion_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_fre1448"
    return response


def _stream_chunk(content: str = "ok") -> Any:
    """One local (llama.cpp) streaming chunk — local dispatch always streams
    (``_respond_local``), unlike the cloud branch's single response object.
    """

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


def _dispatch_stack(captured: dict[str, Any], *, streaming: bool) -> list[Any]:
    """Patches that isolate dispatch from cost, credentials and telemetry (mirrors
    test_dialect_parameter_block.py's ``_dispatch_stack``, kept local per this repo's
    convention of not sharing private test helpers across files).

    Args:
        captured: Dict the fake ``litellm.acompletion`` writes its kwargs into.
        streaming: The local branch (``_respond_local``) always requests a stream,
            unlike the cloud branch — the fake must match, or aggregation raises
            "Empty streaming response" against a non-streaming mock.
    """
    if streaming:

        def _fake_acompletion(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return _fake_stream()

    else:

        async def _fake_acompletion(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _mock_response()

    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="res-fre1448")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()

    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()

    return [
        patch("litellm.acompletion", side_effect=_fake_acompletion),
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=gate),
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock()),
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.01"),
        ),
        patch(
            "personal_agent.llm_client.history_sanitiser.sanitise_messages",
            side_effect=lambda msgs, trace_id: (msgs, []),
        ),
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=tracker,
        ),
        patch(
            "personal_agent.config.settings.get_settings",
            return_value=MagicMock(anthropic_api_key="k", openai_api_key="k", ovh_api_key="k"),
        ),
    ]


class _apply:
    """Enter a list of patches as one context manager."""

    def __init__(self, patches: list[Any]) -> None:
        self._patches = patches

    def __enter__(self) -> None:
        for item in self._patches:
            item.__enter__()

    def __exit__(self, *exc: Any) -> None:
        for item in reversed(self._patches):
            item.__exit__(*exc)


async def _dispatch_sub_agent_as(primary_key: str) -> tuple[dict[str, Any], str | None]:
    """Select ``primary_key`` as the session primary, dispatch sub_agent, capture kwargs.

    Returns ``(captured_litellm_kwargs, resolved_mode_name)``. The mode name is read
    off the client's own effective definition — the discriminator that survives an
    empty mode body (see module docstring on ``claude_haiku``).
    """
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.models import Placement

    set_current_selection({"primary": primary_key})
    client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)
    resolved_mode = client.model_def.default_mode if client.model_def is not None else None

    captured: dict[str, Any] = {}
    with _apply(_dispatch_stack(captured, streaming=client.placement is Placement.LOCAL)):
        await client.respond(
            role=ModelRole.SUB_AGENT,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=make_test_ctx("fre1448"),
        )
    return captured, resolved_mode


class TestAC2WorkerModeReachesTheWirePerDialect:
    """AC-2 — one primary per dialect; the dispatched call carries the declared worker value."""

    @pytest.mark.asyncio
    async def test_llamacpp_qwen_worker_mode_dispatched(self) -> None:
        """Regression pin — FRE-1445 already proved this one; kept here for a complete set."""
        captured, mode = await _dispatch_sub_agent_as("qwen3.8-flash-next")

        assert mode == "worker"
        assert captured["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert captured["temperature"] == 0.7
        assert captured["top_p"] == 0.8
        assert captured["presence_penalty"] == 1.5

    @pytest.mark.asyncio
    async def test_ovh_qwen_worker_mode_dispatched(self) -> None:
        captured, mode = await _dispatch_sub_agent_as("qwen3.8-27b-ovh")

        assert mode == "worker"
        assert captured["reasoning_effort"] == "none"
        assert captured["allowed_openai_params"] == ["reasoning_effort"]
        # `default` declares temperature 1.0; `worker` declares none — must not leak.
        assert "temperature" not in captured

    @pytest.mark.asyncio
    async def test_openai_gpt5_worker_mode_dispatched(self) -> None:
        captured, mode = await _dispatch_sub_agent_as("gpt-5.4-mini")

        assert mode == "worker"
        assert captured["reasoning_effort"] == "none"
        # `default` declares temperature 0.0 (FRE-758's entity-extraction pin); `worker`
        # declares no temperature at all — a generic sub-agent must not inherit it.
        assert "temperature" not in captured

    @pytest.mark.asyncio
    async def test_anthropic_adaptive_worker_mode_dispatched(self) -> None:
        captured, mode = await _dispatch_sub_agent_as("claude_sonnet")

        assert mode == "worker"
        # D6's measured cheapest Sonnet setting — below thinking-off, 10/10 correct.
        assert captured["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_anthropic_budget_worker_mode_dispatched(self) -> None:
        captured, mode = await _dispatch_sub_agent_as("claude_haiku")

        # The resolved mode NAME is the proof here — both `worker` and `default` are
        # empty (D6: "thinking disabled", already true by omission), so kwarg absence
        # alone cannot distinguish a correct resolution from a silent fallback.
        assert mode == "worker"
        assert "reasoning_effort" not in captured
        assert "thinking" not in captured


class TestAC4WorkerDiffersFromDefaultAtTheSamePrimary:
    """AC-4's structural half — a cloud primary's sub-agent does not bill at the provider default.

    Compares the SAME primary selection's two mode resolutions: ``primary``'s own
    ``default`` mode against ``sub_agent``'s inherited ``worker`` mode. This is the
    exposure master flagged on FRE-1445's PR (a cloud primary with no `worker` mode
    would route sub_agent onto that model's `default_mode` instead) — proven closed
    at the dispatch boundary. The cost this parameter difference saves is FRE-1430
    F13's own measured total (823 tokens/$0.0035 at `none` vs 4606/$0.0156 at
    `medium`) — cited, not re-measured, here; re-measuring it live on a real HYBRID
    turn is AC-4's outcome half (see module docstring).
    """

    @pytest.mark.asyncio
    async def test_sub_agent_reasoning_effort_differs_from_primarys(self) -> None:
        from personal_agent.llm_client.factory import get_llm_client

        set_current_selection({"primary": "qwen3.8-27b-ovh"})
        primary_client = get_llm_client(role_name="primary")
        sub_agent_client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)

        primary_captured: dict[str, Any] = {}
        with _apply(_dispatch_stack(primary_captured, streaming=False)):
            await primary_client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("fre1448_primary"),
            )

        sub_captured: dict[str, Any] = {}
        with _apply(_dispatch_stack(sub_captured, streaming=False)):
            await sub_agent_client.respond(
                role=ModelRole.SUB_AGENT,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("fre1448_sub"),
            )

        assert primary_captured["reasoning_effort"] == "medium"
        assert sub_captured["reasoning_effort"] == "none"
