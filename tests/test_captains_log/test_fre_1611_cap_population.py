"""FRE-1611 AC-1 — the self-created ticket cap counts only pipeline-filed tickets.

The in-turn agent tool filed FRE-359..362 and the owner approved them. They carry
``agent-filed`` and nothing else, and they filled the whole cap of 4. The cap exists
to throttle the promotion pipeline, so it must count only what the pipeline filed.

The fake below evaluates the Linear filter against seeded issues. A test that only
inspects the filter dict would pass for any label name. This one fails if the filter
lets a tool ticket through.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import personal_agent.captains_log.promotion as promotion_module
from personal_agent.captains_log.linear_client import LinearClient
from personal_agent.captains_log.promotion import PromotionPipeline
from personal_agent.linear_labels import (
    AGENT_AUTHORED_LABEL,
    PIPELINE_FILED_LABEL,
)

_TOOL_LABELS = ["PersonalAgent", AGENT_AUTHORED_LABEL]
_PIPELINE_LABELS = ["PersonalAgent", "Improvement", AGENT_AUTHORED_LABEL, PIPELINE_FILED_LABEL]


def _issue(identifier: str, labels: list[str], state_type: str = "backlog") -> dict[str, Any]:
    return {"id": identifier, "labels": labels, "state_type": state_type}


def _fake_linear(issues: list[dict[str, Any]]) -> Any:
    """Return a ``_call`` stand-in that applies the issue filter to ``issues``."""

    async def _call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        filt = variables["filter"]
        excluded = set(filt["state"]["type"]["nin"])
        wanted = set(filt["labels"]["some"]["name"]["in"])
        nodes = [
            {"id": i["id"]}
            for i in issues
            if i["state_type"] not in excluded and wanted & set(i["labels"])
        ]
        return {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}}

    return _call


class TestCapCountsPipelineTicketsOnly:
    """AC-1: tool tickets do not count toward the cap."""

    @pytest.mark.asyncio
    async def test_four_tool_tickets_leave_the_full_headroom(self, tmp_path: pathlib.Path) -> None:
        """4 open tool tickets and 0 pipeline tickets leave a headroom of 10."""
        issues = [_issue(f"FRE-{n}", _TOOL_LABELS, "backlog") for n in (359, 360, 361, 362)]
        client = LinearClient()
        with (
            patch.object(client, "_call", side_effect=_fake_linear(issues)),
            patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10),
        ):
            pipeline = PromotionPipeline(log_dir=tmp_path, linear_client=client)
            headroom = await pipeline._ticket_cap_headroom()
        assert headroom == 10

    @pytest.mark.asyncio
    async def test_pipeline_tickets_count_and_terminal_ones_do_not(
        self, tmp_path: pathlib.Path
    ) -> None:
        """3 open pipeline tickets count. A canceled pipeline ticket does not."""
        issues = [
            _issue("FRE-1", _PIPELINE_LABELS),
            _issue("FRE-2", _PIPELINE_LABELS, "started"),
            _issue("FRE-3", _PIPELINE_LABELS),
            _issue("FRE-4", _PIPELINE_LABELS, "canceled"),
            *[_issue(f"FRE-{n}", _TOOL_LABELS) for n in (359, 360, 361, 362)],
        ]
        client = LinearClient()
        with (
            patch.object(client, "_call", side_effect=_fake_linear(issues)),
            patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10),
        ):
            pipeline = PromotionPipeline(log_dir=tmp_path, linear_client=client)
            headroom = await pipeline._ticket_cap_headroom()
        assert headroom == 7


class TestPipelineAppliesItsOwnMarker:
    """The label the cap counts must reach every ticket the pipeline files."""

    @pytest.mark.asyncio
    async def test_issue_create_labels_include_the_pipeline_marker(
        self, tmp_path: pathlib.Path
    ) -> None:
        """``_create_linear_issue`` passes PIPELINE_FILED_LABEL to the creator."""
        from personal_agent.captains_log.models import (
            CaptainLogEntry,
            CaptainLogEntryType,
            ChangeCategory,
            ChangeScope,
            ProposalSource,
            ProposedChange,
        )

        entry = CaptainLogEntry(
            entry_id="CL-test-1611",
            type=CaptainLogEntryType.REFLECTION,
            title="t",
            rationale="r",
            proposed_change=ProposedChange(
                what="Do the thing",
                why="Because",
                how="Carefully",
                category=ChangeCategory.RELIABILITY,
                scope=ChangeScope.LLM_CLIENT,
                fingerprint="fp-1611",
                seen_count=5,
                source=ProposalSource.STATISTICAL_DETECTOR,
            ),
        )
        create = AsyncMock(return_value="FRE-9999")
        pipeline = PromotionPipeline(log_dir=tmp_path, create_issue_fn=create)
        await pipeline._create_linear_issue(entry)
        labels = create.await_args.args[4]
        assert PIPELINE_FILED_LABEL in labels
        assert AGENT_AUTHORED_LABEL in labels


class TestClientCreatesTheMarkerLabel:
    """A cap marker that fails to attach makes a ticket invisible to the cap."""

    @pytest.mark.asyncio
    async def test_pipeline_marker_is_auto_created(self) -> None:
        """``create_issue`` passes a colour for PIPELINE_FILED_LABEL so it can be created."""
        client = LinearClient()
        seen: dict[str, str | None] = {}

        async def _label_id(
            team_id: str, name: str, *, auto_create_color: str | None = None
        ) -> str:
            seen[name] = auto_create_color
            return f"id-{name}"

        calls: list[dict[str, Any]] = []

        async def _call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            calls.append(variables)
            return {"issueCreate": {"issue": {"identifier": "FRE-1"}}}

        with (
            patch.object(client, "_team_id", AsyncMock(return_value="team")),
            patch.object(client, "_state_id", AsyncMock(return_value="state")),
            patch.object(client, "_label_id", side_effect=_label_id),
            patch.object(client, "_project_id", AsyncMock(return_value=None)),
            patch.object(client, "_call", side_effect=_call),
        ):
            await client.create_issue(
                "t",
                "FrenchForest",
                "d",
                3,
                ["PersonalAgent", AGENT_AUTHORED_LABEL, PIPELINE_FILED_LABEL],
                "Needs Approval",
                "",
            )
        assert seen[PIPELINE_FILED_LABEL] is not None
        assert seen["PersonalAgent"] is None


def test_count_method_is_named_for_its_population() -> None:
    """The count method name must not claim the old, wider population."""
    assert hasattr(LinearClient, "count_open_pipeline_issues")
    assert not hasattr(LinearClient, "count_open_agent_issues")


class TestUnreadableCountFailsClosed:
    """A reply that holds no issue list is not a count of zero (codex review, FRE-1611)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply", [{}, {"issues": None}, {"issues": {}}, {"issues": {"nodes": None}}]
    )
    async def test_malformed_reply_raises_instead_of_counting_zero(
        self, reply: dict[str, Any]
    ) -> None:
        """HTTP 200 without errors and without an issue list must not read as 0 open."""
        client = LinearClient()

        async def _call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            return reply

        with (
            patch.object(client, "_call", side_effect=_call),
            pytest.raises(RuntimeError, match="no issue list"),
        ):
            await client.count_open_pipeline_issues("FrenchForest")

    @pytest.mark.asyncio
    async def test_promotion_refuses_when_the_reply_is_malformed(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The pipeline gate returns None (no creation) when the count cannot be read."""
        client = LinearClient()

        async def _call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            return {}

        with (
            patch.object(client, "_call", side_effect=_call),
            patch.object(promotion_module.settings, "seshat_open_ticket_cap", 10),
        ):
            pipeline = PromotionPipeline(log_dir=tmp_path, linear_client=client)
            assert await pipeline._ticket_cap_headroom() is None
