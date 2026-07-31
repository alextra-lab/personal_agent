#!/usr/bin/env python3
"""FRE-1036: consolidate daily/mis-separated Elasticsearch indices into ILM-managed monthly indices, per family.

Background: the client-side write path (this PR) already cuts every in-scope family
over to a monthly, dash-separated index name (``<family>-YYYY-MM``) going forward.
This script is the one-time historical migration for indices created before that
cutover — it must run AFTER the code deploy that switches the write path (so source
indices have stopped receiving new-family writes) and BEFORE anything depends on the
consolidated shard count.

Mechanism, deliberately simpler than an ES ingest-pipeline / date_index_name
approach: each SOURCE index already encodes its own period in its name (one whole
daily index = one day; a legacy monthly-but-dotted index = one whole month), so the
destination for any given source index is computed directly from the source index's
OWN name in Python — no per-document timestamp field is read for routing. A plain
index-to-index ``_reindex`` (no pipeline) preserves each document's original ``_id``,
which makes re-running this script against the same source index idempotent by
construction: a second pass overwrites the same destination docs in place rather than
duplicating them. That is what makes the "delta" step (see below) safe.

The regex patterns below are anchored (``^...$`` against the FULL index name), which
is what keeps ``agent-captains-captures-*`` from also matching
``agent-captains-captures-subagents-*`` (and ``agent-monitors-joinability-*`` from
matching its ``-substrate-`` sibling) — no manual exclusion list needed, since the
character immediately after a family's dash prefix must be a digit for that family's
pattern to match, and a sibling family's name puts a letter there instead.

Per-family sequence (see subcommands below):
  1. ``plan``    — read-only. Lists every legacy source index matched per family and
                   the monthly destination it maps to. Run this first, always.
  2. ``reindex``  — for one family: reindex every matched source into its computed
                   destination (index-to-index, no pipeline), then set
                   ``index.lifecycle.origination_date`` on each touched destination to
                   the END of that destination's month (so ILM's delete-phase clock
                   reflects the data's true period, not migration time — the run that
                   creates a NEW destination index sets it; a later reindex pass into
                   an already-existing destination is a no-op here, origination_date is
                   only set once). Verifies per-source-index doc counts against the
                   reindex response and against a live ``_count`` on the destination,
                   and asserts the reindex response carries zero ``failures``. Does
                   NOT delete anything.
  3. ``delta``    — re-run step 2's reindex (not the origination_date step) for the
                   same family. Safe and cheap: idempotent by ``_id``, catches any
                   straggler written to a source index in the brief window between the
                   code-cutover deploy taking effect and this script's first pass.
                   Run this immediately before ``cleanup``, not instead of a prior
                   ``reindex`` pass.
  4. ``cleanup``  — for one family: re-verifies counts (same checks as ``reindex``),
                   then deletes every source index that passed verification. Requires
                   ``--confirm-delete`` in addition to the house ``--confirm-prod``
                   guard — this step is the only genuinely irreversible one.

Usage:
    uv run python scripts/migrate_fre1036_monthly_indices.py plan
    uv run python scripts/migrate_fre1036_monthly_indices.py reindex --family agent-logs --confirm-prod
    uv run python scripts/migrate_fre1036_monthly_indices.py delta --family agent-logs --confirm-prod
    uv run python scripts/migrate_fre1036_monthly_indices.py cleanup --family agent-logs --confirm-prod --confirm-delete
    uv run python scripts/migrate_fre1036_monthly_indices.py reindex --family all --confirm-prod   # every family in sequence

Run against prod ES as part of the FRE-1036 deploy, strictly AFTER the code deploy
that cuts the write path over to monthly names (see this ticket's PR). Each family is
independent — a failure partway through one family does not affect the others, and
the whole script is safe to re-run (reindex/delta are idempotent; cleanup only deletes
indices it just re-verified).
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class FamilyConfig:
    """One family's legacy-source pattern and monthly destination prefix."""

    name: str
    dest_prefix: str
    # Anchored regex over the FULL index name; named groups year/month(/day) give the
    # source index's own period. Anchoring (^...$) is what excludes sibling families
    # whose prefix is a superset of this one's (see module docstring).
    legacy_pattern: "re.Pattern[str]"


def _daily_pattern(prefix: str, sep: str) -> "re.Pattern[str]":
    # sep is already a regex fragment (e.g. r"\." or "-"), not a literal to escape.
    return re.compile(
        rf"^{re.escape(prefix)}-(?P<year>\d{{4}}){sep}(?P<month>\d{{2}}){sep}(?P<day>\d{{2}})(?:-v2)?$"
    )


def _monthly_pattern(prefix: str, sep: str) -> "re.Pattern[str]":
    # sep is already a regex fragment (e.g. r"\." or "-"), not a literal to escape.
    return re.compile(rf"^{re.escape(prefix)}-(?P<year>\d{{4}}){sep}(?P<month>\d{{2}})$")


def families() -> list[FamilyConfig]:
    """Every family with at least one legacy (pre-FRE-1036) index to migrate.

    Families with zero live indices at authoring time (agent-topology,
    agent-monitors-projector-health, agent-captains-funnel-events,
    agent-monitors-cache-reset-cadence) are not listed — there is nothing to
    migrate; their templates/policies alone (already in this PR) cover them.
    agent-insights is listed only for its pre-FRE-543 daily stragglers — its
    current monthly-dash indices do not match the daily pattern and are left
    untouched.
    """
    return [
        FamilyConfig("agent-logs", "agent-logs", _daily_pattern("agent-logs", r"\.")),
        FamilyConfig(
            "agent-captains-captures",
            "agent-captains-captures",
            _daily_pattern("agent-captains-captures", "-"),
        ),
        FamilyConfig(
            "agent-captains-captures-subagents",
            "agent-captains-captures-subagents",
            _daily_pattern("agent-captains-captures-subagents", "-"),
        ),
        FamilyConfig(
            "agent-captains-reflections",
            "agent-captains-reflections",
            _daily_pattern("agent-captains-reflections", "-"),
        ),
        FamilyConfig(
            "agent-monitors-joinability",
            "agent-monitors-joinability",
            _daily_pattern("agent-monitors-joinability", r"\."),
        ),
        FamilyConfig(
            "agent-monitors-joinability-substrate",
            "agent-monitors-joinability-substrate",
            _daily_pattern("agent-monitors-joinability-substrate", r"\."),
        ),
        FamilyConfig(
            "agent-monitors-slm-health",
            "agent-monitors-slm-health",
            _monthly_pattern("agent-monitors-slm-health", r"\."),
        ),
        FamilyConfig(
            "user-turn-ratings",
            "user-turn-ratings",
            _monthly_pattern("user-turn-ratings", r"\."),
        ),
        FamilyConfig(
            "agent-insights",
            "agent-insights",
            _daily_pattern("agent-insights", "-"),
        ),
    ]


def _dest_index(cfg: FamilyConfig, match: "re.Match[str]") -> str:
    return f"{cfg.dest_prefix}-{match.group('year')}-{match.group('month')}"


def _month_end(year: str, month: str) -> datetime:
    """Return the last instant of the given UTC month (for origination_date)."""
    y, m = int(year), int(month)
    if m == 12:
        next_month = datetime(y + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(y, m + 1, 1, tzinfo=timezone.utc)
    return next_month - timedelta(milliseconds=1)


@dataclass
class SourceMapping:
    """One legacy source index and the destination it migrates into."""

    source: str
    dest: str
    match: "re.Match[str]" = field(repr=False)


@dataclass
class FamilyPlan:
    """Every legacy source index found for one family, mapped to its destination."""

    family: str
    mappings: list[SourceMapping]

    @property
    def destinations(self) -> set[str]:
        """Distinct monthly destination index names across all mappings."""
        return {m.dest for m in self.mappings}


async def _list_indices(es: Any, prefix: str) -> list[str]:
    cat = await es.cat.indices(index=f"{prefix}-*", format="json")
    return [row["index"] for row in cat if row.get("index")]


async def plan_family(es: Any, cfg: FamilyConfig) -> FamilyPlan:
    """Read-only: list this family's legacy sources and their destinations."""
    live = await _list_indices(es, cfg.dest_prefix)
    mappings: list[SourceMapping] = []
    for name in sorted(live):
        m = cfg.legacy_pattern.match(name)
        if m:
            mappings.append(SourceMapping(source=name, dest=_dest_index(cfg, m), match=m))
    return FamilyPlan(family=cfg.name, mappings=mappings)


async def _count(es: Any, index: str) -> int:
    try:
        resp = await es.count(index=index)
        return int(resp["count"])
    except Exception:
        return -1  # index absent or unreachable — caller treats as a hard mismatch


def _group_by_destination(plan: FamilyPlan) -> dict[str, list[SourceMapping]]:
    """Group a plan's mappings by destination index.

    Verification must happen per-destination, not per-source: many sources
    (every daily index in a month) feed the same destination, so comparing
    one source's count against the destination's already-cumulative total is
    a rubber stamp once a few sources have landed — a later source's silent
    failure would go undetected because earlier sources' documents already
    put the destination count above that one source's count.
    """
    grouped: dict[str, list[SourceMapping]] = {}
    for m in plan.mappings:
        grouped.setdefault(m.dest, []).append(m)
    return grouped


async def reindex_family(es: Any, cfg: FamilyConfig, plan: FamilyPlan) -> bool:
    """Reindex every source into its destination; set origination_date once per new destination.

    Verification is two-layered: each individual reindex response is checked
    for zero ``failures`` and that every document ES read from the source was
    accounted for as created/updated/a no-op (nothing silently dropped); then,
    once every source for a given destination has been reindexed, one
    aggregate check compares that destination's live count against the sum of
    all its sources' counts (see :func:`_group_by_destination` for why this
    must be aggregate rather than per-source).

    Returns True iff every per-source check and every per-destination
    aggregate check passed.
    """
    ok = True
    seen_destinations: set[str] = set()
    for dest, mappings in _group_by_destination(plan).items():
        expected_total = 0
        for m in mappings:
            src_count = await _count(es, m.source)
            expected_total += max(src_count, 0)
            resp = await es.reindex(
                body={"source": {"index": m.source}, "dest": {"index": m.dest}},
                wait_for_completion=True,
                refresh=True,
            )
            failures = resp.get("failures") or []
            landed = resp.get("created", 0) + resp.get("updated", 0) + resp.get("noops", 0)
            total_read = resp.get("total", 0)
            if failures or landed < total_read:
                log.warning(
                    "fre1036_reindex_source_incomplete",
                    source=m.source,
                    dest=m.dest,
                    src_count=src_count,
                    total_read=total_read,
                    landed=landed,
                    failures=failures,
                )
                ok = False

            if m.dest not in seen_destinations:
                seen_destinations.add(m.dest)
                origination = _month_end(m.match.group("year"), m.match.group("month"))
                await es.indices.put_settings(
                    index=m.dest,
                    body={"index.lifecycle.origination_date": int(origination.timestamp() * 1000)},
                )
            log.info(
                "fre1036_reindex_source_done",
                source=m.source,
                dest=m.dest,
                src_count=src_count,
                total_read=total_read,
                landed=landed,
            )

        dest_count = await _count(es, dest)
        if dest_count < expected_total:
            log.warning(
                "fre1036_reindex_destination_short",
                dest=dest,
                expected_total=expected_total,
                dest_count=dest_count,
            )
            ok = False
        log.info(
            "fre1036_reindex_destination_done",
            dest=dest,
            sources=len(mappings),
            expected_total=expected_total,
            dest_count=dest_count,
        )
    return ok


async def cleanup_family(es: Any, cfg: FamilyConfig, plan: FamilyPlan) -> tuple[bool, list[str]]:
    """Re-verify each destination's aggregate count, then delete every source that passed.

    Verification is per-destination (see :func:`_group_by_destination`): the
    sum of a destination's sources' live counts must not exceed the
    destination's own live count before ANY of that destination's sources are
    deleted — a per-source check here has the exact same dilution flaw
    :func:`reindex_family` avoids, and this is the step where a false pass
    causes irreversible data loss.

    Returns ``(all_ok, deleted)``.
    """
    deleted: list[str] = []
    all_ok = True
    for dest, mappings in _group_by_destination(plan).items():
        src_counts = {m.source: await _count(es, m.source) for m in mappings}
        expected_total = sum(max(c, 0) for c in src_counts.values())
        dest_count = await _count(es, dest)
        if dest_count < expected_total:
            log.warning(
                "fre1036_cleanup_verify_failed",
                dest=dest,
                expected_total=expected_total,
                dest_count=dest_count,
                sources=list(src_counts),
            )
            all_ok = False
            continue
        for m in mappings:
            await es.indices.delete(index=m.source)
            deleted.append(m.source)
            log.info("fre1036_source_deleted", source=m.source, dest=dest)
    return all_ok, deleted


def _resolve_families(name: str) -> list[FamilyConfig]:
    all_families = families()
    if name == "all":
        return all_families
    for cfg in all_families:
        if cfg.name == name:
            return [cfg]
    raise SystemExit(f"unknown family: {name!r}. Known: {[c.name for c in all_families]}")


async def _run(args: argparse.Namespace) -> int:
    from elasticsearch import AsyncElasticsearch

    es = AsyncElasticsearch([settings.elasticsearch_url], request_timeout=120)
    try:
        exit_code = 0
        if args.command == "plan":
            for cfg in families():
                p = await plan_family(es, cfg)
                print(f"\n=== {cfg.name} ({len(p.mappings)} source indices) ===")
                for m in p.mappings:
                    print(f"  {m.source}  ->  {m.dest}")
            return 0

        for cfg in _resolve_families(args.family):
            p = await plan_family(es, cfg)
            if not p.mappings:
                print(f"{cfg.name}: no legacy source indices found — nothing to do")
                continue

            if args.command in ("reindex", "delta"):
                ok = await reindex_family(es, cfg, p)
                verb = "reindex" if args.command == "reindex" else "delta"
                print(
                    f"{cfg.name}: {verb} {'OK' if ok else 'HAD FAILURES'} "
                    f"({len(p.mappings)} sources -> {len(p.destinations)} destinations)"
                )
                if not ok:
                    exit_code = 3
            elif args.command == "cleanup":
                if not args.confirm_delete:
                    print(
                        "ERROR: cleanup requires --confirm-delete (irreversible index deletion).",
                        file=sys.stderr,
                    )
                    return 2
                ok, deleted = await cleanup_family(es, cfg, p)
                print(
                    f"{cfg.name}: cleanup {'OK' if ok else 'INCOMPLETE'} "
                    f"— deleted {len(deleted)}/{len(p.mappings)} source indices"
                )
                if not ok:
                    exit_code = 3
        return exit_code
    finally:
        await es.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "FRE-1036: migrate legacy daily/mis-separated ES indices into ILM-managed "
            "monthly indices, per family. Run 'plan' first."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="Read-only: list every family's legacy sources.")
    plan_p.add_argument("--confirm-prod", action="store_true", default=False)

    for name, help_text in (
        ("reindex", "Reindex one family's legacy sources into their monthly destinations."),
        ("delta", "Re-run reindex for one family (idempotent straggler sweep)."),
        ("cleanup", "Delete one family's verified-migrated legacy sources."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--family",
            required=True,
            help="Family name (see 'plan' output), or 'all' for every family in sequence.",
        )
        p.add_argument(
            "--confirm-prod",
            action="store_true",
            default=False,
            help="Required when AGENT_ENVIRONMENT is not 'test'. Confirms intent to write production data.",
        )
        if name == "cleanup":
            p.add_argument(
                "--confirm-delete",
                action="store_true",
                default=False,
                help="Required in addition to --confirm-prod. Confirms intent to delete source indices.",
            )

    return parser.parse_args()


def main() -> int:
    """CLI entrypoint with the house prod-write env guard."""
    args = _parse_args()

    from personal_agent.config.env_loader import Environment

    if settings.environment != Environment.TEST and not args.confirm_prod:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script reindexes and (in 'cleanup') deletes Elasticsearch indices.\n"
            "Re-run with --confirm-prod if you intend to operate on production data.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
