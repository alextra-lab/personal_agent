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
import contextlib
import signal
from collections.abc import Iterator, Sequence

import asyncpg  # type: ignore[import-untyped]
import structlog
import uvicorn

from personal_agent.dev_broker.addresses import interface_addresses, select_listen_addresses
from personal_agent.dev_broker.clock import Clock, SystemClock
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


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signals to the broker.

    Two servers that each capture SIGTERM pass it from one to the other, and the last re-raise
    falls to the default handler, which kills the process before its cleanup runs. The broker
    installs one handler that stops both servers instead.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Install no signal handler."""
        yield


#: Seconds past the call limit after which a ``reserved`` call is stale, and the sweep interval.
STALE_MARGIN_SECONDS = 60.0
SWEEP_INTERVAL_SECONDS = 60.0


async def reconcile_forever(
    ledger: SpendLedger, clock: Clock, *, older_than_seconds: float, interval_seconds: float
) -> None:
    """Settle stale reservations at once, then on every interval, until cancelled.

    Args:
        ledger: The spend ledger.
        clock: The time source.
        older_than_seconds: The age past which a ``reserved`` call is stale.
        interval_seconds: Time between sweeps.
    """
    while True:
        try:
            swept = await ledger.settle_stale(
                now=clock.now(), older_than_seconds=older_than_seconds
            )
            if swept:
                log.warning("dev_broker_stale_reservations_settled", count=swept, trace_id=None)
        except Exception as exc:  # noqa: BLE001 — a failed sweep retries at the next interval
            log.error(
                "dev_broker_stale_sweep_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                trace_id=None,
            )
        await clock.sleep(interval_seconds)


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
        dispatcher=LiteLLMDispatcher(config.models),
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
        _Server(
            uvicorn.Config(
                build_model_app(route),
                host=str(addresses.model),
                port=config.model_port,
                lifespan="off",
                access_log=False,
                log_config=None,
                # In-flight model calls finish and settle before the process exits.
                timeout_graceful_shutdown=int(config.call_timeout_seconds) + 10,
            )
        ),
        _Server(
            uvicorn.Config(
                control_app,
                host=str(addresses.control),
                port=config.control_port,
                lifespan="off",
                access_log=False,
                log_config=None,
                # Relayed event streams never end on their own.
                timeout_graceful_shutdown=5,
            )
        ),
    ]

    def stop() -> None:
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    log.info(
        "dev_broker_started",
        model_listener=f"{addresses.model}:{config.model_port}",
        control_listener=f"{addresses.control}:{config.control_port}",
        job_cap_usd=float(config.job_cap_usd),
        month_cap_usd=float(config.month_cap_usd),
        models=list(config.models),
        trace_id=None,
    )
    sweeper = asyncio.create_task(
        reconcile_forever(
            ledger,
            clock,
            older_than_seconds=config.call_timeout_seconds + STALE_MARGIN_SECONDS,
            interval_seconds=SWEEP_INTERVAL_SECONDS,
        )
    )
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        sweeper.cancel()
        await pool.close()
        await cost_tracker.disconnect()
        await gate.disconnect()
        log.info("dev_broker_stopped", trace_id=None)


def main(argv: Sequence[str]) -> int:
    """Run the broker. ``--health`` never reaches here (see ``__main__`` and ``health``).

    Args:
        argv: Command-line arguments without the program name. Unused.

    Returns:
        The exit status.
    """
    from personal_agent.telemetry.logger import configure_logging  # noqa: PLC0415

    del argv
    config = load_broker_config()
    configure_logging()
    asyncio.run(run(config))
    return 0
