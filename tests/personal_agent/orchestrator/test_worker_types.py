"""ADR-0150 D2 — the closed worker registry (FRE-1493)."""

from __future__ import annotations

from personal_agent.config import load_governance_config
from personal_agent.orchestrator.worker_types import (
    WORKER_TYPES,
    WorkerType,
    worker_types_declaring,
)


class TestRegistryShape:
    def test_exactly_two_types(self) -> None:
        assert set(WORKER_TYPES) == {WorkerType.RESEARCHER, WorkerType.GENERAL}
        assert [t.value for t in WorkerType] == ["researcher", "general"]

    def test_researcher_entry(self) -> None:
        spec = WORKER_TYPES[WorkerType.RESEARCHER]
        assert spec.tools == ("web_search",)
        assert spec.report_schema == "worker_report_v1"
        assert spec.default_thoroughness == "standard"
        assert spec.description.startswith("Finds facts on the open web")
        assert spec.prompt_block.startswith("You research one bounded question on the open web.")

    def test_general_entry(self) -> None:
        spec = WORKER_TYPES[WorkerType.GENERAL]
        assert spec.tools == ("run_python", "search_memory", "recall_personal_history")
        assert spec.report_schema is None
        assert spec.default_thoroughness == "quick"
        assert spec.prompt_block == ""

    def test_union_of_type_tools_is_todays_grant_set(self) -> None:
        """D2: no tool becomes unreachable from a fan-out, and no type asks for a refused one."""
        declared = {tool for spec in WORKER_TYPES.values() for tool in spec.tools}
        granted = set(load_governance_config().granted_sub_agent_tool_names())
        assert declared == granted

    def test_the_registry_is_read_only(self) -> None:
        try:
            WORKER_TYPES[WorkerType.GENERAL] = WORKER_TYPES[WorkerType.RESEARCHER]  # type: ignore[index]
        except TypeError:
            return
        raise AssertionError("WORKER_TYPES must not be mutable")


class TestResearcherBlock:
    def test_carries_the_adr_lines_verbatim(self) -> None:
        block = WORKER_TYPES[WorkerType.RESEARCHER].prompt_block
        for line in (
            "Prefer primary sources: the organiser, the venue, the official listing, the "
            "publisher. A news article that names its source is second. An aggregator is "
            "last, and never the only source for a claim.",
            "Record a claim only when a fetched result states it. Quote a source's exact "
            "words only when the wording is load-bearing. Do not recap pages you merely read.",
            "If you find nothing for part of the task, say so and say what you searched — a "
            "report that names nothing you looked for is indistinguishable from never having "
            "looked.",
            "Stop searching when your last two searches returned the same facts.",
        ):
            assert line in block

    def test_absence_is_neutral_and_tied_to_the_search(self) -> None:
        """Owner ruling on FRE-1493: absence is acceptable, never encouraged."""
        block = WORKER_TYPES[WorkerType.RESEARCHER].prompt_block.lower()
        assert "naming what you searched and where" in block
        assert "absence you did not search for is not a finding" in block
        for praise in ("valuable", "powerful", "good", "preferred", "helpful", "great"):
            assert praise not in block

    def test_done_rule_waits_for_t3(self) -> None:
        """Without T3's report-writing call, an obedient worker would report 'DONE'."""
        assert "DONE" not in WORKER_TYPES[WorkerType.RESEARCHER].prompt_block


class TestTypesDeclaring:
    def test_each_tool_has_one_declaring_type(self) -> None:
        assert worker_types_declaring("web_search") == (WorkerType.RESEARCHER,)
        assert worker_types_declaring("run_python") == (WorkerType.GENERAL,)

    def test_an_undeclared_tool_has_none(self) -> None:
        assert worker_types_declaring("fetch_url") == ()
