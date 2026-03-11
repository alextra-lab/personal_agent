"""Root conftest: global test configuration and guards.

Prevents test data from contaminating the production Neo4j graph by:
1. Cleaning up test-tagged nodes after each integration test session
2. Setting AGENT_ENVIRONMENT=test so application code can detect test mode
"""

import os

import pytest

# Set environment to "test" before any application code loads settings.
# This allows guards like write_capture() to skip disk writes during tests.
os.environ.setdefault("AGENT_ENVIRONMENT", "test")


@pytest.fixture(autouse=True, scope="session")
def _set_test_environment():
    """Ensure AGENT_ENVIRONMENT=test for the entire test session."""
    original = os.environ.get("AGENT_ENVIRONMENT")
    os.environ["AGENT_ENVIRONMENT"] = "test"
    yield
    if original is None:
        os.environ.pop("AGENT_ENVIRONMENT", None)
    else:
        os.environ["AGENT_ENVIRONMENT"] = original


def pytest_collection_modifyitems(items):
    """Auto-mark tests in test_memory/ and test_second_brain/ as integration."""
    for item in items:
        # Tests that touch Neo4j should be explicitly marked
        if "test_memory" in str(item.fspath) or "test_second_brain" in str(item.fspath):
            if not item.get_closest_marker("integration"):
                item.add_marker(pytest.mark.integration)
