"""Rank each page fixture with the four arms of ADR-0158 D4.

The ranking is ``personal_agent.tools.passages.rank_passages``, the code T3 ships (D1.8). This
module supplies only the rerank call: Voyage ``rerank-2.5`` over HTTP, with the key from the
secrets store. It records no vendor cost to Postgres and has no fallback, unlike
``memory.reranker.rerank``: the harness writes nothing, and it measures ``rerank-2.5`` alone.

The call is cached by its exact input, so R4 reuses the R1 or R3 work of the same page and costs
no second call. The latency charged to an arm is its local compute time plus the wall time of
every vendor call it makes, cached or not, so R4's latency is what a live fetch would see.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from personal_agent.config.model_loader import load_model_config, resolve_role_definition
from personal_agent.tools.passages import Passage, RankerArm, RerankCall, rank_passages

ARMS: tuple[RankerArm, ...] = ("R1", "R2", "R3", "R4")
SECRET_NAME = "seshat/AGENT_VOYAGE_API_KEY"


@dataclass(frozen=True)
class RerankModel:
    """The production reranker role's model, endpoint and price."""

    model_id: str
    endpoint: str
    usd_per_token: float


def reranker_model() -> RerankModel:
    """Read the reranker role from the model catalog, the source production bills from.

    Returns:
        The model.

    Raises:
        RuntimeError: If the role is missing, is not Voyage, or has no price.
    """
    model = resolve_role_definition("reranker", config=load_model_config())
    if model is None or not model.endpoint or "voyageai.com" not in model.endpoint:
        raise RuntimeError("the reranker role is not Voyage")
    if model.input_cost_per_token is None:
        raise RuntimeError("the reranker role has no price")
    return RerankModel(str(model.id), model.endpoint.rstrip("/"), float(model.input_cost_per_token))


def voyage_key() -> str:
    """Read the Voyage key from the ``pass`` store. The key is never printed or logged.

    Returns:
        The key.
    """
    done = subprocess.run(["pass", "show", SECRET_NAME], capture_output=True, text=True, check=True)
    key = done.stdout.splitlines()[0].strip() if done.stdout else ""
    if not key:
        raise RuntimeError(f"{SECRET_NAME} is empty")
    return key


class VoyageRerank:
    """A cached, rate-limited ``rerank-2.5`` call that records tokens and wall time."""

    def __init__(
        self, client: httpx.AsyncClient, model: RerankModel, key: str, concurrency: int = 4
    ) -> None:
        """Create the call.

        Args:
            client: The HTTP client.
            model: The reranker model.
            key: The Voyage key.
            concurrency: Calls in flight.
        """
        self._client = client
        self._model = model
        self._headers = {"Authorization": f"Bearer {key}"}
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[tuple[str, tuple[str, ...]], tuple[RerankCall, float]] = {}
        self.calls = 0
        self.tokens = 0

    async def timed(self, query: str, documents: Sequence[str]) -> tuple[RerankCall, float, float]:
        """Rerank, returning the result, the vendor wall time, and the time actually waited.

        Args:
            query: The focus.
            documents: The passage texts.

        Returns:
            The call, its original wall time in seconds, and the seconds this call waited.
        """
        t0 = time.monotonic()
        key = (query, tuple(documents))
        if key in self._cache:
            call, wall = self._cache[key]
            return call, wall, time.monotonic() - t0
        delay = 2.0
        for attempt in range(6):
            async with self._sem:
                start = time.monotonic()
                r = await self._client.post(
                    f"{self._model.endpoint}/rerank",
                    json={
                        "model": self._model.model_id,
                        "query": query,
                        "documents": list(documents),
                    },
                    headers=self._headers,
                    timeout=60.0,
                )
                wall = time.monotonic() - start
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 5:
                    r.raise_for_status()
                await asyncio.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            data = r.json()
            scores = [0.0] * len(documents)
            for item in data["data"]:
                scores[int(item["index"])] = float(item["relevance_score"])
            call = RerankCall(
                scores=scores, tokens=int(data.get("usage", {}).get("total_tokens", 0))
            )
            self._cache[key] = (call, wall)
            self.calls += 1
            self.tokens += call.tokens
            return call, wall, time.monotonic() - t0
        raise RuntimeError("unreachable")


async def rank_page(
    focus: str, passages: Sequence[Passage], voyage: VoyageRerank
) -> dict[str, dict[str, object]]:
    """Rank one page with every arm.

    Args:
        focus: The helper's ``spec_task`` (D5.9).
        passages: The page's passages.
        voyage: The rerank call.

    Returns:
        Per arm: ``order``, ``tokens``, ``calls``, ``sent`` and ``latency_s``.
    """
    out: dict[str, dict[str, object]] = {}
    for arm in ARMS:
        charged = 0.0
        waited = 0.0

        async def rerank(query: str, documents: Sequence[str]) -> RerankCall:
            nonlocal charged, waited
            call, wall, wait = await voyage.timed(query, documents)
            charged += wall
            waited += wait
            return call

        t0 = time.monotonic()
        outcome = await rank_passages(arm, focus, passages, rerank)
        elapsed = time.monotonic() - t0
        out[arm] = {
            "order": outcome.order,
            "tokens": outcome.rerank_tokens,
            "calls": outcome.rerank_calls,
            "sent": outcome.sent_to_vendor,
            "latency_s": round(max(elapsed - waited, 0.0) + charged, 4),
        }
    return out
