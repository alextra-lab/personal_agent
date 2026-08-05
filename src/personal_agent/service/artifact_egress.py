"""Route artifacts-origin requests through the internal Caddy egress block.

Replaces ``service/cf_service_token.py`` (deleted by ADR-0132 D1). Where that
module handed the application a Cloudflare Access service token to attach, this
one hands it a URL: an artifacts-origin URL is rewritten onto the internal Caddy
egress base, and Caddy injects the credential on the way out. The application
therefore holds no outbound Cloudflare credential at all.

The rewrite preserves path, query and fragment — only scheme and authority move.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from personal_agent.config import settings


def to_artifacts_egress_url(url: str, *, egress_base_url: str | None = None) -> str:
    """Rewrite an artifacts-origin URL onto the internal Caddy egress base.

    Args:
        url: The public artifacts-origin URL to fetch.
        egress_base_url: Egress base to use. Defaults to
            ``settings.artifacts_egress_base_url``; read at call time so a test
            or a live config reload is honoured.

    Returns:
        The URL to actually request. Returned unchanged when no egress base is
        configured — deployments without a Cloudflare barrier (local, eval)
        address the origin directly, which is the same single custody mode
        rather than a second credential path.
    """
    base = egress_base_url if egress_base_url is not None else settings.artifacts_egress_base_url
    if not base:
        return url

    target = urlparse(url)
    egress = urlparse(base.rstrip("/"))
    return urlunparse(
        (
            egress.scheme or target.scheme,
            egress.netloc,
            target.path,
            target.params,
            target.query,
            target.fragment,
        )
    )
