"""FRE-1498 planner-only probe — the planner's CHOICE, with nobody dispatched.

One planner call per (fixture, prompt variant, trial): the app's own planner system prompt
(with regime D's decline rule), the app's own user message shape, the raw JSON plan back.
Records decline/expand, task count, types, thoroughness, goals. No worker runs.

Committed under FRE-1503 (was left in the explore seat's scratchpad per its own commit-only-
the-document convention). Run from the repo root with the project installed (`uv sync`) —
`personal_agent` resolves without any `sys.path` manipulation, same as `runner.py`:

    uv run python scripts/eval/fre1498/planner_probe.py --backend local|ovh --run-id <id> [--trials 2]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from personal_agent.orchestrator.expansion_controller import _build_planner_system_prompt

HERE = Path(__file__).parent
GRANTED_SUB_AGENT_TOOLS = ["run_python", "web_search", "search_memory", "recall_personal_history"]

DECLINE_RULE = (
    "- First decide whether this query needs independent sub-tasks at all. If one "
    "assistant working alone, with the same tools, would answer it well in one pass "
    "— a greeting, a short factual question, a single lookup, a follow-up — output "
    '{"strategy": "SINGLE", "tasks": []} and nothing else. Choose HYBRID or '
    "DECOMPOSE only when splitting the work into independent sub-tasks would "
    "produce a better answer than one pass\n"
)

BACKENDS = {
    "local": {
        "url": "http://localhost:8600/v1/chat/completions",
        "model": "unsloth/qwen3.8-flash-next",
        "headers": {},
        "extra": {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "chat_template_kwargs": {"enable_thinking": True},
        },
    },
    "ovh": {
        "url": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
        "model": "Qwen3.8-27B",
        "headers": {
            "Authorization": "Bearer " + os.environ.get("AGENT_MANAGED_EMBEDDING_TOKEN", "")
        },
        "extra": {"temperature": 1.0, "top_p": 0.95},
    },
}


def variants() -> dict[str, str]:
    base = _build_planner_system_prompt(GRANTED_SUB_AGENT_TOOLS)
    assert "Rules:\n" in base
    base_decline = base.replace(
        '"strategy": "HYBRID|DECOMPOSE"', '"strategy": "SINGLE|HYBRID|DECOMPOSE"', 1
    ).replace("Rules:\n", "Rules:\n" + DECLINE_RULE, 1)
    # V_norounds: the thoroughness line without its round counts — the time proxy removed.
    no_rounds = re.sub(r" \(\d+ tool round\(s\)\)", "", base_decline)
    fast = base_decline + (
        "\n- The user has said they want this answered within a couple of minutes. Plan accordingly."
    )
    slow = base_decline + (
        "\n- The user has said to take the time needed for accuracy and quality; they do not mind waiting."
    )
    return {"current": base_decline, "no_rounds": no_rounds, "fast": fast, "slow": slow}


def parse_plan(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, flags=re.S)
        if not m:
            return {"parse_error": True, "raw": text[:400]}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {"parse_error": True, "raw": text[:400]}
    tasks = data.get("tasks") if isinstance(data, dict) else None
    tasks = tasks if isinstance(tasks, list) else []
    return {
        "parse_error": False,
        "strategy": data.get("strategy") if isinstance(data, dict) else None,
        "declined": (data.get("strategy") == "SINGLE" and not tasks)
        if isinstance(data, dict)
        else None,
        "task_count": len(tasks),
        "types": [t.get("type") for t in tasks if isinstance(t, dict)],
        "thoroughness": [t.get("thoroughness") for t in tasks if isinstance(t, dict)],
        "goals": [(t.get("goal") or "")[:200] for t in tasks if isinstance(t, dict)],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", required=True, choices=list(BACKENDS))
    p.add_argument("--run-id", required=True)
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--deadline", default="")
    args = p.parse_args()
    b = BACKENDS[args.backend]
    fixtures = yaml.safe_load((HERE / "fixtures.yaml").read_text())["fixtures"]
    out = HERE / "out" / f"planner-probe.{args.run_id}.{args.backend}.jsonl"
    done = set()
    if out.exists():
        done = {
            (r["label"], r["variant"], r["trial"])
            for r in map(json.loads, out.read_text().splitlines())
            if r
        }
    vs = variants()
    with httpx.Client(timeout=240.0) as client:
        for trial in range(args.trials):
            for fx in fixtures:
                for vname, system in vs.items():
                    if (fx["label"], vname, trial) in done:
                        continue
                    if args.deadline and datetime.now(UTC) >= datetime.fromisoformat(args.deadline):
                        print("deadline reached", file=sys.stderr)
                        return 4
                    body = {
                        "model": b["model"],
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": f"Strategy: HYBRID\nQuery: {fx['message']}\n\nProduce the JSON plan.",
                            },
                        ],
                        "max_tokens": 4096,
                        **b["extra"],
                    }
                    t0 = time.monotonic()
                    try:
                        r = client.post(b["url"], json=body, headers=b["headers"])
                        secs = round(time.monotonic() - t0, 1)
                        r.raise_for_status()
                        j = r.json()
                        content = j["choices"][0]["message"]["content"] or ""
                        usage = j.get("usage") or {}
                        row = {
                            "run_id": args.run_id,
                            "backend": args.backend,
                            "model": b["model"],
                            "label": fx["label"],
                            "group": fx.get("group"),
                            "pair": fx.get("pair"),
                            "variant": vname,
                            "trial": trial,
                            "secs": secs,
                            "usage": usage,
                            "finish_reason": j["choices"][0].get("finish_reason"),
                            "plan": parse_plan(content),
                            "content_head": content[:300],
                        }
                    except Exception as exc:  # noqa: BLE001
                        row = {
                            "run_id": args.run_id,
                            "backend": args.backend,
                            "model": b["model"],
                            "label": fx["label"],
                            "variant": vname,
                            "trial": trial,
                            "secs": round(time.monotonic() - t0, 1),
                            "error": repr(exc)[:300],
                        }
                    out.open("a").write(json.dumps(row) + "\n")
                    pl = row.get("plan") or {}
                    if "error" in row:
                        summary = "ERR " + row["error"][:60]
                    elif pl.get("declined"):
                        summary = "declined"
                    else:
                        summary = (
                            f"{pl.get('strategy')}/{pl.get('task_count')} {pl.get('thoroughness')}"
                        )
                    print(
                        f"{args.backend} t{trial} {fx['label']:24s} {vname:9s} {row['secs']:6.1f}s {summary}"
                    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
