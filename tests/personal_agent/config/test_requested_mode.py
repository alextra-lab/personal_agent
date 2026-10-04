"""FRE-1541 AC-3 / ADR-0154 D4 — a call site can request a named mode, on the deployment that runs.

The planner call asks for mode ``planner``. The request resolves against the deployment that the
role finally lands on, after the session selection, so a selected deployment with no ``planner``
mode keeps its own default mode and never silently borrows another deployment's. Each test reads
the kwargs that ``litellm.acompletion`` receives, never the resolved definition as a proxy.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.llm_client.concurrency import set_inference_concurrency_controller
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.models import (
    Dialect,
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    Placement,
    ProviderDefinition,
    RoleBinding,
)
from personal_agent.llm_client.types import ModelRole
from personal_agent.security import DomainGuard
from personal_agent.telemetry.trace import SystemTraceContext

_WITH_MODE = "has_planner_mode"
_WITHOUT_MODE = "no_planner_mode"

_DEFAULT_SAMPLING = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
}


def _config(*, binding_default: str = _WITH_MODE) -> ModelConfig:
    common: dict[str, Any] = {
        "provider": "slm_local",
        "context_length": 131072,
        "max_concurrency": 1,
        "default_timeout": 600,
        "default_mode": "default",
    }
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                base_url="https://slm.test.example/v1",
                placement=Placement.LOCAL,
                max_concurrency=4,
                dialect=Dialect.LLAMACPP_QWEN,
            )
        },
        models={
            _WITH_MODE: ModelDefinition(
                id="model-with-planner-mode",
                modes={
                    "default": ModeSpec(enable_thinking=True, **_DEFAULT_SAMPLING),
                    "planner": ModeSpec(enable_thinking=False, **_DEFAULT_SAMPLING),
                },
                **common,
            ),
            _WITHOUT_MODE: ModelDefinition(
                id="model-without-planner-mode",
                modes={
                    "default": ModeSpec(
                        enable_thinking=True, **{**_DEFAULT_SAMPLING, "temperature": 0.6}
                    )
                },
                **common,
            ),
        },
        roles={"primary": RoleBinding(deployment=binding_default, open=True)},
    )


@pytest.fixture(autouse=True)
def _isolated_process_state() -> Iterator[None]:
    set_inference_concurrency_controller(None)
    token = _current_selection.set({})
    try:
        yield
    finally:
        _current_selection.reset(token)
        set_inference_concurrency_controller(None)


def _guard() -> DomainGuard:
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


async def _chunks() -> Any:
    class _Chunk:
        def model_dump(self) -> dict[str, Any]:
            return {
                "id": "c1",
                "choices": [{"delta": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    yield _Chunk()


async def _wire_kwargs(cfg: ModelConfig, *, mode: str | None) -> dict[str, Any]:
    """Build the client through the factory, dispatch one call, return what litellm received."""
    with patch("personal_agent.llm_client.factory.load_model_config", return_value=cfg):
        client = get_llm_client(role_name="primary", mode=mode)
    client._egress_guard = _guard()
    acompletion = AsyncMock(side_effect=lambda **_: _chunks())
    with patch("litellm.acompletion", acompletion):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=SystemTraceContext.new(
                "test", session_id="00000000-0000-0000-0000-000000000001"
            ),
        )
    return dict(acompletion.call_args.kwargs)


def _thinking_disabled(kwargs: dict[str, Any]) -> bool:
    template = kwargs["extra_body"].get("chat_template_kwargs") or {}
    return template.get("enable_thinking") is False


@pytest.mark.asyncio
class TestTheCallRequestsTheMode:
    async def test_without_a_request_the_default_mode_runs_with_thinking_on(self) -> None:
        kwargs = await _wire_kwargs(_config(), mode=None)
        assert not _thinking_disabled(kwargs)

    async def test_planner_mode_sends_thinking_off_with_the_default_sampling(self) -> None:
        planner = await _wire_kwargs(_config(), mode="planner")
        default = await _wire_kwargs(_config(), mode=None)

        assert _thinking_disabled(planner)
        # Everything but the thinking switch equals the default mode's request.
        planner_body = {
            k: v for k, v in planner["extra_body"].items() if k != "chat_template_kwargs"
        }
        assert planner_body == default["extra_body"]
        for key in ("temperature", "top_p", "presence_penalty"):
            assert planner[key] == default[key]
        assert planner["temperature"] == 1.0
        assert planner["top_p"] == 0.95
        assert planner["extra_body"]["top_k"] == 20
        assert planner["extra_body"]["min_p"] == 0.0
        assert planner["extra_body"]["repetition_penalty"] == 1.0


@pytest.mark.asyncio
class TestTheModeResolvesOnTheDeploymentThatRuns:
    async def test_a_deployment_without_the_mode_keeps_its_own_default_mode(self) -> None:
        cfg = _config(binding_default=_WITHOUT_MODE)
        with structlog.testing.capture_logs() as logs:
            kwargs = await _wire_kwargs(cfg, mode="planner")

        assert not _thinking_disabled(kwargs)
        assert kwargs["temperature"] == 0.6  # its own default, not another deployment's planner
        fallback = [e for e in logs if e["event"] == "role_binding_mode_missing_on_deployment"]
        assert len(fallback) == 1
        assert fallback[0]["requested_mode"] == "planner"
        assert fallback[0]["deployment"] == _WITHOUT_MODE

    async def test_a_session_selection_without_the_mode_does_not_get_the_bindings_mode(
        self,
    ) -> None:
        """The binding default has the mode; the session selected a deployment without it."""
        set_current_selection({"primary": _WITHOUT_MODE})
        kwargs = await _wire_kwargs(_config(binding_default=_WITH_MODE), mode="planner")

        assert not _thinking_disabled(kwargs)
        assert kwargs["temperature"] == 0.6

    async def test_a_session_selection_with_the_mode_gets_it_over_a_default_without(self) -> None:
        """The binding default lacks the mode; the session selected a deployment that has it."""
        set_current_selection({"primary": _WITH_MODE})
        kwargs = await _wire_kwargs(_config(binding_default=_WITHOUT_MODE), mode="planner")

        assert _thinking_disabled(kwargs)

    async def test_a_present_mode_logs_no_fallback(self) -> None:
        with structlog.testing.capture_logs() as logs:
            await _wire_kwargs(_config(), mode="planner")
        assert not [e for e in logs if e["event"] == "role_binding_mode_missing_on_deployment"]


class TestTheShippedCatalog:
    """AC-3 on ``config/models.yaml`` itself: the planner mode of the local primary."""

    def _local_primary(self) -> tuple[ModelDefinition, Dialect]:
        from personal_agent.config.model_loader import _load_model_config_at_path

        repo = Path(__file__).resolve().parents[3]
        config = _load_model_config_at_path(repo / "config" / "models.yaml")
        model = config.models["qwen3.8-flash-next"]
        dialect = model.resolve_dialect(config.providers.get(model.provider or ""))
        assert dialect is not None
        return model, dialect

    def test_the_planner_mode_is_thinking_off_with_the_default_sampling(self) -> None:
        from personal_agent.llm_client.litellm_client import _dialect_params

        model, dialect = self._local_primary()
        planner = _dialect_params(dialect, model.resolve_mode("planner"))
        default = _dialect_params(dialect, model.resolve_mode("default"))

        assert planner["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert "chat_template_kwargs" not in default["extra_body"]
        sampling = {k: v for k, v in planner.items() if k != "extra_body"}
        assert sampling == {k: v for k, v in default.items() if k != "extra_body"}
        body = {k: v for k, v in planner["extra_body"].items() if k != "chat_template_kwargs"}
        assert body == default["extra_body"]
        # The ADR-0154 D4 values, written out so a drift in either mode is visible here.
        assert sampling["temperature"] == 1.0 and sampling["top_p"] == 0.95
        assert sampling["presence_penalty"] == 0.0
        assert body["top_k"] == 20 and body["min_p"] == 0.0 and body["repetition_penalty"] == 1.0

    def test_only_the_probed_deployments_declare_a_planner_mode(self) -> None:
        """FRE-1514 AC-3 / ADR-0154 D7: a `planner` mode, and with it the scope rule,
        reaches a deployment only with a passing probe run on its ticket.

        Adding a deployment here is correct only in the change that posts its probe result.
        """
        from personal_agent.config.model_loader import _load_model_config_at_path

        repo = Path(__file__).resolve().parents[3]
        config = _load_model_config_at_path(repo / "config" / "models.yaml")
        declaring = {key for key, model in config.models.items() if "planner" in model.modes}

        assert declaring == {"qwen3.8-flash-next"}

    def test_the_planner_mode_is_not_the_worker_mode(self) -> None:
        model, _ = self._local_primary()
        assert model.resolve_mode("planner").temperature != model.resolve_mode("worker").temperature
        assert model.resolve_mode("planner").presence_penalty == 0.0
