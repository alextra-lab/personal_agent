#!/usr/bin/env python3
"""FRE-802: measure live entity-extraction token cost + prompt-cache behaviour.

Measure-first cost investigation on the LIVE entity extractor
(``second_brain/entity_extraction.py``, role ``entity_extraction`` →
``gpt-5.4-mini`` on OpenAI). Answers the two ticket questions with reproducible
numbers so a no-change / restructure verdict rests on evidence, not assumption:

1. **Prompt assembly order** — measured directly from the live source constants.
   Reports the cacheable static prefix (system + instructions + few-shot, all of
   which precede the first variable token) and the static JSON-schema footer that
   sits AFTER the variable turn content and is therefore structurally uncacheable.

2. **Live cache accounting** — aggregates the ``model_call_completed`` events for
   ``openai/gpt-5.4-mini`` (exclusively the entity_extraction role per
   ``config/model_roles.yaml``) and reports the input-token distribution, the
   OpenAI automatic-cache hit rate (``cache_read_tokens > 0``), the cached share
   when warm, and per-call cost.

The V2 10-type GoLLIE prompt (FRE-771) went live 2026-07-02, roughly doubling the
extraction input size; ``--since`` defaults to that cutover so the cache stats
describe the *current* prompt regime rather than a mix of two prompts.

Usage:
    uv run python scripts/research/fre802_extraction_cache_probe.py
    uv run python scripts/research/fre802_extraction_cache_probe.py --since 2026-07-02 --es http://localhost:9200

No writes; read-only against source constants and the live ES logs index.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import urllib.request
from typing import Any

from personal_agent.config import settings
from personal_agent.second_brain import entity_extraction as ee

# OpenAI gpt-5.4-mini is exclusively the entity_extraction role (model_roles.yaml:
# captains_log/insights → claude_sonnet, primary → claude). So this model tag on a
# model_call_completed event uniquely identifies a live extraction call.
_EXTRACTION_MODEL = "openai/gpt-5.4-mini"
_V2_CUTOVER = "2026-07-02"  # FRE-771 V2 GoLLIE prompt went live (see daily input-size step).


def _tok() -> tuple[Any, str]:
    """Return a token-count callable and the encoder label.

    Prefers ``tiktoken`` with the gpt-5-series ``o200k_base`` encoding; falls back
    to a chars/4 estimate when tiktoken is unavailable.

    Returns:
        A ``(count_fn, label)`` pair where ``count_fn(str) -> int``.
    """
    try:
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        return (lambda s: len(enc.encode(s)), "tiktoken o200k_base")
    except Exception:  # pragma: no cover - offline fallback
        return (lambda s: max(1, len(s) // 4), "chars/4 estimate")


def measure_prompt_components() -> dict[str, Any]:
    """Measure the extraction prompt's cacheable prefix vs. uncacheable footer.

    Splits the live template at the first variable token (``{user_message}``): the
    head (system + instructions + optional few-shot) is the OpenAI-cacheable prefix;
    the JSON-schema tail after ``{assistant_response}`` is static but positioned
    after the variable turn, so it can never join the cached prefix.

    Returns:
        A dict of per-component token counts and the derived prefix/footer sizes.
    """
    count, unit = _tok()
    sys_p = ee._EXTRACTION_SYSTEM_PROMPT
    tmpl = ee._EXTRACTION_PROMPT_TEMPLATE
    few = ee._EXTRACTION_FEWSHOT_EXEMPLARS

    head = tmpl[: tmpl.index("{user_message}")]
    tail = tmpl[tmpl.index("{user_message}") :]
    footer = tail[tail.index("{assistant_response}") + len("{assistant_response}") :]

    head_off = head.replace("{fewshot_exemplars}", "")
    head_on = head.replace("{fewshot_exemplars}", few)

    return {
        "unit": unit,
        "system_prompt": count(sys_p),
        "static_head_fewshot_off": count(head_off),
        "static_head_fewshot_on": count(head_on),
        "fewshot_block": count(few),
        "static_footer_after_variable": count(footer),
        "cacheable_prefix_fewshot_off": count(sys_p) + count(head_off),
        "cacheable_prefix_fewshot_on": count(sys_p) + count(head_on),
        "fewshot_enabled_in_config": settings.entity_extraction_fewshot_exemplars_enabled,
    }


def query_live_cache_stats(es_url: str, since: str) -> dict[str, Any]:
    """Aggregate live extraction cache accounting from ``model_call_completed``.

    Args:
        es_url: Base Elasticsearch URL (e.g. ``http://localhost:9200``).
        since: ISO date lower bound (inclusive) for ``@timestamp``.

    Returns:
        A dict of summary statistics, or ``{"error": ...}`` if ES is unreachable.
    """
    body = {
        "size": 500,
        "_source": ["input_tokens", "output_tokens", "cache_read_tokens", "cost_usd"],
        "query": {
            "bool": {
                "filter": [
                    {"term": {"event_type": "model_call_completed"}},
                    {"term": {"model": _EXTRACTION_MODEL}},
                    {"range": {"@timestamp": {"gte": since}}},
                ]
            }
        },
    }
    req = urllib.request.Request(
        f"{es_url}/agent-logs-*/_search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - trusted local ES
            payload = json.load(resp)
    except Exception as exc:  # pragma: no cover - network dependent
        return {"error": f"{type(exc).__name__}: {exc}"}

    rows = [h["_source"] for h in payload.get("hits", {}).get("hits", [])]
    if not rows:
        return {"n": 0}

    def g(row: dict[str, Any], key: str) -> float:
        val = row.get(key)
        return val if isinstance(val, (int, float)) else 0

    inp = [g(r, "input_tokens") for r in rows]
    crd = [g(r, "cache_read_tokens") for r in rows]
    out = [g(r, "output_tokens") for r in rows]
    cost = [g(r, "cost_usd") for r in rows]
    hit_shares = [c / i for i, c in zip(inp, crd, strict=False) if c > 0 and i]
    hits = sum(1 for c in crd if c > 0)

    return {
        "n": len(rows),
        "input_min": min(inp),
        "input_median": st.median(inp),
        "input_mean": st.mean(inp),
        "input_max": max(inp),
        "cache_hits": hits,
        "cache_hit_rate": hits / len(rows),
        "cache_read_values_when_hit": sorted({int(c) for c in crd if c > 0}),
        "cached_share_when_hit_median": st.median(hit_shares) if hit_shares else 0.0,
        "cached_share_when_hit_max": max(hit_shares) if hit_shares else 0.0,
        "output_median": st.median(out),
        "cost_per_call_mean": st.mean(cost),
        "cost_window_total": sum(cost),
    }


def main() -> None:
    """Print the prompt-component measurement and the live cache statistics."""
    parser = argparse.ArgumentParser(description="FRE-802 extraction prompt-cache probe")
    parser.add_argument(
        "--es",
        default="http://localhost:9200",  # fre-375-allow: read-only live-telemetry probe; queries prod ES logs, never writes
        help="Elasticsearch base URL",
    )
    parser.add_argument(
        "--since", default=_V2_CUTOVER, help="ISO date lower bound for the cache window"
    )
    args = parser.parse_args()

    comp = measure_prompt_components()
    print(f"=== Prompt components [{comp['unit']}] ===")
    print(f"  system prompt:                 {comp['system_prompt']:5d} tok")
    print(f"  static head (few-shot OFF):     {comp['static_head_fewshot_off']:5d} tok")
    print(f"  static head (few-shot ON):      {comp['static_head_fewshot_on']:5d} tok")
    print(f"    few-shot block alone:        {comp['fewshot_block']:5d} tok")
    print(
        f"  static footer AFTER variable:   {comp['static_footer_after_variable']:5d} tok  <- structurally uncacheable"
    )
    print(f"  => cacheable prefix (few-shot OFF): {comp['cacheable_prefix_fewshot_off']:5d} tok")
    print(f"  => cacheable prefix (few-shot ON):  {comp['cacheable_prefix_fewshot_on']:5d} tok")
    print(f"  few-shot enabled in config: {comp['fewshot_enabled_in_config']}")
    print("  (OpenAI auto-cache threshold: ~1024 tok — the prefix is well above it)")

    print(f"\n=== Live cache stats (model={_EXTRACTION_MODEL}, since {args.since}) ===")
    stats = query_live_cache_stats(args.es, args.since)
    if "error" in stats:
        print(f"  ES unreachable: {stats['error']}")
        return
    if stats["n"] == 0:
        print("  no extraction calls in window")
        return
    print(f"  N calls:            {stats['n']}")
    print(
        f"  input_tokens:       min={stats['input_min']:.0f} median={stats['input_median']:.0f} "
        f"mean={stats['input_mean']:.0f} max={stats['input_max']:.0f}"
    )
    print(
        f"  cache hit rate:     {stats['cache_hits']}/{stats['n']} ({100 * stats['cache_hit_rate']:.0f}%)"
    )
    print(f"  cache_read (warm):  {stats['cache_read_values_when_hit']}")
    print(
        f"  cached share (warm): median={stats['cached_share_when_hit_median']:.2f} "
        f"max={stats['cached_share_when_hit_max']:.2f}"
    )
    print(f"  output_tokens:      median={stats['output_median']:.0f}")
    print(
        f"  cost/call:          mean=${stats['cost_per_call_mean']:.5f}  "
        f"window total=${stats['cost_window_total']:.4f}"
    )


if __name__ == "__main__":
    main()
