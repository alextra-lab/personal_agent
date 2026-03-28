"""Regression tests for live-data tool routing heuristics."""

from personal_agent.orchestrator.executor import _requires_live_data_lookup


def test_requires_live_data_lookup_weather_query() -> None:
    """Weather-style requests should trigger live data lookup."""
    assert _requires_live_data_lookup("How long is it expected rain in Forqualgier?")


def test_requires_live_data_lookup_static_query() -> None:
    """Static knowledge requests should not trigger live data lookup."""
    assert not _requires_live_data_lookup("Explain how TLS handshake works.")

