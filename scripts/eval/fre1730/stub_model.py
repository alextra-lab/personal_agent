"""FRE-1730: a loopback stub of the Anthropic Messages API. It never calls a real model.

It answers with fixed text and a fixed usage, and it keeps each request body. So a test can read what a real
``claude`` process sent, and can make that process report a known cost.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY_TEXT = "stub reply"


@dataclass
class StubModel:
    """What the stub answers and what it received.

    Attributes:
        input_tokens: The input tokens that each reply reports.
        output_tokens: The output tokens that each reply reports.
        status: The HTTP status of each reply. A value other than 200 is an API error.
        tool_loop: Each reply asks for a Bash call, so the session goes on until something stops it.
        tool_command: The command of that call.
        requests: The body of each ``/v1/messages`` request.
    """

    input_tokens: int = 10
    output_tokens: int = 5
    status: int = 200
    tool_loop: bool = False
    tool_command: str = "true"
    requests: list[dict[str, object]] = field(default_factory=list)

    def message(self, model: str) -> dict[str, object]:
        """Build a whole message.

        Args:
            model: The model that the request named.

        Returns:
            The message body.
        """
        block: dict[str, object] = {"type": "text", "text": REPLY_TEXT}
        if self.tool_loop:
            block = {
                "type": "tool_use",
                "id": f"toolu_{len(self.requests)}",
                "name": "Bash",
                "input": {"command": self.tool_command, "description": "stub"},
            }
        return {
            "id": f"msg_stub_{len(self.requests)}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [block],
            "stop_reason": "tool_use" if self.tool_loop else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
        }

    def events(self, model: str) -> list[tuple[str, dict[str, object]]]:
        """Build the same message as server-sent events.

        Args:
            model: The model that the request named.

        Returns:
            The event names and bodies, in order.
        """
        whole = self.message(model)
        start = {**whole, "content": [], "stop_reason": None}
        block = whole["content"][0]  # type: ignore[index]
        if self.tool_loop:
            first = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            first = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": REPLY_TEXT}
        return [
            ("message_start", {"type": "message_start", "message": start}),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": first},
            ),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": whole["stop_reason"], "stop_sequence": None},
                    "usage": {"output_tokens": self.output_tokens},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]


def _handler(stub: StubModel) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, status: int, body: dict[str, object]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
            if "count_tokens" in self.path:
                self._send_json(200, {"input_tokens": stub.input_tokens})
                return
            if not self.path.startswith("/v1/messages"):
                self._send_json(
                    404,
                    {"type": "error", "error": {"type": "not_found_error", "message": self.path}},
                )
                return
            stub.requests.append(body)
            if stub.status != 200:
                error = {"type": "rate_limit_error", "message": "stub quota error"}
                self._send_json(stub.status, {"type": "error", "error": error})
                return
            model = str(body.get("model", "stub"))
            if not body.get("stream"):
                self._send_json(200, stub.message(model))
                return
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for name, event in stub.events(model):
                self.wfile.write(f"event: {name}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

    return Handler


@contextmanager
def serve(stub: StubModel) -> Iterator[str]:
    """Serve the stub on a free loopback port.

    Args:
        stub: The stub.

    Yields:
        The base URL for ``ANTHROPIC_BASE_URL``.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
