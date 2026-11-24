"""Share-link rules with no state (ADR-0153 Amendment 1 L3, L5, L7, L9 / FRE-1782).

Covers the token format and its hash (L3, V-L8 first half), which items a link opens (L5), the
served filename (L7, V-L12, ticket AC-8), and who is a link creator (L9).
"""

from __future__ import annotations

import base64
import hashlib
from email.message import Message
from uuid import uuid4

import pytest

from personal_agent.config import settings
from personal_agent.service.auth import RequestUser
from personal_agent.service.share_link_policy import (
    ResolveCreators,
    is_link_creator,
    is_shareable,
    new_token,
    resolve_creators,
    safe_filename,
    served_content_type,
    token_hash_of,
)

_OWNER = "fre1782-owner@example.com"
_CREATOR = "fre1782-creator@example.com"
_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


@pytest.fixture
def auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", True)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "share_link_creator_emails", [_CREATOR])


def _user(email: str) -> RequestUser:
    return RequestUser(user_id=uuid4(), email=email)


# --- L3: the token -------------------------------------------------------------------------


def test_a_token_is_43_unpadded_base64url_characters_and_its_hash_is_sha256_of_32_bytes() -> None:
    token = new_token()
    assert len(token.token) == 43
    assert "=" not in token.token
    raw = base64.urlsafe_b64decode(token.token + "=")
    assert len(raw) == 32
    assert token.token_hash == hashlib.sha256(raw).digest()
    assert token_hash_of(token.token) == token.token_hash


def test_two_tokens_differ() -> None:
    assert new_token().token != new_token().token


@pytest.mark.parametrize(
    "token",
    ["", "short", "A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + "+", "A" * 42 + "/", "é" * 43],
)
def test_a_malformed_token_has_no_hash(token: str) -> None:
    assert token_hash_of(token) is None


def test_a_non_canonical_encoding_of_the_same_bytes_is_refused() -> None:
    token = new_token().token
    # The last character carries 2 unused bits. Changing only them decodes to the same bytes.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last = alphabet.index(token[-1])
    variant = token[:-1] + alphabet[last ^ 1]
    assert base64.urlsafe_b64decode(variant + "=") == base64.urlsafe_b64decode(token + "=")
    assert token_hash_of(variant) is None


# --- L5: shareable items -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item_type", "content_type", "pending", "expected"),
    [
        ("artifact", "text/html; charset=utf-8", False, True),
        ("artifact", "TEXT/HTML", False, True),
        ("artifact", "text/markdown; charset=utf-8", False, False),
        ("upload", "image/png", False, True),
        ("upload", "image/jpeg", False, True),
        ("upload", "image/gif", False, True),
        ("upload", "image/webp", False, True),
        ("upload", "image/svg+xml", False, True),
        ("upload", "application/pdf", False, True),
        ("upload", "image/png", True, False),
        ("upload", "text/plain", False, False),
        ("upload", "text/markdown", False, False),
        ("upload", "text/csv", False, False),
        ("upload", "application/json", False, False),
        ("upload", "text/html", False, False),
        ("note", "text/markdown; charset=utf-8", False, False),
        ("capture", "image/png", False, False),
    ],
)
def test_only_html_artifacts_and_image_or_pdf_uploads_are_shareable(
    item_type: str, content_type: str, pending: bool, expected: bool
) -> None:
    assert (
        is_shareable(item_type=item_type, content_type=content_type, upload_pending=pending)
        is expected
    )


# --- L7: the filename (AC-8, V-L12) --------------------------------------------------------


def _parsed_filename(header: str) -> str | None:
    message = Message()
    message["Content-Disposition"] = header
    return message.get_param("filename", header="Content-Disposition")  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("text/html; charset=utf-8", "html"),
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/svg+xml", "svg"),
        ("application/pdf", "pdf"),
    ],
)
def test_a_hostile_title_gives_a_safe_filename_with_the_right_extension(
    content_type: str, extension: str
) -> None:
    title = 'Q3 "report"; filename=evil.exe\r\nSet-Cookie: x=1\n'
    name = safe_filename(slug=None, title=title, content_type=content_type)
    header = f'inline; filename="{name}"'

    assert "\r" not in header and "\n" not in header
    parsed = _parsed_filename(header)
    assert parsed == name
    assert set(parsed) <= _SAFE
    assert parsed.endswith(f".{extension}")
    assert parsed == f"Q3reportfilenameevil.exeSet-Cookiex1.{extension}"


def test_the_slug_wins_over_the_title_and_the_stem_is_cut_to_80() -> None:
    assert safe_filename(slug="my-report", title="Other", content_type="text/html") == (
        "my-report.html"
    )
    long = safe_filename(slug=None, title="a" * 200, content_type="image/png")
    assert long == "a" * 80 + ".png"


@pytest.mark.parametrize(("slug", "title"), [(None, None), ("", ""), (None, '"\r\n;')])
def test_an_empty_result_becomes_artifact(slug: str | None, title: str | None) -> None:
    assert safe_filename(slug=slug, title=title, content_type="application/pdf") == "artifact.pdf"


def test_a_snapshot_is_served_as_utf8_html_and_an_upload_keeps_its_type() -> None:
    assert served_content_type(content_type="text/html", is_snapshot=True) == (
        "text/html; charset=utf-8"
    )
    assert served_content_type(content_type="image/png", is_snapshot=False) == "image/png"


# --- L9: link creators ---------------------------------------------------------------------


@pytest.mark.usefixtures("auth_on")
def test_the_owner_and_a_listed_email_are_creators_and_others_are_not() -> None:
    assert is_link_creator(_user(_OWNER))
    assert is_link_creator(_user(_OWNER.upper()))
    assert is_link_creator(_user(_CREATOR))
    assert is_link_creator(_user(f" {_CREATOR.upper()} "))
    assert not is_link_creator(_user("fre1782-other@example.com"))


def test_a_dev_mode_identity_is_never_a_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", False)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "share_link_creator_emails", [_CREATOR])
    assert not is_link_creator(_user(_OWNER))
    assert not is_link_creator(_user(_CREATOR))


def test_the_default_list_is_empty_so_only_the_owner_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert type(settings).model_fields["share_link_creator_emails"].default == []
    assert type(settings).model_fields["share_public_base_url"].default is None
    monkeypatch.setattr(settings, "gateway_auth_enabled", True)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "share_link_creator_emails", [])
    assert is_link_creator(_user(_OWNER))
    assert not is_link_creator(_user(_CREATOR))
    monkeypatch.setattr(settings, "share_link_creator_emails", ["", "  "])
    assert not is_link_creator(_user(""))


# --- FRE-1785: the creators that the resolve binds -----------------------------------------


def _resolve_admits(email: str) -> bool:
    """The resolve statement's creator test, in Python, on a stored (lowercased) users.email."""
    creators = resolve_creators()
    stored = email.lower()
    return stored == creators.owner_email or stored.strip() in creators.listed_emails


def test_the_resolve_binds_the_owner_lowercased_and_the_list_trimmed_and_lowercased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", True)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER.upper())
    monkeypatch.setattr(settings, "share_link_creator_emails", [f" {_CREATOR.upper()} ", "", " "])
    creators = resolve_creators()
    assert creators.owner_email == _OWNER
    assert creators.listed_emails == (_CREATOR,)


def test_with_an_empty_list_the_resolve_still_binds_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", True)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "share_link_creator_emails", [])
    assert resolve_creators() == ResolveCreators(owner_email=_OWNER, listed_emails=())


def test_dev_mode_binds_no_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", False)
    monkeypatch.setattr(settings, "agent_owner_email", _OWNER)
    monkeypatch.setattr(settings, "share_link_creator_emails", [_CREATOR])
    assert resolve_creators() == ResolveCreators(owner_email=None, listed_emails=())


@pytest.mark.parametrize("auth", [True, False])
@pytest.mark.parametrize("owner", [_OWNER, _OWNER.upper(), f" {_OWNER} ", None])
@pytest.mark.parametrize("listed", [[], [_CREATOR], [f" {_CREATOR.upper()} "], [""]])
@pytest.mark.parametrize("email", [_OWNER, _OWNER.upper(), _CREATOR, _CREATOR.upper(), "x@y.z"])
def test_the_resolve_and_the_create_path_agree_on_who_is_a_creator(
    monkeypatch: pytest.MonkeyPatch,
    auth: bool,
    owner: str | None,
    listed: list[str],
    email: str,
) -> None:
    monkeypatch.setattr(settings, "gateway_auth_enabled", auth)
    monkeypatch.setattr(settings, "agent_owner_email", owner)
    monkeypatch.setattr(settings, "share_link_creator_emails", listed)
    assert _resolve_admits(email) is is_link_creator(_user(email))
