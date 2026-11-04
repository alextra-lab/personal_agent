"""A token entry with no real secret does not authenticate (FRE-1732 AC-3).

``load_token_config`` kept an unset ``${VAR}`` as literal text, so the public text of the
config file worked as a bearer token. The ``dev-local`` entry held a plain secret in the same
public file.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from personal_agent.gateway.auth import _reset_token_registry, load_token_config, verify_token

_REPO_CONFIG = Path(__file__).parents[3] / "config" / "gateway_access.yaml"
_PLACEHOLDER = "${FRE1732_UNSET_TOKEN}"
_REMOVED_DEV_TOKEN = "dev-token-not-for-production"


@pytest.fixture(autouse=True)
def _fresh_registry() -> Iterator[None]:
    _reset_token_registry()
    yield
    _reset_token_registry()


def _config(tmp_path: Path, secret: str) -> Path:
    path = tmp_path / "access.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            tokens:
              - name: entry
                secret: "{secret}"
                scopes: [knowledge:read]
                rate_limit: 10
            """
        )
    )
    return path


async def _authenticate(config_path: Path, token: str) -> str:
    """Run the real dependency with auth on, and return the matched token name."""
    settings = MagicMock(gateway_auth_enabled=True, gateway_access_config=str(config_path))
    with patch("personal_agent.gateway.auth.get_settings", return_value=settings):
        info = await verify_token(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    return info.name


@pytest.mark.asyncio
async def test_an_unset_placeholder_does_not_authenticate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The literal text of the config file is not a token."""
    monkeypatch.delenv("FRE1732_UNSET_TOKEN", raising=False)
    config = _config(tmp_path, _PLACEHOLDER)

    with pytest.raises(HTTPException) as refused:
        await _authenticate(config, _PLACEHOLDER)

    assert refused.value.status_code == 401
    assert load_token_config(config) == {}


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_variable_gives_no_token(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A variable that is set to nothing, or to spaces, gives no entry."""
    monkeypatch.setenv("FRE1732_UNSET_TOKEN", value)

    assert load_token_config(_config(tmp_path, _PLACEHOLDER)) == {}


def test_a_secret_that_only_starts_with_a_placeholder_gives_no_token(tmp_path: Path) -> None:
    """Part of a secret that stays unexpanded is still public text."""
    assert load_token_config(_config(tmp_path, "prefix-" + _PLACEHOLDER)) == {}


@pytest.mark.asyncio
async def test_a_set_variable_still_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded positive: the same path accepts a real value, so the refusals above mean something."""
    monkeypatch.setenv("FRE1732_UNSET_TOKEN", "fre1732-real-value")

    name = await _authenticate(_config(tmp_path, _PLACEHOLDER), "fre1732-real-value")

    assert name == "entry"


@pytest.mark.asyncio
async def test_the_removed_dev_token_does_not_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real config file no longer accepts the old public text."""
    for name in ("GATEWAY_TOKEN_CLAUDE_CODE", "GATEWAY_TOKEN_PWA", "GATEWAY_TOKEN_EXTERNAL_AGENT"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(HTTPException) as refused:
        await _authenticate(_REPO_CONFIG, _REMOVED_DEV_TOKEN)

    assert refused.value.status_code == 401


def test_the_repo_config_holds_no_literal_secret() -> None:
    """Every secret in the public file names a variable, so none is usable as written."""
    tokens = yaml.safe_load(_REPO_CONFIG.read_text())["tokens"]

    assert tokens
    for entry in tokens:
        assert entry["secret"].startswith("${"), entry["name"]
        assert entry["secret"].endswith("}"), entry["name"]
