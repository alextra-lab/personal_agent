"""Tests for retention policies (lifecycle.py)."""

from datetime import timedelta

import pytest

from personal_agent.telemetry.lifecycle import RETENTION_POLICIES, RetentionPolicy


def test_retention_policy_should_purge() -> None:
    """should_purge returns False when cold_duration is 0."""
    policy = RetentionPolicy(
        name="Never delete",
        hot_duration=timedelta(days=1),
        warm_duration=timedelta(days=2),
        cold_duration=timedelta(days=0),
    )
    assert policy.should_purge(timedelta(days=365)) is False


def test_retention_policy_should_purge_when_old() -> None:
    """should_purge returns True when age exceeds cold_duration."""
    policy = RetentionPolicy(
        name="90d",
        hot_duration=timedelta(days=14),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=90),
    )
    assert policy.should_purge(timedelta(days=91)) is True
    assert policy.should_purge(timedelta(days=90)) is False
    assert policy.should_purge(timedelta(days=89)) is False


def test_retention_policy_should_archive() -> None:
    """should_archive respects archive_enabled and hot_duration."""
    policy = RetentionPolicy(
        name="Test",
        hot_duration=timedelta(days=7),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=30),
        archive_enabled=True,
    )
    assert policy.should_archive(timedelta(days=8)) is True
    assert policy.should_archive(timedelta(days=6)) is False

    policy_no_archive = RetentionPolicy(
        name="No archive",
        hot_duration=timedelta(days=7),
        warm_duration=timedelta(days=14),
        cold_duration=timedelta(days=30),
        archive_enabled=False,
    )
    assert policy_no_archive.should_archive(timedelta(days=100)) is False


def test_default_policies_keys() -> None:
    """Default policies include expected data types."""
    expected = {
        "file_logs",
        "captains_log_captures",
        "captains_log_reflections",
        "elasticsearch_logs",
        "neo4j_graph",
    }
    assert set(RETENTION_POLICIES.keys()) == expected


def test_neo4j_never_delete() -> None:
    """Neo4j policy has cold_duration=0 (never delete)."""
    policy = RETENTION_POLICIES["neo4j_graph"]
    assert policy.cold_duration.total_seconds() == 0
    assert policy.archive_enabled is False


def test_elasticsearch_no_archive() -> None:
    """Elasticsearch policy has archive_enabled=False."""
    policy = RETENTION_POLICIES["elasticsearch_logs"]
    assert policy.archive_enabled is False
