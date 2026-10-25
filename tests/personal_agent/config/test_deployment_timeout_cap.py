"""FRE-1700 — a deployment's ``max_timeout`` caps the timeout any role resolves to on it.

Incident (trace 1d2d89fe, 2026-10-25): the ``sub_agent`` binding's ``default_timeout: 600``
(FRE-1496, sized for local workers that run 25 to 65 s) beat the 180 s that the two Scaleway
deployments declare, because ADR-0145 D2 applies a binding's budget on any resolved deployment.
A DeepSeek worker call held a turn for 274 s.

The role still owns its budget (D2). The deployment now states the longest wait that it
accepts, and the resolver lowers the role's budget to that ceiling. A deployment without a
ceiling behaves as before.

The numbers are distinguishable on purpose: 600 (binding), 180 (ceiling), 90 (a binding
below the ceiling) and 1 (the hanging-worker ceiling). An equality assertion matches one.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from personal_agent.config.model_loader import load_model_config, resolve_role_target
from personal_agent.config.selection import _current_selection, set_current_selection
from personal_agent.llm_client.concurrency import set_inference_concurrency_controller
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import (
    Dialect,
    ModelConfig,
    ModelDefinition,
    ModeSpec,
    Placement,
    ProviderDefinition,
    RoleBinding,
)
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentResult, SubAgentSpec
from personal_agent.security import DomainGuard

_LOCAL_ENDPOINT = "https://slm.test.example/v1"
_CAPPED = "capped_deployment"
_UNCAPPED = "uncapped_deployment"

_SCALEWAY_KEYS = ("glm-5.3-scaleway", "deepseek-v4.1-flash-scaleway")
_DEFAULT_PRIMARY = "qwen3.8-flash-next"


def _definition(default_timeout: int, max_timeout: int | None) -> ModelDefinition:
    return ModelDefinition(
        id="worker-model",
        provider="slm_local",
        context_length=131072,
        max_concurrency=1,
        endpoint=_LOCAL_ENDPOINT,
        default_timeout=default_timeout,
        max_timeout=max_timeout,
        default_mode="default",
        modes={"default": ModeSpec()},
    )


def _config(binding_timeout: int | None, *, cap: int | None = 180) -> ModelConfig:
    """A catalog with a capped and an uncapped deployment, and a ``sub_agent`` binding."""
    return ModelConfig(
        providers={
            "slm_local": ProviderDefinition(
                base_url=_LOCAL_ENDPOINT,
                placement=Placement.LOCAL,
                max_concurrency=4,
                dialect=Dialect.LLAMACPP_QWEN,
            ),
        },
        models={
            _CAPPED: _definition(min(180, cap or 180), cap),
            _UNCAPPED: _definition(300, None),
        },
        roles={
            "sub_agent": RoleBinding(
                deployment=_CAPPED, open=True, default_timeout=binding_timeout
            ),
        },
    )


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Selection and the concurrency controller are process-global — reset both per test."""
    token = _current_selection.set({})
    set_inference_concurrency_controller(None)
    try:
        yield
    finally:
        _current_selection.reset(token)
        set_inference_concurrency_controller(None)


class TestTheCeilingLowersTheRolesBudget:
    """AC-1 / AC-2 at the resolver, on a synthetic catalog."""

    def test_a_binding_above_the_ceiling_is_lowered_to_it(self) -> None:
        _, definition = resolve_role_target("sub_agent", config=_config(600))

        assert definition is not None
        assert definition.default_timeout == 180

    def test_a_binding_below_the_ceiling_is_kept(self) -> None:
        """The ceiling never raises a timeout."""
        _, definition = resolve_role_target("sub_agent", config=_config(90))

        assert definition is not None
        assert definition.default_timeout == 90

    def test_a_redirect_to_the_capped_deployment_is_capped_too(self) -> None:
        """The case the incident was: the binding names one key and a selection lands on another."""
        base = _config(600)
        config = base.model_copy(
            update={
                "roles": {
                    "sub_agent": RoleBinding(deployment=_UNCAPPED, open=True, default_timeout=600)
                }
            }
        )

        key, definition = resolve_role_target("sub_agent", model_key=_CAPPED, config=config)

        assert key == _CAPPED
        assert definition is not None
        assert definition.default_timeout == 180

    def test_a_deployment_without_a_ceiling_takes_the_bindings_value(self) -> None:
        """D2 unchanged: no ceiling, the binding's 600 applies even above the deployment's 300."""
        _, definition = resolve_role_target("sub_agent", model_key=_UNCAPPED, config=_config(600))

        assert definition is not None
        assert definition.default_timeout == 600

    def test_a_role_with_no_budget_gets_the_deployments_own_value(self) -> None:
        _, definition = resolve_role_target("sub_agent", config=_config(None))

        assert definition is not None
        assert definition.default_timeout == 180


class TestADefaultAboveItsCeilingIsRefused:
    def test_default_timeout_above_max_timeout_raises(self) -> None:
        with pytest.raises(ValidationError, match="max_timeout"):
            _definition(default_timeout=300, max_timeout=180)

    def test_default_timeout_equal_to_max_timeout_is_accepted(self) -> None:
        assert _definition(default_timeout=180, max_timeout=180).max_timeout == 180


class TestTheLiveCatalog:
    """AC-1 / AC-2 on ``config/models.yaml`` and ``config/model_roles.yaml``, as shipped."""

    @pytest.mark.parametrize("key", _SCALEWAY_KEYS)
    def test_ac1_sub_agent_on_a_selected_scaleway_primary_times_out_at_180(self, key: str) -> None:
        set_current_selection({"primary": key})

        resolved_key, definition = resolve_role_target("sub_agent", config=load_model_config())

        assert resolved_key == key
        assert definition is not None
        assert definition.default_timeout == 180

    def test_ac2_sub_agent_on_the_default_primary_keeps_600(self) -> None:
        resolved_key, definition = resolve_role_target("sub_agent", config=load_model_config())

        assert resolved_key == _DEFAULT_PRIMARY
        assert definition is not None
        assert definition.default_timeout == 600

    @pytest.mark.parametrize("key", _SCALEWAY_KEYS)
    def test_the_two_scaleway_entries_declare_the_ceiling(self, key: str) -> None:
        assert load_model_config().models[key].max_timeout == 180


def _guard() -> DomainGuard:
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


def _worker_client(model_key: str, config: ModelConfig) -> LiteLLMClient:
    """The client the factory would build for ``sub_agent`` resolved onto ``model_key``."""
    _, definition = resolve_role_target("sub_agent", model_key=model_key, config=config)
    assert definition is not None
    assert definition.provider is not None
    return LiteLLMClient(
        model_id=definition.id,
        model_key=model_key,
        provider=definition.provider,
        max_tokens=definition.max_tokens,
        budget_role="sub_agent",
        placement=Placement.LOCAL,
        model_def=definition,
        egress_guard=_guard(),
    )


class _Chunk:
    def model_dump(self) -> dict[str, Any]:
        return {
            "id": "chunk-1",
            "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


async def _answers(**_: Any) -> Any:
    yield _Chunk()


async def _hangs(**_: Any) -> Any:
    import asyncio

    await asyncio.sleep(60)
    yield _Chunk()  # pragma: no cover — the ceiling always fires first


def _capped_config() -> ModelConfig:
    """A 1 s ceiling under a 600 s binding: the real wall-clock wait is 1 s, not 600."""
    return _config(600, cap=1)


def _spec(task: str) -> SubAgentSpec:
    return SubAgentSpec(task=task, context=[])


@pytest.mark.asyncio
class TestAWorkerPastTheLimitIsAFailedResultTheTurnGoesOnWithout:
    """AC-3 — the ceiling is enforced, and a timeout costs the turn one worker, not the turn."""

    async def test_ac3_a_hanging_worker_ends_as_a_failed_result_at_the_ceiling(self) -> None:
        client = _worker_client(_CAPPED, _capped_config())
        acompletion = AsyncMock(side_effect=_hangs)

        started = time.monotonic()
        with patch("litellm.acompletion", acompletion):
            result = await run_sub_agent(
                spec=_spec("hang"),
                llm_client=client,
                trace_id="fre1700",
                session_id="00000000-0000-0000-0000-000000000001",
            )
        elapsed = time.monotonic() - started

        assert acompletion.call_args.kwargs["timeout"].read == 1.0
        assert result.success is False
        assert result.stop_reason == "timeout"
        # The inner budget fires first. The outer deadline is the budget plus the absorption
        # margin (25 s by default), so a result inside 10 s is the 1 s ceiling firing.
        assert elapsed < 10

    async def test_ac3_the_turn_goes_on_without_the_timed_out_worker(self) -> None:
        """One real timeout result and one real success: the fan-out is not degraded."""
        from personal_agent.orchestrator.expansion_controller import ExpansionController

        config = _capped_config()
        hanging = _worker_client(_CAPPED, config)
        working = _worker_client(_UNCAPPED, config)
        real_run_sub_agent = run_sub_agent
        results: list[SubAgentResult] = []

        async def _dispatch(*_: object, spec: SubAgentSpec, **kwargs: Any) -> SubAgentResult:
            hangs = "hang" in spec.task
            client = hanging if hangs else working
            kwargs["llm_client"] = client
            with patch("litellm.acompletion", AsyncMock(side_effect=_hangs if hangs else _answers)):
                result = await real_run_sub_agent(spec=spec, **kwargs)
            results.append(result)
            return result

        plan = {
            "strategy": "HYBRID",
            "tasks": [
                {"name": "t0", "goal": "hang", "type": "general"},
                {"name": "t1", "goal": "answer", "type": "general"},
            ],
        }
        planner = AsyncMock()
        planner.respond = AsyncMock(return_value={"content": json.dumps(plan)})

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_dispatch,
        ):
            outcome = await ExpansionController().execute(
                query="research scaling approaches",
                strategy="HYBRID",
                llm_client=planner,
                trace_id=str(uuid4()),
                messages=[],
            )

        assert sorted(r.stop_reason for r in results) == ["completed", "timeout"]
        assert outcome.successful_count == 1
        assert outcome.failed_count == 1
        assert outcome.degraded is False
