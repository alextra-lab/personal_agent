#!/usr/bin/env python3
"""One-time ES backfill: persist a default "ok" rating for historical turns (FRE-757).

FRE-757 changes the per-turn rating so every turn carries a persisted rating
("ok") written on send. Live turns get this from the PWA's DONE hook; this
script backfills the historical turns that predate the change.

For each distinct ``trace_id`` with a ``model_call_completed`` event in
``agent-logs-*`` that does NOT already have a ``user-turn-ratings-*`` document,
it creates one rating doc with:

  * ``rating = 2`` — "ok". This EQUALS the FRE-407 imputation default, so the
    flagging metric is unchanged (invariant) — the backfill only makes the
    store physically carry a rating on every turn.
  * ``rated_at`` = the turn's ORIGINAL event ``@timestamp`` (NOT now) — so the
    metric's time-windowing and the index's monthly partition/ILM stay correct.
  * ``prompt_callsite`` + the ``prompt_*`` denorms copied from the same event,
    using the same callsite preference the live endpoint applies
    (orchestrator.primary → role.primary → gateway.chat → most-recent), so the
    doc lands in the right per-callsite bucket rather than the excluded
    ``unknown`` bucket.

Idempotent: writes use ES ``op_type=create`` keyed on ``doc_id=trace_id`` and
already-rated turns are skipped, so re-running never overwrites a rating.

Usage:
    uv run python scripts/migrate_fre757_backfill_default_rating.py [--dry-run]

Run against prod ES as part of the FRE-757 deploy (order-independent vs the PWA
deploy — the live default write is create-if-absent and cannot clobber these).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from personal_agent.config import settings
from personal_agent.gateway.feedback_models import UserTurnRating

# Default "ok" rating == the FRE-407 imputation default (metric-invariant).
_DEFAULT_RATING = 2

# Callsite preference — mirror feedback_api._CALLSITE_PREFERENCE so a backfilled
# doc is attributed to the same bucket a live rating would use.
_CALLSITE_PREFERENCE = (
    "orchestrator.primary",
    "role.primary",
    "gateway.chat",
)

_SOURCE_FIELDS = [
    "trace_id",
    "session_id",
    "prompt_callsite",
    "prompt_static_prefix_hash",
    "prompt_dynamic_hash",
    "prompt_component_ids",
    "@timestamp",
]


def _parse_timestamp(raw: Any) -> datetime | None:
    """Parse an ES ``@timestamp`` string into a datetime, or None if unusable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # ES emits ISO 8601, often with a trailing 'Z'.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _prefer(a: Mapping[str, Any], b: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the better of two events for the same trace by callsite preference.

    Preference order: an earlier entry in ``_CALLSITE_PREFERENCE`` wins; between
    two non-preferred callsites the more recent ``@timestamp`` wins (ties keep
    the incumbent).
    """

    def rank(ev: Mapping[str, Any]) -> int:
        cs = ev.get("prompt_callsite")
        return (
            _CALLSITE_PREFERENCE.index(cs)
            if cs in _CALLSITE_PREFERENCE
            else len(_CALLSITE_PREFERENCE)
        )

    ra, rb = rank(a), rank(b)
    if rb < ra:
        return b
    if rb > ra:
        return a
    # Same rank → keep the more recent timestamp.
    ta = _parse_timestamp(a.get("@timestamp"))
    tb = _parse_timestamp(b.get("@timestamp"))
    if ta is not None and tb is not None and tb > ta:
        return b
    return a


def select_rating_docs(
    events: Iterable[Mapping[str, Any]],
    existing_trace_ids: set[str],
) -> list[UserTurnRating]:
    """Choose one default rating doc per un-rated trace from raw log events.

    Pure (no I/O) so it is unit-testable. Groups ``model_call_completed`` events
    by ``trace_id``, picks the preferred callsite event per trace, and emits a
    ``UserTurnRating`` (rating=2) for each trace that is not already rated and
    has a usable original ``@timestamp``.

    Args:
        events: Raw ``_source`` dicts of ``model_call_completed`` events.
        existing_trace_ids: Trace IDs that already have a rating doc — skipped.

    Returns:
        One ``UserTurnRating`` per new trace, deterministically ordered by
        trace_id.
    """
    chosen: dict[str, Mapping[str, Any]] = {}
    for ev in events:
        tid = ev.get("trace_id")
        if not isinstance(tid, str) or not tid:
            continue
        if tid in existing_trace_ids:
            continue
        incumbent = chosen.get(tid)
        chosen[tid] = ev if incumbent is None else _prefer(incumbent, ev)

    docs: list[UserTurnRating] = []
    for tid in sorted(chosen):
        ev = chosen[tid]
        rated_at = _parse_timestamp(ev.get("@timestamp"))
        if rated_at is None:
            # No usable original timestamp — skip rather than stamp `now`, which
            # would corrupt the metric window. (Left unrated → still imputed 2.)
            continue
        raw_ids = ev.get("prompt_component_ids") or []
        component_ids = tuple(str(c) for c in raw_ids) if isinstance(raw_ids, Sequence) else ()
        session_id = ev.get("session_id")
        docs.append(
            UserTurnRating(
                trace_id=tid,
                session_id=str(session_id) if session_id is not None else "",
                rating=_DEFAULT_RATING,
                prompt_callsite=ev.get("prompt_callsite"),
                prompt_static_prefix_hash=ev.get("prompt_static_prefix_hash"),
                prompt_dynamic_hash=ev.get("prompt_dynamic_hash"),
                prompt_component_ids=component_ids,
                rated_at=rated_at,
            )
        )
    return docs


async def _load_existing_trace_ids(es: Any) -> set[str]:
    """Collect all trace_ids that already have a user-turn-ratings doc."""
    existing: set[str] = set()
    search_after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "size": 1000,
            "_source": ["trace_id"],
            "sort": [{"trace_id": "asc"}],
            "query": {"exists": {"field": "trace_id"}},
        }
        if search_after is not None:
            body["search_after"] = search_after
        resp = await es.search(index="user-turn-ratings-*", **body)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            tid = h.get("_source", {}).get("trace_id")
            if isinstance(tid, str):
                existing.add(tid)
        search_after = hits[-1].get("sort")
        if search_after is None or len(hits) < 1000:
            break
    return existing


async def _scan_completed_events(es: Any, logs_index: str) -> list[dict[str, Any]]:
    """Page all model_call_completed events, returning their _source dicts."""
    events: list[dict[str, Any]] = []
    search_after: list[Any] | None = None
    while True:
        body: dict[str, Any] = {
            "size": 1000,
            "_source": _SOURCE_FIELDS,
            "sort": [{"@timestamp": "asc"}, {"trace_id": "asc"}],
            "query": {"term": {"event_type": "model_call_completed"}},
        }
        if search_after is not None:
            body["search_after"] = search_after
        resp = await es.search(index=logs_index, **body)
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            break
        events.extend(dict(h.get("_source", {})) for h in hits)
        search_after = hits[-1].get("sort")
        if search_after is None or len(hits) < 1000:
            break
    return events


async def run_backfill(*, dry_run: bool) -> int:
    """Execute the backfill against the configured Elasticsearch cluster.

    Args:
        dry_run: When true, compute + report but write nothing.

    Returns:
        Process exit code (0 success, non-zero on connection failure).
    """
    try:
        from elasticsearch import AsyncElasticsearch
    except ModuleNotFoundError:
        print("elasticsearch package not installed — run 'uv sync' first.", file=sys.stderr)
        return 1

    logs_index = f"{settings.elasticsearch_index_prefix}-*"
    es = AsyncElasticsearch([settings.elasticsearch_url], request_timeout=60)
    try:
        info = await es.info()
        print(
            f"✓ Connected to Elasticsearch {info['version']['number']} at {settings.elasticsearch_url}"
        )

        existing = await _load_existing_trace_ids(es)
        print(f"  {len(existing)} turns already have a rating")

        events = await _scan_completed_events(es, logs_index)
        print(f"  {len(events)} model_call_completed events scanned from {logs_index}")

        docs = select_rating_docs(events, existing)
        print(f"  {len(docs)} historical turns need a default 'ok' rating")

        if dry_run:
            print("DRY-RUN — no writes performed.")
            return 0

        created = 0
        conflicts = 0
        for doc in docs:
            index_name = f"user-turn-ratings-{doc.rated_at.strftime('%Y.%m')}"
            try:
                await es.index(
                    index=index_name,
                    id=doc.trace_id,
                    document=doc.to_es_doc(),
                    op_type="create",
                )
                created += 1
            except Exception as exc:  # noqa: BLE001 — 409 conflict = already rated
                if "conflict" in str(exc).lower() or "version_conflict" in str(exc).lower():
                    conflicts += 1
                    continue
                raise
        print(f"✓ Backfill complete — {created} created, {conflicts} already existed (skipped)")
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level guard for a one-shot script
        print(f"✗ Backfill failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await es.close()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="FRE-757 default-rating backfill.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report the backfill without writing.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run_backfill(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
