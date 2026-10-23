# ruff: noqa: D103
"""FRE-1240 — hermetic behavioral smoke test for the tunnel-only guard on every inbound block.

The static test (test_caddyfile_tunnel_blocks_remote_ip.py) proves each block carries the
matcher. It cannot prove the matcher answers first: Caddy orders `handle` before `respond`, and
a guard in the wrong place would pass a text check and still let a request through. This test
runs the full committed Caddyfile in two disposable Caddy containers on a throwaway network and
sends requests to each tunnel host name.

  - "deny" container: the guard subnet is replaced by one that excludes the client. Every route
    on every tunnel host must answer 403, including the ES `/_bulk` and `/slm-requests-*` paths
    that were reachable without a login before this change.
  - "allow" container: the guard subnet is replaced by the throwaway network's own subnet. The
    same requests must NOT answer 403. They reach `reverse_proxy` and fail there, because no
    upstream exists. The status differs by block (the AGENT and API `handle_errors` turn a 502
    into a 500), so the assertion is "not 403".

A published-port request from the test process arrives NATed through the network's own gateway
IP, which is always in-subnet (confirmed in FRE-1239). That is why the deny case swaps the
subnet instead of trying to send a request from outside it. On the live VPS, a request to the
public IP reaches Caddy with the public IP as its peer address: outside 172.25.0.0/16.

Requires PERSONAL_AGENT_INTEGRATION=1 and a live Docker daemon; skipped otherwise.

    PERSONAL_AGENT_INTEGRATION=1 uv run python -m pytest -m integration \
        tests/scripts/test_fre1240_tunnel_only_smoke.py -v
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE_PATH = REPO_ROOT / "config" / "cloud-sim" / "Caddyfile"

_integration_available = (
    os.environ.get("PERSONAL_AGENT_INTEGRATION") == "1"
    and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
)
pytestmark_integration = pytest.mark.skipif(
    not _integration_available,
    reason="Requires PERSONAL_AGENT_INTEGRATION=1 and a live Docker daemon",
)

_COMMITTED_SUBNET = "172.25.0.0/16"
_EXCLUDING_SUBNET = "203.0.113.0/24"  # TEST-NET-3: never contains the client or the gateway
_READY_TIMEOUT_S = 20

# (label, Host header, method, path, extra headers). Hosts are the Caddyfile's example defaults.
_ROUTES: list[tuple[str, str, str, str, dict[str, str]]] = [
    ("agent /", "agent.example.com", "GET", "/", {}),
    ("agent /api", "agent.example.com", "GET", "/api/x", {}),
    ("agent /ws", "agent.example.com", "GET", "/ws/x", {}),
    ("graph /", "graph.example.com", "GET", "/", {}),
    (
        "graph websocket",
        "graph.example.com",
        "GET",
        "/",
        {"Connection": "Upgrade", "Upgrade": "websocket"},
    ),
    ("es /_bulk", "es.example.com", "GET", "/_bulk", {}),
    ("es /slm-requests-x", "es.example.com", "GET", "/slm-requests-x/_search", {}),
    ("api /", "api.example.com", "GET", "/", {}),
    ("api /v1", "api.example.com", "POST", "/v1/x", {}),
]
_ROUTE_IDS = [route[0] for route in _ROUTES]


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout.strip()


class _Stack:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


def _start_caddy(network: str, name: str, caddyfile: Path) -> str:
    _run(
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "-p",
        "127.0.0.1::80",
        "-v",
        f"{caddyfile}:/etc/caddy/Caddyfile:ro",
        "caddy:2-alpine",
    )
    return f"http://127.0.0.1:{_run('docker', 'port', name, '80').rsplit(':', 1)[-1]}"


def _wait_until_serving(base_url: str) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            requests.get(base_url, headers={"Host": "api.example.com"}, timeout=2)
            return
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(0.5)
    raise AssertionError(f"caddy-under-test never became reachable: {last_error}")


@pytest.fixture(scope="module")
def stacks(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, _Stack]]:
    if not _integration_available:
        pytest.skip("Requires PERSONAL_AGENT_INTEGRATION=1 and a live Docker daemon")

    run_id = uuid.uuid4().hex[:10]
    network = f"fre1240-{run_id}"
    names = {"deny": f"fre1240-deny-{run_id}", "allow": f"fre1240-allow-{run_id}"}
    tmp_dir = tmp_path_factory.mktemp("fre1240")

    _run("docker", "network", "create", network)
    try:
        own_subnet = _run(
            "docker", "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Subnet}}"
        )
        text = CADDYFILE_PATH.read_text()
        assert _COMMITTED_SUBNET in text, "committed subnet literal changed — update this swap"

        result: dict[str, _Stack] = {}
        for case, subnet in (("deny", _EXCLUDING_SUBNET), ("allow", own_subnet)):
            caddyfile = tmp_dir / f"Caddyfile.{case}"
            caddyfile.write_text(text.replace(_COMMITTED_SUBNET, subnet))
            result[case] = _Stack(_start_caddy(network, names[case], caddyfile))
        for stack in result.values():
            _wait_until_serving(stack.base_url)
        yield result
    finally:
        for name in names.values():
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)


def _send(
    stack: _Stack, host: str, method: str, path: str, headers: dict[str, str]
) -> requests.Response:
    return requests.request(
        method,
        f"{stack.base_url}{path}",
        headers={"Host": host, **headers},
        timeout=10,
        allow_redirects=False,
    )


@pytestmark_integration
@pytest.mark.parametrize(("label", "host", "method", "path", "headers"), _ROUTES, ids=_ROUTE_IDS)
def test_a_peer_outside_the_subnet_gets_403_on_every_tunnel_route(
    stacks: dict[str, _Stack],
    label: str,
    host: str,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    response = _send(stacks["deny"], host, method, path, headers)
    assert response.status_code == 403, f"{label}: {response.status_code}"
    assert response.text == "Forbidden"


@pytestmark_integration
@pytest.mark.parametrize(("label", "host", "method", "path", "headers"), _ROUTES, ids=_ROUTE_IDS)
def test_a_peer_inside_the_subnet_is_not_refused_by_the_guard(
    stacks: dict[str, _Stack],
    label: str,
    host: str,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    response = _send(stacks["allow"], host, method, path, headers)
    assert response.status_code != 403, f"{label}: the guard refused an in-subnet peer"
