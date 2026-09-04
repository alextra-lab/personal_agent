#!/usr/bin/env python3
"""FRE-1390 — planner role A/B: ``sub_agent`` (before) vs ``primary`` (after).

Answers the three measurement ACs on the ticket by direct replay of the exact
planner call shape ``expansion_controller._run_planner`` sends — same system
prompt, same message format, same ``max_tokens``/``response_format`` — against
the live local SLM server, for both candidate roles, on the same fixture
queries. This is a **direct replay**, not a full live HYBRID/DECOMPOSE turn
through the ``/chat`` gateway (ADR-0036's expansion controller only fires on
~5% of turns and is not deterministically triggerable from outside); the same
caveat ``fre432_ph0_thinking_probe.py`` documents for its own replay applies
here — this measures the planner call in isolation, an upper bound on the
production planning-phase latency, not a live-turn number.

Deliberately raw ``httpx`` against the SLM server (mirrors
``fre432_ph0_thinking_probe.py``), not the full ``LiteLLMClient`` — that path
goes through the Postgres-backed cost gate (ADR-0065), a production-substrate
write ``scripts/eval/`` must not make without opt-in (FRE-375, ``tests/CLAUDE.md``).
A local SLM call is free and this script only ever reads config + posts
inference requests, so nothing here needs the eval substrate stack either.

AC-1 (role is genuinely thinking-capable, verified live, not from YAML): for
each role, resolves the deployment via ``resolve_role_target`` (the same
function ``get_llm_client`` uses) and separately confirms live, from the
server's own response, whether a reasoning/``<think>`` block was actually
emitted for that role's call — the wire-level fact, not the config's claim
about the wire.

AC-2 (plan quality compared, not assumed): the same fixture queries are
planned under both roles and the two plans are written side by side into the
report.

AC-3 (added latency reported): per-role, per-query wall-clock is recorded,
plus the aggregate p50/p90 delta.

Usage::

    uv run python scripts/eval/fre1390_planner_role_ab/harness.py \\
        --out telemetry/evaluation/fre1390-planner-role-ab
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog

from personal_agent.config.model_loader import resolve_role_target
from personal_agent.llm_client.types import ModelRole
from personal_agent.orchestrator.expansion_controller import (
    _PLANNER_SYSTEM_PROMPT,
    _validate_plan_json,
)

log = structlog.get_logger(__name__)

# Same fixture queries the unit-test suite and the fallback planner's own
# doctest-style examples use — comma-list (entity-bearing, HYBRID) and an
# open-ended query (no enumerable structure, exercises the generic 2-task
# shape both under the LLM planner and, on failure, the deterministic one).
_FIXTURE_QUERIES: list[tuple[str, str]] = [
    ("HYBRID", "Compare Redis, Memcached, and Hazelcast for a session-cache workload"),
    ("HYBRID", "Evaluate Postgres, MySQL, and CockroachDB for a write-heavy OLTP service"),
    ("DECOMPOSE", "What's the best approach to scale a Postgres database under heavy write load?"),
]

_THINK_CLOSED_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>(.*)$", re.DOTALL)


def _extract_thinking(message: dict[str, Any]) -> str:
    """Return the reasoning text a completion message carries, if any.

    Prefers the dedicated ``reasoning_content`` field (llama.cpp's canonical
    shape for a role with thinking enabled) and falls back to an inline
    ``<think>...</think>`` block.
    """
    reasoning = (message.get("reasoning_content") or "").strip()
    if reasoning:
        return reasoning
    content = message.get("content") or ""
    closed = _THINK_CLOSED_RE.findall(content)
    if closed:
        return "".join(closed).strip()
    if "<think>" in content:
        match = _THINK_OPEN_RE.search(content)
        return match.group(1).strip() if match else ""
    return ""


def _local_extra_body(disable_thinking: bool, thinking_budget_tokens: int | None) -> dict[str, Any]:
    """Mirror ``litellm_client._local_extra_body``'s thinking-control wire shape."""
    extra_body: dict[str, Any] = {"cache_prompt": True}
    if disable_thinking:
        extra_body["chat_template_kwargs"] = {"enable_thinking": False}
    elif thinking_budget_tokens is not None:
        extra_body["thinking_budget"] = thinking_budget_tokens
    return extra_body


@dataclass
class RoleResolution:
    """AC-1 — live-resolved role binding, not read from ``model_roles.yaml``."""

    role: str
    deployment_key: str
    served_model_id: str
    disable_thinking: bool
    thinking_budget_tokens: int | None
    endpoint: str


@dataclass
class PlannerCallResult:
    """One planner call under one role, for one fixture query."""

    role: str
    strategy: str
    query: str
    latency_s: float
    http_status: int | None
    plan_valid: bool
    task_count: int
    task_names: list[str]
    thinking_chars: int
    plan_json: str = field(default="", repr=False)
    error: str | None = None


async def call_planner(
    client: httpx.AsyncClient,
    role: ModelRole,
    resolution: RoleResolution,
    strategy: str,
    query: str,
) -> PlannerCallResult:
    """Send one planner-shaped call, mirroring ``_run_planner`` exactly.

    Same system prompt, same user-message template, same ``max_tokens`` and
    JSON response-format constraint as ``expansion_controller._run_planner``.
    """
    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Strategy: {strategy}\nQuery: {query}\n\nProduce the JSON plan.",
        },
    ]
    payload = {
        "model": resolution.served_model_id,
        "messages": messages,
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
        **_local_extra_body(resolution.disable_thinking, resolution.thinking_budget_tokens),
    }
    start = time.monotonic()
    try:
        resp = await client.post(
            f"{resolution.endpoint}/chat/completions", json=payload, timeout=240.0
        )
        latency = time.monotonic() - start
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return PlannerCallResult(
            role=role.value,
            strategy=strategy,
            query=query,
            latency_s=time.monotonic() - start,
            http_status=getattr(getattr(exc, "response", None), "status_code", None),
            plan_valid=False,
            task_count=0,
            task_names=[],
            thinking_chars=0,
            error=str(exc),
        )

    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    thinking = _extract_thinking(message)
    plan = _validate_plan_json(content, strategy)
    return PlannerCallResult(
        role=role.value,
        strategy=strategy,
        query=query,
        latency_s=latency,
        http_status=resp.status_code,
        plan_valid=plan is not None,
        task_count=len(plan.tasks) if plan else 0,
        task_names=[t.name for t in plan.tasks] if plan else [],
        thinking_chars=len(thinking),
        plan_json=content,
    )


def render_markdown(resolutions: list[RoleResolution], results: list[PlannerCallResult]) -> str:
    """Render the AC-1/AC-2/AC-3 side-by-side report."""
    lines = [
        "# FRE-1390 — planner role A/B (sub_agent vs primary)",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()}",
        "",
        "## AC-1 — live-resolved role binding",
        "",
        "| role | deployment | served id | disable_thinking | thinking_budget_tokens |",
        "|---|---|---|---|---|",
    ]
    for r in resolutions:
        lines.append(
            f"| {r.role} | {r.deployment_key} | {r.served_model_id} | "
            f"{r.disable_thinking} | {r.thinking_budget_tokens} |"
        )

    lines += [
        "",
        "## AC-1 (wire-level) — reasoning actually emitted per role",
        "",
        "| role | calls | calls with reasoning text | median thinking_chars |",
        "|---|---|---|---|",
    ]
    for role in ("sub_agent", "primary"):
        role_results = [r for r in results if r.role == role and r.error is None]
        with_reasoning = sum(1 for r in role_results if r.thinking_chars > 0)
        median_chars = (
            statistics.median(r.thinking_chars for r in role_results) if role_results else 0
        )
        lines.append(f"| {role} | {len(role_results)} | {with_reasoning} | {median_chars:.0f} |")

    lines += [
        "",
        "## AC-3 — planner-phase latency, sub_agent (before) vs primary (after)",
        "",
        "| role | n | median_s | p90_s | max_s |",
        "|---|---|---|---|---|",
    ]
    for role in ("sub_agent", "primary"):
        vals = sorted(r.latency_s for r in results if r.role == role and r.error is None)
        if not vals:
            lines.append(f"| {role} | 0 | - | - | - |")
            continue
        median = statistics.median(vals)
        p90 = vals[min(int(0.9 * len(vals)), len(vals) - 1)]
        lines.append(f"| {role} | {len(vals)} | {median:.2f} | {p90:.2f} | {vals[-1]:.2f} |")

    lines += [
        "",
        "## AC-2 — per-query plan comparison",
        "",
    ]
    by_query: dict[str, list[PlannerCallResult]] = {}
    for r in results:
        by_query.setdefault(r.query, []).append(r)
    for query, pair in by_query.items():
        lines.append(f"### {query}")
        lines.append("")
        for r in sorted(pair, key=lambda x: x.role):
            status = "valid" if r.plan_valid else f"INVALID ({r.error or 'schema'})"
            lines.append(
                f"- **{r.role}** [{status}] — {r.latency_s:.2f}s, "
                f"{r.task_count} tasks: {', '.join(r.task_names) or '(none)'}, "
                f"thinking_chars={r.thinking_chars}"
            )
        lines.append("")

    lines += [
        "## AC-4",
        "",
        "Exercised as a unit test, not here: "
        "`tests/personal_agent/orchestrator/test_expansion_controller.py::"
        "TestPlannerServerErrorFallback` raises the real `LLMServerError` class "
        "the client produces for a 5xx after retries and asserts the deterministic "
        "fallback planner still takes over.",
        "",
        "## Methodology caveat",
        "",
        "Direct replay of the exact planner call shape against the live SLM server "
        "— not a full HYBRID/DECOMPOSE turn through the `/chat` gateway (not "
        "deterministically triggerable from outside, and ADR-0036 routes only "
        "~5% of turns there). Latency here is the planner call in isolation: an "
        "upper bound on the production planning-phase number, which also pays "
        "gateway/session overhead this script does not.",
    ]
    return "\n".join(lines)


async def amain(args: argparse.Namespace) -> int:
    resolutions: list[RoleResolution] = []
    role_models: dict[str, str] = {}
    for role in (ModelRole.SUB_AGENT, ModelRole.PRIMARY):
        key, model_def = resolve_role_target(role.value)
        if model_def is None:
            log.error("role_resolves_to_no_definition", role=role.value, key=key)
            return 2
        endpoint = args.slm_url or model_def.endpoint
        if not endpoint:
            log.error("role_resolves_to_no_endpoint", role=role.value, key=key)
            return 2
        resolutions.append(
            RoleResolution(
                role=role.value,
                deployment_key=key,
                served_model_id=model_def.id,
                disable_thinking=bool(model_def.disable_thinking),
                thinking_budget_tokens=model_def.thinking_budget_tokens,
                endpoint=endpoint.rstrip("/"),
            )
        )
        role_models[role.value] = model_def.id

    async with httpx.AsyncClient() as client:
        probe_endpoint = resolutions[0].endpoint
        try:
            probe = await client.get(f"{probe_endpoint}/models", timeout=10.0)
            probe.raise_for_status()
            served = {m["id"] for m in probe.json().get("data", [])}
        except httpx.HTTPError as exc:
            log.error("slm_unreachable", url=probe_endpoint, error=str(exc))
            return 2
        for model_id in set(role_models.values()):
            if model_id not in served:
                log.error("model_not_currently_served", expected=model_id, served=sorted(served))
                return 2

        results: list[PlannerCallResult] = []
        # Serial: the primary deployment is single-concurrency (max_concurrency 1,
        # config/models.yaml), and it and the sub_agent role share the one served
        # model on the shared GPU — parallel calls would contend with each other.
        for strategy, query in _FIXTURE_QUERIES:
            for resolution in resolutions:
                role = ModelRole(resolution.role)
                result = await call_planner(client, role, resolution, strategy, query)
                results.append(result)
                log.info(
                    "planner_call_done",
                    role=role.value,
                    query=query[:60],
                    latency_s=round(result.latency_s, 2),
                    plan_valid=result.plan_valid,
                    thinking_chars=result.thinking_chars,
                    error=result.error,
                )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}.json").write_text(
        json.dumps(
            {
                "resolutions": [asdict(r) for r in resolutions],
                "results": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    report = render_markdown(resolutions, results)
    (out_dir / f"{stamp}.md").write_text(report)
    print(report)
    log.info("report_written", out=str(out_dir / f"{stamp}.md"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="FRE-1390 planner role A/B harness")
    parser.add_argument("--slm-url", default=None, help="SLM /v1 base URL (default: settings)")
    parser.add_argument(
        "--out", default="telemetry/evaluation/fre1390-planner-role-ab", help="Output directory."
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
