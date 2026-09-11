"""ADR-0150 D1 AC-6 — the full-fill probe fixture, mechanism half.

The live half of AC-6 (per-deployment token counts, the landing ceiling in
effect, and the one real call against llama-server) needs the local SLM
tunnel and OVH credentials, neither reachable from this session — see the
ticket's handoff comment. This file checks the committed fixture itself:
every field is genuinely at its ceiling, and the committed JSON matches what
the builder produces (so the two cannot silently drift apart).
"""

from __future__ import annotations

import json

from tests.personal_agent.orchestrator.fixtures.worker_report_full_fill import (
    _committed_json_path,
    build_full_fill_report,
)


class TestFullFillFixture:
    def test_every_array_is_at_maxitems(self) -> None:
        report = build_full_fill_report()
        assert len(report.findings) == 20
        assert len(report.gaps) == 10

    def test_every_string_is_at_maxlength(self) -> None:
        report = build_full_fill_report()
        assert len(report.working_notes) == 1000
        assert len(report.tool_gap) == 60
        for finding in report.findings:
            assert len(finding.claim) == 400
            assert len(finding.source_url) == 200
            assert finding.source_url.startswith("https://")
            assert len(finding.date_or_period) == 60
            assert len(finding.why_it_matters) == 150
        for gap in report.gaps:
            assert len(gap.looked_for) == 150
            assert len(gap.where) == 200

    def test_quote_escaping_is_exercised(self) -> None:
        """A literal `"` lands inside every long field, at every 100th character."""
        report = build_full_fill_report()
        assert '"' in report.working_notes
        assert '"' in report.findings[0].claim

    def test_committed_json_matches_the_builder(self) -> None:
        """The committed fixture and the builder must not drift apart.

        Regenerate with: `uv run python -m
        tests.personal_agent.orchestrator.fixtures.worker_report_full_fill`
        """
        committed = json.loads(_committed_json_path().read_text(encoding="utf-8"))
        built = build_full_fill_report().model_dump(mode="json")
        assert committed == built
