"""FRE-1517 stage 2 smoke checks — the two paths the stage-1 screen left open, sent direct to llama.cpp.

Master's correction of 2026-10-09 (FRE-1517): llama-server leaks tool-call markup under
``tool_choice: "none"`` only when the request still asks for a call. Stage 1's check c asked for
one. These checks use production's own shapes instead:

    synth_primary     the forced synthesis (``executor.py``, "You have reached the tool call limit"):
                      a tool result already in the history, tools retained, ``tool_choice`` "none",
                      the primary's default mode (thinking on)
    synth_worker      the same shape with thinking off, as a worker's own forced synthesis
    worker_tool_call  the worker path: production's worker system prompt and task message
                      (``sub_agent._build_sub_agent_system_prompt`` and ``_build_task_message``),
                      the researcher's one tool, thinking off. It must emit a parseable call

The primary's system prompt, tools and sampling come from a request body that the D7 probe's
capture step saved with ``--model gemma-4-26b-a4b``, so they are the gateway's bytes for that
deployment. Every call is one JSON row. No tool is ever executed.

Run from the repo root, only inside a window that master opened:

    uv run python -m scripts.eval.fre1517.stage2_smoke --captured <run>/captured/mallorca.json
        --model unsloth/gemma-4-26B-A4B-it --out <file>.jsonl     (one command line)
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8600/v1/chat/completions"
#: Verbatim from ``orchestrator/executor.py``, the forced-synthesis message.
FORCED_SYNTHESIS = (
    "You have reached the tool call limit. "
    "Do NOT call any more tools. "
    "Using only the tool results already in this conversation, "
    "synthesize a complete, helpful answer to the user's original request."
)
SEARCH_ARGS = {"query": "events in Sóller Mallorca 10-17 October 2026"}
SEARCH_RESULT = json.dumps(
    {
        "results": [
            {
                "title": "Fira de Sóller and Es Firó — Ajuntament de Sóller",
                "url": "https://example.org/soller-fira",
                "content": "The Fira i Firó of Sóller takes place each May. In October the town "
                "hosts the Mostra de Cuina, with tasting menus in local restaurants.",
            },
            {
                "title": "Concerts at the Teatre Defla, Sóller",
                "url": "https://example.org/defla",
                "content": "Classical concert, Saturday 11 October 2026, 20:00, tickets 15 EUR.",
            },
        ]
    }
)
WORKER_TASK = (
    "Find cultural events, markets and concerts in and near Sóller, Mallorca, between 10 and 17 "
    "October 2026, with dates, venues and a source URL for each."
)
MARKUP = ("tool_call", "<function", "call:web_search", '"name": "web_search"', "<|tool")


def _tool_named(body: Mapping[str, Any], name: str) -> dict[str, Any]:
    for tool in body["tools"]:
        if tool.get("function", {}).get("name") == name:
            return tool
    raise SystemExit(f"the captured body has no {name} tool")


def synth_body(captured: Mapping[str, Any], model: str, thinking: bool) -> dict[str, Any]:
    """Build a forced-synthesis request on the captured primary body.

    Args:
        captured: The captured primary request body.
        model: The served model id.
        thinking: The value of ``enable_thinking``.

    Returns:
        The request body.
    """
    body = copy.deepcopy(dict(captured))
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "web_search", "arguments": json.dumps(SEARCH_ARGS)},
    }
    body["messages"] = [
        *body["messages"],
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "content": SEARCH_RESULT},
        {"role": "user", "content": FORCED_SYNTHESIS},
    ]
    body.update(model=model, stream=False, tool_choice="none", max_tokens=4096)
    body["chat_template_kwargs"] = {
        **body.get("chat_template_kwargs", {}),
        "enable_thinking": thinking,
    }
    return body


def worker_body(captured: Mapping[str, Any], model: str) -> dict[str, Any]:
    """Build the worker's first request from production's prompt builders.

    Args:
        captured: The captured primary request body, for the sampling and the tool schema.
        model: The served model id.

    Returns:
        The request body.
    """
    from personal_agent.orchestrator.sub_agent import (
        _build_sub_agent_system_prompt,
        _build_task_message,
    )
    from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
    from personal_agent.orchestrator.worker_types import WorkerType

    spec = SubAgentSpec(
        task=WORKER_TASK,
        context=[],
        tools=["web_search"],
        worker_type=WorkerType.RESEARCHER,
        thoroughness="standard",
        turn_started_at=datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
    )
    body = copy.deepcopy(dict(captured))
    body["messages"] = [
        {"role": "system", "content": _build_sub_agent_system_prompt(spec)},
        {"role": "user", "content": _build_task_message(spec, "fre1517-smoke", None)},
    ]
    body["tools"] = [_tool_named(captured, "web_search")]
    body.pop("tool_choice", None)
    body.update(model=model, stream=False, max_tokens=2048)
    body["chat_template_kwargs"] = {
        **body.get("chat_template_kwargs", {}),
        "enable_thinking": False,
    }
    return body


def judge(check: str, message: Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether one reply passes its check, and why.

    Args:
        check: The check name.
        message: The reply's ``choices[0].message``.

    Returns:
        The verdict and a short reason.
    """
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    if check.startswith("synth"):
        leaked = [m for m in MARKUP if m in content]
        if calls:
            return False, f"{len(calls)} tool_calls under tool_choice none"
        if leaked:
            return False, f"markup in content: {leaked}"
        return (bool(content.strip()), "clean answer" if content.strip() else "empty content")
    if not calls:
        return False, "no tool_calls"
    for c in calls:
        try:
            args = json.loads(c["function"]["arguments"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            return False, f"arguments do not parse: {exc}"
        if c["function"].get("name") != "web_search" or not str(args.get("query", "")).strip():
            return False, f"bad call: {c['function']}"
    return True, f"{len(calls)} parseable web_search call(s)"


def main(argv: Sequence[str] | None = None) -> int:
    """Run every check ``--trials`` times and print the verdicts.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 when every trial passes, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--captured", type=Path, required=True)
    parser.add_argument("--model", required=True, help="served model id")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="build the bodies, send nothing")
    args = parser.parse_args(argv)
    captured = json.loads(args.captured.read_text())["body"]
    bodies = {
        "synth_primary": synth_body(captured, args.model, thinking=True),
        "synth_worker": synth_body(captured, args.model, thinking=False),
        "worker_tool_call": worker_body(captured, args.model),
    }
    if args.dry_run:
        for name, body in bodies.items():
            sampling = {k: body.get(k) for k in ("temperature", "top_p", "top_k", "min_p")}
            print(
                name,
                "msgs",
                len(body["messages"]),
                "tools",
                len(body["tools"]),
                "chars",
                len(json.dumps(body)),
                body["chat_template_kwargs"],
                body.get("tool_choice"),
                sampling,
            )
        return 0
    failed = 0
    with httpx.Client(timeout=900.0) as client, args.out.open("a") as out:
        for name, body in bodies.items():
            for trial in range(1, args.trials + 1):
                start = time.monotonic()
                resp = client.post(args.url, json=body)
                resp.raise_for_status()
                data = resp.json()
                message = data["choices"][0]["message"]
                ok, why = judge(name, message)
                failed += not ok
                row = {
                    "check": name,
                    "trial": trial,
                    "pass": ok,
                    "why": why,
                    "finish_reason": data["choices"][0].get("finish_reason"),
                    "tool_calls": message.get("tool_calls"),
                    "content": message.get("content"),
                    "reasoning_chars": len(message.get("reasoning_content") or ""),
                    "usage": data.get("usage"),
                    "system_fingerprint": data.get("system_fingerprint"),
                    "secs": round(time.monotonic() - start, 2),
                    "at": datetime.now(UTC).isoformat(),
                }
                out.write(json.dumps(row) + "\n")
                out.flush()
                print(name, trial, "PASS" if ok else "FAIL", why, f"{row['secs']}s", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
