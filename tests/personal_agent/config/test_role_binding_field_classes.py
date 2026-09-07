"""ADR-0145 D2 (FRE-1442) — resolve_role_target sorts a field by what it is.

The pre-D2 rule dropped every ``RoleBinding`` override whenever a per-turn
selection or an explicit ``model_key`` resolved a role to a deployment other
than the binding's own — exactly the case a selection exists for. D2 replaces
that key-match gate with three field classes:

* **Budget** (``max_tokens``, ``default_timeout``) always applies, on any
  resolved deployment — a worker's 90s and 2048 tokens describe the job, not
  the model.
* **Mode** (``binding.mode``) resolves against whichever deployment the role
  lands on: that deployment's own mode of this name, or its ``default_mode``
  with a log when it declares no mode of that name.
* **Sampler** values have no field on ``RoleBinding`` at all — they live only
  inside a mode on the model, so nothing can carry one across a redirect.

AC-1/AC-2/AC-3 are asserted at the dispatch boundary (the kwargs
``litellm.acompletion`` actually receives, and — for the timeout — the real
enforced wall-clock budget), never by reading the resolved
:class:`~personal_agent.llm_client.models.ModelDefinition` back as a proxy for
what the client does with it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

from personal_agent.config.model_loader import resolve_role_target
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
from personal_agent.llm_client.types import LLMTimeout, ModelRole
from personal_agent.security import DomainGuard
from personal_agent.telemetry.trace import SystemTraceContext

_LOCAL_ENDPOINT = "https://slm.test.example/v1"

# The binding's own deployment. Its "worker" mode's temperature (0.2) must
# never be the value that reaches the wire once a redirect selects a
# different deployment — that would be AC-4's failure case.
_BOUND = "bound_deployment"

# A redirect target declaring its OWN "worker" mode, at a temperature (0.9)
# distinct from both _BOUND's (0.2) and its own "default" mode's (0.5) — three
# distinguishable values so the dispatched kwargs can only match one of
# "correct" (0.9), "carried the original mode" (0.2), or "ignored the
# requested mode and fell to the redirect's own default" (0.5).
_REDIRECT_WITH_MODE = "redirect_with_worker_mode"

# A redirect target with no "worker" mode at all — AC-5's fallback case.
_REDIRECT_NO_MODE = "redirect_without_worker_mode"

_ROLE = "primary"

_COMMON_MODEL_KWARGS: dict[str, Any] = {
    "provider": "slm_local",
    "context_length": 131072,
    "max_concurrency": 1,
    "endpoint": _LOCAL_ENDPOINT,
}

_PROVIDERS = {
    "slm_local": ProviderDefinition(
        base_url=_LOCAL_ENDPOINT,
        placement=Placement.LOCAL,
        max_concurrency=4,
        dialect=Dialect.LLAMACPP_QWEN,
    ),
}


def _config() -> ModelConfig:
    return ModelConfig(
        providers=_PROVIDERS,
        models={
            _BOUND: ModelDefinition(
                id="bound-model",
                default_timeout=600,
                default_mode="default",
                modes={
                    "default": ModeSpec(),
                    "worker": ModeSpec(temperature=0.2),
                },
                **_COMMON_MODEL_KWARGS,
            ),
            _REDIRECT_WITH_MODE: ModelDefinition(
                id="redirect-model-with-mode",
                default_timeout=300,
                default_mode="default",
                modes={
                    "default": ModeSpec(temperature=0.5),
                    "worker": ModeSpec(temperature=0.9),
                },
                **_COMMON_MODEL_KWARGS,
            ),
            _REDIRECT_NO_MODE: ModelDefinition(
                id="redirect-model-no-mode",
                default_timeout=45,
                default_mode="default",
                modes={"default": ModeSpec(temperature=0.7)},
                **_COMMON_MODEL_KWARGS,
            ),
        },
        roles={
            _ROLE: RoleBinding(
                deployment=_BOUND,
                open=True,
                mode="worker",
                max_tokens=2048,
                default_timeout=90,
            ),
        },
    )


def _config_short_timeout() -> ModelConfig:
    """AC-2's own fixture: a 1s binding budget against a 30s redirect-target default.

    Kept separate from :func:`_config` so the genuine wall-clock enforcement test
    (which really waits out its timeout) runs in ~1s rather than minutes, while
    every other test keeps the readable 90s/300s numbers.
    """
    return ModelConfig(
        providers=_PROVIDERS,
        models={
            _BOUND: ModelDefinition(
                id="bound-model",
                default_timeout=600,
                default_mode="default",
                modes={"default": ModeSpec()},
                **_COMMON_MODEL_KWARGS,
            ),
            _REDIRECT_WITH_MODE: ModelDefinition(
                id="redirect-model-with-mode",
                default_timeout=30,
                default_mode="default",
                modes={"default": ModeSpec()},
                **_COMMON_MODEL_KWARGS,
            ),
        },
        roles={
            _ROLE: RoleBinding(deployment=_BOUND, open=True, default_timeout=1),
        },
    )


def _permissive_guard() -> DomainGuard:
    """A DomainGuard that refuses nothing — never touches network or disk."""
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


@pytest.fixture(autouse=True)
def _reset_concurrency_singleton() -> Any:
    """The re-homed controller (ADR-0141 D3) is process-global — reset it per test."""
    set_inference_concurrency_controller(None)
    yield
    set_inference_concurrency_controller(None)


def _client_for(
    model_key: str, *, config: ModelConfig | None = None
) -> tuple[ModelDefinition, LiteLLMClient]:
    """Resolve `_ROLE` against `model_key` and build the client that would dispatch it."""
    cfg = config if config is not None else _config()
    _, model_def = resolve_role_target(_ROLE, model_key=model_key, config=cfg)
    assert model_def is not None
    assert model_def.provider is not None
    client = LiteLLMClient(
        model_id=model_def.id,
        model_key=model_key,
        provider=model_def.provider,
        max_tokens=model_def.max_tokens,
        budget_role="primary",
        placement=Placement.LOCAL,
        model_def=model_def,
        egress_guard=_permissive_guard(),
    )
    return model_def, client


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


async def _dispatch(client: LiteLLMClient, *, acompletion: AsyncMock) -> dict[str, Any]:
    """Drive a real respond() call through the local dispatch path; return the acompletion kwargs."""
    with patch("litellm.acompletion", acompletion):
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=SystemTraceContext.new(
                "test", session_id="00000000-0000-0000-0000-000000000001"
            ),
        )
    return dict(acompletion.call_args.kwargs)


@pytest.mark.asyncio
class TestBudgetSurvivesARedirect:
    """AC-1/AC-2/AC-3 — max_tokens and default_timeout apply on any resolved deployment."""

    async def test_ac2_timeout_enforced_at_the_bindings_value_on_a_redirect(self) -> None:
        """AC-2 seeded negative: the real wall-clock budget is the binding's 1s.

        Not the redirected deployment's own 30s default_timeout — asserted by
        actually exceeding it, not by reading the resolved definition back as a
        proxy. Uses its own short-timeout fixture (1s vs. 90s elsewhere) so this
        test, which really waits out its timeout, takes ~1s rather than minutes.
        """
        model_def, client = _client_for(_REDIRECT_WITH_MODE, config=_config_short_timeout())
        assert model_def.default_timeout == 1  # sanity: the override reached the definition

        async def _hangs(**_: Any) -> Any:
            await asyncio.sleep(model_def.default_timeout + 5)
            yield _stream_chunk()  # pragma: no cover — never reached

        with pytest.raises(LLMTimeout, match=r"exceeded its 1\.0s wall-clock"):
            await _dispatch(client, acompletion=AsyncMock(side_effect=_hangs))

    async def test_ac3_max_tokens_dispatched_at_the_bindings_value_on_a_redirect(self) -> None:
        """AC-3 seeded negative, as its own call (not sharing AC-2's call).

        The dispatched max_tokens is the binding's 2048 cap on a redirected
        deployment that declares none of its own.
        """
        _, client = _client_for(_REDIRECT_WITH_MODE)
        kwargs = await _dispatch(
            client, acompletion=AsyncMock(side_effect=lambda **_: _fake_stream())
        )
        assert kwargs["max_tokens"] == 2048

    async def test_ac1_both_survive_a_redirect_in_the_final_dispatched_kwargs(self) -> None:
        """AC-1: both budget fields, read from the actual kwargs handed to litellm."""
        model_def, client = _client_for(_REDIRECT_WITH_MODE)
        kwargs = await _dispatch(
            client, acompletion=AsyncMock(side_effect=lambda **_: _fake_stream())
        )
        assert kwargs["max_tokens"] == 2048
        assert kwargs["timeout"].read == 90.0
        assert model_def.default_timeout == 90

    async def test_no_redirect_baseline_unaffected(self) -> None:
        """Baseline (no selection at all): budget still applies — D2 does not regress this case."""
        _, client = _client_for(_BOUND)
        kwargs = await _dispatch(
            client, acompletion=AsyncMock(side_effect=lambda **_: _fake_stream())
        )
        assert kwargs["max_tokens"] == 2048
        assert kwargs["timeout"].read == 90.0


@pytest.mark.asyncio
class TestModeResolvesThroughTheSelectedModel:
    """AC-4/AC-5 — a mode name resolves against whichever deployment the role lands on."""

    async def test_ac4_the_redirects_own_mode_is_dispatched_not_the_originals(self) -> None:
        """AC-4: redirected to a deployment that DOES declare "worker".

        Its own 0.9 must reach the wire. 0.2 (the original's "worker") reaching
        the wire would mean the mode body was carried across the redirect; 0.5
        (the redirect's own "default") would mean the requested mode was
        silently ignored. Either is a discriminated failure, not just "no crash".
        """
        _, client = _client_for(_REDIRECT_WITH_MODE)
        kwargs = await _dispatch(
            client, acompletion=AsyncMock(side_effect=lambda **_: _fake_stream())
        )
        assert kwargs.get("temperature") == 0.9

    async def test_ac5_missing_mode_falls_back_to_default_mode_and_logs(self) -> None:
        """AC-5: redirected to a deployment with no "worker" mode.

        Falls back to its own default_mode ("default", temperature 0.7), and the
        fallback is logged.
        """
        cfg = _config()
        with structlog.testing.capture_logs() as logs:
            _, model_def = resolve_role_target(_ROLE, model_key=_REDIRECT_NO_MODE, config=cfg)

        assert model_def is not None
        assert model_def.resolve_mode().temperature == 0.7

        fallback_logs = [e for e in logs if e["event"] == "role_binding_mode_missing_on_deployment"]
        assert len(fallback_logs) == 1
        assert fallback_logs[0]["role"] == _ROLE
        assert fallback_logs[0]["requested_mode"] == "worker"
        assert fallback_logs[0]["deployment"] == _REDIRECT_NO_MODE
        assert fallback_logs[0]["default_mode"] == "default"


class TestResolverLevelSanity:
    """Fast, non-dispatch checks of the same three-class rule on the resolver's return value."""

    def test_ac5_no_fallback_log_when_mode_is_declared(self) -> None:
        """No spurious fallback log when the redirect DOES declare the requested mode."""
        cfg = _config()
        with structlog.testing.capture_logs() as logs:
            resolve_role_target(_ROLE, model_key=_REDIRECT_WITH_MODE, config=cfg)

        assert not [e for e in logs if e["event"] == "role_binding_mode_missing_on_deployment"]

    def test_ac6_signature_unchanged_two_tuple(self) -> None:
        """AC-6: still a plain (key, definition) pair — every existing call site unpacks two."""
        cfg = _config()
        result = resolve_role_target(_ROLE, model_key=_REDIRECT_WITH_MODE, config=cfg)
        assert isinstance(result, tuple)
        assert len(result) == 2
        key, definition = result
        assert key == _REDIRECT_WITH_MODE
        assert definition is not None

    def test_budget_survives_redirect(self) -> None:
        """Both budget fields carry over from the binding when the key redirects."""
        cfg = _config()
        _, definition = resolve_role_target(_ROLE, model_key=_REDIRECT_WITH_MODE, config=cfg)
        assert definition is not None
        assert definition.max_tokens == 2048
        assert definition.default_timeout == 90

    def test_budget_applies_with_no_redirect_too(self) -> None:
        """D2 removes the key-match gate entirely.

        Budget applies whether or not a redirect occurred, so the no-redirect
        case is not a special path.
        """
        cfg = _config()
        _, definition = resolve_role_target(_ROLE, config=cfg)
        assert definition is not None
        assert definition.max_tokens == 2048
        assert definition.default_timeout == 90

    def test_unbound_role_untouched(self) -> None:
        """A role with no binding at all resolves via key lookup, unaffected by D2."""
        cfg = _config()
        key, definition = resolve_role_target("no_such_role", model_key=_BOUND, config=cfg)
        assert key == _BOUND
        assert definition is not None
        assert definition.max_tokens is None
        assert definition.default_timeout == 600
