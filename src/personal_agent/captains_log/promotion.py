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
import pathlib
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from personal_agent.captains_log.linear_client import (
    LinearClient,
    extract_issue_identifier_from_description,
)
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogStatus,
    ChangeCategory,
)
from personal_agent.config import settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

LinearIssueCreator = Callable[
    [str, str, str, int, list[str], str, str],
    Coroutine[Any, Any, str | None],
]


class PromotionCriteria(BaseModel):
    """Configurable criteria for promoting a proposal to Linear."""

    min_seen_count: int = Field(
        default=3, ge=1, description="Minimum times the proposal was observed"
    )
    min_age_days: int = Field(default=7, ge=0, description="Minimum days since first_seen")
    max_existing_linear_issues: int = Field(
        default=5, ge=1, description="Cap on issues created per pipeline run (ADR-0040 default 5)"
    )
    excluded_categories: list[ChangeCategory] = Field(
        default_factory=list, description="Categories to skip during promotion"
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
    ) -> None:
        """Initialize the promotion pipeline.

        Args:
            log_dir: Path to the Captain's Log JSON directory.
            criteria: Optional promotion criteria overrides.
            create_issue_fn: Async callable(title, team, description, priority,
                labels, state, project) -> issue_identifier | None.
                If None, promotable entries are identified but not pushed to Linear.
            linear_client: Optional Linear MCP client for budget and duplicate checks.
        """
        if log_dir is None:
            project_root = pathlib.Path(__file__).parent.parent.parent.parent
            log_dir = project_root / "telemetry" / "captains_log"
        self.log_dir = log_dir
        self.criteria = criteria or PromotionCriteria()
        self._create_issue_fn = create_issue_fn
        self._linear_client = linear_client

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

        if self._create_issue_fn is not None and self._linear_client is not None:
            try:
                count = await self._linear_client.count_non_archived_issues(
                    settings.linear_team_name
                )
                if count > settings.issue_budget_threshold:
                    log.warning(
                        "issue_budget_promotion_paused",
                        current_count=count,
                        threshold=settings.issue_budget_threshold,
                    )
                    return []
                if count > settings.issue_budget_threshold - 20:
                    log.warning(
                        "issue_budget_warning",
                        current_count=count,
                        threshold=settings.issue_budget_threshold,
                    )
            except Exception as budget_exc:
                log.warning(
                    "promotion_budget_check_failed",
                    error=str(budget_exc),
                    exc_info=True,
                )

        capped = entries[: self.criteria.max_existing_linear_issues]
        promoted: list[dict[str, str]] = []

        for entry in capped:
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
                        )
                        existing_id = None
                    if existing_id:
                        log.info(
                            "promotion_linear_duplicate_linked",
                            entry_id=entry.entry_id,
                            linear_issue_id=existing_id,
                        )
                        self._mark_promoted(entry, existing_id)
                        promoted.append(
                            {"entry_id": entry.entry_id, "linear_issue_id": existing_id}
                        )
                        continue

                linear_id = await self._create_linear_issue(entry)
                if linear_id:
                    self._mark_promoted(entry, linear_id)
                    promoted.append({"entry_id": entry.entry_id, "linear_issue_id": linear_id})
            except Exception as exc:
                log.warning(
                    "promotion_linear_create_failed",
                    entry_id=entry.entry_id,
                    error=str(exc),
                )

        log.info(
            "promotion_pipeline_completed",
            scanned=len(entries),
            promoted=len(promoted),
        )

        # Publish promotion.issue_created events (Phase 3, ADR-0041)
        await self._publish_promotion_events(promoted, entries)

        return promoted

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
        for record in promoted:
            entry_id = record.get("entry_id", "")
            linear_issue_id = record.get("linear_issue_id", "")
            fingerprint = entry_fp.get(entry_id)
            event = PromotionIssueCreatedEvent(
                entry_id=entry_id,
                linear_issue_id=linear_issue_id,
                fingerprint=fingerprint,
            )
            try:
                await bus.publish(STREAM_PROMOTION_ISSUE_CREATED, event)
            except Exception as exc:
                log.warning(
                    "promotion_event_publish_failed",
                    entry_id=entry_id,
                    linear_issue_id=linear_issue_id,
                    error=str(exc),
                )

    async def _existing_linear_issue_for_fingerprint(self, fingerprint: str) -> str | None:
        """Return Linear issue identifier if one already contains this fingerprint."""
        if self._linear_client is None:
            return None
        fpl = fingerprint.lower().strip()
        issues = await self._linear_client.list_issues(
            team=settings.linear_team_name,
            label="Improvement",
            query=fingerprint,
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
            )
            return None

        try:
            linear_id = await self._create_issue_fn(
                title,
                settings.linear_team_name,
                description,
                priority,
                ["PersonalAgent", "Improvement"],
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
                )
            return linear_id
        except Exception as exc:
            log.warning(
                "promotion_linear_create_failed",
                entry_id=entry.entry_id,
                error=str(exc),
            )
            return None

    def _mark_promoted(self, entry: CaptainLogEntry, linear_issue_id: str) -> None:
        """Update the on-disk JSON file to APPROVED with the Linear issue ID.

        Args:
            entry: The entry that was promoted.
            linear_issue_id: The Linear issue identifier.
        """
        for json_file in self.log_dir.glob(f"{entry.entry_id}-*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                data["status"] = CaptainLogStatus.APPROVED.value
                data["linear_issue_id"] = linear_issue_id
                json_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
                log.info(
                    "promotion_entry_marked_approved",
                    entry_id=entry.entry_id,
                    linear_issue_id=linear_issue_id,
                    file=str(json_file),
                )
            except Exception as exc:
                log.warning(
                    "promotion_mark_approved_failed",
                    entry_id=entry.entry_id,
                    error=str(exc),
                )
