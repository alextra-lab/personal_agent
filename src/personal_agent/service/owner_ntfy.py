"""One-line push from the gateway to the owner's phone through ntfy (FRE-1758).

The dispatch daemons already push to the owner (``scripts/dispatch/owner_notify.py``, FRE-1550).
The gateway image holds only ``src/`` and ``config/``, so this module carries its own small sender.
The transport rules are the same.

**Setting.** ``settings.owner_ntfy_url`` (``AGENT_OWNER_NTFY_URL``) holds the full URL, topic
included. On ntfy.sh the topic name is the secret, so no log line holds the URL. A failure logs the
exception type only, because the exception text can hold the URL. The sender uses ``urllib`` and not
``httpx``, because ``httpx`` logs the request URL at INFO.

**Transport.** One plain-text ``POST`` over IPv4 (FRE-1632). ntfy.sh limits anonymous publishers per
IPv6 /64, and the VPS shares its /64 with other tenants. The send runs in a worker thread and a hard
timeout bounds the wait, so a slow send cannot hold the gateway start for longer than ``timeout_s``.

**Never raises.** A caller can await :func:`push_to_owner` inside the start-up path.
"""

from __future__ import annotations

import asyncio
import http.client
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import ContextManager
from urllib.parse import urlparse

import structlog

from personal_agent.config import settings

log = structlog.get_logger(__name__)

NTFY_URL_KEY = "AGENT_OWNER_NTFY_URL"
DEFAULT_TIMEOUT_S: float = 5.0
_TITLE = "Seshat gateway"

Opener = Callable[..., ContextManager[object]]


def _create_ipv4_connection(
    address: tuple[str, int],
    timeout: float | None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Open a TCP connection to the first IPv4 address of ``address`` (FRE-1632).

    Args:
        address: The host and the port.
        timeout: The connection timeout in seconds.
        source_address: The optional source address.

    Returns:
        The connected socket.

    Raises:
        OSError: When the host has no IPv4 address, or the connection fails.
    """
    host, port = address
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    if not infos:
        raise OSError("no IPv4 address")
    return socket.create_connection((str(infos[0][4][0]), port), timeout, source_address)


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self._create_connection = _create_ipv4_connection
        super().connect()


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        self._create_connection = _create_ipv4_connection
        super().connect()


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_IPv4HTTPConnection, req)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_IPv4HTTPSConnection, req)


urlopen_ipv4: Opener = urllib.request.build_opener(_IPv4HTTPHandler, _IPv4HTTPSHandler).open
"""The default opener. It connects over IPv4 only, whatever the host prefers."""


def _post(url: str, message: str, opener: Opener, timeout_s: float) -> str | None:
    """POST ``message`` to ``url``. Return ``None`` on success, else the failure kind.

    Everything that can raise sits inside the ``try``: ``urlparse`` raises ``ValueError`` for a
    malformed bracketed host, and ``encode`` raises ``UnicodeEncodeError`` for a lone surrogate.
    The failure kind is an exception type name or a fixed word. It never holds the URL.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "bad_url"
        request = urllib.request.Request(  # noqa: S310 - scheme is checked above
            url,
            data=message.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8", "Title": _TITLE},
            method="POST",
        )
        with opener(request, timeout=timeout_s) as response:  # noqa: S310
            status = int(getattr(response, "status", 0))
    except Exception as exc:
        return type(exc).__name__
    return None if 200 <= status < 300 else f"http_{status}"


async def push_to_owner(
    message: str,
    *,
    trace_id: str,
    opener: Opener = urlopen_ipv4,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bool:
    """Send one line to the owner's ntfy topic.

    Args:
        message: The one-line text. The caller must keep secret values out of it.
        trace_id: Trace identifier for the log.
        opener: The HTTP opener seam (injectable for tests).
        timeout_s: The hard limit for the whole send, in seconds.

    Returns:
        ``True`` when ntfy accepted the line with a 2xx response. ``False`` when the URL is unset
        or the send failed. It never raises. It never logs the URL.
    """
    url = settings.owner_ntfy_url
    if not url:
        log.warning("owner_ntfy_unconfigured", setting=NTFY_URL_KEY, trace_id=trace_id)
        return False
    try:
        failure = await asyncio.wait_for(
            asyncio.to_thread(_post, url, message, opener, timeout_s), timeout=timeout_s
        )
    except Exception as exc:
        failure = type(exc).__name__
    if failure is not None:
        log.warning("owner_ntfy_failed", failure=failure, trace_id=trace_id)
        return False
    log.info("owner_ntfy_sent", trace_id=trace_id)
    return True


TEST_LINE = "Seshat gateway: this is a test of the owner notification."


def main() -> int:
    """Send one test line through the gateway code path (FRE-1758, AC-5).

    Run it inside the gateway container: ``python -m personal_agent.service.owner_ntfy``. It reads
    the URL through ``settings`` and calls :func:`push_to_owner`. It reads and writes no override row.

    Returns:
        ``0`` when ntfy accepted the line. ``1`` otherwise. The outcome word (``sent``, ``failed``
        or ``unconfigured``) goes to stdout. The URL is never printed.
    """
    if not settings.owner_ntfy_url:
        sys.stdout.write("unconfigured\n")
        return 1
    sent = asyncio.run(push_to_owner(TEST_LINE, trace_id="owner-ntfy-live-check"))
    sys.stdout.write("sent\n" if sent else "failed\n")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
