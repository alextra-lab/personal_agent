"""Linear feedback polling and label handlers (ADR-0040)."""

from __future__ import annotations

import json
import pathlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from personal_agent.captains_log.linear_client import (
    FEEDBACK_LABEL_NAMES,
    LinearClient,
    extract_issue_identifier_from_description,
)
from personal_agent.captains_log.suppression import (
    feedback_history_dir,
    record_rejection_suppression,
)
from personal_agent.config import settings
from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client.factory import get_llm_client
from personal_agent.llm_client.types import ModelRole
from personal_agent.telemetry import get_logger
from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)

_LABEL_PRIORITY: dict[str, int] = {
    "Rejected": 0,
    "Duplicate": 1,
    "Approved": 2,
    "Deepen": 3,
    "Too Vague": 4,
    "Defer": 5,
}


def _project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent.parent.parent


def poller_state_path() -> pathlib.Path:
    """Path to JSON file storing processed feedback labels per Linear issue."""
    return _project_root() / "telemetry" / "feedback_poller_state.json"


class FeedbackRecord(BaseModel):
    """Preserved feedback history for an archived or closed Linear issue."""

    issue_id: str
    issue_identifier: str
    title: str
    category: str | None = None
    scope: str | None = None
    fingerprint: str | None = None
    feedback_label: str
    feedback_date: datetime
    comments: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime | None = None
    seen_count: int = 0
    time_to_feedback_hours: float | None = None
    original_description: str = ""


@dataclass(frozen=True)
class FeedbackEvent:
    """A detected feedback label on a Linear issue."""

    issue_id: str
    issue_identifier: str
    label: str
    issue_title: str
    updated_at: str


class _PollerState(BaseModel):
    """Persisted poller state."""

    handled: dict[str, list[str]] = Field(default_factory=dict)
    defer_noted: dict[str, str] = Field(default_factory=dict)


def _load_poller_state(path: pathlib.Path) -> _PollerState:
    if not path.is_file():
        return _PollerState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _PollerState.model_validate(data)
    except Exception as exc:
        log.warning("feedback_poller_state_corrupt", path=str(path), error=str(exc))
        return _PollerState()


def _save_poller_state(path: pathlib.Path, state: _PollerState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def _parse_category_scope(description: str) -> tuple[str | None, str | None]:
    cat_m = re.search(r"\*\*Category\*\*:\s*`([^`]+)`", description)
    scope_m = re.search(r"\*\*Scope\*\*:\s*`([^`]+)`", description)
    return (
        cat_m.group(1).strip() if cat_m else None,
        scope_m.group(1).strip() if scope_m else None,
    )


def _parse_seen_count(description: str) -> int:
    m = re.search(r"Observed\s+\*\*(\d+)\*\*\s+time", description)
    if m:
        return int(m.group(1))
    return 0


async def _feedback_llm_complete(role_key: str, system: str, user: str) -> str:
    """Run a single-turn completion for feedback responses."""
    client = get_llm_client(role_key)
    ctx = TraceContext.new_trace()
    try:
        resp = await client.respond(
            ModelRole.PRIMARY,
            messages=[{"role": "user", "content": user}],
            system_prompt=system,
            trace_ctx=ctx,
            priority_timeout=120.0,
        )
        text = (resp.get("content") or "").strip()
        return text or "(No model output.)"
    except Exception as exc:
        log.warning("feedback_llm_failed", error=str(exc), exc_info=True)
        return f"*Model call failed ({type(exc).__name__}: {exc}). Please retry or use comments.*"


def _reevaluation_comment_count(comments: list[dict[str, Any]]) -> int:
    n = 0
    for c in comments:
        body = str(c.get("body") or "")
        if "## Agent Re-evaluation" in body or "## Agent refinement" in body:
            n += 1
    return n


def _save_feedback_record(
    issue: dict[str, Any],
    *,
    feedback_label: str,
    comments: list[dict[str, Any]],
) -> None:
    desc = str(issue.get("description") or "")
    fp = extract_issue_identifier_from_description(desc)
    cat, scope = _parse_category_scope(desc)
    ident = str(issue.get("identifier") or issue.get("id") or "")
    iid = str(issue.get("id") or ident)
    title = str(issue.get("title") or "")
    created_raw = issue.get("createdAt") or issue.get("created_at")
    created_at: datetime | None = None
    if isinstance(created_raw, str):
        try:
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except ValueError:
            created_at = None
    now = datetime.now(timezone.utc)
    ttf: float | None = None
    if created_at:
        ttf = (now - created_at).total_seconds() / 3600.0

    comment_summaries = [
        {
            "author": str(
                c.get("user", {}).get("name", "") if isinstance(c.get("user"), dict) else ""
            ),
            "body": str(c.get("body") or "")[:2000],
        }
        for c in comments[:50]
    ]

    record = FeedbackRecord(
        issue_id=iid,
        issue_identifier=ident,
        title=title,
        category=cat,
        scope=scope,
        fingerprint=fp,
        feedback_label=feedback_label,
        feedback_date=now,
        comments=comment_summaries,
        created_at=created_at,
        seen_count=_parse_seen_count(desc),
        time_to_feedback_hours=ttf,
        original_description=desc[:12000],
    )
    safe_name = re.sub(r"[^\w\-]+", "_", ident) or iid
    path = feedback_history_dir() / f"{safe_name}.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    log.info(
        "feedback_history_captured",
        issue_id=ident,
        feedback_label=feedback_label,
        category=cat,
    )


def _mark_handled(state: _PollerState, issue_id: str, label: str) -> None:
    cur = state.handled.setdefault(issue_id, [])
    if label not in cur:
        cur.append(label)


async def handle_approved(event: FeedbackEvent, client: LinearClient) -> None:
    """Move issue to Approved workflow state."""
    await client.update_issue(event.issue_id, state="Approved")


async def handle_rejected(event: FeedbackEvent, client: LinearClient) -> None:
    """Suppress fingerprint, save history, cancel issue."""
    issue = await client.get_issue(event.issue_id)
    desc = str(issue.get("description") or "")
    fp = extract_issue_identifier_from_description(desc)
    comments_raw = await client.list_comments(event.issue_id)
    _save_feedback_record(issue, feedback_label="Rejected", comments=comments_raw)
    if fp:
        record_rejection_suppression(fp, issue_identifier=event.issue_identifier)
    await client.update_issue(event.issue_id, state="Canceled")
    log.info(
        "feedback_issue_archived",
        issue_id=event.issue_identifier,
        note="canceled_linear_issue_mcp_archive_not_exposed",
    )


async def handle_deepen(event: FeedbackEvent, client: LinearClient) -> None:
    """Re-analyze with insights model and comment."""
    issue = await client.get_issue(event.issue_id)
    comments = await client.list_comments(event.issue_id)
    if _reevaluation_comment_count(comments) >= settings.feedback_max_reevaluations:
        await client.add_comment(
            event.issue_id,
            "Maximum re-evaluation depth reached for this issue. "
            "Please approve, reject, or leave specific guidance in a comment.",
        )
        labels = [x for x in LinearClient.labels_from_issue(issue) if x != "Deepen"]
        await client.update_issue(event.issue_id, labels=labels)
        return

    desc = str(issue.get("description") or "")
    cfg = load_model_config()
    role_key = cfg.insights_role
    system = (
        "You are a senior engineer improving an internal improvement proposal. "
        "Be concrete: files, steps, risks, and measurable outcomes."
    )
    user = (
        "Original proposal (from Linear issue description):\n\n"
        f"{desc}\n\n"
        "Produce a deeper analysis: root cause, evidence, specific file paths to touch, "
        "and a revised What/Why/How. Keep Markdown sections."
    )
    analysis = await _feedback_llm_complete(role_key, system, user)
    body = (
        "## Agent Re-evaluation\n\n"
        f"**Trigger**: Deepen label\n"
        f"**Model role**: `{role_key}`\n\n"
        "### Updated Analysis\n"
        f"{analysis}\n\n"
        "---\n*This comment was generated by the agent's feedback loop (ADR-0040).*"
    )
    await client.add_comment(event.issue_id, body)
    labels = LinearClient.labels_from_issue(issue)
    new_labels = [x for x in labels if x not in ("Deepen",)]
    if "Re-evaluated" not in new_labels:
        new_labels.append("Re-evaluated")
    await client.update_issue(event.issue_id, labels=new_labels, state="Needs Approval")
    log.info(
        "feedback_deepen_completed",
        issue_id=event.issue_identifier,
        model_used=role_key,
    )


async def handle_too_vague(event: FeedbackEvent, client: LinearClient) -> None:
    """Refine proposal with more specificity."""
    issue = await client.get_issue(event.issue_id)
    comments = await client.list_comments(event.issue_id)
    if _reevaluation_comment_count(comments) >= settings.feedback_max_reevaluations:
        await client.add_comment(
            event.issue_id,
            "Maximum refinement depth reached. Please approve, reject, or comment with specifics.",
        )
        labels = [x for x in LinearClient.labels_from_issue(issue) if x != "Too Vague"]
        await client.update_issue(event.issue_id, labels=labels)
        return

    desc = str(issue.get("description") or "")
    cfg = load_model_config()
    role_key = cfg.captains_log_role
    system = (
        "You refine vague improvement proposals into actionable tickets: "
        "exact files, config keys, acceptance criteria."
    )
    user = f"Make this proposal concrete (Markdown):\n\n{desc}"
    refined = await _feedback_llm_complete(role_key, system, user)
    body = (
        "## Agent refinement\n\n"
        "**Trigger**: Too Vague label\n\n"
        "### Refined proposal\n"
        f"{refined}\n\n"
        "---\n*This comment was generated by the agent's feedback loop (ADR-0040).*"
    )
    await client.add_comment(event.issue_id, body)
    labels = LinearClient.labels_from_issue(issue)
    new_labels = [x for x in labels if x not in ("Too Vague",)]
    if "Refined" not in new_labels:
        new_labels.append("Refined")
    await client.update_issue(event.issue_id, labels=new_labels, state="Needs Approval")
    log.info("feedback_refine_completed", issue_id=event.issue_identifier)


async def handle_duplicate(event: FeedbackEvent, client: LinearClient) -> None:
    """Search for another issue with same fingerprint and set duplicateOf."""
    issue = await client.get_issue(event.issue_id)
    desc = str(issue.get("description") or "")
    fp = extract_issue_identifier_from_description(desc)
    original_id: str | None = None
    if fp:
        others = await client.list_issues(
            team=settings.linear_team_name,
            label="Improvement",
            query=fp,
            includeArchived=False,
            limit=25,
        )
        my_id = str(issue.get("id") or "")
        for o in others:
            oid = str(o.get("id") or "")
            if oid == my_id:
                continue
            odesc = str(o.get("description") or "")
            if extract_issue_identifier_from_description(odesc) == fp or fp in odesc.lower():
                original_id = str(o.get("id") or o.get("identifier") or "")
                break
    if original_id:
        await client.update_issue(event.issue_id, state="Duplicate", duplicateOf=original_id)
    else:
        await client.add_comment(
            event.issue_id,
            "Could not find the original issue automatically. "
            "Please link the duplicate manually in Linear.",
        )


_HANDLERS: dict[str, Any] = {
    "Approved": handle_approved,
    "Rejected": handle_rejected,
    "Deepen": handle_deepen,
    "Too Vague": handle_too_vague,
    "Duplicate": handle_duplicate,
}


class FeedbackPoller:
    """Polls Linear for AgentFeedback labels and dispatches handlers."""

    def __init__(
        self,
        linear_client: LinearClient,
        state_path: pathlib.Path | None = None,
    ) -> None:
        """Initialize with a configured :class:`LinearClient` and optional state path."""
        self._client = linear_client
        self._state_path = state_path or poller_state_path()

    async def check_for_feedback(self) -> list[FeedbackEvent]:
        """List recently updated PersonalAgent issues and detect new feedback labels."""
        state = _load_poller_state(self._state_path)
        issues = await self._client.list_issues(
            team=settings.linear_team_name,
            label="PersonalAgent",
            updatedAt="-P3D",
            includeArchived=False,
            limit=50,
            orderBy="updatedAt",
        )
        events: list[FeedbackEvent] = []
        for summary in issues:
            iid = str(summary.get("id") or summary.get("identifier") or "")
            if not iid:
                continue
            full = await self._client.get_issue(iid)
            full_id = str(full.get("id") or iid)
            labels = LinearClient.labels_from_issue(full)
            agent_fb = [x for x in labels if x in FEEDBACK_LABEL_NAMES]
            if not agent_fb:
                continue
            handled = set(state.handled.get(full_id, []))
            ident = str(full.get("identifier") or iid)
            title = str(full.get("title") or "")
            updated_at = str(full.get("updatedAt") or full.get("updated_at") or "")
            for lbl in sorted(agent_fb, key=lambda x: _LABEL_PRIORITY.get(x, 99)):
                if lbl in handled:
                    continue
                events.append(
                    FeedbackEvent(
                        issue_id=full_id,
                        issue_identifier=ident,
                        label=lbl,
                        issue_title=title,
                        updated_at=updated_at,
                    )
                )
        log.info("feedback_polling_check", issues_checked=len(issues), events_found=len(events))
        return events

    async def process_feedback(self, events: list[FeedbackEvent]) -> None:
        """Run handlers and persist poller state."""
        state = _load_poller_state(self._state_path)
        events_sorted = sorted(events, key=lambda e: _LABEL_PRIORITY.get(e.label, 99))

        for event in events_sorted:
            if event.label == "Defer":
                state.defer_noted[event.issue_id] = datetime.now(timezone.utc).isoformat()
                _mark_handled(state, event.issue_id, "Defer")
                log.info(
                    "feedback_event_processed",
                    issue_id=event.issue_identifier,
                    label="Defer",
                    handler="defer",
                    success=True,
                    duration_ms=0,
                )
                continue
            handler = _HANDLERS.get(event.label)
            if not handler:
                continue
            t0 = time.perf_counter()
            success = True
            try:
                await handler(event, self._client)
                _mark_handled(state, event.issue_id, event.label)
            except Exception as exc:
                success = False
                log.warning(
                    "feedback_handler_failed",
                    issue_id=event.issue_identifier,
                    label=event.label,
                    error=str(exc),
                    exc_info=True,
                )
            duration_ms = int((time.perf_counter() - t0) * 1000)
            log.info(
                "feedback_event_processed",
                issue_id=event.issue_identifier,
                label=event.label,
                handler=event.label.lower().replace(" ", "_"),
                success=success,
                duration_ms=duration_ms,
            )
        _save_poller_state(self._state_path, state)

        # Daily budget log (non-archived count)
        try:
            n = await self._client.count_non_archived_issues(settings.linear_team_name)
            if n > settings.issue_budget_threshold - 20:
                log.warning(
                    "issue_budget_warning",
                    current_count=n,
                    threshold=settings.issue_budget_threshold,
                )
        except Exception as exc:
            log.debug("feedback_budget_probe_failed", error=str(exc))
