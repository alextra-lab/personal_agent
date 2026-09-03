"""ADR-0141 T3 / FRE-1366 — the concurrency controller re-homed process-wide.

Ticket ACs:

* AC-a: a cloud chat provider registered through the re-homed singleton
  enforces its ceiling on real ``LiteLLMClient.respond()`` calls — the N+1th
  call blocks until a slot frees, via the real acquire path (``request_slot``
  is never mocked; only the litellm transport is).
* AC-c: slot-wait telemetry (``inference_slot_acquired``) is emitted for a
  cloud chat call that actually waits.

AC-b (existing local priority-preemption controller tests stay green
unchanged) needs no new test here — it is verified by leaving
``tests/test_llm_client/test_concurrency.py`` untouched and green.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.llm_client import concurrency as concurrency_module
from personal_agent.llm_client.concurrency import (
    InferenceConcurrencyController,
    set_inference_concurrency_controller,
)
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import ModelConfig, Placement, ProviderDefinition
from personal_agent.llm_client.types import ModelRole
from personal_agent.security import DomainGuard
from tests._helpers.trace import make_test_ctx

# "anthropic" (not an arbitrary name) so the call rides a real, already-
# classified egress-guard route (D2.1) rather than needing a route-mechanism
# exception carved out just for this test.
PROVIDER = "anthropic"


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """The controller is process-global state — never let one test's leak into another."""
    set_inference_concurrency_controller(None)
    yield
    set_inference_concurrency_controller(None)


def _seeded_controller(ceiling: int, model_keys: list[str]) -> InferenceConcurrencyController:
    """A controller with the provider ceiling as the ONLY binding constraint.

    Each deployment key still needs its own ``register_model`` call —
    ``request_slot`` resolves a role's provider through that registration
    (``_model_provider``), not by name-matching a bare ``register_provider``
    call — but every deployment's own sub-limit is set to ``ceiling`` too, so
    it never itself binds before the shared provider semaphore does. That
    keeps the test proving the PROVIDER ceiling (AC-a), not a deployment one.
    """
    controller = InferenceConcurrencyController()
    controller.register_provider(PROVIDER, max_concurrency=ceiling)
    for key in model_keys:
        controller.register_model(role=key, provider=PROVIDER, max_concurrency=ceiling)
    return controller


def _permissive_guard() -> DomainGuard:
    """A DomainGuard that refuses nothing and never touches network or disk."""
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_fre1366_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


def _fake_response() -> SimpleNamespace:
    msg = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10)
    return SimpleNamespace(choices=[choice], usage=usage, id="resp_fre1366", model="m")


def _client(model_id: str) -> LiteLLMClient:
    return LiteLLMClient(
        model_id=model_id,
        provider=PROVIDER,
        max_tokens=128,
        budget_role="test_fre1366",
        egress_guard=_permissive_guard(),
    )


def _patched_environment() -> ExitStack:
    """Patch every respond()-cloud-path touchpoint EXCEPT the concurrency controller.

    The controller is deliberately left real — AC-a fails if the acquire path
    is mocked rather than exercised.
    """
    catalog = ModelConfig(
        providers={
            PROVIDER: ProviderDefinition(
                base_url=None, auth_env=None, placement=Placement.CLOUD, max_concurrency=50
            )
        },
        models={},
    )
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="reservation-fre1366")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()

    stack = ExitStack()
    stack.enter_context(patch("personal_agent.config.load_model_config", return_value=catalog))
    stack.enter_context(patch("personal_agent.cost_gate.get_default_gate", return_value=gate))
    stack.enter_context(
        patch("personal_agent.cost_gate.load_budget_config", return_value=MagicMock())
    )
    stack.enter_context(
        patch(
            "personal_agent.llm_client.cost_tracker.get_cost_tracker_service",
            return_value=tracker,
        )
    )
    stack.enter_context(
        patch(
            "personal_agent.llm_client.cost_estimator.estimate_reservation_for_call",
            return_value=Decimal("0.0"),
        )
    )
    stack.enter_context(
        patch("personal_agent.llm_client.litellm_client.litellm.completion_cost", return_value=0.0)
    )
    return stack


class TestCloudProviderCeilingEnforcedThroughTheSingleton:
    """AC-a."""

    @pytest.mark.asyncio
    async def test_third_call_blocks_until_a_slot_frees(self) -> None:
        ceiling = 2
        model_keys = [f"fre1366-model-{i}" for i in range(ceiling + 1)]
        set_inference_concurrency_controller(_seeded_controller(ceiling, model_keys))

        entered: list[str] = []
        release = asyncio.Event()

        async def _gated_acompletion(**kwargs: Any) -> SimpleNamespace:
            entered.append(kwargs["model"])
            await release.wait()
            return _fake_response()

        acompletion = AsyncMock(side_effect=_gated_acompletion)

        with _patched_environment():
            with patch(
                "personal_agent.llm_client.litellm_client.litellm.acompletion", new=acompletion
            ):
                clients = [_client(f"fre1366-model-{i}") for i in range(ceiling + 1)]
                tasks = [
                    asyncio.create_task(
                        c.respond(
                            role=ModelRole.PRIMARY,
                            messages=[{"role": "user", "content": "hi"}],
                            trace_ctx=make_test_ctx(f"fre1366_{i}"),
                        )
                    )
                    for i, c in enumerate(clients)
                ]

                # Let the system settle at the ceiling.
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if len(entered) >= ceiling:
                        break
                assert len(entered) == ceiling, (
                    f"expected exactly {ceiling} calls in flight at the provider "
                    f"ceiling, got {len(entered)}"
                )

                # Confirm nothing further leaks in while the ceiling holds —
                # this is what catches an acquire path that was skipped or
                # mocked out rather than genuinely blocking.
                await asyncio.sleep(0.1)
                assert len(entered) == ceiling, (
                    "the (N+1)th call must not reach litellm.acompletion before "
                    "a slot frees — the real acquire path did not block it"
                )

                release.set()
                await asyncio.gather(*tasks)

        assert len(entered) == ceiling + 1, "the (N+1)th call must proceed once a slot frees"
        assert acompletion.call_count == ceiling + 1


class TestSlotWaitTelemetry:
    """AC-c."""

    @pytest.mark.asyncio
    async def test_slot_wait_emits_inference_slot_acquired_with_expected_fields(self) -> None:
        ceiling = 1
        model_keys = ["fre1366-holder", "fre1366-waiter"]
        set_inference_concurrency_controller(_seeded_controller(ceiling, model_keys))

        entered: list[str] = []
        release = asyncio.Event()

        async def _gated_acompletion(**kwargs: Any) -> SimpleNamespace:
            entered.append(kwargs["model"])
            await release.wait()
            return _fake_response()

        acompletion = AsyncMock(side_effect=_gated_acompletion)

        events: list[tuple[str, dict[str, Any]]] = []
        real_info = concurrency_module.log.info

        def _record_info(event: str, **payload: Any) -> Any:
            events.append((event, payload))
            return real_info(event, **payload)

        trace_ctx = make_test_ctx("fre1366_wait")

        with _patched_environment():
            with (
                patch(
                    "personal_agent.llm_client.litellm_client.litellm.acompletion", new=acompletion
                ),
                patch.object(concurrency_module.log, "info", _record_info),
            ):
                holder = asyncio.create_task(
                    _client("fre1366-holder").respond(
                        role=ModelRole.PRIMARY,
                        messages=[{"role": "user", "content": "hi"}],
                        trace_ctx=make_test_ctx("fre1366_holder"),
                    )
                )
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if entered:
                        break
                assert entered, "holder never reached acompletion"

                waiter = asyncio.create_task(
                    _client("fre1366-waiter").respond(
                        role=ModelRole.PRIMARY,
                        messages=[{"role": "user", "content": "hi"}],
                        trace_ctx=trace_ctx,
                    )
                )
                # Force a real wait comfortably past the wait_ms > 100 log gate
                # (concurrency.py only logs inference_slot_acquired above it).
                await asyncio.sleep(0.15)
                release.set()
                await asyncio.gather(holder, waiter)

        (payload,) = [p for name, p in events if name == "inference_slot_acquired"]
        assert payload["provider"] == PROVIDER
        assert payload["priority"] is not None
        assert payload["wait_ms"] > 100
        assert payload["trace_id"] == trace_ctx.trace_id
