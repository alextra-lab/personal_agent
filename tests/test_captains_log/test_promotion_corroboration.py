"""FRE-1354 — the self-improvement loop promotes again.

One test per acceptance criterion on the ticket. Every fixture that stands in for a
live proposal uses the **real** identity read from `sysgraph.proposal` on 2026-09-02,
because AC-1 explicitly fails if the reinforced path is shown only on a synthetic
first-sighting proposal.

Live identities used here (read-only query, recorded in the plan document):

===================  ==============  ============  ============  ==========
fingerprint          source          category      scope         seen_count
===================  ==============  ============  ============  ==========
4d254cb53508e2a2     statistical_..  performance   llm_client    167
ca5b48205324ad16     reflection      performance   orchestrator  54
===================  ==============  ============  ============  ==========

`4d254cb53508e2a2` is also the fingerprint on FRE-623, which is `Canceled` — so the
tombstone rule (FRE-620) applies to it and it must link rather than re-file.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from personal_agent.captains_log.corroboration import (
    stamp_corroboration,
    suppresses_proposal,
)
from personal_agent.linear_labels import AGENT_AUTHORED_LABEL

import personal_agent.captains_log.promotion as promotion_module
from personal_agent.captains_log.manager import CaptainLogManager
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    CaptainLogStatus,
    ChangeCategory,
    ChangeScope,
    ProposalSource,
    ProposedChange,
)
from personal_agent.captains_log.promotion import PromotionCriteria, PromotionPipeline
from personal_agent.sysgraph.dedup import ReadBeforeEmitDecision, ReadBeforeEmitResult

# The live 167-count proposal (has a Canceled tombstone: FRE-623).
LIVE_167_FINGERPRINT = "4d254cb53508e2a2"
LIVE_167_WHAT = "Address insight pattern: Reflection proposes changing prompt component"
LIVE_167_FIRST_SEEN = datetime(2026, 7, 7, tzinfo=timezone.utc)

# The live 54-count proposal (no Linear issue carries this fingerprint).
LIVE_54_FINGERPRINT = "ca5b48205324ad16"
LIVE_54_WHAT = "Add tool-selection guidance to tool_use_rules component to prevent redundant calls"
LIVE_54_FIRST_SEEN = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _entry(
    *,
    fingerprint: str,
    what: str,
    seen_count: int,
    first_seen: datetime,
    source: ProposalSource,
    category: ChangeCategory,
    scope: ChangeScope,
    entry_id: str = "CL-2026-09-02-001",
) -> CaptainLogEntry:
    """Build an AWAITING_APPROVAL entry carrying a corroborated proposal."""
    return CaptainLogEntry(
        entry_id=entry_id,
        timestamp=datetime.now(timezone.utc),
        type=CaptainLogEntryType.REFLECTION,
        title=what[:60],
        rationale="Corroborated across many sightings.",
        proposed_change=ProposedChange(
            what=what,
            why="Recurring pattern in telemetry.",
            how="Apply the mitigation and measure for 7 days.",
            category=category,
            scope=scope,
            source=source,
            fingerprint=fingerprint,
            seen_count=seen_count,
            first_seen=first_seen,
        ),
        status=CaptainLogStatus.AWAITING_APPROVAL,
        telemetry_refs=[],
    )


def _write(log_dir: pathlib.Path, entry: CaptainLogEntry) -> pathlib.Path:
    """Write an entry to disk in the on-disk shape the scanner reads."""
    path = log_dir / f"{entry.entry_id}-reflection.json"
    path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
    return path


def _reinforced(*, seen_count: int, fingerprint: str, first_seen: datetime) -> ReadBeforeEmitResult:
    """A REINFORCED read-before-emit result carrying canonical corroboration."""
    return ReadBeforeEmitResult(
        decision=ReadBeforeEmitDecision.REINFORCED,
        proposal_id="proposal-uuid",
        seen_count=seen_count,
        fingerprint=fingerprint,
        first_seen=first_seen,
    )


def _client(*, open_agent_issues: int = 4, existing: list[dict] | None = None) -> MagicMock:
    """A LinearClient double with the cap count and description-lookup wired."""
    lc = MagicMock()
    lc.count_open_agent_issues = AsyncMock(return_value=open_agent_issues)
    lc.list_issues = AsyncMock(return_value=existing or [])
    lc.resolve_project_id = AsyncMock(return_value="project-uuid")
    return lc


# --------------------------------------------------------------------------- #
# AC-1 — a reinforced proposal above the bar reaches promotion.
# --------------------------------------------------------------------------- #


class TestAC1ReinforcedReachesPromotion:
    """AC-1: the path that has been dead for eight weeks now reaches promotion."""

    def test_reinforced_above_bar_keeps_its_proposal(self) -> None:
        """AC-1: REINFORCED at 167 no longer erases the promotion candidate.

        This is the exact inversion the ticket describes: the most corroborated
        signal the system has was the one that could never promote.
        """
        result = _reinforced(
            seen_count=167,
            fingerprint=LIVE_167_FINGERPRINT,
            first_seen=LIVE_167_FIRST_SEEN,
        )
        assert suppresses_proposal(result, min_seen_count=3) is False

    def test_reinforced_stamps_canonical_identity(self) -> None:
        """AC-1: the surviving proposal carries the canonical row's identity.

        Not this sighting's freshly-computed hash — otherwise every sighting maps
        to a different Linear ticket, which is the 2026-06-26 flood.
        """
        pc = ProposedChange(
            what="a later sighting with different wording",
            why="w",
            how="h",
            category=ChangeCategory.PERFORMANCE,
            scope=ChangeScope.LLM_CLIENT,
            source=ProposalSource.STATISTICAL_DETECTOR,
            fingerprint="a-different-hash",
            seen_count=1,
            first_seen=datetime.now(timezone.utc),
        )
        stamped = stamp_corroboration(
            pc,
            _reinforced(
                seen_count=167,
                fingerprint=LIVE_167_FINGERPRINT,
                first_seen=LIVE_167_FIRST_SEEN,
            ),
        )
        assert stamped.seen_count == 167
        assert stamped.fingerprint == LIVE_167_FINGERPRINT
        assert stamped.first_seen == LIVE_167_FIRST_SEEN
        assert stamped.what == pc.what  # content is never rewritten

    @pytest.mark.asyncio
    async def test_live_167_proposal_links_to_its_cancelled_tombstone(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-1a: the 167 proposal reaches the promotion decision and links to FRE-623.

        FRE-623 carries this exact fingerprint and is Canceled. Per the FRE-620
        disposition rule a cancelled ticket is a permanent tombstone, so the
        correct outcome is a link, never a second ticket. Today the proposal
        never reaches this decision at all.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_167_FINGERPRINT,
                what=LIVE_167_WHAT,
                seen_count=167,
                first_seen=LIVE_167_FIRST_SEEN,
                source=ProposalSource.STATISTICAL_DETECTOR,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.LLM_CLIENT,
            ),
        )
        create = AsyncMock(side_effect=AssertionError("must not file a second ticket"))
        lc = _client(
            existing=[
                {
                    "id": "fre-623-uuid",
                    "identifier": "FRE-623",
                    "description": f"prior\n<!-- fingerprint: {LIVE_167_FINGERPRINT} -->\n",
                }
            ]
        )
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=lc,
        )
        promoted = await pipeline.run()

        assert [p["linear_issue_id"] for p in promoted] == ["FRE-623"]
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_54_proposal_without_tombstone_creates_issue(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-1b: a real above-bar reinforced proposal with no tombstone files a ticket.

        `ca5b48205324ad16` appears on no Linear issue, so this is the create branch
        of the same reinforced path — not a synthetic first sighting.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        create = AsyncMock(return_value="FRE-9001")
        lc = _client()
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=lc,
        )
        promoted = await pipeline.run()

        assert [p["linear_issue_id"] for p in promoted] == ["FRE-9001"]
        create.assert_awaited_once()
        description = create.await_args.args[2]
        assert LIVE_54_FINGERPRINT in description

    def test_authoritative_count_survives_the_captains_log_merge(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-1: the stamped count is not thrown away by merge-into-existing.

        The merge previously did `stale_local + 1`, so a live proposal at 167 whose
        local file sat at 1 became 2 and still failed the min-seen-count bar. The
        stamp is worthless unless it survives the real save path.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        manager = CaptainLogManager(log_dir=log_dir)

        stale = _entry(
            fingerprint=LIVE_167_FINGERPRINT,
            what=LIVE_167_WHAT,
            seen_count=1,
            first_seen=datetime.now(timezone.utc),
            source=ProposalSource.STATISTICAL_DETECTOR,
            category=ChangeCategory.PERFORMANCE,
            scope=ChangeScope.LLM_CLIENT,
        )
        manager.save_entry(stale)

        corroborated = _entry(
            fingerprint=LIVE_167_FINGERPRINT,
            what=LIVE_167_WHAT,
            seen_count=167,
            first_seen=LIVE_167_FIRST_SEEN,
            source=ProposalSource.STATISTICAL_DETECTOR,
            category=ChangeCategory.PERFORMANCE,
            scope=ChangeScope.LLM_CLIENT,
            entry_id="CL-2026-09-02-002",
        )
        merged_path = manager.save_entry(corroborated)

        assert merged_path is not None
        data = json.loads(merged_path.read_text(encoding="utf-8"))
        assert data["proposed_change"]["seen_count"] == 167
        assert data["proposed_change"]["first_seen"].startswith("2026-07-07")


# --------------------------------------------------------------------------- #
# AC-2 — the seeded negative.
# --------------------------------------------------------------------------- #


class TestAC2SeededNegative:
    """AC-2: a proposal below the bar still produces nothing."""

    def test_reinforced_below_bar_is_still_suppressed(self) -> None:
        """AC-2: two sightings is not corroboration; the proposal is still erased."""
        result = _reinforced(seen_count=2, fingerprint="fp-low", first_seen=LIVE_54_FIRST_SEEN)
        assert suppresses_proposal(result, min_seen_count=3) is True

    def test_decided_skip_is_always_suppressed(self) -> None:
        """AC-2: a decided kind never re-promotes, whatever its count."""
        result = ReadBeforeEmitResult(decision=ReadBeforeEmitDecision.DECIDED_SKIP, seen_count=999)
        assert suppresses_proposal(result, min_seen_count=3) is True

    @pytest.mark.asyncio
    async def test_below_bar_entry_is_never_scanned_promotable(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-2: end to end — a below-bar proposal produces no Linear issue."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint="fp-below-bar",
                what="A barely-seen idea",
                seen_count=2,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        create = AsyncMock(side_effect=AssertionError("below the bar must not promote"))
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=_client(),
        )
        assert await pipeline.run() == []
        create.assert_not_called()


# --------------------------------------------------------------------------- #
# AC-3 — the refusal is visible where a person actually looks.
# --------------------------------------------------------------------------- #


class TestAC3RefusalIsVisible:
    """AC-3: the pause reaches the committed Grafana funnel panel, not only a log line."""

    @pytest.mark.asyncio
    async def test_cap_refusal_uses_the_event_name_the_panel_queries(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-3: the structlog event is named `throttled_budget`.

        `self_improvement_funnel.json` panel 2 queries es-agent-logs for
        `event_type: "throttled_budget"`, and `event_type` is derived from the
        structlog **event name** (es_handler `_build_item`). Passing `event_type=`
        as a payload field instead would overwrite the semantic event name — the
        FRE-1066 defect that `test_es_handler.py` guards against.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=AsyncMock(return_value="FRE-1"),
            linear_client=_client(open_agent_issues=10),
        )
        with patch.object(promotion_module, "log") as mock_log:
            assert await pipeline.run() == []

        names = [c.args[0] for c in mock_log.warning.call_args_list if c.args]
        assert "throttled_budget" in names, f"expected throttled_budget, got {names}"

    @pytest.mark.asyncio
    async def test_cap_refusal_emits_the_funnel_document(self, tmp_path: pathlib.Path) -> None:
        """AC-3: the queryable funnel-state document is still written."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        es = MagicMock()
        es._connected = True
        es.es_logger.index_document = AsyncMock(return_value="doc-id")
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=AsyncMock(return_value="FRE-1"),
            linear_client=_client(open_agent_issues=10),
            es_handler=es,
        )
        await pipeline.run()

        es.es_logger.index_document.assert_awaited_once()
        args = es.es_logger.index_document.await_args.args
        assert args[0].startswith("agent-captains-funnel-events-")
        assert args[1]["event_type"] == "throttled_budget"
        assert args[1]["current_count"] == 10
        assert args[1]["threshold"] == 10


# --------------------------------------------------------------------------- #
# AC-4 — the promotion project is validated, not assumed.
# --------------------------------------------------------------------------- #


class TestAC4ProjectValidated:
    """AC-4: a promotion project that does not exist fails loudly."""

    @pytest.mark.asyncio
    async def test_unknown_project_refuses_the_run_and_creates_nothing(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-4: refuse before creating anything, rather than filing project-less."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        create = AsyncMock(side_effect=AssertionError("must not create with a bad project"))
        lc = _client()
        lc.resolve_project_id = AsyncMock(return_value=None)
        es = MagicMock()
        es._connected = True
        es.es_logger.index_document = AsyncMock(return_value="doc-id")

        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=lc,
            es_handler=es,
        )
        assert await pipeline.run() == []
        create.assert_not_called()

        args = es.es_logger.index_document.await_args.args
        assert args[1]["event_type"] == "misconfigured_project"


# --------------------------------------------------------------------------- #
# AC-5 / AC-6 — the cap counts Seshat's own tickets, via one marker.
# --------------------------------------------------------------------------- #


class TestAC5Cap:
    """AC-5: the cap counts Seshat-created open tickets and nothing else."""

    @pytest.mark.asyncio
    async def test_four_open_proceeds_and_ten_refuses(self, tmp_path: pathlib.Path) -> None:
        """AC-5: 4 open (today's real standing) promotes; seeded to 10 it refuses."""
        for open_count, expect_created in ((4, True), (10, False)):
            log_dir = tmp_path / f"cl-{open_count}"
            log_dir.mkdir()
            _write(
                log_dir,
                _entry(
                    fingerprint=LIVE_54_FINGERPRINT,
                    what=LIVE_54_WHAT,
                    seen_count=54,
                    first_seen=LIVE_54_FIRST_SEEN,
                    source=ProposalSource.REFLECTION,
                    category=ChangeCategory.PERFORMANCE,
                    scope=ChangeScope.ORCHESTRATOR,
                ),
            )
            create = AsyncMock(return_value="FRE-9002")
            pipeline = PromotionPipeline(
                log_dir=log_dir,
                criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
                create_issue_fn=create,
                linear_client=_client(open_agent_issues=open_count),
            )
            await pipeline.run()
            assert create.called is expect_created, f"open={open_count}"

    @pytest.mark.asyncio
    async def test_cap_fails_closed_when_the_count_cannot_be_read(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-5: an unreadable count refuses the run — a governance cap fails closed."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        create = AsyncMock(side_effect=AssertionError("must not create when uncounted"))
        lc = _client()
        lc.count_open_agent_issues = AsyncMock(side_effect=RuntimeError("Linear down"))
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=lc,
        )
        assert await pipeline.run() == []
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_dedup_links_do_not_consume_creation_capacity(
        self, tmp_path: pathlib.Path
    ) -> None:
        """AC-5: a link creates no ticket, so it must not eat a creation slot.

        With one slot left, a dedup-linked entry ahead of a genuinely new one would
        otherwise starve the new candidate indefinitely.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_167_FINGERPRINT,
                what=LIVE_167_WHAT,
                seen_count=167,
                first_seen=LIVE_167_FIRST_SEEN,
                source=ProposalSource.STATISTICAL_DETECTOR,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.LLM_CLIENT,
                entry_id="CL-linked",
            ),
        )
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
                entry_id="CL-new",
            ),
        )

        async def list_issues(**kwargs: object) -> list[dict]:
            if kwargs.get("descriptionQuery") == LIVE_167_FINGERPRINT:
                return [
                    {
                        "id": "u",
                        "identifier": "FRE-623",
                        "description": f"<!-- fingerprint: {LIVE_167_FINGERPRINT} -->",
                    }
                ]
            return []

        create = AsyncMock(return_value="FRE-9003")
        lc = _client(open_agent_issues=9)  # exactly one slot left under the cap of 10
        lc.list_issues = AsyncMock(side_effect=list_issues)

        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=lc,
        )
        promoted = await pipeline.run()

        ids = {p["linear_issue_id"] for p in promoted}
        assert ids == {"FRE-623", "FRE-9003"}
        create.assert_awaited_once()


class TestAC6OneMarker:
    """AC-6: "created by Seshat" is a single unambiguous predicate."""

    def test_promotion_applies_the_marker(self) -> None:
        """AC-6: the promotion path carries the marker label."""
        source = pathlib.Path(promotion_module.__file__).read_text(encoding="utf-8")
        assert "AGENT_AUTHORED_LABEL" in source

    def test_agent_tool_path_applies_the_same_marker(self) -> None:
        """AC-6: the in-turn tool path imports the same constant, not its own literal."""
        from personal_agent.tools import linear as linear_tool

        source = pathlib.Path(linear_tool.__file__).read_text(encoding="utf-8")
        assert "AGENT_AUTHORED_LABEL" in source
        assert '_AGENT_FILED_LABEL = "agent-filed"' not in source

    @pytest.mark.asyncio
    async def test_created_issue_carries_the_marker(self, tmp_path: pathlib.Path) -> None:
        """AC-6: an issue filed by promotion actually gets the counted label."""
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        create = AsyncMock(return_value="FRE-9004")
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=_client(),
        )
        await pipeline.run()
        labels = create.await_args.args[4]
        assert AGENT_AUTHORED_LABEL in labels


# --------------------------------------------------------------------------- #
# AC-7 — the 2026-06-26 batch cannot recur.
# --------------------------------------------------------------------------- #


class TestAC7NearDuplicatesAdmitOne:
    """AC-7: nine near-duplicates in one run admit at most one ticket."""

    @pytest.mark.asyncio
    async def test_nine_near_duplicates_collapse_to_one_proposal(self) -> None:
        """AC-7: the (source, category, scope) grouping admits one, and it carries
        the canonical identity so every sighting maps to the same ticket.

        This replays the real event: FRE-623..628 shared a title but carried six
        *different* fingerprints, so fingerprint dedup could never have collapsed
        them — only the grouping can.
        """
        from personal_agent.sysgraph.repository import ProposalRecord

        rows: dict[tuple[str, str, str], dict] = {}

        class GroupingRepo:
            """Implements the documented (source, category, scope) grouping contract."""

            pool = object()

            async def read_before_emit(
                self, source: str, category: str, scope: str | None, proposal: ProposalRecord
            ):
                from personal_agent.sysgraph.repository import (
                    ReadBeforeEmitResult as RepoResult,
                )

                key = (source, category, scope or "")
                if key in rows:
                    rows[key]["seen_count"] += 1
                    return RepoResult(
                        decision="reinforced",
                        proposal_id=rows[key]["id"],
                        seen_count=rows[key]["seen_count"],
                        fingerprint=rows[key]["fingerprint"],
                        first_seen=rows[key]["first_seen"],
                    )
                rows[key] = {
                    "id": "row-1",
                    "seen_count": 1,
                    "fingerprint": proposal.fingerprint,
                    "first_seen": LIVE_167_FIRST_SEEN,
                }
                return RepoResult(decision="generate_new", proposal_id="row-1")

        from personal_agent.sysgraph.dedup import check_before_emit

        repo = GroupingRepo()
        results = []
        for i in range(9):
            results.append(
                await check_before_emit(
                    repo,  # type: ignore[arg-type]
                    source="statistical_detector",
                    category="performance",
                    scope="llm_client",
                    proposal=ProposalRecord(
                        source="statistical_detector",
                        category="performance",
                        fingerprint=f"distinct-hash-{i}",
                        what=f"{LIVE_167_WHAT} variant {i}",
                        why="w",
                        how="h",
                        seen_count=1,
                        scope="llm_client",
                    ),
                )
            )

        assert len(rows) == 1, "nine near-duplicates must occupy one group"
        assert rows[("statistical_detector", "performance", "llm_client")]["seen_count"] == 9

        # Exactly one sighting generates; the rest reinforce and carry the canonical
        # identity, so they all map to the same ticket rather than eight new ones.
        generated = [r for r in results if r.decision is ReadBeforeEmitDecision.GENERATE_NEW]
        assert len(generated) == 1
        canonical = {
            r.fingerprint for r in results if r.decision is ReadBeforeEmitDecision.REINFORCED
        }
        assert canonical == {"distinct-hash-0"}


# --------------------------------------------------------------------------- #
# Repeat-run containment (codex finding 1) and promotion-state persistence.
# --------------------------------------------------------------------------- #


class TestRepeatRunContainment:
    """After a successful promotion, later runs must not re-file."""

    @pytest.mark.asyncio
    async def test_runs_two_three_and_ten_create_nothing(self, tmp_path: pathlib.Path) -> None:
        """Repeat consolidations reinforce but never re-promote.

        Containment comes from the entry being marked APPROVED and the scan's
        status check — not from the ticket cap.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        create = AsyncMock(return_value="FRE-9005")
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=create,
            linear_client=_client(),
        )
        first = await pipeline.run()
        assert len(first) == 1

        for _ in range(9):  # runs 2..10
            assert await pipeline.run() == []
        assert create.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_status_write_is_not_counted_as_promoted(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A swallowed `_mark_promoted` failure is the one uncontained re-fire loop.

        If the entry cannot be marked APPROVED it stays awaiting and re-promotes
        every run, so the run must not report it as promoted.
        """
        log_dir = tmp_path / "captains_log"
        log_dir.mkdir()
        _write(
            log_dir,
            _entry(
                fingerprint=LIVE_54_FINGERPRINT,
                what=LIVE_54_WHAT,
                seen_count=54,
                first_seen=LIVE_54_FIRST_SEEN,
                source=ProposalSource.REFLECTION,
                category=ChangeCategory.PERFORMANCE,
                scope=ChangeScope.ORCHESTRATOR,
            ),
        )
        pipeline = PromotionPipeline(
            log_dir=log_dir,
            criteria=PromotionCriteria(min_seen_count=3, min_age_days=7),
            create_issue_fn=AsyncMock(return_value="FRE-9006"),
            linear_client=_client(),
        )
        with patch.object(pipeline, "_mark_promoted", return_value=False):
            assert await pipeline.run() == []
