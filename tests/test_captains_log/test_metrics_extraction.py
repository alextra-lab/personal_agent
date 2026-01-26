"""Tests for deterministic metrics extraction in Captain's Log.

These tests verify the metrics extraction functionality added in ADR-0014
to extract metrics from RequestMonitor's metrics_summary dict without LLM involvement.

Tests cover:
- All metric types (duration, CPU, memory, GPU, samples, violations)
- Edge cases (missing values, None, empty dict)
- Deterministic behavior (same input → same output)
- Formatting consistency
"""

from personal_agent.captains_log.metrics_extraction import (
    extract_metrics_from_summary,
    format_metrics_string,
)
from personal_agent.captains_log.models import Metric


def test_extract_metrics_complete_summary():
    """Test extraction with all metrics present."""
    summary = {
        "duration_seconds": 20.9,
        "cpu_avg": 9.3,
        "memory_avg": 53.4,
        "gpu_avg": 3.2,
        "samples_collected": 4,
        "threshold_violations": ["cpu_high", "memory_high"],
        "cpu_peak": 15.2,
        "memory_peak": 68.7,
        "gpu_peak": 8.9,
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary)

    # Verify string metrics
    assert "duration: 20.9s" in string_metrics
    assert "cpu: 9.3%" in string_metrics
    assert "memory: 53.4%" in string_metrics
    assert "gpu: 3.2%" in string_metrics
    assert "samples: 4" in string_metrics
    assert "threshold_violations: 2" in string_metrics
    assert "cpu_peak: 15.2%" in string_metrics
    assert "memory_peak: 68.7%" in string_metrics
    assert "gpu_peak: 8.9%" in string_metrics

    # Verify structured metrics
    assert len(structured_metrics) == 9

    # Check specific structured metrics
    duration_metric = next(m for m in structured_metrics if m.name == "duration_seconds")
    assert duration_metric.value == 20.9
    assert duration_metric.unit == "s"

    cpu_metric = next(m for m in structured_metrics if m.name == "cpu_percent")
    assert cpu_metric.value == 9.3
    assert cpu_metric.unit == "%"

    gpu_metric = next(m for m in structured_metrics if m.name == "gpu_percent")
    assert gpu_metric.value == 3.2
    assert gpu_metric.unit == "%"

    samples_metric = next(m for m in structured_metrics if m.name == "samples_collected")
    assert samples_metric.value == 4
    assert samples_metric.unit is None


def test_extract_metrics_minimal_summary():
    """Test extraction with only required metrics (no GPU, no peaks)."""
    summary = {
        "duration_seconds": 5.4,
        "cpu_avg": 8.2,
        "memory_avg": 45.1,
        "samples_collected": 2,
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary)

    # Verify string metrics
    assert "duration: 5.4s" in string_metrics
    assert "cpu: 8.2%" in string_metrics
    assert "memory: 45.1%" in string_metrics
    assert "samples: 2" in string_metrics

    # Should NOT have GPU or peaks
    assert not any("gpu" in m for m in string_metrics)
    assert not any("peak" in m for m in string_metrics)

    # Verify structured metrics count
    assert len(structured_metrics) == 4  # duration, cpu, memory, samples


def test_extract_metrics_empty_summary():
    """Test extraction with None or empty dict."""
    # None summary
    string_metrics, structured_metrics = extract_metrics_from_summary(None)
    assert string_metrics == []
    assert structured_metrics == []

    # Empty dict
    string_metrics, structured_metrics = extract_metrics_from_summary({})
    assert string_metrics == []
    assert structured_metrics == []


def test_extract_metrics_no_threshold_violations():
    """Test that threshold violations are only added if present."""
    summary = {
        "duration_seconds": 5.0,
        "cpu_avg": 5.0,
        "memory_avg": 30.0,
        "samples_collected": 2,
        "threshold_violations": [],  # Empty list
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary)

    # Should not include threshold violations if list is empty
    assert not any("threshold_violations" in m for m in string_metrics)
    assert not any(m.name == "threshold_violations" for m in structured_metrics)


def test_extract_metrics_deterministic():
    """Test that extraction is deterministic (same input → same output)."""
    summary = {
        "duration_seconds": 10.5,
        "cpu_avg": 12.3,
        "memory_avg": 45.6,
        "samples_collected": 3,
    }

    # Run extraction multiple times
    results = [extract_metrics_from_summary(summary) for _ in range(5)]

    # All results should be identical
    for string_metrics, structured_metrics in results:
        assert string_metrics == results[0][0]
        assert len(structured_metrics) == len(results[0][1])

        # Verify all structured metrics match
        for i, metric in enumerate(structured_metrics):
            expected = results[0][1][i]
            assert metric.name == expected.name
            assert metric.value == expected.value
            assert metric.unit == expected.unit


def test_extract_metrics_formatting_precision():
    """Test that float values are formatted consistently."""
    summary = {
        "duration_seconds": 20.123456,
        "cpu_avg": 9.876543,
        "memory_avg": 53.456789,
    }

    string_metrics, _ = extract_metrics_from_summary(summary)

    # Should format to 1 decimal place
    assert "duration: 20.1s" in string_metrics
    assert "cpu: 9.9%" in string_metrics
    assert "memory: 53.5%" in string_metrics


def test_extract_metrics_integer_samples():
    """Test that integer values are handled correctly."""
    summary = {
        "duration_seconds": 5.0,
        "cpu_avg": 10.0,
        "memory_avg": 50.0,
        "samples_collected": 7,  # Integer
        "threshold_violations": ["a", "b", "c"],  # List length → integer
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary)

    # Check string format
    assert "samples: 7" in string_metrics
    assert "threshold_violations: 3" in string_metrics

    # Check structured types
    samples_metric = next(m for m in structured_metrics if m.name == "samples_collected")
    assert isinstance(samples_metric.value, int)
    assert samples_metric.value == 7

    violations_metric = next(m for m in structured_metrics if m.name == "threshold_violations")
    assert isinstance(violations_metric.value, int)
    assert violations_metric.value == 3


def test_extract_metrics_gpu_only_when_present():
    """Test that GPU metrics are only extracted on Apple Silicon."""
    # Summary without GPU (generic platform)
    summary_no_gpu = {
        "duration_seconds": 5.0,
        "cpu_avg": 10.0,
        "memory_avg": 50.0,
        "samples_collected": 2,
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary_no_gpu)
    assert not any("gpu" in m for m in string_metrics)
    assert not any(m.name == "gpu_percent" for m in structured_metrics)

    # Summary with GPU (Apple Silicon)
    summary_with_gpu = {
        **summary_no_gpu,
        "gpu_avg": 15.5,
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary_with_gpu)
    assert "gpu: 15.5%" in string_metrics
    assert any(m.name == "gpu_percent" for m in structured_metrics)


def test_extract_metrics_peak_values():
    """Test extraction of peak values (cpu_peak, memory_peak, gpu_peak)."""
    summary = {
        "duration_seconds": 10.0,
        "cpu_avg": 10.0,
        "memory_avg": 50.0,
        "cpu_peak": 25.5,
        "memory_peak": 75.2,
        "gpu_avg": 5.0,
        "gpu_peak": 12.8,
        "samples_collected": 3,
    }

    string_metrics, structured_metrics = extract_metrics_from_summary(summary)

    # Check string format
    assert "cpu_peak: 25.5%" in string_metrics
    assert "memory_peak: 75.2%" in string_metrics
    assert "gpu_peak: 12.8%" in string_metrics

    # Check structured format
    cpu_peak = next(m for m in structured_metrics if m.name == "cpu_peak_percent")
    assert cpu_peak.value == 25.5
    assert cpu_peak.unit == "%"

    memory_peak = next(m for m in structured_metrics if m.name == "memory_peak_percent")
    assert memory_peak.value == 75.2

    gpu_peak = next(m for m in structured_metrics if m.name == "gpu_peak_percent")
    assert gpu_peak.value == 12.8


def test_format_metrics_string_non_empty():
    """Test formatting string metrics as comma-separated."""
    string_metrics = ["cpu: 9.3%", "duration: 5.4s", "memory: 53.4%"]

    result = format_metrics_string(string_metrics)

    assert result == "cpu: 9.3%, duration: 5.4s, memory: 53.4%"


def test_format_metrics_string_empty():
    """Test formatting empty list returns default message."""
    result = format_metrics_string([])

    assert result == "No metrics available"


def test_format_metrics_string_single_item():
    """Test formatting single metric."""
    result = format_metrics_string(["cpu: 9.3%"])

    assert result == "cpu: 9.3%"


def test_metric_model_validation():
    """Test that Metric model validates correctly."""
    # Valid metric
    metric = Metric(name="cpu_percent", value=9.3, unit="%")
    assert metric.name == "cpu_percent"
    assert metric.value == 9.3
    assert metric.unit == "%"

    # Valid metric with no unit
    metric = Metric(name="llm_calls", value=2, unit=None)
    assert metric.name == "llm_calls"
    assert metric.value == 2
    assert metric.unit is None

    # String value (allowed)
    metric = Metric(name="status", value="healthy", unit=None)
    assert metric.value == "healthy"


def test_extract_metrics_type_conversions():
    """Test that extraction handles type conversions properly."""
    summary = {
        "duration_seconds": "20.5",  # String that can be converted
        "cpu_avg": 9.3,  # Already float
        "memory_avg": 53,  # Integer that should become float
        "samples_collected": "4",  # String that should become int
    }

    _, structured_metrics = extract_metrics_from_summary(summary)

    # Check that types are correct after extraction
    duration = next(m for m in structured_metrics if m.name == "duration_seconds")
    assert isinstance(duration.value, float)
    assert duration.value == 20.5

    memory = next(m for m in structured_metrics if m.name == "memory_percent")
    assert isinstance(memory.value, float)
    assert memory.value == 53.0

    samples = next(m for m in structured_metrics if m.name == "samples_collected")
    assert isinstance(samples.value, int)
    assert samples.value == 4


def test_extract_metrics_order_consistency():
    """Test that metrics are extracted in consistent order."""
    summary = {
        "gpu_avg": 3.2,
        "samples_collected": 4,
        "cpu_avg": 9.3,
        "duration_seconds": 20.9,
        "memory_avg": 53.4,
    }

    # Extract multiple times
    results = [extract_metrics_from_summary(summary) for _ in range(3)]

    # Order should be consistent (duration, cpu, memory, gpu, samples)
    for string_metrics, _ in results:
        assert string_metrics[0].startswith("duration:")
        assert string_metrics[1].startswith("cpu:")
        assert string_metrics[2].startswith("memory:")
        assert string_metrics[3].startswith("gpu:")
        assert string_metrics[4].startswith("samples:")
