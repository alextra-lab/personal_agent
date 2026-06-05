r"""FRE-481 — ADR-0086 artifact-decomposition before/after A/B harness.

Drives an artifact-build turn through the live gateway ``/chat`` and reads back the
**full per-round** token curve from Elasticsearch so the serial-discovery baseline
and the HYBRID-decomposition arm can be compared on the same stack (the FRE-433
recipe, research doc §9). The deterministic claim under test: the parent's
``fresh_in`` no longer climbs to ~71 k because discovery digests bound it.

Two arms, selected by *what flag the gateway is deployed with* (the harness only
tags ``--arm`` and drives traffic):

* **arm ``baseline``** — ``AGENT_ARTIFACT_DECOMPOSITION_ENABLED=false`` (current
  ``main``): TOOL_USE artifact builds route to ``SINGLE`` (serial discovery).
* **arm ``decompose``** — ``AGENT_ARTIFACT_DECOMPOSITION_ENABLED=true``: high-
  complexity artifact builds route to ``HYBRID`` with tool-using discovery
  sub-agents, each returning a digest.

Backend is per-request via ``--profile {local,cloud}`` — no redeploy to switch.
Backend-aware truth source (research doc §9 / FRE-433): a cross-turn cloud call
reports the reused prefix as ``cache_read_input_tokens`` and only the uncached
portion as ``input_tokens``; local reports the full prompt as ``input_tokens``.
Per-round "context size" therefore keys on the max of the token fields.

This harness is the deliverable; **running it is a master post-deploy action**
(it needs a deploy with the flag in the desired state). See README.md.

Usage::

    uv run python scripts/eval/fre481_decomposition_ab/harness.py \\
        --run-id ab-2026-06-05 --arm baseline --profile cloud \\
        --auth-email <loopback-eval-email>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml  # type: ignore[import-untyped]

from personal_agent.config import get_settings

log = structlog.get_logger(__name__)

DEFAULT_CHAT_URL = "http://localhost:9001/chat"


@dataclass(frozen=True)
class PromptDef:
    """One artifact-build turn to run.

    Attributes:
        label: Human-readable identifier (tag in the report).
        note: Why this prompt exposes the decomposition behaviour.
        message: The user message (an ``a0a07227``-equivalent artifact build).
    """

    label: str
    note: str
    message: str


@dataclass
class RoundMetric:
    """One ``model_call_completed`` event in the turn (a single LLM round).

    Attributes:
        seq: 1-based round index within the turn (by ``@timestamp`` ascending).
        role: Model role (``primary`` / ``sub_agent``).
        endpoint: Backend endpoint of the call.
        input_tokens: Fresh (uncached) prompt tokens — the parent ``fresh_in``.
        cache_read_tokens: Reused-prefix tokens (cloud ``cache_read_input_tokens``).
        output_tokens: Generated tokens.
        latency_ms: Call latency.
    """

    seq: int
    role: str | None
    endpoint: str | None
    input_tokens: int | None
    cache_read_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None


@dataclass
class TurnReport:
    """Full before/after picture for one artifact-build turn.

    Attributes:
        label: Prompt label.
        trace_id: Trace id returned by ``/chat``.
        session_id: Session id of the turn.
        strategy: Stage-5 strategy from ``decomposition_assessed`` (single/hybrid).
        reason: Stage-5 routing reason (e.g. ``tool_use_complex_hybrid``).
        intent_signals: Intent signals (must contain ``artifact_build``).
        rounds: Per-round token curve (all ``model_call_completed`` for the trace).
        round_count: Number of model rounds.
        max_parent_fresh_in: Peak ``input_tokens`` across primary rounds (the
            71 k-climb claim — should be bounded by digests under ``decompose``).
        total_input_tokens: Σ fresh input across all rounds.
        total_cache_read_tokens: Σ reused-prefix tokens.
        total_output_tokens: Σ generated tokens.
        wall_time_s: First→last event timestamp span for the trace.
        subagent_iterations: Count of ``sub_agent_tooled_iteration`` events.
        subagent_completes: Count of ``sub_agent_complete`` events (joinable by
            ``session_id`` after FRE-481).
        artifact_response_chars: Length of the ``/chat`` response text (quality eval
            material — paired across arms for the ADR §2 side-by-side rating).
        artifact_id: Artifact id if surfaced by ``/chat`` (else None).
        response_text: The captured response (for the human side-by-side eval).
    """

    label: str
    trace_id: str
    session_id: str
    strategy: str | None
    reason: str | None
    intent_signals: list[str]
    rounds: list[RoundMetric]
    round_count: int
    max_parent_fresh_in: int
    total_input_tokens: int
    total_cache_read_tokens: int
    total_output_tokens: int
    wall_time_s: float
    subagent_iterations: int
    subagent_completes: int
    artifact_response_chars: int
    artifact_id: str | None
    response_text: str = field(default="", repr=False)


def load_dataset(path: Path) -> list[PromptDef]:
    """Load the artifact-build prompt dataset from YAML.

    Args:
        path: Path to ``dataset.yaml``.

    Returns:
        Ordered list of prompt definitions.

    Raises:
        ValueError: If the file has no ``prompts`` list.
    """
    raw = yaml.safe_load(path.read_text())
    prompts = raw.get("prompts") if isinstance(raw, dict) else None
    if not prompts:
        raise ValueError(f"No 'prompts' found in {path}")
    return [
        PromptDef(
            label=str(p["label"]),
            note=str(p.get("note", "")),
            message=str(p["message"]),
        )
        for p in prompts
    ]


async def call_chat(
    client: httpx.AsyncClient,
    chat_url: str,
    message: str,
    auth_email: str | None,
    profile: str,
) -> tuple[dict[str, Any], str, str]:
    """POST one artifact-build turn to ``/chat``.

    Args:
        client: Shared async HTTP client.
        chat_url: Gateway ``/chat`` URL.
        message: The artifact-build message.
        auth_email: CF-Access email to impersonate, or None.
        profile: Model profile (``local`` or ``cloud``).

    Returns:
        Tuple of the parsed response body, session id, and trace id.
    """
    params = {"message": message, "profile": profile, "channel": "EVAL"}
    headers: dict[str, str] = {}
    if auth_email:
        headers["Cf-Access-Authenticated-User-Email"] = auth_email
    resp = await client.post(chat_url, params=params, headers=headers, timeout=1200.0)
    resp.raise_for_status()
    data = resp.json()
    return data, str(data["session_id"]), str(data["trace_id"])


async def _es_search(
    client: httpx.AsyncClient, es_url: str, index: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Run an ES ``_search`` and return the parsed response."""
    resp = await client.post(f"{es_url}/{index}/_search", json=body, timeout=30.0)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def wait_for_trace(
    client: httpx.AsyncClient,
    es_url: str,
    index: str,
    trace_id: str,
    timeout_s: float,
) -> bool:
    """Poll ES until the turn's terminal event is indexed.

    Waits for a ``turn_completed``/``response_persisted`` style terminal marker OR a
    quiescent count of ``model_call_completed`` events. We simply wait for at least
    one ``model_call_completed`` and then a short settle, since the artifact turn is
    long and we read all rounds afterwards.

    Args:
        client: Async HTTP client.
        es_url: Elasticsearch base URL.
        index: Index pattern.
        trace_id: Trace id to wait for.
        timeout_s: Hard polling timeout in seconds.

    Returns:
        True if at least one model round was indexed before the timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event_type": "model_call_completed"}},
                ]
            }
        },
    }
    seen = 0
    while asyncio.get_event_loop().time() < deadline:
        r = await _es_search(client, es_url, index, body)
        hits = int(r["hits"]["total"]["value"])
        if hits > 0 and hits == seen:
            # Count stable across two polls → indexing has settled.
            return True
        seen = hits
        await asyncio.sleep(3.0)
    return seen > 0


def _context_size(s: dict[str, Any]) -> int:
    """Backend-aware context size: max of the three token fields (FRE-433)."""
    return max(
        s.get("input_tokens") or 0,
        s.get("cache_read_tokens") or 0,
        s.get("cache_creation_input_tokens") or 0,
    )


async def fetch_rounds(
    client: httpx.AsyncClient, es_url: str, index: str, trace_id: str
) -> tuple[list[RoundMetric], float]:
    """Fetch the full per-round ``model_call_completed`` series for the trace.

    Args:
        client: Async HTTP client.
        es_url: Elasticsearch base URL.
        index: Index pattern.
        trace_id: Trace id for the turn.

    Returns:
        Tuple of (ordered round metrics, wall-time span in seconds).
    """
    body = {
        "size": 200,
        "_source": [
            "@timestamp",
            "role",
            "endpoint",
            "input_tokens",
            "cache_read_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
            "latency_ms",
        ],
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id": trace_id}},
                    {"term": {"event_type": "model_call_completed"}},
                ]
            }
        },
        "sort": [{"@timestamp": "asc"}],
    }
    r = await _es_search(client, es_url, index, body)
    hits = r["hits"]["hits"]
    rounds: list[RoundMetric] = []
    for seq, h in enumerate(hits, start=1):
        src = h["_source"]
        rounds.append(
            RoundMetric(
                seq=seq,
                role=src.get("role"),
                endpoint=src.get("endpoint"),
                input_tokens=src.get("input_tokens"),
                cache_read_tokens=src.get("cache_read_tokens"),
                output_tokens=src.get("output_tokens"),
                latency_ms=src.get("latency_ms"),
            )
        )
    wall_time_s = _wall_time(hits)
    return rounds, wall_time_s


def _wall_time(hits: list[dict[str, Any]]) -> float:
    """First→last ``@timestamp`` span in seconds across the hits."""
    stamps = [h["_source"].get("@timestamp") for h in hits if h["_source"].get("@timestamp")]
    if len(stamps) < 2:
        return 0.0
    parsed = sorted(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
    return (parsed[-1] - parsed[0]).total_seconds()


async def fetch_routing(
    client: httpx.AsyncClient, es_url: str, index: str, trace_id: str
) -> tuple[str | None, str | None, list[str]]:
    """Fetch the Stage-5 routing decision + intent signals for the trace.

    Args:
        client: Async HTTP client.
        es_url: Elasticsearch base URL.
        index: Index pattern.
        trace_id: Trace id for the turn.

    Returns:
        Tuple of (strategy, reason, intent_signals). Any may be empty if the
        gateway pipeline events are not (yet) joinable by trace_id.
    """
    body = {
        "size": 20,
        "_source": ["event_type", "event", "strategy", "reason", "signals"],
        "query": {
            "bool": {
                "must": [{"term": {"trace_id": trace_id}}],
                "should": [
                    {"term": {"event_type": "decomposition_assessed"}},
                    {"term": {"event_type": "intent_classified"}},
                    {"term": {"event": "decomposition_assessed"}},
                    {"term": {"event": "intent_classified"}},
                ],
                "minimum_should_match": 1,
            }
        },
    }
    r = await _es_search(client, es_url, index, body)
    strategy: str | None = None
    reason: str | None = None
    signals: list[str] = []
    for h in r["hits"]["hits"]:
        src = h["_source"]
        name = src.get("event_type") or src.get("event")
        if name == "decomposition_assessed":
            strategy = src.get("strategy") or strategy
            reason = src.get("reason") or reason
        elif name == "intent_classified":
            signals = list(src.get("signals") or signals)
    return strategy, reason, signals


async def fetch_subagent_slice(
    client: httpx.AsyncClient, es_url: str, index: str, trace_id: str, session_id: str
) -> tuple[int, int]:
    """Count discovery-sub-agent events for the trace (joinable by session_id, FRE-481).

    Args:
        client: Async HTTP client.
        es_url: Elasticsearch base URL.
        index: Index pattern.
        trace_id: Trace id for the turn.
        session_id: Session id (proves session-anchor joinability of the new events).

    Returns:
        Tuple of (tooled-iteration count, sub-agent-complete count).
    """

    async def _count(event_name: str) -> int:
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"trace_id": trace_id}},
                        {"term": {"session_id": session_id}},
                    ],
                    "should": [
                        {"term": {"event_type": event_name}},
                        {"term": {"event": event_name}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        }
        r = await _es_search(client, es_url, index, body)
        return int(r["hits"]["total"]["value"])

    iterations = await _count("sub_agent_tooled_iteration")
    completes = await _count("sub_agent_complete")
    return iterations, completes


def build_report(
    prompt: PromptDef,
    trace_id: str,
    session_id: str,
    rounds: list[RoundMetric],
    wall_time_s: float,
    strategy: str | None,
    reason: str | None,
    signals: list[str],
    subagent_iterations: int,
    subagent_completes: int,
    response_body: dict[str, Any],
) -> TurnReport:
    """Assemble the per-turn report from collected telemetry."""
    primary_fresh = [r.input_tokens or 0 for r in rounds if r.role == "primary"]
    response_text = str(response_body.get("response", ""))
    return TurnReport(
        label=prompt.label,
        trace_id=trace_id,
        session_id=session_id,
        strategy=strategy,
        reason=reason,
        intent_signals=signals,
        rounds=rounds,
        round_count=len(rounds),
        max_parent_fresh_in=max(primary_fresh, default=0),
        total_input_tokens=sum(r.input_tokens or 0 for r in rounds),
        total_cache_read_tokens=sum(r.cache_read_tokens or 0 for r in rounds),
        total_output_tokens=sum(r.output_tokens or 0 for r in rounds),
        wall_time_s=wall_time_s,
        subagent_iterations=subagent_iterations,
        subagent_completes=subagent_completes,
        artifact_response_chars=len(response_text),
        artifact_id=response_body.get("artifact_id"),
        response_text=response_text,
    )


def render_markdown(run_meta: dict[str, Any], reports: list[TurnReport]) -> str:
    """Render an A/B-friendly markdown summary for one pass."""
    lines: list[str] = [
        f"# FRE-481 artifact-decomposition A/B — {run_meta['run_id']}",
        "",
        f"- **arm**: `{run_meta['arm']}` "
        f"({'HYBRID decomposition' if run_meta['arm'] == 'decompose' else 'serial SINGLE baseline'})",
        f"- **profile/backend**: `{run_meta['profile']}`",
        f"- **timestamp**: {run_meta['timestamp']}",
        "",
        "Deterministic claim (ADR-0086 D4): parent `max_fresh_in` is bounded under "
        "`decompose` (digests cross the boundary, not the 71 k discovery tail). "
        "Wall-time + net-cost are measurement-gated (near-zero wall-time win on "
        "single-GPU local; net tokens can rise if slices overlap).",
        "",
        "| prompt | strategy | reason | rounds | max_fresh_in | Σin | Σcache_rd | Σout | wall_s | sa_iters | sa_done | artifact_chars |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rep in reports:
        lines.append(
            f"| {rep.label} | {rep.strategy} | {rep.reason} | {rep.round_count} | "
            f"**{rep.max_parent_fresh_in}** | {rep.total_input_tokens} | "
            f"{rep.total_cache_read_tokens} | {rep.total_output_tokens} | "
            f"{rep.wall_time_s:.1f} | {rep.subagent_iterations} | {rep.subagent_completes} | "
            f"{rep.artifact_response_chars} |"
        )
    lines += [
        "",
        "## Per-round token curve (first report)",
        "",
    ]
    if reports:
        lines += [
            "| seq | role | in_tok | cache_rd | out | lat_ms |",
            "|---|---|---|---|---|---|",
        ]
        for r in reports[0].rounds:
            lines.append(
                f"| {r.seq} | {r.role} | {r.input_tokens} | {r.cache_read_tokens} | "
                f"{r.output_tokens} | {r.latency_ms} |"
            )
    lines += [
        "",
        "Diff `baseline` vs `decompose` on **max_fresh_in** (deterministic) and report "
        "Σin/Σout/wall_s honestly (gated). Artifact text is captured in the JSON for the "
        "human side-by-side quality eval (ADR-0086 §2).",
    ]
    return "\n".join(lines)


async def run_prompt(
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    args: argparse.Namespace,
    es_url: str,
    index: str,
    prompt: PromptDef,
) -> TurnReport:
    """Drive one artifact-build prompt and collect its full report."""
    body, session_id, trace_id = await call_chat(
        http, args.chat_url, prompt.message, args.auth_email, args.profile
    )
    log.info("turn_sent", label=prompt.label, session_id=session_id, trace_id=trace_id)
    ok = await wait_for_trace(es, es_url, index, trace_id, args.trace_timeout_s)
    if not ok:
        log.warning("no_rounds_indexed", trace_id=trace_id, label=prompt.label)
    rounds, wall_time_s = await fetch_rounds(es, es_url, index, trace_id)
    strategy, reason, signals = await fetch_routing(es, es_url, index, trace_id)
    sa_iters, sa_done = await fetch_subagent_slice(es, es_url, index, trace_id, session_id)
    report = build_report(
        prompt,
        trace_id,
        session_id,
        rounds,
        wall_time_s,
        strategy,
        reason,
        signals,
        sa_iters,
        sa_done,
        body,
    )
    log.info(
        "turn_report",
        label=prompt.label,
        strategy=strategy,
        reason=reason,
        rounds=report.round_count,
        max_fresh_in=report.max_parent_fresh_in,
        wall_s=round(report.wall_time_s, 1),
        sa_iters=sa_iters,
    )
    return report


async def amain(args: argparse.Namespace) -> int:
    """Drive the dataset and write the pass results."""
    settings = get_settings()
    es_url = settings.elasticsearch_url.rstrip("/")
    prefix = args.logs_prefix or settings.elasticsearch_index_prefix
    index = f"{prefix}-*"
    dataset = load_dataset(Path(args.dataset))

    run_meta = {
        "run_id": args.run_id,
        "arm": args.arm,
        "profile": args.profile,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_url": args.chat_url,
        "logs_index": index,
    }
    reports: list[TurnReport] = []
    async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
        try:
            h = await http.get(args.chat_url.replace("/chat", "/health"), timeout=10.0)
            log.info("gateway_health", status=h.status_code)
        except httpx.HTTPError as exc:
            log.error("gateway_unreachable", url=args.chat_url, error=str(exc))
            return 2
        for prompt in dataset:
            reports.append(await run_prompt(http, es, args, es_url, index, prompt))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.run_id}_{args.arm}_{args.profile}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"meta": run_meta, "reports": [asdict(r) for r in reports]}, indent=2)
    )
    (out_dir / f"{stem}.md").write_text(render_markdown(run_meta, reports))
    log.info("pass_written", out=str(out_dir / f"{stem}.md"), prompts=len(reports))
    return 0


def main() -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(description="FRE-481 artifact-decomposition A/B harness")
    p.add_argument("--run-id", required=True, help="Run identifier (tag in output).")
    p.add_argument(
        "--arm",
        required=True,
        choices=["baseline", "decompose"],
        help="Which flag state the gateway is deployed with (metadata tag).",
    )
    p.add_argument(
        "--profile", default="cloud", choices=["local", "cloud"], help="Model profile/backend."
    )
    p.add_argument(
        "--dataset",
        default="scripts/eval/fre481_decomposition_ab/dataset.yaml",
        help="Path to the artifact-build prompt dataset YAML.",
    )
    p.add_argument("--chat-url", default=DEFAULT_CHAT_URL, help="Gateway /chat URL.")
    p.add_argument(
        "--auth-email", default=None, help="CF-Access email to impersonate for loopback calls."
    )
    p.add_argument(
        "--out", default="telemetry/evaluation/fre481-decomposition-ab", help="Output directory."
    )
    p.add_argument(
        "--logs-prefix",
        default=None,
        help="ES logs index prefix (default: settings.elasticsearch_index_prefix). "
        "Set explicitly if the deployed prefix diverges, else the read silently returns zero events.",
    )
    p.add_argument(
        "--trace-timeout-s",
        type=float,
        default=900.0,
        help="Per-turn ES indexing wait timeout (artifact turns are long).",
    )
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
