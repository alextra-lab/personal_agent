"""Tests for structured logging configuration."""

import json
import logging
import pathlib

import structlog

from personal_agent.telemetry.logger import configure_logging, get_logger


class TestLoggerConfiguration:
    """Test logger configuration and setup."""

    def test_get_logger_returns_bound_logger(self) -> None:
        """Test that get_logger returns a logger that can be used."""
        log = get_logger(__name__)
        # structlog returns a proxy initially, but it should have the info method
        assert hasattr(log, "info")
        assert hasattr(log, "error")
        assert hasattr(log, "warning")

    def test_get_logger_configures_on_first_call(self) -> None:
        """Test that get_logger configures logging on first call."""
        # Reset structlog configuration
        structlog.reset_defaults()

        # First call should configure
        get_logger("test.module1")
        assert structlog.is_configured()

        # Second call should reuse configuration
        log2 = get_logger("test.module2")
        assert hasattr(log2, "info")
        assert hasattr(log2, "error")

    def test_logger_emits_structured_logs(self, tmp_path: pathlib.Path) -> None:
        """Test that logger emits structured JSON logs to file."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        import personal_agent.telemetry.logger as logger_module

        original_get_log_dir = logger_module._get_log_dir
        original_get_log_level = logger_module._get_log_level
        logger_module._get_log_dir = lambda: log_dir
        logger_module._get_log_level = lambda: "DEBUG"

        try:
            structlog.reset_defaults()
            logging.root.handlers.clear()

            configure_logging()

            log = get_logger("test.component")
            log.info("test_event", key1="value1", key2=42, trace_id="trace-123")

            log_file = log_dir / "current.jsonl"
            assert log_file.exists()

            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
                assert len(lines) > 0

                log_entry = json.loads(lines[-1])
                assert log_entry["event"] == "test_event"
                assert log_entry["key1"] == "value1"
                assert log_entry["key2"] == 42
                assert log_entry["trace_id"] == "trace-123"
                assert "timestamp" in log_entry
                assert "component" in log_entry
                assert log_entry["component"] == "component"
        finally:
            logger_module._get_log_dir = original_get_log_dir
            logger_module._get_log_level = original_get_log_level

    def test_logger_includes_timestamp(self, tmp_path: pathlib.Path) -> None:
        """Test that log entries include UTC timestamp."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        import personal_agent.telemetry.logger as logger_module

        original_get_log_dir = logger_module._get_log_dir
        original_get_log_level = logger_module._get_log_level
        logger_module._get_log_dir = lambda: log_dir
        logger_module._get_log_level = lambda: "DEBUG"

        try:
            structlog.reset_defaults()
            logging.root.handlers.clear()
            configure_logging()

            log = get_logger("test")
            log.info("test_event")

            log_file = log_dir / "current.jsonl"
            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
                log_entry = json.loads(lines[-1])
                assert "timestamp" in log_entry
                timestamp = log_entry["timestamp"]
                assert "T" in timestamp or "Z" in timestamp or "+00:00" in timestamp
        finally:
            logger_module._get_log_dir = original_get_log_dir
            logger_module._get_log_level = original_get_log_level

    def test_logger_includes_component(self, tmp_path: pathlib.Path) -> None:
        """Test that log entries include component name."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        import personal_agent.telemetry.logger as logger_module

        original_get_log_dir = logger_module._get_log_dir
        original_get_log_level = logger_module._get_log_level
        logger_module._get_log_dir = lambda: log_dir
        logger_module._get_log_level = lambda: "DEBUG"

        try:
            structlog.reset_defaults()
            logging.root.handlers.clear()
            configure_logging()

            log = get_logger("personal_agent.orchestrator")
            log.info("test_event")

            log_file = log_dir / "current.jsonl"
            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
                log_entry = json.loads(lines[-1])
                assert log_entry["component"] == "orchestrator"
        finally:
            logger_module._get_log_dir = original_get_log_dir
            logger_module._get_log_level = original_get_log_level

    def test_logger_handles_nested_module_names(self, tmp_path: pathlib.Path) -> None:
        """Test that logger extracts component from nested module names."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()

        import personal_agent.telemetry.logger as logger_module

        original_get_log_dir = logger_module._get_log_dir
        original_get_log_level = logger_module._get_log_level
        logger_module._get_log_dir = lambda: log_dir
        logger_module._get_log_level = lambda: "DEBUG"

        try:
            structlog.reset_defaults()
            logging.root.handlers.clear()
            configure_logging()

            log = get_logger("personal_agent.tools.filesystem")
            log.info("test_event")

            log_file = log_dir / "current.jsonl"
            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
                log_entry = json.loads(lines[-1])
                assert log_entry["component"] == "filesystem"
        finally:
            logger_module._get_log_dir = original_get_log_dir
            logger_module._get_log_level = original_get_log_level

    def test_logger_creates_log_directory(self, tmp_path: pathlib.Path) -> None:
        """Test that logger creates log directory if it doesn't exist."""
        log_dir = tmp_path / "new_logs" / "subdir"

        import personal_agent.telemetry.logger as logger_module

        original_get_log_dir = logger_module._get_log_dir
        logger_module._get_log_dir = lambda: log_dir

        try:
            structlog.reset_defaults()
            logging.root.handlers.clear()

            # Directory shouldn't exist yet
            assert not log_dir.exists()

            # Configure should create it
            configure_logging()

            # Directory should now exist
            assert log_dir.exists()
        finally:
            logger_module._get_log_dir = original_get_log_dir
