"""The agent's only model endpoint: an OpenAI-compatible, metered route (ADR-0159 D5; FRE-1683).

Per request: check the job token, bound the call's worst case, wait out a cost-gate boundary,
reserve the worst case on the job and month counters, dispatch with a hard time limit, and settle.
Every path after the reserve settles exactly once, including a cancellation.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_UP, Decimal
from typing import Literal, Protocol

import asyncpg  # type: ignore[import-untyped]
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from personal_agent.cost_gate.policy import BudgetConfigError
from personal_agent.cost_gate.role_map import UnknownBudgetRoleError
from personal_agent.cost_gate.types import BudgetDenied
from personal_agent.dev_broker.clock import Clock, seconds_until_safe
from personal_agent.dev_broker.config import BrokerConfig
from personal_agent.dev_broker.dispatch import Dispatcher, JsonObject, ModelRequest
from personal_agent.dev_broker.ledger import (
    Admitted,
    Cancelled,
    CostUnknown,
    Failed,
    GateDenied,
    Outcome,
    ProviderError,
    Refusal,
    Settled,
    TimedOut,
    TokenGrant,
)
from personal_agent.dev_broker.tokens import hash_token
from personal_agent.llm_client.concurrency import InferenceSlotTimeout
from personal_agent.llm_client.models import ModelDefinition
from personal_agent.llm_client.types import LLMClientError, LLMResponse
from personal_agent.security import EgressBlockedError
from personal_agent.telemetry.trace import TraceContext

log = structlog.get_logger(__name__)

_MICRO_USD = Decimal("0.000001")

#: HTTP status for each refusal.
_REFUSAL_STATUS: dict[str, int] = {
    "unknown_token": 401,
    "inactive_token": 403,
    "expired_token": 403,
    "wrong_model": 403,
    "cap_reached": 403,
    # Calls in flight hold the headroom; retry once they settle.
    "cap_busy": 429,
    # This call does not fit the room left; a smaller one can.
    "call_too_large": 403,
}


class Ledger(Protocol):
    """The part of :class:`~personal_agent.dev_broker.ledger.SpendLedger` that the route uses."""

    async def check_token(
        self, *, token_hash: str, model_key: str, now: datetime
    ) -> TokenGrant | Refusal:
        """Check a token without a lock."""
        ...

    async def reserve(
        self,
        *,
        token_hash: str,
        model_key: str,
        amount_usd: Decimal,
        prompt_token_bound: int,
        max_output_tokens: int,
        smallest_call_usd: Decimal,
        now: datetime,
    ) -> Admitted | Refusal:
        """Reserve a call's worst case, or refuse it."""
        ...

    async def settle(self, admitted: Admitted, outcome: Outcome, *, now: datetime) -> None:
        """Settle a reserved call."""
        ...


@dataclass(frozen=True)
class RouteResult:
    """What the route answers.

    Attributes:
        status: HTTP status.
        body: The JSON body (a completion, or an OpenAI-shaped error).
        stream: Whether the request asked for SSE. Only a 200 is streamed.
        include_usage: Whether a streamed answer ends with a usage chunk.
    """

    status: int
    body: JsonObject
    stream: bool = False
    include_usage: bool = False


def _error(status: int, code: str, message: str) -> RouteResult:
    return RouteResult(status, {"error": {"message": message, "type": code, "code": code}})


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _text_only(content: object) -> bool:
    """Return whether a message's content is text: absent, a string, or a list of text parts."""
    if content is None or isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return all(
        isinstance(part, Mapping)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
        for part in content
    )


def _temperature(value: object) -> float | None | Literal[False]:
    """Return a request's temperature, ``None`` when absent, or ``False`` when it is unusable."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except OverflowError:
        return False
    return number if math.isfinite(number) else False


def prompt_token_bound(
    messages: Sequence[JsonObject], tools: Sequence[JsonObject] | None, overhead: int
) -> int:
    """Return an upper bound on the prompt tokens of a request.

    A byte-level BPE token covers at least one byte, so the UTF-8 length of the JSON bounds the
    tokens of its text. The JSON keys and quotes of each message exceed its chat-template tokens.

    Args:
        messages: The request's messages.
        tools: The request's tools.
        overhead: Fixed tokens added for the template.

    Returns:
        The bound.
    """
    payload = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
    return len(payload.encode("utf-8")) + overhead


class ModelRoute:
    """The metered chat-completion route."""

    def __init__(
        self,
        *,
        config: BrokerConfig,
        ledger: Ledger,
        dispatcher: Dispatcher,
        catalog: Mapping[str, ModelDefinition],
        clock: Clock,
    ) -> None:
        """Initialise the route.

        Args:
            config: The broker config.
            ledger: The spend ledger.
            dispatcher: The dispatch path.
            catalog: The model catalog (``load_model_config().models``).
            clock: The time source.
        """
        self.config = config
        self._ledger = ledger
        self._dispatcher = dispatcher
        self._catalog = catalog
        self._clock = clock

    async def complete(self, *, authorization: str | None, body: object) -> RouteResult:
        """Answer one chat-completion request.

        Args:
            authorization: The ``Authorization`` header.
            body: The parsed JSON body.

        Returns:
            The answer.
        """
        if not authorization or not authorization.startswith("Bearer "):
            return _error(401, "unknown_token", "a job token is required")
        token_hash = hash_token(authorization.removeprefix("Bearer ").strip())

        if not isinstance(body, Mapping):
            return _error(400, "invalid_request", "the body must be a JSON object")
        model_key = body.get("model")
        messages = body.get("messages")
        tools = body.get("tools")
        if not isinstance(model_key, str) or model_key not in self.config.models:
            return _error(403, "wrong_model", "this model is not available to the job")
        if (
            not isinstance(messages, list)
            or not messages
            or not all(isinstance(m, Mapping) for m in messages)
        ):
            return _error(400, "invalid_request", "messages must be a non-empty list of objects")
        if not all(_text_only(m.get("content")) for m in messages):
            # The byte bound holds for text only. An image part costs far more tokens than its
            # bytes, and the job's models have no vision (config/models.yaml).
            return _error(400, "invalid_request", "only text content is accepted")
        if tools is not None and (
            not isinstance(tools, list) or not all(isinstance(t, Mapping) for t in tools)
        ):
            return _error(400, "invalid_request", "tools must be a list of objects")

        grant = await self._ledger.check_token(
            token_hash=token_hash, model_key=model_key, now=self._clock.now()
        )
        if isinstance(grant, Refusal):
            return self._refused(grant)

        entry = self._catalog.get(model_key)
        if entry is None or not entry.input_cost_per_token or not entry.output_cost_per_token:
            log.error("dev_broker_model_unpriced", model_key=model_key, trace_id=grant.trace_id.hex)
            return _error(500, "model_unpriced", "the model has no price; refusing the call")

        limits = [self.config.max_output_tokens]
        if entry.max_tokens:
            limits.append(entry.max_tokens)
        requested = _positive_int(body.get("max_completion_tokens")) or _positive_int(
            body.get("max_tokens")
        )
        if requested is not None:
            limits.append(requested)
        max_tokens = min(limits)
        bound = prompt_token_bound(messages, tools, self.config.prompt_overhead_tokens)
        worst_case = (
            Decimal(bound) * Decimal(str(entry.input_cost_per_token))
            + Decimal(max_tokens) * Decimal(str(entry.output_cost_per_token))
        ).quantize(_MICRO_USD, rounding=ROUND_UP)
        # The smallest call the job can make: an empty prompt and a full answer. The ledger
        # stops the job only when not even this fits.
        full_answer = min(
            self.config.max_output_tokens, entry.max_tokens or self.config.max_output_tokens
        )
        smallest_call = (
            Decimal(self.config.prompt_overhead_tokens) * Decimal(str(entry.input_cost_per_token))
            + Decimal(full_answer) * Decimal(str(entry.output_cost_per_token))
        ).quantize(_MICRO_USD, rounding=ROUND_UP)

        # Everything that can fail on the request is built here, before the reserve: from the
        # reserve on, every path settles (D5.7 reservations have no time limit).
        temperature = _temperature(body.get("temperature"))
        if temperature is False:
            return _error(400, "invalid_request", "temperature must be a finite number")
        tool_choice = body.get("tool_choice")
        request = ModelRequest(
            model_key=model_key,
            messages=tuple(messages),
            tools=None if tools is None else tuple(tools),
            tool_choice=tool_choice if isinstance(tool_choice, (str, Mapping)) else None,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        stream = body.get("stream") is True
        stream_options = body.get("stream_options")
        include_usage = (
            isinstance(stream_options, Mapping) and stream_options.get("include_usage") is True
        )

        await self._hold_before_boundary(grant)

        admitted = await self._ledger.reserve(
            token_hash=token_hash,
            model_key=model_key,
            amount_usd=worst_case,
            prompt_token_bound=bound,
            max_output_tokens=max_tokens,
            smallest_call_usd=smallest_call,
            now=self._clock.now(),
        )
        if isinstance(admitted, Refusal):
            return self._refused(admitted)

        return await self._dispatch_and_settle(
            admitted, request, stream=stream, include_usage=include_usage
        )

    async def _hold_before_boundary(self, grant: TokenGrant) -> None:
        """Wait until a call cannot cross a cost-gate window boundary (D5.11)."""
        while (
            wait := seconds_until_safe(self._clock.now(), self.config.boundary_guard_seconds)
        ) > 0:
            log.info(
                "dev_broker_boundary_hold",
                job_id=str(grant.job_id),
                trace_id=grant.trace_id.hex,
                wait_seconds=wait,
            )
            await self._clock.sleep(wait)

    async def _dispatch_and_settle(
        self, admitted: Admitted, request: ModelRequest, *, stream: bool, include_usage: bool
    ) -> RouteResult:
        """Dispatch under the time limit and settle exactly once, on every path."""
        trace_ctx = TraceContext(
            trace_id=admitted.trace_id.hex, session_id=str(admitted.session_id)
        )
        # Stays Cancelled() only when a BaseException (a cancellation) leaves the dispatch.
        outcome: Outcome = Cancelled()
        deadline = asyncio.timeout(self.config.call_timeout_seconds)
        try:
            async with deadline:
                response = await self._dispatcher.dispatch(request, trace_ctx)
        except TimeoutError as exc:
            if deadline.expired():
                outcome = TimedOut()
                result = _error(504, "timed_out", "the model call reached the broker's time limit")
            else:
                # Raised inside the call (a database command timeout), not by the deadline.
                outcome = failure_outcome(exc)
                self._log_failure(admitted, exc, outcome)
                result = _error(502, "provider_error", "the model call failed")
        except BudgetDenied:
            outcome = GateDenied()
            result = _error(503, "budget_denied", "the global cost cap refused the call")
        except Exception as exc:  # noqa: BLE001 — classified below; never left unsettled
            outcome = failure_outcome(exc)
            self._log_failure(admitted, exc, outcome)
            result = _error(502, "provider_error", "the model call failed")
        else:
            outcome = outcome_of(response)
            result = RouteResult(
                200,
                completion_body(response, admitted, request.model_key, created=int(time.time())),
                stream=stream,
                include_usage=include_usage,
            )
        finally:
            # Shielded: a cancellation cannot abandon the settle. A settle that fails is logged
            # and the answer still goes out: the call ran, and the stale sweep settles its row
            # at the worst case.
            try:
                await asyncio.shield(self._ledger.settle(admitted, outcome, now=self._clock.now()))
            except Exception as settle_exc:  # noqa: BLE001
                log.error(
                    "dev_broker_settle_failed",
                    job_id=str(admitted.job_id),
                    call_id=str(admitted.call_id),
                    trace_id=admitted.trace_id.hex,
                    outcome=type(outcome).__name__,
                    error_type=type(settle_exc).__name__,
                    error=str(settle_exc),
                )
        return result

    @staticmethod
    def _log_failure(admitted: Admitted, exc: Exception, outcome: Outcome) -> None:
        log.warning(
            "dev_broker_dispatch_failed",
            job_id=str(admitted.job_id),
            call_id=str(admitted.call_id),
            trace_id=admitted.trace_id.hex,
            error_type=type(exc).__name__,
            error=str(exc),
            outcome=type(outcome).__name__,
        )

    def _refused(self, refusal: Refusal) -> RouteResult:
        status = _REFUSAL_STATUS[refusal.reason]
        return _error(status, refusal.reason, f"the broker refused the call: {refusal.reason}")


#: Errors that ``LiteLLMClient.respond`` raises only before the request leaves the process.
_NOT_SENT: tuple[type[BaseException], ...] = (
    BudgetConfigError,
    UnknownBudgetRoleError,
    EgressBlockedError,
    InferenceSlotTimeout,
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
)


def failure_outcome(exc: Exception) -> Failed | ProviderError | CostUnknown:
    """Return the settle outcome of a call that raised, on the side that keeps the cap (D5.8).

    - Not sent (config, egress guard, slot wait, cost-gate database): nothing was billed.
    - ``LLMClientError`` with no cause: ``respond`` refused before dispatch (no provider, no key).
    - ``LLMClientError`` caused by a 4xx other than 408: the provider rejected the request.
    - Any other ``LLMClientError`` (5xx, timeout, a connection lost while reading the answer):
      the provider may have generated and billed, so the worst case stays spent.
    - Anything else came after the answer (parsing): worst case, and the job stops.

    Args:
        exc: What the dispatch raised.

    Returns:
        The outcome.
    """
    if isinstance(exc, _NOT_SENT):
        return Failed()
    if isinstance(exc, LLMClientError):
        cause = exc.__cause__
        if cause is None:
            return Failed()
        status = getattr(cause, "status_code", None)
        # 408 is litellm's own Timeout: the request left and may have been billed.
        if isinstance(status, int) and 400 <= status < 500 and status != 408:
            return Failed()
        return ProviderError()
    return CostUnknown()


def outcome_of(response: LLMResponse) -> Settled | CostUnknown:
    """Return the settle outcome of a completed call (D5.8).

    The cost is known only when the provider reported both token counts and the cost is positive.

    Args:
        response: The normalized response.

    Returns:
        ``Settled`` with the reported cost, else ``CostUnknown``.
    """
    usage = response.get("usage") or {}
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    cost = Decimal(str(response.get("cost_usd") or 0))
    if isinstance(input_tokens, int) and isinstance(output_tokens, int) and cost > 0:
        return Settled(cost, input_tokens, output_tokens)
    return CostUnknown()


def _usage_body(response: LLMResponse) -> dict[str, int]:
    usage = response.get("usage") or {}
    return {
        key: int(usage[key])
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
    }


def completion_body(
    response: LLMResponse, admitted: Admitted, model_key: str, *, created: int
) -> dict[str, object]:
    """Build an OpenAI ``chat.completion`` object from a normalized response.

    Args:
        response: The normalized response.
        admitted: The reserved call (its id names the completion).
        model_key: The model that the agent named.
        created: Unix time of the answer.

    Returns:
        The completion as JSON-ready data.
    """
    tool_calls = [
        {
            "id": call["id"],
            "type": "function",
            "function": {"name": call["name"], "arguments": call["arguments"]},
        }
        for call in response.get("tool_calls") or []
    ]
    message: dict[str, object] = {
        "role": "assistant",
        "content": (response.get("content") or None) if tool_calls else response.get("content", ""),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish_reason = response.get("finish_reason") or "stop"
    if tool_calls and finish_reason == "stop":
        # An OpenAI-compatible client runs the tools only on `tool_calls`.
        finish_reason = "tool_calls"
    return {
        "id": f"chatcmpl-{admitted.call_id.hex}",
        "object": "chat.completion",
        "created": created,
        "model": model_key,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": _usage_body(response),
    }


def stream_events(body: JsonObject, *, include_usage: bool) -> list[str]:
    """Turn a finished ``chat.completion`` into the SSE events of a streamed one.

    The broker dispatches without streaming (ADR-0141 D1: one dispatch path), so the answer is
    complete before the first event. The events carry the same content in the chunk format.

    Args:
        body: A body from :func:`completion_body`.
        include_usage: Whether to end with a usage chunk (``stream_options.include_usage``).

    Returns:
        The SSE lines, each ending in a blank line, the last one ``[DONE]``.
    """
    choice = body["choices"][0]  # type: ignore[index]
    message = choice["message"]
    head = {
        "id": body["id"],
        "object": "chat.completion.chunk",
        "created": body["created"],
        "model": body["model"],
    }

    def chunk(delta: dict[str, object], finish_reason: str | None) -> dict[str, object]:
        return {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

    chunks: list[dict[str, object]] = [
        chunk({"role": "assistant", "content": message.get("content")}, None)
    ]
    for index, call in enumerate(message.get("tool_calls") or []):
        chunks.append(chunk({"tool_calls": [{"index": index, **call}]}, None))
    chunks.append(chunk({}, choice["finish_reason"]))
    if include_usage:
        chunks.append({**head, "choices": [], "usage": body["usage"]})
    return [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]


async def _read_limited(request: Request, limit: int) -> bytes | None:
    """Read the body, or return ``None`` once it passes ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    data = bytearray()
    async for part in request.stream():
        data += part
        if len(data) > limit:
            return None
    return bytes(data)


def build_model_app(route: ModelRoute) -> FastAPI:
    """Build the ASGI app of the model listener. It has one route and no docs pages.

    Args:
        route: The metered route.

    Returns:
        The app.
    """
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        raw = await _read_limited(request, route.config.max_request_bytes)
        if raw is None:
            result = _error(413, "request_too_large", "the request body is too large")
        else:
            try:
                body = json.loads(raw)
            except ValueError:
                result = _error(400, "invalid_request", "the body is not JSON")
            else:
                result = await route.complete(
                    authorization=request.headers.get("authorization"), body=body
                )
        if result.status == 200 and result.stream:
            events = stream_events(result.body, include_usage=result.include_usage)

            async def emit() -> AsyncIterator[str]:
                for event in events:
                    yield event

            return StreamingResponse(emit(), media_type="text/event-stream")
        return JSONResponse(dict(result.body), status_code=result.status)

    return app
