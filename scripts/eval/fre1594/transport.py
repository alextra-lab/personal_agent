"""FRE-1594 / ADR-0156 A5 (T-B): how the spike sends a request and reads the reply.

- :class:`LlamaTransport` sends to llama.cpp directly, streamed, as the gateway does. The history is
  sanitised and its tool-call indices normalised, as ``LiteLLMClient`` does before a local call
  (``litellm_client.py:1760-1805``). The reply goes through the production aggregator and adapter,
  so a ``<tool_call>`` left in the text is recovered exactly as production recovers it. The
  ``timings`` of the last chunk are kept for AC-2.
- :class:`CloudTransport` sends through the production client, ``LiteLLMClient.respond``, with the
  cost gate on the eval Postgres (``fre1537.cloud.CloudSession``). It enforces its own cap: the
  cost in the run's ledger plus a reservation for the full request and its output budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537.common import append_jsonl, read_jsonl, utc_now
from scripts.eval.fre1594.reveal import request_bytes

from personal_agent.cost_gate import BudgetDenied
from personal_agent.llm_client.adapters import (
    _aggregate_streaming_chunks,
    adapt_chat_completions_response,
    normalise_tool_call_indices,
)
from personal_agent.llm_client.history_sanitiser import sanitise_messages
from personal_agent.llm_client.types import LLMClientError, LLMInvalidResponse, ModelRole, ToolCall

TIMING_KEYS = ("cache_n", "prompt_n", "predicted_n", "prompt_ms", "predicted_ms")
CACHE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens")
_ERROR_CHARS = 300
_CHARS_PER_TOKEN = 3  # a low estimate, so the reserved input cost is high
_NO_BUDGET = 64_000  # the output reservation of a deployment that declares no budget


def sha256(data: bytes) -> str:
    """Return the hex SHA-256 of some bytes."""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Reply:
    """What the spike reads from one call.

    Attributes:
        ok: HTTP 200 and a reply that the production adapter parsed (AC-1 step 5).
        status: The HTTP status, or None for a transport error or a cloud call.
        error: The error text, or None.
        content: The reply text, think tags stripped.
        reasoning: The reasoning trace, or None.
        tool_calls: The tool calls, as production reads them.
        structured: True if the server returned the calls as structured ``tool_calls``. False
            when the text parser recovered them, or when there are none.
        finish_reason: The finish reason.
        timings: llama.cpp ``timings`` (local), else empty.
        usage: The usage block.
        secs: Wall seconds.
        system_fingerprint: The engine build, when reported.
        cost_usd: The call's cost (cloud), else 0.
        request_sha256: SHA-256 of the canonical request body that was sent.
    """

    ok: bool
    status: int | None
    error: str | None
    content: str
    reasoning: str | None
    tool_calls: tuple[ToolCall, ...]
    structured: bool
    finish_reason: str | None
    timings: Mapping[str, float | None]
    usage: Mapping[str, object]
    secs: float
    system_fingerprint: str | None
    cost_usd: float
    request_sha256: str
    extra: Mapping[str, object] = field(default_factory=dict)

    def record(self) -> dict[str, object]:
        """Return the row form: every field, the text replaced by its length."""
        row = asdict(self)
        row["content_chars"] = len(row.pop("content"))
        row["reasoning_chars"] = len(row.pop("reasoning") or "")
        row["tool_calls"] = [dict(c) for c in self.tool_calls]
        return row


class Transport(Protocol):
    """Sends one request body and returns the reply."""

    def send(self, body: Mapping[str, object]) -> Reply:
        """Send one request.

        Args:
            body: The request body.

        Returns:
            The reply.
        """
        ...


def _failed(sha: str, status: int | None, error: str, secs: float) -> Reply:
    return Reply(
        ok=False,
        status=status,
        error=error[:_ERROR_CHARS],
        content="",
        reasoning=None,
        tool_calls=(),
        structured=False,
        finish_reason=None,
        timings={},
        usage={},
        secs=round(secs, 2),
        system_fingerprint=None,
        cost_usd=0.0,
        request_sha256=sha,
    )


def parse_stream(lines: Iterable[str]) -> tuple[list[dict[str, object]], dict[str, float | None]]:
    """Read the SSE lines of a streamed reply.

    Args:
        lines: The response lines.

    Returns:
        The parsed chunks and the ``timings`` of the last chunk that carried them.
    """
    chunks: list[dict[str, object]] = []
    timings: dict[str, float | None] = {}
    for line in lines:
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        chunks.append(chunk)
        found = chunk.get("timings")
        if isinstance(found, Mapping):
            timings = {k: found.get(k) for k in TIMING_KEYS}
    return chunks, timings


def wire_messages(messages: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return a history as the production client sends it: sanitised, tool-call indices filled.

    Args:
        messages: The request messages.

    Returns:
        The messages after ``sanitise_messages`` and ``normalise_tool_call_indices``, the two passes
        that ``LiteLLMClient`` runs before every call (``litellm_client.py:1805``).
    """
    clean, _ = sanitise_messages([dict(m) for m in messages], trace_id=None, emit_telemetry=False)
    return normalise_tool_call_indices(clean)


class LlamaTransport:
    """Sends to llama.cpp, streamed, and reads the reply as the production client does."""

    def __init__(self, client: httpx.Client, url: str, model: str) -> None:
        """Build the transport.

        Args:
            client: The HTTP client.
            url: The chat-completions URL.
            model: The llama.cpp model name.
        """
        self._client = client
        self._url = url
        self._model = model

    def wire_body(self, body: Mapping[str, object]) -> dict[str, object]:
        """Return the body as it goes on the wire.

        Args:
            body: The request body.

        Returns:
            The body with the sanitised, index-normalised history, the model and the stream flags.
        """
        messages = body["messages"]
        assert isinstance(messages, list)
        return {
            **body,
            "messages": wire_messages(messages),
            "model": self._model,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

    def send(self, body: Mapping[str, object]) -> Reply:
        """Send one request and read the reply.

        Args:
            body: The request body.

        Returns:
            The reply. A transport error, a non-200 status or an unreadable stream gives
            ``ok=False``.
        """
        wire = self.wire_body(body)
        sha = sha256(request_bytes(wire))
        started = time.monotonic()
        try:
            with self._client.stream("POST", self._url, json=wire) as response:
                if response.status_code != httpx.codes.OK:
                    response.read()
                    return _failed(
                        sha, response.status_code, response.text, time.monotonic() - started
                    )
                chunks, timings = parse_stream(response.iter_lines())
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            return _failed(sha, None, f"{type(exc).__name__}: {exc}", time.monotonic() - started)
        secs = time.monotonic() - started
        try:
            aggregated = _aggregate_streaming_chunks(chunks)
            adapted = adapt_chat_completions_response(aggregated)
        except (LLMInvalidResponse, json.JSONDecodeError) as exc:
            return _failed(sha, httpx.codes.OK, f"unreadable reply: {exc}", secs)
        message = aggregated["choices"][0]["message"]
        fingerprint = aggregated.get("system_fingerprint")
        return Reply(
            ok=True,
            status=httpx.codes.OK,
            error=None,
            content=str(adapted.get("content") or ""),
            reasoning=adapted.get("reasoning_trace"),
            tool_calls=tuple(adapted.get("tool_calls") or ()),
            structured=bool(message.get("tool_calls")),
            finish_reason=adapted.get("finish_reason"),
            timings=timings,
            usage=dict(aggregated.get("usage") or {}),
            secs=round(secs, 2),
            system_fingerprint=fingerprint if isinstance(fingerprint, str) else None,
            cost_usd=0.0,
            request_sha256=sha,
        )


def reserve_usd(
    body: Mapping[str, object], target: cloud_mod.CloudTarget, output_budget: int
) -> float:
    """Return the worst-case cost of one cloud call.

    Args:
        body: The request body.
        target: The deployment, with its declared per-token rates.
        output_budget: The call's output token budget.

    Returns:
        The full request at the input rate (3 characters per token, a low estimate), plus the
        output budget at the output rate.
    """
    return (
        len(request_bytes(body)) / _CHARS_PER_TOKEN * target.input_cost
        + output_budget * target.output_cost
    )


class CloudTransport(cloud_mod.CloudSession):
    """Sends through the production client, under the run's own cost cap."""

    def __init__(
        self,
        target: cloud_mod.CloudTarget,
        *,
        ledger: Path,
        max_usd: float,
        gate: object | None = None,
    ) -> None:
        """Build the transport.

        Args:
            target: The managed deployment, in its default mode.
            ledger: The run's cost ledger. Every call's cost is appended as soon as the call
                returns, so a draw that stops part way still counts.
            max_usd: The cap in USD.
            gate: A cost gate that the caller registered. Default: a real gate on the eval
                Postgres (checked by ``CloudSession``).
        """
        super().__init__(target, gate=gate)
        self._ledger = ledger
        self._max_usd = max_usd

    def spent(self) -> float:
        """Return the cost of every call that the ledger records."""
        total = 0.0
        for row in read_jsonl(self._ledger):
            cost = row.get("cost_usd")
            total += float(cost) if isinstance(cost, int | float) else 0.0
        return total

    def _book(self, sha: str, cost: float, ok: bool) -> None:
        append_jsonl(
            self._ledger, {"at": utc_now(), "request_sha256": sha, "ok": ok, "cost_usd": cost}
        )

    def reserve(self, body: Mapping[str, object]) -> float:
        """Refuse a call that could reach the cap. Return its worst-case cost.

        Args:
            body: The request body.

        Returns:
            The reservation: the full request at the input rate, plus the output budget at the
            output rate.

        Raises:
            SystemExit: If the spent cost plus this reservation reaches the cap.
        """
        budget = self._client.max_tokens or self.target.definition.max_tokens or _NO_BUDGET
        reservation = reserve_usd(body, self.target, budget)
        spent = self.spent()
        if spent + reservation >= self._max_usd:
            raise SystemExit(
                f"cost cap of {self._max_usd:g} USD reached: {spent:.4f} USD spent, this call "
                f"reserves {reservation:.4f} USD"
            )
        return reservation

    def send(self, body: Mapping[str, object]) -> Reply:
        """Send one request through ``LiteLLMClient.respond``.

        Args:
            body: The request body (``messages`` and ``tools``).

        Returns:
            The reply. A provider, transport or timeout error gives ``ok=False``. A failed call is
            counted at its reservation, because it may still be billed.

        Raises:
            SystemExit: The cap is reached, or the cost gate denies the call.
        """
        reservation = self.reserve(body)
        sha = sha256(request_bytes(body))
        started = time.monotonic()
        try:
            response = self._loop.run_until_complete(
                asyncio.wait_for(
                    self._client.respond(
                        role=ModelRole.PRIMARY,
                        messages=list(body["messages"]),  # type: ignore[call-overload]
                        tools=list(body["tools"]),  # type: ignore[call-overload]
                        trace_ctx=self._ctx,
                    ),
                    timeout=cloud_mod.REQUEST_TIMEOUT_S,
                )
            )
        except BudgetDenied as exc:
            raise SystemExit(f"the cost gate denied the call: {exc}") from exc
        except (LLMClientError, TimeoutError) as exc:
            self._book(sha, round(reservation, 8), ok=False)
            failed = _failed(sha, None, f"{type(exc).__name__}: {exc}", time.monotonic() - started)
            return Reply(**{**asdict(failed), "cost_usd": round(reservation, 8)})
        usage = dict(response.get("usage") or {})
        floor = (
            int(usage.get("prompt_tokens") or 0) * self.target.input_cost
            + int(usage.get("completion_tokens") or 0) * self.target.output_cost
        )
        cost = round(max(float(response.get("cost_usd") or 0.0), floor), 8)
        self._book(sha, cost, ok=True)
        return Reply(
            ok=True,
            status=None,
            error=None,
            content=str(response.get("content") or ""),
            reasoning=response.get("reasoning_trace"),
            tool_calls=tuple(response.get("tool_calls") or ()),
            structured=True,
            finish_reason=response.get("finish_reason"),
            timings={},
            usage=usage,
            secs=round(time.monotonic() - started, 2),
            system_fingerprint=None,
            cost_usd=cost,
            request_sha256=sha,
            extra={"served_model": (response.get("raw") or {}).get("model")},
        )
