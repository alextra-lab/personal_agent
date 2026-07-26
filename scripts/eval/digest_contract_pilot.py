"""FRE-996 — does a structured-output contract remove the digest's parse failures?

Runs the same real sessions through the digest generation call three ways and classifies
every reply mechanically:

    A  today's call — the JSON shape described in prose, nothing enforced
    B  the wire schema as a forced tool call
    C  the wire schema as a forced tool call, with per-slot item ceilings

**What this can and cannot show, stated up front.** The contract makes fence wrapping and
trailing prose structurally impossible, because the payload arrives in a tool-call
argument field rather than as free text somebody has to unwrap. It does *not* prevent
truncation: a schema-constrained generation cut off at the output ceiling is still a
fragment. Anthropic's strict tool use — which would make shape and enum conformance a
guarantee rather than a strong tendency — is not reachable through litellm 1.89.2, so
those stay measured classes here rather than assumed-solved ones. A run in which wrapping
goes to zero, drift falls sharply, and truncation persists is the expected result and a
success; see ``docs/superpowers/plans/2026-07-26-fre-996-digest-json-contract.md`` §1.

The producer stays disabled throughout. This calls the model directly and never touches
``generate_session_digest``, so no session is marked clean and nothing is written to any
substrate. It reads the durable captures index and calls the model. That is all.

Costs real money. Estimates first, and refuses to spend without ``--confirm-spend``::

    uv run python scripts/eval/digest_contract_pilot.py                    # estimate only
    uv run python scripts/eval/digest_contract_pilot.py --confirm-spend    # run it
    uv run python scripts/eval/digest_contract_pilot.py --sample-size 10 --arms A,B
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import orjson

from personal_agent.captains_log.capture import (
    CAPTURES_INDEX_PREFIX,
    SUBAGENT_CAPTURES_INDEX_PREFIX,
    TaskCapture,
)
from personal_agent.config import load_model_config, resolve_role_model_key
from personal_agent.llm_client import ModelRole
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.memory.session_digest import digest_token_count
from personal_agent.memory.session_digest_wire import (
    DIGEST_TOOL_NAME,
    DigestEnvelope,
    digest_tool,
    digest_tool_choice,
)
from personal_agent.second_brain.session_summary import (
    _MAX_OUTPUT_TOKENS,
    MIN_TURNS_FOR_DIGEST,
    _strip_fences,
    _system_prompt,
    build_prompt,
    parse_model_output,
)
from personal_agent.telemetry.trace import SystemTraceContext

#: Arms, and the tool each one sends. ``None`` is the prose-only control.
_ARMS: dict[str, list[dict[str, Any]] | None] = {
    "A": None,
    "B": [digest_tool()],
    "C": [digest_tool(bounded=True)],
}

#: Every outcome a reply can be assigned. Mutually exclusive and exhaustive, so the
#: counts add up to the call count — a classifier with an implicit "everything else"
#: bucket is how a truncated reply gets quietly scored as clean.
_OUTCOMES = (
    "ok",
    "ok_at_ceiling",
    "truncated",
    "invalid_json",
    "shape_drift",
    "enum_drift",
    "empty",
    "provider_error",
)

_ENUM_ERROR_MARKERS = ("invalid basis", "invalid tier")

#: Consecutive failures that mean the harness is broken rather than the model failing.
_ABORT_AFTER_CONSECUTIVE_ERRORS = 3


# ── Corpus ────────────────────────────────────────────────────────────────────


async def _es_client() -> Any:
    from elasticsearch import AsyncElasticsearch

    from personal_agent.config.settings import get_settings

    return AsyncElasticsearch(hosts=[get_settings().elasticsearch_url])


async def _sessions_with_enough_turns(es: Any, *, limit: int) -> list[str]:
    """Session ids holding at least :data:`MIN_TURNS_FOR_DIGEST` captures."""
    index = f"{CAPTURES_INDEX_PREFIX}-*,-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"
    response = await es.search(
        index=index,
        size=0,
        aggs={
            "sessions": {
                "terms": {"field": "session_id", "size": limit},
                "aggs": {"turns": {"value_count": {"field": "trace_id"}}},
            }
        },
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    buckets = response.get("aggregations", {}).get("sessions", {}).get("buckets", [])
    return [b["key"] for b in buckets if b["doc_count"] >= MIN_TURNS_FOR_DIGEST]


async def _load_captures(es: Any, session_id: str) -> list[TaskCapture]:
    index = f"{CAPTURES_INDEX_PREFIX}-*,-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"
    response = await es.search(
        index=index,
        query={"bool": {"filter": [{"term": {"session_id": session_id}}]}},
        sort=[{"timestamp": "asc"}, {"trace_id": "asc"}],
        size=1000,
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    captures: list[TaskCapture] = []
    for hit in response.get("hits", {}).get("hits", []):
        source = hit.get("_source")
        if isinstance(source, dict) and source.get("session_id") == session_id:
            try:
                captures.append(TaskCapture(**source))
            except Exception:  # noqa: BLE001 — a bad document is skipped, never fatal
                continue
    return captures


def _sample(session_ids: list[str], *, size: int) -> list[str]:
    """Pick a stable sample.

    Ordered by a hash of the id rather than by the id itself: sorting lexicographically
    would over-select whatever prefix the id scheme happens to favour, and the sample
    must be arbitrary *and* reproducible so all three arms see identical sessions.
    """
    ranked = sorted(session_ids, key=lambda s: hashlib.blake2b(s.encode(), digest_size=8).digest())
    return ranked[:size]


# ── One call ──────────────────────────────────────────────────────────────────


async def _dispatch(prompt: str, *, arm: str, role_name: str, session_id: str) -> dict[str, Any]:
    """Run one arm's call. Mirrors the producer's dispatch, minus the retry loop."""
    from personal_agent.llm_client.factory import get_llm_client_for_key

    tools = _ARMS[arm]
    # Billed to `study`, not `captains_log`. That lane exists precisely for a one-off
    # corpus run (FRE-839) and keeps this pilot from contending with the live digest
    # lane's daily cap — or from polluting the `captains_log` cost series the FRE-995
    # audit measured the digest's real spend from. Its `on_denial: raise` also surfaces
    # a budget denial immediately, which is what a one-shot script wants.
    client = get_llm_client_for_key(role_name, budget_role="study")
    return await client.respond(
        role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": prompt}],
        system_prompt=_system_prompt(),
        tools=tools,
        tool_choice=digest_tool_choice() if tools else None,
        max_tokens=_MAX_OUTPUT_TOKENS,
        # Temperature is deliberately NOT pinned, because it cannot be: claude-sonnet-5
        # rejects any value but 1 ("does not support temperature=0.0", litellm
        # UnsupportedParamsError). So the arms carry sampling variance that no amount of
        # harness care removes, and the per-class rates below are not deterministic
        # counterfactuals. This is why the mechanism argument is the primary evidence for
        # the wrapping claim and the sample is only corroboration — a distinction the
        # results write-up has to keep, not quietly drop.
        trace_ctx=SystemTraceContext.new("digest_contract_pilot", session_id=session_id),
    )


def _payload(response: dict[str, Any]) -> tuple[str, bool]:
    """The digest JSON, and whether it arrived as free text needing an unwrap."""
    for call in response.get("tool_calls") or []:
        if call.get("name") == DIGEST_TOOL_NAME and call.get("arguments"):
            return str(call["arguments"]), False

    content = response.get("content", "") or ""
    stripped = _strip_fences(content)
    return stripped, stripped != content.strip()


def _classify(response: dict[str, Any], *, ended_at: datetime) -> dict[str, Any]:
    """Assign exactly one outcome, plus the measurements FRE-994 inherits."""
    payload, wrapped = _payload(response)
    usage = response.get("usage") or {}
    completion_tokens = usage.get("completion_tokens")
    at_ceiling = response.get("finish_reason") in {"length", "max_tokens"} or (
        isinstance(completion_tokens, int) and completion_tokens >= _MAX_OUTPUT_TOKENS
    )

    record: dict[str, Any] = {
        "wrapped": wrapped,
        "at_ceiling": at_ceiling,
        "finish_reason": response.get("finish_reason"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": completion_tokens,
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cost_usd": response.get("cost_usd", 0.0),
        "payload_chars": len(payload),
        "digest_tokens": None,
        # Whether the *wire* model accepts it. The production parser leaves the locator
        # field open, so an off-vocabulary `field` only shows up here.
        "wire_valid": None,
        "error": None,
    }

    if not payload.strip():
        record["outcome"] = "empty"
        return record

    try:
        _, digest = parse_model_output(payload, ended_at=ended_at)
    except ValueError as e:
        detail = str(e)
        record["error"] = detail
        if at_ceiling:
            record["outcome"] = "truncated"
        elif "not valid JSON" in detail:
            record["outcome"] = "invalid_json"
        elif any(marker in detail for marker in _ENUM_ERROR_MARKERS):
            record["outcome"] = "enum_drift"
        else:
            record["outcome"] = "shape_drift"
        return record

    record["digest_tokens"] = digest_token_count(digest)
    try:
        DigestEnvelope.model_validate(orjson.loads(payload))
        record["wire_valid"] = True
    except Exception as e:  # noqa: BLE001 — a secondary signal, never fatal
        record["wire_valid"] = False
        record["error"] = f"wire: {e}"

    # A digest cut off mid-list still parses as a valid, shorter digest. Scoring that as
    # clean is the cheapest way this measurement produces a false success, so it gets its
    # own class rather than being folded into `ok`.
    record["outcome"] = "ok_at_ceiling" if at_ceiling else "ok"
    return record


# ── Cost ──────────────────────────────────────────────────────────────────────


def _estimate_cost(prompts: list[str], *, arms: list[str], model: str) -> float:
    """Worst-case spend: every call billed at the full output ceiling."""
    import litellm

    total = 0.0
    system_tokens = estimate_tokens(_system_prompt())
    for prompt in prompts:
        prompt_tokens = estimate_tokens(prompt) + system_tokens
        for _ in arms:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=_MAX_OUTPUT_TOKENS,
            )
            total += prompt_cost + completion_cost
    return total


# ── Report ────────────────────────────────────────────────────────────────────


def _summarise(records: list[dict[str, Any]], *, arms: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in arms:
        rows = [r for r in records if r["arm"] == arm]
        if not rows:
            continue
        lengths = sorted(r["digest_tokens"] for r in rows if r["digest_tokens"] is not None)
        outputs = sorted(r["completion_tokens"] for r in rows if r["completion_tokens"])
        summary[arm] = {
            "calls": len(rows),
            "outcomes": {k: v for k, v in Counter(r["outcome"] for r in rows).most_common()},
            "wrapped": sum(1 for r in rows if r["wrapped"]),
            "at_ceiling": sum(1 for r in rows if r["at_ceiling"]),
            "wire_invalid": sum(1 for r in rows if r["wire_valid"] is False),
            "cost_usd": round(sum(r["cost_usd"] or 0.0 for r in rows), 4),
            "input_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
            "output_tokens": sum(r["completion_tokens"] or 0 for r in rows),
            "digest_tokens": {
                "n": len(lengths),
                "min": lengths[0] if lengths else None,
                "p50": lengths[len(lengths) // 2] if lengths else None,
                "max": lengths[-1] if lengths else None,
            },
            "output_tokens_p50": outputs[len(outputs) // 2] if outputs else None,
        }
    return summary


async def _run(args: argparse.Namespace) -> int:
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    unknown = set(arms) - set(_ARMS)
    if unknown:
        raise SystemExit(f"unknown arm(s): {sorted(unknown)}; known: {sorted(_ARMS)}")

    role_name = resolve_role_model_key("session_summary")
    model_def = load_model_config().models.get(role_name)
    model = f"{model_def.provider}/{model_def.id}" if model_def else role_name

    es = await _es_client()
    try:
        candidates = await _sessions_with_enough_turns(es, limit=args.candidate_pool)
        chosen = _sample(candidates, size=args.sample_size)
        sessions = [(sid, await _load_captures(es, sid)) for sid in chosen]
    finally:
        await es.close()

    sessions = [(sid, caps) for sid, caps in sessions if len(caps) >= MIN_TURNS_FOR_DIGEST]
    prompts = {sid: build_prompt(caps) for sid, caps in sessions}

    estimate = _estimate_cost(list(prompts.values()), arms=arms, model=model)
    header = {
        "model": model,
        "arms": arms,
        "candidates": len(candidates),
        "sample_size": len(sessions),
        "calls": len(sessions) * len(arms),
        "estimated_cost_usd_worst_case": round(estimate, 4),
    }
    print(json.dumps({"plan": header}, indent=2))

    if not args.confirm_spend:
        print(
            "\nEstimate only — nothing was spent. "
            "Re-run with --confirm-spend to dispatch these calls."
        )
        return 0

    # A standalone script has no application startup, so nothing has registered the cost
    # gate — and every paid call would refuse. Registering it here is what makes the
    # `study` cap actually bind: without a gate there is no reservation, and an
    # unmetered experiment is exactly what a budget lane exists to prevent.
    from personal_agent.config.settings import get_settings
    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate

    gate = CostGate(config=load_budget_config(), db_url=get_settings().database_url)
    await gate.connect()
    set_default_gate(gate)

    records: list[dict[str, Any]] = []
    consecutive_errors = 0
    for sid, captures in sessions:
        ended_at = captures[-1].timestamp
        # Arms interleaved per session rather than run arm-major, so provider drift or a
        # mid-run outage lands on every arm equally instead of contaminating one.
        for arm in arms:
            try:
                response = await _dispatch(
                    prompts[sid], arm=arm, role_name=role_name, session_id=sid
                )
                record = _classify(response, ended_at=ended_at)
                consecutive_errors = 0
            except Exception as e:  # noqa: BLE001 — one failed call must not end the run
                consecutive_errors += 1
                record = {
                    "outcome": "provider_error",
                    "error": f"{type(e).__name__}: {e}",
                    "wrapped": False,
                    "at_ceiling": False,
                    "cost_usd": 0.0,
                    "digest_tokens": None,
                    "completion_tokens": None,
                    "prompt_tokens": None,
                    "wire_valid": None,
                }
                # A misconfigured harness fails identically on every call, and a run of
                # uniform `provider_error` rows reads like a result rather than a broken
                # setup — which is exactly what happened on this pilot's first attempt
                # (an unregistered cost gate, 90 rows, no data). Stop instead.
                if consecutive_errors >= _ABORT_AFTER_CONSECUTIVE_ERRORS:
                    raise SystemExit(
                        f"aborting: {consecutive_errors} consecutive failures, "
                        f"last was {record['error']}"
                    ) from e
            record |= {"arm": arm, "session_id": sid, "turns": len(captures)}
            records.append(record)
            print(f"  {sid[:12]}… arm {arm}: {record['outcome']}")

    actual = round(sum(r["cost_usd"] or 0.0 for r in records), 4)
    report = {
        "plan": header,
        "actual_cost_usd": actual,
        "summary": _summarise(records, arms=arms),
        "records": records,
    }

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({"summary": report["summary"], "actual_cost_usd": actual}, indent=2))
    print(f"\nEstimated {header['estimated_cost_usd_worst_case']} USD, spent {actual} USD.")
    print(f"Full records: {out}")
    return 0


def main() -> int:
    """Parse arguments and run the pilot.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=30, help="sessions per arm")
    parser.add_argument("--arms", default="A,B,C", help="comma-separated subset of A,B,C")
    parser.add_argument(
        "--candidate-pool",
        type=int,
        default=500,
        help="how many sessions to consider before sampling",
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="actually dispatch the calls; without it the run stops after the estimate",
    )
    parser.add_argument(
        "--output",
        # Gitignored, per the standing convention for eval runs: the raw records are a
        # data dump, and only the curated write-up under docs/research/ is committed.
        default=(
            "telemetry/evaluation/fre996-digest-contract/"
            f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        ),
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
