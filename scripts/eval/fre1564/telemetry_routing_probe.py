"""FRE-1564 AC-4 — does the planner send telemetry questions to `general`, not `researcher`?

Three telemetry questions go to the planner as the gateway will send them: the production
planner system prompt rendered with the live tool surface (plus the D7 decline rule), the
production user-message framing, the catalog's `planner` mode and the catalog's default
sampling. Each question runs ``--trials`` times. The probe records the plan of every call.

The probe passes when no call plans a `researcher` task and every plan parses. It does not
require `general` tasks: a plan that declines (the primary answers alone, with `bash`) is
not a failure of this criterion, and the report counts it so a reader can see it.

This calls the owner's llama.cpp server (about 15 planner calls). Ask master first. Run from
the repo root:

    uv run python -m scripts.eval.fre1564.telemetry_routing_probe --run-dir <dir> [--trials 5]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import yaml
from scripts.eval.fre1537 import llama, render

QUESTIONS: tuple[tuple[str, str], ...] = (
    ("logs_last_hour", "Check the agent logs and tell me which errors occurred in the last hour."),
    ("latency_24h", "Retrieve latency metrics for the last 24 hours."),
    ("backend_health", "Is the model server backend healthy right now? Check its recent probes."),
)
_SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")
_MAX_TOKENS = 4096


def catalog_sampling() -> dict[str, object]:
    """Return the default-mode sampling of the local deployment, as the gateway sends it."""
    entry = yaml.safe_load(llama.CATALOG.read_text())["models"][llama.CATALOG_DEPLOYMENT]
    default = entry["modes"][entry["default_mode"]]
    return {k: default[k] for k in _SAMPLING_KEYS if k in default}


def build_requests(trials: int, surface: Sequence[str]) -> list[dict[str, Any]]:
    """Build every planner request of the run.

    Args:
        trials: Calls per question.
        surface: The tool names grantable to a worker (the live surface).

    Returns:
        One record per call: ``label``, ``trial``, ``prompt_hash`` and the request ``body``.
    """
    system = render.render_system_prompt(surface)
    mode = llama.resolve_mode("planner")
    sampling = catalog_sampling()
    records = []
    for label, question in QUESTIONS:
        user = render.build_user_message("", None, question)
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "max_tokens": _MAX_TOKENS,
            **sampling,
            **mode.params,
        }
        for trial in range(trials):
            records.append(
                {
                    "label": label,
                    "trial": trial,
                    "prompt_hash": render.prompt_hash(system),
                    "body": body,
                }
            )
    return records


def classify(content: str) -> dict[str, Any]:
    """Read a planner reply.

    Args:
        content: The model's reply text.

    Returns:
        ``parsed`` (whether it is a JSON plan), ``strategy``, ``declined`` and the task ``types``.
    """
    try:
        plan = json.loads(content)
    except json.JSONDecodeError:
        return {"parsed": False, "strategy": None, "declined": False, "types": []}
    if not isinstance(plan, dict):
        return {"parsed": False, "strategy": None, "declined": False, "types": []}
    tasks = plan.get("tasks")
    tasks = tasks if isinstance(tasks, list) else []
    return {
        "parsed": True,
        "strategy": plan.get("strategy"),
        "declined": plan.get("strategy") == "SINGLE" and not tasks,
        "types": [t.get("type") for t in tasks if isinstance(t, dict)],
    }


def verdict(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Score a run.

    Args:
        rows: One ``classify`` result (plus ``label``) per call.

    Returns:
        ``passed``, the number of calls, how many planned `general`, declined, or planned a
        `researcher`, and how many failed to parse.
    """
    researcher = [r for r in rows if "researcher" in r["types"]]
    unparsed = [r for r in rows if not r["parsed"]]
    return {
        "passed": bool(rows) and not researcher and not unparsed,
        "calls": len(rows),
        "general": sum(1 for r in rows if "general" in r["types"]),
        "declined": sum(1 for r in rows if r["declined"]),
        "researcher": len(researcher),
        "unparsed": len(unparsed),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the probe against the llama.cpp server and write the rows and the report.

    Args:
        argv: Command-line arguments.

    Returns:
        0 when the probe passes, 1 when it fails.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", maxsplit=1)[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--url", default=llama.DEFAULT_URL)
    parser.add_argument("--model", default=llama.DEFAULT_MODEL)
    args = parser.parse_args(argv)

    from personal_agent.orchestrator.expansion_controller import _current_sub_agent_tool_surface

    surface = _current_sub_agent_tool_surface("fre1564")
    records = build_requests(args.trials, surface)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=240.0) as client, (args.run_dir / "rows.jsonl").open("w") as out:
        for record in records:
            started = time.monotonic()
            reply = client.post(args.url, json={"model": args.model, **record["body"]})
            reply.raise_for_status()
            message = reply.json()["choices"][0]["message"]
            row = {
                "label": record["label"],
                "trial": record["trial"],
                "prompt_hash": record["prompt_hash"],
                "secs": round(time.monotonic() - started, 1),
                "reasoning_chars": len(message.get("reasoning_content") or ""),
                **classify(message.get("content") or ""),
            }
            rows.append(row)
            out.write(json.dumps(row) + "\n")
            print(f"{row['label']:16s} t{row['trial']} {row['strategy']} {row['types']}")
    result = {"surface": list(surface), "model": args.model, **verdict(rows)}
    (args.run_dir / "report.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
