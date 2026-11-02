"""FRE-1728: wires the bridge to one eval gateway and its stores."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import httpx
from scripts.eval.eval_isolation import IsolatedArmRunner, create_eval_driver
from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS
from scripts.eval.fre1728.bridge import (
    BridgeDeps,
    DockerGatewayOps,
    find_graph_turns,
    read_graph_memories,
)
from scripts.eval.fre1728.spend import (
    SpendGuard,
    postgres_budget_denied_reader,
    postgres_spend_reader,
)
from scripts.eval.gateway_freshness import assert_gateway_fresh, repo_root

_EVAL_POSTGRES_ADDRESS = "localhost:5434"


def eval_postgres_dsn(database: str, host_env: Mapping[str, str] | None = None) -> str:
    """Build the connection string for a database on ``postgres-eval``.

    Args:
        database: The database name.
        host_env: The environment that holds ``POSTGRES_PASSWORD``.

    Returns:
        The connection string.

    Raises:
        SystemExit: If the password is not set.
    """
    env = os.environ if host_env is None else host_env
    password = env.get("POSTGRES_PASSWORD")
    if not password:
        raise SystemExit("set POSTGRES_PASSWORD in the environment")
    return f"postgresql://agent:{password}@{_EVAL_POSTGRES_ADDRESS}/{database}"


@dataclass
class Stack:
    """An open connection to one eval gateway and its stores.

    Attributes:
        deps: The bridge's dependencies.
        driver: The eval graph driver.
    """

    deps: BridgeDeps
    driver: object

    async def assert_gateway_fresh(self) -> None:
        """Refuse a gateway that does not run the current tree's build.

        Raises:
            GatewayStaleError: If the gateway is stale or reports no build.
        """
        await assert_gateway_fresh(self.deps.http, EVAL_ARMS[self.deps.arm], repo_root())

    async def close(self) -> None:
        """Close every connection."""
        await self.deps.http.aclose()
        await self.deps.es.aclose()
        await self.driver.close()  # type: ignore[attr-defined]


def open_stack(arm: str, database: str, cap_usd: float, since: datetime) -> Stack:
    """Open the stack for one arm.

    The wipe restarts the gateway, because only startup creates the owner's identity nodes.

    Args:
        arm: An ``EVAL_ARMS`` key.
        database: The database on ``postgres-eval`` that the gateway uses.
        cap_usd: The spend cap.
        since: The run's start.

    Returns:
        The stack.
    """
    dsn = eval_postgres_dsn(database)
    driver = create_eval_driver()
    ops = DockerGatewayOps(arm)
    runner = IsolatedArmRunner(driver=driver, reseed=ops.restart, wipe_on_new_session=False)

    async def read_memories() -> list[dict[str, str]]:
        return await read_graph_memories(driver)

    async def find_turns(trace_ids: Sequence[str]) -> list[str]:
        return await find_graph_turns(driver, trace_ids)

    deps = BridgeDeps(
        runner=runner,
        http=httpx.AsyncClient(),
        es=httpx.AsyncClient(),
        guard=SpendGuard(read_gateway_spend=postgres_spend_reader(dsn, since), cap_usd=cap_usd),
        ops=ops,
        arm=arm,
        read_memories=read_memories,
        find_turns=find_turns,
        count_budget_denied=postgres_budget_denied_reader(dsn, since),
    )
    return Stack(deps=deps, driver=driver)
