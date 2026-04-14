"""Tests for gateway auth module (FRE-206).

Covers:
- load_token_config parsing
- verify_token with auth disabled (fast-path)
- verify_token with auth enabled (valid/invalid tokens)
- require_scope scope checking
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.gateway.auth import (
    TokenInfo,
    _DEV_TOKEN,
    _reset_token_registry,
    load_token_config,
    require_scope,
    verify_token,
)


# ---------------------------------------------------------------------------
# load_token_config
# ---------------------------------------------------------------------------


def test_load_token_config_basic(tmp_path: Path) -> None:
    """load_token_config parses a minimal YAML file correctly."""
    yaml_text = textwrap.dedent(
        """\
        tokens:
          - name: test-token
            secret: "abc123"
            scopes: [knowledge:read, sessions:read]
            rate_limit: 50
        """
    )
    config_file = tmp_path / "gateway_access.yaml"
    config_file.write_text(yaml_text)

    registry = load_token_config(config_file)

    assert "abc123" in registry
    info = registry["abc123"]
    assert info.name == "test-token"
    assert "knowledge:read" in info.scopes
    assert "sessions:read" in info.scopes
    assert info.rate_limit == 50


def test_load_token_config_rate_limit_string(tmp_path: Path) -> None:
    """load_token_config handles 'N/hour' rate limit format."""
    yaml_text = textwrap.dedent(
        """\
        tokens:
          - name: token-a
            secret: "xyz"
            scopes: [knowledge:read]
            rate_limit: "200/hour"
        """
    )
    config_file = tmp_path / "access.yaml"
    config_file.write_text(yaml_text)

    registry = load_token_config(config_file)
    assert registry["xyz"].rate_limit == 200


def test_load_token_config_env_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """load_token_config expands ${ENV_VAR} references."""
    monkeypatch.setenv("MY_TEST_SECRET", "super-secret-value")
    yaml_text = textwrap.dedent(
        """\
        tokens:
          - name: env-token
            secret: "${MY_TEST_SECRET}"
            scopes: [knowledge:write]
            rate_limit: 10
        """
    )
    config_file = tmp_path / "access.yaml"
    config_file.write_text(yaml_text)

    registry = load_token_config(config_file)
    assert "super-secret-value" in registry
    assert registry["super-secret-value"].name == "env-token"


def test_load_token_config_missing_file(tmp_path: Path) -> None:
    """load_token_config returns empty dict for non-existent file."""
    registry = load_token_config(tmp_path / "nonexistent.yaml")
    assert registry == {}


def test_load_token_config_empty_yaml(tmp_path: Path) -> None:
    """load_token_config handles an empty YAML file gracefully."""
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("")
    registry = load_token_config(config_file)
    assert registry == {}


# ---------------------------------------------------------------------------
# verify_token — auth disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_token_auth_disabled_returns_dev_token() -> None:
    """verify_token returns _DEV_TOKEN when gateway_auth_enabled is False."""
    with patch("personal_agent.gateway.auth.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_auth_enabled=False)
        result = await verify_token(credentials=None)

    assert result is _DEV_TOKEN
    assert "knowledge:read" in result.scopes


# ---------------------------------------------------------------------------
# verify_token — auth enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_token_missing_credentials_raises_401() -> None:
    """verify_token raises 401 when credentials are absent and auth is enabled."""
    from fastapi import HTTPException

    with patch("personal_agent.gateway.auth.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(gateway_auth_enabled=True)
        with pytest.raises(HTTPException) as exc_info:
            await verify_token(credentials=None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_valid_token(tmp_path: Path) -> None:
    """verify_token returns TokenInfo for a known valid token."""
    from fastapi.security import HTTPAuthorizationCredentials

    yaml_text = textwrap.dedent(
        """\
        tokens:
          - name: my-app
            secret: "valid-secret"
            scopes: [knowledge:read]
            rate_limit: 100
        """
    )
    config_file = tmp_path / "access.yaml"
    config_file.write_text(yaml_text)

    _reset_token_registry()
    try:
        with (
            patch("personal_agent.gateway.auth.get_settings") as mock_settings,
            patch("personal_agent.gateway.auth._get_token_registry") as mock_registry,
        ):
            mock_settings.return_value = MagicMock(
                gateway_auth_enabled=True,
                gateway_access_config=str(config_file),
            )
            mock_registry.return_value = load_token_config(config_file)

            creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-secret")
            result = await verify_token(credentials=creds)

        assert result.name == "my-app"
        assert "knowledge:read" in result.scopes
    finally:
        _reset_token_registry()


@pytest.mark.asyncio
async def test_verify_token_invalid_token_raises_401(tmp_path: Path) -> None:
    """verify_token raises 401 for an unknown token when auth is enabled."""
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    _reset_token_registry()
    try:
        with (
            patch("personal_agent.gateway.auth.get_settings") as mock_settings,
            patch("personal_agent.gateway.auth._get_token_registry") as mock_registry,
        ):
            mock_settings.return_value = MagicMock(gateway_auth_enabled=True)
            mock_registry.return_value = {}  # No known tokens

            creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token")
            with pytest.raises(HTTPException) as exc_info:
                await verify_token(credentials=creds)

        assert exc_info.value.status_code == 401
    finally:
        _reset_token_registry()


# ---------------------------------------------------------------------------
# require_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_scope_passes_when_scope_present() -> None:
    """require_scope dependency passes when scope is in token.scopes."""
    token = TokenInfo(
        name="test",
        scopes=frozenset(["knowledge:read", "sessions:read"]),
        rate_limit=100,
    )
    dep = require_scope("knowledge:read")
    # Manually call the inner _check coroutine with the token
    inner = dep.__wrapped__ if hasattr(dep, "__wrapped__") else dep
    # Simulate: call the closure-produced coroutine with the already-verified token
    # We need to invoke the inner _check function
    import inspect

    closures = [c.cell_contents for c in dep.__code__.co_consts if False]  # noqa: S101
    # Direct invocation: extract _check from require_scope closure
    # Since require_scope returns an async def _check, we call it directly
    result = await dep.__call__(token) if hasattr(dep, "__call__") else None  # type: ignore[misc]
    # Actually just test via the closure; the function is opaque. Use a simpler approach:
    from personal_agent.gateway.auth import require_scope as rs

    async def _call_inner(t: TokenInfo) -> TokenInfo:
        # require_scope("x") returns a callable whose inner dep takes token via Depends
        # We bypass Depends by calling the inner function signature directly.
        # The inner function is an async def _check with signature (token: TokenInfo).
        # FastAPI only wires Depends at request time; in tests we call _check directly.
        check_fn = rs(scope="knowledge:read")
        # Grab the underlying coroutine function (unwrap from Depends if needed)
        # We can get it from the closure
        return await check_fn(t)  # type: ignore[call-arg]

    result2 = await _call_inner(token)
    assert result2 is token


@pytest.mark.asyncio
async def test_require_scope_raises_403_when_scope_missing() -> None:
    """require_scope raises 403 when required scope is absent from token."""
    from fastapi import HTTPException

    token = TokenInfo(
        name="limited",
        scopes=frozenset(["sessions:read"]),
        rate_limit=100,
    )
    from personal_agent.gateway.auth import require_scope as rs

    dep = rs(scope="knowledge:write")

    with pytest.raises(HTTPException) as exc_info:
        await dep(token)  # type: ignore[call-arg]

    assert exc_info.value.status_code == 403
