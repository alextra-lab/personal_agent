"""Shared Cloudflare Access service-token headers.

A service token lets a backend caller authenticate to a Cloudflare
Access-protected origin (the artifacts Worker, ``/lib/`` shelf, served
artifact URLs) non-interactively, instead of a human SSO session. Both the
commit-time envelope probe (``observability/artifact_envelope/probe.py``) and
the artifact export endpoint (``service/artifacts_router.py``, FRE-530) need
the same header pair, so it lives here once.

The token must also be authorized on the artifacts Access app policy
(terraform, ``personal_agent_secrets``) — until then an authenticated fetch
is rejected at the edge.
"""

from __future__ import annotations

from personal_agent.config import settings


def cf_access_service_token_headers() -> dict[str, str]:
    """Return CF Access service-token headers, when both halves are configured.

    Reads ``settings`` at call time so a test (or a live config reload) that
    swaps the credentials is honoured. Returns an empty mapping when either
    half is unset, so callers can spread it unconditionally.

    Returns:
        ``{"CF-Access-Client-Id": ..., "CF-Access-Client-Secret": ...}`` when
        both ``cf_access_client_id`` and ``cf_access_client_secret`` are set,
        else an empty dict.
    """
    if settings.cf_access_client_id and settings.cf_access_client_secret:
        return {
            "CF-Access-Client-Id": settings.cf_access_client_id,
            "CF-Access-Client-Secret": settings.cf_access_client_secret,
        }
    return {}
