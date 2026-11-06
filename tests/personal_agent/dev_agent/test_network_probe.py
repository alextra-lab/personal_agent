# ruff: noqa: D101, D102
"""The in-container probe of the dev-agent images (ADR-0159 AC-3; FRE-1682).

The probe file is stdlib-only and lives next to the Dockerfiles, so the test loads it by path.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest

_PROBE_PATH = Path(__file__).resolve().parents[3] / "docker" / "dev-agent" / "probe.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_agent_probe", _PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load()


@contextmanager
def _listener() -> Iterator[int]:
    """A TCP listener on localhost that accepts and closes."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join()
        server.close()


@contextmanager
def _fake_proxy(status_line: str) -> Iterator[int]:
    """A proxy that answers every request with `status_line` and records the request."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    stop = threading.Event()
    seen: list[bytes] = []

    def serve() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            with conn:
                seen.append(conn.recv(4096))
                conn.sendall(f"{status_line}\r\n\r\n".encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join()
        server.close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDirect:
    def test_a_listening_port_is_connected_at_the_tcp_stage(self) -> None:
        with _listener() as port:
            outcome = probe.probe_direct("127.0.0.1", port, 2.0)
        assert (outcome.attempted, outcome.stage, outcome.result) == (True, "tcp", "connected")

    def test_a_closed_port_fails_at_the_tcp_stage(self) -> None:
        outcome = probe.probe_direct("127.0.0.1", _closed_port(), 2.0)
        assert (outcome.stage, outcome.result) == ("tcp", "failed")

    def test_an_unresolvable_name_fails_at_the_dns_stage(self) -> None:
        outcome = probe.probe_direct("no-such-host.invalid", 5432, 2.0)
        assert (outcome.stage, outcome.result) == ("dns", "failed")


class TestViaProxy:
    def test_a_tunnel_answer_is_connected(self) -> None:
        with _fake_proxy("HTTP/1.1 200 Connection established") as port:
            outcome = probe.probe_via_proxy(
                f"http://127.0.0.1:{port}", "h.example", 443, 2.0, "tcp"
            )
        assert (outcome.stage, outcome.result) == ("proxy", "connected")

    def test_a_refusal_fails_at_the_proxy_stage(self) -> None:
        with _fake_proxy("HTTP/1.1 403 Forbidden") as port:
            outcome = probe.probe_via_proxy(f"http://127.0.0.1:{port}", "h.example", 80, 2.0, "tcp")
        assert (outcome.stage, outcome.result) == ("proxy", "failed")
        assert "403" in outcome.detail

    def test_a_dead_proxy_is_not_reported_as_a_refusal(self) -> None:
        outcome = probe.probe_via_proxy(
            f"http://127.0.0.1:{_closed_port()}", "h.example", 443, 2.0, "tcp"
        )
        assert (outcome.stage, outcome.result) == ("proxy-down", "failed")

    def test_the_connect_authority_brackets_ipv6(self) -> None:
        assert probe.connect_authority("2606:4700::1111", 443) == "[2606:4700::1111]:443"
        assert probe.connect_authority("registry.npmjs.org", 80) == "registry.npmjs.org:80"


class TestReport:
    def test_target_spec_parsing(self) -> None:
        target = probe.parse_target("postgres|172.30.0.2|5432|direct|deny")
        assert (target.label, target.host, target.port, target.via) == (
            "postgres",
            "172.30.0.2",
            5432,
            "direct",
        )
        assert (target.expect, target.kind) == ("deny", "tcp")
        https = probe.parse_target("npm|registry.npmjs.org|443|proxy|allow|https")
        assert https.kind == "https"

    @pytest.mark.parametrize(
        "spec", ["", "a|b", "a|b|x|direct|deny", "a|b|1|sideways|deny", "a|b|1|direct|maybe"]
    )
    def test_a_bad_target_spec_is_an_error(self, spec: str) -> None:
        with pytest.raises(ValueError):
            probe.parse_target(spec)

    def test_a_forbidden_target_that_answers_makes_the_report_not_ok(self) -> None:
        with _listener() as port:
            target = probe.parse_target(f"canary|127.0.0.1|{port}|direct|deny")
            report = probe.run_network([target], proxy_url=None, timeout=2.0)
        assert report["ok"] is False
        assert report["targets"][0]["result"] == "connected"

    def test_a_refused_forbidden_target_makes_the_report_ok(self) -> None:
        target = probe.parse_target(f"canary|127.0.0.1|{_closed_port()}|direct|deny")
        report = probe.run_network([target], proxy_url=None, timeout=2.0)
        assert report["ok"] is True
        assert report["targets"][0]["attempted"] is True

    def test_an_allowed_target_that_fails_makes_the_report_not_ok(self) -> None:
        target = probe.parse_target(f"control|127.0.0.1|{_closed_port()}|direct|allow")
        assert probe.run_network([target], proxy_url=None, timeout=2.0)["ok"] is False

    def test_the_report_lists_environment_names_and_never_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SESHAT_JOB_TOKEN", "do-not-print-me")
        report = probe.run_network([], proxy_url=None, timeout=1.0)
        assert "SESHAT_JOB_TOKEN" in report["env"]
        assert "do-not-print-me" not in str(report)

    def test_the_builtin_targets_cover_the_ticket_list(self) -> None:
        labels = {t.label for t in probe.default_targets(own_ip="172.30.0.5")}
        assert {
            "metadata-169.254.169.254",
            "github.com",
            "ip-literal",
            "ipv6-literal",
            "npm-port-80-direct",
            "npm-port-80-via-proxy",
            "npm-https-via-proxy",
            "host-gateway",
        } <= labels

    def test_the_only_builtin_allowed_target_is_npm_through_the_proxy(self) -> None:
        allowed = [t for t in probe.default_targets(own_ip="172.30.0.5") if t.expect == "allow"]
        assert [(t.label, t.via, t.kind) for t in allowed] == [
            ("npm-https-via-proxy", "proxy", "https")
        ]
