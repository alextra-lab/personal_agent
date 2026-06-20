"""Tests for budget-compaction quality incident recording (ADR-0092 §D5, FRE-572)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent.telemetry.context_quality import (
    BudgetCompactionIncident,
    record_budget_compaction_incident,
)


def _make_incident(
    *,
    trace_id: str = "trace-a1",
    session_id: str = "sess-a1",
    phases_fired: tuple[int, ...] = (1,),
    severity: str = "low",
    detected_at: datetime | None = None,
) -> BudgetCompactionIncident:
    return BudgetCompactionIncident(
        trace_id=trace_id,
        session_id=session_id,
        phases_fired=phases_fired,
        severity=severity,  # type: ignore[arg-type]
        detected_at=detected_at or datetime.now(timezone.utc),
    )


class TestBudgetCompactionIncidentDataclass:
    def test_is_frozen(self) -> None:
        inc = _make_incident()
        with pytest.raises(Exception):  # frozen dataclass raises on setattr
            inc.severity = "high"  # type: ignore[misc]

    def test_phase2_severity_high(self) -> None:
        inc = _make_incident(phases_fired=(1, 2), severity="high")
        assert inc.severity == "high"

    def test_phase1_only_severity_low(self) -> None:
        inc = _make_incident(phases_fired=(1,), severity="low")
        assert inc.severity == "low"


class TestRecordBudgetCompactionIncident:
    @pytest.mark.asyncio
    async def test_writes_bcomp_jsonl(self, tmp_path: Path) -> None:
        inc = _make_incident()
        await record_budget_compaction_incident(inc, output_dir=tmp_path)

        day = inc.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"BCOMP-{day}.jsonl"
        assert fp.exists(), f"Expected {fp} to exist"

    @pytest.mark.asyncio
    async def test_jsonl_contains_required_fields(self, tmp_path: Path) -> None:
        inc = _make_incident(
            trace_id="trace-x",
            session_id="sess-x",
            phases_fired=(1, 2),
            severity="high",
        )
        await record_budget_compaction_incident(inc, output_dir=tmp_path)

        day = inc.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"BCOMP-{day}.jsonl"
        payload = json.loads(fp.read_text().strip())
        assert payload["trace_id"] == "trace-x"
        assert payload["session_id"] == "sess-x"
        assert payload["phases_fired"] == [1, 2]
        assert payload["severity"] == "high"
        assert "detected_at" in payload

    @pytest.mark.asyncio
    async def test_high_severity_when_phase2_present(self, tmp_path: Path) -> None:
        inc = _make_incident(phases_fired=(1, 2), severity="high")
        await record_budget_compaction_incident(inc, output_dir=tmp_path)

        day = inc.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"BCOMP-{day}.jsonl"
        payload = json.loads(fp.read_text().strip())
        assert payload["severity"] == "high"

    @pytest.mark.asyncio
    async def test_low_severity_phase1_only(self, tmp_path: Path) -> None:
        inc = _make_incident(phases_fired=(1,), severity="low")
        await record_budget_compaction_incident(inc, output_dir=tmp_path)

        day = inc.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"BCOMP-{day}.jsonl"
        payload = json.loads(fp.read_text().strip())
        assert payload["severity"] == "low"

    @pytest.mark.asyncio
    async def test_appends_multiple_incidents(self, tmp_path: Path) -> None:
        ts = datetime.now(timezone.utc)
        inc_a = _make_incident(trace_id="trace-a", detected_at=ts)
        inc_b = _make_incident(trace_id="trace-b", detected_at=ts)
        await record_budget_compaction_incident(inc_a, output_dir=tmp_path)
        await record_budget_compaction_incident(inc_b, output_dir=tmp_path)

        day = ts.strftime("%Y-%m-%d")
        fp = tmp_path / f"BCOMP-{day}.jsonl"
        lines = [json.loads(ln) for ln in fp.read_text().splitlines()]
        assert {ln["trace_id"] for ln in lines} == {"trace-a", "trace-b"}

    @pytest.mark.asyncio
    async def test_uses_default_output_dir_when_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from personal_agent.telemetry import context_quality as cq

        original = cq._default_output_dir
        monkeypatch.setattr(cq, "_default_output_dir", lambda: tmp_path)
        inc = _make_incident()
        await record_budget_compaction_incident(inc)

        day = inc.detected_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        fp = tmp_path / f"BCOMP-{day}.jsonl"
        assert fp.exists()
