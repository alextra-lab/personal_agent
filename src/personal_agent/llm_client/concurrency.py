"""Inference concurrency controller, keyed on provider (ADR-0029; ADR-0121 Layer 1).

Implements per-provider and per-deployment concurrency limits with priority-based
scheduling. A provider owns a total in-flight ceiling shared by all of its
deployments; each deployment may declare a smaller sub-limit beneath it.

**Re-keyed by FRE-916 phase 2.** The outer semaphore used to be keyed on the
normalised *endpoint URL*, with the provider "type" inferred by string-matching
that URL (``infer_provider_type``), and cloud endpoints bypassing control
entirely. Capacity now follows the declared provider, because that is what the
capacity actually belongs to: the owner's GPU is scarce regardless of which URL
reaches it, and two providers behind one hostname are not one pool.

This is a deliberate semantic change, not a refactor. For the deployed catalog
the two coincide — the SLM deployments share one provider *and* one endpoint —
so live behaviour is preserved, but the general case differs in both directions
and is asserted as such in the tests.

Cloud providers are no longer exempt: they carry explicit ceilings too, set high
enough to act as a safety valve rather than a throttle. Placement (local vs
cloud) now decides only *dispatch* — which client class handles the call — which
is `ModelConfig.placement_of`'s job, not this module's.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import AsyncIterator

from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class InferencePriority(IntEnum):
    """Priority tiers for inference requests.

    Lower numeric value = higher priority. When a semaphore slot opens,
    the highest-priority (lowest value) waiting request proceeds first.
    """

    CRITICAL = 0
    USER_FACING = 1
    ELEVATED = 2
    BACKGROUND = 3
    DEFERRED = 4


class _PrioritySlot:
    """A waiter in the priority queue with ordering support."""

    __slots__ = ("priority", "timestamp", "event")

    def __init__(self, priority: InferencePriority) -> None:
        self.priority = priority
        self.timestamp = time.monotonic()
        self.event = asyncio.Event()

    def __lt__(self, other: _PrioritySlot) -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp


class _PrioritySemaphore:
    """Semaphore with priority-based FIFO ordering.

    When a slot is released, the highest-priority (lowest IntEnum value)
    waiter is woken first. Within the same priority, FIFO ordering applies.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._waiters: list[_PrioritySlot] = []
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._active

    @property
    def limit(self) -> int:
        return self._limit

    async def acquire(self, priority: InferencePriority, timeout: float | None = None) -> bool:
        """Acquire a slot, waiting if necessary.

        Args:
            priority: Request priority.
            timeout: Maximum seconds to wait. None means wait forever.

        Returns:
            True if acquired, False if timed out.
        """
        async with self._lock:
            if self._active < self._limit:
                self._active += 1
                return True

            slot = _PrioritySlot(priority)
            self._waiters.append(slot)
            self._waiters.sort()

        try:
            if timeout is not None:
                await asyncio.wait_for(slot.event.wait(), timeout=timeout)
            else:
                await slot.event.wait()
            return True
        except asyncio.TimeoutError:
            async with self._lock:
                if slot in self._waiters:
                    self._waiters.remove(slot)
            return False

    async def release(self) -> None:
        """Release a slot and wake the highest-priority waiter."""
        async with self._lock:
            if self._waiters:
                next_slot = self._waiters.pop(0)
                next_slot.event.set()
            else:
                self._active -= 1


class InferenceConcurrencyController:
    """Manages concurrent access to inference providers.

    Enforces a per-provider total ceiling with optional per-deployment sub-limits,
    both priority-scheduled. A deployment acquires its provider's slot first, then
    its own — so the provider ceiling is the binding constraint across every
    deployment it serves.

    Args:
        default_base_url: Default base URL, retained for log context on
            deployments that declare no endpoint of their own.
        default_provider_limit: Ceiling applied to a provider referenced by a
            deployment but never explicitly registered. A fallback for tests and
            partial fixtures — the real catalog declares every provider.
    """

    def __init__(
        self,
        default_base_url: str = "http://127.0.0.1:1234/v1",
        default_provider_limit: int = 2,
    ) -> None:
        """Initialize the controller with a default URL and fallback provider ceiling."""
        self._default_base_url = default_base_url
        self._default_provider_limit = default_provider_limit

        self._provider_semaphores: dict[str, _PrioritySemaphore] = {}
        self._model_semaphores: dict[str, _PrioritySemaphore] = {}
        self._model_provider: dict[str, str] = {}
        self._model_endpoint: dict[str, str] = {}

    def register_provider(self, provider: str, max_concurrency: int) -> None:
        """Register a provider's total in-flight ceiling.

        Idempotent: re-registering an already-known provider leaves its existing
        semaphore in place, so a mid-flight limit is never silently reset.

        Args:
            provider: Provider name — a key in the catalog's ``providers:`` mapping.
            max_concurrency: Total in-flight requests permitted across all of this
                provider's deployments.
        """
        if provider in self._provider_semaphores:
            return
        self._provider_semaphores[provider] = _PrioritySemaphore(max_concurrency)
        log.info("provider_semaphore_created", provider=provider, limit=max_concurrency)

    def register_model(
        self,
        role: str,
        max_concurrency: int,
        endpoint: str | None = None,
        provider: str | None = None,
    ) -> None:
        """Register a deployment with its provider and its own sub-limit.

        Args:
            role: Deployment key (e.g. ``"qwen3.6-35b-thinking"``).
            max_concurrency: This deployment's own in-flight cap, applied beneath
                its provider's ceiling.
            endpoint: Deployment-specific endpoint URL, kept for log context only.
                It is no longer a grouping key — capacity follows the provider.
            provider: Provider name. ``None`` places the deployment in a private
                pool named for itself, so an unattributed deployment is bounded
                rather than unbounded.
        """
        effective_provider = provider or f"_unattributed:{role}"
        self._model_provider[role] = effective_provider
        self._model_endpoint[role] = endpoint or self._default_base_url

        if effective_provider not in self._provider_semaphores:
            self.register_provider(effective_provider, self._default_provider_limit)

        if role not in self._model_semaphores:
            self._model_semaphores[role] = _PrioritySemaphore(max_concurrency)
            log.info(
                "model_semaphore_created",
                role=role,
                limit=max_concurrency,
                provider=effective_provider,
            )

    @asynccontextmanager
    async def request_slot(
        self,
        role: str,
        priority: InferencePriority = InferencePriority.USER_FACING,
        timeout: float | None = None,
        trace_id: str | None = None,
    ) -> AsyncIterator[None]:
        """Acquire an inference slot with priority: provider ceiling, then deployment sub-limit.

        Args:
            role: Deployment key.
            priority: Request priority tier.
            timeout: Max seconds to wait for a slot. None waits forever.
            trace_id: Originating request trace_id, threaded onto wait/timeout
                logs for §I3 identity threading. Defaults to ``None`` when the
                caller has no request context.

        Yields:
            None when the slot is acquired.

        Raises:
            InferenceSlotTimeout: If timeout expires before a slot is acquired.
        """
        model_sem = self._model_semaphores.get(role)
        provider = self._model_provider.get(role, "")
        provider_sem = self._provider_semaphores.get(provider)

        if not model_sem and not provider_sem:
            yield
            return

        start = time.monotonic()
        model_acquired = False
        provider_acquired = False

        try:
            if provider_sem:
                acquired = await provider_sem.acquire(priority, timeout=timeout)
                if not acquired:
                    raise InferenceSlotTimeout(
                        f"Timed out waiting for provider slot on {provider} "
                        f"(priority={priority.name}, timeout={timeout}s)"
                    )
                provider_acquired = True

            remaining_timeout = None
            if timeout is not None:
                elapsed = time.monotonic() - start
                remaining_timeout = max(0.0, timeout - elapsed)

            if model_sem:
                acquired = await model_sem.acquire(priority, timeout=remaining_timeout)
                if not acquired:
                    raise InferenceSlotTimeout(
                        f"Timed out waiting for model slot on {role} "
                        f"(priority={priority.name}, timeout={timeout}s)"
                    )
                model_acquired = True

            wait_ms = int((time.monotonic() - start) * 1000)
            if wait_ms > 100:
                log.info(
                    "inference_slot_acquired",
                    role=role,
                    priority=priority.name,
                    wait_ms=wait_ms,
                    provider=provider,
                    endpoint=self._model_endpoint.get(role, ""),
                    trace_id=trace_id,
                )

            yield

        except InferenceSlotTimeout:
            log.warning(
                "inference_slot_timeout",
                role=role,
                priority=priority.name,
                timeout=timeout,
                provider=provider,
                trace_id=trace_id,
            )
            raise

        finally:
            if model_acquired and model_sem:
                await model_sem.release()
            if provider_acquired and provider_sem:
                await provider_sem.release()

    def get_status(self) -> dict[str, dict[str, dict[str, int]]]:
        """Return current concurrency status for monitoring.

        Returns:
            Dict with model and provider semaphore states.
        """
        status: dict[str, dict[str, dict[str, int]]] = {"models": {}, "providers": {}}
        for role, sem in self._model_semaphores.items():
            status["models"][role] = {"active": sem.active, "limit": sem.limit}
        for provider, sem in self._provider_semaphores.items():
            status["providers"][provider] = {"active": sem.active, "limit": sem.limit}
        return status


class InferenceSlotTimeout(Exception):
    """Raised when a request cannot acquire an inference slot within the timeout."""

    pass
