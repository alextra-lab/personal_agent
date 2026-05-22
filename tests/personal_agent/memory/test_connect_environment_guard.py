"""Unit tests for MemoryService.connect() substrate isolation guard (FRE-375).

Verifies that connect() returns False (without touching the Neo4j driver) when
the TEST environment is combined with a prod-fingerprint Neo4j URI, and that
it proceeds normally in all other scenarios.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config.env_loader import Environment  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_settings(
    environment: Environment,
    neo4j_uri: str,
    *,
    allow_bypass: bool = False,
    neo4j_user: str = "neo4j",
    neo4j_password: str = "testpass",  # noqa: S107 — unit test fixture, not real cred
) -> MagicMock:
    """Build a minimal mock of the settings singleton for MemoryService tests.

    Args:
        environment: The simulated APP_ENV value.
        neo4j_uri: The simulated AGENT_NEO4J_URI value.
        allow_bypass: Value for allow_test_writes_to_prod_substrate.
        neo4j_user: Neo4j username (default "neo4j").
        neo4j_password: Neo4j password (default "testpass").

    Returns:
        MagicMock that satisfies MemoryService.connect() attribute access.
    """
    mock = MagicMock()
    mock.environment = environment
    mock.neo4j_uri = neo4j_uri
    mock.neo4j_user = neo4j_user
    mock.neo4j_password = neo4j_password
    mock.allow_test_writes_to_prod_substrate = allow_bypass
    return mock


def _make_mock_driver() -> MagicMock:
    """Return a mock Neo4j async driver whose verify_connectivity() succeeds."""
    mock_driver = MagicMock()
    mock_driver.verify_connectivity = AsyncMock(return_value=None)
    return mock_driver


# ---------------------------------------------------------------------------
# Guard fires — TEST env + prod Neo4j URI
# ---------------------------------------------------------------------------


class TestConnectGuardRefuses:
    """connect() returns False without opening a driver in TEST + prod URI."""

    @pytest.mark.asyncio
    async def test_connect_refuses_prod_uri_in_test_env(self) -> None:
        """connect() returns False when TEST env + default prod Neo4j port."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.TEST,
            neo4j_uri="bolt://localhost:7687",
        )

        with patch("personal_agent.memory.service.settings", mock_settings):
            service = MemoryService()
            result = await service.connect()

        assert result is False
        assert service.connected is False
        assert service.driver is None

    @pytest.mark.asyncio
    async def test_connect_refuses_127_alias_in_test_env(self) -> None:
        """Guard also fires when the URI uses 127.0.0.1 instead of localhost."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.TEST,
            neo4j_uri="bolt://127.0.0.1:7687",
        )

        with patch("personal_agent.memory.service.settings", mock_settings):
            service = MemoryService()
            result = await service.connect()

        assert result is False
        assert service.connected is False

    @pytest.mark.asyncio
    async def test_connect_does_not_call_driver_when_refused(self) -> None:
        """Driver constructor is never invoked when the guard fires."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.TEST,
            neo4j_uri="bolt://localhost:7687",
        )
        mock_driver_cls = MagicMock()

        with (
            patch("personal_agent.memory.service.settings", mock_settings),
            patch("personal_agent.memory.service.Neo4jAsyncGraphDatabase", mock_driver_cls),
        ):
            service = MemoryService()
            await service.connect()

        mock_driver_cls.driver.assert_not_called()


# ---------------------------------------------------------------------------
# Guard silent — bypass flag
# ---------------------------------------------------------------------------


class TestConnectGuardBypassFlag:
    """connect() proceeds when allow_test_writes_to_prod_substrate=True."""

    @pytest.mark.asyncio
    async def test_connect_bypass_flag_allows_prod_uri_in_test_env(self) -> None:
        """Bypass flag allows TEST env to connect to a prod-port Neo4j URI."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.TEST,
            neo4j_uri="bolt://localhost:7687",
            allow_bypass=True,
        )
        mock_driver = _make_mock_driver()
        mock_driver_cls = MagicMock()
        mock_driver_cls.driver = MagicMock(return_value=mock_driver)

        with (
            patch("personal_agent.memory.service.settings", mock_settings),
            patch("personal_agent.memory.service.Neo4jAsyncGraphDatabase", mock_driver_cls),
        ):
            service = MemoryService()
            result = await service.connect()

        assert result is True
        assert service.connected is True
        mock_driver_cls.driver.assert_called_once()


# ---------------------------------------------------------------------------
# Guard silent — non-TEST environments
# ---------------------------------------------------------------------------


class TestConnectGuardSilentForNonTestEnv:
    """connect() guard does not fire for PRODUCTION or DEVELOPMENT environments."""

    @pytest.mark.asyncio
    async def test_connect_prod_env_bypasses_guard(self) -> None:
        """PRODUCTION env is never blocked, even with a prod-fingerprint URI."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.PRODUCTION,
            neo4j_uri="bolt://localhost:7687",
        )
        mock_driver = _make_mock_driver()
        mock_driver_cls = MagicMock()
        mock_driver_cls.driver = MagicMock(return_value=mock_driver)

        with (
            patch("personal_agent.memory.service.settings", mock_settings),
            patch("personal_agent.memory.service.Neo4jAsyncGraphDatabase", mock_driver_cls),
        ):
            service = MemoryService()
            result = await service.connect()

        assert result is True
        assert service.connected is True

    @pytest.mark.asyncio
    async def test_connect_development_env_bypasses_guard(self) -> None:
        """DEVELOPMENT env is never blocked."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.DEVELOPMENT,
            neo4j_uri="bolt://localhost:7687",
        )
        mock_driver = _make_mock_driver()
        mock_driver_cls = MagicMock()
        mock_driver_cls.driver = MagicMock(return_value=mock_driver)

        with (
            patch("personal_agent.memory.service.settings", mock_settings),
            patch("personal_agent.memory.service.Neo4jAsyncGraphDatabase", mock_driver_cls),
        ):
            service = MemoryService()
            result = await service.connect()

        assert result is True


# ---------------------------------------------------------------------------
# Guard silent — test env with non-prod URI
# ---------------------------------------------------------------------------


class TestConnectGuardSilentForTestStackURI:
    """connect() guard does not fire when TEST env uses a non-prod-port URI."""

    @pytest.mark.asyncio
    async def test_connect_test_uri_not_blocked(self) -> None:
        """TEST env with a non-default port (e.g. 7688) is allowed through."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.TEST,
            neo4j_uri="bolt://localhost:7688",  # test stack port
        )
        mock_driver = _make_mock_driver()
        mock_driver_cls = MagicMock()
        mock_driver_cls.driver = MagicMock(return_value=mock_driver)

        with (
            patch("personal_agent.memory.service.settings", mock_settings),
            patch("personal_agent.memory.service.Neo4jAsyncGraphDatabase", mock_driver_cls),
        ):
            service = MemoryService()
            result = await service.connect()

        assert result is True
        assert service.connected is True
        mock_driver_cls.driver.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_non_local_test_uri_not_blocked(self) -> None:
        """TEST env with a non-localhost host on port 7687 is NOT blocked."""
        from personal_agent.memory.service import MemoryService

        mock_settings = _make_mock_settings(
            environment=Environment.TEST,
            neo4j_uri="bolt://neo4j-test-container:7687",
        )
        mock_driver = _make_mock_driver()
        mock_driver_cls = MagicMock()
        mock_driver_cls.driver = MagicMock(return_value=mock_driver)

        with (
            patch("personal_agent.memory.service.settings", mock_settings),
            patch("personal_agent.memory.service.Neo4jAsyncGraphDatabase", mock_driver_cls),
        ):
            service = MemoryService()
            result = await service.connect()

        assert result is True
