"""Field validator for Elasticsearch aggregations (FRE-1108).

Detects when an aggregation references a field that does not exist in the target
index/indices, preventing silent-empty aggregation results. Singleton pattern ensures
startup preflight validation before traffic, with cache-only checks on hot path.
"""

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

    Process-level singleton validating fields at startup (preflight) and
    caching results for the request path. Validation failures raise FieldValidationError
    to prevent silent-empty aggregation results.
    """

    _instance: "FieldValidator | None" = None

    def __new__(cls, es_client: AsyncElasticsearch | None = None) -> "FieldValidator":
        """Implement singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, es_client: AsyncElasticsearch | None = None) -> None:
        """Initialize field validator.

        Args:
            es_client: Optional preconfigured Elasticsearch client.
        """
        if self._initialized:
            return
        self._es_client = es_client
        self._client_owned = es_client is None
        self._initialized = True
        # Cache: index_pattern -> {field_name -> bool (exists)}
        self._field_cache: dict[str, dict[str, bool]] = {}
        # Track which patterns have been validated (preflight complete)
        self._validation_ready: dict[str, bool] = {}

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

        # Initialize cache entry if needed
        if index_pattern not in self._field_cache:
            self._field_cache[index_pattern] = {}

        # Check which fields exist
        fields = response.get("fields", {})
        missing = [f for f in field_names if f not in fields]

        # Cache all results
        for field_name in field_names:
            self._field_cache[index_pattern][field_name] = field_name in fields

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

        # Mark this pattern as validated
        self._validation_ready[index_pattern] = True

    def require_validated(
        self, field_names: list[str], index_pattern: str, query_family: str = "unknown"
    ) -> None:
        """Assert that fields have been validated (cache-only, no network I/O).

        Called on the request path to verify validation was done at startup.
        Raises FieldValidationError if validation has not been completed or
        a field was previously found to be missing.

        Args:
            field_names: List of field names to check.
            index_pattern: Elasticsearch index pattern.
            query_family: Name of the aggregation/query family (for error reporting).

        Raises:
            FieldValidationError: If not yet validated or any field is missing.
        """
        if index_pattern not in self._validation_ready:
            raise FieldValidationError(
                f"Validation not completed for index pattern '{index_pattern}'; "
                "call await preflight_validate() at startup",
                index_pattern,
                [],
                query_family,
            )

        if index_pattern not in self._field_cache:
            raise FieldValidationError(
                f"Cache missing for index pattern '{index_pattern}'",
                index_pattern,
                [],
                query_family,
            )

        # Check for cached missing fields
        missing = [
            f
            for f in field_names
            if f in self._field_cache[index_pattern] and not self._field_cache[index_pattern][f]
        ]

        if missing:
            raise FieldValidationError(
                f"Fields {missing} are invalid in '{index_pattern}'",
                index_pattern,
                missing,
                query_family,
            )
