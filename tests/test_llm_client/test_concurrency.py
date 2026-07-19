"""Tests for InferenceConcurrencyController (ADR-0029)."""

import asyncio

import pytest

from personal_agent.llm_client.concurrency import (
    InferenceConcurrencyController,
    InferencePriority,
    InferenceSlotTimeout,
    _PrioritySemaphore,
)


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

        await asyncio.wait_for(asyncio.gather(bg_task, crit_task), timeout=3.0)
        assert order[0] == "critical", f"Expected critical first, got {order}"

    @pytest.mark.asyncio
    async def test_timeout_racing_release_does_not_leak_capacity(self) -> None:
        """A waiter that times out AFTER release() handed it the slot must return it.

        Regression for the race the provider-keyed rewrite put on the hot path:
        release() hands a freed permit to a waiter by popping its slot and setting
        its event WITHOUT decrementing `_active`, on the assumption the waiter will
        use it. If that waiter's `wait_for` timed out in the same window, it used to
        find its slot already gone, return False, and reclaim nothing — leaking the
        permit. Two such races pin `_active` at the limit and wedge every future
        acquire.

        The interleaving is forced deterministically: we hold `_lock` so the
        timed-out waiter's cleanup cannot run, perform release()'s exact handoff by
        hand, then drop the lock and let the cleanup observe the handed-off state.
        """
        sem = _PrioritySemaphore(1)
        assert await sem.acquire(InferencePriority.USER_FACING) is True  # holder, _active == 1

        w = asyncio.create_task(sem.acquire(InferencePriority.BACKGROUND, timeout=0.03))
        await asyncio.sleep(0)  # let w queue its slot and enter wait_for
        assert len(sem._waiters) == 1
        slot = sem._waiters[0]

        await sem._lock.acquire()
        await asyncio.sleep(0.05)  # w times out; its except-handler now blocks on _lock
        # The holder releases into w: pop + set event, _active deliberately unchanged.
        sem._waiters.remove(slot)
        slot.event.set()
        sem._lock.release()

        assert await w is False  # w still reports a timeout...
        # ...but the permit it was handed is returned, not leaked. Proven two ways:
        assert sem.active == 0
        assert await sem.acquire(InferencePriority.USER_FACING, timeout=0.1) is True


class TestInferenceConcurrencyController:
    """Test the main concurrency controller."""

    def _make_controller(self) -> InferenceConcurrencyController:
        ctrl = InferenceConcurrencyController(default_base_url="http://127.0.0.1:1234/v1")
        ctrl.register_provider("slm_local", max_concurrency=2)
        for role, limit in (("router", 4), ("reasoning", 1), ("standard", 2)):
            ctrl.register_model(
                role,
                max_concurrency=limit,
                endpoint="http://127.0.0.1:1234/v1",
                provider="slm_local",
            )
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
    async def test_provider_limit_enforced(self) -> None:
        """Two different deployments of one provider share that provider's ceiling."""
        ctrl = self._make_controller()

        async with ctrl.request_slot("router", InferencePriority.CRITICAL):
            async with ctrl.request_slot("standard", InferencePriority.USER_FACING):
                with pytest.raises(InferenceSlotTimeout):
                    async with ctrl.request_slot(
                        "reasoning", InferencePriority.BACKGROUND, timeout=0.05
                    ):
                        pass

    @pytest.mark.asyncio
    async def test_cloud_provider_ceiling_is_enforced_not_bypassed(self) -> None:
        """Cloud providers are no longer exempt from control (ADR-0121 D5).

        The controller used to skip semaphores entirely for anything it inferred
        as "cloud", so a declared cloud limit was dead config. Cloud ceilings are
        now real — set high enough to be a safety valve, but enforced.
        """
        ctrl = InferenceConcurrencyController(default_base_url="http://127.0.0.1:1234/v1")
        ctrl.register_provider("anthropic", max_concurrency=2)
        ctrl.register_model(
            "reasoning_cloud",
            max_concurrency=10,
            endpoint="https://api.anthropic.com/v1",
            provider="anthropic",
        )

        async with ctrl.request_slot("reasoning_cloud", InferencePriority.BACKGROUND):
            async with ctrl.request_slot("reasoning_cloud", InferencePriority.BACKGROUND):
                with pytest.raises(InferenceSlotTimeout):
                    async with ctrl.request_slot(
                        "reasoning_cloud", InferencePriority.DEFERRED, timeout=0.05
                    ):
                        pass

    @pytest.mark.asyncio
    async def test_unregistered_model_passes_through(self) -> None:
        ctrl = InferenceConcurrencyController()
        async with ctrl.request_slot("unknown_model", InferencePriority.USER_FACING):
            pass

    @pytest.mark.asyncio
    async def test_unattributed_deployment_gets_a_private_pool(self) -> None:
        """A deployment registered without a provider is bounded, not unbounded.

        Previously an unrecognised endpoint fell back to an inferred type; now it
        gets its own private pool so a missing `provider:` can never mean "no limit".
        """
        ctrl = InferenceConcurrencyController()
        ctrl.register_model(
            "orphan", max_concurrency=1, endpoint="https://somewhere.example.com/v1"
        )
        assert ctrl._model_provider["orphan"] == "_unattributed:orphan"

        async with ctrl.request_slot("orphan", InferencePriority.USER_FACING):
            with pytest.raises(InferenceSlotTimeout):
                async with ctrl.request_slot("orphan", InferencePriority.BACKGROUND, timeout=0.05):
                    pass

    @pytest.mark.asyncio
    async def test_get_status(self) -> None:
        ctrl = self._make_controller()
        status = ctrl.get_status()
        assert "models" in status
        assert "providers" in status
        assert "router" in status["models"]
        assert status["models"]["router"]["limit"] == 4
        assert status["models"]["reasoning"]["limit"] == 1

    @pytest.mark.asyncio
    async def test_priority_ordering_across_models(self) -> None:
        """User-facing request should be served before background when both wait."""
        ctrl = InferenceConcurrencyController(default_base_url="http://127.0.0.1:1234/v1")
        ctrl.register_provider("slm_local", max_concurrency=1)
        ctrl.register_model(
            "reasoning",
            max_concurrency=2,
            endpoint="http://127.0.0.1:1234/v1",
            provider="slm_local",
        )
        ctrl.register_model(
            "standard", max_concurrency=2, endpoint="http://127.0.0.1:1234/v1", provider="slm_local"
        )

        order: list[str] = []

        async with ctrl.request_slot("reasoning", InferencePriority.CRITICAL):

            async def bg_request():
                async with ctrl.request_slot(
                    "reasoning", InferencePriority.BACKGROUND, timeout=3.0
                ):
                    order.append("background")

            async def user_request():
                await asyncio.sleep(0.02)
                async with ctrl.request_slot(
                    "standard", InferencePriority.USER_FACING, timeout=3.0
                ):
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


class TestProviderKeyedConcurrency:
    """AC-3 (ADR-0121) — the ceiling is enforced at the PROVIDER, across deployments.

    The AC's original *Fails if* clause claimed four concurrent calls across two
    slm_local deployments run unbounded today. That was false: the pre-ADR-0121
    controller keyed its outer semaphore on the normalised *endpoint*, and the two
    SLM deployments share one, so they were already capped together. Master
    corrected the AC on 2026-07-19.

    The real change is that the ceiling becomes an explicit, configurable provider
    property instead of being inferred from the endpoint URL string. So a test at
    the default limit passes on the OLD behaviour and proves nothing — these use a
    deliberately non-default ceiling.
    """

    _CEILING = 3  # non-default on purpose; the old default_endpoint_limit was 2

    @staticmethod
    def _controller(
        *,
        provider_ceiling: int,
        deployment_limit: int,
        endpoints: tuple[str | None, str | None] = (None, None),
        providers: tuple[str, str] = ("slm_local", "slm_local"),
    ) -> InferenceConcurrencyController:
        ctrl = InferenceConcurrencyController(default_base_url="https://slm.example.com/v1")
        for provider in dict.fromkeys(providers):
            ctrl.register_provider(provider, max_concurrency=provider_ceiling)
        for key, provider, endpoint in zip(
            ("qwen3.6-35b-thinking", "qwen3.6-35b-instruct"), providers, endpoints, strict=True
        ):
            ctrl.register_model(
                key, provider=provider, max_concurrency=deployment_limit, endpoint=endpoint
            )
        return ctrl

    @staticmethod
    async def _drive(
        ctrl: InferenceConcurrencyController, roles: list[str], *, expect_in_flight: int
    ) -> int:
        """Hold every acquirable slot open at once and return the observed peak.

        Peak is measured INSIDE the acquired slot under a lock, never by polling
        ``get_status()`` from outside: ``_PrioritySemaphore.active`` is read
        without the lock that guards its mutation, so an external poll can miss
        the transient peak or observe it after releases have begun.
        """
        in_flight = 0
        peak = 0
        lock = asyncio.Lock()
        release = asyncio.Event()

        async def worker(role: str) -> None:
            nonlocal in_flight, peak
            async with ctrl.request_slot(role, InferencePriority.USER_FACING):
                async with lock:
                    in_flight += 1
                    peak = max(peak, in_flight)
                await release.wait()
                async with lock:
                    in_flight -= 1

        tasks = [asyncio.create_task(worker(role)) for role in roles]
        try:
            # Wait for the system to settle at its ceiling rather than sleeping a
            # fixed interval and hoping.
            for _ in range(200):
                await asyncio.sleep(0.01)
                async with lock:
                    if in_flight >= expect_in_flight:
                        break
            # Then confirm nothing FURTHER leaks in — this is what catches a
            # per-deployment-only implementation, which would keep climbing.
            await asyncio.sleep(0.05)
            async with lock:
                observed = peak
        finally:
            release.set()
            await asyncio.gather(*tasks)
        return observed

    @pytest.mark.asyncio
    async def test_provider_ceiling_caps_across_two_deployments(self) -> None:
        """Six calls over two deployments of one provider never exceed the provider ceiling."""
        ctrl = self._controller(provider_ceiling=self._CEILING, deployment_limit=self._CEILING)
        roles = ["qwen3.6-35b-thinking"] * 3 + ["qwen3.6-35b-instruct"] * 3

        peak = await self._drive(ctrl, roles, expect_in_flight=self._CEILING)

        assert peak == self._CEILING, (
            f"provider ceiling {self._CEILING} not enforced across deployments — peak {peak}. "
            "A per-deployment-only implementation reaches 6."
        )

    @pytest.mark.asyncio
    async def test_deployment_sub_limit_below_provider_ceiling_is_respected(self) -> None:
        """A deployment's own limit still bounds it under a roomier provider ceiling."""
        ctrl = self._controller(provider_ceiling=10, deployment_limit=2)
        roles = ["qwen3.6-35b-thinking"] * 5

        peak = await self._drive(ctrl, roles, expect_in_flight=2)

        assert peak == 2

    @pytest.mark.asyncio
    async def test_same_provider_different_endpoints_share_one_cap(self) -> None:
        """The new semantics: capacity follows the provider, not the URL."""
        ctrl = self._controller(
            provider_ceiling=2,
            deployment_limit=2,
            endpoints=("https://slm.example.com/v1", "https://other.example.com/v1"),
        )
        roles = ["qwen3.6-35b-thinking"] * 2 + ["qwen3.6-35b-instruct"] * 2

        peak = await self._drive(ctrl, roles, expect_in_flight=2)

        assert peak == 2

    @pytest.mark.asyncio
    async def test_different_providers_sharing_an_endpoint_do_not_share_a_cap(self) -> None:
        """The old endpoint-keyed semantics, asserted gone."""
        ctrl = self._controller(
            provider_ceiling=1,
            deployment_limit=1,
            endpoints=("https://shared.example.com/v1", "https://shared.example.com/v1"),
            providers=("slm_local", "other_provider"),
        )
        roles = ["qwen3.6-35b-thinking", "qwen3.6-35b-instruct"]

        peak = await self._drive(ctrl, roles, expect_in_flight=2)

        assert peak == 2, "two providers on one endpoint must not share a semaphore"
