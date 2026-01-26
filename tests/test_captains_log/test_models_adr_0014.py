"""Tests for Metric model and metrics_structured field (ADR-0014).

These tests verify the new Metric model and metrics_structured field added
to CaptainLogEntry for programmatic analysis of metrics.

Tests cover:
- Metric model validation
- CaptainLogEntry with metrics_structured
- Backward compatibility (entries without metrics_structured)
- JSON serialization/deserialization
- Edge cases
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    Metric,
    ProposedChange,
)


class TestMetricModel:
    """Tests for the Metric Pydantic model."""

    def test_metric_with_unit(self):
        """Test creating metric with unit."""
        metric = Metric(name="cpu_percent", value=9.3, unit="%")

        assert metric.name == "cpu_percent"
        assert metric.value == 9.3
        assert metric.unit == "%"

    def test_metric_without_unit(self):
        """Test creating metric without unit (None)."""
        metric = Metric(name="llm_calls", value=2, unit=None)

        assert metric.name == "llm_calls"
        assert metric.value == 2
        assert metric.unit is None

    def test_metric_with_integer_value(self):
        """Test metric with integer value."""
        metric = Metric(name="samples_collected", value=5, unit=None)

        assert metric.value == 5
        assert isinstance(metric.value, int)

    def test_metric_with_float_value(self):
        """Test metric with float value."""
        metric = Metric(name="duration_seconds", value=5.4, unit="s")

        assert metric.value == 5.4
        assert isinstance(metric.value, float)

    def test_metric_with_string_value(self):
        """Test metric with string value (allowed for flexibility)."""
        metric = Metric(name="status", value="healthy", unit=None)

        assert metric.value == "healthy"
        assert isinstance(metric.value, str)

    def test_metric_requires_name(self):
        """Test that name is required."""
        with pytest.raises(ValidationError) as exc_info:
            Metric(value=10.5, unit="%")

        assert "name" in str(exc_info.value)

    def test_metric_requires_value(self):
        """Test that value is required."""
        with pytest.raises(ValidationError) as exc_info:
            Metric(name="cpu_percent", unit="%")

        assert "value" in str(exc_info.value)

    def test_metric_json_serialization(self):
        """Test that Metric can be serialized to JSON."""
        metric = Metric(name="cpu_percent", value=9.3, unit="%")

        json_str = metric.model_dump_json()
        data = json.loads(json_str)

        assert data["name"] == "cpu_percent"
        assert data["value"] == 9.3
        assert data["unit"] == "%"

    def test_metric_json_deserialization(self):
        """Test that Metric can be deserialized from JSON."""
        data = {"name": "cpu_percent", "value": 9.3, "unit": "%"}

        metric = Metric(**data)

        assert metric.name == "cpu_percent"
        assert metric.value == 9.3
        assert metric.unit == "%"


class TestCaptainLogEntryWithMetrics:
    """Tests for CaptainLogEntry with metrics_structured field."""

    def test_entry_with_both_metric_formats(self):
        """Test creating entry with both string and structured metrics."""
        metrics_structured = [
            Metric(name="cpu_percent", value=9.3, unit="%"),
            Metric(name="duration_seconds", value=5.4, unit="s"),
            Metric(name="llm_calls", value=2, unit=None),
        ]

        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test entry",
            rationale="Test rationale",
            supporting_metrics=["cpu: 9.3%", "duration: 5.4s", "llm_calls: 2"],
            metrics_structured=metrics_structured,
        )

        assert len(entry.supporting_metrics) == 3
        assert len(entry.metrics_structured) == 3
        assert entry.metrics_structured[0].name == "cpu_percent"
        assert entry.metrics_structured[0].value == 9.3

    def test_entry_without_metrics_structured(self):
        """Test backward compatibility - entry without metrics_structured."""
        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test entry",
            rationale="Test rationale",
            supporting_metrics=["cpu: 9.3%"],
            # metrics_structured omitted (backward compatibility)
        )

        assert len(entry.supporting_metrics) == 1
        assert entry.metrics_structured is None  # Optional field

    def test_entry_with_empty_metrics_structured(self):
        """Test entry with empty metrics_structured list."""
        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test entry",
            rationale="Test rationale",
            supporting_metrics=[],
            metrics_structured=[],
        )

        assert entry.supporting_metrics == []
        assert entry.metrics_structured == []

    def test_entry_json_serialization_with_metrics(self):
        """Test that entry with metrics_structured serializes correctly."""
        metrics_structured = [
            Metric(name="cpu_percent", value=9.3, unit="%"),
            Metric(name="duration_seconds", value=5.4, unit="s"),
        ]

        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test entry",
            rationale="Test rationale",
            supporting_metrics=["cpu: 9.3%", "duration: 5.4s"],
            metrics_structured=metrics_structured,
        )

        json_str = entry.model_dump_json()
        data = json.loads(json_str)

        assert "metrics_structured" in data
        assert len(data["metrics_structured"]) == 2
        assert data["metrics_structured"][0]["name"] == "cpu_percent"
        assert data["metrics_structured"][0]["value"] == 9.3
        assert data["metrics_structured"][0]["unit"] == "%"

    def test_entry_json_deserialization_with_metrics(self):
        """Test that entry with metrics_structured deserializes correctly."""
        data = {
            "entry_id": "CL-2025-01-01-001",
            "timestamp": "2025-01-01T12:00:00Z",
            "type": "reflection",
            "title": "Test entry",
            "rationale": "Test rationale",
            "supporting_metrics": ["cpu: 9.3%", "duration: 5.4s"],
            "metrics_structured": [
                {"name": "cpu_percent", "value": 9.3, "unit": "%"},
                {"name": "duration_seconds", "value": 5.4, "unit": "s"},
            ],
        }

        entry = CaptainLogEntry(**data)

        assert len(entry.metrics_structured) == 2
        assert entry.metrics_structured[0].name == "cpu_percent"
        assert entry.metrics_structured[0].value == 9.3
        assert entry.metrics_structured[1].unit == "s"

    def test_entry_json_backward_compatibility(self):
        """Test that old entries without metrics_structured load correctly."""
        # Simulate old entry JSON (no metrics_structured field)
        data = {
            "entry_id": "CL-2025-01-01-001",
            "timestamp": "2025-01-01T12:00:00Z",
            "type": "reflection",
            "title": "Test entry",
            "rationale": "Test rationale",
            "supporting_metrics": ["cpu: 9.3%"],
            # metrics_structured not present
        }

        entry = CaptainLogEntry(**data)

        assert entry.metrics_structured is None  # Default for optional field
        assert len(entry.supporting_metrics) == 1

    def test_entry_with_proposed_change_and_metrics(self):
        """Test entry with both proposed change and structured metrics."""
        metrics_structured = [
            Metric(name="cpu_percent", value=9.3, unit="%"),
        ]

        proposed_change = ProposedChange(
            what="Cache GPU metrics",
            why="Tool calls are slow (3.6s)",
            how="Add MetricsCache singleton",
        )

        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.CONFIG_PROPOSAL,
            title="Optimize tool performance",
            rationale="GPU polling is expensive",
            proposed_change=proposed_change,
            supporting_metrics=["cpu: 9.3%"],
            metrics_structured=metrics_structured,
        )

        assert entry.proposed_change is not None
        assert entry.proposed_change.what == "Cache GPU metrics"
        assert entry.metrics_structured[0].name == "cpu_percent"

    def test_metrics_structured_validation(self):
        """Test that metrics_structured validates Metric objects."""
        # Valid: list of Metric objects
        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale="Test",
            metrics_structured=[
                Metric(name="cpu_percent", value=9.3, unit="%"),
            ],
        )
        assert entry.metrics_structured[0].name == "cpu_percent"

        # Invalid: list of dicts (should fail if not Metric objects)
        with pytest.raises(ValidationError):
            CaptainLogEntry(
                entry_id="CL-2025-01-01-001",
                timestamp=datetime.now(timezone.utc),
                type=CaptainLogEntryType.REFLECTION,
                title="Test",
                rationale="Test",
                metrics_structured=[
                    {"invalid": "not a Metric object"},  # Wrong structure
                ],
            )

    def test_entry_pretty_json_with_metrics(self):
        """Test pretty JSON output includes structured metrics."""
        metrics_structured = [
            Metric(name="cpu_percent", value=9.3, unit="%"),
        ]

        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test entry",
            rationale="Test rationale",
            metrics_structured=metrics_structured,
        )

        json_str = entry.model_dump_json_pretty()

        # Should be indented
        assert "\n" in json_str
        assert "  " in json_str  # 2-space indentation

        # Should contain metrics_structured
        assert "metrics_structured" in json_str
        assert "cpu_percent" in json_str


class TestMetricsAnalytics:
    """Tests for analytics use cases with structured metrics."""

    def test_query_metrics_by_name(self):
        """Test that structured metrics enable querying by name."""
        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale="Test",
            metrics_structured=[
                Metric(name="cpu_percent", value=9.3, unit="%"),
                Metric(name="memory_percent", value=53.4, unit="%"),
                Metric(name="duration_seconds", value=5.4, unit="s"),
            ],
        )

        # Query CPU metric
        cpu_metrics = [m for m in entry.metrics_structured if m.name == "cpu_percent"]
        assert len(cpu_metrics) == 1
        assert cpu_metrics[0].value == 9.3

    def test_query_metrics_by_value_range(self):
        """Test that structured metrics enable range queries."""
        entry = CaptainLogEntry(
            entry_id="CL-2025-01-01-001",
            timestamp=datetime.now(timezone.utc),
            type=CaptainLogEntryType.REFLECTION,
            title="Test",
            rationale="Test",
            metrics_structured=[
                Metric(name="cpu_percent", value=9.3, unit="%"),
                Metric(name="memory_percent", value=75.4, unit="%"),  # High
                Metric(name="gpu_percent", value=3.2, unit="%"),
            ],
        )

        # Find metrics > 50%
        high_metrics = [
            m
            for m in entry.metrics_structured
            if isinstance(m.value, (int, float)) and m.value > 50
        ]

        assert len(high_metrics) == 1
        assert high_metrics[0].name == "memory_percent"

    def test_aggregate_metrics_across_entries(self):
        """Test aggregating metrics across multiple entries."""
        entries = [
            CaptainLogEntry(
                entry_id=f"CL-2025-01-01-{i:03d}",
                timestamp=datetime.now(timezone.utc),
                type=CaptainLogEntryType.REFLECTION,
                title=f"Entry {i}",
                rationale="Test",
                metrics_structured=[
                    Metric(name="cpu_percent", value=float(10 + i), unit="%"),
                ],
            )
            for i in range(5)
        ]

        # Extract all CPU values
        cpu_values = [
            m.value
            for entry in entries
            for m in entry.metrics_structured
            if m.name == "cpu_percent"
        ]

        assert len(cpu_values) == 5
        assert cpu_values == [10.0, 11.0, 12.0, 13.0, 14.0]

        # Calculate average
        avg_cpu = sum(cpu_values) / len(cpu_values)
        assert avg_cpu == 12.0
