"""Broker process: startup checks, the two listeners, shutdown (ADR-0159 D4.3, D4.4, D5; FRE-1683).

The broker refuses to start unless:

- it has exactly one address in the ``cloud-sim`` subnet and exactly one other address (its
  ``dev-agent`` address). The control API binds the first, the model route the second, and neither
  binds ``0.0.0.0``;
- the control secret and the Scaleway key are set;
- the process egress guard allows the Scaleway host and refuses every other probe host;
- the budget role declarations agree (``validate_role_totality``).
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import asyncpg  # type: ignore[import-untyped]
import psutil  # type: ignore[import-untyped]
import structlog
import uvicorn

from personal_agent.dev_broker.clock import SystemClock
from personal_agent.dev_broker.config import BrokerConfig, load_broker_config
from personal_agent.dev_broker.dispatch import LiteLLMDispatcher
from personal_agent.dev_broker.ledger import SpendLedger
from personal_agent.dev_broker.model_route import ModelRoute, build_model_app
from personal_agent.dev_broker.relay import build_control_app, resolve_ipv4
from personal_agent.exceptions import BrokerStartupError
from personal_agent.security import DomainGuard, GuardMode

log = structlog.get_logger(__name__)

#: Hosts that the broker's egress guard must refuse. A sibling and an unrelated ``.ai`` host catch
#: an allowlist entry for a parent domain, because the guard also matches parent domains.
_MUST_REFUSE = (
    "https://other.scaleway.ai",
    "https://example.ai",
    "https://api.anthropic.com",
    "https://api.openai.com",
    "https://registry.npmjs.org",
    "https://github.com",
    "http://caddy:8601",
    "http://169.254.169.254",
)


@dataclass(frozen=True)
class ListenAddresses:
    """Where the two listeners bind.

    Attributes:
        model: The broker's ``dev-agent`` address (the model route).
        control: The broker's ``cloud-sim`` address (the control API).
        dev_agent_subnet: The ``dev-agent`` subnet (the relay's target check).
    """

    model: IPv4Address
    control: IPv4Address
    dev_agent_subnet: IPv4Network


def interface_addresses() -> list[IPv4Interface]:
    """Return every IPv4 address of this container, with its netmask.

    Returns:
        The addresses, loopback included.
    """
    found: list[IPv4Interface] = []
    for addresses in psutil.net_if_addrs().values():
        for address in addresses:
            if address.family == socket.AF_INET and address.netmask:
                found.append(IPv4Interface(f"{address.address}/{address.netmask}"))
    return found


def select_listen_addresses(
    interfaces: Sequence[IPv4Interface], *, control_subnet: IPv4Network
) -> ListenAddresses:
    """Choose the two listen addresses, or refuse to start.

    Args:
        interfaces: The container's IPv4 addresses.
        control_subnet: The ``cloud-sim`` subnet.

    Returns:
        The two addresses and the ``dev-agent`` subnet.

    Raises:
        BrokerStartupError: Unless exactly one address is in ``control_subnet`` and exactly one
            other non-loopback address exists in a subnet that does not overlap it.
    """
    usable = [i for i in interfaces if not i.ip.is_loopback]
    control = [i for i in usable if i.ip in control_subnet]
    others = [i for i in usable if i.ip not in control_subnet]
    if len(control) != 1 or len(others) != 1:
        raise BrokerStartupError(
            "the broker must be on exactly two networks: cloud-sim and dev-agent "
            f"(found {[str(i) for i in usable]})"
        )
    dev_agent = others[0]
    if dev_agent.network.overlaps(control_subnet):
        raise BrokerStartupError(f"the dev-agent subnet {dev_agent.network} overlaps cloud-sim")
    return ListenAddresses(
        model=dev_agent.ip, control=control[0].ip, dev_agent_subnet=dev_agent.network
    )


def check_egress_guard(guard: DomainGuard, provider_url: str) -> None:
    """Refuse to start unless the guard allows the provider and nothing else that it probes.

    Args:
        guard: The process egress guard that ``LiteLLMClient`` uses.
        provider_url: The provider's base URL.

    Raises:
        BrokerStartupError: If the guard is not an allowlist, refuses the provider, or allows any
            probe host.
    """
    if guard.mode is not GuardMode.ALLOWLIST:
        raise BrokerStartupError(
            f"the broker's egress guard must be an allowlist, not {guard.mode}"
        )
    if not guard.check_url(provider_url).allowed:
        raise BrokerStartupError(f"the egress guard refuses the provider {provider_url}")
    allowed = [url for url in _MUST_REFUSE if guard.check_url(url).allowed]
    if allowed:
        raise BrokerStartupError(f"the egress guard allows more than the provider: {allowed}")


async def run(config: BrokerConfig) -> None:
    """Start the broker and serve until a signal stops it.

    Args:
        config: The broker config.

    Raises:
        BrokerStartupError: If a startup check fails.
    """
    from personal_agent.config import load_model_config  # noqa: PLC0415
    from personal_agent.config.settings import get_settings  # noqa: PLC0415
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
        validate_role_totality,
    )
    from personal_agent.llm_client.cost_tracker import (  # noqa: PLC0415
        _normalize_asyncpg_dsn,
        get_cost_tracker_service,
    )
    from personal_agent.llm_client.pricing import register_model_pricing  # noqa: PLC0415
    from personal_agent.security import get_domain_guard  # noqa: PLC0415

    settings = get_settings()
    secret = settings.dev_broker_control_secret
    if not secret:
        raise BrokerStartupError("AGENT_DEV_BROKER_CONTROL_SECRET is not set")
    if not settings.scaleway_secret_key:
        raise BrokerStartupError("AGENT_SCALEWAY_SECRET_KEY is not set")

    addresses = select_listen_addresses(interface_addresses(), control_subnet=config.control_subnet)
    model_config = load_model_config()
    provider = model_config.providers["scaleway"]
    assert provider.base_url is not None
    check_egress_guard(get_domain_guard(), provider.base_url)
    register_model_pricing(model_config)

    budget_config = load_budget_config()
    validate_role_totality(budget_config)
    gate = CostGate(config=budget_config, db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    cost_tracker = get_cost_tracker_service()
    await cost_tracker.connect()
    pool = await asyncpg.create_pool(
        _normalize_asyncpg_dsn(settings.database_url), min_size=1, max_size=10, command_timeout=10
    )
    clock = SystemClock()
    ledger = SpendLedger(pool, job_cap_usd=config.job_cap_usd, month_cap_usd=config.month_cap_usd)
    route = ModelRoute(
        config=config,
        ledger=ledger,
        dispatcher=LiteLLMDispatcher(),
        catalog=model_config.models,
        clock=clock,
    )
    control_app = build_control_app(
        secret=secret,
        ledger=ledger,
        resolver=resolve_ipv4,
        subnet=addresses.dev_agent_subnet,
        opencode_port=config.opencode_port,
        clock=clock,
    )
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                build_model_app(route),
                host=str(addresses.model),
                port=config.model_port,
                lifespan="off",
                access_log=False,
                log_config=None,
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                control_app,
                host=str(addresses.control),
                port=config.control_port,
                lifespan="off",
                access_log=False,
                log_config=None,
            )
        ),
    ]
    log.info(
        "dev_broker_started",
        model_listener=f"{addresses.model}:{config.model_port}",
        control_listener=f"{addresses.control}:{config.control_port}",
        job_cap_usd=float(config.job_cap_usd),
        month_cap_usd=float(config.month_cap_usd),
        models=list(config.models),
        trace_id=None,
    )
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        await pool.close()
        await cost_tracker.disconnect()
        await gate.disconnect()
        log.info("dev_broker_stopped", trace_id=None)


def health(config: BrokerConfig) -> bool:
    """Return whether the control listener answers its health route.

    Args:
        config: The broker config.

    Returns:
        ``True`` when ``/healthz`` answers ``ok``.
    """
    addresses = select_listen_addresses(interface_addresses(), control_subnet=config.control_subnet)
    url = f"http://{addresses.control}:{config.control_port}/healthz"
    with urllib.request.urlopen(url, timeout=3) as answer:  # noqa: S310 — fixed http URL
        return bool(json.load(answer).get("status") == "ok")


def main(argv: Sequence[str]) -> int:
    """Run the broker, or its health check with ``--health``.

    Args:
        argv: Command-line arguments without the program name.

    Returns:
        The exit status.
    """
    from personal_agent.telemetry.logger import configure_logging  # noqa: PLC0415

    config = load_broker_config()
    if "--health" in argv:
        try:
            return 0 if health(config) else 1
        except OSError, BrokerStartupError, ValueError:
            return 1
    configure_logging()
    asyncio.run(run(config))
    return 0
