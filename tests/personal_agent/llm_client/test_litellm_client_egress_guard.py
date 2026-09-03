"""ADR-0141 D2 / FRE-1364 — the two-layer egress guard on the litellm dispatch path.

Layer 1 (pre-dispatch, route-independent) and layer 2 (per-route injected
hook) are exercised through the **real** ``litellm.acompletion()`` dispatch —
not mocked — per AC-3's own bar: "fails if... the request reaches a
transport... or the test stubs the layer the guard hangs on." Only the
outbound transport is replaced, at the lowest level each route mechanism
actually builds one:

- OpenAI-SDK route (``openai``): ``httpx.AsyncHTTPTransport`` — the plain
  default ``create_guarded_http_client()`` builds, no litellm involvement.
- AsyncHTTPHandler route (``anthropic``, ``ovhcloud``): litellm's own
  ``AsyncHTTPHandler._create_async_transport`` staticmethod, patched to
  return a deterministic ``httpx.MockTransport`` — the real default is an
  **aiohttp**-backed transport (verified against litellm 1.98.0), not
  ``httpx.AsyncHTTPTransport``, so patching the httpx one would never fire
  for this route and could pass a broken guard for the wrong reason (a real
  failed network call in a sandboxed CI environment also raises).

The primary assertion in every test is that ``DomainGuard.check_url`` was
actually invoked with the expected URL (proves the guard mechanism engaged,
independent of transport internals) — the transport-sentinel assertion is
defense in depth.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from personal_agent.llm_client import litellm_client as litellm_client_module
from personal_agent.llm_client.litellm_client import LiteLLMClient
from personal_agent.llm_client.models import ModelConfig, Placement, ProviderDefinition
from personal_agent.llm_client.types import ModelRole
from personal_agent.security import DomainGuard, EgressBlockedError, GuardMode
from tests._helpers.trace import make_test_ctx

BLOCKED_HOST = "blocked.example"
ALLOWED_HOST = "allowed.example"


@pytest.fixture(autouse=True)
def _clear_guarded_client_caches() -> Any:
    """Prevent cross-test cache growth and stale-guard leakage.

    No explicit aclose()/close(): every client built in these tests has its
    transport patched or mocked before any real connection could be
    attempted, so there is nothing live to release — a bare .clear() is
    sufficient here (unlike a production shutdown path).
    """
    yield
    litellm_client_module._guarded_httpx_clients.clear()
    litellm_client_module._guarded_async_http_handlers.clear()


def _guard(*, blocklist: frozenset[str] = frozenset()) -> DomainGuard:
    """A pre-loaded DomainGuard in BLOCKLIST mode — never touches network or disk."""
    g = DomainGuard(cache_path=Path("telemetry/security/_unused_test_blocklist.json"))
    g._blocklist = blocklist
    g._last_loaded = datetime.now(timezone.utc)
    return g


def _spy_check_url(guard: DomainGuard) -> MagicMock:
    spy = MagicMock(wraps=guard.check_url)
    guard.check_url = spy  # type: ignore[method-assign]
    return spy


def _checked_hosts(spy: MagicMock) -> list[str]:
    """Hostnames check_url was called with, in order.

    Layer 2's hook checks the *actual* request URL (litellm/the SDK append a
    path, e.g. "/chat/completions"), which differs textually from what
    Layer 1 checks (the bare declared base_url) — so a layer1-disabled test
    must compare by hostname, not exact URL string.
    """
    return [httpx.URL(str(c.args[0])).host for c in spy.call_args_list]


def _openai_success_body() -> dict[str, Any]:
    return {
        "id": "resp_test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _sentinel_never_reached(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"sentinel transport reached: {request.url}")


async def _dispatch(
    *,
    provider: str,
    base_url: str,
    guard: DomainGuard,
) -> None:
    """Run LiteLLMClient.respond() with only side effects (not egress) mocked.

    litellm.acompletion is deliberately NOT mocked — it runs for real, down to
    whatever transport the route builds (patched by the caller beforehand).
    """
    catalog = ModelConfig(
        providers={
            provider: ProviderDefinition(
                base_url=base_url,
                auth_env="test_provider_key",
                placement=Placement.CLOUD,
                max_concurrency=10,
            )
        },
        models={},
    )
    settings_mock = MagicMock(test_provider_key="sk-test-999")

    mock_gate = MagicMock()
    mock_gate.reserve = AsyncMock(return_value="res-001")
    mock_gate.commit = AsyncMock()
    mock_gate.refund = AsyncMock()

    mock_tracker = AsyncMock()
    mock_tracker.connect = AsyncMock()
    mock_tracker.disconnect = AsyncMock()
    mock_tracker.record_api_call = AsyncMock()

    client = LiteLLMClient(
        model_id="test-model",
        provider=provider,
        max_tokens=256,
        budget_role="main_inference",
        egress_guard=guard,
    )

    patches = [
        patch("litellm.completion_cost", return_value=0.001),
        patch("personal_agent.cost_gate.get_default_gate", return_value=mock_gate),
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
            return_value=mock_tracker,
        ),
        patch("personal_agent.config.settings.get_settings", return_value=settings_mock),
        patch("personal_agent.config.load_model_config", return_value=catalog),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": "hi"}],
            trace_ctx=make_test_ctx("litellm_egress_guard"),
        )


# ── AC-a: seeded-negative, OpenAI-SDK route (provider="openai") ───────────


class TestOpenAISdkRouteSeededNegative:
    @pytest.mark.asyncio
    async def test_layer1_blocks_before_any_transport(self) -> None:
        guard = _guard(blocklist=frozenset({BLOCKED_HOST}))
        check_url_spy = _spy_check_url(guard)
        with patch.object(
            httpx.AsyncHTTPTransport, "handle_async_request", side_effect=_sentinel_never_reached
        ) as transport_spy:
            with pytest.raises(EgressBlockedError):
                await _dispatch(provider="openai", base_url=f"https://{BLOCKED_HOST}", guard=guard)
        check_url_spy.assert_any_call(f"https://{BLOCKED_HOST}")
        transport_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_layer2_alone_blocks_when_layer1_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer 1 off — proves layer 2's own hook blocks it independently."""
        monkeypatch.setattr(litellm_client_module, "check_egress_or_raise", lambda *a, **kw: None)
        guard = _guard(blocklist=frozenset({BLOCKED_HOST}))
        check_url_spy = _spy_check_url(guard)
        with patch.object(
            httpx.AsyncHTTPTransport, "handle_async_request", side_effect=_sentinel_never_reached
        ) as transport_spy:
            with pytest.raises(Exception):  # noqa: B017 — route's own wrapper shape, per ADR D2.2
                await _dispatch(provider="openai", base_url=f"https://{BLOCKED_HOST}", guard=guard)
        # Layer 1 is off, so only layer 2's hook checked — against the actual
        # request URL (SDK-appended path), not the bare base_url string.
        assert BLOCKED_HOST in _checked_hosts(check_url_spy)
        transport_spy.assert_not_called()


# ── AC-a: seeded-negative, AsyncHTTPHandler route (provider="anthropic") ──


class TestAsyncHttpHandlerRouteSeededNegative:
    @pytest.mark.asyncio
    async def test_layer1_blocks_before_any_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_transport = httpx.MockTransport(_sentinel_never_reached)
        monkeypatch.setattr(
            AsyncHTTPHandler,
            "_create_async_transport",
            staticmethod(lambda *a, **kw: mock_transport),
        )
        guard = _guard(blocklist=frozenset({BLOCKED_HOST}))
        check_url_spy = _spy_check_url(guard)
        with pytest.raises(EgressBlockedError):
            await _dispatch(provider="anthropic", base_url=f"https://{BLOCKED_HOST}", guard=guard)
        check_url_spy.assert_any_call(f"https://{BLOCKED_HOST}")

    @pytest.mark.asyncio
    async def test_layer2_alone_blocks_when_layer1_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(litellm_client_module, "check_egress_or_raise", lambda *a, **kw: None)
        mock_transport = httpx.MockTransport(_sentinel_never_reached)
        monkeypatch.setattr(
            AsyncHTTPHandler,
            "_create_async_transport",
            staticmethod(lambda *a, **kw: mock_transport),
        )
        guard = _guard(blocklist=frozenset({BLOCKED_HOST}))
        check_url_spy = _spy_check_url(guard)
        with pytest.raises(Exception):  # noqa: B017 — route's own wrapper shape, per ADR D2.2
            await _dispatch(provider="anthropic", base_url=f"https://{BLOCKED_HOST}", guard=guard)
        # Layer 1 is off, so only layer 2's hook checked — against the actual
        # request URL (litellm-appended path), not the bare base_url string.
        assert BLOCKED_HOST in _checked_hosts(check_url_spy)


# ── AC-b: guard-attached calls still succeed, one test per route mechanism ─


class TestGuardAttachedCallsStillSucceed:
    @pytest.mark.asyncio
    async def test_openai_sdk_route(self) -> None:
        guard = _guard(blocklist=frozenset())  # empty blocklist — nothing to refuse
        response_body = _openai_success_body()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_body, request=request)

        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request") as transport_mock:

            async def _async_handle(request: httpx.Request) -> httpx.Response:
                return _handler(request)

            transport_mock.side_effect = _async_handle
            await _dispatch(provider="openai", base_url=f"https://{ALLOWED_HOST}", guard=guard)

    @pytest.mark.asyncio
    async def test_async_http_handler_route_via_ovhcloud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OVHCloudChatConfig inherits OpenAIGPTConfig's response parsing unmodified —
        the same OpenAI-shaped fixture proves the AsyncHTTPHandler route's success path
        without hand-building Anthropic's native response schema.
        """
        response_body = _openai_success_body()

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_body, request=request)

        mock_transport = httpx.MockTransport(_handler)
        monkeypatch.setattr(
            AsyncHTTPHandler,
            "_create_async_transport",
            staticmethod(lambda *a, **kw: mock_transport),
        )
        guard = _guard(blocklist=frozenset())
        await _dispatch(provider="ovhcloud", base_url=f"https://{ALLOWED_HOST}", guard=guard)


# ── AC-c: layer-2 hook fires on redirect hops (AsyncHTTPHandler route) ────


class TestRedirectHopsAreGuarded:
    @pytest.mark.asyncio
    async def test_hook_fires_on_the_redirected_request_not_just_the_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Layer 1 disabled — isolates the assertion to layer 2's per-hop hook.

        Layer 1 would run its own separate check_url call against the
        catalog-declared base_url verbatim (not the redirected URL), which
        would confound a "these are the two hop URLs" assertion; layer 2's
        own per-hop hook is what AC-c is actually about.
        """
        monkeypatch.setattr(litellm_client_module, "check_egress_or_raise", lambda *a, **kw: None)
        redirect_target = f"https://{BLOCKED_HOST}/messages"
        call_count = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(307, headers={"location": redirect_target}, request=request)
            raise AssertionError("sentinel transport reached on the redirected hop")

        mock_transport = httpx.MockTransport(_handler)
        monkeypatch.setattr(
            AsyncHTTPHandler,
            "_create_async_transport",
            staticmethod(lambda *a, **kw: mock_transport),
        )
        guard = _guard(blocklist=frozenset({BLOCKED_HOST}))
        check_url_spy = _spy_check_url(guard)

        with pytest.raises(Exception):  # noqa: B017 — route's own wrapper shape, per ADR D2.2
            await _dispatch(provider="anthropic", base_url=f"https://{ALLOWED_HOST}", guard=guard)

        assert call_count["n"] == 1, "the redirected hop must never reach the transport"
        checked_hosts = [httpx.URL(str(c.args[0])).host for c in check_url_spy.call_args_list]
        assert checked_hosts[-2:] == [ALLOWED_HOST, BLOCKED_HOST], (
            f"expected the allowed first hop then the blocked redirect hop, got {checked_hosts}"
        )
