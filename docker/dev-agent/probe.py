#!/usr/bin/env python3
"""Probe for the dev-agent images (ADR-0159 AC-3; FRE-1682). Standard library only.

It runs inside a container and prints one JSON report on stdout.

    probe.py network [--target LABEL|HOST|PORT|VIA|EXPECT[|KIND]]...   what the container can reach
    probe.py csp                                                       what the CSP test server sends
    probe.py browser                                                   Chromium loads a page under that CSP

``network`` exits 0 when every target behaved as expected and 1 otherwise. The verdict is only a
first opinion. The caller must judge the report again, because a probe that cannot fail proves
nothing (see ``personal_agent.dev_agent.verification``).

VIA is ``direct`` or ``proxy`` (an HTTP CONNECT through ``HTTPS_PROXY``). EXPECT is ``deny`` or
``allow``. KIND is ``tcp`` (default) or ``https`` (a GET of ``/-/ping`` over TLS, status 200).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 4.0
_CSP_SERVE = Path("/opt/csp/csp-serve.ts")
_CSP_MANIFEST = Path("/opt/csp/csp-manifest.json")
_BROWSER_SMOKE = Path("/opt/probe/browser-smoke.mjs")


@dataclass(frozen=True)
class Target:
    """One connection that the probe attempts."""

    label: str
    host: str
    port: int
    via: str
    expect: str
    kind: str = "tcp"


@dataclass(frozen=True)
class Outcome:
    """What happened when the probe tried a target.

    ``stage`` says where the attempt ended: ``dns``, ``tcp``, ``proxy`` (the proxy answered),
    or ``proxy-down`` (the proxy itself did not answer).
    """

    attempted: bool
    stage: str
    result: str
    detail: str


def parse_target(spec: str) -> Target:
    """Parse ``LABEL|HOST|PORT|VIA|EXPECT[|KIND]``.

    Raises:
        ValueError: If the spec is malformed.
    """
    parts = spec.split("|")
    if len(parts) not in (5, 6) or not parts[0] or not parts[1]:
        raise ValueError(f"bad target spec: {spec!r}")
    label, host, port_text, via, expect = parts[:5]
    kind = parts[5] if len(parts) == 6 else "tcp"
    if not port_text.isdigit():
        raise ValueError(f"bad port in target spec: {spec!r}")
    if (
        via not in ("direct", "proxy")
        or expect not in ("deny", "allow")
        or kind not in ("tcp", "https")
    ):
        raise ValueError(f"bad value in target spec: {spec!r}")
    return Target(label, host, int(port_text), via, expect, kind)


def connect_authority(host: str, port: int) -> str:
    """Return ``host:port``, with brackets around an IPv6 address."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def default_targets(own_ip: str) -> list[Target]:
    """Return the targets that need no address from the caller.

    Args:
        own_ip: The container's own address on its network. The probe tries the ``.1`` address
            of that /24 as the network gateway, which is the host side of a Docker bridge.
    """
    gateway = ".".join([*own_ip.split(".")[:3], "1"])
    return [
        Target("metadata-169.254.169.254", "169.254.169.254", 80, "direct", "deny"),
        Target("github.com", "github.com", 443, "direct", "deny"),
        Target("github.com-via-proxy", "github.com", 443, "proxy", "deny"),
        Target("ip-literal", "1.1.1.1", 443, "direct", "deny"),
        Target("ip-literal-via-proxy", "1.1.1.1", 443, "proxy", "deny"),
        Target("ipv6-literal", "2606:4700:4700::1111", 443, "direct", "deny"),
        Target("ipv6-literal-via-proxy", "2606:4700:4700::1111", 443, "proxy", "deny"),
        Target("npm-port-80-direct", "registry.npmjs.org", 80, "direct", "deny"),
        Target("npm-port-80-via-proxy", "registry.npmjs.org", 80, "proxy", "deny"),
        Target("host-gateway", gateway, 22, "direct", "deny"),
        Target("postgres-name", "postgres", 5432, "direct", "deny"),
        Target("caddy-name", "caddy", 8601, "direct", "deny"),
        Target("npm-https-via-proxy", "registry.npmjs.org", 443, "proxy", "allow", "https"),
    ]


def probe_direct(host: str, port: int, timeout: float) -> Outcome:
    """Try a direct TCP connection."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return Outcome(True, "dns", "failed", f"{type(exc).__name__}: {exc}")
    last = "no address"
    for family, socktype, proto, _canon, address in infos:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout)
                sock.connect(address)
            return Outcome(True, "tcp", "connected", str(address[0]))
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
    return Outcome(True, "tcp", "failed", last)


def _read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data += chunk
    return data


def _status_code(head: bytes) -> int:
    parts = head.split(b" ", 2)
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def probe_via_proxy(proxy_url: str, host: str, port: int, timeout: float, kind: str) -> Outcome:
    """Try a target through an HTTP CONNECT proxy."""
    proxy = urlsplit(proxy_url)
    try:
        sock = socket.create_connection((proxy.hostname or "", proxy.port or 3128), timeout)
    except OSError as exc:
        return Outcome(True, "proxy-down", "failed", f"{type(exc).__name__}: {exc}")
    authority = connect_authority(host, port)
    try:
        with sock:
            sock.settimeout(timeout)
            sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
            code = _status_code(_read_head(sock))
            if not 200 <= code < 300:
                return Outcome(True, "proxy", "failed", f"proxy answered {code}")
            if kind == "tcp":
                return Outcome(True, "proxy", "connected", f"tunnel {code}")
            context = ssl.create_default_context()
            with context.wrap_socket(sock, server_hostname=host) as tls:
                tls.sendall(
                    f"GET /-/ping HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
                )
                status = _status_code(_read_head(tls))
            if status == 200:
                return Outcome(True, "proxy", "connected", "https 200")
            return Outcome(True, "proxy", "failed", f"https answered {status}")
    except (OSError, ssl.SSLError) as exc:
        return Outcome(True, "proxy", "failed", f"{type(exc).__name__}: {exc}")


def run_network(targets: list[Target], proxy_url: str | None, timeout: float) -> dict[str, object]:
    """Attempt every target and build the report."""
    entries: list[dict[str, object]] = []
    ok = True
    for target in targets:
        if target.via == "direct":
            outcome = probe_direct(target.host, target.port, timeout)
        elif proxy_url is None:
            outcome = Outcome(True, "proxy-down", "failed", "HTTPS_PROXY is not set")
        else:
            outcome = probe_via_proxy(proxy_url, target.host, target.port, timeout, target.kind)
        wanted = "connected" if target.expect == "allow" else "failed"
        ok = ok and outcome.result == wanted
        entries.append({**asdict(target), **asdict(outcome)})
    # Names only. A value can be a credential.
    return {
        "kind": "network",
        "ok": ok,
        "proxy": proxy_url,
        "targets": entries,
        "env": sorted(os.environ),
    }


def _own_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "0.0.0.0"  # noqa: S104 - not a bind, a placeholder for "unknown"


_GOOD_PAGE = """<!doctype html><title>good</title><button id="b">go</button><p id="out">idle</p>
<script>document.getElementById('b').addEventListener('click',()=>{
document.getElementById('out').textContent='clicked';});</script>"""
# connect-src is 'none', so this same-origin fetch is blocked. Without the CSP it succeeds, so only
# the policy can cause the console error.
_BAD_PAGE = """<!doctype html><title>bad</title><button id="b">go</button><p id="out">idle</p>
<script>document.getElementById('b').addEventListener('click',()=>{
fetch(location.origin+'/good.html').catch(()=>{});document.getElementById('out').textContent='clicked';});</script>"""


def _serve(root: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        ["node", str(_CSP_SERVE), str(root), str(_CSP_MANIFEST)],
        stdout=subprocess.PIPE,
        text=True,
    )


def run_csp() -> dict[str, object]:
    """Start the CSP test server and report the header that it sends for an HTML file."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "page.html").write_text(_GOOD_PAGE, encoding="utf-8")
        server = _serve(root)
        try:
            assert server.stdout is not None
            origin = server.stdout.readline().strip()
            with urllib.request.urlopen(f"{origin}/page.html", timeout=10) as response:  # noqa: S310
                header = response.headers.get("Content-Security-Policy") or ""
        finally:
            server.terminate()
            server.wait(timeout=10)
    return {"kind": "csp", "origin": origin, "header": header}


def run_browser() -> dict[str, object]:
    """Load a good page and a CSP-violating page in Chromium, and report the console errors."""
    pages: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "good.html").write_text(_GOOD_PAGE, encoding="utf-8")
        (root / "bad.html").write_text(_BAD_PAGE, encoding="utf-8")
        server = _serve(root)
        try:
            assert server.stdout is not None
            origin = server.stdout.readline().strip()
            for name in ("good.html", "bad.html"):
                done = subprocess.run(  # noqa: S603
                    ["node", str(_BROWSER_SMOKE), f"{origin}/{name}"],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                try:
                    pages.append(json.loads(done.stdout))
                except json.JSONDecodeError:
                    pages.append({"page": name, "error": done.stderr[-500:] or "no output"})
        finally:
            server.terminate()
            server.wait(timeout=10)
    return {"kind": "browser", "pages": pages}


def main(argv: list[str]) -> int:
    """Run one subcommand and print its report."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    network = sub.add_parser("network")
    network.add_argument("--target", action="append", default=[])
    network.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    sub.add_parser("csp")
    sub.add_parser("browser")
    args = parser.parse_args(argv)
    if args.command == "network":
        targets = default_targets(_own_ip()) + [parse_target(s) for s in args.target]
        report = run_network(targets, os.environ.get("HTTPS_PROXY"), args.timeout)
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    report = run_csp() if args.command == "csp" else run_browser()
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
