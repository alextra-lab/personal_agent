# ruff: noqa: D102
"""The broker's health check stays inside its 5 s compose timeout (FRE-1684 fold-in).

The deployed check took 30 s: ``--health`` imported the whole broker, and the import of the cost
gate pulls in ``litellm``, which costs about 11 s of CPU at the broker's 0.5 CPU quota. The health
path must import only what it needs to call ``/healthz``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from personal_agent.dev_broker.config import load_broker_config
from personal_agent.dev_broker.health import load_health_target

_REPO = Path(__file__).resolve().parents[3]

#: Modules that the health path must never load.
_FORBIDDEN_PREFIXES = (
    "litellm",
    "personal_agent.cost_gate",
    "personal_agent.config",
    "personal_agent.llm_client",
    "personal_agent.telemetry",
    "personal_agent.dev_broker.server",
)


def _imported_modules(stderr: str) -> set[str]:
    """Return the module names that ``python -X importtime`` reported."""
    names: set[str] = set()
    for line in stderr.splitlines():
        if line.startswith("import time:") and line.count("|") == 2:
            name = line.rsplit("|", 1)[1].strip()
            if name != "package":
                names.add(name)
    return names


class TestHealthImports:
    """The import set of the health path."""

    def test_the_health_path_loads_no_heavy_module(self) -> None:
        result = subprocess.run(  # noqa: S603 — fixed argument list, no shell
            [sys.executable, "-X", "importtime", "-m", "personal_agent.dev_broker", "--health"],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        # The test host has no broker, so the check fails. It must fail fast and cleanly.
        assert result.returncode in (0, 1), result.stderr[-2000:]
        modules = _imported_modules(result.stderr)
        assert "personal_agent.dev_broker.health" in modules
        loaded = sorted(m for m in modules if m.startswith(_FORBIDDEN_PREFIXES))
        assert loaded == []


class TestHealthTarget:
    """The values that the health path reads."""

    def test_it_reads_the_same_listener_as_the_broker(self) -> None:
        config = load_broker_config()
        port, subnet = load_health_target()
        assert port == config.control_port
        assert subnet == config.control_subnet
