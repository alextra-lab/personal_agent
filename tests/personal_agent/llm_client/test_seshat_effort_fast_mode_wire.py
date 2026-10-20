"""FRE-1622 (ADR-0157 D5): each ``fast`` mode puts its dialect's reasoning field on the wire (AC-4).

* ``chat_template_kwargs.enable_thinking: false`` on the five ``llamacpp_qwen`` entries;
* ``reasoning_effort: "none"`` on ``qwen3.8-27b-ovh``;
* ``effort: "low"`` on ``claude_sonnet`` (litellm's input name is ``reasoning_effort``, which it
  maps to Anthropic's adaptive ``effort``, as the ``worker`` mode test asserts).

The catalog load refuses a ``fast`` mode with a field outside its dialect.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from personal_agent.config.model_loader import CATALOG_PATH, load_model_config
from personal_agent.config.seshat_effort import (
    effort_spec,
    primary_mode_for,
    reset_current_effort,
    set_current_effort,
)
from personal_agent.llm_client.models import ModelConfig
from personal_agent.llm_client.types import ModelRole
from tests._helpers.trace import make_test_ctx
from tests.personal_agent.llm_client.test_worker_mode_dispatch_per_dialect import (
    _apply,
    _dispatch_stack,
    _pin_capabilities,  # noqa: F401 - autouse fixture
    _reset_selection,  # noqa: F401 - autouse fixture
)

_LOCAL = (
    "qwen3.6-35b-thinking",
    "qwen3.8-flash-next",
    "qwen3.8-27b-mtplx",
    "qwen3.8-flash-next-mtplx",
    "gemma-4-26b-a4b",
)


@pytest.fixture(autouse=True)
def _fast() -> Iterator[None]:
    token = set_current_effort(effort_spec("fast"))
    try:
        yield
    finally:
        reset_current_effort(token)


async def _dispatch_primary_as(key: str) -> tuple[dict[str, Any], str | None]:
    from personal_agent.llm_client.factory import get_llm_client
    from personal_agent.llm_client.models import Placement

    client = get_llm_client(role_name="primary", selection_key=key, mode=primary_mode_for(key))
    captured: dict[str, Any] = {}
    with _apply(_dispatch_stack(captured, streaming=client.placement is Placement.LOCAL)):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=make_test_ctx("fre1622"),
        )
    return captured, client.model_def.default_mode


class TestTheFastModeReachesTheWire:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", _LOCAL)
    async def test_llamacpp_qwen_turns_thinking_off(self, key: str) -> None:
        captured, mode = await _dispatch_primary_as(key)
        assert mode == "fast"
        assert captured["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}

    @pytest.mark.asyncio
    async def test_ovh_qwen_sends_reasoning_effort_none(self) -> None:
        captured, mode = await _dispatch_primary_as("qwen3.8-27b-ovh")
        assert mode == "fast"
        assert captured["reasoning_effort"] == "none"
        # `default` declares temperature 1.0; `fast` declares none.
        assert "temperature" not in captured

    @pytest.mark.asyncio
    async def test_anthropic_adaptive_sends_low_effort(self) -> None:
        captured, mode = await _dispatch_primary_as("claude_sonnet")
        assert mode == "fast"
        assert captured["reasoning_effort"] == "low"


class TestTheCatalog:
    @pytest.mark.parametrize("key", _LOCAL)
    def test_a_local_fast_mode_is_a_copy_of_the_worker_sampling(self, key: str) -> None:
        modes = load_model_config().models[key].modes
        assert modes["fast"] == modes["worker"]
        assert modes["fast"].enable_thinking is False

    def test_the_unchanged_catalog_validates_by_the_same_route(self) -> None:
        """The control for the refusals below: they fail for the field, not the route."""
        ModelConfig.model_validate(yaml.safe_load(CATALOG_PATH.read_text()))

    @pytest.mark.parametrize(
        ("key", "fast"),
        [
            ("qwen3.8-flash-next", {"reasoning_effort": "none"}),
            ("qwen3.8-27b-ovh", {"effort": "low"}),
            ("qwen3.8-27b-ovh", {"reasoning_effort": "high"}),
            ("claude_sonnet", {"enable_thinking": False}),
        ],
    )
    def test_a_fast_mode_outside_its_dialect_is_refused(
        self, key: str, fast: dict[str, object]
    ) -> None:
        raw = yaml.safe_load(CATALOG_PATH.read_text())
        raw["models"][key]["modes"]["fast"] = fast
        with pytest.raises(ValueError, match="fast"):
            ModelConfig.model_validate(raw)


class TestModelCallCompletedNamesTheMode:
    """ADR-0157 D8: ``model_call_completed.extra.mode`` on the local and the cloud branch."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["qwen3.8-flash-next", "claude_sonnet"])
    async def test_the_event_carries_the_mode_of_the_call(self, key: str) -> None:
        import structlog.testing

        with structlog.testing.capture_logs() as logs:
            _, mode = await _dispatch_primary_as(key)

        (event,) = [e for e in logs if e.get("event") == "model_call_completed"]
        assert mode == "fast"
        assert event["mode"] == "fast"
