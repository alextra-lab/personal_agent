# ruff: noqa: D102
"""Broker startup: the two listeners and the egress guard (ADR-0159 D4.3, D5.2; FRE-1683 AC-5).

The live port scan is ``scripts/verify_dev_broker.py``. These tests cover the decision that it
checks: each app binds one interface address, never ``0.0.0.0``, and each app has only its routes.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import pytest
from fastapi.routing import APIRoute
from personal_agent.dev_broker.server import (
    ListenAddresses,
    check_egress_guard,
    select_listen_addresses,
)

from personal_agent.exceptions import BrokerStartupError
from personal_agent.security import DomainGuard, GuardMode

_CLOUD_SIM = IPv4Network("172.25.0.0/16")
_SCALEWAY = "https://api.scaleway.ai/v1"


class TestListenAddresses:
    def test_one_address_per_network(self) -> None:
        chosen = select_listen_addresses(
            [
                IPv4Interface("127.0.0.1/8"),
                IPv4Interface("172.25.0.14/16"),
                IPv4Interface("172.19.0.3/16"),
            ],
            control_subnet=_CLOUD_SIM,
        )
        assert chosen == ListenAddresses(
            model=IPv4Address("172.19.0.3"),
            control=IPv4Address("172.25.0.14"),
            dev_agent_subnet=IPv4Network("172.19.0.0/16"),
        )

    @pytest.mark.parametrize(
        "interfaces",
        [
            # not on cloud-sim
            [IPv4Interface("172.19.0.3/16")],
            # not on dev-agent
            [IPv4Interface("172.25.0.14/16")],
            # a third network: the dev-agent address is ambiguous
            [
                IPv4Interface("172.25.0.14/16"),
                IPv4Interface("172.19.0.3/16"),
                IPv4Interface("172.21.0.2/16"),
            ],
            # two cloud-sim addresses
            [
                IPv4Interface("172.25.0.14/16"),
                IPv4Interface("172.25.0.15/16"),
                IPv4Interface("172.19.0.3/16"),
            ],
            # a dev-agent subnet that overlaps cloud-sim
            [IPv4Interface("172.25.0.14/16"), IPv4Interface("172.24.0.3/15")],
        ],
    )
    def test_anything_but_exactly_two_networks_refuses_to_start(
        self, interfaces: list[IPv4Interface]
    ) -> None:
        with pytest.raises(BrokerStartupError):
            select_listen_addresses(interfaces, control_subnet=_CLOUD_SIM)

    def test_neither_address_is_the_wildcard(self) -> None:
        chosen = select_listen_addresses(
            [IPv4Interface("172.25.0.14/16"), IPv4Interface("172.19.0.3/16")],
            control_subnet=_CLOUD_SIM,
        )
        assert not chosen.model.is_unspecified
        assert not chosen.control.is_unspecified


class TestEgressGuard:
    def test_an_allowlist_of_scaleway_only_passes(self) -> None:
        guard = DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({"api.scaleway.ai"}))
        check_egress_guard(guard, _SCALEWAY)

    @pytest.mark.parametrize(
        "guard",
        [
            DomainGuard(mode=GuardMode.BLOCKLIST),
            DomainGuard(mode=GuardMode.OFF),
            DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset()),
            DomainGuard(
                mode=GuardMode.ALLOWLIST,
                allowlist=frozenset({"api.scaleway.ai", "api.anthropic.com"}),
            ),
            DomainGuard(mode=GuardMode.ALLOWLIST, allowlist=frozenset({"scaleway.ai", "ai"})),
        ],
    )
    def test_any_other_guard_refuses_to_start(self, guard: DomainGuard) -> None:
        with pytest.raises(BrokerStartupError):
            check_egress_guard(guard, _SCALEWAY)


class TestApps:
    def test_the_model_app_has_only_the_chat_route(self) -> None:
        from personal_agent.dev_broker.model_route import build_model_app

        app = build_model_app(object())  # type: ignore[arg-type]
        assert {r.path for r in app.routes if isinstance(r, APIRoute)} == {"/v1/chat/completions"}
        assert app.openapi_url is None and app.docs_url is None

    def test_the_control_app_has_only_the_relay_and_health(self) -> None:
        from personal_agent.dev_broker.relay import build_control_app

        app = build_control_app(
            secret="s",
            ledger=object(),  # type: ignore[arg-type]
            resolver=object(),  # type: ignore[arg-type]
            subnet=IPv4Network("172.19.0.0/16"),
            opencode_port=4096,
            clock=object(),  # type: ignore[arg-type]
        )
        assert {r.path for r in app.routes if isinstance(r, APIRoute)} == {
            "/healthz",
            "/relay/{job_id}/{path:path}",
        }
        assert app.openapi_url is None and app.docs_url is None

    def test_the_control_app_needs_a_secret(self) -> None:
        from personal_agent.dev_broker.relay import build_control_app

        with pytest.raises(ValueError, match="secret"):
            build_control_app(
                secret="",
                ledger=object(),  # type: ignore[arg-type]
                resolver=object(),  # type: ignore[arg-type]
                subnet=IPv4Network("172.19.0.0/16"),
                opencode_port=4096,
                clock=object(),  # type: ignore[arg-type]
            )
