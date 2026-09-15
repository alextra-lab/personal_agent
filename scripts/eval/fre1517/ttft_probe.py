"""FRE-1517 amendment A9 — time to first token and prefix reuse, per local arm.

Nothing in ``llm_client`` or telemetry records time to first token, so this probe sends raw
streamed completions straight to the local engine, not through the gateway. For each target
size (8k, 32k and 64k input tokens by default) it sends two requests:

1. **cold** — a prompt that starts with a fresh random nonce, so no earlier cache can match it;
2. **warm** — the byte-identical prompt again, so a working prefix cache can serve it.

Each request records the first-chunk latency, the time of the first content or reasoning token,
the total time, and the final usage chunk (``prompt_tokens``, ``completion_tokens`` and
``prompt_tokens_details.cached_tokens``). The target size is approximate. The row records the
engine's own ``prompt_tokens``, and that is the size to report.

The request uses the worker shape Seshat sends to ``slm_local``: streamed with
``stream_options.include_usage``, ``chat_template_kwargs.enable_thinking`` false, and a small
``max_tokens``. It checks the arm's served id in ``/v1/models`` first (A4), like the session
runner.

Run from the repo root, only inside an arm's window and only with master's go:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. uv run python scripts/eval/fre1517/ttft_probe.py --arm mtplx_27b --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from scripts.eval.fre1517.session_runner import HERE, apply_session_facts, load_arm, tbd_fields

#: About four characters per token for this English filler; the engine's count is recorded.
CHARS_PER_TOKEN = 4
FILLER = (
    "The archive keeps a record of every voyage, with the date, the port, the cargo and the "
    "name of the captain, so that a later reader can check each claim against the ledger. "
)


def build_prompt(target_tokens: int, nonce: str) -> str:
    """Build a prompt of about ``target_tokens`` tokens that starts with ``nonce``.

    Args:
        target_tokens: The approximate input size wanted.
        nonce: A value placed first, so that no earlier cached prefix can match.

    Returns:
        The prompt text.
    """
    body_chars = max(0, target_tokens * CHARS_PER_TOKEN)
    repeats = body_chars // len(FILLER) + 1
    return f"Probe {nonce}.\n" + (FILLER * repeats)[:body_chars] + "\nReply with the word OK."


def stream_once(
    client: httpx.Client, base_url: str, model: str, prompt: str, max_tokens: int
) -> dict[str, Any]:
    """Send one streamed completion and time it.

    Args:
        client: HTTP client.
        base_url: The engine base URL, without ``/v1``.
        model: The served model id.
        prompt: The user message.
        max_tokens: The completion limit.

    Returns:
        HTTP status, first-chunk and first-token latency, total time, the final usage chunk,
        the finish reason, and an error text when the call failed.
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t0 = time.monotonic()
    first_chunk_s = first_token_s = None
    usage: dict[str, Any] | None = None
    finish_reason = None
    try:
        with client.stream(
            "POST", f"{base_url}/v1/chat/completions", json=body, timeout=900.0
        ) as resp:
            status = resp.status_code
            if status != 200:
                return {"status": status, "error": resp.read().decode(errors="replace")[:300]}
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                if first_chunk_s is None:
                    first_chunk_s = round(time.monotonic() - t0, 3)
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if first_token_s is None and (
                        delta.get("content") or delta.get("reasoning_content")
                    ):
                        first_token_s = round(time.monotonic() - t0, 3)
                    finish_reason = choice.get("finish_reason") or finish_reason
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return {
            "status": None,
            "error": repr(exc)[:300],
            "total_s": round(time.monotonic() - t0, 3),
        }
    return {
        "status": status,
        "first_chunk_s": first_chunk_s,
        "first_token_s": first_token_s,
        "total_s": round(time.monotonic() - t0, 3),
        "usage": usage,
        "cached_tokens": ((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens"),
        "finish_reason": finish_reason,
    }


def main() -> int:
    """Run the probe for one arm and append one JSONL row per request.

    Returns:
        0 on completion, 2 on a refused start.
    """
    p = argparse.ArgumentParser()
    p.add_argument("--arm", required=True, help="a local arm key in arms.yaml")
    p.add_argument("--run-id", required=True)
    p.add_argument(
        "--sizes", default="8000,32000,64000", help="target input tokens, comma-separated"
    )
    p.add_argument("--max-tokens", type=int, default=16)
    p.add_argument(
        "--session-fact", action="append", default=[], help="key=value, as in session_runner"
    )
    args = p.parse_args()

    arm = apply_session_facts(load_arm(args.arm), args.session_fact)
    if not arm["models_endpoint"]:
        sys.stderr.write(f"refused: {args.arm} is not a local arm\n")
        return 2
    missing = tbd_fields(arm)
    if missing:
        sys.stderr.write(f"refused: {args.arm} still has TBD fields: {missing}\n")
        return 2
    base_url = arm["models_endpoint"].removesuffix("/v1/models")
    out_dir = HERE / "out" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / f"{args.arm}.ttft.jsonl"

    with httpx.Client() as client:
        served = [
            m.get("id")
            for m in client.get(arm["models_endpoint"], timeout=30.0).json().get("data", [])
        ]
        if arm["served_model_id"] not in served:
            sys.stderr.write(
                f"refused: served ids {served} do not include {arm['served_model_id']}\n"
            )
            return 2
        for size in (int(s) for s in args.sizes.split(",")):
            prompt = build_prompt(size, secrets.token_hex(8))
            for phase in ("cold", "warm"):
                result = stream_once(
                    client, base_url, arm["served_model_id"], prompt, args.max_tokens
                )
                row = {
                    "run_id": args.run_id,
                    "arm": args.arm,
                    "arm_manifest": arm,
                    "served_ids": served,
                    "target_tokens": size,
                    "phase": phase,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    **result,
                }
                with rows_path.open("a") as f:
                    f.write(json.dumps(row) + "\n")
                sys.stdout.write(
                    f"{size:>6} {phase}: status={result.get('status')} first_token={result.get('first_token_s')}s "
                    f"prompt={(result.get('usage') or {}).get('prompt_tokens')} cached={result.get('cached_tokens')}\n"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
