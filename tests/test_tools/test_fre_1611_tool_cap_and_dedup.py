"""FRE-1611 AC-3 — the in-turn Linear tool honors the cap and a working dedup.

The fake below plays Linear. It holds seeded issues and applies the ``state.type``
and ``labels`` filters, and the title filter, to them. A test that only checks which
filter dict the tool sent would pass for a misspelled state type. These tests fail if
the tool lets a duplicate or an over-cap ticket through.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

import personal_agent.tools.linear as lm
from personal_agent.linear_labels import AGENT_AUTHORED_LABEL, PIPELINE_FILED_LABEL
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.executor import ToolExecutionError

_CTX = TraceContext.new_trace()

# The workflow state types Linear defines. A misspelling cannot match any of them.
_LINEAR_STATE_TYPES = {"triage", "backlog", "unstarted", "started", "completed", "canceled"}
_WORKSPACE_STATE_TYPES = _LINEAR_STATE_TYPES | {"duplicate"}


def _issue(
    identifier: str, title: str, state_type: str, labels: list[str] | None = None
) -> dict[str, Any]:
    return {
        "identifier": identifier,
        "title": title,
        "url": f"https://linear.app/x/issue/{identifier}",
        "state_type": state_type,
        "labels": labels or [],
    }


class _FakeLinear:
    """Applies the issue filters the tool sends to a seeded issue list."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        self.filters: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    async def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = variables or {}
        if "issueCreate" in query:
            self.created.append(variables["input"])
            return {"issueCreate": {"issue": {"id": "i", "identifier": "FRE-9999", "title": "t"}}}
        filt = variables["filter"]
        self.filters.append(filt)
        nodes = [i for i in self.issues if self._matches(i, filt)]
        return {"issues": {"nodes": nodes[: variables.get("first", 5)]}}

    @staticmethod
    def _matches(issue: dict[str, Any], filt: dict[str, Any]) -> bool:
        if "state" in filt:
            excluded = set(filt["state"]["type"]["nin"])
            if issue["state_type"] in excluded:
                return False
        if "title" in filt:
            needle = filt["title"]["containsIgnoreCase"].lower()
            if needle not in issue["title"].lower():
                return False
        if "labels" in filt:
            wanted = set(filt["labels"]["some"]["name"]["in"])
            if not wanted & set(issue["labels"]):
                return False
        return True


async def _file(fake: _FakeLinear, title: str) -> dict[str, Any]:
    """Call the tool executor against the fake, with every lookup stubbed."""
    lm._rate_log.clear()
    with (
        patch.object(lm, "_gql", side_effect=fake.gql),
        patch.object(lm, "_get_team_id", return_value="team-1"),
        patch.object(lm, "_get_state_id", return_value="state-na"),
        patch.object(lm, "_get_label_id", return_value="lbl-af"),
        patch.object(lm, "_require_personal_agent_label_id", return_value="lbl-pa"),
        patch.object(lm, "settings") as ms,
    ):
        ms.linear_api_key = "lin_api_test"
        ms.linear_agent_rate_limit_per_day = 10
        ms.seshat_open_ticket_cap = 10
        return await lm.create_linear_issue_executor(title=title, description="body", ctx=_CTX)


class TestDedup:
    """AC-3: a canceled issue with the same title blocks a duplicate."""

    @pytest.mark.asyncio
    async def test_canceled_issue_with_same_title_blocks_the_duplicate(self) -> None:
        """The owner canceled it. The tool must not file it again."""
        fake = _FakeLinear([_issue("FRE-1", "Fix the thing", "canceled")])
        with pytest.raises(ToolExecutionError, match="Duplicate.*FRE-1") as exc_info:
            await _file(fake, "Fix the thing")
        assert exc_info.value.code == "duplicate_issue"
        assert fake.created == []

    @pytest.mark.asyncio
    async def test_issue_closed_as_duplicate_does_not_block(self) -> None:
        """A copy closed as a duplicate is not evidence of the original."""
        fake = _FakeLinear([_issue("FRE-2", "Fix the thing", "duplicate")])
        result = await _file(fake, "Fix the thing")
        assert result["identifier"] == "FRE-9999"

    @pytest.mark.asyncio
    async def test_every_excluded_state_type_is_a_real_linear_state_type(self) -> None:
        """A misspelling such as ``cancelled`` excludes nothing. It must not return."""
        fake = _FakeLinear([])
        await _file(fake, "Fix the thing")
        for filt in fake.filters:
            excluded = set(filt["state"]["type"]["nin"])
            assert excluded <= _WORKSPACE_STATE_TYPES, excluded - _WORKSPACE_STATE_TYPES


class TestCap:
    """The tool honors the same cap as the pipeline, over the same population."""

    @pytest.mark.asyncio
    async def test_tool_tickets_do_not_fill_the_cap(self) -> None:
        """4 open tool tickets and 0 pipeline tickets leave the tool free to file."""
        issues = [
            _issue(
                f"FRE-{n}", f"Tool ticket {n}", "backlog", ["PersonalAgent", AGENT_AUTHORED_LABEL]
            )
            for n in (359, 360, 361, 362)
        ]
        fake = _FakeLinear(issues)
        result = await _file(fake, "A new finding")
        assert result["identifier"] == "FRE-9999"

    @pytest.mark.asyncio
    async def test_the_tool_refuses_at_the_cap(self) -> None:
        """10 open pipeline tickets fill the cap of 10. The tool refuses and files nothing."""
        issues = [
            _issue(
                f"FRE-{n}",
                f"Pipeline ticket {n}",
                "started" if n % 2 else "backlog",
                ["PersonalAgent", "Improvement", AGENT_AUTHORED_LABEL, PIPELINE_FILED_LABEL],
            )
            for n in range(100, 110)
        ]
        fake = _FakeLinear(issues)
        with pytest.raises(ToolExecutionError) as exc_info:
            await _file(fake, "A new finding")
        assert exc_info.value.code == "ticket_cap_reached"
        assert fake.created == []

    @pytest.mark.asyncio
    async def test_terminal_pipeline_tickets_do_not_count(self) -> None:
        """10 canceled pipeline tickets are not open. The tool files."""
        issues = [
            _issue(f"FRE-{n}", f"Pipeline ticket {n}", "canceled", [PIPELINE_FILED_LABEL])
            for n in range(100, 110)
        ]
        fake = _FakeLinear(issues)
        result = await _file(fake, "A new finding")
        assert result["identifier"] == "FRE-9999"
