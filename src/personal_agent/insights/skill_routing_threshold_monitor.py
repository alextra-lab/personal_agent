"""Skill routing threshold monitor — ADR-0066 D2.

Daily job that:
1. Queries ES for the p95 of ``injected_chars`` on ``skill_index_assembled``
   events over the last 7 days.
2. Converts chars → tokens (÷ 4) and persists the datapoint to a rolling
   JSON state file.
3. When the threshold is exceeded for two or more consecutive days, files a
   ``Needs Approval`` Linear issue recommending a routing-mode flip.
4. Idempotent — skips filing if an open trigger ticket already exists.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.captains_log.linear_client import LinearClient
    from personal_agent.telemetry.queries import TelemetryQueries

log = get_logger(__name__)

_CHARS_PER_TOKEN: float = 4.0
_TRIGGER_TICKET_TITLE_MARKER: str = "Skill index p95 threshold exceeded"


class SkillRoutingThresholdMonitor:
    """ADR-0066 D2 monitor: file a Linear ticket when skill injection p95 is too high.

    Args:
        queries: ``TelemetryQueries`` instance for ES access.
        linear_client: ``LinearClient`` for ticket filing; ``None`` disables filing.
        output_dir: Directory for the rolling state file.
        threshold_tokens: p95 token threshold (default 6,000).
        window_days: ES look-back window for computing p95.
        linear_team: Linear team name for ticket filing.
    """

    def __init__(
        self,
        queries: TelemetryQueries,
        linear_client: LinearClient | None,
        output_dir: Path = Path("telemetry/skill_routing_monitor"),
        threshold_tokens: int = 6000,
        window_days: int = 7,
        linear_team: str = "FrenchForest",
    ) -> None:
        """Initialise with ES queries, optional Linear client, and threshold config."""
        self._queries = queries
        self._linear_client = linear_client
        self._output_dir = output_dir
        self._threshold_tokens = threshold_tokens
        self._window_days = window_days
        self._linear_team = linear_team
        self._state_path = output_dir / "state.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Execute one daily monitor pass.

        Fetches p95 from ES, persists the datapoint, and files a Linear
        ticket if the threshold has been exceeded for two or more consecutive
        days and no open trigger ticket already exists.
        """
        today = date.today()
        log.info("skill_routing_threshold_monitor_started", date=today.isoformat())

        try:
            p95_chars = await self._queries.get_skill_index_p95_chars(days=self._window_days)
        except Exception as exc:
            log.warning(
                "skill_routing_threshold_monitor_es_failed",
                error=str(exc),
            )
            return

        p95_tokens = p95_chars / _CHARS_PER_TOKEN
        state = self._load_state()
        state = self._upsert_reading(state, today, p95_chars, p95_tokens)
        self._save_state(state)

        consecutive = self._count_consecutive_exceeded(state["readings"], today)
        log.info(
            "skill_routing_threshold_monitor_reading",
            date=today.isoformat(),
            p95_chars=p95_chars,
            p95_tokens=p95_tokens,
            threshold_tokens=self._threshold_tokens,
            consecutive_days_exceeded=consecutive,
        )

        if consecutive >= 2:
            await self._maybe_file_ticket(state, today, p95_tokens, consecutive)

    # ------------------------------------------------------------------
    # State file helpers
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        """Load state JSON; return empty skeleton on missing or corrupt file."""
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning(
                    "skill_routing_monitor_state_corrupt",
                    path=str(self._state_path),
                )
        return {"readings": [], "last_ticket_identifier": None, "last_ticket_filed_date": None}

    def _save_state(self, state: dict[str, Any]) -> None:
        """Persist state to JSON (creates output dir if needed)."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2))

    @staticmethod
    def _upsert_reading(
        state: dict[str, Any],
        today: date,
        p95_chars: float,
        p95_tokens: float,
    ) -> dict[str, Any]:
        """Add or update today's reading in the state; trims to last 90 days."""
        readings: list[dict[str, Any]] = state.get("readings", [])
        today_iso = today.isoformat()
        readings = [r for r in readings if r.get("date") != today_iso]
        readings.append({"date": today_iso, "p95_chars": p95_chars, "p95_tokens": p95_tokens})
        cutoff = (today - timedelta(days=90)).isoformat()
        readings = [r for r in readings if r.get("date", "") >= cutoff]
        return {**state, "readings": sorted(readings, key=lambda r: r["date"])}

    def _count_consecutive_exceeded(self, readings: list[dict[str, Any]], today: date) -> int:
        """Count consecutive days ending today where p95_tokens >= threshold."""
        by_date = {r["date"]: r.get("p95_tokens", 0.0) for r in readings}
        count = 0
        check = today
        while True:
            key = check.isoformat()
            val = by_date.get(key)
            if val is None or val < self._threshold_tokens:
                break
            count += 1
            check -= timedelta(days=1)
        return count

    # ------------------------------------------------------------------
    # Linear ticket filing
    # ------------------------------------------------------------------

    async def _maybe_file_ticket(
        self,
        state: dict[str, Any],
        today: date,
        p95_tokens: float,
        consecutive: int,
    ) -> None:
        """File a Linear ticket unless one is already open."""
        if self._linear_client is None:
            log.info(
                "skill_routing_threshold_monitor_no_linear",
                reason="linear_client not configured",
            )
            return

        try:
            open_tickets = await self._linear_client.list_issues(
                team=self._linear_team,
                query=_TRIGGER_TICKET_TITLE_MARKER,
                state="Needs Approval",
            )
        except Exception as exc:
            log.warning(
                "skill_routing_threshold_monitor_linear_search_failed",
                error=str(exc),
            )
            return

        if open_tickets:
            identifiers = [t.get("identifier") for t in open_tickets]
            log.info(
                "skill_routing_threshold_monitor_ticket_exists",
                open_tickets=identifiers,
            )
            return

        trend = self._build_trend_table(state["readings"])
        description = self._build_ticket_description(
            p95_tokens=p95_tokens,
            threshold_tokens=self._threshold_tokens,
            consecutive_days=consecutive,
            trend_table=trend,
        )
        title = (
            f"{_TRIGGER_TICKET_TITLE_MARKER}: "
            f"{p95_tokens:.0f} tokens p95 ≥ {self._threshold_tokens} "
            f"({consecutive} consecutive days)"
        )

        try:
            identifier = await self._linear_client.create_issue(
                title=title,
                team=self._linear_team,
                description=description,
                priority=2,  # High
                labels=["PersonalAgent", "Tier-2:Sonnet"],
                state="Needs Approval",
                project="",
            )
        except Exception as exc:
            log.warning(
                "skill_routing_threshold_monitor_ticket_failed",
                error=str(exc),
            )
            return

        state["last_ticket_identifier"] = identifier
        state["last_ticket_filed_date"] = today.isoformat()
        self._save_state(state)
        log.info(
            "skill_routing_threshold_monitor_ticket_filed",
            identifier=identifier,
            p95_tokens=p95_tokens,
            consecutive_days=consecutive,
        )

    @staticmethod
    def _build_trend_table(readings: list[dict[str, Any]]) -> str:
        """Build a markdown table of the last 14 daily readings."""
        recent = sorted(readings, key=lambda r: r["date"])[-14:]
        if not recent:
            return "_No readings yet._"
        lines = ["| date | p95 chars | p95 tokens |", "|---|---:|---:|"]
        for r in recent:
            lines.append(
                f"| {r['date']} | {r.get('p95_chars', 0):.0f} | {r.get('p95_tokens', 0):.0f} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _build_ticket_description(
        p95_tokens: float,
        threshold_tokens: int,
        consecutive_days: int,
        trend_table: str,
    ) -> str:
        """Build the Markdown body for the Linear trigger ticket."""
        return (
            f"## Skill index injection size has exceeded the ADR-0066 D2 threshold\n\n"
            f"**p95 injection size**: {p95_tokens:.0f} tokens/request "
            f"(threshold: {threshold_tokens} tokens)\n\n"
            f"**Consecutive days exceeded**: {consecutive_days}\n\n"
            f"## Recommended action\n\n"
            f"Flip `AGENT_SKILL_ROUTING_MODE=model_decided` in `.env` and restart "
            f"the container. This is a config-only change — no code deployment required.\n\n"
            f"```\n"
            f"AGENT_SKILL_ROUTING_MODE=model_decided\n"
            f"```\n\n"
            f"After restarting, the compact skill index path becomes active and "
            f"`read_skill` becomes the primary skill-fetch mechanism.\n\n"
            f"## 14-day p95 trend\n\n"
            f"{trend_table}\n\n"
            f"---\n"
            f"_Filed automatically by `insights.skill_routing_threshold_monitor` "
            f"(ADR-0066 D2). Verify the trend before acting._"
        )
