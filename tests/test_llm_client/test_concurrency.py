"""Tests for InferenceConcurrencyController (ADR-0029)."""

import asyncio

import pytest

from personal_agent.llm_client.concurrency import (
    InferenceConcurrencyController,
    InferencePriority,
    InferenceSlotTimeout,
    ProviderType,
    _PrioritySemaphore,
    infer_provider_type,
)


class TestProviderTypeInference:
    """Test auto-detection of provider type from endpoint URLs."""

    def test_localhost_is_local(self) -> None:
        assert infer_provider_type("http://localhost:1234/v1") == ProviderType.LOCAL

    def test_127_is_local(self) -> None:
        assert infer_provider_type("http://127.0.0.1:1234/v1") == ProviderType.LOCAL

    def test_none_is_local(self) -> None:
        assert infer_provider_type(None) == ProviderType.LOCAL

    def test_anthropic_is_cloud(self) -> None:
        assert infer_provider_type("https://api.anthropic.com/v1") == ProviderType.CLOUD

    def test_openai_is_cloud(self) -> None:
        assert infer_provider_type("https://api.openai.com/v1") == ProviderType.CLOUD

    def test_internal_http_is_managed(self) -> None:
        assert infer_provider_type("http://gpu-cluster:8080/v1") == ProviderType.MANAGED


class TestPrioritySemaphore:
    """Test the priority-aware semaphore."""

    @pytest.mark.asyncio
    async def test_acquire_within_limit(self) -> None:
        sem = _PrioritySemaphore(2)
        assert await sem.acquire(InferencePriority.USER_FACING) is True
        assert sem.active == 1
        assert await sem.acquire(InferencePriority.USER_FACING) is True
        assert sem.active == 2

    @pytest.mark.asyncio
    async def test_acquire_blocks_at_limit(self) -> None:
        sem = _PrioritySemaphore(1)
        assert await sem.acquire(InferencePriority.USER_FACING) is True

        acquired = await sem.acquire(InferencePriority.BACKGROUND, timeout=0.05)
        assert acquired is False

    @pytest.mark.asyncio
    async def test_release_wakes_waiter(self) -> None:
        sem = _PrioritySemaphore(1)
        assert await sem.acquire(InferencePriority.USER_FACING) is True

        woke_up = asyncio.Event()

        async def waiter():
            result = await sem.acquire(InferencePriority.BACKGROUND, timeout=2.0)
            if result:
                woke_up.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        await sem.release()
        await asyncio.wait_for(woke_up.wait(), timeout=2.0)
        assert woke_up.is_set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_higher_priority_served_first(self) -> None:
        sem = _PrioritySemaphore(1)
        assert await sem.acquire(InferencePriority.USER_FACING) is True

        order: list[str] = []

        async def bg_waiter():
            await sem.acquire(InferencePriority.BACKGROUND, timeout=2.0)
            order.append("background")
            await sem.release()

        async def critical_waiter():
            await asyncio.sleep(0.02)
            await sem.acquire(InferencePriority.CRITICAL, timeout=2.0)
            order.append("critical")
            await sem.release()

        bg_task = asyncio.create_task(bg_waiter())
        crit_task = asyncio.create_task(critical_waiter())

        await asyncio.sleep(0.05)
        await sem.release()

        await asyncio.wait_for(
            asyncio.gather(bg_task, crit_task), timeout=3.0
        )
        assert order[0] == "critical", f"Expected critical first, got {order}"


class TestInferenceConcurrencyController:
    """Test the main concurrency controller."""

    def _make_controller(self) -> InferenceConcurrencyController:
        ctrl = InferenceConcurrencyController(
            default_base_url="http://127.0.0.1:1234/v1",
            default_endpoint_limit=2,
        )
        ctrl.register_model("router", max_concurrency=4, endpoint="http://127.0.0.1:1234/v1")
        ctrl.register_model("reasoning", max_concurrency=1, endpoint="http://127.0.0.1:1234/v1")
        ctrl.register_model("standard", max_concurrency=2, endpoint="http://127.0.0.1:1234/v1")
        return ctrl

    @pytest.mark.asyncio
    async def test_basic_acquire_release(self) -> None:
        ctrl = self._make_controller()
        async with ctrl.request_slot("reasoning", InferencePriority.USER_FACING):
            status = ctrl.get_status()
            assert status["models"]["reasoning"]["active"] == 1

        status = ctrl.get_status()
        assert status["models"]["reasoning"]["active"] == 0

    @pytest.mark.asyncio
    async def test_model_limit_enforced(self) -> None:
        ctrl = self._make_controller()

        async with ctrl.request_slot("reasoning", InferencePriority.USER_FACING):
            with pytest.raises(InferenceSlotTimeout):
                async with ctrl.request_slot(
                    "reasoning", InferencePriority.BACKGROUND, timeout=0.05
                ):
                    pass

    @pytest.mark.asyncio
    async def test_endpoint_limit_enforced(self) -> None:
        """Two different models sharing the same endpoint should share the endpoint semaphore."""
        ctrl = self._make_controller()

        async with ctrl.request_slot("router", InferencePriority.CRITICAL):
            async with ctrl.request_slot("standard", InferencePriority.USER_FACING):
                with pytest.raises(InferenceSlotTimeout):
                    async with ctrl.request_slot(
                        "reasoning", InferencePriority.BACKGROUND, timeout=0.05
                    ):
                        pass

    @pytest.mark.asyncio
    async def test_cloud_provider_no_blocking(self) -> None:
        ctrl = InferenceConcurrencyController(default_base_url="http://127.0.0.1:1234/v1")
        ctrl.register_model(
            "reasoning_cloud",
            max_concurrency=10,
            endpoint="https://api.anthropic.com/v1",
            provider_type="cloud",
        )

        slots_acquired = 0
        async with ctrl.request_slot("reasoning_cloud", InferencePriority.BACKGROUND):
            slots_acquired += 1
            async with ctrl.request_slot("reasoning_cloud", InferencePriority.BACKGROUND):
                slots_acquired += 1
                async with ctrl.request_slot("reasoning_cloud", InferencePriority.DEFERRED):
                    slots_acquired += 1

        assert slots_acquired == 3

    @pytest.mark.asyncio
    async def test_unregistered_model_passes_through(self) -> None:
        ctrl = InferenceConcurrencyController()
        async with ctrl.request_slot("unknown_model", InferencePriority.USER_FACING):
            pass

    @pytest.mark.asyncio
    async def test_explicit_provider_type_overrides_auto(self) -> None:
        ctrl = InferenceConcurrencyController()
        ctrl.register_model(
            "local_override",
            max_concurrency=1,
            endpoint="https://my-gpu-server.example.com/v1",
            provider_type="local",
        )
        assert ctrl._model_provider_type["local_override"] == "local"

        async with ctrl.request_slot("local_override", InferencePriority.USER_FACING):
            with pytest.raises(InferenceSlotTimeout):
                async with ctrl.request_slot(
                    "local_override", InferencePriority.BACKGROUND, timeout=0.05
                ):
                    pass

    @pytest.mark.asyncio
    async def test_get_status(self) -> None:
        ctrl = self._make_controller()
        status = ctrl.get_status()
        assert "models" in status
        assert "endpoints" in status
        assert "router" in status["models"]
        assert status["models"]["router"]["limit"] == 4
        assert status["models"]["reasoning"]["limit"] == 1

    @pytest.mark.asyncio
    async def test_priority_ordering_across_models(self) -> None:
        """User-facing request should be served before background when both wait."""
        ctrl = InferenceConcurrencyController(
            default_base_url="http://127.0.0.1:1234/v1",
            default_endpoint_limit=1,
        )
        ctrl.register_model("reasoning", max_concurrency=2, endpoint="http://127.0.0.1:1234/v1")
        ctrl.register_model("standard", max_concurrency=2, endpoint="http://127.0.0.1:1234/v1")

        order: list[str] = []

        async with ctrl.request_slot("reasoning", InferencePriority.CRITICAL):

            async def bg_request():
                async with ctrl.request_slot("reasoning", InferencePriority.BACKGROUND, timeout=3.0):
                    order.append("background")

            async def user_request():
                await asyncio.sleep(0.02)
                async with ctrl.request_slot("standard", InferencePriority.USER_FACING, timeout=3.0):
                    order.append("user_facing")

            bg_task = asyncio.create_task(bg_request())
            user_task = asyncio.create_task(user_request())

            await asyncio.sleep(0.05)

        await asyncio.wait_for(asyncio.gather(bg_task, user_task), timeout=3.0)
        assert order[0] == "user_facing", f"Expected user_facing first, got {order}"

    @pytest.mark.asyncio
    async def test_slot_released_on_exception(self) -> None:
        """Concurrency slot must be released even if the wrapped code raises."""
        ctrl = self._make_controller()

        with pytest.raises(RuntimeError, match="boom"):
            async with ctrl.request_slot("reasoning", InferencePriority.USER_FACING):
                raise RuntimeError("boom")

        status = ctrl.get_status()
        assert status["models"]["reasoning"]["active"] == 0


class TestInferencePriority:
    """Test priority enum ordering."""

    def test_critical_less_than_user_facing(self) -> None:
        assert InferencePriority.CRITICAL < InferencePriority.USER_FACING

    def test_user_facing_less_than_background(self) -> None:
        assert InferencePriority.USER_FACING < InferencePriority.BACKGROUND

    def test_background_less_than_deferred(self) -> None:
        assert InferencePriority.BACKGROUND < InferencePriority.DEFERRED

    def test_ordering_chain(self) -> None:
        assert (
            InferencePriority.CRITICAL
            < InferencePriority.USER_FACING
            < InferencePriority.ELEVATED
            < InferencePriority.BACKGROUND
            < InferencePriority.DEFERRED
        )
