# ruff: noqa: D103
"""FRE-1239 — hermetic behavioral smoke test for the OTLP ingress Caddy site block.

A static text-parse test (test_caddyfile_otlp_ingress.py) proves the block's shape; it can't
prove the block actually *behaves* that way — a codex plan-review round on this ticket flagged
that gap. This test proves the routing mechanics for real, without touching the live cloud-sim
stack: it extracts the committed OTLP block verbatim from config/cloud-sim/Caddyfile, runs it in
a disposable Caddy container on a throwaway Docker network against a stub upstream standing in
for otel-collector, and asserts the allow/deny behavior end to end.

The stub upstream drains the full request body before responding, matching real otel-collector
semantics (it must read the whole body to decode the protobuf before it can answer at all) — an
earlier draft of this test used a Caddy `respond` stub that answered without reading the body,
which silently no-ops the `request_body max_size` check: Caddy's limit only fires on a Read()
that crosses it, and a stub that never reads never crosses it. Confirmed empirically (`caddy
respond` stub: 21MiB body → 200; body-draining stub: same request → 413) before settling on this
design — worth recording so a future edit doesn't quietly regress back to the non-draining stub.

Two things it deliberately does NOT prove, both left to master's live-verification runbook:
  - AC-1's real claim (a span lands in Tempo) — there is no real Collector/Tempo here, only a
    stub.
  - The `remote_ip` matcher's DENY path (a request outside the compose network is refused) — a
    single-host Docker network can't honestly model "public internet source" vs. "compose
    network source" the way the real cloud-sim deployment can (a published-port request from the
    test process always arrives NATed through the network's own gateway IP, which is always
    in-subnet — confirmed empirically before writing this test). The ALLOW path is still proven:
    the test's own network subnet is discovered at runtime and substituted for the committed
    172.25.0.0/16 literal, so every allowed-case request genuinely passes the remote_ip check
    rather than trivially matching an unbounded range.

Requires PERSONAL_AGENT_INTEGRATION=1 and a live Docker daemon; skipped otherwise.

    PERSONAL_AGENT_INTEGRATION=1 uv run python -m pytest -m integration \
        tests/scripts/test_fre1239_otlp_ingress_smoke.py -v
"""

from __future__ import annotations

import os
import re
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

_BLOCK_START = re.compile(r"^http://\{\$OTLP_HOST:[^}]*\}:80 \{", re.MULTILINE)

_integration_available = (
    os.environ.get("PERSONAL_AGENT_INTEGRATION") == "1"
    and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
)
pytestmark_integration = pytest.mark.skipif(
    not _integration_available,
    reason="Requires PERSONAL_AGENT_INTEGRATION=1 and a live Docker daemon",
)

_OTLP_HOST = "otlp.example.com"
_READY_TIMEOUT_S = 15

# Drains the full request body before responding — matching real otel-collector semantics
# (it must read the whole body to decode the protobuf before it can answer). A stub that
# answers without reading never exercises `request_body max_size` (see module docstring).
_STUB_SERVER_SCRIPT = """\
import http.server
import socketserver


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


with socketserver.TCPServer(("0.0.0.0", 4318), Handler) as httpd:
    httpd.serve_forever()
"""


def _extract_otlp_block() -> str:
    text = CADDYFILE_PATH.read_text()
    match = _BLOCK_START.search(text)
    assert match is not None, "no OTLP_HOST site block found in the Caddyfile"
    start = match.end() - 1
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[match.start() : i + 1]
    raise AssertionError("unbalanced braces in the OTLP_HOST site block")


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class _Stack:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


@pytest.fixture(scope="module")
def otlp_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    if not _integration_available:
        pytest.skip("Requires PERSONAL_AGENT_INTEGRATION=1 and a live Docker daemon")

    run_id = uuid.uuid4().hex[:10]
    network = f"fre1239-{run_id}"
    stub_name = f"fre1239-stub-{run_id}"
    caddy_name = f"fre1239-caddy-{run_id}"
    tmp_dir = tmp_path_factory.mktemp("fre1239")

    _run("docker", "network", "create", network)
    try:
        subnet = _run(
            "docker",
            "network",
            "inspect",
            network,
            "--format",
            "{{(index .IPAM.Config 0).Subnet}}",
        )

        stub_script = tmp_dir / "stub_server.py"
        stub_script.write_text(_STUB_SERVER_SCRIPT)
        _run(
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            stub_name,
            "--network",
            network,
            "--network-alias",
            "otel-collector",
            "-v",
            f"{stub_script}:/app.py:ro",
            "python:3.12-alpine",
            "python",
            "/app.py",
        )

        block = _extract_otlp_block()
        assert "remote_ip 172.25.0.0/16" in block, (
            "committed block's remote_ip literal changed — update this substitution"
        )
        test_block = block.replace("remote_ip 172.25.0.0/16", f"remote_ip {subnet}")
        under_test_caddyfile = tmp_dir / "under-test-Caddyfile"
        under_test_caddyfile.write_text(test_block + "\n")

        _run(
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            caddy_name,
            "--network",
            network,
            "-p",
            "127.0.0.1::80",
            "-e",
            f"OTLP_HOST={_OTLP_HOST}",
            "-v",
            f"{under_test_caddyfile}:/etc/caddy/Caddyfile:ro",
            "caddy:2-alpine",
        )

        port_mapping = _run("docker", "port", caddy_name, "80")
        host_port = port_mapping.rsplit(":", 1)[-1]
        base_url = f"http://127.0.0.1:{host_port}"

        deadline = time.monotonic() + _READY_TIMEOUT_S
        last_error: Exception | str | None = None
        while time.monotonic() < deadline:
            try:
                r = requests.post(
                    f"{base_url}/v1/traces",
                    headers={"Host": _OTLP_HOST},
                    json={},
                    timeout=2,
                )
                if r.status_code == 200:
                    break
                last_error = f"stub not ready yet, got {r.status_code}"
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(0.5)
        else:
            raise AssertionError(f"caddy-under-test never became reachable: {last_error}")

        yield _Stack(base_url=base_url)
    finally:
        subprocess.run(["docker", "rm", "-f", caddy_name], capture_output=True, check=False)
        subprocess.run(["docker", "rm", "-f", stub_name], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", network], capture_output=True, check=False)


@pytestmark_integration
class TestAllowedPathReachesTheCollector:
    def test_post_v1_traces_no_query_returns_200(self, otlp_stack: _Stack) -> None:
        r = requests.post(
            f"{otlp_stack.base_url}/v1/traces",
            headers={"Host": _OTLP_HOST},
            json={"resourceSpans": []},
            timeout=5,
        )
        assert r.status_code == 200
        assert r.text == "ok"


@pytestmark_integration
class TestEveryOtherShapeIsRefused:
    def test_get_v1_traces_returns_403(self, otlp_stack: _Stack) -> None:
        r = requests.get(
            f"{otlp_stack.base_url}/v1/traces", headers={"Host": _OTLP_HOST}, timeout=5
        )
        assert r.status_code == 403

    def test_post_v1_traces_with_query_string_returns_403(self, otlp_stack: _Stack) -> None:
        r = requests.post(
            f"{otlp_stack.base_url}/v1/traces?x=y",
            headers={"Host": _OTLP_HOST},
            json={},
            timeout=5,
        )
        assert r.status_code == 403

    def test_post_root_returns_403(self, otlp_stack: _Stack) -> None:
        r = requests.post(f"{otlp_stack.base_url}/", headers={"Host": _OTLP_HOST}, timeout=5)
        assert r.status_code == 403

    def test_post_v1_logs_returns_403(self, otlp_stack: _Stack) -> None:
        r = requests.post(f"{otlp_stack.base_url}/v1/logs", headers={"Host": _OTLP_HOST}, timeout=5)
        assert r.status_code == 403

    def test_post_search_returns_403(self, otlp_stack: _Stack) -> None:
        r = requests.post(f"{otlp_stack.base_url}/_search", headers={"Host": _OTLP_HOST}, timeout=5)
        assert r.status_code == 403


@pytestmark_integration
class TestOversizedBodyIsRejected:
    def test_body_over_20mib_is_rejected(self, otlp_stack: _Stack) -> None:
        oversized = b"x" * (21 * 1024 * 1024)
        r = requests.post(
            f"{otlp_stack.base_url}/v1/traces",
            headers={"Host": _OTLP_HOST, "Content-Type": "application/octet-stream"},
            data=oversized,
            timeout=10,
        )
        assert r.status_code == 413
