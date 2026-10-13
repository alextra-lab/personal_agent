#!/usr/bin/env python3
"""Probe the backend services for reachability and print one line for each (FRE-1578).

This replaces the shell recipe that stripped ``+asyncpg`` from ``AGENT_DATABASE_URL`` with
``sed`` inside ``$(...)``, and the ``run_python`` fallback that needed Docker.

Usage:
    probe_services.py {postgres,elasticsearch,neo4j,redis,all}

Exit codes: 0 every probed service answered, 1 at least one did not, 2 bad usage.
Every failure is one line that starts with the service name. No password is printed.

Environment (the same names as ``config/``; the script is standalone, because importing
``personal_agent.config`` starts the app configuration and logs to stdout):

    AGENT_DATABASE_URL         required for postgres, e.g. postgresql+asyncpg://user:pw@host/db
    AGENT_ELASTICSEARCH_URL    default http://elasticsearch:9200
    AGENT_NEO4J_URI            default bolt://neo4j:7687 (the HTTP check uses its host, port 7474)
    AGENT_EVENT_BUS_REDIS_URL  default redis://redis:6379/0
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from urllib.parse import unquote, urlsplit

SERVICES = ("postgres", "elasticsearch", "neo4j", "redis")
_TIMEOUT_S = 5
_USAGE = "usage: probe_services.py {postgres,elasticsearch,neo4j,redis,all}"


def _show(url: str) -> str:
    """Return ``url`` without user-info, so that a password in it is never printed."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "the configured URL"
    if not host:
        return "the configured URL"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "") + parts.path


def _clean(text: str, secret: str = "") -> str:
    """Return the first line of ``text``, cut to 200 characters, without ``secret``."""
    line = (text.strip().splitlines() or [""])[0]
    if secret:
        line = line.replace(secret, "***")
    return line[:200]


def probe_postgres(env: Mapping[str, str]) -> tuple[bool, str]:
    """Open a real connection with ``psql`` and run ``SELECT 1``.

    Args:
        env: The environment to read ``AGENT_DATABASE_URL`` from.

    Returns:
        ``(ok, detail)``.
    """
    url = env.get("AGENT_DATABASE_URL", "")
    if not url:
        return False, "AGENT_DATABASE_URL is not set in this environment"
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    password = unquote(parts.password or "")
    child_env = {**os.environ, "PGPASSWORD": password, "PGCONNECT_TIMEOUT": str(_TIMEOUT_S)}
    command = [
        "psql",
        "-w",
        "-h",
        parts.hostname or "postgres",
        "-p",
        str(parts.port or 5432),
        "-U",
        unquote(parts.username or ""),
        "-d",
        parts.path.lstrip("/") or "postgres",
        "-tAc",
        "SELECT 1",
    ]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, text=True, timeout=_TIMEOUT_S * 2, env=child_env
        )
    except FileNotFoundError:
        return False, "psql is not installed in this environment"
    except subprocess.TimeoutExpired:
        return False, f"no answer within {_TIMEOUT_S * 2} seconds"
    if done.returncode == 0 and done.stdout.strip() == "1":
        return True, "connected, SELECT 1 returned 1"
    return False, _clean(done.stderr or done.stdout or "psql failed", password)


def _http_get(url: str) -> tuple[int, bytes]:
    """GET ``url`` and return the status and the first 64 KiB of the body.

    Raises:
        OSError: The host cannot be reached (``urllib.error.URLError`` is one).
    """
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as reply:  # noqa: S310
            return reply.status, reply.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def probe_elasticsearch(env: Mapping[str, str]) -> tuple[bool, str]:
    """Read ``/_cluster/health``. Green and yellow pass, red fails.

    Args:
        env: The environment to read ``AGENT_ELASTICSEARCH_URL`` from.

    Returns:
        ``(ok, detail)``.
    """
    base = env.get("AGENT_ELASTICSEARCH_URL", "http://elasticsearch:9200").rstrip("/")
    shown = _show(base)
    try:
        status, body = _http_get(f"{base}/_cluster/health")
    except OSError as exc:
        return False, f"cannot connect to {shown}: {_clean(str(exc), base)}"
    if status != 200:
        return False, f"{shown}/_cluster/health answered HTTP {status}"
    try:
        cluster = json.loads(body)["status"]
    except (ValueError, KeyError, TypeError):
        return False, "the reply is not a cluster health document"
    return cluster in ("green", "yellow"), f"cluster status {cluster}"


def probe_neo4j(env: Mapping[str, str]) -> tuple[bool, str]:
    """Read the HTTP discovery page on port 7474 of the Bolt host.

    Args:
        env: The environment to read ``AGENT_NEO4J_URI`` from.

    Returns:
        ``(ok, detail)``.
    """
    host = urlsplit(env.get("AGENT_NEO4J_URI", "bolt://neo4j:7687")).hostname or "neo4j"
    url = f"http://{host}:7474/"
    try:
        status, _ = _http_get(url)
    except OSError as exc:
        return False, f"cannot connect to {url}: {_clean(str(exc))}"
    return status == 200, f"{url} answered HTTP {status}"


def probe_redis(env: Mapping[str, str]) -> tuple[bool, str]:
    """Send ``PING`` and read the reply. A reply that asks for a password still counts as up.

    Args:
        env: The environment to read ``AGENT_EVENT_BUS_REDIS_URL`` from.

    Returns:
        ``(ok, detail)``.
    """
    parts = urlsplit(env.get("AGENT_EVENT_BUS_REDIS_URL", "redis://redis:6379/0"))
    host, port = parts.hostname or "redis", parts.port or 6379
    try:
        with socket.create_connection((host, port), timeout=_TIMEOUT_S) as sock:
            sock.sendall(b"PING\r\n")
            reply = sock.recv(128).decode("ascii", errors="replace").strip()
    except OSError as exc:
        return False, f"cannot connect to {host}:{port}: {_clean(str(exc))}"
    if reply.startswith("+PONG"):
        return True, f"{host}:{port} answered PONG"
    if reply.startswith("-NOAUTH"):
        return True, f"{host}:{port} is up and asks for a password"
    return False, f"{host}:{port} sent an unexpected reply: {_clean(reply)}"


_PROBES: Mapping[str, Callable[[Mapping[str, str]], tuple[bool, str]]] = {
    "postgres": probe_postgres,
    "elasticsearch": probe_elasticsearch,
    "neo4j": probe_neo4j,
    "redis": probe_redis,
}


def main(argv: list[str], env: Mapping[str, str]) -> int:
    """Run the probes that ``argv`` names and print one line for each.

    Args:
        argv: The arguments after the program name.
        env: The environment.

    Returns:
        The exit code.
    """
    if argv in (["-h"], ["--help"]):
        print(_USAGE)
        print("Prints '<service>: ok <detail>' or '<service>: FAIL <detail>' for each service.")
        return 0
    if len(argv) != 1 or argv[0] not in (*SERVICES, "all"):
        print(f"error: {_USAGE}", file=sys.stderr)
        return 2
    names = SERVICES if argv[0] == "all" else (argv[0],)
    failed = 0
    for name in names:
        try:
            ok, detail = _PROBES[name](env)
        except (ValueError, http.client.HTTPException) as exc:
            # A bad port in a URL, or a reply that is not HTTP. One line, and the run goes on.
            ok, detail = False, f"unexpected {type(exc).__name__}: check the environment values"
        print(f"{name}: {'ok' if ok else 'FAIL'} {detail}")
        failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ))
