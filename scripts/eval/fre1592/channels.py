"""FRE-1592 M2 and M3 — what each delivery channel costs in cache, and how well a rule holds in it.

Both parts replay a production primary request directly to llama.cpp (the ADR-0154 D7 probe
pattern): no gateway turn, so no graph, consolidation or cloud call. The base request is a
capture of ``scripts/eval/fre1537/capture.py`` with the production head of
``scripts/eval/fre1592/inventory.py`` (deployment context and operator stanza added).

**M2 (``cache``)** delivers one skill body five ways and reads llama.cpp ``timings``
(``cache_n``, ``prompt_n``, ``prompt_ms``, ``predicted_ms``). Each arm has its own nonce, so no
arm reuses another's cache. Each arm sends turn 1 (cold), then turn 2 (measured):

* ``base`` — no body anywhere: the template's own boundary cost;
* ``head`` — the body in the system prompt on both turns;
* ``fence`` — the body in turn 2's ``<turn_context>`` block;
* ``skill`` — turn 2 as sent, then a ``read_skill`` exchange that returns the body, then one more
  call (the cost of the round trip);
* ``head_turn2`` — the body added to the system prompt on turn 2 only (negative control).

**M3 (``follow``)** places one rule in each channel and scores the reply. The rules are not in the
production prompt: a rule that the model follows without being told cannot show a channel effect
(the FRE-1592 preflight found that for the grounding and parallel-calls rules).

* ``a`` — :data:`RULE_A`: the final answer ends with a ``Sources:`` line of citation markers. The
  turn holds two search results. A further tool call gets "No further results" for up to
  :data:`EXTRA_ROUNDS` more rounds, so the final answer is scored;
* ``b`` — :data:`RULE_B`: the first response calls ``search_memory`` and ``web_search`` together.

Arms: ``system`` (after the reasoning rule), ``heavy`` (the same, plus every SKILL.md body in the head),
``skill`` (a ``read_skill`` result), ``fence`` (the ``<turn_context>`` block), ``absent``.

Run from the repo root (llama.cpp must serve the model of ``--model-key``):

    uv run python -m scripts.eval.fre1592.channels cache  --run-dir $RUN --model-key K --llama-model M --capture <body.json>
    uv run python -m scripts.eval.fre1592.channels follow --run-dir $RUN --model-key K --llama-model M --capture <body.json> [--limit N]
    uv run python -m scripts.eval.fre1592.channels report --run-dir $RUN
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from scripts.eval.fre1537.replay import with_nonce
from scripts.eval.fre1590.triggering import replay_request
from scripts.eval.fre1592.inventory import _check_deployment_context, _components, _system

from personal_agent.orchestrator.prompts import render_current_datetime_block
from personal_agent.orchestrator.skills import get_all_skills
from personal_agent.orchestrator.untrusted_channel import MEMORY_RECALL_TOOL, harness_tool_exchange

ITEMS = Path(__file__).with_name("items.yaml")
# A fixed instant, so the date block is the same bytes in every request.
INSTANT = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
M3_ARMS = ("system", "heavy", "skill", "fence", "absent")
M2_ARMS = ("base", "head", "fence", "skill", "head_turn2")
PROBE_MAX_TOKENS = 16
EXTRA_ROUNDS = 2
RULE_A = (
    "## Answer format\n"
    "When your answer uses a search result, end it with one final line that starts with "
    "`Sources:` and lists every citation marker you used, separated by commas."
)
RULE_B = (
    "## Memory first\n"
    "Before you call web_search for a fact, call search_memory with the same query in the same "
    "response, so that both calls run together."
)
RULES = {"a": RULE_A, "b": RULE_B}
SKILL_CALL_MAX_TOKENS = 1024

Message = dict[str, object]
Body = dict[str, object]


# --------------------------------------------------------------------------- message builders


def fence(user_text: str, extra: str = "") -> str:
    """Return a user message as production inlines it: the turn_context block, then the text.

    Args:
        user_text: The user's own words.
        extra: Content placed in the block before the date; empty for none.

    Returns:
        The message text.
    """
    block = "\n\n".join(p for p in (extra, render_current_datetime_block(INSTANT)) if p)
    return f"<turn_context>\n{block}\n</turn_context>\n\n{user_text}"


def memory_exchange() -> list[Message]:
    """Return the memory_recall exchange that every production turn carries.

    Returns:
        The assistant call and its tool result.
    """
    call, result = harness_tool_exchange(
        call_id="call_mem_fre1592", tool_name=MEMORY_RECALL_TOOL, content="Memory: no records."
    )
    return [call, result]


def skill_exchange(name: str, body: str) -> list[Message]:
    """Return a read_skill call and the result that read_skill_executor returns for it.

    Args:
        name: The skill name.
        body: The text that the result carries as ``body``.

    Returns:
        The assistant call and its tool result.
    """
    call_id = "call_skill_fre1592"
    result = {"skill_name": name, "base_dir": f"skills/{name}", "body": body, "files": []}
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_skill", "arguments": json.dumps({"name": name})},
                }
            ],
        },
        {
            "tool_call_id": call_id,
            "role": "tool",
            "name": "read_skill",
            "content": json.dumps(result),
        },
    ]


def search_exchange(query: str, sources: Sequence[Mapping[str, str]]) -> list[Message]:
    """Return one web_search call per source, each result carrying its citation marker.

    Args:
        query: The search query of every call.
        sources: Items with ``marker``, ``title``, ``url`` and ``text``.

    Returns:
        A call and its result for each source, in order.
    """
    out: list[Message] = []
    for i, src in enumerate(sources, start=1):
        call_id = f"call_ws_{i}"
        result = {
            "query": query,
            "results": [{"title": src["title"], "url": src["url"], "content": src["text"]}],
            "_citation": f"[{src['marker']}]",
        }
        out += [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": query}),
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
    return out


# --------------------------------------------------------------------------- production head


class Head:
    """The production system prompt and its variants, rebuilt from one capture."""

    def __init__(self, captured_system: str) -> None:
        """Split and check the captured system prompt."""
        _check_deployment_context()
        self.parts = _components(captured_system)
        self.production = _system(self.parts)
        skills = get_all_skills()
        self.all_bodies = "\n\n".join(skills[name].body for name in sorted(skills))

    def with_rule(self, rule: str) -> str:
        """Return the production system prompt with one rule after the reasoning rule.

        Args:
            rule: ``"a"`` or ``"b"``.

        Returns:
            The system prompt.
        """
        reasoning = self.parts["reasoning_discipline"].rstrip("\n")
        return _system({**self.parts, "reasoning_discipline": f"{reasoning}\n\n{RULES[rule]}\n"})


# --------------------------------------------------------------------------- llama.cpp


def call(client: httpx.Client, url: str, request: Mapping[str, object]) -> dict[str, object]:
    """Send one non-streamed request; return the message, the timings and the wall time.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        request: The request body.

    Returns:
        The row fields, plus ``message`` (the assistant message, for a further round).

    Raises:
        httpx.HTTPError: On a transport error or a non-2xx reply.
    """
    t0 = time.monotonic()
    response = client.post(url, json=request)
    response.raise_for_status()
    data = response.json()
    message = data["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    return {
        "wall_s": round(time.monotonic() - t0, 2),
        "timings": data.get("timings"),
        "usage": data.get("usage"),
        "finish_reason": data["choices"][0].get("finish_reason"),
        "content": message.get("content") or "",
        "reasoning_chars": len(str(message.get("reasoning_content") or "")),
        "tool_names": [c["function"]["name"] for c in calls if isinstance(c, Mapping)],
        "system_fingerprint": data.get("system_fingerprint"),
        "message": {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        },
    }


def request(
    base: Mapping[str, object],
    llama_model: str,
    system: str,
    rest: Sequence[Message],
    **over: object,
) -> Body:
    """Build a llama.cpp request from the captured body with new messages.

    Args:
        base: The captured primary request body (sampling and tools).
        llama_model: The llama.cpp model name.
        system: The system prompt.
        rest: The messages after the system message.
        **over: Request keys to override, such as ``max_tokens``.

    Returns:
        The request body, not streamed.
    """
    body = replay_request(
        {**base, "messages": [{"role": "system", "content": system}, *rest]}, llama_model
    )
    body.update(over)
    return body


def append_row(path: Path, row: Mapping[str, object]) -> None:
    """Append one JSON row.

    Args:
        path: The JSONL file; its directory is created when missing.
        row: The row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def done(
    path: Path, key: Callable[[Mapping[str, object]], tuple[object, ...]]
) -> set[tuple[object, ...]]:
    """Return the keys of the rows already written.

    Args:
        path: The JSONL file.
        key: Maps a row to its key.

    Returns:
        The keys; empty when the file does not exist.
    """
    if not path.exists():
        return set()
    return {key(json.loads(line)) for line in path.read_text().splitlines() if line.strip()}


# --------------------------------------------------------------------------- M2


def run_cache(args: argparse.Namespace, base: Mapping[str, object], head: Head) -> None:
    """Run M2: five deliveries of one skill body, turn 1 cold and turn 2 measured.

    Args:
        args: The parsed command line.
        base: The captured primary request body.
        head: The production system prompt.
    """
    spec = yaml.safe_load(ITEMS.read_text())["m2"]
    body = get_all_skills()[spec["skill"]].body  # what read_skill returns: no frontmatter
    out = args.run_dir / "rows" / args.model_key / "m2.jsonl"
    seen = done(out, lambda r: (r["arm"], r["draw"], r["step"]))
    client = httpx.Client(timeout=900.0)
    user1 = fence(spec["turn1"])
    reply1: Message = {"role": "assistant", "content": spec["turn1_reply"]}
    probe = {"max_tokens": PROBE_MAX_TOKENS}
    for draw in range(args.draws):
        for arm in M2_ARMS:
            if (arm, draw, "turn2") in seen:
                continue
            nonce = f"fre1592-m2-{arm}-{draw}-{uuid.uuid4().hex[:8]}"
            sys1 = head.production + (f"\n\n{body}" if arm == "head" else "")
            sys2 = head.production + (f"\n\n{body}" if arm in ("head", "head_turn2") else "")
            user2 = fence(spec["turn2"], body if arm == "fence" else "")
            steps: list[tuple[str, Body]] = [
                (
                    "turn1",
                    request(
                        base, args.llama_model, sys1, [{"role": "user", "content": user1}], **probe
                    ),
                ),
            ]
            turn2_rest = [
                {"role": "user", "content": user1},
                reply1,
                {"role": "user", "content": user2},
            ]
            if arm == "skill":
                steps.append(
                    (
                        "skill_call",
                        request(
                            base,
                            args.llama_model,
                            sys2,
                            turn2_rest,
                            max_tokens=SKILL_CALL_MAX_TOKENS,
                        ),
                    )
                )
                turn2_rest = [*turn2_rest, *skill_exchange(spec["skill"], body)]
            steps.append(("turn2", request(base, args.llama_model, sys2, turn2_rest, **probe)))
            rows: list[dict[str, object]] = []
            for step, req in steps:
                result = call(client, args.url, with_nonce(req, nonce))
                result.pop("content")
                result.pop("message")
                rows.append({"arm": arm, "draw": draw, "step": step, "nonce": nonce, **result})
                print(f"m2 {arm} d{draw} {step}: {result['timings']}", flush=True)
            # Written only when the arm is complete, so a resumed run never repeats a step.
            for row in rows:
                append_row(out, row)


# --------------------------------------------------------------------------- M3


def m3_request(
    base: Mapping[str, object],
    llama_model: str,
    head: Head,
    arm: str,
    rule: str,
    item: Mapping[str, object],
) -> Body:
    """Build the request of one M3 item in one arm.

    Args:
        base: The captured primary request body.
        llama_model: The llama.cpp model name.
        head: The production system prompt.
        arm: One of :data:`M3_ARMS`.
        rule: ``"a"`` or ``"b"``.
        item: The item from ``items.yaml``.

    Returns:
        The request body.
    """
    question = str(item["question"] if rule == "a" else item["prompt"])
    text = RULES[rule]
    system = {
        "system": head.with_rule(rule),
        "heavy": f"{head.with_rule(rule)}\n\n{head.all_bodies}",
    }.get(arm, head.production)
    user = fence(question, text if arm == "fence" else "")
    rest: list[Message] = [{"role": "user", "content": user}, *memory_exchange()]
    if arm == "skill":
        rest += skill_exchange("answer-format" if rule == "a" else "memory-first", text)
    if rule == "a":
        rest += search_exchange(str(item["query"]), item["sources"])  # type: ignore[arg-type]
    return request(base, llama_model, system, rest)


def score(rule: str, item: Mapping[str, object], result: Mapping[str, object]) -> dict[str, object]:
    """Score one reply against its rule.

    Args:
        rule: ``"a"`` or ``"b"``.
        item: The item from ``items.yaml``.
        result: The row fields of the scored call.

    Returns:
        ``pass`` and ``any`` (the rule partly met), plus ``answered`` for rule a (a reply that
        stopped normally; a truncated or tool-calling reply fails) or ``calls`` for rule b.
    """
    if rule == "a":
        markers = [s["marker"] for s in item["sources"]]  # type: ignore[union-attr]
        lines = [ln.strip().strip("*_ ").lower() for ln in str(result["content"]).splitlines()]
        lines = [ln for ln in lines if ln]
        last = lines[-1] if lines else ""
        listed = last.startswith("sources") and any(m.lower() in last for m in markers)
        answered = result["finish_reason"] == "stop"
        return {
            "answered": answered,
            "pass": answered and listed,
            "any": any(ln.startswith("sources") for ln in lines),
        }
    names = list(result["tool_names"])  # type: ignore[call-overload]
    return {
        "calls": names,
        "pass": "search_memory" in names and "web_search" in names,
        "any": "search_memory" in names,
    }


def answer(client: httpx.Client, url: str, req: Body, nonce: str) -> tuple[dict[str, object], int]:
    """Call until the model answers: each further tool call gets "No further results".

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        req: The request body, without the nonce.
        nonce: The arm's nonce.

    Returns:
        The last call's row fields and the number of further rounds.
    """
    result = call(client, url, with_nonce(req, nonce))
    rounds = 0
    while result["finish_reason"] == "tool_calls" and rounds < EXTRA_ROUNDS:
        message = result["message"]
        calls = message["tool_calls"]  # type: ignore[index]
        results = [
            {
                "tool_call_id": c["id"],
                "role": "tool",
                "name": c["function"]["name"],
                "content": "No further results for this query.",
            }
            for c in calls  # type: ignore[union-attr]
        ]
        req = {**req, "messages": [*req["messages"], message, *results]}  # type: ignore[misc]
        result = call(client, url, with_nonce(req, nonce))
        rounds += 1
    return result, rounds


def run_follow(args: argparse.Namespace, base: Mapping[str, object], head: Head) -> None:
    """Run M3: each rule in each arm, every item, ``--draws`` times, arm by arm.

    Args:
        args: The parsed command line.
        base: The captured primary request body.
        head: The production system prompt.
    """
    items = yaml.safe_load(ITEMS.read_text())
    out = args.run_dir / "rows" / args.model_key / "m3.jsonl"
    seen = done(out, lambda r: (r["rule"], r["arm"], r["item"], r["draw"]))
    client = httpx.Client(timeout=900.0)
    for rule in ("a", "b"):
        for arm in M3_ARMS:
            nonce = f"fre1592-m3-{rule}-{arm}-{uuid.uuid4().hex[:8]}"
            for item in items[rule][: args.limit]:
                for draw in range(args.draws):
                    if (rule, arm, item["id"], draw) in seen:
                        continue
                    req = m3_request(base, args.llama_model, head, arm, rule, item)
                    if rule == "a":
                        result, rounds = answer(client, args.url, req, nonce)
                    else:
                        result, rounds = call(client, args.url, with_nonce(req, nonce)), 0
                    result.pop("message")
                    verdict = score(rule, item, result)
                    append_row(
                        out,
                        {
                            "rule": rule,
                            "arm": arm,
                            "item": item["id"],
                            "draw": draw,
                            "extra_rounds": rounds,
                            **result,
                            **verdict,
                        },
                    )
                    print(
                        f"m3 {rule} {arm} {item['id']} d{draw}: {verdict} {result['wall_s']}s",
                        flush=True,
                    )


# --------------------------------------------------------------------------- report


def _t(row: Mapping[str, object], key: str) -> float:
    """Return one llama.cpp timing of a row, 0 when it is missing."""
    timings = row.get("timings") or {}
    return float(timings.get(key, 0) or 0)  # type: ignore[union-attr]


def report(run_dir: Path) -> str:
    """Render the M2 and M3 tables of every model in a run directory.

    Args:
        run_dir: The run directory.

    Returns:
        Markdown tables: the median of each M2 step, and the M3 counts per rule and arm.
    """
    lines: list[str] = []
    for model_dir in sorted((run_dir / "rows").iterdir()):
        m2 = model_dir / "m2.jsonl"
        if m2.exists():
            rows = [json.loads(x) for x in m2.read_text().splitlines() if x.strip()]
            by: dict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
            for r in rows:
                by[(r["arm"], r["step"])].append(r)
            lines += [
                f"\n### M2 — {model_dir.name}\n",
                "| Arm | Step | n | cache_n | prompt_n | prompt_ms | predicted_ms |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
            for arm in M2_ARMS:
                for step in ("turn1", "skill_call", "turn2"):
                    rs = by.get((arm, step))
                    if not rs:
                        continue
                    med = {
                        k: sorted(_t(r, k) for r in rs)[len(rs) // 2]
                        for k in ("cache_n", "prompt_n", "prompt_ms", "predicted_ms")
                    }
                    lines.append(
                        f"| {arm} | {step} | {len(rs)} | {med['cache_n']:.0f} | {med['prompt_n']:.0f} "
                        f"| {med['prompt_ms']:.0f} | {med['predicted_ms']:.0f} |"
                    )
        m3 = model_dir / "m3.jsonl"
        if m3.exists():
            rows = [json.loads(x) for x in m3.read_text().splitlines() if x.strip()]
            lines += [
                f"\n### M3 — {model_dir.name}\n",
                "| Rule | Arm | n | answered | pass | partial or better | median wall s |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
            for rule in ("a", "b"):
                for arm in M3_ARMS:
                    rs = [r for r in rows if r["rule"] == rule and r["arm"] == arm]
                    if rs:
                        walls = sorted(float(r["wall_s"]) for r in rs)
                        lines.append(
                            f"| {rule} | {arm} | {len(rs)} "
                            f"| {sum(r.get('answered', True) for r in rs)} "
                            f"| {sum(r['pass'] for r in rs)} "
                            f"| {sum(r['any'] for r in rs)} | {walls[len(walls) // 2]:.1f} |"
                        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand.

    Args:
        argv: The command line; ``sys.argv`` when None.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("cache", "follow"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        p.add_argument("--model-key", required=True)
        p.add_argument("--llama-model", required=True)
        p.add_argument("--capture", type=Path, required=True)
        p.add_argument("--url", default="http://127.0.0.1:8600/v1/chat/completions")
        p.add_argument("--draws", type=int, default=3)
        p.add_argument("--limit", type=int, default=None)
    rp = sub.add_parser("report")
    rp.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "report":
        print(report(args.run_dir))
        return 0
    captured = json.loads(args.capture.read_text())
    base = captured.get("body", captured)
    head = Head(str(base["messages"][0]["content"]))
    (run_cache if args.cmd == "cache" else run_follow)(args, base, head)
    return 0


if __name__ == "__main__":
    sys.exit(main())
