"""Tests for missing-skill pattern analysis in InsightsEngine (FRE-328).

Verifies that ``detect_missing_skill_patterns`` produces an Insight only when a
skill name clears both thresholds:

- ``MIN_MISSING_SKILL_REQUESTS`` (3+) requests
- ``MIN_MISSING_SKILL_SESSIONS`` (2+) distinct sessions

The insight then flows through ``create_captain_log_proposals`` into the
existing Captain's Log → promotion → Linear pipeline (covered by other tests).
"""

from unittest.mock import AsyncMock

import pytest

from personal_agent.insights.engine import (
    MIN_MISSING_SKILL_REQUESTS,
    MIN_MISSING_SKILL_SESSIONS,
    InsightsEngine,
)
from personal_agent.telemetry.queries import MissingSkillRequest, TelemetryQueries


@pytest.fixture(autouse=True)
def _no_context_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off Elasticsearch: the context query returns nothing by default."""
    monkeypatch.setattr(
        TelemetryQueries,
        "get_missing_skill_requests",
        AsyncMock(return_value=[]),
    )


@pytest.mark.asyncio
class TestDetectMissingSkillEmpty:
    """No data / under-threshold cases return empty list."""

    async def test_returns_empty_when_no_buckets(self) -> None:
        """No missing_skill_requested events → no insights."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[]
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []

    async def test_below_request_threshold_skipped(self) -> None:
        """Skill requested fewer than MIN_MISSING_SKILL_REQUESTS times → no insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("rare-skill", MIN_MISSING_SKILL_REQUESTS - 1, 3)],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []

    async def test_below_session_threshold_skipped(self) -> None:
        """Skill requested often but in a single session → no insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("loop-skill", 10, MIN_MISSING_SKILL_SESSIONS - 1)],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []

    async def test_es_failure_returns_empty(self) -> None:
        """ES error → analysis_failed logged, returns []."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("ES unavailable"),
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert insights == []


@pytest.mark.asyncio
class TestDetectMissingSkillProducesInsight:
    """Above-threshold clusters produce structured insights."""

    async def test_clears_thresholds_produces_insight(self) -> None:
        """3+ requests across 2+ sessions → one missing_skill insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("kibana-dashboard-builder", 5, 3)],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert len(insights) == 1
        ins = insights[0]
        assert ins.insight_type == "missing_skill"
        assert ins.pattern_kind == "missing_skill_requested"
        assert ins.actionable is True
        assert ins.evidence["requested_name"] == "kibana-dashboard-builder"
        assert ins.evidence["request_count"] == 5
        assert ins.evidence["distinct_sessions"] == 3
        assert ins.evidence["window_days"] == 7
        assert "kibana-dashboard-builder" in ins.title
        assert "kibana-dashboard-builder" in ins.summary
        assert 0.60 <= ins.confidence <= 0.95

    async def test_confidence_scales_with_request_count(self) -> None:
        """Higher request counts produce higher confidence (clamped to 0.95)."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ("hot-skill", 50, 10),
                ("warm-skill", 3, 2),
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        by_name = {i.evidence["requested_name"]: i for i in insights}
        assert by_name["hot-skill"].confidence > by_name["warm-skill"].confidence
        assert by_name["hot-skill"].confidence <= 0.95

    async def test_multiple_skills_produce_multiple_insights(self) -> None:
        """Each qualifying skill name becomes its own insight."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ("skill-a", 4, 2),
                ("skill-b", 3, 2),
                ("skill-c", 1, 1),  # below thresholds → filtered
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        names = {i.evidence["requested_name"] for i in insights}
        assert names == {"skill-a", "skill-b"}


@pytest.mark.asyncio
class TestMissingSkillFingerprint:
    """Insight → Captain's Log proposal fingerprint dedups on skill name."""

    async def test_proposal_fingerprint_per_skill_name(self) -> None:
        """Same skill name → same fingerprint (existing dedup increments seen_count)."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("repeat-skill", 5, 3)],
        )
        first = await engine.detect_missing_skill_patterns(days=7)
        proposals_a = await engine.create_captain_log_proposals(first)

        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[("repeat-skill", 12, 6)],
        )
        second = await engine.detect_missing_skill_patterns(days=7)
        proposals_b = await engine.create_captain_log_proposals(second)

        assert proposals_a and proposals_b
        fp_a = proposals_a[0].proposed_change.fingerprint  # type: ignore[union-attr]
        fp_b = proposals_b[0].proposed_change.fingerprint  # type: ignore[union-attr]
        assert fp_a == fp_b, "fingerprint must be stable per skill name across runs"

    async def test_different_skills_get_different_fingerprints(self) -> None:
        """Different skill names → distinct fingerprints."""
        engine = InsightsEngine()
        engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                ("skill-x", 5, 3),
                ("skill-y", 5, 3),
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        proposals = await engine.create_captain_log_proposals(insights)
        fingerprints = {
            p.proposed_change.fingerprint  # type: ignore[union-attr]
            for p in proposals
            if p.proposed_change is not None
        }
        assert len(fingerprints) == 2


def _req(name: str, session: str | None, host: str | None) -> MissingSkillRequest:
    return MissingSkillRequest(
        requested_name=name,
        session_id=session,
        trace_id=f"trace-{name}-{session}",
        host=host,
    )


def _engine(
    buckets: list[tuple[str, int, int]], requests: list[MissingSkillRequest]
) -> InsightsEngine:
    engine = InsightsEngine()
    engine._queries.get_missing_skill_buckets = AsyncMock(  # type: ignore[method-assign]
        return_value=buckets,
    )
    engine._queries.get_missing_skill_requests = AsyncMock(  # type: ignore[method-assign]
        return_value=requests,
    )
    return engine


@pytest.mark.asyncio
class TestHostClusterProducesInsight:
    """AC-2: differently named requests that reached one host are one gap (FRE-1322)."""

    async def test_the_2026_08_29_requests_produce_one_finding(self) -> None:
        """Three singleton names, two sessions, one host → one insight on the host.

        Mirrors the live events: ``observability-fetcher`` and ``grafana-fetcher``
        (different sessions) and ``session-analyzer`` (same session as the second),
        each a singleton by name, each reaching ``tempo:3200``.
        """
        engine = _engine(
            buckets=[("observability-fetcher", 1, 1), ("grafana-fetcher", 1, 1),
                     ("session-analyzer", 1, 1)],
            requests=[
                _req("observability-fetcher", "session-a", "tempo:3200"),
                _req("grafana-fetcher", "session-b", "tempo:3200"),
                _req("session-analyzer", "session-b", "tempo:3200"),
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert len(insights) == 1
        ins = insights[0]
        assert ins.insight_type == "missing_skill"
        assert ins.pattern_kind == "missing_skill_requested"
        assert ins.actionable is True
        assert ins.title == "Create skill for `tempo:3200`"
        assert ins.evidence["service_host"] == "tempo:3200"
        assert ins.evidence["request_count"] == 3
        assert ins.evidence["distinct_sessions"] == 2
        assert ins.evidence["window_days"] == 7
        assert ins.evidence["requested_names"] == "grafana-fetcher, observability-fetcher, session-analyzer"
        assert 0.60 <= ins.confidence <= 0.95

    async def test_cluster_replaces_the_name_insight_it_covers(self) -> None:
        """A name that fires alone is dropped when its host cluster fires."""
        engine = _engine(
            buckets=[("telemetry-analyzer", 3, 3), ("unrelated-skill", 4, 2)],
            requests=[
                _req("telemetry-analyzer", "s-1", "elasticsearch:9200"),
                _req("telemetry-analyzer", "s-2", "elasticsearch:9200"),
                _req("logs-analyzer", "s-3", "elasticsearch:9200"),
            ],
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        titles = sorted(i.title for i in insights)
        assert len(insights) == 2
        assert titles[0] == "Create skill for `elasticsearch:9200`"
        assert insights[0].evidence.get("requested_name") != "telemetry-analyzer"
        assert any(i.evidence.get("requested_name") == "unrelated-skill" for i in insights)
        assert not any(i.evidence.get("requested_name") == "telemetry-analyzer" for i in insights)

    async def test_context_query_failure_keeps_the_name_insights(self) -> None:
        """A failing context query must not remove the exact-name findings."""
        engine = _engine(buckets=[("skill-a", 4, 2)], requests=[])
        engine._queries.get_missing_skill_requests = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("ES unavailable"),
        )
        insights = await engine.detect_missing_skill_patterns(days=7)
        assert [i.evidence["requested_name"] for i in insights] == ["skill-a"]

    async def test_until_is_passed_to_both_queries(self) -> None:
        """A replay bound reaches the name query and the context query."""
        from datetime import datetime, timezone

        until = datetime(2026, 8, 29, 6, 6, 25, tzinfo=timezone.utc)
        engine = _engine(buckets=[], requests=[])
        await engine.detect_missing_skill_patterns(days=7, until=until)
        engine._queries.get_missing_skill_buckets.assert_awaited_once_with(days=7, until=until)  # type: ignore[attr-defined]
        engine._queries.get_missing_skill_requests.assert_awaited_once_with(days=7, until=until)  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestHostClusterSeededNegatives:
    """AC-3: sets below the bar, or without a shared host, produce nothing."""

    async def test_single_isolated_request(self) -> None:
        engine = _engine(
            buckets=[("grafana-fetcher", 1, 1)],
            requests=[_req("grafana-fetcher", "s-1", "tempo:3200")],
        )
        assert await engine.detect_missing_skill_patterns(days=7) == []

    async def test_two_requests_in_two_sessions_stay_below_the_bar(self) -> None:
        """The 08-29 pair on its own: 2 requests is below MIN_MISSING_SKILL_REQUESTS."""
        engine = _engine(
            buckets=[("observability-fetcher", 1, 1), ("grafana-fetcher", 1, 1)],
            requests=[
                _req("observability-fetcher", "s-1", "tempo:3200"),
                _req("grafana-fetcher", "s-2", "tempo:3200"),
            ],
        )
        assert MIN_MISSING_SKILL_REQUESTS == 3
        assert await engine.detect_missing_skill_patterns(days=7) == []

    async def test_many_requests_in_one_session_stay_below_the_bar(self) -> None:
        """One session that fetched many pages is one workaround, not a recurring gap."""
        engine = _engine(
            buckets=[("citation-checker", 2, 1), ("web-summarizer", 2, 1)],
            requests=[
                _req("citation-checker", "s-1", "bbqcooktime.com"),
                _req("web-summarizer", "s-1", "bbqcooktime.com"),
                _req("citation-checker", "s-1", "bbqcooktime.com"),
                _req("web-summarizer", "s-1", "bbqcooktime.com"),
            ],
        )
        assert MIN_MISSING_SKILL_SESSIONS == 2
        assert await engine.detect_missing_skill_patterns(days=7) == []

    async def test_requests_without_a_host_never_cluster(self) -> None:
        """Guessed names from turns that reached no host are noise, however many."""
        engine = _engine(
            buckets=[(f"guess-{i}", 1, 1) for i in range(6)],
            requests=[_req(f"guess-{i}", f"s-{i % 3}", None) for i in range(6)],
        )
        assert await engine.detect_missing_skill_patterns(days=7) == []

    async def test_different_hosts_do_not_merge(self) -> None:
        """Three requests over three hosts are three singletons."""
        engine = _engine(
            buckets=[("a-skill", 1, 1), ("b-skill", 1, 1), ("c-skill", 1, 1)],
            requests=[
                _req("a-skill", "s-1", "tempo:3200"),
                _req("b-skill", "s-2", "elasticsearch:9200"),
                _req("c-skill", "s-3", "site.api.espn.com"),
            ],
        )
        assert await engine.detect_missing_skill_patterns(days=7) == []

    async def test_request_without_a_session_does_not_count_as_a_session(self) -> None:
        """Three requests, one real session, two with no session → below the 2-session bar."""
        engine = _engine(
            buckets=[("a-skill", 1, 0), ("b-skill", 1, 0), ("c-skill", 1, 1)],
            requests=[
                _req("a-skill", None, "tempo:3200"),
                _req("b-skill", None, "tempo:3200"),
                _req("c-skill", "s-1", "tempo:3200"),
            ],
        )
        assert await engine.detect_missing_skill_patterns(days=7) == []


@pytest.mark.asyncio
class TestHostClusterFingerprint:
    """The cluster insight dedups per host across scans."""

    async def test_fingerprint_is_stable_as_the_count_grows(self) -> None:
        three = [
            _req("a-skill", "s-1", "tempo:3200"),
            _req("b-skill", "s-2", "tempo:3200"),
            _req("c-skill", "s-2", "tempo:3200"),
        ]
        engine = _engine(buckets=[], requests=three)
        first = await engine.create_captain_log_proposals(
            await engine.detect_missing_skill_patterns(days=7)
        )
        engine._queries.get_missing_skill_requests = AsyncMock(  # type: ignore[method-assign]
            return_value=three + [_req("d-skill", "s-3", "tempo:3200")],
        )
        second = await engine.create_captain_log_proposals(
            await engine.detect_missing_skill_patterns(days=7)
        )
        assert first and second
        assert first[0].proposed_change.fingerprint == second[0].proposed_change.fingerprint  # type: ignore[union-attr]
