"""Field validator for Elasticsearch aggregations (FRE-1108).

Detects when an aggregation references a field that does not exist in the target
index/indices, preventing silent-empty aggregation results.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
else:
    AsyncElasticsearch = Any

from personal_agent.config.settings import get_settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


class FieldValidator:
    """Validates that field names exist in target Elasticsearch indices."""

    def __init__(self, es_client: AsyncElasticsearch | None = None) -> None:
        """Initialize field validator.

        Args:
            es_client: Optional preconfigured Elasticsearch client.
        """
        self._es_client = es_client
        self._client_owned = es_client is None
        # Cache: index_pattern -> {field_name -> bool (exists)}
        self._field_cache: dict[str, dict[str, bool]] = {}

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

    async def validate_field(self, field_name: str, index_pattern: str = "agent-logs-*") -> None:
        """Validate that a field exists in the target indices.

        Args:
            field_name: Field name to validate (e.g., "trace_id", "event").
            index_pattern: Elasticsearch index pattern (default: agent-logs-*).

        Raises:
            ValueError: If the field does not exist in any index matching the pattern.
        """
        # Check cache first
        if index_pattern in self._field_cache:
            if field_name in self._field_cache[index_pattern]:
                if not self._field_cache[index_pattern][field_name]:
                    raise ValueError(
                        f"Field '{field_name}' not found in index pattern '{index_pattern}'"
                    )
                return

        # Query ES field capabilities
        client = await self._get_client()
        try:
            response = await client.field_caps(index=index_pattern, fields=[field_name])
        except Exception as exc:
            log.warning(
                "field_validation_query_failed",
                field_name=field_name,
                index_pattern=index_pattern,
                exc_info=True,
            )
            raise ValueError(f"Failed to validate field '{field_name}': {exc}") from exc

        # Initialize cache entry if needed
        if index_pattern not in self._field_cache:
            self._field_cache[index_pattern] = {}

        # Check if field exists in response
        fields = response.get("fields", {})
        field_exists = field_name in fields

        # Cache result
        self._field_cache[index_pattern][field_name] = field_exists

        if not field_exists:
            raise ValueError(f"Field '{field_name}' not found in index pattern '{index_pattern}'")

    async def validate_fields(
        self, field_names: list[str], index_pattern: str = "agent-logs-*"
    ) -> list[str]:
        """Validate multiple fields, returning only the ones that exist.

        Args:
            field_names: List of field names to validate.
            index_pattern: Elasticsearch index pattern.

        Returns:
            List of field names that exist in the target indices.
        """
        client = await self._get_client()
        try:
            response = await client.field_caps(index=index_pattern, fields=field_names)
        except Exception as exc:
            log.warning(
                "bulk_field_validation_query_failed",
                field_count=len(field_names),
                index_pattern=index_pattern,
                exc_info=True,
            )
            raise ValueError(f"Failed to validate fields: {exc}") from exc

        # Initialize cache entry if needed
        if index_pattern not in self._field_cache:
            self._field_cache[index_pattern] = {}

        # Extract existing fields and cache results
        fields = response.get("fields", {})
        existing_fields: list[str] = []
        for field_name in field_names:
            field_exists = field_name in fields
            self._field_cache[index_pattern][field_name] = field_exists
            if field_exists:
                existing_fields.append(field_name)

        return existing_fields
