"""FRE-1600 AC-1 — what a follow-up turn prefills again, by how the session history is stored.

The script replays one scripted session (``session.yaml``, seven turns) directly to llama.cpp, the
FRE-1592 M2 pattern: no gateway turn, so no graph, consolidation or cloud call. Each turn sends the
production first call (the ``<turn_context>`` user message, then the ``memory_recall`` exchange).
A tool turn then sends a second call with one scripted ``web_search`` exchange. Every call
generates :data:`PROBE_MAX_TOKENS` tokens. The scripted reply then goes into the history, as the
FRE-1592 M2 turn-1 reply did. The session runs once per arm, and each arm has its own nonce:

* ``today`` — the history as ``service/app.py`` stores it today: the raw user text and the reply;
* ``control`` — the previous turn's last request, then the reply, then the new content (the
  ticket's control). This is also the history of the "store the turn as sent" design: the
  scripted replies carry no citation marker, so the stored reply equals the sent one;
* ``no_tools`` — the turn as sent without its tool exchanges: the ``<turn_context>`` user message,
  the memory exchange and the reply;
* ``preserve`` — ``control`` with ``preserve_thinking`` true in the request's
  ``chat_template_kwargs``. The production server sets it false, so the template drops the
  ``<think>`` block of every assistant message before the last user message, and a stored turn
  renders differently on the next turn.

For each follow-up turn (2 to 7), the report gives ``prompt_n`` and ``cache_n`` of the turn's first
call, and the tokens of the content that is new in that turn: the ``<turn_context>`` user message
plus the memory result. Chat-template wrappers are not in that count, so they fall into the
control's boundary cost (``control`` ``prompt_n`` minus the new tokens). The server's ``/tokenize``
answers 403 behind the slm_server router, so ``count`` uses the method that master approved for
FRE-1592 M1 (``scripts.eval.fre1592.inventory.Counter``): a user message's ``prompt_tokens`` minus
an empty user message's.

The base request is a call-1 capture of the FRE-1603 routing run (current ``main`` head, 23
tools), with the production head of ``scripts/eval/fre1592/channels.Head``.

Run from the repo root (llama.cpp must serve the model; ``count`` and ``run`` need the window):

    uv run python -m scripts.eval.fre1600.history run    --run-dir $RUN --capture <c.json> --dry-run
    uv run python -m scripts.eval.fre1600.history run    --run-dir $RUN-trace --capture <c.json>
        --arms control preserve --turns 2 --draws 1   (one line: the trace, 6 calls)
    uv run python -m scripts.eval.fre1600.history count  --run-dir $RUN
    uv run python -m scripts.eval.fre1600.history run    --run-dir $RUN --capture <c.json> [--draws N]
    uv run python -m scripts.eval.fre1600.history report --run-dir $RUN
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from scripts.eval.fre1537.replay import with_nonce
from scripts.eval.fre1592.channels import Head, append_row, call, done, fence, request
from scripts.eval.fre1592.inventory import Counter

from personal_agent.orchestrator.untrusted_channel import MEMORY_RECALL_TOOL, harness_tool_exchange

SESSION = Path(__file__).with_name("session.yaml")
ARMS = ("today", "control", "no_tools", "preserve")
PROBE_MAX_TOKENS = 16
# The production server's kwargs (``--chat-template-kwargs``) with preserve_thinking flipped.
PRESERVE_KWARGS = {"enable_thinking": True, "preserve_thinking": True, "reasoning_effort": "medium"}
# FRE-1592 F2: the production prefill rate on Flash-Next, for the dry-run time projection.
MS_PER_TOKEN = 2.6
CHARS_PER_TOKEN = 3.5

Message = dict[str, object]


@dataclass(frozen=True)
class ToolStep:
    """The scripted web_search exchange of a tool turn.

    Attributes:
        query: The search query.
        reasoning: The thinking stored on the assistant tool-call message.
        marker: The citation identifier of the result.
        results: Items with ``title``, ``url`` and ``content``.
    """

    query: str
    reasoning: str
    marker: str
    results: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class Turn:
    """One scripted turn.

    Attributes:
        index: The turn number, from 1.
        user: The user's own words.
        memory: The memory_recall result.
        reply: The final answer, as the service stores it.
        tool: The web_search exchange, or None for a plain turn.
    """

    index: int
    user: str
    memory: str
    reply: str
    tool: ToolStep | None


def load_session(path: Path = SESSION) -> list[Turn]:
    """Load the scripted session.

    Args:
        path: The YAML file.

    Returns:
        The turns, in order.
    """
    raw = yaml.safe_load(path.read_text())["turns"]
    turns: list[Turn] = []
    for i, t in enumerate(raw, start=1):
        tool = t.get("tool")
        turns.append(
            Turn(
                index=i,
                user=t["user"].strip(),
                memory=t["memory"].strip(),
                reply=t["reply"].strip(),
                tool=ToolStep(
                    query=tool["query"],
                    reasoning=tool["reasoning"].strip(),
                    marker=tool["marker"],
                    results=tuple(tool["results"]),
                )
                if tool
                else None,
            )
        )
    return turns


# --------------------------------------------------------------------------- message builders


def first_messages(turn: Turn) -> list[Message]:
    """Return what a turn's first call adds after the history, as production sends it.

    Args:
        turn: The turn.

    Returns:
        The ``<turn_context>`` user message, then the memory_recall call and its result.
    """
    call_msg, result = harness_tool_exchange(
        call_id=f"call_mem_t{turn.index}", tool_name=MEMORY_RECALL_TOOL, content=turn.memory
    )
    return [{"role": "user", "content": fence(turn.user)}, call_msg, result]


def tool_messages(turn: Turn) -> list[Message]:
    """Return a tool turn's web_search call and its result; empty for a plain turn.

    Args:
        turn: The turn.

    Returns:
        The assistant tool-call message (with its reasoning) and the tool result.
    """
    if turn.tool is None:
        return []
    call_id = f"call_ws_t{turn.index}"
    result = {
        "query": turn.tool.query,
        "results": [dict(r) for r in turn.tool.results],
        "_citation": f"[{turn.tool.marker}]",
    }
    return [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": turn.tool.reasoning,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": turn.tool.query}),
                    },
                }
            ],
        },
        {
            "tool_call_id": call_id,
            "role": "tool",
            "name": "web_search",
            "content": json.dumps(result),
        },
    ]


def reply_message(turn: Turn) -> Message:
    """Return the turn's final answer as an assistant message.

    Args:
        turn: The turn.

    Returns:
        The message.
    """
    return {"role": "assistant", "content": turn.reply}


def stored_turn(arm: str, turn: Turn) -> list[Message]:
    """Return what the arm keeps of a finished turn for the next turn's history.

    Args:
        arm: One of :data:`ARMS`.
        turn: The finished turn.

    Returns:
        The messages appended to the history.

    Raises:
        ValueError: For an unknown arm.
    """
    match arm:
        case "today":
            return [{"role": "user", "content": turn.user}, reply_message(turn)]
        case "control" | "preserve":
            return [*first_messages(turn), *tool_messages(turn), reply_message(turn)]
        case "no_tools":
            return [*first_messages(turn), reply_message(turn)]
    raise ValueError(f"unknown arm {arm!r}")


def turn_calls(history: Sequence[Message], turn: Turn) -> list[list[Message]]:
    """Return the message lists (after the system message) of a turn's calls.

    Args:
        history: The stored history before the turn.
        turn: The turn.

    Returns:
        The first call, then the second call of a tool turn.
    """
    first = [*history, *first_messages(turn)]
    calls = [first]
    if turn.tool is not None:
        calls.append([*first, *tool_messages(turn)])
    return calls


def session_calls(arm: str, turns: Sequence[Turn]) -> list[tuple[int, int, list[Message]]]:
    """Return every call of one arm's session.

    Args:
        arm: One of :data:`ARMS`.
        turns: The scripted session.

    Returns:
        ``(turn, call, messages)`` for each call, call numbered from 1.
    """
    history: list[Message] = []
    out: list[tuple[int, int, list[Message]]] = []
    for turn in turns:
        for n, messages in enumerate(turn_calls(history, turn), start=1):
            out.append((turn.index, n, messages))
        history = [*history, *stored_turn(arm, turn)]
    return out


# --------------------------------------------------------------------------- count


def count(args: argparse.Namespace) -> None:
    """Count the tokens of each turn's new content and of its other parts.

    Args:
        args: The parsed command line.
    """
    turns = load_session()
    counter = Counter(args.url, args.llama_model, args.run_dir / "counts-store.json")
    rows = []
    for turn in turns:
        tool = tool_messages(turn)
        rows.append(
            {
                "turn": turn.index,
                "user_fenced": counter.text(fence(turn.user)),
                "memory": counter.text(turn.memory),
                "tool_result": counter.text(str(tool[1]["content"])) if tool else 0,
                "tool_reasoning": counter.text(turn.tool.reasoning) if turn.tool else 0,
                "reply": counter.text(turn.reply),
            }
        )
    out = args.run_dir / "counts.json"
    out.write_text(json.dumps(rows, indent=2))
    print(f"wrote {out} ({counter.calls} calls)")


# --------------------------------------------------------------------------- run


def run(args: argparse.Namespace) -> None:
    """Send every arm's session, once per draw.

    Args:
        args: The parsed command line.
    """
    turns = load_session()[: args.turns]
    capture = json.loads(args.capture.read_text())
    base = capture.get("first", capture)
    head = Head(str(base["messages"][0]["content"]))
    out = args.run_dir / "rows.jsonl"
    seen = done(out, lambda r: (r["arm"], r["draw"]))
    arms = [a for a in ARMS if a in args.arms]
    total_tokens = 0
    n_calls = 0
    client = None if args.dry_run else httpx.Client(timeout=900.0)
    for draw in range(args.draws):
        for arm in arms:
            if (arm, draw) in seen:
                continue
            nonce = f"fre1600-{arm}-{draw}-{uuid.uuid4().hex[:8]}"
            over: dict[str, object] = {"max_tokens": PROBE_MAX_TOKENS}
            if arm == "preserve":
                over["chat_template_kwargs"] = PRESERVE_KWARGS
            rows: list[dict[str, object]] = []
            for turn_no, call_no, messages in session_calls(arm, turns):
                body = with_nonce(
                    request(base, args.llama_model, head.production, messages, **over), nonce
                )
                n_calls += 1
                if client is None:
                    est = int(len(json.dumps(body)) / CHARS_PER_TOKEN)
                    total_tokens += est
                    print(f"{arm} d{draw} t{turn_no}c{call_no}: {len(messages)} msgs, ~{est:,} tok")
                    continue
                result = call(client, args.url, body)
                result.pop("content")
                result.pop("message")
                rows.append(
                    {
                        "arm": arm,
                        "draw": draw,
                        "turn": turn_no,
                        "call": call_no,
                        "nonce": nonce,
                        **result,
                    }
                )
                print(f"{arm} d{draw} t{turn_no}c{call_no}: {result['timings']}", flush=True)
            # Written only when the arm's session is complete, so a resumed run repeats no call.
            for row in rows:
                append_row(out, row)
    if client is None:
        cold_s = total_tokens * MS_PER_TOKEN / 1000
        print(
            f"\n{n_calls} calls. Upper bound if no call reused any cache: "
            f"~{total_tokens:,} prompt tokens, ~{cold_s / 60:.0f} min of prefill."
        )


# --------------------------------------------------------------------------- report


def report(args: argparse.Namespace) -> dict[str, object]:
    """Build the per-turn report from the rows and the counts.

    Args:
        args: The parsed command line.

    Returns:
        The report, also written to ``report.json`` and printed as a Markdown table.
    """
    counts = {c["turn"]: c for c in json.loads((args.run_dir / "counts.json").read_text())}
    rows = [
        json.loads(line)
        for line in (args.run_dir / "rows.jsonl").read_text().splitlines()
        if line.strip()
    ]
    first = {
        (r["arm"], r["draw"], r["turn"]): r["timings"]
        for r in rows
        if r["call"] == 1 and isinstance(r.get("timings"), Mapping)
    }
    draws = sorted({r["draw"] for r in rows})
    turns = sorted({t for (_, _, t) in first if t > 1})
    table: list[dict[str, object]] = []
    for turn in turns:
        new = counts[turn]["user_fenced"] + counts[turn]["memory"]
        entry: dict[str, object] = {"turn": turn, "new_tokens": new}
        for arm in ARMS:
            vals = [first[(arm, d, turn)]["prompt_n"] for d in draws if (arm, d, turn) in first]
            entry[f"{arm}_prompt_n"] = vals
        control = entry["control_prompt_n"]
        if isinstance(control, list) and control:
            boundary = int(statistics.median(control)) - new
            entry["boundary"] = boundary
            entry["bound"] = new + boundary
        table.append(entry)
    result: dict[str, object] = {"draws": draws, "turns": table}
    (args.run_dir / "report.json").write_text(json.dumps(result, indent=2))
    header = "| Turn | New tokens | " + " | ".join(f"{a} prompt_n" for a in ARMS) + " | Boundary |"
    print(header)
    print("|" + "---|" * (len(ARMS) + 3))
    for e in table:
        cells = " | ".join(
            "/".join(str(v) for v in e[f"{a}_prompt_n"])  # type: ignore[attr-defined]
            for a in ARMS
        )
        print(f"| {e['turn']} | {e['new_tokens']} | {cells} | {e.get('boundary', '—')} |")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run the subcommand.

    Args:
        argv: The command line; ``sys.argv`` when None.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("count", "run"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        p.add_argument("--url", default="http://127.0.0.1:8600/v1/chat/completions")
        p.add_argument("--llama-model", default="unsloth/qwen3.8-flash-next")
    run_p = sub.choices["run"]
    run_p.add_argument("--capture", type=Path, required=True)
    run_p.add_argument("--draws", type=int, default=2)
    run_p.add_argument("--arms", nargs="+", default=list(ARMS), choices=ARMS)
    run_p.add_argument("--turns", type=int, default=None, help="only the first N turns (trace)")
    run_p.add_argument("--dry-run", action="store_true", help="list the calls, send nothing")
    rep = sub.add_parser("report")
    rep.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    {"count": count, "run": run, "report": report}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
