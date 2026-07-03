#!/usr/bin/env python3
"""FRE-432 Phase 0: measure primary thinking-token emission on SINGLE turns.

Tests the ADR-0082 / M2 hypothesis — *"primary burns heavy thinking on trivial
turns"* — for ``conversational`` and ``memory_recall`` turns the gateway routed
``SINGLE`` (no decomposition, no sub-agents). It answers the question three ways
and reports the gap between them, because no single durable field records the
quantity of interest:

1. **Ledger proxy** (``route_traces.output_tokens``) — the FRE-452 durable row.
   Kept only as a *contrast*: it is known to undercount (missing ``api_costs``
   rows for some model calls), so it is NOT the authority.
2. **Live authoritative** (ES ``model_call_completed``) — per-call
   ``output_tokens`` summed per turn. The trustworthy count of *total* primary
   generation on the real turn, but it carries no think/visible split.
3. **Direct replay** — re-send the turn's recovered stimulus to the deployed
   thinking model standalone and read the raw ``reasoning_content`` /
   ``<think>`` block. Gives the think/visible split directly, but with the bare
   stimulus (no live grounding context), so it is an *upper bound* on live
   thinking, not a match for it.

Nothing here writes to any substrate: Postgres/ES reads are SELECT/search only,
and the replay is a raw ``/v1/chat/completions`` call that never touches the
``/chat`` gateway, the knowledge graph, or memory. Replays are serialised — the
primary is single-concurrency and parallel calls contend with live traffic.

Usage (run against the LIVE stack — do NOT set ``APP_ENV=test``, which redirects
to the empty test substrate):

    # ES-only distribution (no inference):
    uv run python scripts/research/fre432_ph0_thinking_probe.py --no-replay

    # full probe incl. ~12 serialised replays (needs CF Access service token in
    # the environment: CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET):
    set -a; source /opt/seshat/.env; set +a
    uv run python scripts/research/fre432_ph0_thinking_probe.py --replay-n 12 \
        --out /tmp/fre432_ph0.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
from collections.abc import Sequence
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import httpx
import structlog

from personal_agent.config import get_settings

log = structlog.get_logger(__name__)

# Gateway task types whose SINGLE-strategy turns are the routing candidates (ADR-0082).
_TARGET_TASK_TYPES = ("conversational", "memory_recall")

# Heuristic vision filter: replaying a "describe the photo" stimulus without its
# image measures nothing useful, so such turns are excluded from the replay set.
_VISION_RE = re.compile(r"\b(photo|image|picture|pictured|attached|screenshot)\b", re.IGNORECASE)

# Inline thinking block, some backends emit this instead of a reasoning field.
_THINK_CLOSED_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>(.*)$", re.DOTALL)


# ── Pure computation layer (unit-tested) ──────────────────────────────────────


def extract_think_visible(message: dict[str, Any]) -> tuple[str, str]:
    """Split a completion message into ``(thinking, visible)`` text.

    Prefers the dedicated ``reasoning_content`` field (the llama.cpp canonical
    shape) and falls back to an inline ``<think>...</think>`` block, tolerating a
    truncated generation that leaves the tag unclosed.

    Args:
        message: The ``choices[0].message`` object from a chat completion.

    Returns:
        A ``(thinking_text, visible_text)`` pair, each stripped of surrounding
        whitespace; either may be empty.
    """
    content = (message.get("content") or "").strip()
    reasoning = (message.get("reasoning_content") or "").strip()
    if reasoning:
        return reasoning, content

    closed = _THINK_CLOSED_RE.findall(content)
    if closed:
        visible = _THINK_CLOSED_RE.sub("", content).strip()
        return "".join(closed).strip(), visible
    if "<think>" in content:
        open_match = _THINK_OPEN_RE.search(content)
        thinking = open_match.group(1).strip() if open_match else ""
        return thinking, ""
    return "", content


def think_share(thinking: str, visible: str) -> float:
    """Return the character share of thinking in the total generation (0.0–1.0).

    Args:
        thinking: Extracted thinking text.
        visible: Extracted user-visible text.

    Returns:
        ``len(thinking) / (len(thinking) + len(visible))``, or ``0.0`` when the
        total generation is empty.
    """
    total = len(thinking) + len(visible)
    if total == 0:
        return 0.0
    return len(thinking) / total


def estimate_think_tokens(completion_tokens: int | None, share: float) -> int | None:
    """Apportion provider completion tokens to thinking by the char share.

    Args:
        completion_tokens: Provider-reported completion tokens, or ``None`` if
            the backend did not report usage.
        share: The thinking char share from :func:`think_share`.

    Returns:
        Estimated thinking tokens, or ``None`` when ``completion_tokens`` is
        ``None`` (unknowable — never silently coerced to zero).
    """
    if completion_tokens is None:
        return None
    return round(completion_tokens * share)


def percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Return the ``pct`` percentile of a pre-sorted sequence (top-index clamped).

    Args:
        sorted_vals: Ascending-sorted values.
        pct: Percentile in ``[0.0, 1.0]``.

    Returns:
        The percentile value, or ``0.0`` for an empty sequence.
    """
    if not sorted_vals:
        return 0.0
    idx = int(pct * len(sorted_vals))
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def summarize_tokens(values: Sequence[float]) -> dict[str, float]:
    """Summarise a token distribution into the fields the research note quotes.

    Args:
        values: Per-turn token counts.

    Returns:
        Mapping with ``n``, ``median``, ``p90``, ``max``, ``mean`` (zeros when
        empty).
    """
    if not values:
        return {"n": 0, "median": 0, "p90": 0, "max": 0, "mean": 0}
    ordered = sorted(float(v) for v in values)
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "p90": percentile(ordered, 0.9),
        "max": max(ordered),
        "mean": statistics.mean(ordered),
    }


# ── Substrate I/O (read-only) ─────────────────────────────────────────────────


async def fetch_target_traces(dsn: str) -> list[dict[str, Any]]:
    """Read turn-level SINGLE conversational/memory_recall rows from the ledger.

    Args:
        dsn: asyncpg-compatible DSN for the route-trace Postgres.

    Returns:
        Per-turn dicts with ``trace_id``, ``task_type``, ``output_tokens``
        (ledger proxy), ``final_reply_chars`` and ``tool_iteration_count``.
    """
    # fre-375-allow: read-only research probe; DSN comes from settings, no prod URI hardcoded.
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT trace_id, task_type, output_tokens, final_reply_chars,
                   tool_iteration_count
            FROM route_traces
            WHERE task_id IS NULL AND decomposition_strategy = 'single'
              AND task_type = ANY($1::text[])
            ORDER BY created_at DESC
            """,
            list(_TARGET_TASK_TYPES),
        )
    finally:
        await conn.close()
    # ``trace_id`` is a UUID from asyncpg; stringify so it is JSON-serialisable for
    # the ES query and matches the string keys ES aggregations return.
    return [{**dict(r), "trace_id": str(r["trace_id"])} for r in rows]


async def fetch_live_output_tokens(
    client: httpx.AsyncClient, es_url: str, trace_ids: Sequence[str]
) -> dict[str, int]:
    """Sum authoritative ``model_call_completed`` output tokens per trace (ES).

    Args:
        client: Shared async HTTP client.
        es_url: Elasticsearch base URL.
        trace_ids: Trace ids to aggregate.

    Returns:
        ``{trace_id: summed_output_tokens}`` for the primary-role calls of each
        trace (traces with no indexed calls are absent).
    """
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"event_type": "model_call_completed"}},
                    {"term": {"role": "primary"}},
                    {"terms": {"trace_id": list(trace_ids)}},
                ]
            }
        },
        "aggs": {
            "by_trace": {
                "terms": {"field": "trace_id", "size": len(trace_ids) or 1},
                "aggs": {"out": {"sum": {"field": "output_tokens"}}},
            }
        },
    }
    resp = await client.post(f"{es_url}/agent-logs-*/_search", json=body)
    resp.raise_for_status()
    buckets = resp.json()["aggregations"]["by_trace"]["buckets"]
    return {b["key"]: int(b["out"]["value"]) for b in buckets}


async def fetch_stimulus(client: httpx.AsyncClient, es_url: str, trace_id: str) -> str | None:
    """Recover a turn's raw ``user_message`` from the captures index (ES).

    Args:
        client: Shared async HTTP client.
        es_url: Elasticsearch base URL.
        trace_id: Trace id to look up.

    Returns:
        The stimulus text, or ``None`` if no capture carries it.
    """
    body = {
        "size": 1,
        "query": {"term": {"trace_id": trace_id}},
        "_source": ["user_message"],
    }
    resp = await client.post(f"{es_url}/agent-captains-captures-*/_search", json=body)
    resp.raise_for_status()
    hits = resp.json()["hits"]["hits"]
    for hit in hits:
        msg = hit["_source"].get("user_message")
        if msg:
            return str(msg)
    return None


async def replay_stimulus(
    client: httpx.AsyncClient,
    slm_url: str,
    model: str,
    headers: dict[str, str],
    stimulus: str,
    max_tokens: int,
) -> dict[str, Any] | None:
    """Replay one stimulus to the thinking model and measure the think split.

    Args:
        client: Shared async HTTP client.
        slm_url: SLM ``/v1`` base URL.
        model: Primary model id to target.
        headers: Auth headers (CF Access service token).
        stimulus: The user message to replay (bare, no grounding context).
        max_tokens: Completion cap.

    Returns:
        A result dict (stimulus preview, completion/prompt tokens, think/visible
        chars, think share, estimated thinking tokens), or ``None`` on failure.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": stimulus}],
        "temperature": 0.6,
        "top_p": 0.95,
        "max_tokens": max_tokens,
    }
    try:
        resp = await client.post(f"{slm_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("replay_failed", stimulus=stimulus[:60], error=str(exc))
        return None
    message = data["choices"][0]["message"]
    usage = data.get("usage", {})
    thinking, visible = extract_think_visible(message)
    share = think_share(thinking, visible)
    completion_tokens = usage.get("completion_tokens")
    return {
        "stimulus": stimulus[:120],
        "completion_tokens": completion_tokens,
        "prompt_tokens": usage.get("prompt_tokens"),
        "think_chars": len(thinking),
        "visible_chars": len(visible),
        "think_share": round(share, 3),
        "est_think_tokens": estimate_think_tokens(completion_tokens, share),
    }


def _select_replay_targets(
    traces: Sequence[dict[str, Any]], stimuli: dict[str, str], want: int
) -> list[tuple[str, str]]:
    """Pick up to ``want`` text-only stimuli spread across the token range.

    Vision turns are dropped (a bare replay of "describe the photo" is
    meaningless), then targets are sampled evenly across the ledger
    ``output_tokens`` ordering so the set spans trivial→heavier turns.

    Args:
        traces: Target ledger rows (already newest-first).
        stimuli: ``{trace_id: user_message}`` recovered from captures.
        want: Desired number of replay targets.

    Returns:
        ``[(trace_id, stimulus), ...]`` in ascending ledger-output order.
    """
    eligible = [
        (t["trace_id"], stimuli[t["trace_id"]], t.get("output_tokens") or 0)
        for t in traces
        if t["trace_id"] in stimuli and not _VISION_RE.search(stimuli[t["trace_id"]])
    ]
    if not eligible:
        return []
    eligible.sort(key=lambda e: e[2])
    if len(eligible) <= want:
        return [(tid, s) for tid, s, _ in eligible]
    step = len(eligible) / want
    picked = [eligible[min(int(i * step), len(eligible) - 1)] for i in range(want)]
    return [(tid, s) for tid, s, _ in picked]


async def main() -> None:
    """Run the three-way probe and print + optionally persist the report."""
    parser = argparse.ArgumentParser(description="FRE-432 Phase-0 thinking-token probe")
    parser.add_argument("--pg-dsn", default=None, help="route_traces DSN (default: settings)")
    parser.add_argument("--es-url", default=None, help="Elasticsearch URL (default: settings)")
    parser.add_argument("--slm-url", default=None, help="SLM /v1 base URL (default: settings)")
    parser.add_argument("--model", default="unsloth/qwen3.6-35-A3B", help="primary model id")
    parser.add_argument("--replay-n", type=int, default=12, help="replay sample size")
    parser.add_argument("--max-tokens", type=int, default=4096, help="replay completion cap")
    parser.add_argument("--no-replay", action="store_true", help="skip the inference replays")
    parser.add_argument("--out", default=None, help="write the full JSON report to this path")
    args = parser.parse_args()

    settings = get_settings()
    pg_dsn = (args.pg_dsn or settings.database_url).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    es_url = (args.es_url or settings.elasticsearch_url).rstrip("/")
    slm_url = (args.slm_url or settings.llm_base_url).rstrip("/")

    traces = await fetch_target_traces(pg_dsn)
    log.info("target_population", n=len(traces))

    report: dict[str, Any] = {"population_n": len(traces)}

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        trace_ids = [t["trace_id"] for t in traces]
        live = await fetch_live_output_tokens(client, es_url, trace_ids)

        # Contrast: ledger proxy (undercounts) vs ES-authoritative live totals.
        ledger_out = [t["output_tokens"] or 0 for t in traces]
        live_out = list(live.values())
        report["ledger_proxy_output_tokens"] = summarize_tokens(ledger_out)
        report["live_authoritative_output_tokens"] = summarize_tokens(live_out)
        report["ledger_undercount_traces"] = sum(
            1 for t in traces if (t["output_tokens"] or 0) < live.get(t["trace_id"], 0)
        )

        by_type: dict[str, dict[str, float]] = {}
        for tt in _TARGET_TASK_TYPES:
            vals = [
                live[t["trace_id"]]
                for t in traces
                if t["task_type"] == tt and t["trace_id"] in live
            ]
            by_type[tt] = summarize_tokens(vals)
        report["live_output_tokens_by_task_type"] = by_type

        replays: list[dict[str, Any]] = []
        if not args.no_replay:
            headers = _cf_headers()
            if headers is None:
                log.warning("replay_skipped_no_cf_token")
            else:
                stimuli = {}
                for t in traces:
                    text = await fetch_stimulus(client, es_url, t["trace_id"])
                    if text:
                        stimuli[t["trace_id"]] = text
                targets = _select_replay_targets(traces, stimuli, args.replay_n)
                log.info("replay_targets_selected", n=len(targets))
                for tid, stimulus in targets:  # serial: primary is single-concurrency
                    result = await replay_stimulus(
                        client, slm_url, args.model, headers, stimulus, args.max_tokens
                    )
                    if result:
                        result["trace_id"] = tid
                        replays.append(result)
                        log.info(
                            "replay_done",
                            completion_tokens=result["completion_tokens"],
                            think_share=result["think_share"],
                        )
        report["replays"] = replays
        if replays:
            report["replay_think_share_median"] = statistics.median(
                r["think_share"] for r in replays
            )

    _print_report(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log.info("report_written", path=args.out)


def _cf_headers() -> dict[str, str] | None:
    """Return CF Access service-token headers from the environment, or ``None``.

    Reads ``CF_ACCESS_CLIENT_ID`` / ``CF_ACCESS_CLIENT_SECRET`` (the same infra
    service token the gateway uses) — never any user identity.
    """
    cid = os.environ.get("CF_ACCESS_CLIENT_ID") or os.environ.get("AGENT_CF_ACCESS_CLIENT_ID")
    csec = os.environ.get("CF_ACCESS_CLIENT_SECRET") or os.environ.get(
        "AGENT_CF_ACCESS_CLIENT_SECRET"
    )
    if not cid or not csec:
        return None
    return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}


def _print_report(report: dict[str, Any]) -> None:
    """Print the human-readable probe summary."""
    print("\n=== FRE-432 Phase-0 thinking-token probe ===")
    print(
        f"target population (SINGLE conversational+memory_recall turns): n={report['population_n']}"
    )

    ledger = report["ledger_proxy_output_tokens"]
    live = report["live_authoritative_output_tokens"]
    print("\n-- output_tokens per turn --")
    print(
        f"  ledger proxy (route_traces): "
        f"median={ledger['median']:.0f} p90={ledger['p90']:.0f} max={ledger['max']:.0f}  [UNDERCOUNTS]"
    )
    print(
        f"  live authoritative (ES):     "
        f"median={live['median']:.0f} p90={live['p90']:.0f} max={live['max']:.0f}"
    )
    print(f"  turns where ledger < live (undercounted): {report['ledger_undercount_traces']}")

    print("\n-- live output_tokens by task_type --")
    for tt, s in report["live_output_tokens_by_task_type"].items():
        print(
            f"  {tt:>14}: n={s['n']:.0f} median={s['median']:.0f} p90={s['p90']:.0f} max={s['max']:.0f}"
        )

    replays = report.get("replays", [])
    if replays:
        print(
            f"\n-- direct replays (bare stimulus, UPPER BOUND on live thinking): n={len(replays)} --"
        )
        print(f"  median think_share = {report['replay_think_share_median']:.0%}")
        for r in replays:
            est = r["est_think_tokens"]
            print(
                f"  ct={str(r['completion_tokens']):>5} "
                f"think={r['think_share']:.0%} "
                f"est_think_tok={est if est is not None else 'NA':>5} | {r['stimulus'][:60]!r}"
            )


if __name__ == "__main__":
    asyncio.run(main())
