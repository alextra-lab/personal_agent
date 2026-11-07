"""The broker's two listen addresses (ADR-0159 D4.3; FRE-1683).

This module imports only the standard library, ``psutil`` and the exception module, so the health
check (:mod:`personal_agent.dev_broker.health`) can use it without the import cost of the broker.
"""

from __future__ import annotations

import socket
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import psutil  # type: ignore[import-untyped]

from personal_agent.exceptions import BrokerStartupError


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
