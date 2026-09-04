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

Every provider — including cloud ones — is registered with a ceiling, and any
deployment that acquires a slot is capped at its provider's ceiling.

**Historical note, kept because the module predates ADR-0141's unification:**
this controller was originally instantiated per-client by a local-only
dispatch class, so only local-placement deployments called ``request_slot`` and
the registered cloud ceilings (``openai``/``anthropic``/``voyage``/``ovh``)
were declared-but-inert (ADR-0121 step 2, FRE-917 recorded — incorrectly — that
this had already been fixed). ADR-0141 T2 moved local placement onto
``LiteLLMClient``, T3 (FRE-1366) re-homed this controller as the process-wide
singleton returned by :func:`get_inference_concurrency_controller`, acquired
inside ``LiteLLMClient.respond()`` (and its local-placement ``_respond_local``)
for every chat-completion provider — ``slm_local``, ``anthropic``, ``openai``,
``ovhcloud`` — and T4 (FRE-1367) deleted the local-only dispatch class
entirely. Placement (local vs cloud) now decides only parameter shape and
cost-gate applicability, not which controller instance is acquired.

The local ``max_concurrency: 1`` GPU ceiling and the ``InferencePriority``
tiers carry over unchanged through the singleton's per-deployment
``register_model`` registration. The cloud ceilings (``openai``/``anthropic``/
``ovhcloud``, set high at 50 as a safety valve) are now live for the first
time — ``voyage`` and ``ovh`` (reranker/embedder) never dispatch through
``respond()`` and stay declared-but-inert, out of this ADR's scope by design.
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
                    # Still queued — a clean timeout, nothing was handed to us.
                    self._waiters.remove(slot)
                    return False
                # Race: release() popped this slot and set its event in the
                # window between wait_for firing and us re-acquiring the lock.
                # That slot IS a live grant — release() hands off without
                # decrementing _active — so abandoning it here would leak the
                # permit forever and, once _active pins at _limit, wedge every
                # future acquire on this semaphore. Pass the grant on instead of
                # dropping it: to the next waiter, or back to the pool.
                if self._waiters:
                    self._waiters.pop(0).event.set()
                else:
                    self._active -= 1
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
            deployments that declare no endpoint of their own. ``None`` when the
            caller has no base URL to attribute (ADR-0132 D4 removed the dead
            loopback default that used to stand in here).
        default_provider_limit: Ceiling applied to a provider referenced by a
            deployment but never explicitly registered. A fallback for tests and
            partial fixtures — the real catalog declares every provider.
    """

    def __init__(
        self,
        default_base_url: str | None = None,
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
        # `""` rather than None when neither is known: this value is log context
        # only, and the dead loopback default that used to stand in here is
        # exactly what ADR-0132 D4 removed.
        self._model_endpoint[role] = endpoint or self._default_base_url or ""

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
                    # Report the budget actually waited on the model semaphore
                    # (remaining_timeout), not the original — when the provider
                    # ceiling consumed most of the budget these differ, and the
                    # original would point at the wrong semaphore to tune.
                    raise InferenceSlotTimeout(
                        f"Timed out waiting for model slot on {role} "
                        f"(priority={priority.name}, timeout={remaining_timeout}s "
                        f"of {timeout}s total)"
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

    def effective_ceiling(self, role: str | None, default: int) -> int:
        """Return the binding concurrency ceiling for a role (FRE-1374).

        The tighter of the deployment's own sub-limit and its provider's ceiling —
        the same two constraints :meth:`request_slot` already enforces in sequence.
        Callers use this to size a fan-out so it never guarantees a queue.

        Args:
            role: Deployment key, or ``None`` when the caller has no resolvable role
                (e.g. a test double with no catalog identity).
            default: Value returned when ``role`` is ``None`` or not registered.
                Required rather than an internal fallback, so an unresolvable role
                means "the caller decides," never a silently-guessed ceiling.

        Returns:
            The effective ceiling, or ``default`` when it cannot be determined.
        """
        if role is None:
            return default
        model_sem = self._model_semaphores.get(role)
        provider = self._model_provider.get(role)
        provider_sem = self._provider_semaphores.get(provider) if provider else None
        limits = [sem.limit for sem in (model_sem, provider_sem) if sem is not None]
        return min(limits) if limits else default

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


# ---------------------------------------------------------------------------
# Process-wide singleton (ADR-0141 D3)
# ---------------------------------------------------------------------------

_controller: InferenceConcurrencyController | None = None


def _build_controller_from_catalog() -> InferenceConcurrencyController:
    """Construct a controller registered against every catalog provider + deployment.

    Mirrors the registration the now-deleted local-only dispatch class used to
    do for itself (pre-ADR-0141): every provider gets its declared ceiling,
    every deployment gets its own sub-limit beneath its provider. The
    difference is scope — this now runs once, process-wide, for every
    placement.

    Returns:
        A freshly populated :class:`InferenceConcurrencyController`.
    """
    from personal_agent.config import load_model_config

    config = load_model_config()
    controller = InferenceConcurrencyController()
    for provider_name, provider in config.providers.items():
        controller.register_provider(provider_name, max_concurrency=provider.max_concurrency)
    for role_name, model_def in config.models.items():
        controller.register_model(
            role=role_name,
            max_concurrency=model_def.max_concurrency,
            endpoint=model_def.endpoint,
            provider=model_def.provider,
        )
    return controller


def get_inference_concurrency_controller() -> InferenceConcurrencyController:
    """Return the process-wide ``InferenceConcurrencyController`` singleton.

    Created and populated from the model catalog on first call (ADR-0141 D3):
    every declared provider and deployment is registered, so the returned
    controller enforces the same ceilings ``config/models.yaml`` declares —
    including the cloud providers ADR-0121's FRE-917 note wrongly recorded as
    already live. Acquired by ``LiteLLMClient.respond()`` (both placements) for
    every chat-completion call.

    Returns:
        The singleton controller, creating it on first call.
    """
    global _controller
    if _controller is None:
        _controller = _build_controller_from_catalog()
    return _controller


def set_inference_concurrency_controller(controller: InferenceConcurrencyController | None) -> None:
    """Register (or clear) the process-wide controller. Test seam.

    Args:
        controller: The controller to install, or ``None`` to clear it — the
            next :func:`get_inference_concurrency_controller` call then
            rebuilds a fresh one from the catalog.
    """
    global _controller
    _controller = controller
