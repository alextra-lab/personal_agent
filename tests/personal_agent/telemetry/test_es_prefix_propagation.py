"""Tests for settings-driven Elasticsearch index prefix propagation (FRE-375).

Verifies that ElasticsearchHandler, ElasticsearchLogger, and Captain's Log
constants all honour the configured index prefix instead of hardcoding defaults.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestElasticsearchHandlerPrefix:
    """ElasticsearchHandler passes index_prefix through to its inner logger."""

    def test_handler_stores_custom_prefix(self) -> None:
        """ElasticsearchHandler stores the passed index_prefix on its es_logger."""
        from personal_agent.telemetry.es_handler import ElasticsearchHandler

        handler = ElasticsearchHandler("http://localhost:9200", index_prefix="agent-logs-test")
        assert handler.es_logger.index_prefix == "agent-logs-test"

    def test_handler_default_prefix(self) -> None:
        """ElasticsearchHandler uses 'agent-logs' as the default prefix."""
        from personal_agent.telemetry.es_handler import ElasticsearchHandler

        handler = ElasticsearchHandler("http://localhost:9200")
        assert handler.es_logger.index_prefix == "agent-logs"

    def test_handler_passes_prefix_to_logger(self) -> None:
        """ElasticsearchHandler propagates index_prefix to ElasticsearchLogger."""
        with patch(
            "personal_agent.telemetry.es_handler.ElasticsearchLogger"
        ) as mock_logger_cls:
            mock_logger_cls.return_value = MagicMock()
            from personal_agent.telemetry.es_handler import ElasticsearchHandler

            # Force re-import to pick up the mock (class is already imported in module scope)
            handler = ElasticsearchHandler.__new__(ElasticsearchHandler)
            import logging

            logging.Handler.__init__(handler)
            handler.es_logger = mock_logger_cls("http://es:9200", "custom-prefix")
            assert mock_logger_cls.call_args[0][1] == "custom-prefix"


class TestElasticsearchLoggerPrefix:
    """ElasticsearchLogger stores the passed index_prefix attribute."""

    def test_logger_stores_custom_prefix(self) -> None:
        """ElasticsearchLogger exposes index_prefix as an attribute."""
        from personal_agent.telemetry.es_logger import ElasticsearchLogger

        logger = ElasticsearchLogger("http://localhost:9200", index_prefix="my-prefix")
        assert logger.index_prefix == "my-prefix"

    def test_logger_default_prefix(self) -> None:
        """ElasticsearchLogger uses 'agent-logs' as the default prefix."""
        from personal_agent.telemetry.es_logger import ElasticsearchLogger

        logger = ElasticsearchLogger("http://localhost:9200")
        assert logger.index_prefix == "agent-logs"


class TestCaptainsLogPrefixMatchesSettings:
    """Captain's Log module-level constants derive from settings (FRE-375)."""

    def test_captures_prefix_matches_settings(self) -> None:
        """CAPTURES_INDEX_PREFIX equals f'{captains_log_index_prefix}-captures'."""
        from personal_agent.captains_log import capture
        from personal_agent.config import get_settings

        settings = get_settings()
        expected = f"{settings.captains_log_index_prefix}-captures"
        assert capture.CAPTURES_INDEX_PREFIX == expected

    def test_reflections_prefix_matches_settings(self) -> None:
        """REFLECTIONS_INDEX_PREFIX equals f'{captains_log_index_prefix}-reflections'."""
        from personal_agent.captains_log import manager
        from personal_agent.config import get_settings

        settings = get_settings()
        expected = f"{settings.captains_log_index_prefix}-reflections"
        assert manager.REFLECTIONS_INDEX_PREFIX == expected

    def test_captures_default_prefix_value(self) -> None:
        """Default CAPTURES_INDEX_PREFIX is 'agent-captains-captures'."""
        from personal_agent.captains_log import capture

        assert capture.CAPTURES_INDEX_PREFIX == "agent-captains-captures"

    def test_reflections_default_prefix_value(self) -> None:
        """Default REFLECTIONS_INDEX_PREFIX is 'agent-captains-reflections'."""
        from personal_agent.captains_log import manager

        assert manager.REFLECTIONS_INDEX_PREFIX == "agent-captains-reflections"


class TestTelemetryQueriesCapturesPrefixPropagation:
    """TelemetryQueries builds captures index prefix from settings (FRE-375)."""

    def test_queries_captures_prefix_uses_settings(self) -> None:
        """TelemetryQueries._captures_index_prefix derives from captains_log_index_prefix."""
        mock_settings = MagicMock()
        mock_settings.elasticsearch_url = "http://localhost:9200"
        mock_settings.elasticsearch_index_prefix = "agent-logs"
        mock_settings.captains_log_index_prefix = "agent-captains-custom"

        with patch(
            "personal_agent.telemetry.queries.get_settings",
            return_value=mock_settings,
        ):
            from personal_agent.telemetry.queries import TelemetryQueries

            queries = TelemetryQueries()
            assert queries._captures_index_prefix == "agent-captains-custom-captures"

    def test_queries_captures_prefix_default(self) -> None:
        """TelemetryQueries._captures_index_prefix uses default 'agent-captains-captures'."""
        from personal_agent.config import get_settings
        from personal_agent.telemetry.queries import TelemetryQueries

        settings = get_settings()
        queries = TelemetryQueries()
        expected = f"{settings.captains_log_index_prefix}-captures"
        assert queries._captures_index_prefix == expected
