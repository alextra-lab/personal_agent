"""ADR-0145 D7 — the role carries an InferencePriority read from its binding.

Ticket: FRE-1449.

* AC-4 — priority orders acquisition under real contention: with the shared
  local semaphore saturated, a ``primary`` request queued behind a
  ``sub_agent`` request acquires first.
* AC-5 — the binding is what decides: changing ``sub_agent``'s binding
  priority (not the client, not the controller input) changes AC-4's outcome.
* AC-6 — ``resolve_role_target``'s signature is unchanged: this ticket reads
  ``binding.priority`` directly off ``config.roles``, never through that
  resolver's return.

AC-1/2/3 (the concurrency-widening before/after measurement) live in
``tests/test_llm_client/test_concurrency.py``, alongside the other
``InferenceConcurrencyController``-level tests they belong with.
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

from personal_agent.config import load_model_config
from personal_agent.config.model_loader import resolve_role_target
from personal_agent.llm_client.concurrency import (
    InferenceConcurrencyController,
    get_inference_concurrency_controller,
    set_inference_concurrency_controller,
)
from personal_agent.llm_client.factory import get_llm_client
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
from personal_agent.llm_client.priority import InferencePriority
from personal_agent.llm_client.types import ModelRole
from personal_agent.security import DomainGuard
from tests._helpers.trace import make_test_ctx

PROVIDER = "anthropic"
MODEL_KEY = "fre1449-shared"


@pytest.fixture(autouse=True)
def _reset_singleton() -> Iterator[None]:
    """The controller is process-global state — never let one test's leak into another."""
    set_inference_concurrency_controller(None)
    yield
    set_inference_concurrency_controller(None)


class TestRoleBindingPriorityField:
    """The field itself: default, YAML name coercion, and rejection of an unknown name."""

    def test_default_is_user_facing(self) -> None:
        """No `priority:` given — the field's own default applies."""
        binding = RoleBinding(deployment="some-key")
        assert binding.priority is InferencePriority.USER_FACING

    def test_accepts_a_yaml_enum_name_string(self) -> None:
        """A YAML-shaped enum-name string coerces to the matching member."""
        binding = RoleBinding(deployment="some-key", priority="ELEVATED")  # type: ignore[arg-type]
        assert binding.priority is InferencePriority.ELEVATED

    def test_accepts_the_enum_member_directly(self) -> None:
        """A Python caller may still pass the enum member itself."""
        binding = RoleBinding(deployment="some-key", priority=InferencePriority.BACKGROUND)
        assert binding.priority is InferencePriority.BACKGROUND

    def test_unknown_name_raises_value_error(self) -> None:
        """An unknown name is a validation error, not a raw `KeyError`."""
        with pytest.raises(ValueError, match="unknown InferencePriority name"):
            RoleBinding(deployment="some-key", priority="NOT_A_REAL_TIER")  # type: ignore[arg-type]


class TestRealCatalogPriority:
    """The real repo's `config/model_roles.yaml`, loaded straight."""

    def test_sub_agent_binding_is_elevated(self) -> None:
        """sub_agent's binding declares `priority: ELEVATED` (ADR-0145 D7)."""
        config = load_model_config()
        assert config.roles["sub_agent"].priority is InferencePriority.ELEVATED

    def test_primary_binding_defaults_to_user_facing(self) -> None:
        """Primary declares no `priority:` override — the field's own default applies."""
        config = load_model_config()
        assert config.roles["primary"].priority is InferencePriority.USER_FACING


class TestFactoryWiresBindingPriorityIntoTheClient:
    """`get_llm_client` reads `config.roles[role_name].priority`, not a hard-coded default."""

    def test_sub_agent_client_carries_elevated_default_priority(self) -> None:
        """sub_agent's binding declares `priority: ELEVATED`."""
        client = get_llm_client(role_name=ModelRole.SUB_AGENT.value)
        assert client.default_priority is InferencePriority.ELEVATED

    def test_primary_client_carries_user_facing_default_priority(self) -> None:
        """Primary declares no override — the field's own default applies."""
        client = get_llm_client(role_name="primary")
        assert client.default_priority is InferencePriority.USER_FACING


def _permissive_guard() -> DomainGuard:
    """A DomainGuard that refuses nothing and never touches network or disk."""
    guard = DomainGuard(cache_path=Path("telemetry/security/_unused_fre1449_blocklist.json"))
    guard._blocklist = frozenset()
    guard._last_loaded = datetime.now(timezone.utc)
    return guard


class TestRespondPriorityFallback:
    """`respond()`'s own priority fallback.

    An omitted priority defers to `self.default_priority`; an explicit one still
    overrides it — the existing BACKGROUND-priority callers (captains_log/
    reflection.py etc.) must see no behaviour change.
    """

    @staticmethod
    def _client(default_priority: InferencePriority) -> LiteLLMClient:
        return LiteLLMClient(
            model_id="fre1449-fallback",
            model_key=MODEL_KEY,
            provider=PROVIDER,
            max_tokens=128,
            budget_role="test_fre1449",
            egress_guard=_permissive_guard(),
            default_priority=default_priority,
        )

    @staticmethod
    async def _respond_and_capture_request_slot_kwargs(
        client: LiteLLMClient, **respond_kwargs: Any
    ) -> dict[str, Any]:
        """Call `respond()` for real; return the kwargs `request_slot` was called with."""
        seen: dict[str, Any] = {}

        class _FakeSlot:
            """A no-op async context manager standing in for a real concurrency slot."""

            async def __aenter__(self) -> None:
                """Enter the fake slot; nothing to acquire."""
                return None

            async def __aexit__(self, *exc: Any) -> None:
                """Exit the fake slot; nothing to release."""
                return None

        def _request_slot(**kwargs: Any) -> _FakeSlot:
            seen.update(kwargs)
            return _FakeSlot()

        controller = MagicMock()
        controller.request_slot = _request_slot
        acompletion = AsyncMock(return_value=_fake_response())
        catalog = ModelConfig(
            providers={
                PROVIDER: ProviderDefinition(
                    base_url=None, auth_env=None, placement=Placement.CLOUD, max_concurrency=50
                )
            },
            models={},
        )

        with (
            patch("personal_agent.config.load_model_config", return_value=catalog),
            patch(
                "personal_agent.llm_client.litellm_client.get_inference_concurrency_controller",
                return_value=controller,
            ),
            patch("personal_agent.llm_client.litellm_client.litellm.acompletion", acompletion),
            patch(
                "personal_agent.llm_client.litellm_client.litellm.completion_cost",
                return_value=0.0,
            ),
            _cost_gate_patches(),
        ):
            await client.respond(
                role=ModelRole.SUB_AGENT,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("fre1449_fallback"),
                **respond_kwargs,
            )

        return seen

    @pytest.mark.asyncio
    async def test_omitted_priority_uses_the_clients_default(self) -> None:
        """No `priority=` kwarg — `request_slot` still sees the client's own rank."""
        client = self._client(InferencePriority.ELEVATED)
        seen = await self._respond_and_capture_request_slot_kwargs(client)
        assert seen["priority"] is InferencePriority.ELEVATED

    @pytest.mark.asyncio
    async def test_explicit_priority_still_overrides_the_default(self) -> None:
        """Regression proof.

        The existing BACKGROUND-priority callers (captains_log/reflection.py etc.)
        keep seeing exactly what they pass.
        """
        client = self._client(InferencePriority.ELEVATED)
        seen = await self._respond_and_capture_request_slot_kwargs(
            client, priority=InferencePriority.BACKGROUND
        )
        assert seen["priority"] is InferencePriority.BACKGROUND


def _fake_response() -> SimpleNamespace:
    msg = SimpleNamespace(content="ok", tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=5, total_tokens=10)
    return SimpleNamespace(choices=[choice], usage=usage, id="resp_fre1449", model="m")


def _cost_gate_patches() -> ExitStack:
    """Patch cost-gate reserve/commit/refund and cost-tracker recording.

    Real I/O this suite has no business doing.
    """
    gate = MagicMock()
    gate.reserve = AsyncMock(return_value="reservation-fre1449")
    gate.commit = AsyncMock()
    gate.refund = AsyncMock()
    tracker = AsyncMock()
    tracker.connect = AsyncMock()
    tracker.record_api_call = AsyncMock()

    stack = ExitStack()
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
    return stack


def _synthetic_config(sub_agent_priority: InferencePriority) -> ModelConfig:
    """A minimal catalog where `primary` and `sub_agent` bind to ONE shared deployment.

    Mirrors production's real shape: `sub_agent`'s `deployment: inherit` resolves to
    primary's own deployment, so both roles genuinely contend on one physical
    semaphore — this is exactly why D7 matters. Kept synthetic (a literal shared key,
    not `inherit`) so the test isolates priority-ordering from selection/inherit
    resolution, which is exercised elsewhere (`test_factory_sub_agent.py`).
    """
    return ModelConfig(
        providers={
            PROVIDER: ProviderDefinition(
                base_url=None, auth_env=None, placement=Placement.CLOUD, max_concurrency=50
            )
        },
        models={
            MODEL_KEY: ModelDefinition(
                id="fre1449-shared-model",
                provider=PROVIDER,
                context_length=8000,
                max_concurrency=1,
                default_timeout=30,
                dialect=Dialect.ANTHROPIC_ADAPTIVE,
                modes={"default": ModeSpec()},
                default_mode="default",
            ),
        },
        roles={
            "primary": RoleBinding(deployment=MODEL_KEY, open=True),
            "sub_agent": RoleBinding(deployment=MODEL_KEY, open=True, priority=sub_agent_priority),
        },
    )


def _seeded_controller() -> InferenceConcurrencyController:
    ctrl = InferenceConcurrencyController()
    ctrl.register_provider(PROVIDER, max_concurrency=50)
    ctrl.register_model(MODEL_KEY, provider=PROVIDER, max_concurrency=1)
    return ctrl


async def _drive_contention_through_get_llm_client(
    sub_agent_priority: InferencePriority,
) -> list[str]:
    """Saturate the one shared slot; queue sub_agent first, primary second; release; report order.

    Acquisition order is read from entry into the mocked transport (inside the acquired
    slot), never from the enum value either call site passed (neither passes one).

    Both clients are built through the real `get_llm_client()` door, off a `ModelConfig`
    whose `sub_agent` binding carries `sub_agent_priority` — proving the BINDING decides
    the outcome (AC-5), not a hand-set client attribute.
    """
    config = _synthetic_config(sub_agent_priority)
    set_inference_concurrency_controller(_seeded_controller())

    entered: list[str] = []
    release = asyncio.Event()

    async def _gated_acompletion(**kwargs: Any) -> SimpleNamespace:
        entered.append(kwargs["model"])
        await release.wait()
        return _fake_response()

    acompletion = AsyncMock(side_effect=_gated_acompletion)

    with (
        # `get_llm_client()` (factory.py) and `respond()`'s own deferred provider
        # lookup (litellm_client.py) each resolve `load_model_config` through a
        # different binding — both must return the same synthetic catalog, or
        # `respond()` falls through to the real one and demands real credentials.
        patch("personal_agent.llm_client.factory.load_model_config", return_value=config),
        patch("personal_agent.config.load_model_config", return_value=config),
        patch("personal_agent.llm_client.litellm_client.litellm.acompletion", acompletion),
        patch("personal_agent.llm_client.litellm_client.litellm.completion_cost", return_value=0.0),
        _cost_gate_patches(),
    ):
        holder = get_llm_client(role_name="primary")
        sub = get_llm_client(role_name="sub_agent")
        primary = get_llm_client(role_name="primary")

        # These three clients share model_key/deployment — override model_id per
        # instance so `entered` can tell them apart on the wire.
        holder.model_id = "holder"
        holder._litellm_model = f"{PROVIDER}/holder"
        sub.model_id = "sub"
        sub._litellm_model = f"{PROVIDER}/sub"
        primary.model_id = "primary"
        primary._litellm_model = f"{PROVIDER}/primary"

        holder_task = asyncio.create_task(
            holder.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("fre1449_holder"),
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if entered:
                break
        assert entered == [f"{PROVIDER}/holder"], "holder never occupied the only slot"

        live_ctrl = get_inference_concurrency_controller()
        sem = live_ctrl._model_semaphores[MODEL_KEY]

        sub_task = asyncio.create_task(
            sub.respond(
                role=ModelRole.SUB_AGENT,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("fre1449_sub"),
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(sem._waiters) == 1:
                break
        assert len(sem._waiters) == 1, "sub_agent request never queued before primary started"

        primary_task = asyncio.create_task(
            primary.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
                trace_ctx=make_test_ctx("fre1449_primary"),
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(sem._waiters) == 2:
                break
        assert len(sem._waiters) == 2, "primary request never queued behind sub_agent"

        release.set()
        await asyncio.gather(holder_task, sub_task, primary_task)

    return entered


class TestPriorityOrdersRealContentionAndTheBindingDecides:
    """AC-4 and AC-5, as one paired scenario with the binding value flipped."""

    @pytest.mark.asyncio
    async def test_ac4_primary_overtakes_a_queued_sub_agent_request(self) -> None:
        """sub_agent (ELEVATED) queued first; primary (USER_FACING) still wins.

        Queued second, primary still acquires first — priority orders
        acquisition, not arrival.
        """
        entered = await _drive_contention_through_get_llm_client(InferencePriority.ELEVATED)

        assert entered == [f"{PROVIDER}/holder", f"{PROVIDER}/primary", f"{PROVIDER}/sub"], (
            f"expected primary to overtake the earlier-queued sub_agent request, got {entered}"
        )

    @pytest.mark.asyncio
    async def test_ac5_binding_set_equal_to_primary_restores_fifo(self) -> None:
        """Change ONLY the binding — sub_agent's priority to USER_FACING.

        Equal to primary's. AC-4's outcome must move: ordering reverts to
        arrival order.
        """
        entered = await _drive_contention_through_get_llm_client(InferencePriority.USER_FACING)

        assert entered == [f"{PROVIDER}/holder", f"{PROVIDER}/sub", f"{PROVIDER}/primary"], (
            f"expected FIFO once sub_agent's binding priority equals primary's, got {entered}"
        )


class TestResolveRoleTargetSignatureUnchanged:
    """AC-6 — resolve_role_target's return is untouched.

    This ticket reads `binding.priority` off `config.roles` directly; it never
    widens `resolve_role_target`'s return.
    """

    def test_still_returns_a_two_tuple(self) -> None:
        """A cheap regression guard — the discipline itself is enforced by construction."""
        result = resolve_role_target("primary")
        assert len(result) == 2
        key, definition = result
        assert isinstance(key, str)
        assert definition is not None
