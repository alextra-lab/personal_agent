"""Test doubles for the broker (FRE-1683)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from personal_agent.dev_broker.dispatch import ModelRequest

from personal_agent.dev_broker.ledger import Admitted, Outcome, Refusal, TokenGrant
from personal_agent.llm_client.types import LLMResponse
from personal_agent.telemetry.trace import TraceContext


class FakeClock:
    """A clock that moves only when the code under test sleeps."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class FakeLedger:
    """Accepts one token for one model and records every reserve and settle."""

    def __init__(self, token_hash: str, model_key: str) -> None:
        self.token_hash = token_hash
        self.model_key = model_key
        self.grant = TokenGrant(uuid4(), model_key, uuid4(), uuid4())
        self.refuse_reserve: Refusal | None = None
        self.reserved: list[tuple[Decimal, int, int, datetime]] = []
        self.settled: list[tuple[Admitted, Outcome]] = []

    async def check_token(
        self, *, token_hash: str, model_key: str, now: datetime
    ) -> TokenGrant | Refusal:
        if token_hash != self.token_hash:
            return Refusal("unknown_token")
        if model_key != self.model_key:
            return Refusal("wrong_model")
        return self.grant

    async def reserve(
        self,
        *,
        token_hash: str,
        model_key: str,
        amount_usd: Decimal,
        prompt_token_bound: int,
        max_output_tokens: int,
        now: datetime,
    ) -> Admitted | Refusal:
        if self.refuse_reserve is not None:
            return self.refuse_reserve
        self.reserved.append((amount_usd, prompt_token_bound, max_output_tokens, now))
        g = self.grant
        return Admitted(uuid4(), g.job_id, g.trace_id, g.session_id, amount_usd)

    async def settle(self, admitted: Admitted, outcome: Outcome, *, now: datetime) -> None:
        self.settled.append((admitted, outcome))


def llm_response(
    *,
    content: str = "hello",
    tool_calls: list[dict[str, str]] | None = None,
    usage: dict[str, int] | None = None,
    cost_usd: float = 0.0004,
    finish_reason: str = "stop",
) -> LLMResponse:
    """A response in the shape that ``LiteLLMClient.respond`` returns."""
    return LLMResponse(  # type: ignore[typeddict-item]
        role="assistant",
        content=content,
        tool_calls=tool_calls or [],  # type: ignore[typeddict-item]
        reasoning_trace=None,
        usage={"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
        if usage is None
        else usage,
        response_id="r-1",
        cost_usd=cost_usd,
        finish_reason=finish_reason,
        raw={},
    )


Handler = Callable[[ModelRequest, TraceContext], Coroutine[Any, Any, LLMResponse]]


class FakeDispatcher:
    """Runs ``handler`` for each call and records what it was given."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.calls: list[tuple[ModelRequest, TraceContext]] = []
        self._handler = handler

    async def dispatch(self, request: ModelRequest, trace_ctx: TraceContext) -> LLMResponse:
        self.calls.append((request, trace_ctx))
        if self._handler is None:
            return llm_response()
        return await self._handler(request, trace_ctx)


def job_ids(ledger: FakeLedger) -> tuple[UUID, UUID]:
    """Return the fake job's trace and session ids."""
    return ledger.grant.trace_id, ledger.grant.session_id
