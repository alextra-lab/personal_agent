"""Pure, header-only verifier for the served artifact envelope (FRE-512, ADR-0089 D5).

The D1/D5 scope boundary holds *structurally*: every function here consumes only an
HTTP status code and response headers. Artifact bytes and generation prompts are
not reachable from these signatures, so no telemetry built on them can inspect or
persist content as a security verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from personal_agent.observability.artifact_envelope.spec import (
    EXPECTED_CSP_DIRECTIVES,
    FORBIDDEN_SCRIPT_MIMES,
)

Headers = Mapping[str, str] | Sequence[tuple[str, str]]


@dataclass(frozen=True)
class CspPolicy:
    """A parsed Content-Security-Policy header value.

    Attributes:
        directives: Directive name (lowercased) → set of value tokens. For a
            duplicated directive, the first occurrence wins (CSP semantics).
        duplicates: Names of directives that appeared more than once.
    """

    directives: Mapping[str, frozenset[str]]
    duplicates: tuple[str, ...]


@dataclass(frozen=True)
class EnvelopeReport:
    """The envelope-integrity verdict for one served artifact response.

    Attributes:
        envelope_ok: True iff no failure was detected.
        csp_present: Exactly one enforced CSP header was served.
        missing_directives: Expected directives absent from the served policy.
        mismatched_directives: Directives present with a different token set.
        unexpected_directives: Served directives not in the expected policy.
        served_mime: The raw Content-Type value, or None when absent.
        mime_ok: The MIME check passed (see ``verify_envelope``).
        nosniff_ok: ``X-Content-Type-Options: nosniff`` was served.
        http_status: The served HTTP status code.
        csp_header: The raw served CSP value (posture data, never bytes).
        failures: Stable failure codes, empty when ``envelope_ok``.
    """

    envelope_ok: bool
    csp_present: bool
    missing_directives: tuple[str, ...]
    mismatched_directives: tuple[str, ...]
    unexpected_directives: tuple[str, ...]
    served_mime: str | None
    mime_ok: bool
    nosniff_ok: bool
    http_status: int
    csp_header: str | None
    failures: tuple[str, ...]


def _as_pairs(headers: Headers) -> list[tuple[str, str]]:
    """Normalize headers to ``(lowercase_name, value)`` pairs."""
    items = headers.items() if isinstance(headers, Mapping) else headers
    return [(name.lower(), value) for name, value in items]


def _values(pairs: list[tuple[str, str]], name: str) -> list[str]:
    return [value for header, value in pairs if header == name]


def parse_csp(header_value: str) -> CspPolicy:
    """Parse one CSP header value into directives and duplicate names.

    Args:
        header_value: The raw header value, e.g. ``"default-src 'none'; ..."``.

    Returns:
        The parsed policy; for duplicated directives the first occurrence wins
        and the name is reported in ``duplicates``.
    """
    directives: dict[str, frozenset[str]] = {}
    duplicates: list[str] = []
    for part in header_value.split(";"):
        tokens = part.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        if name in directives:
            if name not in duplicates:
                duplicates.append(name)
            continue
        directives[name] = frozenset(tokens[1:])
    return CspPolicy(directives=directives, duplicates=tuple(duplicates))


def classify_access_denied(status_code: int, headers: Headers) -> bool:
    """Detect a Cloudflare Access denial (login redirect or challenge).

    Used by both the commit-time probe and the live verification CLI so the
    classification can never drift between them.

    Args:
        status_code: The served HTTP status.
        headers: The served response headers.

    Returns:
        True when the response is Access intercepting the request — i.e. the
        Worker was never reached — rather than a Worker-served response.
    """
    pairs = _as_pairs(headers)
    if 300 <= status_code < 400:
        for location in _values(pairs, "location"):
            host = urlsplit(location).hostname or ""
            if host == "cloudflareaccess.com" or host.endswith(".cloudflareaccess.com"):
                return True
        return False
    if status_code in (401, 403):
        return any(
            "cloudflare-access" in value.lower() for value in _values(pairs, "www-authenticate")
        )
    return False


def _parse_content_type(value: str) -> tuple[str, dict[str, str]] | None:
    """Split a Content-Type value into (type/subtype, params), or None if malformed."""
    if "," in value:
        return None  # comma-joined values are never a single valid media type
    media_type, _, raw_params = value.partition(";")
    media_type = media_type.strip().lower()
    if "/" not in media_type:
        return None
    params: dict[str, str] = {}
    if raw_params.strip():
        for chunk in raw_params.split(";"):
            key, sep, val = chunk.partition("=")
            if not sep:
                return None
            params[key.strip().lower()] = val.strip().strip('"').lower()
    return media_type, params


def _check_mime(served: str | None, *, expect_html: bool) -> tuple[bool, str | None]:
    """Return (mime_ok, failure_code) for the served Content-Type."""
    if served is None:
        return False, "wrong_mime"
    parsed = _parse_content_type(served)
    if parsed is None:
        return False, "wrong_mime"
    media_type, params = parsed
    if media_type in FORBIDDEN_SCRIPT_MIMES:
        return False, "executable_mime"
    if expect_html and (media_type != "text/html" or params != {"charset": "utf-8"}):
        return False, "wrong_mime"
    return True, None


def verify_envelope(status_code: int, headers: Headers, *, expect_html: bool) -> EnvelopeReport:
    """Verify a served artifact response against the ADR-0089 D2 envelope.

    Consumes only the status code and headers (D1/D5 scope boundary). The CSP
    comparison is exact-set: missing directives, value mismatches, *and*
    unexpected extras all fail — an extra directive widens the
    ``default-src 'none'`` fallback. Multiple CSP headers and duplicate
    directives fail as malformed rather than being normalized away.

    Args:
        status_code: The served HTTP status.
        headers: The served response headers (mapping or multi-value pairs).
        expect_html: True for HTML commits (served MIME must be exactly
            ``text/html; charset=utf-8``); False for other commits (served MIME
            must merely never be an executable-script type).

    Returns:
        The :class:`EnvelopeReport`; ``envelope_ok`` is True iff no check failed.
    """
    pairs = _as_pairs(headers)
    failures: list[str] = []

    if not (200 <= status_code < 300):
        failures.append("http_error")

    # ── CSP ──────────────────────────────────────────────────────────────────
    csp_values = _values(pairs, "content-security-policy")
    csp_present = len(csp_values) == 1
    csp_header: str | None = csp_values[0] if csp_values else None
    missing: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()

    if not csp_values:
        failures.append("missing_csp")
    elif len(csp_values) > 1:
        failures.append("multiple_csp_policies")
    else:
        policy = parse_csp(csp_values[0])
        if policy.duplicates:
            failures.append("duplicate_directive")
        missing = tuple(d for d in EXPECTED_CSP_DIRECTIVES if d not in policy.directives)
        mismatched = tuple(
            d
            for d, expected_tokens in EXPECTED_CSP_DIRECTIVES.items()
            if d in policy.directives and policy.directives[d] != expected_tokens
        )
        unexpected = tuple(d for d in policy.directives if d not in EXPECTED_CSP_DIRECTIVES)
        if missing:
            failures.append("csp_directive_missing")
        if mismatched:
            failures.append("csp_directive_mismatch")
        if unexpected:
            failures.append("csp_directive_unexpected")

    # ── MIME ─────────────────────────────────────────────────────────────────
    content_type_values = _values(pairs, "content-type")
    if len(content_type_values) == 1:
        served_mime: str | None = content_type_values[0]
        mime_ok, mime_failure = _check_mime(served_mime, expect_html=expect_html)
    elif not content_type_values:
        served_mime = None
        mime_ok, mime_failure = False, "wrong_mime"
    else:
        served_mime = ", ".join(content_type_values)
        mime_ok, mime_failure = False, "wrong_mime"
    if mime_failure is not None:
        failures.append(mime_failure)

    # ── nosniff ──────────────────────────────────────────────────────────────
    nosniff_ok = any(
        value.strip().lower() == "nosniff" for value in _values(pairs, "x-content-type-options")
    )
    if not nosniff_ok:
        failures.append("missing_nosniff")

    return EnvelopeReport(
        envelope_ok=not failures,
        csp_present=csp_present,
        missing_directives=missing,
        mismatched_directives=mismatched,
        unexpected_directives=unexpected,
        served_mime=served_mime,
        mime_ok=mime_ok,
        nosniff_ok=nosniff_ok,
        http_status=status_code,
        csp_header=csp_header,
        failures=tuple(failures),
    )
