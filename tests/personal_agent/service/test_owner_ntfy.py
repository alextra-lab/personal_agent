"""The owner ntfy push of the gateway (FRE-1758, AC-2 and AC-3).

The sender never raises, never logs the URL, and bounds the time it takes. The URL is a secret
because on ntfy.sh the topic name is the credential.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import structlog

from personal_agent.config import settings
from personal_agent.service import owner_ntfy

_TOPIC = "sentinel-topic-fre1758"
_TRACE = "trace-fre1758"
_MESSAGE = "Seshat gateway: 2 stored overrides have no audit row"


class _Received:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []


@contextmanager
def _server(status: int = 200, delay_s: float = 0.0) -> Iterator[tuple[str, _Received]]:
    """Serve one local HTTP endpoint. Yield its URL (with the sentinel topic) and the requests."""
    received = _Received()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.requests.append((self.path, dict(self.headers), body))
            time.sleep(delay_s)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/{_TOPIC}", received
    finally:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/{_TOPIC}"


def _events(entries: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [entry for entry in entries if entry["event"] == event]


@pytest.mark.asyncio
async def test_an_unset_url_logs_once_and_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "owner_ntfy_url", None)
    opened: list[object] = []

    def opener(*args: object, **_kwargs: object) -> object:
        opened.append(args)
        raise AssertionError("an unset URL must not open a connection")

    with structlog.testing.capture_logs() as entries:
        sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE, opener=opener)

    assert sent is False
    assert opened == []
    assert len(_events(entries, "owner_ntfy_unconfigured")) == 1
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_a_send_posts_the_plain_text_line_and_logs_no_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _server() as (url, received):
        monkeypatch.setattr(settings, "owner_ntfy_url", url)
        with structlog.testing.capture_logs() as entries:
            sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE)

    assert sent is True
    assert len(received.requests) == 1
    path, headers, body = received.requests[0]
    assert path == f"/{_TOPIC}"
    assert body == _MESSAGE.encode("utf-8")
    assert headers["Content-Type"].startswith("text/plain")
    assert len(_events(entries, "owner_ntfy_sent")) == 1
    assert _TOPIC not in str(entries)
    assert "127.0.0.1" not in str(entries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (urllib.error.URLError(f"unreachable http://host/{_TOPIC}"), "URLError"),
        (TimeoutError(f"timed out http://host/{_TOPIC}"), "TimeoutError"),
        (ConnectionRefusedError(f"refused http://host/{_TOPIC}"), "ConnectionRefusedError"),
        (http.client.RemoteDisconnected(f"closed http://host/{_TOPIC}"), "RemoteDisconnected"),
        (RuntimeError(f"unexpected http://host/{_TOPIC}"), "RuntimeError"),
    ],
)
async def test_a_failed_send_logs_the_error_type_only_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str
) -> None:
    monkeypatch.setattr(settings, "owner_ntfy_url", f"https://ntfy.invalid/{_TOPIC}")

    def opener(*_args: object, **_kwargs: object) -> object:
        raise error

    with structlog.testing.capture_logs() as entries:
        sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE, opener=opener)

    assert sent is False
    failed = _events(entries, "owner_ntfy_failed")
    assert [entry["failure"] for entry in failed] == [kind]
    assert _TOPIC not in str(entries)
    assert "ntfy.invalid" not in str(entries)


@pytest.mark.asyncio
async def test_a_refused_connection_logs_the_error_type_and_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "owner_ntfy_url", _closed_port_url())

    with structlog.testing.capture_logs() as entries:
        sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE)

    assert sent is False
    failed = _events(entries, "owner_ntfy_failed")
    assert len(failed) == 1
    assert failed[0]["failure"] in {"URLError", "ConnectionRefusedError"}
    assert _TOPIC not in str(entries)


@pytest.mark.asyncio
async def test_a_server_error_logs_the_status_kind_and_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _server(status=500) as (url, _received):
        monkeypatch.setattr(settings, "owner_ntfy_url", url)
        with structlog.testing.capture_logs() as entries:
            sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE)

    assert sent is False
    assert [entry["failure"] for entry in _events(entries, "owner_ntfy_failed")] == ["HTTPError"]
    assert _TOPIC not in str(entries)


@pytest.mark.asyncio
async def test_a_hung_server_stops_at_the_timeout_and_the_call_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _server(delay_s=3.0) as (url, _received):
        monkeypatch.setattr(settings, "owner_ntfy_url", url)
        started = time.monotonic()
        with structlog.testing.capture_logs() as entries:
            sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE, timeout_s=0.3)
        elapsed = time.monotonic() - started

    assert sent is False
    assert elapsed < 1.5
    assert [entry["failure"] for entry in _events(entries, "owner_ntfy_failed")] == ["TimeoutError"]
    assert _TOPIC not in str(entries)


@pytest.mark.asyncio
async def test_a_blocked_opener_cannot_hold_the_call_past_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hard bound holds when the opener ignores its own timeout (for example, a slow DNS read)."""
    monkeypatch.setattr(settings, "owner_ntfy_url", f"https://ntfy.invalid/{_TOPIC}")

    def opener(*_args: object, **_kwargs: object) -> object:
        time.sleep(2.0)
        raise AssertionError("the call must have returned before this point")

    started = time.monotonic()
    with structlog.testing.capture_logs() as entries:
        sent = await owner_ntfy.push_to_owner(
            _MESSAGE, trace_id=_TRACE, opener=opener, timeout_s=0.2
        )
    elapsed = time.monotonic() - started

    assert sent is False
    assert elapsed < 1.0
    assert [entry["failure"] for entry in _events(entries, "owner_ntfy_failed")] == ["TimeoutError"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [f"ftp://host/{_TOPIC}", f"{_TOPIC}", f"http://[bad/{_TOPIC}"])
async def test_a_malformed_url_logs_a_kind_and_never_the_url(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(settings, "owner_ntfy_url", url)

    with structlog.testing.capture_logs() as entries:
        sent = await owner_ntfy.push_to_owner(_MESSAGE, trace_id=_TRACE)

    assert sent is False
    assert len(_events(entries, "owner_ntfy_failed")) == 1
    assert _TOPIC not in str(entries)


def test_the_live_check_prints_unconfigured_when_the_url_is_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "owner_ntfy_url", None)

    code = owner_ntfy.main()

    assert code == 1
    assert capsys.readouterr().out.strip() == "unconfigured"


def test_the_live_check_sends_one_test_line_and_never_prints_the_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with _server() as (url, received):
        monkeypatch.setattr(settings, "owner_ntfy_url", url)
        code = owner_ntfy.main()

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "sent"
    assert len(received.requests) == 1
    assert received.requests[0][2] == owner_ntfy.TEST_LINE.encode("utf-8")
    assert _TOPIC not in captured.out + captured.err


def test_the_live_check_prints_failed_when_the_send_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "owner_ntfy_url", _closed_port_url())

    code = owner_ntfy.main()

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.strip() == "failed"
    assert _TOPIC not in captured.out + captured.err
