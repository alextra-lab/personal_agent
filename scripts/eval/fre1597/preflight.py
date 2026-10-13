"""FRE-1597 / ADR-0156 A5 (T-D1): the preflight of the routing run. It calls no model.

    uv run python -m scripts.eval.fre1597.preflight --run-dir <run>

It loads every capture (each load checks the capture again), compares ``B`` with the aligned ``A``,
runs one call of each revealed tool through the shim on the eval Elasticsearch, and projects the
run time. The owner OKs the run on these numbers (ADR-0156 A5, AC-F).
-> <run>/preflight.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.eval.fre1594 import reveal, shim
from scripts.eval.fre1597 import common, loop, prompt_size, routing
from scripts.eval.fre1597.calls import FidelityError

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.orchestrator.skills import providers_of
from personal_agent.telemetry.trace import SystemTraceContext

# Measured rates. FRE-1590 Part 1 on Flash-Next (2026-10-11): 189 first calls in 18 min, median 3.6 s.
# FRE-1594 F4 (2026-10-12): a call after a tools change re-processes the prompt, median 31.4 s of
# prefill; the simple form ran 12 draws in 28 min. Prefill 285 t/s (config/models.yaml, 2026-08-28).
SECS_FIRST_CALL = 6.0
SECS_SECOND_AFTER_REVEAL = 40.0
SECS_SECOND_NO_REVEAL = 15.0
SECS_LOOP_DRAW = 28 * 60 / 12
PREFILL_TPS = 285.0


def project(
    *, first_calls: int, second_reveal: int, second_plain: int, loop_draws: int, size_tokens: int
) -> dict[str, float]:
    """Return the projected minutes of each part and the total, at the measured rates.

    Args:
        first_calls: Items x draws.
        second_reveal: The most call-2 sends whose tools change (scenario draws of a revealing skill).
        second_plain: The most call-2 sends whose tools do not change.
        loop_draws: Loop draws (each a treatment and its control).
        size_tokens: The tokens of the three AC-3 bodies together.

    Returns:
        Minutes per part and ``total``.
    """
    minutes = {
        "routing_call_1": first_calls * SECS_FIRST_CALL / 60,
        "routing_call_2": (
            second_reveal * SECS_SECOND_AFTER_REVEAL + second_plain * SECS_SECOND_NO_REVEAL
        )
        / 60,
        "loop": loop_draws * SECS_LOOP_DRAW / 60,
        "prompt_size": size_tokens / PREFILL_TPS / 60,
    }
    minutes["total"] = sum(minutes.values())
    return {k: round(v, 1) for k, v in minutes.items()}


def _routing(run_dir: Path, draws: int) -> tuple[dict[str, object], int, int, int]:
    paths = routing.RoutingPaths(run_dir)
    items = routing.load_area_items()
    missing: list[str] = []
    refused: dict[str, str] = {}
    added: dict[str, Counter[str]] = {}
    tools: Counter[str] = Counter()
    tokens: list[int] = []
    builds: set[str] = set()
    second_reveal = second_plain = 0
    for item in items:
        path = paths.captured_file(item.id)
        if not path.exists():
            missing.append(item.id)
            continue
        try:
            meta, two = common.load_capture(path)
        except FidelityError as exc:
            refused[item.id] = str(exc)
            continue
        builds.add(str(meta.get("gateway_build")))
        first_tools = two.first["tools"]
        assert isinstance(first_tools, list)
        tools[",".join(reveal.tool_name(t) for t in first_tools)] += 1
        tokens.append(estimate_tokens(json.dumps(two.first)))
        if item.kind == "scenario":
            added.setdefault(str(item.expected_skill), Counter())[",".join(two.added_tools)] += 1
            if two.added_tools:
                second_reveal += draws
            else:
                second_plain += draws
    report = {
        "items": len(items),
        "captured": len(items) - len(missing) - len(refused),
        "missing": missing,
        "refused": refused,
        "call_1_tool_lists": dict(tools),
        "call_2_added_tools": {k: dict(v) for k, v in sorted(added.items())},
        "call_1_tokens_estimate": [min(tokens), max(tokens)] if tokens else [],
        "gateway_builds": sorted(builds),
    }
    return report, len(items) * draws, second_reveal, second_plain


def _loop(run_dir: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    for prompt in reveal.load_scenarios().prompts:
        path = loop.captured_file(run_dir, prompt.id)
        if not path.exists():
            out[prompt.id] = "missing"
            continue
        try:
            meta, two = common.load_capture(path)
            loop.make_draw(prompt, two, number=0)
        except FidelityError as exc:
            out[prompt.id] = f"refused: {exc}"
            continue
        out[prompt.id] = {
            "added_tools": list(two.added_tools),
            "gateway": meta.get("gateway_build"),
        }
    return out


def _prompt_size(run_dir: Path) -> tuple[dict[str, object], int]:
    root = run_dir / "prompt_size"
    if not (root / "B.json").exists() or not (root / "A.json").exists():
        return {"error": "B.json or A.json is missing"}, 0
    meta_b, b = common.load_capture(root / "B.json")
    meta_a, a = common.load_capture(root / "A.json")
    try:
        aligned = prompt_size.align(a.first, b.first)
    except prompt_size.SizeError as exc:
        return {"error": str(exc)}, 0
    bodies = {"B": b.first, "B-minus": prompt_size.b_minus(b.first), "A": aligned}
    estimates = {k: estimate_tokens(json.dumps(v)) for k, v in bodies.items()}
    diff = prompt_size.body_diff(b.first, aligned)
    report = {
        "gateway_builds": {"B": meta_b.get("gateway_build"), "A": meta_a.get("gateway_build")},
        "tokens_estimate": estimates,
        "unexpected": diff["unexpected"],
        "system_blocks": len(diff["system_blocks"]),  # type: ignore[arg-type]
        "tool_blocks": len(diff["tool_blocks"]),  # type: ignore[arg-type]
        "tools_removed": diff["tools_removed"],
    }
    return report, sum(estimates.values())


def _shim_smoke() -> dict[str, object]:
    settings = get_settings()
    smoke: dict[str, object] = {"elasticsearch_url": settings.elasticsearch_url}
    try:
        shim.assert_eval_elasticsearch(settings.elasticsearch_url)
    except SystemExit as exc:
        smoke["error"] = str(exc)
        return smoke
    ctx = SystemTraceContext.new("fre1597_preflight")
    providers = {t: providers_of(t) for t in ("search_logs", "measure_model_latency")}
    for name, arguments in (
        ("search_logs", '{"action": "count", "index": "agent-logs", "since": "24h"}'),
        ("measure_model_latency", '{"since": "1h"}'),
    ):
        run = asyncio.run(
            shim.run_tool(name, arguments, revealed=(name,), providers=providers, ctx=ctx)
        )
        smoke[name] = {"ran": run.ran, "code": run.code, "content": run.content[:300]}
    return smoke


def main(argv: Sequence[str] | None = None) -> int:
    """Write and print the preflight report."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--draws", type=int, default=common.DEFAULT_DRAWS)
    args = parser.parse_args(argv)
    routing_report, first_calls, second_reveal, second_plain = _routing(args.run_dir, args.draws)
    size_report, size_tokens = _prompt_size(args.run_dir)
    report: Mapping[str, object] = {
        "routing": routing_report,
        "loop": _loop(args.run_dir),
        "prompt_size": size_report,
        "shim_smoke": _shim_smoke(),
        "projection_minutes": project(
            first_calls=first_calls,
            second_reveal=second_reveal,
            second_plain=second_plain,
            loop_draws=4 * args.draws,
            size_tokens=size_tokens,
        ),
    }
    (args.run_dir / "preflight.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
