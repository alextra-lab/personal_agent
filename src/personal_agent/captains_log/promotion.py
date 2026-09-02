"""Promotion pipeline for Captain's Log proposals (ADR-0030).

Scans AWAITING_APPROVAL entries that meet configurable promotion criteria
(min seen_count, min age) and creates Linear backlog issues via the MCP
gateway's Linear integration.  Promoted entries are marked APPROVED with
a linear_issue_id.

The pipeline is designed to be invoked as a scheduled job from the
BrainstemScheduler (weekly by default).
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from personal_agent.captains_log.linear_client import (
    LinearClient,
    extract_issue_identifier_from_description,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogStatus,
    ChangeCategory,
    ProposalSource,
)
from personal_agent.config import settings
from personal_agent.linear_labels import AGENT_AUTHORED_LABEL
from personal_agent.sysgraph import SysgraphRepository
from personal_agent.sysgraph.repository import ProposalRecord
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

if TYPE_CHECKING:
    from personal_agent.telemetry.es_handler import ElasticsearchHandler


def _trace_id_from_entry(entry: CaptainLogEntry) -> str | None:
    """Extract the originating trace_id from a Captain's Log entry, if any.

    Args:
        entry: The Captain's Log entry to inspect.

    Returns:
        The first telemetry-ref trace_id when present and a string; ``None``
        otherwise.
    """
    if not entry.telemetry_refs:
        return None
    raw = entry.telemetry_refs[0].trace_id
    if isinstance(raw, str) and not getattr(raw, "__await__", None):
        return raw
    return None


LinearIssueCreator = Callable[
    [str, str, str, int, list[str], str, str],
    Coroutine[Any, Any, str | None],
]


class PromotionCriteria(BaseModel):
    """Configurable criteria for promoting a proposal to Linear."""

    min_seen_count: int = Field(
        default_factory=lambda: settings.promotion_min_seen_count,
        ge=1,
        description=(
            "Minimum times the proposal was observed. Defaults from "
            "settings.promotion_min_seen_count so this bar and the one read-before-emit "
            "retains a reinforced proposal at are the same number (FRE-1354)."
        ),
    )
    min_age_days: int = Field(default=7, ge=0, description="Minimum days since first_seen")
    max_existing_linear_issues: int = Field(
        default=5, ge=1, description="Cap on issues created per pipeline run (ADR-0040 default 5)"
    )
    excluded_categories: list[ChangeCategory] = Field(
        default_factory=lambda: [ChangeCategory.KNOWLEDGE_QUALITY],
        description=(
            "Categories to skip during promotion. Defaults to excluding "
            "KNOWLEDGE_QUALITY (FRE-620 promotion floor): only RELIABILITY/high-severity "
            "proposals auto-promote to Linear; KNOWLEDGE_QUALITY/medium ones accrue on the "
            "dashboard and the standing graph-hygiene backlog ticket (FRE-621) instead."
        ),
    )


def _map_seen_count_to_priority(seen_count: int) -> int:
    """Map proposal frequency to Linear priority number.

    Linear priorities: 0=None, 1=Urgent, 2=High, 3=Normal, 4=Low.

    Args:
        seen_count: Number of times the proposal was observed.

    Returns:
        Linear priority integer.
    """
    if seen_count >= 10:
        return 2  # High
    if seen_count >= 5:
        return 3  # Normal
    return 4  # Low


def _format_linear_description(entry: CaptainLogEntry) -> str:
    """Format a rich Linear issue description from a CaptainLogEntry.

    Args:
        entry: The promoted Captain's Log entry.

    Returns:
        Markdown description string.
    """
    pc = entry.proposed_change
    if pc is None:
        return ""

    lines = [
        "## Proposed Change",
        "",
        f"**What**: {pc.what}",
        f"**Why**: {pc.why}",
        f"**How**: {pc.how}",
        "",
        f"**Category**: `{pc.category.value if pc.category else 'unknown'}`",
        f"**Scope**: `{pc.scope.value if pc.scope else 'unknown'}`",
        "",
        "## Rationale",
        "",
        entry.rationale,
    ]

    # ADR-0105 D5/AC-4: every substantive field present on the source proposal
    # carries through verbatim — no truncation, no summarizing (only the title
    # truncates pc.what[:80]).
    if entry.experiment_design:
        lines += ["", "## Experiment Design", ""]
        lines += [f"- {step}" for step in entry.experiment_design]

    if entry.expected_outcome:
        lines += ["", "## Expected Outcome", "", entry.expected_outcome]

    if entry.potential_implementation:
        lines += ["", "## Potential Implementation", ""]
        lines += [f"- {step}" for step in entry.potential_implementation]

    lines += [
        "",
        "## Evidence",
        "",
        f"- Observed **{pc.seen_count}** time(s)",
    ]

    if pc.first_seen:
        lines.append(f"- First seen: {pc.first_seen.strftime('%Y-%m-%d %H:%M UTC')}")

    if entry.supporting_metrics:
        lines.append(f"- Metrics: {', '.join(entry.supporting_metrics)}")

    if entry.impact_assessment:
        lines.append(f"- Impact: {entry.impact_assessment}")

    if pc.related_entry_ids:
        ids_str = ", ".join(f"`{eid}`" for eid in pc.related_entry_ids[:10])
        lines.append(f"- Related entries: {ids_str}")

    lines += [
        "",
        f"> Captain's Log entry `{entry.entry_id}`",
        "> Auto-promoted by ADR-0030 pipeline",
    ]

    if pc.fingerprint:
        lines += [
            "",
            f"**Fingerprint**: `{pc.fingerprint}`",
            f"<!-- fingerprint: {pc.fingerprint} -->",
        ]

    return "\n".join(lines)


class PromotionPipeline:
    """Scans Captain's Log entries and promotes qualifying proposals to Linear.

    Usage::

        pipeline = PromotionPipeline(log_dir=captains_log_dir)
        promoted = await pipeline.run()

    The ``create_issue_fn`` parameter allows callers to inject the actual
    Linear API integration (e.g. via the MCP gateway). When not provided,
    promotable entries are logged but no Linear issues are created — useful
    for dry-run / testing.
    """

    def __init__(
        self,
        log_dir: pathlib.Path | None = None,
        criteria: PromotionCriteria | None = None,
        create_issue_fn: LinearIssueCreator | None = None,
        linear_client: LinearClient | None = None,
        sysgraph_repo: SysgraphRepository | None = None,
        es_handler: "ElasticsearchHandler | None" = None,
    ) -> None:
        """Initialize the promotion pipeline.

        Args:
            log_dir: Path to the Captain's Log JSON directory.
            criteria: Optional promotion criteria overrides.
            create_issue_fn: Async callable(title, team, description, priority,
                labels, state, project) -> issue_identifier | None.
                If None, promotable entries are identified but not pushed to Linear.
            linear_client: Optional Linear MCP client for budget and duplicate checks.
            sysgraph_repo: Optional connected SysgraphRepository (ADR-0105 D4). When
                None, the graph-side proposal<->ticket linkage is skipped entirely —
                never blocks Linear promotion.
            es_handler: Optional Elasticsearch handler used to stamp linear_issue_id
                onto the source `agent-insights-*` document for STATISTICAL_DETECTOR-
                sourced promotions (ADR-0105 D4). Falls back to
                ``CaptainLogManager._default_es_handler`` when not provided.
        """
        if log_dir is None:
            project_root = pathlib.Path(__file__).parent.parent.parent.parent
            log_dir = project_root / "telemetry" / "captains_log"
        self.log_dir = log_dir
        self.criteria = criteria or PromotionCriteria()
        self._create_issue_fn = create_issue_fn
        self._linear_client = linear_client
        self._sysgraph_repo = sysgraph_repo
        self._es_handler = es_handler

    def scan_promotable_entries(self) -> list[CaptainLogEntry]:
        """Find all AWAITING_APPROVAL entries that meet promotion criteria.

        Returns:
            List of CaptainLogEntry objects eligible for promotion.
        """
        now = datetime.now(timezone.utc)
        promotable: list[CaptainLogEntry] = []

        for json_file in sorted(self.log_dir.glob("CL-*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            if data.get("status") != CaptainLogStatus.AWAITING_APPROVAL.value:
                continue

            # FRE-523: eval-derived reflection entries are written (the cognitive
            # pipeline runs during eval) but must never be promoted to Linear —
            # filing tickets off synthetic/eval prompts is exactly the side effect
            # the EVAL contract suppresses. Read the raw flag so even a malformed
            # entry is skipped before model validation.
            if data.get("eval_mode"):
                log.debug(
                    "promotion_skipped_eval_entry",
                    file=str(json_file),
                    entry_id=data.get("entry_id"),
                )
                continue

            pc = data.get("proposed_change")
            if not pc:
                continue

            seen_count = pc.get("seen_count", 1)
            if seen_count < self.criteria.min_seen_count:
                continue

            first_seen_raw = pc.get("first_seen")
            if first_seen_raw and self.criteria.min_age_days > 0:
                try:
                    first_seen = datetime.fromisoformat(str(first_seen_raw).replace("Z", "+00:00"))
                    age_days = (now - first_seen).days
                    if age_days < self.criteria.min_age_days:
                        continue
                except (ValueError, TypeError):
                    continue

            category_raw = pc.get("category")
            if category_raw:
                try:
                    cat = ChangeCategory(category_raw)
                    if cat in self.criteria.excluded_categories:
                        continue
                except ValueError:
                    pass

            if data.get("linear_issue_id"):
                continue

            try:
                entry = CaptainLogEntry.model_validate(data)
                promotable.append(entry)
            except Exception as exc:
                log.warning(
                    "promotion_entry_parse_failed",
                    file=str(json_file),
                    error=str(exc),
                )
                continue

        return promotable

    async def run(self) -> list[dict[str, str]]:
        """Execute the promotion pipeline.

        Scans for promotable entries, creates Linear issues, and updates
        the on-disk entries with linear_issue_id + APPROVED status.

        Returns:
            List of dicts with keys ``entry_id`` and ``linear_issue_id``
            for each successfully promoted entry.
        """
        entries = self.scan_promotable_entries()
        if not entries:
            log.info("promotion_pipeline_no_entries")
            return []

        entries = await self._apply_signal_ranking(entries)
        if not entries:
            log.info("promotion_pipeline_all_suppressed")
            return []

        create_capacity = self.criteria.max_existing_linear_issues
        if self._create_issue_fn is not None and self._linear_client is not None:
            headroom = await self._ticket_cap_headroom()
            if headroom is None:
                return []
            create_capacity = min(create_capacity, headroom)
            if not await self._promotion_project_is_valid():
                return []

        promoted: list[dict[str, str]] = []

        for entry in entries:
            try:
                fp = (
                    entry.proposed_change.fingerprint
                    if entry.proposed_change and entry.proposed_change.fingerprint
                    else None
                )
                if self._linear_client is not None and fp:
                    try:
                        existing_id = await self._existing_linear_issue_for_fingerprint(fp)
                    except Exception as dup_exc:
                        log.warning(
                            "promotion_linear_dedup_query_failed",
                            entry_id=entry.entry_id,
                            error=str(dup_exc),
                            trace_id=_trace_id_from_entry(entry),
                        )
                        existing_id = None
                    if existing_id:
                        log.info(
                            "promotion_linear_duplicate_linked",
                            entry_id=entry.entry_id,
                            linear_issue_id=existing_id,
                            trace_id=_trace_id_from_entry(entry),
                        )
                        # Linking creates no ticket, so it must not consume creation
                        # capacity — otherwise a link ahead of a genuinely new
                        # candidate takes the last slot and starves it every run.
                        await self._finalize_promotion(entry, existing_id, promoted)
                        continue

                if create_capacity <= 0:
                    continue

                linear_id = await self._create_linear_issue(entry)
                if linear_id:
                    create_capacity -= 1
                    await self._finalize_promotion(entry, linear_id, promoted)
            except Exception as exc:
                log.warning(
                    "promotion_linear_create_failed",
                    entry_id=entry.entry_id,
                    error=str(exc),
                    trace_id=_trace_id_from_entry(entry),
                )

        log.info(
            "promotion_pipeline_completed",
            scanned=len(entries),
            promoted=len(promoted),
        )

        # Publish promotion.issue_created events (Phase 3, ADR-0041)
        await self._publish_promotion_events(promoted, entries)

        return promoted

    async def _ticket_cap_headroom(self) -> int | None:
        """Remaining room under the self-created ticket cap (FRE-1354, AC-5).

        The owner's rule is "don't let Seshat create more than 10 self-help
        tickets", so the gate counts **Seshat's own open tickets** — not the whole
        team backlog, which is what wedged the previous gate shut at 259/200 on
        volume Seshat did not produce.

        Fails **closed**, unlike the budget check it replaces: if the count cannot
        be read there is no evidence the cap is respected, and a governance cap that
        opens on error is not a cap.

        Returns:
            Remaining headroom (>= 1), or ``None`` when promotion must not proceed —
            already at the cap, or the count is unreadable. Both refusals are
            visible; see :meth:`_emit_funnel_event`.
        """
        assert self._linear_client is not None
        cap = settings.seshat_open_ticket_cap
        try:
            count = await self._linear_client.count_open_agent_issues(settings.linear_team_name)
        except Exception as exc:
            log.warning(
                "throttled_budget",
                reason="seshat_ticket_count_unreadable",
                error=str(exc),
                threshold=cap,
                exc_info=True,
            )
            await self._emit_funnel_event("throttled_budget", -1, cap)
            return None

        if count >= cap:
            # Event NAME, not a payload field: `event_type` in agent-logs is derived
            # from the structlog event name, and a payload key of that name would
            # overwrite it (the FRE-1066 defect). The committed funnel dashboard
            # panel queries es-agent-logs for event_type:"throttled_budget".
            log.warning(
                "throttled_budget",
                reason="seshat_open_ticket_cap_reached",
                current_count=count,
                threshold=cap,
            )
            await self._emit_funnel_event("throttled_budget", count, cap)
            return None

        if count >= math.ceil(cap * 0.8):
            log.warning(
                "issue_budget_warning",
                current_count=count,
                threshold=cap,
            )
        return cap - count

    async def _promotion_project_is_valid(self) -> bool:
        """Resolve the configured promotion project before creating anything (AC-4).

        ``settings.linear_promotion_project`` named a project that did not exist, and
        creation silently filed project-less. Validating up front means one visible
        refusal instead of a stream of mis-filed tickets.

        Returns:
            ``True`` when no project is configured or the name resolves; ``False``
            when it is configured but absent (the run is refused).
        """
        assert self._linear_client is not None
        project = settings.linear_promotion_project
        if not project:
            return True
        try:
            project_id = await self._linear_client.resolve_project_id(
                settings.linear_team_name, project
            )
        except Exception as exc:
            log.warning(
                "promotion_project_lookup_failed",
                project=project,
                error=str(exc),
                exc_info=True,
            )
            return True  # a lookup outage is not evidence of misconfiguration
        if project_id:
            return True
        log.error(
            "misconfigured_project",
            reason="linear_promotion_project_not_found",
            project=project,
            team=settings.linear_team_name,
        )
        await self._emit_funnel_event("misconfigured_project", 0, 0)
        return False

    async def _publish_promotion_events(
        self,
        promoted: list[dict[str, str]],
        all_entries: list[CaptainLogEntry],
    ) -> None:
        """Publish ``promotion.issue_created`` for each promoted entry.

        Args:
            promoted: List of dicts with ``entry_id`` and ``linear_issue_id``.
            all_entries: All scanned entries (used to look up fingerprints).
        """
        if not promoted:
            return
        from personal_agent.events.bus import get_event_bus
        from personal_agent.events.models import (
            STREAM_PROMOTION_ISSUE_CREATED,
            PromotionIssueCreatedEvent,
        )

        bus = get_event_bus()
        entry_fp: dict[str, str | None] = {
            e.entry_id: (
                e.proposed_change.fingerprint
                if e.proposed_change and e.proposed_change.fingerprint
                else None
            )
            for e in all_entries
        }
        entry_trace: dict[str, str | None] = {
            e.entry_id: _trace_id_from_entry(e) for e in all_entries
        }
        for record in promoted:
            entry_id = record.get("entry_id", "")
            linear_issue_id = record.get("linear_issue_id", "")
            fingerprint = entry_fp.get(entry_id)
            event = PromotionIssueCreatedEvent(
                entry_id=entry_id,
                linear_issue_id=linear_issue_id,
                fingerprint=fingerprint,
                source_component="captains_log.promotion",
            )
            try:
                await bus.publish(STREAM_PROMOTION_ISSUE_CREATED, event)
            except Exception as exc:
                log.warning(
                    "promotion_event_publish_failed",
                    entry_id=entry_id,
                    linear_issue_id=linear_issue_id,
                    error=str(exc),
                    trace_id=entry_trace.get(entry_id),
                )

    async def _apply_signal_ranking(self, entries: list[CaptainLogEntry]) -> list[CaptainLogEntry]:
        """Rank by realized value and drop suppressed (source, category) pairs.

        ADR-0105 D7/AC-6: the next promotion run's ordering/suppression must
        reflect the outcome-ingestion signal. Fails open — identical to
        today's un-ranked scan order — whenever ``sysgraph_repo`` is
        unavailable or a signal read errors, matching every other best-effort
        sysgraph call in this file.

        Args:
            entries: Promotable entries in scan order.

        Returns:
            Entries ranked by ``seen_count x (1 + clamp(v, +/-clamp))``
            descending, with suppressed ``(source, category)`` pairs dropped.
        """
        if self._sysgraph_repo is None:
            return entries

        scored: list[tuple[float, CaptainLogEntry]] = []
        for entry in entries:
            pc = entry.proposed_change
            # source is required (ADR-0125 D1) whenever pc is not None, so only
            # pc/category (still Optional) gate the ranked branch below.
            if pc is None or pc.category is None:
                scored.append((float(pc.seen_count) if pc else 0.0, entry))
                continue
            try:
                signal = await self._sysgraph_repo.get_signal(pc.source.value, pc.category.value)
            except Exception as exc:
                log.warning(
                    "promotion_signal_read_failed",
                    entry_id=entry.entry_id,
                    error=str(exc),
                    trace_id=_trace_id_from_entry(entry),
                )
                scored.append((float(pc.seen_count), entry))
                continue
            if signal.suppressed:
                log.info(
                    "promotion_suppressed_by_signal",
                    entry_id=entry.entry_id,
                    source=pc.source.value,
                    category=pc.category.value,
                    value=signal.value,
                    trace_id=_trace_id_from_entry(entry),
                )
                continue
            clamp = settings.signal_priority_clamp
            modulation = 1.0 + max(-clamp, min(clamp, signal.value))
            scored.append((pc.seen_count * modulation, entry))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in scored]

    async def _existing_linear_issue_for_fingerprint(self, fingerprint: str) -> str | None:
        """Return Linear issue identifier if one already contains this fingerprint.

        Uses `includeArchived=False`, so the disposition chosen when a promoted ticket is
        closed determines whether its fingerprint stays suppressed (FRE-620):

        - Noise/structural anomalies (e.g. `insufficient_data`, a stale threshold) →
          cancel-not-archive. The ticket is a permanent tombstone; this lookup keeps
          matching it (cancelled issues are still returned when archived=False), so the
          same non-issue never re-promotes.
        - Reliability anomalies (genuine regressions) → archive on close. Archiving removes
          the issue from this lookup, freeing the fingerprint so a real recurrence
          re-promotes instead of being silently suppressed forever.
        """
        if self._linear_client is None:
            return None
        fpl = fingerprint.lower().strip()
        # FRE-1354: this searched `query=` — a TITLE filter — for a fingerprint that
        # only ever appears in the description, so it matched nothing and every
        # promotion took the create branch. That is why 2026-06-26 filed nine tickets
        # for one idea. No label filter either: the historical tombstones carry only
        # `Improvement`, newer ones also carry the agent marker, and the fingerprint
        # is specific enough on its own.
        issues = await self._linear_client.list_issues(
            team=settings.linear_team_name,
            descriptionQuery=fingerprint,
            includeArchived=False,
            limit=50,
        )
        for issue in issues:
            desc = str(issue.get("description") or "")
            extracted = extract_issue_identifier_from_description(desc)
            if extracted == fpl or fpl in desc.lower():
                ident = issue.get("identifier")
                if isinstance(ident, str):
                    return ident
                oid = issue.get("id")
                if isinstance(oid, str):
                    return oid
        return None

    async def _create_linear_issue(self, entry: CaptainLogEntry) -> str | None:
        """Create a Linear issue for a promoted proposal.

        Delegates to the injected ``create_issue_fn``.  If no function was
        provided, logs the would-be promotion and returns None (dry-run).
        """
        pc = entry.proposed_change
        if pc is None:
            return None

        category_tag = pc.category.value if pc.category else "improvement"
        title = f"[{category_tag}] {pc.what[:80]}"
        description = _format_linear_description(entry)
        priority = _map_seen_count_to_priority(pc.seen_count)

        if self._create_issue_fn is None:
            log.info(
                "promotion_dry_run",
                entry_id=entry.entry_id,
                title=title,
                priority=priority,
                trace_id=_trace_id_from_entry(entry),
            )
            return None

        try:
            linear_id = await self._create_issue_fn(
                title,
                settings.linear_team_name,
                description,
                priority,
                # AGENT_AUTHORED_LABEL is the counted marker (AC-6); "Improvement" is
                # retained for continuity with ADR-0030 and the historical tickets,
                # but it is never the counting predicate.
                ["PersonalAgent", "Improvement", AGENT_AUTHORED_LABEL],
                "Needs Approval",
                settings.linear_promotion_project,
            )
            if linear_id:
                log.info(
                    "promotion_issue_created",
                    entry_id=entry.entry_id,
                    issue_id=linear_id,
                    title=title[:120],
                    category=pc.category.value if pc.category else None,
                    scope=pc.scope.value if pc.scope else None,
                    seen_count=pc.seen_count,
                    priority=priority,
                    trace_id=_trace_id_from_entry(entry),
                )
            return linear_id
        except Exception as exc:
            log.warning(
                "promotion_linear_create_failed",
                entry_id=entry.entry_id,
                error=str(exc),
                trace_id=_trace_id_from_entry(entry),
            )
            return None

    def _mark_promoted(self, entry: CaptainLogEntry, linear_issue_id: str) -> bool:
        """Update the on-disk JSON file to APPROVED with the Linear issue ID.

        The return value is load-bearing (FRE-1354). This status write is what stops
        an already-promoted entry from being rescanned: the scan rejects anything not
        ``AWAITING_APPROVAL``. When the write fails, the entry stays awaiting and
        re-promotes on every subsequent run — the one genuinely uncontained re-fire
        loop — so a swallowed failure must not be reported as a promotion.

        Args:
            entry: The entry that was promoted.
            linear_issue_id: The Linear issue identifier.

        Returns:
            ``True`` when at least one file was marked APPROVED, ``False`` otherwise.
        """
        marked = False
        for json_file in self.log_dir.glob(f"{entry.entry_id}-*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                data["status"] = CaptainLogStatus.APPROVED.value
                data["linear_issue_id"] = linear_issue_id
                json_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
                marked = True
                log.info(
                    "promotion_entry_marked_approved",
                    entry_id=entry.entry_id,
                    linear_issue_id=linear_issue_id,
                    file=str(json_file),
                    trace_id=_trace_id_from_entry(entry),
                )
            except Exception as exc:
                log.warning(
                    "promotion_mark_approved_failed",
                    entry_id=entry.entry_id,
                    error=str(exc),
                    trace_id=_trace_id_from_entry(entry),
                )
        if not marked:
            log.error(
                "promotion_entry_not_marked_approved",
                entry_id=entry.entry_id,
                linear_issue_id=linear_issue_id,
                reason="entry_would_re_promote_every_run",
                trace_id=_trace_id_from_entry(entry),
            )
        return marked

    async def _finalize_promotion(
        self,
        entry: CaptainLogEntry,
        linear_issue_id: str,
        promoted: list[dict[str, str]],
    ) -> None:
        """Mark the entry promoted and record its linkage (ADR-0105 D4/AC-3).

        Shared by both the fresh-Linear-issue path and the dedup-linked-to-an-
        existing-issue path — both represent "this proposal now maps to this
        ticket" and must both get the graph/ES linkage.

        Args:
            entry: The promoted Captain's Log entry.
            linear_issue_id: The Linear issue identifier (freshly created or an
                existing duplicate match).
            promoted: The running list of promoted {entry_id, linear_issue_id}
                dicts this call appends to.
        """
        if not self._mark_promoted(entry, linear_issue_id):
            # Not counted as promoted: the entry is still AWAITING_APPROVAL and will
            # be rescanned. Self-healing, because the fingerprint lookup now works —
            # the next run links to this same issue rather than filing a duplicate.
            return
        promoted.append({"entry_id": entry.entry_id, "linear_issue_id": linear_issue_id})
        await self._record_sysgraph_linkage(entry, linear_issue_id)
        await self._stamp_reflection_linkage(entry.entry_id, linear_issue_id)
        pc = entry.proposed_change
        if pc is not None and pc.source == ProposalSource.STATISTICAL_DETECTOR and pc.fingerprint:
            await self._stamp_insight_linkage(pc.fingerprint, linear_issue_id)

    async def _record_sysgraph_linkage(self, entry: CaptainLogEntry, linear_issue_id: str) -> None:
        """Write the proposal<->ticket PROMOTED_TO edge (ADR-0105 D4), best-effort.

        Skips (logged, not fabricated) when the entry has no source discriminator
        set to a real producer — `sysgraph.proposal.source` is `NOT NULL CHECK
        (source IN ('statistical_detector', 'reflection'))` (migration 0014) and
        cannot hold a guessed value. ADR-0125 D1 made `source` itself required, so
        the remaining case here is `ProposalSource.LEGACY_UNATTRIBUTABLE` — a
        migrated pre-ADR-0105 entry whose original producer is unknown, not a
        value the sysgraph CHECK constraint accepts.

        Args:
            entry: The promoted Captain's Log entry.
            linear_issue_id: The Linear issue identifier just linked.
        """
        if self._sysgraph_repo is None:
            return
        pc = entry.proposed_change
        if pc is None or pc.source is ProposalSource.LEGACY_UNATTRIBUTABLE or not pc.fingerprint:
            log.info(
                "sysgraph_linkage_skipped_no_source",
                entry_id=entry.entry_id,
                trace_id=_trace_id_from_entry(entry),
            )
            return
        try:
            proposal = ProposalRecord(
                source=pc.source.value,
                category=pc.category.value if pc.category else "unknown",
                fingerprint=pc.fingerprint,
                what=pc.what,
                why=pc.why,
                how=pc.how,
                seen_count=pc.seen_count,
            )
            await self._sysgraph_repo.record_promotion(
                proposal, linear_issue_id=linear_issue_id, ticket_title=entry.title
            )
        except Exception as exc:
            log.warning(
                "sysgraph_linkage_write_failed",
                entry_id=entry.entry_id,
                error=str(exc),
                trace_id=_trace_id_from_entry(entry),
            )

    async def _emit_funnel_event(self, event_type: str, current_count: int, threshold: int) -> None:
        """Emit a pipeline-level refusal as a queryable funnel-state event, best-effort.

        ADR-0105 D6: a refusal must be a first-class visible funnel state, not only a
        ``log.warning``. Writes to a small purpose-built index rather than the
        proposal-shaped ``agent-captains-reflections-*`` since this is a
        pipeline-level event, not tied to any single proposal document.

        Args:
            event_type: ``throttled_budget`` (the self-created ticket cap) or
                ``misconfigured_project`` (FRE-1354 AC-4).
            current_count: Count that tripped the refusal; ``-1`` when unreadable.
            threshold: The configured cap.
        """
        from personal_agent.captains_log.manager import CaptainLogManager

        handler = self._es_handler or CaptainLogManager._default_es_handler
        if handler is None or not getattr(handler, "_connected", False):
            return
        now = datetime.now(timezone.utc)
        index_name = f"agent-captains-funnel-events-{now.strftime('%Y-%m')}"
        try:
            await handler.es_logger.index_document(
                index_name,
                {
                    "@timestamp": now.isoformat(),
                    "event_type": event_type,
                    "current_count": current_count,
                    "threshold": threshold,
                },
            )
        except Exception as exc:
            log.warning(
                "promotion_throttle_event_emit_failed",
                event_type=event_type,
                current_count=current_count,
                threshold=threshold,
                error=str(exc),
            )

    async def _stamp_reflection_linkage(self, entry_id: str, linear_issue_id: str) -> None:
        """Stamp linear_issue_id onto the agent-captains-reflections-* doc (ADR-0105 D6), best-effort.

        Unlike ``_stamp_insight_linkage`` (STATISTICAL_DETECTOR-only, keyed on
        fingerprint against ``agent-insights-*``), this applies to every source:
        ``agent-captains-reflections-*`` is the single unified real-document source
        (FRE-715 convergence) the ADR-0105 D6 funnel dashboard reads for "promoted."
        ``_mark_promoted`` only mutates the on-disk JSON, so without this the ES
        document never carries ``linear_issue_id``. Keyed on the deterministic
        ``entry_id`` doc id (see ``CaptainLogManager.save_entry``), which is already
        explicitly mapped.

        Args:
            entry_id: The promoted entry's id (also its ES document id).
            linear_issue_id: The Linear issue identifier just linked.
        """
        from personal_agent.captains_log.manager import CaptainLogManager

        handler = self._es_handler or CaptainLogManager._default_es_handler
        if handler is None or not getattr(handler, "_connected", False):
            return
        try:
            await handler.es_logger.update_by_query(
                "agent-captains-reflections-*",
                {"term": {"entry_id": entry_id}},
                "ctx._source.linear_issue_id = params.linear_issue_id",
                {"linear_issue_id": linear_issue_id},
            )
        except Exception as exc:
            log.warning(
                "promotion_reflection_linkage_stamp_failed",
                entry_id=entry_id,
                linear_issue_id=linear_issue_id,
                error=str(exc),
            )

    async def _stamp_insight_linkage(self, fingerprint: str, linear_issue_id: str) -> None:
        """Stamp linear_issue_id onto matching agent-insights-* docs (ADR-0105 D4), best-effort.

        Matches every historical detection doc sharing this fingerprint via
        `_update_by_query` rather than a deterministic doc id — insight docs are
        an intentional time series (one per detection run), not deduped by
        fingerprint, so overwriting by id would collapse that series.

        Args:
            fingerprint: The STATISTICAL_DETECTOR proposal's fingerprint — shared
                with the `agent-insights-*` document via `_fingerprint_for_insight`.
            linear_issue_id: The Linear issue identifier just linked.
        """
        from personal_agent.captains_log.manager import CaptainLogManager

        handler = self._es_handler or CaptainLogManager._default_es_handler
        if handler is None or not getattr(handler, "_connected", False):
            return
        try:
            await handler.es_logger.update_by_query(
                "agent-insights-*",
                {"term": {"fingerprint": fingerprint}},
                "ctx._source.linear_issue_id = params.linear_issue_id",
                {"linear_issue_id": linear_issue_id},
            )
        except Exception as exc:
            log.warning(
                "promotion_insight_linkage_stamp_failed",
                fingerprint=fingerprint,
                linear_issue_id=linear_issue_id,
                error=str(exc),
            )
