"""ADR-0145 D1 (FRE-1444) — a worker's timeout comes from its role, not from a setting.

Before this change the number was pinned three deep and the innermost pin won:
``expansion_controller`` stamped ``settings.worker_timeout_seconds`` (60) onto every
``SubAgentSpec``, ``SubAgentSpec`` itself defaulted to 120, and an explicit ``timeout_s``
beat the deployment's ``default_timeout`` inside the client. The role's own budget
therefore never reached the wire.

Every number here is distinguishable on purpose. The deployment declares 600, the role
binds 90, and the two wrong answers the ticket names are 60 (the deleted setting) and 120
(the deleted spec default). An equality assertion can match only one of the four, so the
broken half-fix — deleting the settings read alone, which yields 120 — fails these tests
rather than passing a weaker "not 60" claim.

The outer safety net is proved by making it fire (``TestTheOuterDeadlineStillFires``). A net
proved only by never firing has not been proved.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
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
from personal_agent.orchestrator.sub_agent import (
    _effective_hard_deadline,
    _resolve_effective_timeout,
    run_sub_agent,
)
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec

_LOCAL_ENDPOINT = "https://slm.test.example/v1"

#: The deployment the ``sub_agent`` role binds. Its own 600 must never be the dispatched
#: value once the binding names a budget — that is D2's rule, which D1 depends on.
_WORKER_DEPLOYMENT = "worker_deployment"

#: The role's budget. The number ADR-0145 D1 puts on the ``sub_agent`` binding.
_ROLE_BUDGET = 90

#: AC-5's second binding value. Distinct from every other number in this module.
_MOVED_ROLE_BUDGET = 30

_PROVIDERS = {
    "slm_local": ProviderDefinition(
        base_url=_LOCAL_ENDPOINT,
        placement=Placement.LOCAL,
        max_concurrency=4,
        dialect=Dialect.LLAMACPP_QWEN,
    ),
}

#: The `sub_agent` role reaches a cloud deployment through `defaults_by_primary`
#: (claude_sonnet, claude_haiku, gpt-5.4-mini and qwen3.8-27b-ovh all self-pair), so the
#: role's budget must bind on that placement too. The two branches resolve the number
#: differently — only the local one falls back to the definition — which is why this is
#: covered rather than assumed from the local result.
_CLOUD_DEPLOYMENT = "cloud_worker_deployment"


def _config(role_budget: int) -> ModelConfig:
    """A catalog whose ``sub_agent`` binding carries ``role_budget`` seconds."""
    return ModelConfig(
        providers=_PROVIDERS,
        models={
            _WORKER_DEPLOYMENT: ModelDefinition(
                id="worker-model",
                provider="slm_local",
                context_length=131072,
                max_concurrency=1,
                endpoint=_LOCAL_ENDPOINT,
                default_timeout=600,
                default_mode="default",
                modes={"default": ModeSpec()},
            ),
        },
        roles={
            "sub_agent": RoleBinding(
                deployment=_WORKER_DEPLOYMENT,
                open=True,
                default_timeout=role_budget,
            ),
        },
    )


def _cloud_config(role_budget: int) -> ModelConfig:
    """The same binding, on a cloud-placed deployment."""
    return ModelConfig(
        providers={
            "anthropic": ProviderDefinition(
                auth_env="anthropic_api_key",
                placement=Placement.CLOUD,
                max_concurrency=50,
                dialect=Dialect.ANTHROPIC_BUDGET,
            ),
        },
        models={
            _CLOUD_DEPLOYMENT: ModelDefinition(
                id="cloud-worker-model",
                provider="anthropic",
                context_length=200000,
                max_concurrency=20,
                max_tokens=1024,
                default_timeout=600,
                default_mode="default",
                modes={"default": ModeSpec()},
            ),
        },
        roles={
            "sub_agent": RoleBinding(
                deployment=_CLOUD_DEPLOYMENT,
                open=True,
                default_timeout=role_budget,
            ),
        },
    )


def _permissive_guard() -> Any:
    """A DomainGuard that refuses nothing — never touches network or disk."""
    from personal_agent.security import DomainGuard

    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


def _worker_client(role_budget: int = _ROLE_BUDGET) -> LiteLLMClient:
    """The client the factory would build for the ``sub_agent`` role."""
    _, model_def = resolve_role_target("sub_agent", config=_config(role_budget))
    assert model_def is not None
    assert model_def.provider is not None
    return LiteLLMClient(
        model_id=model_def.id,
        model_key=_WORKER_DEPLOYMENT,
        provider=model_def.provider,
        max_tokens=model_def.max_tokens,
        budget_role="sub_agent",
        placement=Placement.LOCAL,
        model_def=model_def,
        egress_guard=_permissive_guard(),
    )


def _worker_spec(**overrides: Any) -> SubAgentSpec:
    """A production-shaped worker spec: it names no budget of its own."""
    return SubAgentSpec(task="summarise the finding", context=[], **overrides)


def _stream_chunk() -> Any:
    class _Chunk:
        def model_dump(self) -> dict[str, Any]:
            return {
                "id": "chunk-1",
                "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    return _Chunk()


async def _fake_stream() -> Any:
    yield _stream_chunk()


@pytest.fixture(autouse=True)
def _reset_concurrency_singleton() -> Any:
    """The re-homed controller (ADR-0141 D3) is process-global — reset it per test."""
    set_inference_concurrency_controller(None)
    yield
    set_inference_concurrency_controller(None)


async def _dispatch_worker(client: LiteLLMClient) -> dict[str, Any]:
    """Run a real ``run_sub_agent`` call and return the kwargs litellm received."""
    acompletion = AsyncMock(side_effect=lambda **_: _fake_stream())
    with patch("litellm.acompletion", acompletion):
        result = await run_sub_agent(
            spec=_worker_spec(),
            llm_client=client,
            trace_id="fre1444",
            session_id="00000000-0000-0000-0000-000000000001",
        )
    assert result.success is True, result.error
    return dict(acompletion.call_args.kwargs)


def _cloud_worker_client(role_budget: int = _ROLE_BUDGET) -> LiteLLMClient:
    """The client the factory would build for a cloud-placed ``sub_agent`` deployment."""
    _, model_def = resolve_role_target("sub_agent", config=_cloud_config(role_budget))
    assert model_def is not None
    assert model_def.provider is not None
    return LiteLLMClient(
        model_id=model_def.id,
        model_key=_CLOUD_DEPLOYMENT,
        provider=model_def.provider,
        max_tokens=model_def.max_tokens,
        budget_role="sub_agent",
        placement=Placement.CLOUD,
        model_def=model_def,
        egress_guard=_permissive_guard(),
    )


def _cloud_response() -> MagicMock:
    usage = MagicMock()
    usage.prompt_tokens = 1
    usage.completion_tokens = 1
    usage.total_tokens = 2
    usage.cache_read_input_tokens = None
    usage.cache_creation_input_tokens = None
    usage.prompt_tokens_details = None

    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "ok"
    response.choices[0].message.tool_calls = None
    response.usage = usage
    response.id = "resp_fre1444"
    return response


class _CloudDispatchIsolation:
    """Patches that isolate a cloud dispatch from cost, credentials and telemetry."""

    def __init__(self, captured: dict[str, Any]) -> None:
        async def _fake_acompletion(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _cloud_response()

        gate = MagicMock()
        gate.reserve = AsyncMock(return_value="res-fre1444")
        gate.commit = AsyncMock()
        gate.refund = AsyncMock()

        tracker = AsyncMock()
        tracker.connect = AsyncMock()
        tracker.record_api_call = AsyncMock()

        self._patches = [
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
                return_value=MagicMock(anthropic_api_key="k", llm_max_retries=0),
            ),
        ]

    def __enter__(self) -> None:
        for item in self._patches:
            item.__enter__()

    def __exit__(self, *exc: object) -> None:
        for item in reversed(self._patches):
            item.__exit__(*exc)


class _IgnoresItsBudgetClient:
    """A client that declares a budget and then ignores it.

    This is the exact condition the outer deadline exists for, stated in
    ``run_sub_agent``'s own comment: a client that never honours ``timeout_s``. A mock
    that also honours it could not distinguish the net firing from the generation
    budget firing.
    """

    def __init__(self, declared: float) -> None:
        self.default_timeout_seconds = declared

    async def respond(self, *args: object, **kwargs: object) -> str:
        await asyncio.sleep(30)
        return "too late"  # pragma: no cover — the deadline always fires first


@pytest.mark.asyncio
class TestTheRolesBudgetReachesTheWire:
    """AC-1 / AC-3 — the dispatched generation budget is the role's own value."""

    async def test_ac1_dispatched_timeout_is_the_roles_value(self) -> None:
        """AC-1: 90 — not 60 (the deleted setting), 120 (the deleted spec default) or 600.

        Asserted on the kwargs that reach ``litellm.acompletion``, not on the resolved
        definition read back as a proxy for what the client does with it.
        """
        kwargs = await _dispatch_worker(_worker_client())

        assert kwargs["timeout"].read == float(_ROLE_BUDGET)

    async def test_ac3_a_spec_with_no_override_dispatches_without_raising(self) -> None:
        """AC-3: the ``None`` case the seam exists for.

        Without the seam ``_effective_hard_deadline`` receives ``None`` and raises on the
        multiplication, before any call is made — so AC-1 could never run. Reaching a
        successful dispatch at all is the proof.
        """
        spec = _worker_spec()
        assert spec.timeout_seconds is None

        kwargs = await _dispatch_worker(_worker_client())

        assert kwargs["timeout"].read == float(_ROLE_BUDGET)

    async def test_ac3_the_seam_resolves_one_number_for_both_uses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-3, at the seam itself: the layer names the number, and sizes a real net from it.

        The strict inequality is part of AC-3 rather than only AC-2's business: a
        deadline that merely computed without raising, and then equalled the generation
        timeout, would be a net that can never fire.
        """
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 25.0)

        spec = _worker_spec()
        resolved = _resolve_effective_timeout(spec, _worker_client())

        assert resolved == float(_ROLE_BUDGET)
        assert _effective_hard_deadline(spec, resolved) > resolved

    async def test_ac1_the_roles_value_also_binds_on_cloud_placement(self) -> None:
        """AC-1 on the other branch, which resolves the number differently.

        The cloud branch applies a timeout only when the call names one, so a
        cloud-placed worker would dispatch with no role budget at all if the sub-agent
        layer merely omitted it and trusted the client to re-derive it.
        """
        captured: dict[str, Any] = {}
        with _CloudDispatchIsolation(captured):
            result = await run_sub_agent(
                spec=_worker_spec(),
                llm_client=_cloud_worker_client(),
                # A real UUID: the cloud branch records cost and parses both identifiers,
                # which the local branch skips.
                trace_id="00000000-0000-0000-0000-0000000014a4",
                session_id="00000000-0000-0000-0000-000000000001",
            )

        assert result.success is True, result.error
        assert captured["timeout"] == float(_ROLE_BUDGET)

    async def test_an_explicit_spec_override_still_wins(self) -> None:
        """The seam is a fallback, not a seizure — a caller that names a budget keeps it."""
        assert (
            _resolve_effective_timeout(_worker_spec(timeout_seconds=7.0), _worker_client()) == 7.0
        )


class TestTheOuterDeadlineExceedsTheGenerationTimeout:
    """AC-2 — the safety net keeps its declared queue-wait absorption."""

    def test_ac2_deadline_is_the_timeout_plus_the_absorption(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-2: 90 + 25 = 115, and strictly greater than 90.

        A fixed 85 against a 90s role budget gives ``max(85, 90) = 90``: the net collapses
        onto the generation timeout and can never fire. The strict inequality is what
        rejects that state.

        The budget is resolved through the seam rather than written in as a literal, so
        this test also fails on the 120 half-fix instead of being blind to it.
        """
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 25.0)

        spec = _worker_spec()
        resolved = _resolve_effective_timeout(spec, _worker_client())
        deadline = _effective_hard_deadline(spec, resolved)

        assert resolved == float(_ROLE_BUDGET)
        assert deadline == 115.0
        assert deadline > resolved

    def test_an_explicit_zero_hard_deadline_is_honoured_not_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0.0 means "no absorption margin", and the clamp floors it at the budget.

        A falsy-value guard, not a scenario: ``0.0 or X`` yields X, so a caller asking
        for no margin would silently receive 25s more than it asked for. The derived
        default is 115, so the two answers are distinguishable.
        """
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 25.0)

        spec = _worker_spec(hard_deadline_seconds=0.0)

        assert _effective_hard_deadline(spec, float(_ROLE_BUDGET)) == float(_ROLE_BUDGET)

    def test_ac2_the_margin_survives_a_larger_role_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The relationship is derived, so it holds for any binding value — not just 90."""
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 25.0)

        assert _effective_hard_deadline(_worker_spec(), 600.0) == 625.0


@pytest.mark.asyncio
class TestTheOuterDeadlineStillFires:
    """AC-4 — the net terminates a worker that overruns."""

    async def test_ac4_the_net_fires_at_the_declared_budget_plus_absorption(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-4: a hanging worker is killed by the outer ``wait_for``, not by the client.

        The spec names no budget, so this also binds the whole chain — the client's
        declaration, the sub-agent layer's resolution of it, the derived deadline, and the
        ``wait_for`` that consumes it. The error wording distinguishes which budget fired:
        the generation branch names itself.

        The absorption (0.8) is four times the declared budget (0.2) so the measured
        window excludes the collapsed answer: a net fixed at the generation timeout would
        fire near 0.2, well below the lower bound asserted here.
        """
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 0.8)

        started = time.monotonic()
        result = await run_sub_agent(
            spec=_worker_spec(),
            llm_client=_IgnoresItsBudgetClient(declared=0.2),
            trace_id="fre1444",
        )
        elapsed = time.monotonic() - started

        assert result.success is False
        assert result.error is not None
        assert result.error.startswith("Timeout after")
        assert "generation budget" not in result.error
        assert 0.7 < elapsed < 1.6


@pytest.mark.asyncio
class TestTheDispatchSiteNoLongerPinsABudget:
    """AC-1's other half — the fan-out must stop stamping a budget onto every spec.

    ``run_sub_agent`` can only fall to the role's value if the spec reaching it names
    none. Asserted on the specs the controller actually builds, because the settings
    read it replaced was invisible from ``run_sub_agent``'s own side.
    """

    async def test_every_dispatched_spec_defers_both_budgets(self) -> None:
        """Both fields are ``None``: the generation budget and the safety net."""
        from personal_agent.orchestrator.expansion_controller import ExpansionController

        captured: list[SubAgentSpec] = []

        async def _capture(*_: object, spec: SubAgentSpec, **__: object) -> Any:
            captured.append(spec)
            raise AssertionError("dispatch is not exercised — only the spec is")

        planner = AsyncMock()
        planner.respond = AsyncMock(
            return_value=(
                '{"strategy": "HYBRID", "tasks": [{"name": "t0", "goal": "g0", '
                '"constraints": [], "expected_output": "text"}]}'
            )
        )

        with patch(
            "personal_agent.orchestrator.expansion_controller.run_sub_agent",
            side_effect=_capture,
        ):
            await ExpansionController().execute(
                query="research scaling approaches",
                strategy="HYBRID",
                llm_client=planner,
                trace_id="fre1444",
                messages=[],
            )

        assert captured, "the controller dispatched no sub-agent"
        assert [s.timeout_seconds for s in captured] == [None] * len(captured)
        assert [s.hard_deadline_seconds for s in captured] == [None] * len(captured)


@pytest.mark.asyncio
class TestChangingTheBindingMovesBothNumbers:
    """AC-5 — one source of truth: the binding moves the timeout and the deadline together."""

    async def test_ac5_both_numbers_follow_the_binding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-5: 90 -> 115 becomes 30 -> 55. Only one number moving would be two sources."""
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 25.0)

        bound = await _dispatch_worker(_worker_client(_ROLE_BUDGET))
        moved = await _dispatch_worker(_worker_client(_MOVED_ROLE_BUDGET))

        assert bound["timeout"].read == 90.0
        assert moved["timeout"].read == 30.0
        assert _effective_hard_deadline(_worker_spec(), 90.0) == 115.0
        assert _effective_hard_deadline(_worker_spec(), 30.0) == 55.0

    async def test_ac5_the_moved_deadline_is_the_one_the_runner_uses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second arm, measured rather than computed: a larger declaration kills later.

        Against AC-4's 0.2 declaration under the same 0.8 absorption, this 0.8 one must
        terminate near 1.6 rather than near 1.0. The lower bound also excludes the
        collapsed answer (0.8), so the measurement discriminates both states.
        """
        monkeypatch.setattr(settings, "worker_queue_absorption_seconds", 0.8)

        started = time.monotonic()
        await run_sub_agent(
            spec=_worker_spec(),
            llm_client=_IgnoresItsBudgetClient(declared=0.8),
            trace_id="fre1444",
        )
        elapsed = time.monotonic() - started

        assert 1.3 < elapsed < 2.2
