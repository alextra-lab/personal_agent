"""The owner notification of the dispatch daemons (FRE-1550).

``cc-master`` is the seat that pushes a stall to the owner. When ``cc-master`` is itself the stuck
party (a running turn, or an unsent draft in its input), the daemons cannot reach it, and the
alert to master (``master_alert``) never lands. The daemons then send one short line straight to
the owner, through an outbound ntfy webhook.

**Setting.** ``AGENT_OWNER_NTFY_URL`` holds the full URL, topic included. It is unset by default.
Unset means the notifier logs ``owner_notify_unconfigured`` and sends nothing. On ntfy.sh the topic
name is the secret, so the URL never goes in the repo, in a log line or in a test fixture. This
module never logs it.

**Transport.** One plain-text ``POST`` with a timeout. A failed send returns ``False`` and never
raises, so the caller leaves its latch open and the next tick tries again. A daemon wraps its
notifier with ``once_per_tick``, so a tick makes at most one attempt and blocks for at most
``timeout_s``, however many conditions are due.

**Live check.** ``python -m scripts.dispatch.owner_notify`` sends one test line through the same
path and prints ``sent``, ``failed`` or ``unconfigured``. It never prints the URL.
"""

from __future__ import annotations

import http.client
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import ContextManager
from urllib.parse import urlparse

import structlog

from scripts.dispatch.trigger_ledger import Logger
from scripts.reconcile_board import _git_toplevel

NTFY_URL_KEY = "AGENT_OWNER_NTFY_URL"
DEFAULT_TIMEOUT_S: float = 5.0
_TITLE = "Seshat dispatch"

OwnerNotifier = Callable[[str], bool]
"""Sends one line to the owner. Returns ``True`` when the line was accepted."""

Opener = Callable[..., ContextManager[object]]


def load_owner_ntfy_url() -> str | None:
    """Resolve the owner notification URL (FRE-1550).

    Prefers the process environment; otherwise parses ``.env`` at the git toplevel for the exact
    key. This mirrors ``gating_watcher.load_channel_secret``: the dispatch daemons do not import
    ``personal_agent.config``.

    Returns:
        The URL, or ``None`` when the setting is absent or empty.
    """
    value = os.environ.get(NTFY_URL_KEY)
    if value:
        return value
    root = _git_toplevel()
    if root is None:
        return None
    env_path = root / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        key, sep, raw = line.strip().partition("=")
        if sep and key.strip() == NTFY_URL_KEY:
            return raw.strip().strip("'\"") or None
    return None


def _post(url: str, message: str, opener: Opener, timeout_s: float) -> str | None:
    """POST ``message`` to ``url``. Return ``None`` on success, else the failure kind.

    Everything that can raise sits inside the ``try``: ``urlparse`` raises ``ValueError`` for a
    malformed bracketed host, and ``encode`` raises ``UnicodeEncodeError`` for a lone surrogate.
    An operator typo in ``.env`` must not stop a daemon tick.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return "bad_url"
        request = urllib.request.Request(  # noqa: S310 - scheme is checked above
            url,
            data=message.encode("utf-8"),
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Title": _TITLE,
                "Priority": "high",
            },
            method="POST",
        )
        with opener(request, timeout=timeout_s) as response:  # noqa: S310
            status = int(getattr(response, "status", 0))
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        return type(exc).__name__
    return None if 200 <= status < 300 else f"http_{status}"


def post_ntfy(
    url: str,
    message: str,
    *,
    opener: Opener = urllib.request.urlopen,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bool:
    """Send one line to an ntfy topic.

    Args:
        url: The full topic URL. Only ``http`` and ``https`` are used.
        message: The one-line text.
        opener: The HTTP opener seam (injectable for tests).
        timeout_s: The request timeout in seconds.

    Returns:
        ``True`` on a 2xx response. ``False`` on any failure. It never raises.
    """
    return _post(url, message, opener, timeout_s) is None


def once_per_tick(notifier: OwnerNotifier) -> OwnerNotifier:
    """Limit ``notifier`` to one attempt (FRE-1550).

    Build one wrapper per daemon tick. The first call goes through, whether it succeeds or not.
    Later calls return ``False`` and send nothing. The caller leaves such a latch open, so the
    next tick picks the condition up. This bounds the time a tick spends on the network.

    Args:
        notifier: The notifier to limit.

    Returns:
        A notifier that makes at most one attempt.
    """
    attempted = False

    def limited(message: str) -> bool:
        nonlocal attempted
        if attempted:
            return False
        attempted = True
        return notifier(message)

    return limited


def make_owner_notifier(
    url: str | None,
    logger: Logger,
    *,
    opener: Opener = urllib.request.urlopen,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> OwnerNotifier:
    """Build the notifier that the daemons call (FRE-1550).

    Args:
        url: The topic URL, or ``None`` when the setting is unset.
        logger: Structured logger. It never receives the URL.
        opener: The HTTP opener seam (injectable for tests).
        timeout_s: The request timeout in seconds.

    Returns:
        A function that sends one line and returns whether the owner path accepted it. With
        ``url is None`` it logs a warning and returns ``False``.
    """

    def notify(message: str) -> bool:
        if url is None:
            logger.warning("owner_notify_unconfigured", setting=NTFY_URL_KEY)
            return False
        failure = _post(url, message, opener, timeout_s)
        if failure is not None:
            logger.warning("owner_notify_failed", failure=failure)
            return False
        logger.info("owner_notify_sent")
        return True

    return notify


TEST_LINE = "Seshat dispatch: this is a test of the owner notification."


def main(argv: Sequence[str] | None = None) -> int:
    """Send one test line through the owner path (FRE-1550, AC-4).

    Args:
        argv: Command-line arguments. None are used.

    Returns:
        ``0`` when the line was accepted. ``1`` otherwise. The outcome word goes to stdout. The
        URL is never printed.
    """
    del argv
    url = load_owner_ntfy_url()
    if url is None:
        print("unconfigured")
        return 1
    sent = make_owner_notifier(url, structlog.get_logger(__name__))(TEST_LINE)
    print("sent" if sent else "failed")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
