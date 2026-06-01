"""FRE-433 — cross-turn KV-cache A/B harness (drives the gateway end-to-end).

Runs a multi-turn dataset through the live gateway ``/chat`` endpoint and reads
back per-turn cache telemetry from Elasticsearch, so the two prompt layouts can
be compared on the SAME stack:

* **arm A** (``head``): volatile block (recalled memory + skill bodies) in the
  system HEAD — current ``main`` behaviour.
* **arm B** (``tail``): volatile block at the message TAIL — gateway running with
  ``AGENT_CACHE_VOLATILE_TAIL_LAYOUT=true`` (branch ``codex/fre-433-layout-tail-arm``).

The arm is determined by *what is deployed* in the gateway; this harness only
**tags** the run (``--arm``) and drives traffic. The backend (local SLM vs cloud
Sonnet) is selected per-request via ``--profile`` — no redeploy needed to switch
backends.

Headline metric: ``cache_read_tokens`` on the **first full-context** model call
of each turn >= 2 (cross-turn reuse). Arm A expectation ~0; arm B expectation > 0.

Usage::

    uv run python scripts/eval/fre433_cache_ab/harness.py \\
        --run-id ab-2026-06-01 --arm head --profile local \\
        --dataset scripts/eval/fre433_cache_ab/dataset.yaml \\
        --auth-email <loopback-eval-email>

Run the four passes (arm A then redeploy → arm B), each with --profile local and
--profile cloud, then diff the four result files. See README.md for the protocol.
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
# Skip small auxiliary primary calls (intent/router ~hundreds of tokens) when
# identifying the turn's first *full-context* orchestrator call.
DEFAULT_MIN_PROMPT_TOKENS = 3000


@dataclass(frozen=True)
class SessionDef:
    """A multi-turn conversation run under a single session_id.

    Attributes:
        label: Human-readable session identifier (tag in the report).
        note: Why this session exposes the behaviour.
        turns: Ordered user messages; each is one turn.
    """

    label: str
    note: str
    turns: tuple[str, ...]


@dataclass
class TurnMetric:
    """Cache telemetry for one turn's first full-context model call.

    Attributes:
        session_label: Owning session label.
        turn_index: 1-based turn number within the session.
        trace_id: Trace id returned by /chat for the turn.
        session_id: Session id (constant across the session's turns).
        endpoint: Backend endpoint of the captured call.
        model: Model id of the captured call.
        input_tokens: Prompt tokens on the captured call.
        cache_read_tokens: Cross-turn reuse signal (None ⇒ 0 / not emitted).
        cache_creation_input_tokens: Cloud cache-write tokens (Anthropic).
        latency_ms: Call latency.
        static_prefix_hash: Stable-prefix hash (should be constant across turns).
        dynamic_hash: Full-prompt hash (changes when the volatile block changes).
        primary_call_count: Number of primary model calls observed in the trace.
    """

    session_label: str
    turn_index: int
    trace_id: str
    session_id: str
    endpoint: str | None
    model: str | None
    input_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_input_tokens: int | None
    latency_ms: int | None
    static_prefix_hash: str | None
    dynamic_hash: str | None
    primary_call_count: int


def load_dataset(path: Path) -> list[SessionDef]:
    """Load the A/B dataset from YAML.

    Args:
        path: Path to ``dataset.yaml``.

    Returns:
        Ordered list of session definitions.

    Raises:
        ValueError: If the file has no ``sessions`` list.
    """
    raw = yaml.safe_load(path.read_text())
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not sessions:
        raise ValueError(f"No 'sessions' found in {path}")
    return [
        SessionDef(
            label=str(s["label"]),
            note=str(s.get("note", "")),
            turns=tuple(str(t) for t in s["turns"]),
        )
        for s in sessions
    ]


async def call_chat(
    client: httpx.AsyncClient,
    chat_url: str,
    message: str,
    session_id: str | None,
    auth_email: str | None,
    profile: str,
) -> tuple[str, str, str]:
    """POST one turn to /chat and return ``(response, session_id, trace_id)``.

    Mirrors ``scripts/eval/recovery_harness.py``: ``profile`` selects the backend
    and the CF-Access email header impersonates an authenticated user for the
    loopback diagnostic call.

    Args:
        client: Shared async HTTP client.
        chat_url: Gateway /chat URL.
        message: Turn message.
        session_id: Prior session id, or None to start a new session.
        auth_email: CF-Access email to stamp, or None.
        profile: Model profile (``local`` or ``cloud``).

    Returns:
        Tuple of response text, session id, and trace id.
    """
    params = {"message": message, "profile": profile, "channel": "EVAL"}
    if session_id is not None:
        params["session_id"] = session_id
    headers: dict[str, str] = {}
    if auth_email:
        headers["Cf-Access-Authenticated-User-Email"] = auth_email
    resp = await client.post(chat_url, params=params, headers=headers, timeout=600.0)
    resp.raise_for_status()
    data = resp.json()
    return (
        str(data.get("response", "")),
        str(data["session_id"]),
        str(data["trace_id"]),
    )


async def _es_search(
    client: httpx.AsyncClient, es_url: str, index: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Run an ES ``_search`` and return the parsed response."""
    resp = await client.post(f"{es_url}/{index}/_search", json=body, timeout=30.0)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def wait_for_trace(
    client: httpx.AsyncClient, es_url: str, index: str, trace_id: str, timeout_s: float
) -> bool:
    """Poll ES until a ``model_call_completed`` exists for ``trace_id``.

    Args:
        client: Async HTTP client.
        es_url: Elasticsearch base URL.
        index: Index pattern to search.
        trace_id: Trace id to wait for.
        timeout_s: Hard polling timeout in seconds.

    Returns:
        True if a completed model call was indexed before the timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    body = {
        "size": 0,
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id.keyword": trace_id}},
                    {"term": {"event_type": "model_call_completed"}},
                ]
            }
        },
    }
    while asyncio.get_event_loop().time() < deadline:
        r = await _es_search(client, es_url, index, body)
        if r["hits"]["total"]["value"] > 0:
            return True
        await asyncio.sleep(2.0)
    return False


async def fetch_turn_metric(
    client: httpx.AsyncClient,
    es_url: str,
    index: str,
    trace_id: str,
    session_label: str,
    turn_index: int,
    session_id: str,
    min_prompt_tokens: int,
) -> TurnMetric:
    """Extract the turn's first full-context primary model call from ES.

    The orchestrator turn may emit several ``model_call_completed`` events under
    one trace (intent/router, then the primary tool loop). The cross-turn reuse
    signal is the FIRST call that processes the full prompt — i.e. the earliest
    primary call whose ``input_tokens`` >= ``min_prompt_tokens`` (skips the small
    auxiliary intent/router call).

    Args:
        client: Async HTTP client.
        es_url: Elasticsearch base URL.
        index: Index pattern.
        trace_id: Trace id for the turn.
        session_label: Session label for the report.
        turn_index: 1-based turn number.
        session_id: Session id.
        min_prompt_tokens: Threshold separating full-context from auxiliary calls.

    Returns:
        A populated :class:`TurnMetric` (fields may be None if nothing matched).
    """
    body = {
        "size": 50,
        "_source": [
            "@timestamp",
            "role",
            "endpoint",
            "model",
            "input_tokens",
            "cache_read_tokens",
            "cache_creation_input_tokens",
            "latency_ms",
            "prompt_static_prefix_hash",
            "prompt_dynamic_hash",
        ],
        "query": {
            "bool": {
                "must": [
                    {"term": {"trace_id.keyword": trace_id}},
                    {"term": {"event_type": "model_call_completed"}},
                    {"term": {"role": "primary"}},
                ]
            }
        },
        "sort": [{"@timestamp": "asc"}],
    }
    r = await _es_search(client, es_url, index, body)
    hits = [h["_source"] for h in r["hits"]["hits"]]

    chosen: dict[str, Any] | None = None
    for src in hits:
        tok = src.get("input_tokens") or 0
        if tok >= min_prompt_tokens:
            chosen = src
            break
    if chosen is None and hits:
        # No full-context call met the threshold; fall back to the largest.
        chosen = max(hits, key=lambda s: s.get("input_tokens") or 0)

    src = chosen or {}
    return TurnMetric(
        session_label=session_label,
        turn_index=turn_index,
        trace_id=trace_id,
        session_id=session_id,
        endpoint=src.get("endpoint"),
        model=src.get("model"),
        input_tokens=src.get("input_tokens"),
        cache_read_tokens=src.get("cache_read_tokens"),
        cache_creation_input_tokens=src.get("cache_creation_input_tokens"),
        latency_ms=src.get("latency_ms"),
        static_prefix_hash=src.get("prompt_static_prefix_hash"),
        dynamic_hash=src.get("prompt_dynamic_hash"),
        primary_call_count=len(hits),
    )


async def run_session(
    http: httpx.AsyncClient,
    es: httpx.AsyncClient,
    chat_url: str,
    es_url: str,
    index: str,
    session: SessionDef,
    auth_email: str | None,
    profile: str,
    min_prompt_tokens: int,
    trace_timeout_s: float,
) -> list[TurnMetric]:
    """Run one session's turns sequentially and collect per-turn metrics."""
    metrics: list[TurnMetric] = []
    session_id: str | None = None
    for turn_index, message in enumerate(session.turns, start=1):
        _resp, session_id, trace_id = await call_chat(
            http, chat_url, message, session_id, auth_email, profile
        )
        log.info(
            "turn_sent",
            session=session.label,
            turn=turn_index,
            session_id=session_id,
            trace_id=trace_id,
        )
        ok = await wait_for_trace(es, es_url, index, trace_id, trace_timeout_s)
        if not ok:
            log.warning("trace_not_indexed", trace_id=trace_id, turn=turn_index)
        metric = await fetch_turn_metric(
            es,
            es_url,
            index,
            trace_id,
            session.label,
            turn_index,
            session_id or "",
            min_prompt_tokens,
        )
        metrics.append(metric)
        log.info(
            "turn_metric",
            session=session.label,
            turn=turn_index,
            cache_read=metric.cache_read_tokens,
            input_tokens=metric.input_tokens,
            static_hash=metric.static_prefix_hash,
            dynamic_hash=metric.dynamic_hash,
        )
    return metrics


def render_markdown(run_meta: dict[str, Any], metrics: list[TurnMetric]) -> str:
    """Render an A/B-friendly markdown summary for one pass."""
    lines: list[str] = [
        f"# FRE-433 cache A/B pass — {run_meta['run_id']}",
        "",
        f"- **arm**: `{run_meta['arm']}` ({'volatile at TAIL' if run_meta['arm'] == 'tail' else 'volatile in HEAD'})",
        f"- **profile/backend**: `{run_meta['profile']}`",
        f"- **timestamp**: {run_meta['timestamp']}",
        "",
        "Headline = `cache_read_tokens` on the first full-context call of turns >= 2.",
        "",
        "| session | turn | endpoint | in_tok | cache_read | cache_create | latency_ms | static_hash | dynamic_hash |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m in metrics:
        ep = (m.endpoint or "")[-22:]
        lines.append(
            f"| {m.session_label} | {m.turn_index} | {ep} | {m.input_tokens} | "
            f"**{m.cache_read_tokens}** | {m.cache_creation_input_tokens} | {m.latency_ms} | "
            f"{(m.static_prefix_hash or '-')[:12]} | {(m.dynamic_hash or '-')[:12]} |"
        )
    # Cross-turn reuse rollup (turns >= 2 only).
    cross = [m for m in metrics if m.turn_index >= 2]
    reused = [m for m in cross if (m.cache_read_tokens or 0) > 0]
    lines += [
        "",
        f"**Cross-turn reuse (turn>=2): {len(reused)}/{len(cross)} turns had cache_read > 0.**",
        "PASS for arm `tail` = most turn>=2 calls show cache_read > 0 vs ~0 on arm `head`.",
    ]
    return "\n".join(lines)


async def amain(args: argparse.Namespace) -> int:
    """Drive the dataset and write the pass results."""
    settings = get_settings()
    es_url = settings.elasticsearch_url.rstrip("/")
    index = f"{settings.elasticsearch_index_prefix}-*"
    dataset = load_dataset(Path(args.dataset))

    run_meta = {
        "run_id": args.run_id,
        "arm": args.arm,
        "profile": args.profile,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_url": args.chat_url,
    }
    all_metrics: list[TurnMetric] = []
    async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
        # Probe gateway health before driving traffic.
        try:
            h = await http.get(args.chat_url.replace("/chat", "/health"), timeout=10.0)
            log.info("gateway_health", status=h.status_code)
        except httpx.HTTPError as exc:
            log.error("gateway_unreachable", url=args.chat_url, error=str(exc))
            return 2
        for session in dataset:
            metrics = await run_session(
                http,
                es,
                args.chat_url,
                es_url,
                index,
                session,
                args.auth_email,
                args.profile,
                args.min_prompt_tokens,
                args.trace_timeout_s,
            )
            all_metrics.extend(metrics)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.run_id}_{args.arm}_{args.profile}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps({"meta": run_meta, "metrics": [asdict(m) for m in all_metrics]}, indent=2)
    )
    (out_dir / f"{stem}.md").write_text(render_markdown(run_meta, all_metrics))
    log.info("pass_written", out=str(out_dir / f"{stem}.md"), turns=len(all_metrics))
    return 0


def main() -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(description="FRE-433 cross-turn cache A/B harness")
    p.add_argument("--run-id", required=True, help="Run identifier (tag in output).")
    p.add_argument(
        "--arm",
        required=True,
        choices=["head", "tail"],
        help="Layout arm the gateway is deployed with (metadata tag).",
    )
    p.add_argument(
        "--profile", default="local", choices=["local", "cloud"], help="Model profile/backend."
    )
    p.add_argument(
        "--dataset",
        default="scripts/eval/fre433_cache_ab/dataset.yaml",
        help="Path to the exposure dataset YAML.",
    )
    p.add_argument("--chat-url", default=DEFAULT_CHAT_URL, help="Gateway /chat URL.")
    p.add_argument(
        "--auth-email", default=None, help="CF-Access email to impersonate for loopback calls."
    )
    p.add_argument(
        "--out", default="telemetry/evaluation/fre433-cache-ab", help="Output directory."
    )
    p.add_argument(
        "--min-prompt-tokens",
        type=int,
        default=DEFAULT_MIN_PROMPT_TOKENS,
        help="Min input_tokens to count a call as the turn's full-context call.",
    )
    p.add_argument(
        "--trace-timeout-s", type=float, default=60.0, help="Per-turn ES indexing wait timeout."
    )
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
