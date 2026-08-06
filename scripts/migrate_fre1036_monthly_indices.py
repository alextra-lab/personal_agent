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
is what keeps ``agent-captains-captures-*`` from also *matching*
``agent-captains-captures-subagents-*`` (and ``agent-monitors-joinability-*`` from
matching its ``-substrate-`` sibling) — no manual exclusion list needed, since the
character immediately after a family's dash prefix must be a digit for that family's
pattern to match, and a sibling family's name puts a letter there instead. The
completeness check in ``plan_family`` (FRE-1105) is a separate concern from matching:
``cat.indices`` itself returns a sibling's indices under the parent's glob prefix, so
that check does need an explicit sibling registry to avoid flagging them unaccounted.

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
    """One family's legacy-source pattern(s) and monthly destination prefix."""

    name: str
    dest_prefix: str
    # Anchored regexes over the FULL index name; named groups year/month(/day) give the
    # source index's own period. Anchoring (^...$) is what excludes sibling families
    # whose prefix is a superset of this one's (see module docstring). A family may
    # carry more than one shape of legacy index (e.g. agent-monitors-slm-health had
    # both dotted-monthly and dotted-daily stragglers live at once — FRE-1105); the
    # daily and monthly patterns are mutually exclusive by construction (the monthly
    # pattern is anchored immediately after the month, the daily pattern requires a
    # further separator + day group), so at most one pattern ever matches a given name.
    legacy_patterns: tuple["re.Pattern[str]", ...]
    # Index names confirmed to carry this family's prefix but hold no migratable data
    # (a dead scaffold, verified empty) — cleanup deletes them directly once it
    # re-verifies the live count is actually 0, independent of the reindex flow.
    known_empty_deletions: frozenset[str] = field(default_factory=frozenset)


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

    A family absent from this list is NOT a verified-safe fact — it was true
    at authoring time for agent-topology, agent-monitors-projector-health,
    agent-captains-funnel-events, and agent-monitors-cache-reset-cadence (zero
    live indices, nothing to migrate), and two of those four have since
    silently accumulated dozens of live indices with no one noticing (FRE-1105
    master-gate finding). Do not trust this list's completeness by reading it;
    ``cluster_unaccounted_indices`` re-derives it live against the cluster
    every time ``plan`` runs, which is the only thing that can't go stale the
    same way. agent-insights is listed only for its pre-FRE-543 daily
    stragglers — its current monthly-dash indices do not match the daily
    pattern and are left untouched.
    """
    return [
        FamilyConfig(
            "agent-logs",
            "agent-logs",
            (_daily_pattern("agent-logs", r"\."),),
            # Dead rollover-alias bootstrap index: agent-logs-template never set
            # index.lifecycle.name, so no index was ever ILM-managed and this index
            # sat at 0 docs (see docker/elasticsearch/ilm-policy.json). Confirmed
            # 0 docs on the live cluster 2026-08-06 (FRE-1105).
            known_empty_deletions=frozenset({"agent-logs-000001"}),
        ),
        FamilyConfig(
            "agent-captains-captures",
            "agent-captains-captures",
            (_daily_pattern("agent-captains-captures", "-"),),
        ),
        FamilyConfig(
            "agent-captains-captures-subagents",
            "agent-captains-captures-subagents",
            (_daily_pattern("agent-captains-captures-subagents", "-"),),
        ),
        FamilyConfig(
            "agent-captains-reflections",
            "agent-captains-reflections",
            (_daily_pattern("agent-captains-reflections", "-"),),
        ),
        FamilyConfig(
            "agent-monitors-joinability",
            "agent-monitors-joinability",
            (_daily_pattern("agent-monitors-joinability", r"\."),),
        ),
        FamilyConfig(
            "agent-monitors-joinability-substrate",
            "agent-monitors-joinability-substrate",
            (_daily_pattern("agent-monitors-joinability-substrate", r"\."),),
        ),
        FamilyConfig(
            "agent-monitors-slm-health",
            "agent-monitors-slm-health",
            # FRE-1105: this family holds dotted-monthly AND dotted-daily legacy
            # indices simultaneously — a monthly-only pattern silently orphaned
            # the daily stragglers (10 found live on 2026-08-06).
            (
                _monthly_pattern("agent-monitors-slm-health", r"\."),
                _daily_pattern("agent-monitors-slm-health", r"\."),
            ),
        ),
        FamilyConfig(
            "user-turn-ratings",
            "user-turn-ratings",
            # FRE-1105: same defect class as agent-monitors-slm-health (7 daily
            # stragglers found live on 2026-08-06).
            (
                _monthly_pattern("user-turn-ratings", r"\."),
                _daily_pattern("user-turn-ratings", r"\."),
            ),
        ),
        FamilyConfig(
            "agent-insights",
            "agent-insights",
            (_daily_pattern("agent-insights", "-"),),
        ),
    ]


#: Prefixes confirmed live (2026-08-06) to hold indices genuinely out of this
#: migration's scope, each with a stated reason — a decision, not a default.
#: Everything else is either a configured family above or is unaccounted:
#: see ``cluster_unaccounted_indices``. Do NOT add agent-topology or
#: agent-monitors-projector-health here — they are unaccounted on purpose
#: (FRE-1105 master-gate finding); adding them here would silence the exact
#: signal this registry exists to keep loud.
EXCLUDED_PREFIXES: dict[str, str] = {
    "caddy-access": (
        "writes its monthly-dash destination shape natively under its own ILM policy "
        "(docker/elasticsearch/caddy-access-ilm-policy.json) — never had a legacy-index "
        "migration story, not one of FRE-1036's per-family targets."
    ),
    "slm-requests": (
        "has no lifecycle policy and has never cut over to a monthly destination shape "
        "at all (still 100% daily-dotted, actively written as of 2026-08-06) — a "
        "distinct, already-tracked gap (FRE-1106), not this migration's "
        "daily/monthly-consolidation problem."
    ),
}


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
    """Every legacy source index found for one family, mapped to its destination.

    ``pending_deletions`` and ``unaccounted`` complete the family's picture
    (FRE-1105): every live index under the family's prefix must land in
    ``mappings``, ``pending_deletions``, be a current destination, or belong
    to a registered sibling family — anything left over is ``unaccounted``,
    the exact silent-orphan condition this ticket exists to catch.
    """

    family: str
    mappings: list[SourceMapping]
    pending_deletions: list[str] = field(default_factory=list)
    unaccounted: list[str] = field(default_factory=list)

    @property
    def destinations(self) -> set[str]:
        """Distinct monthly destination index names across all mappings."""
        return {m.dest for m in self.mappings}


class IncompleteFamilyError(RuntimeError):
    """Raised when a FamilyPlan has unaccounted indices — see FamilyPlan's docstring.

    FRE-1105: this is the assertion the pre-fix design had no equivalent of —
    the condition that let cleanup report "deleted 2 of 2" while ten of a
    family's fourteen indices sat untouched, uncounted by either denominator.
    """


def assert_family_complete(plan: FamilyPlan) -> None:
    """Raise IncompleteFamilyError if any live index in ``plan`` is unaccounted for."""
    if plan.unaccounted:
        raise IncompleteFamilyError(
            f"{plan.family}: {len(plan.unaccounted)} index(es) carry this family's prefix but are "
            f"accounted for by no legacy pattern, no current destination, no sibling family, and "
            f"no explicit exclusion: {plan.unaccounted}"
        )


async def _list_indices(es: Any, prefix: str) -> list[str]:
    cat = await es.cat.indices(index=f"{prefix}-*", format="json")
    return [row["index"] for row in cat if row.get("index")]


async def _list_all_indices(es: Any) -> list[str]:
    """Every live index in the cluster, excluding ES system indices (dot-prefixed)."""
    cat = await es.cat.indices(index="*", format="json")
    return [row["index"] for row in cat if row.get("index") and not row["index"].startswith(".")]


def cluster_unaccounted_indices(live_indices: list[str]) -> list[str]:
    """Every live index that maps to no configured family and no explicit exclusion.

    FRE-1105 master-gate finding: the per-family completeness check
    (plan_family/assert_family_complete) only ever sees the families()
    already knows about — it fixed the denominator WITHIN a family and left
    the denominator OVER families (the config list itself) free to go stale
    the same way. This is that same check one level up: a family whose
    "nothing to migrate" exclusion has silently expired (a prefix that held
    zero indices when families() was authored and now holds dozens) becomes a
    loud finding here instead of a permanent, undetectable blind spot.
    """
    known_prefixes = [f"{cfg.dest_prefix}-" for cfg in families()] + [
        f"{prefix}-" for prefix in EXCLUDED_PREFIXES
    ]
    return sorted(
        name for name in live_indices if not any(name.startswith(p) for p in known_prefixes)
    )


class IncompleteClusterError(RuntimeError):
    """Raised when the cluster holds indices under no configured family and no exclusion."""


def assert_cluster_complete(unaccounted: list[str]) -> None:
    """Raise IncompleteClusterError if any live index maps to nothing in the registry."""
    if unaccounted:
        raise IncompleteClusterError(
            f"{len(unaccounted)} live index(es) map to no configured family in families() "
            f"and no explicit exclusion in EXCLUDED_PREFIXES — this is either real "
            f"migration-target residue (a family whose 'nothing to migrate' exclusion has "
            f"expired) or a genuinely new prefix that needs a stated reason: {unaccounted}"
        )


def _match_legacy(cfg: FamilyConfig, name: str) -> "re.Match[str] | None":
    """First legacy pattern (of possibly several) that matches ``name``, if any."""
    for pattern in cfg.legacy_patterns:
        m = pattern.match(name)
        if m is not None:
            return m
    return None


async def plan_family(es: Any, cfg: FamilyConfig) -> FamilyPlan:
    """Read-only: list this family's legacy sources, destinations, and completeness.

    Classifies every live index carrying this family's prefix (see
    :class:`FamilyPlan` for the full classification this produces). FRE-1105:
    an unaccounted index is exactly the silent-orphan defect this plan exists
    to catch.
    """
    live = await _list_indices(es, cfg.dest_prefix)
    # cat.indices(f"{dest_prefix}-*") also returns a registered sibling family's
    # own indices (e.g. agent-captains-captures-subagents under the
    # agent-captains-captures glob) — those are that sibling's own plan_family
    # call's responsibility, not an orphan of this family.
    sibling_prefixes = [
        f"{other.dest_prefix}-"
        for other in families()
        if other.name != cfg.name and other.dest_prefix.startswith(f"{cfg.dest_prefix}-")
    ]
    # A migrated destination is always dash-monthly (see _dest_index) — reuse
    # the monthly-source pattern generator rather than a bespoke regex.
    dest_pattern = _monthly_pattern(cfg.dest_prefix, "-")

    mappings: list[SourceMapping] = []
    for name in sorted(live):
        m = _match_legacy(cfg, name)
        if m:
            mappings.append(SourceMapping(source=name, dest=_dest_index(cfg, m), match=m))

    matched = {m.source for m in mappings}
    pending_deletions = sorted(set(live) & cfg.known_empty_deletions)
    overlap = matched & set(pending_deletions)
    if overlap:
        # Would otherwise be deleted once by the reindex-verified path and again
        # by cleanup_family's independent known_empty_deletions loop.
        raise ValueError(
            f"{cfg.name}: index both matched as a legacy source and declared a "
            f"known-empty deletion — fix FamilyConfig: {sorted(overlap)}"
        )

    accounted = matched | set(pending_deletions)
    unaccounted = sorted(
        name
        for name in live
        if name not in accounted
        and not dest_pattern.match(name)
        and not any(name.startswith(prefix) for prefix in sibling_prefixes)
    )
    return FamilyPlan(
        family=cfg.name,
        mappings=mappings,
        pending_deletions=pending_deletions,
        unaccounted=unaccounted,
    )


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

    Also deletes ``plan.pending_deletions`` (FRE-1105: e.g. agent-logs-000001,
    a dead scaffold index with no data to reindex) — a step independent of the
    per-destination reindex flow above, since these carry no source/dest
    relationship at all. Never trusted on the ``known_empty_deletions`` label
    alone — the live count is re-verified as 0 immediately before deleting.
    A nonzero count (config drift, or a name reused for something else)
    refuses the deletion and fails the family rather than silently discarding
    data.

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

    for name in plan.pending_deletions:
        count = await _count(es, name)
        if count != 0:
            log.warning("fre1036_cleanup_known_empty_not_actually_empty", index=name, count=count)
            all_ok = False
            continue
        await es.indices.delete(index=name)
        deleted.append(name)
        log.info("fre1036_known_empty_deleted", index=name)

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
            # Read-only diagnostic: must keep working, and show the breakage, even
            # when a family is incomplete (FRE-1105) — it never raises, it reports.
            any_unaccounted = False
            for cfg in families():
                p = await plan_family(es, cfg)
                print(f"\n=== {cfg.name} ({len(p.mappings)} source indices) ===")
                for m in p.mappings:
                    print(f"  {m.source}  ->  {m.dest}")
                if p.pending_deletions:
                    print(f"  [pending deletion, verified empty at cleanup]: {p.pending_deletions}")
                if p.unaccounted:
                    any_unaccounted = True
                    print(
                        f"  !!! UNACCOUNTED (no pattern, no destination, no exclusion): "
                        f"{p.unaccounted}"
                    )

            # Cluster-level check (FRE-1105 master-gate finding): the per-family loop
            # above only ever sees the families() already knows about. This is the
            # same check one level up — over the registry itself, not within one
            # family — so a family whose exclusion has silently gone stale (a prefix
            # that held zero indices at authoring time and now holds dozens) is a
            # loud finding here instead of a permanent blind spot.
            cluster_unaccounted = cluster_unaccounted_indices(await _list_all_indices(es))
            if cluster_unaccounted:
                any_unaccounted = True
                print(
                    f"\n=== CLUSTER: {len(cluster_unaccounted)} index(es) map to no "
                    f"configured family and no EXCLUDED_PREFIXES entry ==="
                )
                for name in cluster_unaccounted:
                    print(f"  !!! {name}")
            return 1 if any_unaccounted else 0

        for cfg in _resolve_families(args.family):
            p = await plan_family(es, cfg)
            try:
                assert_family_complete(p)
            except IncompleteFamilyError as exc:
                # One broken family must not abort the rest of a `--family all` run.
                print(f"ERROR: {exc}", file=sys.stderr)
                exit_code = 3
                continue

            if not p.mappings and not p.pending_deletions:
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
                expected = len(p.mappings) + len(p.pending_deletions)
                print(
                    f"{cfg.name}: cleanup {'OK' if ok else 'INCOMPLETE'} "
                    f"— deleted {len(deleted)}/{expected} indices"
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

    plan_p = sub.add_parser(
        "plan",
        help=(
            "Read-only: list every family's legacy sources. Never writes; exits 1 "
            "(not an error, a signal) if any family has an unaccounted index, or if "
            "any live cluster index maps to no configured family and no "
            "EXCLUDED_PREFIXES entry."
        ),
    )
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
