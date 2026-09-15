"""FRE-1521 Phase A — planner-only probe on Flash-Next MTPLX, three prompt variants.

Owner direction 2026-09-15: "Run the planner test now", Flash-Next only. No gateway, no workers:
each call is the planner request alone, sent directly to the local engine.

V0 current: the deployed planner prompt; user message = Strategy + Query.
V1 context: V0 plus the conversation before the turn (the messages the primary saw).
V2 briefing: V1 plus rules (a) carry rules/facts, (b) end points, (c) worker capability,
             and (d) the identical "(20 tool round(s))" annotation removed.
Inputs: turns 2, 5 and 8 of production session ef9473fe. 3 draws per variant x turn.
"""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

HERE = Path(__file__).parent
URL = "http://localhost:8600/v1/chat/completions"
MODEL = "mtplx-qwen38-flash-next-optimized-speed"
TURN_INDEX = {2: 2, 5: 8, 8: 14}  # user message index in the stored session
DRAWS = 3

RULE_A = (
    "- Before writing tasks, list the user's standing rules and facts from the conversation that "
    "apply. Copy into each task's constraints every rule and fact that task depends on. The worker "
    "sees nothing else\n"
)
RULE_B = (
    "- Every task must state its end point: how many items, and what counts as done. Never ask for "
    "all or every\n"
)
RULE_C = (
    "- Workers are small models with thinking off and a limited number of tool rounds. Give each one "
    "a narrow, concrete task\n"
)


def prompts() -> dict[str, str]:
    text = (HERE / "deployed_prompt.txt").read_text()
    base = text.split("===PROMPT===\n", 1)[1]
    anchor = "- Do NOT answer the question"
    assert anchor in base and " (20 tool round(s))" in base
    v2 = base.replace(" (20 tool round(s))", "").replace(
        anchor, RULE_A + RULE_B + RULE_C + anchor, 1
    )
    return {"V0": base, "V1": base, "V2": v2}


def user_message(variant: str, history: list[dict], query: str) -> str:
    tail = f"Strategy: HYBRID\nQuery: {query}\n\nProduce the JSON plan."
    if variant == "V0":
        return tail
    convo = "\n\n".join(f"[{m['role']}]: {m.get('content') or ''}" for m in history)
    return f"Conversation so far:\n\n{convo}\n\n---\n\n{tail}"


def call(client: httpx.Client, system: str, user: str) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": 16384,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    t0 = time.monotonic()
    try:
        r = client.post(URL, json=body, timeout=1800.0)
        wall = round(time.monotonic() - t0, 1)
        r.raise_for_status()
        j = r.json()
        msg = j["choices"][0]["message"]
        return {
            "wall_s": wall,
            "finish_reason": j["choices"][0].get("finish_reason"),
            "usage": j.get("usage"),
            "content": msg.get("content") or "",
            "reasoning_chars": len(msg.get("reasoning_content") or ""),
        }
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return {"wall_s": round(time.monotonic() - t0, 1), "error": repr(exc)[:300]}


def main() -> int:
    session = json.loads((HERE / "session_ef9473fe.json").read_text())
    ps = prompts()
    out = HERE / "probe_rows.jsonl"
    done = set()
    if out.exists():
        done = {
            (r["variant"], r["turn"], r["draw"])
            for r in map(json.loads, out.read_text().splitlines())
        }
    with httpx.Client() as client:
        for turn, idx in TURN_INDEX.items():
            query = session[idx]["content"]
            history = session[:idx]
            for variant in ("V0", "V1", "V2"):
                for draw in range(1, DRAWS + 1):
                    if (variant, turn, draw) in done:
                        continue
                    user = user_message(variant, history, query)
                    res = call(client, ps[variant], user)
                    row = {
                        "at": datetime.now(UTC).isoformat(timespec="seconds"),
                        "variant": variant,
                        "turn": turn,
                        "draw": draw,
                        "user_chars": len(user),
                        **res,
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(row) + "\n")
                    print(
                        f"t{turn} {variant} d{draw}: wall={res.get('wall_s')}s finish={res.get('finish_reason')} "
                        f"prompt={(res.get('usage') or {}).get('prompt_tokens')} err={res.get('error')}",
                        flush=True,
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
