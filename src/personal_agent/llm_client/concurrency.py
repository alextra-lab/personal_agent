"""Inference concurrency controller for local and remote LLM endpoints.

Implements per-model and per-endpoint concurrency limits with priority-based
scheduling. Local inference servers (LM Studio, Ollama) are single-request-at-a-time
for large models; cloud providers handle concurrency server-side.

See ADR-0029 for design rationale.
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


class ProviderType(str):
    """Provider type constants for endpoint classification."""

    LOCAL = "local"
    MANAGED = "managed"
    CLOUD = "cloud"


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]"})


def infer_provider_type(endpoint: str | None) -> str:
    """Auto-detect provider type from endpoint URL.

    Args:
        endpoint: Base URL for the inference server.

    Returns:
        Provider type string: "local", "managed", or "cloud".
    """
    if not endpoint:
        return ProviderType.LOCAL

    try:
        from urllib.parse import urlparse
        parsed = urlparse(endpoint)
        host = (parsed.hostname or "").lower()
        if host in _LOCAL_HOSTS:
            return ProviderType.LOCAL
        if parsed.scheme == "http" and not host.endswith((".com", ".ai", ".io", ".dev")):
            return ProviderType.MANAGED
    except Exception:
        pass

    return ProviderType.CLOUD


def _normalize_endpoint(endpoint: str | None, default_base_url: str) -> str:
    """Normalize endpoint to a canonical key for grouping.

    Strips trailing path components so models on the same server share a key.

    Args:
        endpoint: Model-specific endpoint or None.
        default_base_url: Fallback base URL from settings.

    Returns:
        Normalized endpoint string (scheme + host + port).
    """
    url = endpoint or default_base_url
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://{host}:{port}"
    except Exception:
        return url


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
    """Manages concurrent access to inference endpoints.

    Enforces per-model and per-endpoint concurrency limits with
    priority-based scheduling. Local endpoints get strict control;
    cloud endpoints pass through with minimal overhead.

    Args:
        default_base_url: Default base URL for models without explicit endpoints.
        default_endpoint_limit: Default per-endpoint concurrency limit for local providers.
    """

    def __init__(
        self,
        default_base_url: str = "http://127.0.0.1:1234/v1",
        default_endpoint_limit: int = 2,
    ) -> None:
        self._default_base_url = default_base_url
        self._default_endpoint_limit = default_endpoint_limit

        self._model_semaphores: dict[str, _PrioritySemaphore] = {}
        self._endpoint_semaphores: dict[str, _PrioritySemaphore] = {}
        self._model_endpoint_map: dict[str, str] = {}
        self._model_provider_type: dict[str, str] = {}
        self._endpoint_provider_type: dict[str, str] = {}

    def register_model(
        self,
        role: str,
        max_concurrency: int,
        endpoint: str | None = None,
        provider_type: str | None = None,
    ) -> None:
        """Register a model role with its concurrency limits.

        Args:
            role: Model role name (e.g., "router", "reasoning").
            max_concurrency: Maximum concurrent requests for this model.
            endpoint: Model-specific endpoint URL. None uses default.
            provider_type: "local", "managed", or "cloud". None auto-detects.
        """
        norm_endpoint = _normalize_endpoint(endpoint, self._default_base_url)
        effective_provider = provider_type or infer_provider_type(endpoint)

        self._model_endpoint_map[role] = norm_endpoint
        self._model_provider_type[role] = effective_provider
        self._endpoint_provider_type[norm_endpoint] = effective_provider

        if effective_provider == ProviderType.CLOUD:
            log.debug(
                "model_registered_cloud",
                role=role,
                endpoint=norm_endpoint,
                provider_type=effective_provider,
                max_concurrency=max_concurrency,
            )
            return

        if role not in self._model_semaphores:
            self._model_semaphores[role] = _PrioritySemaphore(max_concurrency)
            log.info(
                "model_semaphore_created",
                role=role,
                limit=max_concurrency,
                provider_type=effective_provider,
            )

        if norm_endpoint not in self._endpoint_semaphores:
            self._endpoint_semaphores[norm_endpoint] = _PrioritySemaphore(
                self._default_endpoint_limit
            )
            log.info(
                "endpoint_semaphore_created",
                endpoint=norm_endpoint,
                limit=self._default_endpoint_limit,
                provider_type=effective_provider,
            )

    def _needs_control(self, role: str) -> bool:
        """Check if a model role requires concurrency control."""
        return self._model_provider_type.get(role, ProviderType.LOCAL) != ProviderType.CLOUD

    @asynccontextmanager
    async def request_slot(
        self,
        role: str,
        priority: InferencePriority = InferencePriority.USER_FACING,
        timeout: float | None = None,
    ) -> AsyncIterator[None]:
        """Context manager to acquire an inference slot with priority.

        For cloud providers, yields immediately with no blocking.
        For local/managed providers, acquires both model and endpoint semaphores.

        Args:
            role: Model role name.
            priority: Request priority tier.
            timeout: Max seconds to wait for a slot. None waits forever.

        Yields:
            None when slot is acquired.

        Raises:
            InferenceSlotTimeout: If timeout expires before slot is acquired.
        """
        if not self._needs_control(role):
            yield
            return

        model_sem = self._model_semaphores.get(role)
        endpoint_key = self._model_endpoint_map.get(role, "")
        endpoint_sem = self._endpoint_semaphores.get(endpoint_key)

        if not model_sem and not endpoint_sem:
            yield
            return

        start = time.monotonic()
        model_acquired = False
        endpoint_acquired = False

        try:
            if endpoint_sem:
                acquired = await endpoint_sem.acquire(priority, timeout=timeout)
                if not acquired:
                    raise InferenceSlotTimeout(
                        f"Timed out waiting for endpoint slot on {endpoint_key} "
                        f"(priority={priority.name}, timeout={timeout}s)"
                    )
                endpoint_acquired = True

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
                    endpoint=endpoint_key,
                )

            yield

        except InferenceSlotTimeout:
            log.warning(
                "inference_slot_timeout",
                role=role,
                priority=priority.name,
                timeout=timeout,
                endpoint=endpoint_key,
            )
            raise

        finally:
            if model_acquired and model_sem:
                await model_sem.release()
            if endpoint_acquired and endpoint_sem:
                await endpoint_sem.release()

    def get_status(self) -> dict[str, dict[str, dict[str, int]]]:
        """Return current concurrency status for monitoring.

        Returns:
            Dict with model and endpoint semaphore states.
        """
        status: dict[str, dict[str, dict[str, int]]] = {"models": {}, "endpoints": {}}
        for role, sem in self._model_semaphores.items():
            status["models"][role] = {"active": sem.active, "limit": sem.limit}
        for endpoint, sem in self._endpoint_semaphores.items():
            status["endpoints"][endpoint] = {"active": sem.active, "limit": sem.limit}
        return status


class InferenceSlotTimeout(Exception):
    """Raised when a request cannot acquire an inference slot within the timeout."""

    pass
