"""Commit-time served-envelope probe (FRE-512 / ADR-0089 D5).

After every artifact commit the backend issues one real GET through the full edge
path and records the envelope actually applied — extending the FRE-506 telemetry
family with the reframed "gate decision": envelope integrity, not a content
verdict. The probe consumes only status + headers (the body is never read) and is
never load-bearing: nothing it does can fail or block the commit beyond the
configured timeout.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from personal_agent.config import settings
from personal_agent.observability.artifact_envelope.verifier import (
    EnvelopeReport,
    classify_access_denied,
    verify_envelope,
)

log = structlog.get_logger(__name__)

_EVENT = "artifact_envelope_integrity"


def _service_token_headers() -> dict[str, str]:
    """CF Access service-token headers, when configured.

    The token must also be authorized on the artifacts Access app policy
    (terraform, ``personal_agent_secrets``) — until then the probe reports
    ``unverified_access_denied``.
    """
    if settings.cf_access_client_id and settings.cf_access_client_secret:
        return {
            "CF-Access-Client-Id": settings.cf_access_client_id,
            "CF-Access-Client-Secret": settings.cf_access_client_secret,
        }
    return {}


async def probe_served_envelope(
    *,
    public_url: str,
    artifact_id: str,
    slug: str,
    content_type: str,
    trace_id: str,
    session_id: str | None,
    user_id: str | None,
) -> None:
    """Probe the served artifact URL and emit ``artifact_envelope_integrity``.

    Severity encodes the verdict: a verified envelope logs at info; an envelope
    failure (CSP absent/incomplete, wrong MIME, missing nosniff) logs at
    **error** — the ADR-0089 D5 alarm condition; an Access denial or probe
    failure logs at warning (visible, distinct from the alarm).

    Args:
        public_url: The artifact's public Worker URL.
        artifact_id: Committed artifact id (ADR-0074 identity).
        slug: Artifact slug.
        content_type: The *committed* MIME type — decides whether the served
            MIME must be exactly HTML (``expect_html``).
        trace_id: Caller trace id.
        session_id: Caller session id, or None.
        user_id: Owning user id, or None.
    """
    identity: dict[str, Any] = {
        "trace_id": trace_id,
        "session_id": session_id,
        "user_id": user_id,
        "artifact_id": artifact_id,
        "slug": slug,
        "content_type": content_type,
        "public_url": public_url,
    }
    started = time.monotonic()
    try:
        timeout = float(settings.artifact_envelope_probe_timeout_s)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                "GET", public_url, headers=_service_token_headers()
            ) as response:
                status_code = int(response.status_code)
                header_pairs = list(response.headers.multi_items())
        # Headers only — the stream is closed without ever reading the body
        # (ADR-0089 D1/D5 scope boundary).
    except Exception as exc:
        log.warning(
            _EVENT,
            **identity,
            probe_status="probe_failed",
            error_message=str(exc),
            probe_duration_ms=int((time.monotonic() - started) * 1000),
        )
        return

    duration_ms = int((time.monotonic() - started) * 1000)

    if classify_access_denied(status_code, header_pairs):
        log.warning(
            _EVENT,
            **identity,
            probe_status="unverified_access_denied",
            http_status=status_code,
            probe_duration_ms=duration_ms,
        )
        return

    report: EnvelopeReport = verify_envelope(
        status_code,
        header_pairs,
        expect_html=content_type.lower().startswith("text/html"),
    )
    emit = log.info if report.envelope_ok else log.error
    emit(
        _EVENT,
        **identity,
        probe_status="verified",
        envelope_ok=report.envelope_ok,
        csp_present=report.csp_present,
        missing_directives=list(report.missing_directives),
        mismatched_directives=list(report.mismatched_directives),
        unexpected_directives=list(report.unexpected_directives),
        envelope_failures=list(report.failures),
        served_mime=report.served_mime,
        mime_ok=report.mime_ok,
        nosniff_ok=report.nosniff_ok,
        http_status=report.http_status,
        csp_header=report.csp_header,
        probe_duration_ms=duration_ms,
    )
