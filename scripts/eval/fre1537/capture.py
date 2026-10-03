"""FRE-1511 / ADR-0154 D7 and Appendix A1: capture the primary's request body of every fixture.

Each fixture runs as one real turn through the capture gateway (``gateway.py up``). The gateway's model
endpoint is the recording stub, so no model is called. A two-turn fixture runs turn 1 with the scripted
assistant reply, then turn 2 in the same session. The primary's request body of the final turn is saved
to ``<run>/captured/<label>.json``. The bodies hold full prompts and stay out of git.

    uv run python -m scripts.eval.fre1537.capture --run-dir <run>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537.common import RunPaths
from scripts.eval.fre1537.fixtures import Fixture, load_fixtures

DEFAULT_CHAT_URL = "http://127.0.0.1:9012/chat"
SETTLE_SECONDS = 2.0


def last_user_text(body: Mapping[str, object]) -> str:
    """Return the text of the last user message of a request body."""
    messages = body.get("messages") or []
    assert isinstance(messages, list)
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def turn(
    client: httpx.Client,
    chat_url: str,
    paths: RunPaths,
    message: str,
    reply: str,
    session_id: str | None,
    settle: float = SETTLE_SECONDS,
) -> tuple[str, list[Path]]:
    """Run one gateway turn and return the stub calls that it caused.

    Args:
        client: HTTP client.
        chat_url: The capture gateway's ``/chat`` URL.
        paths: The run paths.
        message: The user message.
        reply: The text that the stub returns to the primary.
        session_id: The session of a second turn, else ``None``.
        settle: Seconds to wait for post-turn background calls, so that they are not credited to the
            next turn.

    Returns:
        The session id and the new stub call files, oldest first.
    """
    calls = paths.stub_out / "calls"
    calls.mkdir(parents=True, exist_ok=True)
    (paths.stub_out / "next_reply.txt").write_text(reply)
    before = set(calls.glob("*.json"))
    params = {"message": message, "profile": "local", "channel": "EVAL"}
    if session_id:
        params["session_id"] = session_id
    response = client.post(chat_url, params=params, timeout=600)
    response.raise_for_status()
    time.sleep(settle)
    return response.json()["session_id"], sorted(set(calls.glob("*.json")) - before)


def primary_body(call_files: Sequence[Path], message: str) -> dict[str, object]:
    """Pick the primary's request body among the calls of a turn.

    Args:
        call_files: The stub call files of the turn.
        message: The turn's user message.

    Returns:
        The one request body that carries tools and ends on ``message``.

    Raises:
        ValueError: If the turn holds no such request, or more than one.
    """
    hits = []
    for path in call_files:
        body = json.loads(path.read_text())["body"]
        if body.get("tools") and message in last_user_text(body):
            hits.append(body)
    if len(hits) != 1:
        raise ValueError(f"expected one primary request for {message!r}, found {len(hits)}")
    return hits[0]


def capture_fixture(
    client: httpx.Client,
    chat_url: str,
    paths: RunPaths,
    fx: Fixture,
    settle: float = SETTLE_SECONDS,
) -> dict[str, object]:
    """Capture one fixture and return its record.

    Args:
        client: HTTP client.
        chat_url: The capture gateway's ``/chat`` URL.
        paths: The run paths.
        fx: The fixture.
        settle: Seconds to wait after each turn.

    Returns:
        ``label``, ``expected``, ``history``, ``message`` and the primary's request ``body``.
    """
    session_id = None
    if fx.history_user is not None and fx.history_assistant is not None:
        session_id, _ = turn(
            client, chat_url, paths, fx.history_user, fx.history_assistant, None, settle
        )
    _, files = turn(client, chat_url, paths, fx.message, "OK", session_id, settle)
    return {
        "label": fx.label,
        "expected": fx.expected,
        "history": fx.history,
        "message": fx.message,
        "body": primary_body(files, fx.message),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Capture every fixture that has no record yet.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--chat-url", default=DEFAULT_CHAT_URL)
    args = parser.parse_args(argv)
    paths = RunPaths(args.run_dir)
    paths.captured.mkdir(parents=True, exist_ok=True)
    with httpx.Client() as client:
        for fx in load_fixtures():
            target = paths.captured / f"{fx.label}.json"
            if target.exists():
                continue
            record = capture_fixture(client, args.chat_url, paths, fx)
            target.write_text(json.dumps(record))
            body = record["body"]
            assert isinstance(body, dict)
            print(fx.label, "tools", len(body["tools"]), "msgs", len(body["messages"]), flush=True)
    print(f"captured {len(list(paths.captured.glob('*.json')))} fixtures in {paths.captured}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
