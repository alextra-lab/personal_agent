# ruff: noqa: D103
"""FRE-1550: the outbound owner notification (an ntfy webhook).

Every test talks to a local fake HTTP server. None calls ntfy.sh.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ContextManager

import pytest
from scripts.dispatch import owner_notify


class _Received:
    """What the fake server saw."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []


@contextmanager
def _fake_ntfy(*, status: int = 200, delay_s: float = 0.0) -> Iterator[tuple[str, _Received]]:
    received = _Received()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8")
            received.requests.append(
                {"path": self.path, "body": body, "headers": dict(self.headers.items())}
            )
            if delay_s:
                time.sleep(delay_s)
            self.send_response(status)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/fake-topic", received
    finally:
        server.shutdown()
        server.server_close()


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/fake-topic"


class _CapturingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append(("warning", event, fields))


def test_post_ntfy_sends_one_short_line_to_the_topic() -> None:
    with _fake_ntfy() as (url, received):
        assert owner_notify.post_ntfy(url, "PR #1218 is stuck") is True
    (request,) = received.requests
    assert request["path"] == "/fake-topic"
    assert request["body"] == "PR #1218 is stuck"
    headers = request["headers"]
    assert isinstance(headers, dict)
    assert headers["Title"] == "Seshat dispatch"
    assert headers["Priority"] == "high"


def test_post_ntfy_sends_non_ascii_text_in_the_body() -> None:
    with _fake_ntfy() as (url, received):
        assert owner_notify.post_ntfy(url, "PR #7 … stuck") is True
    assert received.requests[0]["body"] == "PR #7 … stuck"


def test_a_server_error_is_a_failed_send() -> None:
    with _fake_ntfy(status=500) as (url, received):
        assert owner_notify.post_ntfy(url, "x") is False
    assert len(received.requests) == 1


def test_a_hung_server_is_cut_off_by_the_timeout() -> None:
    with _fake_ntfy(delay_s=2.0) as (url, _received):
        started = time.monotonic()
        assert owner_notify.post_ntfy(url, "x", timeout_s=0.2) is False
        assert time.monotonic() - started < 1.5


def test_an_unreachable_server_is_a_failed_send_not_an_exception() -> None:
    assert owner_notify.post_ntfy(_closed_port_url(), "x", timeout_s=0.5) is False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.invalid/x", "not a url", ""])
def test_only_http_and_https_urls_are_used(url: str) -> None:
    def never(*_a: object, **_k: object) -> ContextManager[object]:
        raise AssertionError("the opener must not be called")

    assert owner_notify.post_ntfy(url, "x", opener=never) is False


def test_an_unconfigured_notifier_warns_and_sends_nothing() -> None:
    logger = _CapturingLogger()

    def never(*_a: object, **_k: object) -> ContextManager[object]:
        raise AssertionError("the opener must not be called")

    notify = owner_notify.make_owner_notifier(None, logger, opener=never)
    assert notify("PR #1 is stuck") is False
    assert [(level, event) for level, event, _f in logger.events] == [
        ("warning", "owner_notify_unconfigured")
    ]


def test_a_configured_notifier_logs_the_outcome_and_never_the_url() -> None:
    logger = _CapturingLogger()
    with _fake_ntfy() as (url, received):
        notify = owner_notify.make_owner_notifier(url, logger)
        assert notify("PR #1 is stuck") is True
        assert len(received.requests) == 1
    failing = owner_notify.make_owner_notifier(_closed_port_url(), logger, timeout_s=0.5)
    assert failing("PR #1 is stuck") is False
    events = [event for _level, event, _f in logger.events]
    assert events == ["owner_notify_sent", "owner_notify_failed"]
    dumped = repr(logger.events)
    assert url not in dumped
    assert "/fake-topic" not in dumped
    assert "127.0.0.1" not in dumped


def test_once_per_tick_lets_one_attempt_through_even_when_it_fails() -> None:
    calls: list[str] = []

    def failing(message: str) -> bool:
        calls.append(message)
        return False

    limited = owner_notify.once_per_tick(failing)
    assert [limited("a"), limited("b"), limited("c")] == [False, False, False]
    assert calls == ["a"]


def test_once_per_tick_passes_the_result_of_the_first_attempt() -> None:
    sent: list[str] = []

    def ok(message: str) -> bool:
        sent.append(message)
        return True

    limited = owner_notify.once_per_tick(ok)
    assert [limited("a"), limited("b")] == [True, False]
    assert sent == ["a"]


def test_load_owner_ntfy_url_prefers_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("AGENT_OWNER_NTFY_URL=http://127.0.0.1:9/from-file\n")
    monkeypatch.setattr(owner_notify, "_git_toplevel", lambda: tmp_path)
    monkeypatch.setenv("AGENT_OWNER_NTFY_URL", "http://127.0.0.1:9/from-env")
    assert owner_notify.load_owner_ntfy_url() == "http://127.0.0.1:9/from-env"


def test_load_owner_ntfy_url_reads_the_exact_env_file_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "AGENT_OWNER_NTFY_URL_OTHER=http://127.0.0.1:9/wrong\n"
        "AGENT_OWNER_NTFY_URLX=http://127.0.0.1:9/wrong\n"
        '  AGENT_OWNER_NTFY_URL = "http://127.0.0.1:9/right"  \n'
    )
    monkeypatch.delenv("AGENT_OWNER_NTFY_URL", raising=False)
    monkeypatch.setattr(owner_notify, "_git_toplevel", lambda: tmp_path)
    assert owner_notify.load_owner_ntfy_url() == "http://127.0.0.1:9/right"


@pytest.mark.parametrize("content", [None, "OTHER=1\n", "AGENT_OWNER_NTFY_URL=\n"])
def test_load_owner_ntfy_url_is_none_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / ".env").write_text(content)
    monkeypatch.delenv("AGENT_OWNER_NTFY_URL", raising=False)
    monkeypatch.setattr(owner_notify, "_git_toplevel", lambda: tmp_path)
    assert owner_notify.load_owner_ntfy_url() is None


def test_the_cli_sends_one_test_line_and_prints_only_the_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with _fake_ntfy() as (url, received):
        monkeypatch.setenv("AGENT_OWNER_NTFY_URL", url)
        assert owner_notify.main([]) == 0
    assert [r["body"] for r in received.requests] == [owner_notify.TEST_LINE]
    captured = capsys.readouterr()
    assert captured.out.strip().splitlines()[-1] == "sent"
    assert url not in captured.out + captured.err
    assert "fake-topic" not in captured.out + captured.err


def test_the_cli_reports_failed_and_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_OWNER_NTFY_URL", _closed_port_url())
    assert owner_notify.main([]) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "failed"
    monkeypatch.delenv("AGENT_OWNER_NTFY_URL")
    monkeypatch.setattr(owner_notify, "_git_toplevel", lambda: tmp_path)
    assert owner_notify.main([]) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "unconfigured"


@pytest.mark.parametrize("url", ["http://[abc/topic", "http://[notip]/x"])
def test_a_malformed_url_is_a_failed_send_not_an_exception(url: str) -> None:
    """An operator typo in .env must not kill a daemon tick."""

    def never(*_a: object, **_k: object) -> ContextManager[object]:
        raise AssertionError("the opener must not be called")

    assert owner_notify.post_ntfy(url, "x", opener=never) is False
    notify = owner_notify.make_owner_notifier(url, _CapturingLogger(), opener=never)
    assert notify("x") is False


def test_a_message_that_cannot_be_encoded_is_a_failed_send_not_an_exception() -> None:
    with _fake_ntfy() as (url, received):
        assert owner_notify.post_ntfy(url, "bad \ud800 text") is False
    assert received.requests == []
