"""FRE-1511 / ADR-0154 D7: recording stub for the model endpoint behind the capture gateway.

Records every chat-completions request body to ``<out>/calls/NNNN.json`` and answers with a scripted
reply, streamed in OpenAI SSE form. It never calls a real model. The capture driver writes
``<out>/next_reply.txt`` before each turn. A request that carries a ``tools`` array (the primary's call)
gets that text. Every other request gets ``OK``, or ``{}`` when it asks for a JSON object.

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
        if body.get("tools"):
            reply = out / "next_reply.txt"
            text = reply.read_text() if reply.exists() else "OK"
        elif (body.get("response_format") or {}).get("type") == "json_object":
            text = "{}"
        else:
            text = "OK"
        created = int(time.time())
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if not body.get("stream"):
            return JSONResponse(
                {
                    "id": f"stub-{n}",
                    "object": "chat.completion",
                    "created": created,
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
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
            frames = [
                {
                    **base,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": text},
                            "finish_reason": None,
                        }
                    ],
                },
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
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
