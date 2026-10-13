"""FRE-1511 / ADR-0154 D7: recording stub for the model endpoint behind the capture gateway.

Records every chat-completions request body to ``<out>/calls/NNNN.json`` and answers with a scripted
reply, streamed in OpenAI SSE form. It never calls a real model. The capture driver writes
``<out>/next_reply.txt`` before each turn. A request that carries a ``tools`` array (the primary's call)
gets that text. Every other request gets ``OK``, or ``{}`` when it asks for a JSON object.

FRE-1597: when ``<out>/next_replies.json`` holds a non-empty list, a request that carries ``tools``
takes its first entry instead, and the rest is written back. An entry is ``{"content": "..."}`` or
``{"tool_calls": [{"name": ..., "arguments": {...}}]}``. A tool-call entry makes the gateway run the
tool and send its next request, so a capture holds the production body of that second call.

This file imports nothing from the repo. It runs inside a container, started by ``gateway.py``.
"""

from __future__ import annotations

import itertools
import json
import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

MODEL = "unsloth/qwen3.8-flash-next"
PORT = 8700


def _next_scripted(out: Path) -> dict[str, object] | None:
    """Take the first entry of ``<out>/next_replies.json`` and write the rest back (FRE-1597).

    Returns:
        The entry, or None when the file is absent or its list is empty.
    """
    path = out / "next_replies.json"
    if not path.exists():
        return None
    entries = json.loads(path.read_text())
    if not entries:
        return None
    path.write_text(json.dumps(entries[1:]))
    entry = entries[0]
    return entry if isinstance(entry, dict) else None


def _tool_calls(calls: object, n: int) -> list[dict[str, object]]:
    """Return scripted calls in the OpenAI ``tool_calls`` shape, ids unique per request."""
    out: list[dict[str, object]] = []
    for i, call in enumerate(calls if isinstance(calls, list) else []):
        out.append(
            {
                "id": f"stub_{n}_{i}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call.get("arguments") or {}),
                },
            }
        )
    return out


def create_app(out: Path) -> FastAPI:
    """Build the stub app.

    Args:
        out: Directory for the recorded calls and the reply file.

    Returns:
        The FastAPI app.
    """
    calls = out / "calls"
    calls.mkdir(parents=True, exist_ok=True)
    seq = itertools.count(len(list(calls.glob("*.json"))) + 1)
    app = FastAPI()

    @app.get("/v1/models")
    @app.get("/models")
    async def models() -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [{"id": MODEL, "backend": "llamacpp", "context_length": 131072}],
            }
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat(request: Request) -> Response:
        body = await request.json()
        n = next(seq)
        (calls / f"{n:04d}.json").write_text(
            json.dumps({"seq": n, "ts": time.time(), "body": body})
        )
        calls_out: list[dict[str, object]] = []
        if body.get("tools"):
            scripted = _next_scripted(out)
            if scripted is not None and scripted.get("tool_calls"):
                calls_out = _tool_calls(scripted["tool_calls"], n)
                text = ""
            elif scripted is not None:
                text = str(scripted.get("content", ""))
            else:
                reply = out / "next_reply.txt"
                text = reply.read_text() if reply.exists() else "OK"
        elif (body.get("response_format") or {}).get("type") == "json_object":
            text = "{}"
        else:
            text = "OK"
        finish = "tool_calls" if calls_out else "stop"
        created = int(time.time())
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not body.get("stream"):
            message: dict[str, object] = {"role": "assistant", "content": text}
            if calls_out:
                message["tool_calls"] = calls_out
            return JSONResponse(
                {
                    "id": f"stub-{n}",
                    "object": "chat.completion",
                    "created": created,
                    "model": MODEL,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": usage,
                }
            )
        base = {
            "id": f"stub-{n}",
            "object": "chat.completion.chunk",
            "created": created,
            "model": MODEL,
        }

        def sse() -> list[str]:
            delta: dict[str, object] = {"role": "assistant", "content": text}
            if calls_out:
                delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(calls_out)]
            frames = [
                {
                    **base,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                {**base, "choices": [], "usage": usage},
            ]
            return [f"data: {json.dumps(f)}\n\n" for f in frames] + ["data: [DONE]\n\n"]

        return StreamingResponse(iter(sse()), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    uvicorn.run(
        create_app(Path(os.environ.get("STUB_OUT", "/out"))),
        host="0.0.0.0",  # noqa: S104 - reachable only on the container network
        port=PORT,
        log_level="warning",
    )
