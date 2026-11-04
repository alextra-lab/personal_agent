"""FRE-1730: the only way out of a session's jail.

The jail has no network. A relay inside it reaches this gate over a Unix socket. The gate tunnels ``CONNECT`` to an
allowed host, or forwards plain HTTP to the stub, and refuses the rest. Each refusal is kept for the report.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ANTHROPIC_API = ("api.anthropic.com", 443)
_BUFFER = 65536


@dataclass
class EgressPolicy:
    """What the gate lets through.

    Attributes:
        allowed: The hosts and ports that ``CONNECT`` may reach.
        stub: The address of the stub model, for plain HTTP. ``None`` on a paid run.
        refused: The target of each refused request.
    """

    allowed: frozenset[tuple[str, int]] = frozenset()
    stub: tuple[str, int] | None = None
    refused: list[str] = field(default_factory=list)


def _pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while data := source.recv(_BUFFER):
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _tunnel(client: socket.socket, upstream: socket.socket, first: bytes) -> None:
    if first:
        upstream.sendall(first)
    back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
    back.start()
    _pump(client, upstream)
    back.join()


def _read_head(client: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head and len(head) < _BUFFER:
        data = client.recv(_BUFFER)
        if not data:
            break
        head += data
    return head


def _handler(policy: EgressPolicy) -> type[socketserver.BaseRequestHandler]:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            head = _read_head(self.request)
            words = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if not words:
                return
            if words[0] == "CONNECT" and len(words) > 1:
                host, _, text = words[1].rpartition(":")
                port = int(text) if text.isdigit() else 0
                if (host, port) in policy.allowed:
                    with socket.create_connection((host, port)) as upstream:
                        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                        _tunnel(self.request, upstream, head.split(b"\r\n\r\n", 1)[1])
                    return
            elif policy.stub is not None:
                with socket.create_connection(policy.stub) as upstream:
                    _tunnel(self.request, upstream, head)
                return
            policy.refused.append(" ".join(words[:2]))
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")

    return Handler


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@contextmanager
def serve_gate(socket_path: Path, policy: EgressPolicy) -> Iterator[None]:
    """Serve the gate on a Unix socket.

    Args:
        socket_path: The socket's path.
        policy: The policy.

    Yields:
        Nothing.
    """
    server = _Server(str(socket_path), _handler(policy))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        # The session can write to this directory. A failure here must not lose the session's result.
        try:
            socket_path.unlink(missing_ok=True)
        except OSError:
            pass
