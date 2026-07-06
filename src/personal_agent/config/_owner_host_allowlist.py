"""Owner-controlled storage host allowlist helper (ADR-0112 AC-1).

A pure function that decides whether a resolved storage target (Postgres,
Neo4j, Elasticsearch connection URI) is owner-controlled: loopback, or a host
declared in config as an exact hostname or a CIDR range. Placement mirrors
``_substrate_fingerprint.py`` — this module lives in ``personal_agent.config``
to avoid the same ``memory.service`` → ``config.settings`` import-cycle risk.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from urllib.parse import urlparse

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def is_owner_controlled_host(uri: str, allowlist: Sequence[str]) -> bool:
    """Return True when *uri*'s host is loopback or on the owner allowlist.

    Each *allowlist* entry is either an exact hostname (matched
    case-insensitively) or a CIDR range (e.g. ``"10.0.0.0/8"``), checked only
    against hosts that parse as IP addresses. A private IP is never
    auto-allowed — a CIDR range covering it must be explicitly declared, so
    an empty or non-matching allowlist fails closed.

    Args:
        uri: A storage connection URI/URL, e.g.
            ``postgresql+asyncpg://user:pw@postgres:5432/db``.
        allowlist: Declared owner-controlled hosts — exact hostnames or CIDR
            ranges.

    Returns:
        True when the host is loopback, matches an allowlist hostname, or
        falls within an allowlisted CIDR range. False for malformed/hostless
        URIs rather than raising.
    """
    host = urlparse(uri).hostname or ""
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True

    try:
        address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        address = None

    for entry in allowlist:
        if "/" in entry:
            if address is None:
                continue
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if address in network:
                return True
        elif host.lower() == entry.lower():
            return True

    return False
