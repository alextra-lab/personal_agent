"""FRE-1728: the loopback bridge between MemAudit's environment and the eval stack.

MemAudit stays in its own environment, out of this project's lock file, so it cannot import
``IsolatedArmRunner``. Its adapter reaches Seshat only through this app, over HTTP. The app owns the
runner, the wipe, the graph read, the settle wait and the spend stop.

**Why the wipe deletes captures.** ``consolidate_recent_captures`` reads seven days of capture files and
skips a capture only if its ``Turn`` is in the graph. After a graph wipe alone, it extracts every earlier
capture again: an earlier user's facts return, and the extraction is paid again.

**Why the wipe restarts the gateway.** Only startup creates the owner's ``Person`` and ``Agent`` nodes, and
the restart also clears process caches.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from scripts.eval.eval_isolation import IsolatedArmRunner, wait_for_gateway_idle
from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS
from scripts.eval.fre1728.spend import SpendCapReached, SpendGuard

GATEWAY_CONTAINERS: dict[str, str] = {
    "control": "cloud-sim-seshat-gateway-control",
    "treatment": "cloud-sim-seshat-gateway-treatment",
    "stub": "fre1728-stub-gateway",
}
_CAPTURES_DIR = "/app/telemetry/captains_log/captures"
_HEALTH_TIMEOUT_S = 180.0

_ENTITIES = (
    "MATCH (e:Entity) RETURN 'entity' AS kind, e.name AS name, "
    "coalesce(e.description, '') AS content, coalesce(e.entity_type, '') AS type"
)
_CLAIMS = (
    "MATCH (c:Claim) WHERE c.superseded_by IS NULL RETURN 'claim' AS kind, "
    "coalesce(c.facet, '') AS name, c.content AS content, '' AS type"
)
_STANCES = (
    "MATCH ()-[s:HAS_STANCE]->(c) WHERE s.valid_to IS NULL RETURN 'stance' AS kind, "
    "coalesce(c.name, '') AS name, coalesce(s.affect, '') AS content, '' AS type"
)


class GatewayOps(Protocol):
    """The container actions that the bridge needs."""

    async def clear_captures(self) -> None:
        """Delete the gateway's capture files."""
        ...

    async def stop(self) -> None:
        """Stop the gateway, so background work ends."""
        ...


class TurnRequest(BaseModel):
    """One user message."""

    model_config = ConfigDict(frozen=True)

    message: str
    session_id: str | None = None


class SettleRequest(BaseModel):
    """The turns that must be in the graph."""

    model_config = ConfigDict(frozen=True)

    trace_ids: list[str]


class UsageRequest(BaseModel):
    """One MemAudit call's tokens."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int
    completion_tokens: int


@dataclass
class DockerGatewayOps:
    """Container actions for one eval gateway.

    Attributes:
        arm: An ``EVAL_ARMS`` key. No other container can be named.
    """

    arm: str

    @property
    def container(self) -> str:
        """Return the arm's container name."""
        return GATEWAY_CONTAINERS[self.arm]

    async def clear_captures(self) -> None:
        """Delete the capture files inside the gateway."""
        await _docker("exec", self.container, "find", _CAPTURES_DIR, "-type", "f", "-delete")

    async def restart(self) -> None:
        """Restart the gateway and wait for ``/health``.

        Raises:
            RuntimeError: If the gateway is not healthy in time.
        """
        await _docker("restart", self.container)
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.get(f"{EVAL_ARMS[self.arm]}/health", timeout=5.0)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(3.0)
        raise RuntimeError(f"{self.container} was not healthy after {_HEALTH_TIMEOUT_S:.0f} s")

    async def stop(self) -> None:
        """Stop the gateway."""
        await _docker("stop", self.container)


async def _docker(*args: str) -> None:
    await asyncio.to_thread(
        subprocess.run, ["docker", *args], check=True, capture_output=True, text=True
    )


@dataclass
class BridgeDeps:
    """Everything the bridge acts on.

    Attributes:
        runner: Drives the turns. It must have ``wipe_on_new_session=False``.
        http: Client for the eval gateway.
        es: Client for eval Elasticsearch.
        guard: The spend stop.
        ops: Container actions.
        arm: The ``EVAL_ARMS`` key to drive.
        read_memories: Reads the structured facts of the eval graph.
        find_turns: Returns the given trace ids whose ``Turn`` is in the eval graph.
        count_budget_denied: Counts denied extraction attempts since the run started.
    """

    runner: IsolatedArmRunner
    http: httpx.AsyncClient
    es: httpx.AsyncClient
    guard: SpendGuard
    ops: GatewayOps
    arm: str
    read_memories: Callable[[], Awaitable[list[dict[str, str]]]]
    find_turns: Callable[[Sequence[str]], Awaitable[list[str]]]
    count_budget_denied: Callable[[], Awaitable[int]]
    _stopped: bool = field(default=False, init=False)


def create_app(deps: BridgeDeps) -> FastAPI:
    """Build the bridge.

    Args:
        deps: What the bridge acts on.

    Returns:
        The FastAPI app.
    """
    app = FastAPI()
    app.state.deps = deps

    async def cap_response() -> JSONResponse | None:
        try:
            await deps.guard.check()
        except SpendCapReached as exc:
            if not app.state.deps._stopped:
                await app.state.deps.ops.stop()
                app.state.deps._stopped = True
            return JSONResponse({"stop": True, "detail": str(exc)}, status_code=402)
        return None

    @app.post("/turn", response_model=None)
    async def turn(request: TurnRequest) -> JSONResponse | dict[str, str]:
        refused = await cap_response()
        if refused is not None:
            return refused
        result = await deps.runner.run_turn(
            deps.http,
            deps.es,
            request.message,
            arm=deps.arm,
            session_id=request.session_id,
            wait_settle=False,
        )
        return {
            "session_id": result.session_id,
            "trace_id": result.trace_id,
            "response": result.response,
        }

    @app.post("/wipe")
    async def wipe() -> dict[str, object]:
        waited = await wait_for_gateway_idle(deps.es)
        await deps.ops.clear_captures()
        await deps.runner.wipe(deps.es)
        return {"waited_s": waited, "residual_memories": await deps.read_memories()}

    @app.post("/settle")
    async def settle(request: SettleRequest) -> dict[str, object]:
        waited = await wait_for_gateway_idle(deps.es)
        found = set(await deps.find_turns(request.trace_ids))
        return {
            "waited_s": waited,
            "missing_turns": [t for t in request.trace_ids if t not in found],
            "budget_denied": await deps.count_budget_denied(),
        }

    @app.get("/memories")
    async def memories() -> list[dict[str, str]]:
        return await deps.read_memories()

    @app.post("/usage")
    async def usage(request: UsageRequest) -> dict[str, bool]:
        deps.guard.add_usage(request.prompt_tokens, request.completion_tokens)
        return {"ok": True}

    @app.get("/spend")
    async def spend() -> dict[str, object]:
        refused = await cap_response()
        return {
            "spent_usd": await deps.guard.spent(),
            "cap_usd": deps.guard.cap_usd,
            "stop": refused is not None,
        }

    return app


async def read_graph_memories(driver: object) -> list[dict[str, str]]:
    """Read the facts of the eval graph: entities, live claims and live stances.

    Raw ``Turn`` messages stay out. They hold every disclosed sentence, so a write failure could not
    appear if they counted as facts.

    Args:
        driver: A ``neo4j.AsyncDriver`` from ``create_eval_driver``, the only eval graph driver.

    Returns:
        One record per fact, each with ``kind``, ``name``, ``content`` and ``type``.
    """
    records: list[dict[str, str]] = []
    async with driver.session() as session:  # type: ignore[attr-defined]
        for query in (_ENTITIES, _CLAIMS, _STANCES):
            result = await session.run(query)
            records.extend([dict(record) async for record in result])
    return records


async def find_graph_turns(driver: object, trace_ids: Sequence[str]) -> list[str]:
    """Return the trace ids whose ``Turn`` is in the eval graph.

    Args:
        driver: A ``neo4j.AsyncDriver`` from ``create_eval_driver``.
        trace_ids: The ids to look for. A ``Turn``'s ``turn_id`` equals its trace id.

    Returns:
        The ids found.
    """
    async with driver.session() as session:  # type: ignore[attr-defined]
        result = await session.run(
            "MATCH (t:Turn) WHERE t.turn_id IN $ids RETURN t.turn_id AS id", ids=list(trace_ids)
        )
        return [record["id"] async for record in result]
