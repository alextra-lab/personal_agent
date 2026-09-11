"""ADR-0150 D2 — the closed worker registry (FRE-1493)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config import load_governance_config
from personal_agent.orchestrator.worker_types import (
    WORKER_REPORT_RESPONSE_FORMAT,
    WORKER_TYPES,
    Finding,
    Gap,
    WorkerReport,
    WorkerType,
    render_report_instruction,
    render_worker_report_body,
    render_worker_report_summary,
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

    def test_done_rule_present_now_t3_has_the_landing_call(self) -> None:
        """ADR-0150 D1/D5 (FRE-1494): T3's report-writing call makes the DONE

        rule safe — an obedient worker's "DONE" reply is transcript notes, not
        the report, so it no longer risks becoming one.
        """
        block = WORKER_TYPES[WorkerType.RESEARCHER].prompt_block
        assert (
            "To finish, reply with the single word DONE and no tool calls. "
            "You will then be asked for your report." in block
        )


class TestTypesDeclaring:
    def test_each_tool_has_one_declaring_type(self) -> None:
        assert worker_types_declaring("web_search") == (WorkerType.RESEARCHER,)
        assert worker_types_declaring("run_python") == (WorkerType.GENERAL,)

    def test_an_undeclared_tool_has_none(self) -> None:
        assert worker_types_declaring("fetch_url") == ()


def _finding(**overrides: object) -> Finding:
    fields = {
        "claim": "a claim",
        "source_url": "https://example.com/a",
        "date_or_period": "",
        "why_it_matters": "it matters",
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


def _gap(**overrides: object) -> Gap:
    fields = {"looked_for": "a gap", "where": "web_search"}
    fields.update(overrides)
    return Gap(**fields)  # type: ignore[arg-type]


class TestWorkerReportSchema:
    """ADR-0150 D1 — every property required, every object closed."""

    def test_every_field_required_no_defaults(self) -> None:
        with pytest.raises(ValidationError):
            WorkerReport(findings=[], gaps=[], tool_gap="")  # type: ignore[call-arg]

    def test_may_be_empty_fields_accept_the_empty_string(self) -> None:
        report = WorkerReport(working_notes="", findings=[], gaps=[], tool_gap="")
        assert report.working_notes == ""
        assert report.tool_gap == ""

    def test_finding_source_url_and_claim_reject_empty(self) -> None:
        with pytest.raises(ValidationError):
            _finding(claim="")
        with pytest.raises(ValidationError):
            _finding(source_url="")

    def test_finding_date_or_period_accepts_empty(self) -> None:
        assert _finding(date_or_period="").date_or_period == ""

    def test_length_ceilings_enforced(self) -> None:
        with pytest.raises(ValidationError):
            _finding(claim="x" * 401)
        assert _finding(claim="x" * 400).claim == "x" * 400
        with pytest.raises(ValidationError):
            WorkerReport(working_notes="x" * 1001, findings=[], gaps=[], tool_gap="")

    def test_max_items_enforced(self) -> None:
        with pytest.raises(ValidationError):
            WorkerReport(working_notes="", findings=[_finding()] * 21, gaps=[], tool_gap="")
        report = WorkerReport(working_notes="", findings=[_finding()] * 20, gaps=[], tool_gap="")
        assert len(report.findings) == 20

    def test_additional_properties_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WorkerReport(working_notes="", findings=[], gaps=[], tool_gap="", extra_field="nope")  # type: ignore[call-arg]

    def test_frozen(self) -> None:
        report = WorkerReport(working_notes="", findings=[], gaps=[], tool_gap="")
        with pytest.raises(ValidationError):
            report.working_notes = "changed"  # type: ignore[misc]


class TestWorkerReportJsonSchema:
    """The response_format payload OpenAI/llama.cpp strict-mode contracts expect."""

    def test_every_object_forbids_additional_properties(self) -> None:
        schema = WORKER_REPORT_RESPONSE_FORMAT["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        finding_schema = schema["properties"]["findings"]["items"]
        assert finding_schema["additionalProperties"] is False
        gap_schema = schema["properties"]["gaps"]["items"]
        assert gap_schema["additionalProperties"] is False

    def test_every_property_is_required(self) -> None:
        schema = WORKER_REPORT_RESPONSE_FORMAT["json_schema"]["schema"]
        assert set(schema["required"]) == set(schema["properties"])
        finding_schema = schema["properties"]["findings"]["items"]
        assert set(finding_schema["required"]) == set(finding_schema["properties"])

    def test_no_dollar_ref_or_defs_remains(self) -> None:
        schema = WORKER_REPORT_RESPONSE_FORMAT["json_schema"]["schema"]

        def _walk(node: object) -> None:
            if isinstance(node, dict):
                assert "$ref" not in node
                assert "$defs" not in node
                for value in node.values():
                    _walk(value)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(schema)

    def test_strict_and_name(self) -> None:
        assert WORKER_REPORT_RESPONSE_FORMAT["type"] == "json_schema"
        assert WORKER_REPORT_RESPONSE_FORMAT["json_schema"]["strict"] is True
        assert WORKER_REPORT_RESPONSE_FORMAT["json_schema"]["name"] == "worker_report_v1"

    def test_limits_are_under_the_grammar_bound(self) -> None:
        """Every maxLength stays under llama.cpp's ~2000-char grammar bound (ADR-0150 D1)."""
        schema = WORKER_REPORT_RESPONSE_FORMAT["json_schema"]["schema"]

        def _max_lengths(node: object) -> list[int]:
            found: list[int] = []
            if isinstance(node, dict):
                if "maxLength" in node:
                    found.append(node["maxLength"])
                for value in node.values():
                    found.extend(_max_lengths(value))
            elif isinstance(node, list):
                for item in node:
                    found.extend(_max_lengths(item))
            return found

        assert all(n < 2000 for n in _max_lengths(schema))


class TestReportInstructionAndRendering:
    def test_schema_backed_type_gets_the_schema_instruction(self) -> None:
        line = render_report_instruction(WorkerType.RESEARCHER)
        assert line.startswith("Report:")
        assert "1000 characters" in line
        assert "20 findings" in line

    def test_text_reporting_type_gets_the_old_line(self) -> None:
        assert render_report_instruction(WorkerType.GENERAL) == "Report in text."

    def test_summary_renders_findings_then_not_found_then_notes(self) -> None:
        report = WorkerReport(
            working_notes="the notes",
            findings=[_finding(claim="c1", why_it_matters="w1", source_url="https://x.example")],
            gaps=[_gap(looked_for="missing thing", where="tried here")],
            tool_gap="",
        )
        summary = render_worker_report_summary(report)
        assert "c1 — w1 [https://x.example]" in summary
        assert "Not found:" in summary
        assert "missing thing" in summary
        assert "the notes" in summary
        assert summary.index("c1") < summary.index("Not found")
        assert summary.index("Not found") < summary.index("the notes")

    def test_body_omits_gaps(self) -> None:
        report = WorkerReport(
            working_notes="notes",
            findings=[_finding(claim="c1")],
            gaps=[_gap(looked_for="missing thing")],
            tool_gap="",
        )
        body = render_worker_report_body(report)
        assert "c1" in body
        assert "missing thing" not in body
        assert "Not found" not in body

    def test_date_suffix_only_when_present(self) -> None:
        no_date = render_worker_report_summary(
            WorkerReport(
                working_notes="", findings=[_finding(date_or_period="")], gaps=[], tool_gap=""
            )
        )
        assert "()" not in no_date
        with_date = render_worker_report_summary(
            WorkerReport(
                working_notes="",
                findings=[_finding(date_or_period="2026-09-11")],
                gaps=[],
                tool_gap="",
            )
        )
        assert "(2026-09-11)" in with_date
