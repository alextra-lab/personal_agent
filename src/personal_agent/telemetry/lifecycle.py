"""Data lifecycle retention policies (Phase 2.3).

Defines hot/warm/cold retention and archival settings per data type.
"""

from datetime import timedelta

from pydantic import BaseModel, Field


class RetentionPolicy(BaseModel):
    """Retention policy for a data type.

    Hot: keep locally, indexed. Warm: archive compressed. Cold: delete after.
    """

    name: str = Field(..., description="Human-readable policy name")
    hot_duration: timedelta = Field(..., description="Keep locally and indexed")
    warm_duration: timedelta = Field(..., description="Archive window (compressed)")
    cold_duration: timedelta = Field(..., description="Delete after this (0 = never delete)")
    archive_enabled: bool = Field(default=True, description="Whether to archive before purge")

    def should_purge(self, age: timedelta) -> bool:
        """Return True if data older than cold_duration should be purged.

        Args:
            age: Age of the data.

        Returns:
            True if age exceeds cold_duration. False if cold_duration is zero (never delete).
        """
        if self.cold_duration.total_seconds() <= 0:
            return False
        return age > self.cold_duration

    def should_archive(self, age: timedelta) -> bool:
        """Return True if data older than hot_duration should be archived.

        Args:
            age: Age of the data.

        Returns:
            True if archive_enabled and age exceeds hot_duration.
        """
        if not self.archive_enabled:
            return False
        return age > self.hot_duration


# Default policies (dev environment — 2 week archival)
RETENTION_POLICIES: dict[str, RetentionPolicy] = {
    "file_logs": RetentionPolicy(
        name="File Logs",
        hot_duration=timedelta(days=7),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=30),
    ),
    "captains_log_captures": RetentionPolicy(
        name="Task Captures",
        hot_duration=timedelta(days=14),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=90),
    ),
    "captains_log_reflections": RetentionPolicy(
        name="Reflections",
        hot_duration=timedelta(days=14),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=180),
    ),
    "elasticsearch_logs": RetentionPolicy(
        name="ES Event Logs",
        hot_duration=timedelta(days=14),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=30),  # Aligned with docker/elasticsearch/ilm-policy.json delete phase (30d)
        archive_enabled=False,
    ),
    "neo4j_graph": RetentionPolicy(
        name="Knowledge Graph",
        hot_duration=timedelta(days=365),
        warm_duration=timedelta(days=730),
        cold_duration=timedelta(days=0),  # Never delete
        archive_enabled=False,
    ),
}
