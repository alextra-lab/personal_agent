"""The broker's health check: ``python -m personal_agent.dev_broker --health`` (FRE-1684).

The compose check has a 5 s timeout. An import of the broker costs about 11 s of CPU, mostly
``litellm`` through the cost gate, and the broker has 0.5 CPU. So this path reads only the two
listener values from ``broker.yaml`` and calls ``/healthz``. It must not import the cost gate, the
settings or the LLM client.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Sequence
from ipaddress import IPv4Network
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from personal_agent.dev_broker.addresses import interface_addresses, select_listen_addresses
from personal_agent.exceptions import BrokerStartupError

#: The same file that :mod:`personal_agent.dev_broker.config` loads.
BROKER_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "dev_agent" / "broker.yaml"


class _HealthTarget(BaseModel):
    """The two values of ``broker.yaml`` that the check needs. Other keys are ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    control_port: int = Field(gt=0, lt=65536)
    control_subnet: IPv4Network


def load_health_target(path: Path = BROKER_CONFIG_PATH) -> tuple[int, IPv4Network]:
    """Return the control listener's port and subnet.

    Args:
        path: The broker config file.

    Returns:
        ``(control_port, control_subnet)``.

    Raises:
        pydantic.ValidationError: If either value is missing or invalid.
        OSError: If the file cannot be read.
    """
    with path.open(encoding="utf-8") as handle:
        target = _HealthTarget.model_validate(yaml.safe_load(handle))
    return target.control_port, target.control_subnet


def health() -> bool:
    """Return whether the control listener answers its health route.

    Returns:
        ``True`` when ``/healthz`` answers ``ok``.
    """
    port, subnet = load_health_target()
    addresses = select_listen_addresses(interface_addresses(), control_subnet=subnet)
    url = f"http://{addresses.control}:{port}/healthz"
    with urllib.request.urlopen(url, timeout=3) as answer:  # noqa: S310 — fixed http URL
        return bool(json.load(answer).get("status") == "ok")


def main(argv: Sequence[str]) -> int:
    """Run the health check.

    Args:
        argv: Command-line arguments without the program name. Unused.

    Returns:
        ``0`` when healthy, else ``1``.
    """
    del argv
    try:
        return 0 if health() else 1
    except OSError, BrokerStartupError, ValueError:
        return 1
