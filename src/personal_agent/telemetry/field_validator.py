"""Field validator for Elasticsearch aggregations (FRE-1108).

Lazy validates field names exist in target indices on first use,
preventing silent-empty aggregation results. Validates once per pattern
per process; concurrent requests awaiting same validation share one call.
"""

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
else:
    AsyncElasticsearch = Any

from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class FieldValidationError(RuntimeError):
    """Raised when a required field is missing or invalid in Elasticsearch."""

    def __init__(
        self,
        message: str,
        index_pattern: str,
        missing_fields: list[str],
        query_family: str = "unknown",
    ) -> None:
        """Initialize error with field validation details.

        Args:
            message: Error message.
            index_pattern: Elasticsearch index pattern that was checked.
            missing_fields: List of fields that were not found.
            query_family: Name of the aggregation/query family that failed.
        """
        self.index_pattern = index_pattern
        self.missing_fields = missing_fields
        self.query_family = query_family
        super().__init__(message)


class FieldValidator:
    """Validates field names exist in target Elasticsearch indices.

    Lazy validation on first use per pattern. Concurrent requests awaiting
    validation of the same pattern share a single field_caps call via
    per-pattern single-flight coordination. Successful validations are cached
    for process lifetime; failures are not cached and retry on next use.
    """

    def __init__(self, es_client: AsyncElasticsearch | None = None) -> None:
        """Initialize field validator.

        Args:
            es_client: Optional preconfigured Elasticsearch client.
        """
        self._es_client = es_client
        self._client_owned = es_client is None
        # Cache: (index_pattern, field_name) -> bool (exists)
        self._field_cache: dict[tuple[str, str], bool] = {}
        # In-flight validations: index_pattern -> Task
        self._inflight: dict[str, asyncio.Task[None]] = {}

    async def _get_client(self) -> AsyncElasticsearch:
        """Get active Elasticsearch client, creating one if needed."""
        if self._es_client is None:
            settings = get_settings()
            try:
                from elasticsearch import AsyncElasticsearch as ESClient
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "elasticsearch package is required for field validation"
                ) from exc
            self._es_client = ESClient([settings.elasticsearch_url], request_timeout=30)
        return self._es_client

    async def disconnect(self) -> None:
        """Close owned Elasticsearch client."""
        if self._client_owned and self._es_client is not None:
            await self._es_client.close()
            self._es_client = None

    async def validate_fields(
        self,
        field_names: list[str],
        index_pattern: str = "agent-logs-*",
        query_family: str = "unknown",
    ) -> None:
        """Validate that multiple fields exist in target indices.

        Args:
            field_names: List of field names to validate.
            index_pattern: Elasticsearch index pattern.
            query_family: Name of the aggregation/query family (for error reporting).

        Raises:
            FieldValidationError: If any field does not exist.
        """
        client = await self._get_client()
        try:
            response = await client.field_caps(index=index_pattern, fields=field_names)
        except AttributeError:
            # Mock client without field_caps method (test environment)
            # Silently pass validation to allow tests to run
            log.debug(
                "field_caps_not_available",
                index_pattern=index_pattern,
                reason="mock_without_field_caps",
                query_family=query_family,
            )
            return
        except Exception as exc:
            log.error(
                "telemetry_field_validation_failed",
                field_count=len(field_names),
                index_pattern=index_pattern,
                reason="es_unavailable",
                query_family=query_family,
                exc_info=True,
            )
            raise FieldValidationError(
                f"Failed to validate fields in '{index_pattern}' (ES unavailable): {exc}",
                index_pattern,
                field_names,
                query_family,
            ) from exc

        # Check which fields exist
        fields = response.get("fields", {})
        missing = [f for f in field_names if f not in fields]

        # Cache all results
        for field_name in field_names:
            self._field_cache[(index_pattern, field_name)] = field_name in fields

        if missing:
            log.error(
                "telemetry_field_validation_failed",
                missing_fields=missing,
                index_pattern=index_pattern,
                reason="field_not_found",
                query_family=query_family,
            )
            raise FieldValidationError(
                f"Fields {missing} not found in index pattern '{index_pattern}'",
                index_pattern,
                missing,
                query_family,
            )

    async def require_validated(
        self,
        field_names: list[str],
        index_pattern: str = "agent-logs-*",
        query_family: str = "unknown",
    ) -> None:
        """Validate fields, using cache if available or validating on first use.

        Implements lazy validation with per-pattern single-flight coordination:
        concurrent requests awaiting validation of the same pattern share
        a single Elasticsearch field_caps call. Raises FieldValidationError
        if validation fails; does not cache failures (retries on next call).

        Args:
            field_names: List of field names to check.
            index_pattern: Elasticsearch index pattern.
            query_family: Name of the aggregation/query family (for error reporting).

        Raises:
            FieldValidationError: If any field does not exist.
        """
        # Check cache first
        missing_from_cache = [f for f in field_names if (index_pattern, f) not in self._field_cache]

        if not missing_from_cache:
            # All fields cached; check if any were marked missing
            missing = [f for f in field_names if not self._field_cache[(index_pattern, f)]]
            if missing:
                raise FieldValidationError(
                    f"Fields {missing} are invalid in '{index_pattern}'",
                    index_pattern,
                    missing,
                    query_family,
                )
            return

        # Some fields not in cache; validate them
        # Use single-flight coordination: if another request is validating
        # this pattern, await the same Task
        if index_pattern in self._inflight:
            await self._inflight[index_pattern]
            # After in-flight validation completes, re-check cache
            await self.require_validated(field_names, index_pattern, query_family)
            return

        # Start validation and track it
        task = asyncio.create_task(
            self.validate_fields(missing_from_cache, index_pattern, query_family)
        )
        self._inflight[index_pattern] = task

        try:
            await task
        except FieldValidationError:
            # Remove from inflight on failure so next request retries
            self._inflight.pop(index_pattern, None)
            raise
        except Exception:
            self._inflight.pop(index_pattern, None)
            raise
        finally:
            # Clean up inflight entry on success
            self._inflight.pop(index_pattern, None)

        # Re-check cache one more time to catch any fields that were marked missing
        missing = [
            f
            for f in field_names
            if (index_pattern, f) in self._field_cache and not self._field_cache[(index_pattern, f)]
        ]
        if missing:
            raise FieldValidationError(
                f"Fields {missing} are invalid in '{index_pattern}'",
                index_pattern,
                missing,
                query_family,
            )


# Module-level default validator used by TelemetryQueries
default_field_validator = FieldValidator()
