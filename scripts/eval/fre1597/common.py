"""FRE-1597 / ADR-0156 A5 (T-D1): the helpers that the routing run's three parts share.

- :func:`capture_turn` runs one probe-gateway turn with a scripted stub reply and checks the capture.
- :func:`save_capture` and :func:`load_capture` keep a capture on disk and check it again on load.
- :func:`add_local_args` and :func:`local_pins` pin the llama.cpp side of a run (ADR-0156 A5,
  "Pinning for every AC-F run").
- :func:`warm_up` sends the warm-up call that each local run starts with.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537 import capture
from scripts.eval.fre1537.common import RunPaths, append_jsonl, utc_now
from scripts.eval.fre1594 import spike
from scripts.eval.fre1594.reveal import request_bytes, with_nonce
from scripts.eval.fre1594.transport import Reply, Transport
from scripts.eval.fre1597.calls import JsonDict, TwoCalls, primary_calls

DEFAULT_URL = "http://127.0.0.1:8600/v1/chat/completions"
DEFAULT_MODEL_KEY = "qwen3.8-flash-next"
DEFAULT_DRAWS = 3
SCRIPT_FILE = "next_replies.json"
WARMUP = (
    "one request before the first draw of each invocation: the first item's first call, its own "
    "nonce, max_tokens 1; its row is kept in warmup.jsonl"
)


def sha256(data: bytes) -> str:
    """Return the hex SHA-256 of some bytes."""
    return hashlib.sha256(data).hexdigest()


def body_sha(body: Mapping[str, object]) -> str:
    """Return the SHA-256 of a request body's canonical bytes."""
    return sha256(request_bytes(body))


def script_for(skill: str | None) -> list[JsonDict]:
    """Return the stub's scripted replies: one ``read_skill`` call of ``skill``, then text."""
    if skill is None:
        return []
    return [
        {"tool_calls": [{"name": "read_skill", "arguments": {"name": skill}}]},
        {"content": "OK"},
    ]


def gateway_build(chat_url: str) -> str | None:
    """Return the ``build_fingerprint`` that the probe gateway's ``/health`` reports.

    Args:
        chat_url: The gateway's ``/chat`` URL.

    Returns:
        The fingerprint, or None if ``/health`` does not report one.
    """
    health = httpx.get(chat_url.rsplit("/chat", 1)[0] + "/health", timeout=10.0).json()
    build = health.get("build_fingerprint") if isinstance(health, Mapping) else None
    return build if isinstance(build, str) else None


def capture_turn(
    client: httpx.Client,
    chat_url: str,
    run_dir: Path,
    prompt: str,
    *,
    model_key: str,
    skill: str | None,
    settle: float = capture.SETTLE_SECONDS,
) -> TwoCalls:
    """Run one fresh-session turn through the probe gateway and check its primary calls.

    Args:
        client: HTTP client.
        chat_url: The gateway's ``/chat`` URL.
        run_dir: The run directory that ``gateway up`` was given (the stub writes there).
        prompt: The user prompt.
        model_key: The catalog key sent as ``/chat``'s ``model``.
        skill: The skill that the stub's first reply loads, or None for a text reply.
        settle: Seconds to wait for post-turn background calls.

    Returns:
        The checked calls.

    Raises:
        httpx.HTTPError: On a gateway error.
        calls.FidelityError: The calls are not the expected production sequence.
        ValueError: The stub did not use its whole script.
    """
    paths = RunPaths(run_dir)
    paths.stub_out.mkdir(parents=True, exist_ok=True)
    script = paths.stub_out / SCRIPT_FILE
    script.write_text(json.dumps(script_for(skill)))
    _, files = capture.turn(client, chat_url, paths, prompt, "OK", None, settle, model_key)
    left = json.loads(script.read_text())
    script.write_text("[]")
    if left:
        raise ValueError(f"the stub did not use {len(left)} scripted replies")
    return TwoCalls.from_capture(primary_calls(files, prompt), skill)


def save_capture(path: Path, two: TwoCalls, meta: Mapping[str, object]) -> None:
    """Write a capture record: the metadata, the skill and both bodies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {**meta, "skill": two.skill, "first": two.first, "second": two.second}
    path.write_text(json.dumps(record))


def load_capture(path: Path) -> tuple[dict[str, object], TwoCalls]:
    """Read a capture record and check its calls again.

    Returns:
        The record without the bodies, and the calls.
    """
    record = json.loads(path.read_text())
    bodies = [record.pop("first")]
    second = record.pop("second")
    if second is not None:
        bodies.append(second)
    skill = record.get("skill")
    return record, TwoCalls.from_capture(bodies, skill if isinstance(skill, str) else None)


def add_local_args(parser: argparse.ArgumentParser) -> None:
    """Add the llama.cpp arguments of a local run."""
    parser.add_argument("--llama-model", required=True, help="the llama.cpp model name")
    parser.add_argument("--quant", required=True, help="the quant of the loaded model")
    parser.add_argument(
        "--server-args",
        required=True,
        help="the llama-server launch arguments, verbatim (use the = form)",
    )
    parser.add_argument("--props-file", type=Path, help="a saved llama-server /props reply")
    parser.add_argument("--url", default=DEFAULT_URL)


def local_pins(args: argparse.Namespace, client: httpx.Client) -> dict[str, object]:
    """Return the llama.cpp pins: model, quant, URL, ``/props`` and the server arguments.

    Raises:
        SystemExit: If ``/props`` cannot be read, so the chat template would not be pinned.
    """
    props = spike.read_props(client, args.url, args.props_file)
    if "error" in props:
        raise SystemExit(
            f"no llama.cpp /props ({props['error']}): pass --props-file, so the run pins the "
            "chat template and the cache settings"
        )
    return {
        "llama_model": args.llama_model,
        "quant": args.quant,
        "llama_url": args.url,
        "llama_props": props,
        "llama_server_args": args.server_args,
    }


def request_pins(bodies: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Return the pins of a set of first requests: sampling, system prompts and tools.

    Args:
        bodies: The first requests of the items.

    Returns:
        ``sampling`` (every key but ``messages`` and ``tools``, which must agree across bodies),
        and the sorted distinct SHA-256 values of the system prompts and of the tools.

    Raises:
        SystemExit: If the bodies disagree on a sampling key.
    """
    samplings = {
        json.dumps({k: v for k, v in b.items() if k not in ("messages", "tools")}, sort_keys=True)
        for b in bodies
    }
    if len(samplings) != 1:
        raise SystemExit(f"the captured requests disagree on sampling: {sorted(samplings)}")
    systems: set[str] = set()
    tools: set[str] = set()
    for body in bodies:
        messages = body.get("messages")
        assert isinstance(messages, list)
        systems.add(sha256(str(messages[0].get("content")).encode()))
        tools.add(body_sha({"tools": body.get("tools")}))
    return {
        "sampling": json.loads(samplings.pop()),
        "system_prompt_sha256": sorted(systems),
        "tools_sha256": sorted(tools),
    }


TRANSIENT_STATUS = frozenset({502, 503, 504})
CUT_STREAM = "stream ended without a finish_reason"


def is_transient(reply: Reply) -> bool:
    """Return True for a transport failure: no HTTP status, 502/503/504, or a cut stream."""
    if reply.ok:
        return False
    if reply.status is None or reply.status in TRANSIENT_STATUS:
        return True
    return reply.status == httpx.codes.OK and CUT_STREAM in (reply.error or "")


class RetryOnce:
    """Send again once after a transport failure, and record the first failure on the reply.

    A reset, a 502, 503 or 504, or a stream cut before its ``finish_reason`` is sent once more.
    The returned reply's ``extra["retried"]`` holds the first failure's status and error, so the
    row shows it. A request error (any other status) is returned at once.
    """

    def __init__(self, inner: Transport) -> None:
        """Wrap a transport.

        Args:
            inner: The transport that sends.
        """
        self._inner = inner

    def send(self, body: Mapping[str, object]) -> Reply:
        """Send one request, and once more after a transport failure.

        Args:
            body: The request body.

        Returns:
            The reply of the last attempt.
        """
        reply = self._inner.send(body)
        if not is_transient(reply):
            return reply
        first = {"status": reply.status, "error": reply.error}
        again = self._inner.send(body)
        return dataclasses.replace(again, extra={**again.extra, "retried": first})


def warm_up(sender: Transport, body: Mapping[str, object], path: Path) -> None:
    """Send the warm-up call and keep its row.

    Args:
        sender: The transport.
        body: The first item's first request.
        path: The ``warmup.jsonl`` file.
    """
    warm = with_nonce({**body, "max_tokens": 1}, f"warmup-{secrets.token_hex(6)}")
    append_jsonl(path, {"at": utc_now(), **sender.send(warm).record()})
