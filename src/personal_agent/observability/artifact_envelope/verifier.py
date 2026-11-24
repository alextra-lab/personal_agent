"""Pure, header-only verifier for the served artifact envelope (FRE-512, ADR-0089 D5).

The D1/D5 scope boundary holds *structurally*: every function here consumes only an
HTTP status code and response headers. Artifact bytes and generation prompts are
not reachable from these signatures, so no telemetry built on them can inspect or
persist content as a security verdict. The one function that takes a body,
``verify_share_lib_response``, hashes a curated ``/lib/`` toolkit file (public,
pinned bytes), never an artifact.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from personal_agent.observability.artifact_envelope.spec import (
    EXECUTABLE_SCRIPT_MIMES,
    EXPECTED_FONT_MIMES,
    EXPECTED_STYLE_MIME,
    LIB_KIND_CSP_DIRECTIVE,
    SHARE_LIB_CORS_ORIGIN,
    SHARE_LIB_PROBE_PATH,
    SHARE_LIB_REQUIRED_HEADERS,
    SHARE_MIME_EXTENSIONS,
    SHARE_REQUIRED_HEADERS,
    LibAsset,
    ShareCspKind,
    expected_csp_directives,
    expected_share_csp_directives,
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


def _compare_csp(
    header_value: str, expected: Mapping[str, frozenset[str]]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], list[str]]:
    """Exact-set comparison of one CSP value against an expected directive set.

    Returns:
        ``(missing, mismatched, unexpected, failure_codes)``.
    """
    policy = parse_csp(header_value)
    failures: list[str] = []
    if policy.duplicates:
        failures.append("duplicate_directive")
    missing = tuple(d for d in expected if d not in policy.directives)
    mismatched = tuple(
        d
        for d, expected_tokens in expected.items()
        if d in policy.directives and policy.directives[d] != expected_tokens
    )
    unexpected = tuple(d for d in policy.directives if d not in expected)
    if missing:
        failures.append("csp_directive_missing")
    if mismatched:
        failures.append("csp_directive_mismatch")
    if unexpected:
        failures.append("csp_directive_unexpected")
    return missing, mismatched, unexpected, failures


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
    if media_type in EXECUTABLE_SCRIPT_MIMES:
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
        missing, mismatched, unexpected, csp_failures = _compare_csp(
            csp_values[0], expected_csp_directives()
        )
        failures.extend(csp_failures)

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


@dataclass(frozen=True)
class LibAssetReport:
    """The verification verdict for one served ``/lib/`` toolkit asset.

    Attributes:
        asset_ok: True iff no failure was detected.
        name: The library name (from the manifest).
        path: The asset path relative to ``/lib/``.
        kind: The asset kind (``script`` / ``style`` / ``font``).
        http_status: The served HTTP status code.
        served_mime: The raw Content-Type value, or None when absent.
        mime_ok: The served MIME matched the kind's requirement.
        nosniff_ok: ``X-Content-Type-Options: nosniff`` was served.
        csp_host_ok: The serving origin is admitted by the artifact CSP
            directive that governs this asset kind.
        failures: Stable failure codes, empty when ``asset_ok``.
    """

    asset_ok: bool
    name: str
    path: str
    kind: str
    http_status: int
    served_mime: str | None
    mime_ok: bool
    nosniff_ok: bool
    csp_host_ok: bool
    failures: tuple[str, ...]


def _check_lib_mime(served: str | None, asset: LibAsset) -> tuple[bool, str | None]:
    """Return (mime_ok, failure_code) for a served ``/lib/`` asset Content-Type.

    The polarity is inverse to the artifact rule: a ``script`` asset *must* serve
    an executable JS MIME; a ``style``/``font`` asset must serve its exact type.
    """
    if served is None:
        return False, "wrong_mime"
    parsed = _parse_content_type(served)
    if parsed is None:
        return False, "wrong_mime"
    media_type, _ = parsed
    if asset.kind == "script":
        if media_type not in EXECUTABLE_SCRIPT_MIMES:
            return False, "non_executable_script_mime"
        return True, None
    if asset.kind == "style":
        if media_type != EXPECTED_STYLE_MIME:
            return False, "wrong_mime"
        return True, None
    # font
    suffix = urlsplit(asset.path).path.rsplit(".", 1)
    ext = f".{suffix[-1]}" if len(suffix) == 2 else ""
    if media_type != EXPECTED_FONT_MIMES.get(ext):
        return False, "wrong_mime"
    return True, None


def verify_lib_asset(
    status_code: int, headers: Headers, *, asset: LibAsset, origin: str
) -> LibAssetReport:
    """Verify a served ``/lib/`` toolkit asset (ADR-0089 Addendum A · A7).

    Consumes only the status code and headers (D1/D5 scope boundary). Asserts the
    asset is reachable (2xx), serves the correct MIME for its kind, carries
    ``nosniff``, and is served from an origin the artifact CSP admits for that
    kind — i.e. genuinely loadable *under the artifact CSP*, not merely same-host.

    Args:
        status_code: The served HTTP status.
        headers: The served response headers (mapping or multi-value pairs).
        asset: The manifest entry being verified.
        origin: The serving origin (e.g. ``https://artifacts.example.com``).

    Returns:
        The :class:`LibAssetReport`; ``asset_ok`` is True iff no check failed.
    """
    pairs = _as_pairs(headers)
    failures: list[str] = []

    if not (200 <= status_code < 300):
        failures.append("http_error")

    content_type_values = _values(pairs, "content-type")
    if len(content_type_values) == 1:
        served_mime: str | None = content_type_values[0]
    elif not content_type_values:
        served_mime = None
    else:
        served_mime = ", ".join(content_type_values)
    mime_ok, mime_failure = _check_lib_mime(served_mime, asset)
    if mime_failure is not None:
        failures.append(mime_failure)

    nosniff_ok = any(
        value.strip().lower() == "nosniff" for value in _values(pairs, "x-content-type-options")
    )
    if not nosniff_ok:
        failures.append("missing_nosniff")

    directive = LIB_KIND_CSP_DIRECTIVE[asset.kind]
    csp_host_ok = origin in expected_csp_directives()[directive]
    if not csp_host_ok:
        failures.append("csp_host_not_allowed")

    return LibAssetReport(
        asset_ok=not failures,
        name=asset.name,
        path=asset.path,
        kind=asset.kind,
        http_status=status_code,
        served_mime=served_mime,
        mime_ok=mime_ok,
        nosniff_ok=nosniff_ok,
        csp_host_ok=csp_host_ok,
        failures=tuple(failures),
    )


# ── Share hostname profile (ADR-0153 Amendment 1 L7 · FRE-1783) ───────────────

#: ``inline; filename="<stem>.<ext>"`` with the gateway's stem rule (L7).
_SHARE_DISPOSITION_RE = re.compile(
    r'inline; filename="(?P<stem>[A-Za-z0-9._-]{1,80})\.(?P<ext>[a-z]+)"'
)


@dataclass(frozen=True)
class ShareEnvelopeReport:
    """The verdict for one response of the share hostname (ADR-0153 L7, V-L4).

    Attributes:
        envelope_ok: True iff no failure was detected.
        kind: The CSP kind the caller declared for this response.
        http_status: The served HTTP status code.
        expected_status: The status the caller expected.
        csp_header: The raw served CSP value, or None.
        missing_directives: Expected directives absent from the served policy.
        mismatched_directives: Directives present with a different token set.
        unexpected_directives: Served directives not in the expected policy.
        missing_headers: Required share headers that were not served.
        wrong_headers: Required share headers served with another value, or twice.
        set_cookie_present: A ``Set-Cookie`` header was served.
        served_mime: The raw Content-Type value, or None when absent.
        mime_ok: The served media type is one of the kind's types.
        content_disposition: The raw Content-Disposition value, or None.
        failures: Stable failure codes, empty when ``envelope_ok``.
    """

    envelope_ok: bool
    kind: ShareCspKind
    http_status: int
    expected_status: int
    csp_header: str | None
    missing_directives: tuple[str, ...]
    mismatched_directives: tuple[str, ...]
    unexpected_directives: tuple[str, ...]
    missing_headers: tuple[str, ...]
    wrong_headers: tuple[str, ...]
    set_cookie_present: bool
    served_mime: str | None
    mime_ok: bool
    content_disposition: str | None
    failures: tuple[str, ...]


@dataclass(frozen=True)
class ShareLibReport:
    """The verdict for the share hostname's ``/lib/`` toolkit file (V-L5).

    Attributes:
        lib_ok: True iff no failure was detected.
        http_status: The served HTTP status code.
        served_mime: The raw Content-Type value, or None when absent.
        sri_ok: The body's digest equals the pinned SRI value.
        failures: Stable failure codes, empty when ``lib_ok``.
    """

    lib_ok: bool
    http_status: int
    served_mime: str | None
    sri_ok: bool
    failures: tuple[str, ...]


def _header_tokens(value: str) -> list[str]:
    """Split a comma-list header value into sorted lowercase tokens."""
    return sorted(token.strip().lower() for token in value.split(",") if token.strip())


def _check_required_headers(
    pairs: list[tuple[str, str]], required: Mapping[str, tuple[str, ...]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(missing, wrong)`` header names against exact token lists.

    A header served twice, or with a repeated, missing or extra token, is wrong.
    """
    missing: list[str] = []
    wrong: list[str] = []
    for name, tokens in required.items():
        values = _values(pairs, name)
        if not values:
            missing.append(name)
        elif len(values) > 1 or _header_tokens(values[0]) != sorted(tokens):
            wrong.append(name)
    return tuple(missing), tuple(wrong)


def _has_repeated_csp_value(header_value: str) -> bool:
    """True when one directive lists the same value token twice."""
    for part in header_value.split(";"):
        tokens = part.split()[1:]
        if len(tokens) != len(set(tokens)):
            return True
    return False


def _single(pairs: list[tuple[str, str]], name: str) -> str | None:
    """The one value of ``name``; several values are joined so that they fail parsing."""
    values = _values(pairs, name)
    return ", ".join(values) if values else None


def verify_share_response(
    status_code: int,
    headers: Headers,
    *,
    kind: ShareCspKind,
    share_origin: str,
    expected_status: int,
) -> ShareEnvelopeReport:
    """Verify one share-hostname response against ADR-0153 L7 (V-L4).

    Consumes only the status code and headers. The caller declares the kind;
    it is never inferred from the response, so a wrong policy on the right MIME
    still fails. The CSP comparison is exact-set, as for the artifact host, and
    also fails a repeated value token.

    Args:
        status_code: The served HTTP status.
        headers: The served response headers (mapping or multi-value pairs).
        kind: ``html`` (snapshot and error pages), ``svg``, or ``media``.
        share_origin: The share hostname origin, e.g. ``https://share.example.com``.
        expected_status: 200 for a link, or 404 / 405 / 410 / 503 for an error page.

    Returns:
        The :class:`ShareEnvelopeReport`; ``envelope_ok`` is True iff no check failed.
    """
    pairs = _as_pairs(headers)
    failures: list[str] = []

    if status_code != expected_status:
        failures.append("unexpected_status")

    # ── CSP ──────────────────────────────────────────────────────────────────
    csp_values = _values(pairs, "content-security-policy")
    csp_header = csp_values[0] if csp_values else None
    missing_d: tuple[str, ...] = ()
    mismatched_d: tuple[str, ...] = ()
    unexpected_d: tuple[str, ...] = ()
    if not csp_values:
        failures.append("missing_csp")
    elif len(csp_values) > 1:
        failures.append("multiple_csp_policies")
    else:
        missing_d, mismatched_d, unexpected_d, csp_failures = _compare_csp(
            csp_values[0], expected_share_csp_directives(kind, share_origin)
        )
        failures.extend(csp_failures)
        if _has_repeated_csp_value(csp_values[0]):
            failures.append("duplicate_csp_value")

    # ── Share headers and cookies ────────────────────────────────────────────
    missing_h, wrong_h = _check_required_headers(pairs, SHARE_REQUIRED_HEADERS)
    if missing_h:
        failures.append("missing_share_header")
    if wrong_h:
        failures.append("wrong_share_header")
    set_cookie_present = bool(_values(pairs, "set-cookie"))
    if set_cookie_present:
        failures.append("set_cookie_present")

    # ── MIME ─────────────────────────────────────────────────────────────────
    served_mime = _single(pairs, "content-type")
    parsed = _parse_content_type(served_mime) if served_mime is not None else None
    allowed = SHARE_MIME_EXTENSIONS[kind]
    mime_ok = False
    if parsed is None:
        failures.append("wrong_mime")
    elif parsed[0] in EXECUTABLE_SCRIPT_MIMES:
        failures.append("executable_mime")
    elif parsed[0] not in allowed or (kind == "html" and parsed[1] != {"charset": "utf-8"}):
        failures.append("wrong_mime")
    else:
        mime_ok = True

    # ── Content-Disposition (a served link only) ─────────────────────────────
    disposition = _single(pairs, "content-disposition")
    if expected_status == 200:
        match = _SHARE_DISPOSITION_RE.fullmatch(disposition or "")
        expected_ext = allowed.get(parsed[0]) if parsed is not None else None
        if match is None or match.group("ext") != expected_ext:
            failures.append("bad_content_disposition")

    return ShareEnvelopeReport(
        envelope_ok=not failures,
        kind=kind,
        http_status=status_code,
        expected_status=expected_status,
        csp_header=csp_header,
        missing_directives=missing_d,
        mismatched_directives=mismatched_d,
        unexpected_directives=unexpected_d,
        missing_headers=missing_h,
        wrong_headers=wrong_h,
        set_cookie_present=set_cookie_present,
        served_mime=served_mime,
        mime_ok=mime_ok,
        content_disposition=disposition,
        failures=tuple(failures),
    )


def _sri_matches(body: bytes, expected_sri: str) -> bool:
    """True when ``body`` hashes to the ``"<algo>-<base64>"`` value."""
    algo, _, _ = expected_sri.partition("-")
    if algo not in {"sha256", "sha384", "sha512"}:
        return False
    digest = base64.b64encode(hashlib.new(algo, body).digest()).decode("ascii")
    return f"{algo}-{digest}" == expected_sri


def verify_share_lib_response(
    status_code: int, headers: Headers, body: bytes, *, expected_sri: str
) -> ShareLibReport:
    """Verify the share hostname's ``/lib/`` toolkit file (V-L5).

    The bytes must match the pinned SRI value from the substitution map. The
    headers are the share ``/lib/`` set: ``Access-Control-Allow-Origin: *`` and
    no other CORS header, ``Cache-Control: private, no-store``, ``nosniff``, an
    executable script MIME, no CSP, and no cookie.

    Args:
        status_code: The served HTTP status.
        headers: The served response headers.
        body: The served toolkit bytes (public and pinned, never an artifact).
        expected_sri: The pinned ``"sha384-…"`` value.

    Returns:
        The :class:`ShareLibReport`; ``lib_ok`` is True iff no check failed.
    """
    pairs = _as_pairs(headers)
    failures: list[str] = []

    if status_code != 200:
        failures.append("http_error")
    sri_ok = _sri_matches(body, expected_sri)
    if not sri_ok:
        failures.append("sri_mismatch")

    served_mime = _single(pairs, "content-type")
    probe_asset = LibAsset(name="katex", path=SHARE_LIB_PROBE_PATH, kind="script")
    _, mime_failure = _check_lib_mime(served_mime, probe_asset)
    if mime_failure is not None:
        failures.append(mime_failure)

    missing_h, wrong_h = _check_required_headers(pairs, SHARE_LIB_REQUIRED_HEADERS)
    if missing_h:
        failures.append("missing_share_header")
    if wrong_h:
        failures.append("wrong_share_header")
    if _values(pairs, "access-control-allow-origin") != [SHARE_LIB_CORS_ORIGIN]:
        failures.append("wrong_cors")
    if any(
        name.startswith("access-control-") and name != "access-control-allow-origin"
        for name, _ in pairs
    ):
        failures.append("unexpected_cors_header")
    if _values(pairs, "content-security-policy"):
        failures.append("unexpected_csp")
    if _values(pairs, "set-cookie"):
        failures.append("set_cookie_present")

    return ShareLibReport(
        lib_ok=not failures,
        http_status=status_code,
        served_mime=served_mime,
        sri_ok=sri_ok,
        failures=tuple(failures),
    )
