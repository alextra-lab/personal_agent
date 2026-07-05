"""FRE-795: Test substrate isolation — verify test Postgres decouples from prod .env."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_postgres_test_substrate_password_isolation():
    """Verify test Postgres on :5433 uses test password, not prod .env secret.

    This test ensures that docker-compose.test.yml pins POSTGRES_PASSWORD to
    agent_dev_password, decoupling the test stack from any POSTGRES_PASSWORD
    in the primary .env file. FRE-375 hermetic substrate compliance check.
    """
    import asyncpg

    # Test substrate connection parameters (hardcoded to match conftest.py).
    test_host = "localhost"
    test_port = 5433
    test_user = "agent"
    test_password = "agent_dev_password"
    test_db = "personal_agent"

    # Attempt to connect with test credentials.
    try:
        conn = await asyncpg.connect(
            host=test_host,
            port=test_port,
            user=test_user,
            password=test_password,
            database=test_db,
        )
        # If we reach here, auth succeeded.
        await conn.close()
    except asyncpg.InvalidPasswordError as e:
        pytest.fail(
            f"Test Postgres auth failed at {test_host}:{test_port} with user={test_user}, "
            f"password={test_password}. Error: {e}. "
            f"The test Postgres container may have been initialized with a different password. "
            f"Verify docker-compose.test.yml pins POSTGRES_PASSWORD=agent_dev_password "
            f"and the test volume has been recreated."
        )
    except (OSError, asyncpg.CannotConnectNowError) as e:
        pytest.skip(f"Test Postgres not reachable at {test_host}:{test_port}: {e}")
