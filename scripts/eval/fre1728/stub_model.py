"""FRE-1728: a stub for every model that the dry run reaches. It never calls a real model.

It answers two callers:

- The stub gateway: the primary, entity extraction, every background role and the embedder.
- MemAudit's OpenAI client, through ``OPENAI_BASE_URL``: the simulated user, slot fill, the judge and attribution.
- Inspect Scout's scanner, through its ``model_base_url``.

A fact is a token of the form ``FACT-ABC123``. The stub's extraction writes each token that it finds in the user's
message as an entity and a claim. So a token is in the graph only if the real consolidation path wrote it, and it
is in a primary request only if the real recall path read it.

A message that holds ``ECHO-MEMORY:`` makes the primary answer with each token in its request, outside that
message. Every other primary call answers that it does not recall, which gives the dry run a retrieval failure.

This file imports nothing from the repo. It runs inside a container, started by ``stub_stack.py``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

MODEL = "unsloth/qwen3.8-flash-next"
PORT = 8700
ECHO_PREFIX = "ECHO-MEMORY:"
NO_RECALL = "I do not recall that."
_TOKEN = re.compile(r"FACT-[A-Z0-9]+")
_DIMENSIONS = 1024


def _text_of(message: dict[str, object]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _extraction_reply(prompt: str) -> str:
    match = re.search(r"User: (.*?)\nAssistant: ", prompt, re.DOTALL)
    user_message = match.group(1) if match else ""
    tokens = sorted(set(_TOKEN.findall(user_message)))
    return json.dumps(
        {
            "summary": f"The user mentioned {', '.join(tokens) or 'nothing durable'}.",
            "entities": [
                {
                    "name": token,
                    "type": "KnowledgeArtifact",
                    "output_kind": "knowledge",
                    "class": "World",
                    "description": f"The user said: {user_message.strip()}",
                    "description_update_kind": "new",
                    "properties": {},
                }
                for token in tokens
            ],
            "relationships": [],
            "stances": [],
            "claims": [
                {
                    "subject": "owner",
                    "content": f"The user said: {user_message.strip()}",
                    "facet": "",
                    "update_kind": "new",
                    "description": "",
                }
                for token in tokens
            ],
        }
    )


def _scout_reply(prompt: str) -> str:
    """Answer Scout's yes or no prompt. Only the internal-failure question finds a problem."""
    answer = "yes" if "internal failure" in prompt else "no"
    return f"Stub assessment of [M2].\n\nANSWER: {answer}"


def _memaudit_reply(system: str, prompt: str) -> str | None:
    """Answer MemAudit's prompts by their fixed text, or return None for any other prompt."""
    if system.startswith("You are the user asking an AI assistant"):
        digest = hashlib.sha256(prompt.split("Task / original question:")[-1][:80].encode())
        token = f"FACT-{digest.hexdigest()[:8].upper()}"
        return json.dumps(
            {"feedback_type": "satisfied", "text": f"Thanks. A detail about me: {token}."}
        )
    if "fill in this dimension about the user" in prompt:
        tokens = _TOKEN.findall(prompt)
        label = tokens[0] if tokens else "unknown"
        return json.dumps({"label": label, "reason": "stub slot fill"})
    if "Compare two short descriptions" in prompt:
        predicted = prompt.split("Predicted:")[-1]
        score = 1.0 if _TOKEN.search(predicted) else 0.0
        return json.dumps({"score": score, "reason": "stub judge"})
    if "can ever naturally invite" in prompt:
        return json.dumps({"can_invite": True, "reason": "stub oracle"})
    if "Did the User simulator" in prompt:
        found = _TOKEN.search(prompt)
        evidence = found.group(0) if found else "none"
        return json.dumps({"disclosed": True, "evidence": evidence, "reason": "stub disclosure"})
    if "Pick exactly one" in prompt:
        return json.dumps({"category": "A", "reason": "stub subclass", "evidence": "none"})
    return None


def _primary_reply(messages: list[dict[str, object]]) -> str:
    user_messages = [m for m in messages if m.get("role") == "user"]
    last = _text_of(user_messages[-1]) if user_messages else ""
    # The gateway puts a <turn_context> block before the user's message.
    if ECHO_PREFIX not in last:
        return NO_RECALL
    others = [_text_of(m) for m in messages if m is not user_messages[-1]]
    tokens = sorted(set(_TOKEN.findall(" ".join(others))))
    return f"I recall: {', '.join(tokens)}." if tokens else NO_RECALL


def reply_for(body: dict[str, object]) -> str:
    """Choose the reply for one chat-completions request.

    Args:
        body: The request body.

    Returns:
        The reply text.
    """
    messages = [m for m in body.get("messages", []) if isinstance(m, dict)]  # type: ignore[attr-defined]
    system = " ".join(_text_of(m) for m in messages if m.get("role") == "system")
    prompt = " ".join(_text_of(m) for m in messages if m.get("role") != "system")
    whole = f"{system}\n{prompt}"
    if "[BEGIN TRANSCRIPT" in whole:
        return _scout_reply(whole)
    if '"claims": [' in whole and "Return ONLY valid JSON" in whole:
        return _extraction_reply(whole)
    from_memaudit = _memaudit_reply(system, prompt)
    if from_memaudit is not None:
        return from_memaudit
    if body.get("tools"):
        return _primary_reply(messages)
    if (body.get("response_format") or {}).get("type") == "json_object":  # type: ignore[attr-defined]
        return "{}"
    return "OK"


def embedding_for(text: str) -> list[float]:
    """Build a repeatable bag-of-words vector, so that texts with shared words are similar.

    Args:
        text: The text.

    Returns:
        A unit vector.
    """
    vector = [0.0] * _DIMENSIONS
    for word in re.findall(r"[A-Za-z0-9-]+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % _DIMENSIONS] += 1.0
    norm = sum(x * x for x in vector) ** 0.5 or 1.0
    return [x / norm for x in vector]


def create_app(out: Path) -> FastAPI:
    """Build the stub app.

    Args:
        out: The directory for the recorded calls.

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

    @app.post("/v1/embeddings")
    @app.post("/embeddings")
    async def embeddings(request: Request) -> JSONResponse:
        body = await request.json()
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        data = [
            {"object": "embedding", "index": i, "embedding": embedding_for(str(text))}
            for i, text in enumerate(inputs)
        ]
        usage = {"prompt_tokens": 1, "total_tokens": 1}
        return JSONResponse(
            {"object": "list", "data": data, "model": body.get("model"), "usage": usage}
        )

    @app.post("/v1/rerank")
    @app.post("/rerank")
    async def rerank(request: Request) -> JSONResponse:
        body = await request.json()
        results = [
            {"index": i, "relevance_score": 0.9} for i, _ in enumerate(body.get("documents", []))
        ]
        return JSONResponse({"results": results, "data": results, "model": body.get("model")})

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat(request: Request) -> Response:
        body = await request.json()
        n = next(seq)
        text = reply_for(body)
        (calls / f"{n:05d}.json").write_text(
            json.dumps({"seq": n, "ts": time.time(), "reply": text, "body": body})
        )
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
        lines = [f"data: {json.dumps(f)}\n\n" for f in frames] + ["data: [DONE]\n\n"]
        return StreamingResponse(iter(lines), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    uvicorn.run(
        create_app(Path(os.environ.get("STUB_OUT", "/out"))),
        host="0.0.0.0",  # noqa: S104 - published on loopback only
        port=PORT,
        log_level="warning",
    )
