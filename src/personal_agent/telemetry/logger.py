"""Structured logging configuration using structlog.

This module configures structlog for structured JSON logging with:
- JSON formatter for file output
- Pretty-printed console output for debugging
- UTC timestamps
- File rotation for log management
- Component and event tracking
"""

import logging
import logging.handlers
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

import structlog


def _get_log_level() -> str:
    """Get log level from configuration.

    Returns:
        Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    # Bootstrap from environment to avoid circular imports during startup.
    # Full configuration is still loaded/validated via personal_agent.config.settings.
    from personal_agent.config.bootstrap import get_bootstrap_log_level  # noqa: PLC0415

    return get_bootstrap_log_level()


def _get_log_dir() -> pathlib.Path:
    """Get log directory path.

    Returns:
        Path to telemetry/logs directory.
    """
    # Try to get from settings, otherwise use project root
    try:
        from personal_agent.config.settings import get_settings  # noqa: PLC0415

        return pathlib.Path(str(get_settings().log_dir))
    except Exception:
        # Config module not yet implemented, use default
        # Assume we're in src/personal_agent/telemetry, go up to project root
        project_root = pathlib.Path(__file__).parent.parent.parent.parent
        return project_root / "telemetry" / "logs"


def _add_timestamp(
    logger: logging.Logger, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add UTC timestamp to log event.

    Args:
        logger: The logger instance.
        method_name: The log method name (info, error, etc.).
        event_dict: The event dictionary.

    Returns:
        Event dictionary with timestamp added.
    """
    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    return event_dict


def _add_component(
    logger: logging.Logger, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add component name to log event.

    Args:
        logger: The logger instance.
        method_name: The log method name (info, error, etc.).
        event_dict: The event dictionary.

    Returns:
        Event dictionary with component added.
    """
    # Extract component name from logger name (e.g., "personal_agent.orchestrator" -> "orchestrator")
    # Guard against None logger (can happen with third-party libraries during shutdown)
    if logger is None or not hasattr(logger, "name"):
        event_dict["component"] = "unknown"
        return event_dict

    logger_name = logger.name
    if "." in logger_name:
        component = logger_name.split(".")[-1]
    else:
        component = logger_name
    event_dict["component"] = component
    return event_dict


def _add_component_from_event_dict(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Add component name to log event from event_dict logger name.

    This processor works with structlog's event_dict which contains the logger name
    after add_logger_name processor runs.

    Args:
        logger: The structlog logger instance.
        method_name: The log method name (info, error, etc.).
        event_dict: The event dictionary.

    Returns:
        Event dictionary with component added.
    """
    # Get logger name from event_dict (added by add_logger_name processor)
    logger_name = event_dict.get("logger", "")

    # Extract component name (last part of dotted name)
    if "." in logger_name:
        component = logger_name.split(".")[-1]
    else:
        component = logger_name or "unknown"

    event_dict["component"] = component
    return event_dict


def _configure_file_handler(log_dir: pathlib.Path) -> logging.handlers.RotatingFileHandler:
    """Configure rotating file handler for JSON logs.

    Args:
        log_dir: Directory for log files.

    Returns:
        Configured RotatingFileHandler.
    """
    # Ensure log directory exists
    log_dir.mkdir(parents=True, exist_ok=True)

    # Create rotating file handler
    log_file = log_dir / "current.jsonl"
    handler = logging.handlers.RotatingFileHandler(
        filename=str(log_file),
        maxBytes=100 * 1024 * 1024,  # 100 MB
        backupCount=5,
        encoding="utf-8",
    )

    # Set formatter to JSON
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                _add_timestamp,  # type: ignore[list-item]
                _add_component,  # type: ignore[list-item]
            ],
        )
    )

    return handler


def _configure_console_handler() -> logging.StreamHandler[Any]:
    """Configure console handler for pretty-printed logs.

    Returns:
        Configured StreamHandler.
    """
    handler = logging.StreamHandler(sys.stderr)

    # Set formatter to pretty-printed console output
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=True),
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                _add_timestamp,  # type: ignore[list-item]
                _add_component,  # type: ignore[list-item]
            ],
        )
    )

    return handler


def configure_logging() -> None:
    """Configure structlog for structured logging.

    This function should be called once at application startup to set up
    structured logging with JSON file output and pretty-printed console output.
    """
    log_level = _get_log_level()
    log_dir = _get_log_dir()

    # Root logger accepts all levels; individual handlers gate output.
    # This ensures telemetry INFO events reach the ES handler and file
    # handler even when the user-configured level is WARNING.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG,
    )

    # Get root logger and add handlers
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()  # Remove default handler

    # Silence noisy third-party loggers.
    logging.getLogger("elastic_transport").setLevel(logging.ERROR)
    logging.getLogger("elastic_transport.transport").setLevel(logging.ERROR)
    logging.getLogger("elastic_transport.node_pool").setLevel(logging.ERROR)
    logging.getLogger("elasticsearch").setLevel(logging.ERROR)
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    configured_level = getattr(logging, log_level, logging.INFO)

    # File handler captures INFO+ (telemetry events) regardless of user config
    file_handler = _configure_file_handler(log_dir)
    file_handler.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)

    # Console handler uses the user-configured level (e.g. WARNING)
    console_handler = _configure_console_handler()
    console_handler.setLevel(configured_level)
    root_logger.addHandler(console_handler)

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            _add_component_from_event_dict,  # type: ignore[list-item]
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def add_elasticsearch_handler(handler: logging.Handler) -> None:
    """Add an Elasticsearch handler to the logging system.

    This should be called after the service has connected to Elasticsearch
    to enable automatic forwarding of all logs.

    Args:
        handler: Elasticsearch logging handler (already connected)
    """
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)


def get_logger(name: str) -> Any:  # Returns structlog.stdlib.BoundLogger
    """Get a structured logger instance.

    Args:
        name: Logger name (typically __name__ of the calling module).

    Returns:
        Configured structlog logger instance.

    Example:
        >>> from personal_agent.telemetry import get_logger
        >>> log = get_logger(__name__)
        >>> log.info("task_started", task_id="123", trace_id="abc")
    """
    # Ensure logging is configured (idempotent)
    if not structlog.is_configured():
        configure_logging()

    return structlog.get_logger(name)
