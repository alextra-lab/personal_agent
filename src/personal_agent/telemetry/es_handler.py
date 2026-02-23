"""Elasticsearch logging handler for structlog integration."""

import asyncio
import logging
import time
from typing import Any

from personal_agent.telemetry.es_logger import ElasticsearchLogger


class ElasticsearchHandler(logging.Handler):
    """Logging handler that forwards logs to Elasticsearch.

    This handler allows structlog to send all logs to Elasticsearch
    in addition to file/console output.
    """

    def __init__(self, es_url: str = "http://localhost:9200", index_prefix: str = "agent-logs"):
        """Initialize Elasticsearch handler.

        Args:
            es_url: Elasticsearch URL
            index_prefix: Index name prefix
        """
        super().__init__()
        self.es_logger = ElasticsearchLogger(es_url, index_prefix)
        self._connected = False
        self._connect_attempted = False
        # Limit concurrent ES writes to prevent connection pool exhaustion
        self._write_semaphore = asyncio.Semaphore(10)
        # Circuit breaker to prevent timeout storms from flooding logs/output.
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._circuit_breaker_threshold = 3
        self._circuit_breaker_cooldown_s = 30.0

    def _is_circuit_open(self) -> bool:
        """Return True when ES writes are temporarily paused."""
        return time.monotonic() < self._circuit_open_until

    def _record_failure(self) -> None:
        """Track a failed ES write and open circuit when threshold reached."""
        self._failure_count += 1
        if self._failure_count >= self._circuit_breaker_threshold:
            self._circuit_open_until = time.monotonic() + self._circuit_breaker_cooldown_s
            self._failure_count = 0
            logging.getLogger(__name__).warning(
                "elasticsearch_circuit_opened",
                extra={"cooldown_seconds": self._circuit_breaker_cooldown_s},
            )

    def _record_success(self) -> None:
        """Reset transient failure tracking after successful write."""
        self._failure_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record to Elasticsearch.

        Args:
            record: Log record to emit
        """
        # Skip if not connected
        if not self._connected:
            return

        # Circuit open: skip ES forwarding temporarily
        if self._is_circuit_open():
            return

        # Filter out Elasticsearch client's own logs to prevent feedback loop
        if record.name.startswith(("elastic_transport", "elasticsearch")):
            return

        # Filter out other noisy third-party logs
        if record.name.startswith(("neo4j", "httpx", "httpcore")):
            return

        # Parse the structured log message
        try:
            # Structlog stores the event_dict in record.msg as a dict (before formatting)
            event_dict = {}

            # Extract from record.msg if it's a dict (structlog format)
            if isinstance(record.msg, dict):
                event_dict = record.msg.copy()
            # Fallback: try to extract from record attributes (standard logging)
            else:
                for key, value in record.__dict__.items():
                    if not key.startswith("_") and key not in (
                        "name",
                        "msg",
                        "args",
                        "created",
                        "filename",
                        "funcName",
                        "levelname",
                        "levelno",
                        "lineno",
                        "module",
                        "msecs",
                        "pathname",
                        "process",
                        "processName",
                        "relativeCreated",
                        "thread",
                        "threadName",
                        "exc_info",
                        "exc_text",
                        "stack_info",
                        "getMessage",
                        "taskName",
                        "stack",
                    ):
                        event_dict[key] = value

            # Extract trace_id if present
            trace_id = event_dict.get("trace_id")
            span_id = event_dict.get("span_id")

            # Extract event type (default to log level if not present)
            event_type = event_dict.get("event", record.levelname.lower())

            # Build event data with rich context
            event_data = {
                "level": record.levelname,
                "logger": record.name,
                "component": event_dict.get("component", "unknown"),
                "module": record.module,
                "function": record.funcName,
                "line_number": record.lineno,
            }

            # Set message to event name (not the full stringified dict)
            # The individual fields are extracted separately below
            event_data["message"] = event_dict.get("event", record.levelname.lower())

            # Add exception info if present
            if record.exc_info:
                import traceback

                event_data["exception"] = "".join(traceback.format_exception(*record.exc_info))

            # Add all custom fields from event_dict (this is where the interesting data is)
            for key, value in event_dict.items():
                if key not in (
                    "event",
                    "trace_id",
                    "span_id",
                    "component",
                    "level",
                    "logger",
                    "message",
                    "timestamp",
                    "module",
                    "function",
                    "line_number",
                ):
                    # Convert non-JSON-serializable types
                    try:
                        import json

                        json.dumps(value)
                        event_data[key] = value
                    except (TypeError, ValueError):
                        event_data[key] = str(value)

            # Send to Elasticsearch asynchronously (non-blocking)
            # Use asyncio.create_task if event loop is running, otherwise skip
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._log_async(event_type, event_data, trace_id, span_id))
            except RuntimeError:
                # No event loop, skip ES logging (logs still go to files)
                pass
        except Exception:
            # Don't let logging errors crash the application
            # Silently fail - logs still go to files
            pass

    async def _log_async(
        self,
        event_type: str,
        data: dict[str, Any],
        trace_id: str | None,
        span_id: str | None,
    ) -> None:
        """Asynchronously log event to Elasticsearch with concurrency limiting.

        Args:
            event_type: Event type
            data: Event data
            trace_id: Trace ID
            span_id: Span ID
        """
        if not self._connected:
            return

        if self._is_circuit_open():
            return

        # Use semaphore to limit concurrent ES writes
        async with self._write_semaphore:
            try:
                result = await self.es_logger.log_event(event_type, data, trace_id, span_id)
                if result is None:
                    self._record_failure()
                    return
                self._record_success()
            except Exception:
                # Silently fail - don't break logging
                self._record_failure()

    async def connect(self) -> bool:
        """Connect to Elasticsearch.

        Returns:
            True if connected successfully
        """
        self._connect_attempted = True
        self._connected = await self.es_logger.connect()
        return self._connected

    async def disconnect(self) -> None:
        """Disconnect from Elasticsearch."""
        await self.es_logger.disconnect()
        self._connected = False

    def close(self) -> None:
        """Close handler (sync version for logging.Handler interface)."""
        # We can't await here, so just mark as disconnected
        self._connected = False
        super().close()
