"""Share-link rules that hold no state (ADR-0153 Amendment 1, FRE-1782).

Who may create links (L9), which items a link can open (L5), the token format (L3), and the
filename a link serves under (L7). This module reads settings and writes nothing. The only writer
of ``artifact_share_links`` is :mod:`personal_agent.service.share_links_router`. The artifact list
endpoints import these rules to compute ``can_share_link``.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass

from personal_agent.config import settings
from personal_agent.service import settings_edit
from personal_agent.service.auth import RequestUser

#: Upload media types a link can open (L5). Text, Markdown, CSV and JSON uploads are excluded.
SHAREABLE_UPLOAD_TYPES: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "application/pdf",
    }
)

#: The extension a served filename gets, by media type (L7).
_EXTENSION_BY_TYPE: dict[str, str] = {
    "text/html": "html",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "application/pdf": "pdf",
}

_TOKEN_BYTES = 32
#: 32 bytes as unpadded base64url: 43 characters.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_FILENAME_STEM = 80


@dataclass(frozen=True)
class NewToken:
    """A freshly drawn share token.

    Attributes:
        token: The unpadded base64url text that goes in the link URL. Shown once, never stored.
        token_hash: SHA-256 of the 32 raw bytes, the only form that Postgres stores.
    """

    token: str
    token_hash: bytes


def media_type(content_type: str) -> str:
    """Return the media type of a ``Content-Type`` value, without parameters, lowercased.

    Args:
        content_type: A content type such as ``"text/html; charset=utf-8"``.

    Returns:
        The bare media type, such as ``"text/html"``.
    """
    return content_type.split(";", 1)[0].strip().lower()


def is_link_creator(user: RequestUser) -> bool:
    """Return whether a request comes from a link creator, on a verified identity (L9).

    A creator is the owner (``settings_edit.is_owner``), or a user whose email is in
    ``share_link_creator_emails``. Both need ``gateway_auth_enabled``, so a dev-mode identity, which
    comes from a plaintext header or the owner fallback, never counts.

    Args:
        user: The resolved request identity.

    Returns:
        ``True`` for the owner or a listed creator, on a verified identity.
    """
    if settings_edit.is_owner(user):
        return True
    if not settings.gateway_auth_enabled:
        return False
    creators = {
        _normalised_email(email) for email in settings.share_link_creator_emails if email.strip()
    }
    return _normalised_email(user.email) in creators


def _normalised_email(email: str) -> str:
    """The email form that every creator comparison uses: no surrounding whitespace, lowercase."""
    return email.strip().lower()


@dataclass(frozen=True)
class ResolveCreators:
    """The link creators that the resolve statement binds (L9, owner decision of 2026-11-24).

    Each half uses the comparison of its create-path check, so a creator is a creator in both
    places for the same settings and email:

    Attributes:
        owner_email: ``agent_owner_email`` lowercased, as ``settings_edit.is_owner`` compares it;
            the statement matches it against ``lower(users.email)``. ``None`` when unset.
        listed_emails: ``share_link_creator_emails`` trimmed and lowercased, as
            :func:`is_link_creator` compares them; the statement matches them against
            ``lower(btrim(users.email))``.
    """

    owner_email: str | None
    listed_emails: tuple[str, ...]


def resolve_creators() -> ResolveCreators:
    """Return the creators for the resolve statement. None in dev mode, as for the create path.

    Returns:
        The owner email and the listed emails, both empty when ``gateway_auth_enabled`` is off.
    """
    if not settings.gateway_auth_enabled:
        return ResolveCreators(owner_email=None, listed_emails=())
    owner = settings.agent_owner_email
    listed = {
        _normalised_email(email) for email in settings.share_link_creator_emails if email.strip()
    }
    return ResolveCreators(
        owner_email=owner.lower() if owner else None, listed_emails=tuple(sorted(listed))
    )


def offers_share_links(user: RequestUser) -> bool:
    """Return whether this deployment lets this caller create links (``can_share_link``, L9).

    It also needs ``share_public_base_url``, so the PWA shows no Share button that would get 503.

    Args:
        user: The resolved request identity.

    Returns:
        ``True`` for a link creator on a deployment with a share base URL.
    """
    return bool(settings.share_public_base_url) and is_link_creator(user)


def is_shareable(*, item_type: str, content_type: str, upload_pending: bool) -> bool:
    """Return whether a link can open this item (L5).

    Args:
        item_type: The ``artifacts.type`` value.
        content_type: The ``artifacts.content_type`` value.
        upload_pending: The ``artifacts.upload_pending`` value.

    Returns:
        ``True`` for an HTML artifact, or for a completed upload of an image or PDF type.
    """
    kind = media_type(content_type)
    if item_type == "artifact":
        return kind == "text/html"
    if item_type == "upload":
        return not upload_pending and kind in SHAREABLE_UPLOAD_TYPES
    return False


def new_token() -> NewToken:
    """Draw a token from the operating system's secure random source (L3).

    Returns:
        The token text and the SHA-256 of its 32 raw bytes.
    """
    raw = secrets.token_bytes(_TOKEN_BYTES)
    token = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return NewToken(token=token, token_hash=hashlib.sha256(raw).digest())


def token_hash_of(token: str) -> bytes | None:
    """Return the stored hash for a presented token, or ``None`` when it is malformed.

    Only the canonical encoding of 32 bytes is accepted, so each link has exactly one token text.

    Args:
        token: The token text from the link URL.

    Returns:
        The SHA-256 of the decoded bytes, or ``None`` for any other shape.
    """
    if not _TOKEN_RE.fullmatch(token):
        return None
    raw = base64.urlsafe_b64decode(token + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
        return None
    return hashlib.sha256(raw).digest()


def served_content_type(*, content_type: str, is_snapshot: bool) -> str:
    """Return the ``Content-Type`` a link serves (L7).

    Args:
        content_type: The ``artifacts.content_type`` value, from an allowlist.
        is_snapshot: Whether the link serves an HTML snapshot.

    Returns:
        ``text/html; charset=utf-8`` for a snapshot, else the stored type.
    """
    return "text/html; charset=utf-8" if is_snapshot else content_type


def safe_filename(*, slug: str | None, title: str | None, content_type: str) -> str:
    """Build the ``Content-Disposition`` filename of a link (L7).

    The stem is the slug, else the title, with only ``[A-Za-z0-9._-]`` kept and cut to 80
    characters. An empty stem becomes ``artifact``. No raw user text reaches a header.

    Args:
        slug: The artifact slug, if any.
        title: The artifact title, if any.
        content_type: The type that the link serves.

    Returns:
        A filename such as ``my-report.html``.
    """
    extension = _EXTENSION_BY_TYPE.get(media_type(content_type), "bin")
    stem = _UNSAFE_FILENAME_CHARS.sub("", slug or title or "")[:_MAX_FILENAME_STEM]
    return f"{stem or 'artifact'}.{extension}"
